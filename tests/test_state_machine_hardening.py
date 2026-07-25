from __future__ import annotations

import copy
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
from benchhandoff.errors import BoundaryError, EvidenceError
from tests.test_audit_regressions import IDENTITY, completed_state, evidence_plan
from tests.workspace_temp import WorkspaceTemporaryDirectory


def failed_attempt(number: int, *, recovered: bool) -> dict[str, object]:
    value: dict[str, object] = {
        "number": number,
        "status": "failed",
        "started_at": "2026-07-24T00:00:01Z",
        "ended_at": "2026-07-24T00:00:02Z",
        "child_pid": None,
        "child_start_token": None,
        "child_launch_guard": False,
        "return_code": None,
        "argv": ["python", "worker.py"],
        "verified_inputs": {"input.txt": dict(IDENTITY)},
        "stdout": f"logs/one/attempt-{number:04d}.stdout.log",
        "stderr": f"logs/one/attempt-{number:04d}.stderr.log",
        "error": "synthetic failure",
    }
    if recovered:
        value["quarantined_outputs"] = []
    return value


def completed_attempt(number: int) -> dict[str, object]:
    value = copy.deepcopy(completed_state()["tasks"]["one"]["attempts"][0])
    value["number"] = number
    value["stdout"] = f"logs/one/attempt-{number:04d}.stdout.log"
    value["stderr"] = f"logs/one/attempt-{number:04d}.stderr.log"
    return value


