from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.engine as engine
from benchhandoff.errors import EvidenceError
from benchhandoff.storage import read_json_file
from tests.workspace_temp import WorkspaceTemporaryDirectory


class ChildControlFailureTests(unittest.TestCase):
    def test_child_is_stopped_if_pid_state_write_fails(self) -> None:
        worker_source = """\
import pathlib
import sys
import time

time.sleep(30)
pathlib.Path(sys.argv[1]).write_text("should-not-complete", encoding="utf-8")
"""
        with WorkspaceTemporaryDirectory(prefix="benchhandoff-control-") as temporary:
            root = Path(temporary)
            suite_root = root / "suite"
            suite_root.mkdir()
            (suite_root / "worker.py").write_text(worker_source, encoding="utf-8")
            suite = suite_root / "suite.toml"
            suite.write_text(
                "\n".join(
                    [
                        "version = 1",
                        'name = "child-control-failure"',
                        "",
                        "[[task]]",
                        'id = "sleeping-child"',
                        "argv = ["
                        + ", ".join(
                            json.dumps(item)
                            for item in (sys.executable, "worker.py", "result.txt")
                        )
                        + "]",
                        'inputs = ["worker.py"]',
                        'outputs = ["result.txt"]',
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            real_write_state = engine._write_state
            failed_pid_write = False

            def fail_pid_write(context: object) -> None:
                nonlocal failed_pid_write
                state = context.state  # type: ignore[attr-defined]
                attempts = state["tasks"]["sleeping-child"]["attempts"]
                child_pid = attempts[-1]["child_pid"] if attempts else None
                if child_pid is not None and not failed_pid_write:
                    failed_pid_write = True
                    raise OSError("synthetic PID state write failure")
                real_write_state(context)

            real_popen = subprocess.Popen
            children: list[subprocess.Popen[bytes]] = []

            def capture_child(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
                child = real_popen(*args, **kwargs)
                children.append(child)
                return child

            with (
                mock.patch.object(engine, "_write_state", side_effect=fail_pid_write),
                mock.patch.object(engine.subprocess, "Popen", side_effect=capture_child),
            ):
                result = engine.start_run(suite, root / "run")

            self.assertEqual(result.status, "failed")
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertFalse((suite_root / "result.txt").exists())
            self.assertFalse((root / "run" / "bundle.json").exists())


    def test_unconfirmed_shutdown_leaves_durable_launch_guard(self) -> None:
        worker_source = """\
import pathlib
pathlib.Path("result.txt").write_text("must-not-run", encoding="utf-8")
"""

        class FakeProcess:
            pid = 424242

        with WorkspaceTemporaryDirectory(prefix="benchhandoff-control-guard-") as temporary:
            root = Path(temporary)
            suite_root = root / "suite"
            suite_root.mkdir()
            (suite_root / "worker.py").write_text(worker_source, encoding="utf-8")
            suite = suite_root / "suite.toml"
            suite.write_text(
                "\n".join(
                    [
                        "version = 1",
                        'name = "child-control-guard"',
                        "",
                        "[[task]]",
                        'id = "guarded-child"',
                        "argv = ["
                        + ", ".join(
                            json.dumps(item)
                            for item in (sys.executable, "worker.py")
                        )
                        + "]",
                        'inputs = ["worker.py"]',
                        'outputs = ["result.txt"]',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            run = root / "run"

            with (
                mock.patch.object(engine.subprocess, "Popen", return_value=FakeProcess()),
                mock.patch.object(engine, "process_start_token", return_value="test:token"),
                mock.patch.object(
                    engine,
                    "_write_state",
                    side_effect=OSError("synthetic child identity state write failure"),
                ),
                mock.patch.object(
                    engine,
                    "stop_process",
                    side_effect=EvidenceError("synthetic unconfirmed shutdown"),
                ),
                self.assertRaisesRegex(
                    EvidenceError,
                    "child identity state write failed and child shutdown could not be confirmed",
                ),
            ):
                engine.start_run(suite, run)

            state_path = run / "state.json"
            state = read_json_file(state_path, label="state")
            attempt = state["tasks"]["guarded-child"]["attempts"][0]
            self.assertTrue(attempt["child_launch_guard"])
            self.assertIsNone(attempt["child_pid"])
            self.assertIsNone(attempt["child_start_token"])
            state_before = state_path.read_bytes()
            events_before = (run / "events.jsonl").read_bytes()

            with self.assertRaisesRegex(EvidenceError, "unresolved child launch guard"):
                engine.resume_run(run)

            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual((run / "events.jsonl").read_bytes(), events_before)
            self.assertFalse((suite_root / "result.txt").exists())

if __name__ == "__main__":
    unittest.main()
