from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from fnmatch import fnmatchcase
from pathlib import Path

from .models import WorkspaceSpec

GIT_IDENTITY = ["-c", "user.name=conductor", "-c", "user.email=conductor@localhost", "-c", "core.hooksPath=/dev/null"]


class WorkspaceError(Exception):
    pass


def is_excluded(rel: str, patterns: list[str]) -> bool:
    rel = rel.strip("/")
    return any(fnmatchcase(rel, p) or fnmatchcase(rel + "/", p) for p in patterns)


def check_source(spec: WorkspaceSpec, allowed: list[str]) -> str:
    source = spec.source or ""
    if "://" not in source and not source.startswith("git@"):
        source = str(Path(source).expanduser().resolve())
    if allowed and not any(source == a or source.startswith(a.rstrip("/") + "/") for a in allowed):
        raise WorkspaceError(f"workspace source {source} is not in this runner's allowed_sources")
    return source


def git(*args: str, cwd: Path | None = None, capture: bool = True) -> bytes:
    proc = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=cwd,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args[:2])} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout or b""


def prepare(spec: WorkspaceSpec, dest: Path, exclude: list[str], allowed: list[str], cache_root: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if spec.type == "none":
        return
    source = check_source(spec, allowed)
    if spec.type == "dir":
        _copy_dir(Path(source), dest, exclude)
    else:
        _export_git(source, spec.ref, dest, exclude, cache_root)


def _copy_dir(src: Path, dest: Path, exclude: list[str]) -> None:
    if not src.is_dir():
        raise WorkspaceError(f"{src} is not a directory")

    def ignore(directory: str, names: list[str]) -> list[str]:
        base = Path(directory).relative_to(src)
        return [n for n in names if is_excluded((base / n).as_posix(), exclude)]

    shutil.copytree(src, dest, ignore=ignore, dirs_exist_ok=True, symlinks=True)


def _export_git(source: str, ref: str, dest: Path, exclude: list[str], cache_root: Path) -> None:
    """Export a snapshot into a fresh repository.

    Excluded paths are dropped before the new repository is created, so their
    content is absent from the agent's git object database as well as its tree.
    """
    cache = cache_root / hashlib.sha256(source.encode()).hexdigest()[:24]
    if cache.exists():
        git("remote", "update", "--prune", cwd=cache)
    else:
        cache_root.mkdir(parents=True, exist_ok=True)
        git("clone", "--mirror", "--quiet", source, str(cache))
    commit = git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=cache).decode().strip()
    data = git("archive", "--format=tar", commit, cwd=cache)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = [m for m in tar.getmembers() if not is_excluded(m.name, exclude)]
        tar.extractall(dest, members=members, filter="data")
    git("init", "--quiet", cwd=dest)
    git("add", "-A", cwd=dest)
    git("commit", "--quiet", "--allow-empty", "-m", f"conductor base {commit[:12]}", cwd=dest)


def diff(dest: Path) -> bytes:
    git("add", "-A", cwd=dest)
    return git("diff", "--cached", "--binary", "HEAD", cwd=dest)
