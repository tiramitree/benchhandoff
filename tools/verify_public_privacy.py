"""Fail closed on personal identity, contact, and credential material."""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path


LOGIN = "tiramitree"
MAX_TEXT_BYTES = 5 * 1024 * 1024
EMAIL_RE = re.compile(
    rb"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)
HOME_RE = re.compile(
    rb"(?:[A-Z]:[\\/](?:Users|Documents[ ]and[ ]Settings)[\\/][^\\/\s]+"
    rb"|/(?:Users|home)/[^/\s]+)",
    re.IGNORECASE,
)
IPV4_RE = re.compile(rb"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
TOKEN_RE = re.compile(
    rb"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
    rb"|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})"
)
PRIVATE_KEY_MARKER = ("-----BEGIN " + "PRIVATE KEY-----").encode()
OPENSSH_PRIVATE_KEY_MARKER = (
    "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
).encode()
ALLOWED_HOME_FIXTURES = {"tests/test_reproduction_package.py"}
ALLOWED_NETWORK_FIXTURES = {
    "tests/test_public_privacy.py",
    "tools/verify_public_privacy.py",
}


def fixture_allowed(relative: str, allowed: set[str]) -> bool:
    return any(
        relative == value or relative.endswith("/" + value)
        for value in allowed
    )


def allowed_noreply(value: bytes) -> bool:
    text = value.decode("ascii", errors="ignore").lower()
    suffix = "@users.noreply.github.com"
    if not text.endswith(suffix):
        return text == "noreply@github.com"
    local = text[: -len(suffix)]
    return local == LOGIN or local.endswith("+" + LOGIN)


def private_network_present(data: bytes) -> bool:
    for match in IPV4_RE.finditer(data):
        try:
            address = ipaddress.ip_address(match.group(0).decode("ascii"))
        except ValueError:
            continue
        if address.is_loopback:
            continue
        if (
            address in ipaddress.ip_network("10.0.0.0/8")
            or address in ipaddress.ip_network("172.16.0.0/12")
            or address in ipaddress.ip_network("192.168.0.0/16")
            or address in ipaddress.ip_network("100.64.0.0/10")
            or address.is_link_local
        ):
            return True
    return False


def categories_for(relative: str, data: bytes) -> tuple[str, ...]:
    findings: set[str] = set()
    if any(not allowed_noreply(match.group(0)) for match in EMAIL_RE.finditer(data)):
        findings.add("non_noreply_email")
    if not fixture_allowed(relative, ALLOWED_HOME_FIXTURES) and HOME_RE.search(data):
        findings.add("absolute_user_home_path")
    if TOKEN_RE.search(data):
        findings.add("credential_token_shape")
    if PRIVATE_KEY_MARKER in data or OPENSSH_PRIVATE_KEY_MARKER in data:
        findings.add("private_key_material")
    if (
        not fixture_allowed(relative, ALLOWED_NETWORK_FIXTURES)
        and private_network_present(data)
    ):
        findings.add("private_or_cgnat_network_address")
    return tuple(sorted(findings))


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [
        root / raw.decode("utf-8", errors="surrogateescape")
        for raw in result.stdout.split(b"\0")
        if raw
    ]


def inspect_bytes(
    relative: str,
    data: bytes,
    findings: list[tuple[str, tuple[str, ...]]],
) -> None:
    if len(data) > MAX_TEXT_BYTES or b"\0" in data:
        return
    categories = categories_for(relative, data)
    if categories:
        findings.append((relative, categories))


def inspect_archive(
    path: Path,
    findings: list[tuple[str, tuple[str, ...]]],
) -> None:
    relative = path.name
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir() and info.file_size <= MAX_TEXT_BYTES:
                    inspect_bytes(
                        f"{relative}!{info.filename}",
                        archive.read(info),
                        findings,
                    )
        return
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            for info in archive.getmembers():
                if not info.isfile() or info.size > MAX_TEXT_BYTES:
                    continue
                stream = archive.extractfile(info)
                if stream is not None:
                    inspect_bytes(
                        f"{relative}!{info.name}",
                        stream.read(),
                        findings,
                    )


def verify_project_author(root: Path) -> None:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    if project.get("authors") != [{"name": LOGIN}]:
        raise RuntimeError("project authors must contain only the GitHub login")


def verify_head_identity(root: Path) -> None:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "show",
            "-s",
            "--format=%an%x00%ae%x00%cn%x00%ce",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    values = result.stdout.rstrip("\n").split("\0")
    if len(values) != 4:
        raise RuntimeError("unexpected Git identity record")
    author_name, author_email, committer_name, committer_email = values
    if author_name != LOGIN or committer_name != LOGIN:
        raise RuntimeError("HEAD name must use only the GitHub login")
    if not all(
        allowed_noreply(value.encode("ascii", errors="ignore"))
        for value in (author_email, committer_email)
    ):
        raise RuntimeError("HEAD email must use the GitHub noreply address")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--skip-head", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    verify_project_author(root)
    if not args.skip_head:
        verify_head_identity(root)

    findings: list[tuple[str, tuple[str, ...]]] = []
    for path in tracked_files(root):
        if not path.is_file() or path.stat().st_size > MAX_TEXT_BYTES:
            continue
        inspect_bytes(path.relative_to(root).as_posix(), path.read_bytes(), findings)
    if args.dist:
        for path in sorted(args.dist.glob("*")):
            if path.is_file() and (
                path.suffix == ".whl" or path.name.endswith(".tar.gz")
            ):
                inspect_archive(path, findings)

    if findings:
        for relative, categories in findings:
            print(f"{relative}: {','.join(categories)}")
        return 1
    print("public privacy gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
