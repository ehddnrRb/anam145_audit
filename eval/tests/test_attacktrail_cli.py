import _thread
import json
import shutil
import subprocess
import sys
import threading
import time

import pytest

from adapters import attacktrail_cli
from adapters.attacktrail import SCAN_PY, AttackTrailAgent, load_scan_module, sum_usage
from adapters.attacktrail_cli import AttackTrailCLIAgent
from adapters.base import RunContext, Scope


def _fake_claude(responses):
    def call(exe, model, system, schema, prompt, cwd, timeout_s):
        assert "Step-01 automated scanner" in system  # scan.py's ROLE, unmodified
        assert "AUD-SEV-01" in system
        name = prompt.split("\n", 1)[0].split()[1]
        if responses[name] is None:
            raise RuntimeError("boom")
        return {"structured_output": {"candidates": responses[name]}, "total_cost_usd": 0.01,
                "usage": {"input_tokens": 100, "cache_read_input_tokens": 20, "output_tokens": 7},
                "num_turns": 2}
    return call


def test_cli_agent_verifies_locations_and_sums_usage(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "A.sol").write_text("contract A {\n  function f() public {}\n}\n", encoding="utf-8")
    (repo / "B.sol").write_text("contract B {}\n", encoding="utf-8")
    (repo / "C.sol").write_text("contract C {}\n", encoding="utf-8")
    cand = {"title": "t", "line": 2, "category": "access", "severity_guess": "High",
            "why": "anyone can call", "poc_sketch": ""}
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "call_claude", _fake_claude({
        "A.sol": [dict(cand, code_excerpt="function f() public {}"),
                  dict(cand, code_excerpt="not on that line")],  # location hallucination
        "B.sol": [],
        "C.sol": None,  # CLI failure for one file must not kill the project
    }))
    pdir = tmp_path / "out"
    res = AttackTrailCLIAgent(model="m").run(
        Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=pdir, timeout_s=60, env={}))

    assert res.meta.status == "ok", res.meta.error
    assert [f.title for f in res.vulnerabilities] == ["t"]  # mismatch dropped by verify()
    assert res.vulnerabilities[0].file == "A.sol" and res.vulnerabilities[0].lines == [2]
    assert (res.meta.tokens_in, res.meta.tokens_out, res.meta.cost_usd) == (240, 14, 0.02)
    assert res.meta.error == "1 of 3 files not scanned: 1 failed, 0 skipped; failed files: C.sol (RuntimeError: boom)"
    assert (res.meta.files_total, res.meta.files_scanned, res.meta.files_failed,
            res.meta.files_skipped) == (3, 2, 1, 0)
    lines = [json.loads(l) for l in (pdir / "raw" / "scan-cli.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[0]["_run"]["backend"] == "claude-cli"
    assert sum("_usage" in l for l in lines) == 2


def test_cli_agent_odd_candidate_types_fail_only_that_file(tmp_path, monkeypatch):
    # D-19: a line "5" / null / non-list candidates used to raise inside scan.verify() in the
    # writer loop and crash the whole project. Now only that file is ERROR; its billed usage is kept.
    repo = tmp_path / "repo"
    repo.mkdir()
    names = ["A.sol", "B.sol", "C.sol", "D.sol", "E.sol"]
    for n in names:
        (repo / n).write_text("contract X {\n  function f() public {}\n}\n", encoding="utf-8")
    good = {"title": "t", "line": 2, "code_excerpt": "function f() public {}", "category": "access",
            "severity_guess": "High", "why": "anyone can call", "poc_sketch": ""}
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "call_claude", _fake_claude({
        "A.sol": [good],
        "B.sol": [dict(good, line="2")],            # string line
        "C.sol": [dict(good, line=None)],           # null line
        "D.sol": [dict(good, code_excerpt=7)],      # non-string excerpt
        "E.sol": "not a list",                      # candidates is not a list
    }))
    pdir = tmp_path / "out"
    res = AttackTrailCLIAgent(model="m").run(
        Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=pdir, timeout_s=60, env={}))

    assert res.meta.status == "ok", res.meta.error
    assert [(f.file, f.lines) for f in res.vulnerabilities] == [("A.sol", [2])]
    assert res.meta.error.startswith("4 of 5 files not scanned: 4 failed, 0 skipped; failed files: B.sol (ScanError: ")
    assert (res.meta.files_total, res.meta.files_scanned, res.meta.files_failed,
            res.meta.files_skipped) == (5, 1, 4, 0)
    log = (pdir / "raw" / "scan.stdout.txt").read_text(encoding="utf-8")
    for n, why in (("B.sol", "line 이 정수 아님 (str)"), ("C.sol", "line 이 정수 아님 (NoneType)"),
                   ("D.sol", "code_excerpt 가 문자열 아님 (int)"), ("E.sol", "candidates 가 list 아님 (str)")):
        assert "%s  ERROR ScanError: candidates" % n in log and why in log, (n, log)
    # all 5 calls were billed: 5 x (100 + 20) in, 5 x 7 out
    assert (res.meta.tokens_in, res.meta.tokens_out) == (600, 35)
    lines = [json.loads(l) for l in (pdir / "raw" / "scan-cli.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum("_usage" in l for l in lines) == 5


def test_cli_agent_all_calls_failed_is_crash(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "A.sol").write_text("contract A {}\n", encoding="utf-8")
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "call_claude", _fake_claude({"A.sol": None}))
    res = AttackTrailCLIAgent(model="m").run(
        Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=tmp_path / "o", timeout_s=60, env={}))
    assert res.meta.status == "crashed"


