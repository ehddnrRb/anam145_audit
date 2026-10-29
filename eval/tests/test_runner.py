import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import checkout, dataset, executor, manifest  # noqa: E402
from runner.checkout import checkout_project, safe_rmtree  # noqa: E402


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def two_commit_repo(tmp_path):
    repo = tmp_path / "origin"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "a.txt").write_text("one")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "c1", cwd=repo)
    first = git("rev-parse", "HEAD", cwd=repo)
    (repo / "a.txt").write_text("two")
    git("commit", "-q", "-am", "c2", cwd=repo)
    second = git("rev-parse", "HEAD", cwd=repo)
    # unencoded file:// url so --depth is honoured (plain local paths ignore it)
    return "file:///" + repo.as_posix().lstrip("/"), first, second


def proj(url, commit, pid="demo"):
    return {"project_id": pid, "codebases": [{"repo_url": url, "commit": commit}]}


def test_checkout_first_commit_needs_unshallow_then_reuse(tmp_path, two_commit_repo):
    url, first, _ = two_commit_repo
    src = tmp_path / "sources"
    r = checkout_project(proj(url, first), src, depth=1)
    assert r.status == "ok", r.error
    assert r.head == first and r.unshallowed and r.pinned
    assert (src / "demo" / "a.txt").read_text() == "one"
    r2 = checkout_project(proj(url, first[:7]), src, depth=1)  # short sha, existing correct checkout reused
    assert r2.status == "ok" and r2.head == first and not r2.unshallowed


def test_checkout_unpinned_ref_marked(tmp_path, two_commit_repo):
    url, _, second = two_commit_repo
    r = checkout_project(proj(url, "main"), tmp_path / "sources", depth=1)
    assert r.status == "ok" and r.head == second and r.pinned is False


def test_checkout_failures(tmp_path, two_commit_repo):
    url, _, _ = two_commit_repo
    src = tmp_path / "sources"
    r = checkout_project(proj(url, ""), src)
    assert r.status == "pinned_commit_missing" and r.path is None
    assert not src.exists()
    assert checkout_project(proj(url, "f" * 40), src, depth=1).status == "checkout_failed"
    assert checkout_project(proj(str(tmp_path / "nope"), "f" * 40, pid="x"), src).status == "clone_failed"
    with pytest.raises(ValueError):
        checkout_project(proj(url, "f" * 40, pid="../evil"), src)


def test_safe_rmtree(tmp_path):
    root = tmp_path / "root"
    target = root / "t" / "deep"
    target.mkdir(parents=True)
    ro = target / "ro.txt"
    ro.write_text("x")
    os.chmod(ro, stat.S_IREAD)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError):
        safe_rmtree(outside, root)
    with pytest.raises(ValueError):
        safe_rmtree(root / ".." / "outside", root)
    with pytest.raises(ValueError):
        safe_rmtree(root, root)
    safe_rmtree(root / "t", root)
    assert not (root / "t").exists() and outside.exists()


def test_dataset_and_exclusions():
    items, info = dataset.load_dataset("scabench")
    assert len(items) == 31
    assert info["sha"] == "eec0020939a47bbc06a98a7348a90908b96f4b4d"
    excluded = {p["project_id"] for p in items if dataset.excluded_reason(p) == "pinned_commit_missing"}
    assert excluded == {"code4rena_initia-move_2025_04", "code4rena_starknet-perpetual_2025_06"}
    with pytest.raises(ValueError):
        dataset.load_dataset("nope")


def test_holdout_guard():
    with pytest.raises(dataset.HoldoutNotAllowed):
        dataset.load_dataset("holdout")


def test_run_guards(monkeypatch, tmp_path):
    from runner import run as run_mod
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        run_mod.run("baseline", "scabench", tmp_path / "out")
    with pytest.raises(dataset.HoldoutNotAllowed):
        run_mod.run("baseline", "holdout", tmp_path / "out", judge="mock")
    assert not (tmp_path / "out").exists()


def test_agent_env_is_scoped(monkeypatch):
    from runner.run import agent_env
    monkeypatch.setenv("OPENAI_API_KEY", "o-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-test")
    monkeypatch.setenv("SOMETHING_ELSE", "1")
    monkeypatch.setenv("ATTACKTRAIL_ANTHROPIC_API_KEY", "alias-test")   # D-24 alias wins over the plain key
    env = agent_env("attacktrail")
    assert env.get("ANTHROPIC_API_KEY") == "alias-test" and "OPENAI_API_KEY" not in env and "SOMETHING_ELSE" not in env
    assert "ATTACKTRAIL_ANTHROPIC_API_KEY" not in env
    monkeypatch.delenv("ATTACKTRAIL_ANTHROPIC_API_KEY")                  # without the alias the plain key is used
    assert agent_env("attacktrail").get("ANTHROPIC_API_KEY") == "a-test"


def test_executor_env_and_timeout():
    env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "ONLY_ME": "1"}
    r = executor.LocalExecutor().run([sys.executable, "-c", "import os;print(sorted(os.environ))"], None, env, 60)
    assert r.returncode == 0 and "ONLY_ME" in r.stdout and "'PATH'" not in r.stdout
    r = executor.LocalExecutor().run([sys.executable, "-c", "import time;time.sleep(30)"], None, env, 1)
    assert r.timed_out and r.returncode is None and r.seconds < 20
    with pytest.raises(NotImplementedError):
        executor.DockerExecutor().run(["x"], None, {}, 1)


def test_manifest_never_holds_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-thisisnotarealkey1234567890")
    with pytest.raises(ValueError):
        manifest.build_manifest(env={"A": "b"})
    m = manifest.build_manifest(run_id="r", projects=[{"agent_error": "bad key sk-thisisnotarealkey1234567890"}])
    path = manifest.write_manifest(tmp_path, m)
    text = path.read_text(encoding="utf-8")
    assert "sk-thisisnotarealkey" not in text and "[redacted]" in text
    assert m["judge"] is None and m["denominators_note"]
    assert manifest.judge_block("mock", "o4-mini")["scorer_commit"] == "a62a15d1ea254158dd5cda4bea8ce8cf5791fb27"


def test_validate_name_rejects_windows_aliases():
    for bad in ("x.", "CON", "nul", "nul.txt", "COM1", "lpt9.json", "a/b", "../x"):
        with pytest.raises(ValueError):
            checkout.validate_name(bad)
    assert checkout.validate_name("sherlock_axion_2025_01") == "sherlock_axion_2025_01"


