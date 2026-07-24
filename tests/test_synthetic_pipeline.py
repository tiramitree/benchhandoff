from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "benchmarks" / "synthetic" / "run_pipeline_comparison.py"

from tests.workspace_temp import WorkspaceTemporaryDirectory


def _load_comparison_module() -> object:
    specification = importlib.util.spec_from_file_location(
        "benchhandoff_synthetic_pipeline",
        SCRIPT,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("unable to load synthetic comparison")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        specification.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPT.parent))
    module.tempfile.TemporaryDirectory = WorkspaceTemporaryDirectory
    return module


class SyntheticPipelineTests(unittest.TestCase):
    def test_exact_restart_and_resume_work_counts(self) -> None:
        module = _load_comparison_module()
        result = module.run()
        naive = result["naive_restart"]
        ledger = result["ledger_resume"]

        self.assertEqual(naive["subprocess_calls"], 18)
        self.assertEqual(ledger["subprocess_calls"], 13)
        self.assertEqual(naive["duplicate_successful_executions"], 5)
        self.assertEqual(ledger["duplicate_successful_executions"], 0)
        self.assertEqual(naive["final_tasks_present"], 12)
        self.assertEqual(ledger["final_tasks_completed"], 12)
        self.assertEqual(ledger["first_failure_code"], 75)
        self.assertEqual(ledger["quarantined_outputs"], 1)
        self.assertEqual(naive["final_output"], ledger["final_output"])
        self.assertEqual(ledger["verify_status"], "verified")
        self.assertEqual(result["timing_claim"], "none; this benchmark reports deterministic work counts only")


if __name__ == "__main__":
    unittest.main()
