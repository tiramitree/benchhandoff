"""Add bounded environment and exact-source metadata to synthetic results."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import platform as runtime_platform
import subprocess
import sys


def _invoke_git(repository_root: Path, *arguments: str) -> subprocess.CompletedProcess[str] | None:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            shell=False,
            check=False,
        )
    except OSError:
        return None


def _source_git_state(repository_root: Path) -> tuple[str | None, bool | None]:
    """Return metadata only when repository_root is itself the Git top level."""

    top_level = _invoke_git(repository_root, "rev-parse", "--show-toplevel")
    if top_level is None or top_level.returncode != 0:
        return None, None
    try:
        discovered = Path(top_level.stdout.strip()).resolve()
        expected = repository_root.resolve()
    except OSError:
        return None, None
    if os.path.normcase(str(discovered)) != os.path.normcase(str(expected)):
        return None, None

    commit = _invoke_git(repository_root, "rev-parse", "--verify", "HEAD")
    if commit is None or commit.returncode != 0 or not commit.stdout.strip():
        return None, None

    status = _invoke_git(
        repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    clean = None
    if status is not None and status.returncode == 0:
        clean = not bool(status.stdout.strip())
    return commit.stdout.strip(), clean


def benchmark_provenance(repository_root: Path) -> dict[str, object]:
    source_commit, clean = _source_git_state(repository_root)
    return {
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "operating_system": runtime_platform.system(),
        "platform": sys.platform,
        "platform_details": runtime_platform.system() or "Unknown",
        "python_version": runtime_platform.python_version(),
        "python_implementation": runtime_platform.python_implementation(),
        "source_git_commit": source_commit,
        "source_git_clean": clean,
    }