def test_git_env_has_no_api_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "o-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-test")
    env = checkout.git_env()
    assert not any(k.endswith("_API_KEY") for k in env)
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_LFS_SKIP_SMUDGE"] == "1"


def test_checkout_never_materializes_symlinks(tmp_path):
    repo = tmp_path / "origin"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input="../../outside-secret",
                          check=True, capture_output=True, text=True).stdout.strip()
    git("update-index", "--add", "--cacheinfo", f"120000,{blob},Vault.sol", cwd=repo)
    git("commit", "-q", "-m", "link", cwd=repo)
    sha = git("rev-parse", "HEAD", cwd=repo)
    url = "file:///" + repo.as_posix().lstrip("/")
    r = checkout_project(proj(url, sha), tmp_path / "sources", depth=1)
    assert r.status == "ok", r.error
    f = tmp_path / "sources" / "demo" / "Vault.sol"
    assert not f.is_symlink() and f.read_text() == "../../outside-secret"


def test_no_scored_project_still_writes_definitions(tmp_path, monkeypatch):
    import json
    from runner import run as runmod
    from scoring.nethermind import DEFINITIONS
    out = runmod.run("baseline", "scabench", tmp_path / "r", projects=["code4rena_initia-move_2025_04"],
                     judge="mock")  # empty commit -> excluded, nothing cloned or scored
    assert json.loads((out / "scores.json").read_text(encoding="utf-8"))["definitions"] == DEFINITIONS


def test_manifest_scan_py_sha256_is_the_snapshot_hash_when_scan_py_is_edited_mid_run(tmp_path, monkeypatch):
    # D-26: the 2026-10-21 36-file run wrote the live scan.py hash (4661b112...) into the manifest, but the run had
    # started with the snapshot (DBEFCC26...) because scan.py was edited while it was going. Through the real
    # runner (_single -> agent -> manifest) the recorded hash must be the snapshot's, whatever happens to scan.py later.
    import hashlib
    import json
    from dataclasses import dataclass

    from adapters.attacktrail import AttackTrailAgent
    from runner import run as run_mod
    from runner.executor import ExecResult

    @dataclass
    class Co:
        status: str = "ok"
        path: Path = tmp_path

    monkeypatch.setattr(run_mod, "checkout_project", lambda project, root: Co())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    tools = tmp_path / "tools"
    tools.mkdir()
    scan_py = tools / "scan.py"
    scan_py.write_text("# scan.py as the run started\n", encoding="utf-8")
    original = hashlib.sha256(scan_py.read_bytes()).hexdigest()

    class EditsScanPyWhileRunning:
        def run(self, argv, cwd, env, timeout_s):
            scan_py.write_text("# scan.py as the run started\n# edited while the run was going\n", encoding="utf-8")
            return ExecResult(returncode=1, stdout="", stderr="stop", seconds=0.0, timed_out=False)  # crashed: nothing to score

    agent = AttackTrailAgent(scan_py=scan_py, executor=EditsScanPyWhileRunning())
    project = {"project_id": "demo", "codebases": [{"commit": "abc"}]}
    run_dir = tmp_path / "run"
    run_mod._single(run_dir, agent, "attacktrail", {"name": "scabench"}, "scabench", [project], 1, 60, "mock", "mock", (1, 1))
    man = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert man["projects"][0]["agent_status"] == "crashed"          # the executor stub failed on purpose
    assert hashlib.sha256(scan_py.read_bytes()).hexdigest() != original
    assert man["agent"]["scan_py_sha256"] == original
    assert man["agent"]["scan_py_sha256"] == hashlib.sha256(
        (run_dir / "agent_snapshot" / "tools" / "scan.py").read_bytes()).hexdigest()


def test_rescore_writes_the_run_dirs_snapshot_hashes_into_the_manifest(tmp_path):
    # D-26: eval rescore builds a fresh agent, which hashed the live scan.py as of rescore time. When the run dir holds
    # an agent_snapshot the manifest must name that copy, however the live files changed since. No API: the one project
    # is excluded (empty commit), the judge is mock, so nothing is run or scored.
    import hashlib
    import json

    from adapters.attacktrail import AttackTrailAgent
    from runner import run as run_mod

    tools, docs = tmp_path / "tools", tmp_path / "docs"
    tools.mkdir()
    docs.mkdir()
    live = tools / "scan.py"
    src = ('import os\nROLE = "role v1"\n'
           'STD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")\n'
           'STD_BASE = ["A.md"]\nSTD_WEB = []\n'
           'def load_standards(web):\n    return open(os.path.join(STD_DIR, "A.md"), encoding="utf-8").read()\n')
    live.write_text(src, encoding="utf-8")
    (docs / "A.md").write_text("standard v1", encoding="utf-8")
    started = AttackTrailAgent(scan_py=live)
    run_dir, bare_dir = tmp_path / "run", tmp_path / "bare"
    started.snapshot(run_dir)                                   # what the original run left behind
    bare_dir.mkdir()                                            # a run dir that never had one
    snap_sha = hashlib.sha256((run_dir / "agent_snapshot" / "tools" / "scan.py").read_bytes()).hexdigest()
    live.write_text(src.replace("role v1", "role v2") + "# edited after the run\n", encoding="utf-8")
    (docs / "A.md").write_text("standard v2", encoding="utf-8")

    def agent_block(d):
        run_mod.rescore(d, "attacktrail", "scabench", projects=["code4rena_initia-move_2025_04"], judge="mock",
                        agent_opts={"scan_py": live})
        return json.loads((d / "manifest.json").read_text(encoding="utf-8"))["agent"]

    a = agent_block(run_dir)
    assert a["scan_py_sha256"] == snap_sha != hashlib.sha256(live.read_bytes()).hexdigest()
    assert a["prompt_sha256"] == hashlib.sha256(b"role v1\n\nstandard v1").hexdigest()   # as the run had them
    assert a["prompt_hash"] == started.prompt_hash()
    assert a["snapshot_dir"] == "agent_snapshot" and a["fingerprint_source"] == "snapshot"
    assert "timeout_s" not in a                                 # rescore does not know the project timeout
    b = agent_block(bare_dir)                                   # control: no copy -> live files, and it says so
    assert b["scan_py_sha256"] == hashlib.sha256(live.read_bytes()).hexdigest()
    assert b["fingerprint_source"] == "live" and "snapshot_dir" not in b


