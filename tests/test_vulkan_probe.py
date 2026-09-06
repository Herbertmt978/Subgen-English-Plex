import copy
import json

import pytest

from subgen_core.vulkan_probe import VulkanProbeError, decode_vulkan_observations


def document():
    return {"protocol": 1, "usage_scope": "process", "devices": [{
        "physical_index": 0, "name": "Integrated graphics", "uuid": "1234" * 8,
        "pci_id": None, "vendor_id": 4098, "device_id": 123,
        "driver_version_raw": 456, "api_version_raw": 789,
        "memory_topology": "shared", "budget_supported": True,
        "heaps": [{"index": 0, "size_bytes": 1024, "device_local": True,
                   "budget_bytes": 900, "usage_bytes": 300, "available_bytes": 600}],
    }]}


def decode(value):
    return decode_vulkan_observations(json.dumps(value).encode(), observed_at=10)


def test_shared_and_dedicated_observations_are_kept_separate():
    value = document()
    second = copy.deepcopy(value["devices"][0])
    second.update(physical_index=1, uuid="abcd" * 8, pci_id="0000:01:00.0",
                  memory_topology="dedicated", name="Discrete graphics")
    value["devices"].append(second)
    shared, dedicated = decode(value)
    assert shared.memory_topology == "shared" and dedicated.memory_topology == "dedicated"
    assert shared.heaps[0].available_bytes == dedicated.heaps[0].available_bytes == 600
    assert shared.usage_scope == "process" and shared.observed_at == 10
    assert not hasattr(shared, "total_available_bytes")  # no hidden cross-heap pooling


def test_absent_budget_is_unknown_not_heap_capacity():
    value = document()
    value["devices"][0]["budget_supported"] = False
    value["devices"][0]["heaps"][0].update(budget_bytes=None, usage_bytes=None, available_bytes=None)
    heap = decode(value)[0].heaps[0]
    assert heap.size_bytes == 1024 and heap.available_bytes is None


def test_over_budget_usage_is_zero_available_not_unsigned_wrap():
    value = document()
    value["devices"][0]["heaps"][0].update(usage_bytes=1200, available_bytes=0)
    assert decode(value)[0].heaps[0].available_bytes == 0


@pytest.mark.parametrize("key,value", [
    ("uuid", "0" * 32), ("uuid", "wrong"), ("pci_id", "invalid"),
    ("physical_index", True), ("physical_index", 32), ("driver_version_raw", -1),
    ("budget_supported", 1), ("memory_topology", "unknown"), ("memory_topology", {}),
    ("name", "bad\x00name"), ("name", "\ud800"), ("heaps", []),
])
def test_malformed_device_is_rejected(key, value):
    data = document()
    data["devices"][0][key] = value
    with pytest.raises(VulkanProbeError):
        decode(data)


@pytest.mark.parametrize("key,value", [
    ("index", 1), ("size_bytes", 0), ("size_bytes", True),
    ("device_local", 1), ("budget_bytes", 2000), ("usage_bytes", -1),
    ("available_bytes", 700), ("budget_bytes", None),
])
def test_malformed_heap_is_rejected(key, value):
    data = document()
    data["devices"][0]["heaps"][0][key] = value
    with pytest.raises(VulkanProbeError):
        decode(data)


def test_unsupported_budget_cannot_claim_full_capacity_free():
    data = document()
    data["devices"][0]["budget_supported"] = False
    with pytest.raises(VulkanProbeError, match="remain unknown"):
        decode(data)


@pytest.mark.parametrize("identity", ["uuid", "pci_id", "physical_index"])
def test_repeated_physical_device_rejected(identity):
    data = document()
    first = data["devices"][0]
    first["pci_id"] = "0000:01:00.0"
    second = copy.deepcopy(first)
    second.update(uuid="abcd" * 8, pci_id="0000:02:00.0", physical_index=1)
    second[identity] = first[identity]
    data["devices"].append(second)
    with pytest.raises(VulkanProbeError, match="repeats"):
        decode(data)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf")])
def test_bad_timestamp_rejected(value):
    with pytest.raises(VulkanProbeError):
        decode_vulkan_observations(json.dumps(document()).encode(), observed_at=value)


@pytest.mark.parametrize("payload", [b'{}', b'[]', b'\xff', b'{"protocol":1,"protocol":1}', b'{"devices":NaN}'])
def test_malformed_wire_payload_rejected(payload):
    with pytest.raises(VulkanProbeError):
        decode_vulkan_observations(payload, observed_at=10)


def test_large_payload_constructed_inside_test_not_pytest_id():
    with pytest.raises(VulkanProbeError, match="byte bound"):
        decode_vulkan_observations(b"x" * 262145, observed_at=10)


def test_no_gpu_is_an_empty_observation_not_cpu_substitution():
    value = document()
    value["devices"] = []
    assert decode(value) == ()


def test_cannot_relabel_process_usage_as_global_usage():
    value = document()
    value["usage_scope"] = "device"
    with pytest.raises(VulkanProbeError, match="usage scope"):
        decode(value)


def test_allocating_instance_scope_is_explicit_not_assumed_from_same_process():
    value = document()
    assert decode(value)[0].query_scope == "independent_instance"
    value["query_scope"] = "allocating_instance"
    assert decode(value)[0].query_scope == "allocating_instance"
    value["query_scope"] = "assumed"
    with pytest.raises(VulkanProbeError, match="query scope"):
        decode(value)