def test_prompt_sha256_ignores_scan_py_comments(tmp_path):
    # D-14: a comment-only edit to scan.py changes the file hash, not the prompt hash.
    tools, docs = tmp_path / "tools", tmp_path / "docs"
    tools.mkdir()
    docs.mkdir()
    shutil.copy(SCAN_PY, tools / "scan.py")
    for name in load_scan_module(SCAN_PY).STD_BASE:
        shutil.copy(SCAN_PY.parent.parent / "docs" / name, docs / name)
    before = AttackTrailAgent(scan_py=tools / "scan.py").fingerprints()
    with open(tools / "scan.py", "a", encoding="utf-8") as fh:
        fh.write("\n# comment only\n")
    after = AttackTrailAgent(scan_py=tools / "scan.py").fingerprints()

    assert before["prompt_sha256"] == after["prompt_sha256"] != "unknown"
    assert before["scan_py_sha256"] != after["scan_py_sha256"]
    # D-24: api bumped to v6 (streaming, max_tokens 64000, xhigh default; the default model is Fable 5.1 since D-25, still v6);
    # the CLI call is unchanged
    assert (before["call_shape"], AttackTrailCLIAgent().fingerprints()["call_shape"]) == ("api-v6", "cli-v1")


def test_cli_follows_the_scan_py_model_default_and_sends_no_effort_or_fallback(tmp_path, monkeypatch):
    # D-23/D-25: the CLI adapter reads the --model default from scan.py, so it runs `claude -p --model claude-fable-5-1`
    # like the API adapter's default. `claude -p` gets --model only: no effort, no server-side fallback. The manifest
    # says "n/a" for effort (the API adapter records xhigh) and the fallback counts stay None (not measured).
    agent = AttackTrailCLIAgent()
    assert agent.model() == AttackTrailAgent().model() == "claude-fable-5-1"
    argv = attacktrail_cli.cli_argv("claude", agent.model(), "SYS", {})
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"
    assert not [a for a in argv if "effort" in a or "fallback" in a]
    assert agent.fingerprints()["effort"] == "n/a"
    assert AttackTrailAgent().fingerprints()["effort"] == "xhigh"            # scan.py's --effort default
    assert AttackTrailAgent(effort="high").fingerprints()["effort"] == "high"

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "A.sol").write_text("contract A {}\n", encoding="utf-8")
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "call_claude", _fake_claude({"A.sol": []}))
    res = agent.run(Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=tmp_path / "o", timeout_s=60, env={}))
    assert res.meta.model == "claude-fable-5-1"
    assert (res.meta.files_fallback, res.meta.fallback_models) == (None, None)


