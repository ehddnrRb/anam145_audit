"""Agent interface and the Findings schema every adapter must emit."""
from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SEVERITIES = ("critical", "high", "medium", "low", "info")
AGENT_STATUS = ("ok", "timeout", "build_failed", "crashed")

EVAL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = EVAL_DIR.parent

# OS variables a Python subprocess needs on Windows/POSIX. Nothing else is inherited.
OS_ENV_KEYS = ("PATH", "SYSTEMROOT", "TEMP", "TMP")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Strict):
    poc: Any | None = None
    tool_output: Any | None = None


class Finding(_Strict):
    id: str
    title: str
    file: str | None
    lines: list[int]
    severity: Literal[SEVERITIES]  # type: ignore[valid-type]
    description: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: Evidence = Field(default_factory=Evidence)

    @field_validator("description")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("description must not be blank")
        return v


class Meta(_Strict):
    agent: str
    agent_version: str
    model: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    wall_seconds: float | None = None
    status: Literal[AGENT_STATUS]  # type: ignore[valid-type]
    error: str | None = None


class Findings(_Strict):
    vulnerabilities: list[Finding]
    meta: Meta


@dataclass
class Scope:
    project_id: str
    repo_path: Path


@dataclass
class RunContext:
    run_dir: Path
    project_dir: Path
    timeout_s: int
    env: dict[str, str]  # the ONLY env vars the agent subprocess may see


_SEV_ALIASES = {"informational": "info", "information": "info"}


def map_severity(raw: Any) -> str | None:
    """Agent severity string -> SEVERITIES value, or None when unknown."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    s = _SEV_ALIASES.get(s, s)
    return s if s in SEVERITIES else None


def repo_version() -> str:
    """git sha of this repo's HEAD, '-dirty' when the work tree has changes."""
    try:
        sha = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (sha or "unknown") + ("-dirty" if dirty else "")


def scoped_env(ctx: RunContext, secret_key: str) -> dict[str, str]:
    """Minimal OS env plus exactly one secret. Values come from ctx.env, then os.environ."""
    env = {}
    for k in OS_ENV_KEYS + (secret_key,):
        v = ctx.env.get(k) or os.environ.get(k)
        if v:
            env[k] = v
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class Agent(ABC):
    name: str

    def version(self) -> str:
        return repo_version()

    def model(self) -> str:
        raise NotImplementedError

    def prompt_hash(self) -> str:
        raise NotImplementedError

    def failed(self, status: str, error: str, wall_seconds: float | None = None) -> Findings:
        """Findings for an agent-side failure: no vulnerabilities, status + error recorded."""
        return Findings(vulnerabilities=[], meta=Meta(
            agent=self.name, agent_version=self.version(), model=self.model(),
            status=status, error=error, wall_seconds=wall_seconds))

    @abstractmethod
    def run(self, scope: Scope, ctx: RunContext) -> Findings:
        """Must not raise for agent-side failures; return self.failed(...) instead."""
