from __future__ import annotations

import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.cli as cli
import benchhandoff.engine as engine
from benchhandoff.errors import BoundaryError, ConfigurationError, EvidenceError
from benchhandoff.model import load_suite
from benchhandoff.storage import (
    canonical_json_bytes,
    file_identity,
    normalize_relative_file,
    require_same_filesystem,
)
from tests.workspace_temp import WorkspaceTemporaryDirectory

IDENTITY = {"sha256": "a" * 64, "size": 7}
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def evidence_plan(root: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "benchhandoff-plan",
        "run_id": "1" * 32,
        "created_at": "2026-07-24T00:00:00Z",
        "run_directory": str((root / "run").absolute()),
        "suite_file": {
            "path": str((root / "suite.toml").absolute()),
            **IDENTITY,
        },
        "suite_root": str(root.absolute()),
        "suite": {
            "version": 1,
            "name": "schema-test",
            "tasks": [
                {
                    "id": "one",
                    "argv": ["python", "worker.py"],
                    "inputs": ["input.txt"],
                    "outputs": ["result.txt"],
                }
            ],
        },
        "seed_inputs": {"input.txt": dict(IDENTITY)},
        "environment": {
            "python_implementation": "CPython",
            "python_version": "3.12.13",
            "platform": "test",
        },
    }


def completed_state() -> dict[str, object]:
    attempt = {
        "number": 1,
        "status": "completed",
        "started_at": "2026-07-24T00:00:01Z",
        "ended_at": "2026-07-24T00:00:02Z",
        "child_pid": 123,
        "child_start_token": "test:123",
        "child_launch_guard": False,
        "return_code": 0,
        "argv": ["python", "worker.py"],
        "verified_inputs": {"input.txt": dict(IDENTITY)},
        "stdout": "logs/one/attempt-0001.stdout.log",
        "stderr": "logs/one/attempt-0001.stderr.log",
        "verified_outputs": {"result.txt": dict(IDENTITY)},
    }
    return {
        "schema_version": 1,
        "kind": "benchhandoff-state",
        "run_id": "1" * 32,
        "plan_sha256": "b" * 64,
        "status": "completed",
        "created_at": "2026-07-24T00:00:00Z",
        "updated_at": "2026-07-24T00:00:03Z",
        "last_error": None,
        "event_log": {"sha256": EMPTY_SHA256, "size": 0, "count": 0},
        "pending_event": None,
        "tasks": {
            "one": {
                "status": "completed",
                "attempts": [attempt],
                "verified_inputs": {"input.txt": dict(IDENTITY)},
                "verified_outputs": {"result.txt": dict(IDENTITY)},
            }
        },
    }


def evidence_bundle(plan: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "benchhandoff-bundle",
        "run_id": "1" * 32,
        "created_at": "2026-07-24T00:00:04Z",
        "suite_file": copy.deepcopy(plan["suite_file"]),
        "seed_inputs": {"input.txt": dict(IDENTITY)},
        "verified_outputs": [
            {"task_id": "one", "path": "result.txt", **IDENTITY}
        ],
        "run_artifacts": [
            {"path": "state.json", **IDENTITY},
        ],
    }


