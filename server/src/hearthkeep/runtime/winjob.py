"""Windows Job Objects: a child in a kill-on-close job dies when the job handle is closed,
including when Hearthkeep exits or crashes (Windows closes the handle for us)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import cache

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


@cache
def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    ]  # fmt: skip
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.TerminateJobObject.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k


def _fail() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


class KillOnCloseJob:
    """One job object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. The handle is not inheritable."""

    def __init__(self) -> None:
        k = _kernel32()
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            raise _fail()
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = _fail()
            k.CloseHandle(handle)
            raise error
        self._handle: int | None = handle

    def assign(self, pid: int) -> None:
        k = _kernel32()
        process = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            raise _fail()
        try:
            if not k.AssignProcessToJobObject(self._handle, process):
                raise _fail()
        finally:
            k.CloseHandle(process)

    def terminate(self, exit_code: int = 1) -> None:
        """TerminateProcess for every process in the job (the child and anything it started)."""
        if self._handle is not None and not _kernel32().TerminateJobObject(self._handle, exit_code):
            raise _fail()

    def close(self) -> None:
        """Closing the last handle kills everything still in the job."""
        if self._handle is not None:
            _kernel32().CloseHandle(self._handle)
            self._handle = None
