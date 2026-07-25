from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

MAX_METADATA_BYTES = 256 * 1024
MAX_LICENSE_BYTES = 64 * 1024
PENDING_NOTICE = "LICENSING_STATUS.md"
LICENSE_FILE = "LICENSE"
FINAL_BUILD_REQUIREMENT = "setuptools>=77.0.3"
REPARSE_POINT = 0x400
SETUPTOOLS_REQUIREMENT = re.compile(r"setuptools(?:[<>=!~].*)?\Z", re.IGNORECASE)


class LicenseStateError(ValueError):
    """A bounded license-state validation failure."""


@dataclass(frozen=True)
class LicenseSpec:
    identifier: str
    size: int
    sha256: str
    source_commit: str
    source_url: str
    derivation: str | None = None


DEFAULT_LICENSE_SPECS: Mapping[str, LicenseSpec] = {
    "Apache-2.0": LicenseSpec(
        identifier="Apache-2.0",
        size=10280,
        sha256="074e6e32c86a4c0ef8b3ed25b721ca23aca83df277cd88106ef7177c354615ff",
        source_commit="c4a7237ec8f4654e867546f9f409749300f1bf4c",
        source_url=(
            "https://raw.githubusercontent.com/spdx/license-list-data/"
            "c4a7237ec8f4654e867546f9f409749300f1bf4c/text/Apache-2.0.txt"
        ),
    ),
    "MIT": LicenseSpec(
        identifier="MIT",
        size=1068,
        sha256="ff4ef001f40e1f04fc476bf5b893b993c8b66708e204697ad9b5e03b9addd13d",
        source_commit="c4a7237ec8f4654e867546f9f409749300f1bf4c",
        source_url=(
            "https://raw.githubusercontent.com/spdx/license-list-data/"
            "c4a7237ec8f4654e867546f9f409749300f1bf4c/text/MIT.txt"
        ),
        derivation=(
            "replace <year> <copyright holders> with 2026 tiramitree"
        ),
    ),
}


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    return bool(getattr(file_stat, "st_file_attributes", 0) & REPARSE_POINT)


def _identity(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def path_exists_no_follow(path: Path) -> bool:
    try:
        path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LicenseStateError(f"unable to inspect {path.name}: {exc}") from None
    return True


def read_bounded_regular_file(path: Path, *, maximum: int) -> bytes:
    try:
        before = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise LicenseStateError(f"unable to inspect {path.name}: {exc}") from None
    if (
        not stat.S_ISREG(before.st_mode)
        or _is_reparse_point(before)
        or before.st_nlink != 1
    ):
        raise LicenseStateError(f"{path.name} must be a regular non-linked file")
    if before.st_size > maximum:
        raise LicenseStateError(f"{path.name} exceeds {maximum} bytes")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LicenseStateError(f"unable to open {path.name}: {exc}") from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse_point(opened)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(before)
        ):
            raise LicenseStateError(f"{path.name} identity changed before read")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            raise LicenseStateError(f"{path.name} exceeds {maximum} bytes")
        if _identity(os.fstat(descriptor)) != _identity(opened):
            raise LicenseStateError(f"{path.name} changed during read")
    finally:
        os.close(descriptor)

    try:
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise LicenseStateError(f"unable to re-inspect {path.name}: {exc}") from None
    if _identity(after) != _identity(before):
        raise LicenseStateError(f"{path.name} identity changed during read")
    return raw


