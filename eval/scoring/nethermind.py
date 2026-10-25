"""Scoring via Nethermind scoring_algo (pinned a62a15d). We never match or score ourselves.

What the scorer source guarantees (paths relative to site-packages/scoring_algo/):

(a) `python -m scoring_algo.cli evaluate` (cli.py:17-62) builds Settings() from env
    (settings.py:10-44, no prefix), resolves relative DATA_ROOT/OUTPUT_ROOT against the
    package dir (cli.py:28-36, so we always pass absolute paths), and for each name in
    REPOS_TO_RUN calls in-process core.evaluate.run_evaluation(...) (cli.py:50-59) with
    iterations=ITERATIONS=3 (settings.py:7). cli.py:22 also calls load_dotenv(), which
    can pick up a .env found by walking up from the package dir; it does not override
    variables we set explicitly. We set cwd to OUTPUT_ROOT so pydantic's env_file=".env"
    (settings.py:44) does not read a stray .env from the caller's cwd.

(b) Evaluated array shape (core/evaluate.py):
    - Truths are judged in truth order (evaluate.py:56) and one EvaluatedFinding is
      appended per truth (evaluate.py:107-122), EXCEPT when the judge returned no result
      at all for that truth (every LLM call failed, llm.py:74-76, or the scan had zero
      findings so no batch ran, batching.py:33): then the truth is skipped with
      `continue` (evaluate.py:85-87) and leaves NO entry.
    - post_process_partial_matches keeps order and length (evaluate.py:186-221).
    - FP entries are appended afterwards (evaluate.py:135-152), one per scan finding not
      referenced by any truth entry, skipping scan severities Info/Best Practices
      (evaluate.py:139). Their index_of_finding_from_junior_auditor is the position in
      the scan file's findings list (evaluate.py:135, storage.py:151-161).
    - An entry carries no truth identifier (types.py:47-62). So "entry k <-> truth k" holds
      only when the number of non-FP entries equals the number of truths. We map truths
      positionally only in that case ("mapping": "positional"). Otherwise FN entries are
      reported with only the fields the entry has ("mapping": "unverified") and
      per_project[id]["truths_unevaluated"] records how many truths got no entry.

(c) compute_repo_stats (generate_report.py:155-221):
    - matched = non-FP entries with is_match; partial = non-FP entries with
      is_partial_match and not is_match; fps = entries with is_fp (generate_report.py:127-139).
    - actual_findings = number of non-FP entries, all severities (generate_report.py:109-124).
      A truth skipped per (b) therefore drops out of the scorer's recall denominator.
      We keep the scorer's numbers as-is (R1) but headline recall_vs_dataset /
      high_recall over the DATASET truth count, so a skipped truth counts as a miss.
      scoring.truth_trace records, per truth, whether the scorer produced an entry.
    - scan_root: reads scan_root/<repo>_results.json and uses it only if it is a JSON
      LIST (generate_report.py:167-172) for scan_findings and Info/BP qa_findings. Our
      scan files are the {"project", "findings": [...]} dict shape (storage.py:149-161), so
      that branch is not taken: scan_findings = matched + partial + fps and qa = 0
      (generate_report.py:177-181), i.e. strict FP = partial + fps, lenient FP = fps.
      scan_severity_counts stays empty. We still pass scan_root as the contract says.
    - false_negatives = actual - matched (strict; partials count as FN),
      recall = matched/actual, recall_with_partial = (matched+partial)/actual
      (generate_report.py:55-102).
    - Severity: truth/scan severities go through storage.normalize_severity
      (storage.py:26-42, critical -> High) and evaluate.py:90-92 stamps the truth severity
      onto severity_from_truth, so High recall uses severity_from_truth == "High".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path

from scoring_algo.core.storage import (
    get_evaluation_path,
    get_scan_path,
    get_truth_path,
    normalize_severity,
)
from scoring_algo.generate_report import aggregate_overall, compute_repo_stats
from scoring_algo.settings import ITERATIONS, Settings

from scoring.truth_trace import trace_path

SCORER_COMMIT = "a62a15d1ea254158dd5cda4bea8ce8cf5791fb27"
EVAL_DIR = Path(__file__).resolve().parents[1]

DEFINITIONS = {
    "strict": "exact matches only (is_match). Partial matches count as misses for recall and as false positives for precision.",
    "lenient": "exact + partial matches (is_match or is_partial_match) count as hits.",
    "recall": "headline: hits / truths in the dataset file (all severities), i.e. a truth the scorer never judged counts as a miss. Primary metric. The scorer's own recall (hits / truths it judged) stays in per_project.recall.",
    "precision": "hits / non-Info agent findings, as computed by scoring_algo. Secondary metric (auditor time cost).",
    "high_recall": "hits among High truths / High truths in the dataset file (severity normalized by scoring_algo; critical folded into High; the SCABench curated set has 0 critical truths). Unjudged High truths count as misses. None only when a project has 0 High truths.",
    "info_findings": "agent findings with Info/Best Practices severity. The scorer excludes them from FP counting; listed separately.",
    "truths_unevaluated": "truths for which the scorer produced no entry (no findings left to compare, or every judge call failed). Counted as misses in the headline; absent from the scorer's own denominator.",
    "judge_mock": "if judge == mock, verdicts are content-independent and every number is meaningless.",
}


@dataclass
class ScoreResult:
    per_project: dict[str, dict]
    overall: dict
    fn: list[dict]
    fp: list[dict]
    info_findings: list[dict]
    judge: str
    scorer_version: str


def _venv_python() -> str:
    for p in (EVAL_DIR / ".venv" / "Scripts" / "python.exe", EVAL_DIR / ".venv" / "bin" / "python"):
        if p.exists():
            return str(p)
    return sys.executable


def _run_nethermind(data_root: Path, output_root: Path, project_ids, scan_source, model, batch_size, key):
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if k in os.environ}
    env.update(
        PYTHONIOENCODING="utf-8",
        OPENAI_API_KEY=key,
        MODEL=model,
        BATCH_SIZE=str(batch_size),
        SCAN_SOURCE=scan_source,
        DATA_ROOT=str(data_root),
        OUTPUT_ROOT=str(output_root),
        REPOS_TO_RUN=json.dumps(project_ids),
    )
    output_root.mkdir(parents=True, exist_ok=True)
    # = python -m scoring_algo.cli, plus an observation-only per-truth trace (scoring/truth_trace.py)
    argv = [_venv_python(), str(EVAL_DIR / "scoring" / "truth_trace.py"),
            "evaluate", "--no-telemetry", "--log-level", "INFO"]
    proc = subprocess.run(argv, cwd=output_root, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    (output_root / "scorer_stdout.log").write_text(proc.stdout, encoding="utf-8")
    (output_root / "scorer_stderr.log").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"scoring_algo exited {proc.returncode}: {proc.stderr[-2000:]}")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _items(data) -> list[dict]:
    if isinstance(data, list):
        return data
    return data.get("vulnerabilities") or data.get("findings") or []


def load_trace(pid: str, output_root: Path) -> list[bool] | None:
    p = trace_path(pid, output_root)
    return _load(p).get("truth_has_verdict") if p.exists() else None


def align_truths(judged: list[dict], n_truths: int, trace: list[bool] | None):
    """(entry-or-None per truth, mapping kind). None per truth = the scorer left no entry for it.
    Returns (None, "unverified") when entries cannot be placed on truths without guessing."""
    if trace is not None and len(trace) == n_truths and sum(trace) == len(judged):
        it = iter(judged)
        return [next(it) if v else None for v in trace], "traced"
    if len(judged) == n_truths:
        return list(judged), "positional"
    if not judged:  # nothing judged: every truth is unjudged, no placement needed
        return [None] * n_truths, "none_evaluated"
    return None, "unverified"


def _is_high(t: dict) -> bool:
    return normalize_severity(t.get("severity") or t.get("Severity")) == "High"


def evaluate(run_dir: Path, scan_source: str, project_ids: list[str], judge: str = "nethermind",
             model: str = "o4-mini", batch_size: int = 10, only_missing: bool = False) -> ScoreResult:
    """only_missing: judge only projects without scoring_output/<id>_results.json (rescore after a crash);
    stats are still computed for every project id."""
    run_dir = Path(run_dir).resolve()
    data_root = run_dir / "scoring_data"
    output_root = run_dir / "scoring_output"
    all_ids = project_ids
    if only_missing:
        project_ids = [p for p in project_ids if not (output_root / f"{p}_results.json").exists()]

    if judge == "nethermind":
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set: judge 'nethermind' needs it (use --judge mock for plumbing tests)")
        supported = Settings.model_fields["SUPPORTED_MODELS"].default
        if not any(model in m for m in supported.values()):
            raise ValueError(f"judge model {model!r} not supported by scoring_algo: {supported}")
        if project_ids:
            _run_nethermind(data_root, output_root, project_ids, scan_source, model, batch_size, key)
    elif judge == "mock":
        from scoring.mock_judge import run_mock
        model, batch_size = "mock", 10  # run_mock always uses batch 10
        if project_ids:
            run_mock(data_root, output_root, project_ids, scan_source)
    else:
        raise ValueError(f"unknown judge {judge!r}")

    per_project: dict[str, dict] = {}
    stats, fn, fp, info = [], [], [], []
    for pid in all_ids:
        eval_path = get_evaluation_path(pid, output_root)
        if not eval_path.exists():
            raise RuntimeError(f"scorer wrote no evaluation for {pid}: {eval_path}")
        entries = _load(eval_path)
        truths = _items(_load(get_truth_path(pid, data_root)))
        scan = _items(_load(get_scan_path(pid, data_root, scan_source)))

        rs = compute_repo_stats(eval_path, scan_root=data_root / scan_source)
        stats.append(rs)

        judged = [e for e in entries if e.get("is_fp") is not True]
        high = [e for e in judged if e.get("severity_from_truth") == "High"]
        hm = sum(1 for e in high if e.get("is_match") is True)
        hp = sum(1 for e in high if e.get("is_match") is not True and e.get("is_partial_match") is True)
        n = len(truths)
        n_high = sum(1 for t in truths if _is_high(t))
        aligned, mapping = align_truths(judged, n, load_trace(pid, output_root))
        per_project[pid] = {
            **rs.model_dump(),
            "recall_vs_dataset": rs.matched / n if n else None,
            "recall_with_partial_vs_dataset": (rs.matched + rs.partial) / n if n else None,
            "high_truths": n_high,
            "high_truths_evaluated": len(high),
            "high_matched": hm,
            "high_partial": hp,
            "high_recall": hm / n_high if n_high else None,
            "high_recall_with_partial": (hm + hp) / n_high if n_high else None,
            "truths_in_dataset": n,
            "truths_unevaluated": n - len(judged),
            "truth_mapping": mapping,
        }

        def fn_row(e, t, verdict):
            row = {"project_id": pid, "mapping": mapping, "verdict": verdict,
                   "severity_from_truth": e.get("severity_from_truth") if e else None,
                   "explanation": e.get("explanation") if e else "scorer produced no entry for this truth",
                   "finding_id": None, "title": None, "description": None, "severity": None}
            if t is not None:
                row.update(finding_id=t.get("finding_id"), title=t.get("title") or t.get("Issue"),
                           description=t.get("description") or t.get("Description"),
                           severity=t.get("severity") or t.get("Severity"))
            return row

        if aligned is None:  # cannot place entries on truths: list judged misses with entry fields only
            fn += [fn_row(e, None, "missed") for e in judged
                   if not (e.get("is_match") or e.get("is_partial_match"))]
        else:
            for t, e in zip(truths, aligned):
                if e is None:
                    fn.append(fn_row(None, t, "unevaluated"))
                elif not (e.get("is_match") or e.get("is_partial_match")):
                    fn.append(fn_row(e, t, "missed"))

        for e in entries:
            if e.get("is_fp") is not True:
                continue
            i = e.get("index_of_finding_from_junior_auditor")
            src = scan[i] if isinstance(i, int) and 0 <= i < len(scan) else {}
            fp.append({"project_id": pid, "index": i, "title": src.get("title"),
                       "file": src.get("file"),
                       "severity": e.get("severity_from_junior_auditor"),
                       "description": e.get("finding_description_from_junior_auditor")})

        for i, f in enumerate(scan):
            if normalize_severity(f.get("severity") or f.get("Severity")) in ("Info", "Best Practices"):
                info.append({"project_id": pid, "index": i, "title": f.get("title"),
                             "file": f.get("file"), "severity": f.get("severity"),
                             "description": f.get("description")})

    overall = aggregate_overall(stats).model_dump()
    tot = lambda k: sum(p[k] for p in per_project.values())
    nt, th, tm, tp_ = tot("truths_in_dataset"), tot("high_truths"), tot("high_matched"), tot("high_partial")
    overall.update(recall_vs_dataset=overall["matched"] / nt if nt else None,
                   recall_with_partial_vs_dataset=(overall["matched"] + overall["partial"]) / nt if nt else None,
                   truths_in_dataset=nt, high_truths=th, high_matched=tm, high_partial=tp_,
                   high_recall=tm / th if th else None,
                   high_recall_with_partial=(tm + tp_) / th if th else None,
                   truths_unevaluated=tot("truths_unevaluated"))

    result = ScoreResult(per_project, overall, fn, fp, info, judge, version("scoring-algo"))
    scores = {
        "judge": judge, "scorer_version": result.scorer_version, "scorer_commit": SCORER_COMMIT,
        "model": model, "batch_size": batch_size, "iterations": ITERATIONS,
        **{k: v for k, v in asdict(result).items() if k not in ("judge", "scorer_version")},
        "definitions": DEFINITIONS,
    }
    (run_dir / "scores.json").write_text(json.dumps(scores, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