class AuditRegressionTests(unittest.TestCase):
    def test_nested_evidence_schemas_reject_corruption(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-schema-") as temporary:
            root = Path(temporary)
            plan = evidence_plan(root)
            engine._validate_plan_shape(plan)
            state = completed_state()
            engine._validate_state_shape(state, plan)
            bundle = evidence_bundle(plan)
            engine._validate_bundle_shape(bundle, plan)

            broken_plan = copy.deepcopy(plan)
            broken_plan["suite"]["tasks"][0]["argv"] = "not-an-array"
            with self.assertRaises(EvidenceError):
                engine._validate_plan_shape(broken_plan)

            broken_attempt = copy.deepcopy(state)
            broken_attempt["tasks"]["one"]["attempts"][0]["child_pid"] = "123"
            with self.assertRaises(EvidenceError):
                engine._validate_state_shape(broken_attempt, plan)

            broken_bundle = copy.deepcopy(bundle)
            broken_bundle["run_artifacts"][0]["size"] = "seven"
            with self.assertRaises(EvidenceError):
                engine._validate_bundle_shape(broken_bundle, plan)

    def test_cli_converts_evidence_failure_to_exit_30_without_traceback(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(cli, "verify_run", side_effect=EvidenceError("damaged evidence")),
            redirect_stderr(stderr),
        ):
            self.assertEqual(cli.main(["verify", "run"]), 30)
        self.assertIn('"error_type":"EvidenceError"', stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_start_api_and_cli_normalize_operational_os_errors(self) -> None:
        with mock.patch.object(
            engine,
            "_start_run_checked",
            side_effect=PermissionError("synthetic access denial"),
        ):
            with self.assertRaisesRegex(EvidenceError, "run could not be safely started"):
                engine.start_run("suite.toml", "run")

        stderr = io.StringIO()
        with (
            mock.patch.object(
                cli,
                "start_run",
                side_effect=PermissionError("synthetic access denial"),
            ),
            redirect_stderr(stderr),
        ):
            self.assertEqual(
                cli.main(["start", "suite.toml", "--run-dir", "run"]),
                30,
            )
        self.assertIn('"error_type":"PermissionError"', stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_only_latest_running_child_identity_blocks_resume(self) -> None:
        task = SimpleNamespace(task_id="one")
        failed_context = SimpleNamespace(
            suite=SimpleNamespace(tasks=(task,)),
            state={
                "tasks": {
                    "one": {
                        "status": "failed",
                        "attempts": [
                            {
                                "child_pid": 123,
                                "child_start_token": "test:old",
                                "child_launch_guard": False,
                                "status": "failed",
                                "number": 1,
                                "quarantined_outputs": [],
                            }
                        ],
                    }
                }
            },
        )
        with mock.patch.object(engine, "process_liveness", return_value="alive") as probe:
            engine._assert_recovery_liveness(failed_context)
        probe.assert_not_called()

        running_context = SimpleNamespace(
            suite=SimpleNamespace(tasks=(task,)),
            state={
                "tasks": {
                    "one": {
                        "status": "running",
                        "attempts": [
                            {
                                "child_pid": 123,
                                "child_start_token": "test:same",
                                "child_launch_guard": False,
                                "status": "running",
                                "number": 1,
                            }
                        ],
                    }
                }
            },
        )
        for liveness in ("alive", "unknown"):
            with (
                self.subTest(liveness=liveness),
                mock.patch.object(engine, "process_start_token", return_value="test:same"),
                mock.patch.object(engine, "process_liveness", return_value=liveness),
                self.assertRaises(EvidenceError),
            ):
                engine._assert_recovery_liveness(running_context)
        with (
            mock.patch.object(engine, "process_start_token", return_value="test:same"),
            mock.patch.object(engine, "process_liveness", return_value="dead"),
        ):
            engine._assert_recovery_liveness(running_context)
        with (
            mock.patch.object(engine, "process_start_token", return_value="test:reused"),
            mock.patch.object(engine, "process_liveness", return_value="alive") as probe,
        ):
            engine._assert_recovery_liveness(running_context)
        probe.assert_not_called()
    def test_event_mandatory_integer_fields_reject_null(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-event-types-") as temporary:
            plan = evidence_plan(Path(temporary))
            cases = (
                ("run_started", None, {"suite": "schema-test", "tasks": 1}, "tasks"),
                ("run_completed", None, {"operation": "resume", "tasks": 1}, "tasks"),
                ("task_started", "one", {"attempt": 1}, "attempt"),
                (
                    "task_failed",
                    "one",
                    {"attempt": 1, "return_code": None, "reason": "synthetic"},
                    "attempt",
                ),
                (
                    "task_recovery_prepared",
                    "one",
                    {"previous_status": "failed", "attempt": 1, "quarantined_outputs": 0},
                    "attempt",
                ),
                (
                    "task_recovery_prepared",
                    "one",
                    {"previous_status": "failed", "attempt": 1, "quarantined_outputs": 0},
                    "quarantined_outputs",
                ),
                ("task_completed", "one", {"attempt": 1, "outputs": 1}, "attempt"),
                ("task_completed", "one", {"attempt": 1, "outputs": 1}, "outputs"),
            )
            for event_type, task_id, details, field in cases:
                event = {
                    "schema_version": 1,
                    "time": "2026-07-24T00:00:00Z",
                    "type": event_type,
                    "run_id": plan["run_id"],
                    "sequence": 1,
                    "previous_sha256": EMPTY_SHA256,
                    "details": dict(details),
                }
                if task_id is not None:
                    event["task_id"] = task_id
                event["details"][field] = None
                with (
                    self.subTest(event_type=event_type, field=field),
                    self.assertRaisesRegex(EvidenceError, "must be an integer"),
                ):
                    engine._validate_event_record(
                        event,
                        plan,
                        record_number=1,
                        previous_sha256=EMPTY_SHA256,
                    )

            allowed_null = {
                "schema_version": 1,
                "time": "2026-07-24T00:00:00Z",
                "type": "task_failed",
                "run_id": plan["run_id"],
                "sequence": 1,
                "previous_sha256": EMPTY_SHA256,
                "task_id": "one",
                "details": {"attempt": 1, "return_code": None, "reason": "synthetic"},
            }
            engine._validate_event_record(
                allowed_null,
                plan,
                record_number=1,
                previous_sha256=EMPTY_SHA256,
            )

    def test_events_tampering_breaks_state_binding(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-events-") as temporary:
            root = Path(temporary)
            plan = evidence_plan(root)
            event = {
                "schema_version": 1,
                "time": "2026-07-24T00:00:00Z",
                "type": "run_started",
                "run_id": plan["run_id"],
                "sequence": 1,
                "previous_sha256": EMPTY_SHA256,
                "details": {"suite": "schema-test", "tasks": 1},
            }
            event_path = root / "events.jsonl"
            event_path.write_bytes(canonical_json_bytes(event))
            observation = engine._event_log_observation(root, plan)
            event["type"] = "run_changed"
            event_path.write_bytes(canonical_json_bytes(event))
            with self.assertRaises(EvidenceError):
                engine._event_log_observation(root, plan, expected=observation)

    def test_quarantine_identity_is_bound_to_exact_task_attempt(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-quarantine-") as temporary:
            root = Path(temporary)
            (root / "logs" / "one").mkdir(parents=True)
            (root / "quarantine").mkdir()
            stdout = root / "logs" / "one" / "attempt-0001.stdout.log"
            stderr = root / "logs" / "one" / "attempt-0001.stderr.log"
            stdout.write_bytes(b"")
            stderr.write_bytes(b"")
            artifact_name = engine._quarantine_name("one", 1, "result.txt")
            artifact = root / "quarantine" / artifact_name
            artifact.write_text("partial", encoding="utf-8")
            identity = file_identity(artifact, label="artifact")
            record = {
                "source": "result.txt",
                "artifact": f"quarantine/{artifact_name}",
                **identity,
            }
            plan = evidence_plan(root)
            state = {
                "tasks": {
                    "one": {
                        "status": "pending",
                        "attempts": [
                            {
                                "stdout": "logs/one/attempt-0001.stdout.log",
                                "stderr": "logs/one/attempt-0001.stderr.log",
                                "quarantined_outputs": [record],
                            }
                        ]
                    }
                }
            }
            engine._validate_attempt_artifacts(root, state, plan)
            state["tasks"]["one"]["attempts"][0]["quarantined_outputs"][0][
                "sha256"
            ] = "0" * 64
            with self.assertRaises(EvidenceError):
                engine._validate_attempt_artifacts(root, state, plan)

    def test_cross_filesystem_quarantine_is_rejected(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-filesystem-") as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            real_stat = Path.stat

            def different_devices(path: Path, *args: object, **kwargs: object) -> object:
                result = real_stat(path, *args, **kwargs)
                device = 1 if Path(path).name == "first" else 2
                return SimpleNamespace(st_dev=device, st_mode=result.st_mode)

            with mock.patch.object(Path, "stat", autospec=True, side_effect=different_devices):
                with self.assertRaises(BoundaryError):
                    require_same_filesystem(
                        first,
                        second,
                        labels=("first", "second"),
                    )

    def test_windows_aliases_and_device_names_are_rejected_portably(self) -> None:
        for path in ("dir/result. ", "dir/CON.txt", "aux"):
            with self.subTest(path=path), self.assertRaises(BoundaryError):
                normalize_relative_file(path, label="test path")

        with WorkspaceTemporaryDirectory(prefix="benchhandoff-alias-") as temporary:
            root = Path(temporary)
            suite = root / "suite.toml"
            suite.write_text(
                "\n".join(
                    [
                        "version = 1",
                        'name = "aliases"',
                        "",
                        "[[task]]",
                        'id = "Task"',
                        'argv = ["python", "-c", "pass"]',
                        "inputs = []",
                        'outputs = ["first.txt"]',
                        "",
                        "[[task]]",
                        'id = "task"',
                        'argv = ["python", "-c", "pass"]',
                        "inputs = []",
                        'outputs = ["second.txt"]',
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                load_suite(suite)

            suite.write_text(
                "\n".join(
                    [
                        "version = 1",
                        'name = "path-alias"',
                        "",
                        "[[task]]",
                        'id = "one"',
                        'argv = ["python", "-c", "pass"]',
                        'inputs = ["Data.txt"]',
                        'outputs = ["data.TXT"]',
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                load_suite(suite)


if __name__ == "__main__":
    unittest.main()