def test_aggregate_takes_first_non_empty_definitions(tmp_path):
    import json
    from runner import run as runmod
    reps = []
    for k, defs in ((1, None), (2, {"strict": "x"})):
        d = tmp_path / f"rep-{k}"
        d.mkdir()
        (d / "scores.json").write_text(json.dumps({"overall": None, "definitions": defs}), encoding="utf-8")
        (d / "manifest.json").write_text(json.dumps({"projects": [], "agent": {}, "judge": {}, "executor": "local"}),
                                         encoding="utf-8")
        reps.append(d)
    runmod._aggregate(tmp_path, reps, "t0", {"name": "scabench"}, "scabench")
    assert json.loads((tmp_path / "scores.json").read_text(encoding="utf-8"))["definitions"] == {"strict": "x"}


def test_run_project_copies_agent_coverage_into_the_manifest_record(tmp_path, monkeypatch):
    # D-20 (H2): Meta.files_* -> manifest projects[]; agents that do not report coverage leave None.
    from dataclasses import dataclass

    from adapters.base import Findings, Meta
    from runner import run as run_mod

    @dataclass
    class Co:
        status: str = "ok"
        path: Path = tmp_path

    monkeypatch.setattr(run_mod, "checkout_project", lambda project, root: Co())

    class Agent:
        def __init__(self, **cov):
            self.cov = cov

        def run(self, scope, ctx):
            return Findings(vulnerabilities=[], meta=Meta(agent="a", agent_version="v", model="m", status="ok",
                                                          **self.cov))
    project = {"project_id": "demo"}
    rec = run_mod._run_project(project, Agent(files_total=53, files_scanned=40, files_failed=3, files_skipped=10),
                               "attacktrail", tmp_path / "r1", 60)
    assert (rec["files_total"], rec["files_scanned"], rec["files_failed"], rec["files_skipped"]) == (53, 40, 3, 10)
    assert rec["agent_status"] == "ok", rec["agent_error"]
    none = run_mod._run_project(project, Agent(), "baseline", tmp_path / "r2", 60)
    assert [none[k] for k in run_mod.COVERAGE_KEYS] == [None] * 4
    assert set(run_mod._blank("x")) >= set(run_mod.COVERAGE_KEYS)


def test_run_project_copies_fallback_counts_into_the_manifest_record(tmp_path, monkeypatch):
    # D-23: Meta.files_fallback / fallback_models ride the same optional-field path as the coverage counts.
    from dataclasses import dataclass

    from adapters.base import Findings, Meta
    from runner import run as run_mod

    @dataclass
    class Co:
        status: str = "ok"
        path: Path = tmp_path

    monkeypatch.setattr(run_mod, "checkout_project", lambda project, root: Co())

    class Agent:
        def __init__(self, **extra):
            self.extra = extra

        def run(self, scope, ctx):
            return Findings(vulnerabilities=[], meta=Meta(agent="a", agent_version="v", model="m", status="ok",
                                                          **self.extra))
    project = {"project_id": "demo"}
    rec = run_mod._run_project(project, Agent(files_fallback=12, fallback_models=["claude-sonnet-5"]),
                               "attacktrail", tmp_path / "r1", 60)
    assert rec["agent_status"] == "ok", rec["agent_error"]
    assert (rec["files_fallback"], rec["fallback_models"]) == (12, ["claude-sonnet-5"])
    none = run_mod._run_project(project, Agent(), "attacktrail-cli", tmp_path / "r2", 60)
    assert (none["files_fallback"], none["fallback_models"]) == (None, None)       # not measured, not zero
    assert set(run_mod._blank("x")) >= set(run_mod.META_KEYS) >= set(run_mod.COVERAGE_KEYS)
    assert {"files_fallback", "fallback_models"} <= set(run_mod.META_KEYS)
    # the findings.json an older run wrote has no such keys; rescore must still read it (fields default to None)
    old = Findings(vulnerabilities=[], meta=Meta(agent="a", agent_version="v", model="m", status="ok")).model_dump()
    old["meta"].pop("files_fallback")
    old["meta"].pop("fallback_models")
    assert Findings.model_validate(old).meta.files_fallback is None


def _write_manifest(run_dir, projects):
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest.write_manifest(run_dir, manifest.build_manifest(run_id="r", projects=projects))
    return run_dir


SUMMARY_PROJECTS = [
    {"project_id": "pa", "agent_status": "ok", "agent_error": "memo", "files_total": 53, "files_scanned": 48,
     "files_failed": 3, "files_skipped": 2, "files_fallback": 7, "fallback_models": ["claude-sonnet-5"]},
    {"project_id": "pb", "agent_status": "crashed", "agent_error": "scan.py exit 2: 연속 API 오류로 스캔을 중단했습니다"},
    {"project_id": "pc", "agent_status": "ok", "agent_error": "old memo"}]


def test_run_prints_the_summary_on_stderr_and_keeps_stdout_for_the_path(tmp_path, monkeypatch, capsys):
    # D-23: at the end of `eval run` the console lists partial failures (project, failed/total files, coverage 미측정),
    # crashed projects with a one-line reason, and fallback files. The report keeps its own sections.
    from runner import run as run_mod

    def fake_single(run_dir, *args, rep):
        return _write_manifest(run_dir, SUMMARY_PROJECTS)
    monkeypatch.setattr(run_mod, "_single", fake_single)
    out = run_mod.run("baseline", "scabench", tmp_path / "r", projects=["code4rena_initia-move_2025_04"], judge="mock")
    cap = capsys.readouterr()
    assert out == (tmp_path / "r").resolve() and cap.out == ""
    assert cap.err.splitlines() == [
        "[run 요약] r",
        "부분 실패 프로젝트 2개",
        "  pa: 실패 3/53 파일, 건너뜀 2, 커버리지 48/53",
        "  pc: 커버리지 미측정",
        "crashed 프로젝트 1개",
        "  pb [crashed]: scan.py exit 2: 연속 API 오류로 스캔을 중단했습니다",
        "폴백 응답 프로젝트 1개",
        "  pa: 폴백 응답 7 파일 (모델 claude-sonnet-5)"]


