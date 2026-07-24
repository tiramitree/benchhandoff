from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import stat
import sys
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import SplitResult, urlsplit

MAX_LEDGER_BYTES = 256 * 1024
SCHEMA_VERSION = 1
ROOT_FIELDS = {"as_of_date", "counts", "records", "schema_version"}
COUNT_FIELDS = {
    "independent_reproduction_reports",
    "independent_users",
    "institutional_adopters",
    "third_party_reviews",
}
KINDS = {
    "independent_reproduction",
    "independent_user",
    "institutional_adoption",
    "third_party_review",
}
BASE_RECORD_FIELDS = {
    "consent_to_list",
    "evidence_url",
    "id",
    "kind",
    "observed_date",
    "relationship",
    "scope",
    "source_commit",
    "status",
    "subject",
    "verified_date",
}
RETRACTED_RECORD_FIELDS = BASE_RECORD_FIELDS | {
    "retracted_date",
    "retraction_reason",
}
RECORD_ID = re.compile(r"EXT-[0-9]{4}-[0-9]{4}\Z")
SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
REPARSE_POINT = 0x400


class ExternalEvidenceError(ValueError):
    """A bounded validation failure suitable for a public CLI message."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExternalEvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ExternalEvidenceError(f"non-finite JSON number is forbidden: {value}")


def _identity(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & REPARSE_POINT)


def _is_regular_unlinked(file_stat: os.stat_result) -> bool:
    return (
        stat.S_ISREG(file_stat.st_mode)
        and not _is_reparse_point(file_stat)
        and file_stat.st_nlink == 1
    )


def _read_bounded_regular_file(path: Path) -> bytes:
    try:
        before = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ExternalEvidenceError(f"unable to inspect ledger: {exc}") from None
    if not _is_regular_unlinked(before):
        raise ExternalEvidenceError("ledger must be a regular non-linked file")
    if before.st_size > MAX_LEDGER_BYTES:
        raise ExternalEvidenceError(
            f"ledger exceeds {MAX_LEDGER_BYTES} bytes"
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExternalEvidenceError(f"unable to open ledger: {exc}") from None
    try:
        opened = os.fstat(descriptor)
        if not _is_regular_unlinked(opened):
            raise ExternalEvidenceError("ledger must be a regular non-linked file")
        if _identity(opened) != _identity(before):
            raise ExternalEvidenceError("ledger identity changed before read")
        chunks: list[bytes] = []
        remaining = MAX_LEDGER_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_LEDGER_BYTES:
            raise ExternalEvidenceError(
                f"ledger exceeds {MAX_LEDGER_BYTES} bytes"
            )
        after_read = os.fstat(descriptor)
        if _identity(after_read) != _identity(opened):
            raise ExternalEvidenceError("ledger changed during read")
    finally:
        os.close(descriptor)

    try:
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ExternalEvidenceError(
            f"unable to re-inspect ledger: {exc}"
        ) from None
    if _identity(after) != _identity(before) or not _is_regular_unlinked(after):
        raise ExternalEvidenceError("ledger identity changed during read")
    return raw


def _parse_json(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ExternalEvidenceError("ledger must be strict UTF-8") from None
    if text.startswith("\ufeff"):
        raise ExternalEvidenceError("UTF-8 byte-order marks are forbidden")
    try:
        document = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except ExternalEvidenceError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ExternalEvidenceError(f"invalid JSON: {exc}") from None
    if not isinstance(document, dict):
        raise ExternalEvidenceError("ledger root must be an object")
    try:
        canonical = (
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    except UnicodeEncodeError:
        raise ExternalEvidenceError(
            "ledger contains an invalid Unicode scalar value"
        ) from None
    if raw != canonical:
        raise ExternalEvidenceError(
            "ledger must use canonical UTF-8 JSON with sorted keys and a final newline"
        )
    return document


def _exact_fields(value: dict[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise ExternalEvidenceError(
            f"{context} fields differ; missing={missing}, extra={extra}"
        )


def _bounded_line(
    value: Any,
    field: str,
    *,
    maximum: int,
    minimum: int = 1,
) -> str:
    if type(value) is not str:
        raise ExternalEvidenceError(f"{field} must be a string")
    if value != value.strip() or not minimum <= len(value) <= maximum:
        raise ExternalEvidenceError(
            f"{field} must be {minimum}..{maximum} stripped characters"
        )
    if any(
        ord(character) < 32
        or ord(character) == 127
        or unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    ):
        raise ExternalEvidenceError(f"{field} must be one printable line")
    return value


def _iso_date(value: Any, field: str) -> date:
    text = _bounded_line(value, field, maximum=10, minimum=10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ExternalEvidenceError(f"{field} must be an ISO calendar date") from None
    if parsed.isoformat() != text:
        raise ExternalEvidenceError(f"{field} must be an ISO calendar date")
    return parsed


def _url_key(value: Any, field: str) -> tuple[str, str, int | None, str, str, str]:
    text = _bounded_line(value, field, maximum=2048)
    if "\\" in text:
        raise ExternalEvidenceError(f"{field} must not contain backslashes")
    try:
        parsed: SplitResult = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ExternalEvidenceError(f"{field} is invalid: {exc}") from None
    if parsed.scheme != "https" or not parsed.netloc or parsed.hostname is None:
        raise ExternalEvidenceError(f"{field} must be a public HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ExternalEvidenceError(f"{field} must not contain credentials")
    if port == 0:
        raise ExternalEvidenceError(f"{field} has an invalid port")
    if parsed.query:
        raise ExternalEvidenceError(f"{field} must not contain a query string")
    hostname = parsed.hostname
    lowered = hostname.casefold().rstrip(".")
    if (
        lowered == "localhost"
        or lowered.endswith(".localhost")
        or lowered.endswith(".local")
        or lowered.endswith(".internal")
    ):
        raise ExternalEvidenceError(f"{field} must not name a local host")
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        try:
            ascii_hostname = lowered.encode("idna").decode("ascii")
        except UnicodeError:
            raise ExternalEvidenceError(f"{field} has an invalid host") from None
        if (
            len(ascii_hostname) > 253
            or "." not in ascii_hostname
            or any(not DNS_LABEL.fullmatch(label) for label in ascii_hostname.split("."))
        ):
            raise ExternalEvidenceError(f"{field} has an invalid public host")
        normalized_host = ascii_hostname.casefold()
    else:
        if not address.is_global:
            raise ExternalEvidenceError(
                f"{field} must not use a private or reserved address"
            )
        normalized_host = address.compressed
    return (
        parsed.scheme,
        normalized_host,
        None if port in (None, 443) else port,
        parsed.path or "/",
        "",
        "",
    )


def _validate_record(
    record: Any,
    *,
    index: int,
    as_of: date,
) -> tuple[str, str, str, tuple[str, str, int | None, str, str, str], str]:
    context = f"records[{index}]"
    if not isinstance(record, dict):
        raise ExternalEvidenceError(f"{context} must be an object")
    status = record.get("status")
    if status == "verified":
        _exact_fields(record, BASE_RECORD_FIELDS, context)
    elif status == "retracted":
        _exact_fields(record, RETRACTED_RECORD_FIELDS, context)
    else:
        raise ExternalEvidenceError(f"{context}.status is invalid")

    record_id = _bounded_line(record["id"], f"{context}.id", maximum=13, minimum=13)
    if not RECORD_ID.fullmatch(record_id):
        raise ExternalEvidenceError(f"{context}.id must match EXT-YYYY-NNNN")
    kind = _bounded_line(record["kind"], f"{context}.kind", maximum=32)
    if kind not in KINDS:
        raise ExternalEvidenceError(f"{context}.kind is invalid")
    subject = _bounded_line(record["subject"], f"{context}.subject", maximum=120)
    scope = _bounded_line(record["scope"], f"{context}.scope", maximum=240)
    relationship = _bounded_line(
        record["relationship"],
        f"{context}.relationship",
        maximum=32,
    )
    if relationship != "independent":
        raise ExternalEvidenceError(
            f"{context}.relationship must be exactly 'independent'"
        )
    if record["consent_to_list"] is not True:
        raise ExternalEvidenceError(f"{context}.consent_to_list must be true")
    source_commit = _bounded_line(
        record["source_commit"],
        f"{context}.source_commit",
        maximum=40,
        minimum=40,
    )
    if not SOURCE_COMMIT.fullmatch(source_commit):
        raise ExternalEvidenceError(
            f"{context}.source_commit must be 40 lowercase hexadecimal characters"
        )
    observed = _iso_date(record["observed_date"], f"{context}.observed_date")
    verified = _iso_date(record["verified_date"], f"{context}.verified_date")
    if record_id[4:8] != observed.isoformat()[:4]:
        raise ExternalEvidenceError(f"{context}.id year must match observed_date")
    if verified < observed:
        raise ExternalEvidenceError(
            f"{context}.verified_date precedes observed_date"
        )
    if verified > as_of:
        raise ExternalEvidenceError(f"{context}.verified_date exceeds as_of_date")
    evidence_key = _url_key(record["evidence_url"], f"{context}.evidence_url")

    if status == "retracted":
        retracted = _iso_date(
            record["retracted_date"],
            f"{context}.retracted_date",
        )
        if retracted < verified or retracted > as_of:
            raise ExternalEvidenceError(
                f"{context}.retracted_date is outside the permitted range"
            )
        _bounded_line(
            record["retraction_reason"],
            f"{context}.retraction_reason",
            maximum=240,
        )
    subject_key = unicodedata.normalize("NFKC", subject).casefold()
    return record_id, kind, subject_key, evidence_key, status


def validate_document(document: dict[str, Any]) -> dict[str, int]:
    _exact_fields(document, ROOT_FIELDS, "root")
    if type(document["schema_version"]) is not int:
        raise ExternalEvidenceError("schema_version must be an integer")
    if document["schema_version"] != SCHEMA_VERSION:
        raise ExternalEvidenceError(
            f"schema_version must be {SCHEMA_VERSION}"
        )
    as_of = _iso_date(document["as_of_date"], "as_of_date")

    counts = document["counts"]
    if not isinstance(counts, dict):
        raise ExternalEvidenceError("counts must be an object")
    _exact_fields(counts, COUNT_FIELDS, "counts")
    for field in sorted(COUNT_FIELDS):
        value = counts[field]
        if type(value) is not int or value < 0:
            raise ExternalEvidenceError(f"counts.{field} must be a non-negative integer")

    records = document["records"]
    if not isinstance(records, list):
        raise ExternalEvidenceError("records must be an array")
    if len(records) > 10_000:
        raise ExternalEvidenceError("records exceeds 10000 entries")

    ids: list[str] = []
    id_set: set[str] = set()
    evidence_keys: set[tuple[str, str, int | None, str, str, str]] = set()
    reproduction_reports = 0
    independent_users: set[str] = set()
    institutional_adopters: set[str] = set()
    third_party_reviews = 0
    for index, record in enumerate(records):
        record_id, kind, subject, evidence_key, status = _validate_record(
            record,
            index=index,
            as_of=as_of,
        )
        if record_id in id_set:
            raise ExternalEvidenceError(f"duplicate record id: {record_id}")
        if evidence_key in evidence_keys:
            raise ExternalEvidenceError(
                f"duplicate evidence URL in record: {record_id}"
            )
        ids.append(record_id)
        id_set.add(record_id)
        evidence_keys.add(evidence_key)
        if status == "retracted":
            continue
        if kind == "independent_reproduction":
            reproduction_reports += 1
        elif kind == "independent_user":
            independent_users.add(subject)
        elif kind == "institutional_adoption":
            institutional_adopters.add(subject)
        elif kind == "third_party_review":
            third_party_reviews += 1

    if ids != sorted(ids):
        raise ExternalEvidenceError("records must be sorted by id")
    derived = {
        "independent_reproduction_reports": reproduction_reports,
        "independent_users": len(independent_users),
        "institutional_adopters": len(institutional_adopters),
        "third_party_reviews": third_party_reviews,
    }
    if counts != derived:
        raise ExternalEvidenceError(
            f"published counts do not match verified records: expected {derived}"
        )
    return derived


def load_and_validate(path: Path) -> tuple[dict[str, Any], dict[str, int]]:
    document = _parse_json(_read_bounded_regular_file(path))
    return document, validate_document(document)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate BenchHandoff's canonical external-evidence ledger."
    )
    parser.add_argument(
        "ledger",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "EXTERNAL_EVIDENCE.json",
        help="ledger path (default: repository EXTERNAL_EVIDENCE.json)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        document, counts = load_and_validate(arguments.ledger)
    except ExternalEvidenceError as exc:
        print(f"external evidence validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "external evidence ledger valid: "
        f"as_of={document['as_of_date']}; "
        f"records={len(document['records'])}; "
        f"reproductions={counts['independent_reproduction_reports']}; "
        f"independent_users={counts['independent_users']}; "
        f"institutions={counts['institutional_adopters']}; "
        f"third_party_reviews={counts['third_party_reviews']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
