from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.writer_lock as writer_lock_module
from benchhandoff.errors import EvidenceError
from benchhandoff.storage import canonical_json_bytes
from benchhandoff.writer_lock import (
    WriterLock,
    inspect_writer_lock,
    recover_writer_lock,
    writer_lock_path,
)
from tests.workspace_temp import WorkspaceTemporaryDirectory


def _lock_record(
    run: Path,
    *,
    owner_pid: int,
    owner_process_start_token: str | None,
    nonce: str = "1" * 32,
) -> dict[str, object]:
    normalized_run = run.parent.resolve(strict=True) / run.name
    return {
        "schema_version": 1,
        "kind": "benchhandoff-writer-lock",
        "run_directory": str(normalized_run),
        "owner_pid": owner_pid,
        "owner_process_start_token": owner_process_start_token,
        "lock_nonce": nonce,
    }


class WriterLockRecoveryBoundaryTests(unittest.TestCase):
    def test_acquisition_write_failure_releases_kernel_guard_and_partial_file(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-write-failure-") as temporary:
            root = Path(temporary)
            run = root / "run"
            with mock.patch.object(
                writer_lock_module,
                "_write_all",
                side_effect=OSError("synthetic writer-lock write failure"),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "synthetic writer-lock write failure",
                ):
                    WriterLock.acquire(run)

            self.assertFalse(writer_lock_path(run).exists())
            self.assertEqual(list(root.iterdir()), [])
            replacement = WriterLock.acquire(run)
            replacement.release()
            self.assertEqual(list(root.iterdir()), [])

    def test_malformed_noncanonical_and_oversized_locks_fail_without_mutation(self) -> None:
        cases = (
            ("malformed", b"{}\n", "fields do not match"),
            (
                "noncanonical",
                b'{\n  "schema_version": 1\n}\n',
                "canonical JSON",
            ),
            ("oversized", b"x" * 4097, "4096-byte size limit"),
        )
        for label, payload, pattern in cases:
            with self.subTest(label=label):
                with WorkspaceTemporaryDirectory(
                    prefix=f"writer-recovery-{label}-"
                ) as temporary:
                    run = Path(temporary) / "run"
                    path = writer_lock_path(run)
                    path.write_bytes(payload)
                    with self.assertRaisesRegex(EvidenceError, pattern):
                        inspect_writer_lock(run)
                    self.assertEqual(path.read_bytes(), payload)

    def test_invalid_expected_digest_is_rejected_before_recovery_mutation(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-digest-") as temporary:
            run = Path(temporary) / "run"
            path = writer_lock_path(run)
            payload = canonical_json_bytes(
                _lock_record(
                    run,
                    owner_pid=2_000_000_000,
                    owner_process_start_token="synthetic:dead",
                )
            )
            path.write_bytes(payload)
            decision = inspect_writer_lock(run)
            self.assertEqual(decision["action"], "recover-orphan")

            with self.assertRaisesRegex(
                EvidenceError,
                "64 lowercase hexadecimal",
            ):
                recover_writer_lock(
                    run,
                    expected_decision_sha256="A" * 64,
                )

            self.assertEqual(path.read_bytes(), payload)
            self.assertFalse(Path(decision["tombstone_path"]).exists())

    def test_live_pid_without_recorded_start_token_is_not_recoverable(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-no-token-") as temporary:
            run = Path(temporary) / "run"
            path = writer_lock_path(run)
            payload = canonical_json_bytes(
                _lock_record(
                    run,
                    owner_pid=os.getpid(),
                    owner_process_start_token=None,
                )
            )
            path.write_bytes(payload)

            decision = inspect_writer_lock(run)

            self.assertEqual(decision["action"], "refuse")
            self.assertEqual(decision["reason"], "owner-identity-unverifiable")
            with self.assertRaisesRegex(EvidenceError, "not recoverable"):
                recover_writer_lock(
                    run,
                    expected_decision_sha256=decision["decision_sha256"],
                )
            self.assertEqual(path.read_bytes(), payload)

    def test_changing_owner_observation_fails_closed(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-unstable-") as temporary:
            run = Path(temporary) / "run"
            path = writer_lock_path(run)
            payload = canonical_json_bytes(
                _lock_record(
                    run,
                    owner_pid=2_000_000_000,
                    owner_process_start_token="synthetic:old",
                )
            )
            path.write_bytes(payload)
            with mock.patch.object(
                writer_lock_module,
                "process_liveness",
                side_effect=["dead", "alive"],
            ):
                decision = inspect_writer_lock(run)

            self.assertEqual(decision["action"], "refuse")
            self.assertEqual(decision["reason"], "owner-observation-changed")
            self.assertEqual(path.read_bytes(), payload)

    def test_unexpected_preexisting_hard_link_refuses_recovery(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-extra-link-") as temporary:
            root = Path(temporary)
            run = root / "run"
            path = writer_lock_path(run)
            payload = canonical_json_bytes(
                _lock_record(
                    run,
                    owner_pid=2_000_000_000,
                    owner_process_start_token="synthetic:dead",
                )
            )
            path.write_bytes(payload)
            external_link = root / "unexpected-hard-link.json"
            os.link(path, external_link, follow_symlinks=False)
            decision = inspect_writer_lock(run)

            with self.assertRaisesRegex(EvidenceError, "unexpected hard-link count"):
                recover_writer_lock(
                    run,
                    expected_decision_sha256=decision["decision_sha256"],
                )

            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(external_link.read_bytes(), payload)
            self.assertFalse(Path(decision["tombstone_path"]).exists())


if __name__ == "__main__":
    unittest.main()
