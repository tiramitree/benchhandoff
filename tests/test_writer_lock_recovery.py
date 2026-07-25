from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.writer_lock as writer_lock_module
from benchhandoff.cli import main as cli_main
from benchhandoff.engine import resume_run, start_run, verify_run
from benchhandoff.errors import EvidenceError
from benchhandoff.storage import canonical_json_bytes
from benchhandoff.writer_lock import (
    WriterLock,
    inspect_writer_lock,
    recover_writer_lock,
    writer_lock_path,
)
from tests.test_benchhandoff import FAIL_ONCE_WORKER, write_suite
from tests.workspace_temp import WorkspaceTemporaryDirectory


def _tree_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


class WriterLockRecoveryTests(unittest.TestCase):
    def _failed_run(self, root: Path) -> Path:
        suite = write_suite(root / "suite", FAIL_ONCE_WORKER)
        run = root / "run"
        self.assertEqual(start_run(suite, run).status, "failed")
        return run

    def _orphan_lock(self, run: Path) -> tuple[Path, bytes]:
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
        return lock_path, lock_path.read_bytes()

    def test_orphan_inspection_is_read_only_and_bound_recovery_preserves_lock(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-orphan-") as temporary:
            root = Path(temporary)
            run = self._failed_run(root)
            lock_path, original = self._orphan_lock(run)
            before_parent = _tree_snapshot(root)
            before_run = _tree_snapshot(run)

            first = inspect_writer_lock(run)
            second = inspect_writer_lock(run)

            self.assertEqual(first, second)
            self.assertEqual(before_parent, _tree_snapshot(root))
            self.assertEqual(first["kind"], "benchhandoff-writer-lock-recovery-decision")
            self.assertEqual(first["action"], "recover-orphan")
            self.assertEqual(first["reason"], "owner-dead")
            decision_body = dict(first)
            recorded_digest = decision_body.pop("decision_sha256")
            self.assertEqual(
                recorded_digest,
                hashlib.sha256(canonical_json_bytes(decision_body)).hexdigest(),
            )

            recovered = recover_writer_lock(
                run,
                expected_decision_sha256=first["decision_sha256"],
            )

            tombstone = Path(recovered["tombstone"]["path"])
            self.assertEqual(recovered["status"], "recovered")
            self.assertEqual(recovered["decision_sha256"], first["decision_sha256"])
            self.assertFalse(lock_path.exists())
            self.assertEqual(tombstone.read_bytes(), original)
            self.assertEqual(before_run, _tree_snapshot(run))
            self.assertEqual(resume_run(run).status, "completed")
            self.assertEqual(verify_run(run)["status"], "verified")
            self.assertEqual(tombstone.read_bytes(), original)

    def test_live_owner_is_read_only_and_not_recoverable(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-live-") as temporary:
            run = Path(temporary) / "run"
            lock = WriterLock.acquire(run)
            try:
                before = lock.path.read_bytes()
                decision = inspect_writer_lock(run)
                self.assertEqual(decision["action"], "refuse")
                self.assertEqual(decision["reason"], "owner-alive")
                with self.assertRaisesRegex(EvidenceError, "not recoverable"):
                    recover_writer_lock(
                        run,
                        expected_decision_sha256=decision["decision_sha256"],
                    )
                self.assertEqual(lock.path.read_bytes(), before)
                self.assertFalse(Path(decision["tombstone_path"]).exists())
            finally:
                lock.release()

    def test_unknown_liveness_fails_closed(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-unknown-") as temporary:
            run = self._failed_run(Path(temporary))
            lock_path, original = self._orphan_lock(run)
            with mock.patch.object(
                writer_lock_module,
                "process_liveness",
                return_value="unknown",
            ):
                decision = inspect_writer_lock(run)
                self.assertEqual(decision["action"], "refuse")
                self.assertEqual(decision["reason"], "owner-liveness-unknown")
                with self.assertRaisesRegex(EvidenceError, "not recoverable"):
                    recover_writer_lock(
                        run,
                        expected_decision_sha256=decision["decision_sha256"],
                    )
            self.assertEqual(lock_path.read_bytes(), original)

    def test_stable_pid_reuse_is_recoverable(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-reused-") as temporary:
            run = self._failed_run(Path(temporary))
            lock_path, original = self._orphan_lock(run)
            recorded = json.loads(original.decode("utf-8"))
            self.assertNotEqual(
                recorded["owner_process_start_token"],
                "synthetic:reused-pid",
            )
            with (
                mock.patch.object(
                    writer_lock_module,
                    "process_liveness",
                    return_value="alive",
                ),
                mock.patch.object(
                    writer_lock_module,
                    "process_start_token",
                    return_value="synthetic:reused-pid",
                ),
            ):
                decision = inspect_writer_lock(run)
                self.assertEqual(decision["action"], "recover-orphan")
                self.assertEqual(decision["reason"], "owner-pid-reused")
                recovered = recover_writer_lock(
                    run,
                    expected_decision_sha256=decision["decision_sha256"],
                )
            self.assertFalse(lock_path.exists())
            self.assertEqual(Path(recovered["tombstone"]["path"]).read_bytes(), original)

    def test_stale_decision_rejects_changed_lock_without_recovery_mutation(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-stale-") as temporary:
            run = self._failed_run(Path(temporary))
            lock_path, _ = self._orphan_lock(run)
            decision = inspect_writer_lock(run)
            changed = json.loads(lock_path.read_text(encoding="utf-8"))
            changed["lock_nonce"] = "0" * 32
            lock_path.write_bytes(canonical_json_bytes(changed))
            changed_bytes = lock_path.read_bytes()

            with self.assertRaisesRegex(EvidenceError, "decision is stale"):
                recover_writer_lock(
                    run,
                    expected_decision_sha256=decision["decision_sha256"],
                )

            self.assertEqual(lock_path.read_bytes(), changed_bytes)
            self.assertFalse(Path(decision["tombstone_path"]).exists())

    def test_partial_hard_link_recovery_is_safely_resumable(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-partial-") as temporary:
            run = self._failed_run(Path(temporary))
            lock_path, original = self._orphan_lock(run)
            decision = inspect_writer_lock(run)
            tombstone = Path(decision["tombstone_path"])
            os.link(lock_path, tombstone, follow_symlinks=False)
            self.assertTrue(os.path.samefile(lock_path, tombstone))

            recovered = recover_writer_lock(
                run,
                expected_decision_sha256=decision["decision_sha256"],
            )

            self.assertEqual(recovered["status"], "recovered")
            self.assertFalse(lock_path.exists())
            self.assertEqual(tombstone.read_bytes(), original)
            self.assertEqual(tombstone.stat().st_nlink, 1)

    def test_foreign_tombstone_refuses_and_preserves_source(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-foreign-") as temporary:
            run = self._failed_run(Path(temporary))
            lock_path, original = self._orphan_lock(run)
            decision = inspect_writer_lock(run)
            tombstone = Path(decision["tombstone_path"])
            tombstone.write_bytes(b"foreign\n")

            with self.assertRaisesRegex(EvidenceError, "tombstone"):
                recover_writer_lock(
                    run,
                    expected_decision_sha256=decision["decision_sha256"],
                )

            self.assertEqual(lock_path.read_bytes(), original)
            self.assertEqual(tombstone.read_bytes(), b"foreign\n")

    def test_kernel_lock_blocks_recovery_when_liveness_probe_is_wrong(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-kernel-") as temporary:
            run = Path(temporary) / "run"
            holder_source = """\
import sys
sys.path.insert(0, sys.argv[2])

from benchhandoff.writer_lock import WriterLock

lock = WriterLock.acquire(sys.argv[1])
print("ready", flush=True)
sys.stdin.readline()
lock.release()
"""
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    holder_source,
                    str(run),
                    str(SOURCE_ROOT),
                ],
                cwd=REPOSITORY_ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ready")
                with mock.patch.object(
                    writer_lock_module,
                    "process_liveness",
                    return_value="dead",
                ):
                    decision = inspect_writer_lock(run)
                    self.assertEqual(decision["action"], "recover-orphan")
                    with self.assertRaisesRegex(EvidenceError, "kernel lock"):
                        recover_writer_lock(
                            run,
                            expected_decision_sha256=decision["decision_sha256"],
                        )
                self.assertTrue(writer_lock_path(run).is_file())
                self.assertFalse(Path(decision["tombstone_path"]).exists())
            finally:
                if holder.stdin is not None:
                    holder.stdin.write("\n")
                    holder.stdin.flush()
                stdout, stderr = holder.communicate(timeout=10)
                self.assertEqual(holder.returncode, 0, stdout + stderr)
            self.assertFalse(writer_lock_path(run).exists())

    def test_cli_inspect_and_recover_writer_lock(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-cli-") as temporary:
            run = self._failed_run(Path(temporary))
            lock_path, original = self._orphan_lock(run)
            with mock.patch("benchhandoff.cli._emit") as emit:
                self.assertEqual(cli_main(["inspect-writer-lock", str(run)]), 0)
            decision = emit.call_args.args[0]

            with mock.patch("benchhandoff.cli._emit") as emit:
                code = cli_main(
                    [
                        "recover-writer-lock",
                        str(run),
                        "--expected-decision-sha256",
                        decision["decision_sha256"],
                    ]
                )
            result = emit.call_args.args[0]
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "recovered")
            self.assertFalse(lock_path.exists())
            self.assertEqual(Path(result["tombstone"]["path"]).read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
