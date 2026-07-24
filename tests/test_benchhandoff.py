from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.engine as engine
import benchhandoff.model as model
from benchhandoff.cli import main as cli_main
from benchhandoff.engine import resume_run, start_run, verify_run
from benchhandoff.errors import BoundaryError, ConfigurationError, EvidenceError
from benchhandoff.model import load_suite
from benchhandoff.storage import atomic_write_json, read_json_file
from tests.workspace_temp import WorkspaceTemporaryDirectory


def toml_string(value: str) -> str:
    return json.dumps(value)


def write_suite(
    root: Path,
    worker_source: str,
    *,
    inputs: tuple[str, ...] = ("worker.py", "input.txt"),
    outputs: tuple[str, ...] = ("result.txt",),
    extra_root: str = "",
) -> Path:
    root.mkdir()
    (root / "worker.py").write_text(worker_source, encoding="utf-8")
    (root / "input.txt").write_text("payload\n", encoding="utf-8")
    argv = [sys.executable, "worker.py", "input.txt", outputs[0]]
    suite = "\n".join(
        [
            "version = 1",
            'name = "test-suite"',
            extra_root,
            "",
            "[[task]]",
            'id = "one"',
            "argv = [" + ", ".join(toml_string(value) for value in argv) + "]",
            "inputs = [" + ", ".join(toml_string(value) for value in inputs) + "]",
            "outputs = [" + ", ".join(toml_string(value) for value in outputs) + "]",
            "",
        ]
    )
    path = root / "suite.toml"
    path.write_text(suite, encoding="utf-8")
    return path


SUCCESS_WORKER = """\
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
destination.write_bytes(source.read_bytes().upper())
"""


FAIL_ONCE_WORKER = """\
import os
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
attempt = int(os.environ["BENCHHANDOFF_ATTEMPT"])
if attempt == 1:
    destination.write_text("partial", encoding="utf-8")
    raise SystemExit(75)
destination.write_bytes(source.read_bytes().upper())
"""


