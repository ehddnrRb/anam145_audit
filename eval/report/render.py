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

    b.append(("h2", "Per project"))
    per = scores.get("per_project") or {}
    by_id = {p.get("project_id"): p for p in manifest.get("projects") or []}
    prow = []
    for pid in sorted(set(per) | set(by_id)):
        s, m = per.get(pid, {}), by_id.get(pid, {})
        co = (m.get("checkout") or {}).get("status")
        status = (m.get("excluded_reason") or (co if co not in (None, "ok") else None)
                  or m.get("agent_status") or ("scored" if s else ""))
        prow.append([pid, fmt(s.get("recall_vs_dataset")), fmt(s.get("recall_with_partial_vs_dataset")),
                     fmt(s.get("high_recall")), fmt(s.get("precision")), fmt(s.get("precision_with_partial")),
                     fmt(m.get("cost_usd"), "usd", none=UNMEASURED), status])
    b.append(("table", ["Project", "Recall strict", "Recall lenient", "High Recall strict",
                        "Precision strict", "Precision lenient", "Cost", "Status"], prow))

    b.append(("h2", "Excluded / failed projects"))
    b.append(("p", manifest.get("denominators_note") or
              "Denominator rule: projects excluded by the dataset, failed checkout, or crashed/timed-out agents "
              "are excluded from every metric denominator."))
    ex = _excluded(manifest, scores)
    b.append(("table", ["Project", "Reason"], [list(x) for x in ex]) if ex else ("p", "없음"))

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
        ["prompt hash", str(agent.get("prompt_hash"))],
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
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#ddd;--alt:#f6f6f6;--red:#b00020}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e8e8;--muted:#9a9a9a;--line:#333;--alt:#1e1e1e;--red:#ff6b6b}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:16px}
table{border-collapse:collapse;width:100%;margin:8px 0 16px;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
tr:nth-child(even) td{background:var(--alt)}
pre{white-space:pre-wrap;background:var(--alt);padding:8px;border:1px solid var(--line)}
.banner{background:var(--red);color:#fff;font-weight:700;padding:12px;margin:8px 0}
h3{font-size:15px;margin:16px 0 4px}
"""


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
            out += ["<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>" for r in blk[2]]
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
