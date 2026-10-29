import json
from pathlib import Path

import pytest

from adapters import get_agent
from adapters.attacktrail import (ABANDONED_MARK, DEFAULT_TIMEOUT_S, MAX_CALL_S, SCAN_PY, SDK_HOME_KEYS, AttackTrailAgent, coverage_meta,
                                  load_scan_module, parse_scan_jsonl, sum_usage)
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
    assert out.meta.status == "crashed"
    assert out.meta.error == "ANTHROPIC_API_KEY not set (eval run also reads ATTACKTRAIL_ANTHROPIC_API_KEY first)"


class _FakeScanPy:
    """Stands in for running scan.py: records argv and env, writes a JSONL where --out says,
    prints progress. `rows` are extra JSONL rows after the _run header."""

    def __init__(self, scan_py, lines, header_files, rows=(), returncode=0, stderr="",
                 timed_out=False, write=True, header=None):
        self.scan_py, self.lines, self.files = scan_py, lines, header_files
        self.rows, self.returncode, self.stderr = list(rows), returncode, stderr
        self.timed_out, self.write, self.header = timed_out, write, header or {}
        self.argv = self.env = None
        self.home_was_empty_dir = None

    def run(self, argv, cwd, env, timeout_s):
        from runner.executor import ExecResult
        self.argv, self.env = list(argv), dict(env)
        home = env.get("HOME")
        self.home_was_empty_dir = bool(home) and Path(home).is_dir() and not any(Path(home).iterdir())
        if self.write:
            out = Path(argv[argv.index("--out") + 1])
            out.write_text("".join(json.dumps(r) + "\n" for r in
                                   [{"_run": {"model": "m", "files": self.files, **self.header}}] + self.rows),
                           encoding="utf-8")
        return ExecResult(returncode=None if self.timed_out else self.returncode,
                          stdout="\n".join(self.lines), stderr=self.stderr, seconds=0.1,
                          timed_out=self.timed_out)


def _scan_py_in(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    scan_py = tools / "scan.py"
    scan_py.write_text("# placeholder; the fake executor never runs it\n", encoding="utf-8")
    return scan_py


def test_attacktrail_gives_scan_py_an_empty_temp_home(tmp_path, monkeypatch):
    # D-18: anthropic 1.x needs a home dir at client creation; the real one must not leak in.
    monkeypatch.setenv("USERPROFILE", r"C:\Users\real-user")
    scan_py = _scan_py_in(tmp_path)
    fake = _FakeScanPy(scan_py, ["  [1/1] A.sol  후보 0"], 1)
    agent = AttackTrailAgent(scan_py=scan_py, executor=fake)
    out = agent.run(Scope("p", tmp_path / "repo"),
                    RunContext(tmp_path, tmp_path / "proj", 10, {"ANTHROPIC_API_KEY": "k"}))
    assert out.meta.status == "ok", out.meta.error
    assert fake.home_was_empty_dir
    assert SDK_HOME_KEYS == ("HOME", "USERPROFILE", "APPDATA")
    homes = {fake.env[k] for k in SDK_HOME_KEYS}
    assert len(homes) == 1 and homes != {r"C:\Users\real-user"}
    assert fake.env["ANTHROPIC_API_KEY"] == "k"
    # D-19: any ANTHROPIC_CONFIG_DIR makes the SDK print a bogus "API_KEY takes precedence" warning
    assert "ANTHROPIC_CONFIG_DIR" not in fake.env
    assert not Path(homes.pop()).exists()  # removed after the run


def test_attacktrail_reports_per_file_errors(tmp_path):
    # D-18: the old "]  ERROR " check never matched scan.py's "  [i/n] <file>  ERROR ..." lines.
    scan_py = _scan_py_in(tmp_path)
    fake = _FakeScanPy(scan_py, ["  [1/2] A.sol  후보 0",
                                 "  [2/2] B.sol  ERROR stop_reason=refusal category=cyber"], 2)
    out = AttackTrailAgent(scan_py=scan_py, executor=fake).run(
        Scope("p", tmp_path / "repo"),
        RunContext(tmp_path, tmp_path / "proj", 10, {"ANTHROPIC_API_KEY": "k"}))
    assert out.meta.status == "ok"
    # D-20: memo = counts + the failed files by name; coverage fields on Meta
    assert out.meta.error == ("1 of 2 files not scanned: 1 failed, 0 skipped; "
                              "failed files: B.sol (stop_reason=refusal category=cyber)")
    assert (out.meta.files_total, out.meta.files_scanned, out.meta.files_failed,
            out.meta.files_skipped) == (2, 1, 1, 0)


def test_attacktrail_every_file_failed_is_crash(tmp_path):
    scan_py = _scan_py_in(tmp_path)
    fake = _FakeScanPy(scan_py, ["  [1/2] A.sol  ERROR stop_reason=refusal category=cyber",
                                 "  [2/2] B.sol  ERROR stop_reason=refusal category=cyber"], 2)
    out = AttackTrailAgent(scan_py=scan_py, executor=fake).run(
        Scope("p", tmp_path / "repo"),
        RunContext(tmp_path, tmp_path / "proj", 10, {"ANTHROPIC_API_KEY": "k"}))
    assert out.meta.status == "crashed" and "every file failed inside scan.py" in out.meta.error


NO_FILES_STDERR = "스캔할 파일이 0개입니다: C:\\repo (확장자·제외 규칙 확인)\n"


def test_attacktrail_zero_files_is_no_files_not_crashed_and_records_measured_zero_coverage(tmp_path):
    # D-27: scan.py ends a repo with nothing to send with exit 1 and that message on stderr (before any API call).
    # That is a limit of the tool (e.g. an all-.move scope), so the project is no_files, not crashed.
    fake = _FakeScanPy(_scan_py_in(tmp_path), [], 0, returncode=1, stderr=NO_FILES_STDERR, write=False)
    out = _agent_run(tmp_path, fake)
    assert out.meta.status == "no_files" and out.vulnerabilities == []
    assert "no files selected" in out.meta.error and "스캔할 파일이 0개입니다" in out.meta.error
    # selected nothing, so every count is a measured 0 (a report would otherwise say 미측정)
    assert (out.meta.files_total, out.meta.files_scanned, out.meta.files_failed, out.meta.files_skipped) == (0, 0, 0, 0)
    assert (out.meta.tokens_in, out.meta.tokens_out) == (None, None)


@pytest.mark.parametrize("returncode, stderr", [
    (1, "Traceback (most recent call last):\nValueError: boom\n"),    # exit 1 for another reason
    (2, NO_FILES_STDERR),                                             # the message under another exit code (fatal API error is 2)
    (1, ""),
    (3, NO_FILES_STDERR)])
def test_attacktrail_only_exit_1_with_the_zero_files_message_is_no_files(tmp_path, returncode, stderr):
    fake = _FakeScanPy(_scan_py_in(tmp_path), [], 0, returncode=returncode, stderr=stderr, write=False)
    out = _agent_run(tmp_path, fake)
    assert out.meta.status == "crashed" and out.meta.files_total is None


TRIAGE ={"title": "t", "file": "A.sol", "line": 3, "code_excerpt": "x", "category": "c",
          "severity_guess": "High", "why": "w", "poc_sketch": "p", "_verify": "OK", "_status": "triage"}


def _agent_run(tmp_path, fake, timeout_s=10):
    return AttackTrailAgent(scan_py=fake.scan_py, executor=fake).run(
        Scope("p", tmp_path / "repo"),
        RunContext(tmp_path, tmp_path / "proj", timeout_s, {"ANTHROPIC_API_KEY": "k"}))


@pytest.mark.parametrize("timeout_s, budget, deadline", [
    # D-24: the soft margin is now SOFT_MARGIN_S = 15 + MAX_CALL_S 1800 = 1815 s (was max(60 s, 10%)), so a call that starts
    # at the soft limit can still run for MAX_CALL_S and end before the hard limit. Below 3,630 s the half-timeout floor binds.
    (10800, "8985", "10785"), (7200, "5385", "7185"), (36000, "32400", "35985"),   # 36000: 10% (3600 s) > 1815 s
    (3630, "1815", "3615"), (1800, "900", "1785"), (600, "300", "585"), (300, "150", "285"), (100, "50", "85"),
    (10, "5", "5")])
def test_attacktrail_passes_out_path_time_budget_and_deadline(tmp_path, timeout_s, budget, deadline):
    # D-19/D-20/D-24: scan.py gets a soft --time-budget (timeout - max(soft margin, 10%), never below half: stop
    # starting calls) and a hard --deadline-s (timeout - DEADLINE_MARGIN_S 15 s, never below half: every
    # stream is closed by then), both shorter than the harness timeout, and writes straight into
    # <project_dir>/raw/scan.jsonl. The budget never exceeds the deadline. The soft margin here is SOFT_MARGIN_S 1815 s because
    # this run has the default max_tokens 64000; _run computes it from max_tokens (3615 s at 128000, D-28).
    fake = _FakeScanPy(_scan_py_in(tmp_path), ["  [1/1] A.sol  후보 0"], 1)
    out = _agent_run(tmp_path, fake, timeout_s)
    assert out.meta.status == "ok", out.meta.error
    argv = fake.argv
    assert argv[argv.index("--time-budget") + 1] == budget
    assert argv[argv.index("--deadline-s") + 1] == deadline
    assert int(budget) <= int(deadline) <= timeout_s
    if timeout_s >= 3630:   # the margin really leaves room for one worst-case call, from 2 x SOFT_MARGIN_S = 3630 s up (D-24, D-25)
        assert int(deadline) - int(budget) >= MAX_CALL_S
    else:                   # below it the half-timeout floor binds and the room is gone
        assert int(deadline) - int(budget) < MAX_CALL_S
    assert argv[argv.index("--out") + 1] == str(tmp_path / "proj" / "raw" / "scan.jsonl")
    assert (tmp_path / "proj" / "raw" / "scan.jsonl").is_file()


def test_attacktrail_reads_only_its_own_out_file(tmp_path):
    # D-19: the old code guessed scan-<commit8>.jsonl next to scan.py by mtime. A file left there
    # (another project, same commit) must neither be read nor moved.
    scan_py = _scan_py_in(tmp_path)
    stale = scan_py.parent / "scan-unknown.jsonl"
    stale.write_text('{"_run": {}}\n' + json.dumps(dict(TRIAGE, title="stale")) + "\n", encoding="utf-8")
    fake = _FakeScanPy(scan_py, ["  [1/1] A.sol  후보 1"], 1, rows=[TRIAGE])
    out = _agent_run(tmp_path, fake)
    assert [f.title for f in out.vulnerabilities] == ["t"]
    assert stale.is_file() and "stale" in stale.read_text(encoding="utf-8")

    # A leftover raw/scan.jsonl from an earlier run in the same dir is removed before the run.
    raw = tmp_path / "proj" / "raw" / "scan.jsonl"
    raw.write_text(json.dumps(dict(TRIAGE, title="old")) + "\n", encoding="utf-8")
    nothing = _FakeScanPy(scan_py, [], 1, write=False)
    res = _agent_run(tmp_path, nothing)
    assert (res.meta.status, res.meta.error) == ("crashed", "scan.py wrote no scan.jsonl")


def test_attacktrail_sums_usage_rows(tmp_path):
    rows = [{"_usage": {"file": "A.sol", "input_tokens": 10, "cache_creation_input_tokens": 30,
                        "cache_read_input_tokens": 0, "output_tokens": 5}},
            TRIAGE,
            {"_usage": {"file": "B.sol", "input_tokens": 4, "cache_creation_input_tokens": None,
                        "cache_read_input_tokens": 30, "output_tokens": 7,
                        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}}}]
    fake = _FakeScanPy(_scan_py_in(tmp_path), ["  [1/2] A.sol  후보 1", "  [2/2] B.sol  후보 0"], 2, rows=rows)
    out = _agent_run(tmp_path, fake)
    assert out.meta.status == "ok", out.meta.error
    assert [f.title for f in out.vulnerabilities] == ["t"]      # _usage rows are not candidates
    assert (out.meta.tokens_in, out.meta.tokens_out) == (10 + 30 + 0 + 4 + 30, 5 + 7)
    assert out.meta.cost_usd is None                              # prices not fixed (D-19)

    sub = tmp_path / "n"
    sub.mkdir()
    res = _agent_run(sub, _FakeScanPy(_scan_py_in(sub), ["  [1/1] A.sol  후보 1"], 1, rows=[TRIAGE]))
    assert (res.meta.tokens_in, res.meta.tokens_out) == (None, None)  # no usage rows: unmeasured


