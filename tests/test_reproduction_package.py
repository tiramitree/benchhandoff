from __future__ import annotations

from contextlib import redirect_stderr
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "benchmarks" / "synthetic" / "reproduce.py"

from tests.workspace_temp import WorkspaceTemporaryDirectory


def _load_module() -> object:
    specification = importlib.util.spec_from_file_location(
        "benchhandoff_reproduction_package",
        SCRIPT,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("unable to load reproduction package module")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        specification.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPT.parent))
    return module


def _provenance(commit: str, *, clean: bool) -> dict[str, object]:
    return {
        "generated_at_utc": "2026-07-24T00:00:00Z",
        "operating_system": "TestOS",
        "platform": "test",
        "platform_details": "Unknown",
        "python_version": "3.12.0",
        "python_implementation": "CPython",
        "source_git_commit": commit,
        "source_git_clean": clean,
    }


def _focused(commit: str) -> dict[str, object]:
    return {
        **_provenance(commit, clean=True),
        "schema_version": 1,
        "benchmark": "synthetic-interruption-recovery",
        "attempts": 2,
        "first_exit_code": 75,
        "first_status": "failed",
        "quarantined_outputs": 1,
        "recovery_success": True,
        "resume_status": "completed",
        "verify_status": "verified",
    }


def _pipeline(commit: str) -> dict[str, object]:
    final = {"sha256": "f" * 64, "size": 10}
    return {
        **_provenance(commit, clean=True),
        "schema_version": 1,
        "benchmark": "synthetic-12-task-restart-vs-resume",
        "task_count": 12,
        "first_failure_task": 6,
        "exact_expectations": {
            "naive_subprocess_calls": True,
            "ledger_subprocess_calls": True,
            "naive_duplicate_successes": True,
            "ledger_duplicate_successes": True,
            "naive_final_tasks": True,
            "ledger_final_tasks": True,
            "ledger_failure_code": True,
            "ledger_quarantined_outputs": True,
            "same_final_output": True,
            "ledger_verified": True,
        },
        "naive_restart": {
            "subprocess_calls": 18,
            "successful_task_executions": 17,
            "duplicate_successful_executions": 5,
            "final_tasks_present": 12,
            "failure_codes": [75],
            "final_output": final,
        },
        "ledger_resume": {
            "subprocess_calls": 13,
            "successful_task_executions": 12,
            "duplicate_successful_executions": 0,
            "final_tasks_completed": 12,
            "first_failure_code": 75,
            "quarantined_outputs": 1,
            "verify_status": "verified",
            "final_output": final,
        },
        "comparison": {
            "avoided_subprocess_calls": 5,
            "avoided_duplicate_successful_executions": 5,
        },
        "timing_claim": "none; this benchmark reports deterministic work counts only",
        "scope": "local synthetic behavior, not production or third-party evidence",
    }


def _paths(root: Path) -> tuple[Path, Path]:
    source = root / "source"
    source.mkdir()
    output_parent = root / "output-parent"
    output_parent.mkdir()
    return source, output_parent / "package"


