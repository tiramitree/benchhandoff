from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.workspace_temp import WorkspaceTemporaryDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "benchmarks" / "synthetic" / "run_writer_recovery.py"


class WriterRecoveryBenchmarkTests(unittest.TestCase):
    def test_exact_orphan_recovery_counts_and_boundaries(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-recovery-benchmark-") as temporary:
            output = Path(temporary) / "writer-recovery.json"
            completed = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "--output", str(output)],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output_bytes = output.read_bytes()
            self.assertNotIn(b"\r", output_bytes)
            self.assertTrue(output_bytes.endswith(b"\n"))
            record = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(record, json.loads(completed.stdout))
            self.assertEqual(
                record["benchmark"],
                "synthetic-orphan-writer-lock-recovery",
            )
            self.assertEqual(record["writer_processes"], 2)
            self.assertEqual(record["hard_exit_holder_return_code"], 0)
            self.assertEqual(record["writer_lock_inspections"], 2)
            self.assertTrue(record["inspection_decisions_identical"])
            self.assertTrue(record["decision_digest_valid"])
            self.assertEqual(record["decision_action"], "recover-orphan")
            self.assertEqual(record["decision_reason"], "owner-dead")
            self.assertEqual(record["run_evidence_files_changed_by_inspection"], 0)
            self.assertEqual(
                record["run_evidence_files_changed_by_lock_recovery"],
                0,
            )
            self.assertFalse(record["partial_output_changed_by_lock_recovery"])
            self.assertEqual(record["attempts_before_lock_recovery"], 1)
            self.assertEqual(record["attempts_after_lock_recovery"], 1)
            self.assertEqual(record["attempts_after_bound_resume"], 2)
            self.assertTrue(record["source_lock_absent_after_recovery"])
            self.assertTrue(record["tombstone_preserved_after_resume"])
            self.assertRegex(
                record["tombstone_identity"]["sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertGreater(record["tombstone_identity"]["size"], 0)
            self.assertIn(
                record["kernel_guard"],
                {"windows-named-mutex", "posix-flock"},
            )
            self.assertEqual(record["final_resume_status"], "completed")
            self.assertEqual(record["final_verify_status"], "verified")
            self.assertIn("not safe-child-retry", record["scope"])
            self.assertEqual(
                record["timing_claim"],
                "none; this benchmark reports deterministic state counts",
            )


if __name__ == "__main__":
    unittest.main()