def test_run_with_repeat_prints_one_summary_per_rep(tmp_path, monkeypatch, capsys):
    import importlib

    from runner import run as run_mod

    def fake_single(run_dir, *args, rep):
        return _write_manifest(run_dir, SUMMARY_PROJECTS if rep[0] == 1 else [])
    monkeypatch.setattr(run_mod, "_single", fake_single)
    monkeypatch.setattr(run_mod, "_aggregate", lambda *a, **k: None)     # needs scores.json; not what this tests
    # report/__init__ re-exports the function under the name of the submodule, so go through sys.modules
    monkeypatch.setattr(importlib.import_module("report.render"), "render", lambda d: None)
    run_mod.run("baseline", "scabench", tmp_path / "r", projects=["code4rena_initia-move_2025_04"], judge="mock",
                repeat=2)
    err = capsys.readouterr().err
    assert err.count("[run 요약] rep-") == 2
    first, second = err.split("[run 요약] rep-2")
    assert "[run 요약] rep-1" in first and "pa: 실패 3/53 파일" in first
    assert second.splitlines()[1:] == ["부분 실패 프로젝트: 없음", "crashed 프로젝트: 없음"]


def test_a_broken_summary_never_fails_a_finished_run(tmp_path, capsys):
    from runner import run as run_mod
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "manifest.json").write_text("{not json", encoding="utf-8")
    run_mod._print_summary([tmp_path / "bad", tmp_path / "missing"])          # must not raise
    err = capsys.readouterr().err
    assert err.count("[run 요약] 요약을 만들지 못했다") == 2


def _no_files_run(tmp_path, monkeypatch, projects, out_name="run", n_findings=8):
    """Run the real _single (checkout stubbed, mock judge) with an agent that reports no_files for `p_nf` and `n_findings`
    findings for any other project. Returns (run_dir, manifest, scores).

    The mock judge pops a finding for each match, so with fewer findings than truths the rest get no scorer entry
    (truths_unevaluated > 0, D-28). The default 8 covers the 4-truth projects here: nothing is left unevaluated.
    """
    import json
    from dataclasses import dataclass

    from adapters.base import Finding, Findings, Meta
    from runner import run as run_mod

    @dataclass
    class Co:
        status: str = "ok"
        path: Path = tmp_path

    monkeypatch.setattr(run_mod, "checkout_project", lambda project, root: Co())

    class Agent:
        name, default_timeout_s = "attacktrail", 60
        version = lambda self: "v"  # noqa: E731
        model = lambda self: "m"  # noqa: E731
        prompt_hash = lambda self: "h"  # noqa: E731
        fingerprints = lambda self: {}  # noqa: E731

        def run(self, scope, ctx):
            meta = dict(agent="a", agent_version="v", model="m")
            if scope.project_id == "p_nf":
                return Findings(vulnerabilities=[], meta=Meta(
                    **meta, status="no_files", error="no files selected: 스캔할 파일이 0개입니다", files_total=0,
                    files_scanned=0, files_failed=0, files_skipped=0))
            return Findings(vulnerabilities=[Finding(id=f"a{i}", title=f"x{i}", file="A.sol", lines=[1], severity="high",
                                                     description=f"d{i}") for i in range(n_findings)],
                            meta=Meta(**meta, status="ok"))

    run_dir = tmp_path / out_name
    run_mod._single(run_dir, Agent(), "attacktrail", {"name": "scabench"}, "scabench", projects, 1, 60, "mock", "mock",
                    (1, 1))
    load = lambda f: json.loads((run_dir / f).read_text(encoding="utf-8"))  # noqa: E731
    return run_dir, load("manifest.json"), load("scores.json")


def _project(pid, n_truths):
    return {"project_id": pid, "codebases": [{"commit": "abc"}], "vulnerabilities": [
        {"finding_id": f"{pid}-{i}", "severity": "high" if i == 0 else "medium", "title": f"t{i}", "description": f"d{i}"}
        for i in range(n_truths)]}


def test_no_files_project_is_not_a_crash_stays_out_of_the_headline_and_its_truths_are_counted_apart(
        tmp_path, monkeypatch, capsys):
    # D-27: a project the scanner had nothing to scan is agent_status no_files (not crashed). It is in no denominator, so
    # `overall` is exactly what the scored project alone gives. Its truths are kept in `unsupported` (manifest and
    # scores.json), with the Recall that counts them all as misses.
    from runner import run as run_mod

    ok, nf = _project("p_ok", 4), _project("p_nf", 8)
    _, alone_man, alone = _no_files_run(tmp_path, monkeypatch, [ok], "alone")
    run_dir, man, scores = _no_files_run(tmp_path, monkeypatch, [ok, nf], "both")
    assert {p["project_id"]: p["agent_status"] for p in man["projects"]} == {"p_ok": "ok", "p_nf": "no_files"}
    nf_rec = next(p for p in man["projects"] if p["project_id"] == "p_nf")
    assert (nf_rec["files_total"], nf_rec["files_scanned"]) == (0, 0) and "스캔할 파일이 0개입니다" in nf_rec["agent_error"]
    assert list(scores["per_project"]) == ["p_ok"]              # not scored
    assert scores["overall"] == alone["overall"]                # the headline does not move
    assert "unsupported" not in alone and "unsupported" not in alone_man    # no such project: nothing added

    o = scores["overall"]
    assert o["truths_in_dataset"] == 4
    u = scores["unsupported"]
    assert man["unsupported"] == u                              # one block, written to both
    assert u["projects"] == [{"project_id": "p_nf", "truths": 8}] and (u["n_projects"], u["truths"]) == (1, 8)
    assert u["truths_in_dataset_incl_unsupported"] == 12        # 4 scored + 8 unsupported
    assert u["recall_vs_dataset_incl_unsupported"] == pytest.approx(o["matched"] / 12)
    assert u["recall_with_partial_vs_dataset_incl_unsupported"] == pytest.approx((o["matched"] + o["partial"]) / 12)
    assert u["recall_vs_dataset_incl_unsupported"] < o["recall_vs_dataset"]      # the 8 misses pull it down
    assert u["line"].startswith("지원 밖 프로젝트 1개, 정답 8건. 이 정답을 전부 미탐으로 넣으면 Recall ")
    assert f"{u['recall_vs_dataset_incl_unsupported'] * 100:.1f}% (strict)" in u["line"]
    # D-28: the one-liner says what the denominator is made of, and what stays out of it
    assert ("분모는 채점된 프로젝트 정답과 지원 밖 정답의 합이고 체크아웃 실패·pinned 누락·crashed·timeout 정답은 제외한다"
            in u["line"])
    assert u["line"].endswith(f"헤드라인(지원 프로젝트만)은 {o['recall_vs_dataset'] * 100:.1f}% (strict) / "
                              f"{o['recall_with_partial_vs_dataset'] * 100:.1f}% (lenient)")
    assert "no_files" in man["denominators_note"] and "unsupported" in man["denominators_note"]

    run_mod._print_summary([run_dir])                           # the console summary (D-23) names it, not as crashed
    assert capsys.readouterr().err.splitlines() == [
        "[run 요약] both", "부분 실패 프로젝트: 없음", "crashed 프로젝트: 없음", "지원 밖 프로젝트: p_nf (정답 8)"]


