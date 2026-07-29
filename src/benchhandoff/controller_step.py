"""Narrow controller step with a bounded, path-free termination protocol."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from benchhandoff.engine import (
    BUNDLE_FILE,
    inspect_resume,
    resume_run,
    start_run,
    verify_run,
)
from benchhandoff.errors import (
    BenchHandoffError,
    BoundaryError,
    ConfigurationError,
    EvidenceError,
    TaskExecutionError,
)
from benchhandoff.model import MAX_SUITE_BYTES, load_suite
from benchhandoff.storage import (
    checked_directory,
    normalize_relative_file,
    read_regular_bytes,
    resolve_member,
)

PROTOCOL = "benchhandoff-controller-step/v1"
DEFAULT_DATA_ROOT = "/benchhandoff-data"
DEFAULT_TERMINATION_LOG = "/dev/termination-log"
MAX_TERMINATION_BYTES = 1024
MAX_OVERRIDE_PATH_BYTES = 4096
MAX_BUNDLE_BYTES = 16 * 1024 * 1024

_ACTIONS = frozenset({"start", "resume", "verify"})
_OUTCOMES = frozenset({"awaiting_approval", "completed", "verified", "blocked"})
_ERROR_CODES = frozenset(
    {"invalid_request", "execution_failed", "evidence_invalid", "internal_error"}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_OPTIONS = (
    "--action",
    "--agent-run-uid",
    "--execution-spec-sha256",
    "--suite-sha256",
    "--suite-path",
    "--resume-decision-sha256",
    "--data-root",
    "--termination-log",
)


class _InvalidRequest(Exception):
    """An invalid controller request whose original text must not be emitted."""


class _SilentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _InvalidRequest

    def exit(self, status: int = 0, message: str | None = None) -> None:
        del status, message
        raise _InvalidRequest


def _parser() -> argparse.ArgumentParser:
    parser = _SilentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--action", required=True, choices=sorted(_ACTIONS))
    parser.add_argument("--agent-run-uid", required=True)
    parser.add_argument("--execution-spec-sha256", required=True)
    parser.add_argument("--suite-sha256", required=True)
    parser.add_argument("--suite-path", required=True)
    parser.add_argument("--resume-decision-sha256")
    parser.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    parser.add_argument("--termination-log", default=DEFAULT_TERMINATION_LOG)
    return parser


def _reject_duplicate_options(arguments: Sequence[str]) -> None:
    for option in _OPTIONS:
        count = sum(
            value == option or value.startswith(option + "=")
            for value in arguments
        )
        if count > 1:
            raise _InvalidRequest


def _parse(arguments: Sequence[str]) -> argparse.Namespace:
    _reject_duplicate_options(arguments)
    try:
        return _parser().parse_args(list(arguments))
    except (argparse.ArgumentError, TypeError, ValueError) as exc:
        raise _InvalidRequest from exc


def _valid_absolute_path(value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _InvalidRequest
    try:
        if len(value.encode("utf-8")) > MAX_OVERRIDE_PATH_BYTES:
            raise _InvalidRequest
    except UnicodeEncodeError as exc:
        raise _InvalidRequest from exc
    path = Path(value)
    if not path.is_absolute():
        raise _InvalidRequest
    return path


def _termination_candidate(arguments: Sequence[str]) -> Path:
    values: list[str] = []
    for index, value in enumerate(arguments):
        if value == "--termination-log" and index + 1 < len(arguments):
            values.append(arguments[index + 1])
        elif value.startswith("--termination-log="):
            values.append(value.split("=", 1)[1])
    if len(values) == 1:
        try:
            return _valid_absolute_path(values[0])
        except _InvalidRequest:
            pass
    return Path(DEFAULT_TERMINATION_LOG)


def _valid_uid(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 36 or value.lower() != value:
        raise _InvalidRequest
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise _InvalidRequest from exc
    if str(parsed) != value:
        raise _InvalidRequest
    return value


def _valid_sha256(value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _InvalidRequest
    return value


def _valid_run_id(value: Any) -> str:
    if not isinstance(value, str) or _RUN_ID.fullmatch(value) is None:
        raise EvidenceError("controller result contains an invalid run identity")
    return value


def _paths(arguments: argparse.Namespace) -> tuple[Path, Path, Path]:
    data_root = checked_directory(
        _valid_absolute_path(arguments.data_root),
        label="controller data root",
    )
    suites_root = checked_directory(
        data_root / "suites",
        label="controller suites root",
    )
    runs_root = checked_directory(
        data_root / "runs",
        label="controller runs root",
    )
    suite_relative = normalize_relative_file(
        arguments.suite_path,
        label="controller suite path",
    )
    suite_path = resolve_member(
        suites_root,
        suite_relative,
        label="controller suite",
    )
    run_path = runs_root / arguments.agent_run_uid
    termination_log = _valid_absolute_path(arguments.termination_log)
    return suite_path, run_path, termination_log


def _validate_suite_source(suite_path: Path, expected_sha256: str) -> None:
    raw = read_regular_bytes(
        suite_path,
        label="controller suite",
        max_bytes=MAX_SUITE_BYTES,
    )
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise EvidenceError("controller suite does not match the expected SHA-256")
    suite = load_suite(suite_path)
    if suite.identity["sha256"] != expected_sha256:
        raise EvidenceError("controller suite does not match the expected SHA-256")
    if suite.version != 3:
        raise ConfigurationError("AgentRun requires a version 3 suite")


def _bundle_bytes(run_path: Path) -> bytes:
    return read_regular_bytes(
        run_path / BUNDLE_FILE,
        label="controller bundle",
        max_bytes=MAX_BUNDLE_BYTES,
    )


def _verify_stable_bundle(run_path: Path) -> tuple[str, str]:
    before = _bundle_bytes(run_path)
    verification = verify_run(run_path)
    after = _bundle_bytes(run_path)
    if before != after:
        raise EvidenceError("bundle changed during controller verification")
    return (
        _valid_run_id(verification.get("run_id")),
        hashlib.sha256(after).hexdigest(),
    )


def _termination(
    *,
    action: str,
    outcome: str,
    agent_run_uid: str,
    execution_spec_sha256: str,
    run_id: str = "",
    resume_decision_sha256: str = "",
    bundle_sha256: str = "",
    error_code: str = "",
) -> dict[str, str]:
    if action not in _ACTIONS and action != "":
        raise RuntimeError("invalid bounded action")
    if outcome not in _OUTCOMES:
        raise RuntimeError("invalid bounded outcome")
    if error_code not in _ERROR_CODES and error_code != "":
        raise RuntimeError("invalid bounded error code")
    return {
        "protocol": PROTOCOL,
        "action": action,
        "outcome": outcome,
        "agent_run_uid": agent_run_uid,
        "execution_spec_sha256": execution_spec_sha256,
        "run_id": run_id,
        "resume_decision_sha256": resume_decision_sha256,
        "bundle_sha256": bundle_sha256,
        "error_code": error_code,
    }


def _encoded_termination(value: dict[str, str]) -> bytes:
    payload = (
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_TERMINATION_BYTES:
        raise RuntimeError("bounded termination protocol exceeded its size cap")
    return payload


def _write_termination(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as stream:
        stream.write(payload)
        stream.flush()


def _blocked(
    *,
    action: str,
    agent_run_uid: str,
    execution_spec_sha256: str,
    error_code: str,
) -> dict[str, str]:
    return _termination(
        action=action,
        outcome="blocked",
        agent_run_uid=agent_run_uid,
        execution_spec_sha256=execution_spec_sha256,
        error_code=error_code,
    )


def _classify_error(exc: BaseException) -> tuple[str, int]:
    if isinstance(exc, (_InvalidRequest, BoundaryError, ConfigurationError)):
        return "invalid_request", 10
    if isinstance(exc, TaskExecutionError):
        return "execution_failed", 20
    if isinstance(exc, EvidenceError):
        return "evidence_invalid", 30
    if isinstance(exc, BenchHandoffError):
        return "execution_failed", 20
    return "internal_error", 70


def _awaiting_approval(run_id: str, run_path: Path) -> dict[str, str]:
    decision = inspect_resume(run_path)
    decision_run_id = _valid_run_id(decision.get("run_id"))
    if decision_run_id != run_id:
        raise EvidenceError("resume decision run identity does not match the result")
    decision_sha256 = decision.get("decision_sha256")
    if not isinstance(decision_sha256, str) or _SHA256.fullmatch(decision_sha256) is None:
        raise EvidenceError("resume decision contains an invalid digest")
    return {
        "run_id": run_id,
        "resume_decision_sha256": decision_sha256,
    }


def _execute(arguments: argparse.Namespace) -> dict[str, str]:
    suite_path, run_path, _ = _paths(arguments)
    _validate_suite_source(suite_path, arguments.suite_sha256)

    if arguments.action == "start":
        if arguments.resume_decision_sha256 is not None:
            raise _InvalidRequest
        result = start_run(
            suite_path,
            run_path,
            expected_suite_sha256=arguments.suite_sha256,
        )
        run_id = _valid_run_id(result.run_id)
        if result.status == "failed":
            fields = _awaiting_approval(run_id, run_path)
            return _termination(
                action="start",
                outcome="awaiting_approval",
                agent_run_uid=arguments.agent_run_uid,
                execution_spec_sha256=arguments.execution_spec_sha256,
                **fields,
            )
        if result.status != "completed":
            raise EvidenceError("start returned an unsupported status")
        verified_run_id, bundle_sha256 = _verify_stable_bundle(run_path)
        if verified_run_id != run_id:
            raise EvidenceError("verified run identity does not match start result")
        return _termination(
            action="start",
            outcome="completed",
            agent_run_uid=arguments.agent_run_uid,
            execution_spec_sha256=arguments.execution_spec_sha256,
            run_id=run_id,
            bundle_sha256=bundle_sha256,
        )

    if arguments.action == "resume":
        decision_sha256 = _valid_sha256(arguments.resume_decision_sha256)
        result = resume_run(
            run_path,
            expected_decision_sha256=decision_sha256,
        )
        run_id = _valid_run_id(result.run_id)
        if result.status == "failed":
            raise TaskExecutionError("approved resume did not complete")
        if result.status != "completed":
            raise EvidenceError("resume returned an unsupported status")
        verified_run_id, bundle_sha256 = _verify_stable_bundle(run_path)
        if verified_run_id != run_id:
            raise EvidenceError("verified run identity does not match resume result")
        return _termination(
            action="resume",
            outcome="completed",
            agent_run_uid=arguments.agent_run_uid,
            execution_spec_sha256=arguments.execution_spec_sha256,
            run_id=run_id,
            resume_decision_sha256=decision_sha256,
            bundle_sha256=bundle_sha256,
        )

    if arguments.resume_decision_sha256 is not None:
        raise _InvalidRequest
    run_id, bundle_sha256 = _verify_stable_bundle(run_path)
    return _termination(
        action="verify",
        outcome="verified",
        agent_run_uid=arguments.agent_run_uid,
        execution_spec_sha256=arguments.execution_spec_sha256,
        run_id=run_id,
        bundle_sha256=bundle_sha256,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one controller action and emit only the bounded protocol."""

    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    termination_log = _termination_candidate(raw_arguments)
    action = ""
    agent_run_uid = ""
    execution_spec_sha256 = ""
    exit_code = 0
    try:
        arguments = _parse(raw_arguments)
        action = arguments.action
        agent_run_uid = _valid_uid(arguments.agent_run_uid)
        execution_spec_sha256 = _valid_sha256(arguments.execution_spec_sha256)
        arguments.suite_sha256 = _valid_sha256(arguments.suite_sha256)
        arguments.agent_run_uid = agent_run_uid
        arguments.execution_spec_sha256 = execution_spec_sha256
        termination_log = _valid_absolute_path(arguments.termination_log)
        result = _execute(arguments)
    except Exception as exc:
        error_code, exit_code = _classify_error(exc)
        result = _blocked(
            action=action,
            agent_run_uid=agent_run_uid,
            execution_spec_sha256=execution_spec_sha256,
            error_code=error_code,
        )

    payload = _encoded_termination(result)
    try:
        _write_termination(termination_log, payload)
    except OSError:
        exit_code = 70
        result = _blocked(
            action=action,
            agent_run_uid=agent_run_uid,
            execution_spec_sha256=execution_spec_sha256,
            error_code="internal_error",
        )
        payload = _encoded_termination(result)
    sys.stdout.write(payload.decode("utf-8"))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
