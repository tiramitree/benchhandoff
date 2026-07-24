"""Conservative child-process liveness and shutdown controls."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal, Protocol

from benchhandoff.errors import EvidenceError

ProcessLiveness = Literal["alive", "dead", "unknown"]


class ControllableProcess(Protocol):
    """The small ``subprocess.Popen`` surface used by the shutdown guard."""

    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


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