def test_a_run_where_every_project_is_no_files_still_reports_the_unsupported_truths(tmp_path, monkeypatch):
    # nothing scored: overall is None, and the all-misses Recall is 0 over the unsupported truths (not None, not a crash)
    _, man, scores = _no_files_run(tmp_path, monkeypatch, [_project("p_nf", 8)])
    assert scores["overall"] is None and scores["per_project"] == {}
    u = scores["unsupported"]
    assert (u["truths"], u["truths_in_dataset_incl_unsupported"], u["recall_vs_dataset_incl_unsupported"]) == (8, 8, 0.0)
    assert "해당 없음" in u["line"] and man["unsupported"] == u


def test_rescore_of_a_no_files_project_reports_cabals_eight_truths_from_the_dataset(tmp_path):
    # D-27 with the real dataset: cabal has 8 truths and, under the D-26 rules, no scannable file (its scope is all
    # .move). rescore rebuilds the records from findings.json, so a no_files meta must come through it as well.
    import json

    from adapters.base import Findings, Meta
    from runner import run as run_mod

    pid = "code4rena_cabal-liquid-staking-token_2025_05"
    pdir = tmp_path / "projects" / pid
    pdir.mkdir(parents=True)
    (pdir / "checkout.json").write_text(json.dumps({"status": "ok", "path": str(tmp_path)}), encoding="utf-8")
    (pdir / "findings.json").write_text(Findings(vulnerabilities=[], meta=Meta(
        agent="attacktrail", agent_version="v", model="m", status="no_files", error="no files selected",
        files_total=0, files_scanned=0, files_failed=0, files_skipped=0)).model_dump_json(), encoding="utf-8")
    run_mod.rescore(tmp_path, "attacktrail", "scabench", projects=[pid], judge="mock")
    man = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert man["projects"][0]["agent_status"] == "no_files"
    assert man["unsupported"]["projects"] == [{"project_id": pid, "truths": 8}]
    assert json.loads((tmp_path / "scores.json").read_text(encoding="utf-8"))["unsupported"] == man["unsupported"]


@pytest.mark.parametrize("agent_name, explicit, want", [
    ("attacktrail", None, 10800), ("baseline", None, 1800), ("attacktrail", 60, 60), ("baseline", 60, 60)])
def test_run_resolves_the_project_timeout_from_the_agent_and_records_it(tmp_path, agent_name, explicit, want):
    # D-24: the API adapter's calls take minutes to tens of minutes (Fable 5.1 at xhigh), so its default project timeout
    # is 3 h; other agents keep 1800 s. cli_argv alone no longer shows the value, so the manifest's agent block records it.
    import json
    from runner import run as runmod
    out = runmod.run(agent_name, "scabench", tmp_path / "r", projects=["code4rena_initia-move_2025_04"],
                     judge="mock", timeout_s=explicit)      # empty commit -> excluded: nothing cloned, run or scored
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert man["agent"]["timeout_s"] == want


def test_attacktrail_key_alias_wins_and_the_child_sees_the_standard_name(monkeypatch, tmp_path):
    # D-24: the key can live in the user-level variable ATTACKTRAIL_ANTHROPIC_API_KEY (Claude Code reads only
    # ANTHROPIC_API_KEY and would bill it). The harness takes the alias first, then ANTHROPIC_API_KEY, and the child still
    # gets the name scan.py and the SDK read. The alias name itself never reaches the child.
    import json
    from adapters.attacktrail import AttackTrailAgent
    from adapters.base import RunContext
    from runner import run as runmod
    monkeypatch.setenv("ATTACKTRAIL_ANTHROPIC_API_KEY", "sk-ant-alias-0123456789abcdef")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-standard-0123456789abcdef")
    env = runmod.agent_env("attacktrail")
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-alias-0123456789abcdef"            # the alias wins
    assert "ATTACKTRAIL_ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
    monkeypatch.delenv("ATTACKTRAIL_ANTHROPIC_API_KEY")
    assert runmod.agent_env("attacktrail")["ANTHROPIC_API_KEY"] == "sk-ant-standard-0123456789abcdef"   # as before
    monkeypatch.setenv("ATTACKTRAIL_ANTHROPIC_API_KEY", "")                         # an empty alias falls back
    assert runmod.agent_env("attacktrail")["ANTHROPIC_API_KEY"] == "sk-ant-standard-0123456789abcdef"
    # the alias alone is enough, and the adapter's key check (ctx.env first) accepts it without os.environ's own name
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("ATTACKTRAIL_ANTHROPIC_API_KEY", "sk-ant-alias-0123456789abcdef")
    env = runmod.agent_env("attacktrail")
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-alias-0123456789abcdef"

    class Spy:
        """Stands in for the executor: records the env scan.py would get and does not run anything."""
        def run(self, argv, cwd, env, timeout_s):
            self.env = dict(env)
            from runner.executor import ExecResult
            return ExecResult(returncode=1, stdout="", stderr="stop", seconds=0.0, timed_out=False)

    spy = Spy()
    scan_py = tmp_path / "tools" / "scan.py"
    scan_py.parent.mkdir()
    scan_py.write_text("# placeholder\n", encoding="utf-8")
    AttackTrailAgent(scan_py=scan_py, executor=spy).run(
        __import__("adapters.base", fromlist=["Scope"]).Scope("p", tmp_path),
        RunContext(tmp_path, tmp_path / "proj", 10, env))
    assert spy.env["ANTHROPIC_API_KEY"] == "sk-ant-alias-0123456789abcdef" and "ATTACKTRAIL_ANTHROPIC_API_KEY" not in spy.env
    # the value stays out of the manifest even when it does not look like a key (exact-value redaction knows the alias)
    monkeypatch.setenv("ATTACKTRAIL_ANTHROPIC_API_KEY", "plain-secret-value")
    m = manifest.build_manifest(run_id="r", cli_argv=["eval", "plain-secret-value"], projects=[])
    assert "plain-secret-value" not in json.dumps(m) and "[redacted]" in json.dumps(m)


