"""Adapter for the AttackTrail Step-01 scanner (internal/tools/scan.py).

scan.py is never modified. It is run as `python scan.py <repo> [--model M] [--web]`
and writes internal/tools/scan-<commit8>.jsonl next to itself; this adapter moves
that file into <project_dir>/raw/ and parses it.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from adapters.base import (REPO_ROOT, Agent, Evidence, Finding, Findings, Meta,
                           RunContext, Scope, map_severity, scoped_env)

SCAN_PY = REPO_ROOT / "internal" / "tools" / "scan.py"
KEY = "ANTHROPIC_API_KEY"


def _scan_constants(scan_py: Path) -> dict:
    """Read STD_BASE, STD_WEB and the --model default from scan.py's source (no import)."""
    tree = ast.parse(scan_py.read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in ("STD_BASE", "STD_WEB"):
            out[node.targets[0].id] = ast.literal_eval(node.value)
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_argument" \
                and node.args and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value == "--model":
            for kw in node.keywords:
                if kw.arg == "default":
                    out["default_model"] = ast.literal_eval(kw.value)
    return out


def parse_scan_jsonl(path: Path) -> tuple[dict, list[Finding], int]:
    """Returns (_run header, triage candidates as Findings, count skipped for empty text).

    Candidates with _status != "triage" (rejected_by_scanner) are dropped: scan.py
    itself withholds them from human reviewers.
    """
    run: dict = {}
    findings: list[Finding] = []
    skipped = 0
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            c = json.loads(line)
            if "_run" in c:
                run = c["_run"]
                continue
            if c.get("_status") != "triage":
                continue
            why = (c.get("why") or "").strip()
            poc = (c.get("poc_sketch") or "").strip()
            desc = why + ("\n\nPoC sketch: " + poc if poc else "")
            if not desc.strip():
                skipped += 1
                continue
            ln = c.get("line")
            findings.append(Finding(
                id="at-%d" % n,
                title=c.get("title") or "",
                file=c.get("file"),
                lines=[ln] if isinstance(ln, int) and ln > 0 else [],
                # scan.py's schema enum is Critical/High/Medium/Low/Informational
                severity=map_severity(c.get("severity_guess")) or "info",
                description=desc.strip(),
                confidence=None,
                evidence=Evidence(tool_output={
                    "code_excerpt": c.get("code_excerpt"),
                    "category": c.get("category"),
                    "_verify": c.get("_verify"),
                }),
            ))
    return run, findings, skipped


class AttackTrailAgent(Agent):
    name = "attacktrail"

    def __init__(self, model: str | None = None, web: bool = False,
                 python: str | None = None, scan_py: Path = SCAN_PY, executor=None):
        self._model = model
        self.web = web
        # scan.py needs the anthropic SDK, which the eval venv does not have.
        self.python = python or sys.executable
        self.scan_py = Path(scan_py)
        self._executor = executor

    def _consts(self) -> dict:
        try:
            return _scan_constants(self.scan_py)
        except OSError:
            return {}

    def model(self) -> str:
        return self._model or self._consts().get("default_model", "unknown")

    def prompt_hash(self) -> str:
        """sha256 over scan.py bytes plus every standards file it loads."""
        h = hashlib.sha256()
        try:
            h.update(self.scan_py.read_bytes())
        except OSError:
            return "unknown"
        c = self._consts()
        names = list(c.get("STD_BASE", [])) + (list(c.get("STD_WEB", [])) if self.web else [])
        std_dir = self.scan_py.parent / ".." / "docs"  # scan.py's STD_DIR
        for name in names:
            p = std_dir / name
            h.update(name.encode("utf-8"))
            h.update(p.read_bytes() if p.is_file() else b"<missing>")
        return h.hexdigest()

    def run(self, scope: Scope, ctx: RunContext) -> Findings:
        if not (ctx.env.get(KEY) or os.environ.get(KEY)):
            return self.failed("crashed", KEY + " not set")
        if not self.scan_py.is_file():
            return self.failed("crashed", "scan.py not found at %s" % self.scan_py)
        try:
            return self._run(scope, ctx)
        except Exception as e:  # adapter must never raise for agent-side failures
            return self.failed("crashed", "%s: %s" % (type(e).__name__, e))

    def _run(self, scope: Scope, ctx: RunContext) -> Findings:
        argv = [self.python, str(self.scan_py), str(scope.repo_path)]
        if self._model:
            argv += ["--model", self._model]
        if self.web:
            argv.append("--web")

        # Same commit lookup scan.py does, to know which file it will write.
        from runner.checkout import git_env
        commit = subprocess.run(["git", "-C", str(scope.repo_path), "rev-parse", "HEAD"],
                                capture_output=True, text=True, env=git_env()).stdout.strip() or "unknown"
        produced = self.scan_py.parent / ("scan-%s.jsonl" % commit[:8])

        executor = self._executor
        if executor is None:
            from runner.executor import LocalExecutor
            executor = LocalExecutor()
        started = time.time()
        res = executor.run(argv, cwd=self.scan_py.parent, env=scoped_env(ctx, KEY),
                           timeout_s=ctx.timeout_s)

        raw_dir = Path(ctx.project_dir) / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "scan.stdout.txt").write_text(res.stdout or "", encoding="utf-8")
        (raw_dir / "scan.stderr.txt").write_text(res.stderr or "", encoding="utf-8")
        raw = None
        # mtime guard: never pick up a stale file left by an earlier run.
        if produced.is_file() and produced.stat().st_mtime >= started - 1:
            raw = raw_dir / produced.name
            shutil.move(str(produced), str(raw))

        if res.timed_out:
            return self.failed("timeout", "scan.py exceeded %ss" % ctx.timeout_s, res.seconds)
        if res.returncode != 0:
            return self.failed("crashed", "scan.py exit %s: %s"
                               % (res.returncode, (res.stderr or "")[-2000:]), res.seconds)
        if raw is None:
            return self.failed("crashed", "scan.py produced no fresh %s" % produced.name,
                               res.seconds)

        _, findings, skipped = parse_scan_jsonl(raw)
        notes = []
        # scan.py logs "  [i/n] <file>  ERROR <exc>" and skips that file
        errored = sum(1 for ln in (res.stdout or "").splitlines() if "]  ERROR " in ln)
        if errored:
            notes.append("%d files failed inside scan.py and were not scanned" % errored)
        if skipped:
            notes.append("%d triage candidates dropped for empty why/poc_sketch" % skipped)
        return Findings(vulnerabilities=findings, meta=Meta(
            agent=self.name, agent_version=self.version(), model=self.model(),
            tokens_in=None, tokens_out=None, cost_usd=None,  # scan.py does not record usage
            wall_seconds=res.seconds, status="ok", error="; ".join(notes) or None))
