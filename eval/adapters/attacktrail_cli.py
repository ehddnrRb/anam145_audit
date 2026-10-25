"""AttackTrail Step-01 scan through the Claude Code CLI (`claude -p`) instead of the API.

scan.py is never modified. Its ROLE, SCHEMA, ALL_EXT, load_standards(), select_files()
and verify() are imported as-is; only the model call is swapped for one `claude -p` per file. Output is
the same JSONL scan.py writes, plus one {"_usage": ...} line per file, so
parse_scan_jsonl() reads it unchanged.

Not identical to scan.py: no system-block cache_control, no effort and no server-side
fallback (the CLI gets `--model` only), structured output goes through the CLI's
--json-schema, and cost is the CLI's `total_cost_usd`, which is a list-price figure
(costBasis "list"), not what a subscription is billed.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from adapters.attacktrail import (NO_FILES_COVERAGE, AttackTrailAgent, coverage_meta, load_scan_module,
                                  parse_scan_jsonl, system_prompt, too_big_reason)
from adapters.base import OS_ENV_KEYS, Findings, Meta, RunContext, Scope

# The CLI finds its login under the user profile. No API keys are passed.
PROFILE_ENV_KEYS = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "HOME")
# Strip Claude Code down to a bare model call: no tools, MCP, skills, settings/hooks
# or session files. Measured 2026-10-13: ~700 input tokens of fixed overhead per call
# instead of ~54,700 with defaults.
CLI_FLAGS = ["--output-format", "json", "--tools", "", "--strict-mcp-config",
             "--setting-sources", "", "--disable-slash-commands",
             "--no-session-persistence", "--exclude-dynamic-system-prompt-sections"]
WORKERS = 4  # ponytail: fixed parallelism; make it a flag if rate limits bite
# Windows CreateProcess caps the whole command line at 32,767 chars including the NUL.
# Linux caps a single argument at 128 KiB. The Windows cap binds first.
ARGV_LIMIT = 32_767


def claude_bin() -> str | None:
    """npm installs a .cmd shim; call the real exe so cmd.exe never re-parses the prompt."""
    found = shutil.which("claude")
    if found and found.lower().endswith((".cmd", ".ps1")):
        exe = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        return str(exe) if exe.is_file() else None
    return found


def cli_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in OS_ENV_KEYS + PROFILE_ENV_KEYS if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def cli_argv(exe: str, model: str, system: str, schema: dict) -> list[str]:
    return [exe, "-p", "--model", model, "--system-prompt", system,
            "--json-schema", json.dumps(schema, ensure_ascii=False)] + CLI_FLAGS


# The `claude` processes running per run, keyed by that run's temp cwd (every call of a run gets the same one).
# set = open run, None = closed (kill_live ran). A closed run starts no new process, which is what stops a
# worker that is between "picked the next file" and "spawned claude" when Ctrl-C lands (D-21).
# ponytail: closed keys are never removed (one short str per interrupted run); prune if runs ever number in the millions
_live: dict[str, set | None] = {}
_live_lock = threading.Lock()


def kill_live(cwd: str) -> None:
    """Close the run and kill its running `claude` processes (Popen.kill only: no descendants)."""
    with _live_lock:
        procs = list(_live.get(cwd) or ())
        _live[cwd] = None
    for p in procs:
        try:
            p.kill()
        except OSError:  # already gone
            pass
    for p in procs:  # the temp cwd is deleted next; on Windows it cannot be while a process still has it
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


@contextlib.contextmanager
def _temp_cwd():
    """Empty temp cwd for the claude processes. If it cannot be removed (a leftover process still holds it on
    Windows), say so on stderr instead of leaving an at-cli-* dir behind silently (D-22)."""
    d = tempfile.mkdtemp(prefix="at-cli-")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d)
        except OSError as e:
            print("attacktrail-cli: could not remove temp cwd %s: %s" % (d, e), file=sys.stderr)


@contextlib.contextmanager
def interruptible_pool(cwd: str):
    """ThreadPoolExecutor that never makes the caller wait for calls in flight. On any exit, Ctrl-C included:
    queued files are cancelled, running `claude` processes are killed, and the exception goes on up so eval
    handles the KeyboardInterrupt itself (this runs inside eval: no os._exit, unlike scan.py)."""
    pool = ThreadPoolExecutor(WORKERS)
    try:
        yield pool
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # not wait=True: that joins the running calls
        kill_live(cwd)


def _read_back(fh) -> str:
    fh.seek(0)
    return fh.read().decode("utf-8", "replace")


def call_claude(exe: str, model: str, system: str, schema: dict, prompt: str,
                cwd: str, timeout_s: float) -> dict:
    """One `claude -p` call. The file prompt goes on stdin. The system prompt goes in
    argv, so it must fit ARGV_LIMIT; _run_cli checks that once before any call.

    Same argv, stdin and env as the old subprocess.run (call shape cli-v1); Popen only so the process can be
    killed from another thread (kill_live) instead of running on after Ctrl-C.

    stdout/stderr go to temp files, not pipes (D-22). A descendant of claude that inherits a pipe keeps it open
    after claude is killed, and communicate() then waits for that pipe's EOF: measured 60 s past Ctrl-C and
    past a 5 s project timeout. With files, communicate() only waits for claude itself."""
    argv = cli_argv(exe, model, system, schema)
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        with _live_lock:
            procs = _live.setdefault(cwd, set())  # a cwd nobody registered (direct caller) is tracked from here
            if procs is None:
                raise RuntimeError("interrupted: run closed, claude not started")
            p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out_f, stderr=err_f,
                                 text=True, encoding="utf-8", errors="replace", cwd=cwd, env=cli_env())
            procs.add(p)
        try:
            try:
                p.communicate(prompt, timeout=timeout_s)
            except BaseException:  # timeout or interrupt: what subprocess.run does, kill and reap
                p.kill()
                p.communicate()
                raise
        finally:
            with _live_lock:
                procs.discard(p)
        stdout, stderr = _read_back(out_f), _read_back(err_f)
    try:
        d = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError("exit %s: %s" % (p.returncode, (stderr or stdout)[-500:]))
    if d.get("is_error") or d.get("structured_output") is None:
        raise RuntimeError("%s: %s" % (d.get("subtype"), str(d.get("result"))[:500]))
    return d


class AttackTrailCLIAgent(AttackTrailAgent):
    name = "attacktrail-cli"
    # system prompt in argv (--system-prompt), file on stdin, --json-schema, CLI_FLAGS.
    # Bump when call_claude() or CLI_FLAGS change.
    call_shape = "cli-v1"
    default_timeout_s = 1800  # not the API adapter's 3 h (D-24): no per-call limits here, the call gets what is left

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Same rule as `eval run --effort` (cli.py _agent_opts): `claude -p` is called with --model only, so an effort
        # given here would be silently dropped. Refuse it instead of recording a setting that never reached the model.
        if self._effort is not None:
            raise ValueError("effort is not supported by the attacktrail-cli adapter: `claude -p` is called "
                             "with --model only. Use --agent attacktrail for --effort")
        # D-28: same rule for max_tokens. `claude -p` is given no output limit by this adapter.
        if self._max_tokens is not None:
            raise ValueError("max_tokens is not supported by the attacktrail-cli adapter: `claude -p` is called "
                             "with --model only. Use --agent attacktrail for --max-tokens")

    def max_tokens(self) -> str:
        """`claude -p` is called with --model only: this adapter sets no max_tokens (D-28)."""
        return "n/a"

    def effort(self) -> str:
        """`claude -p` is called with --model only: no effort and no server-side fallback (D-23).
        The model follows scan.py's --model default, so it is claude-fable-5-1 like the API adapter."""
        return "n/a"

    def prompt_hash(self) -> str:
        h = hashlib.sha256(super().prompt_hash().encode())
        h.update(Path(__file__).read_bytes())  # call shape lives here, not in scan.py
        return h.hexdigest()

    def run(self, scope: Scope, ctx: RunContext) -> Findings:
        exe = claude_bin()
        if not exe:
            return self.failed("crashed", "claude CLI not found on PATH")
        try:
            self.snapshot(ctx.run_dir)  # scan.py + standards, once per run dir (D-20)
            return self._run_cli(exe, scope, ctx)
        except Exception as e:  # adapter must never raise for agent-side failures
            return self.failed("crashed", "%s: %s" % (type(e).__name__, e))

    def _run_cli(self, exe: str, scope: Scope, ctx: RunContext) -> Findings:
        scan = load_scan_module(self.scan_py)
        root = str(scope.repo_path)
        model = self.model()
        system = system_prompt(scan, self.web)
        n = len(subprocess.list2cmdline(cli_argv(exe, model, system, scan.SCHEMA)))
        if n >= ARGV_LIMIT:
            return self.failed("crashed", "system prompt does not fit argv: command line %d chars, "
                               "limit %d. Run web scans with --agent attacktrail (DECISIONS D-15)"
                               % (n, ARGV_LIMIT))
        files, too_big = scan.select_files(root, self.web)  # same selection as scan.py main()
        if not files:  # D-27: the same tool limit the API adapter reads from scan.py's exit 1; here select_files is called directly
            # D-28: --web with every candidate over MAX_BYTES_WEB is told apart from "nothing of a supported type"
            return self.failed("no_files", "no files selected under %s%s" % (
                os.path.abspath(root), " (%s)" % too_big_reason(len(too_big)) if too_big else ""), **NO_FILES_COVERAGE)
        started = time.time()
        deadline = started + ctx.timeout_s

        def one(p: str):
            rel = os.path.relpath(p, root).replace("\\", "/")
            # Reading the file and building the prompt are inside the try: a directory named X.sol or one
            # unreadable file fails that file only. Outside it the exception surfaced in the writer loop
            # (pool.map) and run() recorded the whole project as crashed (D-20).
            try:
                src = io.open(p, encoding="utf-8", errors="replace").read()
                numbered = "\n".join("%d\t%s" % (n + 1, l) for n, l in enumerate(src.splitlines()))
                prompt = ("File: %s (%s)\nLine numbers are prefixed and tab-separated; "
                          "report the file's own numbering.\n\n%s"
                          % (rel, scan.ALL_EXT[os.path.splitext(p)[1]], numbered))
                left = deadline - time.time()
                if left <= 0:
                    return rel, None, None, "skipped: project timeout reached"
                d = call_claude(exe, model, system, scan.SCHEMA, prompt, cwd, left)
            except Exception as e:
                return rel, None, None, "%s: %s" % (type(e).__name__, e)
            # One file's odd candidate types (line "5", null, ...) must fail that file only; in the
            # writer loop below they would raise inside verify() and kill the whole project (D-19).
            so = d["structured_output"]
            try:
                cands = scan.check_candidates(so.get("candidates") if isinstance(so, dict) else None)
            except Exception as e:
                return rel, d, None, "%s: %s" % (type(e).__name__, e)  # d kept: the call was billed
            return rel, d, cands, None

        raw_dir = Path(ctx.project_dir) / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        out = raw_dir / "scan-cli.jsonl"
        log = []
        failed, not_sent = [], []  # coverage: (file, reason) of failed calls / reasons files were never sent
        kept = dropped = 0
        tokens_in = tokens_out = 0
        cost = 0.0
        # Empty cwd: no project CLAUDE.md or settings get discovered.
        # Exit order matters (last opened, first closed): the pool is shut down and its claude processes killed
        # before the file closes and the temp cwd is deleted (Windows cannot delete a dir a process runs in).
        with _temp_cwd() as cwd, \
                io.open(out, "w", encoding="utf-8") as fh, interruptible_pool(cwd) as pool:
            run = {
                "started": datetime.datetime.now().isoformat(timespec="seconds"),
                "backend": "claude-cli", "model": model, "target": os.path.abspath(root),
                "standards": scan.STD_BASE + (scan.STD_WEB if self.web else []),
                "prompt_sha256": hashlib.sha256(system.encode("utf-8")).hexdigest()[:16],
                "files": len(files)}
            if self.web:
                run["skipped_large"] = too_big
            fh.write(json.dumps({"_run": run}, ensure_ascii=False) + "\n")
            futs = [pool.submit(one, p) for p in files]
            # Results in file order, as pool.map gave them. pool.map waits on each future without a timeout, and on
            # Windows that wait is not woken by Ctrl-C, so the interrupt only arrived when the first call ended and
            # new calls kept starting meanwhile. scan.wait_result polls every POLL_S (0.5 s) instead (D-21).
            for rel, d, cands, err in (scan.wait_result(f) for f in futs):
                if d is not None:  # a response came back, so its usage was billed even if err is set
                    u = d.get("usage") or {}
                    t_in = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens"))
                    tokens_in += t_in
                    tokens_out += u.get("output_tokens") or 0
                    cost += d.get("total_cost_usd") or 0.0
                    # input_tokens stays the sum of the three kinds (how this row has always read). The raw
                    # values sit beside it under the names scan.py's API rows use; input_tokens_uncached
                    # is what the API row calls input_tokens (DECISIONS D-20 table).
                    row = {"file": rel, "input_tokens": t_in, "input_tokens_uncached": u.get("input_tokens"),
                           "cache_creation_input_tokens": u.get("cache_creation_input_tokens"),
                           "cache_read_input_tokens": u.get("cache_read_input_tokens"),
                           "output_tokens": u.get("output_tokens"),
                           "cost_usd": d.get("total_cost_usd"), "num_turns": d.get("num_turns")}
                    row.update({k: u[k] for k in ("cache_creation", "service_tier", "speed", "inference_geo")
                                if u.get(k) is not None})
                    fh.write(json.dumps({"_usage": row}) + "\n")
                if err:
                    log.append("%s  ERROR %s" % (rel, err))
                    if err.startswith("skipped:"):
                        not_sent.append(err[len("skipped: "):])
                    else:
                        failed.append((rel, err))
                    continue
                for c in cands:
                    c["file"] = rel
                    c["_verify"] = scan.verify(root, c)
                    c["_status"] = "triage" if c["_verify"].startswith("OK") else "rejected_by_scanner"
                    kept += c["_status"] == "triage"
                    dropped += c["_status"] != "triage"
                    fh.write(json.dumps(c, ensure_ascii=False) + "\n")
                log.append("%s  후보 %d" % (rel, len(cands)))
        seconds = round(time.time() - started, 3)
        log.append("검토 대기 %d · 위치검증 실패로 폐기 %d" % (kept, dropped))
        (raw_dir / "scan.stdout.txt").write_text("\n".join(log), encoding="utf-8")

        errored = sum(1 for ln in log if "  ERROR " in ln)
        if files and errored == len(files):
            return self.failed("crashed", "every CLI call failed; first: %s" % log[0], seconds)
        _, findings, skipped = parse_scan_jsonl(out)
        # Coverage (D-20). Files over the --web size cap were never sent either (scan.select_files).
        scanned = len(files) - len(failed) - len(not_sent)
        cov, memo = coverage_meta(len(files) + len(too_big), scanned, failed,
                                  not_sent + ["too large for --web"] * len(too_big))
        notes = [memo] if memo else []
        if skipped:
            notes.append("%d triage candidates dropped for empty why/poc_sketch" % skipped)
        return Findings(vulnerabilities=findings, meta=Meta(
            agent=self.name, agent_version=self.version(), model=model,
            tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=round(cost, 6),
            wall_seconds=seconds, status="ok", error="; ".join(notes) or None, **cov))
