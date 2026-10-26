"""Adapter for the AttackTrail Step-01 scanner (internal/tools/scan.py).

scan.py is run as `python scan.py <repo> --out <project_dir>/raw/scan.jsonl
--time-budget S --deadline-s S [--model M] [--effort E] [--max-tokens N] [--web]` and this adapter parses that file.
(scan.py gained --out, --workers, --time-budget and usage rows in D-19, --deadline-s and the
consecutive-error breaker in D-20, --effort, server-side fallback and the 5m cache TTL in D-23,
in D-24 streaming calls, max_tokens 64000 and the xhigh default, and in D-25 the claude-fable-5-1 default model, the
first_event_s / max_gap_s call log and a 1800 s no-data limit; in D-26 the non-web file selection stopped sending
test, ignition, script(s), deploy and mocks directories; in D-28 --effort accepts max, --max-tokens (1..128000) was added
and the no-data limit scales with it; its prompt is unchanged.)
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path

from adapters.base import (REPO_ROOT, Agent, Evidence, Finding, Findings, Meta,
                           RunContext, Scope, map_severity, scoped_env)

SCAN_PY = REPO_ROOT / "internal" / "tools" / "scan.py"
KEY = "ANTHROPIC_API_KEY"
# anthropic 1.15.0 looks for the active-profile pointer under the config dir while the
# client is created, even when ANTHROPIC_API_KEY is set (lib/credentials/_constants.py
# _config_dir via _client._warn_env_shadow). With no home it raises "Could not determine
# home directory" (D-18). scan.py gets an empty temp dir for these three, so it neither
# crashes nor depends on the real profile path. ANTHROPIC_CONFIG_DIR is deliberately not
# set: any value makes the SDK warn "ANTHROPIC_API_KEY ... takes precedence" on every
# run (D-19). On Windows the config dir is %APPDATA%\Anthropic, so APPDATA covers it.
SDK_HOME_KEYS = ("HOME", "USERPROFILE", "APPDATA")
# The harness kills scan.py at ctx.timeout_s and this adapter then discards every result
# (D-19). scan.py gets two limits derived from that timeout (D-20, D-24):
#   --time-budget  soft. timeout - max(soft margin, 10%), never below half. The soft margin is computed in _run() as
#                  DEADLINE_MARGIN_S + max_call_s(max_tokens), in proportion to max_tokens: 3615 s at 128000.
#                  SOFT_MARGIN_S below is that margin for max_tokens 64000 (1815 s), a base-case constant the code
#                  does not read (D-28). New file calls stop starting here, so a call that starts just before it
#                  can still run for MAX_CALL_S and end before the hard limit instead of being cut mid-stream
#                  (billed, result lost). That holds only when the harness timeout is at least 2 x the soft margin
#                  (2 x SOFT_MARGIN_S = 3630 s at 64000). Below
#                  that the half-timeout floor binds, the soft limit sits closer to the hard one than
#                  MAX_CALL_S, and a call started near it can still be cut. _run() prints one warning line
#                  for such a timeout and does not refuse it: a short test run can be what is wanted (D-25).
#                  With --max-tokens above 64000 (D-28) MAX_CALL_S, and so the margin and the 3630 s, grow in proportion.
#                  At 128000 max_call_s() gives the call limit 3600 s, the soft margin 3615 s and the short-timeout warning threshold 7230 s.
#   --deadline-s   hard. timeout - DEADLINE_MARGIN_S, never below half. scan.py closes every open
#                  stream at this time (what it already received is logged as partial usage) and
#                  whatever still hangs is written off, so scan.py writes its results and exits
#                  before the harness kills it.
# Both are seconds from scan.py's main() entry. The harness clock starts at spawn.
#
# D-24 numbers, all estimates (no API response time has been measured; the pilot measures them):
#   MAX_CALL_S         the longest call the soft margin must leave room for. Claude Fable 5.1 (the same
#                      model as Mythos 5.1) at xhigh can take minutes to tens of minutes per request on hard
#                      work (model-migration.md); 30 min is the top of that range. With max_tokens 64000 that
#                      means an effective ~36 output tokens/s (thinking tokens are output tokens). If
#                      max_tokens or the model changes, revisit this, DEFAULT_TIMEOUT_S and scan.py's
#                      CALL_TIMEOUT_S, which is set equal to MAX_CALL_S by hand (D-25).
#   DEFAULT_TIMEOUT_S  project timeout when --timeout is not given (3 h). The pilot is secondswap, 36 files,
#                      workers 4: 9 rounds of calls. At 10k output tokens per file and the same ~36 tok/s a
#                      call is ~280 s, so ~42 min; at 20k tokens ~84 min; 3 h leaves ~2x on the latter. All
#                      64k-token calls (worst case) would need 36/4 x 1800 s = 4.5 h and end as partial
#                      coverage, which the report shows. The CLI adapter and the baseline keep 1800 s.
MAX_CALL_S = 1800
DEFAULT_TIMEOUT_S = 10800
# D-28: MAX_CALL_S is the call length for max_tokens 64000 (BASE_MAX_TOKENS, ~36 tok/s). A larger --max-tokens scales it in
# proportion, the same formula as scan.py call_cap() (a test pins that they agree): 128000 -> 3600 s, which covers the
# ~3,556 s that many tokens take at 36 tok/s (at the measured 74 tok/s it is ~1,730 s). Below 64000 nothing changes.
# DEFAULT_TIMEOUT_S stays 10800 for every max_tokens. Only the pathological case leaves files skipped: every file uses all
# 128000 tokens, 16 files on 4 workers are 4 rounds, and the speed decides how many rounds start before the 7,185 s soft limit
# (timeout 10800 - 3615). At 36 tok/s a call takes 3,556 s: round 2 ends at 7,111 s, round 3 starts then and ends at 10,667 s,
# and round 4 (4 files) ends as "skipped: time budget reached". At 3,600 s per call (about 35.6 tok/s or slower) round 2 ends at 7,200 s, 15 s past the soft
# limit, so rounds 3 and 4 (8 files) are skipped. Coverage shows 12/16 or 8/16. Pass --timeout 14500 for either (soft 10885):
# round 4 starts at 10,667 s and ends at 14,222 s at 36 tok/s (3,556 s per call), or starts at 10,800 s and ends at 14,400 s at
# 3,600 s per call. At 74 tok/s 4 rounds x 1,730 s = 6,920 s fits the default soft limit (D-28).
BASE_MAX_TOKENS = 64000
MAX_OUTPUT_TOKENS = 128000  # scan.py MAX_TOKENS_LIMIT: the model output cap (Fable 5.1, Opus 5.5)


def max_call_s(max_tokens: int | None) -> int:
    """Longest call (s) the soft margin must leave room for, given scan.py's max_tokens. None/unknown: the 64000 value."""
    if not isinstance(max_tokens, int):
        return MAX_CALL_S
    return math.ceil(MAX_CALL_S * max(max_tokens, BASE_MAX_TOKENS) / BASE_MAX_TOKENS)


