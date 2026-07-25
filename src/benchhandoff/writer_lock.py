"""Cooperative local cross-process serialization for run mutations."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from benchhandoff.errors import BoundaryError, EvidenceError
from benchhandoff.processes import process_liveness, process_start_token
from benchhandoff.storage import (
    canonical_json_bytes,
    checked_directory,
    file_identity,
    identities_match,
    read_regular_bytes,
)

WRITER_LOCK_SCHEMA_VERSION = 1
WRITER_LOCK_SUFFIX = ".benchhandoff-writer-lock.json"
WRITER_LOCK_MAX_BYTES = 4096
WRITER_LOCK_RECOVERY_DECISION_SCHEMA_VERSION = 1
WRITER_LOCK_RECOVERY_RESULT_SCHEMA_VERSION = 1
RECOVERED_WRITER_LOCK_SUFFIX = ".benchhandoff-recovered-writer-lock.json"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_NONCE_PATTERN = re.compile(r"[0-9a-f]{32}")
_WRITER_LOCK_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "run_directory",
        "owner_pid",
        "owner_process_start_token",
        "lock_nonce",
    }
)


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


def _recovery_tombstone_path(path: Path, identity: dict[str, Any]) -> Path:
    return path.parent / (
        f".{identity['sha256']}{RECOVERED_WRITER_LOCK_SUFFIX}"
    )


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


def _identity_from_payload(payload: bytes) -> dict[str, Any]:
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


def _regular_object_key(path: Path, *, label: str) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise BoundaryError(f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise BoundaryError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise BoundaryError(f"{label} must be a regular file: {path}")
    return metadata.st_dev, metadata.st_ino


def _decode_writer_lock_record(
    payload: bytes,
    *,
    run_path: Path,
) -> dict[str, Any]:
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvidenceError(f"writer lock is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise EvidenceError("writer lock must be one JSON object")
    if canonical_json_bytes(record) != payload:
        raise EvidenceError("writer lock must use exact canonical JSON bytes")
    if set(record) != _WRITER_LOCK_FIELDS:
        raise EvidenceError("writer lock fields do not match the schema")
    if (
        not isinstance(record["schema_version"], int)
        or isinstance(record["schema_version"], bool)
        or record["schema_version"] != WRITER_LOCK_SCHEMA_VERSION
    ):
        raise EvidenceError("writer lock schema_version is unsupported")
    if record["kind"] != "benchhandoff-writer-lock":
        raise EvidenceError("writer lock kind is invalid")
    if record["run_directory"] != str(run_path):
        raise EvidenceError("writer lock run_directory does not match the requested run")
    owner_pid = record["owner_pid"]
    if (
        not isinstance(owner_pid, int)
        or isinstance(owner_pid, bool)
        or owner_pid <= 0
    ):
        raise EvidenceError("writer lock owner_pid must be a positive integer")
    owner_token = record["owner_process_start_token"]
    if owner_token is not None and (
        not isinstance(owner_token, str)
        or not owner_token
        or len(owner_token.encode("utf-8")) > 512
    ):
        raise EvidenceError(
            "writer lock owner_process_start_token must be null or bounded text"
        )
    nonce = record["lock_nonce"]
    if not isinstance(nonce, str) or _NONCE_PATTERN.fullmatch(nonce) is None:
        raise EvidenceError("writer lock lock_nonce must be 32 lowercase hexadecimal characters")
    return record


def _read_writer_lock_snapshot(
    run_path: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any], bytes, tuple[int, int]]:
    path = writer_lock_path(run_path)
    object_key = _regular_object_key(path, label="writer lock")
    payload = read_regular_bytes(
        path,
        label="writer lock",
        max_bytes=WRITER_LOCK_MAX_BYTES,
    )
    record = _decode_writer_lock_record(payload, run_path=run_path)
    identity = _identity_from_payload(payload)
    current = file_identity(path, label="writer lock")
    if (
        not identities_match(identity, current)
        or _regular_object_key(path, label="writer lock") != object_key
    ):
        raise EvidenceError("writer lock changed during inspection")
    return path, record, identity, payload, object_key


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("writer lock write made no progress")
        view = view[written:]


def _windows_mutex_name(run_path: Path) -> str:
    canonical_path = os.path.normcase(str(run_path))
    digest = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()
    return f"Local\\BenchHandoffWriterLock-{digest}"


def _acquire_windows_mutex(run_path: Path) -> int | None:
    import ctypes
    from ctypes import wintypes

    wait_object_0 = 0x00000000
    wait_abandoned = 0x00000080
    wait_timeout = 0x00000102
    wait_failed = 0xFFFFFFFF

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_mutex(None, False, _windows_mutex_name(run_path))
    if not handle:
        raise EvidenceError(
            "unable to create writer-lock coordination mutex: "
            f"Windows error {ctypes.get_last_error()}"
        )
    wait_result = wait_for_single_object(handle, 0)
    if wait_result in {wait_object_0, wait_abandoned}:
        return int(handle)
    close_handle(handle)
    if wait_result == wait_timeout:
        return None
    if wait_result == wait_failed:
        raise EvidenceError(
            "unable to wait on writer-lock coordination mutex: "
            f"Windows error {ctypes.get_last_error()}"
        )
    raise EvidenceError(
        f"writer-lock coordination mutex returned unexpected wait result {wait_result}"
    )


def _release_windows_mutex(handle_value: int) -> None:
    import ctypes
    from ctypes import wintypes

    handle = wintypes.HANDLE(handle_value)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    release_mutex = kernel32.ReleaseMutex
    release_mutex.argtypes = (wintypes.HANDLE,)
    release_mutex.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    release_error: EvidenceError | None = None
    if not release_mutex(handle):
        release_error = EvidenceError(
            "unable to release writer-lock coordination mutex: "
            f"Windows error {ctypes.get_last_error()}"
        )
    if not close_handle(handle) and release_error is None:
        release_error = EvidenceError(
            "unable to close writer-lock coordination mutex: "
            f"Windows error {ctypes.get_last_error()}"
        )
    if release_error is not None:
        raise release_error


def _try_posix_flock(descriptor: int) -> bool:
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise EvidenceError(
            f"unable to acquire writer-lock coordination flock: {exc}"
        ) from exc
    return True


def _release_posix_flock(descriptor: int) -> None:
    import fcntl

    release_error: EvidenceError | None = None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError as exc:
        release_error = EvidenceError(
            f"unable to release writer-lock coordination flock: {exc}"
        )
    try:
        os.close(descriptor)
    except OSError as exc:
        if release_error is None:
            release_error = EvidenceError(
                f"unable to close writer-lock coordination descriptor: {exc}"
            )
    if release_error is not None:
        raise release_error


@dataclass
class _KernelGuard:
    kind: str
    value: int
    object_key: tuple[int, int] | None = None
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.kind == "windows-mutex":
            _release_windows_mutex(self.value)
            return
        if self.kind == "posix-flock":
            _release_posix_flock(self.value)
            return
        raise EvidenceError(f"unknown writer-lock coordination kind: {self.kind!r}")


def _open_writer_lock_readwrite(path: Path) -> tuple[int, tuple[int, int]]:
    object_key = _regular_object_key(path, label="writer lock")
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceError(f"unable to open writer lock for recovery: {exc}") from exc
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != object_key
    ):
        os.close(descriptor)
        raise BoundaryError(f"writer lock changed while opening: {path}")
    return descriptor, object_key


def _acquire_recovery_guard(run_path: Path, path: Path) -> _KernelGuard | None:
    if os.name == "nt":
        handle = _acquire_windows_mutex(run_path)
        if handle is None:
            return None
        return _KernelGuard(kind="windows-mutex", value=handle)
    if os.name == "posix":
        descriptor, object_key = _open_writer_lock_readwrite(path)
        if not _try_posix_flock(descriptor):
            os.close(descriptor)
            return None
        return _KernelGuard(
            kind="posix-flock",
            value=descriptor,
            object_key=object_key,
        )
    raise EvidenceError(
        f"writer-lock kernel coordination is unsupported on platform {os.name!r}"
    )


def _owner_observation(
    record: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    owner_pid = record["owner_pid"]
    recorded_token = record["owner_process_start_token"]
    first_liveness = process_liveness(owner_pid)
    if first_liveness == "unknown":
        return (
            {"liveness": "unknown", "process_start_token": None},
            "refuse",
            "owner-liveness-unknown",
        )
    if first_liveness == "dead":
        second_liveness = process_liveness(owner_pid)
        if second_liveness == "dead":
            return (
                {"liveness": "dead", "process_start_token": None},
                "recover-orphan",
                "owner-dead",
            )
        if second_liveness == "unknown":
            return (
                {"liveness": "unknown", "process_start_token": None},
                "refuse",
                "owner-liveness-unknown",
            )
        return (
            {"liveness": "unstable", "process_start_token": None},
            "refuse",
            "owner-observation-changed",
        )

    first_token = process_start_token(owner_pid)
    second_liveness = process_liveness(owner_pid)
    if second_liveness == "unknown":
        return (
            {"liveness": "unknown", "process_start_token": None},
            "refuse",
            "owner-liveness-unknown",
        )
    if second_liveness != "alive":
        return (
            {"liveness": "unstable", "process_start_token": None},
            "refuse",
            "owner-observation-changed",
        )
    second_token = process_start_token(owner_pid)
    if (
        not isinstance(first_token, str)
        or not first_token
        or not isinstance(second_token, str)
        or not second_token
    ):
        return (
            {"liveness": "alive", "process_start_token": None},
            "refuse",
            "owner-identity-unverifiable",
        )
    if first_token != second_token:
        return (
            {"liveness": "unstable", "process_start_token": None},
            "refuse",
            "owner-observation-changed",
        )
    observation = {
        "liveness": "alive",
        "process_start_token": first_token,
    }
    if not isinstance(recorded_token, str) or not recorded_token:
        return observation, "refuse", "owner-identity-unverifiable"
    if first_token == recorded_token:
        return observation, "refuse", "owner-alive"
    return observation, "recover-orphan", "owner-pid-reused"


def _build_recovery_decision(
    *,
    run_path: Path,
    path: Path,
    record: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, Any]:
    observation, action, reason = _owner_observation(record)
    body: dict[str, Any] = {
        "schema_version": WRITER_LOCK_RECOVERY_DECISION_SCHEMA_VERSION,
        "kind": "benchhandoff-writer-lock-recovery-decision",
        "run_directory": str(run_path),
        "lock_path": str(path),
        "lock_identity": identity,
        "lock_record": record,
        "owner_observation": observation,
        "action": action,
        "reason": reason,
        "tombstone_path": str(_recovery_tombstone_path(path, identity)),
    }
    body["decision_sha256"] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    return body


def inspect_writer_lock(run_directory: Path | str) -> dict[str, Any]:
    """Return a read-only, digest-bound orphan-recovery decision."""

    run_path = _normalized_run_path(run_directory)
    path, record, identity, _, _ = _read_writer_lock_snapshot(run_path)
    return _build_recovery_decision(
        run_path=run_path,
        path=path,
        record=record,
        identity=identity,
    )


def recover_writer_lock(
    run_directory: Path | str,
    *,
    expected_decision_sha256: str,
) -> dict[str, Any]:
    """Archive and unlink only an exactly inspected, provably orphaned lock."""

    if (
        not isinstance(expected_decision_sha256, str)
        or _SHA256_PATTERN.fullmatch(expected_decision_sha256) is None
    ):
        raise EvidenceError(
            "expected writer-lock recovery decision SHA-256 must be "
            "64 lowercase hexadecimal characters"
        )
    run_path = _normalized_run_path(run_directory)
    decision = inspect_writer_lock(run_path)
    actual_decision = decision["decision_sha256"]
    if actual_decision != expected_decision_sha256:
        raise EvidenceError(
            "writer-lock recovery decision is stale: "
            f"expected {expected_decision_sha256}, got {actual_decision}"
        )
    if decision["action"] != "recover-orphan":
        raise EvidenceError(
            f"writer lock is not recoverable: {decision['reason']}"
        )

    path = writer_lock_path(run_path)
    guard = _acquire_recovery_guard(run_path, path)
    if guard is None:
        raise EvidenceError(
            "writer-lock coordination kernel lock is active; refusing recovery"
        )
    try:
        path, record, identity, payload, object_key = _read_writer_lock_snapshot(
            run_path
        )
        if guard.object_key is not None and guard.object_key != object_key:
            raise EvidenceError(
                "writer lock changed identity while acquiring the kernel guard"
            )
        current_decision = _build_recovery_decision(
            run_path=run_path,
            path=path,
            record=record,
            identity=identity,
        )
        if current_decision["decision_sha256"] != expected_decision_sha256:
            raise EvidenceError(
                "writer-lock recovery decision became stale before mutation"
            )
        if current_decision["action"] != "recover-orphan":
            raise EvidenceError(
                f"writer lock is not recoverable: {current_decision['reason']}"
            )

        tombstone = Path(current_decision["tombstone_path"])
        source_metadata = path.lstat()
        if (source_metadata.st_dev, source_metadata.st_ino) != object_key:
            raise EvidenceError("writer lock changed identity before recovery")
        if os.path.lexists(tombstone):
            tombstone_key = _regular_object_key(
                tombstone,
                label="writer-lock recovery tombstone",
            )
            try:
                same_file = os.path.samefile(path, tombstone)
            except OSError as exc:
                raise EvidenceError(
                    f"unable to compare writer lock and tombstone: {exc}"
                ) from exc
            if (
                not same_file
                or tombstone_key != object_key
                or source_metadata.st_nlink != 2
            ):
                raise EvidenceError(
                    "writer-lock tombstone does not preserve the exact source file"
                )
        else:
            if source_metadata.st_nlink != 1:
                raise EvidenceError(
                    "writer lock has an unexpected hard-link count before recovery"
                )
            try:
                os.link(path, tombstone, follow_symlinks=False)
            except FileExistsError:
                pass
            except OSError as exc:
                raise EvidenceError(
                    f"unable to create writer-lock recovery tombstone: {exc}"
                ) from exc

        if (
            _regular_object_key(path, label="writer lock") != object_key
            or _regular_object_key(
                tombstone,
                label="writer-lock recovery tombstone",
            )
            != object_key
            or path.stat().st_nlink != 2
        ):
            raise EvidenceError(
                "writer-lock tombstone does not preserve the exact source file"
            )
        current_payload = read_regular_bytes(
            path,
            label="writer lock before recovery unlink",
            max_bytes=WRITER_LOCK_MAX_BYTES,
        )
        if current_payload != payload:
            raise EvidenceError("writer lock changed after tombstone creation")
        tombstone_identity = file_identity(
            tombstone,
            label="writer-lock recovery tombstone",
        )
        if not identities_match(tombstone_identity, identity):
            raise EvidenceError("writer-lock recovery tombstone identity changed")

        try:
            path.unlink()
            _sync_directory(path.parent)
        except OSError as exc:
            raise EvidenceError(
                f"unable to unlink archived writer lock: {path}: {exc}"
            ) from exc

        if os.path.lexists(path):
            recreated_key = _regular_object_key(
                path,
                label="writer-lock source path after recovery",
            )
            if recreated_key == object_key or os.name == "nt":
                raise EvidenceError(
                    "writer-lock source path was recreated before recovery completed"
                )
        final_tombstone_identity = file_identity(
            tombstone,
            label="recovered writer-lock tombstone",
        )
        if not identities_match(final_tombstone_identity, identity):
            raise EvidenceError("recovered writer-lock tombstone identity changed")
        if tombstone.stat().st_nlink != 1:
            raise EvidenceError(
                "recovered writer-lock tombstone has an unexpected hard-link count"
            )
        return {
            "schema_version": WRITER_LOCK_RECOVERY_RESULT_SCHEMA_VERSION,
            "kind": "benchhandoff-writer-lock-recovery",
            "status": "recovered",
            "reason": current_decision["reason"],
            "run_directory": str(run_path),
            "decision_sha256": expected_decision_sha256,
            "lock_identity": identity,
            "tombstone": {
                "path": str(tombstone),
                "identity": final_tombstone_identity,
            },
        }
    finally:
        guard.close()


@dataclass
class WriterLock:
    """One owned lock file plus an automatically released kernel guard."""

    path: Path
    record: dict[str, Any]
    identity: dict[str, Any]
    _object_key: tuple[int, int]
    _guard: _KernelGuard | None
    _released: bool = False

    @classmethod
    def acquire(cls, run_directory: Path | str) -> "WriterLock":
        """Acquire the cooperative writer lock without overwriting any file."""

        run_path = _normalized_run_path(run_directory)
        path = writer_lock_path(run_path)
        guard: _KernelGuard | None = None
        descriptor: int | None = None
        created = False
        created_object_key: tuple[int, int] | None = None

        if os.name == "nt":
            handle = _acquire_windows_mutex(run_path)
            if handle is None:
                if os.path.lexists(path):
                    raise EvidenceError(
                        f"writer lock already exists for run directory: {path}; "
                        "another writer may be active or a prior writer may have "
                        "stopped without releasing it"
                    )
                raise EvidenceError(
                    "writer-lock coordination mutex is active before lock publication"
                )
            guard = _KernelGuard(kind="windows-mutex", value=handle)
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
            )
        elif os.name == "posix":
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        else:
            raise EvidenceError(
                f"writer-lock kernel coordination is unsupported on platform {os.name!r}"
            )

        record = _owner_record(run_path)
        payload = canonical_json_bytes(record)
        expected_identity = _identity_from_payload(payload)
        try:
            try:
                descriptor = os.open(path, flags, 0o600)
                created = True
                opened = os.fstat(descriptor)
                created_object_key = (opened.st_dev, opened.st_ino)
            except FileExistsError as exc:
                raise EvidenceError(
                    f"writer lock already exists for run directory: {path}; "
                    "another writer may be active or a prior writer may have stopped "
                    "without releasing it"
                ) from exc
            except OSError as exc:
                raise EvidenceError(
                    f"unable to create writer lock {path}: {exc}"
                ) from exc

            if os.name == "posix":
                if not _try_posix_flock(descriptor):
                    raise EvidenceError(
                        "new writer lock unexpectedly conflicted at the kernel boundary"
                    )
                guard = _KernelGuard(
                    kind="posix-flock",
                    value=descriptor,
                    object_key=created_object_key,
                )
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            if os.name == "nt":
                os.close(descriptor)
                descriptor = None
            _sync_directory(path.parent)
            object_key = _regular_object_key(path, label="writer lock")
            if object_key != created_object_key:
                raise EvidenceError("writer lock changed identity during acquisition")
            identity = file_identity(path, label="writer lock")
            if not identities_match(identity, expected_identity):
                raise EvidenceError("writer lock changed during acquisition")
            return cls(
                path=path,
                record=record,
                identity=identity,
                _object_key=object_key,
                _guard=guard,
            )
        except Exception:
            if descriptor is not None and (
                guard is None or guard.kind == "windows-mutex"
            ):
                os.close(descriptor)
                descriptor = None
            if created and created_object_key is not None:
                try:
                    current_key = _regular_object_key(
                        path,
                        label="writer lock after failed acquisition",
                    )
                except Exception:
                    current_key = None
                if current_key == created_object_key:
                    try:
                        path.unlink()
                        _sync_directory(path.parent)
                    except OSError:
                        pass
            if guard is not None:
                guard.close()
            elif descriptor is not None:
                os.close(descriptor)
            raise

    def release(self) -> None:
        """Release only the exact lock file object and bytes this instance owns."""

        if self._released:
            return
        if self._guard is None:
            raise EvidenceError("writer-lock coordination ownership is unavailable")
        guard = self._guard
        try:
            if (
                _regular_object_key(self.path, label="writer lock")
                != self._object_key
            ):
                raise EvidenceError(
                    f"writer lock changed identity before release: {self.path}"
                )
            current = file_identity(self.path, label="writer lock")
            if not identities_match(current, self.identity):
                raise EvidenceError(
                    f"writer lock changed before release; refusing to remove it: {self.path}"
                )
            try:
                self.path.unlink()
                _sync_directory(self.path.parent)
            except OSError as exc:
                raise EvidenceError(
                    f"unable to release writer lock {self.path}: {exc}"
                ) from exc
            self._released = True
        finally:
            self._guard = None
            guard.close()
