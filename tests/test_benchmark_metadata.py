from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "benchmarks" / "synthetic" / "benchmark_metadata.py"


def load_metadata_module():
    specification = importlib.util.spec_from_file_location(
        "benchhandoff_benchmark_metadata",
        SCRIPT,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("could not load benchmark metadata module")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


class BenchmarkMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.metadata = load_metadata_module()

    def test_untracked_source_makes_git_provenance_dirty(self) -> None:
        repository_root = REPOSITORY_ROOT.resolve()
        responses = (
            subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=str(repository_root) + "\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout="a" * 40 + "\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout="?? untracked-source.py\n",
                stderr="",
            ),
        )
        with mock.patch.object(
            self.metadata,
            "_invoke_git",
            side_effect=responses,
        ) as invoke:
            commit, clean = self.metadata._source_git_state(repository_root)

        self.assertEqual(commit, "a" * 40)
        self.assertFalse(clean)
        self.assertIn("--untracked-files=all", invoke.call_args_list[-1].args)

    def test_clean_git_provenance_is_explicit(self) -> None:
        repository_root = REPOSITORY_ROOT.resolve()
        responses = (
            subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=str(repository_root) + "\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout="b" * 40 + "\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout="",
                stderr="",
            ),
        )
        with mock.patch.object(
            self.metadata,
            "_invoke_git",
            side_effect=responses,
        ):
            commit, clean = self.metadata._source_git_state(repository_root)

        self.assertEqual(commit, "b" * 40)
        self.assertTrue(clean)

    def test_provenance_uses_only_generic_platform_metadata(self) -> None:
        with (
            mock.patch.object(
                self.metadata,
                "_source_git_state",
                return_value=("a" * 40, True),
            ),
            mock.patch.object(
                self.metadata.runtime_platform,
                "system",
                return_value="TestOS",
            ),
            mock.patch.object(
                self.metadata.runtime_platform,
                "platform",
                side_effect=AssertionError("exact host build must not be read"),
            ),
        ):
            record = self.metadata.benchmark_provenance(REPOSITORY_ROOT)

        self.assertEqual(record["operating_system"], "TestOS")
        self.assertEqual(record["platform_details"], "TestOS")
        self.assertNotIn("timezone", record)


if __name__ == "__main__":
    unittest.main()
