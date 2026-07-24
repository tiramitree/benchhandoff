from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.engine as engine
from benchhandoff.errors import EvidenceError
from benchhandoff.storage import atomic_write_json, read_json_file
from tests.test_audit_regressions import completed_state, evidence_plan
from tests.workspace_temp import WorkspaceTemporaryDirectory


class TransitionRecoveryTests(unittest.TestCase):
    def make_context(self, root: Path) -> engine._RunContext:
        plan = evidence_plan(root)
        state = completed_state()
        (root / engine.EVENTS_FILE).write_bytes(b"")
        atomic_write_json(root / engine.STATE_FILE, state)
        return engine._RunContext(
            suite=SimpleNamespace(),
            run_root=root,
            plan=plan,
            state=state,
        )

    def test_pending_before_log_write_is_reconciled(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-transition-before-") as temporary:
            root = Path(temporary)
            context = self.make_context(root)
            with mock.patch.object(
                engine,
                "atomic_write_bytes",
                side_effect=OSError("synthetic event replace failure"),
            ):
                with self.assertRaises(OSError):
                    engine._commit_transition(
                        context,
                        "run_started",
                        details={"suite": "schema-test", "tasks": 1},
                    )

            state = read_json_file(root / engine.STATE_FILE, label="state")
            recovered = engine._RunContext(
                suite=SimpleNamespace(),
                run_root=root,
                plan=context.plan,
                state=state,
            )
            self.assertEqual(
                engine._event_transition_status(root, context.plan, state),
                "pending_before_log",
            )
            engine._reconcile_pending_event(recovered)
            self.assertIsNone(recovered.state["pending_event"])
            self.assertEqual(recovered.state["event_log"]["count"], 1)
            self.assertEqual((root / engine.EVENTS_FILE).read_bytes().count(b"\n"), 1)

    def test_pending_after_log_write_is_reconciled_without_duplicate(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-transition-after-") as temporary:
            root = Path(temporary)
            context = self.make_context(root)
            real_persist = engine._persist_state
            calls = 0

            def fail_ack(run_context: engine._RunContext, *, touch_updated_at: bool) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic state acknowledgement failure")
                real_persist(run_context, touch_updated_at=touch_updated_at)

            with mock.patch.object(engine, "_persist_state", side_effect=fail_ack):
                with self.assertRaises(OSError):
                    engine._commit_transition(
                        context,
                        "run_started",
                        details={"suite": "schema-test", "tasks": 1},
                    )

            event_bytes = (root / engine.EVENTS_FILE).read_bytes()
            state = read_json_file(root / engine.STATE_FILE, label="state")
            recovered = engine._RunContext(
                suite=SimpleNamespace(),
                run_root=root,
                plan=context.plan,
                state=state,
            )
            self.assertEqual(
                engine._event_transition_status(root, context.plan, state),
                "pending_after_log",
            )
            engine._reconcile_pending_event(recovered)
            self.assertEqual((root / engine.EVENTS_FILE).read_bytes(), event_bytes)
            self.assertEqual(recovered.state["event_log"]["count"], 1)

    def test_divergence_outside_one_pending_event_is_rejected(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-transition-diverge-") as temporary:
            root = Path(temporary)
            context = self.make_context(root)
            with mock.patch.object(
                engine,
                "atomic_write_bytes",
                side_effect=OSError("synthetic event replace failure"),
            ):
                with self.assertRaises(OSError):
                    engine._commit_transition(
                        context,
                        "run_started",
                        details={"suite": "schema-test", "tasks": 1},
                    )
            state = read_json_file(root / engine.STATE_FILE, label="state")
            (root / engine.EVENTS_FILE).write_bytes(b"{}\n")
            with self.assertRaises(EvidenceError):
                engine._event_transition_status(root, context.plan, state)

    def test_bundle_refuses_pending_transition(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-transition-bundle-") as temporary:
            root = Path(temporary)
            context = self.make_context(root)
            context.state["pending_event"] = {
                "schema_version": 1,
                "time": "2026-07-24T00:00:00Z",
                "type": "run_started",
                "run_id": context.plan["run_id"],
                "sequence": 1,
                "previous_sha256": context.state["event_log"]["sha256"],
                "details": {"suite": "schema-test", "tasks": 1},
            }
            with self.assertRaises(EvidenceError):
                engine._build_bundle(context)

    def test_verify_refuses_pending_transition_without_mutation(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-transition-verify-") as temporary:
            root = Path(temporary)
            context = self.make_context(root)
            context.state["pending_event"] = {
                "schema_version": 1,
                "time": "2026-07-24T00:00:00Z",
                "type": "run_started",
                "run_id": context.plan["run_id"],
                "sequence": 1,
                "previous_sha256": context.state["event_log"]["sha256"],
                "details": {"suite": "schema-test", "tasks": 1},
            }
            before_state = (root / engine.STATE_FILE).read_bytes()
            before_events = (root / engine.EVENTS_FILE).read_bytes()
            with mock.patch.object(engine, "_load_context", return_value=context):
                with self.assertRaisesRegex(EvidenceError, "pending transition"):
                    engine._verify_run_checked(root)
            self.assertEqual((root / engine.STATE_FILE).read_bytes(), before_state)
            self.assertEqual((root / engine.EVENTS_FILE).read_bytes(), before_events)

    def test_event_log_total_limit_is_checked_before_pending_intent(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-transition-limit-") as temporary:
            root = Path(temporary)
            context = self.make_context(root)
            state_before = (root / engine.STATE_FILE).read_bytes()
            with mock.patch.object(engine, "MAX_EVENT_LOG_BYTES", 1):
                with self.assertRaisesRegex(EvidenceError, "total size limit"):
                    engine._commit_transition(
                        context,
                        "run_started",
                        details={"suite": "schema-test", "tasks": 1},
                    )
            self.assertIsNone(context.state["pending_event"])
            self.assertEqual((root / engine.STATE_FILE).read_bytes(), state_before)
            self.assertEqual((root / engine.EVENTS_FILE).read_bytes(), b"")

    def test_event_count_limit_is_checked_before_pending_intent(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-transition-count-") as temporary:
            root = Path(temporary)
            context = self.make_context(root)
            state_before = (root / engine.STATE_FILE).read_bytes()
            with mock.patch.object(engine, "MAX_EVENT_COUNT", 0):
                with self.assertRaisesRegex(EvidenceError, "event-count limit"):
                    engine._commit_transition(
                        context,
                        "run_started",
                        details={"suite": "schema-test", "tasks": 1},
                    )
            self.assertIsNone(context.state["pending_event"])
            self.assertEqual((root / engine.STATE_FILE).read_bytes(), state_before)
            self.assertEqual((root / engine.EVENTS_FILE).read_bytes(), b"")

    def test_event_type_and_details_are_bounded(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-transition-schema-") as temporary:
            plan = evidence_plan(Path(temporary))
            base = {
                "schema_version": 1,
                "time": "2026-07-24T00:00:00Z",
                "type": "run_started",
                "run_id": plan["run_id"],
                "sequence": 1,
                "previous_sha256": hashlib.sha256(b"").hexdigest(),
                "details": {"suite": "schema-test", "tasks": 1},
            }
            unknown = copy.deepcopy(base)
            unknown["type"] = "run_changed"
            with self.assertRaises(EvidenceError):
                engine._validate_event_record(
                    unknown,
                    plan,
                    record_number=1,
                    previous_sha256=base["previous_sha256"],
                )
            oversized = copy.deepcopy(base)
            oversized.update({"type": "task_failed", "task_id": "one"})
            oversized["details"] = {
                "attempt": 1,
                "return_code": None,
                "reason": "x" * 8193,
            }
            with self.assertRaises(EvidenceError):
                engine._validate_event_record(
                    oversized,
                    plan,
                    record_number=1,
                    previous_sha256=base["previous_sha256"],
                )


if __name__ == "__main__":
    unittest.main()
