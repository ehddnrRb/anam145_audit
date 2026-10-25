import json
import sys
import types
from pathlib import Path

import pytest

import cli

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL / "datasets" / "holdout"))
import validate  # noqa: E402

DATASET = EVAL / "datasets/scabench/datasets/curated-2025-08-18/curated-2025-08-18.json"


@pytest.fixture
def fake_run(monkeypatch, tmp_path):
    calls = []
    mod = types.ModuleType("runner.run")

    def run(*a, **kw):
        calls.append((a, kw))
        return tmp_path

    mod.run = run
    monkeypatch.setitem(sys.modules, "runner.run", mod)
    return calls


def test_holdout_refused_without_flag(fake_run, capsys, tmp_path):
    rc = cli.main(["run", "--agent", "baseline", "--dataset", "holdout", "--out", str(tmp_path)])
    assert rc == 2
    assert cli.HOLDOUT_WARNING in capsys.readouterr().out
    assert fake_run == []


def test_holdout_with_flag_warns_and_continues(fake_run, capsys, tmp_path):
    rc = cli.main(["run", "--agent", "baseline", "--dataset", "holdout", "--out", str(tmp_path),
                   "--i-understand-holdout", "--judge", "mock"])
    assert rc == 0
    assert cli.HOLDOUT_WARNING in capsys.readouterr().err
    (a, kw), = fake_run
    assert a == ("baseline", "holdout", tmp_path)
    assert kw["allow_holdout"] is True


def test_warning_text_exact():
    assert cli.HOLDOUT_WARNING == ("holdout 셋은 마일스톤 평가용입니다. 튜닝에 쓰면 오염됩니다. "
                                   "진행하려면 --i-understand-holdout 을 붙이세요.")


def test_run_args_passed_through(fake_run, tmp_path):
    rc = cli.main(["run", "--agent", "baseline", "--dataset", "scabench", "--out", str(tmp_path),
                   "--projects", "a, b", "--suite", "regression", "--repeat", "3", "--jobs", "4",
                   "--timeout", "60", "--judge", "mock", "--judge-model", "gpt-x",
                   "--baseline-mode", "precomputed", "--model", "m1"])
    assert rc == 0
    (a, kw), = fake_run
    assert kw == dict(projects=["a", "b"], suite="regression", repeat=3, jobs=4, timeout_s=60,
                      judge="mock", judge_model="gpt-x",
                      agent_opts={"model": "m1", "mode": "precomputed"}, allow_holdout=False)


def test_run_defaults(fake_run, tmp_path):
    cli.main(["run", "--agent", "attacktrail", "--dataset", "scabench", "--out", str(tmp_path)])
    (_, kw), = fake_run
    assert kw["judge"] == "nethermind" and kw["judge_model"] == "o4-mini"
    # D-24: no --timeout means "the agent's default" (attacktrail 10800 s, others 1800 s), resolved in the runner
    assert (kw["repeat"], kw["jobs"], kw["timeout_s"]) == (1, 2, None)
    assert kw["projects"] is None and kw["agent_opts"] == {}


@pytest.mark.parametrize("argv", [
    ["run", "--agent", "nope", "--dataset", "scabench", "--out", "x"],
    ["run", "--agent", "baseline", "--dataset", "other", "--out", "x"],
    ["run", "--agent", "baseline", "--dataset", "scabench"],
    ["run", "--agent", "baseline", "--dataset", "scabench", "--out", "x", "--judge", "gpt"],
    ["compare", "only_one"],
    ["render"],
])
def test_bad_args_exit_2(argv):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2


def test_compare_and_render_parse():
    p = cli.build_parser()
    a = p.parse_args(["compare", "runs/a", "runs/b"])
    assert (a.cmd, a.run_a, a.run_b) == ("compare", Path("runs/a"), Path("runs/b"))
    r = p.parse_args(["render", "runs/a"])
    assert (r.cmd, r.run) == ("render", Path("runs/a"))


