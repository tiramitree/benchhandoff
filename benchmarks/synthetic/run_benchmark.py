"""Execute and measure one deterministic failure-then-resume scenario."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from benchmark_metadata import benchmark_provenance  # noqa: E402
from benchhandoff.engine import (  # noqa: E402
    inspect_resume,
    resume_run,
    start_run,
    verify_run,
)
from benchhandoff.errors import EvidenceError  # noqa: E402
from benchhandoff.storage import file_identity, read_json_file  # noqa: E402


def _toml_string(value: str) -> str:
    return json.dumps(value)


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="benchhandoff-synthetic-") as temporary:
        root = Path(temporary)
        suite_root = root / "suite"
        suite_root.mkdir()
        run_root = root / "evidence"
        worker_source = Path(__file__).with_name("worker.py")
        shutil.copyfile(worker_source, suite_root / "worker.py")
        (suite_root / "input.txt").write_text("synthetic-input\n", encoding="utf-8")
        suite_text = "\n".join(
            [
                "version = 1",
                'name = "synthetic-interruption-recovery"',
                "",
                "[[task]]",
                'id = "recover-once"',
                "argv = ["
                + ", ".join(
                    _toml_string(item)
                    for item in (sys.executable, "worker.py", "input.txt", "result.json")
                )
                + "]",
                'inputs = ["worker.py", "input.txt"]',
                'outputs = ["result.json"]',
                "",
            ]
        )
        (suite_root / "suite.toml").write_text(suite_text, encoding="utf-8")

        start_clock = time.perf_counter_ns()
        first = start_run(suite_root / "suite.toml", run_root)
        first_elapsed = time.perf_counter_ns() - start_clock
        if first.status != "failed":
            raise RuntimeError(f"synthetic first attempt did not fail: {first.status}")
        if (run_root / "bundle.json").exists():
            raise RuntimeError("failed first attempt unexpectedly produced a bundle")

        stale_decision = inspect_resume(run_root)
        partial_output = suite_root / "result.json"
        partial_output.write_text(
            "partial-result-after-reviewed-drift\n",
            encoding="utf-8",
        )
        state_before_rejection = file_identity(
            run_root / "state.json",
            label="state before stale-decision rejection",
        )
        events_before_rejection = file_identity(
            run_root / "events.jsonl",
            label="events before stale-decision rejection",
        )
        quarantine_before_rejection = sorted(
            path.name for path in (run_root / "quarantine").iterdir()
        )
        attempts_before_rejection = len(
            read_json_file(
                run_root / "state.json",
                label="state before stale-decision rejection",
            )["tasks"]["recover-once"]["attempts"]
        )
        stale_decision_blocked = False
        try:
            resume_run(
                run_root,
                expected_decision_sha256=stale_decision["decision_sha256"],
            )
        except EvidenceError as exc:
            if "resume decision is stale" not in str(exc):
                raise RuntimeError("unexpected bound-resume rejection") from exc
            stale_decision_blocked = True

        state_unchanged_after_rejection = state_before_rejection == file_identity(
            run_root / "state.json",
            label="state after stale-decision rejection",
        )
        events_unchanged_after_rejection = events_before_rejection == file_identity(
            run_root / "events.jsonl",
            label="events after stale-decision rejection",
        )
        quarantine_unchanged_after_rejection = (
            quarantine_before_rejection
            == sorted(path.name for path in (run_root / "quarantine").iterdir())
        )
        partial_output_preserved_after_rejection = (
            partial_output.read_text(encoding="utf-8")
            == "partial-result-after-reviewed-drift\n"
        )
        attempts_after_rejection = len(
            read_json_file(
                run_root / "state.json",
                label="state after stale-decision rejection",
            )["tasks"]["recover-once"]["attempts"]
        )
        refreshed_decision = inspect_resume(run_root)
        decision_changed = (
            stale_decision["decision_sha256"]
            != refreshed_decision["decision_sha256"]
        )
        if not all(
            (
                stale_decision_blocked,
                decision_changed,
                state_unchanged_after_rejection,
                events_unchanged_after_rejection,
                quarantine_unchanged_after_rejection,
                partial_output_preserved_after_rejection,
                attempts_before_rejection == 1,
                attempts_after_rejection == 1,
            )
        ):
            raise RuntimeError(
                "stale resume decision did not fail closed without mutation"
            )

        resume_clock = time.perf_counter_ns()
        second = resume_run(
            run_root,
            expected_decision_sha256=refreshed_decision["decision_sha256"],
        )
        resume_elapsed = time.perf_counter_ns() - resume_clock
        if second.status != "completed":
            raise RuntimeError(f"synthetic resume did not complete: {second.status}")
        verification = verify_run(run_root)
        state = read_json_file(run_root / "state.json", label="synthetic state")
        attempts = state["tasks"]["recover-once"]["attempts"]
        quarantine = attempts[0].get("quarantined_outputs", [])
        if len(attempts) != 2 or len(quarantine) != 1:
            raise RuntimeError("synthetic recovery did not preserve the expected evidence")

        output_identity = file_identity(
            suite_root / "result.json",
            label="synthetic final output",
        )
        result = {
            "schema_version": 1,
            "benchmark": "synthetic-interruption-recovery",
            "recovery_success": True,
            "first_status": first.status,
            "first_exit_code": attempts[0]["return_code"],
            "resume_status": second.status,
            "verify_status": verification["status"],
            "attempts": len(attempts),
            "quarantined_outputs": len(quarantine),
            "resume_decision": {
                "stale_decision_blocked": stale_decision_blocked,
                "decision_changed_after_partial_output_drift": decision_changed,
                "state_unchanged_after_rejection": state_unchanged_after_rejection,
                "events_unchanged_after_rejection": events_unchanged_after_rejection,
                "quarantine_unchanged_after_rejection": (
                    quarantine_unchanged_after_rejection
                ),
                "partial_output_preserved_after_rejection": (
                    partial_output_preserved_after_rejection
                ),
                "attempts_before_rejection": attempts_before_rejection,
                "attempts_after_rejection": attempts_after_rejection,
                "refreshed_decision_resume_status": second.status,
            },
            "final_output": output_identity,
            "first_elapsed_ms": round(first_elapsed / 1_000_000, 3),
            "resume_elapsed_ms": round(resume_elapsed / 1_000_000, 3),
        }
        result.update(benchmark_provenance(REPOSITORY_ROOT))
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        help="optional path for the raw JSON record; existing files are refused",
    )
    arguments = parser.parse_args()
    result = run()
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if arguments.output is not None:
        with arguments.output.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
