from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.processes as processes
from benchhandoff.processes import process_liveness, process_start_token, stop_process


class ProcessGuardTests(unittest.TestCase):
    def test_process_start_token_is_stable_for_current_process(self) -> None:
        first = process_start_token(os.getpid())
        if first is None:
            self.skipTest("platform does not expose a supported process start token")
        self.assertTrue(first.startswith(("windows:", "linux:")))
        self.assertEqual(process_start_token(os.getpid()), first)

    def test_liveness_probe_is_tri_state_and_does_not_signal_process(self) -> None:
        self.assertEqual(process_liveness(os.getpid()), "alive")
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertEqual(process_liveness(child.pid), "alive")
            self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertEqual(process_liveness(child.pid), "dead")
        self.assertEqual(process_liveness(None), "dead")
        self.assertEqual(process_liveness(-1), "dead")

    def test_probe_failure_is_unknown_not_dead(self) -> None:
        if os.name == "nt":
            with mock.patch.object(
                processes,
                "_windows_process_liveness",
                return_value="unknown",
            ):
                self.assertEqual(process_liveness(424242), "unknown")
        elif sys.platform.startswith("linux") and Path("/proc").is_dir():
            with mock.patch.object(
                processes,
                "_linux_process_liveness",
                return_value="unknown",
            ):
                self.assertEqual(process_liveness(424242), "unknown")
        else:
            with mock.patch.object(processes.os, "kill", side_effect=PermissionError):
                self.assertEqual(process_liveness(424242), "unknown")

    def test_linux_liveness_treats_zombie_as_terminal_and_parse_failure_as_unknown(
        self,
    ) -> None:
        for state in ("Z", "X"):
            with (
                self.subTest(state=state),
                mock.patch.object(
                    Path,
                    "read_text",
                    return_value=f"123 (synthetic worker) {state} 1 123 123 0",
                ),
            ):
                self.assertEqual(processes._linux_process_liveness(123), "dead")
        with mock.patch.object(
            Path,
            "read_text",
            side_effect=FileNotFoundError("synthetic exit race"),
        ):
            self.assertEqual(processes._linux_process_liveness(123), "dead")
        with mock.patch.object(Path, "read_text", return_value="malformed"):
            self.assertEqual(processes._linux_process_liveness(123), "unknown")
        with mock.patch.object(
            Path,
            "read_text",
            side_effect=PermissionError("synthetic denial"),
        ):
            self.assertEqual(processes._linux_process_liveness(123), "unknown")

    def test_stop_process_uses_bounded_terminate_then_kill(self) -> None:
        class StubbornProcess:
            pid = 12345

            def __init__(self) -> None:
                self.actions: list[str] = []
                self.waits = 0
                self.done = False

            def poll(self) -> int | None:
                return -9 if self.done else None

            def terminate(self) -> None:
                self.actions.append("terminate")

            def kill(self) -> None:
                self.actions.append("kill")
                self.done = True

            def wait(self, timeout: float | None = None) -> int:
                self.actions.append(f"wait:{timeout}")
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired("synthetic", timeout)
                return -9

        process = StubbornProcess()
        self.assertEqual(stop_process(process, terminate_timeout=0.01, kill_timeout=0.02), -9)
        self.assertEqual(
            process.actions,
            ["terminate", "wait:0.01", "kill", "wait:0.02"],
        )


if __name__ == "__main__":
    unittest.main()
