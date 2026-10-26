import json
import shutil
import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[1]
if str(EVAL) not in sys.path:  # conftest.py normally does this
    sys.path.insert(0, str(EVAL))

from report.compare import IncomparableRuns, compare  # noqa: E402
from report.render import MOCK_BANNER, render, run_summary  # noqa: E402

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
    # the fixtures' agent block records only model (run_a and run_b both have 'fixture-model'), and a key neither side recorded warns
    # (D-29 review R2); "no WARNING" below is about runs that agree on all four
    _agent_both(a, b, effort="xhigh", max_tokens=64000, call_shape="api-v6")
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


def _section(md: str, title: str) -> str:
    return md.split("## " + title)[1].split("\n## ")[0]


def _partial_parts(md: str) -> tuple[str, str]:
    """The "Partial failures" section split into its table and the "Agent notes" list below it (D-21)."""
    section = _section(md, "Partial failures (부분 실패)")
    head, notes = section.split("### Agent notes (정보, 부분 실패 아님)")
    return head, notes


def test_render_partial_failure_lists_ok_projects_with_agent_memo(runs):
    _, b = runs
    memo = "2 of 9 files not scanned: 2 failed, 0 skipped; failed files: A.sol (stop_reason=refusal category=cyber)"

    def add_memo(d):
        for p in d["projects"]:
            if p["project_id"] == "fx_alpha":     # agent_status ok, scored; 2 of 9 files unscanned
                p.update(agent_error=memo, files_total=9, files_scanned=7, files_failed=2, files_skipped=0)
    _edit(b / "manifest.json", add_memo)
    h, m = render(b)
    md = m.read_text(encoding="utf-8")
    partial, notes = _partial_parts(md)
    assert "| fx_alpha | " + memo + " |" in partial
    assert "fx_alpha" not in notes and notes.strip().endswith("없음")
    # still scored (policy unchanged): not in the excluded list, still in the per-project table
    assert "fx_alpha" not in _section(md, "Excluded / failed projects")
    assert "fx_alpha" in _section(md, "Per project")
    assert "Partial failures" in h.read_text(encoding="utf-8")


def test_render_partial_failure_empty_and_ignores_non_ok(runs):
    _, b = runs
    md = render(b)[1].read_text(encoding="utf-8")
    partial, notes = _partial_parts(md)
    assert partial.strip().endswith("없음") and notes.strip().endswith("없음")

    def crash(d):  # a crashed project is excluded, not "partial" and not a note
        for p in d["projects"]:
            if p["project_id"] == "fx_alpha":
                p.update(agent_status="crashed", agent_error="boom")
    _edit(b / "manifest.json", crash)
    md = render(b)[1].read_text(encoding="utf-8")
    partial, notes = _partial_parts(md)
    assert partial.strip().endswith("없음") and notes.strip().endswith("없음")
    assert "agent crashed: boom" in _section(md, "Excluded / failed projects")


def test_render_fully_scanned_project_with_an_info_memo_is_not_partial(runs):
    # D-21 (M-3): the report called any ok project with an agent memo "부분 실패", even when every file was
    # scanned and the memo only said e.g. that candidates were dropped. Partial means files left unscanned:
    # files_scanned < files_total, or files_failed / files_skipped above 0. A memo alone is an information note.
    _, b = runs
    memo = "3 triage candidates dropped for empty why/poc_sketch"

    def full(d):
        for p in d["projects"]:
            if p["project_id"] == "fx_alpha":
                p.update(agent_error=memo, files_total=53, files_scanned=53, files_failed=0, files_skipped=0)
    _edit(b / "manifest.json", full)
    h, m = render(b)
    md = m.read_text(encoding="utf-8")
    partial, notes = _partial_parts(md)
    assert "fx_alpha" not in partial and partial.strip().endswith("없음")
    assert "| fx_alpha | 53/53 | " + memo + " |" in notes             # still visible, in its own list, with the coverage
    assert "fx_alpha" not in _section(md, "Excluded / failed projects")
    assert "Agent notes" in h.read_text(encoding="utf-8")


