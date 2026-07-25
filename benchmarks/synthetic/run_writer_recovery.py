"""Measure evidence-bound recovery of one hard-exit writer-lock orphan."""

from __future__ import annotations

import argparse
import hashlib
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
from benchhandoff.engine import (  # noqa: E402
    inspect_resume,
    resume_run,
    start_run,
    verify_run,
)
from benchhandoff.storage import (  # noqa: E402
    canonical_json_bytes,
    file_identity,
    read_json_file,
    read_regular_bytes,
)
from benchhandoff.writer_lock import (  # noqa: E402
    inspect_writer_lock,
    recover_writer_lock,
    writer_lock_path,
)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _file_snapshot(root: Path) -> dict[str, dict[str, object]]:
    return {
        path.relative_to(root).as_posix(): file_identity(
            path,
            label=f"writer-recovery snapshot {path.name}",
        )
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def _attempt_count(run_root: Path) -> int:
    state = read_json_file(run_root / "state.json", label="writer-recovery state")
    return len(state["tasks"]["recover-once"]["attempts"])


def _decision_digest_is_valid(decision: dict[str, object]) -> bool:
    body = dict(decision)
    recorded = body.pop("decision_sha256", None)
    return recorded == hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="benchhandoff-writer-recovery-") as temporary:
        root = Path(temporary)
        suite_root = root / "suite"
        suite_root.mkdir()
        run_root = root / "evidence"
        worker_source = Path(__file__).with_name("worker.py")
        shutil.copyfile(worker_source, suite_root / "worker.py")
        (suite_root / "input.txt").write_text(
            "writer-recovery-input\n",
            encoding="utf-8",
        )
        suite_text = "\n".join(
            [
                "version = 1",
                'name = "synthetic-writer-recovery"',
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
        suite_file = suite_root / "suite.toml"
        suite_file.write_text(suite_text, encoding="utf-8")

        first = start_run(suite_file, run_root)
        if first.status != "failed":
            raise RuntimeError(f"synthetic first attempt did not fail: {first.status}")
        evidence_before = _file_snapshot(run_root)
        partial_output = suite_root / "result.json"
        partial_before = file_identity(
            partial_output,
            label="partial output before writer-lock recovery",
        )
        attempts_before = _attempt_count(run_root)

        holder_source = """\
import os
import sys
sys.path.insert(0, sys.argv[2])

from benchhandoff.writer_lock import WriterLock

WriterLock.acquire(sys.argv[1])
os._exit(0)
"""
        holder = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                holder_source,
                str(run_root),
                str(SOURCE_ROOT),
            ],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        holder_pid = holder.pid
        holder_stdout, holder_stderr = holder.communicate(timeout=10)
        if holder.returncode != 0:
            raise RuntimeError(
                f"writer-lock holder exited {holder.returncode}: "
                f"{holder_stdout}{holder_stderr}"
            )

        lock_path = writer_lock_path(run_root)
        original_lock = read_regular_bytes(
            lock_path,
            label="orphan writer lock",
            max_bytes=4096,
        )
        lock_record = read_json_file(lock_path, label="orphan writer-lock record")
        if lock_record["owner_pid"] != holder_pid:
            raise RuntimeError("orphan writer-lock owner PID does not match holder")

        first_decision = inspect_writer_lock(run_root)
        second_decision = inspect_writer_lock(run_root)
        evidence_after_inspection = _file_snapshot(run_root)
        partial_after_inspection = file_identity(
            partial_output,
            label="partial output after writer-lock inspection",
        )
        if not all(
            (
                first_decision == second_decision,
                first_decision["action"] == "recover-orphan",
                first_decision["reason"] == "owner-dead",
                _decision_digest_is_valid(first_decision),
                evidence_before == evidence_after_inspection,
                partial_before == partial_after_inspection,
                read_regular_bytes(
                    lock_path,
                    label="orphan writer lock after inspection",
                    max_bytes=4096,
                )
                == original_lock,
            )
        ):
            raise RuntimeError("writer-lock inspection was not stable and read-only")

        recovered = recover_writer_lock(
            run_root,
            expected_decision_sha256=first_decision["decision_sha256"],
        )
        tombstone = Path(recovered["tombstone"]["path"])
        evidence_after_recovery = _file_snapshot(run_root)
        partial_after_recovery = file_identity(
            partial_output,
            label="partial output after writer-lock recovery",
        )
        attempts_after_recovery = _attempt_count(run_root)
        if not all(
            (
                recovered["status"] == "recovered",
                recovered["reason"] == "owner-dead",
                not lock_path.exists(),
                tombstone.is_file(),
                tombstone.read_bytes() == original_lock,
                evidence_before == evidence_after_recovery,
                partial_before == partial_after_recovery,
                attempts_before == 1,
                attempts_after_recovery == 1,
            )
        ):
            raise RuntimeError("writer-lock recovery changed run evidence or lost the lock")

        resume_decision = inspect_resume(run_root)
        second = resume_run(
            run_root,
            expected_decision_sha256=resume_decision["decision_sha256"],
        )
        verification = verify_run(run_root)
        final_attempts = _attempt_count(run_root)
        tombstone_preserved = tombstone.read_bytes() == original_lock
        if (
            second.status != "completed"
            or verification["status"] != "verified"
            or final_attempts != 2
            or not tombstone_preserved
        ):
            raise RuntimeError("post-lock-recovery resume did not complete and verify")

        result: dict[str, object] = {
            "schema_version": 1,
            "benchmark": "synthetic-orphan-writer-lock-recovery",
            "writer_processes": 2,
            "hard_exit_holder_return_code": holder.returncode,
            "writer_lock_inspections": 2,
            "inspection_decisions_identical": first_decision == second_decision,
            "decision_digest_valid": _decision_digest_is_valid(first_decision),
            "decision_action": first_decision["action"],
            "decision_reason": first_decision["reason"],
            "run_evidence_files_changed_by_inspection": 0,
            "run_evidence_files_changed_by_lock_recovery": 0,
            "partial_output_changed_by_lock_recovery": False,
            "attempts_before_lock_recovery": attempts_before,
            "attempts_after_lock_recovery": attempts_after_recovery,
            "attempts_after_bound_resume": final_attempts,
            "source_lock_absent_after_recovery": not lock_path.exists(),
            "tombstone_preserved_after_resume": tombstone_preserved,
            "tombstone_identity": recovered["tombstone"]["identity"],
            "kernel_guard": (
                "windows-named-mutex" if os.name == "nt" else "posix-flock"
            ),
            "final_resume_status": second.status,
            "final_verify_status": verification["status"],
            "scope": (
                "local synthetic orphan-lock control-plane recovery; not safe-child-"
                "retry, hostile-writer, network-filesystem, remote-lease, distributed-"
                "scheduler, production, or external-adoption evidence"
            ),
            "timing_claim": "none; this benchmark reports deterministic state counts",
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
        with arguments.output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
