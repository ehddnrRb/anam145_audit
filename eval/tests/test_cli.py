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
    assert (kw["repeat"], kw["jobs"], kw["timeout_s"]) == (1, 2, 1800)
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
