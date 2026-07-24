from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "tools" / "verify_external_evidence.py"
LEDGER = REPOSITORY_ROOT / "EXTERNAL_EVIDENCE.json"


def _load_module() -> object:
    specification = importlib.util.spec_from_file_location(
        "benchhandoff_external_evidence",
        SCRIPT,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("unable to load external-evidence validator")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


MODULE = _load_module()


def _canonical(document: dict[str, object]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _empty() -> dict[str, object]:
    return {
        "as_of_date": "2026-07-24",
        "counts": {
            "independent_reproduction_reports": 0,
            "independent_users": 0,
            "institutional_adopters": 0,
            "third_party_reviews": 0,
        },
        "records": [],
        "schema_version": 1,
    }


def _record(
    number: int,
    kind: str,
    subject: str,
    *,
    status: str = "verified",
) -> dict[str, object]:
    record: dict[str, object] = {
        "consent_to_list": True,
        "evidence_url": f"https://github.com/example/project/issues/{number}",
        "id": f"EXT-2026-{number:04d}",
        "kind": kind,
        "observed_date": "2026-07-20",
        "relationship": "independent",
        "scope": "Bounded public synthetic validation.",
        "source_commit": f"{number:040x}",
        "status": status,
        "subject": subject,
        "verified_date": "2026-07-21",
    }
    if status == "retracted":
        record["retracted_date"] = "2026-07-22"
        record["retraction_reason"] = "Reporter withdrew the public evidence."
    return record


class ExternalEvidenceTests(unittest.TestCase):
    def test_checked_in_zero_baseline_passes(self) -> None:
        document, counts = MODULE.load_and_validate(LEDGER)
        self.assertEqual(document["records"], [])
        self.assertEqual(set(counts.values()), {0})

    def test_mixed_records_derive_counts_and_unique_subjects(self) -> None:
        document = _empty()
        records = [
            _record(1, "independent_reproduction", "@alice"),
            _record(2, "independent_reproduction", "@alice"),
            _record(3, "independent_user", "@alice"),
            _record(4, "independent_user", "＠Ａｌｉｃｅ"),
            _record(5, "institutional_adoption", "Example Robotics Lab"),
            _record(6, "third_party_review", "@reviewer"),
            _record(
                7,
                "institutional_adoption",
                "Withdrawn Institution",
                status="retracted",
            ),
        ]
        document["records"] = records
        document["counts"] = {
            "independent_reproduction_reports": 2,
            "independent_users": 1,
            "institutional_adopters": 1,
            "third_party_reviews": 1,
        }
        self.assertEqual(MODULE.validate_document(document), document["counts"])

    def test_count_drift_is_rejected(self) -> None:
        document = _empty()
        document["records"] = [_record(1, "independent_user", "@alice")]
        with self.assertRaisesRegex(
            MODULE.ExternalEvidenceError,
            "published counts do not match",
        ):
            MODULE.validate_document(document)

    def test_duplicate_keys_and_noncanonical_json_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            path.write_text(
                '{"schema_version":1,"schema_version":1}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                MODULE.ExternalEvidenceError,
                "duplicate JSON key",
            ):
                MODULE.load_and_validate(path)

            path.write_text(json.dumps(_empty()), encoding="utf-8")
            with self.assertRaisesRegex(
                MODULE.ExternalEvidenceError,
                "canonical UTF-8 JSON",
            ):
                MODULE.load_and_validate(path)

            path.write_bytes(b'{"x":"\\ud800"}\n')
            with self.assertRaisesRegex(
                MODULE.ExternalEvidenceError,
                "invalid Unicode scalar",
            ):
                MODULE.load_and_validate(path)

            source = Path(directory) / "source.json"
            linked = Path(directory) / "linked.json"
            source.write_text(_canonical(_empty()), encoding="utf-8")
            os.link(source, linked)
            with self.assertRaisesRegex(
                MODULE.ExternalEvidenceError,
                "regular non-linked",
            ):
                MODULE.load_and_validate(linked)

    def test_unsafe_url_relationship_and_missing_consent_are_rejected(self) -> None:
        base = _empty()
        base["records"] = [_record(1, "independent_user", "@alice")]
        for field, value, pattern in (
            ("evidence_url", "https://127.0.0.1/private", "private or reserved"),
            (
                "evidence_url",
                "https://github.com/example/project?token=private",
                "query string",
            ),
            ("relationship", "affiliate", "exactly 'independent'"),
            ("consent_to_list", False, "must be true"),
        ):
            with self.subTest(field=field):
                document = copy.deepcopy(base)
                document["records"][0][field] = value
                with self.assertRaisesRegex(MODULE.ExternalEvidenceError, pattern):
                    MODULE.validate_document(document)

    def test_retracted_record_requires_reason_and_does_not_count(self) -> None:
        document = _empty()
        record = _record(
            1,
            "third_party_review",
            "@reviewer",
            status="retracted",
        )
        document["records"] = [record]
        self.assertEqual(MODULE.validate_document(document), document["counts"])

        del record["retraction_reason"]
        with self.assertRaisesRegex(
            MODULE.ExternalEvidenceError,
            "fields differ",
        ):
            MODULE.validate_document(document)

    def test_record_order_id_commit_and_date_boundaries_are_rejected(self) -> None:
        cases: list[tuple[str, object, str]] = [
            ("id", "EXT-2025-0001", "id year"),
            ("source_commit", "A" * 40, "40 lowercase"),
            ("verified_date", "2026-07-19", "precedes"),
        ]
        for field, value, pattern in cases:
            with self.subTest(field=field):
                document = _empty()
                record = _record(1, "independent_reproduction", "@alice")
                record[field] = value
                document["records"] = [record]
                document["counts"]["independent_reproduction_reports"] = 1
                with self.assertRaisesRegex(MODULE.ExternalEvidenceError, pattern):
                    MODULE.validate_document(document)

        document = _empty()
        document["records"] = [
            _record(2, "independent_reproduction", "@bob"),
            _record(1, "independent_reproduction", "@alice"),
        ]
        document["counts"]["independent_reproduction_reports"] = 2
        with self.assertRaisesRegex(MODULE.ExternalEvidenceError, "sorted by id"):
            MODULE.validate_document(document)

        document = _empty()
        first = _record(1, "third_party_review", "@alice")
        second = _record(2, "third_party_review", "@bob")
        first["evidence_url"] += "#first"
        second["evidence_url"] = first["evidence_url"].replace("#first", "#second")
        document["records"] = [first, second]
        document["counts"]["third_party_reviews"] = 2
        with self.assertRaisesRegex(MODULE.ExternalEvidenceError, "duplicate evidence"):
            MODULE.validate_document(document)

    def test_cli_failure_is_bounded_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            path.write_text("{}\n", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = MODULE.main([str(path)])
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("external evidence validation failed:", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
