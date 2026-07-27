"""Strict suite.toml parsing and normalized task definitions."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchhandoff.errors import BoundaryError, ConfigurationError
from benchhandoff.storage import (
    file_identity,
    normalize_relative_file,
    read_regular_bytes,
    windows_component_key,
    windows_path_key,
)

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_DESCRIPTOR_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ROOT_KEYS_V1 = {"version", "name", "task"}
_ROOT_KEYS_V2 = {"version", "name", "context", "task"}
_CONTEXT_KEYS = {"path", "media_type", "digest", "size"}
_TASK_KEYS = {"id", "argv", "inputs", "outputs"}
MAX_SUITE_BYTES = 256 * 1024
MAX_SUITE_TASKS = 64
MAX_SUITE_NAME_UTF8_BYTES = 256
MAX_CONTEXT_MEDIA_TYPE_UTF8_BYTES = 512
MAX_CONTEXT_SIZE = (1 << 63) - 1
MAX_SUITE_PATH_REFERENCES = 512
MAX_TASK_ARGUMENTS = 128
MAX_TASK_ARGUMENT_UTF8_BYTES = 4096
MAX_TASK_ARGUMENTS_UTF8_BYTES = 64 * 1024
MAX_TASK_INPUTS = 64
MAX_TASK_OUTPUTS = 64


@dataclass(frozen=True)
class ContextDescriptor:
    """One external execution-context descriptor bound by digest and size."""

    path: str
    media_type: str
    digest: str
    size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "media_type": self.media_type,
            "digest": self.digest,
            "size": self.size,
        }


@dataclass(frozen=True)
class TaskSpec:
    """One sequential subprocess and its declared file boundary."""

    task_id: str
    argv: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.task_id,
            "argv": list(self.argv),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
        }


@dataclass(frozen=True)
class SuiteSpec:
    """A validated suite plus the identity of its source file."""

    path: Path
    root: Path
    name: str
    version: int
    tasks: tuple[TaskSpec, ...]
    seed_inputs: tuple[str, ...]
    identity: dict[str, Any]
    context: ContextDescriptor | None

    def normalized(self) -> dict[str, Any]:
        value = {
            "version": self.version,
            "name": self.name,
            "tasks": [task.as_dict() for task in self.tasks],
        }
        if self.context is not None:
            value["context"] = self.context.as_dict()
        return value


def _only_keys(value: dict[str, Any], allowed: set[str], *, label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigurationError(f"{label} contains unsupported keys: {', '.join(unknown)}")


def _string_list(value: Any, *, label: str, paths: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{label} must be an array of strings")
    normalized: list[str] = []
    for index, item in enumerate(value):
        if paths:
            try:
                item = normalize_relative_file(item, label=f"{label}[{index}]")
            except BoundaryError as exc:
                raise ConfigurationError(str(exc)) from exc
        elif "\x00" in item:
            raise ConfigurationError(f"{label}[{index}] contains a NUL byte")
        normalized.append(item)
    if paths and len(set(normalized)) != len(normalized):
        raise ConfigurationError(f"{label} contains duplicates")
    if paths:
        aliases: dict[tuple[str, ...], str] = {}
        for item in normalized:
            key = windows_path_key(item, label=label)
            previous = aliases.get(key)
            if previous is not None and previous != item:
                raise ConfigurationError(
                    f"{label} contains Windows-aliasing paths: {previous!r} and {item!r}"
                )
            aliases[key] = item
    return tuple(normalized)


def load_suite(path: Path | str) -> SuiteSpec:
    """Load one exact suite.toml and reject ambiguous or forward-dependent plans."""

    suite_path = Path(path).absolute()
    raw = read_regular_bytes(suite_path, label="suite.toml", max_bytes=MAX_SUITE_BYTES)
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"suite.toml is not valid UTF-8 TOML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigurationError("suite.toml must contain a table")
    version = parsed.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version not in {1, 2}:
        raise ConfigurationError("suite.toml version must be exactly 1 or 2")
    _only_keys(
        parsed,
        _ROOT_KEYS_V1 if version == 1 else _ROOT_KEYS_V2,
        label="suite.toml",
    )
    name = parsed.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigurationError("suite.toml name must be a non-empty string")
    if len(name.encode("utf-8")) > MAX_SUITE_NAME_UTF8_BYTES:
        raise ConfigurationError(
            f"suite.toml name exceeds {MAX_SUITE_NAME_UTF8_BYTES} UTF-8 bytes"
        )

    context: ContextDescriptor | None = None
    if version == 2:
        raw_context = parsed.get("context")
        if not isinstance(raw_context, dict):
            raise ConfigurationError("suite.toml v2 must define one [context] table")
        _only_keys(raw_context, _CONTEXT_KEYS, label="suite.toml context")
        raw_context_path = raw_context.get("path")
        if not isinstance(raw_context_path, str):
            raise ConfigurationError(
                "suite.toml context.path must be a portable relative file"
            )
        try:
            context_path = normalize_relative_file(
                raw_context_path,
                label="suite.toml context.path",
            )
        except BoundaryError as exc:
            raise ConfigurationError(str(exc)) from exc
        media_type = raw_context.get("media_type")
        if not isinstance(media_type, str) or not media_type:
            raise ConfigurationError("suite.toml context.media_type must be non-empty text")
        if "\x00" in media_type:
            raise ConfigurationError("suite.toml context.media_type contains a NUL byte")
        if len(media_type.encode("utf-8")) > MAX_CONTEXT_MEDIA_TYPE_UTF8_BYTES:
            raise ConfigurationError(
                "suite.toml context.media_type exceeds "
                f"{MAX_CONTEXT_MEDIA_TYPE_UTF8_BYTES} UTF-8 bytes"
            )
        digest = raw_context.get("digest")
        if not isinstance(digest, str) or not _DESCRIPTOR_DIGEST.fullmatch(digest):
            raise ConfigurationError(
                "suite.toml context.digest must be lowercase sha256:<64 hex>"
            )
        size = raw_context.get("size")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_CONTEXT_SIZE
        ):
            raise ConfigurationError(
                f"suite.toml context.size must be an integer from 0 to {MAX_CONTEXT_SIZE}"
            )
        context = ContextDescriptor(
            path=context_path,
            media_type=media_type,
            digest=digest,
            size=size,
        )

    raw_tasks = parsed.get("task")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ConfigurationError("suite.toml must define at least one [[task]]")
    if len(raw_tasks) > MAX_SUITE_TASKS:
        raise ConfigurationError(
            f"suite.toml exceeds the {MAX_SUITE_TASKS}-task limit"
        )

    task_ids: set[str] = set()
    task_id_aliases: dict[str, str] = {}
    path_aliases: dict[tuple[str, ...], str] = {}
    path_prefixes: dict[tuple[str, ...], str] = {}
    produced: dict[str, str] = {}
    observed_seed_inputs: set[str] = set()
    seed_order: list[str] = []
    tasks: list[TaskSpec] = []
    declared_path_references = 0
    for index, raw_task in enumerate(raw_tasks):
        label = f"task[{index}]"
        if not isinstance(raw_task, dict):
            raise ConfigurationError(f"{label} must be a table")
        _only_keys(raw_task, _TASK_KEYS, label=label)

        task_id = raw_task.get("id")
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise ConfigurationError(
                f"{label}.id must match {_TASK_ID.pattern} and be at most 64 characters"
            )
        try:
            task_alias = windows_component_key(task_id, label=f"{label}.id")
        except BoundaryError as exc:
            raise ConfigurationError(str(exc)) from exc
        previous_task_id = task_id_aliases.get(task_alias)
        if previous_task_id is not None:
            raise ConfigurationError(
                f"task ids alias on Windows: {previous_task_id!r} and {task_id!r}"
            )
        if task_id in task_ids:
            raise ConfigurationError(f"duplicate task id: {task_id}")
        task_ids.add(task_id)
        task_id_aliases[task_alias] = task_id

        argv = _string_list(raw_task.get("argv"), label=f"{label}.argv")
        if not argv or not argv[0]:
            raise ConfigurationError(f"{label}.argv must contain a non-empty executable")
        if version == 2:
            try:
                normalize_relative_file(
                    argv[0],
                    label=f"{label}.argv executable",
                )
            except BoundaryError as exc:
                raise ConfigurationError(
                    "suite.toml v2 executable must be a portable bare name "
                    "or suite-relative path"
                ) from exc
        if len(argv) > MAX_TASK_ARGUMENTS:
            raise ConfigurationError(
                f"{label}.argv exceeds the {MAX_TASK_ARGUMENTS}-argument limit"
            )
        encoded_arguments = [len(item.encode("utf-8")) for item in argv]
        if any(length > MAX_TASK_ARGUMENT_UTF8_BYTES for length in encoded_arguments):
            raise ConfigurationError(
                f"{label}.argv contains an argument over "
                f"{MAX_TASK_ARGUMENT_UTF8_BYTES} UTF-8 bytes"
            )
        if sum(encoded_arguments) > MAX_TASK_ARGUMENTS_UTF8_BYTES:
            raise ConfigurationError(
                f"{label}.argv exceeds the {MAX_TASK_ARGUMENTS_UTF8_BYTES}-byte total limit"
            )
        inputs = _string_list(raw_task.get("inputs", []), label=f"{label}.inputs", paths=True)
        outputs = _string_list(raw_task.get("outputs"), label=f"{label}.outputs", paths=True)
        if len(inputs) > MAX_TASK_INPUTS:
            raise ConfigurationError(
                f"{label}.inputs exceeds the {MAX_TASK_INPUTS}-path limit"
            )
        if not outputs:
            raise ConfigurationError(f"{label}.outputs must declare at least one file")
        if len(outputs) > MAX_TASK_OUTPUTS:
            raise ConfigurationError(
                f"{label}.outputs exceeds the {MAX_TASK_OUTPUTS}-path limit"
            )
        declared_path_references += len(inputs) + len(outputs)
        if declared_path_references > MAX_SUITE_PATH_REFERENCES:
            raise ConfigurationError(
                f"suite.toml exceeds the {MAX_SUITE_PATH_REFERENCES}-path "
                "reference limit"
            )
        for path_value in (*inputs, *outputs):
            path_alias = windows_path_key(path_value, label=f"{label} path")
            previous_path = path_aliases.get(path_alias)
            if previous_path is not None and previous_path != path_value:
                raise ConfigurationError(
                    f"suite paths alias on Windows: {previous_path!r} and {path_value!r}"
                )
            descendant = path_prefixes.get(path_alias)
            if descendant is not None and descendant != path_value:
                raise ConfigurationError(
                    f"declared file paths have an ancestor conflict: "
                    f"{path_value!r} and {descendant!r}"
                )
            for depth in range(1, len(path_alias)):
                ancestor = path_aliases.get(path_alias[:depth])
                if ancestor is not None and ancestor != path_value:
                    raise ConfigurationError(
                        f"declared file paths have an ancestor conflict: "
                        f"{ancestor!r} and {path_value!r}"
                    )
            path_aliases[path_alias] = path_value
            for depth in range(1, len(path_alias)):
                path_prefixes.setdefault(path_alias[:depth], path_value)
        output_aliases = {windows_path_key(value, label=f"{label}.outputs") for value in outputs}
        overlap = sorted(
            value
            for value in inputs
            if windows_path_key(value, label=f"{label}.inputs") in output_aliases
        )
        if overlap:
            raise ConfigurationError(
                f"{label} cannot use the same path as input and output: {', '.join(overlap)}"
            )

        for input_path in inputs:
            if input_path not in produced and input_path not in observed_seed_inputs:
                observed_seed_inputs.add(input_path)
                seed_order.append(input_path)
        for output_path in outputs:
            if output_path in produced:
                raise ConfigurationError(
                    f"output {output_path!r} is already produced by task {produced[output_path]!r}"
                )
            if output_path in observed_seed_inputs:
                raise ConfigurationError(
                    f"task {task_id!r} would overwrite declared seed input {output_path!r}"
                )
            produced[output_path] = task_id

        tasks.append(TaskSpec(task_id, argv, inputs, outputs))

    if context is not None and context.path not in observed_seed_inputs:
        raise ConfigurationError(
            "suite.toml context.path must be declared as a seed task input"
        )

    resolved_path = suite_path.resolve(strict=True)
    return SuiteSpec(
        path=resolved_path,
        root=resolved_path.parent,
        name=name.strip(),
        version=version,
        tasks=tuple(tasks),
        seed_inputs=tuple(seed_order),
        identity=file_identity(resolved_path, label="suite.toml"),
        context=context,
    )