class StateMachineHardeningTests(unittest.TestCase):
    def test_completed_attempt_cannot_have_successor(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-state-completed-") as temporary:
            plan = evidence_plan(Path(temporary))
            state = completed_state()
            task = state["tasks"]["one"]
            task["attempts"].append(failed_attempt(2, recovered=False))
            task["status"] = "failed"
            task["verified_inputs"] = {}
            task["verified_outputs"] = {}
            state["status"] = "failed"
            state["last_error"] = "synthetic"
            with self.assertRaises(EvidenceError):
                engine._validate_state_shape(state, plan)

    def test_successor_requires_recovery_marker(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-state-recovery-") as temporary:
            plan = evidence_plan(Path(temporary))
            state = completed_state()
            task = state["tasks"]["one"]
            task["attempts"] = [
                failed_attempt(1, recovered=False),
                completed_attempt(2),
            ]
            with self.assertRaises(EvidenceError):
                engine._validate_state_shape(state, plan)

            task["attempts"][0]["quarantined_outputs"] = []
            engine._validate_state_shape(state, plan)

    def test_latest_recovered_attempt_maps_only_to_pending(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-state-latest-") as temporary:
            plan = evidence_plan(Path(temporary))
            state = completed_state()
            task = state["tasks"]["one"]
            task["attempts"] = [failed_attempt(1, recovered=True)]
            task["status"] = "failed"
            task["verified_inputs"] = {}
            task["verified_outputs"] = {}
            state["status"] = "failed"
            state["last_error"] = "synthetic"
            with self.assertRaises(EvidenceError):
                engine._validate_state_shape(state, plan)

            task["status"] = "pending"
            state["status"] = "running"
            state["last_error"] = None
            engine._validate_state_shape(state, plan)

    def test_terminal_child_requires_confirmed_return_code(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-state-child-") as temporary:
            plan = evidence_plan(Path(temporary))
            state = completed_state()
            attempt = state["tasks"]["one"]["attempts"][0]
            attempt["status"] = "failed"
            attempt["ended_at"] = "2026-07-24T00:00:02Z"
            attempt["return_code"] = None
            attempt["error"] = "synthetic"
            attempt.pop("verified_outputs")
            task = state["tasks"]["one"]
            task["status"] = "failed"
            task["verified_inputs"] = {}
            task["verified_outputs"] = {}
            state["status"] = "failed"
            state["last_error"] = "synthetic"
            with self.assertRaises(EvidenceError):
                engine._validate_state_shape(state, plan)

    def test_empty_orphan_logs_are_reusable_but_nonempty_log_is_rejected(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-log-pair-") as temporary:
            root = Path(temporary)
            stdout = root / "attempt.stdout.log"
            stderr = root / "attempt.stderr.log"
            first = engine._prepare_attempt_logs(stdout, stderr)
            first[0].close()
            first[1].close()
            second = engine._prepare_attempt_logs(stdout, stderr)
            second[0].close()
            second[1].close()

            stderr.unlink()
            third = engine._prepare_attempt_logs(stdout, stderr)
            third[0].close()
            third[1].close()
            stdout.write_bytes(b"not empty")
            with self.assertRaises(EvidenceError):
                engine._prepare_attempt_logs(stdout, stderr)

    def test_orphan_log_symlink_pair_is_rejected(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-log-symlink-") as temporary:
            root = Path(temporary)
            target = root / "target.log"
            target.write_bytes(b"")
            stdout = root / "attempt.stdout.log"
            stderr = root / "attempt.stderr.log"
            try:
                os.symlink(target, stdout)
                os.symlink(target, stderr)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaises(EvidenceError):
                engine._prepare_attempt_logs(stdout, stderr)
            self.assertEqual(target.read_bytes(), b"")

    def test_existing_log_boundary_failure_is_an_evidence_error(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-log-boundary-") as temporary:
            root = Path(temporary)
            stdout = root / "attempt.stdout.log"
            stderr = root / "attempt.stderr.log"
            stdout.write_bytes(b"")
            stderr.write_bytes(b"")
            with (
                mock.patch.object(
                    engine,
                    "file_identity",
                    side_effect=BoundaryError("synthetic log boundary"),
                ),
                self.assertRaisesRegex(EvidenceError, "synthetic log boundary"),
            ):
                engine._prepare_attempt_logs(stdout, stderr)

    def test_attempt_history_limit_is_enforced_before_full_validation(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-attempt-limit-") as temporary:
            plan = evidence_plan(Path(temporary))
            state = completed_state()
            state["tasks"]["one"]["attempts"] = [
                failed_attempt(1, recovered=True),
                completed_attempt(2),
            ]
            with mock.patch.object(engine, "MAX_ATTEMPTS_PER_TASK", 1):
                with self.assertRaisesRegex(EvidenceError, "attempt limit"):
                    engine._validate_state_shape(state, plan)

    def test_single_empty_orphan_log_is_completed_and_reused(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-log-single-") as temporary:
            root = Path(temporary)
            stdout = root / "attempt.stdout.log"
            stderr = root / "attempt.stderr.log"
            stdout.write_bytes(b"")
            handles = engine._prepare_attempt_logs(stdout, stderr)
            handles[0].close()
            handles[1].close()
            self.assertTrue(stdout.is_file())
            self.assertTrue(stderr.is_file())
            self.assertEqual(stdout.stat().st_size, 0)
            self.assertEqual(stderr.stat().st_size, 0)

    def test_later_tasks_must_remain_untouched_after_first_incomplete(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-task-order-") as temporary:
            plan = evidence_plan(Path(temporary))
            second_task = copy.deepcopy(plan["suite"]["tasks"][0])
            second_task["id"] = "two"
            second_task["outputs"] = ["result-two.txt"]
            plan["suite"]["tasks"].append(second_task)

            state = completed_state()
            first = state["tasks"]["one"]
            first["status"] = "failed"
            first["attempts"] = [failed_attempt(1, recovered=False)]
            first["verified_inputs"] = {}
            first["verified_outputs"] = {}
            second_attempt = failed_attempt(1, recovered=False)
            second_attempt["stdout"] = "logs/two/attempt-0001.stdout.log"
            second_attempt["stderr"] = "logs/two/attempt-0001.stderr.log"
            state["tasks"]["two"] = {
                "status": "failed",
                "attempts": [second_attempt],
                "verified_inputs": {},
                "verified_outputs": {},
            }
            state["status"] = "failed"
            state["last_error"] = "synthetic"

            with self.assertRaisesRegex(EvidenceError, "untouched and pending"):
                engine._validate_state_shape(state, plan)

    def test_quarantine_name_is_fixed_length_for_long_legal_source(self) -> None:
        first = engine._quarantine_name("one", 1, "nested/" + "x" * 240)
        second = engine._quarantine_name("one", 1, "nested/" + "y" * 240)
        self.assertLess(len(first.encode("utf-8")), 128)
        self.assertNotEqual(first, second)
        self.assertEqual(first, engine._quarantine_name("one", 1, "nested/" + "x" * 240))


if __name__ == "__main__":
    unittest.main()
