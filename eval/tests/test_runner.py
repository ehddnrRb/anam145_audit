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
    env = agent_env("attacktrail")
    assert env.get("ANTHROPIC_API_KEY") == "a-test" and "OPENAI_API_KEY" not in env and "SOMETHING_ELSE" not in env


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
