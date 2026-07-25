from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from benchhandoff.engine import resume_run, start_run, verify_run
from benchhandoff.errors import EvidenceError
from benchhandoff.writer_lock import WriterLock, writer_lock_path
from tests.test_benchhandoff import FAIL_ONCE_WORKER, write_suite
from tests.workspace_temp import WorkspaceTemporaryDirectory


def _tree_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


class WriterLockTests(unittest.TestCase):
    def _failed_run(self, root: Path) -> tuple[Path, Path]:
        suite = write_suite(root / "suite", FAIL_ONCE_WORKER)
        run = root / "run"
        self.assertEqual(start_run(suite, run).status, "failed")
        self.assertFalse(writer_lock_path(run).exists())
        return suite, run

    def test_exclusive_claim_blocks_a_second_writer(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-lock-exclusive-") as temporary:
            run = Path(temporary) / "run"
            first = WriterLock.acquire(run)
            try:
                self.assertTrue(first.path.is_file())
                with self.assertRaisesRegex(EvidenceError, "writer lock already exists"):
                    WriterLock.acquire(run)
            finally:
                first.release()
            self.assertFalse(first.path.exists())

    def test_exclusive_claim_blocks_a_second_process(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-lock-process-") as temporary:
            run = Path(temporary) / "run"
            first = WriterLock.acquire(run)
            contender_source = """\
import sys
sys.path.insert(0, sys.argv[2])

from benchhandoff.errors import EvidenceError
from benchhandoff.writer_lock import WriterLock

try:
    WriterLock.acquire(sys.argv[1])
except EvidenceError as exc:
    print(str(exc))
    raise SystemExit(30)
raise SystemExit(0)
"""
            try:
                contender = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        contender_source,
                        str(run),
                        str(SOURCE_ROOT),
                    ],
                    cwd=REPOSITORY_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(contender.returncode, 30, contender.stderr)
                self.assertIn("writer lock already exists", contender.stdout)
            finally:
                first.release()
            self.assertFalse(first.path.exists())

    def test_orphaned_lock_blocks_resume_without_evidence_mutation(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-lock-orphan-") as temporary:
            root = Path(temporary)
            _, run = self._failed_run(root)
            holder_source = """\
import os
import sys
sys.path.insert(0, sys.argv[2])

from benchhandoff.writer_lock import WriterLock

WriterLock.acquire(sys.argv[1])
os._exit(0)
"""
            holder = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    holder_source,
                    str(run),
                    str(SOURCE_ROOT),
                ],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(holder.returncode, 0, holder.stderr)
            lock_path = writer_lock_path(run)
            self.assertTrue(lock_path.is_file())
            lock_record = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(lock_record["kind"], "benchhandoff-writer-lock")
            before = _tree_snapshot(run)
            try:
                with self.assertRaisesRegex(EvidenceError, "writer lock already exists"):
                    resume_run(run)
                self.assertEqual(before, _tree_snapshot(run))
            finally:
                if lock_path.exists():
                    lock_path.unlink()

            self.assertEqual(resume_run(run).status, "completed")
            self.assertEqual(verify_run(run)["status"], "verified")

    def test_start_is_blocked_before_run_directory_creation(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-lock-start-") as temporary:
            root = Path(temporary)
            suite = write_suite(root / "suite", FAIL_ONCE_WORKER)
            run = root / "run"
            lock = WriterLock.acquire(run)
            try:
                with self.assertRaisesRegex(EvidenceError, "writer lock already exists"):
                    start_run(suite, run)
                self.assertFalse(run.exists())
            finally:
                lock.release()

            self.assertEqual(start_run(suite, run).status, "failed")
            self.assertFalse(writer_lock_path(run).exists())

    def test_resume_contention_is_rejected_without_evidence_mutation(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-lock-resume-") as temporary:
            root = Path(temporary)
            _, run = self._failed_run(root)
            before = _tree_snapshot(run)
            state_before = json.loads((run / "state.json").read_text(encoding="utf-8"))
            attempts_before = len(state_before["tasks"]["one"]["attempts"])

            lock = WriterLock.acquire(run)
            try:
                with self.assertRaisesRegex(EvidenceError, "writer lock already exists"):
                    resume_run(run)
                self.assertEqual(before, _tree_snapshot(run))
                state_after = json.loads((run / "state.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    attempts_before,
                    len(state_after["tasks"]["one"]["attempts"]),
                )
            finally:
                lock.release()

            self.assertEqual(resume_run(run).status, "completed")
            self.assertEqual(verify_run(run)["status"], "verified")
            self.assertFalse(writer_lock_path(run).exists())

    def test_changed_lock_record_refuses_release(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-lock-drift-") as temporary:
            run = Path(temporary) / "run"
            lock = WriterLock.acquire(run)
            original = lock.path.read_bytes()
            lock.path.write_bytes(original + b"drift")
            with self.assertRaisesRegex(EvidenceError, "writer lock changed"):
                lock.release()
            self.assertTrue(lock.path.exists())

            lock.path.write_bytes(original)
            lock.path.unlink()
            self.assertFalse(lock.path.exists())


if __name__ == "__main__":
    unittest.main()