# Margin of the hard limit, for what happens outside scan.py's clock. Measured 2026-10-20 on this
# machine (Windows 11, Python 3.12.10, eval/.venv), as process end minus --deadline-s, both from
# spawn, with a fake API server on localhost: calls cut by the SDK timeout end +0.61..0.75 s
# after the deadline (3 runs); with a call the SDK cannot cut (a server that drips its reply)
# scan.py waits HARD_GRACE_S = 2 s for it, gives up and ends +2.23..2.66 s after (3 runs).
# Spawn -> main() entry is 0.08-0.12 s (7 runs, included above). Worst measured 2.7 s; 15 s is
# about 5.5x that, room for a busy machine (--jobs > 1 runs several scans at once).
DEADLINE_MARGIN_S = 15
# The soft margin for max_tokens 64000 (BASE_MAX_TOKENS), 1815 s. Nothing reads it: _run() uses DEADLINE_MARGIN_S +
# max_call_s(max_tokens), which is 3615 s at 128000 (D-28). The name stays because the D-25 test pins this base value.
SOFT_MARGIN_S = DEADLINE_MARGIN_S + MAX_CALL_S  # at 64000: a worst-case call started at the soft limit ends before the hard one
SNAPSHOT_DIR = "agent_snapshot"  # <run_dir>/agent_snapshot/{tools/scan.py, docs/<standard>.md}
# scan.py prints one line per file: "  [i/n] <file>  ERROR <reason>" or "  [i/n] <file>  후보 <k>"
PROGRESS = re.compile(r"^\s*\[(\d+)/(\d+)\] (.+?)  (ERROR|후보) (.*)$")
MAX_LISTED = 5  # failed files named in the partial-failure memo
REASON_CHARS = 80
# Text scan.py puts in the ERROR reason of a call it abandoned at the hard deadline (scan.py STILL_RUNNING).
# No response was read for it, so it has no _usage row (D-21).
ABANDONED_MARK = "call still running"
# scan.py's whole answer to a repo with nothing to send: `sys.exit("스캔할 파일이 0개입니다: ...")`, which is exit 1 and the
# text on stderr, before any client or API call. Matched together with exit 1 and not by changing scan.py, so the scan.py
# bytes (scan_py_sha256) stay what they were (D-27). tests/test_scan_select.py runs the real scan.py to pin both.
NO_FILES_MARK = "스캔할 파일이 0개입니다"
# D-28: with --web, every candidate file over MAX_BYTES_WEB also ends in the message above; scan.py then names the count as
# "크기 상한 초과로 뺀 파일 N개" (select_files' too_big). The adapter puts that reason in the no_files error text
# (CLI adapter: from select_files directly), so "nothing of a supported type" and "everything too big" read differently.
TOO_BIG_RE = re.compile(r"크기 상한 초과로 뺀 파일 (\d+)개")


