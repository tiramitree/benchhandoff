from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.processes as processes
from benchhandoff.errors import EvidenceError
from benchhandoff.processes import ProcessScope, process_liveness


_FAMILY_SCRIPT = """
import json
import os
import subprocess
import sys
import time
from pathlib import Path

grandchild = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path(sys.argv[1]).write_text(
    json.dumps({"child": os.getpid(), "grandchild": grandchild.pid}),
    encoding="utf-8",
)
time.sleep(120)
"""


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _process_is_terminal(process_id: int) -> bool:
    if process_liveness(process_id) == "dead":
        return True
    if os.name == "posix":
        try:
            process_stat = Path(f"/proc/{process_id}/stat").read_text(encoding="ascii")
        except (FileNotFoundError, PermissionError, OSError, UnicodeError):
            return False
        closing_parenthesis = process_stat.rfind(")")
        if closing_parenthesis >= 0:
            fields_after_comm = process_stat[closing_parenthesis + 2 :].split()
            return bool(fields_after_comm) and fields_after_comm[0] in {"Z", "X"}
    return False


class ProcessScopeTests(unittest.TestCase):
    def test_unsupported_posix_platform_refuses_before_popen(self) -> None:
        with (
            mock.patch.object(processes.os, "name", "posix"),
            mock.patch.object(processes.sys, "platform", "darwin"),
            mock.patch.object(processes.subprocess, "Popen") as popen,
            self.assertRaisesRegex(
                EvidenceError,
                "supported only on Windows and Linux",
            ),
        ):
            ProcessScope.start([sys.executable, "-c", "pass"])

        popen.assert_not_called()

    def test_linux_without_proc_refuses_before_popen(self) -> None:
        fake_path = mock.Mock()
        fake_path.return_value.is_dir.return_value = False
        with (
            mock.patch.object(processes.os, "name", "posix"),
            mock.patch.object(processes.sys, "platform", "linux"),
            mock.patch.object(processes, "Path", fake_path),
            mock.patch.object(processes.subprocess, "Popen") as popen,
            self.assertRaisesRegex(
                EvidenceError,
                "require readable /proc process identities",
            ),
        ):
            ProcessScope.start([sys.executable, "-c", "pass"])

        popen.assert_not_called()

    def test_linux_membership_read_denial_is_unknown_not_empty(self) -> None:
        numeric_entry = Path("/proc/123")
        with (
            mock.patch.object(
                Path,
                "iterdir",
                return_value=iter((numeric_entry,)),
            ),
            mock.patch.object(
                Path,
                "read_text",
                side_effect=PermissionError("synthetic denial"),
            ),
            self.assertRaisesRegex(
                EvidenceError,
                "unable to inspect process-group member 123",
            ),
        ):
            processes._linux_process_group_members(123)

        fake_path = mock.Mock()
        fake_path.return_value.is_dir.return_value = True
        with (
            mock.patch.object(processes.os, "name", "posix"),
            mock.patch.object(processes, "Path", fake_path),
            mock.patch.object(
                processes,
                "_linux_process_group_members",
                side_effect=EvidenceError("synthetic denial"),
            ),
        ):
            self.assertEqual(
                processes.process_scope_liveness(
                    "posix-cooperative-process-group",
                    123,
                ),
                "unknown",
            )

    def test_linux_disappearing_member_can_be_treated_as_absent(self) -> None:
        numeric_entry = Path("/proc/123")
        with (
            mock.patch.object(
                Path,
                "iterdir",
                return_value=iter((numeric_entry,)),
            ),
            mock.patch.object(
                Path,
                "read_text",
                side_effect=FileNotFoundError("synthetic exit race"),
            ),
        ):
            self.assertEqual(processes._linux_process_group_members(123), ())

    def _start_real_family(
        self,
        marker: Path,
    ) -> tuple[ProcessScope, dict[str, int]]:
        scope = ProcessScope.start(
            [sys.executable, "-c", _FAMILY_SCRIPT, str(marker)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            marker_ready = _wait_until(
                lambda: marker.is_file() or scope.poll() is not None,
            )
            self.assertTrue(marker_ready, "child did not report its grandchild")
            self.assertIsNone(scope.poll(), "scope leader exited before test control")
            process_ids = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(process_ids["child"], scope.pid)
            self.assertNotEqual(process_ids["child"], process_ids["grandchild"])
            expected = {process_ids["child"], process_ids["grandchild"]}
            self.assertTrue(
                _wait_until(
                    lambda: expected.issubset(set(scope.active_process_ids())),
                ),
                "scope membership never included both child and grandchild",
            )
            return scope, process_ids
        except BaseException:
            with contextlib.suppress(Exception):
                scope.close()
            raise

    def _assert_real_processes_dead(self, process_ids: dict[str, int]) -> None:
        for label, process_id in process_ids.items():
            self.assertTrue(
                _wait_until(
                    lambda process_id=process_id: _process_is_terminal(process_id),
                ),
                f"{label} process {process_id} remained live",
            )

    def test_real_child_grandchild_terminate_and_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker = Path(temporary_directory) / "family.json"
            scope, process_ids = self._start_real_family(marker)
            try:
                if os.name == "nt":
                    self.assertEqual(scope.mode, "windows-job")
                    self.assertFalse(scope.cooperative)
                else:
                    self.assertEqual(
                        scope.mode,
                        "posix-cooperative-process-group",
                    )
                    self.assertTrue(scope.cooperative)

                return_code = scope.terminate(
                    terminate_timeout=0.5,
                    kill_timeout=5.0,
                )
                self.assertIsInstance(return_code, int)
                self.assertTrue(scope.wait_empty(0))
                self.assertEqual(scope.active_process_ids(), ())
                self._assert_real_processes_dead(process_ids)
            finally:
                scope.close()

    def test_close_cleans_an_active_real_process_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker = Path(temporary_directory) / "family.json"
            scope, process_ids = self._start_real_family(marker)
            scope.close()
            scope.close()  # Idempotent release is safe for cleanup paths.
            self.assertEqual(scope.active_process_ids(), ())
            self._assert_real_processes_dead(process_ids)

    def test_close_reaps_a_naturally_exited_scope_leader(self) -> None:
        scope = ProcessScope.start(
            [sys.executable, "-c", "pass"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(
                _wait_until(lambda: not scope.active_process_ids()),
                "naturally exited process remained active",
            )
            scope.close()
            self.assertIsNotNone(scope.process.returncode)
        finally:
            scope.close()


if __name__ == "__main__":
    unittest.main()
