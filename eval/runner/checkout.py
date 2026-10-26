"""Clone + pin target repos. Strategy is the one measured in Phase 0 (see contract); do not 'simplify' it.

Only git runs here. Nothing from the cloned repo is ever executed, submodules are never initialised.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# no trailing '.': Windows strips it, so "x." and "x" would alias the same directory
SAFE_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9_-])?$")
WIN_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?$", re.IGNORECASE)
# git and its helpers (git-lfs, git-remote-https) handle untrusted repos: never hand them API keys
GIT_ENV_KEYS = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                "PROGRAMDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LANG", "XDG_CONFIG_HOME",
                "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
                "GIT_SSL_CAINFO", "SSL_CERT_FILE")
SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
HEX_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
GIT_TIMEOUT_S = 1800


@dataclass
class CheckoutResult:
    status: str  # ok | clone_failed | checkout_failed | pinned_commit_missing
    path: Path | None
    head: str | None
    seconds: float
    unshallowed: bool
    error: str | None
    # Extension (not in contract): False when the dataset's "commit" is a branch name such as "main"
    # (5 SCABench projects). The checkout then follows the moving ref and is not reproducible.
    pinned: bool = True


def git_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in GIT_ENV_KEYS if k in os.environ}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    return env


def _git(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=git_env(), timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(e.cmd, 124, "", f"timeout after {GIT_TIMEOUT_S}s")


def _err(cp: subprocess.CompletedProcess) -> str:
    return (cp.stderr or cp.stdout or "").strip()[-2000:]


def validate_name(name: str) -> str:
    if not SAFE_NAME.match(name or "") or ".." in name or WIN_RESERVED.match(name):
        raise ValueError(f"unsafe directory name: {name!r}")
    return name


def _repo_name(url: str) -> str:
    last = url.rstrip("/").rsplit("/", 1)[-1]
    last = last[:-4] if last.endswith(".git") else last
    return re.sub(r"[^A-Za-z0-9._-]", "_", last).strip("._-") or "repo"


def _head(d: Path) -> str | None:
    if not (d / ".git").exists():
        return None
    cp = _git("-C", str(d), "rev-parse", "HEAD")
    return cp.stdout.strip() if cp.returncode == 0 else None


def _at_commit(d: Path, head: str | None, commit: str) -> bool:
    if not head:
        return False
    if HEX_SHA.match(commit):
        return head.lower().startswith(commit.lower())
    cp = _git("-C", str(d), "rev-parse", "--verify", "--quiet", commit + "^{commit}")
    return cp.returncode == 0 and cp.stdout.strip() == head


def _checkout_one(url: str, commit: str, dest: Path, root: Path, depth: int):
    """Returns (status, head, unshallowed, error)."""
    if not SAFE_REF.match(commit) or ".." in commit:
        return "checkout_failed", None, False, f"unsafe commit/ref: {commit!r}"
    if dest.exists():
        head = _head(dest)
        no_links = _git("-C", str(dest), "config", "--get", "core.symlinks").stdout.strip() == "false"
        if no_links and _at_commit(dest, head, commit):
            return "ok", head, False, None  # reuse existing correct checkout
        safe_rmtree(dest, root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cp = _git("-c", "core.longpaths=true", "-c", "core.symlinks=false",
              "clone", "--no-checkout", "--depth", str(depth), "--", url, str(dest))
    if cp.returncode != 0:
        if dest.exists():
            safe_rmtree(dest, root)
        return "clone_failed", None, False, _err(cp)
    _git("-C", str(dest), "config", "core.longpaths", "true")
    # a repo symlink (e.g. -> ~/.aws/credentials) would otherwise be followed by the agent and sent to an LLM;
    # with symlinks=false git writes the link target as a plain text file instead
    if _git("-C", str(dest), "config", "core.symlinks", "false").returncode != 0:
        return "checkout_failed", None, False, "could not set core.symlinks=false"
    unshallowed = False
    cp = _git("-C", str(dest), "checkout", "-f", "--quiet", commit)
    if cp.returncode != 0:
        first = _err(cp)
        unshallowed = True
        fe = _git("-C", str(dest), "fetch", "--unshallow", "--quiet")
        cp = _git("-C", str(dest), "checkout", "-f", "--quiet", commit)
        if cp.returncode != 0:
            return "checkout_failed", None, unshallowed, f"{first}\n[after unshallow rc={fe.returncode}] {_err(fe)}\n{_err(cp)}"
    head = _head(dest)
    if not _at_commit(dest, head, commit):
        return "checkout_failed", head, unshallowed, f"HEAD {head} != {commit}"
    real = _materialized_links(dest)
    if real:
        return "checkout_failed", head, unshallowed, f"symlinks materialized despite core.symlinks=false: {real[:5]}"
    return "ok", head, unshallowed, None


def _materialized_links(d: Path) -> list[str]:
    """Tracked symlinks (mode 120000) that exist on disk as real links. Expected: none."""
    cp = _git("-C", str(d), "ls-files", "-s", "-z")
    out = []
    for rec in cp.stdout.split("\0"):
        meta, _, path = rec.partition("\t")
        if meta.startswith("120000 ") and (d / path).is_symlink():
            out.append(path)
    return out


def checkout_project(project: dict, sources_root: Path, depth: int = 50) -> CheckoutResult:
    t0 = time.monotonic()
    root = Path(sources_root).resolve()
    pid = validate_name(project["project_id"])
    codebases = project.get("codebases") or []

    def done(status, path=None, head=None, unshallowed=False, error=None, pinned=True):
        return CheckoutResult(status, path, head, round(time.monotonic() - t0, 3), unshallowed, error, pinned)

    if not codebases or any(not (cb.get("commit") or "").strip() for cb in codebases):
        return done("pinned_commit_missing", error="empty commit in dataset")
    pinned = all(HEX_SHA.match(cb["commit"].strip()) for cb in codebases)

    if len(codebases) == 1:
        targets = [(codebases[0], root / pid)]
        path = root / pid
    else:
        # one project dir holding <project_id>_<repo> per codebase, so the agent gets a single scope path
        path = root / pid
        targets = [(cb, path / validate_name(f"{pid}_{_repo_name(cb['repo_url'])}")) for cb in codebases]

    heads, any_unshallow = [], False
    for cb, dest in targets:
        status, head, unsh, err = _checkout_one(cb["repo_url"], cb["commit"].strip(), dest, root, depth)
        any_unshallow |= unsh
        if status != "ok":
            return done(status, head=head, unshallowed=any_unshallow, error=err, pinned=pinned)
        heads.append(head)
    return done("ok", path=path, head=",".join(heads), unshallowed=any_unshallow, pinned=pinned)


def _long(p: Path) -> str:
    s = str(p)
    if sys.platform == "win32" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s
    return s


def _onexc(func, path, exc):
    # read-only files (git pack files) block deletion on Windows
    os.chmod(path, stat.S_IWRITE)
    func(path)


def safe_rmtree(path: Path, root: Path) -> None:
    """Delete `path` only if it resolves strictly inside `root`. Links are removed, never followed."""
    r = Path(root).resolve()
    p = Path(os.path.abspath(path))
    rp = p.resolve()
    if rp == r or not rp.is_relative_to(r):
        raise ValueError(f"refusing to delete {p}: not inside {r}")
    if p.is_symlink() or os.path.isjunction(p):
        os.unlink(p) if p.is_symlink() and not p.is_dir() else os.rmdir(p)
        return
    if not p.exists():
        return
    if p.is_dir():
        shutil.rmtree(_long(rp), onexc=_onexc)
    else:
        os.chmod(rp, stat.S_IWRITE)
        os.remove(_long(rp))
