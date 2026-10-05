"""Dataset loading. The holdout set is never loaded unless explicitly allowed (owner rule R5)."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parent.parent
SCABENCH_DIR = EVAL_ROOT / "datasets" / "scabench"
SCABENCH_FILE = SCABENCH_DIR / "datasets" / "curated-2025-08-18" / "curated-2025-08-18.json"
HOLDOUT_DIR = EVAL_ROOT / "datasets" / "holdout" / "projects"


class HoldoutNotAllowed(RuntimeError):
    pass


def _git_sha(repo: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def load_dataset(name: str, allow_holdout: bool = False) -> tuple[list[dict], dict]:
    if name == "scabench":
        projects = json.loads(SCABENCH_FILE.read_text(encoding="utf-8"))
        return projects, {"name": name, "path": str(SCABENCH_FILE), "sha": _git_sha(SCABENCH_DIR)}
    if name == "holdout":
        if not allow_holdout:
            raise HoldoutNotAllowed("holdout dataset requires allow_holdout=True")
        files = sorted(HOLDOUT_DIR.glob("*.json"))
        projects, h = [], hashlib.sha256()
        for f in files:
            raw = f.read_bytes()
            h.update(f.name.encode() + b"\0" + raw)
            data = json.loads(raw.decode("utf-8"))
            projects.extend(data if isinstance(data, list) else [data])
        # holdout is not a git submodule; sha = sha256 over (filename, bytes) of every project file
        return projects, {"name": name, "path": str(HOLDOUT_DIR), "sha": "sha256:" + h.hexdigest()}
    raise ValueError(f"unknown dataset: {name!r} (expected 'scabench' or 'holdout')")


def excluded_reason(project: dict) -> str | None:
    if any(not (cb.get("commit") or "").strip() for cb in project.get("codebases") or [{}]):
        return "pinned_commit_missing"
    return None
