"""Injected driver ABI tests: no GPU, library loading, or context allocation."""
import ctypes
from types import SimpleNamespace

import pytest

from subgen_core.cuda_discovery import CudaDiscoveryError, discover_cuda_devices
from subgen_core.execution_policy import ExecutionDevice, resolve_execution_devices


class Function:
    def __init__(self, implementation):
        self.implementation = implementation
    def __call__(self, *args):
        self.implementation(*args)
        return 0


def driver(count=2):
    def uuid(result, handle):
        result._obj.bytes[:] = bytes([handle.value]) * 16
    def name(result, _length, handle):
        result.value = f'Visible GPU {handle.value}'.encode()
    return SimpleNamespace(
        cuInit=Function(lambda flags:None),
        cuDeviceGetCount=Function(lambda p:setattr(p._obj, 'value', count)),
        cuDeviceGet=Function(lambda p,index:setattr(p._obj, 'value', index+3)),
        cuDeviceGetName=Function(name),
        cuDeviceGetUuid=Function(uuid), cuDeviceGetUuid_v2=Function(uuid),
        cuDeviceGetAttribute=Function(lambda p,attr,h:setattr(p._obj, 'value', h.value % 2)),
        cuDeviceTotalMem_v2=Function(lambda p,h:setattr(p._obj, 'value', 24*1024**3)),
    )


def test_visible_ordinals_uuid_and_real_topology_are_preserved():
    d = driver()
    first, second = discover_cuda_devices(driver=d)
    assert first.device.index == 0 and second.device.index == 1
    assert first.device.physical_uuid == '03'*16
    assert first.device.memory_topology == 'shared'
    assert second.device.memory_topology == 'dedicated'
    assert first.total_bytes == 24*1024**3
    assert not hasattr(first, 'free_bytes')
    assert d.cuDeviceTotalMem_v2.restype is ctypes.c_int


@pytest.mark.parametrize('count', [0, 1, 3, 4, 5, 32])
def test_bounded_discovery_count(count):
    assert len(discover_cuda_devices(driver=driver(count))) == count


@pytest.mark.parametrize('count', [-1, 33, 1000000])
def test_invalid_counts_fail(count):
    with pytest.raises(CudaDiscoveryError):
        discover_cuda_devices(driver=driver(count))


def test_no_device_is_distinct_from_broken_driver():
    d = driver()
    d.cuInit = lambda _:100
    assert discover_cuda_devices(driver=d) == ()
    d.cuInit = lambda _:999
    with pytest.raises(CudaDiscoveryError):
        discover_cuda_devices(driver=d)


@pytest.mark.parametrize('query', ['cuDeviceGetCount', 'cuDeviceGetName', 'cuDeviceGetUuid',
    'cuDeviceGetUuid_v2', 'cuDeviceGetAttribute', 'cuDeviceTotalMem_v2'])
def test_driver_query_failure_never_fabricates_inventory(query):
    d = driver()
    setattr(d, query, lambda *args:999)
    with pytest.raises(CudaDiscoveryError):
        discover_cuda_devices(driver=d)


def test_partition_identity_cannot_hide_shared_physical_memory():
    d = driver()
    d.cuDeviceGetUuid_v2 = Function(lambda p,h:p._obj.bytes.__setitem__(slice(None), bytes([8])*16))
    with pytest.raises(CudaDiscoveryError, match='Partitioned'):
        discover_cuda_devices(driver=d)


def test_same_card_selected_via_cuda_and_vulkan_is_rejected():
    cuda = discover_cuda_devices(driver=driver(1))[0].device
    vulkan = ExecutionDevice('vulkan', 1, cuda.physical_uuid, 'Same card', 'shared')
    with pytest.raises(ValueError, match='same physical GPU'):
        resolve_execution_devices('cuda:0,vulkan:1', (cuda, vulkan))


@pytest.mark.parametrize('integrated,total', [(2, 1024), (0, 0), (1, 2**61)])
def test_unknown_topology_or_capacity_is_not_assumed(integrated, total):
    d = driver()
    d.cuDeviceGetAttribute = Function(lambda p,*args:setattr(p._obj, 'value', integrated))
    d.cuDeviceTotalMem_v2 = Function(lambda p,*args:setattr(p._obj, 'value', total))
    with pytest.raises(CudaDiscoveryError):
        discover_cuda_devices(driver=d)


@pytest.mark.parametrize('name', [b'', b'bad\x01name', b'\xff', b'x'*256])
def test_invalid_driver_name_is_rejected(name):
    d = driver()
    d.cuDeviceGetName = Function(lambda p,*args:setattr(p, 'raw', name.ljust(256, b'\0')))
    with pytest.raises(CudaDiscoveryError, match='identity'):
        discover_cuda_devices(driver=d)


@pytest.mark.parametrize('byte', [0, 7])
def test_zero_or_duplicate_uuid_is_rejected(byte):
    d = driver()
    def same(p,h):
        p._obj.bytes[:] = bytes([byte])*16
    d.cuDeviceGetUuid = Function(same)
    d.cuDeviceGetUuid_v2 = Function(same)
    with pytest.raises(CudaDiscoveryError):
        discover_cuda_devices(driver=d)


def test_required_query_cannot_be_silently_substituted():
    d = driver()
    del d.cuDeviceGetUuid_v2
    with pytest.raises(CudaDiscoveryError, match='required identity queries'):
        discover_cuda_devices(driver=d)
