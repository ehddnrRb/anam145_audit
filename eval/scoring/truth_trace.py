"""Record, per truth, whether scoring_algo produced a verdict. Observation only: nothing is changed.

scoring_algo/core/evaluate.py:60-87 calls process_in_batches once per truth, in truth order,
and skips the truth (no entry written) when the result is falsy: no findings left to compare
(batching.py:28,33) or every judge call failed. The evaluated file carries no truth id, so
without this trace a skipped truth cannot be placed. We wrap evaluate.process_in_batches
(imported by name at evaluate.py:9) and write output_root/<repo>_truth_trace.json =
{"truth_has_verdict": [bool, ...]} in truth order.

Run as a script, it is `python -m scoring_algo.cli` with the wrapper installed:
    python truth_trace.py evaluate --no-telemetry --log-level INFO
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from scoring_algo.core import evaluate as _ev


def trace_path(repo: str, output_root: Path) -> Path:
    return Path(output_root) / f"{repo}_truth_trace.json"


@contextmanager
def traced():
    seen: dict[tuple[str, str], list[bool]] = {}
    orig = _ev.process_in_batches

    async def wrapper(**kw):
        res = await orig(**kw)
        repo, out = kw["repo_name"], Path(kw["output_root"])
        rows = seen.setdefault((str(out), repo), [])
        rows.append(bool(res))  # evaluate.py:85 skips on `not content`
        out.mkdir(parents=True, exist_ok=True)
        trace_path(repo, out).write_text(json.dumps({"truth_has_verdict": rows}), encoding="utf-8")
        return res

    with mock.patch.object(_ev, "process_in_batches", wrapper):
        yield


if __name__ == "__main__":
    from scoring_algo import cli

    sys.argv = ["scoring_algo.cli", *sys.argv[1:]]
    with traced():
        cli.app()
