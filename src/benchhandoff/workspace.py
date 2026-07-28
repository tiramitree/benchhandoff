"""Closed-world observation for one explicitly reviewed workspace tree."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchhandoff.errors import BoundaryError, EvidenceError
from benchhandoff.storage import (
    canonical_json_bytes,
    checked_directory,
    file_identity,
    identities_match,
    normalize_relative_file,
    read_regular_bytes,
    windows_component_key,
    windows_path_key,
)

WORKSPACE_POLICY = "closed-world-primary-stream-v1"
WORKSPACE_MANIFEST_KIND = "benchhandoff-workspace-snapshot"
WORKSPACE_TREE_KIND = "benchhandoff-workspace-tree"
WORKSPACE_MANIFEST_SCHEMA_VERSION = 1
MAX_WORKSPACE_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_WORKSPACE_ENTRIES = 10_000
MAX_WORKSPACE_DEPTH = 32
MAX_WORKSPACE_FILE_BYTES = 1024 * 1024 * 1024
MAX_WORKSPACE_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True)
class WorkspaceManifest:
    """One canonical manifest and its derived baseline observation."""

    entries: tuple[dict[str, Any], ...]
    identity: dict[str, Any]
    summary: dict[str, Any]


@dataclass(frozen=True)
class WorkspaceObservation:
    """One stable two-pass tree observation."""

    entries: tuple[dict[str, Any], ...]
    summary: dict[str, Any]


class WorkspaceVerificationError(EvidenceError):
    """A stable workspace observation did not match its bound expectation."""

    def __init__(
        self,
        message: str,
        *,
        observation: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.observation = observation


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def checked_workspace_root(path: Path | str) -> Path:
    """Resolve a workspace only after rejecting linked path components."""

    candidate = Path(os.path.abspath(path))
    if not candidate.anchor:
        raise BoundaryError("workspace root must be an absolute local path")
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise BoundaryError("workspace root path is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise BoundaryError(
                "workspace root path must not cross a link or reparse point"
            )
        if current != candidate and not stat.S_ISDIR(metadata.st_mode):
            raise BoundaryError("workspace root has a non-directory parent")
    try:
        final_metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise BoundaryError("workspace root is unavailable") from exc
    if not stat.S_ISDIR(final_metadata.st_mode):
        raise BoundaryError("workspace root must be a directory")
    if stat.S_ISLNK(final_metadata.st_mode) or _is_reparse(final_metadata):
        raise BoundaryError("workspace root must not be a link or reparse point")
    if os.path.normcase(os.path.normpath(str(resolved))) != os.path.normcase(
        os.path.normpath(str(candidate))
    ):
        raise BoundaryError("workspace root changed identity while resolving")
    return resolved


def _canonical_path(value: str, *, label: str) -> str:
    normalized = normalize_relative_file(value, label=label)
    if unicodedata.normalize("NFC", normalized) != normalized:
        raise EvidenceError(f"{label} must use Unicode NFC")
    if len(normalized.split("/")) > MAX_WORKSPACE_DEPTH:
        raise EvidenceError(
            f"{label} exceeds the {MAX_WORKSPACE_DEPTH}-component depth limit"
        )
    return normalized


def _entry_sort_key(entry: dict[str, Any]) -> bytes:
    return entry["path"].encode("utf-8")


def _tree_summary(entries: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    body = {
        "schema_version": WORKSPACE_MANIFEST_SCHEMA_VERSION,
        "kind": WORKSPACE_TREE_KIND,
        "entries": list(entries),
    }
    directories = sum(entry["kind"] == "directory" for entry in entries)
    files = len(entries) - directories
    total_bytes = sum(
        entry["size"] for entry in entries if entry["kind"] == "file"
    )
    return {
        "tree_sha256": hashlib.sha256(canonical_json_bytes(body)).hexdigest(),
        "directory_count": directories,
        "file_count": files,
        "total_bytes": total_bytes,
    }


def _object_key(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _checked_directory_metadata(
    path: Path,
    *,
    root_device: int,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise EvidenceError("workspace directory is unavailable") from exc
    if metadata.st_dev != root_device:
        raise EvidenceError("workspace directory crosses a filesystem boundary")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
    ):
        raise EvidenceError("workspace directory changed type during observation")
    return metadata


def _bounded_file_identity(
    path: Path,
    *,
    metadata: os.stat_result,
    relative: str,
    root_device: int,
) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != root_device
            or _object_key(opened) != _object_key(metadata)
            or getattr(opened, "st_nlink", 1) != 1
        ):
            raise EvidenceError(
                f"workspace file changed identity while opening: {relative!r}"
            )
        if opened.st_size > MAX_WORKSPACE_FILE_BYTES:
            raise EvidenceError(
                f"workspace file exceeds the {MAX_WORKSPACE_FILE_BYTES}-byte "
                f"limit: {relative!r}"
            )

        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            while True:
                remaining = MAX_WORKSPACE_FILE_BYTES - size + 1
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                size += len(block)
                if size > MAX_WORKSPACE_FILE_BYTES:
                    raise EvidenceError(
                        f"workspace file exceeds the {MAX_WORKSPACE_FILE_BYTES}-byte "
                        f"limit: {relative!r}"
                    )
                digest.update(block)
            final_opened = os.fstat(handle.fileno())
        current = path.lstat()
        if (
            _object_key(final_opened) != _object_key(opened)
            or final_opened.st_size != size
            or getattr(final_opened, "st_nlink", 1) != 1
            or not stat.S_ISREG(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or _is_reparse(current)
            or _object_key(current) != _object_key(opened)
            or getattr(current, "st_nlink", 1) != 1
        ):
            raise EvidenceError(
                f"workspace file changed during hashing: {relative!r}"
            )
        return {"sha256": digest.hexdigest(), "size": size}
    except EvidenceError:
        raise
    except OSError as exc:
        raise EvidenceError(
            f"unable to hash workspace file: {relative!r}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _scan_workspace_once(
    root: Path,
    *,
    root_device: int,
    root_key: tuple[int, int],
) -> WorkspaceObservation:
    entries: list[dict[str, Any]] = []
    aliases: dict[tuple[str, ...], str] = {}
    total_bytes = 0
    stack = [(root, root_key)]
    while stack:
        directory, expected_directory_key = stack.pop()
        before = _checked_directory_metadata(directory, root_device=root_device)
        if _object_key(before) != expected_directory_key:
            raise EvidenceError("workspace directory identity changed before scanning")
        children: list[os.DirEntry[str]] = []
        try:
            with os.scandir(directory) as iterator:
                opened = _checked_directory_metadata(
                    directory,
                    root_device=root_device,
                )
                if _object_key(opened) != expected_directory_key:
                    raise EvidenceError(
                        "workspace directory identity changed while opening"
                    )
                remaining_entries = MAX_WORKSPACE_ENTRIES - len(entries)
                for child in iterator:
                    try:
                        child.name.encode("utf-8")
                    except UnicodeEncodeError as exc:
                        raise EvidenceError(
                            "workspace entry name is not valid UTF-8"
                        ) from exc
                    if len(children) >= remaining_entries:
                        raise EvidenceError(
                            f"workspace exceeds the {MAX_WORKSPACE_ENTRIES}-entry limit"
                        )
                    children.append(child)
            after = _checked_directory_metadata(directory, root_device=root_device)
            if _object_key(after) != expected_directory_key:
                raise EvidenceError(
                    "workspace directory identity changed during scanning"
                )
            children.sort(
                key=lambda entry: entry.name.encode("utf-8"),
                reverse=True,
            )
        except EvidenceError:
            raise
        except OSError as exc:
            raise EvidenceError("unable to scan workspace directory") from exc
        for child in children:
            path = Path(child.path)
            relative = _canonical_path(
                path.relative_to(root).as_posix(),
                label="workspace entry path",
            )
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise EvidenceError(
                    f"unable to inspect workspace entry: {relative!r}"
                ) from exc
            alias = windows_path_key(relative, label="workspace entry path")
            previous = aliases.get(alias)
            if previous is not None and previous != relative:
                raise EvidenceError(
                    "workspace contains Windows-aliasing paths: "
                    f"{previous!r} and {relative!r}"
                )
            aliases[alias] = relative
            if metadata.st_dev != root_device:
                raise EvidenceError(
                    f"workspace entry crosses a filesystem boundary: {relative!r}"
                )
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                raise EvidenceError(
                    f"workspace entry must not be a link or reparse point: {relative!r}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                entries.append({"path": relative, "kind": "directory"})
                stack.append((path, _object_key(metadata)))
            elif stat.S_ISREG(metadata.st_mode):
                if getattr(metadata, "st_nlink", 1) != 1:
                    raise EvidenceError(
                        f"workspace file must not be hard-linked: {relative!r}"
                    )
                if metadata.st_size > MAX_WORKSPACE_FILE_BYTES:
                    raise EvidenceError(
                        f"workspace file exceeds the {MAX_WORKSPACE_FILE_BYTES}-byte "
                        f"limit: {relative!r}"
                    )
                identity = _bounded_file_identity(
                    path,
                    metadata=metadata,
                    relative=relative,
                    root_device=root_device,
                )
                total_bytes += identity["size"]
                if total_bytes > MAX_WORKSPACE_TOTAL_BYTES:
                    raise EvidenceError(
                        "workspace exceeds the "
                        f"{MAX_WORKSPACE_TOTAL_BYTES}-byte total limit"
                    )
                entries.append({"path": relative, "kind": "file", **identity})
            else:
                raise EvidenceError(
                    f"workspace contains a non-directory, non-regular entry: {relative!r}"
                )
    ordered = tuple(sorted(entries, key=_entry_sort_key))
    return WorkspaceObservation(entries=ordered, summary=_tree_summary(ordered))


def observe_workspace(path: Path | str) -> WorkspaceObservation:
    """Hash a bounded workspace twice and fail if the observations differ."""

    root = checked_workspace_root(path)
    root_metadata = root.lstat()
    root_device = root_metadata.st_dev
    root_key = _object_key(root_metadata)
    first = _scan_workspace_once(root, root_device=root_device, root_key=root_key)
    second = _scan_workspace_once(root, root_device=root_device, root_key=root_key)
    if first.entries != second.entries or first.summary != second.summary:
        raise EvidenceError("workspace observation was unstable between two scans")
    return second


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceError(f"workspace manifest contains duplicate key: {key!r}")
        value[key] = item
    return value


def _reject_constant(value: str) -> Any:
    raise EvidenceError(f"workspace manifest contains a non-finite number: {value}")


def _validate_manifest_value(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "kind",
        "policy",
        "entries",
    }:
        raise EvidenceError("workspace manifest root fields are invalid")
    if (
        not isinstance(value["schema_version"], int)
        or isinstance(value["schema_version"], bool)
        or value["schema_version"] != WORKSPACE_MANIFEST_SCHEMA_VERSION
    ):
        raise EvidenceError("workspace manifest schema_version is unsupported")
    if value["kind"] != WORKSPACE_MANIFEST_KIND:
        raise EvidenceError("workspace manifest kind is unsupported")
    if value["policy"] != WORKSPACE_POLICY:
        raise EvidenceError("workspace manifest policy is unsupported")
    raw_entries = value["entries"]
    if not isinstance(raw_entries, list):
        raise EvidenceError("workspace manifest entries must be an array")
    if len(raw_entries) > MAX_WORKSPACE_ENTRIES:
        raise EvidenceError(
            f"workspace manifest exceeds the {MAX_WORKSPACE_ENTRIES}-entry limit"
        )

    entries: list[dict[str, Any]] = []
    paths: set[str] = set()
    aliases: dict[tuple[str, ...], str] = {}
    directories: set[str] = set()
    files: set[str] = set()
    total_bytes = 0
    for index, raw_entry in enumerate(raw_entries):
        label = f"workspace manifest entry[{index}]"
        if not isinstance(raw_entry, dict):
            raise EvidenceError(f"{label} must be an object")
        kind = raw_entry.get("kind")
        expected_keys = (
            {"path", "kind"}
            if kind == "directory"
            else {"path", "kind", "sha256", "size"}
        )
        if set(raw_entry) != expected_keys or kind not in {"directory", "file"}:
            raise EvidenceError(f"{label} fields or kind are invalid")
        path = _canonical_path(raw_entry.get("path"), label=f"{label}.path")
        if path in paths:
            raise EvidenceError(f"workspace manifest contains duplicate path: {path!r}")
        alias = windows_path_key(path, label=f"{label}.path")
        previous = aliases.get(alias)
        if previous is not None and previous != path:
            raise EvidenceError(
                "workspace manifest contains Windows-aliasing paths: "
                f"{previous!r} and {path!r}"
            )
        aliases[alias] = path
        paths.add(path)
        if kind == "directory":
            directories.add(path)
            entries.append({"path": path, "kind": "directory"})
            continue
        sha256 = raw_entry.get("sha256")
        size = raw_entry.get("size")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise EvidenceError(f"{label}.sha256 is invalid")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_WORKSPACE_FILE_BYTES
        ):
            raise EvidenceError(f"{label}.size is invalid")
        total_bytes += size
        if total_bytes > MAX_WORKSPACE_TOTAL_BYTES:
            raise EvidenceError(
                "workspace manifest exceeds the "
                f"{MAX_WORKSPACE_TOTAL_BYTES}-byte total limit"
            )
        files.add(path)
        entries.append(
            {"path": path, "kind": "file", "sha256": sha256, "size": size}
        )

    ordered = tuple(sorted(entries, key=_entry_sort_key))
    if tuple(entries) != ordered:
        raise EvidenceError("workspace manifest entries are not in canonical UTF-8 order")
    for path in paths:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if parent in files:
                raise EvidenceError(
                    f"workspace manifest file is an ancestor of {path!r}: {parent!r}"
                )
            if parent not in directories:
                raise EvidenceError(
                    f"workspace manifest omits parent directory {parent!r}"
                )
    return ordered


def load_workspace_manifest(
    path: Path | str,
    *,
    expected_identity: dict[str, Any] | None = None,
) -> WorkspaceManifest:
    """Load a canonical, bounded workspace manifest."""

    candidate = Path(path)
    raw = read_regular_bytes(
        candidate,
        label="workspace manifest",
        max_bytes=MAX_WORKSPACE_MANIFEST_BYTES,
    )
    identity = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }
    if expected_identity is not None and not identities_match(identity, expected_identity):
        raise EvidenceError("workspace manifest identity does not match suite.toml")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except EvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise EvidenceError(f"workspace manifest is not valid strict UTF-8 JSON: {exc}") from exc
    entries = _validate_manifest_value(value)
    if canonical_json_bytes(value) != raw:
        raise EvidenceError("workspace manifest bytes are not canonical JSON")
    return WorkspaceManifest(
        entries=entries,
        identity=identity,
        summary=_tree_summary(entries),
    )


def _entry_map(entries: tuple[dict[str, Any], ...]) -> dict[str, dict[str, Any]]:
    return {entry["path"]: entry for entry in entries}


def _path_set_digest(paths: list[str]) -> str:
    return hashlib.sha256(canonical_json_bytes(paths)).hexdigest()


def _validate_output_parent(
    relative: str,
    *,
    baseline_directories: set[str],
) -> None:
    parts = relative.split("/")
    if len(parts) == 1:
        return
    parent = "/".join(parts[:-1])
    if parent not in baseline_directories:
        raise EvidenceError(
            f"declared output parent must exist in the workspace manifest: {parent!r}"
        )


def project_workspace_summary(
    manifest: WorkspaceManifest,
    outputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Derive one expected tree summary from a baseline plus sealed files."""

    expected = _entry_map(manifest.entries)
    for path, identity in outputs.items():
        normalized = _canonical_path(path, label="projected workspace output")
        if normalized in expected:
            raise EvidenceError(
                f"projected output collides with the workspace baseline: {normalized!r}"
            )
        if not isinstance(identity, dict) or set(identity) != {"sha256", "size"}:
            raise EvidenceError("projected workspace output identity is invalid")
        sha256 = identity["sha256"]
        size = identity["size"]
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_WORKSPACE_FILE_BYTES
        ):
            raise EvidenceError("projected workspace output identity is invalid")
        expected[normalized] = {"path": normalized, "kind": "file", **identity}
    return _tree_summary(tuple(sorted(expected.values(), key=_entry_sort_key)))