def test_rescore_prints_the_run_summary_on_stderr_and_keeps_stdout_empty(tmp_path, capsys):
    # D-28: `eval rescore` ends with the same console summary as `eval run` (D-23): stderr, stdout left to the path cli.py prints.
    # Real rescore, real dataset (cabal: 8 truths, no scannable file) and the mock judge, as in the cabal test above.
    import json

    from adapters.base import Findings, Meta
    from runner import run as run_mod

    pid = "code4rena_cabal-liquid-staking-token_2025_05"
    pdir = tmp_path / "projects" / pid
    pdir.mkdir(parents=True)
    (pdir / "checkout.json").write_text(json.dumps({"status": "ok", "path": str(tmp_path)}), encoding="utf-8")
    (pdir / "findings.json").write_text(Findings(vulnerabilities=[], meta=Meta(
        agent="attacktrail", agent_version="v", model="m", status="no_files", error="no files selected",
        files_total=0, files_scanned=0, files_failed=0, files_skipped=0)).model_dump_json(), encoding="utf-8")
    out = run_mod.rescore(tmp_path, "attacktrail", "scabench", projects=[pid], judge="mock")
    cap = capsys.readouterr()
    assert out == tmp_path.resolve() and cap.out == ""
    # no raw/scan.jsonl here, so the `_run` check (D-29 review R1) puts its one line before the summary
    assert cap.err.splitlines() == [f"rescore: {tmp_path.name} 의 _run 행에서 model/effort/max_tokens 를 찾지 못해 인자 또는 기본값으로 기록한다",
                                    f"[run 요약] {tmp_path.name}", "부분 실패 프로젝트: 없음", "crashed 프로젝트: 없음",
                                    f"지원 밖 프로젝트: {pid} (정답 8)"]


def test_incomplete_judging_is_flagged_in_scores_manifest_summary_and_report_and_a_complete_run_has_no_flag(
        tmp_path, monkeypatch, capsys):
    # D-28: truths_unevaluated > 0 with at least one agent finding (the scorer gave those truths no entry: judge calls that
    # failed with "Connection error", or the findings were used up by matches) is flagged, because the headline Recall counts them as misses and the run reads low for a reason that
    # is not the agent. One finding for four truths: the mock judge matches truth 1, uses the finding up, and truths 2..4
    # get no entry (3 unevaluated). Flag: judge_incomplete true + judge_incomplete_projects in scores.json AND the manifest;
    # a console line; the same line right under the report's Headline table. A run with every truth judged carries none of it.
    import json

    from runner import run as run_mod

    ok = _project("p_ok", 4)
    full_dir, full_man, full_scores = _no_files_run(tmp_path, monkeypatch, [ok], "full")
    assert full_scores["per_project"]["p_ok"]["truths_unevaluated"] == 0
    assert not {"judge_incomplete", "judge_incomplete_projects"} & (set(full_scores) | set(full_man))
    run_mod._print_summary([full_dir])
    assert "채점 불완전" not in capsys.readouterr().err

    run_dir, man, scores = _no_files_run(tmp_path, monkeypatch, [ok], "short", n_findings=1)
    assert scores["per_project"]["p_ok"]["truths_unevaluated"] == 3
    want = [{"project_id": "p_ok", "truths_unevaluated": 3}]
    assert scores["judge_incomplete"] is True and scores["judge_incomplete_projects"] == want
    assert man["judge_incomplete"] is True and man["judge_incomplete_projects"] == want

    run_mod._print_summary([run_dir])
    assert capsys.readouterr().err.splitlines() == [
        "[run 요약] short", "부분 실패 프로젝트: 없음", "crashed 프로젝트: 없음",
        "채점 불완전: p_ok 미평가 정답 3건(채점 로그 확인)"]

    line = "채점 불완전: p_ok 미평가 정답 3건(채점 로그 확인)"
    md = (run_dir / "report.md").read_text(encoding="utf-8")
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert line in html
    head = md.index("## Headline")
    assert head < md.index(line) < md.index("## Per project")          # near the headline, not buried below
    assert md.index(line) - md.index("| Metric | Value | Role |") < 1500   # right under the headline table
    assert line not in (full_dir / "report.md").read_text(encoding="utf-8")
    json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))   # still valid JSON


def test_a_repeat_aggregate_carries_the_incomplete_rep_so_its_means_are_not_trusted(tmp_path, monkeypatch):
    # D-28: runner._aggregate averages the reps' scores. One rep judged incompletely makes those means and stdevs wrong, so the
    # aggregate scores.json and manifest say which rep and project, and the aggregate report shows the line with the rep.
    import json

    from report.render import render
    from runner import run as run_mod

    ok = _project("p_ok", 4)
    reps = [_no_files_run(tmp_path, monkeypatch, [ok], "agg/rep-1")[0],
            _no_files_run(tmp_path, monkeypatch, [ok], "agg/rep-2", n_findings=1)[0],
            _no_files_run(tmp_path, monkeypatch, [ok], "agg/rep-3")[0]]
    out = tmp_path / "agg"
    run_mod._aggregate(out, reps, "t0", {"name": "scabench"}, "scabench")
    scores = json.loads((out / "scores.json").read_text(encoding="utf-8"))
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    want = [{"project_id": "p_ok", "truths_unevaluated": 3, "run": "rep-2"}]
    assert scores["judge_incomplete"] is True and scores["judge_incomplete_projects"] == want
    assert man["judge_incomplete"] is True and man["judge_incomplete_projects"] == want
    render(out)
    assert "채점 불완전: p_ok 미평가 정답 3건(채점 로그 확인, rep-2)" in (out / "report.md").read_text(encoding="utf-8")
    # all reps complete: the aggregate has no flag
    ok_reps = [_no_files_run(tmp_path, monkeypatch, [ok], f"agg2/rep-{k}")[0] for k in (1, 2)]
    run_mod._aggregate(tmp_path / "agg2", ok_reps, "t0", {"name": "scabench"}, "scabench")
    assert "judge_incomplete" not in json.loads((tmp_path / "agg2" / "scores.json").read_text(encoding="utf-8"))


