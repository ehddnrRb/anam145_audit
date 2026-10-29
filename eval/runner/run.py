"""End-to-end run: dataset -> checkout -> agent -> scorer files -> Nethermind scoring -> manifest -> report."""
from __future__ import annotations

import json
import os
import statistics
import sys
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from runner import budget
from runner.checkout import checkout_project, validate_name
from runner.dataset import EVAL_ROOT, excluded_reason, load_dataset
from runner.manifest import SCORER_COMMIT, build_manifest, judge_block, scorer_version, write_manifest
from scoring.nethermind import DEFINITIONS, evaluate

SOURCES_ROOT = EVAL_ROOT / "datasets" / "sources"
SUITES_DIR = EVAL_ROOT / "suites"
BATCH_SIZE = 10
AGENT_KEY = {"attacktrail": "ANTHROPIC_API_KEY", "baseline": "OPENAI_API_KEY"}
# The harness reads this user-level variable first (D-24). Claude Code reads only ANTHROPIC_API_KEY and would bill
# that key, so the Anthropic key can be stored under the alias instead. The child process still sees the name scan.py
# and the SDK read (ANTHROPIC_API_KEY); the alias itself is never passed on.
KEY_ALIAS = {"ANTHROPIC_API_KEY": "ATTACKTRAIL_ANTHROPIC_API_KEY"}
MINIMAL_OS_ENV = ("PATH", "SYSTEMROOT", "TEMP", "TMP")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonable(o):
    if is_dataclass(o):
        o = asdict(o)
    return json.loads(json.dumps(o, default=str))


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(obj), indent=2, ensure_ascii=False), encoding="utf-8")


