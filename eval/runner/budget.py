"""API spend cap for `eval run` (D-34, user decision 2026-10-29: at most $100 from here on).

The ledger is one JSON line per project that spent on the API ({run, project, model, cost_usd, at}; `unpriced: true`
when the project ran but no price was known, so the row is $0 and visible). `check()` runs before a run starts and
refuses once the ledger has reached the cap; `record()` appends a run's projects from its manifest, scoring failure
included (the manifest is written either way). Both read the cap and the ledger path at call time: ATTACKTRAIL_BUDGET_USD
(default 100) and ATTACKTRAIL_SPEND_LEDGER (default runs/spend_ledger.jsonl, git-ignored). Tests point the ledger at a
temp file (conftest), so bare `pytest` never writes the real one.
ponytail: pre-run check only. A run is not stopped mid-way; with Haiku 5.5 a run costs cents, and a Fable max run
(about $60 for the 4-project set) is an explicit --model/--effort choice.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CAP_USD = 100.0
DEFAULT_LEDGER = Path(__file__).resolve().parents[1] / "runs" / "spend_ledger.jsonl"


def cap_usd() -> float:
    raw = os.environ.get("ATTACKTRAIL_BUDGET_USD")
    if raw is None or not raw.strip():
        return DEFAULT_CAP_USD
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"ATTACKTRAIL_BUDGET_USD 는 달러 숫자여야 한다: {raw!r}") from None


def ledger_path() -> Path:
    return Path(os.environ.get("ATTACKTRAIL_SPEND_LEDGER") or DEFAULT_LEDGER)


def spent(ledger: Path | None = None) -> float:
    ledger = ledger or ledger_path()
    if not ledger.exists():
        return 0.0
    total = 0.0
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if line.strip():
            total += float(json.loads(line).get("cost_usd") or 0)
    return total


def check(cap: float | None = None, ledger: Path | None = None) -> float:
    """Spent so far; raises RuntimeError when it has reached the cap (nothing is run)."""
    cap = cap_usd() if cap is None else cap
    ledger = ledger or ledger_path()
    used = spent(ledger)
    if used >= cap:
        raise RuntimeError(f"API 지출 상한 도달: 장부 ${used:.2f} >= 상한 ${cap:.2f} ({ledger}). "
                           f"상한을 올리려면 ATTACKTRAIL_BUDGET_USD, 새 예산이면 장부를 옮긴다 (D-34)")
    return used


def _spent_on_api(p: dict) -> bool:
    """A project that ran and got billed: excluded, crashed, no_files and baseline (precomputed) projects have no spend."""
    return p.get("agent_status") == "ok" and p.get("tokens_in") is not None


def record(run_dir: Path, ledger: Path | None = None) -> float:
    """Append the run's API-spending projects from its manifest; returns the run's priced total."""
    ledger = ledger or ledger_path()
    man = json.loads((Path(run_dir) / "manifest.json").read_text(encoding="utf-8"))
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total = 0.0
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as fh:
        for p in man.get("projects") or []:
            cost = p.get("cost_usd")
            if cost is None and not _spent_on_api(p):
                continue
            row = {"run": Path(run_dir).name, "project": p.get("project_id"),
                   "model": (man.get("agent") or {}).get("model"), "cost_usd": cost or 0.0, "at": at}
            if cost is None:
                row["unpriced"] = True
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            total += cost or 0.0
    return total