def test_manifest_and_report_record_effort_and_max_tokens_from_the_agent_options(tmp_path):
    # D-28: `eval run --effort max --max-tokens 128000` -> agent_opts -> the adapter -> manifest agent block and the report's
    # Manifest table. The project is excluded by the dataset (empty commit), so nothing is cloned, run or scored.
    import json

    from runner import run as runmod
    out = runmod.run("attacktrail", "scabench", tmp_path / "r", projects=["code4rena_initia-move_2025_04"], judge="mock",
                     agent_opts={"effort": "max", "max_tokens": 128000})
    agent = json.loads((out / "manifest.json").read_text(encoding="utf-8"))["agent"]
    assert (agent["effort"], agent["max_tokens"], agent["call_shape"]) == ("max", 128000, "api-v6")
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "| max_tokens (요청 출력 상한) | 128000 |" in md and "| effort (output_config.effort) | max |" in md
    assert "max_tokens (요청 출력 상한)" in (out / "report.html").read_text(encoding="utf-8")
    default = runmod.run("attacktrail", "scabench", tmp_path / "d", projects=["code4rena_initia-move_2025_04"], judge="mock")
    assert json.loads((default / "manifest.json").read_text(encoding="utf-8"))["agent"]["max_tokens"] == 64000


# ---------------------------------------------------------------------------------------------
# D-28 review 1 and 4: rescore against the logged `_run` row, and no incomplete-judging flag without agent findings
# ---------------------------------------------------------------------------------------------

EXCLUDED_PID = "code4rena_initia-move_2025_04"        # excluded by the dataset (empty commit): rescore needs no checkout for it
LOGGED = {"model": "claude-fable-5-1", "effort": "max", "max_tokens": 128000}   # the _run row of the max 128000 runs (D-29)


def _logged_run(d, **run_row):
    """A run dir whose projects/<id>/raw/scan.jsonl starts with scan.py's `_run` row, then a `_usage` row like the real one."""
    import json

    raw = d / "projects" / EXCLUDED_PID / "raw"
    raw.mkdir(parents=True)
    (raw / "scan.jsonl").write_text(json.dumps({"_run": run_row}) + "\n" + json.dumps({"_usage": {"file": "A.sol"}}) + "\n",
                                    encoding="utf-8")
    return d


def test_rescore_refuses_arguments_that_differ_from_the_logged_run_row_and_accepts_equal_ones(tmp_path, capsys):
    # D-28 review 1: rescore only labels the manifest with --model/--effort/--max-tokens, so a value that differs from the
    # `_run` row of the original run would be a wrong record. Any one of the three differing refuses before anything is
    # written (ValueError; cli.py prints it as "재채점 중단" with exit 1). The same values as logged go through.
    import json

    import cli
    from runner import run as run_mod

    d = _logged_run(tmp_path / "r", **LOGGED)
    for opts, flag in [({"effort": "xhigh"}, "--effort xhigh"), ({"max_tokens": 64000}, "--max-tokens 64000"),
                       ({"model": "claude-opus-5-5"}, "--model claude-opus-5-5"),
                       ({"effort": "high", "max_tokens": 128000}, "--effort high")]:   # one differing value is enough
        with pytest.raises(ValueError, match=flag) as e:
            run_mod.rescore(d, "attacktrail", "scabench", projects=[EXCLUDED_PID], judge="mock", agent_opts=opts)
        assert "원래 run" in str(e.value) and "_run" in str(e.value)
        assert not (d / "manifest.json").exists() and not (d / "scores.json").exists()
    rc = cli.main(["rescore", str(d), "--agent", "attacktrail", "--dataset", "scabench", "--projects", EXCLUDED_PID,
                   "--judge", "mock", "--effort", "xhigh"])
    err = capsys.readouterr().err
    assert rc == 1 and err.startswith("재채점 중단: --effort xhigh 는 원래 run 의 effort 값(max)과 다르다") and not (d / "manifest.json").exists()
    run_mod.rescore(d, "attacktrail", "scabench", projects=[EXCLUDED_PID], judge="mock", agent_opts=dict(LOGGED))
    a = json.loads((d / "manifest.json").read_text(encoding="utf-8"))["agent"]
    assert (a["model"], a["effort"], a["max_tokens"]) == ("claude-fable-5-1", "max", 128000)


def test_rescore_takes_the_logged_values_for_arguments_not_given_and_scan_pys_defaults_without_a_row(tmp_path):
    # D-28 review 1: not given -> the `_run` row's value goes into the manifest (a max 128000 run rescored without flags used
    # to be labelled xhigh 64000, scan.py's defaults). A given one that matches is kept, the others are filled in. With no
    # scan.jsonl (the CLI adapter, baseline, an older run) nothing is read and nothing changes: the defaults as before.
    import json

    from runner import run as run_mod

    agent = lambda d: json.loads((d / "manifest.json").read_text(encoding="utf-8"))["agent"]  # noqa: E731
    d = _logged_run(tmp_path / "all", **LOGGED)
    run_mod.rescore(d, "attacktrail", "scabench", projects=[EXCLUDED_PID], judge="mock")
    a = agent(d)
    assert (a["model"], a["effort"], a["max_tokens"]) == ("claude-fable-5-1", "max", 128000) and a["call_shape"] == "api-v6"
    d = _logged_run(tmp_path / "part", model="claude-opus-5-5", effort="xhigh", max_tokens=64000)
    run_mod.rescore(d, "attacktrail", "scabench", projects=[EXCLUDED_PID], judge="mock", agent_opts={"effort": "xhigh"})
    a = agent(d)
    assert (a["model"], a["effort"], a["max_tokens"]) == ("claude-opus-5-5", "xhigh", 64000)
    (tmp_path / "bare").mkdir()
    run_mod.rescore(tmp_path / "bare", "attacktrail", "scabench", projects=[EXCLUDED_PID], judge="mock")
    a = agent(tmp_path / "bare")
    assert (a["effort"], a["max_tokens"]) == ("high", 64000)            # scan.py's own defaults (D-34)
    # the CLI adapter is never checked, whatever sits in raw/: it has no effort or max_tokens to compare
    d = _logged_run(tmp_path / "cli", **LOGGED)
    run_mod.rescore(d, "attacktrail-cli", "scabench", projects=[EXCLUDED_PID], judge="mock")
    assert agent(d)["effort"] == "n/a"