def agent_env(agent_name: str) -> dict[str, str]:
    """The ONLY env the agent subprocess sees: minimal OS env + that agent's own API key."""
    env = {k: os.environ[k] for k in MINIMAL_OS_ENV if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    key = AGENT_KEY.get(agent_name)
    value = key and (os.environ.get(KEY_ALIAS.get(key, "")) or os.environ.get(key))
    if value:
        env[key] = value
    return env


def select_projects(items: list[dict], projects: list[str] | None, suite: str | None) -> list[dict]:
    by_id = {p["project_id"]: p for p in items}
    if projects:
        ids = list(projects)
    elif suite:
        path = SUITES_DIR / f"{validate_name(suite)}.txt"
        lines = path.read_text(encoding="utf-8").splitlines()
        ids = [ln.split("#", 1)[0].strip() for ln in lines]
        ids = [i for i in ids if i]
    else:
        return list(items)
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ValueError(f"unknown project ids: {unknown}")
    return [by_id[i] for i in dict.fromkeys(ids)]


COVERAGE_KEYS = ("files_total", "files_scanned", "files_failed", "files_skipped")
# Optional Meta fields copied into manifest projects[]: the coverage counts plus the server-side fallback
# counts (D-23), which ride the same path.
META_KEYS = COVERAGE_KEYS + ("files_fallback", "fallback_models")


def _blank(pid: str) -> dict:
    return {"project_id": pid, "excluded_reason": None, "checkout": None, "agent_status": None,
            "agent_error": None, "wall_seconds": None, "tokens_in": None, "tokens_out": None,
            "cost_usd": None, "n_findings": None, **dict.fromkeys(META_KEYS)}


def _run_project(project: dict, agent, agent_name: str, run_dir: Path, timeout_s: int) -> dict:
    from adapters.base import Findings, RunContext, Scope
    from adapters.convert import to_scorer

    pid = validate_name(project["project_id"])
    rec = _blank(pid)
    try:
        pdir = run_dir / "projects" / pid
        pdir.mkdir(parents=True, exist_ok=True)
        co = checkout_project(project, SOURCES_ROOT)
        rec["checkout"] = _jsonable(co)
        _write_json(pdir / "checkout.json", co)
        if co.status != "ok":
            rec["excluded_reason"] = co.status
            return rec
        ctx = RunContext(run_dir=run_dir, project_dir=pdir, timeout_s=timeout_s, env=agent_env(agent_name))
        findings = agent.run(Scope(project_id=pid, repo_path=co.path), ctx)
        findings = Findings.model_validate(findings.model_dump() if hasattr(findings, "model_dump") else findings)
        (pdir / "findings.json").write_text(findings.model_dump_json(indent=2), encoding="utf-8")
        m = findings.meta
        rec.update(agent_status=m.status, agent_error=m.error, wall_seconds=m.wall_seconds, tokens_in=m.tokens_in,
                   tokens_out=m.tokens_out, cost_usd=m.cost_usd, n_findings=len(findings.vulnerabilities),
                   **{k: getattr(m, k) for k in META_KEYS})
        if m.status == "ok":
            data = run_dir / "scoring_data"
            _write_json(data / agent_name / f"{pid}_results.json", to_scorer(findings, pid))
            _write_json(data / "source_of_truth" / f"{pid}.json", project)
    except Exception as e:  # one project's failure never kills the run
        # AGENT_STATUS has no 'harness_error'; a harness-side failure is recorded as crashed with a 'harness:' prefix
        rec["agent_status"] = "crashed"
        rec["agent_error"] = f"harness: {type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}"
    return rec


def _single(run_dir: Path, agent, agent_name: str, info: dict, dataset: str, selected: list[dict], jobs: int,
            timeout_s: int, judge: str, judge_model: str, rep: tuple[int, int]) -> Path:
    if (run_dir / "manifest.json").exists():
        raise FileExistsError(f"{run_dir} already holds a run; choose a new --out")
    run_dir.mkdir(parents=True, exist_ok=True)
    started = _now()
    records: dict[str, dict] = {}
    todo = []
    for p in selected:
        reason = excluded_reason(p)
        if reason:
            records[p["project_id"]] = _blank(p["project_id"]) | {"excluded_reason": reason}
        else:
            todo.append(p)
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for rec in pool.map(lambda p: _run_project(p, agent, agent_name, run_dir, timeout_s), todo):
            records[rec["project_id"]] = rec
    try:
        return _score_and_finish(run_dir, agent, agent_name, info, dataset, selected, records, judge, judge_model,
                                 rep, started, timeout_s=timeout_s)
    finally:
        # D-34: the run's API spend goes on the ledger even when scoring raised (the manifest is written in that
        # case too); only a failure before the manifest exists leaves nothing to book
        if (run_dir / "manifest.json").exists():
            budget.record(run_dir)


def _unsupported(selected: list[dict], records: dict[str, dict]) -> dict | None:
    """Projects whose agent found no files to scan (agent_status no_files, D-27) and the truths they carry, None if none.

    They are never scored, so no metric denominator holds their truths. This block keeps them visible: a tool limit,
    not a crash, and not a silent rise of Recall either."""
    rows = [{"project_id": p["project_id"], "truths": len(p.get("vulnerabilities") or [])}
            for p in selected if records[p["project_id"]]["agent_status"] == "no_files"]
    return {"projects": rows, "n_projects": len(rows), "truths": sum(r["truths"] for r in rows)} if rows else None


def _with_recall(u: dict, overall: dict | None) -> dict:
    """u plus Recall as if the unsupported truths were all misses, and a one-line summary (D-27).

    overall is the scorer-derived block of the scored projects (None: nothing was scored). The headline keeps the
    scored projects' own denominator; this adds these truths to it, with the same numerators:
    strict  matched / (truths of scored projects + unsupported truths)
    lenient (matched + partial) / the same"""
    from report.render import fmt

    o = overall or {}
    hits, partial = o.get("matched") or 0, o.get("partial") or 0
    n = (o.get("truths_in_dataset") or 0) + u["truths"]
    strict, lenient = (hits / n, (hits + partial) / n) if n else (None, None)
    line = (f"지원 밖 프로젝트 {u['n_projects']}개, 정답 {u['truths']}건. 이 정답을 전부 미탐으로 넣으면 Recall "
            f"{fmt(strict)} (strict) / {fmt(lenient)} (lenient). 분모는 채점된 프로젝트 정답과 지원 밖 정답의 합이고 "
            f"체크아웃 실패·pinned 누락·crashed·timeout 정답은 제외한다. 헤드라인(지원 프로젝트만)은 "
            f"{fmt(o.get('recall_vs_dataset'))} (strict) / {fmt(o.get('recall_with_partial_vs_dataset'))} (lenient)")
    return {**u, "truths_in_dataset_incl_unsupported": n, "recall_vs_dataset_incl_unsupported": strict,
            "recall_with_partial_vs_dataset_incl_unsupported": lenient, "line": line}


def _judge_incomplete(scores: dict, records: dict[str, dict]) -> list[dict]:
    """Projects with truths_unevaluated > 0 in a scores.json and at least one agent finding: the scorer produced no entry for
    those truths (D-28).

    Judge calls that failed (OpenAI "Connection error", "Request timed out" in scoring_output/scorer_stdout.log) leave truths
    unevaluated, and the headline Recall counts them as misses, so such a run reads low for a reason that is not the agent. The
    scorer also leaves truths unevaluated when matches used all the findings up; this cannot tell that from a failed call, so
    it is a flag to look at the log, and `eval rescore` (README) is the repair for the failed-call case. A project whose agent
    gave no findings (records[pid]["n_findings"] 0) is not flagged: every truth is unevaluated there and no judge call was made
    (D-28 review 4). n_findings is in the manifest records, not in scores.json, hence the second argument."""
    return [{"project_id": pid, "truths_unevaluated": s["truths_unevaluated"]}
            for pid, s in (scores.get("per_project") or {}).items()
            if isinstance(s.get("truths_unevaluated"), int) and s["truths_unevaluated"] > 0
            and (records.get(pid, {}).get("n_findings") or 0) > 0]


def _score_and_finish(run_dir: Path, agent, agent_name: str, info: dict, dataset: str, selected: list[dict],
                      records: dict[str, dict], judge: str, judge_model: str, rep: tuple[int, int], started: str,
                      only_missing: bool = False, timeout_s: int | None = None) -> Path:
    ok_ids = [p["project_id"] for p in selected if records[p["project_id"]]["agent_status"] == "ok"]
    unsupported = _unsupported(selected, records)  # D-27; the manifest gets it even if scoring fails

    scoring_error = None
    incomplete: list[dict] = []
    try:
        if ok_ids:
            res = evaluate(run_dir, agent_name, ok_ids, judge=judge, model=judge_model, batch_size=BATCH_SIZE,
                           only_missing=only_missing)
            if not (run_dir / "scores.json").exists():
                _write_json(run_dir / "scores.json", res)
        else:
            _write_json(run_dir / "scores.json", {
                "judge": judge, "scorer_version": scorer_version(), "scorer_commit": SCORER_COMMIT,
                "model": judge_model, "batch_size": BATCH_SIZE, "iterations": 3, "per_project": {},
                "overall": None, "fn": [], "fp": [], "info_findings": [],
                "note": "no project reached agent_status ok; nothing was scored", "definitions": DEFINITIONS})
        path = run_dir / "scores.json"
        scores = json.loads(path.read_text(encoding="utf-8"))
        extra = {}
        if unsupported:  # the same block in scores.json and the manifest; the headline in `overall` is unchanged
            unsupported = _with_recall(unsupported, scores.get("overall"))
            extra["unsupported"] = unsupported
        incomplete = _judge_incomplete(scores, records)  # D-28: the same flag in scores.json and the manifest
        if incomplete:
            extra.update(judge_incomplete=True, judge_incomplete_projects=incomplete)
        if extra:
            _write_json(path, {**scores, **extra})
    except Exception as e:
        scoring_error = f"{type(e).__name__}: {e}"
        raise
    finally:
        fields = dict(
            run_id=f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}",
            started_at=started, finished_at=_now(), cli_argv=list(sys.argv), dataset=info,
            holdout_used=(dataset == "holdout"),
            agent={"name": agent_name, "version": agent.version(), "model": agent.model(),
                   "prompt_hash": agent.prompt_hash(), **agent.fingerprints(),
                   # the project timeout depends on the agent's default (D-24), so cli_argv alone no longer shows it.
                   # rescore never ran the agent and does not know it: no key then
                   **({} if timeout_s is None else {"timeout_s": timeout_s})},
            # mock judge calls no model; recording judge_model there would misdescribe the run
            judge=judge_block(judge, "mock" if judge == "mock" else judge_model, BATCH_SIZE), executor="local",
            repeat={"index": rep[0], "of": rep[1]}, projects=[records[p["project_id"]] for p in selected])
        if unsupported:
            fields["unsupported"] = unsupported
        if incomplete:
            fields.update(judge_incomplete=True, judge_incomplete_projects=incomplete)
        if scoring_error:
            fields["scoring_error"] = scoring_error
        write_manifest(run_dir, build_manifest(**fields))
    from report.render import render
    render(run_dir)
    return run_dir


