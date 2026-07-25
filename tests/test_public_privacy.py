from __future__ import annotations

import importlib.util
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

    def test_project_author_is_pseudonymous(self) -> None:
        self.verifier.verify_project_author(REPOSITORY_ROOT)


if __name__ == "__main__":
    unittest.main()
