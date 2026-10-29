"""D-34: per-model cost table and the API spend cap."""
import json

import pytest

from adapters.attacktrail import cost_of_rows, row_cost, sum_usage
from runner import budget


def _row(model, tin, cw, cr, out, **extra):
    return {"model": model, "input_tokens": tin, "cache_creation_input_tokens": cw,
            "cache_read_input_tokens": cr, "output_tokens": out, **extra}


def test_row_cost_by_model_and_haiku_rate_card():
    assert row_cost(_row("claude-fable-5-1", 1_000_000, 0, 0, 0)) == pytest.approx(10.0)
    assert row_cost(_row("claude-fable-5-1", 0, 1_000_000, 1_000_000, 1_000_000)) == pytest.approx(12.5 + 0.25 + 50.0)
    short = row_cost(_row("claude-haiku-5-5", 50_000, 0, 0, 10_000))
    assert short == pytest.approx((50_000 * 0.10 + 10_000 * 0.50) / 1e6)
    long = row_cost(_row("claude-haiku-5-5", 1_000, 0, 110_000, 10_000))  # prompt > 100K: second rate card
    assert long == pytest.approx((1_000 * 0.50 + 110_000 * 0.05 + 10_000 * 2.50) / 1e6)
    assert row_cost(_row("claude-opus-4-8", 100, 0, 0, 0)) == pytest.approx(100 * 5.0 / 1e6)
    assert row_cost(_row("claude-unknown", 100, 0, 0, 0)) is None
    assert row_cost({"model": "claude-haiku-5-5", "input_tokens": 5, "input_tokens_uncached": 5}) is None  # CLI row


def test_cost_of_rows_sums_priced_rows_and_counts_unpriced(tmp_path):
    f = tmp_path / "scan.jsonl"
    rows = [{"_run": {}}, {"_usage": _row("claude-haiku-5-5", 1000, 0, 0, 1000)},
            {"_usage": _row("claude-haiku-5-5", 1000, 0, 0, 1000)},
            {"title": "x", "_usage": {"input_tokens": 10 ** 6}}]  # a candidate carrying _usage is not a usage row
    f.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert cost_of_rows(f) == (pytest.approx(2 * (1000 * 0.10 + 1000 * 0.50) / 1e6), 0)
    rows.append({"_usage": _row("claude-unknown", 1, 0, 0, 1)})  # one unknown model: lower bound, not None
    f.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert cost_of_rows(f) == (pytest.approx(2 * (1000 * 0.10 + 1000 * 0.50) / 1e6), 1)
    f.write_text(json.dumps({"_usage": _row("claude-unknown", 1, 0, 0, 1)}), encoding="utf-8")
    assert cost_of_rows(f) == (None, 1)
    f.write_text("", encoding="utf-8")
    assert cost_of_rows(f) == (None, 0)
    # the helpers also take a materialized list (the adapter reads the file once)
    lst = [_row("claude-haiku-5-5", 10, 0, 0, 5)]
    assert cost_of_rows(lst) == (pytest.approx((10 * 0.10 + 5 * 0.50) / 1e6), 0) and sum_usage(lst) == (10, 5)


def test_budget_check_and_record(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    assert budget.spent(ledger) == 0.0
    assert budget.check(cap=100, ledger=ledger) == 0.0
    run = tmp_path / "run-a"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({"agent": {"model": "claude-haiku-5-5"}, "projects": [
        {"project_id": "p1", "agent_status": "ok", "tokens_in": 5, "cost_usd": 60.5},
        {"project_id": "p2", "agent_status": "ok", "tokens_in": 5, "cost_usd": None},      # ran, unpriced: $0 row
        {"project_id": "p3", "agent_status": "ok", "tokens_in": None, "cost_usd": None},   # no usage rows: no row
        {"project_id": "p4", "agent_status": "crashed", "cost_usd": None},                 # never billed: no row
        {"project_id": "p5", "excluded_reason": "x", "agent_status": "excluded", "cost_usd": None}]}),
        encoding="utf-8")
    assert budget.record(run, ledger=ledger) == pytest.approx(60.5)
    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [(line["project"], line["cost_usd"]) for line in lines] == [("p1", 60.5), ("p2", 0.0)]
    assert lines[1]["unpriced"] is True and "unpriced" not in lines[0]
    assert budget.check(cap=100, ledger=ledger) == pytest.approx(60.5)
    budget.record(run, ledger=ledger)  # 121 > 100
    with pytest.raises(RuntimeError, match="상한"):
        budget.check(cap=100, ledger=ledger)


def test_budget_reads_env_at_call_time(tmp_path, monkeypatch):
    ledger = tmp_path / "env-ledger.jsonl"
    monkeypatch.setenv("ATTACKTRAIL_SPEND_LEDGER", str(ledger))
    assert budget.ledger_path() == ledger and budget.check() == 0.0
    ledger.write_text(json.dumps({"cost_usd": 3.0}) + "\n", encoding="utf-8")
    monkeypatch.setenv("ATTACKTRAIL_BUDGET_USD", "2.5")
    with pytest.raises(RuntimeError, match="상한"):
        budget.check()
    monkeypatch.setenv("ATTACKTRAIL_BUDGET_USD", "100$")
    with pytest.raises(RuntimeError, match="ATTACKTRAIL_BUDGET_USD"):
        budget.check()
    monkeypatch.setenv("ATTACKTRAIL_BUDGET_USD", "")
    assert budget.cap_usd() == 100.0