RUN_OPTS = ("model", "effort", "max_tokens")  # what scan.py logs of its options in the `_run` row (D-28 review 1)


def _opt_ok(key: str, value) -> bool:
    return type(value) is int if key == "max_tokens" else isinstance(value, str) and value != ""


def _logged_opts(run_dir: Path, pids: list[str]) -> dict[str, dict]:
    """{project_id: {model, effort, max_tokens}} from the first line (`_run`) of projects/<id>/raw/scan.jsonl.

    A project without that file or row is left out (an unreadable first line counts as no row), and a key the row lacks or holds
    in the wrong type is left out of its dict."""
    out = {}
    for pid in pids:
        try:
            with open(run_dir / "projects" / pid / "raw" / "scan.jsonl", encoding="utf-8") as f:
                row = json.loads(f.readline()).get("_run")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(row, dict):
            out[pid] = {k: row[k] for k in RUN_OPTS if _opt_ok(k, row.get(k))}
    return out


def _check_logged_opts(run_dir: Path, pids: list[str], opts: dict) -> dict:
    """opts (the --model, --effort, --max-tokens arguments) held against what the agent stage logged (D-28 review 1).

    rescore never runs the agent, so these arguments only label the manifest, and a label that differs from the `_run` row
    would be a wrong record. A different value is refused (ValueError, which the CLI prints with exit 1). A value not given is
    taken from the row, not from scan.py's current default, which would mislabel a max or 128000 run. Nothing is checked
    for a key no project logged (no raw/scan.jsonl, a run from before D-28 that has no max_tokens, or an older scan.py): the
    manifest then takes the argument or scan.py's default, and one stderr line names those keys because that label is unverified.
    Projects that logged different values for one key can't be labelled with either, so that is refused too."""
    logged = _logged_opts(run_dir, pids)
    out = dict(opts)
    missing = []  # keys no project's `_run` row gave
    for k in RUN_OPTS:
        seen: dict = {}  # logged value -> first project that logged it
        for pid, row in logged.items():
            if k in row:
                seen.setdefault(row[k], pid)
        flag = "--" + k.replace("_", "-")
        if len(seen) > 1:
            raise ValueError(f"projects/*/raw/scan.jsonl 의 _run 이 서로 다른 {k} 를 기록했다: "
                             + ", ".join(f"{v} ({p})" for v, p in seen.items()) + ". 한 값으로 라벨을 정할 수 없다")
        if not seen:
            missing.append(k)
            continue
        (val, pid), = seen.items()
        if opts.get(k) is not None and opts[k] != val:
            raise ValueError(f"{flag} {opts[k]} 는 원래 run 의 {k} 값({val})과 다르다 (projects/{pid}/raw/scan.jsonl 첫 줄 _run). "
                             f"원래 값을 주거나 {flag} 를 빼라(빼면 _run 값을 쓴다)")
        out[k] = val
    if missing:
        print(f"rescore: {run_dir.name} 의 _run 행에서 {'/'.join(missing)} 를 찾지 못해 인자 또는 기본값으로 기록한다", file=sys.stderr)
    return out