def test_cli_run_header_matches_manifest_prompt_sha256(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "A.sol").write_text("contract A {}\n", encoding="utf-8")
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "call_claude", _fake_claude({"A.sol": []}))
    agent = AttackTrailCLIAgent(model="m")
    pdir = tmp_path / "out"
    agent.run(Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=pdir, timeout_s=60, env={}))
    header = json.loads((pdir / "raw" / "scan-cli.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert header["_run"]["prompt_sha256"] == agent.fingerprints()["prompt_sha256"][:16]


def test_cli_web_prompt_over_argv_limit_fails_before_any_call(tmp_path, monkeypatch):
    # D-15: --web standards (~220K chars) cannot ride in argv; fail fast, call nothing.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "A.sol").write_text("contract A {}\n", encoding="utf-8")

    def must_not_call(*a, **k):
        raise AssertionError("claude must not be called")
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "call_claude", must_not_call)
    res = AttackTrailCLIAgent(model="m", web=True).run(
        Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=tmp_path / "o", timeout_s=60, env={}))
    assert res.meta.status == "crashed"
    assert "does not fit argv" in res.meta.error and "--agent attacktrail" in res.meta.error


def test_cli_env_carries_no_api_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-y")
    monkeypatch.setenv("ATTACKTRAIL_ANTHROPIC_API_KEY", "sk-z")   # D-24 alias: not inherited by `claude -p` either
    env = attacktrail_cli.cli_env()
    assert "OPENAI_API_KEY" not in env and "ANTHROPIC_API_KEY" not in env and "ATTACKTRAIL_ANTHROPIC_API_KEY" not in env


def test_cli_agent_unreadable_files_fail_only_that_file(tmp_path, monkeypatch):
    # D-20 (M4): reading the file and building the prompt used to sit outside the try in one(). A directory
    # named X.sol (select_files returns it: it matches the extension) or one unreadable file raised in the
    # writer loop (pool.map) and the whole project was recorded as crashed.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Dir.sol").mkdir()
    (repo / "A.sol").write_text("contract A {\n  function f() public {}\n}\n", encoding="utf-8")
    (repo / "B.sol").write_text("contract B {}\n", encoding="utf-8")
    good = {"title": "t", "line": 2, "code_excerpt": "function f() public {}", "category": "access",
            "severity_guess": "High", "why": "anyone can call", "poc_sketch": ""}
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "call_claude", _fake_claude({"A.sol": [good], "B.sol": []}))
    pdir = tmp_path / "out"
    res = AttackTrailCLIAgent(model="m").run(
        Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=pdir, timeout_s=60, env={}))
    assert res.meta.status == "ok", res.meta.error
    assert [(f.file, f.lines) for f in res.vulnerabilities] == [("A.sol", [2])]    # the other files still ran
    assert (res.meta.files_total, res.meta.files_scanned, res.meta.files_failed) == (3, 2, 1)
    assert res.meta.error.startswith("1 of 3 files not scanned: 1 failed, 0 skipped; failed files: Dir.sol (")
    log = (pdir / "raw" / "scan.stdout.txt").read_text(encoding="utf-8")
    assert "Dir.sol  ERROR " in log and "A.sol  후보 1" in log and "B.sol  후보 0" in log


def test_cli_usage_rows_keep_input_tokens_as_the_sum_and_add_the_raw_kinds(tmp_path, monkeypatch):
    # D-20 (M6): `input_tokens` in a CLI row is uncached + cache_creation + cache_read, so it does not mean what
    # an API row's `input_tokens` (uncached only) means. The sum stays (compatibility); the raw kinds are added
    # under the API row's names, and the uncached value gets its own name.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "A.sol").write_text("contract A {}\n", encoding="utf-8")

    def call(exe, model, system, schema, prompt, cwd, timeout_s):
        return {"structured_output": {"candidates": []}, "total_cost_usd": 0.5, "num_turns": 1,
                "usage": {"input_tokens": 100, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 20,
                          "output_tokens": 7, "service_tier": "standard",
                          "cache_creation": {"ephemeral_1h_input_tokens": 30, "ephemeral_5m_input_tokens": 0}}}
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "call_claude", call)
    pdir = tmp_path / "out"
    res = AttackTrailCLIAgent(model="m").run(
        Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=pdir, timeout_s=60, env={}))
    assert (res.meta.tokens_in, res.meta.tokens_out, res.meta.cost_usd) == (150, 7, 0.5)
    rows = [json.loads(l) for l in (pdir / "raw" / "scan-cli.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[1] == {"_usage": {
        "file": "A.sol", "input_tokens": 150, "input_tokens_uncached": 100,
        "cache_creation_input_tokens": 30, "cache_read_input_tokens": 20, "output_tokens": 7,
        "cost_usd": 0.5, "num_turns": 1, "service_tier": "standard",
        "cache_creation": {"ephemeral_1h_input_tokens": 30, "ephemeral_5m_input_tokens": 0}}}
    assert sum_usage(pdir / "raw" / "scan-cli.jsonl") == (150, 7)    # same as the adapter's own meta


