"""Render a run directory (manifest.json + scores.json) into report.html and report.md.

Numbers come only from scores.json (written by scoring.nethermind from the Nethermind
scorer's own RepoStats). This module formats; it never scores or matches.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from statistics import mean

MOCK_BANNER = "MOCK JUDGE — numbers are meaningless (plumbing test)"
UNMEASURED = "미측정"
NA = "해당 없음"

# (label, key in overall, kind). Order = report order. Recall first (primary).
HEADLINE = [
    # denominator = dataset truth count: a truth the scorer never judged is a miss (see scoring.nethermind)
    ("Recall (strict)", "recall_vs_dataset", "pct"),
    ("Recall (lenient)", "recall_with_partial_vs_dataset", "pct"),
    ("High Recall (strict)", "high_recall", "pct"),
    ("High Recall (lenient)", "high_recall_with_partial", "pct"),
    ("Precision (strict)", "precision", "pct"),
    ("Precision (lenient)", "precision_with_partial", "pct"),
]


def load(run_dir: Path) -> tuple[dict, dict]:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    scores = json.loads((run_dir / "scores.json").read_text(encoding="utf-8"))
    return manifest, scores


def judge_kind(manifest: dict, scores: dict) -> str | None:
    j = manifest.get("judge")
    if isinstance(j, dict) and j.get("kind"):
        return j["kind"]
    return scores.get("judge")


def split(v) -> tuple[float | None, float | None]:
    """A metric is a plain number, or {"mean", "stdev"} in a repeat aggregate."""
    if isinstance(v, dict):
        return v.get("mean"), v.get("stdev")
    return v, None


def _sum_high(per_project: dict) -> dict:
    """Overall High recall from the scorer-derived per-project counts (sum, not average)."""
    t = m = p = 0
    for s in per_project.values():
        vals = [s.get(k) for k in ("high_truths", "high_matched", "high_partial")]
        if not all(isinstance(x, int) for x in vals):
            return {}
        t, m, p = t + vals[0], m + vals[1], p + vals[2]
    if t == 0:
        return {"high_truths": 0, "high_recall": None, "high_recall_with_partial": None}
    return {"high_truths": t, "high_matched": m, "high_partial": p,
            "high_recall": m / t, "high_recall_with_partial": (m + p) / t}


def headline(manifest: dict, scores: dict) -> dict[str, tuple[float | None, float | None]]:
    """label -> (value, stdev). Shared with compare.py."""
    # repeat runs (runner._aggregate) keep {metric: {mean, stdev, n, values}} under "aggregate"
    agg = scores.get("aggregate")
    overall = dict(scores.get("overall") or agg or {})
    if "high_recall" not in overall:
        overall.update(_sum_high(scores.get("per_project") or {}))
    out = {label: split(overall.get(key)) for label, key, _ in HEADLINE}
    out["High truths (denominator)"] = split(overall.get("high_truths"))
    out["Truths not judged by scorer"] = split(overall.get("truths_unevaluated"))
    if agg is not None:
        out["Cost avg USD / project"] = split(agg.get("cost_usd_per_project"))
        out["Wall seconds avg / project"] = split(agg.get("wall_seconds_per_project"))
    else:
        out["Cost avg USD / project"] = _avg(manifest, "cost_usd")
        out["Wall seconds avg / project"] = _avg(manifest, "wall_seconds")
    return out


def _avg(manifest: dict, key: str) -> tuple[float | None, float | None]:
    vals = [p.get(key) for p in manifest.get("projects") or [] if p.get("agent_status") == "ok"]
    measured = [v for v in vals if isinstance(v, (int, float))]
    # ponytail: a partially measured average would mislead; any missing value -> 미측정
    if not vals or len(measured) != len(vals):
        return None, None
    return mean(measured), None


def fmt(v, kind: str = "pct", stdev=None, none: str = NA) -> str:
    if isinstance(v, dict):
        v, stdev = split(v)
    if v is None:
        return none
    if kind == "pct":
        s = f"{v * 100:.1f}%"
        return s + (f" ± {stdev * 100:.1f}%p" if stdev is not None else "")
    if kind == "usd":
        s = f"${v:.4f}"
        return s + (f" ± ${stdev:.4f}" if stdev is not None else "")
    if kind == "sec":
        return f"{v:.1f}s" + (f" ± {stdev:.1f}s" if stdev is not None else "")
    return str(v) + (f" ± {stdev}" if stdev is not None else "")


def _excluded(manifest: dict, scores: dict) -> list[tuple[str, str]]:
    scored = set((scores.get("per_project") or {}).keys())
    out = []
    for p in manifest.get("projects") or []:
        pid = p.get("project_id")
        if p.get("agent_status") == "no_files":  # a tool limit with its own section (D-27), not a failure
            continue
        co = p.get("checkout") or {}
        co_err = co.get("error") if co.get("status") not in (None, "ok") else None
        if p.get("excluded_reason"):
            reason = p["excluded_reason"] + (f": {co_err}" if co_err else "")
        elif co_err or co.get("status") not in (None, "ok"):
            reason = f"checkout {co.get('status')}" + (f": {co_err}" if co_err else "")
        elif p.get("agent_status") not in (None, "ok"):
            reason = f"agent {p['agent_status']}" + (f": {p['agent_error']}" if p.get("agent_error") else "")
        elif scored and pid not in scored:
            reason = "not scored"
        else:
            continue
        out.append((pid, reason))
    return out


def _no_files(manifest: dict) -> tuple[str | None, list[tuple]]:
    """(summary line, [(project, truths, reason)]) for projects the agent could not scan at all (no_files, D-27).

    The projects and reasons come from projects[]; the truth counts and the line from manifest["unsupported"], which the
    runner writes (a manifest without it gives truths None and no line)."""
    u = manifest.get("unsupported") or {}
    truths = {r.get("project_id"): r.get("truths") for r in u.get("projects") or []}
    rows = [(p.get("project_id"), truths.get(p.get("project_id")), p.get("agent_error") or "사유 없음")
            for p in manifest.get("projects") or [] if p.get("agent_status") == "no_files"]
    return u.get("line"), rows


def coverage(p: dict) -> str:
    """"scanned/total" files of a project, e.g. "53/53" or "40/53". 미측정 when the agent did not record it
    (baseline, runs from before files_* existed). SCABench truth has no file location, so a truth that sat in
    an unscanned file still counts as missed in Recall; this column is where that shows."""
    s, t = p.get("files_scanned"), p.get("files_total")
    return f"{s}/{t}" if isinstance(s, int) and isinstance(t, int) else UNMEASURED


def _partial(manifest: dict) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """(partial, notes) for the projects the agent finished as ok.

    partial: (project, memo) for projects with files left unscanned: files_scanned < files_total, or
    files_failed / files_skipped above 0. The memo is the agent's note, or "coverage s/t" without one.
    Also a memo with coverage 미측정 (older runs, agents that do not record it, e.g. an old "N of M files
    failed" note): the report cannot tell the scan was complete, so it is not listed as "not partial" (D-22).
    They stay in every metric denominator; this only makes the partial scan visible.
    notes: (project, coverage, memo) for ok projects whose coverage is measured and complete but that carry a
    memo, e.g. "3 triage candidates dropped for empty why/poc_sketch". Information, not a failure.
    """
    part, notes = [], []
    for p in manifest.get("projects") or []:
        if p.get("agent_status") != "ok":
            continue
        s, t = p.get("files_scanned"), p.get("files_total")
        measured = isinstance(s, int) and isinstance(t, int)
        count = lambda k: p.get(k) if isinstance(p.get(k), int) else 0
        if (measured and s < t) or count("files_failed") > 0 or count("files_skipped") > 0:
            part.append((p.get("project_id"), p.get("agent_error") or f"coverage {coverage(p)}"))
        elif p.get("agent_error") and not measured:
            part.append((p.get("project_id"), f"[coverage {UNMEASURED}] {p['agent_error']}"))
        elif p.get("agent_error"):
            notes.append((p.get("project_id"), coverage(p), p["agent_error"]))
    return part, notes


def fallback_text(p: dict) -> str:
    """"폴백 응답 12 파일 (모델 claude-sonnet-5)" for a project record with files_fallback > 0 (D-23)."""
    return f"폴백 응답 {p['files_fallback']} 파일 (모델 {', '.join(p.get('fallback_models') or []) or '알 수 없음'})"


def _fallback(manifest: dict) -> list[tuple[str, str]] | None:
    """(project, text) for projects where a server-side fallback model answered some files.

    None when no project recorded the count (the CLI adapter, baseline, runs from before D-23): not measured,
    which is different from 0. Which model answered matters: those files were not scored from the requested model.
    """
    ps = [p for p in manifest.get("projects") or [] if isinstance(p.get("files_fallback"), int)]
    if not ps:
        return None
    return [(p.get("project_id"), fallback_text(p)) for p in ps if p["files_fallback"] > 0]


def judge_incomplete_lines(manifest: dict) -> list[str]:
    """"채점 불완전: <project> 미평가 정답 N건(채점 로그 확인)" per project the scorer left truths unevaluated for (D-28).

    The runner writes manifest["judge_incomplete_projects"] when scores.json has truths_unevaluated > 0 for a project whose agent
    gave at least one finding. A repeat aggregate carries the rep each came from ("run"), shown after "채점 로그 확인". The scorer
    also leaves truths unevaluated when the findings were all used up by matches, so a line is a reason to look at
    scorer_stdout.log, not proof of failed judge calls (and not a call to rescore before looking).
    """
    out = []
    for r in manifest.get("judge_incomplete_projects") or []:
        run = f", {r['run']}" if r.get("run") else ""
        out.append(f"채점 불완전: {r.get('project_id')} 미평가 정답 {r.get('truths_unevaluated')}건(채점 로그 확인{run})")
    return out


def _oneline(s, n: int = 200) -> str:
    return " ".join(str(s).split())[:n]


def run_summary(manifest: dict) -> list[str]:
    """Console lines printed at the end of `eval run` (D-23). The report keeps its own sections.

    Partial failures follow the report's rule (_partial): per project the failed/total file counts, skipped
    files, and the coverage, or 커버리지 미측정 when the agent did not record it. Then the projects whose agent
    did not finish ok (crashed, e.g. after a fatal API error or the consecutive-error breaker; timeout), one line
    with the reason each. Then, only when there are some, the projects with no scannable file (no_files, D-27) with
    their truth counts: a tool limit, so not on the crashed lines. Then, only when there are some, one line per project the
    scorer left truths unevaluated for (채점 불완전, D-28). Then fallback responses, only for agents that report them.
    """
    projects = {p.get("project_id"): p for p in manifest.get("projects") or []}
    count = lambda p, k: p.get(k) if isinstance(p.get(k), int) else None
    part, _ = _partial(manifest)
    out = [f"부분 실패 프로젝트 {len(part)}개" if part else "부분 실패 프로젝트: 없음"]
    for pid, _memo in part:
        p = projects[pid]
        bits = []
        if count(p, "files_total") is not None:
            bits.append(f"실패 {count(p, 'files_failed') or 0}/{p['files_total']} 파일")
        if count(p, "files_skipped"):
            bits.append(f"건너뜀 {p['files_skipped']}")
        bits.append(f"커버리지 {coverage(p)}")
        out.append(f"  {pid}: " + ", ".join(bits))
    bad = [p for p in projects.values() if p.get("agent_status") not in (None, "ok", "no_files")]
    kinds = "/".join(sorted({p["agent_status"] for p in bad}))
    out.append(f"{kinds} 프로젝트 {len(bad)}개" if bad else "crashed 프로젝트: 없음")
    out += [f"  {p.get('project_id')} [{p['agent_status']}]: {_oneline(p.get('agent_error') or '사유 없음')}"
            for p in bad]
    _, nf = _no_files(manifest)
    if nf:
        out.append("지원 밖 프로젝트: " + ", ".join(
            f"{pid} (정답 {t if isinstance(t, int) else UNMEASURED})" for pid, t, _ in nf))
    out += judge_incomplete_lines(manifest)   # D-28: only when the scorer left truths unevaluated
    fb = _fallback(manifest)
    if fb is not None:
        out.append(f"폴백 응답 프로젝트 {len(fb)}개" if fb else "폴백 응답: 없음")
        out += [f"  {pid}: {text}" for pid, text in fb]
    return out


def _fn_fp(run_dir: Path, scores: dict, key: str) -> list[dict]:
    if scores.get(key) is not None:
        return scores[key]
    # repeat aggregate without merged lists: collect from each rep, tagged
    out = []
    for rep in sorted(Path(run_dir).glob("rep-*/scores.json")):
        for item in json.loads(rep.read_text(encoding="utf-8")).get(key) or []:
            out.append({"rep": rep.parent.name, **item})
    return out


def blocks(run_dir: Path) -> tuple[str, list[tuple]]:
    """Report as a neutral list of blocks; emitted as both md and html."""
    manifest, scores = load(run_dir)
    kind = judge_kind(manifest, scores)
    agent = manifest.get("agent") or {}
    judge = manifest.get("judge") or {}
    ds = manifest.get("dataset") or {}
    rep = manifest.get("repeat") or {}
    title = f"Eval report — {agent.get('name', '?')} on {ds.get('name', '?')} ({manifest.get('run_id', Path(run_dir).name)})"
    b: list[tuple] = []
    if kind == "mock":
        b.append(("banner", MOCK_BANNER))
    if manifest.get("synthetic_fixture") or scores.get("synthetic_fixture"):
        b.append(("banner", "SYNTHETIC FIXTURE — test data, not a measurement"))

    h = headline(manifest, scores)
    b.append(("h2", "Headline"))
    if rep.get("of", 1) and rep.get("of", 1) > 1:
        b.append(("p", f"repeat {rep.get('of')}회: mean ± sample stdev"))
    rows = []
    for label, _, k in HEADLINE:
        role = "secondary (auditor time cost)" if label.startswith("Precision") else "primary"
        rows.append([label, fmt(*h[label][:1], kind=k, stdev=h[label][1]), role])
    hd, hs = h["High truths (denominator)"]
    rows.insert(4, ["High truths (denominator)", fmt(hd, "int", hs), "High Recall 분모"])
    ud, us = h["Truths not judged by scorer"]
    rows.insert(2, ["Truths not judged by scorer", fmt(ud, "int", us),
                    "Recall 에서 미탐으로 계산됨" + (" — WARNING: 채점기가 판정하지 않은 truth 있음" if ud else "")])
    rows.append(["Cost avg USD / project", fmt(h["Cost avg USD / project"][0], "usd", none=UNMEASURED), "reference"])
    rows.append(["Wall seconds avg / project", fmt(h["Wall seconds avg / project"][0], "sec", none=UNMEASURED), "reference"])
    b.append(("table", ["Metric", "Value", "Role"], rows))

    inc = judge_incomplete_lines(manifest)   # D-28: right under the headline, so the numbers are not read without it
    if inc:
        b += [("p", x) for x in inc]
        b.append(("p", "채점기가 판정하지 않은 정답이 있어 위 Recall 은 미탐으로 계산된 만큼 낮다(judge 호출 실패로 생길 수 있다. "
                       "오류는 scoring_output/scorer_stdout.log 에 있다). 지적을 매칭으로 다 쓴 프로젝트도 이렇게 보인다"
                       "(에이전트 지적이 0건인 프로젝트는 표시하지 않는다). 복구: README 의 '채점 불완전 시'."))

    line, nf = _no_files(manifest)
    if nf:
        b.append(("h2", "Unsupported projects (지원 밖 프로젝트)"))
        b.append(("p", "스캐너가 지원하는 언어의 파일을 하나도 보내지 못한 프로젝트(agent_status no_files. 지원하는 확장자의 파일이 "
                       "없거나, --web 에서 전부 크기 상한을 넘었다). 크래시가 아니라 도구의 한계다. 지표 분모에 들어가지 않으므로 "
                       "위 Headline 은 지원 프로젝트만의 값이다. 이 프로젝트의 정답은 아래에 따로 센다. 전체 포함 Recall 의 분모는 "
                       "채점된 프로젝트 정답과 지원 밖 정답의 합이고, 체크아웃 실패·pinned 누락·crashed·timeout 프로젝트의 정답은 "
                       "들어가지 않는다."))
        if line:
            b.append(("p", line))
        b.append(("table", ["Project", "Truths (정답 수)", "Reason"],
                  [[pid, t if isinstance(t, int) else UNMEASURED, reason] for pid, t, reason in nf]))

    b.append(("h2", "Per project"))
    per = scores.get("per_project") or {}
    by_id = {p.get("project_id"): p for p in manifest.get("projects") or []}
    prow = []
    for pid in sorted(set(per) | set(by_id)):
        s, m = per.get(pid, {}), by_id.get(pid, {})
        co = (m.get("checkout") or {}).get("status")
        status = (m.get("excluded_reason") or (co if co not in (None, "ok") else None)
                  or m.get("agent_status") or ("scored" if s else ""))
        if status == "no_files":  # not "crashed": the scanner had nothing to scan (D-27)
            status = "no_files (지원 밖)"
        prow.append([pid, fmt(s.get("recall_vs_dataset")), fmt(s.get("recall_with_partial_vs_dataset")),
                     fmt(s.get("high_recall")), fmt(s.get("precision")), fmt(s.get("precision_with_partial")),
                     fmt(m.get("cost_usd"), "usd", none=UNMEASURED), coverage(m), status])
    b.append(("table", ["Project", "Recall strict", "Recall lenient", "High Recall strict",
                        "Precision strict", "Precision lenient", "Cost", "Coverage (files scanned/total)",
                        "Status"], prow))

    b.append(("h2", "Excluded / failed projects"))
    b.append(("p", manifest.get("denominators_note") or
              "Denominator rule: projects excluded by the dataset, failed checkout, or crashed/timed-out agents "
              "are excluded from every metric denominator. no_files projects are too, and are listed apart."))
    ex = _excluded(manifest, scores)
    b.append(("table", ["Project", "Reason"], [list(x) for x in ex]) if ex else ("p", "없음"))

    b.append(("h2", "Partial failures (부분 실패)"))
    b.append(("p", "agent_status 가 ok 라 모든 지표 분모에 들어갔지만 스캔되지 않은 파일이 있는 프로젝트(스캔한 파일 수가 "
                   "전체보다 적거나 실패·건너뜀 파일이 있다). SCABench truth 에는 파일 위치가 없어 그 파일에 있던 "
                   "truth 도 Recall 에서 미탐으로 계산된다. 메모에 실패한 파일(최대 5개)이 있다. 커버리지를 기록하지 "
                   "않은 프로젝트의 메모도 온전한 스캔인지 알 수 없어 여기 [coverage 미측정] 으로 올린다."))
    part, notes = _partial(manifest)
    b.append(("table", ["Project", "Memo"], [list(x) for x in part]) if part else ("p", "없음"))
    b.append(("h3", "Agent notes (정보, 부분 실패 아님)"))
    b.append(("p", "모든 파일을 스캔한 것이 기록으로 확인된 ok 프로젝트에 에이전트가 남긴 메모. 위 부분 실패가 아니다."))
    b.append(("table", ["Project", "Coverage (files scanned/total)", "Memo"], [list(x) for x in notes])
             if notes else ("p", "없음"))

    b.append(("h2", "Fallback responses (폴백 응답)"))
    b.append(("p", "서버 측 폴백이 거절된 요청을 다른 모델로 다시 보내 그 모델이 응답을 낸 파일 수와 모델. 응답을 낸 것까지만 "
                   "뜻한다: 그 응답이 파싱에 실패하거나 max_tokens 로 잘려 ERROR 가 된 파일도 센다. 후보를 낸 파일의 후보는 "
                   "요청한 모델(agent model)이 아니라 이 모델이 낸 것이다. 문서의 sticky 라우팅(한 번 폴백한 요청 계열은 "
                   "약 1시간 폴백 모델이 바로 받는다)이 이 스캔에 걸리는지는 실측 전이고, 걸리면 이 수는 거절 횟수보다 "
                   "크다. 폴백 모델까지 거절한 파일은 ERROR 이고 응답을 낸 모델이 없어 여기 세지 않는다."))
    fb = _fallback(manifest)
    b.append(("p", UNMEASURED) if fb is None else
             ("table", ["Project", "Fallback"], [list(x) for x in fb]) if fb else ("p", "없음"))

    fn = _fn_fp(run_dir, scores, "fn")
    b.append(("h2", f"False negatives ({len(fn)}) — missed truths"))
    b.append(("p", "SCABench truth has no file/line location; only title, severity and description."))
    for i, x in enumerate(fn, 1):
        head = " · ".join(str(v) for v in (x.get("rep"), x.get("project_id"), x.get("finding_id"),
                                            x.get("severity") or x.get("severity_from_truth")) if v)
        b.append(("h3", f"FN {i}. {x.get('title') or '(no title)'}"))
        b.append(("p", head))
        b.append(("pre", x.get("description") or x.get("explanation") or ""))

    fp = _fn_fp(run_dir, scores, "fp")
    b.append(("h2", f"False positives ({len(fp)})"))
    for i, x in enumerate(fp, 1):
        head = " · ".join(str(v) for v in (x.get("rep"), x.get("project_id"),
                                            x.get("severity_from_junior_auditor") or x.get("severity"),
                                            f"scan #{x['index_of_finding_from_junior_auditor'] + 1}"
                                            if isinstance(x.get("index_of_finding_from_junior_auditor"), int) else None) if v)
        b.append(("h3", f"FP {i}. {x.get('title') or head}"))
        b.append(("p", head))
        b.append(("pre", x.get("finding_description_from_junior_auditor") or x.get("description") or ""))

    info = _fn_fp(run_dir, scores, "info_findings")
    b.append(("h2", "Info findings"))
    b.append(("p", f"{len(info)} agent findings with severity info (excluded from FP counting by the scorer)."))

    b.append(("h2", "Definitions"))
    d = scores.get("definitions") or {}
    if isinstance(d, dict):
        b.append(("table", ["Term", "Definition"], [[k, str(v)] for k, v in d.items()]))
    else:
        b.append(("p", str(d)))

    b.append(("h2", "Manifest"))
    b.append(("table", ["Field", "Value"], [
        ["dataset", f"{ds.get('name')} @ {ds.get('sha')}"],
        ["holdout_used", str(manifest.get("holdout_used"))],
        ["agent version", str(agent.get("version"))],
        ["agent model", str(agent.get("model"))],
        # D-14: compare runs by prompt_sha256; prompt_hash is the legacy file-bytes hash
        *[[label, str(agent[k])] for label, k in (
            ("prompt sha256 (system prompt text)", "prompt_sha256"),
            ("call shape", "call_shape"),
            ("effort (output_config.effort)", "effort"),
            ("max_tokens (요청 출력 상한)", "max_tokens"),
            ("project timeout (s)", "timeout_s"),
            ("scan.py sha256", "scan_py_sha256"),
            ("agent snapshot (run dir 기준)", "snapshot_dir")) if k in agent],
        ["prompt hash (legacy, file bytes)" if "prompt_sha256" in agent else "prompt hash",
         str(agent.get("prompt_hash"))],
        ["judge", str(kind)],
        ["judge model", str(judge.get("model") or scores.get("model"))],
        ["batch size", str(judge.get("batch_size") or scores.get("batch_size"))],
        ["scorer commit", str(judge.get("scorer_commit") or scores.get("scorer_commit"))],
        ["started / finished", f"{manifest.get('started_at')} / {manifest.get('finished_at')}"],
    ]))
    return title, b


def to_md(title: str, b: list[tuple]) -> str:
    out = [f"# {title}", ""]
    cell = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
    for blk in b:
        t = blk[0]
        if t == "banner":
            out += [f"> **{blk[1]}**", ""]
        elif t in ("h2", "h3"):
            out += [("## " if t == "h2" else "### ") + blk[1], ""]
        elif t == "p":
            out += [blk[1], ""]
        elif t == "pre":
            out += ["```", blk[1].replace("```", "'''"), "```", ""]
        elif t == "table":
            out.append("| " + " | ".join(map(cell, blk[1])) + " |")
            out.append("|" + "---|" * len(blk[1]))
            out += ["| " + " | ".join(map(cell, r)) + " |" for r in blk[2]]
            out.append("")
    return "\n".join(out)


CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#ddd;--alt:#f6f6f6;--red:#b00020;--amber:#a85d00}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e8e8;--muted:#9a9a9a;--line:#333;--alt:#1e1e1e;--red:#ff6b6b;--amber:#ffb74d}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:16px}
table{border-collapse:collapse;width:100%;margin:8px 0 16px;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
tr:nth-child(even) td{background:var(--alt)}
pre{white-space:pre-wrap;background:var(--alt);padding:8px;border:1px solid var(--line)}
.banner{background:var(--red);color:#fff;font-weight:700;padding:12px;margin:8px 0}
h3{font-size:15px;margin:16px 0 4px}
td.nofiles{color:var(--amber);font-style:italic}
td.failed{color:var(--red);font-weight:700}
"""


def _td_class(c) -> str:
    """Class of any table cell, by its text (D-27): a cell starting with no_files (a tool limit) reads amber and italic, a cell
    that is exactly crashed, timeout or build_failed red, so the two differ. Every cell of every table goes through this; only
    the Status column holds such text in practice (a project id or a memo that is exactly "crashed" would be colored too)."""
    s = str(c)
    if s.startswith("no_files"):
        return " class='nofiles'"
    return " class='failed'" if s in ("crashed", "timeout", "build_failed") else ""


def to_html(title: str, b: list[tuple]) -> str:
    e = html.escape
    out = [f"<!doctype html><html><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{e(title)}</title><style>{CSS}</style></head><body><h1>{e(title)}</h1>"]
    for blk in b:
        t = blk[0]
        if t == "banner":
            out.append(f"<div class='banner'>{e(blk[1])}</div>")
        elif t in ("h2", "h3", "p"):
            out.append(f"<{t}>{e(blk[1])}</{t}>")
        elif t == "pre":
            out.append(f"<pre>{e(blk[1])}</pre>")
        elif t == "table":
            out.append("<table><tr>" + "".join(f"<th>{e(str(c))}</th>" for c in blk[1]) + "</tr>")
            out += ["<tr>" + "".join(f"<td{_td_class(c)}>{e(str(c))}</td>" for c in r) + "</tr>" for r in blk[2]]
            out.append("</table>")
    out.append("</body></html>")
    return "\n".join(out)


def render(run_dir: Path) -> tuple[Path, Path]:
    run_dir = Path(run_dir)
    title, b = blocks(run_dir)
    h, m = run_dir / "report.html", run_dir / "report.md"
    h.write_text(to_html(title, b), encoding="utf-8")
    m.write_text(to_md(title, b), encoding="utf-8")
    return h, m
