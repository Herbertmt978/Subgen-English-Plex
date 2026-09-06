"""User execution preferences; resource admission remains in resource_management.

This module deliberately does not inspect hardware or modify reserves. The
working-budget percentage is for chunk planning, never model selection.
"""

from dataclasses import dataclass
import re
from typing import Mapping


_ACTIVITIES = {
    "passive": (50, 10, 5),
    "balanced": (75, 20, 1),
    "max": (100, 30, 0),
}
_RUN_MODES = ("adaptive", "dedicated")


@dataclass(frozen=True)
class ExecutionDevice:
    """One discovered adapter, before memory admission or model loading.

    UUIDs use the normalized physical UUID from discovery, not backend indexes.
    The same card may appear in both CUDA and Vulkan inventories.
    """

    backend: str
    index: int
    physical_uuid: str
    name: str
    memory_topology: str

    def __post_init__(self):
        if self.backend not in ("cuda", "vulkan"):
            raise ValueError("Device backend must be cuda or vulkan")
        if type(self.index) is not int or not 0 <= self.index < 32:
            raise ValueError("Device index must be between 0 and 31")
        if (not isinstance(self.physical_uuid, str)
                or re.fullmatch(r"[0-9a-f]{32}", self.physical_uuid) is None
                or self.physical_uuid == "0" * 32):
            raise ValueError("Device requires a normalized physical UUID")
        if (not isinstance(self.name, str) or not 1 <= len(self.name) <= 256
                or any(ord(c) < 32 for c in self.name)):
            raise ValueError("Device requires a printable name")
        if self.memory_topology not in ("shared", "dedicated"):
            raise ValueError("Device memory topology must be known")

    @property
    def selector(self):
        return f"{self.backend}:{self.index}"


def resolve_execution_devices(value, inventory):
    """Resolve an explicit, bounded device list without fallback or allocation.

    An unset/blank value preserves the existing single-device composition path.
    Three or more devices use the same contract as two; 32 is the probe bound,
    not a claim that a 32-card machine has been physically qualified.
    """
    if value is None or value == "":
        return ()
    if not isinstance(value, str) or len(value) > 1024:
        raise ValueError("SUBGEN_DEVICES requires a bounded comma-separated list")
    if not value.strip():
        return ()
    selectors = tuple(part.strip().lower() for part in value.split(","))
    if not 1 <= len(selectors) <= 32 or any(
        re.fullmatch(r"(?:cuda|vulkan):(?:0|[1-9][0-9]?)", part) is None
        for part in selectors
    ):
        raise ValueError("SUBGEN_DEVICES must list cuda:N or vulkan:N devices")
    if len(set(selectors)) != len(selectors):
        raise ValueError("SUBGEN_DEVICES repeats a device")
    if not isinstance(inventory, (tuple, list)) or len(inventory) > 64:
        raise ValueError("Device discovery requires a bounded inventory")
    discovered = {}
    for device in inventory:
        if type(device) is not ExecutionDevice:
            raise TypeError("Inventory must contain ExecutionDevice observations")
        device.__post_init__()
        if device.selector in discovered:
            raise ValueError("Device discovery repeats a backend index")
        discovered[device.selector] = device
    if any(selector not in discovered for selector in selectors):
        raise ValueError("A selected GPU is unavailable; no substitute was selected")
    selected = tuple(discovered[selector] for selector in selectors)
    if len({device.physical_uuid for device in selected}) != len(selected):
        raise ValueError("The same physical GPU was selected through multiple backends")
    return selected


@dataclass(frozen=True)
class ExecutionPolicy:
    activity: str
    run_mode: str
    working_budget_percent: int
    automatic_chunk_ceiling_minutes: int
    inter_file_delay_seconds: int
    priority_signal_enabled: bool
    adaptive_segmentation_enabled: bool = True

    @property
    def retain_model_for_queued_work(self) -> bool:
        return self.run_mode == "dedicated"

    def status_snapshot(self) -> dict[str, object]:
        """Only stable, public policy fields; never environment values or paths."""
        return {
            "activity": self.activity,
            "run_mode": self.run_mode,
            "working_budget_percent": self.working_budget_percent,
            "automatic_chunk_ceiling_minutes": self.automatic_chunk_ceiling_minutes,
            "inter_file_delay_seconds": self.inter_file_delay_seconds,
            "priority_signal_enabled": self.priority_signal_enabled,
            "adaptive_segmentation_enabled": self.adaptive_segmentation_enabled,
        }


def _choice(value: str, name: str, accepted) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if normalized not in accepted:
        # Do not echo arbitrary environment content into logs.
        raise ValueError(f"{name} must be one of: {', '.join(accepted)}")
    return normalized


def resolve_execution_policy(
    environment: Mapping[str, str],
    *,
    segmentation_enabled: bool = True,
    memory_pressure_yield: bool = True,
    canonical_shared_cuda: bool = False,
    shared_host_gate_enabled: bool = False,
    priority_pressure_file: str = "",
) -> ExecutionPolicy:
    """Resolve once at startup using the caller's already-parsed safety flags."""
    activity = _choice(
        environment.get("SUBGEN_ACTIVITY", "balanced"),
        "SUBGEN_ACTIVITY",
        _ACTIVITIES,
    )
    run_mode = _choice(
        environment.get("SUBGEN_RUN_MODE", "adaptive"),
        "SUBGEN_RUN_MODE",
        _RUN_MODES,
    )
    for name, value in (
        ("SEGMENTATION_ENABLED", segmentation_enabled),
        ("MEMORY_PRESSURE_YIELD", memory_pressure_yield),
        ("CANONICAL_SHARED_CUDA", canonical_shared_cuda),
        ("shared-host gate", shared_host_gate_enabled),
    ):
        if type(value) is not bool:
            raise ValueError(f"{name} must be a parsed boolean")
    if not segmentation_enabled:
        raise ValueError("All execution modes require SEGMENTATION_ENABLED=True")
    if not memory_pressure_yield:
        raise ValueError("All execution modes require MEMORY_PRESSURE_YIELD=True")
    if not isinstance(priority_pressure_file, str):
        raise ValueError("PRIORITY_PRESSURE_FILE must be a string")
    priority_configured = bool(priority_pressure_file.strip())
    if run_mode == "dedicated" and shared_host_gate_enabled:
        raise ValueError(
            "The shared-host acceptance gate requires SUBGEN_RUN_MODE=adaptive; "
            "dedicated mode cannot produce an application-priority acceptance receipt"
        )
    percent, ceiling, delay = _ACTIVITIES[activity]
    return ExecutionPolicy(
        activity=activity,
        run_mode=run_mode,
        working_budget_percent=percent,
        automatic_chunk_ceiling_minutes=ceiling,
        inter_file_delay_seconds=delay,
        priority_signal_enabled=priority_configured and run_mode == "adaptive",
    )