def test_parse_scan_jsonl_skips_usage_rows(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"_run": {"model": "m"}}, {"_usage": {"file": "A.sol", "input_tokens": 1}}, TRIAGE]),
        encoding="utf-8")
    run, fs, skipped = parse_scan_jsonl(p)
    assert run == {"model": "m"} and [f.title for f in fs] == ["t"] and skipped == 0


def test_only_a_row_whose_single_key_is_usage_or_run_is_bookkeeping(tmp_path):
    # D-20 (blackbox "ad"): a candidate dict that merely carries a "_usage"/"_run" key is a candidate row,
    # not usage (it inflated tokens_in by 10**6) and not the run header (it would replace it).
    # D-19 treated any row with a "_usage" key as usage and skipped it; now it is read as the candidate it is.
    p = tmp_path / "s.jsonl"
    usage = {"_usage": {"file": "A.sol", "input_tokens": 10, "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0, "output_tokens": 5}}
    evil_usage = dict(TRIAGE, title="evil-usage", _usage={"input_tokens": 10 ** 6, "output_tokens": 10 ** 6})
    evil_run = dict(TRIAGE, title="evil-run", _run={"model": "evil"})
    p.write_text("\n".join(json.dumps(r) for r in [
        {"_run": {"model": "m"}}, usage, evil_usage, evil_run, usage]), encoding="utf-8")
    assert sum_usage(p) == (20, 10)              # only the two real rows; the 10**6 never counts
    run, fs, _ = parse_scan_jsonl(p)
    assert run == {"model": "m"}                 # the header was not replaced by the candidate's _run
    assert [f.title for f in fs] == ["evil-usage", "evil-run"]


def test_sum_usage_reads_cli_rows_as_already_summed(tmp_path):
    # D-20: CLI rows keep input_tokens = uncached + cache_creation + cache_read and add the raw kinds under
    # the API row's names plus input_tokens_uncached. Adding them again would double count.
    p = tmp_path / "s.jsonl"
    row = {"file": "A.sol", "input_tokens": 150, "input_tokens_uncached": 100,
           "cache_creation_input_tokens": 30, "cache_read_input_tokens": 20, "output_tokens": 7}
    p.write_text("\n".join(json.dumps({"_usage": r}) for r in [row, row]), encoding="utf-8")
    assert sum_usage(p) == (300, 14)


def test_attacktrail_nonzero_exit_is_crash_but_raw_is_kept(tmp_path):
    # scan.py exits 2 on 401/403/404; what it wrote before stopping stays in raw/ for diagnosis.
    err = "치명적 API 오류로 스캔을 중단했습니다: AuthenticationError (HTTP 401): invalid x-api-key"
    fake = _FakeScanPy(_scan_py_in(tmp_path), ["  [1/2] A.sol  후보 1", "  [2/2] B.sol  ERROR x"], 2,
                       rows=[TRIAGE], returncode=2, stderr=err)
    out = _agent_run(tmp_path, fake)
    raw = tmp_path / "proj" / "raw"
    assert out.meta.status == "crashed" and out.vulnerabilities == []
    assert "scan.py exit 2" in out.meta.error and "AuthenticationError (HTTP 401)" in out.meta.error
    assert "partial output kept" in out.meta.error
    assert '"title": "t"' in (raw / "scan.jsonl").read_text(encoding="utf-8")
    assert (raw / "scan.stderr.txt").read_text(encoding="utf-8") == err

    sub = tmp_path / "t"
    sub.mkdir()
    slow = _FakeScanPy(_scan_py_in(sub), ["  [1/2] A.sol  후보 1"], 2, rows=[TRIAGE], timed_out=True)
    res = _agent_run(sub, slow)
    assert res.meta.status == "timeout" and "partial output kept" in res.meta.error
    assert (sub / "proj" / "raw" / "scan.jsonl").is_file()


def test_attacktrail_notes_files_skipped_by_time_budget(tmp_path):
    fake = _FakeScanPy(_scan_py_in(tmp_path), [
        "  [1/3] A.sol  후보 0",
        "  [2/3] B.sol  ERROR skipped: time budget reached",
        "  [3/3] C.sol  ERROR skipped: time budget reached"], 3)
    out = _agent_run(tmp_path, fake)
    assert out.meta.status == "ok"
    assert out.meta.error == "2 of 3 files not scanned: 0 failed, 2 skipped (2 time budget reached)"
    assert (out.meta.files_total, out.meta.files_scanned, out.meta.files_failed,
            out.meta.files_skipped) == (3, 1, 0, 2)


def test_baseline_precomputed_parse(tmp_path):
    pid = "cantina_minimal-delegation_2025_04"
    agent = get_agent("baseline", mode="precomputed", model=None)
    out = agent.run(Scope(pid, tmp_path), RunContext(tmp_path, tmp_path, 10, {}))
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


# ---------------------------------------------------------------------------------------------
# D-20: coverage, hard deadline notes, agent snapshot
# ---------------------------------------------------------------------------------------------

def test_coverage_meta_names_at_most_five_failed_files_and_counts_skips_by_reason():
    failed = [("F%d.sol" % i, "boom %d " % i + "x" * 200) for i in range(7)]
    fields, memo = coverage_meta(20, 8, failed, ["time budget reached"] * 3 + ["hard deadline reached"] * 2)
    assert fields == {"files_total": 20, "files_scanned": 8, "files_failed": 7, "files_skipped": 5}
    assert fields["files_total"] == 8 + 7 + 5
    assert memo.startswith("12 of 20 files not scanned: 7 failed, 5 skipped "
                           "(3 time budget reached, 2 hard deadline reached); failed files (first 5 of 7): ")
    assert "F4.sol (boom 4 " in memo and "F5.sol" not in memo and "F6.sol" not in memo
    listed = memo.split("failed files (first 5 of 7): ")[1].split(", F")
    assert len(listed) == 5 and max(len(x) for x in listed) < 100   # each reason is cut to 80 chars
    assert coverage_meta(3, 3, [], []) == ({"files_total": 3, "files_scanned": 3, "files_failed": 0,
                                            "files_skipped": 0}, None)


def test_attacktrail_records_full_coverage_without_a_memo(tmp_path):
    fake = _FakeScanPy(_scan_py_in(tmp_path), ["  [1/2] A.sol  후보 1", "  [2/2] B.sol  후보 0"], 2, rows=[TRIAGE])
    out = _agent_run(tmp_path, fake)
    assert out.meta.error is None
    assert (out.meta.files_total, out.meta.files_scanned, out.meta.files_failed,
            out.meta.files_skipped) == (2, 2, 0, 0)


STILL_RUNNING = load_scan_module(SCAN_PY).STILL_RUNNING   # the reason scan.py logs for a call it abandoned (D-21)


def test_attacktrail_coverage_counts_deadline_timeouts_failures_and_oversized_web_files(tmp_path):
    lines = ["  [1/6] A.sol  후보 2",
             "  [2/6] B.sol  ERROR timeout: no response within 588s (max_retries=2)",
             "  [3/6] C d.sol  ERROR timeout: hard deadline reached (call limited to 12s)",
             "  [4/6] D.sol  후보 0",
             "  [5/6] E.sol  ERROR skipped: hard deadline reached",
             "  [6/6] F.sol  ERROR skipped: time budget reached"]
    fake = _FakeScanPy(_scan_py_in(tmp_path), lines, 6, header={"skipped_large": ["big.js", "huge.js"]})
    out = _agent_run(tmp_path, fake)
    assert out.meta.status == "ok", out.meta.error
    assert (out.meta.files_total, out.meta.files_scanned, out.meta.files_failed,
            out.meta.files_skipped) == (8, 2, 2, 4)             # 6 sent + 2 over the --web size cap
    assert out.meta.files_total == 2 + 2 + 4
    assert out.meta.error == (
        "6 of 8 files not scanned: 2 failed, 4 skipped (2 too large for --web, 1 hard deadline reached, "
        "1 time budget reached); failed files: B.sol (timeout: no response within 588s (max_retries=2)), "
        "C d.sol (timeout: hard deadline reached (call limited to 12s))")   # cut by the deadline, response not read: no abandoned note


def test_attacktrail_flags_abandoned_calls_as_unmetered_in_the_memo(tmp_path):
    # D-21 (M-1): a call scan.py gave up on at deadline + grace was sent and billed but its response was never read,
    # so it has no _usage row and tokens_in/tokens_out are low. The memo says so; the report shows the memo.
    assert ABANDONED_MARK in STILL_RUNNING     # the adapter matches scan.py's text
    lines = ["  [1/3] A.sol  후보 0", "  [2/3] B.sol  ERROR " + STILL_RUNNING, "  [3/3] C.sol  ERROR " + STILL_RUNNING]
    usage = {"_usage": {"file": "A.sol", "input_tokens": 10, "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0, "output_tokens": 5}}
    out = _agent_run(tmp_path, _FakeScanPy(_scan_py_in(tmp_path), lines, 3, rows=[usage]))
    assert out.meta.status == "ok" and (out.meta.tokens_in, out.meta.tokens_out) == (10, 5)
    assert out.meta.error.startswith("2 of 3 files not scanned: 2 failed, 0 skipped; failed files: B.sol (")
    assert out.meta.error.endswith("2 call(s) still running at the hard deadline were abandoned: no response was "
                                   "read, so their tokens are not in tokens_in/tokens_out (undercount)")
    assert (out.meta.files_failed, out.meta.files_scanned) == (2, 1)
    # no abandoned call, no note
    (tmp_path / "x").mkdir()
    out = _agent_run(tmp_path / "x", _FakeScanPy(_scan_py_in(tmp_path / "x"), lines[:1], 1, rows=[usage]))
    assert out.meta.error is None


def test_sum_usage_ignores_the_call_timing_field_scan_py_adds(tmp_path):
    # D-21 (H-3): each _usage row carries a "call" dict (start offset, duration, timeout, retries allowed). The
    # token sums read named keys only, so the extra field changes nothing and the row is still bookkeeping.
    p = tmp_path / "scan.jsonl"
    call = {"started_at_s": 1.5, "elapsed_s": 41.2, "timeout_s": 588.667, "max_retries": 2}
    rows = [{"_run": {}}, {"_usage": {"file": "A.sol", "input_tokens": 10, "cache_creation_input_tokens": 30,
                                      "cache_read_input_tokens": 20, "output_tokens": 5, "call": call}},
            {"_usage": {"file": "B.sol", "input_tokens": 1, "cache_creation_input_tokens": None,
                        "cache_read_input_tokens": None, "output_tokens": 2}}]    # no "call": an older row
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert sum_usage(p) == (61, 7)
    assert parse_scan_jsonl(p)[1] == []


def test_attacktrail_aborted_runs_stay_crashed_with_the_reason(tmp_path):
    # exit 2 is also what the consecutive-error breaker does (D-20), not only 401/402/403/404
    err = ("연속 API 오류로 스캔을 중단했습니다: aborted after 8 consecutive API errors (429). "
           "마지막 오류: Error code: 429")
    fake = _FakeScanPy(_scan_py_in(tmp_path), ["  [1/2] A.sol  ERROR Error code: 429",
                                               "  [2/2] B.sol  ERROR skipped: aborted after 8 consecutive API errors (429)"],
                       2, returncode=2, stderr=err)
    out = _agent_run(tmp_path, fake)
    assert out.meta.status == "crashed" and "scan.py exit 2" in out.meta.error
    assert "aborted after 8 consecutive API errors (429)" in out.meta.error
    assert (tmp_path / "proj" / "raw" / "scan.jsonl").is_file()


def test_attacktrail_without_progress_lines_leaves_coverage_unmeasured(tmp_path):
    out = _agent_run(tmp_path, _FakeScanPy(_scan_py_in(tmp_path), [], 1))
    assert out.meta.status == "ok"
    assert (out.meta.files_total, out.meta.files_scanned) == (None, None)


STD_PY = ('STD_BASE = ["A.md", "B.md"]\n'
          'STD_WEB = ["W.md"]\n'
          'ap.add_argument("--model", default="m")\n')


def _snapshot_agent(tmp_path, web=False):
    tools, docs = tmp_path / "tools", tmp_path / "docs"
    tools.mkdir()
    docs.mkdir()
    (tools / "scan.py").write_text(STD_PY, encoding="utf-8")
    for n in ("A.md", "B.md", "W.md"):
        (docs / n).write_text("standard " + n, encoding="utf-8")
    fake = _FakeScanPy(tools / "scan.py", ["  [1/1] A.sol  후보 0"], 1)
    return AttackTrailAgent(scan_py=tools / "scan.py", executor=fake, web=web), fake


def test_attacktrail_snapshots_scan_py_and_standards_once_per_run_dir(tmp_path):
    agent, fake = _snapshot_agent(tmp_path)
    assert "snapshot_dir" not in agent.fingerprints()          # nothing run yet
    run_dir = tmp_path / "run"
    ctx = lambda pid: RunContext(run_dir, run_dir / "projects" / pid, 10, {"ANTHROPIC_API_KEY": "k"})
    out = agent.run(Scope("p1", tmp_path / "repo"), ctx("p1"))
    assert out.meta.status == "ok", out.meta.error
    snap = run_dir / "agent_snapshot"
    assert sorted(str(p.relative_to(snap)).replace("\\", "/") for p in snap.rglob("*") if p.is_file()) == [
        "docs/A.md", "docs/B.md", "tools/scan.py"]               # base standards only (web=False)
    assert (snap / "tools" / "scan.py").read_bytes() == (tmp_path / "tools" / "scan.py").read_bytes()
    assert (snap / "docs" / "B.md").read_text(encoding="utf-8") == "standard B.md"
    assert agent.fingerprints()["snapshot_dir"] == "agent_snapshot"
    # a second project in the same run dir reuses the copy; identical bytes are fine
    first = (snap / "tools" / "scan.py").stat().st_mtime_ns
    assert agent.run(Scope("p2", tmp_path / "repo"), ctx("p2")).meta.status == "ok"
    assert (snap / "tools" / "scan.py").stat().st_mtime_ns == first


def test_attacktrail_snapshot_includes_web_standards_and_rejects_a_second_version(tmp_path):
    agent, fake = _snapshot_agent(tmp_path, web=True)
    run_dir = tmp_path / "run"
    ctx = RunContext(run_dir, run_dir / "projects" / "p", 10, {"ANTHROPIC_API_KEY": "k"})
    assert agent.run(Scope("p", tmp_path / "repo"), ctx).meta.status == "ok"
    assert (run_dir / "agent_snapshot" / "docs" / "W.md").is_file()
    # a standard edited mid-run must not be copied over silently: the run would mix two versions
    (tmp_path / "docs" / "A.md").write_text("standard A.md EDITED", encoding="utf-8")
    out = agent.run(Scope("q", tmp_path / "repo"), RunContext(run_dir, run_dir / "projects" / "q", 10,
                                                              {"ANTHROPIC_API_KEY": "k"}))
    assert out.meta.status == "crashed" and "agent_snapshot" in out.meta.error and "differs" in out.meta.error
    assert (run_dir / "agent_snapshot" / "docs" / "A.md").read_text(encoding="utf-8") == "standard A.md"


def test_attacktrail_without_key_makes_no_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent, _ = _snapshot_agent(tmp_path)
    out = agent.run(Scope("p", tmp_path / "repo"), RunContext(tmp_path / "run", tmp_path / "run" / "p", 10, {}))
    assert out.meta.status == "crashed" and not (tmp_path / "run" / "agent_snapshot").exists()


def test_attacktrail_snapshot_is_safe_under_parallel_projects(tmp_path):
    import threading
    agent, _ = _snapshot_agent(tmp_path)
    errors = []

    def go():
        try:
            agent.snapshot(tmp_path / "run")
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and (tmp_path / "run" / "agent_snapshot" / "tools" / "scan.py").is_file()


SCAN_WITH_PROMPT = ('import os\n'
                    'ROLE = "role v1"\n'
                    'STD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")\n'
                    'STD_BASE = ["A.md"]\n'
                    'STD_WEB = []\n'
                    'def load_standards(web):\n'
                    '    return open(os.path.join(STD_DIR, "A.md"), encoding="utf-8").read()\n')


def test_fingerprints_name_the_snapshot_not_a_scan_py_edited_during_the_run(tmp_path):
    # D-26 (found on the 2026-10-21 36-file run): scan.py was edited while the run was going and the manifest then
    # recorded the live file's hash although the run had started with the copy taken in the run dir. The fingerprints
    # must follow the copy: scan_py_sha256, prompt_sha256 and the legacy prompt_hash.
    import hashlib
    tools, docs = tmp_path / "tools", tmp_path / "docs"
    tools.mkdir()
    docs.mkdir()
    live = tools / "scan.py"
    live.write_text(SCAN_WITH_PROMPT, encoding="utf-8")
    (docs / "A.md").write_text("standard v1", encoding="utf-8")
    started = AttackTrailAgent(scan_py=live).fingerprints()         # what a run started now would be
    started_legacy = AttackTrailAgent(scan_py=live).prompt_hash()
    assert started["prompt_sha256"] == hashlib.sha256(b"role v1\n\nstandard v1").hexdigest()

    fake = _FakeScanPy(live, ["  [1/1] A.sol  후보 0"], 1)
    agent = AttackTrailAgent(scan_py=live, executor=fake)
    run_dir = tmp_path / "run"
    assert agent.run(Scope("p", tmp_path / "repo"),
                     RunContext(run_dir, run_dir / "projects" / "p", 10, {"ANTHROPIC_API_KEY": "k"})).meta.status == "ok"
    # the live files change after the run started (here: after it ended; the manifest is written after the run either way)
    live.write_text(SCAN_WITH_PROMPT.replace("role v1", "role v2") + "# edited during the run\n", encoding="utf-8")
    (docs / "A.md").write_text("standard v2", encoding="utf-8")

    snap = (run_dir / "agent_snapshot" / "tools" / "scan.py").read_bytes()
    fp = agent.fingerprints()
    assert fp["scan_py_sha256"] == hashlib.sha256(snap).hexdigest() == started["scan_py_sha256"]
    assert fp["scan_py_sha256"] != hashlib.sha256(live.read_bytes()).hexdigest()
    assert fp["prompt_sha256"] == started["prompt_sha256"]          # ROLE and standards as copied, not as edited
    assert agent.prompt_hash() == started_legacy
    # control: an agent that never made a snapshot (rescore, a fresh process) can only see the live file
    after = AttackTrailAgent(scan_py=live).fingerprints()
    assert after["scan_py_sha256"] == hashlib.sha256(live.read_bytes()).hexdigest() != fp["scan_py_sha256"]
    assert after["prompt_sha256"] != started["prompt_sha256"]


def test_adopt_snapshot_takes_the_copy_an_earlier_run_left_and_writes_nothing(tmp_path):
    # D-26 (rescore): a fresh agent never called snapshot(), so it could only hash the live files. adopt_snapshot()
    # points the fingerprints at the copy already in the run dir: scan_py_sha256, prompt_sha256 and prompt_hash, with
    # the standards named by the copied scan.py (not by the live one, which may have changed since).
    import hashlib
    tools, docs = tmp_path / "tools", tmp_path / "docs"
    tools.mkdir()
    docs.mkdir()
    live = tools / "scan.py"
    live.write_text(SCAN_WITH_PROMPT, encoding="utf-8")
    (docs / "A.md").write_text("standard v1", encoding="utf-8")
    started = AttackTrailAgent(scan_py=live).fingerprints()
    started_legacy = AttackTrailAgent(scan_py=live).prompt_hash()
    run_dir = tmp_path / "run"
    AttackTrailAgent(scan_py=live).snapshot(run_dir)                  # what the original run left behind
    live.write_text(SCAN_WITH_PROMPT.replace("role v1", "role v2").replace('STD_BASE = ["A.md"]', 'STD_BASE = ["Z.md"]'),
                    encoding="utf-8")
    (docs / "A.md").write_text("standard v2", encoding="utf-8")

    agent = AttackTrailAgent(scan_py=live)
    assert agent.fingerprints()["fingerprint_source"] == "live" and "snapshot_dir" not in agent.fingerprints()
    def tree():  # importing the copy for prompt_sha256 may leave a __pycache__ next to it; that is not the snapshot
        return {p: p.read_bytes() for p in run_dir.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    before = tree()
    assert agent.adopt_snapshot(run_dir) is True
    fp = agent.fingerprints()
    assert fp["scan_py_sha256"] == hashlib.sha256(
        (run_dir / "agent_snapshot" / "tools" / "scan.py").read_bytes()).hexdigest() == started["scan_py_sha256"]
    assert fp["prompt_sha256"] == started["prompt_sha256"]
    assert agent.prompt_hash() == started_legacy
    assert fp["snapshot_dir"] == "agent_snapshot" and fp["fingerprint_source"] == "snapshot"
    assert tree() == before

    # nothing to adopt: no copy, or a copy that lacks a standard its scan.py names -> False, fingerprints stay live
    none = AttackTrailAgent(scan_py=live)
    assert none.adopt_snapshot(tmp_path / "no-such-run") is False
    assert none.fingerprints()["fingerprint_source"] == "live" and "snapshot_dir" not in none.fingerprints()
    (run_dir / "agent_snapshot" / "docs" / "A.md").unlink()
    assert AttackTrailAgent(scan_py=live).adopt_snapshot(run_dir) is False


def test_model_and_effort_follow_the_snapshot_not_the_live_scan_py_defaults(tmp_path):
    # D-26 (after review): model() and effort() read the live scan.py, so a --model/--effort default edited while a run was
    # going (or before a rescore) was recorded in the manifest although the run used the copy's. A value the user gave wins.
    tools, docs = tmp_path / "tools", tmp_path / "docs"
    tools.mkdir()
    docs.mkdir()
    live = tools / "scan.py"
    scan = lambda m, e: SCAN_WITH_PROMPT + 'ap.add_argument("--model", default="%s")\nap.add_argument("--effort", default="%s")\n' % (m, e)
    live.write_text(scan("model-v1", "xhigh"), encoding="utf-8")
    (docs / "A.md").write_text("standard v1", encoding="utf-8")
    run_dir = tmp_path / "run"
    snapped, given = AttackTrailAgent(scan_py=live), AttackTrailAgent(scan_py=live, model="mine", effort="low")
    snapped.snapshot(run_dir)
    given.snapshot(run_dir)
    live.write_text(scan("model-v2", "high"), encoding="utf-8")        # the live defaults change after the run started
    assert (snapped.model(), snapped.effort()) == ("model-v1", "xhigh")
    assert snapped.fingerprints()["effort"] == "xhigh"
    assert (given.model(), given.effort()) == ("mine", "low")
    adopted = AttackTrailAgent(scan_py=live)
    assert (adopted.model(), adopted.effort()) == ("model-v2", "high")  # no snapshot yet: the live file, as before
    assert adopted.adopt_snapshot(run_dir) is True                      # rescore: the copy the run left
    assert (adopted.model(), adopted.effort()) == ("model-v1", "xhigh")


def test_adopt_snapshot_refuses_a_copy_whose_scan_py_does_not_parse(tmp_path):
    # D-26 (after review): a copy that does not parse gives no constants, hence no standards to check, and
    # all([]) said True. adopt_snapshot() must say False and leave the fingerprints live.
    live = tmp_path / "scan.py"
    live.write_text(SCAN_WITH_PROMPT, encoding="utf-8")
    copy = tmp_path / "run" / "agent_snapshot" / "tools"
    copy.mkdir(parents=True)
    (copy / "scan.py").write_text('STD_BASE = ["A.md"\n', encoding="utf-8")        # SyntaxError
    agent = AttackTrailAgent(scan_py=live)
    assert agent.adopt_snapshot(tmp_path / "run") is False
    fp = agent.fingerprints()
    assert fp["fingerprint_source"] == "live" and "snapshot_dir" not in fp


def test_agent_snapshot_dir_is_not_committable():
    # D-20: internal/ is outside git and the standards are internal documents, so the copy under a run dir must
    # stay uncommitted. .gitignore keeps eval/runs/*/* ignored except manifest.json; check git itself says so.
    import shutil as _sh
    import subprocess
    from adapters.base import REPO_ROOT
    if not _sh.which("git"):
        pytest.skip("git not available")
    ignored = ["eval/runs/x/agent_snapshot/tools/scan.py", "eval/runs/x/agent_snapshot/docs/AUD-SEV-01-severity.md",
               "eval/runs/x/rep-1/agent_snapshot/tools/scan.py", "eval/runs/x/projects/p/raw/scan.jsonl"]
    kept = ["eval/runs/x/manifest.json"]
    for rel, want in [(r, True) for r in ignored] + [(r, False) for r in kept]:
        r = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "-q", rel], capture_output=True)
        assert (r.returncode == 0) is want, (rel, r.returncode)


# ---------------------------------------------------------------------------------------------
# D-23: effort argument, api-v5, fallback counts from the _usage rows
# ---------------------------------------------------------------------------------------------

def _usage_row(file, fallback, model, tokens=10, **extra):
    return {"_usage": {"file": file, "input_tokens": tokens, "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0, "output_tokens": 5, "model": model, "fallback": fallback, **extra}}


def test_attacktrail_records_how_many_files_a_fallback_model_answered_and_which_model(tmp_path):
    rows = [_usage_row("A.sol", False, "claude-sonnet-5-5"), _usage_row("B.sol", True, "claude-sonnet-5"),
            _usage_row("C.sol", True, "claude-sonnet-5"), _usage_row("D.sol", False, "claude-sonnet-5-5")]
    lines = ["  [%d/4] %s.sol  후보 0" % (i, c) for i, c in enumerate("ABCD", 1)]
    out = _agent_run(tmp_path, _FakeScanPy(_scan_py_in(tmp_path), lines, 4, rows=rows))
    assert out.meta.status == "ok", out.meta.error
    assert (out.meta.files_fallback, out.meta.fallback_models) == (2, ["claude-sonnet-5"])
    assert (out.meta.tokens_in, out.meta.tokens_out) == (40, 20)


def test_attacktrail_fallback_counts_are_zero_when_rows_say_so_and_unmeasured_without_rows(tmp_path):
    only_plain = [_usage_row("A.sol", False, "claude-sonnet-5-5")]
    out = _agent_run(tmp_path, _FakeScanPy(_scan_py_in(tmp_path), ["  [1/1] A.sol  후보 0"], 1, rows=only_plain))
    assert (out.meta.files_fallback, out.meta.fallback_models) == (0, [])        # measured: none
    (tmp_path / "n").mkdir()
    out = _agent_run(tmp_path / "n", _FakeScanPy(_scan_py_in(tmp_path / "n"), ["  [1/1] A.sol  후보 0"], 1))
    assert (out.meta.files_fallback, out.meta.fallback_models) == (None, None)   # no usage rows: not measured
    # a row from an older scan.py has no fallback key: it is not a fallback answer
    old = [{"_usage": {"file": "A.sol", "input_tokens": 1, "output_tokens": 1}}]
    (tmp_path / "o").mkdir()
    out = _agent_run(tmp_path / "o", _FakeScanPy(_scan_py_in(tmp_path / "o"), ["  [1/1] A.sol  후보 0"], 1, rows=old))
    assert (out.meta.files_fallback, out.meta.fallback_models) == (0, [])


def test_fallback_usage_lists_distinct_models_sorted_and_ignores_non_bookkeeping_rows(tmp_path):
    from adapters.attacktrail import fallback_usage
    p = tmp_path / "s.jsonl"
    evil = dict(TRIAGE, _usage=_usage_row("X.sol", True, "evil")["_usage"])      # a candidate carrying a _usage key
    rows = [{"_run": {}}, _usage_row("A.sol", True, "claude-sonnet-5"), _usage_row("B.sol", True, "claude-opus-4-8"),
            _usage_row("C.sol", True, "claude-sonnet-5"), evil, _usage_row("D.sol", False, "claude-sonnet-5-5")]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert fallback_usage(p) == (3, ["claude-opus-4-8", "claude-sonnet-5"])
    p.write_text(json.dumps({"_run": {}}) + "\n", encoding="utf-8")
    assert fallback_usage(p) is None


def test_sum_usage_reads_the_per_attempt_totals_in_the_row_and_does_not_add_returned_or_attempts(tmp_path):
    # D-23: a fallback row's four token keys are already the per-attempt totals (cost cap: never undercount).
    # "returned" (top-level usage of the answering attempt) and "attempts" repeat part of them for audit; adding them
    # again would double count.
    att = [{"type": "message", "model": "claude-sonnet-5-5", "input_tokens": 100, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": 0},
           {"type": "fallback_message", "model": "claude-sonnet-5", "input_tokens": 10,
            "cache_creation_input_tokens": 30, "cache_read_input_tokens": 20, "output_tokens": 5}]
    row = {"_usage": {"file": "A.sol", "input_tokens": 110, "cache_creation_input_tokens": 30,
                      "cache_read_input_tokens": 20, "output_tokens": 5, "model": "claude-sonnet-5",
                      "fallback": True, "attempts": att,
                      "returned": {"input_tokens": 10, "cache_creation_input_tokens": 30,
                                   "cache_read_input_tokens": 20, "output_tokens": 5}}}
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert sum_usage(p) == (110 + 30 + 20, 5)


def test_attacktrail_passes_effort_to_scan_py_only_when_given_and_records_it(tmp_path):
    fake = _FakeScanPy(_scan_py_in(tmp_path), ["  [1/1] A.sol  후보 0"], 1)
    assert AttackTrailAgent(scan_py=fake.scan_py, executor=fake, effort="xhigh").run(
        Scope("p", tmp_path / "repo"), RunContext(tmp_path, tmp_path / "proj", 10, {"ANTHROPIC_API_KEY": "k"})
    ).meta.status == "ok"
    assert fake.argv[fake.argv.index("--effort") + 1] == "xhigh"
    (tmp_path / "d").mkdir()
    fake = _FakeScanPy(_scan_py_in(tmp_path / "d"), ["  [1/1] A.sol  후보 0"], 1)
    assert _agent_run(tmp_path / "d", fake).meta.status == "ok"
    assert "--effort" not in fake.argv and "--model" not in fake.argv          # scan.py's own defaults apply


def test_agent_fingerprints_carry_effort_and_api_call_shape_is_v6():
    # effort comes from the argument, else from scan.py's --effort default read out of its source (like the model)
    # D-24: api-v6 (streaming, max_tokens 64000). Defaults: xhigh + Fable 5.1 (D-25) until D-34 made them high + Haiku 5.5
    # (still api-v6: the call shape did not change); the non-web prompt hash is unchanged
    assert AttackTrailAgent.call_shape == "api-v6"
    assert AttackTrailAgent().fingerprints()["effort"] == "high" and AttackTrailAgent().effort() == "high"
    assert AttackTrailAgent().fingerprints()["prompt_sha256"][:16] == "2d0bb457ca2c605a"
    assert AttackTrailAgent(effort="xhigh").fingerprints()["effort"] == "xhigh"
    assert AttackTrailAgent().model() == "claude-haiku-5-5"
    assert AttackTrailAgent(model="claude-sonnet-5-5", effort="high").model() == "claude-sonnet-5-5"
    # get_agent passes it through; None is dropped like the other CLI flags
    assert get_agent("attacktrail", effort="high").effort() == "high"
    assert get_agent("attacktrail", effort=None).effort() == "high"   # scan.py default (D-34)


def test_default_project_timeout_is_per_agent():
    # D-24: Fable 5.1 at xhigh streams for minutes to tens of minutes per file, so the API adapter's default project timeout
    # is 3 h. The CLI adapter (no per-call limits) and the baseline keep 1800 s. The numbers and their basis: adapters/attacktrail.py
    from adapters.attacktrail_cli import AttackTrailCLIAgent
    assert (DEFAULT_TIMEOUT_S, MAX_CALL_S) == (10800, 1800)
    assert AttackTrailAgent().default_timeout_s == 10800
    assert AttackTrailCLIAgent().default_timeout_s == 1800
    assert get_agent("baseline").default_timeout_s == 1800


def test_effort_default_is_read_from_the_scan_py_source_not_hardcoded(tmp_path):
    scan_py = tmp_path / "scan.py"
    scan_py.write_text('ap.add_argument("--model", default="m1")\nap.add_argument("--effort", default="xhigh")\n',
                       encoding="utf-8")
    a = AttackTrailAgent(scan_py=scan_py)
    assert (a.model(), a.effort()) == ("m1", "xhigh")
    scan_py.write_text('ap.add_argument("--model", default="m1")\n', encoding="utf-8")   # an older scan.py: no flag
    assert AttackTrailAgent(scan_py=scan_py).effort() == "unknown"


@pytest.mark.parametrize("source", [
    b'ap.add_argument("--model", default=MODEL)\nap.add_argument("--effort", default=EFFORT)\n',   # not literals: ValueError
    b'ap.add_argument("--model", default=os.environ["M"])\n',                                       # a call: ValueError
    b'ap.add_argument("--model", default="m1"\n',                                                  # does not parse: SyntaxError
    b'\xff\xfe not utf-8 \x80\n',                                                                # UnicodeDecodeError (a ValueError)
])
def test_unreadable_scan_py_defaults_give_unknown_and_fingerprints_does_not_raise(tmp_path, source):
    # review: _consts() caught only OSError, so a scan.py whose --effort/--model default is not a literal made
    # fingerprints() (run while the manifest is written) die with ValueError. It records "unknown" instead.
    scan_py = tmp_path / "scan.py"
    scan_py.write_bytes(source)
    a = AttackTrailAgent(scan_py=scan_py)
    assert (a.model(), a.effort()) == ("unknown", "unknown")
    fp = a.fingerprints()
    assert fp["effort"] == "unknown" and fp["call_shape"] == "api-v6"
    assert AttackTrailAgent(scan_py=scan_py, model="m", effort="high").model() == "m"    # explicit values still win


def test_fallback_files_count_responses_that_were_not_usable_too(tmp_path):
    # review: files_fallback is "files a fallback model produced a response for". A response that then hit max_tokens (or
    # failed to parse) is an ERROR file in the coverage counts, and still counts here: the row says who answered.
    rows = [_usage_row("A.sol", False, "claude-fable-5-1"), _usage_row("B.sol", True, "claude-opus-5"),
            _usage_row("C.sol", True, "claude-opus-5")]
    lines = ["  [1/3] A.sol  후보 0", "  [2/3] B.sol  ERROR stop_reason=max_tokens (출력이 잘림. 사고 토큰도 max_tokens 를 쓴다)",
             "  [3/3] C.sol  후보 1"]
    out = _agent_run(tmp_path, _FakeScanPy(_scan_py_in(tmp_path), lines, 3, rows=rows))
    assert out.meta.status == "ok", out.meta.error
    assert (out.meta.files_fallback, out.meta.fallback_models) == (2, ["claude-opus-5"])   # B (cut at max_tokens) is in
    assert (out.meta.files_scanned, out.meta.files_failed) == (2, 1)                       # but B is a failed file


def test_attacktrail_notes_streams_that_ended_mid_way(tmp_path):
    # D-24: a stream the deadline closed or that broke leaves a partial _usage row (what message_start reported: output a lower bound).
    # The memo says how many, so tokens_out is not read as exact. Rows are counted from the JSONL, not from log text.
    lines = ["  [1/3] A.sol  후보 0", "  [2/3] B.sol  ERROR stream interrupted: RemoteProtocolError: x; 받은 만큼만 집계",
             "  [3/3] C.sol  ERROR timeout: hard deadline reached (call limited to 12s; 받은 만큼만 집계)"]
    rows = [_usage_row("A.sol", False, "claude-fable-5-1"),
            _usage_row("B.sol", False, "claude-fable-5-1", partial=True),
            _usage_row("C.sol", False, "claude-fable-5-1", partial=True)]
    out = _agent_run(tmp_path, _FakeScanPy(_scan_py_in(tmp_path), lines, 3, rows=rows))
    assert out.meta.status == "ok" and (out.meta.tokens_in, out.meta.tokens_out) == (30, 15)
    assert "2 stream(s) ended mid-way" in out.meta.error and "tokens_out is a lower bound" in out.meta.error
    # D-25: the input side is not claimed to be complete: a fallback before the cut can leave the other attempt's input out.
    # No direction ("earlier"/"later"): a cut after a mid-output fallback leaves out the LATER attempt (message_start has the
    # requested model's input), a cut after a pre-output fallback leaves out the earlier one. Both directions are an estimate
    # from the SDK implementation (_beta_messages.py lines 517-520, 580-601); nobody has seen it on the wire.
    assert "tokens_in is complete" not in out.meta.error and "the other attempt" in out.meta.error
    assert "earlier attempt" not in out.meta.error
    (tmp_path / "n").mkdir()
    out = _agent_run(tmp_path / "n", _FakeScanPy(_scan_py_in(tmp_path / "n"), lines[:1], 1, rows=rows[:1]))
    assert out.meta.error is None                      # no partial row, no note


def test_attacktrail_warns_once_on_stderr_when_the_timeout_is_below_two_soft_margins_and_still_runs(tmp_path, capsys):
    # D-25: soft limit = timeout - max(1815, 10%), never below half. That leaves room for a MAX_CALL_S call before the hard
    # limit only if timeout >= 2 x 1815 = 3630 s. Below it _run prints one warning line to stderr. It does not refuse: a
    # short test run can be what is wanted. 1815 is the margin for max_tokens 64000 (scan.py's default here), which is what
    # SOFT_MARGIN_S holds. _run computes its margin as 15 + max_call_s(max_tokens), so at 128000 it is 3615 and the
    # threshold 7230 (the 128000 cases of the parametrized test below pin that); SOFT_MARGIN_S itself is not read by the code (D-28).
    from adapters.attacktrail import SOFT_MARGIN_S
    assert 2 * SOFT_MARGIN_S == 3630    # the 64000 base value
    for sub, timeout_s, warns in [("a", 1800, True), ("b", 3629, True), ("c", 3630, False), ("d", 10800, False)]:
        (tmp_path / sub).mkdir()
        fake = _FakeScanPy(_scan_py_in(tmp_path / sub), ["  [1/1] A.sol  후보 0"], 1)
        capsys.readouterr()
        out = _agent_run(tmp_path / sub, fake, timeout_s)
        err = capsys.readouterr().err
        assert out.meta.status == "ok", out.meta.error                      # never refused
        lines = [l for l in err.splitlines() if l.startswith("attacktrail: warning")]
        assert len(lines) == (1 if warns else 0), (timeout_s, err)
        if warns:
            assert "%gs is below 3630s" % timeout_s in lines[0] and "1800s call" in lines[0]


def test_fallback_files_count_a_cut_stream_whose_message_start_named_another_model(tmp_path):
    # D-25: a partial row scan.py marked fallback (message_start already named the fallback model) counts: the fallback model
    # had begun answering when the stream broke. The file itself is a failed one.
    rows = [_usage_row("A.sol", False, "claude-fable-5-1"), _usage_row("B.sol", True, "claude-opus-5", partial=True)]
    lines = ["  [1/2] A.sol  후보 0", "  [2/2] B.sol  ERROR stream interrupted: RemoteProtocolError: x; 받은 만큼만 집계"]
    out = _agent_run(tmp_path, _FakeScanPy(_scan_py_in(tmp_path), lines, 2, rows=rows))
    assert out.meta.status == "ok", out.meta.error
    assert (out.meta.files_fallback, out.meta.fallback_models) == (1, ["claude-opus-5"])
    assert (out.meta.files_scanned, out.meta.files_failed) == (1, 1)


# ---------------------------------------------------------------------------------------------
# D-28: effort max, --max-tokens, and the time constants that follow max_tokens
# ---------------------------------------------------------------------------------------------

def _run_with(tmp_path, timeout_s=10800, **opts):
    """Run the real adapter against a fake scan.py executor. Returns (findings, fake, agent)."""
    fake = _FakeScanPy(_scan_py_in(tmp_path), ["  [1/1] A.sol  후보 0"], 1)
    agent = AttackTrailAgent(scan_py=fake.scan_py, executor=fake, **opts)
    out = agent.run(Scope("p", tmp_path / "repo"),
                    RunContext(tmp_path, tmp_path / "proj", timeout_s, {"ANTHROPIC_API_KEY": "k"}))
    return out, fake, agent


def test_effort_max_and_max_tokens_reach_scan_py_argv_and_the_fingerprints(tmp_path):
    out, fake, agent = _run_with(tmp_path, effort="max", max_tokens=128000, model="claude-opus-5-5")
    assert out.meta.status == "ok", out.meta.error
    assert fake.argv[fake.argv.index("--effort") + 1] == "max"
    assert fake.argv[fake.argv.index("--max-tokens") + 1] == "128000"
    fp = agent.fingerprints()
    assert (fp["effort"], fp["max_tokens"], fp["call_shape"]) == ("max", 128000, "api-v6")   # a value, not a new call shape
    (tmp_path / "d").mkdir()
    out, fake, agent = _run_with(tmp_path / "d")                       # nothing given: scan.py's own defaults, no flags
    assert out.meta.status == "ok" and "--max-tokens" not in fake.argv and "--effort" not in fake.argv


def test_max_tokens_default_is_scan_pys_constant_and_cli_flag_none_is_dropped():
    # the real scan.py: MAX_TOKENS 64000 (D-24), read from its source like the model and effort defaults
    assert AttackTrailAgent().max_tokens() == 64000 == AttackTrailAgent().fingerprints()["max_tokens"]
    assert AttackTrailAgent(max_tokens=128000).max_tokens() == 128000
    assert get_agent("attacktrail", max_tokens=128000).max_tokens() == 128000
    assert get_agent("attacktrail", max_tokens=None).max_tokens() == 64000


def test_max_tokens_default_is_read_from_the_scan_py_source_not_hardcoded(tmp_path):
    scan_py = tmp_path / "scan.py"
    scan_py.write_text("MAX_TOKENS = 100000\n", encoding="utf-8")
    assert AttackTrailAgent(scan_py=scan_py).max_tokens() == 100000
    assert AttackTrailAgent(scan_py=scan_py, max_tokens=5).max_tokens() == 5             # explicit wins
    scan_py.write_text("X = 1\n", encoding="utf-8")                                      # an older scan.py without it
    assert AttackTrailAgent(scan_py=scan_py).max_tokens() == "unknown"
    scan_py.write_text("MAX_TOKENS = int(os.environ['N'])\n", encoding="utf-8")          # not a literal: ValueError inside
    a = AttackTrailAgent(scan_py=scan_py)
    assert a.max_tokens() == "unknown" and a.fingerprints()["max_tokens"] == "unknown"   # must not raise (manifest time)


@pytest.mark.parametrize("bad", [0, -1, 128001, 10 ** 9, 1.5, "64000", True])
def test_max_tokens_outside_1_to_128000_is_refused_by_the_constructor(bad):
    with pytest.raises(ValueError, match="max_tokens must be an integer from 1 to 128000"):
        AttackTrailAgent(max_tokens=bad)


@pytest.mark.parametrize("n", [1, 16000, 63999, 64000, 64001, 100000, 127999, 128000])
def test_adapter_max_call_s_is_scan_pys_call_cap(n):
    # D-28: the adapter's longest-call figure and scan.py's no-data limit follow max_tokens by the same formula, by hand
    from adapters.attacktrail import max_call_s
    scan = load_scan_module()
    assert max_call_s(n) == scan.call_cap(n)
    assert max_call_s(n) == (1800 if n <= 64000 else -(-1800 * n // 64000))             # 64000 and below: unchanged
    assert max_call_s(n + 1) >= max_call_s(n) or n == 128000


def test_max_call_s_covers_the_slow_case_at_128000_tokens_and_unknown_falls_back_to_the_64000_value():
    from adapters.attacktrail import max_call_s
    import math
    assert max_call_s(128000) == 3600 >= math.ceil(128000 / 36)       # 36 tok/s worst case ~3,556 s; measured 74 tok/s ~1,730 s
    assert 3600 > 128000 / 74
    assert max_call_s(None) == max_call_s("unknown") == MAX_CALL_S == 1800


@pytest.mark.parametrize("max_tokens, timeout_s, budget, deadline, warns", [
    # 128000: MAX_CALL 3600, soft margin 15 + 3600 = 3615, so the margin holds from 2 x 3615 = 7230 s up (D-28)
    (128000, 10800, "7185", "10785", False), (128000, 7230, "3615", "7215", False), (128000, 7229, "3614", "7214", True),
    (100000, 10800, "7972", "10785", False),            # 100000: MAX_CALL 2813, margin 2828
    (64000, 10800, "8985", "10785", False), (16000, 10800, "8985", "10785", False)])   # 64000 and below: as in D-24/D-25
def test_time_budget_and_deadline_follow_max_tokens(tmp_path, capsys, max_tokens, timeout_s, budget, deadline, warns):
    out, fake, _ = _run_with(tmp_path, timeout_s, max_tokens=max_tokens)
    err = capsys.readouterr().err
    assert out.meta.status == "ok", out.meta.error
    assert fake.argv[fake.argv.index("--time-budget") + 1] == budget
    assert fake.argv[fake.argv.index("--deadline-s") + 1] == deadline
    lines = [l for l in err.splitlines() if l.startswith("attacktrail: warning")]
    assert len(lines) == (1 if warns else 0), err
    if warns:
        assert "project timeout 7229s is below 7230s (2 x the 3615s soft margin)" in lines[0] and "a 3600s call" in lines[0]
    elif max_tokens > 64000:       # the margin really leaves room for one worst-case call of this size
        from adapters.attacktrail import max_call_s
        assert int(deadline) - int(budget) >= max_call_s(max_tokens)


def test_default_project_timeout_stays_10800_whatever_max_tokens_is():
    # D-28 decision: 16 files x 4 workers x 4 rounds of a worst-case 3600 s call is 14,400 s > 10800, but only when every file uses
    # all 128000 tokens at 36 tok/s. The measured speed (74 tok/s) gives 4 x 1,730 s = 6,920 s, inside the 7185 s soft limit. The
    # default is not raised; --timeout 14500 covers the pathological case (soft 10885, hard 14485: round 4 starts at 10800)
    from adapters.attacktrail import DEADLINE_MARGIN_S, max_call_s
    assert AttackTrailAgent(max_tokens=128000).default_timeout_s == DEFAULT_TIMEOUT_S == 10800
    soft = lambda timeout: max(timeout - max(DEADLINE_MARGIN_S + max_call_s(128000), 0.1 * timeout), timeout / 2)
    assert 3 * 1730 < soft(10800)                      # measured speed: round 4 starts well inside the soft limit
    assert 2 * 3600 > soft(10800) == 7185              # 36 tok/s worst case: round 3 starts 15 s past it, 8 files skipped (visible)
    assert 3 * 3600 < soft(14500) and 4 * 3600 < 14500 - DEADLINE_MARGIN_S      # --timeout 14500 covers it
