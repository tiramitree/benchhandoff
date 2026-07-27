from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.engine as engine
from benchhandoff.engine import start_run
from benchhandoff.errors import EvidenceError
from benchhandoff.processes import process_liveness
from benchhandoff.storage import read_json_file
from benchhandoff.writer_lock import inspect_writer_lock, recover_writer_lock
from tests.workspace_temp import WorkspaceTemporaryDirectory


_DESCRIPTOR_BYTES = b'{"kind":"synthetic-process-scope-test","version":1}\n'


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _write_suite(root: Path, worker_source: str, grandchild_source: str) -> Path:
    root.mkdir()
    (root / "context.json").write_bytes(_DESCRIPTOR_BYTES)
    (root / "worker.py").write_text(worker_source, encoding="utf-8")
    (root / "grandchild.py").write_text(grandchild_source, encoding="utf-8")
    digest = hashlib.sha256(_DESCRIPTOR_BYTES).hexdigest()
    suite = root / "suite.toml"
    suite.write_text(
        "\n".join(
            [
                "version = 2",
                'name = "process-scope-engine-test"',
                "",
                "[context]",
                'path = "context.json"',
                'media_type = "application/vnd.benchhandoff.test-context+json"',
                f'digest = "sha256:{digest}"',
                f"size = {len(_DESCRIPTOR_BYTES)}",
                "",
                "[[task]]",
                'id = "family"',
                'argv = ["python", "worker.py"]',
                "inputs = ["
                + ", ".join(
                    _toml_string(value)
                    for value in ("context.json", "worker.py", "grandchild.py")
                )
                + "]",
                'outputs = ["result.txt"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return suite


def _wait_for_dead(process_id: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_liveness(process_id) == "dead":
            return True
        time.sleep(0.01)
    return process_liveness(process_id) == "dead"


def _assert_terminal_scope(
    test: unittest.TestCase,
    attempt: dict[str, object],
    *,
    closure: str,
) -> None:
    scope = attempt["process_scope"]
    test.assertIsInstance(scope, dict)
    assert isinstance(scope, dict)
    if os.name == "nt":
        test.assertEqual(scope["mode"], "windows-job")
        test.assertIs(scope["cooperative"], False)
    else:
        test.assertEqual(scope["mode"], "posix-cooperative-process-group")
        test.assertIs(scope["cooperative"], True)
    test.assertEqual(scope["scope_id"], attempt["child_pid"])
    test.assertIs(scope["empty_confirmed"], True)
    test.assertEqual(scope["closure"], closure)


class ProcessScopeEngineTests(unittest.TestCase):
    def test_descendant_after_leader_exit_is_terminated_before_output_hashing(
        self,
    ) -> None:
        worker = """\
import os
import subprocess
import sys

python_executable = (
    os.readlink("/proc/self/exe")
    if sys.platform.startswith("linux")
    else sys.executable
)
subprocess.Popen(
    [python_executable, "grandchild.py"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
"""
        grandchild = """\
import time
from pathlib import Path

time.sleep(1.0)
Path("result.txt").write_text("late output", encoding="utf-8")
Path("late-marker.txt").write_text("escaped", encoding="utf-8")
"""
        with WorkspaceTemporaryDirectory(
            prefix="process-scope-engine-leader-exit-"
        ) as temporary:
            root = Path(temporary)
            suite = _write_suite(root / "suite", worker, grandchild)
            run = root / "run"
            with mock.patch.object(
                engine.shutil,
                "which",
                return_value=sys.executable,
            ):
                result = start_run(suite, run)

            self.assertEqual(result.status, "failed")
            state = read_json_file(run / "state.json", label="state")
            attempt = state["tasks"]["family"]["attempts"][0]
            self.assertEqual(attempt["status"], "failed")
            self.assertIn("scope remained active", attempt["error"])
            self.assertNotIn("verified_outputs", attempt)
            _assert_terminal_scope(self, attempt, closure="terminated")
            self.assertFalse((run / "bundle.json").exists())
            time.sleep(1.1)
            self.assertFalse((suite.parent / "result.txt").exists())
            self.assertFalse((suite.parent / "late-marker.txt").exists())

    def test_state_write_failure_stops_real_child_and_grandchild_scope(
        self,
    ) -> None:
        worker = """\
import json
import os
import subprocess
import sys
import time
from pathlib import Path

python_executable = (
    os.readlink("/proc/self/exe")
    if sys.platform.startswith("linux")
    else sys.executable
)
grandchild = subprocess.Popen(
    [python_executable, "grandchild.py"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path("family.json").write_text(
    json.dumps({"child": os.getpid(), "grandchild": grandchild.pid}),
    encoding="utf-8",
)
time.sleep(120)
"""
        grandchild = """\
import time
from pathlib import Path

time.sleep(1.0)
Path("result.txt").write_text("late output", encoding="utf-8")
"""
        with WorkspaceTemporaryDirectory(
            prefix="process-scope-engine-state-failure-"
        ) as temporary:
            root = Path(temporary)
            suite = _write_suite(root / "suite", worker, grandchild)
            run = root / "run"
            family_path = suite.parent / "family.json"
            real_write_state = engine._write_state

            def fail_identity_write(context: object) -> None:
                state = context.state  # type: ignore[attr-defined]
                attempts = state["tasks"]["family"]["attempts"]
                if (
                    attempts
                    and attempts[-1]["child_pid"] is not None
                    and attempts[-1]["status"] == "running"
                ):
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline and not family_path.is_file():
                        time.sleep(0.01)
                    if not family_path.is_file():
                        raise AssertionError("real child did not report its grandchild")
                    raise OSError("synthetic child-identity state write failure")
                real_write_state(context)  # type: ignore[arg-type]

            with (
                mock.patch.object(
                    engine.shutil,
                    "which",
                    return_value=sys.executable,
                ),
                mock.patch.object(
                    engine,
                    "_write_state",
                    side_effect=fail_identity_write,
                ),
            ):
                result = start_run(suite, run)

            self.assertEqual(result.status, "failed")
            family = json.loads(family_path.read_text(encoding="utf-8"))
            self.assertTrue(_wait_for_dead(family["child"]))
            self.assertTrue(_wait_for_dead(family["grandchild"]))
            state = read_json_file(run / "state.json", label="state")
            attempt = state["tasks"]["family"]["attempts"][0]
            self.assertEqual(attempt["status"], "failed")
            self.assertIn("unable to persist child identity", attempt["error"])
            _assert_terminal_scope(self, attempt, closure="terminated")
            time.sleep(1.1)
            self.assertFalse((suite.parent / "result.txt").exists())
            self.assertFalse((run / "bundle.json").exists())

    def test_runner_hard_exit_obeys_platform_crash_boundary(self) -> None:
        worker = """\
import json
import os
import subprocess
import sys
import time
from pathlib import Path

python_executable = (
    os.readlink("/proc/self/exe")
    if sys.platform.startswith("linux")
    else sys.executable
)
grandchild = subprocess.Popen(
    [python_executable, "grandchild.py"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path("family.json").write_text(
    json.dumps({"child": os.getpid(), "grandchild": grandchild.pid}),
    encoding="utf-8",
)
time.sleep(120)
"""
        grandchild = """\
import time
from pathlib import Path

time.sleep(5.0)
Path("result.txt").write_text("late output", encoding="utf-8")
"""
        with WorkspaceTemporaryDirectory(
            prefix="process-scope-engine-hard-exit-"
        ) as temporary:
            root = Path(temporary)
            suite = _write_suite(root / "suite", worker, grandchild)
            run = root / "run"
            family_path = suite.parent / "family.json"
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(SOURCE_ROOT)
            driver = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "benchhandoff",
                    "start",
                    str(suite),
                    "--run-dir",
                    str(run),
                ],
                cwd=REPOSITORY_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                close_fds=True,
            )
            family: dict[str, int] = {}
            try:
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    if family_path.is_file() and (run / "state.json").is_file():
                        state = read_json_file(run / "state.json", label="state")
                        attempts = state["tasks"]["family"]["attempts"]
                        if (
                            attempts
                            and attempts[-1]["child_pid"] is not None
                            and attempts[-1]["child_launch_guard"] is False
                        ):
                            family = json.loads(
                                family_path.read_text(encoding="utf-8")
                            )
                            break
                    if driver.poll() is not None:
                        break
                    time.sleep(0.01)
                self.assertTrue(family, "runner did not persist an active family")
                driver.kill()
                driver.wait(timeout=5)

                state = read_json_file(run / "state.json", label="state")
                attempt = state["tasks"]["family"]["attempts"][0]
                self.assertEqual(attempt["status"], "running")
                self.assertFalse(attempt["process_scope"]["empty_confirmed"])
                self.assertEqual(attempt["process_scope"]["closure"], "active")

                if os.name == "nt":
                    self.assertTrue(_wait_for_dead(family["child"]))
                    self.assertTrue(_wait_for_dead(family["grandchild"]))
                else:
                    os.kill(family["child"], signal.SIGKILL)
                    self.assertTrue(_wait_for_dead(family["child"]))
                    self.assertEqual(
                        process_liveness(family["grandchild"]),
                        "alive",
                    )
                    decision = inspect_writer_lock(run)
                    self.assertEqual(decision["action"], "recover-orphan")
                    recover_writer_lock(
                        run,
                        expected_decision_sha256=decision["decision_sha256"],
                    )
                    with self.assertRaisesRegex(
                        EvidenceError,
                        "cooperative process group is still active",
                    ):
                        engine.resume_run(run)
                    os.killpg(family["child"], signal.SIGKILL)
                    self.assertTrue(_wait_for_dead(family["grandchild"]))

                time.sleep(0.2)
                self.assertFalse((suite.parent / "result.txt").exists())
                self.assertFalse((run / "bundle.json").exists())
            finally:
                if driver.poll() is None:
                    driver.kill()
                    driver.wait(timeout=5)
                if os.name == "posix" and family:
                    try:
                        os.killpg(family["child"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