class BenchHandoffTests(unittest.TestCase):
    def temporary_root(self) -> WorkspaceTemporaryDirectory:
        return WorkspaceTemporaryDirectory(prefix="benchhandoff-test-")

    def test_suite_resource_caps_block_before_run_creation(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", SUCCESS_WORKER)
            oversized_run = root / "oversized-run"
            with mock.patch.object(model, "MAX_SUITE_BYTES", 8):
                with self.assertRaises(EvidenceError):
                    start_run(suite, oversized_run)
            self.assertFalse(oversized_run.exists())

            argument_run = root / "argument-run"
            with mock.patch.object(model, "MAX_TASK_ARGUMENTS", 1):
                with self.assertRaisesRegex(ConfigurationError, "argument limit"):
                    start_run(suite, argument_run)
            self.assertFalse(argument_run.exists())

            path_reference_run = root / "path-reference-run"
            with mock.patch.object(model, "MAX_SUITE_PATH_REFERENCES", 1):
                with self.assertRaisesRegex(ConfigurationError, "path reference limit"):
                    start_run(suite, path_reference_run)
            self.assertFalse(path_reference_run.exists())

    def test_ancestor_file_paths_are_rejected_before_any_child(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite_root = root / "suite"
            suite_root.mkdir()
            marker = root / "child-ran.txt"
            child_code = (
                "from pathlib import Path; "
                f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')"
            )
            lines = [
                "version = 1",
                "name = \"ancestor-conflict\"",
                "",
            ]
            for task_id, output in (("one", "a"), ("two", "a/b")):
                argv = [sys.executable, "-c", child_code]
                lines.extend(
                    [
                        "[[task]]",
                        f"id = {toml_string(task_id)}",
                        "argv = ["
                        + ", ".join(toml_string(value) for value in argv)
                        + "]",
                        "inputs = []",
                        f"outputs = [{toml_string(output)}]",
                        "",
                    ]
                )
            suite = suite_root / "suite.toml"
            suite.write_text("\n".join(lines), encoding="utf-8")
            run = root / "run"

            with self.assertRaisesRegex(ConfigurationError, "ancestor conflict"):
                start_run(suite, run)
            self.assertFalse(marker.exists())
            self.assertFalse(run.exists())

    def test_repeated_argv_values_are_preserved_for_the_child(self) -> None:
        worker_source = """\
import json
import pathlib
import sys
pathlib.Path(sys.argv[-1]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
"""
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite_root = root / "suite"
            suite_root.mkdir()
            (suite_root / "worker.py").write_text(worker_source, encoding="utf-8")
            suite = suite_root / "suite.toml"
            repeated_argv = (sys.executable, "worker.py", "same", "same", "result.json")
            suite.write_text(
                "\n".join(
                    [
                        "version = 1",
                        'name = "repeated-argv"',
                        "",
                        "[[task]]",
                        'id = "repeat"',
                        "argv = ["
                        + ", ".join(json.dumps(item) for item in repeated_argv)
                        + "]",
                        'inputs = ["worker.py"]',
                        'outputs = ["result.json"]',
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = start_run(suite, root / "run")
            self.assertEqual(result.status, "completed")
            self.assertEqual(
                json.loads((suite_root / "result.json").read_text(encoding="utf-8")),
                ["same", "same", "result.json"],
            )

    def test_successful_start_and_verify(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", SUCCESS_WORKER)
            run = root / "run"

            result = start_run(suite, run)
            self.assertEqual(result.status, "completed")
            verified = verify_run(run)
            self.assertEqual(verified["status"], "verified")
            self.assertEqual((suite.parent / "result.txt").read_text(), "PAYLOAD\n")

            state = read_json_file(run / "state.json", label="state")
            self.assertEqual(state["tasks"]["one"]["status"], "completed")
            self.assertEqual(len(state["tasks"]["one"]["attempts"]), 1)
            self.assertTrue((run / "bundle.json").is_file())

    def test_nonzero_child_fails_closed_then_resume_quarantines(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", FAIL_ONCE_WORKER)
            run = root / "run"

            first = start_run(suite, run)
            self.assertEqual(first.status, "failed")
            self.assertFalse((run / "bundle.json").exists())
            failed_state = read_json_file(run / "state.json", label="failed state")
            self.assertEqual(
                failed_state["tasks"]["one"]["attempts"][0]["return_code"],
                75,
            )

            second = resume_run(run)
            self.assertEqual(second.status, "completed")
            verify_run(run)
            state = read_json_file(run / "state.json", label="resumed state")
            attempts = state["tasks"]["one"]["attempts"]
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0]["quarantined_outputs"][0]["source"], "result.txt")
            self.assertEqual((suite.parent / "result.txt").read_text(), "PAYLOAD\n")
            quarantined = list((run / "quarantine").iterdir())
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_text(), "partial")

    def test_quarantine_move_crash_recovers_from_fresh_disk_state(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", FAIL_ONCE_WORKER)
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "failed")

            real_move = engine.move_regular_same_filesystem

            def move_then_crash(*args: object, **kwargs: object) -> dict[str, object]:
                identity = real_move(*args, **kwargs)
                raise OSError(f"synthetic crash after quarantine move: {identity}")

            with mock.patch.object(
                engine,
                "move_regular_same_filesystem",
                side_effect=move_then_crash,
            ):
                with self.assertRaisesRegex(EvidenceError, "synthetic crash"):
                    resume_run(run)

            crashed_state = read_json_file(run / "state.json", label="crashed state")
            first_attempt = crashed_state["tasks"]["one"]["attempts"][0]
            self.assertNotIn("quarantined_outputs", first_attempt)
            self.assertFalse((suite.parent / "result.txt").exists())
            self.assertEqual(len(list((run / "quarantine").iterdir())), 1)

            self.assertEqual(resume_run(run).status, "completed")
            self.assertEqual(verify_run(run)["status"], "verified")
            recovered = read_json_file(run / "state.json", label="recovered state")
            attempts = recovered["tasks"]["one"]["attempts"]
            self.assertEqual(len(attempts), 2)
            self.assertEqual(len(attempts[0]["quarantined_outputs"]), 1)

    def test_crash_before_task_started_state_reuses_empty_orphan_logs(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", SUCCESS_WORKER)
            run = root / "run"
            real_commit = engine._commit_transition

            def fail_task_started(
                context: engine._RunContext,
                event_type: str,
                **kwargs: object,
            ) -> None:
                if event_type == "task_started":
                    raise OSError("synthetic crash before task_started state")
                real_commit(context, event_type, **kwargs)

            with mock.patch.object(engine, "_commit_transition", side_effect=fail_task_started):
                with self.assertRaisesRegex(
                    EvidenceError,
                    "run could not be safely started.*before task_started",
                ):
                    start_run(suite, run)

            crashed_state = read_json_file(run / "state.json", label="crashed state")
            self.assertEqual(crashed_state["tasks"]["one"]["status"], "pending")
            self.assertEqual(crashed_state["tasks"]["one"]["attempts"], [])
            orphan_logs = list((run / "logs" / "one").iterdir())
            self.assertEqual(len(orphan_logs), 2)
            self.assertTrue(all(path.stat().st_size == 0 for path in orphan_logs))

            self.assertEqual(resume_run(run).status, "completed")
            self.assertEqual(verify_run(run)["status"], "verified")
            recovered = read_json_file(run / "state.json", label="recovered state")
            self.assertEqual(len(recovered["tasks"]["one"]["attempts"]), 1)

    def test_unreferenced_log_blocks_resume_before_state_or_child_mutation(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", FAIL_ONCE_WORKER)
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "failed")
            rogue = run / "logs" / "rogue.log"
            rogue.write_text("unreferenced\n", encoding="utf-8")
            state_before = (run / "state.json").read_bytes()
            events_before = (run / "events.jsonl").read_bytes()

            with self.assertRaisesRegex(EvidenceError, "log files do not exactly match"):
                resume_run(run)

            self.assertEqual((run / "state.json").read_bytes(), state_before)
            self.assertEqual((run / "events.jsonl").read_bytes(), events_before)
            self.assertEqual((suite.parent / "result.txt").read_text(), "partial")

    def test_unexpected_root_entry_blocks_resume_without_mutation(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", FAIL_ONCE_WORKER)
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "failed")
            (run / ".state.json.synthetic.tmp").write_text("orphan", encoding="utf-8")
            state_before = (run / "state.json").read_bytes()
            events_before = (run / "events.jsonl").read_bytes()

            with self.assertRaisesRegex(EvidenceError, "unexpected root entry"):
                resume_run(run)

            self.assertEqual((run / "state.json").read_bytes(), state_before)
            self.assertEqual((run / "events.jsonl").read_bytes(), events_before)
            self.assertEqual((suite.parent / "result.txt").read_text(), "partial")

    def test_unexpected_root_entry_blocks_sealed_verify(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", SUCCESS_WORKER)
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "completed")
            (run / "rogue.txt").write_text("unbound", encoding="utf-8")

            with self.assertRaisesRegex(EvidenceError, "unexpected root entry"):
                verify_run(run)

    def test_resume_attempt_exhaustion_is_repeatable_and_read_only(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", FAIL_ONCE_WORKER)
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "failed")
            state_before = (run / "state.json").read_bytes()
            events_before = (run / "events.jsonl").read_bytes()

            with mock.patch.object(engine, "MAX_ATTEMPTS_PER_TASK", 1):
                for _ in range(2):
                    with self.assertRaisesRegex(EvidenceError, "exhausted.*attempt limit"):
                        resume_run(run)

            self.assertEqual((run / "state.json").read_bytes(), state_before)
            self.assertEqual((run / "events.jsonl").read_bytes(), events_before)
            self.assertEqual((suite.parent / "result.txt").read_text(), "partial")

    def test_completed_prefix_drift_blocks_resume_before_transition_or_child(self) -> None:
        worker_source = """\
import pathlib
import sys

target = pathlib.Path(sys.argv[2])
if sys.argv[1] == "producer":
    target.write_text("stable", encoding="utf-8")
    raise SystemExit(0)
target.write_text("partial", encoding="utf-8")
raise SystemExit(75)
"""
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite_root = root / "suite"
            suite_root.mkdir()
            (suite_root / "worker.py").write_text(worker_source, encoding="utf-8")
            suite = suite_root / "suite.toml"
            suite.write_text(
                "\n".join(
                    [
                        "version = 1",
                        'name = "completed-prefix-drift"',
                        "",
                        "[[task]]",
                        'id = "producer"',
                        "argv = ["
                        + ", ".join(
                            json.dumps(item)
                            for item in (sys.executable, "worker.py", "producer", "first.txt")
                        )
                        + "]",
                        'inputs = ["worker.py"]',
                        'outputs = ["first.txt"]',
                        "",
                        "[[task]]",
                        'id = "consumer"',
                        "argv = ["
                        + ", ".join(
                            json.dumps(item)
                            for item in (sys.executable, "worker.py", "consumer", "result.txt")
                        )
                        + "]",
                        'inputs = ["worker.py", "first.txt"]',
                        'outputs = ["result.txt"]',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "failed")
            (suite_root / "first.txt").write_text("drifted", encoding="utf-8")
            state_before = (run / "state.json").read_bytes()
            events_before = (run / "events.jsonl").read_bytes()

            with mock.patch.object(engine, "_run_task") as child_runner:
                with self.assertRaisesRegex(EvidenceError, "completed output"):
                    resume_run(run)
            child_runner.assert_not_called()
            self.assertEqual((run / "state.json").read_bytes(), state_before)
            self.assertEqual((run / "events.jsonl").read_bytes(), events_before)
            self.assertEqual((suite_root / "result.txt").read_text(), "partial")

    def test_dead_recorded_child_recovers_without_inventing_return_code(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", FAIL_ONCE_WORKER)
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "failed")

            state_path = run / "state.json"
            state = read_json_file(state_path, label="state")
            state["status"] = "running"
            state["last_error"] = None
            task_state = state["tasks"]["one"]
            task_state["status"] = "running"
            first_attempt = task_state["attempts"][0]
            first_attempt["status"] = "running"
            first_attempt["child_pid"] = 424242
            first_attempt["child_start_token"] = "test:recorded"
            first_attempt["child_launch_guard"] = False
            first_attempt["ended_at"] = None
            first_attempt["return_code"] = None
            first_attempt.pop("error", None)
            atomic_write_json(state_path, state)

            with (
                mock.patch.object(
                    engine,
                    "process_start_token",
                    return_value="test:recorded",
                ),
                mock.patch.object(engine, "process_liveness", return_value="dead"),
            ):
                self.assertEqual(resume_run(run).status, "completed")

            self.assertEqual(verify_run(run)["status"], "verified")
            recovered = read_json_file(state_path, label="recovered state")
            first_attempt = recovered["tasks"]["one"]["attempts"][0]
            self.assertEqual(first_attempt["status"], "interrupted")
            self.assertIsNone(first_attempt["return_code"])
            self.assertIn("return_code_unavailable_reason", first_attempt)

    def test_unresolved_child_launch_guard_blocks_resume_without_mutation(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", FAIL_ONCE_WORKER)
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "failed")

            state_path = run / "state.json"
            state = read_json_file(state_path, label="state")
            state["status"] = "running"
            state["last_error"] = None
            state["tasks"]["one"]["status"] = "running"
            first_attempt = state["tasks"]["one"]["attempts"][0]
            first_attempt["status"] = "running"
            first_attempt["child_pid"] = None
            first_attempt["child_start_token"] = None
            first_attempt["child_launch_guard"] = True
            first_attempt["ended_at"] = None
            first_attempt["return_code"] = None
            first_attempt.pop("error", None)
            atomic_write_json(state_path, state)
            state_before = state_path.read_bytes()
            events_before = (run / "events.jsonl").read_bytes()

            with self.assertRaisesRegex(EvidenceError, "unresolved child launch guard"):
                resume_run(run)

            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual((run / "events.jsonl").read_bytes(), events_before)
            self.assertEqual((suite.parent / "result.txt").read_text(), "partial")

    def test_seed_input_drift_blocks_resume_before_second_child(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", FAIL_ONCE_WORKER)
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "failed")
            (suite.parent / "input.txt").write_text("tampered\n", encoding="utf-8")

            with self.assertRaises(EvidenceError):
                resume_run(run)
            state = read_json_file(run / "state.json", label="state")
            self.assertEqual(len(state["tasks"]["one"]["attempts"]), 1)

    def test_verified_output_drift_is_detected(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", SUCCESS_WORKER)
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "completed")
            (suite.parent / "result.txt").write_text("changed\n", encoding="utf-8")

            with self.assertRaises(EvidenceError):
                verify_run(run)
            with self.assertRaises(EvidenceError):
                resume_run(run)

    def test_preexisting_output_blocks_before_any_child(self) -> None:
        worker = """\
import pathlib
pathlib.Path("child-ran.marker").write_text("yes", encoding="utf-8")
"""
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", worker)
            (suite.parent / "result.txt").write_text("stale", encoding="utf-8")
            run = root / "run"

            with self.assertRaises(BoundaryError):
                start_run(suite, run)
            self.assertFalse((suite.parent / "child-ran.marker").exists())
            self.assertFalse(run.exists())

    def test_zero_exit_with_directory_output_fails_without_bundle(self) -> None:
        worker = """\
import pathlib
import sys
pathlib.Path(sys.argv[2]).mkdir()
"""
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", worker)
            run = root / "run"

            result = start_run(suite, run)
            self.assertEqual(result.status, "failed")
            self.assertFalse((run / "bundle.json").exists())
            with self.assertRaises(EvidenceError):
                resume_run(run)

    def test_extra_log_after_bundle_is_detected(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", SUCCESS_WORKER)
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "completed")
            (run / "logs" / "unrecorded.log").write_text("extra", encoding="utf-8")

            with self.assertRaises(EvidenceError):
                verify_run(run)

    def test_suite_tampering_is_detected(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", SUCCESS_WORKER)
            run = root / "run"
            self.assertEqual(start_run(suite, run).status, "completed")
            suite.write_text(suite.read_text(encoding="utf-8") + "\n", encoding="utf-8")

            with self.assertRaises(EvidenceError):
                verify_run(run)

    def test_unknown_suite_key_is_rejected(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", SUCCESS_WORKER, extra_root='mystery = "no"')
            with self.assertRaises(ConfigurationError):
                load_suite(suite)

    def test_forward_dependency_is_rejected_as_seed_overwrite(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite_root = root / "suite"
            suite_root.mkdir()
            suite = suite_root / "suite.toml"
            suite.write_text(
                "\n".join(
                    [
                        "version = 1",
                        'name = "forward"',
                        "",
                        "[[task]]",
                        'id = "consumer"',
                        f"argv = [{toml_string(sys.executable)}, \"-c\", \"pass\"]",
                        'inputs = ["future.txt"]',
                        'outputs = ["first.txt"]',
                        "",
                        "[[task]]",
                        'id = "producer"',
                        f"argv = [{toml_string(sys.executable)}, \"-c\", \"pass\"]",
                        "inputs = []",
                        'outputs = ["future.txt"]',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                load_suite(suite)

    def test_cli_returns_stable_codes(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", FAIL_ONCE_WORKER)
            run = root / "run"
            self.assertEqual(cli_main(["start", str(suite), "--run-dir", str(run)]), 20)
            self.assertEqual(cli_main(["resume", str(run)]), 0)
            self.assertEqual(cli_main(["verify", str(run)]), 0)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support unavailable")
    def test_symlink_seed_is_rejected_when_platform_allows_creation(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", SUCCESS_WORKER)
            real_input = suite.parent / "real-input.txt"
            real_input.write_text("payload\n", encoding="utf-8")
            (suite.parent / "input.txt").unlink()
            try:
                os.symlink(real_input, suite.parent / "input.txt")
            except OSError as exc:
                self.skipTest(f"symlink creation not permitted: {exc}")
            with self.assertRaises(BoundaryError):
                start_run(suite, root / "run")


if __name__ == "__main__":
    unittest.main()
