"""Compare two runs: headline/per-project deltas and per-truth regressions.

Per-truth status is read from the scorer's own evaluated entries
(scoring_output/<id>_results.json). scoring_algo/core/evaluate.py appends one
non-FP entry per truth in truth order, but skips a truth when the judge returns
no result. Entries are placed on truths by scoring.nethermind.align_truths: via the
per-truth trace, by count equality, or trivially when nothing was judged. A truth
the scorer left without an entry in a scored project counts as NOT matched. When
entries cannot be placed, the project is reported as unmappable, never guessed.
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, stdev

from report.render import HEADLINE, fmt, headline, judge_kind, load, split
from scoring.nethermind import align_truths, load_trace


class IncomparableRuns(ValueError):
    """Exactly one side used the mock judge. CLI must exit non-zero."""


def _reps(run_dir: Path) -> list[Path]:
    reps = sorted(p for p in run_dir.glob("rep-*") if (p / "scoring_output").is_dir())
    return reps or [run_dir]


def truth_status(run_dir: Path) -> tuple[dict, set[str], int]:
    """{(project_id, truth_key): {"title", "strict", "lenient"}} counts over reps; unmappable pids; n reps."""
    reps = _reps(Path(run_dir))
    out: dict = {}
    unmappable: set[str] = set()
    for rep in reps:
        for ev_path in sorted((rep / "scoring_output").glob("*_results.json")):
            pid = ev_path.name[: -len("_results.json")]
            truth_path = rep / "scoring_data" / "source_of_truth" / f"{pid}.json"
            if not truth_path.exists():
                unmappable.add(pid)
                continue
            truth = json.loads(truth_path.read_text(encoding="utf-8"))
            vulns = truth.get("vulnerabilities", []) if isinstance(truth, dict) else truth
            entries = [e for e in json.loads(ev_path.read_text(encoding="utf-8")) if e.get("is_fp") is not True]
            aligned, _ = align_truths(entries, len(vulns), load_trace(pid, rep / "scoring_output"))
            if aligned is None:
                unmappable.add(pid)
                continue
            for i, (t, e) in enumerate(zip(vulns, aligned)):
                key = (pid, str(t.get("finding_id") or f"#{i}"))
                s = out.setdefault(key, {"title": t.get("title") or t.get("Issue") or "", "strict": 0, "lenient": 0})
                e = e or {}  # no scorer entry in a scored project = not matched
                s["strict"] += bool(e.get("is_match"))
                s["lenient"] += bool(e.get("is_match") or e.get("is_partial_match"))
    return out, unmappable, len(reps)


def _comparability(ma: dict, sa: dict, mb: dict, sb: dict) -> list[str]:
    def get(m, s, *path, fallback=None):
        v = m
        for k in path:
            v = v.get(k) if isinstance(v, dict) else None
        return v if v is not None else (s.get(fallback) if fallback else None)

    checks = [
        ("dataset sha", ("dataset", "sha"), None),
        ("judge model", ("judge", "model"), "model"),
        ("scorer commit", ("judge", "scorer_commit"), "scorer_commit"),
        ("batch size", ("judge", "batch_size"), "batch_size"),
    ]
    warns = []
    for label, path, fb in checks:
        a, b = get(ma, sa, *path, fallback=fb), get(mb, sb, *path, fallback=fb)
        if a != b:
            warns.append(f"{label} differs: A={a} B={b}")
    return warns


AGENT_KEYS = ("model", "effort", "max_tokens", "call_shape")  # what the agent was told; prompt_sha256 cannot show these (D-28)


def _agent_diff(ma: dict, mb: dict) -> list[str]:
    """One warning per setting in AGENT_KEYS that differs between the two manifests' `agent` blocks.

    A repeat run's own manifest carries rep-1's agent block (runner._aggregate), so the aggregate manifest is read as it is. A
    side that never recorded a key (a manifest from before D-28 has no max_tokens) shows as 미기록: unknown is not equal. So a
    key neither side recorded is a warning too, in the short form "agent.<key>: 양쪽 미기록" (nothing shows the two were set alike)."""
    aa, ab = (m.get("agent") if isinstance(m.get("agent"), dict) else {} for m in (ma, mb))
    show = lambda v: "미기록" if v is None else v  # noqa: E731
    out = []
    for k in AGENT_KEYS:
        a, b = aa.get(k), ab.get(k)
        if a is None and b is None:
            out.append(f"agent.{k}: 양쪽 미기록")
        elif a != b:
            out.append(f"agent.{k} differs: A={show(a)} B={show(b)}")
    return out


def _rep_scores(run_dir: Path, scores: dict) -> list[dict]:
    reps = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(run_dir).glob("rep-*/scores.json"))]
    return reps or [scores]


def _no_files(run_dir: Path, manifest: dict) -> set[str]:
    """Projects the agent could not scan at all (agent_status no_files, D-27); every rep of a repeat run counts."""
    mans = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(run_dir).glob("rep-*/manifest.json"))]
    return {p.get("project_id") for m in mans or [manifest] for p in m.get("projects") or []
            if p.get("agent_status") == "no_files"}


def _scored(reps: list[dict]) -> set[str]:
    """Projects scored in every rep."""
    return set.intersection(*(set(s.get("per_project") or {}) for s in reps))


def _common_recall(reps: list[dict], pids: set[str]) -> dict[str, tuple]:
    """Recall over a fixed project set, from the scorer-derived per-project counts. (mean, stdev) over reps."""
    cols = {"Recall strict": ("matched",), "Recall lenient": ("matched", "partial"),
            "High Recall strict": ("high_matched",), "High Recall lenient": ("high_matched", "high_partial")}
    out = {}
    for label, num in cols.items():
        den = "high_truths" if label.startswith("High") else "truths_in_dataset"
        vals = []
        for s in reps:
            pp = [s["per_project"][p] for p in pids]
            if not pp or any(not isinstance(x.get(den), int) for x in pp):
                vals = None
                break
            d = sum(x[den] for x in pp)
            vals.append(sum(x[k] for x in pp for k in num) / d if d else None)
        if not vals or None in vals:
            out[label] = (None, None)
        else:
            out[label] = (mean(vals), stdev(vals) if len(vals) > 1 else None)
    return out


def _delta(a, b) -> str:
    """a, b: number, {"mean","stdev"}, or (value, stdev) from headline()."""
    av, as_ = a if isinstance(a, tuple) else split(a)
    bv, bs = b if isinstance(b, tuple) else split(b)
    if av is None or bv is None:
        return "n/a"
    d = bv - av
    s = f"{d * 100:+.1f}%p"
    noise = max([x for x in (as_, bs) if x is not None], default=None)
    if noise is not None and abs(d) < noise:
        s += " (within noise)"
    return s


def compare(run_a: Path, run_b: Path) -> str:
    run_a, run_b = Path(run_a), Path(run_b)
    ma, sa = load(run_a)
    mb, sb = load(run_b)
    ka, kb = judge_kind(ma, sa), judge_kind(mb, sb)
    if (ka == "mock") != (kb == "mock"):
        raise IncomparableRuns(f"refusing to compare mock judge with real judge (A={ka}, B={kb})")

    lines = [f"# Compare {run_a.name} -> {run_b.name}", ""]
    if ka == "mock":
        lines += ["> **MOCK JUDGE on both sides — numbers are meaningless (plumbing test)**", ""]
    warns = _comparability(ma, sa, mb, sb) + _agent_diff(ma, mb)
    ra, rb = _rep_scores(run_a, sa), _rep_scores(run_b, sb)
    pa_set, pb_set = _scored(ra), _scored(rb)
    common_pids = pa_set & pb_set
    if pa_set != pb_set:
        warns.append(f"scored project sets differ (only A: {sorted(pa_set - pb_set) or '-'}; "
                     f"only B: {sorted(pb_set - pa_set) or '-'}). Headline deltas average different projects; "
                     "use the common-projects table")
    nfa, nfb = _no_files(run_a, ma), _no_files(run_b, mb)
    if nfa != nfb:
        warns.append(f"no_files project sets differ (only A: {sorted(nfa - nfb) or '-'}; only B: {sorted(nfb - nfa) or '-'}). "
                     "The scanner found no file to scan there (a tool limit, not a crash). A no_files project is outside "
                     "that run's Recall denominator, so the two sides count different truths")
    for w in warns:
        lines.append(f"> **WARNING: {w}. Deltas may not be comparable.**")
    if warns:
        lines.append("")

    ha, hb = headline(ma, sa), headline(mb, sb)
    lines += ["## Headline", "", "| Metric | A | B | Delta |", "|---|---|---|---|"]
    for label, _, k in HEADLINE:
        a, b = ha[label], hb[label]
        lines.append(f"| {label} | {fmt(a[0], k, a[1])} | {fmt(b[0], k, b[1])} | {_delta(a, b)} |")
    lines.append("")

    ca, cb = _common_recall(ra, common_pids), _common_recall(rb, common_pids)
    lines += [f"## Headline, common projects only ({len(common_pids)} scored on both sides)", "",
              "| Metric | A | B | Delta |", "|---|---|---|---|"]
    for label in ca:
        a, b = ca[label], cb[label]
        lines.append(f"| {label} | {fmt(a[0], 'pct', a[1])} | {fmt(b[0], 'pct', b[1])} | {_delta(a, b)} |")
    lines.append("")

    pa, pb = sa.get("per_project") or {}, sb.get("per_project") or {}
    keys = [("recall", "Recall strict"), ("recall_with_partial", "Recall lenient"),
            ("precision", "Precision strict"), ("precision_with_partial", "Precision lenient")]
    lines += ["## Per project", "", "| Project | " + " | ".join(n for _, n in keys) + " |",
              "|---|" + "---|" * len(keys)]
    for pid in sorted(set(pa) | set(pb)):
        if pid not in pa or pid not in pb:
            lines.append(f"| {pid} | only in {'A' if pid in pa else 'B'} |" + " |" * (len(keys) - 1))
            continue
        lines.append(f"| {pid} | " + " | ".join(_delta(pa[pid].get(k), pb[pid].get(k)) for k, _ in keys) + " |")
    lines.append("")

    ta, ua, na = truth_status(run_a)
    tb, ub, nb = truth_status(run_b)
    major = lambda s, n: s * 2 > n  # matched in a majority of reps (n=1: matched)
    common = set(ta) & set(tb)
    regress = sorted(k for k in common if major(ta[k]["strict"], na) and not major(tb[k]["strict"], nb))
    caught = sorted(k for k in common if not major(ta[k]["strict"], na) and major(tb[k]["strict"], nb))

    def row(k):
        return (f"| {k[0]} | {k[1]} | {ta[k]['title']} | {ta[k]['strict']}/{na} strict, {ta[k]['lenient']}/{na} lenient"
                f" | {tb[k]['strict']}/{nb} strict, {tb[k]['lenient']}/{nb} lenient |")

    hdr = ["| Project | Truth | Title | A | B |", "|---|---|---|---|---|"]
    lines += [f"## REGRESSIONS ({len(regress)}) — matched (strict) in A, not in B", ""]
    lines += (hdr + [row(k) for k in regress]) if regress else ["없음"]
    lines += ["", f"## New catches ({len(caught)}) — matched (strict) in B, not in A", ""]
    lines += (hdr + [row(k) for k in caught]) if caught else ["없음"]
    lines.append("")
    if na > 1 or nb > 1:
        lines += [f"With repeats (A n={na}, B n={nb}) a truth counts as matched when matched in a majority of reps.", ""]
    only = (set(ta) ^ set(tb))
    if only:
        lines += [f"{len(only)} truths were evaluated on only one side and are not compared.", ""]
    if ua | ub:
        lines += [f"> **Per-truth mapping unavailable for: {', '.join(sorted(ua | ub))}** "
                  "(scorer entry count != truth count, or truth file missing). Regressions for these are not listed.", ""]

    md = "\n".join(lines)
    (run_b / f"compare-vs-{run_a.name}.md").write_text(md, encoding="utf-8")
    return md