class ReproductionPackageTests(unittest.TestCase):
    def test_builds_exact_bounded_package_and_manifest(self) -> None:
        module = _load_module()
        commit = "a" * 40
        with WorkspaceTemporaryDirectory(prefix="reproduction-package-") as temporary:
            source, output = _paths(Path(temporary))
            with mock.patch.object(
                module,
                "benchmark_provenance",
                return_value=_provenance(commit, clean=True),
            ):
                summary = module.build_reproduction_package(
                    output,
                    repository_root=source,
                    focused_runner=lambda: _focused(commit),
                    pipeline_runner=lambda: _pipeline(commit),
                )

            self.assertEqual(summary["source_git_commit"], commit)
            self.assertEqual(
                {entry.name for entry in output.iterdir()},
                {
                    "focused-recovery.json",
                    "pipeline-comparison.json",
                    "summary.json",
                    "SHA256SUMS.txt",
                    "PACKAGE_COMPLETE.json",
                },
            )
            manifest = {}
            for row in (output / "SHA256SUMS.txt").read_text(
                encoding="utf-8"
            ).splitlines():
                digest, filename = row.split("  ", 1)
                manifest[filename] = digest
            self.assertEqual(
                set(manifest),
                {
                    "focused-recovery.json",
                    "pipeline-comparison.json",
                    "summary.json",
                },
            )
            for filename, digest in manifest.items():
                self.assertEqual(module._sha256_file(output / filename), digest)
            stored = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(stored, summary)
            completion = json.loads(
                (output / "PACKAGE_COMPLETE.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                completion["manifest_sha256"],
                module._sha256_file(output / "SHA256SUMS.txt"),
            )

    def test_refuses_existing_output_before_running_benchmarks(self) -> None:
        module = _load_module()
        with WorkspaceTemporaryDirectory(prefix="reproduction-existing-") as temporary:
            source, output = _paths(Path(temporary))
            output.mkdir()
            runner = mock.Mock()
            with self.assertRaisesRegex(
                module.ReproductionPackageError,
                "must not already exist",
            ):
                module.build_reproduction_package(
                    output,
                    repository_root=source,
                    focused_runner=runner,
                    pipeline_runner=runner,
                )
            runner.assert_not_called()

    def test_refuses_dirty_source_without_creating_output(self) -> None:
        module = _load_module()
        commit = "b" * 40
        with WorkspaceTemporaryDirectory(prefix="reproduction-dirty-") as temporary:
            source, output = _paths(Path(temporary))
            with mock.patch.object(
                module,
                "benchmark_provenance",
                return_value=_provenance(commit, clean=False),
            ):
                with self.assertRaisesRegex(
                    module.ReproductionPackageError,
                    "must be clean",
                ):
                    module.build_reproduction_package(
                        output,
                        repository_root=source,
                    )
            self.assertFalse(output.exists())

    def test_refuses_output_inside_source_repository(self) -> None:
        module = _load_module()
        with WorkspaceTemporaryDirectory(prefix="reproduction-inside-") as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            output = source / "package"
            with self.assertRaisesRegex(
                module.ReproductionPackageError,
                "outside the source repository",
            ):
                module.build_reproduction_package(
                    output,
                    repository_root=source,
                )
            self.assertFalse(output.exists())

    def test_refuses_nonempty_output_parent(self) -> None:
        module = _load_module()
        with WorkspaceTemporaryDirectory(prefix="reproduction-parent-") as temporary:
            source, output = _paths(Path(temporary))
            (output.parent / "unexpected.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(
                module.ReproductionPackageError,
                "new, empty, caller-private",
            ):
                module.build_reproduction_package(
                    output,
                    repository_root=source,
                )
            self.assertFalse(output.exists())

    def test_invariant_failure_does_not_create_output(self) -> None:
        module = _load_module()
        commit = "c" * 40
        invalid = _pipeline(commit)
        invalid["comparison"]["avoided_subprocess_calls"] = 4
        with WorkspaceTemporaryDirectory(prefix="reproduction-invalid-") as temporary:
            source, output = _paths(Path(temporary))
            with mock.patch.object(
                module,
                "benchmark_provenance",
                return_value=_provenance(commit, clean=True),
            ):
                with self.assertRaisesRegex(
                    module.ReproductionPackageError,
                    "assertions failed",
                ):
                    module.build_reproduction_package(
                        output,
                        repository_root=source,
                        focused_runner=lambda: _focused(commit),
                        pipeline_runner=lambda: invalid,
                    )
            self.assertFalse(output.exists())

    def test_expected_engine_error_is_bounded_and_does_not_create_output(self) -> None:
        module = _load_module()
        from benchhandoff.errors import EvidenceError

        commit = "d" * 40
        with WorkspaceTemporaryDirectory(prefix="reproduction-engine-error-") as temporary:
            source, output = _paths(Path(temporary))

            def fail_with_sensitive_path() -> dict[str, object]:
                raise EvidenceError(r"failed at C:\Users\private\run")

            with mock.patch.object(
                module,
                "benchmark_provenance",
                return_value=_provenance(commit, clean=True),
            ):
                with self.assertRaisesRegex(
                    module.ReproductionPackageError,
                    r"generation failed \(EvidenceError\)",
                ) as raised:
                    module.build_reproduction_package(
                        output,
                        repository_root=source,
                        focused_runner=fail_with_sensitive_path,
                        pipeline_runner=lambda: _pipeline(commit),
                    )
            self.assertNotIn("private", str(raised.exception))
            self.assertFalse(output.exists())

    def test_write_failure_cannot_create_completion_marker(self) -> None:
        module = _load_module()
        commit = "e" * 40
        with WorkspaceTemporaryDirectory(prefix="reproduction-write-failure-") as temporary:
            source, output = _paths(Path(temporary))
            original_write = module._write_new
            calls = 0

            def fail_second_write(path: Path, value: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic write failure")
                original_write(path, value)

            with (
                mock.patch.object(
                    module,
                    "benchmark_provenance",
                    return_value=_provenance(commit, clean=True),
                ),
                mock.patch.object(module, "_write_new", side_effect=fail_second_write),
            ):
                with self.assertRaisesRegex(
                    module.ReproductionPackageError,
                    "write failed closed",
                ):
                    module.build_reproduction_package(
                        output,
                        repository_root=source,
                        focused_runner=lambda: _focused(commit),
                        pipeline_runner=lambda: _pipeline(commit),
                    )
            self.assertTrue(output.exists())
            self.assertFalse((output / "PACKAGE_COMPLETE.json").exists())

    def test_main_emits_bounded_json_without_traceback(self) -> None:
        module = _load_module()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                module,
                "build_reproduction_package",
                side_effect=module.ReproductionPackageError("bounded failure"),
            ),
            redirect_stderr(stderr),
        ):
            code = module.main(["--output-dir", "unused"])
        self.assertEqual(code, 30)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["detail"], "bounded failure")
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
