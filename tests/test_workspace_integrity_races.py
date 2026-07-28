from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.workspace as workspace_module
from benchhandoff.engine import start_run, verify_run
from benchhandoff.errors import BoundaryError, EvidenceError
from benchhandoff.storage import file_identity
from benchhandoff.workspace import (
    WORKSPACE_POLICY,
    load_workspace_manifest,
    snapshot_workspace,
)
from tests.workspace_temp import WorkspaceTemporaryDirectory

_CONTEXT_BYTES = b"synthetic closed-world execution context\n"


def _toml(value: str) -> str:
    return json.dumps(value)


def _write_v3_suite(root: Path, *, program: str) -> tuple[Path, Path]:
    suite_root = root / "suite"
    workspace = suite_root / "workspace"
    (workspace / "results").mkdir(parents=True)
    (workspace / "context.json").write_bytes(_CONTEXT_BYTES)
    (workspace / "input.txt").write_text("seed\n", encoding="utf-8")

    manifest = suite_root / "workspace.snapshot.json"
    snapshot_workspace(workspace, manifest)
    manifest_identity = file_identity(manifest, label="test workspace manifest")
    context_sha256 = hashlib.sha256(_CONTEXT_BYTES).hexdigest()
    executable = Path(sys.executable).name
    suite = suite_root / "suite.toml"
    suite.write_text(
        "\n".join(
            [
                "version = 3",
                'name = "closed-world-race-test"',
                "",
                "[context]",
                'path = "context.json"',
                'media_type = "application/vnd.benchhandoff.test-context+json"',
                f'digest = "sha256:{context_sha256}"',
                f"size = {len(_CONTEXT_BYTES)}",
                "",
                "[workspace]",
                'root = "workspace"',
                'manifest = "workspace.snapshot.json"',
                f'digest = "sha256:{manifest_identity["sha256"]}"',
                f'size = {manifest_identity["size"]}',
                f"policy = {_toml(WORKSPACE_POLICY)}",
                "",
                "[[task]]",
                'id = "one"',
                "argv = ["
                + ", ".join(_toml(value) for value in (executable, "-c", program))
                + "]",
                'inputs = ["context.json", "input.txt"]',
                'outputs = ["results/result.txt"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return suite, workspace


class WorkspaceIntegrityRaceTests(unittest.TestCase):
    def test_concurrent_snapshot_to_same_manifest_has_one_winner(self) -> None:
        with WorkspaceTemporaryDirectory(
            prefix="benchhandoff-workspace-race-"
        ) as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "input.txt").write_text("seed\n", encoding="utf-8")
            manifest = root / "workspace.snapshot.json"
            barrier = threading.Barrier(2)

            def publish() -> tuple[str, object]:
                barrier.wait(timeout=10)
                try:
                    return "created", snapshot_workspace(workspace, manifest)
                except Exception as exc:  # The losing publication is the assertion.
                    return "error", exc

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(lambda _index: publish(), range(2)))

            created = [value for status, value in outcomes if status == "created"]
            errors = [value for status, value in outcomes if status == "error"]
            self.assertEqual(len(created), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], BoundaryError)
            self.assertIn("must start absent", str(errors[0]))
            loaded = load_workspace_manifest(manifest)
            self.assertEqual(loaded.summary, created[0]["workspace"])

    def test_snapshot_rejects_same_byte_object_replacement_before_readback(
        self,
    ) -> None:
        with WorkspaceTemporaryDirectory(
            prefix="benchhandoff-workspace-readback-"
        ) as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "input.txt").write_text("seed\n", encoding="utf-8")
            manifest = root / "workspace.snapshot.json"
            displaced = root / "workspace.snapshot.original"
            real_write_new_bytes = workspace_module._write_new_bytes

            def replace_after_publish(
                path: Path,
                payload: bytes,
            ) -> tuple[dict[str, object], tuple[int, int]]:
                expected = real_write_new_bytes(path, payload)
                path.replace(displaced)
                path.write_bytes(payload)
                return expected

            with (
                mock.patch.object(
                    workspace_module,
                    "_write_new_bytes",
                    side_effect=replace_after_publish,
                ),
                self.assertRaisesRegex(
                    EvidenceError,
                    "changed object identity during publication",
                ),
            ):
                snapshot_workspace(workspace, manifest)

            self.assertEqual(manifest.read_bytes(), displaced.read_bytes())

    def test_failed_snapshot_write_leaves_candidate_instead_of_deleting(self) -> None:
        with WorkspaceTemporaryDirectory(
            prefix="benchhandoff-workspace-write-failure-"
        ) as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "input.txt").write_text("seed\n", encoding="utf-8")
            manifest = root / "workspace.snapshot.json"

            with (
                mock.patch.object(
                    workspace_module.os,
                    "write",
                    side_effect=OSError("synthetic write failure"),
                ),
                self.assertRaisesRegex(OSError, "synthetic write failure"),
            ):
                snapshot_workspace(workspace, manifest)

            self.assertTrue(manifest.is_file())
            candidate = manifest.read_bytes()
            with self.assertRaisesRegex(BoundaryError, "must start absent"):
                snapshot_workspace(workspace, manifest)
            self.assertEqual(manifest.read_bytes(), candidate)

    def test_workspace_lock_blocks_start_in_a_different_run_directory(self) -> None:
        program = (
            "from pathlib import Path; import time; "
            "Path('results/result.txt').write_text('ok', encoding='utf-8'); "
            "time.sleep(3)"
        )
        with WorkspaceTemporaryDirectory(
            prefix="benchhandoff-workspace-lock-"
        ) as temporary:
            root = Path(temporary)
            suite, workspace = _write_v3_suite(root, program=program)
            first_run = root / "run-a"
            second_run = root / "run-b"

            with ThreadPoolExecutor(max_workers=1) as executor:
                first = executor.submit(start_run, suite, first_run)
                deadline = time.monotonic() + 10
                output = workspace / "results" / "result.txt"
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(output.exists(), "first run did not reach its worker")
                with self.assertRaisesRegex(
                    EvidenceError,
                    "writer lock already exists",
                ):
                    start_run(suite, second_run)
                first_result = first.result(timeout=15)

            self.assertEqual(first_result.status, "completed")
            self.assertFalse(second_run.exists())

    def test_workspace_drift_after_bundle_fails_without_rewriting_bundle(self) -> None:
        program = (
            "from pathlib import Path; "
            "Path('results/result.txt').write_text('ok', encoding='utf-8')"
        )
        with WorkspaceTemporaryDirectory(
            prefix="benchhandoff-workspace-bundle-"
        ) as temporary:
            root = Path(temporary)
            suite, workspace = _write_v3_suite(root, program=program)
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "completed")
            bundle = run / "bundle.json"
            bundle_before = bundle.read_bytes()

            (workspace / "unexpected.txt").write_text("drift\n", encoding="utf-8")
            with self.assertRaisesRegex(EvidenceError, "workspace"):
                verify_run(run)

            self.assertEqual(bundle.read_bytes(), bundle_before)


if __name__ == "__main__":
    unittest.main()