def rescore(run_dir, agent_name, dataset, projects=None, suite=None, judge="nethermind",
            judge_model="gpt-5-nano-2025-08-07", agent_opts=None) -> Path:
    """Finish a run whose agent stage completed but scoring did not (e.g. the process was killed).

    Re-uses projects/<id>/{checkout,findings}.json and judges only projects that have no
    scoring_output/<id>_results.json yet. The agent is never run again, so the only cost is the judge.
    --model/--effort/--max-tokens (agent_opts) are checked against the `_run` row of the attacktrail API agent's logs and filled
    from it when not given (_check_logged_opts, D-28).
    """
    from adapters.base import Findings

    run_dir = Path(run_dir).resolve()
    if (run_dir / "manifest.json").exists():
        raise FileExistsError(f"{run_dir} already has a manifest; nothing to rescore")
    if judge == "nethermind" and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("judge 'nethermind' needs OPENAI_API_KEY; not set")
    items, info = load_dataset(dataset)
    selected = select_projects(items, projects, suite)
    opts = dict(agent_opts or {})
    if agent_name == "attacktrail":  # the CLI adapter and baseline write no scan.jsonl and have no effort or max_tokens
        opts = _check_logged_opts(run_dir, [p["project_id"] for p in selected], opts)
    records: dict[str, dict] = {}
    for p in selected:
        pid = p["project_id"]
        rec = _blank(pid)
        reason = excluded_reason(p)
        pdir = run_dir / "projects" / pid
        if reason:
            rec["excluded_reason"] = reason
        elif not (pdir / "findings.json").is_file():
            rec["agent_status"] = "crashed"
            rec["agent_error"] = "rescore: no findings.json (agent stage did not finish)"
        else:
            rec["checkout"] = json.loads((pdir / "checkout.json").read_text(encoding="utf-8"))
            f = Findings.model_validate_json((pdir / "findings.json").read_text(encoding="utf-8"))
            m = f.meta
            rec.update(agent_status=m.status, agent_error=m.error, wall_seconds=m.wall_seconds,
                       tokens_in=m.tokens_in, tokens_out=m.tokens_out, cost_usd=m.cost_usd,
                       n_findings=len(f.vulnerabilities), **{k: getattr(m, k) for k in META_KEYS})
            if m.status == "ok" and not (run_dir / "scoring_data" / agent_name / f"{pid}_results.json").is_file():
                rec["agent_status"] = "crashed"
                rec["agent_error"] = "rescore: findings.json present but scorer input missing"
        records[pid] = rec

    from adapters import get_agent
    agent = get_agent(agent_name, **opts)
    # the agent stage ran in this dir and left the scan.py + standards it started with: fingerprint those, not the
    # live files as they are now (D-26). Only some adapters snapshot; no copy found -> live, and the manifest says so.
    adopt = getattr(agent, "adopt_snapshot", None)
    if adopt:
        adopt(run_dir)
    done = _score_and_finish(run_dir, agent, agent_name, info, dataset, selected, records, judge, judge_model,
                             (1, 1), _now(), only_missing=True)
    _print_summary([done])  # the same end-of-run summary `eval run` prints (D-23); stdout stays the path cli.py prints
    return done


