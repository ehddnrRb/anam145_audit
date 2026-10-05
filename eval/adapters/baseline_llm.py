"""SCABench's upstream baseline (datasets/scabench/baseline-runner/baseline_runner.py).

precomputed: reads the GPT-5 outputs shipped with the dataset. No network, no key.
live: runs baseline_runner.py with only OPENAI_API_KEY in its env.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from adapters.base import (EVAL_DIR, Agent, Evidence, Finding, Findings, Meta,
                           RunContext, Scope, map_severity, scoped_env)

SCABENCH = EVAL_DIR / "datasets" / "scabench"
RESULTS_DIR = SCABENCH / "datasets" / "curated-2025-08-18" / "baseline-results"
RUNNER_PY = SCABENCH / "baseline-runner" / "baseline_runner.py"
KEY = "OPENAI_API_KEY"
PRECOMPUTED_MODEL = "gpt-5 (precomputed upstream)"


def parse_baseline_json(path: Path) -> tuple[list[Finding], dict, int]:
    """Returns (findings, token_usage, count of unknown severities mapped to info)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out, unknown = [], 0
    for i, f in enumerate(data.get("findings") or []):
        sev = map_severity(f.get("severity"))
        if sev is None:
            unknown += 1
            sev = "info"
        conf = f.get("confidence")
        if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
            conf = None
        out.append(Finding(
            id=f.get("id") or "bl-%d" % i,
            title=f.get("title") or "",
            file=f.get("file"),
            lines=[],
            severity=sev,
            description=f.get("description") or "",
            confidence=conf,
            # location is a free-text string, or occasionally a list of strings
            evidence=Evidence(tool_output=f.get("location")),
        ))
    return out, data.get("token_usage") or {}, unknown


class BaselineAgent(Agent):
    name = "baseline"

    def __init__(self, mode: str = "precomputed", model: str = "gpt-5-mini",
                 results_dir: Path = RESULTS_DIR, executor=None):
        if mode not in ("precomputed", "live"):
            raise ValueError("mode must be precomputed or live, got %r" % mode)
        self.mode = mode
        self._model = model
        self.results_dir = Path(results_dir)
        self._executor = executor

    def model(self) -> str:
        return PRECOMPUTED_MODEL if self.mode == "precomputed" else self._model

    def prompt_hash(self) -> str:
        # The prompts live inline in baseline_runner.py.
        try:
            return hashlib.sha256(RUNNER_PY.read_bytes()).hexdigest()
        except OSError:
            return "unknown"

    def run(self, scope: Scope, ctx: RunContext) -> Findings:
        try:
            if self.mode == "precomputed":
                return self._load(self.results_dir / ("baseline_%s.json" % scope.project_id),
                                  None)
            return self._live(scope, ctx)
        except Exception as e:  # adapter must never raise for agent-side failures
            return self.failed("crashed", "%s: %s" % (type(e).__name__, e))

    def _load(self, path: Path, wall_seconds: float | None) -> Findings:
        if not path.is_file():
            return self.failed("crashed", "baseline result not found: %s" % path.name,
                               wall_seconds)
        findings, usage, unknown = parse_baseline_json(path)
        return Findings(vulnerabilities=findings, meta=Meta(
            agent=self.name, agent_version=self.version(), model=self.model(),
            tokens_in=usage.get("input_tokens"), tokens_out=usage.get("output_tokens"),
            cost_usd=None,  # no verified price
            wall_seconds=wall_seconds, status="ok",
            error=("%d findings had unknown severity, mapped to info" % unknown)
            if unknown else None))

    def _live(self, scope: Scope, ctx: RunContext) -> Findings:
        if not (ctx.env.get(KEY) or os.environ.get(KEY)):
            return self.failed("crashed", KEY + " not set")
        raw_dir = Path(ctx.project_dir) / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, str(RUNNER_PY), "--project", scope.project_id,
                "--source", str(scope.repo_path), "--output", str(raw_dir),
                "--model", self._model]
        executor = self._executor
        if executor is None:
            from runner.executor import LocalExecutor
            executor = LocalExecutor()
        res = executor.run(argv, cwd=RUNNER_PY.parent, env=scoped_env(ctx, KEY),
                           timeout_s=ctx.timeout_s)
        (raw_dir / "baseline.stdout.txt").write_text(res.stdout or "", encoding="utf-8")
        (raw_dir / "baseline.stderr.txt").write_text(res.stderr or "", encoding="utf-8")
        if res.timed_out:
            return self.failed("timeout", "baseline_runner exceeded %ss" % ctx.timeout_s,
                               res.seconds)
        if res.returncode != 0:
            return self.failed("crashed", "baseline_runner exit %s: %s"
                               % (res.returncode, (res.stderr or "")[-2000:]), res.seconds)
        return self._load(raw_dir / ("baseline_%s.json" % scope.project_id), res.seconds)
