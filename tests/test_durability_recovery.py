from __future__ import annotations

import errno
import os
import stat
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.engine as engine
import benchhandoff.storage as storage
from benchhandoff.errors import EvidenceError
from tests.test_audit_regressions import completed_state, evidence_plan
from tests.workspace_temp import WorkspaceTemporaryDirectory


@unittest.skipUnless(os.name == "posix", "POSIX directory durability failures")
class DurabilityRecoveryTests(unittest.TestCase):
    def test_directory_open_failure_is_reported_after_atomic_replace(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-dir-open-") as temporary:
            root = Path(temporary)
            destination = root / "record.json"
            real_open = os.open

            def fail_directory(path, flags, *arguments, **keywords):
                if Path(path) == root and flags & getattr(os, "O_DIRECTORY", 0):
                    raise OSError(errno.EACCES, "synthetic directory open failure")
                return real_open(path, flags, *arguments, **keywords)

            with mock.patch.object(storage.os, "open", side_effect=fail_directory):
                with self.assertRaises(EvidenceError):
                    storage.atomic_write_bytes(destination, b"committed bytes\n")
            self.assertEqual(destination.read_bytes(), b"committed bytes\n")

    def test_directory_fsync_failure_is_not_success(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-dir-sync-") as temporary:
            root = Path(temporary)
            real_sync = os.fsync

            def fail_directory(descriptor):
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise OSError(errno.EIO, "synthetic directory flush failure")
                return real_sync(descriptor)

            with mock.patch.object(storage.os, "fsync", side_effect=fail_directory):
                with self.assertRaises(EvidenceError):
                    storage.atomic_write_bytes(root / "record.json", b"bytes\n")
            self.assertEqual((root / "record.json").read_bytes(), b"bytes\n")

    def _assert_transition_failure_recovers(self, fail_at: int, phase: str) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-transition-sync-") as temporary:
            root = Path(temporary)
            plan = evidence_plan(root)
            state = completed_state()
            (root / engine.EVENTS_FILE).write_bytes(b"")
            storage.atomic_write_json(root / engine.STATE_FILE, state)
            context = engine._RunContext(SimpleNamespace(), root, plan, state)
            real_sync = os.fsync
            directory_flushes = 0

            def fail_one_directory(descriptor):
                nonlocal directory_flushes
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    directory_flushes += 1
                    if directory_flushes == fail_at:
                        raise OSError(errno.EIO, "synthetic transition flush failure")
                return real_sync(descriptor)

            with mock.patch.object(storage.os, "fsync", side_effect=fail_one_directory):
                with self.assertRaises(EvidenceError):
                    engine._commit_transition(
                        context, "run_started", details={"suite": "schema-test", "tasks": 1}
                    )
            durable = storage.read_json_file(root / engine.STATE_FILE, label="state")
            recovered = engine._RunContext(SimpleNamespace(), root, plan, durable)
            self.assertEqual(engine._event_transition_status(root, plan, durable), phase)
            engine._reconcile_pending_event(recovered)
            engine._reconcile_pending_event(recovered)
            self.assertIsNone(recovered.state["pending_event"])
            self.assertEqual(recovered.state["event_log"]["count"], 1)
            self.assertEqual((root / engine.EVENTS_FILE).read_bytes().count(b"\n"), 1)

    def test_intent_directory_flush_failure_recovers_one_event(self) -> None:
        self._assert_transition_failure_recovers(1, "pending_before_log")

    def test_event_directory_flush_failure_does_not_duplicate_event(self) -> None:
        self._assert_transition_failure_recovers(2, "pending_after_log")

    def test_ack_directory_flush_failure_preserves_exact_stable_event(self) -> None:
        self._assert_transition_failure_recovers(3, "stable")


class AttemptLogOwnershipTests(unittest.TestCase):
    def _assert_competing_log_preserved(self, competing_name: str) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-log-conflict-") as temporary:
            root = Path(temporary)
            stdout = root / "stdout.log"
            stderr = root / "stderr.log"
            competing = root / competing_name
            real_open = os.open
            identity = None

            def competing_open(path, flags, *arguments, **keywords):
                nonlocal identity
                if Path(path) == competing and flags & os.O_EXCL:
                    competing.write_bytes(b"")
                    value = competing.stat()
                    identity = (value.st_dev, value.st_ino)
                    raise FileExistsError(errno.EEXIST, "synthetic log collision")
                return real_open(path, flags, *arguments, **keywords)

            with mock.patch.object(engine.os, "open", side_effect=competing_open):
                with self.assertRaises(EvidenceError):
                    engine._prepare_attempt_logs(stdout, stderr)
            self.assertTrue(competing.is_file(), "competing log was deleted")
            value = competing.stat()
            self.assertEqual((value.st_dev, value.st_ino), identity)
            self.assertEqual(competing.read_bytes(), b"")

    def test_competing_stdout_log_is_preserved(self) -> None:
        self._assert_competing_log_preserved("stdout.log")

    def test_competing_stderr_log_is_preserved(self) -> None:
        self._assert_competing_log_preserved("stderr.log")

    def test_second_log_failure_preserves_first_for_retry(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-log-retry-") as temporary:
            root = Path(temporary)
            stdout = root / "stdout.log"
            stderr = root / "stderr.log"
            real_open = os.open

            def fail_second(path, flags, *arguments, **keywords):
                if Path(path) == stderr:
                    raise OSError(errno.EIO, "synthetic second log failure")
                return real_open(path, flags, *arguments, **keywords)

            with mock.patch.object(engine.os, "open", side_effect=fail_second):
                with self.assertRaises(EvidenceError):
                    engine._prepare_attempt_logs(stdout, stderr)
            self.assertTrue(stdout.is_file())
            before = stdout.stat()
            first, second = engine._prepare_attempt_logs(stdout, stderr)
            first.close()
            second.close()
            after = stdout.stat()
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
            self.assertEqual(stdout.read_bytes(), b"")
            self.assertEqual(stderr.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