def test_cli_agent_snapshots_scan_py_and_standards(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "A.sol").write_text("contract A {}\n", encoding="utf-8")
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "call_claude", _fake_claude({"A.sol": []}))
    agent = AttackTrailCLIAgent(model="m")
    run_dir = tmp_path / "run"
    res = agent.run(Scope("p", repo), RunContext(run_dir=run_dir, project_dir=run_dir / "p", timeout_s=60, env={}))
    assert res.meta.status == "ok", res.meta.error
    snap = run_dir / "agent_snapshot"
    assert (snap / "tools" / "scan.py").read_bytes() == SCAN_PY.read_bytes()
    names = sorted(p.name for p in (snap / "docs").iterdir())
    assert names == sorted(load_scan_module(SCAN_PY).STD_BASE)
    assert agent.fingerprints()["snapshot_dir"] == "agent_snapshot"


# ---------------------------------------------------------------------------------------------
# D-21 (H-2): Ctrl-C in the CLI adapter. The adapter runs inside eval, so it re-raises KeyboardInterrupt
# (no os._exit) after stopping its own work.
# ---------------------------------------------------------------------------------------------

CTRL_C_S = 5.0   # from the interrupt until run() lets go. The poll is 0.5 s; the rest is slack for a slow machine

# A stand-in for `claude -p`: reads the prompt, then "works" for 25 s, touching a heartbeat file every 0.1 s.
FAKE_CLAUDE = """
import os, pathlib, sys, time
beat = pathlib.Path(sys.argv[1]) / ("alive-%d" % os.getpid())
sys.stdin.read()
end = time.time() + 25
while time.time() < end:
    beat.write_text(str(time.time()))
    time.sleep(0.1)
print('{"structured_output": {"candidates": []}}')
"""


def _twelve_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(12):
        (repo / ("A%02d.sol" % i)).write_text("contract A {}\n", encoding="utf-8")
    return repo


def _interrupt_when(cond, marks, limit=60):
    """In another thread: wait until cond() holds, note the time, then raise KeyboardInterrupt in the main thread
    (what Ctrl-C does)."""
    def go():
        end = time.monotonic() + limit
        while time.monotonic() < end and not cond():
            time.sleep(0.02)
        marks.append(time.monotonic())
        _thread.interrupt_main()
    threading.Thread(target=go, daemon=True).start()


def test_cli_ctrl_c_wakes_the_wait_and_starts_no_new_calls(tmp_path, monkeypatch):
    # Port of the 3rd review's x3 experiment. Old code: pool.map waits on the first future with no timeout, and on
    # Windows that wait is not woken by Ctrl-C, so the interrupt waited for a call to end (30 s here) and the
    # `with ThreadPoolExecutor` exit joined every running call; calls kept starting meanwhile.
    release, calls, marks = threading.Event(), [], []

    def hang(exe, model, system, schema, prompt, cwd, timeout_s):
        calls.append(time.monotonic())
        release.wait(30)
        raise RuntimeError("released")
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "call_claude", hang)
    repo = _twelve_files(tmp_path)
    _interrupt_when(lambda: len(calls) == attacktrail_cli.WORKERS, marks)
    try:
        with pytest.raises(KeyboardInterrupt):       # re-raised for eval, not turned into a crashed result
            AttackTrailCLIAgent(model="m").run(
                Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=tmp_path / "out", timeout_s=600, env={}))
        escaped = time.monotonic() - marks[0]
    finally:
        release.set()
    assert escaped < CTRL_C_S, escaped
    time.sleep(1.0)                                  # freed workers must not take the queued files
    assert len(calls) == attacktrail_cli.WORKERS     # 12 files, only the ones in flight at the interrupt were sent
    rows = [json.loads(l) for l in (tmp_path / "out" / "raw" / "scan-cli.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [list(r) for r in rows] == [["_run"]]     # the result file was closed intact (header only: nothing finished)


def test_cli_ctrl_c_kills_running_claude_processes_and_starts_none(tmp_path, monkeypatch):
    # Real child processes: argv is swapped for a fake `claude` script, everything else (Popen, stdin, env, the
    # process registry) is the production path. The call shape test below pins that the argv/stdin/env are unchanged.
    script, scratch, marks = tmp_path / "fake_claude.py", tmp_path / "scratch", []
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    scratch.mkdir()
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "cli_argv",
                        lambda exe, model, system, schema: [sys.executable, str(script), str(scratch)])
    repo = _twelve_files(tmp_path)
    _interrupt_when(lambda: len(list(scratch.glob("alive-*"))) == attacktrail_cli.WORKERS, marks)
    with pytest.raises(KeyboardInterrupt):
        AttackTrailCLIAgent(model="m").run(
            Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=tmp_path / "out", timeout_s=600, env={}))
    assert time.monotonic() - marks[0] < CTRL_C_S
    beats = {p.name: p.stat().st_mtime_ns for p in scratch.glob("alive-*")}
    time.sleep(1.0)
    assert {p.name: p.stat().st_mtime_ns for p in scratch.glob("alive-*")} == beats   # no heartbeat: all killed
    assert len(beats) == attacktrail_cli.WORKERS      # and no process was started after the interrupt


