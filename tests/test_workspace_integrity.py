from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.engine as engine
import benchhandoff.workspace as workspace_module
from benchhandoff.engine import (
    inspect_resume,
    inspect_workspace_suite,
    resume_run,
    start_run,
    verify_run,
)
from benchhandoff.errors import BoundaryError, EvidenceError
from benchhandoff.storage import canonical_json_bytes, file_identity, read_json_file
from benchhandoff.workspace import (
    WORKSPACE_MANIFEST_KIND,
    WORKSPACE_POLICY,
    load_workspace_manifest,
    snapshot_workspace,
)
from benchhandoff.writer_lock import WriterLock
from tests.workspace_temp import WorkspaceTemporaryDirectory

_CONTEXT_BYTES = b"synthetic closed-world execution context\n"


def _toml(value: str) -> str:
    return json.dumps(value)


def _write_v3_suite(root: Path, *, program: str) -> tuple[Path, Path, Path]:
    suite_root = root / "suite"
    workspace = suite_root / "workspace"
    results = workspace / "results"
    results.mkdir(parents=True)
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
                'name = "closed-world-test"',
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
    return suite, workspace, manifest


def _write_v3_two_task_suite(root: Path) -> tuple[Path, Path]:
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
    first_program = (
        "from pathlib import Path; "
        "Path('results/first.txt').write_text("
        "Path('input.txt').read_text(encoding='utf-8').upper(), encoding='utf-8')"
    )
    second_program = (
        "from pathlib import Path; "
        "Path('results/second.txt').write_text("
        "Path('results/first.txt').read_text(encoding='utf-8') + 'done', "
        "encoding='utf-8')"
    )
    suite = suite_root / "suite.toml"
    suite.write_text(
        "\n".join(
            [
                "version = 3",
                'name = "closed-world-two-task-test"',
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
                'id = "first"',
                "argv = ["
                + ", ".join(_toml(value) for value in (executable, "-c", first_program))
                + "]",
                'inputs = ["context.json", "input.txt"]',
                'outputs = ["results/first.txt"]',
                "",
                "[[task]]",
                'id = "second"',
                "argv = ["
                + ", ".join(_toml(value) for value in (executable, "-c", second_program))
                + "]",
                'inputs = ["context.json", "results/first.txt"]',
                'outputs = ["results/second.txt"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return suite, workspace


class WorkspaceIntegrityTests(unittest.TestCase):
    def test_snapshot_is_exclusive_and_inspection_is_path_redacted(self) -> None:
        program = (
            "from pathlib import Path; "
            "Path('results/result.txt').write_text('ok', encoding='utf-8')"
        )
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            suite, _workspace, manifest = _write_v3_suite(root, program=program)

            original = manifest.read_bytes()
            snapshot_result = snapshot_workspace(
                root / "suite" / "workspace",
                root / "candidate.snapshot.json",
            )
            self.assertNotIn(str(root), json.dumps(snapshot_result, sort_keys=True))
            with self.assertRaisesRegex(BoundaryError, "must start absent"):
                snapshot_workspace(root / "suite" / "workspace", manifest)
            self.assertEqual(manifest.read_bytes(), original)

            inspection = inspect_workspace_suite(suite)
            self.assertEqual(inspection["status"], "matched")
            self.assertEqual(inspection["schema_version"], 3)
            self.assertEqual(inspection["tasks"], 1)
            self.assertEqual(inspection["declared_outputs"], 1)
            self.assertNotIn(str(root), json.dumps(inspection, sort_keys=True))

    def test_checked_in_closed_world_example_matches_its_manifest(self) -> None:
        suite = (
            REPOSITORY_ROOT
            / "examples"
            / "closed_world_workspace"
            / "suite.toml"
        )
        inspection = inspect_workspace_suite(suite)
        self.assertEqual(inspection["status"], "matched")
        self.assertEqual(inspection["schema_version"], 3)
        self.assertEqual(inspection["workspace"]["file_count"], 3)

    def test_prelaunch_workspace_drift_blocks_before_run_creation(self) -> None:
        program = (
            "from pathlib import Path; "
            "Path('results/result.txt').write_text('ok', encoding='utf-8')"
        )
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            suite, workspace, _manifest = _write_v3_suite(root, program=program)
            (workspace / "input.txt").write_text("drift\n", encoding="utf-8")
            run = root / "run"

            with self.assertRaisesRegex(EvidenceError, "exactly match"):
                start_run(suite, run)
            self.assertFalse(run.exists())

    def test_completed_run_seals_and_reverifies_workspace(self) -> None:
        program = (
            "from pathlib import Path; "
            "Path('results/result.txt').write_text('ok', encoding='utf-8')"
        )
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            suite, workspace, _manifest = _write_v3_suite(root, program=program)
            run = root / "run"

            result = start_run(suite, run)
            self.assertEqual(result.status, "completed")
            self.assertEqual((workspace / "results" / "result.txt").read_text(), "ok")
            verification = verify_run(run)
            self.assertEqual(verification["status"], "verified")
            bundle = read_json_file(run / "bundle.json", label="bundle")
            self.assertEqual(bundle["workspace"]["policy"], WORKSPACE_POLICY)
            self.assertEqual(bundle["final_workspace"]["file_count"], 3)

    def test_two_task_pipeline_derives_workspace_history_across_outputs(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            suite, workspace = _write_v3_two_task_suite(root)
            run = root / "run"

            self.assertEqual(start_run(suite, run).status, "completed")
            self.assertEqual(
                (workspace / "results" / "second.txt").read_text(encoding="utf-8"),
                "SEED\ndone",
            )
            self.assertEqual(verify_run(run)["status"], "verified")

            state = read_json_file(run / "state.json", label="state")
            first = state["tasks"]["first"]["attempts"][0]
            second = state["tasks"]["second"]["attempts"][0]
            self.assertEqual(first["workspace_after"], second["workspace_before"])
            self.assertEqual(first["workspace_after"]["file_count"], 3)
            self.assertEqual(second["workspace_after"]["file_count"], 4)

    def test_undeclared_file_fails_and_persists_stable_observation(self) -> None:
        program = (
            "from pathlib import Path; "
            "Path('results/result.txt').write_text('ok', encoding='utf-8'); "
            "Path('scratch.tmp').write_text('undeclared', encoding='utf-8')"
        )
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            suite, _workspace, _manifest = _write_v3_suite(root, program=program)
            run = root / "run"

            result = start_run(suite, run)
            self.assertEqual(result.status, "failed")
            state = read_json_file(run / "state.json", label="state")
            attempt = state["tasks"]["one"]["attempts"][0]
            self.assertEqual(attempt["status"], "failed")
            self.assertIn("workspace_after", attempt)
            self.assertEqual(attempt["workspace_after"]["file_count"], 4)
            self.assertIn("extra_count=1", attempt["error"])
            self.assertFalse((run / "bundle.json").exists())

    def test_many_undeclared_files_produce_bounded_durable_failure(self) -> None:
        program = (
            "from pathlib import Path; "
            "Path('results/result.txt').write_text('ok', encoding='utf-8'); "
            "[Path(f'undeclared-{i:04d}.tmp').write_text('x', encoding='utf-8') "
            "for i in range(500)]"
        )
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            suite, _workspace, _manifest = _write_v3_suite(root, program=program)
            run = root / "run"

            result = start_run(suite, run)
            self.assertEqual(result.status, "failed")
            state = read_json_file(run / "state.json", label="state")
            attempt = state["tasks"]["one"]["attempts"][0]
            self.assertEqual(attempt["status"], "failed")
            self.assertLessEqual(len(attempt["error"]), 8192)
            self.assertIn("extra_count=500", attempt["error"])
            self.assertEqual(attempt["workspace_after"]["file_count"], 503)
            self.assertFalse((run / "bundle.json").exists())

    def test_resume_refuses_new_workspace_drift_without_mutation(self) -> None:
        program = (
            "from pathlib import Path; import sys; "
            "Path('results/result.txt').write_text('partial', encoding='utf-8'); "
            "sys.exit(7)"
        )
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            suite, workspace, _manifest = _write_v3_suite(root, program=program)
            run = root / "run"

            self.assertEqual(start_run(suite, run).status, "failed")
            (workspace / "unexpected.txt").write_text("drift", encoding="utf-8")
            state_before = (run / "state.json").read_bytes()
            events_before = (run / "events.jsonl").read_bytes()

            with self.assertRaisesRegex(EvidenceError, "workspace topology drifted"):
                inspect_resume(run)
            with self.assertRaisesRegex(EvidenceError, "workspace topology drifted"):
                resume_run(run)
            self.assertEqual((run / "state.json").read_bytes(), state_before)
            self.assertEqual((run / "events.jsonl").read_bytes(), events_before)

    def test_workspace_recovered_requires_a_recovered_failed_attempt(self) -> None:
        success_program = (
            "from pathlib import Path; "
            "Path('results/result.txt').write_text('ok', encoding='utf-8')"
        )
        failure_program = "import sys; sys.exit(7)"
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            success_suite, _workspace, _manifest = _write_v3_suite(
                root / "success",
                program=success_program,
            )
            success_run = root / "success-run"
            self.assertEqual(start_run(success_suite, success_run).status, "completed")
            success_plan = read_json_file(success_run / "plan.json", label="plan")
            success_state = read_json_file(success_run / "state.json", label="state")
            completed = copy.deepcopy(success_state)
            completed_attempt = completed["tasks"]["one"]["attempts"][0]
            completed_attempt["workspace_recovered"] = completed_attempt["workspace_after"]
            with self.assertRaisesRegex(EvidenceError, "cannot have workspace_recovered"):
                engine._validate_state_shape(completed, success_plan)

            failure_suite, _workspace, _manifest = _write_v3_suite(
                root / "failure",
                program=failure_program,
            )
            failure_run = root / "failure-run"
            self.assertEqual(start_run(failure_suite, failure_run).status, "failed")
            failure_plan = read_json_file(failure_run / "plan.json", label="plan")
            failure_state = read_json_file(failure_run / "state.json", label="state")
            terminal_without_observation = copy.deepcopy(failure_state)
            del terminal_without_observation["tasks"]["one"]["attempts"][0][
                "workspace_after"
            ]
            with self.assertRaisesRegex(EvidenceError, "lacks workspace_after"):
                engine._validate_state_shape(
                    terminal_without_observation, failure_plan
                )
            failed = copy.deepcopy(failure_state)
            failed_attempt = failed["tasks"]["one"]["attempts"][0]
            failed_attempt["workspace_recovered"] = failed_attempt["workspace_after"]
            with self.assertRaisesRegex(
                EvidenceError,
                "workspace_recovered requires quarantined_outputs",
            ):
                engine._validate_state_shape(failed, failure_plan)

            recovered_without_summary = copy.deepcopy(failure_state)
            recovered_attempt = recovered_without_summary["tasks"]["one"]["attempts"][0]
            recovered_attempt["quarantined_outputs"] = []
            recovered_without_summary["tasks"]["one"]["status"] = "pending"
            recovered_without_summary["status"] = "running"
            recovered_without_summary["last_error"] = None
            with self.assertRaisesRegex(EvidenceError, "lacks workspace_recovered"):
                engine._validate_state_shape(recovered_without_summary, failure_plan)

    def test_declared_partial_output_mutation_blocks_resume_without_mutation(self) -> None:
        program = (
            "from pathlib import Path; import sys; "
            "Path('results/result.txt').write_text('partial', encoding='utf-8'); "
            "sys.exit(7)"
        )
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            suite, workspace, _manifest = _write_v3_suite(root, program=program)
            run = root / "run"

            self.assertEqual(start_run(suite, run).status, "failed")
            (workspace / "results" / "result.txt").write_text(
                "changed after failure",
                encoding="utf-8",
            )
            state_before = (run / "state.json").read_bytes()
            events_before = (run / "events.jsonl").read_bytes()

            with self.assertRaisesRegex(
                EvidenceError,
                "workspace observation changed since the recorded attempt",
            ):
                inspect_resume(run)
            with self.assertRaisesRegex(
                EvidenceError,
                "workspace observation changed since the recorded attempt",
            ):
                resume_run(run)
            self.assertEqual((run / "state.json").read_bytes(), state_before)
            self.assertEqual((run / "events.jsonl").read_bytes(), events_before)

    def test_failed_output_is_quarantined_before_bound_retry_success(self) -> None:
        program = (
            "from pathlib import Path; import os, sys; "
            "attempt = int(os.environ['BENCHHANDOFF_ATTEMPT']); "
            "Path('results/result.txt').write_text("
            "'partial' if attempt == 1 else 'final', encoding='utf-8'); "
            "sys.exit(7 if attempt == 1 else 0)"
        )
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            suite, workspace, _manifest = _write_v3_suite(root, program=program)
            run = root / "run"

            self.assertEqual(start_run(suite, run).status, "failed")
            decision = inspect_resume(run)
            result = resume_run(
                run,
                expected_decision_sha256=decision["decision_sha256"],
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(
                (workspace / "results" / "result.txt").read_text(encoding="utf-8"),
                "final",
            )
            self.assertEqual(verify_run(run)["status"], "verified")

            state = read_json_file(run / "state.json", label="state")
            attempts = state["tasks"]["one"]["attempts"]
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0]["status"], "failed")
            self.assertEqual(len(attempts[0]["quarantined_outputs"]), 1)
            self.assertIn("workspace_recovered", attempts[0])
            self.assertEqual(attempts[1]["status"], "completed")
            quarantine_record = attempts[0]["quarantined_outputs"][0]
            quarantined = run / Path(quarantine_record["artifact"])
            self.assertEqual(quarantined.read_text(encoding="utf-8"), "partial")

            context = engine._load_context(run)
            context.state["tasks"]["one"]["attempts"][0]["workspace_recovered"][
                "tree_sha256"
            ] = "0" * 64
            with self.assertRaisesRegex(
                EvidenceError,
                "workspace_recovered is not derivable",
            ):
                engine._validate_workspace_history(context)

    def test_partial_quarantine_crash_replays_without_losing_evidence(self) -> None:
        program = (
            "from pathlib import Path; import os, sys; "
            "attempt = int(os.environ['BENCHHANDOFF_ATTEMPT']); "
            "suffix = 'partial' if attempt == 1 else 'final'; "
            "Path('results/result.txt').write_text('one-' + suffix, encoding='utf-8'); "
            "Path('results/second.txt').write_text('two-' + suffix, encoding='utf-8'); "
            "sys.exit(9 if attempt == 1 else 0)"
        )
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            suite, workspace, _manifest = _write_v3_suite(root, program=program)
            suite.write_text(
                suite.read_text(encoding="utf-8").replace(
                    'outputs = ["results/result.txt"]',
                    'outputs = ["results/result.txt", "results/second.txt"]',
                ),
                encoding="utf-8",
            )
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "failed")

            real_move = engine.move_regular_same_filesystem
            moves = 0

            def move_then_crash(*args: object, **kwargs: object) -> dict[str, object]:
                nonlocal moves
                identity = real_move(*args, **kwargs)
                moves += 1
                if moves == 1:
                    raise OSError("synthetic crash after first quarantine move")
                return identity

            with (
                mock.patch.object(
                    engine,
                    "move_regular_same_filesystem",
                    side_effect=move_then_crash,
                ),
                self.assertRaisesRegex(EvidenceError, "synthetic crash"),
            ):
                resume_run(run)

            crashed = read_json_file(run / "state.json", label="crashed state")
            first_attempt = crashed["tasks"]["one"]["attempts"][0]
            self.assertNotIn("quarantined_outputs", first_attempt)
            self.assertFalse((workspace / "results" / "result.txt").exists())
            self.assertTrue((workspace / "results" / "second.txt").is_file())
            self.assertEqual(len(list((run / "quarantine").iterdir())), 1)

            decision = inspect_resume(run)
            resumed = resume_run(
                run,
                expected_decision_sha256=decision["decision_sha256"],
            )
            self.assertEqual(resumed.status, "completed")
            self.assertEqual(verify_run(run)["status"], "verified")
            self.assertEqual(
                (workspace / "results" / "result.txt").read_text(encoding="utf-8"),
                "one-final",
            )
            self.assertEqual(
                (workspace / "results" / "second.txt").read_text(encoding="utf-8"),
                "two-final",
            )
            recovered = read_json_file(run / "state.json", label="recovered state")
            first_attempt = recovered["tasks"]["one"]["attempts"][0]
            self.assertEqual(len(first_attempt["quarantined_outputs"]), 2)
            self.assertEqual(len(list((run / "quarantine").iterdir())), 2)

    def test_unobservable_post_exit_tree_keeps_recoverable_running_state(self) -> None:
        program = (
            "from pathlib import Path; import os; "
            "attempt = int(os.environ['BENCHHANDOFF_ATTEMPT']); "
            "os.link('input.txt', 'unexpected-link.txt') if attempt == 1 else "
            "Path('results/result.txt').write_text('ok', encoding='utf-8')"
        )
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            suite, workspace, _manifest = _write_v3_suite(root, program=program)
            run = root / "run"

            try:
                with self.assertRaisesRegex(
                    EvidenceError,
                    "post-exit workspace observation is unavailable",
                ):
                    start_run(suite, run)
            except OSError as exc:
                self.skipTest(f"hard links unavailable in this test environment: {exc}")

            state = read_json_file(run / "state.json", label="running state")
            task_state = state["tasks"]["one"]
            self.assertEqual(state["status"], "running")
            self.assertEqual(task_state["status"], "running")
            self.assertEqual(task_state["attempts"][0]["status"], "running")
            self.assertNotIn("workspace_after", task_state["attempts"][0])

            unexpected = workspace / "unexpected-link.txt"
            self.assertTrue(unexpected.exists())
            unexpected.unlink()
            decision = inspect_resume(run)
            resumed = resume_run(
                run,
                expected_decision_sha256=decision["decision_sha256"],
            )
            self.assertEqual(resumed.status, "completed")
            self.assertEqual(verify_run(run)["status"], "verified")
            recovered = read_json_file(run / "state.json", label="recovered state")
            attempts = recovered["tasks"]["one"]["attempts"]
            self.assertEqual(attempts[0]["status"], "interrupted")
            self.assertIn("workspace_after", attempts[0])
            self.assertIn("workspace_recovered", attempts[0])
            self.assertEqual(attempts[1]["status"], "completed")
    def test_manifest_schema_rejects_boolean_and_float_versions(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            for schema_version in (True, 1.0):
                with self.subTest(schema_version=schema_version):
                    payload = {
                        "schema_version": schema_version,
                        "kind": WORKSPACE_MANIFEST_KIND,
                        "policy": WORKSPACE_POLICY,
                        "entries": [],
                    }
                    manifest = root / "manifest.json"
                    manifest.write_bytes(canonical_json_bytes(payload))
                    with self.assertRaisesRegex(
                        EvidenceError,
                        "schema_version is unsupported",
                    ):
                        load_workspace_manifest(manifest)
                    manifest.unlink()

    def test_entry_cap_stops_directory_iteration_at_ten_thousand_one(self) -> None:
        class FakeScan:
            def __init__(self, root: Path) -> None:
                self.root = root
                self.consumed = 0

            def __enter__(self) -> FakeScan:
                return self

            def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
                return None

            def __iter__(self):
                for index in range(20_000):
                    self.consumed += 1
                    yield SimpleNamespace(
                        name=f"entry-{index:05d}",
                        path=str(self.root / f"entry-{index:05d}"),
                    )

        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            metadata = root.lstat()
            fake = FakeScan(root)
            with (
                mock.patch.object(workspace_module.os, "scandir", return_value=fake),
                self.assertRaisesRegex(EvidenceError, "10000-entry limit"),
            ):
                workspace_module._scan_workspace_once(
                    root,
                    root_device=metadata.st_dev,
                    root_key=(metadata.st_dev, metadata.st_ino),
                )
            self.assertEqual(fake.consumed, 10_001)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support unavailable")
    def test_linked_workspace_entry_and_root_are_rejected(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            target = root / "target.txt"
            target.write_text("outside", encoding="utf-8")
            linked_entry = workspace / "linked.txt"
            try:
                os.symlink(target, linked_entry)
            except OSError as exc:
                self.skipTest(f"symlink creation not permitted: {exc}")

            with self.assertRaisesRegex(EvidenceError, "link or reparse point"):
                snapshot_workspace(workspace, root / "entry-manifest.json")

            linked_entry.unlink()
            real_workspace = root / "real-workspace"
            real_workspace.mkdir()
            linked_root = root / "linked-workspace"
            try:
                os.symlink(real_workspace, linked_root, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink creation not permitted: {exc}")

            with self.assertRaisesRegex(BoundaryError, "link or reparse point"):
                snapshot_workspace(linked_root, root / "root-manifest.json")

    def test_hard_link_is_rejected_from_snapshot(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-workspace-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            original = workspace / "input.txt"
            original.write_text("seed", encoding="utf-8")
            try:
                os.link(original, workspace / "alias.txt")
            except OSError as exc:
                self.skipTest(f"hard links unavailable in this test environment: {exc}")

            with self.assertRaisesRegex(EvidenceError, "hard-linked"):
                snapshot_workspace(workspace, root / "manifest.json")


if __name__ == "__main__":
    unittest.main()