def test_compare_refuses_single_mock(tmp_path, monkeypatch):
    for name, judge in (("a", "mock"), ("b", "nethermind")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "scores.json").write_text(json.dumps({"judge": judge}), encoding="utf-8")
    mod = types.ModuleType("report.compare")
    mod.compare = lambda a, b: pytest.fail("compare must not be called")
    monkeypatch.setitem(sys.modules, "report.compare", mod)
    assert cli.main(["compare", str(tmp_path / "a"), str(tmp_path / "b")]) == 1


def test_scabench_items_match_holdout_shape():
    items = json.loads(DATASET.read_text(encoding="utf-8"))
    nonempty = [x for x in items if all(c["commit"] for c in x["codebases"])]
    assert len(items) == 31 and len(nonempty) == 29
    # 29 중 5개는 commit='main'(브랜치), 3개는 7자 짧은 sha 다. holdout 은 40자 sha 만 받는다.
    ok = [x for x in items if all(validate.SHA40.match(c["commit"]) for c in x["codebases"])]
    assert len(ok) == 21
    for x in ok:
        validate.Project.model_validate(x)


def test_validate_rejects_empty_commit_and_template(tmp_path):
    items = json.loads(DATASET.read_text(encoding="utf-8"))
    empty = next(x for x in items if not x["codebases"][0]["commit"])
    f = tmp_path / f"{empty['project_id']}.json"
    f.write_text(json.dumps(empty), encoding="utf-8")
    assert any("commit" in e for e in validate.validate_file(f))
    t = tmp_path / "TEMPLATE.json"
    t.write_text((EVAL / "datasets/holdout/TEMPLATE.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert validate.validate_file(t)
    assert validate.main(tmp_path) == 1


# ---------------------------------------------------------------------------------------------
# D-23: --effort
# ---------------------------------------------------------------------------------------------

def test_effort_choices_match_scan_py():
    from adapters.attacktrail import load_scan_module
    assert cli.EFFORTS == load_scan_module().EFFORTS == ("high", "xhigh", "max")      # D-28 added max


def test_effort_reaches_the_attacktrail_agent_options(fake_run, tmp_path):
    rc = cli.main(["run", "--agent", "attacktrail", "--dataset", "scabench", "--out", str(tmp_path),
                   "--effort", "xhigh", "--model", "m1"])
    assert rc == 0
    (_, kw), = fake_run
    assert kw["agent_opts"] == {"model": "m1", "effort": "xhigh"}


@pytest.mark.parametrize("agent", ["attacktrail-cli", "baseline"])
def test_effort_is_refused_for_agents_that_do_not_send_it(fake_run, tmp_path, agent):
    # the CLI adapter calls `claude -p --model ...` and sends no effort; accepting the flag would record a setting
    # that never reached the model
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--agent", agent, "--dataset", "scabench", "--out", str(tmp_path), "--effort", "high"])
    assert "--effort" in str(e.value.code) and fake_run == []


@pytest.mark.parametrize("bad", ["medium", "low", "HIGH", "maximum"])
def test_effort_accepts_only_high_xhigh_and_max(bad, tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--agent", "attacktrail", "--dataset", "scabench", "--out", str(tmp_path), "--effort", bad])
    assert e.value.code == 2


def test_rescore_passes_model_and_effort_to_the_agent(monkeypatch, tmp_path):
    # `rescore` never runs the agent; it builds one only so the manifest can record these (see the --help text).
    # This checks that the options reach get_agent's agent_opts. What the manifest then shows is the agent's job.
    seen = []
    mod = types.ModuleType("runner.run")
    mod.rescore = lambda run, agent, dataset, **kw: seen.append((agent, kw)) or tmp_path
    monkeypatch.setitem(sys.modules, "runner.run", mod)
    assert cli.main(["rescore", str(tmp_path), "--agent", "attacktrail", "--dataset", "scabench", "--judge", "mock",
                     "--model", "m1", "--effort", "xhigh"]) == 0
    assert seen[0][1]["agent_opts"] == {"model": "m1", "effort": "xhigh"}
    seen.clear()
    cli.main(["rescore", str(tmp_path), "--agent", "baseline", "--dataset", "scabench", "--judge", "mock"])
    assert seen[0][1]["agent_opts"] == {}                    # no flags: same as before (the old code passed None)


def test_timeout_must_be_positive_when_given(tmp_path, capsys):
    assert cli.main(["run", "--agent", "attacktrail", "--dataset", "scabench", "--out", str(tmp_path),
                     "--timeout", "0"]) == 2
    assert "--timeout" in capsys.readouterr().err


# ---------------------------------------------------------------------------------------------
# D-28: --effort max, --max-tokens
# ---------------------------------------------------------------------------------------------

def test_effort_max_reaches_the_attacktrail_agent_options(fake_run, tmp_path):
    rc = cli.main(["run", "--agent", "attacktrail", "--dataset", "scabench", "--out", str(tmp_path), "--effort", "max"])
    assert rc == 0
    (_, kw), = fake_run
    assert kw["agent_opts"] == {"effort": "max"}


def test_max_tokens_limit_matches_scan_py_and_the_adapter():
    from adapters.attacktrail import MAX_OUTPUT_TOKENS, load_scan_module
    assert cli.MAX_TOKENS_LIMIT == load_scan_module().MAX_TOKENS_LIMIT == MAX_OUTPUT_TOKENS == 128000


def test_max_tokens_reaches_the_attacktrail_agent_options_as_an_int(fake_run, tmp_path):
    rc = cli.main(["run", "--agent", "attacktrail", "--dataset", "scabench", "--out", str(tmp_path),
                   "--effort", "max", "--max-tokens", "128000", "--model", "claude-opus-5-5"])
    assert rc == 0
    (_, kw), = fake_run
    assert kw["agent_opts"] == {"model": "claude-opus-5-5", "effort": "max", "max_tokens": 128000}
    fake_run.clear()
    cli.main(["run", "--agent", "attacktrail", "--dataset", "scabench", "--out", str(tmp_path)])
    assert fake_run[0][1]["agent_opts"] == {}                  # not given: scan.py's own default (64000) applies


@pytest.mark.parametrize("agent", ["attacktrail-cli", "baseline"])
def test_max_tokens_is_refused_for_agents_that_do_not_send_it(fake_run, tmp_path, agent):
    # same rule as --effort: `claude -p` is called with --model only, so the flag would record a value that never reached it
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--agent", agent, "--dataset", "scabench", "--out", str(tmp_path), "--max-tokens", "64000"])
    assert "--max-tokens" in str(e.value.code) and fake_run == []


@pytest.mark.parametrize("bad", ["0", "-1", "128001", "999999", "abc", "1.5", ""])
def test_max_tokens_outside_1_to_128000_is_an_argparse_error(fake_run, tmp_path, capsys, bad):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--agent", "attacktrail", "--dataset", "scabench", "--out", str(tmp_path), "--max-tokens", bad])
    assert e.value.code == 2 and "--max-tokens" in capsys.readouterr().err and fake_run == []


@pytest.mark.parametrize("ok", ["1", "64000", "128000"])
def test_max_tokens_bounds_are_inclusive(fake_run, tmp_path, ok):
    assert cli.main(["run", "--agent", "attacktrail", "--dataset", "scabench", "--out", str(tmp_path),
                     "--max-tokens", ok]) == 0
    assert fake_run[0][1]["agent_opts"] == {"max_tokens": int(ok)}


def test_rescore_passes_max_tokens_to_the_agent_for_the_manifest(monkeypatch, tmp_path):
    # `rescore` records it only (the agent never runs again); this checks that the option reaches agent_opts
    seen = []
    mod = types.ModuleType("runner.run")
    mod.rescore = lambda run, agent, dataset, **kw: seen.append((agent, kw)) or tmp_path
    monkeypatch.setitem(sys.modules, "runner.run", mod)
    assert cli.main(["rescore", str(tmp_path), "--agent", "attacktrail", "--dataset", "scabench", "--judge", "mock",
                     "--effort", "max", "--max-tokens", "128000"]) == 0
    assert seen[0][1]["agent_opts"] == {"effort": "max", "max_tokens": 128000}
    with pytest.raises(SystemExit) as e:                      # the CLI adapter sends none, so rescore refuses it too
        cli.main(["rescore", str(tmp_path), "--agent", "attacktrail-cli", "--dataset", "scabench", "--judge", "mock",
                  "--max-tokens", "64000"])
    assert "--max-tokens" in str(e.value.code)
