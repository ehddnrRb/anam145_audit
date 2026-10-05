from pathlib import Path

import pytest

from adapters import get_agent
from adapters.attacktrail import AttackTrailAgent, parse_scan_jsonl
from adapters.base import Finding, Findings, Meta, RunContext, Scope, map_severity
from adapters.baseline_llm import RESULTS_DIR, BaselineAgent
from adapters.convert import to_scorer

FIXTURE = Path(__file__).parent / "fixtures" / "scan_sample.jsonl"


def _meta():
    return Meta(agent="t", agent_version="v", model="m", status="ok")


def test_to_scorer_rejects_empty_description():
    f = Finding.model_construct(id="x", title="t", file=None, lines=[], severity="high",
                                description="  ", confidence=None)
    with pytest.raises(ValueError):
        to_scorer(Findings.model_construct(vulnerabilities=[f], meta=_meta()), "p")
    with pytest.raises(ValueError):  # the schema itself refuses it too
        Finding(id="x", title="t", file=None, lines=[], severity="high", description="")


def test_to_scorer_shape_passes_severity_through():
    f = Finding(id="x", title="t", file="a.sol", lines=[1], severity="critical", description="d")
    out = to_scorer(Findings(vulnerabilities=[f], meta=_meta()), "p")
    assert out == {"project": "p", "findings": [
        {"title": "t", "description": "d", "severity": "critical", "file": "a.sol"}]}


def test_severity_mapping():
    assert map_severity("Critical") == "critical"
    assert map_severity("HIGH") == "high"
    assert map_severity(" medium ") == "medium"
    assert map_severity("Informational") == "info"
    assert map_severity("info") == "info"
    assert map_severity("bogus") is None
    assert map_severity(None) is None


def test_attacktrail_fixture_keeps_only_triage():
    run, fs, skipped = parse_scan_jsonl(FIXTURE)
    assert run["model"] == "claude-sonnet-5" and skipped == 0
    assert [f.title for f in fs] == [
        "Reentrancy in withdraw(): state updated after external call",
        "Missing access control on setFeeRecipient",
        "Floating pragma"]
    a, b, c = fs
    assert (a.file, a.lines, a.severity, a.confidence) == ("src/Vault.sol", [42], "high", None)
    assert a.description.endswith("PoC sketch: Attacker contract calls withdraw() and "
                                  "re-enters from receive() until the vault is empty.")
    assert a.evidence.tool_output == {"code_excerpt": '(bool ok,) = msg.sender.call{value: amount}("");',
                                      "category": "reentrancy", "_verify": "OK"}
    assert b.severity == "medium" and "PoC sketch" not in b.description  # empty poc_sketch
    assert c.severity == "info"
    assert to_scorer(Findings(vulnerabilities=fs, meta=_meta()), "p")["findings"][2]["severity"] == "info"


def test_attacktrail_without_key_does_not_spawn(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class Boom:
        def run(self, *a, **k):
            raise AssertionError("must not spawn")

    agent = AttackTrailAgent(executor=Boom())
    out = agent.run(Scope("p", tmp_path), RunContext(tmp_path, tmp_path, 10, {}))
    assert out.vulnerabilities == []
    assert (out.meta.status, out.meta.error) == ("crashed", "ANTHROPIC_API_KEY not set")


def test_baseline_precomputed_parse(tmp_path):
    pid = "cantina_minimal-delegation_2025_04"
    agent = get_agent("baseline", mode="precomputed", model=None)
    out = agent.run(Scope(pid, tmp_path), RunContext(tmp_path, tmp_path, 10, {}))
    import json
    raw = json.loads((RESULTS_DIR / f"baseline_{pid}.json").read_text(encoding="utf-8"))
    assert out.meta.status == "ok", out.meta.error
    assert out.meta.model == "gpt-5 (precomputed upstream)"
    assert out.meta.tokens_in == raw["token_usage"]["input_tokens"]
    assert out.meta.tokens_out == raw["token_usage"]["output_tokens"]
    assert out.meta.cost_usd is None and out.meta.wall_seconds is None
    assert len(out.vulnerabilities) == len(raw["findings"])
    f, r = out.vulnerabilities[0], raw["findings"][0]
    assert (f.id, f.title, f.file, f.lines) == (r["id"], r["title"], r["file"], [])
    assert f.severity == r["severity"] and f.confidence == r["confidence"]
    assert f.evidence.tool_output == r["location"]


def test_baseline_missing_project_is_crashed(tmp_path):
    out = BaselineAgent().run(Scope("no_such_project", tmp_path),
                              RunContext(tmp_path, tmp_path, 10, {}))
    assert out.meta.status == "crashed" and out.vulnerabilities == []
