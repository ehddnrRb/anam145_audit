import json
import shutil
import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[1]
if str(EVAL) not in sys.path:  # conftest.py normally does this
    sys.path.insert(0, str(EVAL))

from report.compare import IncomparableRuns, compare  # noqa: E402
from report.render import MOCK_BANNER, render  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def runs(tmp_path):
    for name in ("run_a", "run_b"):
        shutil.copytree(FIX / name, tmp_path / name)
    return tmp_path / "run_a", tmp_path / "run_b"


def _edit(path: Path, fn):
    d = json.loads(path.read_text(encoding="utf-8"))
    fn(d)
    path.write_text(json.dumps(d), encoding="utf-8")


def test_fixtures_are_marked_synthetic():
    for name in ("run_a", "run_b"):
        for f in ("manifest.json", "scores.json"):
            assert json.loads((FIX / name / f).read_text(encoding="utf-8"))["synthetic_fixture"] is True


def test_render_html_and_md(runs):
    a, b = runs
    h, m = render(b)
    for text in (h.read_text(encoding="utf-8"), m.read_text(encoding="utf-8")):
        for label in ("Recall (strict)", "Recall (lenient)", "High Recall (strict)", "High Recall (lenient)",
                      "Precision (strict)", "Precision (lenient)", "Cost avg USD / project",
                      "High truths (denominator)", "auditor time cost"):
            assert label in text, label
        assert "False negatives" in text and "False positives" in text
        assert "Fixture truth one full description text." in text  # full FN truth text
        assert "B finding two synthetic agent description." in text  # full FP text
        assert "no file/line location" in text
        assert "미측정" in text  # cost None
        assert "fx_beta" in text and "pinned_commit_missing" in text
        assert "fx_gamma" in text and "clone_failed" in text
        assert MOCK_BANNER not in text
    html = h.read_text(encoding="utf-8")
    assert "<script" not in html and "http://" not in html and "https://" not in html.replace(
        "https://example.invalid", "")


def test_render_mock_banner(runs):
    a, _ = runs
    _edit(a / "manifest.json", lambda d: d["judge"].update(kind="mock"))
    h, m = render(a)
    assert MOCK_BANNER in h.read_text(encoding="utf-8")
    assert MOCK_BANNER in m.read_text(encoding="utf-8").split("## Headline")[0]


def test_render_repeat_mean_stdev(runs):
    a, _ = runs

    # shape written by runner.run._aggregate: no "overall", metrics under "aggregate"
    def agg(d):
        d.pop("overall")
        d["aggregate"] = {"recall_vs_dataset": {"mean": 0.5, "stdev": 0.1, "n": 2, "values": [0.4, 0.6]},
                          "recall_with_partial_vs_dataset": {"mean": 0.5, "stdev": None, "n": 1, "values": [0.5, None]},
                          "cost_usd_per_project": {"mean": None, "stdev": None, "n": 0, "values": [None, None]}}

    _edit(a / "scores.json", agg)
    _, m = render(a)
    text = m.read_text(encoding="utf-8")
    assert "| Recall (strict) | 50.0% ± 10.0%p |" in text
    assert "| Cost avg USD / project | 미측정 |" in text


def test_compare_detects_regression_and_new_catch(runs):
    a, b = runs
    md = compare(a, b)
    reg = md.split("## REGRESSIONS")[1].split("## New catches")[0]
    new = md.split("## New catches")[1]
    assert "(1)" in reg and "FX-1" in reg and "FX-2" not in reg
    assert "(2)" in new and "FX-2" in new and "FX-3" in new and "FX-1" not in new
    assert "WARNING" not in md
    assert (b / "compare-vs-run_a.md").read_text(encoding="utf-8") == md


def test_compare_refuses_mock_vs_real(runs):
    a, b = runs
    _edit(b / "manifest.json", lambda d: d["judge"].update(kind="mock"))
    with pytest.raises(IncomparableRuns):
        compare(a, b)


def test_compare_warns_on_mismatch(runs):
    a, b = runs
    _edit(b / "manifest.json", lambda d: (d["dataset"].update(sha="deadbeef"), d["judge"].update(batch_size=5)))
    md = compare(a, b)
    assert "WARNING: dataset sha differs" in md and "WARNING: batch size differs" in md


def test_compare_unmappable_not_guessed(runs):
    a, b = runs
    ev = b / "scoring_output" / "fx_alpha_results.json"
    _edit(ev, lambda d: d.pop(0))  # simulate a truth with NO RESULT from the judge
    md = compare(a, b)
    assert "mapping unavailable for: fx_alpha" in md
    assert "## REGRESSIONS (0)" in md


def test_compare_within_noise(runs):
    a, b = runs
    _edit(a / "scores.json", lambda d: d["overall"].update(precision={"mean": d["overall"]["precision"], "stdev": 1.0}))
    md = compare(a, b)
    line = next(x for x in md.splitlines() if x.startswith("| Precision (strict)"))
    assert "within noise" in line


def test_compare_zero_findings_on_b_is_regression(runs):
    a, b = runs
    (b / "scoring_output" / "fx_alpha_results.json").write_text("[]", encoding="utf-8")  # scorer judged nothing
    md = compare(a, b)
    reg = md.split("## REGRESSIONS")[1].split("## New catches")[0]
    assert "FX-1" in reg and "mapping unavailable" not in md


def test_compare_warns_when_scored_projects_differ(runs):
    a, b = runs
    _edit(b / "scores.json", lambda d: d["per_project"].update(fx_delta=d["per_project"]["fx_alpha"]))
    md = compare(a, b)
    assert "WARNING: scored project sets differ" in md and "fx_delta" in md
    assert "common projects only (1 scored on both sides)" in md


def test_render_excluded_shows_checkout_error(runs):
    _, b = runs

    def add_err(d):
        for p in d["projects"]:
            if p["project_id"] == "fx_gamma":
                p["excluded_reason"] = "clone_failed"  # what runner.run writes for a failed checkout
                p["checkout"] = {**(p.get("checkout") or {}), "status": "clone_failed",
                                 "error": "fatal: repository not found"}
    _edit(b / "manifest.json", add_err)
    _, m = render(b)
    assert "clone_failed: fatal: repository not found" in m.read_text(encoding="utf-8")
