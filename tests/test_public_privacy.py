from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = REPOSITORY_ROOT / "tools" / "verify_public_privacy.py"


def load_verifier():
    specification = importlib.util.spec_from_file_location(
        "verify_public_privacy", VERIFY_SCRIPT
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("could not load privacy verifier")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class PublicPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.verifier = load_verifier()

    def test_account_specific_noreply_is_allowed(self) -> None:
        value = f"12345+{self.verifier.LOGIN}@users.noreply.github.com".encode()
        self.assertTrue(self.verifier.allowed_noreply(value))

    def test_ordinary_email_is_rejected(self) -> None:
        value = ("person" + "@" + "example.com").encode()
        self.assertFalse(self.verifier.allowed_noreply(value))

    def test_credential_shape_is_rejected(self) -> None:
        value = b"gh" + b"p_" + (b"a" * 30)
        self.assertIn(
            "credential_token_shape",
            self.verifier.categories_for("README.md", value),
        )

    def test_private_network_is_rejected(self) -> None:
        self.assertIn(
            "private_or_cgnat_network_address",
            self.verifier.categories_for("config.txt", b"service=10.20.30.40"),
        )

    def test_loopback_is_allowed(self) -> None:
        self.assertNotIn(
            "private_or_cgnat_network_address",
            self.verifier.categories_for("tests/example.txt", b"127.0.0.1"),
        )

    def test_non_utc_timezone_metadata_is_rejected(self) -> None:
        value = (
            b'{"time'
            + b'zone":"Example/'
            + b'Local","observed":"UTC'
            + b"+"
            + b'03:30"}'
        )
        self.assertIn(
            "local_timezone_metadata",
            self.verifier.categories_for("evidence.json", value),
        )

    def test_compact_gmt_and_iso_offsets_are_rejected(self) -> None:
        for value in (
            b"GMT" + b"+" + b"7",
            b"UTC" + b"-" + b"0430",
            b"2026-07-29T12:00:00" + b"+" + b"06:45",
        ):
            with self.subTest(value_length=len(value)):
                self.assertIn(
                    "local_timezone_metadata",
                    self.verifier.categories_for("evidence.json", value),
                )

    def test_zero_timezone_offsets_are_allowed(self) -> None:
        for value in (
            b"UTC" + b"+" + b"00:00",
            b"GMT" + b"-" + b"0",
            b"2026-07-29T12:00:00" + b"+" + b"0000",
        ):
            with self.subTest(value_length=len(value)):
                self.assertNotIn(
                    "local_timezone_metadata",
                    self.verifier.categories_for("evidence.json", value),
                )

    def test_exact_host_build_metadata_is_rejected(self) -> None:
        value = (
            b'{"platform_'
            + b'details":"Windows'
            + b"-11-10.0."
            + b'99999-SP0"}'
        )
        self.assertIn(
            "exact_host_platform_metadata",
            self.verifier.categories_for("evidence.json", value),
        )

    def test_generic_platform_metadata_is_allowed(self) -> None:
        self.assertNotIn(
            "exact_host_platform_metadata",
            self.verifier.categories_for(
                "evidence.json",
                b'{"platform_' + b'details":"Windows"}',
            ),
        )

    def test_project_author_is_pseudonymous(self) -> None:
        self.verifier.verify_project_author(REPOSITORY_ROOT)

    def test_evidence_directory_scans_every_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "takeover-evidence.json").write_text(
                '{"kind":"synthetic"}\n',
                encoding="utf-8",
            )
            (root / "SHA256SUMS").write_bytes(
                b"service=10.20.30.40\n",
            )
            findings: list[tuple[str, tuple[str, ...]]] = []
            self.verifier.inspect_evidence_directory(root, findings)
        self.assertEqual(
            findings,
            [
                (
                    "evidence!SHA256SUMS",
                    ("private_or_cgnat_network_address",),
                )
            ],
        )

    def test_evidence_directory_rejects_nested_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            with self.assertRaisesRegex(RuntimeError, "non-file"):
                self.verifier.inspect_evidence_directory(root, [])

    def test_evidence_directory_rejects_nul_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "takeover-evidence.json").write_bytes(
                b'{"kind":"synthetic"}\0service=10.20.30.40\n'
            )
            with self.assertRaisesRegex(RuntimeError, "bounded text"):
                self.verifier.inspect_evidence_directory(root, [])


if __name__ == "__main__":
    unittest.main()
