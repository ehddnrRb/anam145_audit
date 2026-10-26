import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # eval/ (conftest may also do this)

from scoring import nethermind  # noqa: E402
from scoring.nethermind import evaluate  # noqa: E402

PID = "synthetic_proj"
SRC = "testagent"


def _make_run(tmp_path: Path) -> Path:
    data = tmp_path / "scoring_data"
    (data / "source_of_truth").mkdir(parents=True)
    (data / SRC).mkdir()
    truth = {
        "project_id": PID,
        "vulnerabilities": [
            {"finding_id": "T-1", "severity": "high", "title": "truth one", "description": "d1"},
            {"finding_id": "T-2", "severity": "medium", "title": "truth two", "description": "d2"},
        ],
    }
    scan = {
        "project": PID,
        "findings": [
            {"title": "f1", "description": "x1", "severity": "high", "file": "a.sol"},
            {"title": "f2", "description": "x2", "severity": "low", "file": "b.sol"},
            {"title": "f3", "description": "x3", "severity": "info", "file": None},
        ],
    }
    (data / "source_of_truth" / f"{PID}.json").write_text(json.dumps(truth), encoding="utf-8")
    (data / SRC / f"{PID}_results.json").write_text(json.dumps(scan), encoding="utf-8")
    return tmp_path


def test_mock_evaluation_offline(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("mock must not spawn"))
    run_dir = _make_run(tmp_path)

    res = evaluate(run_dir, SRC, [PID], judge="mock")

    out = run_dir / "scoring_output"
    entries = json.loads((out / f"{PID}_results.json").read_text(encoding="utf-8"))
    assert (out / "MOCK_JUDGE.json").exists()
    assert sum(1 for e in entries if not e["is_fp"]) == 2  # one per truth
    assert all("MOCK JUDGE" in e["explanation"] for e in entries if not e["is_fp"])

    scores = json.loads((run_dir / "scores.json").read_text(encoding="utf-8"))
    assert scores["judge"] == "mock" and res.judge == "mock"
    assert scores["iterations"] == 3 and scores["scorer_commit"] == nethermind.SCORER_COMMIT
    p = scores["per_project"][PID]
    assert p["truth_mapping"] == "traced" and p["truths_unevaluated"] == 0
    assert p["high_truths"] == 1
    assert [i["title"] for i in scores["info_findings"]] == ["f3"]
    assert all(f["mapping"] == "traced" and f["finding_id"] in ("T-1", "T-2") for f in scores["fn"])


def test_nethermind_without_key_fails_fast(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("spawned without key"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("spawned without key"))
    run_dir = _make_run(tmp_path)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        evaluate(run_dir, SRC, [PID], judge="nethermind")
    assert not (run_dir / "scoring_output").exists()
    assert not (run_dir / "scores.json").exists()


def test_only_missing_skips_scored_projects_but_reports_them(tmp_path, monkeypatch):
    run_dir = _make_run(tmp_path)
    evaluate(run_dir, SRC, [PID], judge="mock")
    scored = (run_dir / "scoring_output" / f"{PID}_results.json").read_text(encoding="utf-8")
    from scoring import mock_judge
    monkeypatch.setattr(mock_judge, "run_mock", lambda *a, **k: pytest.fail("re-judged a scored project"))

    res = evaluate(run_dir, SRC, [PID], judge="mock", only_missing=True)

    assert (run_dir / "scoring_output" / f"{PID}_results.json").read_text(encoding="utf-8") == scored
    assert PID in res.per_project


def _verdicts(run_dir: Path) -> list[tuple]:
    entries = json.loads((run_dir / "scoring_output" / f"{PID}_results.json").read_text(encoding="utf-8"))
    return [(e["is_match"], e["is_partial_match"], e["is_fp"]) for e in entries]


def test_mock_judge_is_content_independent(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    a = _make_run(tmp_path / "a")
    evaluate(a, SRC, [PID], judge="mock")

    b = _make_run(tmp_path / "b")
    for path, key in ((b / "scoring_data" / "source_of_truth" / f"{PID}.json", "vulnerabilities"),
                      (b / "scoring_data" / SRC / f"{PID}_results.json", "findings")):
        d = json.loads(path.read_text(encoding="utf-8"))
        for i, item in enumerate(d[key]):
            item["title"] = f"scrambled {i} zzz"
            item["description"] = f"completely different text {i} " * 7
        path.write_text(json.dumps(d), encoding="utf-8")
    evaluate(b, SRC, [PID], judge="mock")
    assert _verdicts(a) == _verdicts(b) and _verdicts(a)


def _make_custom(tmp_path: Path, truths: list[dict], findings: list[dict]) -> Path:
    run_dir = _make_run(tmp_path)
    data = run_dir / "scoring_data"
    (data / "source_of_truth" / f"{PID}.json").write_text(
        json.dumps({"project_id": PID, "vulnerabilities": truths}), encoding="utf-8")
    (data / SRC / f"{PID}_results.json").write_text(json.dumps({"project": PID, "findings": findings}),
                                                    encoding="utf-8")
    return run_dir


TRUTHS3 = [{"finding_id": f"T-{i}", "severity": s, "title": f"t{i}", "description": f"d{i}"}
           for i, s in ((1, "high"), (2, "high"), (3, "medium"))]


def test_zero_findings_counts_every_truth_as_missed(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    run_dir = _make_custom(tmp_path, TRUTHS3, [])
    res = evaluate(run_dir, SRC, [PID], judge="mock")
    p = res.per_project[PID]
    assert p["truths_in_dataset"] == 3 and p["truths_unevaluated"] == 3
    assert p["recall_vs_dataset"] == 0 and p["high_truths"] == 2 and p["high_recall"] == 0
    assert res.overall["recall_vs_dataset"] == 0 and res.overall["high_recall"] == 0
    assert [(f["finding_id"], f["verdict"]) for f in res.fn] == [("T-1", "unevaluated"), ("T-2", "unevaluated"),
                                                                  ("T-3", "unevaluated")]


def test_exhausted_findings_count_remaining_truths_as_missed(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # mock call #0/#1 agree on a match, which pops the only finding; truths 2 and 3 then get no entry
    run_dir = _make_custom(tmp_path, TRUTHS3, [{"title": "f", "description": "x", "severity": "high", "file": None}])
    res = evaluate(run_dir, SRC, [PID], judge="mock")
    p = res.per_project[PID]
    assert p["matched"] == 1 and p["recall"] == 1.0  # scorer's own denominator: judged truths only
    assert p["truths_unevaluated"] == 2 and p["truth_mapping"] == "traced"
    assert abs(p["recall_vs_dataset"] - 1 / 3) < 1e-9 and p["high_recall"] == 0.5
    assert [(f["finding_id"], f["verdict"]) for f in res.fn] == [("T-2", "unevaluated"), ("T-3", "unevaluated")]