def test_render_partial_follows_the_file_counts_not_the_memo(runs):
    # Each count alone makes a project partial. A memo with unknown coverage is partial too (D-22): the report
    # cannot say the scan was complete, so it must not list it under "not partial".
    _, b = runs

    def edit(**kw):
        def f(d):
            for p in d["projects"]:
                if p["project_id"] == "fx_alpha":
                    p.update(agent_error=None, files_total=None, files_scanned=None, files_failed=None,
                             files_skipped=None)
                    p.update(kw)
        _edit(b / "manifest.json", f)
        return _partial_parts(render(b)[1].read_text(encoding="utf-8"))

    partial, notes = edit(files_total=10, files_scanned=10, files_failed=1, files_skipped=0)      # failed alone
    assert "| fx_alpha | coverage 10/10 |" in partial and "fx_alpha" not in notes
    partial, notes = edit(files_total=10, files_scanned=10, files_failed=0, files_skipped=2)      # skipped alone
    assert "| fx_alpha | coverage 10/10 |" in partial and "fx_alpha" not in notes
    partial, notes = edit(files_total=10, files_scanned=9)                                          # short alone
    assert "| fx_alpha | coverage 9/10 |" in partial and "fx_alpha" not in notes
    partial, notes = edit(files_total=10, files_scanned=10, files_failed=0, files_skipped=0)      # complete, no memo
    assert "fx_alpha" not in partial + notes
    partial, notes = edit(agent_error="old-format memo")                                           # memo, coverage unknown
    assert "| fx_alpha | [coverage 미측정] old-format memo |" in partial and "fx_alpha" not in notes
    partial, notes = edit(agent_error="info memo", files_total=10, files_scanned=10, files_failed=0,
                          files_skipped=0)                                                          # memo, complete
    assert "fx_alpha" not in partial and "| fx_alpha | 10/10 | info memo |" in notes


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


def test_render_per_project_coverage_column_and_partial_by_coverage(runs):
    # D-20 (H2): files_scanned/files_total per project. The denominator policy is unchanged: a project with unscanned
    # files stays scored; the report shows the gap (SCABench truth has no file location, so a truth in an
    # unscanned file still counts as missed).
    _, b = runs

    def cov(d):
        for p in d["projects"]:
            if p["project_id"] == "fx_alpha":
                p.update(files_total=53, files_scanned=40, files_failed=3, files_skipped=10)
    _edit(b / "manifest.json", cov)
    h, m = render(b)
    md = m.read_text(encoding="utf-8")
    per = _section(md, "Per project")
    assert "Coverage (files scanned/total)" in per.splitlines()[2]
    assert "| 40/53 |" in next(l for l in per.splitlines() if l.startswith("| fx_alpha"))
    assert "40/53" in h.read_text(encoding="utf-8")
    # no memo from the agent, but coverage < 100%: it is listed as partial with the numbers
    assert "| fx_alpha | coverage 40/53 |" in _partial_parts(md)[0]
    assert "fx_alpha" not in _section(md, "Excluded / failed projects")     # policy unchanged

    def full(d):
        for p in d["projects"]:
            if p["project_id"] == "fx_alpha":
                p.update(files_scanned=53, files_failed=0, files_skipped=0)
    _edit(b / "manifest.json", full)
    md = render(b)[1].read_text(encoding="utf-8")
    assert "| 53/53 |" in next(l for l in _section(md, "Per project").splitlines() if l.startswith("| fx_alpha"))
    assert all(part.strip().endswith("없음") for part in _partial_parts(md))


