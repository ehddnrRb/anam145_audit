"""Mock judge: runs the REAL scoring_algo pipeline with a fake, content-independent LLM.

Plumbing tests only. The verdict is chosen from a call counter alone; the prompt is
never read, so the output says nothing about the findings. Every judged row's
explanation starts with "MOCK JUDGE", and output_root/MOCK_JUDGE.json marks the dir.

Patch point: scoring_algo/core/batching.py:8 imports LLMClient by name and
batching.py:29 instantiates it per truth, so we replace
scoring_algo.core.batching.LLMClient. The real LLMClient (core/llm.py:14-38) is never
constructed, so there is no key check, no Settings() model check and no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from scoring_algo.core import batching
from scoring_algo.core.evaluate import run_evaluation
from scoring_algo.core.telemetry import set_telemetry
from scoring_algo.core.types import Finding
from scoring_algo.settings import ITERATIONS

from scoring.truth_trace import traced

MOCK_EXPLANATION = "MOCK JUDGE: content-independent verdict, not a real judgment"


class _MockLLMClient:
    calls = 0  # shared counter across instances; reset by run_mock

    def __init__(self, model: str):
        self.model = model

    async def generate_async(self, prompt: str) -> Finding | None:  # prompt deliberately ignored
        n = _MockLLMClient.calls
        _MockLLMClient.calls += 1
        kind = (n // 2) % 3  # pairs agree, so batching.py:102 skips the 3rd call
        return Finding(
            is_match=kind == 0,
            is_partial_match=kind == 1,
            explanation=f"{MOCK_EXPLANATION} (call #{n})",
            severity_from_junior_auditor="MOCK",
            severity_from_truth="MOCK",  # overwritten from truth at evaluate.py:89-92
            index_of_finding_from_junior_auditor=0,  # always in range when a batch exists
        )


def run_mock(data_root: Path, output_root: Path, project_ids: list[str], scan_source: str) -> None:
    set_telemetry(False)  # observe() would otherwise lazily import langfuse
    _MockLLMClient.calls = 0
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "MOCK_JUDGE.json").write_text(
        json.dumps({"judge": "mock", "note": "numbers are meaningless (plumbing test)"}),
        encoding="utf-8",
    )
    with mock.patch.object(batching, "LLMClient", _MockLLMClient), traced():
        for pid in project_ids:
            run_evaluation(
                repo_name=pid,
                data_root=data_root,
                scan_source=scan_source,
                output_root=output_root,
                model="mock",
                iterations=ITERATIONS,
                batch_size=10,
                debug_prompt=False,
            )