def _utf8_lf_text(raw: bytes, name: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise LicenseStateError(f"{name} must be strict UTF-8") from None
    if text.startswith("\ufeff"):
        raise LicenseStateError(f"{name} must not contain a byte-order mark")
    if "\r" in text or not text.endswith("\n"):
        raise LicenseStateError(f"{name} must use LF and end with one newline")
    return text


def load_pyproject(repository: Path) -> tuple[bytes, dict[str, object]]:
    path = repository / "pyproject.toml"
    raw = read_bounded_regular_file(path, maximum=MAX_METADATA_BYTES)
    text = _utf8_lf_text(raw, path.name)
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LicenseStateError(f"invalid pyproject.toml: {exc}") from None
    if not isinstance(document, dict):
        raise LicenseStateError("pyproject.toml root must be a table")
    return raw, document


def _project_tables(
    document: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    project = document.get("project")
    build_system = document.get("build-system")
    if not isinstance(project, dict) or not isinstance(build_system, dict):
        raise LicenseStateError("pyproject.toml needs project and build-system tables")
    return project, build_system


def _setuptools_requirement(build_system: dict[str, object]) -> str:
    requirements = build_system.get("requires")
    if not isinstance(requirements, list) or not all(
        isinstance(value, str) for value in requirements
    ):
        raise LicenseStateError("build-system.requires must be a string array")
    matches = [
        value
        for value in requirements
        if SETUPTOOLS_REQUIREMENT.fullmatch(value)
    ]
    if len(matches) != 1:
        raise LicenseStateError("build-system must have one setuptools requirement")
    return matches[0]


def _pending_state(
    repository: Path,
    project: dict[str, object],
    build_system: dict[str, object],
) -> dict[str, object]:
    if "license" in project or "license-files" in project:
        raise LicenseStateError("pending state must not publish license metadata")
    raw = read_bounded_regular_file(
        repository / PENDING_NOTICE,
        maximum=MAX_METADATA_BYTES,
    )
    text = _utf8_lf_text(raw, PENDING_NOTICE)
    if not text.startswith("# License Pending\n") or (
        "No open-source license has been selected" not in text
    ):
        raise LicenseStateError("pending notice lacks the required boundary")
    return {
        "license_file": None,
        "license_sha256": None,
        "mode": "pending",
        "pending_notice": PENDING_NOTICE,
        "setuptools_requirement": _setuptools_requirement(build_system),
        "spdx_expression": None,
    }


def _final_state(
    repository: Path,
    project: dict[str, object],
    build_system: dict[str, object],
    *,
    license_specs: Mapping[str, LicenseSpec],
) -> dict[str, object]:
    expression = project.get("license")
    if not isinstance(expression, str) or expression not in license_specs:
        raise LicenseStateError("final license must be Apache-2.0 or MIT")
    if project.get("license-files") != [LICENSE_FILE]:
        raise LicenseStateError('project.license-files must equal ["LICENSE"]')
    requirement = _setuptools_requirement(build_system)
    if requirement != FINAL_BUILD_REQUIREMENT:
        raise LicenseStateError(
            f"final state requires {FINAL_BUILD_REQUIREMENT}"
        )

    raw = read_bounded_regular_file(
        repository / LICENSE_FILE,
        maximum=MAX_LICENSE_BYTES,
    )
    _utf8_lf_text(raw, LICENSE_FILE)
    digest = hashlib.sha256(raw).hexdigest()
    spec = license_specs[expression]
    if len(raw) != spec.size or digest != spec.sha256:
        raise LicenseStateError(
            f"LICENSE does not match the canonical {expression} candidate"
        )
    return {
        "license_file": LICENSE_FILE,
        "license_sha256": digest,
        "license_source_commit": spec.source_commit,
        "license_source_derivation": spec.derivation,
        "license_source_url": spec.source_url,
        "mode": "final",
        "pending_notice": None,
        "setuptools_requirement": requirement,
        "spdx_expression": expression,
    }


def inspect_license_state(
    repository: Path,
    *,
    license_specs: Mapping[str, LicenseSpec] = DEFAULT_LICENSE_SPECS,
) -> dict[str, object]:
    repository = repository.resolve()
    if not repository.is_dir():
        raise LicenseStateError("repository must be a directory")
    _, document = load_pyproject(repository)
    project, build_system = _project_tables(document)
    pending_exists = path_exists_no_follow(repository / PENDING_NOTICE)
    license_exists = path_exists_no_follow(repository / LICENSE_FILE)
    if pending_exists == license_exists:
        raise LicenseStateError(
            "exactly one of LICENSING_STATUS.md or LICENSE must exist"
        )
    if pending_exists:
        return _pending_state(repository, project, build_system)
    return _final_state(
        repository,
        project,
        build_system,
        license_specs=license_specs,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate BenchHandoff's pending or final license state."
    )
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (default: this checkout)",
    )
    parser.add_argument(
        "--require-final",
        action="store_true",
        help="reject the valid pending state; use for package/release gates",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = inspect_license_state(arguments.repository)
        if arguments.require_final and result["mode"] != "final":
            raise LicenseStateError("final license state is required")
    except LicenseStateError as exc:
        print(f"license state validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
