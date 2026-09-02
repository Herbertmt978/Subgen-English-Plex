"""Pure capacity, pressure, and adaptive chunk policy.

This module deliberately depends only on the standard library.  Runtime owners
provide readers, clocks, sleepers, and cancellation signals when deterministic
behaviour or a platform-specific adapter is required.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
import errno
import math
import os
import re
import threading
import time
from typing import Callable, Iterable, Optional, Union

from .model_envelope_catalog import EnvelopeResolution
from .priority_pressure import PriorityObservation
from .resource_probes import (
    CGROUP_V1_MEMORY_LIMITS,
    CGROUP_V2_MEMORY_MAX,
    UNBOUNDED_CGROUP_THRESHOLD,
    PressureSample,
    default_physical_memory_bytes,
    is_unbounded_cgroup_value,
    parse_finite_cgroup_limit,
    read_optional,
    read_pressure_sample,
    read_text,
)


GIB = 1024**3
MIB = 1024**2
CAPACITY_QUANTUM_BYTES = 256 * MIB
MAX_AUTOMATIC_SUBGEN_MEMORY_BYTES = 24 * GIB

_MAX_BYTES = (1 << 63) - 1
_PRIORITY_RECOVERY_CLEAR_COUNT = 3
_MODEL_ORDER = ("tiny", "base", "small", "medium", "large-v3")
_MODEL_FAMILIES = {
    **{name: name for name in _MODEL_ORDER},
    "tiny.en": "tiny",
    "base.en": "base",
    "small.en": "small",
    "medium.en": "medium",
    "large": "large-v3",
    "large-v1": "large-v3",
    "large-v2": "large-v3",
    "turbo": "large-v3",
    "large-v3-turbo": "large-v3",
}
_FALLBACK_HOST_LOAD_BYTES = {
    "tiny": 3 * GIB // 4,
    "base": GIB,
    "small": 2 * GIB,
    "medium": 5 * GIB,
    "large-v3": 9 * GIB,
}
_FALLBACK_DEVICE_LOAD_BYTES = {
    "tiny": GIB,
    "base": 2 * GIB,
    "small": 3 * GIB,
    "medium": 7 * GIB,
    "large-v3": 12 * GIB,
}
_FALLBACK_HOST_MARGIN_BYTES = 512 * MIB
_FALLBACK_DEVICE_MARGIN_BYTES = GIB


@dataclass(frozen=True)
class CapacityProfile:
    """Stable memory capacity used for process-lifetime policy decisions."""

    effective_bytes: Optional[int]
    host_total_bytes: Optional[int]
    cgroup_limit_bytes: Optional[int]
    source: str
    cgroup_version: Optional[int] = None
    cgroup_unbounded: bool = False
    warning: Optional[str] = None

    @property
    def capacity_bytes(self) -> Optional[int]:
        """Compatibility spelling for the effective stable capacity."""

        return self.effective_bytes

    @property
    def effective_capacity_bytes(self) -> Optional[int]:
        return self.effective_bytes

    @property
    def is_known(self) -> bool:
        return self.effective_bytes is not None


@dataclass(frozen=True)
class ModelDecision:
    """A selected model and the automatic ceiling that informed the choice."""

    selected_model: Optional[str]
    automatic_ceiling: str
    explicit: bool
    warning: Optional[str] = None
    admitted: bool = True
    reason: str = "selected"
    provenance: Optional[str] = None
    requirement: Optional["ModelLoadRequirement"] = None
    admission: Optional["AdmissionDecision"] = None
    recovery_requirements: tuple["ModelLoadRequirement", ...] = ()

    @property
    def model(self) -> Optional[str]:
        return self.selected_model


@dataclass(frozen=True)
class StabilizedGpuCapacity:
    """Minimum free VRAM from three fresh samples of one exact device."""

    device_id: str
    total_bytes: int
    free_bytes: int
    observed_at: float
    sample_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.device_id, str) or not self.device_id:
            raise ValueError("GPU device must be a non-empty string")
        _require_bytes(self.total_bytes, "GPU total", positive=True)
        free = _require_bytes(self.free_bytes, "GPU free")
        if free > self.total_bytes:
            raise ValueError("GPU free bytes cannot exceed total bytes")
        _require_time(self.observed_at, "GPU observation time")
        if self.sample_count != 3:
            raise ValueError("Stabilized GPU capacity requires three samples")


class MemoryPressureYield(RuntimeError):
    """Private control signal used to unwind an uncommitted inference chunk."""


@dataclass(frozen=True)
class ModelLoadRequirement:
    """Incremental model-load evidence plus its uncertainty margins."""

    model: str
    host_incremental_bytes: int
    cgroup_incremental_bytes: int
    device_incremental_bytes: int
    host_margin_bytes: int
    device_margin_bytes: int
    provenance: str
    envelope_resolution: Optional[EnvelopeResolution] = None

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("Model load requirement needs a model")
        for field_name in (
            "host_incremental_bytes",
            "cgroup_incremental_bytes",
            "device_incremental_bytes",
        ):
            _require_bytes(getattr(self, field_name), field_name)
        for field_name in ("host_margin_bytes", "device_margin_bytes"):
            _require_bytes(getattr(self, field_name), field_name, positive=True)
        if self.provenance not in {"fallback", "envelope"}:
            raise ValueError("Unknown model-load provenance")
        if self.provenance == "fallback":
            family = _model_family(self.model)
            if family is None or self.envelope_resolution is not None:
                raise ValueError("Invalid fallback model-load evidence")
            expected = (
                _FALLBACK_HOST_LOAD_BYTES[family],
                _FALLBACK_HOST_LOAD_BYTES[family],
                _FALLBACK_DEVICE_LOAD_BYTES[family],
                _FALLBACK_HOST_MARGIN_BYTES,
                _FALLBACK_DEVICE_MARGIN_BYTES,
            )
        else:
            resolution = _validated_exact_resolution(self.envelope_resolution)
            envelope = resolution.envelope
            if envelope.policy.model != self.model:
                raise ValueError("Envelope model does not match the requirement")
            measurements = envelope.measurements
            expected = (
                measurements.host_incremental_peak_bytes,
                measurements.cgroup_incremental_peak_bytes,
                measurements.device_incremental_peak_bytes,
                measurements.host_margin_bytes,
                measurements.device_margin_bytes,
            )
        actual = (
            self.host_incremental_bytes,
            self.cgroup_incremental_bytes,
            self.device_incremental_bytes,
            self.host_margin_bytes,
            self.device_margin_bytes,
        )
        if actual != expected:
            raise ValueError(
                "Model-load requirement does not match its source evidence"
            )
        _require_bytes(
            max(self.host_incremental_bytes, self.cgroup_incremental_bytes)
            + self.host_margin_bytes,
            "Derived host requirement",
        )
        _require_bytes(
            self.device_incremental_bytes + self.device_margin_bytes,
            "Derived device requirement",
        )

    @property
    def exact_match(self) -> bool:
        """Whether this requirement retains validated exact-match provenance."""

        return self.envelope_resolution is not None

    @property
    def host_load_bytes(self) -> int:
        return max(self.host_incremental_bytes, self.cgroup_incremental_bytes)

    @property
    def required_host_bytes(self) -> int:
        return self.host_load_bytes + self.host_margin_bytes

    @property
    def required_device_bytes(self) -> int:
        return self.device_incremental_bytes + self.device_margin_bytes


@dataclass(frozen=True)
class AdmissionDecision:
    """One fresh model-load admission decision and its independent terms."""

    admitted: bool
    reasons: tuple[str, ...]
    host_admission_bytes: Optional[int]
    cgroup_admission_bytes: Optional[int]
    effective_host_admission_bytes: Optional[int]
    device_admission_bytes: Optional[int]
    requirement: ModelLoadRequirement


def _require_bytes(value: object, name: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _MAX_BYTES
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a bounded {qualifier} integer")
    return value


def _optional_bytes(
    value: object,
    name: str,
    *,
    positive: bool = False,
) -> Optional[int]:
    return None if value is None else _require_bytes(value, name, positive=positive)


def _require_time(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite non-negative number")
    try:
        parsed = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite non-negative number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return parsed


def _positive_finite_number(value: object, name: str) -> float:
    parsed = _require_time(value, name)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _explicit_gib_bytes(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive GiB value")
    if isinstance(value, int):
        valid = 0 < value <= _MAX_BYTES // GIB
    else:
        valid = math.isfinite(value) and 0 < value <= _MAX_BYTES / GIB
    if not valid:
        raise ValueError(f"{name} must be a positive GiB value")
    return _require_bytes(int(value * GIB), name, positive=True)


def discover_capacity(
    *,
    read_text: Callable[[Union[str, os.PathLike[str]]], str] = read_text,
    physical_memory_reader: Callable[[], Optional[int]] = default_physical_memory_bytes,
    cgroup_v2_path: Union[str, os.PathLike[str]] = CGROUP_V2_MEMORY_MAX,
    cgroup_v1_paths: Iterable[Union[str, os.PathLike[str]]] = CGROUP_V1_MEMORY_LIMITS,
) -> CapacityProfile:
    """Discover stable capacity, preferring finite cgroup v2 and v1 limits."""

    try:
        host_total = _optional_bytes(
            physical_memory_reader(), "Physical memory", positive=True
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        host_total = None

    v2_raw = read_optional(read_text, cgroup_v2_path)
    v2_limit = parse_finite_cgroup_limit(v2_raw)
    if v2_limit is not None:
        effective = min(v2_limit, host_total) if host_total is not None else v2_limit
        warning = None
        if host_total is not None and v2_limit > host_total:
            warning = (
                "Finite cgroup v2 memory exceeds physical memory; effective "
                "capacity is clamped to physical memory"
            )
        return CapacityProfile(
            effective_bytes=effective,
            host_total_bytes=host_total,
            cgroup_limit_bytes=v2_limit,
            source="cgroup_v2",
            cgroup_version=2,
            warning=warning,
        )

    saw_unbounded = is_unbounded_cgroup_value(v2_raw)
    for path in cgroup_v1_paths:
        raw = read_optional(read_text, path)
        limit = parse_finite_cgroup_limit(raw)
        if limit is not None:
            effective = min(limit, host_total) if host_total is not None else limit
            warning = None
            if host_total is not None and limit > host_total:
                warning = (
                    "Finite cgroup v1 memory exceeds physical memory; effective "
                    "capacity is clamped to physical memory"
                )
            return CapacityProfile(
                effective_bytes=effective,
                host_total_bytes=host_total,
                cgroup_limit_bytes=limit,
                source="cgroup_v1",
                cgroup_version=1,
                cgroup_unbounded=saw_unbounded,
                warning=warning,
            )
        saw_unbounded = saw_unbounded or is_unbounded_cgroup_value(raw)

    if host_total is not None:
        return CapacityProfile(
            effective_bytes=host_total,
            host_total_bytes=host_total,
            cgroup_limit_bytes=None,
            source="physical",
            cgroup_unbounded=saw_unbounded,
        )

    reason = "Memory capacity is unavailable"
    if saw_unbounded:
        reason = "Cgroup memory is unbounded and physical memory is unavailable"
    return CapacityProfile(
        effective_bytes=None,
        host_total_bytes=None,
        cgroup_limit_bytes=None,
        source="unknown",
        cgroup_unbounded=saw_unbounded,
        warning=reason,
    )


def _capacity_value(capacity: Union[CapacityProfile, int, None]) -> Optional[int]:
    if isinstance(capacity, CapacityProfile):
        return _optional_bytes(capacity.effective_bytes, "Effective capacity")
    return _optional_bytes(capacity, "Effective capacity")


def _system_model_ceiling(capacity_bytes: Optional[int]) -> str:
    if capacity_bytes is None:
        return "small"
    if capacity_bytes < 2 * GIB:
        return "tiny"
    if capacity_bytes < 4 * GIB:
        return "base"
    if capacity_bytes < 8 * GIB:
        return "small"
    if capacity_bytes < 16 * GIB:
        return "medium"
    return "large-v3"


def _round_up_capacity_quantum(value: int) -> int:
    value = _require_bytes(value, "Capacity value", positive=True)
    return (
        (value + CAPACITY_QUANTUM_BYTES - 1) // CAPACITY_QUANTUM_BYTES
    ) * CAPACITY_QUANTUM_BYTES


def _round_down_capacity_quantum(value: int) -> int:
    value = _require_bytes(value, "Capacity value", positive=True)
    return value // CAPACITY_QUANTUM_BYTES * CAPACITY_QUANTUM_BYTES


def automatic_host_reserve_bytes(host_total_bytes: Optional[int]) -> int:
    """Return the non-reducible host reserve rounded up to 256 MiB."""

    total = _optional_bytes(host_total_bytes, "Host total", positive=True)
    if total is None:
        return GIB
    percentage = (total * 15 + 99) // 100
    return _round_up_capacity_quantum(max(GIB, percentage))


def automatic_subgen_memory_limit_bytes(host_total_bytes: int) -> int:
    """Return the finite public cgroup limit for stable engine capacity."""

    total = _require_bytes(host_total_bytes, "Docker engine memory", positive=True)
    reserve = automatic_host_reserve_bytes(total)
    usable = total - reserve
    if usable < CAPACITY_QUANTUM_BYTES:
        raise ValueError("Docker engine memory cannot preserve the host reserve")
    return min(
        MAX_AUTOMATIC_SUBGEN_MEMORY_BYTES,
        _round_down_capacity_quantum(usable),
    )


def _nominal_system_model_ceiling(
    capacity: Union[CapacityProfile, int, None],
    *,
    reserve_bytes: int,
) -> str:
    """Return a gross load-budget ceiling; fresh admission remains decisive."""

    reserve = _require_bytes(reserve_bytes, "Host reserve")
    budgets: list[int] = []
    if isinstance(capacity, CapacityProfile):
        host_total = _optional_bytes(
            capacity.host_total_bytes, "Host total", positive=True
        )
        cgroup_limit = _optional_bytes(
            capacity.cgroup_limit_bytes, "Cgroup limit", positive=True
        )
        if host_total is not None:
            budgets.append(max(0, host_total - reserve))
        if cgroup_limit is not None:
            floor = cgroup_headroom_floor(cgroup_limit)
            if floor is not None:
                budgets.append(max(0, cgroup_limit - floor))
        if not budgets and capacity.effective_bytes is not None:
            budgets.append(max(0, capacity.effective_bytes - reserve))
    else:
        value = _capacity_value(capacity)
        if value is not None:
            budgets.append(max(0, value - reserve))

    if not budgets:
        return "small"
    nominal_budget = min(budgets)
    for model in reversed(_MODEL_ORDER):
        required = _FALLBACK_HOST_LOAD_BYTES[model] + _FALLBACK_HOST_MARGIN_BYTES
        if nominal_budget >= required:
            return model
    return "tiny"


def _vram_model_ceiling(vram_bytes: Optional[int]) -> str:
    if vram_bytes is None:
        return "small"
    if vram_bytes < 2 * GIB:
        return "tiny"
    if vram_bytes < 3 * GIB:
        return "base"
    if vram_bytes < 7 * GIB:
        return "small"
    if vram_bytes < 12 * GIB:
        return "medium"
    return "large-v3"


def _model_rank(model: str) -> Optional[int]:
    family = _MODEL_FAMILIES.get(model.strip().casefold())
    if family is None:
        return None
    try:
        return _MODEL_ORDER.index(family)
    except ValueError:
        return None


def _model_family(model: str) -> Optional[str]:
    return _MODEL_FAMILIES.get(model.strip().casefold())


def _is_gpu_device(device: str) -> bool:
    if not isinstance(device, str):
        raise ValueError("Device must be a string")
    return device.strip().casefold().startswith(("cuda", "gpu"))


def fallback_model_ceiling(
    capacity: Union[CapacityProfile, int, None],
    *,
    device: str = "cpu",
    allocatable_vram_bytes: Optional[int] = None,
) -> str:
    """Return the generic ceiling; this function never performs admission."""

    system_ceiling = _system_model_ceiling(_capacity_value(capacity))
    if not _is_gpu_device(device):
        return system_ceiling
    device_ceiling = _vram_model_ceiling(
        _optional_bytes(allocatable_vram_bytes, "Allocatable VRAM")
    )
    return min(
        (system_ceiling, device_ceiling),
        key=lambda model: _MODEL_ORDER.index(model),
    )


def gpu_priority_reserve_bytes(
    total_vram_bytes: Optional[int],
    *,
    explicit_reserve_gib: Optional[float] = None,
    canonical_shared_cuda: bool = False,
) -> int:
    """Return the mandatory GPU floor or a larger explicit priority reserve."""

    automatic_floor = _gpu_reserve_floor_bytes(total_vram_bytes)
    if explicit_reserve_gib is None:
        if canonical_shared_cuda:
            raise ValueError(
                "Canonical shared CUDA requires an explicit positive GPU reserve"
            )
        return automatic_floor
    explicit = _explicit_gib_bytes(explicit_reserve_gib, "Explicit GPU reserve")
    return max(automatic_floor, explicit)


def _gpu_reserve_floor_bytes(total_vram_bytes: Optional[int]) -> int:
    total = _optional_bytes(total_vram_bytes, "Total VRAM", positive=True)
    return max(GIB, total // 10) if total is not None else GIB


def _require_gpu_reserve_floor(
    reserve_bytes: object,
    total_vram_bytes: Optional[int],
    *,
    name: str = "GPU priority reserve",
) -> int:
    reserve = _require_bytes(reserve_bytes, name, positive=True)
    floor = _gpu_reserve_floor_bytes(total_vram_bytes)
    if reserve < floor:
        raise ValueError(f"{name} is below the mandatory GPU reserve floor")
    return reserve


def _gpu_sample_is_fresh(
    sample: PressureSample,
    *,
    expected_device: Optional[str],
    now: float,
    maximum_age_seconds: float,
) -> bool:
    try:
        total = _optional_bytes(sample.gpu_total_bytes, "GPU total", positive=True)
        free = _optional_bytes(sample.gpu_free_bytes, "GPU free")
        observed = _require_time(sample.gpu_observed_at, "GPU observation time")
        decision_time = _require_time(now, "Decision time")
    except ValueError:
        return False
    return bool(
        total is not None
        and free is not None
        and free <= total
        and sample.gpu_device_id
        and (expected_device is None or sample.gpu_device_id == expected_device)
        and 0 <= decision_time - observed <= maximum_age_seconds
    )


def stabilize_gpu_capacity(
    sample_reader: Callable[[], PressureSample],
    *,
    expected_device: str,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    sample_count: int = 3,
    interval_seconds: float = 5.0,
    maximum_age_seconds: float = 10.0,
) -> Optional[StabilizedGpuCapacity]:
    """Return the minimum free VRAM across three fresh exact-device samples."""

    if sample_count != 3:
        raise ValueError("GPU stabilization requires exactly three samples")
    if not isinstance(expected_device, str) or not expected_device:
        raise ValueError("GPU stabilization requires an exact device")
    for value, name in (
        (interval_seconds, "GPU sample interval"),
        (maximum_age_seconds, "Maximum GPU sample age"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be positive")
    samples = []
    observed_times = set()
    for index in range(sample_count):
        sample = sample_reader()
        if not isinstance(sample, PressureSample):
            raise TypeError("sample_reader must return PressureSample")
        now = clock()
        if not _gpu_sample_is_fresh(
            sample,
            expected_device=expected_device,
            now=now,
            maximum_age_seconds=maximum_age_seconds,
        ):
            return None
        if sample.gpu_observed_at in observed_times:
            return None
        observed_times.add(sample.gpu_observed_at)
        samples.append(sample)
        if index + 1 < sample_count:
            sleep(interval_seconds)

    total = samples[0].gpu_total_bytes
    if any(sample.gpu_total_bytes != total for sample in samples[1:]):
        return None
    return StabilizedGpuCapacity(
        device_id=expected_device,
        total_bytes=total,
        free_bytes=min(sample.gpu_free_bytes for sample in samples),
        observed_at=samples[-1].gpu_observed_at,
        sample_count=sample_count,
    )


def _validated_exact_resolution(value: object) -> EnvelopeResolution:
    if type(value) is not EnvelopeResolution:
        raise TypeError("Envelope evidence must be an exact EnvelopeResolution")
    EnvelopeResolution.__post_init__(value)
    if not value.matched or value.envelope is None:
        raise ValueError("Only exact matched envelope resolutions are admissible")
    return value


def _validated_envelopes_by_model(
    values: Iterable[object],
) -> dict[str, EnvelopeResolution]:
    result: dict[str, EnvelopeResolution] = {}
    for value in values:
        resolution = _validated_exact_resolution(value)
        model = resolution.envelope.policy.model
        if model in result:
            raise ValueError(f"Duplicate envelope model {model!r}")
        result[model] = resolution
    return result


def _validated_stabilized_gpu(value: object) -> StabilizedGpuCapacity:
    if type(value) is not StabilizedGpuCapacity:
        raise TypeError("stabilized_gpu must be an exact StabilizedGpuCapacity")
    value.__post_init__()
    return value


def select_model(
    requested_model: Optional[str],
    capacity: Union[CapacityProfile, int, None],
    *,
    device: str = "cpu",
    admission_sample: Optional[PressureSample] = None,
    stabilized_gpu: Optional[StabilizedGpuCapacity] = None,
    host_reserve: Optional[int] = None,
    gpu_reserve_bytes: Optional[int] = None,
    expected_gpu_device: Optional[str] = None,
    envelopes: Iterable[object] = (),
    canonical_shared_cuda: bool = False,
    require_cgroup: bool = False,
    now: Optional[float] = None,
) -> ModelDecision:
    """Select and admit one fixed model from exact envelopes or fallbacks."""

    is_gpu = _is_gpu_device(device)
    if expected_gpu_device is not None and (
        not isinstance(expected_gpu_device, str) or not expected_gpu_device
    ):
        raise ValueError("Expected GPU device must be a non-empty string")
    if host_reserve is not None:
        host_reserve = _require_bytes(host_reserve, "Host reserve")
    envelopes_by_model = _validated_envelopes_by_model(envelopes)

    if admission_sample is not None and not isinstance(
        admission_sample, PressureSample
    ):
        raise TypeError("admission_sample must be PressureSample")
    if stabilized_gpu is not None:
        stabilized_gpu = _validated_stabilized_gpu(stabilized_gpu)

    immediate_gpu_total = None
    if is_gpu and admission_sample is not None:
        immediate_gpu_total = _optional_bytes(
            admission_sample.gpu_total_bytes, "GPU total", positive=True
        )
    gpu_totals = [
        total
        for total in (
            stabilized_gpu.total_bytes if stabilized_gpu is not None else None,
            immediate_gpu_total,
        )
        if total is not None
    ]
    gpu_total = max(gpu_totals) if gpu_totals else None

    stabilized_usable = False
    combined_gpu_free = None
    stabilization_conflict = False
    if is_gpu and stabilized_gpu is not None and admission_sample is not None:
        stabilized_sample = PressureSample(
            gpu_total_bytes=stabilized_gpu.total_bytes,
            gpu_free_bytes=stabilized_gpu.free_bytes,
            gpu_device_id=stabilized_gpu.device_id,
            gpu_observed_at=stabilized_gpu.observed_at,
        )
        stabilized_fresh = _gpu_sample_is_fresh(
            stabilized_sample,
            expected_device=expected_gpu_device,
            now=now,
            maximum_age_seconds=10.0,
        )
        immediate_fresh = _gpu_sample_is_fresh(
            admission_sample,
            expected_device=expected_gpu_device,
            now=now,
            maximum_age_seconds=10.0,
        )
        stabilization_conflict = bool(
            immediate_gpu_total != stabilized_gpu.total_bytes
            or admission_sample.gpu_device_id != stabilized_gpu.device_id
        )
        stabilized_usable = bool(
            stabilized_fresh and immediate_fresh and not stabilization_conflict
        )
        if stabilized_usable:
            combined_gpu_free = min(
                stabilized_gpu.free_bytes,
                admission_sample.gpu_free_bytes,
            )

    if gpu_reserve_bytes is not None:
        gpu_reserve_bytes = _require_gpu_reserve_floor(
            gpu_reserve_bytes,
            gpu_total,
        )
    if is_gpu and gpu_reserve_bytes is None and not canonical_shared_cuda:
        gpu_reserve_bytes = gpu_priority_reserve_bytes(gpu_total)
    allocatable_vram = None
    if stabilized_usable and gpu_reserve_bytes is not None:
        allocatable_vram = max(0, combined_gpu_free - gpu_reserve_bytes)
    if host_reserve is None:
        host_reserve = host_reserve_bytes(capacity)
    automatic_ceiling = _nominal_system_model_ceiling(
        capacity,
        reserve_bytes=host_reserve,
    )
    if is_gpu:
        device_ceiling = _vram_model_ceiling(allocatable_vram)
        automatic_ceiling = min(
            (automatic_ceiling, device_ceiling),
            key=lambda model: _MODEL_ORDER.index(model),
        )

    warnings = []
    if _capacity_value(capacity) is None:
        warnings.append(
            "System memory capacity is unavailable; using the small ceiling"
        )
    if is_gpu and not stabilized_usable:
        warnings.append(
            "Stabilized exact-device VRAM is unavailable; limiting fallback to small"
        )
    if stabilization_conflict:
        warnings.append(
            "Stabilized GPU telemetry conflicts with the fresh admission sample"
        )
    requested = (requested_model or "").strip()
    automatic = not requested or requested.casefold() == "auto"

    selection_sample = admission_sample
    if admission_sample is not None and is_gpu and stabilized_usable:
        selection_sample = replace(
            admission_sample,
            gpu_free_bytes=combined_gpu_free,
        )

    canonical_issue = None
    if canonical_shared_cuda:
        if not is_gpu:
            canonical_issue = "Canonical shared CUDA requires a CUDA device"
        elif gpu_reserve_bytes is None:
            canonical_issue = "Canonical shared CUDA requires an explicit GPU reserve"
        elif not expected_gpu_device:
            canonical_issue = "Canonical shared CUDA requires an exact GPU device"
        elif not stabilized_usable:
            canonical_issue = (
                "Canonical shared CUDA requires stabilized exact-device telemetry"
            )
    if canonical_issue:
        warnings.append(canonical_issue)

    if not automatic:
        envelope_resolution = envelopes_by_model.get(requested)
        try:
            requirement = model_load_requirement(
                requested,
                resolution=envelope_resolution,
            )
        except ValueError:
            return ModelDecision(
                requested,
                automatic_ceiling,
                True,
                "Explicit model has no validated load budget",
                False,
                "no_load_budget",
            )
        admission = None
        if (
            admission_sample is not None
            and canonical_issue is None
            and not stabilization_conflict
            and (not is_gpu or expected_gpu_device is not None)
        ):
            admission = evaluate_admission(
                requirement,
                selection_sample,
                host_reserve_bytes=host_reserve,
                gpu_priority_reserve_bytes=gpu_reserve_bytes,
                require_cgroup=require_cgroup,
                require_gpu=is_gpu,
                expected_gpu_device=expected_gpu_device,
                now=now,
            )
        requested_rank = _model_rank(requested)
        explicit_warnings = list(warnings)
        if requested_rank is not None and requested_rank > _MODEL_ORDER.index(
            automatic_ceiling
        ):
            explicit_warnings.append(
                f"Explicit model {requested!r} is above the automatic "
                f"{automatic_ceiling!r} ceiling"
            )
        if canonical_shared_cuda and envelope_resolution is None:
            explicit_warnings.append(
                "Explicit model uses the conservative fallback load budget"
            )
        admitted = admission is not None and admission.admitted
        reason = (
            "selected"
            if admitted
            else "no_safe_model"
            if stabilization_conflict
            else "insufficient_capacity"
        )
        return ModelDecision(
            selected_model=requested,
            automatic_ceiling=automatic_ceiling,
            explicit=True,
            warning="; ".join(explicit_warnings) or None,
            admitted=admitted,
            reason=reason,
            provenance=requirement.provenance,
            requirement=requirement,
            admission=admission,
            recovery_requirements=(requirement,),
        )

    ceiling_rank = _MODEL_ORDER.index(automatic_ceiling)
    recovery_requirements = []
    for model in reversed(_MODEL_ORDER):
        catalog_resolution = envelopes_by_model.get(model)
        if canonical_shared_cuda and catalog_resolution is None:
            continue
        envelope_resolution = catalog_resolution
        if is_gpu and not stabilized_usable and not canonical_shared_cuda:
            envelope_resolution = None
        if _MODEL_ORDER.index(model) > ceiling_rank and (
            envelope_resolution is None or (is_gpu and not stabilized_usable)
        ):
            continue
        requirement = model_load_requirement(
            model,
            resolution=envelope_resolution,
        )
        recovery_requirements.append(requirement)
        if (
            canonical_issue is not None
            or stabilization_conflict
            or admission_sample is None
            or (is_gpu and expected_gpu_device is None)
        ):
            continue
        admission = evaluate_admission(
            requirement,
            selection_sample,
            host_reserve_bytes=host_reserve,
            gpu_priority_reserve_bytes=gpu_reserve_bytes,
            require_cgroup=require_cgroup,
            require_gpu=is_gpu,
            expected_gpu_device=expected_gpu_device,
            now=now,
        )
        if admission.admitted:
            if model == "tiny":
                warnings.append("Capacity is constrained; using the tiny model")
            return ModelDecision(
                selected_model=model,
                automatic_ceiling=automatic_ceiling,
                explicit=False,
                warning="; ".join(warnings) or None,
                admitted=True,
                provenance=requirement.provenance,
                requirement=requirement,
                admission=admission,
                recovery_requirements=(requirement,),
            )

    if admission_sample is None:
        warnings.append("Fresh admission telemetry is unavailable")
    warnings.append("No model satisfies the fresh admission floor")
    return ModelDecision(
        selected_model=None,
        automatic_ceiling=automatic_ceiling,
        explicit=False,
        warning="; ".join(warnings),
        admitted=False,
        reason="no_safe_model",
        recovery_requirements=tuple(recovery_requirements),
    )


def initial_chunk_seconds(
    capacity: Union[CapacityProfile, int, None],
    configured_minutes: Optional[int] = None,
) -> int:
    """Return the deterministic initial core duration in seconds."""

    if configured_minutes is not None:
        if isinstance(configured_minutes, bool) or not isinstance(
            configured_minutes, int
        ):
            raise ValueError(
                "Configured chunk duration must be an integer number of minutes"
            )
        if not 5 <= configured_minutes <= 60:
            raise ValueError(
                "Configured chunk duration must be between 5 and 60 minutes"
            )
        return configured_minutes * 60

    capacity_bytes = _capacity_value(capacity)
    if capacity_bytes is None:
        return 10 * 60
    if capacity_bytes < 4 * GIB:
        return 5 * 60
    if capacity_bytes < 8 * GIB:
        return 10 * 60
    if capacity_bytes < 16 * GIB:
        return 20 * 60
    return 30 * 60


def host_reserve_bytes(
    host_total: Union[CapacityProfile, int, None],
    effective_capacity_bytes: Optional[int] = None,
    *,
    explicit_reserve_gib: Optional[float] = None,
) -> int:
    """Calculate the host reserve; explicit configuration may only raise it."""

    if isinstance(host_total, CapacityProfile):
        profile = host_total
        host_total_bytes = _optional_bytes(
            profile.host_total_bytes, "Host total", positive=True
        )
    else:
        host_total_bytes = _optional_bytes(host_total, "Host total", positive=True)

    if effective_capacity_bytes is not None:
        _optional_bytes(effective_capacity_bytes, "Effective capacity")
    reserve = automatic_host_reserve_bytes(host_total_bytes)
    if explicit_reserve_gib is None:
        return reserve
    explicit = _explicit_gib_bytes(explicit_reserve_gib, "Explicit host reserve")
    return max(reserve, explicit)


def cgroup_headroom_floor(cgroup_limit_bytes: Optional[int]) -> Optional[int]:
    """Return the mandatory headroom floor for a finite cgroup limit."""

    limit = _optional_bytes(cgroup_limit_bytes, "Cgroup limit")
    if limit is None or limit >= UNBOUNDED_CGROUP_THRESHOLD:
        return None
    return max(512 * MIB, limit // 10)


def paired_incremental_peak_bytes(
    preload_bytes: Iterable[int],
    peak_bytes: Iterable[int],
) -> int:
    """Return ``max(0, peak-preload)`` aggregated only after pairing runs."""

    preloads = tuple(preload_bytes)
    peaks = tuple(peak_bytes)
    if not preloads or len(preloads) != len(peaks):
        raise ValueError("Preload and peak samples must have the same non-zero length")
    for value in (*preloads, *peaks):
        _require_bytes(value, "Preload/peak sample")
    return max(max(0, peak - preload) for preload, peak in zip(preloads, peaks))


def model_load_requirement(
    model: str,
    *,
    resolution: EnvelopeResolution | None = None,
) -> ModelLoadRequirement:
    """Build requirements from exact resolution evidence or generic fallback."""

    if resolution is None:
        family = _model_family(model)
        if family is None:
            raise ValueError(f"No fallback load budget exists for model {model!r}")
        try:
            host_load = _FALLBACK_HOST_LOAD_BYTES[family]
            device_load = _FALLBACK_DEVICE_LOAD_BYTES[family]
        except KeyError as exc:
            raise ValueError(
                f"No fallback load budget exists for model {model!r}"
            ) from exc
        return ModelLoadRequirement(
            model=model,
            host_incremental_bytes=host_load,
            cgroup_incremental_bytes=host_load,
            device_incremental_bytes=device_load,
            host_margin_bytes=_FALLBACK_HOST_MARGIN_BYTES,
            device_margin_bytes=_FALLBACK_DEVICE_MARGIN_BYTES,
            provenance="fallback",
        )

    resolution = _validated_exact_resolution(resolution)
    envelope = resolution.envelope
    policy = envelope.policy
    measurements = envelope.measurements
    if policy.model != model:
        raise ValueError("Envelope model does not match the requested model")
    return ModelLoadRequirement(
        model=model,
        host_incremental_bytes=measurements.host_incremental_peak_bytes,
        cgroup_incremental_bytes=measurements.cgroup_incremental_peak_bytes,
        device_incremental_bytes=measurements.device_incremental_peak_bytes,
        host_margin_bytes=measurements.host_margin_bytes,
        device_margin_bytes=measurements.device_margin_bytes,
        provenance="envelope",
        envelope_resolution=resolution,
    )


def evaluate_admission(
    requirement: ModelLoadRequirement,
    sample: PressureSample,
    *,
    host_reserve_bytes: int,
    gpu_priority_reserve_bytes: Optional[int] = None,
    require_cgroup: bool = False,
    require_gpu: bool = False,
    expected_gpu_device: Optional[str] = None,
    now: Optional[float] = None,
    maximum_sample_age_seconds: float = 10.0,
) -> AdmissionDecision:
    """Evaluate fresh host, cgroup, and exact-device terms independently."""

    if not isinstance(requirement, ModelLoadRequirement):
        raise TypeError("requirement must be ModelLoadRequirement")
    requirement.__post_init__()
    if not isinstance(sample, PressureSample):
        raise TypeError("sample must be PressureSample")
    host_reserve_bytes = _require_bytes(host_reserve_bytes, "Host reserve")
    if not isinstance(require_cgroup, bool) or not isinstance(require_gpu, bool):
        raise ValueError("Admission requirement flags must be booleans")
    if (
        isinstance(maximum_sample_age_seconds, bool)
        or not isinstance(maximum_sample_age_seconds, (int, float))
        or not math.isfinite(maximum_sample_age_seconds)
        or maximum_sample_age_seconds <= 0
    ):
        raise ValueError("Maximum sample age must be positive")

    host_available = _optional_bytes(sample.host_available_bytes, "Host available")
    host_total = _optional_bytes(sample.host_total_bytes, "Host total", positive=True)
    host_inconsistent = bool(
        host_available is not None
        and host_total is not None
        and host_available > host_total
    )
    cgroup_current = _optional_bytes(sample.cgroup_current_bytes, "Cgroup current")
    cgroup_limit = _optional_bytes(sample.cgroup_limit_bytes, "Cgroup limit")
    gpu_total = _optional_bytes(sample.gpu_total_bytes, "GPU total", positive=True)
    gpu_free = _optional_bytes(sample.gpu_free_bytes, "GPU free")

    time_reason = None
    if now is None:
        time_reason = "decision_time_unavailable"
    else:
        decision_time = _require_time(now, "Decision time")
        if sample.observed_at is None:
            time_reason = "sample_time_unavailable"
        else:
            observed_at = _require_time(sample.observed_at, "Sample observation time")
            if not 0 <= decision_time - observed_at <= maximum_sample_age_seconds:
                time_reason = "sample_stale"

    host_admission = None
    if time_reason is None and host_available is not None and not host_inconsistent:
        host_admission = max(0, host_available - host_reserve_bytes)

    cgroup_admission = None
    if time_reason is None and cgroup_limit is not None and cgroup_current is not None:
        floor = cgroup_headroom_floor(cgroup_limit)
        if floor is not None:
            cgroup_admission = max(
                0,
                cgroup_limit - cgroup_current - floor,
            )

    if host_admission is None or (require_cgroup and cgroup_admission is None):
        effective_host = None
    elif cgroup_admission is None:
        effective_host = host_admission
    else:
        effective_host = min(host_admission, cgroup_admission)

    device_admission = None
    device_valid = not require_gpu
    reserve_below_floor = False
    if require_gpu:
        if not isinstance(expected_gpu_device, str) or not expected_gpu_device:
            raise ValueError("GPU admission requires an exact device")
        reserve = None
        if gpu_priority_reserve_bytes is not None:
            reserve = _require_gpu_reserve_floor(
                gpu_priority_reserve_bytes,
                None,
            )
            reserve_below_floor = bool(
                gpu_total is not None and reserve < _gpu_reserve_floor_bytes(gpu_total)
            )
        device_valid = (
            time_reason is None
            and reserve is not None
            and not reserve_below_floor
            and gpu_total is not None
            and gpu_free is not None
            and sample.gpu_device_id is not None
            and sample.gpu_device_id == expected_gpu_device
            and gpu_free <= gpu_total
        )
        if device_valid:
            observed_at = sample.gpu_observed_at
            device_valid = (
                observed_at is not None
                and 0
                <= _require_time(now, "Decision time")
                - _require_time(observed_at, "GPU observation time")
                <= maximum_sample_age_seconds
            )
        if device_valid:
            device_admission = max(0, gpu_free - reserve)

    reasons = []
    if time_reason is not None:
        reasons.append(time_reason)
    if host_inconsistent:
        reasons.append("host_inconsistent")
    elif host_admission is None:
        reasons.append("host_unavailable")
    if require_cgroup and cgroup_admission is None:
        reasons.append("cgroup_unavailable")
    if effective_host is not None and effective_host < requirement.required_host_bytes:
        reasons.append("insufficient_host")
    if require_gpu:
        if reserve_below_floor:
            reasons.append("gpu_reserve_below_floor")
        elif not device_valid:
            reasons.append("gpu_unavailable")
        elif device_admission < requirement.required_device_bytes:
            reasons.append("insufficient_device")

    return AdmissionDecision(
        admitted=not reasons,
        reasons=tuple(reasons),
        host_admission_bytes=host_admission,
        cgroup_admission_bytes=cgroup_admission,
        effective_host_admission_bytes=effective_host,
        device_admission_bytes=device_admission,
        requirement=requirement,
    )


def _normalized_psi_percent(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(parsed) or not 0 <= parsed <= 100:
        return None
    return parsed


def _maximum_known(*values: object) -> Optional[float]:
    known = [
        parsed
        for value in values
        if (parsed := _normalized_psi_percent(value)) is not None
    ]
    return max(known) if known else None


_CUDA_OOM_PATTERNS = (
    re.compile(r"^\s*(?:runtimeerror:\s*)?cuda out of memory\b", re.IGNORECASE),
    re.compile(
        r"^\s*(?:runtimeerror:\s*)?cuda (?:runtime )?error:\s*out of memory\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:runtimeerror:\s*)?cuda failed with error out of memory\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:runtimeerror:\s*)?(?:cublas|cudnn|cuda)_status_alloc_failed\b",
        re.IGNORECASE,
    ),
)
_CTRANSLATE2_OOM_PATTERNS = (
    re.compile(r"^\s*(?:runtimeerror:\s*)?(?:std::)?bad_alloc\b", re.IGNORECASE),
    re.compile(
        r"^\s*(?:runtimeerror:\s*)?ctranslate2\b.{0,120}"
        r"\b(?:out of memory|failed to allocate|allocation failed)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:runtimeerror:\s*)?"
        r"(?:out of memory|failed to allocate|allocation failed)\b.{0,120}"
        r"\bctranslate2\b",
        re.IGNORECASE,
    ),
)


def is_allocation_failure(error: BaseException) -> bool:
    """Recognize only strong Python, CUDA, or CTranslate2 allocation signals."""

    seen: set[int] = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, MemoryError):
            return True
        if isinstance(current, OSError) and current.errno == errno.ENOMEM:
            return True

        error_type = type(current)
        type_name = error_type.__name__.casefold()
        module_name = error_type.__module__.casefold()
        if type_name == "outofmemoryerror" and (
            "torch" in module_name or "cuda" in module_name
        ):
            return True

        if isinstance(current, RuntimeError):
            message = str(current)
            if any(pattern.search(message) for pattern in _CUDA_OOM_PATTERNS):
                return True
            if any(pattern.search(message) for pattern in _CTRANSLATE2_OOM_PATTERNS):
                return True

        current = current.__cause__ or current.__context__
    return False


class PressureController:
    """Rate-limited three-state pressure and recovery controller."""

    NORMAL = "normal"
    YIELDING = "yielding"
    RECOVERING = "recovering"

    def __init__(
        self,
        sample_reader: Callable[[], PressureSample] = read_pressure_sample,
        *,
        reserve_bytes: Optional[int] = None,
        gpu_reserve_bytes: Optional[int] = None,
        expected_gpu_device: Optional[str] = None,
        canonical_shared_cuda: bool = False,
        explicit_model_authority: bool = False,
        selected_requirement: Optional[ModelLoadRequirement] = None,
        recovery_requirements: Iterable[ModelLoadRequirement] = (),
        require_cgroup: bool = False,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        sample_interval_seconds: float = 1.0,
        priority_reader: Optional[Callable[[], PriorityObservation]] = None,
        priority_observer: Optional[Callable[[dict[str, object]], None]] = None,
        priority_transition_lock: Optional[object] = None,
        priority_interval_seconds: float = 1.0,
        recovery_sample_count: int = 3,
        maximum_wait_seconds: float = 60.0,
        maximum_sample_age_seconds: float = 10.0,
    ) -> None:
        sample_interval_seconds = _positive_finite_number(
            sample_interval_seconds, "Sample interval"
        )
        priority_interval_seconds = _positive_finite_number(
            priority_interval_seconds, "Priority sample interval"
        )
        if priority_interval_seconds > 1.0:
            raise ValueError("Priority sample interval must not exceed one second")
        maximum_wait_seconds = _positive_finite_number(
            maximum_wait_seconds, "Maximum wait"
        )
        maximum_sample_age_seconds = _positive_finite_number(
            maximum_sample_age_seconds, "Maximum sample age"
        )
        if (
            isinstance(recovery_sample_count, bool)
            or not isinstance(recovery_sample_count, int)
            or recovery_sample_count <= 0
        ):
            raise ValueError("Recovery sample count must be a positive integer")
        if maximum_wait_seconds < sample_interval_seconds:
            raise ValueError("Maximum wait must not be below the sample interval")
        if reserve_bytes is not None:
            reserve_bytes = _require_bytes(reserve_bytes, "Host reserve")
        if gpu_reserve_bytes is not None:
            gpu_reserve_bytes = _require_gpu_reserve_floor(
                gpu_reserve_bytes,
                None,
                name="GPU reserve",
            )
        if expected_gpu_device is not None and (
            not isinstance(expected_gpu_device, str) or not expected_gpu_device
        ):
            raise ValueError("Expected GPU device must be a non-empty string")
        if gpu_reserve_bytes is not None and expected_gpu_device is None:
            raise ValueError("GPU pressure control requires an exact GPU device")
        if canonical_shared_cuda and (not gpu_reserve_bytes or not expected_gpu_device):
            raise ValueError(
                "Canonical shared CUDA requires an explicit positive reserve "
                "and exact GPU device"
            )
        if type(explicit_model_authority) is not bool:
            raise ValueError("Explicit model authority must be a boolean")
        if priority_observer is not None and not callable(priority_observer):
            raise TypeError("priority_observer must be callable")
        if priority_transition_lock is not None and not (
            callable(getattr(priority_transition_lock, "__enter__", None))
            and callable(getattr(priority_transition_lock, "__exit__", None))
        ):
            raise TypeError("priority_transition_lock must be a context manager")
        if (priority_observer is None) != (priority_transition_lock is None):
            raise ValueError(
                "priority_observer and priority_transition_lock must be configured together"
            )

        if selected_requirement is not None:
            if not isinstance(selected_requirement, ModelLoadRequirement):
                raise TypeError("selected_requirement must be ModelLoadRequirement")
            selected_requirement.__post_init__()
        candidates = tuple(recovery_requirements)
        if not all(isinstance(item, ModelLoadRequirement) for item in candidates):
            raise TypeError("recovery_requirements must contain ModelLoadRequirement")
        for item in candidates:
            item.__post_init__()
        if selected_requirement is not None:
            candidates = (selected_requirement,)
        if explicit_model_authority and selected_requirement is None:
            raise ValueError(
                "Explicit model authority requires a fixed selected requirement"
            )
        if canonical_shared_cuda and any(
            item.envelope_resolution is None
            and not (
                explicit_model_authority
                and selected_requirement is not None
                and item == selected_requirement
            )
            for item in candidates
        ):
            raise ValueError(
                "Canonical CUDA fallback requires explicit model authority"
            )

        self._lock = threading.RLock()
        self._sample_reader = sample_reader
        self._priority_reader = priority_reader
        self._priority_observer = priority_observer
        self._priority_transition_lock = priority_transition_lock
        self._clock = clock
        self._sleep = sleep
        self._configured_reserve_bytes = reserve_bytes
        self.gpu_reserve_bytes = gpu_reserve_bytes
        self.expected_gpu_device = expected_gpu_device
        self.canonical_shared_cuda = canonical_shared_cuda
        self.explicit_model_authority = explicit_model_authority
        self.selected_requirement = selected_requirement
        self.recovery_requirements = candidates
        self.require_cgroup = require_cgroup
        self.sample_interval_seconds = sample_interval_seconds
        self.priority_interval_seconds = priority_interval_seconds
        self.recovery_sample_count = recovery_sample_count
        self.maximum_wait_seconds = maximum_wait_seconds
        self.maximum_sample_age_seconds = maximum_sample_age_seconds

        self._priority_configured = priority_reader is not None
        self._state = self.RECOVERING if self._priority_configured else self.NORMAL
        self.admission_open = (
            not canonical_shared_cuda and not self._priority_configured
        )
        self.recovery_reason: Optional[str] = (
            "priority_pressure" if self._priority_configured else None
        )
        self.last_pressure_reasons: tuple[str, ...] = ()
        self.last_critical_reasons: tuple[str, ...] = ()
        self._last_sample: Optional[PressureSample] = None
        self._last_sample_at: Optional[float] = None
        self._sample_token = 0
        self._observed_token = -1
        self._consecutive_pressure = 0
        self._consecutive_healthy = 0
        self._consecutive_gpu_unavailable = 0
        self._last_oom_counts: tuple[Optional[int], Optional[int]] = (None, None)
        self._last_observed_at: Optional[float] = None
        self._last_observed_sample: Optional[PressureSample] = None
        self._last_priority_poll_at: Optional[float] = None
        self._priority_observation = PriorityObservation(
            state="unavailable" if self._priority_configured else "disabled",
            configured=self._priority_configured,
        )
        self._priority_observed_at: Optional[float] = None
        self._priority_transition_observation_digest: Optional[str] = None
        self._priority_transition_sequence = 0
        self._priority_distinct_clear_count = 0
        self._priority_recovery_generation_high_water: Optional[int] = None
        self._external_pressure_episode_pending = False
        self._external_pressure_recovery_generation = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def should_yield(self) -> bool:
        with self._lock:
            return self._state == self.YIELDING

    @property
    def healthy_recovery_samples(self) -> int:
        with self._lock:
            return self._consecutive_healthy

    @property
    def priority_configured(self) -> bool:
        return self._priority_configured

    @property
    def external_pressure_recovery_generation(self) -> int:
        """Count completed external assert/unavailable recovery episodes."""

        with self._lock:
            return self._external_pressure_recovery_generation

    @property
    def poll_interval_seconds(self) -> float:
        if self._priority_configured:
            return min(self.sample_interval_seconds, self.priority_interval_seconds)
        return self.sample_interval_seconds

    def _priority_recovery_ready_locked(self) -> bool:
        return bool(
            not self._priority_configured
            or (
                self._priority_observation.state == "clear"
                and self._priority_distinct_clear_count
                >= _PRIORITY_RECOVERY_CLEAR_COUNT
            )
        )

    def _maybe_finish_recovery_locked(self) -> None:
        if (
            self._state == self.RECOVERING
            and self._consecutive_healthy >= self.recovery_sample_count
            and self._priority_recovery_ready_locked()
        ):
            if self._external_pressure_episode_pending:
                if self._external_pressure_recovery_generation == _MAX_BYTES:
                    raise RuntimeError(
                        "External pressure recovery generation exhausted"
                    )
                self._external_pressure_recovery_generation += 1
                self._external_pressure_episode_pending = False
            self._consecutive_healthy = 0
            self._state = self.NORMAL
            self.recovery_reason = None
            self.admission_open = True

    @staticmethod
    def _validate_priority_observation(observation: PriorityObservation) -> None:
        if not isinstance(observation, PriorityObservation):
            raise TypeError("priority_reader must return PriorityObservation")
        if type(observation.configured) is not bool:
            raise ValueError("Priority configured state must be a boolean")
        if observation.state not in {
            "disabled",
            "clear",
            "neutral",
            "asserted",
            "unavailable",
        }:
            raise ValueError("Priority observation state is invalid")
        if observation.configured == (observation.state == "disabled"):
            raise ValueError("Priority observation configuration is inconsistent")
        for value, name in (
            (observation.accepted, "accepted"),
            (observation.new_publication, "new publication"),
            (observation.producer_epoch_changed, "producer epoch change"),
            (observation.sequence_gap, "sequence gap"),
        ):
            if type(value) is not bool:
                raise ValueError(f"Priority {name} flag must be a boolean")
        if observation.new_publication and not observation.accepted:
            raise ValueError("A new priority publication must be accepted")
        if observation.producer_epoch_changed and not observation.new_publication:
            raise ValueError("A priority epoch change must be a new publication")

    def _priority_transition_locked(
        self,
        observation: PriorityObservation,
        *,
        previous_state: str,
    ) -> None:
        if (
            observation.state == previous_state
            and not observation.producer_epoch_changed
        ):
            return
        if self._priority_transition_sequence == _MAX_BYTES:
            raise RuntimeError("Priority transition generation exhausted")
        self._priority_transition_sequence += 1
        self._priority_transition_observation_digest = (
            observation.observation_digest if observation.accepted else None
        )

    def _observe_priority_locked(
        self,
        observation: PriorityObservation,
        *,
        model_resident: bool,
    ) -> str:
        self._validate_priority_observation(observation)
        if not self._priority_configured:
            if observation.configured or observation.state != "disabled":
                raise ValueError("Disabled priority control received configured input")
            return self._state
        if not observation.configured or observation.state == "disabled":
            raise ValueError("Configured priority control received disabled input")

        previous_state = self._priority_observation.state
        self._priority_observation = observation
        self._priority_observed_at = self._clock()
        self._priority_transition_locked(
            observation,
            previous_state=previous_state,
        )

        reset_recovery = bool(
            observation.state in {"asserted", "neutral", "unavailable"}
            or observation.producer_epoch_changed
        )
        if reset_recovery:
            self._priority_distinct_clear_count = 0
            source_generation = observation.source_generation
            if source_generation is not None:
                if (
                    isinstance(source_generation, bool)
                    or not isinstance(source_generation, int)
                    or source_generation <= 0
                ):
                    raise ValueError("Priority source generation is invalid")
                if (
                    self._priority_recovery_generation_high_water is None
                    or source_generation > self._priority_recovery_generation_high_water
                ):
                    self._priority_recovery_generation_high_water = source_generation

        critical = bool(
            observation.state in {"asserted", "unavailable"}
            or observation.producer_epoch_changed
        )
        if observation.state in {"asserted", "unavailable"}:
            self._external_pressure_episode_pending = True
        if critical:
            self.admission_open = False
            self._consecutive_healthy = 0
            self._state = self.YIELDING if model_resident else self.RECOVERING
            self.recovery_reason = "priority_pressure"
            return self._state

        if observation.state == "neutral":
            self.admission_open = False
            self._consecutive_healthy = 0
            if self._state == self.NORMAL:
                self._state = self.RECOVERING
            self.recovery_reason = "priority_pressure"
            return self._state

        if (
            observation.state == "clear"
            and observation.new_publication
            and not observation.producer_epoch_changed
        ):
            source_generation = observation.source_generation
            if (
                isinstance(source_generation, bool)
                or not isinstance(source_generation, int)
                or source_generation <= 0
            ):
                raise ValueError("Accepted clear priority input needs a generation")
            if (
                self._priority_recovery_generation_high_water is None
                or source_generation > self._priority_recovery_generation_high_water
            ):
                self._priority_recovery_generation_high_water = source_generation
                self._priority_distinct_clear_count = min(
                    _PRIORITY_RECOVERY_CLEAR_COUNT,
                    self._priority_distinct_clear_count + 1,
                )
        self._maybe_finish_recovery_locked()
        return self._state

    def _poll_priority_locked(
        self,
        *,
        model_resident: bool,
        observation: Optional[PriorityObservation] = None,
        force: bool = False,
    ) -> Optional[dict[str, object]]:
        if not self._priority_configured:
            if observation is not None:
                self._observe_priority_locked(
                    observation,
                    model_resident=model_resident,
                )
                return self._gate_priority_snapshot_locked()
            return None
        now = self._clock()
        if observation is None:
            if (
                not force
                and self._last_priority_poll_at is not None
                and now - self._last_priority_poll_at < self.priority_interval_seconds
            ):
                return None
            observation = self._priority_reader()
        self._last_priority_poll_at = now
        self._observe_priority_locked(
            observation,
            model_resident=model_resident,
        )
        return self._gate_priority_snapshot_locked()

    def _gate_priority_snapshot_locked(self) -> dict[str, object]:
        """Capture the exact gate-only priority fields after one observation."""

        status = self._priority_status_snapshot_locked(
            {
                "model_resident": False,
                "model_load_generation": 0,
                "model_unload_generation": 0,
            }
        )
        for name in (
            "model_resident",
            "model_load_generation",
            "model_unload_generation",
        ):
            status.pop(name)
        status["source_generation"] = (
            self._priority_observation.source_generation
            if self._priority_configured
            else None
        )
        status["admission_open"] = self.admission_open
        return status

    def _publish_priority_snapshot(self, snapshot: Optional[dict[str, object]]) -> None:
        observer = self._priority_observer
        if snapshot is not None and observer is not None:
            observer(snapshot)

    def _priority_transition_scope(self):
        """Serialize gate mutations beneath the model-condition lock."""

        if self._priority_transition_lock is None:
            return nullcontext()
        return self._priority_transition_lock

    def _receipt_transition_key_locked(self):
        """Return controller fields whose mutation requires a durable receipt."""

        if self._priority_observer is None:
            return None
        observation = self._priority_observation
        return (
            self._state,
            self.admission_open,
            self.recovery_reason,
            observation,
            self._priority_transition_observation_digest,
            self._priority_transition_sequence,
            self._priority_distinct_clear_count,
            self._priority_recovery_generation_high_water,
        )

    def _publish_transition_locked(self, previous, *, force: bool = False) -> None:
        """Fsync a changed gate snapshot before either lock is released."""

        if self._priority_observer is None:
            return
        if force or self._receipt_transition_key_locked() != previous:
            try:
                self._publish_priority_snapshot(self._gate_priority_snapshot_locked())
            except BaseException:
                self._latch_receipt_failure_locked()
                raise

    def _latch_receipt_failure_locked(self) -> None:
        """Close admission after receipt loss without recursively publishing."""

        if self._state != self.YIELDING:
            self._state = self.RECOVERING
        self.admission_open = False
        self.recovery_reason = "receipt_unavailable"
        self._consecutive_pressure = 0
        self._consecutive_healthy = 0

    def latch_receipt_failure_without_publication(self) -> None:
        """Fail closed for a receipt failure detected by the runtime owner.

        The model-runtime condition is the outer lock at this call site.  This
        method deliberately acquires only the controller lock and never invokes
        the configured receipt observer, avoiding a recursive journal write.
        """

        with self._lock:
            self._latch_receipt_failure_locked()

    def latch_release_failure_without_publication(
        self,
        *,
        model_resident: bool,
    ) -> None:
        """Quarantine a still-resident backend after release cannot be proved."""

        if type(model_resident) is not bool:
            raise TypeError("Release-failure residency state must be a boolean")
        with self._lock:
            self._state = self.YIELDING if model_resident else self.RECOVERING
            self.admission_open = False
            self.recovery_reason = "model_release_failed"
            self._consecutive_pressure = 0
            self._consecutive_healthy = 0

    def sample(self) -> PressureSample:
        """Return a cached sample when called inside the five-second window."""

        with self._lock:
            now = self._clock()
            if (
                self._last_sample is not None
                and self._last_sample_at is not None
                and now - self._last_sample_at < self.sample_interval_seconds
            ):
                return self._last_sample

            sample = self._sample_reader()
            if not isinstance(sample, PressureSample):
                raise TypeError("sample_reader must return PressureSample")
            self._last_sample = sample
            self._last_sample_at = now
            self._sample_token += 1
            return sample

    def _reserve_for(self, sample: PressureSample) -> int:
        if self._configured_reserve_bytes is not None:
            return self._configured_reserve_bytes
        return host_reserve_bytes(sample.host_total_bytes)

    def _classify(
        self, sample: PressureSample
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        pressure = []
        critical = []
        reserve = self._reserve_for(sample)
        host_available = _optional_bytes(sample.host_available_bytes, "Host available")
        host_total = _optional_bytes(
            sample.host_total_bytes, "Host total", positive=True
        )
        if (
            host_available is not None
            and host_total is not None
            and host_available > host_total
        ):
            critical.append("host_inconsistent")
        elif host_available is not None and host_available < reserve:
            critical.append("host_headroom")

        floor = cgroup_headroom_floor(sample.cgroup_limit_bytes)
        headroom = None
        if (
            sample.cgroup_limit_bytes is not None
            and sample.cgroup_current_bytes is not None
        ):
            headroom = _require_bytes(
                sample.cgroup_limit_bytes, "Cgroup limit"
            ) - _require_bytes(sample.cgroup_current_bytes, "Cgroup current")
        if floor is not None and headroom is not None:
            if headroom < floor:
                pressure.append("cgroup_headroom")
            if headroom * 2 < floor:
                critical.append("critical_cgroup_headroom")

        if self.gpu_reserve_bytes is not None and self._gpu_telemetry_fresh(sample):
            if not self._gpu_reserve_floor_satisfied(sample):
                critical.append("gpu_reserve_below_floor")
            else:
                if sample.gpu_free_bytes < self.gpu_reserve_bytes:
                    pressure.append("gpu_headroom")
                if sample.gpu_free_bytes * 2 < self.gpu_reserve_bytes:
                    critical.append("critical_gpu_headroom")

        some_values = (
            sample.psi_some_avg10,
            sample.host_psi_some_avg10,
            sample.cgroup_psi_some_avg10,
        )
        full_values = (
            sample.psi_full_avg10,
            sample.host_psi_full_avg10,
            sample.cgroup_psi_full_avg10,
        )
        some = _maximum_known(*some_values)
        full = _maximum_known(*full_values)
        if full is not None and full >= 1.0:
            pressure.append("psi_full")
        if some is not None and some >= 10.0:
            pressure.append("psi_some")

        current_counts = (sample.cgroup_oom_events, sample.cgroup_oom_kill_events)
        baselines = list(self._last_oom_counts)
        for index, current in enumerate(current_counts):
            if current is None:
                continue
            current = _require_bytes(current, "Cgroup OOM counter")
            previous = baselines[index]
            if previous is not None and current > previous:
                critical.append("cgroup_oom")
            baselines[index] = current
        self._last_oom_counts = (baselines[0], baselines[1])
        return tuple(dict.fromkeys(pressure)), tuple(dict.fromkeys(critical))

    def _gpu_telemetry_fresh(self, sample: PressureSample) -> bool:
        if self.gpu_reserve_bytes is None:
            return True
        return _gpu_sample_is_fresh(
            sample,
            expected_device=self.expected_gpu_device,
            now=self._clock(),
            maximum_age_seconds=self.maximum_sample_age_seconds,
        )

    def _gpu_reserve_floor_satisfied(self, sample: PressureSample) -> bool:
        if self.gpu_reserve_bytes is None:
            return True
        try:
            total = _optional_bytes(sample.gpu_total_bytes, "GPU total", positive=True)
        except ValueError:
            return False
        return bool(
            total is not None
            and self.gpu_reserve_bytes >= _gpu_reserve_floor_bytes(total)
        )

    def _gpu_fresh(self, sample: PressureSample) -> bool:
        return self._gpu_telemetry_fresh(sample) and self._gpu_reserve_floor_satisfied(
            sample
        )

    def _recovery_qualified(self, sample: PressureSample) -> bool:
        if not self.recovery_requirements:
            return False
        if self.gpu_reserve_bytes is not None and not self._gpu_fresh(sample):
            return False
        for requirement in self.recovery_requirements:
            if not self._canonical_requirement_allowed(requirement):
                continue
            if evaluate_admission(
                requirement,
                sample,
                host_reserve_bytes=self._reserve_for(sample),
                gpu_priority_reserve_bytes=self.gpu_reserve_bytes,
                require_cgroup=self.require_cgroup,
                require_gpu=self.gpu_reserve_bytes is not None,
                expected_gpu_device=self.expected_gpu_device,
                now=self._clock(),
                maximum_sample_age_seconds=self.maximum_sample_age_seconds,
            ).admitted:
                return True
        return False

    def _canonical_requirement_allowed(
        self,
        requirement: ModelLoadRequirement,
    ) -> bool:
        if (
            not self.canonical_shared_cuda
            or requirement.envelope_resolution is not None
        ):
            return True
        return bool(
            self.explicit_model_authority
            and self.selected_requirement is not None
            and requirement == self.selected_requirement
        )

    def _observe_locked(
        self,
        sample: PressureSample,
        *,
        model_resident: bool,
    ) -> str:
        if not isinstance(sample, PressureSample):
            raise TypeError("sample must be PressureSample")
        observed_at = _require_time(sample.observed_at, "Sample observation time")
        if self._last_observed_at is not None and observed_at <= self._last_observed_at:
            return self._state
        self._last_observed_at = observed_at
        self._last_observed_sample = sample
        pressure, critical = self._classify(sample)
        self.last_pressure_reasons = pressure
        self.last_critical_reasons = critical
        pressured = bool(pressure or critical)

        gpu_fresh = self._gpu_fresh(sample)
        if self.canonical_shared_cuda:
            if gpu_fresh:
                self._consecutive_gpu_unavailable = 0
            else:
                self._consecutive_gpu_unavailable += 1
                self.admission_open = False
                if self._consecutive_gpu_unavailable >= 2:
                    self._consecutive_pressure = 0
                    self._consecutive_healthy = 0
                    self._state = self.YIELDING if model_resident else self.RECOVERING
                    self.recovery_reason = "gpu_telemetry_unavailable"

        if self._state == self.NORMAL:
            self._consecutive_healthy = 0
            if critical:
                self._consecutive_pressure = 0
                self._state = self.YIELDING
            elif pressure:
                self._consecutive_pressure += 1
                if self._consecutive_pressure >= 2:
                    self._consecutive_pressure = 0
                    self._state = self.YIELDING
            else:
                self._consecutive_pressure = 0
                if (
                    not self.canonical_shared_cuda or gpu_fresh
                ) and self._priority_recovery_ready_locked():
                    self.admission_open = True
        elif self._state == self.RECOVERING:
            self._consecutive_pressure = 0
            qualified = not pressured and self._recovery_qualified(sample)
            if not qualified:
                self._consecutive_healthy = 0
            else:
                self._consecutive_healthy = min(
                    self.recovery_sample_count,
                    self._consecutive_healthy + 1,
                )
                self._maybe_finish_recovery_locked()
        if self._state == self.YIELDING:
            self.admission_open = False
        return self._state

    def observe(self, sample: PressureSample, *, model_resident: bool = True) -> str:
        """Apply one timestamp-distinct sample under the controller lock."""

        with self._priority_transition_scope():
            with self._lock:
                previous = self._receipt_transition_key_locked()
                self._observe_locked(
                    sample,
                    model_resident=model_resident,
                )
                priority_snapshot = self._poll_priority_locked(
                    model_resident=model_resident,
                    observation=sample.priority_observation,
                )
                self._publish_transition_locked(
                    previous,
                    force=priority_snapshot is not None,
                )
                return self._state

    def poll_priority(
        self,
        *,
        model_resident: bool = True,
        force: bool = False,
    ) -> str:
        """Poll only the required priority signal on its independent cadence."""

        if type(force) is not bool:
            raise TypeError("force must be a boolean")

        with self._priority_transition_scope():
            with self._lock:
                previous = self._receipt_transition_key_locked()
                priority_snapshot = self._poll_priority_locked(
                    model_resident=model_resident,
                    force=force,
                )
                self._publish_transition_locked(
                    previous,
                    force=priority_snapshot is not None,
                )
                return self._state

    def poll(
        self,
        *,
        model_resident: bool = True,
        force_priority: bool = False,
    ) -> str:
        """Read at most one fresh sample per interval and apply it once."""

        if type(force_priority) is not bool:
            raise TypeError("force_priority must be a boolean")

        with self._priority_transition_scope():
            with self._lock:
                previous = self._receipt_transition_key_locked()
                priority_observed = (
                    self._poll_priority_locked(
                        model_resident=model_resident,
                        force=force_priority,
                    )
                    is not None
                )
                if self._state != self.YIELDING:
                    sample = self.sample()
                    priority_observation = None
                    if self._observed_token != self._sample_token:
                        self._observe_locked(
                            sample,
                            model_resident=model_resident,
                        )
                        self._observed_token = self._sample_token
                        priority_observation = sample.priority_observation
                    if priority_observation is not None:
                        priority_observed = bool(
                            self._poll_priority_locked(
                                model_resident=model_resident,
                                observation=priority_observation,
                            )
                            is not None
                            or priority_observed
                        )
                self._publish_transition_locked(
                    previous,
                    force=priority_observed,
                )
                return self._state

    def poll_idle_resident(self) -> bool:
        """Poll a loaded idle CUDA model and report whether it should unload."""

        return self.poll(model_resident=True) == self.YIELDING

    def check_or_raise(self, *, force_priority: bool = False) -> str:
        """Pressure callback entry point; raise only for the yielding transition."""

        state = self.poll(force_priority=force_priority)
        if state == self.YIELDING:
            with self._lock:
                reasons = self.last_critical_reasons or self.last_pressure_reasons
            detail = ", ".join(reasons) if reasons else "memory pressure"
            raise MemoryPressureYield(detail)
        return state

    def _mark_released_locked(self, reason: Optional[str]) -> str:
        self._state = self.RECOVERING
        self.admission_open = False
        if reason is not None:
            self.recovery_reason = reason
        self._consecutive_pressure = 0
        self._consecutive_healthy = 0
        return self._state

    def close_admission(self) -> None:
        """Close controller admission beneath its own lock."""

        with self._priority_transition_scope():
            with self._lock:
                previous = self._receipt_transition_key_locked()
                self.admission_open = False
                self._publish_transition_locked(previous)

    def open_admission_if_normal(self) -> bool:
        """Open only when both controller inputs have fully recovered."""

        with self._priority_transition_scope():
            with self._lock:
                previous = self._receipt_transition_key_locked()
                opened = bool(
                    self._state == self.NORMAL
                    and self._priority_recovery_ready_locked()
                    and (
                        not self.canonical_shared_cuda
                        or (
                            self._last_observed_sample is not None
                            and self._consecutive_gpu_unavailable == 0
                            and self._gpu_fresh(self._last_observed_sample)
                        )
                    )
                )
                self.admission_open = opened
                self._publish_transition_locked(previous)
                return opened

    def mark_released(self, reason: Optional[str] = None) -> str:
        """Mark model/cache release complete and start recovery hysteresis."""

        with self._priority_transition_scope():
            with self._lock:
                previous = self._receipt_transition_key_locked()
                state = self._mark_released_locked(reason)
                self._publish_transition_locked(previous)
                return state

    begin_recovery = mark_released

    def enter_no_safe_model(
        self,
        requirements: Optional[Iterable[ModelLoadRequirement]] = None,
    ) -> str:
        """Wait without attempting a load when even tiny is inadmissible."""

        with self._priority_transition_scope():
            with self._lock:
                previous = self._receipt_transition_key_locked()
                if requirements is not None:
                    candidates = tuple(requirements)
                    if not all(
                        isinstance(item, ModelLoadRequirement) for item in candidates
                    ):
                        raise TypeError(
                            "recovery requirements must contain ModelLoadRequirement"
                        )
                    for item in candidates:
                        item.__post_init__()
                    if self.selected_requirement is not None and candidates != (
                        self.selected_requirement,
                    ):
                        raise ValueError("The selected model requirement is fixed")
                    if any(
                        not self._canonical_requirement_allowed(item)
                        for item in candidates
                    ):
                        raise ValueError(
                            "Canonical CUDA fallback requires explicit model authority"
                        )
                    self.recovery_requirements = candidates
                state = self._mark_released_locked("no_safe_model")
                self._publish_transition_locked(previous)
                return state

    def immediate_load_admission(
        self,
        requirement: Optional[ModelLoadRequirement] = None,
        *,
        sample_reader: Optional[Callable[[], PressureSample]] = None,
    ) -> AdmissionDecision:
        """Bypass throttling for the fresh check inside every load/reload gate."""

        with self._priority_transition_scope():
            with self._lock:
                previous = self._receipt_transition_key_locked()
                decision, priority_snapshot = self._immediate_load_admission_locked(
                    requirement,
                    sample_reader=sample_reader,
                )
                self._publish_transition_locked(
                    previous,
                    force=priority_snapshot is not None,
                )
                return decision

    def _immediate_load_admission_locked(
        self,
        requirement: Optional[ModelLoadRequirement],
        *,
        sample_reader: Optional[Callable[[], PressureSample]],
    ) -> tuple[AdmissionDecision, Optional[dict[str, object]]]:
        selected = requirement or self.selected_requirement
        if selected is None:
            raise ValueError("A selected model load requirement is required")
        selected.__post_init__()
        if (
            self.selected_requirement is not None
            and selected != self.selected_requirement
        ):
            raise ValueError("The selected model requirement is fixed")
        if not self._canonical_requirement_allowed(selected):
            raise ValueError(
                "Canonical CUDA fallback requires explicit model authority"
            )

        def denied() -> AdmissionDecision:
            return AdmissionDecision(
                admitted=False,
                reasons=(f"controller_{self._state}",),
                host_admission_bytes=None,
                cgroup_admission_bytes=None,
                effective_host_admission_bytes=None,
                device_admission_bytes=None,
                requirement=selected,
            )

        if self._state != self.NORMAL:
            self.admission_open = False
            return denied(), None

        sample = (sample_reader or self._sample_reader)()
        if not isinstance(sample, PressureSample):
            raise TypeError("sample_reader must return PressureSample")
        priority_snapshot = None
        if sample.priority_observation is not None or self._priority_configured:
            priority_snapshot = self._poll_priority_locked(
                model_resident=False,
                observation=sample.priority_observation,
                force=sample.priority_observation is None,
            )
            if self._state != self.NORMAL:
                self.admission_open = False
                return denied(), priority_snapshot
        decision = evaluate_admission(
            selected,
            sample,
            host_reserve_bytes=self._reserve_for(sample),
            gpu_priority_reserve_bytes=self.gpu_reserve_bytes,
            require_cgroup=self.require_cgroup,
            require_gpu=self.gpu_reserve_bytes is not None,
            expected_gpu_device=self.expected_gpu_device,
            now=self._clock(),
            maximum_sample_age_seconds=self.maximum_sample_age_seconds,
        )
        self.admission_open = bool(
            decision.admitted
            and self._state == self.NORMAL
            and self._priority_recovery_ready_locked()
        )
        if not decision.admitted:
            reason = (
                "gpu_telemetry_unavailable"
                if "gpu_unavailable" in decision.reasons
                else "insufficient_capacity"
            )
            self._mark_released_locked(reason)
        return decision, priority_snapshot

    @staticmethod
    def _cancelled(cancelled: object) -> bool:
        if cancelled is None:
            return False
        is_set = getattr(cancelled, "is_set", None)
        if callable(is_set):
            return bool(is_set())
        if callable(cancelled):
            return bool(cancelled())
        return bool(cancelled)

    def wait_for_recovery(
        self,
        cancelled: object = None,
        *,
        heartbeat: Optional[Callable[[str, float], None]] = None,
    ) -> bool:
        """Wait with 5-60 second exponential polling until recovery or cancellation."""

        if self.state == self.NORMAL:
            return True
        if self.state == self.YIELDING:
            self.mark_released()

        delay = (
            self.priority_interval_seconds
            if self._priority_configured
            else max(5.0, self.poll_interval_seconds)
        )
        while self.state != self.NORMAL:
            if self._cancelled(cancelled):
                return False
            if heartbeat is not None:
                heartbeat(self.state, delay)

            event_wait = getattr(cancelled, "wait", None)
            if callable(event_wait) and callable(getattr(cancelled, "is_set", None)):
                if event_wait(delay):
                    return False
            else:
                self._sleep(delay)

            if self._cancelled(cancelled):
                return False
            self.poll(model_resident=False)
            if self._priority_configured:
                delay = self.priority_interval_seconds
            else:
                delay = min(delay * 2, self.maximum_wait_seconds)
        return True

    def _coarse_recovery_reason_locked(self) -> Optional[str]:
        if self._state == self.NORMAL:
            return None
        if self._priority_configured and not self._priority_recovery_ready_locked():
            return "priority_pressure"
        reason = self.recovery_reason
        if reason == "priority_pressure":
            return "priority_pressure"
        if reason in {
            "no_safe_model",
            "insufficient_capacity",
            "model_load_profile_unhealthy",
            "model_load_allocation_failure",
            "release_during_selection",
        }:
            return "model_admission"
        return "resource_pressure"

    def _bounded_priority_age_locked(
        self,
        value: Optional[int],
        *,
        now: float,
    ) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("Priority age state is invalid")
        if self._priority_observed_at is None:
            return min(60_000, value)
        elapsed_ms = max(0, int((now - self._priority_observed_at) * 1000))
        return min(60_000, value + elapsed_ms)

    def _priority_status_snapshot_locked(self, model_snapshot: dict) -> dict:
        observation = self._priority_observation
        configured = self._priority_configured
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise RuntimeError("Controller clock state is invalid")
        now = float(now)
        if not math.isfinite(now):
            raise RuntimeError("Controller clock state is invalid")
        return {
            "configured": configured,
            "state": observation.state if configured else "disabled",
            "heartbeat_age_ms": (
                self._bounded_priority_age_locked(
                    observation.heartbeat_age_ms,
                    now=now,
                )
                if configured
                else None
            ),
            "source_age_ms": (
                self._bounded_priority_age_locked(
                    observation.source_age_ms,
                    now=now,
                )
                if configured
                else None
            ),
            "policy_sha256": observation.policy_sha256 if configured else None,
            "observation_digest": (
                observation.observation_digest if configured else None
            ),
            "transition_observation_digest": (
                self._priority_transition_observation_digest if configured else None
            ),
            "transition_sequence": (
                self._priority_transition_sequence if configured else 0
            ),
            "controller_phase": self._state,
            "recovery_reason": self._coarse_recovery_reason_locked(),
            "distinct_clear_count": (
                self._priority_distinct_clear_count if configured else 0
            ),
            "model_resident": model_snapshot["model_resident"],
            "model_load_generation": model_snapshot["model_load_generation"],
            "model_unload_generation": model_snapshot["model_unload_generation"],
        }

    @staticmethod
    def _validate_model_snapshot(model_snapshot: dict) -> None:
        if not isinstance(model_snapshot, dict):
            raise TypeError("model_snapshot must be a dictionary")
        required = {
            "model_resident",
            "model_load_generation",
            "model_unload_generation",
        }
        if not required.issubset(model_snapshot):
            raise ValueError("model_snapshot is incomplete")
        model_resident = model_snapshot["model_resident"]
        if type(model_resident) is not bool:
            raise ValueError("model_resident must be a boolean")
        for name in ("model_load_generation", "model_unload_generation"):
            value = model_snapshot[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_BYTES
            ):
                raise ValueError(f"{name} is invalid")

    def priority_status_snapshot(self, model_snapshot: dict) -> dict:
        """Return the exact privacy-safe priority/model causal snapshot.

        The runtime owner must call this while holding its model condition.  This
        method then acquires only the controller lock, preserving the documented
        model-condition -> controller lock order.
        """

        self._validate_model_snapshot(model_snapshot)

        with self._lock:
            return self._priority_status_snapshot_locked(model_snapshot)

    def gate_priority_status_snapshot(self) -> dict[str, object]:
        """Return gate-only source-generation evidence without model fields."""

        with self._lock:
            return self._gate_priority_snapshot_locked()

    def runtime_status_snapshot(self, model_snapshot: dict) -> dict:
        """Capture controller and priority fields beneath one controller lock."""

        self._validate_model_snapshot(model_snapshot)
        with self._lock:
            return {
                "controller_state": self._state,
                "recovery_reason": self.recovery_reason,
                "admission_open": self.admission_open,
                "priority_pressure": self._priority_status_snapshot_locked(
                    model_snapshot
                ),
            }


class AdaptiveChunkState:
    """Mutable per-file chunk duration and minimum-allocation failure state."""

    def __init__(
        self,
        baseline_seconds: int,
        *,
        minimum_seconds: int = 5 * 60,
        successes_to_grow: int = 3,
    ) -> None:
        for value, label in (
            (baseline_seconds, "baseline"),
            (minimum_seconds, "minimum"),
            (successes_to_grow, "success count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"Chunk {label} must be a positive integer")
        if baseline_seconds < minimum_seconds:
            raise ValueError("Chunk baseline must not be below its minimum")

        self.baseline_seconds = baseline_seconds
        self.minimum_seconds = minimum_seconds
        self.successes_to_grow = successes_to_grow
        self.current_seconds = baseline_seconds
        self.minimum_allocation_failures = 0
        self.consecutive_healthy_successes = 0

    @property
    def exhausted(self) -> bool:
        return self.minimum_allocation_failures >= 2

    def shrink(self) -> int:
        self.current_seconds = max(self.minimum_seconds, self.current_seconds // 2)
        self.consecutive_healthy_successes = 0
        return self.current_seconds

    def record_pressure_yield(self) -> int:
        """Shrink after external pressure without counting a media failure."""

        self.minimum_allocation_failures = 0
        return self.shrink()

    on_pressure_yield = record_pressure_yield

    def record_external_pressure_recovery(self) -> None:
        """Separate minimum allocation failures across a pressure transition."""

        self.minimum_allocation_failures = 0
        self.consecutive_healthy_successes = 0

    def record_allocation_failure(self) -> bool:
        """Shrink and report whether two attempts at the minimum have failed."""

        failed_at_minimum = self.current_seconds == self.minimum_seconds
        self.shrink()
        if failed_at_minimum:
            self.minimum_allocation_failures += 1
        else:
            self.minimum_allocation_failures = 0
        return self.exhausted

    on_allocation_failure = record_allocation_failure

    def record_success(self, *, healthy: bool) -> int:
        """Grow after three consecutive successful chunks with healthy samples."""

        self.minimum_allocation_failures = 0
        if not healthy or self.current_seconds >= self.baseline_seconds:
            self.consecutive_healthy_successes = 0
            return self.current_seconds

        self.consecutive_healthy_successes += 1
        if self.consecutive_healthy_successes >= self.successes_to_grow:
            self.current_seconds = min(
                self.baseline_seconds,
                self.current_seconds * 2,
            )
            self.consecutive_healthy_successes = 0
        return self.current_seconds

    on_success = record_success


__all__ = [
    "AdaptiveChunkState",
    "AdmissionDecision",
    "CapacityProfile",
    "GIB",
    "MIB",
    "MemoryPressureYield",
    "ModelDecision",
    "ModelLoadRequirement",
    "PressureController",
    "PressureSample",
    "StabilizedGpuCapacity",
    "cgroup_headroom_floor",
    "discover_capacity",
    "evaluate_admission",
    "fallback_model_ceiling",
    "gpu_priority_reserve_bytes",
    "host_reserve_bytes",
    "initial_chunk_seconds",
    "is_allocation_failure",
    "model_load_requirement",
    "paired_incremental_peak_bytes",
    "read_pressure_sample",
    "select_model",
    "stabilize_gpu_capacity",
]
