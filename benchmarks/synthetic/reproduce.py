"""Create or verify one bounded, commit-bound synthetic reproduction package."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Callable

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from benchmark_metadata import benchmark_provenance  # noqa: E402
from benchhandoff.errors import BenchHandoffError  # noqa: E402
from run_benchmark import run as run_focused_benchmark  # noqa: E402
from run_pipeline_comparison import run as run_pipeline_benchmark  # noqa: E402

SCHEMA_VERSION = 1
FOCUSED_FILE = "focused-recovery.json"
PIPELINE_FILE = "pipeline-comparison.json"
SUMMARY_FILE = "summary.json"
MANIFEST_FILE = "SHA256SUMS.txt"
COMPLETE_FILE = "PACKAGE_COMPLETE.json"
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_MANIFEST_ROW_PATTERN = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)")
_MANIFESTED_FILES = (FOCUSED_FILE, PIPELINE_FILE, SUMMARY_FILE)
_REQUIRED_FILES = (*_MANIFESTED_FILES, MANIFEST_FILE)
_PACKAGE_FILES = (*_REQUIRED_FILES, COMPLETE_FILE)
_MAX_FILE_BYTES = {
    FOCUSED_FILE: 1024 * 1024,
    PIPELINE_FILE: 1024 * 1024,
    SUMMARY_FILE: 1024 * 1024,
    MANIFEST_FILE: 8 * 1024,
    COMPLETE_FILE: 64 * 1024,
}
_VERIFIED_CLAIMS = {
    "focused_fail_quarantine_resume_verify": True,
    "focused_stale_resume_decision_rejected_without_mutation": True,
    "pipeline_child_calls_naive_vs_resume": [18, 13],
    "pipeline_duplicate_successes_naive_vs_resume": [5, 0],
    "pipeline_final_output_identity_equal": True,
}
_PACKAGE_SCOPE = (
    "synthetic commit-bound behavior; not elapsed-time, production, "
    "security, third-party, or adoption evidence"
)
_SUMMARY_KEYS = {
    "schema_version",
    "kind",
    "source_git_commit",
    "source_git_clean",
    "python_implementation",
    "python_version",
    "operating_system",
    "records",
    "verified_claims",
    "scope",
}
_COMPLETION_KEYS = {
    "schema_version",
    "kind",
    "manifest_file",
    "manifest_sha256",
    "manifest_size",
    "required_files",
}
_PROVENANCE_KEYS = {
    "generated_at_utc",
    "operating_system",
    "platform",
    "platform_details",
    "python_implementation",
    "python_version",
    "source_git_clean",
    "source_git_commit",
}
_FOCUSED_KEYS = _PROVENANCE_KEYS | {
    "schema_version",
    "benchmark",
    "attempts",
    "first_elapsed_ms",
    "first_exit_code",
    "first_status",
    "quarantined_outputs",
    "recovery_success",
    "resume_elapsed_ms",
    "resume_status",
    "verify_status",
    "resume_decision",
    "final_output",
}
_PIPELINE_KEYS = _PROVENANCE_KEYS | {
    "schema_version",
    "benchmark",
    "task_count",
    "first_failure_task",
    "exact_expectations",
    "naive_restart",
    "ledger_resume",
    "comparison",
    "timing_claim",
    "scope",
}
_NAIVE_KEYS = {
    "strategy",
    "subprocess_calls",
    "successful_task_executions",
    "duplicate_successful_executions",
    "final_tasks_present",
    "failure_codes",
    "final_output",
}
_RESUMED_KEYS = {
    "strategy",
    "subprocess_calls",
    "successful_task_executions",
    "duplicate_successful_executions",
    "final_tasks_completed",
    "first_failure_code",
    "quarantined_outputs",
    "verify_status",
    "final_output",
}
_COMPARISON_KEYS = {
    "avoided_subprocess_calls",
    "avoided_duplicate_successful_executions",
}
_FILE_IDENTITY_KEYS = {"sha256", "size"}
_RESUME_DECISION_KEYS = {
    "stale_decision_blocked",
    "decision_changed_after_partial_output_drift",
    "state_unchanged_after_rejection",
    "events_unchanged_after_rejection",
    "quarantine_unchanged_after_rejection",
    "partial_output_preserved_after_rejection",
    "attempts_before_rejection",
    "attempts_after_rejection",
    "refreshed_decision_resume_status",
}
_RESUME_DECISION_BOOLEAN_KEYS = _RESUME_DECISION_KEYS - {
    "attempts_before_rejection",
    "attempts_after_rejection",
    "refreshed_decision_resume_status",
}
_PIPELINE_ASSERTION_KEYS = {
    "naive_subprocess_calls",
    "ledger_subprocess_calls",
    "naive_duplicate_successes",
    "ledger_duplicate_successes",
    "naive_final_tasks",
    "ledger_final_tasks",
    "ledger_failure_code",
    "ledger_quarantined_outputs",
    "same_final_output",
    "ledger_verified",
}


class ReproductionPackageError(RuntimeError):
    """A bounded failure while producing or verifying a reproduction package."""


@dataclass(frozen=True)
class _OutputTarget:
    path: Path
    parent: Path
    parent_identity: tuple[int, int]


def _render_json(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_json_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> object:
    raise ValueError("non-finite JSON number")


def _parse_json_object(name: str, value: bytes) -> dict[str, object]:
    try:
        decoded = value.decode("utf-8")
        parsed = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReproductionPackageError(f"{name} is not strict UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise ReproductionPackageError(f"{name} must contain one JSON object")
    if value != _render_json(parsed):
        raise ReproductionPackageError(f"{name} is not in canonical JSON form")
    return parsed


def _write_new(path: Path, value: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _directory_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def _is_link_or_reparse(path: Path) -> bool:
    metadata = path.stat(follow_symlinks=False)
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _regular_file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _read_bounded_regular_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        before = path.stat(follow_symlinks=False)
        if _is_link_or_reparse(path) or not stat.S_ISREG(before.st_mode):
            raise ReproductionPackageError(
                f"{path.name} must be a regular non-linked file"
            )
        if before.st_size < 0 or before.st_size > max_bytes:
            raise ReproductionPackageError(f"{path.name} exceeds its size limit")
        with path.open("rb") as handle:
            value = handle.read(max_bytes + 1)
        after = path.stat(follow_symlinks=False)
        linked_after = _is_link_or_reparse(path)
    except ReproductionPackageError:
        raise
    except OSError as exc:
        raise ReproductionPackageError(
            f"{path.name} could not be read ({type(exc).__name__})"
        ) from exc
    if (
        len(value) != before.st_size
        or len(value) > max_bytes
        or _regular_file_identity(before) != _regular_file_identity(after)
        or linked_after
        or not stat.S_ISREG(after.st_mode)
    ):
        raise ReproductionPackageError(f"{path.name} changed while being read")
    return value


def _verification_directory(package_directory: Path) -> Path:
    try:
        raw = package_directory.absolute()
        resolved = raw.resolve(strict=True)
        if (
            _normalized_path(raw) != _normalized_path(resolved)
            or _is_link_or_reparse(resolved)
            or not resolved.is_dir()
        ):
            raise ReproductionPackageError(
                "package directory must be a regular non-linked directory"
            )
        entries = list(resolved.iterdir())
    except ReproductionPackageError:
        raise
    except OSError as exc:
        raise ReproductionPackageError(
            f"package directory is unavailable ({type(exc).__name__})"
        ) from exc
    if len(entries) != len(_PACKAGE_FILES) or {entry.name for entry in entries} != set(
        _PACKAGE_FILES
    ):
        raise ReproductionPackageError("package topology is invalid")
    return resolved


def _output_target(output_directory: Path, repository_root: Path) -> _OutputTarget:
    if os.path.lexists(output_directory):
        raise ReproductionPackageError("output directory must not already exist")
    try:
        raw_parent = output_directory.absolute().parent
        parent = raw_parent.resolve(strict=True)
        repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise ReproductionPackageError(
            f"output or repository parent is unavailable ({type(exc).__name__})"
        ) from exc
    if not parent.is_dir():
        raise ReproductionPackageError("output parent must be a directory")
    if (
        _normalized_path(raw_parent) != _normalized_path(parent)
        or _is_link_or_reparse(parent)
    ):
        raise ReproductionPackageError(
            "output parent must not traverse a symlink or reparse point"
        )
    target = parent / output_directory.name
    if target == repository or target.is_relative_to(repository):
        raise ReproductionPackageError(
            "output directory must be outside the source repository"
        )
    if os.path.lexists(target):
        raise ReproductionPackageError("output directory must not already exist")
    try:
        if any(parent.iterdir()):
            raise ReproductionPackageError(
                "output parent must be a new, empty, caller-private directory"
            )
        identity = _directory_identity(parent)
    except OSError as exc:
        raise ReproductionPackageError(
            f"output parent inspection failed ({type(exc).__name__})"
        ) from exc
    return _OutputTarget(target, parent, identity)


def _revalidate_output_parent(target: _OutputTarget) -> None:
    try:
        resolved = target.parent.resolve(strict=True)
        if (
            _normalized_path(resolved) != _normalized_path(target.parent)
            or _is_link_or_reparse(target.parent)
            or _directory_identity(target.parent) != target.parent_identity
            or any(target.parent.iterdir())
            or os.path.lexists(target.path)
        ):
            raise ReproductionPackageError(
                "output parent changed while benchmarks were running"
            )
    except OSError as exc:
        raise ReproductionPackageError(
            f"output parent revalidation failed ({type(exc).__name__})"
        ) from exc


def _source_identity(repository_root: Path) -> tuple[str, dict[str, object]]:
    provenance = benchmark_provenance(repository_root)
    commit = provenance.get("source_git_commit")
    if not isinstance(commit, str) or _COMMIT_PATTERN.fullmatch(commit) is None:
        raise ReproductionPackageError(
            "source repository must have one exact 40-character Git commit"
        )
    if provenance.get("source_git_clean") is not True:
        raise ReproductionPackageError(
            "source repository must be clean, including untracked files"
        )
    return commit, provenance


def _validate_provenance(record: dict[str, object], benchmark: str) -> None:
    for key in (
        "generated_at_utc",
        "operating_system",
        "platform",
        "platform_details",
        "python_implementation",
        "python_version",
    ):
        value = record.get(key)
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise ReproductionPackageError(f"{benchmark} provenance is invalid")


def _validate_file_identity(value: object, benchmark: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != _FILE_IDENTITY_KEYS
        or not isinstance(value.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
        or type(value.get("size")) is not int
        or value["size"] < 0
    ):
        raise ReproductionPackageError(f"{benchmark} file identity is invalid")


def _validate_record(
    record: dict[str, object],
    *,
    benchmark: str,
    source_commit: str,
) -> None:
    if record.get("schema_version") != 1:
        raise ReproductionPackageError(f"{benchmark} schema version is not 1")
    if record.get("benchmark") != benchmark:
        raise ReproductionPackageError(f"{benchmark} record identity is invalid")
    if record.get("source_git_clean") is not True:
        raise ReproductionPackageError(f"{benchmark} source is not clean")
    if record.get("source_git_commit") != source_commit:
        raise ReproductionPackageError(f"{benchmark} source commit drifted")
    _validate_provenance(record, benchmark)


def _validate_focused(record: dict[str, object], source_commit: str) -> None:
    benchmark = "synthetic-interruption-recovery"
    _validate_record(record, benchmark=benchmark, source_commit=source_commit)
    if set(record) != _FOCUSED_KEYS:
        raise ReproductionPackageError(f"{benchmark} field set is invalid")
    _validate_file_identity(record.get("final_output"), benchmark)
    decision = record.get("resume_decision")
    expected_decision = {
        "stale_decision_blocked": True,
        "decision_changed_after_partial_output_drift": True,
        "state_unchanged_after_rejection": True,
        "events_unchanged_after_rejection": True,
        "quarantine_unchanged_after_rejection": True,
        "partial_output_preserved_after_rejection": True,
        "attempts_before_rejection": 1,
        "attempts_after_rejection": 1,
        "refreshed_decision_resume_status": "completed",
    }
    if (
        not isinstance(decision, dict)
        or set(decision) != _RESUME_DECISION_KEYS
        or decision != expected_decision
        or any(
            decision.get(key) is not True
            for key in _RESUME_DECISION_BOOLEAN_KEYS
        )
        or type(decision.get("attempts_before_rejection")) is not int
        or type(decision.get("attempts_after_rejection")) is not int
    ):
        raise ReproductionPackageError(
            f"{benchmark} resume-decision assertions failed"
        )
    if any(
        not isinstance(record.get(key), (int, float))
        or isinstance(record.get(key), bool)
        or record[key] < 0
        for key in ("first_elapsed_ms", "resume_elapsed_ms")
    ):
        raise ReproductionPackageError(f"{benchmark} elapsed value is invalid")
    expected = {
        "attempts": 2,
        "first_exit_code": 75,
        "first_status": "failed",
        "quarantined_outputs": 1,
        "recovery_success": True,
        "resume_status": "completed",
        "verify_status": "verified",
    }
    if (
        any(record.get(key) != value for key, value in expected.items())
        or type(record.get("attempts")) is not int
        or type(record.get("first_exit_code")) is not int
        or type(record.get("quarantined_outputs")) is not int
    ):
        raise ReproductionPackageError(f"{benchmark} assertions failed")


def _validate_pipeline(record: dict[str, object], source_commit: str) -> None:
    benchmark = "synthetic-12-task-restart-vs-resume"
    _validate_record(record, benchmark=benchmark, source_commit=source_commit)
    if set(record) != _PIPELINE_KEYS:
        raise ReproductionPackageError(f"{benchmark} field set is invalid")
    exact = record.get("exact_expectations")
    naive = record.get("naive_restart")
    resumed = record.get("ledger_resume")
    comparison = record.get("comparison")
    if (
        not isinstance(exact, dict)
        or set(exact) != _PIPELINE_ASSERTION_KEYS
        or any(value is not True for value in exact.values())
        or not isinstance(naive, dict)
        or set(naive) != _NAIVE_KEYS
        or not isinstance(resumed, dict)
        or set(resumed) != _RESUMED_KEYS
        or not isinstance(comparison, dict)
        or set(comparison) != _COMPARISON_KEYS
        or record.get("task_count") != 12
        or record.get("first_failure_task") != 6
        or naive.get("strategy") != "naive-full-restart"
        or resumed.get("strategy") != "benchhandoff-resume"
        or naive.get("subprocess_calls") != 18
        or naive.get("successful_task_executions") != 17
        or resumed.get("subprocess_calls") != 13
        or resumed.get("successful_task_executions") != 12
        or naive.get("duplicate_successful_executions") != 5
        or resumed.get("duplicate_successful_executions") != 0
        or naive.get("final_tasks_present") != 12
        or resumed.get("final_tasks_completed") != 12
        or naive.get("failure_codes") != [75]
        or resumed.get("first_failure_code") != 75
        or resumed.get("quarantined_outputs") != 1
        or resumed.get("verify_status") != "verified"
        or comparison.get("avoided_subprocess_calls") != 5
        or comparison.get("avoided_duplicate_successful_executions") != 5
        or naive.get("final_output") != resumed.get("final_output")
        or record.get("timing_claim")
        != "none; this benchmark reports deterministic work counts only"
        or record.get("scope")
        != "local synthetic behavior, not production or third-party evidence"
        or any(
            type(value) is not int
            for value in (
                record.get("task_count"),
                record.get("first_failure_task"),
                naive.get("subprocess_calls"),
                naive.get("successful_task_executions"),
                naive.get("duplicate_successful_executions"),
                naive.get("final_tasks_present"),
                resumed.get("subprocess_calls"),
                resumed.get("successful_task_executions"),
                resumed.get("duplicate_successful_executions"),
                resumed.get("final_tasks_completed"),
                resumed.get("first_failure_code"),
                resumed.get("quarantined_outputs"),
                comparison.get("avoided_subprocess_calls"),
                comparison.get("avoided_duplicate_successful_executions"),
            )
        )
    ):
        raise ReproductionPackageError(f"{benchmark} assertions failed")
    _validate_file_identity(naive.get("final_output"), benchmark)
    _validate_file_identity(resumed.get("final_output"), benchmark)


def _validate_manifest(
    manifest_bytes: bytes,
    file_bytes: dict[str, bytes],
) -> None:
    try:
        manifest_text = manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReproductionPackageError("SHA256SUMS.txt is not UTF-8") from exc
    if not manifest_text.endswith("\n") or "\r" in manifest_text:
        raise ReproductionPackageError("SHA256SUMS.txt has invalid line endings")
    rows = manifest_text[:-1].split("\n")
    if len(rows) != len(_MANIFESTED_FILES):
        raise ReproductionPackageError("SHA256SUMS.txt row count is invalid")
    for row, expected_name in zip(rows, _MANIFESTED_FILES, strict=True):
        match = _MANIFEST_ROW_PATTERN.fullmatch(row)
        if match is None or match.group(2) != expected_name:
            raise ReproductionPackageError("SHA256SUMS.txt row is invalid")
        if match.group(1) != _sha256_bytes(file_bytes[expected_name]):
            raise ReproductionPackageError(
                f"SHA256SUMS.txt hash failed for {expected_name}"
            )


def verify_reproduction_package(
    package_directory: Path | str,
    *,
    expected_commit: str | None = None,
) -> dict[str, object]:
    """Verify package topology, bounds, records, manifest, and source binding."""

    if (
        expected_commit is not None
        and _COMMIT_PATTERN.fullmatch(expected_commit) is None
    ):
        raise ReproductionPackageError(
            "expected commit must be 40 lowercase hexadecimal characters"
        )
    package = _verification_directory(Path(package_directory))
    try:
        package_identity = _directory_identity(package)
    except OSError as exc:
        raise ReproductionPackageError(
            f"package directory identity is unavailable ({type(exc).__name__})"
        ) from exc
    file_bytes = {
        name: _read_bounded_regular_file(
            package / name,
            max_bytes=_MAX_FILE_BYTES[name],
        )
        for name in _PACKAGE_FILES
    }

    completion = _parse_json_object(COMPLETE_FILE, file_bytes[COMPLETE_FILE])
    if (
        set(completion) != _COMPLETION_KEYS
        or completion.get("schema_version") != SCHEMA_VERSION
        or completion.get("kind")
        != "benchhandoff-reproduction-package-completion"
        or completion.get("manifest_file") != MANIFEST_FILE
        or completion.get("manifest_sha256")
        != _sha256_bytes(file_bytes[MANIFEST_FILE])
        or completion.get("manifest_size") != len(file_bytes[MANIFEST_FILE])
        or completion.get("required_files") != list(_REQUIRED_FILES)
    ):
        raise ReproductionPackageError("completion record is invalid")

    _validate_manifest(file_bytes[MANIFEST_FILE], file_bytes)
    focused = _parse_json_object(FOCUSED_FILE, file_bytes[FOCUSED_FILE])
    pipeline = _parse_json_object(PIPELINE_FILE, file_bytes[PIPELINE_FILE])
    summary = _parse_json_object(SUMMARY_FILE, file_bytes[SUMMARY_FILE])
    source_commit = summary.get("source_git_commit")
    if (
        set(summary) != _SUMMARY_KEYS
        or summary.get("schema_version") != SCHEMA_VERSION
        or summary.get("kind") != "benchhandoff-synthetic-reproduction-package"
        or not isinstance(source_commit, str)
        or _COMMIT_PATTERN.fullmatch(source_commit) is None
        or summary.get("source_git_clean") is not True
        or any(
            not isinstance(summary.get(field), str) or not summary.get(field)
            for field in (
                "python_implementation",
                "python_version",
                "operating_system",
            )
        )
        or summary.get("verified_claims") != _VERIFIED_CLAIMS
        or summary.get("scope") != _PACKAGE_SCOPE
    ):
        raise ReproductionPackageError("summary record is invalid")
    if expected_commit is not None and source_commit != expected_commit:
        raise ReproductionPackageError("package source commit does not match expected")

    _validate_focused(focused, source_commit)
    _validate_pipeline(pipeline, source_commit)
    expected_records = [
        {
            "benchmark": focused["benchmark"],
            "file": FOCUSED_FILE,
            "sha256": _sha256_bytes(file_bytes[FOCUSED_FILE]),
            "size": len(file_bytes[FOCUSED_FILE]),
        },
        {
            "benchmark": pipeline["benchmark"],
            "file": PIPELINE_FILE,
            "sha256": _sha256_bytes(file_bytes[PIPELINE_FILE]),
            "size": len(file_bytes[PIPELINE_FILE]),
        },
    ]
    if summary.get("records") != expected_records:
        raise ReproductionPackageError("summary record bindings are invalid")
    revalidated_package = _verification_directory(Path(package_directory))
    try:
        if (
            revalidated_package != package
            or _directory_identity(revalidated_package) != package_identity
        ):
            raise ReproductionPackageError("package directory identity changed")
    except OSError as exc:
        raise ReproductionPackageError(
            f"package directory revalidation failed ({type(exc).__name__})"
        ) from exc
    return summary


def build_reproduction_package(
    output_directory: Path | str,
    *,
    repository_root: Path = REPOSITORY_ROOT,
    focused_runner: Callable[[], dict[str, object]] = run_focused_benchmark,
    pipeline_runner: Callable[[], dict[str, object]] = run_pipeline_benchmark,
) -> dict[str, object]:
    """Build a non-overwriting package and return its bounded summary."""

    target = _output_target(Path(output_directory), repository_root)
    source_commit, source_provenance = _source_identity(repository_root)
    try:
        focused = focused_runner()
        pipeline = pipeline_runner()
    except (BenchHandoffError, OSError, RuntimeError, ValueError) as exc:
        raise ReproductionPackageError(
            f"synthetic benchmark generation failed ({type(exc).__name__})"
        ) from exc

    _validate_focused(focused, source_commit)
    _validate_pipeline(pipeline, source_commit)
    raw_bytes = {
        FOCUSED_FILE: _render_json(focused),
        PIPELINE_FILE: _render_json(pipeline),
    }
    records = [
        {
            "benchmark": focused["benchmark"],
            "file": FOCUSED_FILE,
            "sha256": _sha256_bytes(raw_bytes[FOCUSED_FILE]),
            "size": len(raw_bytes[FOCUSED_FILE]),
        },
        {
            "benchmark": pipeline["benchmark"],
            "file": PIPELINE_FILE,
            "sha256": _sha256_bytes(raw_bytes[PIPELINE_FILE]),
            "size": len(raw_bytes[PIPELINE_FILE]),
        },
    ]
    summary: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "benchhandoff-synthetic-reproduction-package",
        "source_git_commit": source_commit,
        "source_git_clean": True,
        "python_implementation": source_provenance["python_implementation"],
        "python_version": source_provenance["python_version"],
        "operating_system": source_provenance["operating_system"],
        "records": records,
        "verified_claims": {**_VERIFIED_CLAIMS},
        "scope": _PACKAGE_SCOPE,
    }
    raw_bytes[SUMMARY_FILE] = _render_json(summary)
    manifest_rows = [
        f"{_sha256_bytes(raw_bytes[name])}  {name}"
        for name in _MANIFESTED_FILES
    ]
    manifest_bytes = ("\n".join(manifest_rows) + "\n").encode("utf-8")
    completion: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "benchhandoff-reproduction-package-completion",
        "manifest_file": MANIFEST_FILE,
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "manifest_size": len(manifest_bytes),
        "required_files": [
            FOCUSED_FILE,
            PIPELINE_FILE,
            SUMMARY_FILE,
            MANIFEST_FILE,
        ],
    }

    try:
        _revalidate_output_parent(target)
        target.path.mkdir()
        for name in _MANIFESTED_FILES:
            _write_new(target.path / name, raw_bytes[name])
        _write_new(target.path / MANIFEST_FILE, manifest_bytes)
        for name in _MANIFESTED_FILES:
            expected = _sha256_bytes(raw_bytes[name])
            if _sha256_file(target.path / name) != expected:
                raise ReproductionPackageError(
                    f"post-write hash verification failed for {name}"
                )
        if _sha256_file(target.path / MANIFEST_FILE) != completion["manifest_sha256"]:
            raise ReproductionPackageError("post-write manifest verification failed")
        expected_entries = set(_REQUIRED_FILES)
        if {entry.name for entry in target.path.iterdir()} != expected_entries:
            raise ReproductionPackageError("unexpected package entry appeared")
        _write_new(target.path / COMPLETE_FILE, _render_json(completion))
    except OSError as exc:
        raise ReproductionPackageError(
            f"package write failed closed ({type(exc).__name__})"
        ) from exc

    if {entry.name for entry in target.path.iterdir()} != set(_PACKAGE_FILES):
        raise ReproductionPackageError("final package topology is invalid")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create or verify one bounded BenchHandoff synthetic reproduction package."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "new directory inside a new, empty, caller-private parent outside "
            "the source repository"
        ),
    )
    mode.add_argument(
        "--verify-dir",
        type=Path,
        help="existing complete reproduction package to verify without mutation",
    )
    parser.add_argument(
        "--expected-commit",
        help="optional full lowercase source commit required in verification mode",
    )
    arguments = parser.parse_args(argv)
    if arguments.output_dir is not None and arguments.expected_commit is not None:
        parser.error("--expected-commit is only valid with --verify-dir")
    try:
        if arguments.output_dir is not None:
            summary = build_reproduction_package(arguments.output_dir)
            status = "created"
        else:
            summary = verify_reproduction_package(
                arguments.verify_dir,
                expected_commit=arguments.expected_commit,
            )
            status = "verified"
    except ReproductionPackageError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 30
    print(
        json.dumps(
            {
                "status": status,
                "kind": summary["kind"],
                "source_git_commit": summary["source_git_commit"],
                "files": list(_PACKAGE_FILES),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
