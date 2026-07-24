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
from benchhandoff.engine import resume_run, start_run, verify_run  # noqa: E402
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

        resume_clock = time.perf_counter_ns()
        second = resume_run(run_root)
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