def too_big_reason(n: int) -> str:
    return "크기 상한 초과: --web 후보 파일 %d개가 모두 MAX_BYTES_WEB 를 넘어 보내지 않음" % n
# What a no_files project records for coverage: nothing was selected, so every count is a measured 0 (not 미측정).
NO_FILES_COVERAGE = {"files_total": 0, "files_scanned": 0, "files_failed": 0, "files_skipped": 0}


def load_scan_module(scan_py: Path = SCAN_PY):
    """Import scan.py without running main(); anthropic is only imported inside main()."""
    spec = importlib.util.spec_from_file_location("attacktrail_scan", scan_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def system_prompt(scan, web: bool) -> str:
    """The system prompt text the model reads: ROLE then the standards.

    scan.py sends these as two system blocks; the CLI adapter sends this exact string.
    """
    return scan.ROLE + "\n\n" + scan.load_standards(web)


def _sha256_file(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return "unknown"


def _scan_constants(scan_py: Path) -> dict:
    """Read STD_BASE, STD_WEB, MAX_TOKENS (the --max-tokens default, D-28) and the --model / --effort defaults from
    scan.py's source (no import)."""
    tree = ast.parse(scan_py.read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in ("STD_BASE", "STD_WEB", "MAX_TOKENS"):
            out[node.targets[0].id] = ast.literal_eval(node.value)
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_argument" \
                and node.args and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value in ("--model", "--effort"):
            for kw in node.keywords:
                if kw.arg == "default":
                    out["default_" + node.args[0].value[2:]] = ast.literal_eval(kw.value)
    return out


def _only(row: dict, key: str) -> bool:
    """True for scan.py's own bookkeeping rows: the one key is `key` and nothing else.

    A candidate dict that merely carries an extra "_usage" or "_run" key is not one. scan.py's
    check_candidates() now rejects such candidates, and this keeps the readers right on a
    JSONL written by anything else (blackbox "ad": a candidate with "_usage": {input_tokens:
    10**6} inflated tokens_in by a million).
    """
    return set(row) == {key}


def parse_scan_jsonl(path: Path) -> tuple[dict, list[Finding], int]:
    """Returns (_run header, triage candidates as Findings, count skipped for empty text).

    Candidates with _status != "triage" (rejected_by_scanner) are dropped: scan.py
    itself withholds them from human reviewers.
    """
    run: dict = {}
    findings: list[Finding] = []
    skipped = 0
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            c = json.loads(line)
            if _only(c, "_run"):
                run = c["_run"]
                continue
            if _only(c, "_usage"):  # token bookkeeping row, never a candidate (see sum_usage)
                continue
            if c.get("_status") != "triage":
                continue
            why = (c.get("why") or "").strip()
            poc = (c.get("poc_sketch") or "").strip()
            desc = why + ("\n\nPoC sketch: " + poc if poc else "")
            if not desc.strip():
                skipped += 1
                continue
            ln = c.get("line")
            findings.append(Finding(
                id="at-%d" % n,
                title=c.get("title") or "",
                file=c.get("file"),
                lines=[ln] if isinstance(ln, int) and ln > 0 else [],
                # scan.py's schema enum is Critical/High/Medium/Low/Informational
                severity=map_severity(c.get("severity_guess")) or "info",
                description=desc.strip(),
                confidence=None,
                evidence=Evidence(tool_output={
                    "code_excerpt": c.get("code_excerpt"),
                    "category": c.get("category"),
                    "_verify": c.get("_verify"),
                }),
            ))
    return run, findings, skipped


def _usage_rows(path: Path):
    """The `_usage` dicts of a scan JSONL: only rows whose single key is "_usage" (see _only)."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line) if line.strip() else {}
            if _only(row, "_usage"):
                yield row["_usage"]


def sum_usage(path: Path) -> tuple[int, int] | None:
    """(tokens_in, tokens_out) summed over scan.py's {"_usage": {...}} rows, None if there are none.

    scan.py writes one row per file it got a response for, failed parses included
    (refusal, max_tokens), since those calls were billed. tokens_in is every billed input
    kind added up: input_tokens + cache_creation_input_tokens + cache_read_input_tokens.
    The kinds carry different prices, so cost_usd is not derived from this sum (D-19).
    Only a row whose single key is "_usage" counts (see _only). The CLI adapter's rows have
    the same name `input_tokens` for the sum of all three kinds (and `input_tokens_uncached` for
    the raw value); the row says which meaning applies, so this works on both (D-20).
    The four token keys of an API row are already the per-attempt totals when a file took more than
    one attempt (refused, then served by the fallback model), so this sum does not undercount (D-23).
    A `partial` row (a stream that was cut or broke, D-24) holds what the stream had reported at message_start:
    the input tokens are those of the attempt message_start named (if a fallback happened before the cut, the other
    attempt's input can be missing, so tokens_in can be short too), and output_tokens is only the value at
    message_start, so tokens_out is a lower bound (D-25).
    """
    tin = tout = 0
    seen = False
    for u in _usage_rows(path):
        seen = True
        if "input_tokens_uncached" in u:  # CLI row: input_tokens is already the sum
            tin += u.get("input_tokens") or 0
        else:
            tin += sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                "cache_read_input_tokens"))
        tout += u.get("output_tokens") or 0
    return (tin, tout) if seen else None


def partial_rows(path: Path) -> int:
    """How many `_usage` rows are partial: the stream was cut or broke and the row holds what it had reported (D-24)."""
    return sum(1 for u in _usage_rows(path) if u.get("partial") is True)


def fallback_usage(path: Path) -> tuple[int, list[str]] | None:
    """(files a fallback model produced a response for, their sorted distinct models), None without `_usage` rows (D-23).

    A row counts when scan.py set fallback true: a fallback model produced a response for that file, which
    includes a response that then failed to parse or was cut at max_tokens (the row says who answered, not
    whether the answer was usable). A file whose fallback model also refused is not counted (nobody answered).
    A partial row (a cut stream) counts when message_start already named a model other than the requested one:
    the fallback model had begun answering when the stream broke, though no complete response exists (D-25).
    The docs describe sticky routing (after a fallback, later requests of the same conversation go straight
    to the fallback model for about an hour); whether it applies across files of a scan is unmeasured, and if
    it does this is larger than the number of refusals.
    """
    rows = list(_usage_rows(path))
    hit = [u for u in rows if u.get("fallback") is True]
    return (len(hit), sorted({u["model"] for u in hit if u.get("model")})) if rows else None


def coverage_meta(total: int, scanned: int, failed: list[tuple[str, str]],
                  skipped: list[str]) -> tuple[dict, str | None]:
    """Meta files_* fields and the memo for a scan that left files unscanned (D-20).

    failed:  [(file, reason)] calls that errored or whose response could not be read.
    skipped: [reason] files never sent (time budget, deadline, aborted run, too large).
    total == scanned + len(failed) + len(skipped). The memo is None when everything was scanned.
    It names the first MAX_LISTED failed files; skipped files are only counted by reason.
    """
    fields = {"files_total": total, "files_scanned": scanned,
              "files_failed": len(failed), "files_skipped": len(skipped)}
    if not failed and not skipped:
        return fields, None
    head = "%d of %d files not scanned: %d failed, %d skipped" % (
        len(failed) + len(skipped), total, len(failed), len(skipped))
    if skipped:
        head += " (%s)" % ", ".join("%d %s" % (n, r) for r, n in Counter(skipped).most_common())
    parts = [head]
    if failed:
        more = " (first %d of %d)" % (MAX_LISTED, len(failed)) if len(failed) > MAX_LISTED else ""
        parts.append("failed files%s: %s" % (more, ", ".join(
            "%s (%s)" % (f, r[:REASON_CHARS]) for f, r in failed[:MAX_LISTED])))
    return fields, "; ".join(parts)


class AttackTrailAgent(Agent):
    name = "attacktrail"
    # Bump by hand when the structure of scan.py's request body changes (endpoint, system block layout, output format, which
    # parameters are sent) or this adapter's argv changes. prompt_sha256 cannot see call-shape changes. A value the run is
    # given as an option (model, effort, max_tokens) is not part of the shape: it is recorded in the manifest fields
    # agent.model, agent.effort, agent.max_tokens, and `eval compare` warns when they differ (D-28). max_tokens used to be a
    # constant, so api-v2 and api-v6 below changed it together with a shape change.
    # api-v2 (D-17): max_tokens 8000 -> 16000; response read by parse_candidates()
    # (text blocks only, stop_reason checked) instead of content[0].text.
    # api-v3 (D-19): files are called concurrently (scan.py --workers, default 4) instead
    # of one by one, under a --time-budget. Concurrency changes rate-limit behaviour and
    # which calls share a prompt-cache write, so runs are not like-for-like with api-v2.
    # api-v4 (D-20): each call gets a timeout and a retry count sized to the time left before
    # --deadline-s (the SDK's 600 s / 2 retries only while time is plentiful; a call that would
    # outlive the deadline is cut), a run stops after 8 consecutive API errors, and 402 is
    # fatal. That changes which files get a result, and when, so api-v3 runs are not
    # like-for-like. The request body (system blocks, output format, max_tokens) is unchanged.
    # api-v5 (D-23): the call goes to the beta endpoint (client.beta.messages) with
    # betas=["server-side-fallback-2026-07-01"] and fallbacks="default", output_config.effort is
    # sent explicitly, and the system block's cache TTL is 5m instead of 1h.
    # api-v6 (D-24): the call streams (client.beta.messages.stream, stream=true on the wire), max_tokens
    # is 64000 instead of 16000, and the defaults are model claude-fable-5-1 and effort xhigh (D-25 changed
    # the model from claude-mythos-5-1, which this account cannot reach; no api-v6 API run predates that, so
    # the v6 label was kept). A
    # streamed call is not cut by an httpx read timeout (which now means "no bytes for a while"): the
    # hard deadline closes the stream, and a stream that breaks mid-way is an error that is not retried
    # (the SDK retried a dropped non-streaming request). The request body keeps the same system blocks,
    # output format, betas and fallbacks.
    # D-28: effort "max" and --max-tokens (up to 128000) change values in the request body, not its shape (same endpoint,
    # betas, fallbacks, system blocks, output format, stream), so the label stays api-v6 by the rule above. The values are in
    # manifest agent.effort and agent.max_tokens.
    call_shape = "api-v6"
    default_timeout_s = DEFAULT_TIMEOUT_S

    def __init__(self, model: str | None = None, web: bool = False,
                 python: str | None = None, scan_py: Path = SCAN_PY, executor=None,
                 effort: str | None = None, max_tokens: int | None = None):
        self._model = model
        self._effort = effort  # None: scan.py's own default (read from its source, like the model)
        # None: scan.py's own default (its MAX_TOKENS constant). Checked here as well as in scan.py: the time limits below
        # follow it, and a direct caller should not get a nonsense value silently.
        if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int)
                                       or not 1 <= max_tokens <= MAX_OUTPUT_TOKENS):
            raise ValueError("max_tokens must be an integer from 1 to %d, got %r" % (MAX_OUTPUT_TOKENS, max_tokens))
        self._max_tokens = max_tokens
        self.web = web
        # scan.py needs the anthropic SDK; eval/.venv has it since 2026-10-19 (D-17).
        self.python = python or sys.executable
        self.scan_py = Path(scan_py)
        self._executor = executor
        self._snap_lock = threading.Lock()  # --jobs > 1 runs projects in threads on one agent
        self.snapshot_dir: str | None = None  # set once a snapshot exists in some run dir
        # The run-dir copy of scan.py and its sha256, taken right after snapshot() copied it (D-26). The live file can
        # be edited while a long run is going; the fingerprints must name the code that was started, not that edit.
        self._snap_scan: Path | None = None
        self._snap_sha: str | None = None
        # --model/--effort defaults of that copy (None: no snapshot, read the live file). {} is a copy that gave none.
        self._snap_consts: dict | None = None

    def _consts(self, scan_py: Path | None = None) -> dict:
        # A scan.py whose --model/--effort default is not a literal (ast.literal_eval raises ValueError), that does
        # not parse (SyntaxError) or is not valid UTF-8 (UnicodeDecodeError, a ValueError) gives "unknown"
        # instead of killing fingerprints(), which runs while the manifest is written.
        try:
            return _scan_constants(scan_py or self.scan_py)
        except (OSError, SyntaxError, ValueError):
            return {}

    def _defaults(self) -> dict:
        """scan.py's --model/--effort defaults as the run saw them: the snapshot's once there is one, else the live file."""
        return self._consts() if self._snap_consts is None else self._snap_consts

    def model(self) -> str:
        return self._model or self._defaults().get("default_model", "unknown")

    def effort(self) -> str:
        """output_config.effort this run sends. The CLI adapter overrides it: `claude -p` gets none."""
        return self._effort or self._defaults().get("default_effort", "unknown")

    def max_tokens(self):
        """max_tokens this run sends (D-28): the argument, else scan.py's MAX_TOKENS read from its source ("unknown" if absent).
        The CLI adapter overrides it: `claude -p` is given none."""
        return self._max_tokens or self._defaults().get("MAX_TOKENS", "unknown")

    def _src(self) -> Path:
        """scan.py as it ran: the run-dir copy once snapshot() has made one, else the live file (D-26).

        The manifest is written after the run. A scan.py edited in between would otherwise be what gets hashed.
        The copy sits next to its own docs/ copy, so scan.py's STD_DIR resolves to the standards that were copied.
        model() and effort() follow the copy too (D-26, after review): an --model/--effort given by the user wins, else the
        copy's defaults, which scan.py logs in the _run row.
        """
        return self._snap_scan or self.scan_py

    def fingerprints(self) -> dict:
        """Three separate identities for the manifest (eval/DECISIONS.md D-14), plus the effort setting.

        scan_py_sha256  scan.py file bytes; integrity record, changes on any edit. After a run this is the hash of
                        the snapshot taken at its start (D-26), not of the live file at manifest time
        prompt_sha256   system prompt text the model reads; compare runs by this (from the snapshot after a run)
        call_shape      how the model is called; bumped by hand
        effort          output_config.effort (D-23); "n/a" for the CLI adapter, which sends none
        max_tokens      request max_tokens (D-28); "n/a" for the CLI adapter. Not part of call_shape: it changes a value, not the shape
        snapshot_dir    only after a run: where scan.py and the standards it loaded were copied,
                        relative to the run dir (see snapshot())
        fingerprint_source  "snapshot" once snapshot() or adopt_snapshot() found a run-dir copy, else "live"
                        (the live scan.py: a rescore of a run dir without agent_snapshot, a run that never started)
        """
        try:
            prompt = hashlib.sha256(system_prompt(load_scan_module(self._src()), self.web)
                                    .encode("utf-8")).hexdigest()
        except Exception:  # called while writing the manifest; must not raise
            prompt = "unknown"
        out = {"scan_py_sha256": self._snap_sha or _sha256_file(self.scan_py), "prompt_sha256": prompt,
               "call_shape": self.call_shape, "effort": self.effort(), "max_tokens": self.max_tokens(),
               "fingerprint_source": "snapshot" if self._snap_sha else "live"}
        if self.snapshot_dir:
            out["snapshot_dir"] = self.snapshot_dir
        return out

    def snapshot(self, run_dir: Path) -> None:
        """Copy scan.py and the standards it loads into <run_dir>/agent_snapshot/, once (D-20).

        internal/ is outside git, so without this a run cannot be reproduced after scan.py or a
        standard changes. The layout mirrors internal/ (tools/scan.py, docs/<name>.md), so
        `python agent_snapshot/tools/scan.py` finds its standards at the same relative path.
        A copy that already exists must be byte-identical: two versions in one run would make
        its prompt identity ambiguous, so a difference raises and the project is recorded as
        crashed. Projects run in threads and share the directory, hence the lock.
        Never commit this dir: eval/runs/*/* is ignored except manifest.json (.gitignore).
        """
        base = Path(run_dir) / SNAPSHOT_DIR
        c = self._consts()
        names = list(c.get("STD_BASE", [])) + (list(c.get("STD_WEB", [])) if self.web else [])
        std_dir = self.scan_py.parent / ".." / "docs"  # scan.py's STD_DIR
        items = [(self.scan_py, base / "tools" / "scan.py")] + [
            (std_dir / n, base / "docs" / n) for n in names]
        with self._snap_lock:
            for src, dst in items:
                data = src.read_bytes()
                if dst.exists():
                    if dst.read_bytes() != data:
                        raise RuntimeError(
                            "agent_snapshot %s differs from %s (sha256 %s vs %s): this run dir "
                            "already holds another version" % (
                                dst, src, hashlib.sha256(dst.read_bytes()).hexdigest()[:16],
                                hashlib.sha256(data).hexdigest()[:16]))
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(data)
            self.snapshot_dir = SNAPSHOT_DIR
            # Hash the copy, not the live file: scan.py starts right after this call and reads its own bytes once.
            self._snap_scan = base / "tools" / "scan.py"
            self._snap_sha = _sha256_file(self._snap_scan)
            self._snap_consts = self._consts(self._snap_scan)  # from the copy, not the live file read above

    def adopt_snapshot(self, run_dir: Path) -> bool:
        """Take an existing <run_dir>/agent_snapshot as the fingerprint source without running anything (D-26, rescore).

        `eval rescore` builds a fresh agent, so it never called snapshot(). The copy the original run made is still in
        the run dir; this points _src() and the stored hash at it, so scan_py_sha256, prompt_sha256 and prompt_hash name
        the code that run started with, not the live file at rescore time. Nothing is copied; only the import that
        computes prompt_sha256 may leave a __pycache__ beside the copy (as after snapshot()).
        False (nothing changed, fingerprints stay live) when tools/scan.py is missing or gives no constants (it does not
        parse: all([]) on an empty standards list would have said True), or any standard it names is missing.
        """
        base = Path(run_dir) / SNAPSHOT_DIR
        scan = base / "tools" / "scan.py"
        if not scan.is_file():
            return False
        c = self._consts(scan)
        if not c:
            return False
        names = list(c.get("STD_BASE", [])) + (list(c.get("STD_WEB", [])) if self.web else [])
        if not all((base / "docs" / n).is_file() for n in names):
            return False
        with self._snap_lock:
            self.snapshot_dir = SNAPSHOT_DIR
            self._snap_scan = scan
            self._snap_sha = _sha256_file(scan)
            self._snap_consts = c
        return True

    def prompt_hash(self) -> str:
        """Legacy identity: sha256 over scan.py bytes plus every standards file it loads.

        Kept so older manifests stay comparable with each other. It changes on any
        scan.py edit, comments included; compare prompts with fingerprints() instead.
        Same definition as before; after a run its source is the snapshot (see _src(), D-26).
        """
        h = hashlib.sha256()
        src = self._src()
        try:
            h.update(src.read_bytes())
        except OSError:
            return "unknown"
        c = self._consts(src)  # the standards that file names, as for the file bytes above
        names = list(c.get("STD_BASE", [])) + (list(c.get("STD_WEB", [])) if self.web else [])
        std_dir = src.parent / ".." / "docs"  # scan.py's STD_DIR
        for name in names:
            p = std_dir / name
            h.update(name.encode("utf-8"))
            h.update(p.read_bytes() if p.is_file() else b"<missing>")
        return h.hexdigest()

    def run(self, scope: Scope, ctx: RunContext) -> Findings:
        if not (ctx.env.get(KEY) or os.environ.get(KEY)):
            return self.failed("crashed", KEY + " not set (eval run also reads ATTACKTRAIL_" + KEY + " first)")
        if not self.scan_py.is_file():
            return self.failed("crashed", "scan.py not found at %s" % self.scan_py)
        try:
            self.snapshot(ctx.run_dir)
            return self._run(scope, ctx)
        except Exception as e:  # adapter must never raise for agent-side failures
            return self.failed("crashed", "%s: %s" % (type(e).__name__, e))

    def _run(self, scope: Scope, ctx: RunContext) -> Findings:
        raw_dir = Path(ctx.project_dir) / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        # scan.py writes straight into raw/ (--out). No guessing which scan-<commit8>.jsonl
        # in scan.py's own directory is ours: two projects on the same commit would share
        # that name, and a different git env could resolve another commit (D-19).
        out = raw_dir / "scan.jsonl"
        out.unlink(missing_ok=True)  # never read a leftover from an earlier run in this dir
        # D-28: the longest call follows max_tokens (scan.py's own default when --max-tokens is not given; a scan.py that
        # gives none, or a placeholder, falls back to the 64000 value)
        call_s = max_call_s(self.max_tokens())
        soft_margin = DEADLINE_MARGIN_S + call_s
        budget = max(ctx.timeout_s - max(soft_margin, 0.1 * ctx.timeout_s), ctx.timeout_s / 2)
        deadline = max(ctx.timeout_s - DEADLINE_MARGIN_S, ctx.timeout_s / 2)
        if ctx.timeout_s < 2 * soft_margin:  # below this the half-timeout floor binds (see the comment at MAX_CALL_S)
            print("attacktrail: warning: project timeout %gs is below %gs (2 x the %gs soft margin): the soft limit "
                  "does not leave room for a %gs call, so a call started near it can be cut at the hard deadline"
                  % (ctx.timeout_s, 2 * soft_margin, soft_margin, call_s), file=sys.stderr)
        argv = [self.python, str(self.scan_py), str(scope.repo_path),
                "--out", str(out), "--time-budget", "%d" % budget, "--deadline-s", "%d" % deadline]
        if self._model:
            argv += ["--model", self._model]
        if self._effort:
            argv += ["--effort", self._effort]
        if self._max_tokens:
            argv += ["--max-tokens", str(self._max_tokens)]
        if self.web:
            argv.append("--web")

        executor = self._executor
        if executor is None:
            from runner.executor import LocalExecutor
            executor = LocalExecutor()
        with tempfile.TemporaryDirectory(prefix="at-home-") as home:
            env = scoped_env(ctx, KEY)
            env.update({k: home for k in SDK_HOME_KEYS})
            res = executor.run(argv, cwd=self.scan_py.parent, env=env, timeout_s=ctx.timeout_s)

        (raw_dir / "scan.stdout.txt").write_text(res.stdout or "", encoding="utf-8")
        (raw_dir / "scan.stderr.txt").write_text(res.stderr or "", encoding="utf-8")

        # On timeout or a non-zero exit (scan.py exits 2 on 401/402/403/404 or after 8
        # consecutive API errors) the JSONL written so far stays in raw/ as diagnostic
        # evidence; only the project result is marked failed.
        kept = " (partial output kept in raw/)" if out.is_file() else ""
        if res.timed_out:
            return self.failed("timeout", "scan.py exceeded %ss%s" % (ctx.timeout_s, kept), res.seconds)
        if res.returncode == 1 and NO_FILES_MARK in (res.stderr or ""):  # D-27: tool limit, not a crash
            big = TOO_BIG_RE.search(res.stderr or "")  # D-28: --web, everything over the size cap
            return self.failed("no_files", "no files selected%s: %s" % (
                " (%s)" % too_big_reason(int(big.group(1))) if big else "", (res.stderr or "").strip()[-500:]),
                res.seconds, **NO_FILES_COVERAGE)
        if res.returncode != 0:
            return self.failed("crashed", "scan.py exit %s: %s%s"
                               % (res.returncode, (res.stderr or "")[-2000:], kept), res.seconds)
        if not out.is_file():
            return self.failed("crashed", "scan.py wrote no %s" % out.name, res.seconds)

        # scan.py skips a file it could not scan and logs it as ERROR. The old check looked
        # for "]  ERROR ", which never matches because the file name sits in between (D-18).
        progress = [m for m in map(PROGRESS.match, (res.stdout or "").splitlines()) if m]
        errors = [m for m in progress if m.group(4) == "ERROR"]
        total = int(progress[0].group(2)) if progress else 0
        if total and len(errors) == total:
            return self.failed("crashed", "every file failed inside scan.py; first: %s"
                               % errors[0].string.strip()[:500], res.seconds)
        run, findings, skipped = parse_scan_jsonl(out)
        # Coverage (D-20): a project with unscanned files stays ok and in every denominator
        # (policy unchanged, user decision pending), so the gap is recorded and shown instead.
        failed = [(m.group(3), m.group(5)) for m in errors if not m.group(5).startswith("skipped:")]
        not_sent = [m.group(5)[len("skipped: "):] for m in errors if m.group(5).startswith("skipped:")]
        too_big = run.get("skipped_large") or []  # --web only: over MAX_BYTES_WEB, never sent
        not_sent += ["too large for --web"] * len(too_big)
        cov, memo = coverage_meta(total + len(too_big), sum(m.group(4) == "후보" for m in progress),
                                  failed, not_sent) if progress else ({}, None)  # no log: unmeasured
        notes = [memo] if memo else []
        abandoned = sum(ABANDONED_MARK in reason for _, reason in failed)
        if abandoned:  # the abandoned calls were sent and billed but never read, so the token sums are low
            notes.append("%d call(s) still running at the hard deadline were abandoned: no response was read, "
                         "so their tokens are not in tokens_in/tokens_out (undercount)" % abandoned)
        cut = partial_rows(out)
        if cut:  # D-24: streams the deadline closed or that broke; their usage rows are what the stream had reported
            notes.append("%d stream(s) ended mid-way (deadline, stall or connection loss): their usage is what the "
                         "stream had reported at message_start, so tokens_out is a lower bound and tokens_in can leave out "
                         "the input of the other attempt (a fallback before the cut)" % cut)
        if skipped:
            notes.append("%d triage candidates dropped for empty why/poc_sketch" % skipped)
        tokens = sum_usage(out)
        fb = fallback_usage(out)  # files a fallback model answered (D-23); None: no usage rows, unmeasured
        return Findings(vulnerabilities=findings, meta=Meta(
            agent=self.name, agent_version=self.version(), model=self.model(),
            tokens_in=tokens[0] if tokens else None, tokens_out=tokens[1] if tokens else None,
            cost_usd=None,  # prices per token kind are not fixed yet (D-19)
            files_fallback=fb[0] if fb else None, fallback_models=fb[1] if fb else None,
            wall_seconds=res.seconds, status="ok", error="; ".join(notes) or None, **cov))
