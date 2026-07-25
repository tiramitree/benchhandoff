"""Fail-closed execution, recovery, bundling, and verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import platform
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from benchhandoff.errors import BenchHandoffError, BoundaryError, EvidenceError
from benchhandoff.model import (
    MAX_SUITE_NAME_UTF8_BYTES,
    MAX_SUITE_PATH_REFERENCES,
    MAX_SUITE_TASKS,
    MAX_TASK_ARGUMENTS,
    MAX_TASK_ARGUMENT_UTF8_BYTES,
    MAX_TASK_ARGUMENTS_UTF8_BYTES,
    MAX_TASK_INPUTS,
    MAX_TASK_OUTPUTS,
    SuiteSpec,
    TaskSpec,
    load_suite,
)
from benchhandoff.processes import process_liveness, process_start_token, stop_process
from benchhandoff.storage import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    checked_directory,
    create_empty_regular,
    ensure_member_absent,
    ensure_output_parent_boundary,
    file_identity,
    identities_match,
    iter_regular_artifacts,
    member_identity,
    move_regular_same_filesystem,
    nearest_existing_directory,
    normalize_relative_file,
    prepare_new_directory,
    read_json_file,
    read_regular_bytes,
    require_same_filesystem,
    require_separate_trees,
    resolve_member,
    utc_now,
)
from benchhandoff.writer_lock import WriterLock

SCHEMA_VERSION = 1
PLAN_FILE = "plan.json"
STATE_FILE = "state.json"
BUNDLE_FILE = "bundle.json"
EVENTS_FILE = "events.jsonl"
LOGS_DIRECTORY = "logs"
QUARANTINE_DIRECTORY = "quarantine"
MAX_EVENT_LOG_BYTES = 64 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_EVENT_COUNT = 100_000
MAX_ATTEMPTS_PER_TASK = 4
MAX_TOTAL_ATTEMPTS = 256
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class RunResult:
    """A machine-readable result returned by the Python API and CLI."""

    status: str
    run_directory: str
    run_id: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "run_directory": self.run_directory,
            "run_id": self.run_id,
            "detail": self.detail,
        }


@dataclass
class _RunContext:
    suite: SuiteSpec
    run_root: Path
    plan: dict[str, Any]
    state: dict[str, Any]


def _event_path(run_root: Path) -> Path:
    return run_root / EVENTS_FILE


def _persist_state(context: _RunContext, *, touch_updated_at: bool) -> None:
    if touch_updated_at:
        context.state["updated_at"] = utc_now()
    _validate_state_shape(context.state, context.plan)
    atomic_write_json(context.run_root / STATE_FILE, context.state)


def _write_state(context: _RunContext) -> None:
    """Persist a non-event internal update such as a captured child identity."""

    _persist_state(context, touch_updated_at=True)


def _event_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceError(f"events.jsonl contains duplicate key: {key!r}")
        value[key] = item
    return value


_EVENT_DETAIL_FIELDS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "run_started": {"suite": str, "tasks": int},
    "run_resumed": {"previous_status": str},
    "run_completed": {"operation": str, "tasks": int},
    "task_started": {"attempt": int},
    "task_failed": {"attempt": int, "return_code": (int, type(None)), "reason": str},
    "task_recovery_prepared": {
        "previous_status": str,
        "attempt": int,
        "quarantined_outputs": int,
    },
    "task_completed": {"attempt": int, "outputs": int},
}
_TASK_EVENT_TYPES = {
    "task_started",
    "task_failed",
    "task_recovery_prepared",
    "task_completed",
}


def _validate_event_record(
    event: Any,
    plan: dict[str, Any],
    *,
    record_number: int,
    previous_sha256: str,
) -> dict[str, Any]:
    label = f"events.jsonl record {record_number}"
    if not isinstance(event, dict):
        raise EvidenceError(f"{label} must be an object")
    required = {
        "schema_version",
        "time",
        "type",
        "run_id",
        "sequence",
        "previous_sha256",
        "details",
    }
    optional = {"task_id"}
    _exact_keys(event, required, optional=optional, label=label)
    if not _non_bool_int(event["schema_version"]) or event["schema_version"] != SCHEMA_VERSION:
        raise EvidenceError(f"{label} has an invalid schema version")
    if event["run_id"] != plan["run_id"]:
        raise EvidenceError(f"{label} has a mismatched run_id")
    if not _non_bool_int(event["sequence"]) or event["sequence"] != record_number:
        raise EvidenceError(f"{label} has a non-contiguous sequence")
    if event["previous_sha256"] != previous_sha256:
        raise EvidenceError(f"{label} breaks the hash chain")
    _validate_text(event["time"], label=f"{label}.time")
    event_type = _validate_text(event["type"], label=f"{label}.type")
    if event_type not in _EVENT_DETAIL_FIELDS:
        raise EvidenceError(f"{label} has an unknown event type")

    task_ids = {task["id"] for task in plan["suite"]["tasks"]}
    if event_type in _TASK_EVENT_TYPES:
        if "task_id" not in event or event["task_id"] not in task_ids:
            raise EvidenceError(f"{label} has an unknown or missing task_id")
    elif "task_id" in event:
        raise EvidenceError(f"{label} run event must not contain task_id")

    details = event["details"]
    if not isinstance(details, dict):
        raise EvidenceError(f"{label}.details must be an object")
    fields = _EVENT_DETAIL_FIELDS[event_type]
    _exact_keys(details, set(fields), label=f"{label}.details")
    for key, expected_type in fields.items():
        value = details[key]
        allowed_types = (
            expected_type if isinstance(expected_type, tuple) else (expected_type,)
        )
        if int in allowed_types:
            allows_none = type(None) in allowed_types
            if value is None:
                if not allows_none:
                    raise EvidenceError(f"{label}.details.{key} must be an integer")
            elif not _non_bool_int(value):
                suffix = " or null" if allows_none else ""
                raise EvidenceError(
                    f"{label}.details.{key} must be an integer{suffix}"
                )
            elif key != "return_code" and value < 0:
                raise EvidenceError(f"{label}.details.{key} must be non-negative")
        elif not isinstance(value, expected_type):
            raise EvidenceError(f"{label}.details.{key} has an invalid type")
        if isinstance(value, str):
            _validate_text(value, label=f"{label}.details.{key}")
            if len(value) > 8192:
                raise EvidenceError(f"{label}.details.{key} is too long")
    try:
        payload = canonical_json_bytes(event)
    except (RecursionError, OverflowError, TypeError, UnicodeEncodeError, ValueError) as exc:
        raise EvidenceError(f"{label} is not safely serializable") from exc
    if len(payload) > MAX_EVENT_BYTES:
        raise EvidenceError(f"{label} exceeds the per-event size limit")
    return event


def _event_log_observation(
    run_root: Path,
    plan: dict[str, Any],
    *,
    expected: Any | None = None,
) -> dict[str, Any]:
    raw = read_regular_bytes(
        _event_path(run_root),
        label=EVENTS_FILE,
        max_bytes=MAX_EVENT_LOG_BYTES,
    )
    digest = hashlib.sha256()
    count = 0
    offset = 0
    while offset < len(raw):
        if count >= MAX_EVENT_COUNT:
            raise EvidenceError("events.jsonl exceeds the event-count limit")
        line_end = raw.find(b"\n", offset)
        if line_end < 0:
            raise EvidenceError("events.jsonl has an unterminated final record")
        line = raw[offset : line_end + 1]
        if line == b"\n":
            raise EvidenceError("events.jsonl contains a blank record")
        if len(line) > MAX_EVENT_BYTES:
            raise EvidenceError(f"events.jsonl record {count + 1} exceeds the size limit")
        prefix_sha256 = digest.hexdigest()
        try:
            event = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_event_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    EvidenceError(f"events.jsonl contains non-finite number: {value}")
                ),
            )
        except EvidenceError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, OverflowError) as exc:
            raise EvidenceError(f"events.jsonl record {count + 1} is invalid JSON: {exc}") from exc
        _validate_event_record(
            event,
            plan,
            record_number=count + 1,
            previous_sha256=prefix_sha256,
        )
        try:
            if canonical_json_bytes(event) != line:
                raise EvidenceError(f"events.jsonl record {count + 1} is not canonical JSON")
        except (RecursionError, OverflowError, TypeError, UnicodeEncodeError, ValueError) as exc:
            raise EvidenceError(f"events.jsonl record {count + 1} is not serializable") from exc
        digest.update(line)
        count += 1
        offset = line_end + 1

    observation = {"sha256": digest.hexdigest(), "size": len(raw), "count": count}
    if expected is not None:
        if not isinstance(expected, dict):
            raise EvidenceError("state.json event_log binding must be an object")
        if not identities_match(observation, expected) or expected.get("count") != count:
            raise EvidenceError("events.jsonl identity does not match state.json")
    return observation


def _event_transition_status(
    run_root: Path,
    plan: dict[str, Any],
    state: dict[str, Any],
) -> str:
    raw = read_regular_bytes(
        _event_path(run_root),
        label=EVENTS_FILE,
        max_bytes=MAX_EVENT_LOG_BYTES,
    )
    actual = _event_log_observation(run_root, plan)
    acknowledged = state["event_log"]
    pending = state["pending_event"]
    if pending is None:
        if not identities_match(actual, acknowledged) or actual["count"] != acknowledged["count"]:
            raise EvidenceError("events.jsonl identity does not match acknowledged state")
        return "stable"

    pending_bytes = canonical_json_bytes(pending)
    if identities_match(actual, acknowledged) and actual["count"] == acknowledged["count"]:
        return "pending_before_log"
    acknowledged_size = acknowledged["size"]
    if (
        acknowledged_size <= len(raw)
        and len(raw) == acknowledged_size + len(pending_bytes)
        and raw[acknowledged_size:] == pending_bytes
        and hashlib.sha256(raw[:acknowledged_size]).hexdigest() == acknowledged["sha256"]
        and actual["count"] == acknowledged["count"] + 1
    ):
        return "pending_after_log"
    raise EvidenceError("events.jsonl diverges from the one pending transition")


def _commit_transition(
    context: _RunContext,
    event_type: str,
    *,
    task_id: str | None = None,
    details: dict[str, Any],
) -> None:
    if context.state.get("pending_event") is not None:
        raise EvidenceError("cannot commit a new transition while another event is pending")
    if _event_transition_status(context.run_root, context.plan, context.state) != "stable":
        raise EvidenceError("event/state transition is not stable")
    current = context.state["event_log"]
    if current["count"] >= MAX_EVENT_COUNT:
        raise EvidenceError("events.jsonl has reached the event-count limit")
    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "time": utc_now(),
        "type": event_type,
        "run_id": context.plan["run_id"],
        "sequence": current["count"] + 1,
        "previous_sha256": current["sha256"],
        "details": details,
    }
    if task_id is not None:
        event["task_id"] = task_id
    _validate_event_record(
        event,
        context.plan,
        record_number=current["count"] + 1,
        previous_sha256=current["sha256"],
    )
    payload = canonical_json_bytes(event)
    prior_log = read_regular_bytes(
        _event_path(context.run_root),
        label=EVENTS_FILE,
        max_bytes=MAX_EVENT_LOG_BYTES,
    )
    if len(prior_log) + len(payload) > MAX_EVENT_LOG_BYTES:
        raise EvidenceError("events.jsonl would exceed the total size limit")

    context.state["pending_event"] = event
    try:
        _persist_state(context, touch_updated_at=True)
    except Exception:
        context.state["pending_event"] = None
        raise
    atomic_write_bytes(_event_path(context.run_root), prior_log + payload)
    observation = _event_log_observation(context.run_root, context.plan)
    context.state["event_log"] = observation
    context.state["pending_event"] = None
    _persist_state(context, touch_updated_at=False)


def _reconcile_pending_event(context: _RunContext) -> None:
    status = _event_transition_status(context.run_root, context.plan, context.state)
    if status == "stable":
        return
    pending = context.state["pending_event"]
    if pending is None:
        raise EvidenceError("event reconciliation has no pending event")
    if status == "pending_before_log":
        prior_log = read_regular_bytes(
            _event_path(context.run_root),
            label=EVENTS_FILE,
            max_bytes=MAX_EVENT_LOG_BYTES,
        )
        pending_bytes = canonical_json_bytes(pending)
        if len(prior_log) + len(pending_bytes) > MAX_EVENT_LOG_BYTES:
            raise EvidenceError("events.jsonl would exceed the total size limit")
        atomic_write_bytes(
            _event_path(context.run_root),
            prior_log + pending_bytes,
        )
    observation = _event_log_observation(context.run_root, context.plan)
    context.state["event_log"] = observation
    context.state["pending_event"] = None
    _persist_state(context, touch_updated_at=False)

def _identity_or_raise(
    actual: dict[str, Any],
    expected: dict[str, Any],
    *,
    label: str,
) -> None:
    if not identities_match(actual, expected):
        raise EvidenceError(
            f"{label} identity drifted: expected "
            f"{expected.get('sha256')}/{expected.get('size')}, got "
            f"{actual.get('sha256')}/{actual.get('size')}"
        )


def _preflight_suite(suite: SuiteSpec) -> dict[str, dict[str, Any]]:
    seed_inputs: dict[str, dict[str, Any]] = {}
    for relative in suite.seed_inputs:
        seed_inputs[relative] = member_identity(
            suite.root,
            relative,
            label=f"seed input {relative!r}",
        )
    for task in suite.tasks:
        for relative in task.outputs:
            ensure_output_parent_boundary(
                suite.root,
                relative,
                label=f"output {relative!r}",
            )
            ensure_member_absent(
                suite.root,
                relative,
                label=f"output {relative!r}",
            )
    return seed_inputs


def _initial_plan(
    suite: SuiteSpec,
    run_root: Path,
    seed_inputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "benchhandoff-plan",
        "run_id": uuid4().hex,
        "created_at": utc_now(),
        "run_directory": str(run_root),
        "suite_file": {
            "path": str(suite.path),
            **suite.identity,
        },
        "suite_root": str(suite.root),
        "suite": suite.normalized(),
        "seed_inputs": seed_inputs,
        "environment": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "platform": platform.system() or "Unknown",
        },
    }


def _initial_state(
    plan: dict[str, Any],
    plan_identity: dict[str, Any],
    event_log_identity: dict[str, Any],
) -> dict[str, Any]:
    created_at = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "benchhandoff-state",
        "run_id": plan["run_id"],
        "plan_sha256": plan_identity["sha256"],
        "status": "running",
        "created_at": created_at,
        "updated_at": created_at,
        "last_error": None,
        "event_log": {**event_log_identity, "count": 0},
        "pending_event": None,
        "tasks": {
            task["id"]: {
                "status": "pending",
                "attempts": [],
                "verified_inputs": {},
                "verified_outputs": {},
            }
            for task in plan["suite"]["tasks"]
        },
    }


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _exact_keys(
    value: dict[str, Any],
    required: set[str],
    *,
    label: str,
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    missing = sorted(required - set(value))
    extra = sorted(set(value) - allowed)
    if missing or extra:
        raise EvidenceError(
            f"{label} fields are invalid; missing={missing or []}, extra={extra or []}"
        )


def _non_bool_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_text(value: Any, *, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or (not allow_empty and not value):
        raise EvidenceError(f"{label} must be a valid string")
    return value


def _validate_bound_path(value: Any, *, label: str) -> str:
    text = _validate_text(value, label=label)
    try:
        if not Path(text).is_absolute():
            raise EvidenceError(f"{label} must be an absolute path")
    except (OSError, ValueError) as exc:
        raise EvidenceError(f"{label} is not a valid path") from exc
    return text


def _validate_portable_path(value: Any, *, label: str) -> str:
    text = _validate_text(value, label=label)
    try:
        return normalize_relative_file(text, label=label)
    except BenchHandoffError as exc:
        raise EvidenceError(str(exc)) from exc


def _validate_identity(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    _exact_keys(value, {"sha256", "size"}, label=label)
    if not isinstance(value["sha256"], str) or not _SHA256.fullmatch(value["sha256"]):
        raise EvidenceError(f"{label}.sha256 is invalid")
    if not _non_bool_int(value["size"]) or value["size"] < 0:
        raise EvidenceError(f"{label}.size is invalid")
    return value


def _validate_file_record(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    _exact_keys(value, {"path", "sha256", "size"}, label=label)
    _validate_bound_path(value["path"], label=f"{label}.path")
    _validate_identity(
        {"sha256": value["sha256"], "size": value["size"]},
        label=label,
    )
    return value


def _validate_identity_map(
    value: Any,
    *,
    label: str,
    expected_paths: tuple[str, ...] | list[str] | set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    normalized: set[str] = set()
    for path, identity in value.items():
        normalized.add(_validate_portable_path(path, label=f"{label} path"))
        _validate_identity(identity, label=f"{label}[{path!r}]")
    if expected_paths is not None and normalized != set(expected_paths):
        raise EvidenceError(f"{label} paths do not match the expected declaration")
    return value


def _validate_plan_task(task: Any, *, index: int) -> dict[str, Any]:
    label = f"plan.json suite task[{index}]"
    if not isinstance(task, dict):
        raise EvidenceError(f"{label} must be an object")
    _exact_keys(task, {"id", "argv", "inputs", "outputs"}, label=label)
    task_id = _validate_text(task["id"], label=f"{label}.id")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", task_id) is None:
        raise EvidenceError(f"{label}.id is not a portable task id")

    argv = task["argv"]
    if not isinstance(argv, list) or not argv or any(
        not isinstance(item, str) or "\x00" in item for item in argv
    ):
        raise EvidenceError(f"{label}.argv must be a non-empty string array")
    if not argv[0]:
        raise EvidenceError(f"{label}.argv executable must be non-empty")
    if len(argv) > MAX_TASK_ARGUMENTS:
        raise EvidenceError(
            f"{label}.argv exceeds the {MAX_TASK_ARGUMENTS}-argument limit"
        )
    try:
        encoded_arguments = [len(item.encode("utf-8")) for item in argv]
    except UnicodeEncodeError as exc:
        raise EvidenceError(f"{label}.argv is not valid UTF-8 text") from exc
    if any(length > MAX_TASK_ARGUMENT_UTF8_BYTES for length in encoded_arguments):
        raise EvidenceError(
            f"{label}.argv contains an argument over "
            f"{MAX_TASK_ARGUMENT_UTF8_BYTES} UTF-8 bytes"
        )
    if sum(encoded_arguments) > MAX_TASK_ARGUMENTS_UTF8_BYTES:
        raise EvidenceError(
            f"{label}.argv exceeds the {MAX_TASK_ARGUMENTS_UTF8_BYTES}-byte total limit"
        )

    for field, maximum in (
        ("inputs", MAX_TASK_INPUTS),
        ("outputs", MAX_TASK_OUTPUTS),
    ):
        paths = task[field]
        if not isinstance(paths, list) or any(
            not isinstance(item, str) for item in paths
        ):
            raise EvidenceError(f"{label}.{field} must be a string array")
        if len(paths) > maximum:
            raise EvidenceError(f"{label}.{field} exceeds the {maximum}-path limit")
        for item in paths:
            _validate_portable_path(item, label=f"{label}.{field}")
        if len(paths) != len(set(paths)):
            raise EvidenceError(f"{label}.{field} contains duplicate paths")
    if not task["outputs"]:
        raise EvidenceError(f"{label}.outputs must not be empty")
    return task


def _validate_plan_shape(plan: Any) -> None:
    if not isinstance(plan, dict):
        raise EvidenceError("plan.json must contain an object")
    required = {
        "schema_version",
        "kind",
        "run_id",
        "created_at",
        "run_directory",
        "suite_file",
        "suite_root",
        "suite",
        "seed_inputs",
        "environment",
    }
    _exact_keys(plan, required, label="plan.json")
    if (
        not _non_bool_int(plan["schema_version"])
        or plan["schema_version"] != SCHEMA_VERSION
        or plan["kind"] != "benchhandoff-plan"
    ):
        raise EvidenceError("plan.json has an unsupported schema or kind")
    run_id = _validate_text(plan["run_id"], label="plan.json run_id")
    if len(run_id) != 32 or any(character not in "0123456789abcdef" for character in run_id):
        raise EvidenceError("plan.json run_id is invalid")
    _validate_text(plan["created_at"], label="plan.json created_at")
    _validate_bound_path(plan["run_directory"], label="plan.json run_directory")
    _validate_bound_path(plan["suite_root"], label="plan.json suite_root")
    _validate_file_record(plan["suite_file"], label="plan.json suite_file")

    suite = plan["suite"]
    if not isinstance(suite, dict):
        raise EvidenceError("plan.json suite must be an object")
    _exact_keys(suite, {"version", "name", "tasks"}, label="plan.json suite")
    if not _non_bool_int(suite["version"]) or suite["version"] != 1:
        raise EvidenceError("plan.json suite version is invalid")
    suite_name = _validate_text(suite["name"], label="plan.json suite name")
    if len(suite_name.encode("utf-8")) > MAX_SUITE_NAME_UTF8_BYTES:
        raise EvidenceError(
            f"plan.json suite name exceeds {MAX_SUITE_NAME_UTF8_BYTES} UTF-8 bytes"
        )
    if not isinstance(suite["tasks"], list) or not suite["tasks"]:
        raise EvidenceError("plan.json suite tasks must be a non-empty array")
    if len(suite["tasks"]) > MAX_SUITE_TASKS:
        raise EvidenceError(
            f"plan.json suite exceeds the {MAX_SUITE_TASKS}-task limit"
        )
    task_ids: set[str] = set()
    path_references = 0
    for index, task in enumerate(suite["tasks"]):
        validated = _validate_plan_task(task, index=index)
        path_references += len(validated["inputs"]) + len(validated["outputs"])
        if path_references > MAX_SUITE_PATH_REFERENCES:
            raise EvidenceError(
                f"plan.json suite exceeds the {MAX_SUITE_PATH_REFERENCES}-path "
                "reference limit"
            )
        if validated["id"] in task_ids:
            raise EvidenceError("plan.json suite contains duplicate task ids")
        task_ids.add(validated["id"])
    _validate_identity_map(plan["seed_inputs"], label="plan.json seed_inputs")

    environment = plan["environment"]
    if not isinstance(environment, dict):
        raise EvidenceError("plan.json environment must be an object")
    _exact_keys(
        environment,
        {"python_implementation", "python_version", "platform"},
        label="plan.json environment",
    )
    for field in environment:
        _validate_text(environment[field], label=f"plan.json environment.{field}")


def _validate_quarantine_records(
    value: Any,
    *,
    task: dict[str, Any],
    attempt_number: int,
    label: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise EvidenceError(f"{label} must be an array")
    sources: set[str] = set()
    artifacts: set[str] = set()
    for index, record in enumerate(value):
        record_label = f"{label}[{index}]"
        if not isinstance(record, dict):
            raise EvidenceError(f"{record_label} must be an object")
        _exact_keys(record, {"source", "artifact", "sha256", "size"}, label=record_label)
        source = _validate_portable_path(record["source"], label=f"{record_label}.source")
        artifact = _validate_portable_path(record["artifact"], label=f"{record_label}.artifact")
        if source not in task["outputs"]:
            raise EvidenceError(f"{record_label}.source is not a declared task output")
        expected = f"{QUARANTINE_DIRECTORY}/{_quarantine_name(task['id'], attempt_number, source)}"
        if artifact != expected:
            raise EvidenceError(f"{record_label}.artifact does not match its task attempt")
        if source in sources or artifact in artifacts:
            raise EvidenceError(f"{label} contains duplicate quarantine identities")
        sources.add(source)
        artifacts.add(artifact)
        _validate_identity(
            {"sha256": record["sha256"], "size": record["size"]},
            label=record_label,
        )
    return value


def _validate_attempt(
    attempt: Any,
    *,
    task: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    label = f"state.json task {task['id']!r} attempt[{index}]"
    if not isinstance(attempt, dict):
        raise EvidenceError(f"{label} must be an object")
    required = {
        "number",
        "status",
        "started_at",
        "ended_at",
        "child_pid",
        "child_start_token",
        "child_launch_guard",
        "return_code",
        "argv",
        "verified_inputs",
        "stdout",
        "stderr",
    }
    optional = {
        "error",
        "verified_outputs",
        "interruption_reason",
        "quarantined_outputs",
        "return_code_unavailable_reason",
    }
    _exact_keys(attempt, required, optional=optional, label=label)
    if not _non_bool_int(attempt["number"]) or attempt["number"] != index + 1:
        raise EvidenceError(f"{label}.number must be contiguous and one-based")
    status = attempt["status"]
    if not isinstance(status, str) or status not in {
        "running",
        "failed",
        "interrupted",
        "completed",
    }:
        raise EvidenceError(f"{label}.status is invalid")
    _validate_text(attempt["started_at"], label=f"{label}.started_at")
    if status == "running":
        if attempt["ended_at"] is not None or attempt["return_code"] is not None:
            raise EvidenceError(f"{label} running terminal fields must be null")
        if any(
            field in attempt
            for field in (
                "error",
                "interruption_reason",
                "verified_outputs",
                "quarantined_outputs",
            )
        ):
            raise EvidenceError(f"{label} running attempt has terminal-only fields")
    else:
        _validate_text(attempt["ended_at"], label=f"{label}.ended_at")
    child_pid = attempt["child_pid"]
    if child_pid is not None and (not _non_bool_int(child_pid) or child_pid <= 0):
        raise EvidenceError(f"{label}.child_pid is invalid")
    child_start_token = attempt["child_start_token"]
    if child_pid is None:
        if child_start_token is not None:
            raise EvidenceError(f"{label}.child_start_token requires child_pid")
    elif not isinstance(child_start_token, str) or not child_start_token or len(child_start_token) > 256:
        raise EvidenceError(f"{label}.child_start_token is invalid")
    child_launch_guard = attempt["child_launch_guard"]
    if not isinstance(child_launch_guard, bool):
        raise EvidenceError(f"{label}.child_launch_guard must be a boolean")
    if child_launch_guard:
        if status != "running" or child_pid is not None or child_start_token is not None:
            raise EvidenceError(
                f"{label}.child_launch_guard requires an unidentified running launch"
            )
    elif status == "running" and child_pid is None:
        raise EvidenceError(
            f"{label} running attempt without a launch guard must identify its child"
        )
    return_code = attempt["return_code"]
    if return_code is not None and not _non_bool_int(return_code):
        raise EvidenceError(f"{label}.return_code is invalid")
    unavailable_field = "return_code_unavailable_reason"
    if unavailable_field in attempt:
        _validate_text(attempt[unavailable_field], label=f"{label}.{unavailable_field}")
        if status != "interrupted" or child_pid is None or return_code is not None:
            raise EvidenceError(
                f"{label}.{unavailable_field} requires an interrupted child "
                "with no recorded return_code"
            )
    if status in {"failed", "interrupted"}:
        if child_pid is None and return_code is not None:
            raise EvidenceError(f"{label} has a return_code without a child process")
        if (
            child_pid is not None
            and return_code is None
            and unavailable_field not in attempt
        ):
            raise EvidenceError(
                f"{label} lacks either a confirmed child return_code or "
                "an explicit unavailable reason"
            )
    if attempt["argv"] != task["argv"]:
        raise EvidenceError(f"{label}.argv does not match plan.json")
    _validate_identity_map(
        attempt["verified_inputs"],
        label=f"{label}.verified_inputs",
        expected_paths=task["inputs"],
    )
    expected_stdout = f"{LOGS_DIRECTORY}/{task['id']}/attempt-{index + 1:04d}.stdout.log"
    expected_stderr = f"{LOGS_DIRECTORY}/{task['id']}/attempt-{index + 1:04d}.stderr.log"
    if _validate_portable_path(attempt["stdout"], label=f"{label}.stdout") != expected_stdout:
        raise EvidenceError(f"{label}.stdout does not match its task attempt")
    if _validate_portable_path(attempt["stderr"], label=f"{label}.stderr") != expected_stderr:
        raise EvidenceError(f"{label}.stderr does not match its task attempt")

    if status == "completed":
        if child_pid is None or child_start_token is None:
            raise EvidenceError(f"{label} completed attempt must identify its child process")
        if return_code != 0:
            raise EvidenceError(f"{label} completed return_code must be zero")
        if "verified_outputs" not in attempt:
            raise EvidenceError(f"{label} is missing verified_outputs")
        _validate_identity_map(
            attempt["verified_outputs"],
            label=f"{label}.verified_outputs",
            expected_paths=task["outputs"],
        )
    elif "verified_outputs" in attempt:
        raise EvidenceError(f"{label} non-completed attempt must not verify outputs")

    if status in {"failed", "interrupted"}:
        if "error" not in attempt and "interruption_reason" not in attempt:
            raise EvidenceError(f"{label} has no terminal failure detail")
    for field in ("error", "interruption_reason"):
        if field in attempt:
            value = _validate_text(attempt[field], label=f"{label}.{field}")
            if len(value) > 8192:
                raise EvidenceError(f"{label}.{field} exceeds 8192 characters")
    if "quarantined_outputs" in attempt:
        if status not in {"failed", "interrupted"}:
            raise EvidenceError(f"{label} cannot have quarantined outputs")
        _validate_quarantine_records(
            attempt["quarantined_outputs"],
            task=task,
            attempt_number=index + 1,
            label=f"{label}.quarantined_outputs",
        )
    return attempt


def _validate_state_shape(state: Any, plan: dict[str, Any]) -> None:
    if not isinstance(state, dict):
        raise EvidenceError("state.json must contain an object")
    required = {
        "schema_version",
        "kind",
        "run_id",
        "plan_sha256",
        "status",
        "created_at",
        "updated_at",
        "last_error",
        "event_log",
        "pending_event",
        "tasks",
    }
    _exact_keys(state, required, label="state.json")
    if (
        not _non_bool_int(state["schema_version"])
        or state["schema_version"] != SCHEMA_VERSION
        or state["kind"] != "benchhandoff-state"
    ):
        raise EvidenceError("state.json has an unsupported schema or kind")
    if state["run_id"] != plan["run_id"]:
        raise EvidenceError("state.json run_id does not match plan.json")
    if not isinstance(state["plan_sha256"], str) or not _SHA256.fullmatch(state["plan_sha256"]):
        raise EvidenceError("state.json plan_sha256 is invalid")
    if not isinstance(state["status"], str) or state["status"] not in {"running", "failed", "completed"}:
        raise EvidenceError("state.json has an invalid run status")
    _validate_text(state["created_at"], label="state.json created_at")
    _validate_text(state["updated_at"], label="state.json updated_at")
    if state["last_error"] is not None:
        _validate_text(state["last_error"], label="state.json last_error")
    event_log = state["event_log"]
    if not isinstance(event_log, dict):
        raise EvidenceError("state.json event_log must be an object")
    _exact_keys(event_log, {"sha256", "size", "count"}, label="state.json event_log")
    _validate_identity(
        {"sha256": event_log["sha256"], "size": event_log["size"]},
        label="state.json event_log",
    )
    if not _non_bool_int(event_log["count"]) or event_log["count"] < 0:
        raise EvidenceError("state.json event_log.count is invalid")
    if event_log["count"] > MAX_EVENT_COUNT:
        raise EvidenceError("state.json event_log.count exceeds the supported limit")
    pending_event = state["pending_event"]
    if pending_event is not None:
        if event_log["count"] >= MAX_EVENT_COUNT:
            raise EvidenceError(
                "state.json cannot have a pending event after the event-count limit"
            )
        _validate_event_record(
            pending_event,
            plan,
            record_number=event_log["count"] + 1,
            previous_sha256=event_log["sha256"],
        )

    tasks = state["tasks"]
    if not isinstance(tasks, dict):
        raise EvidenceError("state.json tasks must be an object")
    task_specs = {task["id"]: task for task in plan["suite"]["tasks"]}
    if set(tasks) != set(task_specs):
        raise EvidenceError("state.json task ids do not match plan.json")
    completed_flags: list[bool] = []
    total_attempts = 0
    for task_id, task in task_specs.items():
        task_state = tasks[task_id]
        label = f"state.json task {task_id!r}"
        if not isinstance(task_state, dict):
            raise EvidenceError(f"{label} must be an object")
        _exact_keys(
            task_state,
            {"status", "attempts", "verified_inputs", "verified_outputs"},
            label=label,
        )
        status = task_state["status"]
        if not isinstance(status, str) or status not in {"pending", "running", "failed", "completed"}:
            raise EvidenceError(f"{label}.status is invalid")
        attempts = task_state["attempts"]
        if not isinstance(attempts, list):
            raise EvidenceError(f"{label}.attempts must be an array")
        if len(attempts) > MAX_ATTEMPTS_PER_TASK:
            raise EvidenceError(
                f"{label}.attempts exceeds the {MAX_ATTEMPTS_PER_TASK}-attempt limit"
            )
        total_attempts += len(attempts)
        if total_attempts > MAX_TOTAL_ATTEMPTS:
            raise EvidenceError(
                f"state.json exceeds the {MAX_TOTAL_ATTEMPTS}-attempt global limit"
            )
        validated_attempts = [
            _validate_attempt(attempt, task=task, index=index)
            for index, attempt in enumerate(attempts)
        ]
        for previous in validated_attempts[:-1]:
            if previous["status"] not in {"failed", "interrupted"}:
                raise EvidenceError(f"{label} has an impossible non-final attempt")
            if "quarantined_outputs" not in previous:
                raise EvidenceError(f"{label} has an unrecovered attempt before a successor")
        _validate_identity_map(
            task_state["verified_inputs"],
            label=f"{label}.verified_inputs",
            expected_paths=task["inputs"] if status == "completed" else (),
        )
        _validate_identity_map(
            task_state["verified_outputs"],
            label=f"{label}.verified_outputs",
            expected_paths=task["outputs"] if status == "completed" else (),
        )
        latest = validated_attempts[-1] if validated_attempts else None
        if status == "pending":
            if latest is not None and (
                latest["status"] not in {"failed", "interrupted"}
                or "quarantined_outputs" not in latest
            ):
                raise EvidenceError(f"{label} pending status is inconsistent with its attempts")
        elif latest is None:
            raise EvidenceError(f"{label} status requires an attempt")
        elif status == "running":
            if latest["status"] != "running" or "quarantined_outputs" in latest:
                raise EvidenceError(f"{label} running status is inconsistent with its latest attempt")
        elif status == "failed":
            if (
                latest["status"] not in {"failed", "interrupted"}
                or "quarantined_outputs" in latest
            ):
                raise EvidenceError(f"{label} failed status is inconsistent with its latest attempt")
        elif status == "completed":
            if latest["status"] != "completed":
                raise EvidenceError(f"{label} completed status is inconsistent with its latest attempt")
            if task_state["verified_inputs"] != latest["verified_inputs"]:
                raise EvidenceError(f"{label} verified inputs differ from its completed attempt")
            if task_state["verified_outputs"] != latest["verified_outputs"]:
                raise EvidenceError(f"{label} verified outputs differ from its completed attempt")
        completed_flags.append(status == "completed")

    saw_incomplete = False
    for task in plan["suite"]["tasks"]:
        task_state = tasks[task["id"]]
        if task_state["status"] == "completed":
            if saw_incomplete:
                raise EvidenceError(
                    "completed tasks in state.json do not form an ordered prefix"
                )
            continue
        if not saw_incomplete:
            saw_incomplete = True
            continue
        if task_state["status"] != "pending" or task_state["attempts"]:
            raise EvidenceError(
                "tasks after the first incomplete task must be untouched and pending"
            )
    if state["status"] == "completed" and not all(completed_flags):
        raise EvidenceError("completed run state has incomplete tasks")
    if state["status"] == "completed" and state["last_error"] is not None:
        raise EvidenceError("completed run state must not have last_error")
    if state["status"] == "failed" and not isinstance(state["last_error"], str):
        raise EvidenceError("failed run state must have last_error")


def _validate_attempt_artifacts(
    run_root: Path,
    state: dict[str, Any],
    plan: dict[str, Any],
) -> None:
    referenced_logs: set[str] = set()
    referenced_quarantine: dict[str, dict[str, Any]] = {}
    for task in plan["suite"]["tasks"]:
        for attempt in state["tasks"][task["id"]]["attempts"]:
            for stream_name in ("stdout", "stderr"):
                relative = attempt[stream_name]
                if relative in referenced_logs:
                    raise EvidenceError(f"attempt log is referenced twice: {relative!r}")
                member_identity(
                    run_root,
                    relative,
                    label=f"attempt {task['id']!r} {stream_name}",
                )
                referenced_logs.add(relative)
            for record in attempt.get("quarantined_outputs", []):
                artifact = record["artifact"]
                if artifact in referenced_quarantine:
                    raise EvidenceError(
                        f"quarantine artifact is referenced twice: {artifact!r}"
                    )
                actual = member_identity(
                    run_root,
                    artifact,
                    label=f"quarantine artifact {artifact!r}",
                )
                _identity_or_raise(
                    actual,
                    record,
                    label=f"quarantine artifact {artifact!r}",
                )
                referenced_quarantine[artifact] = record

    first_incomplete = next(
        (
            task
            for task in plan["suite"]["tasks"]
            if state["tasks"][task["id"]]["status"] != "completed"
        ),
        None,
    )

    recoverable_orphan_logs: set[str] = set()
    recoverable_quarantine: set[str] = set()
    if first_incomplete is not None:
        task_id = first_incomplete["id"]
        task_state = state["tasks"][task_id]
        attempts = task_state["attempts"]
        if task_state["status"] == "pending":
            attempt_number = len(attempts) + 1
            recoverable_orphan_logs = {
                f"{LOGS_DIRECTORY}/{task_id}/attempt-{attempt_number:04d}.stdout.log",
                f"{LOGS_DIRECTORY}/{task_id}/attempt-{attempt_number:04d}.stderr.log",
            }
        elif (
            task_state["status"] in {"running", "failed"}
            and attempts
            and "quarantined_outputs" not in attempts[-1]
        ):
            attempt_number = attempts[-1]["number"]
            recoverable_quarantine = {
                f"{QUARANTINE_DIRECTORY}/"
                f"{_quarantine_name(task_id, attempt_number, relative)}"
                for relative in first_incomplete["outputs"]
            }

    current_logs = set(iter_regular_artifacts(run_root, (LOGS_DIRECTORY,)))
    missing_logs = referenced_logs - current_logs
    unexpected_logs = current_logs - referenced_logs
    if missing_logs:
        raise EvidenceError(
            f"referenced attempt logs are missing: {sorted(missing_logs)}"
        )
    if not unexpected_logs.issubset(recoverable_orphan_logs):
        raise EvidenceError(
            "log files do not exactly match attempt records or the next "
            "recoverable empty log pair"
        )
    for relative in unexpected_logs:
        identity = member_identity(
            run_root,
            relative,
            label=f"recoverable orphan log {relative!r}",
        )
        if identity["size"] != 0:
            raise EvidenceError(
                f"recoverable orphan log must be empty: {relative!r}"
            )

    current_quarantine = set(
        iter_regular_artifacts(run_root, (QUARANTINE_DIRECTORY,))
    )
    missing_quarantine = set(referenced_quarantine) - current_quarantine
    unexpected_quarantine = current_quarantine - set(referenced_quarantine)
    if missing_quarantine:
        raise EvidenceError(
            f"referenced quarantine artifacts are missing: {sorted(missing_quarantine)}"
        )
    if not unexpected_quarantine.issubset(recoverable_quarantine):
        raise EvidenceError(
            "quarantine files do not exactly match attempt records or a "
            "deterministic interrupted recovery"
        )


def _validate_run_root_topology(run_root: Path) -> None:
    """Require the exact durable run-root file and directory topology."""

    required_files = {PLAN_FILE, STATE_FILE, EVENTS_FILE}
    optional_files = {BUNDLE_FILE}
    required_directories = {LOGS_DIRECTORY, QUARANTINE_DIRECTORY}
    seen: set[str] = set()
    try:
        entries = list(os.scandir(run_root))
        for entry in entries:
            name = entry.name
            seen.add(name)
            if entry.is_symlink():
                raise EvidenceError(
                    f"run directory root entry must not be a symlink: {name!r}"
                )
            if name in required_files or name in optional_files:
                if not entry.is_file(follow_symlinks=False):
                    raise EvidenceError(
                        f"run directory root entry must be a regular file: {name!r}"
                    )
                continue
            if name in required_directories:
                if not entry.is_dir(follow_symlinks=False):
                    raise EvidenceError(
                        f"run directory root entry must be a directory: {name!r}"
                    )
                continue
            raise EvidenceError(
                f"run directory contains an unexpected root entry: {name!r}"
            )
    except EvidenceError:
        raise
    except OSError as exc:
        raise EvidenceError(f"unable to inspect run directory root: {exc}") from exc

    missing = (required_files | required_directories) - seen
    if missing:
        raise EvidenceError(
            f"run directory is missing required root entries: {sorted(missing)}"
        )


def _load_context(run_directory: Path | str) -> _RunContext:
    run_root = checked_directory(Path(run_directory).absolute(), label="run directory")
    _validate_run_root_topology(run_root)
    plan = read_json_file(run_root / PLAN_FILE, label=PLAN_FILE)
    _validate_plan_shape(plan)
    if plan["run_directory"] != str(run_root):
        raise EvidenceError("run directory does not match the path bound in plan.json")

    state = read_json_file(run_root / STATE_FILE, label=STATE_FILE)
    _validate_state_shape(state, plan)
    current_plan_identity = file_identity(run_root / PLAN_FILE, label=PLAN_FILE)
    if state["plan_sha256"] != current_plan_identity["sha256"]:
        raise EvidenceError("state.json is not bound to the current plan.json")

    suite_file = plan["suite_file"]
    try:
        suite = load_suite(suite_file["path"])
    except BenchHandoffError as exc:
        raise EvidenceError(f"suite bound by plan.json cannot be loaded: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise EvidenceError("suite path bound by plan.json is invalid") from exc
    if str(suite.root) != plan["suite_root"]:
        raise EvidenceError("current suite root does not match plan.json")
    _identity_or_raise(suite.identity, suite_file, label="suite.toml")
    if suite.normalized() != plan["suite"]:
        raise EvidenceError("normalized suite no longer matches plan.json")
    require_separate_trees(run_root, suite.root, labels=("run directory", "suite root"))

    logs_root = checked_directory(run_root / LOGS_DIRECTORY, label=LOGS_DIRECTORY)
    quarantine_root = checked_directory(
        run_root / QUARANTINE_DIRECTORY,
        label=QUARANTINE_DIRECTORY,
    )
    require_same_filesystem(
        suite.root,
        quarantine_root,
        labels=("suite root", "quarantine directory"),
    )
    for task in suite.tasks:
        for relative in task.outputs:
            output = resolve_member(suite.root, relative, label=f"output {relative!r}")
            output_parent = nearest_existing_directory(
                output.parent,
                label=f"existing output parent for {relative!r}",
            )
            require_same_filesystem(
                output_parent,
                quarantine_root,
                labels=(f"output parent for {relative!r}", "quarantine directory"),
            )

    if set(plan["seed_inputs"]) != set(suite.seed_inputs):
        raise EvidenceError("plan.json seed input set does not match suite.toml")
    for relative, expected in plan["seed_inputs"].items():
        actual = member_identity(suite.root, relative, label=f"seed input {relative!r}")
        _identity_or_raise(actual, expected, label=f"seed input {relative!r}")

    file_identity(_event_path(run_root), label=EVENTS_FILE)
    _event_transition_status(run_root, plan, state)
    _validate_attempt_artifacts(run_root, state, plan)
    del logs_root
    return _RunContext(suite=suite, run_root=run_root, plan=plan, state=state)

def _producer_map(suite: SuiteSpec) -> dict[str, str]:
    return {
        output: task.task_id
        for task in suite.tasks
        for output in task.outputs
    }


def _expected_inputs(
    context: _RunContext,
    task: TaskSpec,
) -> dict[str, dict[str, Any]]:
    producers = _producer_map(context.suite)
    inputs: dict[str, dict[str, Any]] = {}
    for relative in task.inputs:
        if relative in context.plan["seed_inputs"]:
            expected = context.plan["seed_inputs"][relative]
        else:
            producer_id = producers.get(relative)
            if producer_id is None:
                raise EvidenceError(f"task {task.task_id!r} input has no producer: {relative!r}")
            producer_state = context.state["tasks"][producer_id]
            if producer_state["status"] != "completed":
                raise EvidenceError(
                    f"task {task.task_id!r} input producer is not complete: {producer_id!r}"
                )
            expected = producer_state["verified_outputs"].get(relative)
            if not isinstance(expected, dict):
                raise EvidenceError(
                    f"producer {producer_id!r} has no verified identity for {relative!r}"
                )
        actual = member_identity(
            context.suite.root,
            relative,
            label=f"input {relative!r} for task {task.task_id!r}",
        )
        _identity_or_raise(
            actual,
            expected,
            label=f"input {relative!r} for task {task.task_id!r}",
        )
        inputs[relative] = actual
    return inputs


def _verify_completed_task(context: _RunContext, task: TaskSpec) -> None:
    task_state = context.state["tasks"][task.task_id]
    if set(task_state["verified_outputs"]) != set(task.outputs):
        raise EvidenceError(f"completed task {task.task_id!r} has an incomplete output record")
    for relative in task.outputs:
        actual = member_identity(
            context.suite.root,
            relative,
            label=f"completed output {relative!r}",
        )
        _identity_or_raise(
            actual,
            task_state["verified_outputs"][relative],
            label=f"completed output {relative!r}",
        )


def _quarantine_name(task_id: str, attempt_number: int, relative: str) -> str:
    """Return one fixed-length name derived from exact task, attempt, and source."""

    identity = hashlib.sha256(
        f"{task_id}\0{attempt_number}\0{relative}".encode("utf-8")
    ).hexdigest()
    return f"a{attempt_number:08d}.{identity}.artifact"

def _assert_recovery_liveness(context: _RunContext) -> None:
    """Refuse resume while the latest running child may still be the same process."""

    for task in context.suite.tasks:
        task_state = context.state["tasks"][task.task_id]
        if task_state["status"] != "running":
            continue
        attempts = task_state["attempts"]
        if not attempts or attempts[-1]["status"] != "running":
            raise EvidenceError(f"running task {task.task_id!r} lacks its latest running attempt")
        attempt = attempts[-1]
        if attempt["child_launch_guard"]:
            raise EvidenceError(
                f"task {task.task_id!r} attempt {attempt['number']} has an "
                "unresolved child launch guard; refusing resume"
            )
        process_id = attempt["child_pid"]
        if process_id is None:
            raise EvidenceError(
                f"task {task.task_id!r} running attempt has no child identity"
            )
        recorded_token = attempt["child_start_token"]
        current_token = process_start_token(process_id)
        if current_token is not None and current_token != recorded_token:
            # The numeric PID was reused; it is not the recorded child.
            continue
        liveness = process_liveness(process_id)
        attempt_number = attempt["number"]
        if liveness == "alive":
            raise EvidenceError(
                f"task {task.task_id!r} attempt {attempt_number} child process "
                f"{process_id} is still alive; refusing resume"
            )
        if liveness == "unknown":
            raise EvidenceError(
                f"task {task.task_id!r} attempt {attempt_number} child process "
                f"{process_id} identity or liveness is unknown; refusing resume"
            )

def _recover_incomplete_task(context: _RunContext, task: TaskSpec) -> None:
    task_state = context.state["tasks"][task.task_id]
    previous_status = task_state["status"]
    attempts = task_state["attempts"]
    if not attempts:
        raise EvidenceError(f"incomplete task {task.task_id!r} has no attempt record")
    latest = attempts[-1]
    attempt_number = latest.get("number")
    if not isinstance(attempt_number, int) or attempt_number <= 0:
        raise EvidenceError(f"task {task.task_id!r} has an invalid attempt number")

    _assert_recovery_liveness(context)
    if previous_status == "running":
        latest["status"] = "interrupted"
        latest["ended_at"] = utc_now()
        latest["interruption_reason"] = "runner did not record a terminal child result"
        if latest["child_pid"] is not None and latest["return_code"] is None:
            latest["return_code_unavailable_reason"] = (
                "the recorded child identity was no longer live when resume began"
            )

    quarantined: list[dict[str, Any]] = []
    quarantine_root = checked_directory(
        context.run_root / QUARANTINE_DIRECTORY,
        label=QUARANTINE_DIRECTORY,
    )
    for relative in task.outputs:
        source = resolve_member(
            context.suite.root,
            relative,
            label=f"unverified output {relative!r}",
        )
        destination_name = _quarantine_name(task.task_id, attempt_number, relative)
        destination = quarantine_root / destination_name
        source_exists = os.path.lexists(source)
        destination_exists = os.path.lexists(destination)
        if source_exists and destination_exists:
            raise EvidenceError(
                f"both unverified output and its quarantine destination exist for {relative!r}"
            )
        artifact = f"{QUARANTINE_DIRECTORY}/{destination_name}"
        if source_exists:
            identity = move_regular_same_filesystem(
                source,
                destination,
                label=f"quarantine output {relative!r}",
            )
        elif destination_exists:
            identity = file_identity(
                destination,
                label=f"recovered quarantine artifact for {relative!r}",
            )
        else:
            continue
        quarantined.append(
            {
                "source": relative,
                "artifact": artifact,
                **identity,
            }
        )

    recorded_quarantine = latest.get("quarantined_outputs")
    if recorded_quarantine is not None and recorded_quarantine != quarantined:
        raise EvidenceError(
            f"task {task.task_id!r} quarantine identities changed during recovery"
        )
    latest["quarantined_outputs"] = quarantined
    task_state["status"] = "pending"
    task_state["verified_inputs"] = {}
    task_state["verified_outputs"] = {}
    _commit_transition(
        context,
        "task_recovery_prepared",
        task_id=task.task_id,
        details={
            "previous_status": previous_status,
            "attempt": attempt_number,
            "quarantined_outputs": len(quarantined),
        },
    )


def _task_log_directory(context: _RunContext, task: TaskSpec) -> Path:
    logs_root = checked_directory(context.run_root / LOGS_DIRECTORY, label=LOGS_DIRECTORY)
    destination = logs_root / task.task_id
    if os.path.lexists(destination):
        return checked_directory(destination, label=f"log directory for {task.task_id!r}")
    destination.mkdir(mode=0o700)
    return checked_directory(destination, label=f"log directory for {task.task_id!r}")


def _open_attempt_log(path: Path, *, create: bool, label: str) -> Any:
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    else:
        try:
            existing = file_identity(path, label=label)
        except BoundaryError as exc:
            raise EvidenceError(str(exc)) from exc
        if existing["size"] != 0:
            raise EvidenceError(f"{label} must be an empty regular file")
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != 0:
            raise EvidenceError(f"{label} must be an empty regular file")
        return os.fdopen(descriptor, "ab", closefd=True)
    except Exception:
        os.close(descriptor)
        raise


def _prepare_attempt_logs(
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[Any, Any]:
    stdout_exists = os.path.lexists(stdout_path)
    stderr_exists = os.path.lexists(stderr_path)
    stdout_handle: Any | None = None
    stderr_handle: Any | None = None
    created_paths: list[Path] = []
    try:
        if not stdout_exists:
            created_paths.append(stdout_path)
        stdout_handle = _open_attempt_log(
            stdout_path,
            create=not stdout_exists,
            label="attempt stdout log",
        )
        if not stderr_exists:
            created_paths.append(stderr_path)
        stderr_handle = _open_attempt_log(
            stderr_path,
            create=not stderr_exists,
            label="attempt stderr log",
        )
        return stdout_handle, stderr_handle
    except Exception as exc:
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None:
            stderr_handle.close()
        for candidate in created_paths:
            try:
                if os.path.lexists(candidate) and file_identity(
                    candidate,
                    label="partially prepared attempt log",
                )["size"] == 0:
                    candidate.unlink()
            except BenchHandoffError:
                pass
        if isinstance(exc, BenchHandoffError):
            raise
        raise EvidenceError(f"unable to prepare attempt logs: {exc}") from exc


def _finish_failed_attempt(
    context: _RunContext,
    task: TaskSpec,
    attempt: dict[str, Any],
    *,
    status: str,
    detail: str,
    return_code: int | None,
) -> None:
    attempt["child_launch_guard"] = False
    attempt["status"] = status
    attempt["ended_at"] = utc_now()
    attempt["return_code"] = return_code
    attempt["error"] = detail
    task_state = context.state["tasks"][task.task_id]
    task_state["status"] = "failed"
    context.state["status"] = "failed"
    context.state["last_error"] = f"{task.task_id}: {detail}"
    _commit_transition(
        context,
        "task_failed",
        task_id=task.task_id,
        details={
            "attempt": attempt["number"],
            "return_code": return_code,
            "reason": detail,
        },
    )


def _run_task(context: _RunContext, task: TaskSpec) -> bool:
    task_state = context.state["tasks"][task.task_id]
    verified_inputs = _expected_inputs(context, task)
    for relative in task.outputs:
        ensure_output_parent_boundary(
            context.suite.root,
            relative,
            label=f"output {relative!r}",
        )
        ensure_member_absent(
            context.suite.root,
            relative,
            label=f"output {relative!r}",
        )

    attempt_number = len(task_state["attempts"]) + 1
    if attempt_number > MAX_ATTEMPTS_PER_TASK:
        raise EvidenceError(
            f"task {task.task_id!r} exceeds the "
            f"{MAX_ATTEMPTS_PER_TASK}-attempt limit"
        )
    total_attempts = sum(
        len(item["attempts"]) for item in context.state["tasks"].values()
    )
    if total_attempts >= MAX_TOTAL_ATTEMPTS:
        raise EvidenceError(
            f"run exceeds the {MAX_TOTAL_ATTEMPTS}-attempt global limit"
        )
    log_directory = _task_log_directory(context, task)
    stdout_path = log_directory / f"attempt-{attempt_number:04d}.stdout.log"
    stderr_path = log_directory / f"attempt-{attempt_number:04d}.stderr.log"
    stdout_relative = stdout_path.relative_to(context.run_root).as_posix()
    stderr_relative = stderr_path.relative_to(context.run_root).as_posix()
    stdout_handle, stderr_handle = _prepare_attempt_logs(stdout_path, stderr_path)

    attempt: dict[str, Any] = {
        "number": attempt_number,
        "status": "running",
        "started_at": utc_now(),
        "ended_at": None,
        "child_pid": None,
        "child_start_token": None,
        "child_launch_guard": True,
        "return_code": None,
        "argv": list(task.argv),
        "verified_inputs": verified_inputs,
        "stdout": stdout_relative,
        "stderr": stderr_relative,
    }
    task_state["attempts"].append(attempt)
    task_state["status"] = "running"
    context.state["status"] = "running"
    context.state["last_error"] = None
    try:
        _commit_transition(
            context,
            "task_started",
            task_id=task.task_id,
            details={"attempt": attempt_number},
        )
    except Exception:
        stdout_handle.close()
        stderr_handle.close()
        raise

    environment = os.environ.copy()
    environment.update(
        {
            "BENCHHANDOFF_RUN_ID": context.plan["run_id"],
            "BENCHHANDOFF_TASK_ID": task.task_id,
            "BENCHHANDOFF_ATTEMPT": str(attempt_number),
        }
    )

    return_code: int | None = None
    launch_error: str | None = None
    interrupted = False
    process: subprocess.Popen[bytes] | None = None
    try:
        try:
            process = subprocess.Popen(
                list(task.argv),
                cwd=context.suite.root,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                env=environment,
                shell=False,
                close_fds=True,
            )
        except OSError as exc:
            attempt["child_launch_guard"] = False
            launch_error = f"child launch failed: {exc}"
        if process is not None:
            attempt["child_pid"] = process.pid
            captured_token = process_start_token(process.pid)
            if captured_token is None:
                attempt["child_start_token"] = "unavailable"
                try:
                    return_code = stop_process(process)
                except EvidenceError as control_exc:
                    raise EvidenceError(
                        "child start identity was unavailable and shutdown could not be confirmed"
                    ) from control_exc
                attempt["child_launch_guard"] = False
                launch_error = "unable to capture a stable child process start identity"
            else:
                attempt["child_start_token"] = captured_token
                attempt["child_launch_guard"] = False
                try:
                    _write_state(context)
                except KeyboardInterrupt:
                    interrupted = True
                    return_code = stop_process(process)
                except Exception as exc:
                    try:
                        return_code = stop_process(process)
                    except EvidenceError as control_exc:
                        raise EvidenceError(
                            "child identity state write failed and child shutdown could not be confirmed"
                        ) from control_exc
                    if isinstance(exc, BenchHandoffError):
                        raise
                    launch_error = (
                        "unable to persist child identity before monitoring: "
                        f"{type(exc).__name__}: {exc}"
                    )
                if launch_error is None and not interrupted:
                    try:
                        return_code = process.wait()
                    except KeyboardInterrupt:
                        interrupted = True
                        return_code = stop_process(process)
                    except OSError as exc:
                        try:
                            return_code = stop_process(process)
                        except EvidenceError as control_exc:
                            raise EvidenceError(
                                "child wait failed and child shutdown could not be confirmed"
                            ) from control_exc
                        launch_error = f"child wait failed: {exc}"
    finally:
        stdout_handle.close()
        stderr_handle.close()
    if launch_error is not None:
        _finish_failed_attempt(
            context,
            task,
            attempt,
            status="failed",
            detail=launch_error,
            return_code=return_code,
        )
        return False
    if interrupted:
        _finish_failed_attempt(
            context,
            task,
            attempt,
            status="interrupted",
            detail="runner received KeyboardInterrupt",
            return_code=return_code,
        )
        return False
    if return_code != 0:
        _finish_failed_attempt(
            context,
            task,
            attempt,
            status="failed",
            detail=f"child process exited non-zero ({return_code})",
            return_code=return_code,
        )
        return False

    try:
        for relative, expected in verified_inputs.items():
            current = member_identity(
                context.suite.root,
                relative,
                label=f"post-run input {relative!r}",
            )
            _identity_or_raise(
                current,
                expected,
                label=f"post-run input {relative!r}",
            )
        verified_outputs = {
            relative: member_identity(
                context.suite.root,
                relative,
                label=f"output {relative!r}",
            )
            for relative in task.outputs
        }
    except BenchHandoffError as exc:
        _finish_failed_attempt(
            context,
            task,
            attempt,
            status="failed",
            detail=f"output or input validation failed: {exc}",
            return_code=return_code,
        )
        return False

    attempt["status"] = "completed"
    attempt["ended_at"] = utc_now()
    attempt["return_code"] = return_code
    attempt["verified_outputs"] = verified_outputs
    task_state["status"] = "completed"
    task_state["verified_inputs"] = verified_inputs
    task_state["verified_outputs"] = verified_outputs
    _commit_transition(
        context,
        "task_completed",
        task_id=task.task_id,
        details={"attempt": attempt_number, "outputs": len(verified_outputs)},
    )
    return True


def _artifact_paths(run_root: Path) -> list[str]:
    dynamic = iter_regular_artifacts(
        run_root,
        (LOGS_DIRECTORY, QUARANTINE_DIRECTORY),
    )
    return sorted([PLAN_FILE, STATE_FILE, EVENTS_FILE, *dynamic])


def _final_outputs(context: _RunContext) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for task in context.suite.tasks:
        task_state = context.state["tasks"][task.task_id]
        for relative in task.outputs:
            identity = task_state["verified_outputs"][relative]
            outputs.append({"task_id": task.task_id, "path": relative, **identity})
    return outputs


def _build_bundle(context: _RunContext) -> dict[str, Any]:
    if context.state["pending_event"] is not None:
        raise EvidenceError("cannot build a bundle with a pending transition")
    if _event_transition_status(context.run_root, context.plan, context.state) != "stable":
        raise EvidenceError("cannot build a bundle from unstable event/state evidence")
    bundle_path = context.run_root / BUNDLE_FILE
    if os.path.lexists(bundle_path):
        raise EvidenceError("bundle.json already exists; refusing to overwrite evidence")

    run_artifacts = []
    for relative in _artifact_paths(context.run_root):
        identity = member_identity(
            context.run_root,
            relative,
            label=f"run artifact {relative!r}",
        )
        run_artifacts.append({"path": relative, **identity})

    bundle = {
        "schema_version": SCHEMA_VERSION,
        "kind": "benchhandoff-bundle",
        "run_id": context.plan["run_id"],
        "created_at": utc_now(),
        "suite_file": {
            "path": str(context.suite.path),
            **context.suite.identity,
        },
        "seed_inputs": context.plan["seed_inputs"],
        "verified_outputs": _final_outputs(context),
        "run_artifacts": run_artifacts,
    }
    atomic_write_json(bundle_path, bundle)
    return bundle


def _verify_completed_prefix(context: _RunContext) -> None:
    """Verify every completed task before any resume transition is committed."""

    saw_incomplete = False
    for task in context.suite.tasks:
        task_state = context.state["tasks"][task.task_id]
        if task_state["status"] == "completed":
            if saw_incomplete:
                raise EvidenceError("completed tasks must form one ordered prefix")
            _verify_completed_task(context, task)
            continue
        saw_incomplete = True


def _assert_resume_attempt_budget(context: _RunContext) -> None:
    """Refuse a resume that cannot start its next attempt without mutation."""

    total_attempts = sum(
        len(item["attempts"]) for item in context.state["tasks"].values()
    )
    for task in context.suite.tasks:
        task_state = context.state["tasks"][task.task_id]
        if task_state["status"] == "completed":
            continue
        if len(task_state["attempts"]) >= MAX_ATTEMPTS_PER_TASK:
            raise EvidenceError(
                f"task {task.task_id!r} has exhausted the "
                f"{MAX_ATTEMPTS_PER_TASK}-attempt limit"
            )
        if total_attempts >= MAX_TOTAL_ATTEMPTS:
            raise EvidenceError(
                f"run has exhausted the {MAX_TOTAL_ATTEMPTS}-attempt global limit"
            )
        return


def _resume_decision_sha256(decision: dict[str, Any]) -> str:
    """Hash a resume decision without its self-referential digest field."""

    body = dict(decision)
    body.pop("decision_sha256", None)
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def _current_output_observation(
    context: _RunContext,
    task: TaskSpec,
) -> list[dict[str, Any]]:
    """Describe every output of an incomplete task without changing it."""

    observations: list[dict[str, Any]] = []
    for relative in task.outputs:
        candidate = resolve_member(
            context.suite.root,
            relative,
            label=f"unverified output {relative!r}",
        )
        if not os.path.lexists(candidate):
            observations.append({"path": relative, "status": "absent"})
            continue
        identity = file_identity(candidate, label=f"unverified output {relative!r}")
        observations.append({"path": relative, "status": "present", **identity})
    return observations


def _quarantine_observation(
    context: _RunContext,
    task: TaskSpec,
) -> list[dict[str, Any]]:
    """Describe deterministic recovery destinations for the next task."""

    task_state = context.state["tasks"][task.task_id]
    if task_state["status"] not in {"running", "failed"}:
        return []
    attempts = task_state["attempts"]
    if not attempts:
        raise EvidenceError(f"incomplete task {task.task_id!r} has no attempt record")
    attempt_number = attempts[-1]["number"]
    observations: list[dict[str, Any]] = []
    for relative in task.outputs:
        artifact = (
            f"{QUARANTINE_DIRECTORY}/"
            f"{_quarantine_name(task.task_id, attempt_number, relative)}"
        )
        candidate = resolve_member(
            context.run_root,
            artifact,
            label=f"quarantine candidate for {relative!r}",
        )
        if not os.path.lexists(candidate):
            observations.append(
                {
                    "source": relative,
                    "artifact": artifact,
                    "status": "absent",
                }
            )
            continue
        identity = file_identity(
            candidate,
            label=f"quarantine candidate for {relative!r}",
        )
        observations.append(
            {
                "source": relative,
                "artifact": artifact,
                "status": "present",
                **identity,
            }
        )
    return observations


def _build_resume_decision(context: _RunContext) -> dict[str, Any]:
    """Build a deterministic, mutation-free authorization view of one run."""

    if _event_transition_status(context.run_root, context.plan, context.state) != "stable":
        raise EvidenceError(
            "resume decision requires stable event/state evidence; "
            "resume without a decision token may reconcile the pending transition"
        )
    _verify_completed_prefix(context)
    _assert_recovery_liveness(context)

    completed_prefix: list[str] = []
    completed_outputs: list[dict[str, Any]] = []
    next_task_spec: TaskSpec | None = None
    for task in context.suite.tasks:
        task_state = context.state["tasks"][task.task_id]
        if task_state["status"] == "completed":
            completed_prefix.append(task.task_id)
            for relative in task.outputs:
                identity = member_identity(
                    context.suite.root,
                    relative,
                    label=f"completed output {relative!r}",
                )
                completed_outputs.append(
                    {"task_id": task.task_id, "path": relative, **identity}
                )
            continue
        next_task_spec = task
        break

    bundle_path = context.run_root / BUNDLE_FILE
    bundle_exists = os.path.lexists(bundle_path)
    if bundle_exists:
        if next_task_spec is not None or context.state["status"] != "completed":
            raise EvidenceError("bundle.json exists for a non-completed run")
        action = "already-complete"
    elif next_task_spec is None:
        if context.state["status"] != "completed":
            raise EvidenceError("all tasks are complete but run status is not completed")
        action = "seal-completed-run"
    else:
        _assert_resume_attempt_budget(context)
        next_status = context.state["tasks"][next_task_spec.task_id]["status"]
        action = (
            "recover-and-resume"
            if next_status in {"running", "failed"}
            else "resume"
        )

    evidence_paths = _artifact_paths(context.run_root)
    if bundle_exists:
        evidence_paths.append(BUNDLE_FILE)
    evidence_files = [
        {
            "path": relative,
            **member_identity(
                context.run_root,
                relative,
                label=f"resume evidence {relative!r}",
            ),
        }
        for relative in sorted(evidence_paths)
    ]

    next_task: dict[str, Any] | None = None
    if next_task_spec is not None:
        task_state = context.state["tasks"][next_task_spec.task_id]
        verified_inputs = _expected_inputs(context, next_task_spec)
        output_observations = _current_output_observation(context, next_task_spec)
        quarantine = _quarantine_observation(context, next_task_spec)
        output_by_path = {item["path"]: item for item in output_observations}
        quarantine_by_source = {item["source"]: item for item in quarantine}
        for relative in next_task_spec.outputs:
            source_present = output_by_path[relative]["status"] == "present"
            destination_present = (
                quarantine_by_source.get(relative, {}).get("status") == "present"
            )
            if source_present and destination_present:
                raise EvidenceError(
                    "both unverified output and its quarantine destination exist "
                    f"for {relative!r}"
                )
        next_task = {
            "id": next_task_spec.task_id,
            "current_status": task_state["status"],
            "next_attempt": len(task_state["attempts"]) + 1,
            "verified_inputs": [
                {"path": relative, **identity}
                for relative, identity in sorted(verified_inputs.items())
            ],
            "unverified_outputs": output_observations,
            "quarantine_candidates": quarantine,
        }

    decision: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "benchhandoff-resume-decision",
        "run_id": context.plan["run_id"],
        "run_directory": str(context.run_root),
        "action": action,
        "completed_prefix": completed_prefix,
        "completed_outputs": completed_outputs,
        "next_task": next_task,
        "suite_file": {
            "path": str(context.suite.path),
            **file_identity(context.suite.path, label="suite.toml"),
        },
        "evidence_files": evidence_files,
    }
    decision["decision_sha256"] = _resume_decision_sha256(decision)
    return decision


def _inspect_resume_checked(run_directory: Path | str) -> dict[str, Any]:
    """Return a read-only resume decision bound to the current evidence bytes."""

    context = _load_context(run_directory)
    if os.path.lexists(context.run_root / BUNDLE_FILE):
        verify_run(context.run_root)
        context = _load_context(context.run_root)
    return _build_resume_decision(context)


def inspect_resume(run_directory: Path | str) -> dict[str, Any]:
    """Inspect resume eligibility without reconciling or mutating run evidence."""

    try:
        return _inspect_resume_checked(run_directory)
    except EvidenceError:
        raise
    except BenchHandoffError as exc:
        raise EvidenceError(f"run evidence is invalid: {exc}") from exc
    except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
        raise EvidenceError(
            f"run evidence could not be safely interpreted: {type(exc).__name__}: {exc}"
        ) from exc

def _execute_remaining(context: _RunContext, *, operation: str) -> RunResult:
    saw_incomplete = False
    for task in context.suite.tasks:
        task_state = context.state["tasks"][task.task_id]
        if task_state["status"] == "completed":
            if saw_incomplete:
                raise EvidenceError("completed tasks must form one ordered prefix")
            _verify_completed_task(context, task)
            continue

        saw_incomplete = True
        if task_state["status"] in {"running", "failed"}:
            _recover_incomplete_task(context, task)
        elif task_state["status"] != "pending":
            raise EvidenceError(f"task {task.task_id!r} cannot be resumed")

        if not _run_task(context, task):
            return RunResult(
                status="failed",
                run_directory=str(context.run_root),
                run_id=context.plan["run_id"],
                detail=context.state["last_error"] or "task failed closed",
            )

    context.state["status"] = "completed"
    context.state["last_error"] = None
    _commit_transition(
        context,
        "run_completed",
        details={"operation": operation, "tasks": len(context.suite.tasks)},
    )
    _build_bundle(context)
    verify_run(context.run_root)
    return RunResult(
        status="completed",
        run_directory=str(context.run_root),
        run_id=context.plan["run_id"],
        detail="all tasks completed and the evidence bundle verified",
    )


def _start_run_checked(suite_file: Path | str, run_directory: Path | str) -> RunResult:
    """Start one new suite in an absent, separate run directory."""

    suite = load_suite(suite_file)
    run_candidate = Path(run_directory).absolute()
    require_separate_trees(run_candidate, suite.root, labels=("run directory", "suite root"))
    run_parent = nearest_existing_directory(
        run_candidate.parent,
        label="existing run-directory parent",
    )
    require_same_filesystem(
        suite.root,
        run_parent,
        labels=("suite root", "run-directory parent"),
    )
    for task in suite.tasks:
        for relative in task.outputs:
            output = resolve_member(suite.root, relative, label=f"output {relative!r}")
            output_parent = nearest_existing_directory(
                output.parent,
                label=f"existing output parent for {relative!r}",
            )
            require_same_filesystem(
                output_parent,
                run_parent,
                labels=(f"output parent for {relative!r}", "run-directory parent"),
            )

    seed_inputs = _preflight_suite(suite)
    run_root = prepare_new_directory(run_candidate, label="run directory")
    (run_root / LOGS_DIRECTORY).mkdir(mode=0o700)
    (run_root / QUARANTINE_DIRECTORY).mkdir(mode=0o700)

    plan = _initial_plan(suite, run_root, seed_inputs)
    atomic_write_json(run_root / PLAN_FILE, plan)
    plan_identity = file_identity(run_root / PLAN_FILE, label=PLAN_FILE)
    create_empty_regular(run_root / EVENTS_FILE, label=EVENTS_FILE)
    event_identity = file_identity(run_root / EVENTS_FILE, label=EVENTS_FILE)
    state = _initial_state(plan, plan_identity, event_identity)
    atomic_write_json(run_root / STATE_FILE, state)
    context = _RunContext(suite=suite, run_root=run_root, plan=plan, state=state)
    _commit_transition(
        context,
        "run_started",
        details={"suite": suite.name, "tasks": len(suite.tasks)},
    )
    return _execute_remaining(context, operation="start")

def start_run(suite_file: Path | str, run_directory: Path | str) -> RunResult:
    """Start while converting operational failures into a stable evidence error."""

    try:
        writer_lock = WriterLock.acquire(run_directory)
        try:
            return _start_run_checked(suite_file, run_directory)
        finally:
            writer_lock.release()
    except BenchHandoffError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
        raise EvidenceError(
            f"run could not be safely started: {type(exc).__name__}: {exc}"
        ) from exc


def _resume_run_checked(
    run_directory: Path | str,
    *,
    expected_decision_sha256: str | None = None,
) -> RunResult:
    """Resume the first incomplete task after revalidating all prior evidence."""

    if expected_decision_sha256 is not None and (
        not isinstance(expected_decision_sha256, str)
        or _SHA256_PATTERN.fullmatch(expected_decision_sha256) is None
    ):
        raise EvidenceError(
            "expected resume decision SHA-256 must be 64 lowercase hexadecimal characters"
        )
    context = _load_context(run_directory)
    if expected_decision_sha256 is not None:
        actual_decision = _build_resume_decision(context)["decision_sha256"]
        if actual_decision != expected_decision_sha256:
            raise EvidenceError(
                "resume decision is stale: expected "
                f"{expected_decision_sha256}, got {actual_decision}"
            )
    bundle_path = context.run_root / BUNDLE_FILE
    if os.path.lexists(bundle_path):
        if context.state["pending_event"] is not None:
            raise EvidenceError("sealed bundle has an unacknowledged pending transition")
        if context.state["status"] != "completed":
            raise EvidenceError("bundle.json exists for a non-completed run")
        verify_run(context.run_root)
        return RunResult(
            status="completed",
            run_directory=str(context.run_root),
            run_id=context.plan["run_id"],
            detail="run was already complete and remains verified",
        )

    if expected_decision_sha256 is None:
        _reconcile_pending_event(context)
    _verify_completed_prefix(context)
    _assert_recovery_liveness(context)
    _assert_resume_attempt_budget(context)
    if expected_decision_sha256 is not None:
        actual_decision = _build_resume_decision(context)["decision_sha256"]
        if actual_decision != expected_decision_sha256:
            raise EvidenceError(
                "resume decision became stale before the first transition: expected "
                f"{expected_decision_sha256}, got {actual_decision}"
            )
    previous_status = context.state["status"]
    context.state["status"] = "running"
    context.state["last_error"] = None
    _commit_transition(
        context,
        "run_resumed",
        details={"previous_status": previous_status},
    )
    return _execute_remaining(context, operation="resume")

def resume_run(
    run_directory: Path | str,
    *,
    expected_decision_sha256: str | None = None,
) -> RunResult:
    """Resume while converting damaged evidence into a stable evidence error."""

    try:
        writer_lock = WriterLock.acquire(run_directory)
        try:
            return _resume_run_checked(
                run_directory,
                expected_decision_sha256=expected_decision_sha256,
            )
        finally:
            writer_lock.release()
    except EvidenceError:
        raise
    except BenchHandoffError as exc:
        raise EvidenceError(f"run evidence is invalid: {exc}") from exc
    except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
        raise EvidenceError(
            f"run evidence could not be safely interpreted: {type(exc).__name__}: {exc}"
        ) from exc

def _validate_bundle_shape(bundle: Any, plan: dict[str, Any]) -> None:
    if not isinstance(bundle, dict):
        raise EvidenceError("bundle.json must contain an object")
    required = {
        "schema_version",
        "kind",
        "run_id",
        "created_at",
        "suite_file",
        "seed_inputs",
        "verified_outputs",
        "run_artifacts",
    }
    _exact_keys(bundle, required, label="bundle.json")
    if (
        not _non_bool_int(bundle["schema_version"])
        or bundle["schema_version"] != SCHEMA_VERSION
        or bundle["kind"] != "benchhandoff-bundle"
    ):
        raise EvidenceError("bundle.json has an unsupported schema or kind")
    if bundle["run_id"] != plan["run_id"]:
        raise EvidenceError("bundle.json run_id does not match plan.json")
    _validate_text(bundle["created_at"], label="bundle.json created_at")
    _validate_file_record(bundle["suite_file"], label="bundle.json suite_file")
    if bundle["suite_file"] != plan["suite_file"]:
        raise EvidenceError("bundle.json suite_file does not match plan.json")
    _validate_identity_map(
        bundle["seed_inputs"],
        label="bundle.json seed_inputs",
        expected_paths=set(plan["seed_inputs"]),
    )

    task_specs = {task["id"]: task for task in plan["suite"]["tasks"]}
    outputs = bundle["verified_outputs"]
    if not isinstance(outputs, list):
        raise EvidenceError("bundle.json verified_outputs must be an array")
    output_keys: set[tuple[str, str]] = set()
    for index, output in enumerate(outputs):
        label = f"bundle.json verified_outputs[{index}]"
        if not isinstance(output, dict):
            raise EvidenceError(f"{label} must be an object")
        _exact_keys(output, {"task_id", "path", "sha256", "size"}, label=label)
        task_id = _validate_text(output["task_id"], label=f"{label}.task_id")
        path = _validate_portable_path(output["path"], label=f"{label}.path")
        if task_id not in task_specs or path not in task_specs[task_id]["outputs"]:
            raise EvidenceError(f"{label} does not match a declared task output")
        key = (task_id, path)
        if key in output_keys:
            raise EvidenceError(f"bundle.json contains duplicate output record: {key!r}")
        output_keys.add(key)
        _validate_identity(
            {"sha256": output["sha256"], "size": output["size"]},
            label=label,
        )

    artifacts = bundle["run_artifacts"]
    if not isinstance(artifacts, list):
        raise EvidenceError("bundle.json run_artifacts must be an array")
    artifact_paths: set[str] = set()
    for index, artifact in enumerate(artifacts):
        label = f"bundle.json run_artifacts[{index}]"
        if not isinstance(artifact, dict):
            raise EvidenceError(f"{label} must be an object")
        _exact_keys(artifact, {"path", "sha256", "size"}, label=label)
        path = _validate_portable_path(artifact["path"], label=f"{label}.path")
        if path in artifact_paths:
            raise EvidenceError(f"bundle.json contains duplicate artifact path: {path!r}")
        artifact_paths.add(path)
        _validate_identity(
            {"sha256": artifact["sha256"], "size": artifact["size"]},
            label=label,
        )

def _verify_run_checked(run_directory: Path | str) -> dict[str, Any]:
    """Re-hash the suite, inputs, outputs, state, and complete run-artifact set."""

    context = _load_context(run_directory)
    if context.state["pending_event"] is not None:
        raise EvidenceError("run has a pending transition; resume before verification")
    if context.state["status"] != "completed":
        raise EvidenceError(f"run is not complete: {context.state['status']}")
    bundle = read_json_file(context.run_root / BUNDLE_FILE, label=BUNDLE_FILE)
    _validate_bundle_shape(bundle, context.plan)

    if bundle["seed_inputs"] != context.plan["seed_inputs"]:
        raise EvidenceError("bundle seed inputs do not match plan.json")
    current_suite_identity = file_identity(context.suite.path, label="suite.toml")
    _identity_or_raise(
        current_suite_identity,
        bundle.get("suite_file", {}),
        label="bundle suite.toml",
    )

    expected_outputs = _final_outputs(context)
    if bundle["verified_outputs"] != expected_outputs:
        raise EvidenceError("bundle output records do not match state.json")
    for output in expected_outputs:
        actual = member_identity(
            context.suite.root,
            output["path"],
            label=f"verified output {output['path']!r}",
        )
        _identity_or_raise(actual, output, label=f"verified output {output['path']!r}")

    recorded_artifacts: dict[str, dict[str, Any]] = {}
    for artifact in bundle["run_artifacts"]:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise EvidenceError("bundle contains an invalid run artifact record")
        relative = artifact["path"]
        if relative in recorded_artifacts:
            raise EvidenceError(f"bundle contains duplicate artifact path: {relative!r}")
        recorded_artifacts[relative] = artifact

    current_paths = _artifact_paths(context.run_root)
    if set(recorded_artifacts) != set(current_paths):
        missing = sorted(set(recorded_artifacts) - set(current_paths))
        extra = sorted(set(current_paths) - set(recorded_artifacts))
        raise EvidenceError(
            f"run artifact set drifted; missing={missing or []}, extra={extra or []}"
        )
    for relative in current_paths:
        actual = member_identity(
            context.run_root,
            relative,
            label=f"run artifact {relative!r}",
        )
        _identity_or_raise(
            actual,
            recorded_artifacts[relative],
            label=f"run artifact {relative!r}",
        )

    return {
        "status": "verified",
        "run_directory": str(context.run_root),
        "run_id": context.plan["run_id"],
        "tasks": len(context.suite.tasks),
        "outputs": len(expected_outputs),
        "artifacts": len(current_paths),
    }

def verify_run(run_directory: Path | str) -> dict[str, Any]:
    """Verify while converting damaged evidence into a stable evidence error."""

    try:
        return _verify_run_checked(run_directory)
    except EvidenceError:
        raise
    except BenchHandoffError as exc:
        raise EvidenceError(f"run evidence is invalid: {exc}") from exc
    except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
        raise EvidenceError(
            f"run evidence could not be safely interpreted: {type(exc).__name__}: {exc}"
        ) from exc
