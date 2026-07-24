"""Filesystem boundary checks and durable evidence-file operations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from benchhandoff.errors import BoundaryError, EvidenceError

_MAX_WINDOWS_COMPONENT_UTF8_BYTES = 240
_MAX_PORTABLE_PATH_UTF8_BYTES = 1024
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 100_000
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    "conin$",
    "conout$",
    "clock$",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    *(f"com{number}" for number in ("\u00b9", "\u00b2", "\u00b3")),
    *(f"lpt{number}" for number in ("\u00b9", "\u00b2", "\u00b3")),
}


def windows_component_key(value: str, *, label: str) -> str:
    """Return a conservative Windows alias key for one portable component."""

    if not isinstance(value, str) or not value:
        raise BoundaryError(f"{label} must be a non-empty string")
    invalid = next(
        (
            character
            for character in value
            if character in _WINDOWS_INVALID_CHARACTERS or ord(character) < 32
        ),
        None,
    )
    if invalid is not None:
        raise BoundaryError(
            f"{label} contains a character forbidden in portable Windows paths: "
            f"U+{ord(invalid):04X}"
        )
    if value.endswith((" ", ".")):
        raise BoundaryError(f"{label} must not end with a space or dot: {value!r}")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise BoundaryError(f"{label} is not valid UTF-8 text") from exc
    if encoded_length > _MAX_WINDOWS_COMPONENT_UTF8_BYTES:
        raise BoundaryError(
            f"{label} exceeds the conservative "
            f"{_MAX_WINDOWS_COMPONENT_UTF8_BYTES}-byte UTF-8 component limit"
        )
    normalized = unicodedata.normalize("NFC", value)
    device_stem = normalized.split(".", 1)[0].casefold()
    if device_stem in _WINDOWS_RESERVED_NAMES:
        raise BoundaryError(f"{label} uses a reserved Windows device name: {value!r}")
    return normalized.casefold()


def windows_path_key(value: str, *, label: str) -> tuple[str, ...]:
    """Return the case-insensitive alias key for a normalized portable path."""

    normalized = normalize_relative_file(value, label=label)
    return tuple(
        windows_component_key(part, label=f"{label} component")
        for part in normalized.split("/")
    )


def utc_now() -> str:
    """Return a stable, timezone-explicit timestamp."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically for hashing and durable writes."""

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise EvidenceError(
            f"value cannot be represented as strict canonical JSON: {exc}"
        ) from exc
    return (text + "\n").encode("utf-8")


