"""Bounded host, cgroup, PSI, and GPU telemetry probes.

This leaf module parses observations only.  Capacity, admission, reserve, and
pressure policy remain in :mod:`subgen_core.resource_management`.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable, Iterable, Optional, Union

from .priority_pressure import PriorityObservation


CGROUP_V2_MEMORY_MAX = "/sys/fs/cgroup/memory.max"
CGROUP_V2_ROOT = "/sys/fs/cgroup"
CGROUP_V1_MEMORY_LIMITS = (
    "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    "/sys/fs/cgroup/memory.limit_in_bytes",
)
CGROUP_V1_ROOTS = (
    "/sys/fs/cgroup/memory",
    "/sys/fs/cgroup",
)
UNBOUNDED_CGROUP_THRESHOLD = 1 << 60
MAX_PROBE_BYTES = (1 << 63) - 1
_PSI_AVG10 = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")


@dataclass(frozen=True)
class PressureSample:
    """One immutable raw observation; interpretation belongs to policy."""

    observed_at: Optional[float] = None
    host_available_bytes: Optional[int] = None
    host_total_bytes: Optional[int] = None
    cgroup_current_bytes: Optional[int] = None
    cgroup_limit_bytes: Optional[int] = None
    psi_some_avg10: Optional[float] = None
    psi_full_avg10: Optional[float] = None
    cgroup_oom_events: Optional[int] = None
    cgroup_oom_kill_events: Optional[int] = None
    host_psi_some_avg10: Optional[float] = None
    host_psi_full_avg10: Optional[float] = None
    cgroup_psi_some_avg10: Optional[float] = None
    cgroup_psi_full_avg10: Optional[float] = None
    gpu_total_bytes: Optional[int] = None
    gpu_free_bytes: Optional[int] = None
    gpu_device_id: Optional[str] = None
    gpu_observed_at: Optional[float] = None
    priority_observation: Optional[PriorityObservation] = None

    @property
    def timestamp(self) -> Optional[float]:
        return self.observed_at


def read_text(path: Union[str, os.PathLike[str]]) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def read_optional(
    reader: Callable[[Union[str, os.PathLike[str]]], str],
    path: Union[str, os.PathLike[str]],
) -> Optional[str]:
    try:
        return reader(path)
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _bounded_int(value: object, *, allow_zero: bool) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    minimum = 0 if allow_zero else 1
    return value if minimum <= value <= MAX_PROBE_BYTES else None


def _parse_bounded_int(raw: object, *, allow_zero: bool) -> Optional[int]:
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)  # Text from procfs/cgroupfs is intentionally accepted.
    except (TypeError, ValueError, OverflowError):
        return None
    minimum = 0 if allow_zero else 1
    return value if minimum <= value <= MAX_PROBE_BYTES else None


def parse_finite_cgroup_limit(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    value = raw.strip().casefold()
    if not value or value == "max":
        return None
    parsed = _parse_bounded_int(value, allow_zero=False)
    if parsed is None or parsed >= UNBOUNDED_CGROUP_THRESHOLD:
        return None
    return parsed


def is_unbounded_cgroup_value(raw: Optional[str]) -> bool:
    if raw is None:
        return False
    value = raw.strip().casefold()
    if value == "max":
        return True
    parsed = _parse_bounded_int(value, allow_zero=False)
    return parsed is not None and parsed >= UNBOUNDED_CGROUP_THRESHOLD


def _windows_memory_status() -> Optional[tuple[int, int]]:
    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    try:
        succeeded = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except (AttributeError, OSError):
        return None
    if not succeeded:
        return None
    return int(status.ullAvailPhys), int(status.ullTotalPhys)


def default_physical_memory_bytes() -> Optional[int]:
    if sys.platform == "win32":
        memory = _windows_memory_status()
        return memory[1] if memory is not None else None
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    return _bounded_int(pages * page_size, allow_zero=False)


def _default_platform_memory() -> tuple[Optional[int], Optional[int]]:
    if sys.platform == "win32":
        memory = _windows_memory_status()
        return memory if memory is not None else (None, None)
    return None, default_physical_memory_bytes()


def _parse_key_values(raw: Optional[str]) -> dict[str, int]:
    values: dict[str, int] = {}
    if raw is None:
        return values
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        parsed = _parse_bounded_int(parts[1], allow_zero=True)
        if parsed is not None:
            values[parts[0].rstrip(":").casefold()] = parsed
    return values


def _parse_meminfo(raw: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    values = _parse_key_values(raw)
    available_kib = values.get("memavailable")
    total_kib = values.get("memtotal")
    return (
        _bounded_int(available_kib * 1024, allow_zero=True)
        if available_kib is not None
        else None,
        _bounded_int(total_kib * 1024, allow_zero=False)
        if total_kib is not None
        else None,
    )


def _parse_psi(raw: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    some = None
    full = None
    if raw is None:
        return some, full
    for line in raw.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        avg10_tokens = [
            token.removeprefix("avg10=")
            for token in parts[1].split()
            if token.startswith("avg10=")
        ]
        if len(avg10_tokens) != 1:
            continue
        raw_value = avg10_tokens[0]
        if _PSI_AVG10.fullmatch(raw_value) is None:
            continue
        try:
            value = float(raw_value)
        except (OverflowError, ValueError):
            continue
        if not math.isfinite(value) or not 0 <= value <= 100:
            continue
        if parts[0] == "some":
            some = value
        elif parts[0] == "full":
            full = value
    return some, full


def _maximum_known(*values: Optional[float]) -> Optional[float]:
    known = [value for value in values if value is not None]
    return max(known) if known else None


def _validated_time(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Probe clock must return a finite non-negative number")
    try:
        parsed = float(value)
    except OverflowError as exc:
        raise ValueError(
            "Probe clock must return a finite non-negative number"
        ) from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError("Probe clock must return a finite non-negative number")
    return parsed


def read_pressure_sample(
    *,
    read_text: Callable[[Union[str, os.PathLike[str]]], str] = read_text,
    clock: Callable[[], float] = time.monotonic,
    platform_memory_reader: Callable[
        [], tuple[Optional[int], Optional[int]]
    ] = _default_platform_memory,
    gpu_memory_reader: Optional[Callable[[], tuple[str, int, int]]] = None,
    priority_reader: Optional[Callable[[], PriorityObservation]] = None,
    cgroup_v2_root: Union[str, os.PathLike[str]] = CGROUP_V2_ROOT,
    cgroup_v1_roots: Iterable[Union[str, os.PathLike[str]]] = CGROUP_V1_ROOTS,
) -> PressureSample:
    """Read one bounded observation without applying resource policy."""

    host_available, host_total = _parse_meminfo(
        read_optional(read_text, "/proc/meminfo")
    )
    if host_available is None or host_total is None:
        try:
            platform_available, platform_total = platform_memory_reader()
        except (OSError, RuntimeError, TypeError, ValueError):
            platform_available, platform_total = None, None
        if host_available is None:
            host_available = _bounded_int(platform_available, allow_zero=True)
        if host_total is None:
            host_total = _bounded_int(platform_total, allow_zero=False)

    v2_root = Path(cgroup_v2_root)
    v2_limit_raw = read_optional(read_text, v2_root / "memory.max")
    v2_current_raw = read_optional(read_text, v2_root / "memory.current")
    cgroup_limit = parse_finite_cgroup_limit(v2_limit_raw)
    cgroup_current = (
        _parse_bounded_int(v2_current_raw.strip(), allow_zero=True)
        if v2_current_raw is not None
        else None
    )
    cgroup_psi_raw = read_optional(read_text, v2_root / "memory.pressure")
    events_raw = read_optional(read_text, v2_root / "memory.events")

    if v2_limit_raw is None and v2_current_raw is None:
        for root_value in cgroup_v1_roots:
            root = Path(root_value)
            limit_raw = read_optional(read_text, root / "memory.limit_in_bytes")
            current_raw = read_optional(read_text, root / "memory.usage_in_bytes")
            if limit_raw is None and current_raw is None:
                continue
            cgroup_limit = parse_finite_cgroup_limit(limit_raw)
            cgroup_current = (
                _parse_bounded_int(current_raw.strip(), allow_zero=True)
                if current_raw is not None
                else None
            )
            cgroup_psi_raw = read_optional(read_text, root / "memory.pressure_level")
            events_raw = read_optional(read_text, root / "memory.failcnt")
            break

    host_psi = _parse_psi(read_optional(read_text, "/proc/pressure/memory"))
    cgroup_psi = _parse_psi(cgroup_psi_raw)
    events = _parse_key_values(events_raw)
    if events_raw is not None and len(events_raw.split()) == 1:
        fail_count = _parse_bounded_int(events_raw.strip(), allow_zero=True)
        if fail_count is not None:
            events["oom"] = fail_count

    observed_at = _validated_time(clock())
    gpu_device = None
    gpu_total = None
    gpu_free = None
    if gpu_memory_reader is not None:
        try:
            candidate_device, candidate_total, candidate_free = gpu_memory_reader()
            candidate_total = _bounded_int(candidate_total, allow_zero=False)
            candidate_free = _bounded_int(candidate_free, allow_zero=True)
            if (
                isinstance(candidate_device, str)
                and candidate_device.strip() == candidate_device
                and candidate_device
                and candidate_total is not None
                and candidate_free is not None
                and candidate_free <= candidate_total
            ):
                gpu_device = candidate_device
                gpu_total = candidate_total
                gpu_free = candidate_free
        except (
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            pass

    priority_observation = priority_reader() if priority_reader is not None else None

    return PressureSample(
        observed_at=observed_at,
        host_available_bytes=host_available,
        host_total_bytes=host_total,
        cgroup_current_bytes=cgroup_current,
        cgroup_limit_bytes=cgroup_limit,
        psi_some_avg10=_maximum_known(host_psi[0], cgroup_psi[0]),
        psi_full_avg10=_maximum_known(host_psi[1], cgroup_psi[1]),
        cgroup_oom_events=events.get("oom"),
        cgroup_oom_kill_events=events.get("oom_kill"),
        host_psi_some_avg10=host_psi[0],
        host_psi_full_avg10=host_psi[1],
        cgroup_psi_some_avg10=cgroup_psi[0],
        cgroup_psi_full_avg10=cgroup_psi[1],
        gpu_total_bytes=gpu_total,
        gpu_free_bytes=gpu_free,
        gpu_device_id=gpu_device,
        gpu_observed_at=observed_at if gpu_total is not None else None,
        priority_observation=priority_observation,
    )


__all__ = ["PressureSample", "read_pressure_sample"]
