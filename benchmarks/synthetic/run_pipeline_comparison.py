"""Compare a full naive restart with BenchHandoff resume on 12 sequential tasks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from benchmark_metadata import benchmark_provenance  # noqa: E402
from benchhandoff.engine import resume_run, start_run, verify_run  # noqa: E402
from benchhandoff.storage import file_identity, read_json_file  # noqa: E402

TASK_COUNT = 12
FAILURE_TASK = 6
EXPECTED_NAIVE_CALLS = 18
EXPECTED_LEDGER_CALLS = 13


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _task_output(number: int) -> str:
    return f"task-{number:02d}.txt"


def _task_input(number: int) -> str:
    return "seed.txt" if number == 1 else _task_output(number - 1)


def _write_ledger_suite(root: Path, worker_source: Path) -> Path:
    root.mkdir()
    shutil.copyfile(worker_source, root / "pipeline_worker.py")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    lines = [
        "version = 1",
        'name = "synthetic-12-task-interruption"',
        "",
    ]
    for number in range(1, TASK_COUNT + 1):
        arguments = [
            sys.executable,
            "pipeline_worker.py",
            str(number),
            _task_input(number),
            _task_output(number),
        ]
        lines.extend(
            [
                "[[task]]",
                f'id = "task-{number:02d}"',
                "argv = [" + ", ".join(_toml_string(item) for item in arguments) + "]",
                "inputs = ["
                + ", ".join(
                    _toml_string(item)
                    for item in ("pipeline_worker.py", _task_input(number))
                )
                + "]",
                f'outputs = ["{_task_output(number)}"]',
                "",
            ]
        )
    suite = root / "suite.toml"
    suite.write_text("\n".join(lines), encoding="utf-8")
    return suite


def _run_naive(root: Path, worker_source: Path) -> dict[str, object]:
    root.mkdir()
    worker = root / "pipeline_worker.py"
    shutil.copyfile(worker_source, worker)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    calls = 0
    successful_by_task = {number: 0 for number in range(1, TASK_COUNT + 1)}
    failure_codes: list[int] = []

    for pipeline_pass in (1, 2):
        for number in range(1, TASK_COUNT + 1):
            environment = os.environ.copy()
            environment.update(
                {
                    "SYNTHETIC_MODE": "naive",
                    "SYNTHETIC_PASS": str(pipeline_pass),
                }
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(worker),
                    str(number),
                    _task_input(number),
                    _task_output(number),
                ],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                shell=False,
                check=False,
            )
            calls += 1
            if completed.returncode == 0:
                successful_by_task[number] += 1
            else:
                failure_codes.append(completed.returncode)
                break

    completed_tasks = sum(
        1 for number in range(1, TASK_COUNT + 1) if (root / _task_output(number)).is_file()
    )
    duplicate_successes = sum(max(0, count - 1) for count in successful_by_task.values())
    final_identity = file_identity(
        root / _task_output(TASK_COUNT),
        label="naive final output",
    )
    return {
        "strategy": "naive-full-restart",
        "subprocess_calls": calls,
        "successful_task_executions": sum(successful_by_task.values()),
        "duplicate_successful_executions": duplicate_successes,
        "final_tasks_present": completed_tasks,
        "failure_codes": failure_codes,
        "final_output": final_identity,
    }


def _run_ledger(root: Path, worker_source: Path) -> dict[str, object]:
    root.mkdir()
    suite_root = root / "suite"
    run_root = root / "evidence"
    suite = _write_ledger_suite(suite_root, worker_source)
    first = start_run(suite, run_root)
    if first.status != "failed":
        raise RuntimeError(f"task {FAILURE_TASK} did not fail on its first attempt")
    if (run_root / "bundle.json").exists():
        raise RuntimeError("failed ledger attempt unexpectedly produced a bundle")

    second = resume_run(run_root)
    if second.status != "completed":
        raise RuntimeError("ledger resume did not complete")
    verification = verify_run(run_root)
    state = read_json_file(run_root / "state.json", label="pipeline state")

    attempts_by_task = {
        task_id: task_state["attempts"]
        for task_id, task_state in state["tasks"].items()
    }
    calls = sum(len(attempts) for attempts in attempts_by_task.values())
    successful_by_task = {
        task_id: sum(1 for attempt in attempts if attempt["status"] == "completed")
        for task_id, attempts in attempts_by_task.items()
    }
    duplicate_successes = sum(max(0, count - 1) for count in successful_by_task.values())
    completed_tasks = sum(
        1 for task_state in state["tasks"].values() if task_state["status"] == "completed"
    )
    failure_attempt = attempts_by_task[f"task-{FAILURE_TASK:02d}"][0]
    final_identity = file_identity(
        suite_root / _task_output(TASK_COUNT),
        label="ledger final output",
    )
    return {
        "strategy": "benchhandoff-resume",
        "subprocess_calls": calls,
        "successful_task_executions": sum(successful_by_task.values()),
        "duplicate_successful_executions": duplicate_successes,
        "final_tasks_completed": completed_tasks,
        "first_failure_code": failure_attempt["return_code"],
        "quarantined_outputs": len(failure_attempt.get("quarantined_outputs", [])),
        "verify_status": verification["status"],
        "final_output": final_identity,
    }


def run() -> dict[str, object]:
    worker = Path(__file__).with_name("pipeline_worker.py")
    with tempfile.TemporaryDirectory(prefix="benchhandoff-pipeline-") as temporary:
        root = Path(temporary)
        naive = _run_naive(root / "naive", worker)
        ledger = _run_ledger(root / "ledger", worker)

        exact_expectations = {
            "naive_subprocess_calls": naive["subprocess_calls"] == EXPECTED_NAIVE_CALLS,
            "ledger_subprocess_calls": ledger["subprocess_calls"] == EXPECTED_LEDGER_CALLS,
            "naive_duplicate_successes": naive["duplicate_successful_executions"] == 5,
            "ledger_duplicate_successes": ledger["duplicate_successful_executions"] == 0,
            "naive_final_tasks": naive["final_tasks_present"] == TASK_COUNT,
            "ledger_final_tasks": ledger["final_tasks_completed"] == TASK_COUNT,
            "ledger_failure_code": ledger["first_failure_code"] == 75,
            "ledger_quarantined_outputs": ledger["quarantined_outputs"] == 1,
            "same_final_output": naive["final_output"] == ledger["final_output"],
            "ledger_verified": ledger["verify_status"] == "verified",
        }
        if not all(exact_expectations.values()):
            raise RuntimeError(f"comparison invariant failed: {exact_expectations}")

        result = {
            "schema_version": 1,
            "benchmark": "synthetic-12-task-restart-vs-resume",
            "task_count": TASK_COUNT,
            "first_failure_task": FAILURE_TASK,
            "naive_restart": naive,
            "ledger_resume": ledger,
            "comparison": {
                "avoided_subprocess_calls": EXPECTED_NAIVE_CALLS - EXPECTED_LEDGER_CALLS,
                "avoided_duplicate_successful_executions": 5,
            },
            "exact_expectations": exact_expectations,
            "timing_claim": "none; this benchmark reports deterministic work counts only",
            "scope": "local synthetic behavior, not production or third-party evidence",
        }
        result.update(benchmark_provenance(REPOSITORY_ROOT))
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        help="optional new file for the raw result; existing files are refused",
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
