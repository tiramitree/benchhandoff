from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from benchhandoff.engine import resume_run, start_run, verify_run
from tests.workspace_temp import WorkspaceTemporaryDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPOSITORY_ROOT / "examples" / "recovery_pipeline"


class RecoveryExampleTests(unittest.TestCase):
    def test_failure_resume_and_verify_contract(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-example-") as temporary:
            root = Path(temporary)
            suite_root = root / "suite"
            run_root = root / "run"
            shutil.copytree(EXAMPLE, suite_root)

            first = start_run(suite_root / "suite.toml", run_root)
            self.assertEqual(first.status, "failed")
            self.assertFalse((run_root / "bundle.json").exists())
            self.assertEqual(
                json.loads((suite_root / "metrics.json").read_text(encoding="utf-8")),
                {"status": "partial"},
            )

            second = resume_run(run_root)
            self.assertEqual(second.status, "completed")
            self.assertEqual(verify_run(run_root)["status"], "verified")

            state = json.loads((run_root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["tasks"]["prepare-features"]["status"], "completed")
            attempts = state["tasks"]["evaluate"]["attempts"]
            self.assertEqual([attempt["status"] for attempt in attempts], ["failed", "completed"])
            self.assertEqual(len(attempts[0]["quarantined_outputs"]), 1)
            self.assertEqual(state["tasks"]["summarize"]["status"], "completed")

            quarantined = list((run_root / "quarantine").iterdir())
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(
                json.loads(quarantined[0].read_text(encoding="utf-8")),
                {"status": "partial"},
            )
            self.assertEqual(
                json.loads((suite_root / "summary.json").read_text(encoding="utf-8")),
                {"message": "processed 4 synthetic samples", "scaled_sum": 120},
            )


if __name__ == "__main__":
    unittest.main()