def test_logged_run_rows_that_disagree_are_refused_and_unreadable_ones_are_ignored(tmp_path):
    # D-28 review 1: projects of one run share one agent, so rows that disagree on a key can't label the manifest with either
    # value. A first line that is not JSON, has no `_run`, or holds wrong types gives no opinion, and so does a missing file.
    import json

    from runner import run as run_mod

    def put(pid, first_line):
        raw = tmp_path / "projects" / pid / "raw"
        raw.mkdir(parents=True)
        (raw / "scan.jsonl").write_text(first_line + "\n", encoding="utf-8")

    put("a", json.dumps({"_run": {"effort": "max", "max_tokens": 128000}}))
    put("b", json.dumps({"_run": {"effort": "xhigh", "max_tokens": 128000}}))
    with pytest.raises(ValueError, match=r"effort.*max \(a\).*xhigh \(b\)"):
        run_mod._check_logged_opts(tmp_path, ["a", "b"], {})
    put("c", "{not json")
    put("d", json.dumps({"_usage": {}}))
    put("e", json.dumps({"_run": {"effort": 5, "max_tokens": True, "model": ""}}))
    assert run_mod._check_logged_opts(tmp_path, ["c", "d", "e", "missing"], {"effort": "high"}) == {"effort": "high"}
    assert run_mod._check_logged_opts(tmp_path, ["a"], {"model": "m"}) == {"model": "m", "effort": "max", "max_tokens": 128000}


def test_rescore_says_on_stderr_which_keys_the_logged_run_row_did_not_give(tmp_path, capsys):
    # D-29 review R1: a key that no selected project's `_run` row holds (no raw/scan.jsonl, or an artifact whose row lacks the key;
    # every run now in eval/runs logs all three, only their manifest agent.max_tokens is None before D-28) is labelled from the
    # argument or scan.py's default, and nothing checked that label. One stderr line names those keys, and only when no selected
    # project gave the key. A row that gives all three prints nothing, and neither does the CLI adapter (it is never checked).
    from runner import run as run_mod

    def warns(d, agent="attacktrail"):
        run_mod.rescore(d, agent, "scabench", projects=[EXCLUDED_PID], judge="mock")
        return [x for x in capsys.readouterr().err.splitlines() if x.startswith("rescore:")]

    (tmp_path / "bare").mkdir()
    assert warns(tmp_path / "bare") == ["rescore: bare 의 _run 행에서 model/effort/max_tokens 를 찾지 못해 인자 또는 기본값으로 기록한다"]
    old = _logged_run(tmp_path / "old", model="claude-fable-5-1", effort="xhigh")      # a `_run` row without max_tokens (hand-made here; the runs in eval/runs all log it)
    assert warns(old) == ["rescore: old 의 _run 행에서 max_tokens 를 찾지 못해 인자 또는 기본값으로 기록한다"]
    assert warns(_logged_run(tmp_path / "full", **LOGGED)) == []
    (tmp_path / "cli").mkdir()
    assert warns(tmp_path / "cli", "attacktrail-cli") == []


def test_rescore_refuses_a_run_whose_projects_logged_different_values_before_writing_anything(tmp_path, capsys):
    # D-29 review R3: the disagreement check is unit-tested above through _check_logged_opts. This goes through rescore() and
    # cli.py with two real dataset projects: the refusal comes before any record is built, so manifest and scores stay unwritten.
    import json

    import cli
    from runner import run as run_mod

    a, b = EXCLUDED_PID, "code4rena_cabal-liquid-staking-token_2025_05"
    for pid, effort in ((a, "max"), (b, "xhigh")):
        raw = tmp_path / "projects" / pid / "raw"
        raw.mkdir(parents=True)
        (raw / "scan.jsonl").write_text(json.dumps({"_run": {"effort": effort}}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="서로 다른 effort") as e:
        run_mod.rescore(tmp_path, "attacktrail", "scabench", projects=[a, b], judge="mock")
    assert f"max ({a})" in str(e.value) and f"xhigh ({b})" in str(e.value)
    rc = cli.main(["rescore", str(tmp_path), "--agent", "attacktrail", "--dataset", "scabench", "--projects", f"{a},{b}",
                   "--judge", "mock"])
    assert rc == 1 and capsys.readouterr().err.startswith("재채점 중단: projects/*/raw/scan.jsonl 의 _run 이 서로 다른 effort 를 기록했다")
    assert not (tmp_path / "manifest.json").exists() and not (tmp_path / "scores.json").exists()


def test_a_project_without_agent_findings_is_not_flagged_as_incomplete_judging(tmp_path, monkeypatch):
    # D-28 review 4: with no agent findings every truth is unevaluated and no judge call was made, so a rescore would change
    # nothing. The flag needs truths_unevaluated > 0 and n_findings > 0; n_findings sits in the manifest record, not in scores.json.
    from runner import run as run_mod

    run_dir, man, scores = _no_files_run(tmp_path, monkeypatch, [_project("p_ok", 4)], "none", n_findings=0)
    assert man["projects"][0]["n_findings"] == 0 and scores["per_project"]["p_ok"]["truths_unevaluated"] == 4
    assert not {"judge_incomplete", "judge_incomplete_projects"} & (set(scores) | set(man))
    assert "채점 불완전" not in (run_dir / "report.md").read_text(encoding="utf-8")
    sc = {"per_project": {"a": {"truths_unevaluated": 3}}}
    assert run_mod._judge_incomplete(sc, {"a": {"n_findings": 0}}) == []
    assert run_mod._judge_incomplete(sc, {"a": {"n_findings": None}}) == [] == run_mod._judge_incomplete(sc, {})
    assert run_mod._judge_incomplete(sc, {"a": {"n_findings": 1}}) == [{"project_id": "a", "truths_unevaluated": 3}]