def test_cli_closed_run_starts_no_process(tmp_path, monkeypatch):
    # The race Ctrl-C can hit: a worker picked the next file but has not spawned claude yet when the run is closed.
    marker, script, cwd = tmp_path / "started", tmp_path / "fake_claude.py", tmp_path / "cwd"
    script.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('x')\n", encoding="utf-8")
    cwd.mkdir()
    monkeypatch.setattr(attacktrail_cli, "cli_argv",
                        lambda exe, model, system, schema: [sys.executable, str(script), str(marker)])
    attacktrail_cli.kill_live(str(cwd))
    with pytest.raises(RuntimeError, match="interrupted"):
        attacktrail_cli.call_claude("claude", "m", "sys", {}, "prompt", str(cwd), 10)
    assert not marker.exists()


def test_cli_call_shape_is_unchanged_by_popen(tmp_path, monkeypatch):
    # cli-v1 stays only if argv, stdin and env are what subprocess.run(argv, input=prompt, capture_output=True,
    # text=True, encoding="utf-8", errors="replace", cwd=cwd, env=cli_env(), timeout=t) gave. What claude reads
    # (argv, stdin, env, cwd) is the same; only where its output lands moved from pipes to temp files (D-22).
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            seen.update(argv=argv, kw=kw)
            self.returncode = 0

        def communicate(self, input=None, timeout=None):
            seen.update(input=input, timeout=timeout)
            seen["kw"]["stdout"].write(b'{"structured_output": {"candidates": []}}')
            return None, None
    monkeypatch.setattr(attacktrail_cli.subprocess, "Popen", FakePopen)
    cwd = str(tmp_path)
    d = attacktrail_cli.call_claude("claude", "m", "SYS", {"a": 1}, "PROMPT", cwd, 12.5)
    assert d["structured_output"] == {"candidates": []}
    assert seen["argv"] == attacktrail_cli.cli_argv("claude", "m", "SYS", {"a": 1})
    kw = dict(seen["kw"])
    out, err = kw.pop("stdout"), kw.pop("stderr")
    assert kw == dict(stdin=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", cwd=cwd,
                      env=attacktrail_cli.cli_env())
    assert out is not subprocess.PIPE and err is not subprocess.PIPE and out is not err
    assert out.closed and err.closed            # temp files, closed (and so deleted) once the call returned
    assert (seen["input"], seen["timeout"]) == ("PROMPT", 12.5)


# A `claude` stand-in that starts a descendant holding the inherited stdout/stderr for 60 s, then works for 60 s.
# The descendant writes its pid so the test can end it.
FAKE_CLAUDE_WITH_DESCENDANT = """
import os, pathlib, subprocess, sys, time
d = pathlib.Path(sys.argv[1])
sys.stdin.read()
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # inherits stdout/stderr
(d / ("grandchild-%d" % g.pid)).write_text("x")
(d / ("alive-%d" % os.getpid())).write_text("x")
time.sleep(60)
"""


def _end_grandchildren(scratch):
    import os, signal
    for p in scratch.glob("grandchild-*"):
        try:
            os.kill(int(p.name.split("-")[1]), signal.SIGTERM)
        except OSError:
            pass


