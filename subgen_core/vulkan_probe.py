"""Strict decoding of native Vulkan observations, not a memory policy.

Heap usage is process-scoped. Standalone discovery cannot substitute for a
resident worker's observation. Shared GPU heaps are not extra host RAM, and
unknown budget support is never converted into available memory.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re


MAX_PROBE_BYTES = 262144
_MAX_BYTES = (1 << 63) - 1


class VulkanProbeError(ValueError):
    """The observation cannot support an honest resource measurement."""


@dataclass(frozen=True)
class VulkanHeap:
    index: int
    size_bytes: int
    device_local: bool
    budget_bytes: int | None
    usage_bytes: int | None
    available_bytes: int | None


@dataclass(frozen=True)
class VulkanDeviceObservation:
    physical_index: int
    name: str
    uuid: str
    pci_id: str | None
    vendor_id: int
    device_id: int
    driver_version_raw: int
    api_version_raw: int
    memory_topology: str
    budget_supported: bool
    heaps: tuple[VulkanHeap, ...]
    observed_at: float
    usage_scope: str = "process"
    query_scope: str = "independent_instance"


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VulkanProbeError("Vulkan observation contains duplicate keys")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise VulkanProbeError("Vulkan observation contains non-finite data")


def _integer(value, *, minimum=0, maximum=_MAX_BYTES):
    if type(value) is not int or not minimum <= value <= maximum:
        raise VulkanProbeError("Vulkan observation contains an invalid integer")
    return value


def _boolean(value):
    if type(value) is not bool:
        raise VulkanProbeError("Vulkan observation contains an invalid boolean")
    return value


def _text(value, maximum=256):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise VulkanProbeError("Vulkan observation contains invalid text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise VulkanProbeError("Vulkan observation contains invalid Unicode") from None
    return value


def decode_vulkan_observations(payload: bytes, *, observed_at: float) -> tuple[VulkanDeviceObservation, ...]:
    """Preserve per-device/per-heap measurements; do not pool shared capacities.

    The caller supplies the observation's monotonic timestamp, not a fabricated
    timestamp from the child. Admission owns freshness and mandatory reserves.
    """
    if (isinstance(observed_at, bool) or not isinstance(observed_at, (int, float))
            or not math.isfinite(observed_at) or observed_at < 0):
        raise VulkanProbeError("Vulkan observation requires a monotonic timestamp")
    if not isinstance(payload, bytes) or len(payload) > MAX_PROBE_BYTES:
        raise VulkanProbeError("Vulkan observation exceeds its byte bound")
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_object,
                              parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise VulkanProbeError("Vulkan observation is not valid JSON") from None
    if (not isinstance(document, dict) or type(document.get("protocol")) is not int
            or document["protocol"] != 1 or document.get("usage_scope") != "process"):
        raise VulkanProbeError("Vulkan observation has an unsupported protocol or usage scope")
    devices = document.get("devices")
    query_scope = document.get("query_scope", "independent_instance")
    if not isinstance(query_scope, str) or query_scope not in {"independent_instance", "allocating_instance"}:
        raise VulkanProbeError("Vulkan observation has an unknown query scope")
    if not isinstance(devices, list) or len(devices) > 32:
        raise VulkanProbeError("Vulkan observation requires a bounded device list")
    parsed = []
    indexes, uuids, pci_ids = set(), set(), set()
    for device in devices:
        if not isinstance(device, dict):
            raise VulkanProbeError("Vulkan device must be an object")
        index = _integer(device.get("physical_index"), maximum=31)
        uuid = _text(device.get("uuid"), 32)
        if re.fullmatch(r"[0-9a-f]{32}", uuid) is None or uuid == "0" * 32:
            raise VulkanProbeError("Vulkan device has no usable physical identity")
        pci_id = device.get("pci_id")
        if pci_id is not None and (not isinstance(pci_id, str) or re.fullmatch(r"[0-9a-f]{4,8}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", pci_id) is None):
            raise VulkanProbeError("Vulkan device PCI identity is invalid")
        if index in indexes or uuid in uuids or (pci_id is not None and pci_id in pci_ids):
            raise VulkanProbeError("Vulkan observation repeats a physical device")
        indexes.add(index)
        uuids.add(uuid)
        if pci_id is not None:
            pci_ids.add(pci_id)
        topology = device.get("memory_topology")
        if not isinstance(topology, str) or topology not in {"shared", "dedicated"}:
            raise VulkanProbeError("Vulkan memory topology is unknown")
        supported = _boolean(device.get("budget_supported"))
        source_heaps = device.get("heaps")
        if not isinstance(source_heaps, list) or not 1 <= len(source_heaps) <= 16:
            raise VulkanProbeError("Vulkan device requires a bounded heap list")
        heaps = []
        for heap_index, heap in enumerate(source_heaps):
            if not isinstance(heap, dict) or _integer(heap.get("index"), maximum=15) != heap_index:
                raise VulkanProbeError("Vulkan heap indexes are invalid")
            size = _integer(heap.get("size_bytes"), minimum=1)
            local = _boolean(heap.get("device_local"))
            if not all(key in heap for key in ("budget_bytes", "usage_bytes", "available_bytes")):
                raise VulkanProbeError("Vulkan heap is missing its budget observation")
            budget, usage, available = (heap[key] for key in ("budget_bytes", "usage_bytes", "available_bytes"))
            if supported:
                budget = _integer(budget, maximum=size)
                usage = _integer(usage)
                available = _integer(available, maximum=size)
                if available != max(0, budget - usage):
                    raise VulkanProbeError("Vulkan heap headroom contradicts its budget and usage")
            elif any(value is not None for value in (budget, usage, available)):
                raise VulkanProbeError("Unsupported Vulkan budget must remain unknown")
            heaps.append(VulkanHeap(heap_index, size, local, budget, usage, available))
        parsed.append(VulkanDeviceObservation(
            index, _text(device.get("name")), uuid, pci_id,
            *(_integer(device.get(key), maximum=2**32 - 1) for key in
              ("vendor_id", "device_id", "driver_version_raw", "api_version_raw")),
            topology, supported, tuple(heaps), float(observed_at), query_scope=query_scope,
        ))
    return tuple(parsed)
