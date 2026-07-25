from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Mapping, Sequence

from verify_license_state import (
    DEFAULT_LICENSE_SPECS,
    FINAL_BUILD_REQUIREMENT,
    LICENSE_FILE,
    MAX_LICENSE_BYTES,
    MAX_METADATA_BYTES,
    PENDING_NOTICE,
    LicenseSpec,
    LicenseStateError,
    inspect_license_state,
    path_exists_no_follow,
    read_bounded_regular_file,
)

SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
PENDING_BUILD_LINE = 'requires = ["setuptools>=69"]'
FINAL_BUILD_LINE = f'requires = ["{FINAL_BUILD_REQUIREMENT}"]'
README_LINE = 'readme = "README.md"'


class LicenseFinalizationError(ValueError):
    """A bounded, fail-closed license-finalization error."""


def _git(repository: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
        )
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        raise LicenseFinalizationError(f"git check failed: {exc}") from None
    return completed.stdout.strip()


def _check_git(
    repository: Path,
    expected_source_commit: str,
) -> None:
    if not SOURCE_COMMIT.fullmatch(expected_source_commit):
        raise LicenseFinalizationError(
            "expected source commit must be 40 lowercase hexadecimal characters"
        )
    if _git(repository, "rev-parse", "HEAD") != expected_source_commit:
        raise LicenseFinalizationError("repository HEAD differs from expected commit")
    if _git(repository, "status", "--porcelain=v1", "--untracked-files=all"):
        raise LicenseFinalizationError("repository must be clean before finalization")


def _transform_pyproject(raw: bytes, choice: str) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise LicenseFinalizationError("pyproject.toml must be strict UTF-8") from None
    if "\r" in text or not text.endswith("\n"):
        raise LicenseFinalizationError("pyproject.toml must use canonical LF")
    if text.count(PENDING_BUILD_LINE) != 1:
        raise LicenseFinalizationError("pending setuptools requirement is not exact")
    if text.count(README_LINE) != 1:
        raise LicenseFinalizationError("project readme anchor is not exact")
    if re.search(r"(?m)^license(?:-files)?\s*=", text):
        raise LicenseFinalizationError("license metadata already exists")
    updated = text.replace(PENDING_BUILD_LINE, FINAL_BUILD_LINE)
    metadata = (
        f'{README_LINE}\nlicense = "{choice}"\n'
        f'license-files = ["{LICENSE_FILE}"]'
    )
    updated = updated.replace(README_LINE, metadata)
    return updated.encode("utf-8")


def _license_input(
    path: Path,
    choice: str,
    *,
    license_specs: Mapping[str, LicenseSpec],
) -> tuple[bytes, LicenseSpec]:
    if choice not in license_specs:
        raise LicenseFinalizationError("license must be Apache-2.0 or MIT")
    try:
        raw = read_bounded_regular_file(path, maximum=MAX_LICENSE_BYTES)
    except LicenseStateError as exc:
        raise LicenseFinalizationError(str(exc)) from None
    digest = hashlib.sha256(raw).hexdigest()
    spec = license_specs[choice]
    if len(raw) != spec.size or digest != spec.sha256:
        raise LicenseFinalizationError(
            f"license input does not match canonical {choice} bytes"
        )
    return raw, spec


