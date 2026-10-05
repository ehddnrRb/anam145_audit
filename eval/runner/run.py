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

from runner.checkout import checkout_project, validate_name
from runner.dataset import EVAL_ROOT, excluded_reason, load_dataset
from runner.manifest import SCORER_COMMIT, build_manifest, judge_block, scorer_version, write_manifest
from scoring.nethermind import DEFINITIONS, evaluate

SOURCES_ROOT = EVAL_ROOT / "datasets" / "sources"
SUITES_DIR = EVAL_ROOT / "suites"
BATCH_SIZE = 10
AGENT_KEY = {"attacktrail": "ANTHROPIC_API_KEY", "baseline": "OPENAI_API_KEY"}
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
    if key and os.environ.get(key):
        env[key] = os.environ[key]
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


def _blank(pid: str) -> dict:
    return {"project_id": pid, "excluded_reason": None, "checkout": None, "agent_status": None,
            "agent_error": None, "wall_seconds": None, "tokens_in": None, "tokens_out": None,
            "cost_usd": None, "n_findings": None}


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
                   tokens_out=m.tokens_out, cost_usd=m.cost_usd, n_findings=len(findings.vulnerabilities))
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
    ok_ids = [p["project_id"] for p in selected if records[p["project_id"]]["agent_status"] == "ok"]

    scoring_error = None
    try:
        if ok_ids:
            res = evaluate(run_dir, agent_name, ok_ids, judge=judge, model=judge_model, batch_size=BATCH_SIZE)
            if not (run_dir / "scores.json").exists():
                _write_json(run_dir / "scores.json", res)
        else:
            _write_json(run_dir / "scores.json", {
                "judge": judge, "scorer_version": scorer_version(), "scorer_commit": SCORER_COMMIT,
                "model": judge_model, "batch_size": BATCH_SIZE, "iterations": 3, "per_project": {},
                "overall": None, "fn": [], "fp": [], "info_findings": [],
                "note": "no project reached agent_status ok; nothing was scored", "definitions": DEFINITIONS})
    except Exception as e:
        scoring_error = f"{type(e).__name__}: {e}"
        raise
    finally:
        fields = dict(
            run_id=f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}",
            started_at=started, finished_at=_now(), cli_argv=list(sys.argv), dataset=info,
            holdout_used=(dataset == "holdout"),
            agent={"name": agent_name, "version": agent.version(), "model": agent.model(),
                   "prompt_hash": agent.prompt_hash()},
            # mock judge calls no model; recording judge_model there would misdescribe the run
            judge=judge_block(judge, "mock" if judge == "mock" else judge_model, BATCH_SIZE), executor="local",
            repeat={"index": rep[0], "of": rep[1]}, projects=[records[p["project_id"]] for p in selected])
        if scoring_error:
            fields["scoring_error"] = scoring_error
        write_manifest(run_dir, build_manifest(**fields))
    from report.render import render
    render(run_dir)
    return run_dir


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
    _write_json(out_dir / "scores.json", {
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
        repeat={"index": None, "of": len(rep_dirs)}, projects=[], runs=[d.name for d in rep_dirs]))


def run(agent_name, dataset, out_dir, projects=None, suite=None, repeat=1, jobs=2, timeout_s=1800,
        judge="nethermind", judge_model="o4-mini", agent_opts=None, allow_holdout=False) -> Path:
    if judge not in ("nethermind", "mock"):
        raise ValueError(f"unknown judge: {judge!r}")
    if judge == "nethermind" and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("judge 'nethermind' needs OPENAI_API_KEY; not set (nothing was cloned or run)")
    if repeat < 1:
        raise ValueError("repeat must be >= 1")
    items, info = load_dataset(dataset, allow_holdout=allow_holdout)
    selected = select_projects(items, projects, suite)

    from adapters import get_agent
    agent = get_agent(agent_name, **(agent_opts or {}))
    out_dir = Path(out_dir).resolve()
    args = (agent, agent_name, info, dataset, selected, jobs, timeout_s, judge, judge_model)
    if repeat == 1:
        return _single(out_dir, *args, rep=(1, 1))
    if (out_dir / "manifest.json").exists():
        raise FileExistsError(f"{out_dir} already holds a run; choose a new --out")
    started = _now()
    rep_dirs = [_single(out_dir / f"rep-{k}", *args, rep=(k, repeat)) for k in range(1, repeat + 1)]
    _aggregate(out_dir, rep_dirs, started, info, dataset)
    from report.render import render
    render(out_dir)
    return out_dir
