"""Subprocess execution with an explicit env (no inheritance). Today only the agent runs here, never target code."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ExecResult:
    returncode: int | None
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.kill()


class LocalExecutor:
    name = "local"

    def run(self, argv: list[str], cwd: Path | str | None, env: dict[str, str], timeout_s: float) -> ExecResult:
        t0 = time.monotonic()
        proc = subprocess.Popen(
            [str(a) for a in argv], cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            start_new_session=(sys.platform != "win32"),
        )
        try:
            out, err = proc.communicate(timeout=timeout_s)
            return ExecResult(proc.returncode, out, err, time.monotonic() - t0, False)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, err = proc.communicate()
            return ExecResult(None, out or "", err or "", time.monotonic() - t0, True)


class DockerExecutor:
    name = "docker"

    def run(self, argv, cwd, env, timeout_s) -> ExecResult:
        raise NotImplementedError("Docker not installed; container isolation is mandatory before any PoC/forge execution")