def _mean_known(values: list) -> float | None:
    """Mean only when every value is known; a partial average would hide missing data."""
    if not values or any(v is None for v in values):
        return None
    return statistics.mean(values)


def _aggregate(out_dir: Path, rep_dirs: list[Path], started: str, info: dict, dataset: str) -> None:
    scores = [json.loads((d / "scores.json").read_text(encoding="utf-8")) for d in rep_dirs]
    mans = [json.loads((d / "manifest.json").read_text(encoding="utf-8")) for d in rep_dirs]
    per_rep: list[dict[str, float | None]] = []
    for s, m in zip(scores, mans):
        row = {k: v for k, v in (s.get("overall") or {}).items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
        ok = [p for p in m["projects"] if p["agent_status"] == "ok"]
        row["cost_usd_per_project"] = _mean_known([p["cost_usd"] for p in ok])
        row["wall_seconds_per_project"] = _mean_known([p["wall_seconds"] for p in ok])
        per_rep.append(row)
    agg = {}
    for key in dict.fromkeys(k for row in per_rep for k in row):
        vals = [row.get(key) for row in per_rep]
        known = [v for v in vals if v is not None]
        agg[key] = {"mean": statistics.mean(known) if known else None,
                    "stdev": statistics.stdev(known) if len(known) > 1 else None,
                    "n": len(known), "values": vals}
    first = scores[0]
    # D-28: a rep judged incompletely makes the means and stdevs above wrong (its unjudged truths count as misses)
    inc = [{**r, "run": d.name} for s, d in zip(scores, rep_dirs) for r in s.get("judge_incomplete_projects") or []]
    inc_fields = {"judge_incomplete": True, "judge_incomplete_projects": inc} if inc else {}
    _write_json(out_dir / "scores.json", {
        **inc_fields,
        "repeat": len(rep_dirs), "judge": first.get("judge"), "scorer_version": first.get("scorer_version"),
        "scorer_commit": first.get("scorer_commit"), "model": first.get("model"),
        "batch_size": first.get("batch_size"), "iterations": 3, "aggregate": agg,
        "runs": [d.name for d in rep_dirs],
        "definitions": next((s["definitions"] for s in scores if s.get("definitions")), DEFINITIONS)})
    m0 = mans[0]
    write_manifest(out_dir, build_manifest(
        run_id=f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}-agg",
        started_at=started, finished_at=_now(), cli_argv=list(sys.argv), dataset=info,
        holdout_used=(dataset == "holdout"), agent=m0["agent"], judge=m0["judge"], executor=m0["executor"],
        repeat={"index": None, "of": len(rep_dirs)}, projects=[], runs=[d.name for d in rep_dirs], **inc_fields))


