"""Workspace-local temporary directories usable by restricted Windows test tokens."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_BASE = REPOSITORY_ROOT / ".test-runtime"


def _rmtree_path(path: Path) -> Path | str:
    """Use an extended-length root so Windows can remove long descendants."""

    if os.name != "nt":
        return path
    absolute = str(path.resolve(strict=True))
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


class WorkspaceTemporaryDirectory:
    """A minimal TemporaryDirectory equivalent with inherited workspace ACLs."""

    def __init__(self, *, prefix: str) -> None:
        _BASE.mkdir(exist_ok=True)
        self.path = _BASE / f"{prefix}{uuid4().hex}"
        self.path.mkdir()
        self.name = str(self.path)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        if self.path.exists():
            shutil.rmtree(_rmtree_path(self.path))
        try:
            _BASE.rmdir()
        except OSError:
            pass