def prepare_workspace_binding(
    workspace_root: Path | str,
    manifest_path: Path | str,
    *,
    expected_manifest_identity: dict[str, Any],
    declared_outputs: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """Validate an initial baseline and return its plan-safe binding."""

    manifest = load_workspace_manifest(
        manifest_path,
        expected_identity=expected_manifest_identity,
    )
    observed = observe_workspace(workspace_root)
    if observed.entries != manifest.entries:
        raise EvidenceError("workspace does not exactly match its reviewed manifest")
    baseline = _entry_map(manifest.entries)
    directories = {
        path for path, entry in baseline.items() if entry["kind"] == "directory"
    }
    for output in declared_outputs:
        normalized = _canonical_path(output, label="declared workspace output")
        if normalized in baseline:
            raise EvidenceError(
                f"declared output must be absent from the workspace manifest: {normalized!r}"
            )
        _validate_output_parent(normalized, baseline_directories=directories)
    return {
        "manifest": manifest.identity,
        "baseline": manifest.summary,
    }


def verify_workspace(
    workspace_root: Path | str,
    manifest_path: Path | str,
    *,
    expected_manifest_identity: dict[str, Any],
    expected_baseline: dict[str, Any],
    completed_outputs: dict[str, dict[str, Any]],
    volatile_outputs: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Reject missing, extra, type, or content drift in a reviewed workspace."""

    manifest = load_workspace_manifest(
        manifest_path,
        expected_identity=expected_manifest_identity,
    )
    if manifest.summary != expected_baseline:
        raise EvidenceError("workspace manifest baseline does not match plan.json")
    baseline = _entry_map(manifest.entries)
    expected = dict(baseline)
    for path, identity in completed_outputs.items():
        normalized = _canonical_path(path, label="completed workspace output")
        if normalized in expected:
            raise EvidenceError(
                f"completed output collides with the workspace baseline: {normalized!r}"
            )
        expected[normalized] = {"path": normalized, "kind": "file", **identity}
    volatile = {
        _canonical_path(path, label="volatile workspace output")
        for path in volatile_outputs
    }
    if volatile & set(expected):
        raise EvidenceError("volatile workspace outputs overlap immutable entries")

    observed = observe_workspace(workspace_root)
    actual = _entry_map(observed.entries)
    missing = sorted(set(expected) - set(actual))
    extras = sorted(set(actual) - set(expected))
    unexpected = sorted(set(extras) - volatile)
    if missing or unexpected:
        raise WorkspaceVerificationError(
            "workspace topology drifted; "
            f"missing_count={len(missing)}, missing_sha256={_path_set_digest(missing)}, "
            f"extra_count={len(unexpected)}, "
            f"extra_sha256={_path_set_digest(unexpected)}",
            observation=observed.summary,
        )
    for path, expected_entry in expected.items():
        if actual[path] != expected_entry:
            raise WorkspaceVerificationError(
                f"workspace entry drifted: {path!r}",
                observation=observed.summary,
            )
    for path in extras:
        if actual[path]["kind"] != "file":
            raise WorkspaceVerificationError(
                f"volatile workspace output must be a regular file: {path!r}",
                observation=observed.summary,
            )
    return observed.summary


def _write_new_bytes(
    path: Path,
    payload: bytes,
) -> tuple[dict[str, Any], tuple[int, int]]:
    """Publish one new file without ever replacing a competing path."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    opened_key = _object_key(os.fstat(descriptor))
    complete = False
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("workspace manifest write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        complete = True
        return (
            {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            },
            opened_key,
        )
    finally:
        try:
            os.close(descriptor)
        except OSError:
            if complete:
                raise


def snapshot_workspace(
    workspace_root: Path | str,
    output_path: Path | str,
) -> dict[str, Any]:
    """Create one candidate manifest without exposing absolute host paths."""

    root = checked_workspace_root(Path(workspace_root).absolute())
    output = Path(output_path).absolute()
    if not output.name:
        raise BoundaryError("workspace manifest output must name one file")
    windows_component_key(output.name, label="workspace manifest output name")
    if os.path.lexists(output):
        raise BoundaryError("workspace manifest output must start absent")
    try:
        output_parent = checked_directory(
            output.parent,
            label="workspace manifest output parent",
        )
        output_parent_key = _object_key(output_parent.lstat())
    except (BoundaryError, OSError) as exc:
        raise BoundaryError("workspace manifest output parent is invalid") from exc
    resolved_output = output_parent / output.name
    try:
        inside = os.path.commonpath((str(resolved_output), str(root))) == str(root)
    except ValueError:
        inside = False
    if inside:
        raise BoundaryError("workspace manifest output must be outside the workspace root")

    observation = observe_workspace(root)
    manifest = {
        "schema_version": WORKSPACE_MANIFEST_SCHEMA_VERSION,
        "kind": WORKSPACE_MANIFEST_KIND,
        "policy": WORKSPACE_POLICY,
        "entries": list(observation.entries),
    }
    payload = canonical_json_bytes(manifest)
    if len(payload) > MAX_WORKSPACE_MANIFEST_BYTES:
        raise EvidenceError(
            f"workspace manifest exceeds the {MAX_WORKSPACE_MANIFEST_BYTES}-byte limit"
        )
    try:
        expected_identity, expected_object_key = _write_new_bytes(
            resolved_output,
            payload,
        )
    except FileExistsError as exc:
        raise BoundaryError("workspace manifest output must start absent") from exc
    try:
        current_metadata = resolved_output.lstat()
        if _object_key(current_metadata) != expected_object_key:
            raise EvidenceError(
                "created workspace manifest changed object identity during publication"
            )
        identity = _bounded_file_identity(
            resolved_output,
            metadata=current_metadata,
            relative="created workspace manifest",
            root_device=current_metadata.st_dev,
        )
        current_parent_key = _object_key(output_parent.lstat())
    except (BoundaryError, OSError) as exc:
        raise EvidenceError(
            "created workspace manifest could not be reverified"
        ) from exc
    if current_parent_key != output_parent_key:
        raise EvidenceError("workspace manifest output parent changed during publication")
    if not identities_match(identity, expected_identity):
        raise EvidenceError("workspace manifest changed during publication")
    return {
        "status": "created",
        "manifest": expected_identity,
        "workspace": observation.summary,
        "entries": len(observation.entries),
        "review_required": True,
    }