def test_render_coverage_is_unmeasured_without_files_fields(runs):
    _, b = runs                 # the fixtures predate files_*: baseline and old runs have no coverage
    md = render(b)[1].read_text(encoding="utf-8")
    row = next(l for l in _section(md, "Per project").splitlines() if l.startswith("| fx_alpha"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[-2] == "미측정"


# ---------------------------------------------------------------------------------------------
# D-23: fallback responses in the report, the effort row, the end-of-run console summary
# ---------------------------------------------------------------------------------------------

def test_render_fallback_section_lists_projects_where_a_fallback_model_answered(runs):
    _, b = runs

    def fb(d):
        for p in d["projects"]:
            if p["project_id"] == "fx_alpha":
                p.update(files_fallback=12, fallback_models=["claude-sonnet-5"])
    _edit(b / "manifest.json", fb)
    h, m = render(b)
    md = m.read_text(encoding="utf-8")
    sec = _section(md, "Fallback responses (폴백 응답)")
    assert "| fx_alpha | 폴백 응답 12 파일 (모델 claude-sonnet-5) |" in sec
    assert "폴백 모델까지 거절한 파일은 ERROR" in sec and "sticky" in sec
    # review: the count means "a fallback model produced a response", including one that then failed to parse or was cut
    # at max_tokens, not "the file's candidates came from this model"
    assert "응답을 낸 파일 수" in sec and "max_tokens 로 잘려 ERROR 가 된 파일도 센다" in sec
    assert "후보를 낸 파일의 후보는" in sec
    assert "폴백 응답 12 파일 (모델 claude-sonnet-5)" in h.read_text(encoding="utf-8")
    assert md.index("## Fallback responses") > md.index("### Agent notes")      # its own section, below the partial-failure ones


def test_render_fallback_section_distinguishes_none_from_unmeasured(runs):
    _, b = runs
    md = render(b)[1].read_text(encoding="utf-8")                       # fixtures predate the field: nobody measured it
    assert _section(md, "Fallback responses (폴백 응답)").strip().endswith("미측정")

    def zero(d):
        for p in d["projects"]:
            if p["project_id"] == "fx_alpha":
                p.update(files_fallback=0, fallback_models=[])
    _edit(b / "manifest.json", zero)
    md = render(b)[1].read_text(encoding="utf-8")
    assert _section(md, "Fallback responses (폴백 응답)").strip().endswith("없음")   # measured, none answered


def test_render_manifest_table_shows_effort(runs):
    _, b = runs
    _edit(b / "manifest.json", lambda d: d["agent"].update(effort="xhigh", call_shape="api-v5"))
    md = render(b)[1].read_text(encoding="utf-8")
    assert "| effort (output_config.effort) | xhigh |" in _section(md, "Manifest")
    assert "| call shape | api-v5 |" in _section(md, "Manifest")
    # the fixtures' agent has no effort key (older runs, baseline): no row and no crash
    _edit(b / "manifest.json", lambda d: d["agent"].pop("effort"))
    assert "effort (output_config.effort)" not in render(b)[1].read_text(encoding="utf-8")


NO_FILES_RECORD = {"project_id": "fx_nf", "excluded_reason": None, "checkout": {"status": "ok"}, "agent_status": "no_files",
                   "agent_error": "no files selected: 스캔할 파일이 0개입니다: C:\\repo", "files_total": 0, "files_scanned": 0,
                   "files_failed": 0, "files_skipped": 0}
UNSUPPORTED = {"projects": [{"project_id": "fx_nf", "truths": 8}], "n_projects": 1, "truths": 8,
               "line": "지원 밖 프로젝트 1개, 정답 8건. 이 정답을 전부 미탐으로 넣으면 Recall 4.2% (strict) / 5.0% (lenient)."}
CRASHED_RECORD = {"project_id": "fx_dead", "excluded_reason": None, "checkout": {"status": "ok"}, "agent_status": "crashed",
                  "agent_error": "scan.py exit 2: boom"}


def test_render_unsupported_section_and_no_files_status_differ_from_crashed(runs):
    # D-27: no_files gets its own section (project, truths, reason) after the headline, its own status wording and color,
    # and is not listed among the excluded/failed projects, where a crashed agent still is.
    _, b = runs
    _edit(b / "manifest.json", lambda d: (d["projects"].extend([NO_FILES_RECORD, CRASHED_RECORD]),
                                          d.update(unsupported=UNSUPPORTED)))
    h, m = render(b)
    md, html = m.read_text(encoding="utf-8"), h.read_text(encoding="utf-8")
    sec = _section(md, "Unsupported projects (지원 밖 프로젝트)")
    assert UNSUPPORTED["line"] in sec
    assert "| fx_nf | 8 | no files selected: 스캔할 파일이 0개입니다: C:\\repo |" in sec
    assert md.index("\n## Unsupported projects") < md.index("\n## Per project")      # a level-2 heading, after the headline
    per = _section(md, "Per project")
    assert "| no_files (지원 밖) |" in per and "| crashed |" in per
    excluded = _section(md, "Excluded / failed projects")
    assert "fx_dead" in excluded and "agent crashed" in excluded and "fx_nf" not in excluded
    assert "fx_nf" not in _section(md, "Partial failures (부분 실패)")
    assert "<td class='nofiles'>no_files (지원 밖)</td>" in html and "<td class='failed'>crashed</td>" in html
    assert "class='nofiles'" not in render(runs[0])[0].read_text(encoding="utf-8")        # run_a has none


def test_render_has_no_unsupported_section_without_a_no_files_project_and_old_manifests_still_render(runs):
    _, b = runs
    md = render(b)[1].read_text(encoding="utf-8")
    assert "Unsupported projects" not in md and "(지원 밖)" not in md
    # a manifest from before D-27 that somehow holds a no_files status but no `unsupported` block: truths 미측정, no line
    _edit(b / "manifest.json", lambda d: d["projects"].append(NO_FILES_RECORD))
    sec = _section(render(b)[1].read_text(encoding="utf-8"), "Unsupported projects (지원 밖 프로젝트)")
    assert "| fx_nf | 미측정 |" in sec and "지원 밖 프로젝트 1개" not in sec


def test_run_summary_puts_no_files_on_its_own_line_and_not_among_the_crashed():
    s = run_summary({"projects": [
        {"project_id": "a", "agent_status": "crashed", "agent_error": "scan.py exit 2: x"},
        dict(NO_FILES_RECORD, project_id="b"), dict(NO_FILES_RECORD, project_id="c")],
        "unsupported": {"projects": [{"project_id": "b", "truths": 8}, {"project_id": "c", "truths": 3}]}})
    assert s == ["부분 실패 프로젝트: 없음", "crashed 프로젝트 1개", "  a [crashed]: scan.py exit 2: x",
                 "지원 밖 프로젝트: b (정답 8), c (정답 3)"]
    only = run_summary({"projects": [NO_FILES_RECORD]})        # truths unknown (no block): 미측정, never a crash line
    assert only == ["부분 실패 프로젝트: 없음", "crashed 프로젝트: 없음", "지원 밖 프로젝트: fx_nf (정답 미측정)"]


def test_compare_warns_when_the_no_files_sets_differ_and_only_then(runs):
    a, b = runs
    _edit(b / "manifest.json", lambda d: d["projects"].append(NO_FILES_RECORD))
    md = compare(a, b)
    assert "WARNING: no_files project sets differ (only A: -; only B: ['fx_nf'])" in md
    _edit(a / "manifest.json", lambda d: d["projects"].append(NO_FILES_RECORD))
    assert "no_files project sets differ" not in compare(a, b)          # same set on both sides: no warning
    _edit(b / "manifest.json", lambda d: d["projects"].remove(NO_FILES_RECORD))
    assert "(only A: ['fx_nf']; only B: -)" in compare(a, b)


def test_compare_reads_the_no_files_set_of_a_repeat_run_from_its_reps(runs, tmp_path):
    # a repeat run's own manifest has projects [] (runner._aggregate); the no_files projects sit in rep-k/manifest.json
    a, b = runs
    _edit(a / "manifest.json", lambda d: d["projects"].append(NO_FILES_RECORD))
    (b / "rep-1").mkdir()
    shutil.copy(a / "manifest.json", b / "rep-1" / "manifest.json")
    assert "no_files project sets differ" not in compare(a, b)


def _agent_both(a, b, **kw):
    for run in (a, b):
        _edit(run / "manifest.json", lambda d: d["agent"].update(kw))


def test_compare_warns_per_agent_setting_that_differs_and_only_for_those(runs):
    # D-28 review 2: model, effort, max_tokens and call_shape of the two manifests' `agent` blocks. One line per differing
    # setting, in the same "<what> differs: A=.. B=.." form as the other comparability warnings, none for settings that match.
    # D-29 review R2: a setting neither manifest recorded is a line too, "agent.<key>: 양쪽 미기록" (unknown is not equal).
    a, b = runs
    _agent_both(a, b, model="claude-fable-5-1", effort="xhigh", max_tokens=64000, call_shape="api-v6")
    assert "agent." not in compare(a, b)                                        # same on both sides: no warning
    _edit(b / "manifest.json", lambda d: d["agent"].update(effort="max", max_tokens=128000))
    md = compare(a, b)
    assert "> **WARNING: agent.effort differs: A=xhigh B=max. Deltas may not be comparable.**" in md
    assert "> **WARNING: agent.max_tokens differs: A=64000 B=128000. Deltas may not be comparable.**" in md
    assert "agent.model differs" not in md and "agent.call_shape differs" not in md
    _edit(b / "manifest.json", lambda d: d["agent"].update(model="claude-opus-5-5", call_shape="cli-v1"))
    md = compare(a, b)
    assert "agent.model differs: A=claude-fable-5-1 B=claude-opus-5-5" in md and "agent.call_shape differs: A=api-v6 B=cli-v1" in md
    # a manifest from before D-28 never recorded max_tokens: that side reads 미기록 (unknown is not equal)
    _edit(a / "manifest.json", lambda d: d["agent"].pop("max_tokens"))
    assert "agent.max_tokens differs: A=미기록 B=128000" in compare(a, b)
    _edit(b / "manifest.json", lambda d: d["agent"].pop("max_tokens"))
    md = compare(a, b)                                                          # neither side knows: a line, not a "differs"
    assert "agent.max_tokens differs" not in md
    assert "WARNING: agent.max_tokens: 양쪽 미기록. Deltas may not be comparable." in md
    assert md.count("양쪽 미기록") == 1                                          # only the key neither side recorded


def test_compare_reads_the_agent_of_a_repeat_run_from_its_aggregate_manifest(runs):
    # D-28 review 2: a repeat run's own manifest carries the agent block (runner._aggregate copies rep-1's). It is read as it
    # is, not rebuilt from rep-k/manifest.json, so a rep dir that disagrees does not change the warning.
    a, b = runs
    _agent_both(a, b, effort="max", max_tokens=128000)
    (b / "rep-1").mkdir()
    shutil.copy(b / "manifest.json", b / "rep-1" / "manifest.json")
    _edit(b / "rep-1" / "manifest.json", lambda d: d["agent"].update(effort="xhigh"))
    assert "agent.effort differs" not in compare(a, b)
    _edit(b / "manifest.json", lambda d: d["agent"].update(effort="high"))
    assert "agent.effort differs: A=max B=high" in compare(a, b)


def test_compare_warns_for_each_agent_setting_neither_manifest_recorded(runs):
    # D-29 review R2, "unknown is not equal": the fixtures' baseline agent block holds a model and nothing else, so effort,
    # max_tokens and call_shape are unrecorded on both sides. Each gets one line, in the short form, and not the "differs" one.
    a, b = runs
    md = compare(a, b)
    for k in ("effort", "max_tokens", "call_shape"):
        assert f"WARNING: agent.{k}: 양쪽 미기록. Deltas may not be comparable." in md
        assert f"agent.{k} differs" not in md
    assert "agent.model" not in md                                              # recorded on both sides, and equal


def test_run_summary_lists_partial_failures_crashed_projects_and_fallback_files():
    manifest = {"projects": [
        {"project_id": "fx_alpha", "agent_status": "ok", "agent_error": "11 of 53 files not scanned: ...",
         "files_total": 53, "files_scanned": 40, "files_failed": 3, "files_skipped": 10,
         "files_fallback": 12, "fallback_models": ["claude-sonnet-5"]},
        {"project_id": "fx_clean", "agent_status": "ok", "files_total": 5, "files_scanned": 5, "files_failed": 0,
         "files_skipped": 0, "files_fallback": 0, "fallback_models": []},
        {"project_id": "fx_old", "agent_status": "ok", "agent_error": "2 of 9 files failed inside scan.py ..."},
        {"project_id": "fx_dead", "agent_status": "crashed",
         "agent_error": "scan.py exit 2: 치명적 API 오류로 스캔을 중단했습니다: AuthenticationError (HTTP 401)\n"
                        "invalid x-api-key (partial output kept in raw/)"},
        {"project_id": "fx_skip", "agent_status": None, "excluded_reason": "pinned_commit_missing"}]}
    assert run_summary(manifest) == [
        "부분 실패 프로젝트 2개",
        "  fx_alpha: 실패 3/53 파일, 건너뜀 10, 커버리지 40/53",
        "  fx_old: 커버리지 미측정",
        "crashed 프로젝트 1개",
        "  fx_dead [crashed]: scan.py exit 2: 치명적 API 오류로 스캔을 중단했습니다: AuthenticationError (HTTP 401) "
        "invalid x-api-key (partial output kept in raw/)",
        "폴백 응답 프로젝트 1개",
        "  fx_alpha: 폴백 응답 12 파일 (모델 claude-sonnet-5)"]


def test_run_summary_with_nothing_to_report_and_for_agents_without_fallback_counts():
    ok = {"project_id": "p", "agent_status": "ok", "files_total": 5, "files_scanned": 5, "files_failed": 0,
          "files_skipped": 0}
    # CLI adapter and baseline never record fallback counts: no fallback line at all (not measured, not "없음")
    assert run_summary({"projects": [ok]}) == ["부분 실패 프로젝트: 없음", "crashed 프로젝트: 없음"]
    # measured and zero: say so
    assert run_summary({"projects": [dict(ok, files_fallback=0, fallback_models=[])]})[-1] == "폴백 응답: 없음"
    assert run_summary({}) == ["부분 실패 프로젝트: 없음", "crashed 프로젝트: 없음"]


def test_run_summary_names_timeouts_and_long_reasons_are_one_line_and_bounded():
    long_err = "harness: KeyError: 'x'\nTraceback (most recent call last):\n" + "  frame\n" * 80
    s = run_summary({"projects": [{"project_id": "a", "agent_status": "timeout", "agent_error": "scan.py exceeded 1800s"},
                                  {"project_id": "b", "agent_status": "crashed", "agent_error": long_err},
                                  {"project_id": "c", "agent_status": "crashed", "agent_error": None}]})
    assert s[1] == "crashed/timeout 프로젝트 3개"
    assert s[2] == "  a [timeout]: scan.py exceeded 1800s"
    assert s[3].startswith("  b [crashed]: harness: KeyError: 'x' Traceback (most recent call last): frame frame")
    assert "\n" not in s[3] and len(s[3]) <= len("  b [crashed]: ") + 200
    assert s[4] == "  c [crashed]: 사유 없음"


# ---------------------------------------------------------------------------------------------
# D-28: max_tokens row, the denominator sentence of the unsupported section, incomplete judging
# ---------------------------------------------------------------------------------------------

def test_render_manifest_table_shows_max_tokens_and_omits_the_row_when_the_manifest_has_none(runs):
    _, b = runs
    _edit(b / "manifest.json", lambda d: d["agent"].update(effort="max", max_tokens=128000))
    h, m = render(b)
    sec = _section(m.read_text(encoding="utf-8"), "Manifest")
    assert "| max_tokens (요청 출력 상한) | 128000 |" in sec and "| effort (output_config.effort) | max |" in sec
    assert "max_tokens (요청 출력 상한)" in h.read_text(encoding="utf-8")
    _edit(b / "manifest.json", lambda d: d["agent"].update(max_tokens="n/a"))          # the CLI adapter records n/a
    assert "| max_tokens (요청 출력 상한) | n/a |" in _section(render(b)[1].read_text(encoding="utf-8"), "Manifest")
    _edit(b / "manifest.json", lambda d: d["agent"].pop("max_tokens"))                  # runs from before D-28: no row
    assert "max_tokens (요청 출력 상한)" not in render(b)[1].read_text(encoding="utf-8")


def test_render_unsupported_section_states_what_the_all_in_recall_denominator_is(runs):
    # D-28: the section says the denominator is the scored projects' truths plus the unsupported truths, and that failed
    # checkouts, pinned-commit gaps, crashed and timed-out projects are not in it. It also names the --web size-cap case.
    _, b = runs
    _edit(b / "manifest.json", lambda d: (d["projects"].append(NO_FILES_RECORD), d.update(unsupported=UNSUPPORTED)))
    sec = _section(render(b)[1].read_text(encoding="utf-8"), "Unsupported projects (지원 밖 프로젝트)")
    assert ("전체 포함 Recall 의 분모는 채점된 프로젝트 정답과 지원 밖 정답의 합이고, 체크아웃 실패·pinned 누락·crashed·timeout "
            "프로젝트의 정답은 들어가지 않는다") in sec
    assert "--web 에서 전부 크기 상한을 넘었다" in sec


JUDGE_INCOMPLETE = [{"project_id": "fx_a", "truths_unevaluated": 17}, {"project_id": "fx_b", "truths_unevaluated": 2, "run": "rep-3"}]


def test_render_and_run_summary_show_incomplete_judging_and_only_when_flagged(runs):
    # D-28: "채점 불완전: <project> 미평가 정답 N건(채점 로그 확인)" under the Headline table and in the console summary. A repeat
    # aggregate adds the rep after "채점 로그 확인". No flag, no line, and the older manifests render as before.
    _, b = runs
    md = render(b)[1].read_text(encoding="utf-8")
    assert "채점 불완전" not in md and "채점 불완전" not in "\n".join(run_summary(json.loads(
        (b / "manifest.json").read_text(encoding="utf-8"))))
    _edit(b / "manifest.json", lambda d: d.update(judge_incomplete=True, judge_incomplete_projects=JUDGE_INCOMPLETE))
    h, m = render(b)
    md, html = m.read_text(encoding="utf-8"), h.read_text(encoding="utf-8")
    first = "채점 불완전: fx_a 미평가 정답 17건(채점 로그 확인)"
    second = "채점 불완전: fx_b 미평가 정답 2건(채점 로그 확인, rep-3)"
    assert first in md and second in md and first in html
    assert md.index("## Headline") < md.index(first) < md.index(second) < md.index("## Per project")
    assert "judge 호출 실패로 생길 수 있다" in md and "scoring_output/scorer_stdout.log" in md
    lines = run_summary(json.loads((b / "manifest.json").read_text(encoding="utf-8")))
    assert lines[-2:] == [first, second] or [first, second] == [l for l in lines if l.startswith("채점 불완전")]
