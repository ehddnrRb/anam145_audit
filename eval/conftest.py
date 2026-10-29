import sys
from pathlib import Path

_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

# bare `pytest` must never import code from cloned target repos or the scabench submodule
collect_ignore = ["datasets", "runs"]

import pytest


@pytest.fixture(autouse=True)
def _isolated_spend_ledger(tmp_path, monkeypatch):
    """D-34: every test run of the runner books spend on a temp ledger, never on runs/spend_ledger.jsonl."""
    monkeypatch.setenv("ATTACKTRAIL_SPEND_LEDGER", str(tmp_path / "spend_ledger.jsonl"))
    monkeypatch.delenv("ATTACKTRAIL_BUDGET_USD", raising=False)
