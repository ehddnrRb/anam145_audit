"""Run manifest. Never contains API keys or an env dump."""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from importlib import metadata
from pathlib import Path

SCORER_COMMIT = "a62a15d1ea254158dd5cda4bea8ce8cf5791fb27"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DENOMINATORS_NOTE = (
    "Metric denominators include only projects whose checkout succeeded and whose agent_status is ok. "
    "Projects excluded by the dataset (excluded_reason), failed checkouts and crashed/timed-out agents "
    "are listed in projects[] with their reason and are not counted. "
    "Projects with agent_status no_files (the scanner found no file in its supported languages) are not counted "
    "either, but they are a tool limit, not a failure: their truths and the Recall with those truths counted as "
    "misses are in `unsupported` (here and in scores.json)."
)
_SECRET_ENV = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ATTACKTRAIL_ANTHROPIC_API_KEY")
_SECRET_PAT = re.compile(r"sk-(ant-)?[A-Za-z0-9_-]{16,}")
_FORBIDDEN_KEYS = {"env", "environ", "environment", "api_key", "openai_api_key", "anthropic_api_key"}


def repo_version(repo: Path = REPO_ROOT) -> str | None:
    """git sha of repo HEAD, plus '-dirty' when the working tree has changes."""
    try:
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
        if sha.returncode != 0:
            return None
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return sha.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")


def scorer_version() -> str | None:
    try:
        return metadata.version("scoring-algo")
    except metadata.PackageNotFoundError:
        return None


def judge_block(kind: str, model: str, batch_size: int = 10) -> dict:
    return {"kind": kind, "scorer_package": "scoring-algo", "scorer_version": scorer_version(),
            "scorer_commit": SCORER_COMMIT, "model": model, "batch_size": batch_size, "iterations": 3}


def _scrub(obj, secrets: list[str]):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in _FORBIDDEN_KEYS:
                raise ValueError(f"manifest field {k!r} is not allowed (no keys / env dumps)")
            out[k] = _scrub(v, secrets)
        return out
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, secrets) for v in obj]
    if isinstance(obj, Path):
        obj = str(obj)
    if isinstance(obj, str):
        for s in secrets:
            obj = obj.replace(s, "[redacted]")
        return _SECRET_PAT.sub("[redacted]", obj)
    return obj


def build_manifest(**fields) -> dict:
    m = {
        "run_id": None, "started_at": None, "finished_at": None, "cli_argv": None,
        "dataset": None, "holdout_used": False, "agent": None, "judge": None,
        "executor": "local", "platform": f"{platform.platform()} python {platform.python_version()}",
        "repeat": {"index": 1, "of": 1}, "projects": [], "denominators_note": DENOMINATORS_NOTE,
    }
    m.update(fields)
    secrets = [os.environ[k] for k in _SECRET_ENV if os.environ.get(k)]
    return _scrub(m, secrets)


def write_manifest(run_dir: Path, manifest: dict) -> Path:
    path = Path(run_dir) / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    secrets = [os.environ[k] for k in _SECRET_ENV if os.environ.get(k)]
    path.write_text(json.dumps(_scrub(manifest, secrets), indent=2, ensure_ascii=False), encoding="utf-8")
    return path
