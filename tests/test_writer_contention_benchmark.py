from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.workspace_temp import WorkspaceTemporaryDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "benchmarks" / "synthetic" / "run_writer_contention.py"


class WriterContentionBenchmarkTests(unittest.TestCase):
    def test_exact_contention_counts_and_boundaries(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="writer-contention-benchmark-") as temporary:
            output = Path(temporary) / "writer-contention.json"
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
                "synthetic-cooperative-writer-contention",
            )
            self.assertEqual(record["writer_processes"], 2)
            self.assertEqual(record["contending_resume_calls"], 1)
            self.assertTrue(
                record["contender_rejected_before_run_evidence_mutation"]
            )
            self.assertEqual(record["run_evidence_files_changed_on_rejection"], 0)
            self.assertFalse(record["partial_output_changed_on_rejection"])
            self.assertEqual(record["attempts_before_contention"], 1)
            self.assertEqual(record["attempts_after_rejection"], 1)
            self.assertEqual(record["attempts_after_successful_resume"], 2)
            self.assertEqual(record["final_resume_status"], "completed")
            self.assertEqual(record["final_verify_status"], "verified")
            self.assertEqual(
                record["holder_record_kind"],
                "benchhandoff-writer-lock",
            )
            self.assertTrue(record["lock_absent_after_clean_holder_exit"])
            self.assertEqual(record["rejection_error_type"], "EvidenceError")
            self.assertEqual(record["rejection_reason"], "writer-lock-exists")
            self.assertIn("not hostile-writer", record["scope"])
            self.assertEqual(
                record["timing_claim"],
                "none; this benchmark reports deterministic state counts",
            )


if __name__ == "__main__":
    unittest.main()
