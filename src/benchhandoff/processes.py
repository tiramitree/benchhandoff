"""Conservative child-process liveness and shutdown controls."""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any, Literal, Protocol

from benchhandoff.errors import EvidenceError

ProcessLiveness = Literal["alive", "dead", "unknown"]
ProcessScopeMode = Literal["windows-job", "posix-cooperative-process-group"]


class ProcessScopeLaunchError(EvidenceError):
    """A process-scope launch failure whose cleanup was confirmed."""


class ControllableProcess(Protocol):
    """The small ``subprocess.Popen`` surface used by the shutdown guard."""

    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class _ProcessScopeBackend(Protocol):
    """Platform lifetime primitive owned by :class:`ProcessScope`."""

    mode: ProcessScopeMode
    cooperative: bool

    def active_process_ids(self) -> tuple[int, ...]: ...

    def terminate(
        self,
        process: subprocess.Popen[Any],
        *,
        terminate_timeout: float,
        kill_timeout: float,
    ) -> int: ...

    def wait_empty(
        self,
        process: subprocess.Popen[Any],
        *,
        timeout: float,
    ) -> bool: ...

    def close(self) -> None: ...


def _validate_timeout(value: float, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{label} must be a non-negative number")
    return float(value)


def process_scope_policy() -> dict[str, bool | str]:
    """Describe the process-family backend selected on this platform."""

    if os.name == "nt":
        return {"mode": "windows-job", "cooperative": False}
    if os.name == "posix" and sys.platform.startswith("linux"):
        if not Path("/proc").is_dir() or process_start_token(os.getpid()) is None:
            raise EvidenceError(
                "Linux process scopes require readable /proc process identities"
            )
        try:
            _linux_process_group_members(os.getpgrp())
        except (AttributeError, OSError, EvidenceError) as exc:
            raise EvidenceError(
                "Linux process scopes require readable /proc process groups"
            ) from exc
        return {
            "mode": "posix-cooperative-process-group",
            "cooperative": True,
        }
    raise EvidenceError(
        "process scopes are supported only on Windows and Linux"
    )


def require_process_identity_support() -> None:
    """Reject child execution when stable PID identity is unavailable."""

    if process_start_token(os.getpid()) is None:
        raise EvidenceError(
            "child execution requires a stable Windows or Linux process identity"
        )


def _wait_for_empty(
    process: subprocess.Popen[Any],
    *,
    timeout: float,
    active_process_ids: Callable[[], tuple[int, ...]],
) -> bool:
    """Poll a platform membership query while also reaping the direct child."""

    deadline = time.monotonic() + timeout
    while True:
        # Popen.poll() reaps a completed direct child on POSIX. Without this,
        # its zombie can keep an otherwise terminated process group visible.
        process.poll()
        if not active_process_ids():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


def _confirmed_return_code(
    process: subprocess.Popen[Any],
    *,
    timeout: float,
) -> int:
    return_code = process.poll()
    if return_code is None:
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise EvidenceError(
                f"scope leader process {process.pid} did not reach a terminal status"
            ) from exc
        except OSError as exc:
            raise EvidenceError(
                f"unable to confirm scope leader process {process.pid}: {exc}"
            ) from exc
    return return_code


def _windows_error(operation: str) -> OSError:
    error = ctypes.get_last_error()
    return OSError(error, f"{operation} failed")


class _WindowsJobScope:
    """One non-inheritable Job handle with kill-on-last-handle-close."""

    mode: ProcessScopeMode = "windows-job"
    cooperative = False

    _JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _ERROR_MORE_DATA = 234

    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def create(cls) -> _WindowsJobScope:
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        create_job.restype = wintypes.HANDLE
        set_job_information = kernel32.SetInformationJobObject
        set_job_information.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        set_job_information.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = create_job(None, None)
        if not handle:
            raise _windows_error("CreateJobObjectW")
        handle_value = int(handle)
        information = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = (
            cls._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not set_job_information(
            handle,
            cls._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = _windows_error("SetInformationJobObject")
            close_handle(handle)
            raise error
        return cls(handle_value)

    def _open_handle(self) -> int:
        if self._handle is None:
            raise EvidenceError("Windows process scope is already closed")
        return self._handle

    def assign(self, process_id: int) -> None:
        from ctypes import wintypes

        process_set_quota = 0x0100
        process_terminate = 0x0001
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        assign_process = kernel32.AssignProcessToJobObject
        assign_process.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        assign_process.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        process_handle = open_process(
            process_set_quota | process_terminate,
            False,
            process_id,
        )
        if not process_handle:
            raise _windows_error("OpenProcess for Job assignment")
        try:
            if not assign_process(self._open_handle(), process_handle):
                raise _windows_error("AssignProcessToJobObject")
        finally:
            close_handle(process_handle)

    def active_process_ids(self) -> tuple[int, ...]:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        query_job = kernel32.QueryInformationJobObject
        query_job.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        query_job.restype = wintypes.BOOL

        capacity = 8
        while True:
            process_id_array = ctypes.c_size_t * capacity

            class JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
                _fields_ = [
                    ("NumberOfAssignedProcesses", wintypes.DWORD),
                    ("NumberOfProcessIdsInList", wintypes.DWORD),
                    ("ProcessIdList", process_id_array),
                ]

            information = JOBOBJECT_BASIC_PROCESS_ID_LIST()
            returned_length = wintypes.DWORD()
            if query_job(
                self._open_handle(),
                self._JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                ctypes.byref(information),
                ctypes.sizeof(information),
                ctypes.byref(returned_length),
            ):
                listed = int(information.NumberOfProcessIdsInList)
                return tuple(
                    sorted(
                        int(information.ProcessIdList[index])
                        for index in range(listed)
                    )
                )
            error = ctypes.get_last_error()
            if error != self._ERROR_MORE_DATA:
                raise _windows_error("QueryInformationJobObject")
            capacity = max(capacity * 2, int(information.NumberOfAssignedProcesses), 1)

    def wait_empty(
        self,
        process: subprocess.Popen[Any],
        *,
        timeout: float,
    ) -> bool:
        return _wait_for_empty(
            process,
            timeout=timeout,
            active_process_ids=self.active_process_ids,
        )

    def terminate(
        self,
        process: subprocess.Popen[Any],
        *,
        terminate_timeout: float,
        kill_timeout: float,
    ) -> int:
        del terminate_timeout  # Job termination is immediate, not a graceful signal.
        from ctypes import wintypes

        if self.active_process_ids():
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            terminate_job = kernel32.TerminateJobObject
            terminate_job.argtypes = (wintypes.HANDLE, wintypes.UINT)
            terminate_job.restype = wintypes.BOOL
            if not terminate_job(self._open_handle(), 1):
                raise EvidenceError(
                    f"unable to terminate Windows Job for process {process.pid}: "
                    f"{_windows_error('TerminateJobObject')}"
                )
        if not self.wait_empty(process, timeout=kill_timeout):
            remaining = self.active_process_ids()
            raise EvidenceError(
                "Windows Job did not become empty after termination"
                f"; remaining process ids: {remaining}"
            )
        return _confirmed_return_code(process, timeout=kill_timeout)

    def close(self) -> None:
        if self._handle is None:
            return
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = self._handle
        if not close_handle(handle):
            raise EvidenceError(
                f"unable to close Windows Job handle: {_windows_error('CloseHandle')}"
            )
        self._handle = None

    def __del__(self) -> None:
        # Last-resort handle release. KILL_ON_JOB_CLOSE makes an abandoned
        # scope fail closed if explicit cleanup was skipped during unwinding.
        with contextlib.suppress(Exception):
            self.close()


def _resume_only_thread(process_id: int) -> None:
    """Resume the sole primary thread of a CREATE_SUSPENDED process."""

    from ctypes import wintypes

    th32cs_snapthread = 0x00000004
    thread_suspend_resume = 0x0002
    invalid_handle_value = ctypes.c_void_p(-1).value

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    create_snapshot.restype = wintypes.HANDLE
    thread_first = kernel32.Thread32First
    thread_first.argtypes = (wintypes.HANDLE, ctypes.POINTER(THREADENTRY32))
    thread_first.restype = wintypes.BOOL
    thread_next = kernel32.Thread32Next
    thread_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(THREADENTRY32))
    thread_next.restype = wintypes.BOOL
    open_thread = kernel32.OpenThread
    open_thread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_thread.restype = wintypes.HANDLE
    resume_thread = kernel32.ResumeThread
    resume_thread.argtypes = (wintypes.HANDLE,)
    resume_thread.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    snapshot = create_snapshot(th32cs_snapthread, 0)
    if int(snapshot) == invalid_handle_value:
        raise _windows_error("CreateToolhelp32Snapshot")
    thread_ids: list[int] = []
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        if thread_first(snapshot, ctypes.byref(entry)):
            while True:
                if int(entry.th32OwnerProcessID) == process_id:
                    thread_ids.append(int(entry.th32ThreadID))
                if not thread_next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        close_handle(snapshot)

    if len(thread_ids) != 1:
        raise EvidenceError(
            "CREATE_SUSPENDED process did not expose exactly one resumable "
            f"primary thread; process={process_id}, thread_count={len(thread_ids)}"
        )
    thread_handle = open_thread(thread_suspend_resume, False, thread_ids[0])
    if not thread_handle:
        raise _windows_error("OpenThread for ResumeThread")
    try:
        previous_suspend_count = int(resume_thread(thread_handle))
        if previous_suspend_count == 0xFFFFFFFF:
            raise _windows_error("ResumeThread")
        if previous_suspend_count != 1:
            raise EvidenceError(
                "CREATE_SUSPENDED primary thread had an unexpected suspend count "
                f"{previous_suspend_count}"
            )
    finally:
        close_handle(thread_handle)


def _linux_process_group_members(process_group_id: int) -> tuple[int, ...]:
    members: list[int] = []
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as exc:
        raise EvidenceError(f"unable to enumerate /proc process groups: {exc}") from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            process_stat = (entry / "stat").read_text(encoding="ascii")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as exc:
            raise EvidenceError(
                f"unable to inspect process-group member {entry.name}: {exc}"
            ) from exc
        closing_parenthesis = process_stat.rfind(")")
        if closing_parenthesis < 0:
            raise EvidenceError(
                f"unable to parse process-group member {entry.name}: "
                "missing command terminator"
            )
        fields_after_comm = process_stat[closing_parenthesis + 2 :].split()
        # Fields 3 and 5 (state and pgrp) are indexes 0 and 2 here.
        if len(fields_after_comm) <= 2:
            raise EvidenceError(
                f"unable to parse process-group member {entry.name}: "
                "truncated stat record"
            )
        try:
            group = int(fields_after_comm[2])
        except ValueError as exc:
            raise EvidenceError(
                f"unable to parse process-group member {entry.name}: "
                "invalid process group"
            ) from exc
        if group == process_group_id and fields_after_comm[0] not in {"Z", "X"}:
            members.append(int(entry.name))
    return tuple(sorted(members))


def _ps_process_group_members(process_group_id: int) -> tuple[int, ...]:
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,pgid=,state="],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceError(f"unable to enumerate POSIX process groups: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        raise EvidenceError(
            "unable to enumerate POSIX process groups"
            + (f": {detail}" if detail else "")
        )
    members: list[int] = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            process_id = int(fields[0])
            group = int(fields[1])
        except ValueError:
            continue
        if group == process_group_id and not fields[2].startswith(("Z", "X")):
            members.append(process_id)
    return tuple(sorted(members))


def process_scope_liveness(
    mode: ProcessScopeMode,
    scope_id: int | None,
) -> ProcessLiveness:
    """Classify a persisted cooperative process group after runner exit.

    Anonymous Windows Job objects cannot be reopened from persisted evidence;
    KILL_ON_JOB_CLOSE supplies their crash cleanup instead. Callers therefore
    use this probe only for the POSIX cooperative backend.
    """

    if (
        mode != "posix-cooperative-process-group"
        or os.name != "posix"
        or not isinstance(scope_id, int)
        or isinstance(scope_id, bool)
        or scope_id <= 0
    ):
        return "unknown"
    try:
        members = (
            _linux_process_group_members(scope_id)
            if Path("/proc").is_dir()
            else _ps_process_group_members(scope_id)
        )
    except EvidenceError:
        return "unknown"
    return "alive" if members else "dead"


class _PosixProcessGroupScope:
    """Cooperative session/process-group cleanup, not a security boundary.

    Descendants can leave this scope with ``setsid()`` or ``setpgid()``. The
    backend only controls processes that continue to belong to the launch
    process group.
    """

    mode: ProcessScopeMode = "posix-cooperative-process-group"
    cooperative = True

    def __init__(self, process_group_id: int) -> None:
        self._process_group_id = process_group_id
        self._closed = False

    def active_process_ids(self) -> tuple[int, ...]:
        if self._closed:
            return ()
        if Path("/proc").is_dir():
            return _linux_process_group_members(self._process_group_id)
        return _ps_process_group_members(self._process_group_id)

    def wait_empty(
        self,
        process: subprocess.Popen[Any],
        *,
        timeout: float,
    ) -> bool:
        return _wait_for_empty(
            process,
            timeout=timeout,
            active_process_ids=self.active_process_ids,
        )

    def _signal_group(self, signal_number: int) -> None:
        try:
            os.killpg(self._process_group_id, signal_number)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise EvidenceError(
                f"permission denied signalling process group {self._process_group_id}"
            ) from exc
        except OSError as exc:
            raise EvidenceError(
                f"unable to signal process group {self._process_group_id}: {exc}"
            ) from exc

    def terminate(
        self,
        process: subprocess.Popen[Any],
        *,
        terminate_timeout: float,
        kill_timeout: float,
    ) -> int:
        if self.active_process_ids():
            self._signal_group(signal.SIGTERM)
        if not self.wait_empty(process, timeout=terminate_timeout):
            self._signal_group(signal.SIGKILL)
            if not self.wait_empty(process, timeout=kill_timeout):
                remaining = self.active_process_ids()
                raise EvidenceError(
                    "POSIX process group did not become empty after TERM and KILL"
                    f"; remaining process ids: {remaining}"
                )
        return _confirmed_return_code(process, timeout=kill_timeout)

    def close(self) -> None:
        self._closed = True


class ProcessScope:
    """Own one launched process family and bound its cleanup.

    This is a process-lifetime primitive, not a sandbox. Windows uses a Job
    object assigned before the primary thread can run. POSIX uses a cooperative
    process group whose descendants can deliberately leave the group.
    """

    def __init__(
        self,
        process: subprocess.Popen[Any],
        backend: _ProcessScopeBackend,
    ) -> None:
        self.process = process
        self._backend = backend
        self._closed = False

    @classmethod
    def start(
        cls,
        args: Sequence[str | os.PathLike[str]],
        *,
        executable: str | os.PathLike[str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        stdin: int | IO[Any] | None = None,
        stdout: int | IO[Any] | None = None,
        stderr: int | IO[Any] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcessScope:
        """Launch a child inside a new platform process scope."""

        policy = process_scope_policy()
        popen_arguments: dict[str, Any] = {
            "executable": executable,
            "cwd": cwd,
            "stdin": stdin,
            "stdout": stdout,
            "stderr": stderr,
            "env": env,
            "close_fds": True,
        }
        if policy["mode"] == "windows-job":
            # subprocess intentionally does not expose CREATE_SUSPENDED as a
            # public constant. The Win32 value is stable and documented.
            create_suspended = 0x00000004
            job = _WindowsJobScope.create()
            process: subprocess.Popen[Any] | None = None
            assigned = False
            try:
                process = subprocess.Popen(
                    list(args),
                    creationflags=create_suspended,
                    **popen_arguments,
                )
                # The primary thread has not executed user code: assignment is
                # complete before the only suspended thread is resumed.
                job.assign(process.pid)
                assigned = True
                _resume_only_thread(process.pid)
            except BaseException as launch_error:
                cleanup_errors: list[Exception] = []
                if process is not None:
                    if assigned:
                        try:
                            job.terminate(
                                process,
                                terminate_timeout=0,
                                kill_timeout=5,
                            )
                        except Exception as exc:
                            cleanup_errors.append(exc)
                    try:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=5)
                    except Exception as exc:
                        cleanup_errors.append(exc)
                try:
                    job.close()
                except Exception as exc:
                    cleanup_errors.append(exc)
                if cleanup_errors:
                    raise EvidenceError(
                        "Windows process-scope launch failed and cleanup "
                        "could not be confirmed"
                    ) from launch_error
                if isinstance(launch_error, OSError):
                    raise
                raise ProcessScopeLaunchError(
                    "Windows process-scope launch failed after confirmed cleanup"
                ) from launch_error
            return cls(process, job)

        if policy["mode"] == "posix-cooperative-process-group":
            process = subprocess.Popen(
                list(args),
                start_new_session=True,
                **popen_arguments,
            )
            return cls(process, _PosixProcessGroupScope(process.pid))

        raise EvidenceError("selected process-scope policy is unsupported")

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def mode(self) -> ProcessScopeMode:
        return self._backend.mode

    @property
    def cooperative(self) -> bool:
        return self._backend.cooperative

    def poll(self) -> int | None:
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.process.wait(timeout=timeout)

    def active_process_ids(self) -> tuple[int, ...]:
        """Return currently active members known to the platform scope."""

        if self._closed:
            return ()
        return self._backend.active_process_ids()

    def wait_empty(self, timeout: float = 5.0) -> bool:
        """Wait a bounded interval for every in-scope process to exit."""

        validated_timeout = _validate_timeout(timeout, label="timeout")
        if self._closed:
            return True
        return self._backend.wait_empty(
            self.process,
            timeout=validated_timeout,
        )

    def terminate(
        self,
        *,
        terminate_timeout: float = 5.0,
        kill_timeout: float = 5.0,
    ) -> int:
        """Stop every in-scope process and confirm the scope is empty."""

        if self._closed:
            return _confirmed_return_code(self.process, timeout=0)
        validated_terminate_timeout = _validate_timeout(
            terminate_timeout,
            label="terminate_timeout",
        )
        validated_kill_timeout = _validate_timeout(
            kill_timeout,
            label="kill_timeout",
        )
        return self._backend.terminate(
            self.process,
            terminate_timeout=validated_terminate_timeout,
            kill_timeout=validated_kill_timeout,
        )

    def close(self) -> None:
        """Stop remaining members and release the platform scope."""

        if self._closed:
            return
        if self.active_process_ids():
            self.terminate()
        else:
            # Reap a direct child that exited naturally before scope release.
            self.process.poll()
        self._backend.close()
        self._closed = True

    def __enter__(self) -> ProcessScope:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _windows_process_liveness(process_id: int) -> ProcessLiveness:
    # os.kill(pid, 0) is not a portable liveness probe on Windows. Query the
    # process handle instead, without requesting termination rights.
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, process_id)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return "dead"
        if error == error_access_denied:
            return "unknown"
        return "unknown"
    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return "unknown"
        return "alive" if exit_code.value == still_active else "dead"
    finally:
        close_handle(handle)


def _windows_process_start_token(process_id: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    )
    get_process_times.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, process_id)
    if not handle:
        return None
    try:
        creation = FILETIME()
        exit_time = FILETIME()
        kernel_time = FILETIME()
        user_time = FILETIME()
        if not get_process_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        value = (int(creation.high) << 32) | int(creation.low)
        return f"windows:{value}"
    finally:
        close_handle(handle)


def _linux_process_start_token(process_id: int) -> str | None:
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        process_stat = Path(f"/proc/{process_id}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, PermissionError, OSError, UnicodeError):
        return None
    closing_parenthesis = process_stat.rfind(")")
    if not boot_id or closing_parenthesis < 0:
        return None
    fields_after_comm = process_stat[closing_parenthesis + 2 :].split()
    # Field 3 (state) is index 0 here; field 22 (starttime) is index 19.
    if len(fields_after_comm) <= 19 or not fields_after_comm[19].isdigit():
        return None
    return f"linux:{boot_id}:{fields_after_comm[19]}"


def process_start_token(process_id: int | None) -> str | None:
    """Return a stable launch identity for a live Windows or Linux PID."""

    if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
        return None
    if os.name == "nt":
        return _windows_process_start_token(process_id)
    if os.name == "posix" and Path("/proc").is_dir():
        return _linux_process_start_token(process_id)
    return None

def process_liveness(process_id: int | None) -> ProcessLiveness:
    """Classify a PID without treating probe failures as evidence of death."""

    if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
        return "dead"
    if os.name == "nt":
        return _windows_process_liveness(process_id)
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    except OSError:
        return "unknown"
    return "alive"


def stop_process(
    process: ControllableProcess,
    *,
    terminate_timeout: float = 5.0,
    kill_timeout: float = 5.0,
) -> int:
    """Terminate, wait, kill if needed, and confirm one child is reaped.

    A control failure is an evidence failure: callers must not continue while
    the child may still be able to mutate declared outputs.
    """

    try:
        return_code = process.poll()
    except OSError as exc:
        raise EvidenceError(
            f"unable to inspect child process {process.pid} before shutdown: {exc}"
        ) from exc
    if return_code is not None:
        return return_code

    try:
        process.terminate()
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise EvidenceError(
            f"unable to terminate child process {process.pid}: {exc}"
        ) from exc

    try:
        return_code = process.wait(timeout=terminate_timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise EvidenceError(
                f"unable to kill child process {process.pid}: {exc}"
            ) from exc
        try:
            return_code = process.wait(timeout=kill_timeout)
        except subprocess.TimeoutExpired as exc:
            raise EvidenceError(
                f"child process {process.pid} did not exit after terminate and kill"
            ) from exc
        except OSError as exc:
            raise EvidenceError(
                f"unable to confirm killed child process {process.pid}: {exc}"
            ) from exc
    except OSError as exc:
        raise EvidenceError(
            f"unable to confirm terminated child process {process.pid}: {exc}"
        ) from exc

    try:
        if process.poll() is None:
            raise EvidenceError(
                f"child process {process.pid} has no confirmed terminal status"
            )
    except OSError as exc:
        raise EvidenceError(
            f"unable to confirm terminal child process {process.pid}: {exc}"
        ) from exc
    return return_code
