"""Cooperative local cross-process serialization for run mutations."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from benchhandoff.errors import BoundaryError, EvidenceError
from benchhandoff.processes import process_start_token
from benchhandoff.storage import (
    canonical_json_bytes,
    checked_directory,
    file_identity,
    identities_match,
)

WRITER_LOCK_SCHEMA_VERSION = 1
WRITER_LOCK_SUFFIX = ".benchhandoff-writer-lock.json"


def _sync_directory(path: Path) -> None:
    """Best-effort directory-entry durability on platforms that expose it."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _normalized_run_path(run_directory: Path | str) -> Path:
    candidate = Path(run_directory).absolute()
    if not candidate.name:
        raise BoundaryError("run directory must have a final path component")
    parent = checked_directory(candidate.parent, label="writer-lock parent")
    return parent / candidate.name


def writer_lock_path(run_directory: Path | str) -> Path:
    """Return the sibling lock path for one normalized run directory."""

    run_path = _normalized_run_path(run_directory)
    return run_path.parent / f".{run_path.name}{WRITER_LOCK_SUFFIX}"


def _owner_record(run_path: Path) -> dict[str, Any]:
    process_id = os.getpid()
    return {
        "schema_version": WRITER_LOCK_SCHEMA_VERSION,
        "kind": "benchhandoff-writer-lock",
        "run_directory": str(run_path),
        "owner_pid": process_id,
        "owner_process_start_token": process_start_token(process_id),
        "lock_nonce": uuid4().hex,
    }


@dataclass
class WriterLock:
    """One owned local lock file created with O_EXCL and removed after mutation."""

    path: Path
    record: dict[str, Any]
    identity: dict[str, Any]
    _released: bool = False

    @classmethod
    def acquire(cls, run_directory: Path | str) -> "WriterLock":
        """Acquire the cooperative writer lock without overwriting any file."""

        run_path = _normalized_run_path(run_directory)
        path = writer_lock_path(run_path)
        record = _owner_record(run_path)
        payload = canonical_json_bytes(record)
        expected_identity = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise EvidenceError(
                f"writer lock already exists for run directory: {path}; "
                "another writer may be active or a prior writer may have stopped "
                "without releasing it"
            ) from exc
        except OSError as exc:
            raise EvidenceError(f"unable to create writer lock {path}: {exc}") from exc

        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _sync_directory(path.parent)
            identity = file_identity(path, label="writer lock")
            if not identities_match(identity, expected_identity):
                raise EvidenceError("writer lock changed during acquisition")
        except Exception:
            try:
                current = file_identity(path, label="writer lock after failed acquisition")
            except Exception:
                current = None
            if identities_match(current, expected_identity):
                try:
                    path.unlink()
                    _sync_directory(path.parent)
                except OSError:
                    pass
            raise
        return cls(path=path, record=record, identity=identity)

    def release(self) -> None:
        """Release only the exact lock bytes acquired by this instance."""

        if self._released:
            return
        try:
            current = file_identity(self.path, label="writer lock")
        except Exception as exc:
            raise EvidenceError(
                f"writer lock disappeared or became unsafe before release: {self.path}"
            ) from exc
        if not identities_match(current, self.identity):
            raise EvidenceError(
                f"writer lock changed before release; refusing to remove it: {self.path}"
            )
        try:
            self.path.unlink()
            _sync_directory(self.path.parent)
        except OSError as exc:
            raise EvidenceError(f"unable to release writer lock {self.path}: {exc}") from exc
        self._released = True