def normalize_relative_file(value: str, *, label: str) -> str:
    """Validate a portable, relative file path without normalizing ambiguity."""

    if not isinstance(value, str) or not value:
        raise BoundaryError(f"{label} must be a non-empty string")
    if "\x00" in value:
        raise BoundaryError(f"{label} contains a NUL byte")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise BoundaryError(f"{label} is not valid UTF-8 text") from exc
    if encoded_length > _MAX_PORTABLE_PATH_UTF8_BYTES:
        raise BoundaryError(
            f"{label} exceeds the conservative "
            f"{_MAX_PORTABLE_PATH_UTF8_BYTES}-byte UTF-8 path limit"
        )
    if "\\" in value:
        raise BoundaryError(f"{label} must use '/' as its separator: {value!r}")
    if ":" in value:
        raise BoundaryError(f"{label} must not contain ':': {value!r}")

    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise BoundaryError(
            f"{label} must be normalized and must not contain empty, '.' or '..' components: {value!r}"
        )
    for index, part in enumerate(raw_parts):
        windows_component_key(part, label=f"{label} component[{index}]")

    parsed = PurePosixPath(value)
    if parsed.is_absolute() or str(parsed) != value:
        raise BoundaryError(f"{label} must be a normalized relative path: {value!r}")
    return value


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def checked_directory(path: Path | str, *, label: str) -> Path:
    """Return a resolved directory after rejecting a final symlink."""

    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except FileNotFoundError as exc:
        raise BoundaryError(f"{label} does not exist: {candidate}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise BoundaryError(f"{label} must not be a symlink: {candidate}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise BoundaryError(f"{label} must be a directory: {candidate}")
    return candidate.resolve(strict=True)


def resolve_member(root: Path | str, relative: str, *, label: str) -> Path:
    """Resolve a portable relative path while rejecting symlink traversal."""

    normalized = normalize_relative_file(relative, label=label)
    resolved_root = checked_directory(root, label=f"{label} root")
    candidate = resolved_root.joinpath(*normalized.split("/"))

    current = resolved_root
    for part in normalized.split("/"):
        current = current / part
        if not os.path.lexists(current):
            continue
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise BoundaryError(f"{label} crosses a symlink: {current}")
        if current != candidate and not stat.S_ISDIR(metadata.st_mode):
            raise BoundaryError(f"{label} has a non-directory parent: {current}")

    resolved_candidate = candidate.resolve(strict=False)
    if not _is_within(resolved_candidate, resolved_root):
        raise BoundaryError(f"{label} escapes its root: {relative!r}")
    return candidate


def _open_regular_readonly(path: Path, *, label: str) -> int:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise BoundaryError(f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise BoundaryError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise BoundaryError(f"{label} must be a regular file: {path}")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BoundaryError(f"unable to open {label} as a regular file: {path}: {exc}") from exc

    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        os.close(descriptor)
        raise BoundaryError(f"{label} changed type while opening: {path}")
    if hasattr(metadata, "st_ino") and (metadata.st_ino, metadata.st_dev) != (
        opened.st_ino,
        opened.st_dev,
    ):
        os.close(descriptor)
        raise BoundaryError(f"{label} changed identity while opening: {path}")
    return descriptor


def read_regular_bytes(
    path: Path | str,
    *,
    label: str,
    max_bytes: int | None = None,
) -> bytes:
    """Read one ordinary file through a checked descriptor."""

    if max_bytes is not None and (
        not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0
    ):
        raise ValueError("max_bytes must be a non-negative integer or None")
    candidate = Path(path)
    descriptor = _open_regular_readonly(candidate, label=label)
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        if max_bytes is None:
            return handle.read()
        opened = os.fstat(handle.fileno())
        if opened.st_size > max_bytes:
            raise EvidenceError(f"{label} exceeds the {max_bytes}-byte size limit")
        payload = handle.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise EvidenceError(f"{label} exceeds the {max_bytes}-byte size limit")
        return payload


def file_identity(path: Path | str, *, label: str) -> dict[str, Any]:
    """Hash one regular file and return its content identity."""

    candidate = Path(path)
    descriptor = _open_regular_readonly(candidate, label=label)
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return {"sha256": digest.hexdigest(), "size": size}


def member_identity(root: Path | str, relative: str, *, label: str) -> dict[str, Any]:
    """Hash one declared member after containment checks."""

    return file_identity(resolve_member(root, relative, label=label), label=label)


def identities_match(left: Any, right: Any) -> bool:
    """Compare only the immutable fields of two identity records."""

    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return left.get("sha256") == right.get("sha256") and left.get("size") == right.get("size")


def ensure_member_absent(root: Path | str, relative: str, *, label: str) -> Path:
    """Require an output path to be absent before a subprocess starts."""

    candidate = resolve_member(root, relative, label=label)
    if os.path.lexists(candidate):
        raise BoundaryError(f"{label} must start absent: {candidate}")
    return candidate


def ensure_output_parent_boundary(root: Path | str, relative: str, *, label: str) -> None:
    """Validate all currently existing parents of a not-yet-created output."""

    candidate = resolve_member(root, relative, label=label)
    current = candidate.parent
    resolved_root = checked_directory(root, label=f"{label} root")
    while current != resolved_root:
        if os.path.lexists(current):
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise BoundaryError(f"{label} has an unsafe parent: {current}")
        current = current.parent


def prepare_new_directory(path: Path | str, *, label: str) -> Path:
    """Create one new private directory after validating its existing parent."""

    candidate = Path(path).absolute()
    if os.path.lexists(candidate):
        raise BoundaryError(f"{label} must start absent: {candidate}")
    parent = checked_directory(candidate.parent, label=f"{label} parent")
    candidate = parent / candidate.name
    candidate.mkdir(mode=0o700)
    return checked_directory(candidate, label=label)


def require_separate_trees(first: Path, second: Path, *, labels: tuple[str, str]) -> None:
    """Reject nested roots so a task cannot overwrite its own ledger."""

    first_resolved = first.resolve(strict=False)
    second_resolved = second.resolve(strict=False)
    if _is_within(first_resolved, second_resolved) or _is_within(second_resolved, first_resolved):
        raise BoundaryError(f"{labels[0]} and {labels[1]} must be separate directory trees")


def nearest_existing_directory(path: Path | str, *, label: str) -> Path:
    """Resolve the closest existing directory at or above a future path."""

    current = Path(path).absolute()
    while not os.path.lexists(current):
        parent = current.parent
        if parent == current:
            raise BoundaryError(f"{label} has no existing parent directory: {path}")
        current = parent
    return checked_directory(current, label=label)


def require_same_filesystem(
    first: Path | str,
    second: Path | str,
    *,
    labels: tuple[str, str],
) -> None:
    """Require two existing directories to support an atomic rename between them."""

    first_directory = checked_directory(first, label=labels[0])
    second_directory = checked_directory(second, label=labels[1])
    try:
        first_device = first_directory.stat().st_dev
        second_device = second_directory.stat().st_dev
    except OSError as exc:
        raise BoundaryError(
            f"unable to compare filesystems for {labels[0]} and {labels[1]}: {exc}"
        ) from exc
    if first_device != second_device:
        raise BoundaryError(
            f"{labels[0]} and {labels[1]} must be on the same filesystem for atomic quarantine"
        )


def _fsync_directory(path: Path) -> None:
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


def move_regular_same_filesystem(
    source: Path | str,
    destination: Path | str,
    *,
    label: str,
) -> dict[str, Any]:
    """Atomically move one regular file on one filesystem and recheck its identity."""

    source_candidate = Path(source)
    destination_candidate = Path(destination)
    source_parent = checked_directory(
        source_candidate.parent,
        label=f"source parent for {label}",
    )
    destination_parent = checked_directory(
        destination_candidate.parent,
        label=f"destination parent for {label}",
    )
    source_candidate = source_parent / source_candidate.name
    destination_candidate = destination_parent / destination_candidate.name
    if os.path.lexists(destination_candidate):
        raise BoundaryError(f"{label} destination must start absent: {destination_candidate}")

    try:
        source_device = source_candidate.lstat().st_dev
        destination_device = destination_parent.stat().st_dev
    except FileNotFoundError as exc:
        raise BoundaryError(f"{label} source does not exist: {source_candidate}") from exc
    except OSError as exc:
        raise BoundaryError(f"unable to inspect filesystem boundary for {label}: {exc}") from exc
    if source_device != destination_device:
        raise BoundaryError(
            f"{label} source and destination must be on the same filesystem"
        )

    expected = file_identity(source_candidate, label=f"{label} source")
    try:
        os.replace(source_candidate, destination_candidate)
    except OSError as exc:
        raise EvidenceError(f"unable to atomically move {label}: {exc}") from exc

    actual = file_identity(destination_candidate, label=f"{label} destination")
    if not identities_match(actual, expected):
        raise EvidenceError(f"{label} identity changed during atomic move")
    if os.name != "nt":
        _fsync_directory(source_parent)
        if destination_parent != source_parent:
            _fsync_directory(destination_parent)
    return actual


def atomic_write_bytes(path: Path | str, payload: bytes) -> None:
    """Write bytes through a same-directory temporary file and atomic replace."""

    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    destination = Path(path)
    parent = checked_directory(destination.parent, label=f"parent of {destination.name}")
    if os.path.lexists(destination):
        file_identity(destination, label=f"existing {destination.name}")

    temporary = parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _fsync_directory(parent)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def atomic_write_json(path: Path | str, value: Any) -> None:
    """Write reader-compatible JSON through an atomic same-directory replace."""

    _validate_json_complexity(value, label="JSON output")
    payload = canonical_json_bytes(value)
    if len(payload) > _MAX_JSON_BYTES:
        raise EvidenceError(
            f"JSON output exceeds the {_MAX_JSON_BYTES}-byte reader limit"
        )
    atomic_write_bytes(path, payload)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceError(f"JSON object contains duplicate key: {key!r}")
        value[key] = item
    return value


def _reject_nonfinite_json(value: str) -> Any:
    raise EvidenceError(f"JSON contains non-finite number: {value}")


def _validate_json_complexity(value: Any, *, label: str) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_JSON_DEPTH:
            raise EvidenceError(
                f"{label} exceeds the maximum JSON depth of {_MAX_JSON_DEPTH}"
            )
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise EvidenceError(
                f"{label} exceeds the maximum JSON node count of {_MAX_JSON_NODES}"
            )
        if isinstance(current, dict):
            nodes += len(current)
            if nodes > _MAX_JSON_NODES:
                raise EvidenceError(
                    f"{label} exceeds the maximum JSON node count of {_MAX_JSON_NODES}"
                )
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise EvidenceError(f"{label} contains a non-finite JSON number")


def read_json_file(path: Path | str, *, label: str) -> Any:
    """Read strict UTF-8 JSON from one checked regular file."""

    raw = read_regular_bytes(path, label=label, max_bytes=_MAX_JSON_BYTES)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_nonfinite_json,
        )
    except EvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvidenceError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    except (RecursionError, OverflowError) as exc:
        raise EvidenceError(f"{label} exceeds safe JSON parser limits: {exc}") from exc
    _validate_json_complexity(value, label=label)
    return value


def create_empty_regular(path: Path | str, *, label: str) -> None:
    """Create one empty evidence file without overwriting anything."""

    destination = Path(path)
    checked_directory(destination.parent, label=f"parent of {label}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(destination, flags, 0o600)
    os.close(descriptor)


def iter_regular_artifacts(root: Path, directories: Iterable[str]) -> list[str]:
    """List regular files beneath evidence directories without following links."""

    resolved_root = checked_directory(root, label="run directory")
    collected: list[str] = []
    for directory_name in directories:
        start = resolve_member(resolved_root, directory_name, label="artifact directory")
        start_checked = checked_directory(start, label=f"artifact directory {directory_name}")
        stack = [start_checked]
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    entry_path = Path(entry.path)
                    if entry.is_symlink():
                        raise EvidenceError(f"artifact tree contains a symlink: {entry_path}")
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry_path)
                    elif entry.is_file(follow_symlinks=False):
                        relative = entry_path.relative_to(resolved_root).as_posix()
                        normalize_relative_file(relative, label="artifact path")
                        collected.append(relative)
                    else:
                        raise EvidenceError(f"artifact tree contains a non-regular entry: {entry_path}")
    return sorted(collected)
