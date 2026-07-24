"""Create one bounded, commit-bound synthetic reproduction package."""

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
    """A bounded failure while producing a reproduction package."""


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


def _validate_focused(record: dict[str, object], source_commit: str) -> None:
    benchmark = "synthetic-interruption-recovery"
    _validate_record(record, benchmark=benchmark, source_commit=source_commit)
    expected = {
        "attempts": 2,
        "first_exit_code": 75,
        "first_status": "failed",
        "quarantined_outputs": 1,
        "recovery_success": True,
        "resume_status": "completed",
        "verify_status": "verified",
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise ReproductionPackageError(f"{benchmark} assertions failed")


def _validate_pipeline(record: dict[str, object], source_commit: str) -> None:
    benchmark = "synthetic-12-task-restart-vs-resume"
    _validate_record(record, benchmark=benchmark, source_commit=source_commit)
    exact = record.get("exact_expectations")
    naive = record.get("naive_restart")
    resumed = record.get("ledger_resume")
    comparison = record.get("comparison")
    if (
        not isinstance(exact, dict)
        or set(exact) != _PIPELINE_ASSERTION_KEYS
        or any(value is not True for value in exact.values())
        or not isinstance(naive, dict)
        or not isinstance(resumed, dict)
        or not isinstance(comparison, dict)
        or record.get("task_count") != 12
        or record.get("first_failure_task") != 6
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
    ):
        raise ReproductionPackageError(f"{benchmark} assertions failed")


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
        "verified_claims": {
            "focused_fail_quarantine_resume_verify": True,
            "pipeline_child_calls_naive_vs_resume": [18, 13],
            "pipeline_duplicate_successes_naive_vs_resume": [5, 0],
            "pipeline_final_output_identity_equal": True,
        },
        "scope": (
            "synthetic commit-bound behavior; not elapsed-time, production, "
            "security, third-party, or adoption evidence"
        ),
    }
    raw_bytes[SUMMARY_FILE] = _render_json(summary)
    manifest_rows = [
        f"{_sha256_bytes(raw_bytes[name])}  {name}"
        for name in (FOCUSED_FILE, PIPELINE_FILE, SUMMARY_FILE)
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
        for name in (FOCUSED_FILE, PIPELINE_FILE, SUMMARY_FILE):
            _write_new(target.path / name, raw_bytes[name])
        _write_new(target.path / MANIFEST_FILE, manifest_bytes)
        for name in (FOCUSED_FILE, PIPELINE_FILE, SUMMARY_FILE):
            expected = _sha256_bytes(raw_bytes[name])
            if _sha256_file(target.path / name) != expected:
                raise ReproductionPackageError(
                    f"post-write hash verification failed for {name}"
                )
        if _sha256_file(target.path / MANIFEST_FILE) != completion["manifest_sha256"]:
            raise ReproductionPackageError("post-write manifest verification failed")
        expected_entries = {
            FOCUSED_FILE,
            PIPELINE_FILE,
            SUMMARY_FILE,
            MANIFEST_FILE,
        }
        if {entry.name for entry in target.path.iterdir()} != expected_entries:
            raise ReproductionPackageError("unexpected package entry appeared")
        _write_new(target.path / COMPLETE_FILE, _render_json(completion))
    except OSError as exc:
        raise ReproductionPackageError(
            f"package write failed closed ({type(exc).__name__})"
        ) from exc

    if {entry.name for entry in target.path.iterdir()} != {
        FOCUSED_FILE,
        PIPELINE_FILE,
        SUMMARY_FILE,
        MANIFEST_FILE,
        COMPLETE_FILE,
    }:
        raise ReproductionPackageError("final package topology is invalid")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create one bounded BenchHandoff synthetic reproduction package."
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help=(
            "new directory inside a new, empty, caller-private parent outside "
            "the source repository"
        ),
    )
    arguments = parser.parse_args(argv)
    try:
        summary = build_reproduction_package(arguments.output_dir)
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
                "status": "created",
                "kind": summary["kind"],
                "source_git_commit": summary["source_git_commit"],
                "files": [
                    FOCUSED_FILE,
                    PIPELINE_FILE,
                    SUMMARY_FILE,
                    MANIFEST_FILE,
                    COMPLETE_FILE,
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
