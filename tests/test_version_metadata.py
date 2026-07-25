from __future__ import annotations

import io
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from benchhandoff import __version__
from benchhandoff.cli import main

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class VersionMetadataTests(unittest.TestCase):
    def test_source_version_matches_project_metadata(self) -> None:
        metadata = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["project"]["version"], __version__)

    def test_tested_python_classifiers_match_checked_in_matrix(self) -> None:
        metadata = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        classifiers = set(metadata["project"]["classifiers"])
        expected = {
            f"Programming Language :: Python :: {version}"
            for version in ("3.11", "3.12", "3.13", "3.14")
        }
        self.assertTrue(expected.issubset(classifiers))

    def test_cli_reports_the_same_version(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), f"benchhandoff {__version__}\n")


if __name__ == "__main__":
    unittest.main()