def _exclusive_write(path: Path, raw: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(path, flags, 0o644)
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError(f"short write while creating {path.name}")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_replace(path: Path, raw: bytes) -> None:
    temporary = path.with_name(f".{path.name}.license-{uuid.uuid4().hex}.tmp")
    try:
        _exclusive_write(temporary, raw)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _remove_exact(path: Path, expected: bytes) -> None:
    try:
        actual = read_bounded_regular_file(path, maximum=MAX_METADATA_BYTES)
    except LicenseStateError as exc:
        raise LicenseFinalizationError(str(exc)) from None
    if actual != expected:
        raise LicenseFinalizationError(f"{path.name} changed before removal")
    path.unlink()


def _read_repository_file(path: Path, *, maximum: int) -> bytes:
    try:
        return read_bounded_regular_file(path, maximum=maximum)
    except LicenseStateError as exc:
        raise LicenseFinalizationError(str(exc)) from None


def _rollback(
    repository: Path,
    *,
    original_pyproject: bytes,
    original_pending: bytes,
    license_bytes: bytes,
    license_creation_attempted: bool,
    license_created: bool,
    pyproject_replaced: bool,
    pending_removed: bool,
) -> list[str]:
    failures: list[str] = []
    pending = repository / PENDING_NOTICE
    pyproject = repository / "pyproject.toml"
    license_path = repository / LICENSE_FILE
    if pending_removed:
        try:
            if path_exists_no_follow(pending):
                actual = read_bounded_regular_file(
                    pending,
                    maximum=MAX_METADATA_BYTES,
                )
                if actual != original_pending:
                    failures.append("pending notice changed; refusing overwrite")
            else:
                _exclusive_write(pending, original_pending)
        except (OSError, LicenseStateError) as exc:
            failures.append(f"restore pending notice: {exc}")
    if pyproject_replaced:
        try:
            _atomic_replace(pyproject, original_pyproject)
        except OSError as exc:
            failures.append(f"restore pyproject.toml: {exc}")
    if license_created:
        try:
            actual = read_bounded_regular_file(
                license_path,
                maximum=MAX_LICENSE_BYTES,
            )
            if actual != license_bytes:
                failures.append("LICENSE changed; refusing rollback unlink")
            else:
                license_path.unlink()
        except (OSError, LicenseStateError) as exc:
            failures.append(f"remove created LICENSE: {exc}")
    elif license_creation_attempted:
        try:
            if path_exists_no_follow(license_path):
                failures.append(
                    "LICENSE appeared during a failed create; "
                    "manual inspection required"
                )
        except LicenseStateError as exc:
            failures.append(f"inspect failed LICENSE create: {exc}")
    return failures


def finalize_repository(
    repository: Path,
    *,
    choice: str,
    license_file: Path,
    expected_source_commit: str | None,
    apply: bool,
    require_clean_git: bool = True,
    license_specs: Mapping[str, LicenseSpec] = DEFAULT_LICENSE_SPECS,
) -> dict[str, object]:
    repository = repository.resolve()
    if not repository.is_dir():
        raise LicenseFinalizationError("repository must be a directory")
    if require_clean_git:
        if expected_source_commit is None:
            raise LicenseFinalizationError("expected source commit is required")
        _check_git(repository, expected_source_commit)
    try:
        state = inspect_license_state(repository, license_specs=license_specs)
    except LicenseStateError as exc:
        raise LicenseFinalizationError(str(exc)) from None
    if state["mode"] != "pending":
        raise LicenseFinalizationError("repository is not in pending license state")

    license_bytes, spec = _license_input(
        license_file.resolve(),
        choice,
        license_specs=license_specs,
    )
    original_pyproject = _read_repository_file(
        repository / "pyproject.toml",
        maximum=MAX_METADATA_BYTES,
    )
    original_pending = _read_repository_file(
        repository / PENDING_NOTICE,
        maximum=MAX_METADATA_BYTES,
    )
    final_pyproject = _transform_pyproject(original_pyproject, choice)
    plan: dict[str, object] = {
        "applied": False,
        "created": [LICENSE_FILE],
        "deleted": [PENDING_NOTICE],
        "license": choice,
        "license_bytes": len(license_bytes),
        "license_sha256": spec.sha256,
        "license_source_commit": spec.source_commit,
        "license_source_derivation": spec.derivation,
        "license_source_url": spec.source_url,
        "modified": ["pyproject.toml"],
        "source_commit": expected_source_commit,
    }
    if not apply:
        return plan

    if require_clean_git:
        assert expected_source_commit is not None
        _check_git(repository, expected_source_commit)

    license_path = repository / LICENSE_FILE
    license_creation_attempted = False
    license_created = False
    pyproject_replaced = False
    pending_removed = False
    try:
        license_creation_attempted = True
        _exclusive_write(license_path, license_bytes)
        license_created = True
        _atomic_replace(repository / "pyproject.toml", final_pyproject)
        pyproject_replaced = True
        _remove_exact(repository / PENDING_NOTICE, original_pending)
        pending_removed = True
        try:
            final_state = inspect_license_state(
                repository,
                license_specs=license_specs,
            )
        except LicenseStateError as exc:
            raise LicenseFinalizationError(str(exc)) from None
    except (OSError, LicenseFinalizationError) as exc:
        failures = _rollback(
            repository,
            original_pyproject=original_pyproject,
            original_pending=original_pending,
            license_bytes=license_bytes,
            license_creation_attempted=license_creation_attempted,
            license_created=license_created,
            pyproject_replaced=pyproject_replaced,
            pending_removed=pending_removed,
        )
        if failures:
            raise LicenseFinalizationError(
                f"finalization failed ({exc}); rollback incomplete: {failures}"
            ) from None
        raise LicenseFinalizationError(
            f"finalization failed and was rolled back: {exc}"
        ) from None

    plan["applied"] = True
    plan["verified_state"] = final_state
    return plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare or apply the owner-selected BenchHandoff license transition."
        )
    )
    parser.add_argument(
        "--license",
        choices=sorted(DEFAULT_LICENSE_SPECS),
        required=True,
    )
    parser.add_argument("--license-file", type=Path, required=True)
    parser.add_argument(
        "--expected-source-commit",
        required=True,
        help="full clean source commit expected before finalization",
    )
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the transition; without this flag the command is read-only",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = finalize_repository(
            arguments.repository,
            choice=arguments.license,
            license_file=arguments.license_file,
            expected_source_commit=arguments.expected_source_commit,
            apply=arguments.apply,
        )
    except LicenseFinalizationError as exc:
        print(f"license finalization failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