def _print_summary(run_dirs: list[Path]) -> None:
    """End-of-run console summary (D-23) on stderr, so stdout stays the run path `eval run` prints.

    The run is already scored and reported when this runs; a failure to summarize must not turn it into an error.
    """
    from report.render import run_summary

    for d in run_dirs:
        try:
            man = json.loads((Path(d) / "manifest.json").read_text(encoding="utf-8"))
            lines = [f"[run 요약] {Path(d).name}", *run_summary(man)]
        except Exception as e:
            lines = [f"[run 요약] 요약을 만들지 못했다: {type(e).__name__}: {e}"]
        print("\n".join(lines), file=sys.stderr)


def run(agent_name, dataset, out_dir, projects=None, suite=None, repeat=1, jobs=2, timeout_s=None,
        judge="nethermind", judge_model="gpt-5-nano-2025-08-07", agent_opts=None, allow_holdout=False) -> Path:
    """timeout_s: project timeout in seconds; None means the agent's default_timeout_s (D-24)."""
    if judge not in ("nethermind", "mock"):
        raise ValueError(f"unknown judge: {judge!r}")
    if judge == "nethermind" and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("judge 'nethermind' needs OPENAI_API_KEY; not set (nothing was cloned or run)")
    if repeat < 1:
        raise ValueError("repeat must be >= 1")
    budget.check()  # D-34: refuse to start once the spend ledger has reached the cap
    items, info = load_dataset(dataset, allow_holdout=allow_holdout)
    selected = select_projects(items, projects, suite)

    from adapters import get_agent
    agent = get_agent(agent_name, **(agent_opts or {}))
    timeout_s = timeout_s or agent.default_timeout_s
    out_dir = Path(out_dir).resolve()
    args = (agent, agent_name, info, dataset, selected, jobs, timeout_s, judge, judge_model)
    if repeat == 1:
        done = _single(out_dir, *args, rep=(1, 1))
        _print_summary([done])
        return done
    if (out_dir / "manifest.json").exists():
        raise FileExistsError(f"{out_dir} already holds a run; choose a new --out")
    started = _now()
    rep_dirs = [_single(out_dir / f"rep-{k}", *args, rep=(k, repeat)) for k in range(1, repeat + 1)]
    _aggregate(out_dir, rep_dirs, started, info, dataset)
    from report.render import render
    render(out_dir)
    _print_summary(rep_dirs)
    return out_dir
