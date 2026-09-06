"""OS containment for the actual resident child, before its load handshake.

Windows jobs cap committed process memory, not every GPU driver allocation.
Linux workers inherit the application's finite cgroup limit; this is ONE shared
container budget, not a separate allowance per worker. Admission and live RAM /
VRAM pressure remain owned by resource_management. No host limits are changed.
"""
from __future__ import annotations

import ctypes as C
import os
from pathlib import Path
import signal
import time


class ProcessLimitError(RuntimeError):
    """Containment could not be established or its release is unconfirmed."""


def _capacity(value):
    if type(value) is not int or not 64 * 1024**2 <= value < 2**60:
        raise ValueError('Worker memory limit must be a finite byte count of at least 64 MiB')
    return value


class WindowsJobLimit:
    """One directly owned process, no breakaway children, kill on handle close."""

    def __init__(self, process, memory_bytes):
        _capacity(memory_bytes)
        if os.name != 'nt':
            raise ProcessLimitError('Windows worker jobs are unavailable on this system')
        from ctypes import wintypes as W

        class Basic(C.Structure):
            _fields_ = [('process_time', C.c_longlong), ('job_time', C.c_longlong),
                ('flags', W.DWORD), ('min_ws', C.c_size_t), ('max_ws', C.c_size_t),
                ('active', W.DWORD), ('affinity', C.c_size_t),
                ('priority', W.DWORD), ('scheduling', W.DWORD)]

        class IO(C.Structure):
            _fields_ = [(name, C.c_ulonglong) for name in
                ('reads', 'writes', 'others', 'read_bytes', 'write_bytes', 'other_bytes')]

        class Extended(C.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_limit', C.c_size_t),
                ('job_limit', C.c_size_t), ('peak_process', C.c_size_t), ('peak_job', C.c_size_t)]

        class Accounting(C.Structure):
            _fields_ = [(name, C.c_longlong) for name in ('user', 'kernel', 'period_user', 'period_kernel')] + [
                (name, W.DWORD) for name in ('faults', 'total', 'active', 'terminated')]

        self._extended, self._accounting = Extended, Accounting
        self._kernel = k = C.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': ([C.c_void_p, W.LPCWSTR], W.HANDLE),
            'SetInformationJobObject': ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
            'QueryInformationJobObject': ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
            'AssignProcessToJobObject': ([W.HANDLE, W.HANDLE], W.BOOL),
            'TerminateJobObject': ([W.HANDLE, W.UINT], W.BOOL),
            'CloseHandle': ([W.HANDLE], W.BOOL),
        }
        for name, (args, result) in signatures.items():
            method = getattr(k, name)
            method.argtypes, method.restype = args, result
        self.memory_bytes, self.peak_bytes = memory_bytes, 0
        self._handle = self._check(k.CreateJobObjectW(None, None))
        try:
            limits = Extended()
            limits.basic.flags = 0x2000 | 0x200 | 0x8
            limits.basic.active, limits.job_limit = 1, memory_bytes
            self._check(k.SetInformationJobObject(self._handle, 9, C.byref(limits), C.sizeof(limits)))
            self._check(k.AssignProcessToJobObject(self._handle, int(process._handle)))
            current = Extended()
            self._query(9, current)
            if current.job_limit != memory_bytes or current.basic.flags != limits.basic.flags or current.basic.active != 1:
                raise ProcessLimitError('Worker job did not confirm its memory/process limits')
        except BaseException:
            k.CloseHandle(self._handle)  # Also kills an assigned child on setup failure.
            self._handle = None
            raise

    @staticmethod
    def _check(result):
        if not result:
            raise ProcessLimitError('Windows worker containment operation failed') from C.WinError(C.get_last_error())
        return result

    def _query(self, kind, value):
        self._check(self._kernel.QueryInformationJobObject(
            self._handle, kind, C.byref(value), C.sizeof(value), None))

    def terminate(self):
        if self._handle is not None:
            self._check(self._kernel.TerminateJobObject(self._handle, 124))

    def close(self):
        if self._handle is None:
            return
        accounting = self._accounting()
        self._query(1, accounting)
        # Job accounting can settle just after the process handle is signalled.
        deadline = time.monotonic() + 1
        while accounting.active and time.monotonic() < deadline:
            time.sleep(.01)
            self._query(1, accounting)
        if accounting.active:
            raise ProcessLimitError('Worker job is still active; keep its memory reservation')
        limits = self._extended()
        self._query(9, limits)
        self.peak_bytes = int(limits.peak_job)
        self._check(self._kernel.CloseHandle(self._handle))
        self._handle = None


def _v2_membership(path):
    text = path.read_text(encoding='ascii')
    rows = [line[3:] for line in text.splitlines() if line.startswith('0::')]
    if len(rows) != 1 or not rows[0].startswith('/'):
        raise ProcessLimitError('A cgroup v2 memory limit is required for GPU workers')
    return rows[0]


class LinuxContainerLimit:
    """Read back an inherited finite container limit; own only the child session.

The parent must be in a private cgroup namespace rooted at /sys/fs/cgroup,
as in normal Docker deployments. Native Linux service deployment needs the
same namespace boundary. Do not modify an arbitrary host cgroup or raise caps.
"""

    def __init__(self, process, memory_bytes):
        _capacity(memory_bytes)
        if os.name != 'posix' or not Path('/proc/self/cgroup').exists():
            raise ProcessLimitError('Linux cgroup worker containment is unavailable')
        self.pid = process.pid
        if os.getpgid(self.pid) != self.pid or os.getsid(self.pid) != self.pid:
            raise ProcessLimitError('The worker must own a separate process session')
        parent = _v2_membership(Path('/proc/self/cgroup'))
        child = _v2_membership(Path(f'/proc/{self.pid}/cgroup'))
        if parent != '/' or child != parent:
            raise ProcessLimitError('GPU workers require an inherited private cgroup namespace')
        root = Path('/sys/fs/cgroup')
        try:
            actual = int((root / 'memory.max').read_text(encoding='ascii').strip())
            swap = int((root / 'memory.swap.max').read_text(encoding='ascii').strip())
        except (ValueError, OSError):
            raise ProcessLimitError('GPU workers require finite container RAM and swap limits') from None
        if not 0 < actual <= memory_bytes or swap != 0:
            raise ProcessLimitError('Container RAM exceeds the approved budget or additional swap is enabled')
        self.memory_bytes, self._closed = actual, False

    def _alive(self):
        try:
            os.killpg(self.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def terminate(self):
        if not self._closed:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        if self._closed:
            return
        # Reaping of descendants can lag the direct child by a few milliseconds.
        deadline = time.monotonic() + 1
        while self._alive() and time.monotonic() < deadline:
            time.sleep(.01)
        if self._alive():
            raise ProcessLimitError('Worker process group remains active; keep its memory reservation')
        self._closed = True


def establish_worker_limits(memory_bytes):
    """Return the resident transport's process-limit factory, not a new policy."""
    _capacity(memory_bytes)
    kind = WindowsJobLimit if os.name == 'nt' else LinuxContainerLimit
    return lambda process: kind(process, memory_bytes)
