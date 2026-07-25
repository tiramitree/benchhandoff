"""Measure fail-closed behavior when two local processes target one run."""

from __future__ import annotations

import argparse
import json
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
from benchhandoff.errors import EvidenceError  # noqa: E402
from benchhandoff.storage import file_identity, read_json_file  # noqa: E402
from benchhandoff.writer_lock import writer_lock_path  # noqa: E402


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _file_snapshot(root: Path) -> dict[str, dict[str, object]]:
    return {
        path.relative_to(root).as_posix(): file_identity(
            path,
            label=f"contention snapshot {path.name}",
        )
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def _attempt_count(run_root: Path) -> int:
    state = read_json_file(run_root / "state.json", label="contention state")
    return len(state["tasks"]["recover-once"]["attempts"])


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="benchhandoff-writer-contention-") as temporary:
        root = Path(temporary)
        suite_root = root / "suite"
        suite_root.mkdir()
        run_root = root / "evidence"
        worker_source = Path(__file__).with_name("worker.py")
        shutil.copyfile(worker_source, suite_root / "worker.py")
        (suite_root / "input.txt").write_text("contention-input\n", encoding="utf-8")
        suite_text = "\n".join(
            [
                "version = 1",
                'name = "synthetic-writer-contention"',
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
            label="partial output before writer contention",
        )
        attempts_before = _attempt_count(run_root)

        holder_source = """\
import sys
sys.path.insert(0, sys.argv[2])

from benchhandoff.writer_lock import WriterLock

lock = WriterLock.acquire(sys.argv[1])
print("READY", flush=True)
try:
    sys.stdin.readline()
finally:
    lock.release()
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
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        blocked = False
        rejection_detail = ""
        holder_record: dict[str, object] | None = None
        try:
            if holder.stdout is None or holder.stdout.readline().strip() != "READY":
                stderr = "" if holder.stderr is None else holder.stderr.read()
                raise RuntimeError(f"writer-lock holder did not become ready: {stderr}")
            lock_path = writer_lock_path(run_root)
            holder_record = read_json_file(lock_path, label="held writer lock")
            if holder_record["owner_pid"] != holder.pid:
                raise RuntimeError("writer-lock owner PID does not match holder process")
            try:
                resume_run(run_root)
            except EvidenceError as exc:
                rejection_detail = str(exc)
                if "writer lock already exists" not in rejection_detail:
                    raise RuntimeError("unexpected contention rejection") from exc
                blocked = True
        finally:
            if holder.poll() is None:
                if holder.stdin is not None:
                    holder.stdin.write("release\n")
                    holder.stdin.flush()
                    holder.stdin.close()
                holder.wait(timeout=10)
            stderr = "" if holder.stderr is None else holder.stderr.read()
            if holder.returncode != 0:
                raise RuntimeError(
                    f"writer-lock holder exited {holder.returncode}: {stderr}"
                )

        evidence_after = _file_snapshot(run_root)
        partial_after = file_identity(
            partial_output,
            label="partial output after writer contention",
        )
        attempts_after_rejection = _attempt_count(run_root)
        lock_absent_after_holder = not writer_lock_path(run_root).exists()
        if not all(
            (
                blocked,
                evidence_before == evidence_after,
                partial_before == partial_after,
                attempts_before == 1,
                attempts_after_rejection == 1,
                lock_absent_after_holder,
            )
        ):
            raise RuntimeError("writer contention did not fail closed without mutation")

        second = resume_run(run_root)
        verification = verify_run(run_root)
        final_attempts = _attempt_count(run_root)
        if (
            second.status != "completed"
            or verification["status"] != "verified"
            or final_attempts != 2
        ):
            raise RuntimeError("post-contention resume did not complete and verify")

        result: dict[str, object] = {
            "schema_version": 1,
            "benchmark": "synthetic-cooperative-writer-contention",
            "writer_processes": 2,
            "contending_resume_calls": 1,
            "contender_rejected_before_run_evidence_mutation": blocked,
            "run_evidence_files_changed_on_rejection": 0,
            "partial_output_changed_on_rejection": False,
            "attempts_before_contention": attempts_before,
            "attempts_after_rejection": attempts_after_rejection,
            "attempts_after_successful_resume": final_attempts,
            "final_resume_status": second.status,
            "final_verify_status": verification["status"],
            "holder_record_kind": (
                None if holder_record is None else holder_record.get("kind")
            ),
            "lock_absent_after_clean_holder_exit": lock_absent_after_holder,
            "rejection_error_type": "EvidenceError",
            "rejection_reason": "writer-lock-exists",
            "scope": (
                "local cooperative processes on one filesystem; not hostile-writer, "
                "network-filesystem, remote-lease, distributed-scheduler, or "
                "production evidence"
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