def test_cli_timeout_is_not_held_by_a_descendant_of_claude(tmp_path, monkeypatch):
    # D-22: with stdout/stderr on pipes, a descendant that inherited them kept them open after claude was killed,
    # and communicate() waited for their EOF: a 5 s project timeout returned after 60 s.
    script, scratch = tmp_path / "fake_claude.py", tmp_path / "scratch"
    script.write_text(FAKE_CLAUDE_WITH_DESCENDANT, encoding="utf-8")
    scratch.mkdir()
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "cli_argv",
                        lambda exe, model, system, schema: [sys.executable, str(script), str(scratch)])
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "A.sol").write_text("contract A {}\n", encoding="utf-8")
    t0 = time.monotonic()
    try:
        res = AttackTrailCLIAgent(model="m").run(
            Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=tmp_path / "out", timeout_s=5, env={}))
        elapsed = time.monotonic() - t0
    finally:
        _end_grandchildren(scratch)
    assert list(scratch.glob("grandchild-*")), "the fake claude never started its descendant"
    assert res.meta.status == "crashed" and elapsed < 5 + 10, (res.meta.error, elapsed)


def test_cli_ctrl_c_is_not_held_by_a_descendant_of_claude(tmp_path, monkeypatch):
    # The same descendant after Ctrl-C: run() let go at once even before D-22, but the pool's worker stayed in
    # communicate() waiting for the pipe's EOF, and the interpreter joins that thread before the process can end.
    script, scratch, marks = tmp_path / "fake_claude.py", tmp_path / "scratch", []
    script.write_text(FAKE_CLAUDE_WITH_DESCENDANT, encoding="utf-8")
    scratch.mkdir()
    monkeypatch.setattr(attacktrail_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(attacktrail_cli, "cli_argv",
                        lambda exe, model, system, schema: [sys.executable, str(script), str(scratch)])
    repo = _twelve_files(tmp_path)
    before = set(threading.enumerate())
    _interrupt_when(lambda: len(list(scratch.glob("alive-*"))) == attacktrail_cli.WORKERS, marks)
    try:
        with pytest.raises(KeyboardInterrupt):
            AttackTrailCLIAgent(model="m").run(
                Scope("p", repo), RunContext(run_dir=tmp_path, project_dir=tmp_path / "out", timeout_s=600, env={}))
        workers = [t for t in threading.enumerate() if t not in before and not t.daemon]
        end = time.monotonic() + CTRL_C_S
        while time.monotonic() < end and any(t.is_alive() for t in workers):
            time.sleep(0.05)
        still = [t.name for t in workers if t.is_alive()]
    finally:
        _end_grandchildren(scratch)
    assert not still, "pool workers still waiting %.1f s after Ctrl-C: %s" % (CTRL_C_S, still)


def test_cli_adapter_refuses_an_effort_argument(tmp_path):
    # review: `eval run --effort` is refused for the CLI adapter (cli.py _agent_opts), but the constructor accepted one and
    # fingerprints() then said "n/a" while the caller believed it had set something. Same rule on both paths: ValueError.
    for effort in ("xhigh", "high"):
        with pytest.raises(ValueError, match="effort is not supported by the attacktrail-cli adapter"):
            AttackTrailCLIAgent(effort=effort)
    from adapters import get_agent
    with pytest.raises(ValueError, match="attacktrail-cli"):
        get_agent("attacktrail-cli", effort="high")
    assert get_agent("attacktrail-cli", effort=None).effort() == "n/a"    # None is dropped like any unset CLI flag
    assert AttackTrailCLIAgent(model="m1").model() == "m1"                # model is still accepted


def test_cli_adapter_refuses_a_max_tokens_argument_and_records_n_a(tmp_path):
    # D-28: same rule as effort. `claude -p` is called with --model only, so a max_tokens given here would never reach the
    # model; `eval run --max-tokens` is refused for this agent (cli.py _agent_opts) and so is the constructor.
    from adapters import get_agent
    for mt in (64000, 128000):
        with pytest.raises(ValueError, match="max_tokens is not supported by the attacktrail-cli adapter"):
            AttackTrailCLIAgent(max_tokens=mt)
    with pytest.raises(ValueError, match="attacktrail-cli"):
        get_agent("attacktrail-cli", max_tokens=128000)
    agent = get_agent("attacktrail-cli", max_tokens=None)               # None is dropped like any unset CLI flag
    assert agent.max_tokens() == "n/a" and agent.fingerprints()["max_tokens"] == "n/a"
    assert AttackTrailAgent().fingerprints()["max_tokens"] == 64000     # the API adapter records the number
