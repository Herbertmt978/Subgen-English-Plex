"""CUDA-visible identity/topology discovery without a model or CUDA context.

This deliberately reports no free-memory value: device capacity is not available
memory. Admission still needs fresh observations from its resource owner.
Driver calls belong in a bounded discovery child when used by the public root.
"""
from dataclasses import asdict, dataclass
import ctypes
import json
import os

from .execution_policy import ExecutionDevice


class CudaDiscoveryError(ValueError):
    """Driver discovery is unavailable or cannot establish physical identity."""


@dataclass(frozen=True)
class CudaDeviceObservation:
    device: ExecutionDevice
    total_bytes: int

    def __post_init__(self):
        if type(self.device) is not ExecutionDevice or self.device.backend != 'cuda':
            raise CudaDiscoveryError('CUDA observation requires a CUDA device identity')
        self.device.__post_init__()
        if type(self.total_bytes) is not int or not 0 < self.total_bytes <= 2**60:
            raise CudaDiscoveryError('CUDA device capacity is invalid')


class _Uuid(ctypes.Structure):
    _fields_ = [('bytes', ctypes.c_ubyte * 16)]


def _load_driver():
    try:
        # Use the Windows system directory, never a current-directory DLL.
        return (ctypes.WinDLL('nvcuda.dll', winmode=0x800) if os.name == 'nt'
                else ctypes.CDLL('libcuda.so.1'))
    except OSError:
        raise CudaDiscoveryError('CUDA driver library is unavailable') from None


def discover_cuda_devices(*, driver=None):
    """Use driver-visible ordinals, not nvidia-smi's differently scoped indexes.

    CUDA partition identities are refused until physical-card sharing can be
    accounted for. Same-card CUDA/Vulkan duplicates are rejected later by the
    canonical explicit-device selector. No allocation or context API is called.
    """
    driver = _load_driver() if driver is None else driver
    pointer = ctypes.POINTER
    signatures = {
        'cuInit': [ctypes.c_uint],
        'cuDeviceGetCount': [pointer(ctypes.c_int)],
        'cuDeviceGet': [pointer(ctypes.c_int), ctypes.c_int],
        'cuDeviceGetName': [pointer(ctypes.c_char), ctypes.c_int, ctypes.c_int],
        'cuDeviceGetUuid': [pointer(_Uuid), ctypes.c_int],
        'cuDeviceGetUuid_v2': [pointer(_Uuid), ctypes.c_int],
        'cuDeviceGetAttribute': [pointer(ctypes.c_int), ctypes.c_int, ctypes.c_int],
        'cuDeviceTotalMem_v2': [pointer(ctypes.c_size_t), ctypes.c_int],
    }
    functions = {}
    try:
        for name, signature in signatures.items():
            function = getattr(driver, name)
            function.argtypes, function.restype = signature, ctypes.c_int
            functions[name] = function
    except (AttributeError, TypeError):
        raise CudaDiscoveryError('CUDA driver lacks required identity queries') from None

    def call(name, *args):
        code = functions[name](*args)
        if type(code) is not int or code != 0:
            raise CudaDiscoveryError(f'CUDA discovery query failed: {name}')

    initialized = functions['cuInit'](0)
    if type(initialized) is int and initialized == 100:  # CUDA_ERROR_NO_DEVICE
        return ()
    if type(initialized) is not int or initialized != 0:
        raise CudaDiscoveryError('CUDA driver initialization failed')
    count = ctypes.c_int()
    call('cuDeviceGetCount', ctypes.byref(count))
    if not 0 <= count.value <= 32:
        raise CudaDiscoveryError('CUDA device count exceeds the supported discovery bound')
    observations, seen = [], set()
    for index in range(count.value):
        handle, integrated = ctypes.c_int(), ctypes.c_int()
        total, name = ctypes.c_size_t(), ctypes.create_string_buffer(256)
        original, current = _Uuid(), _Uuid()
        call('cuDeviceGet', ctypes.byref(handle), index)
        call('cuDeviceGetName', name, len(name), handle)
        call('cuDeviceGetUuid', ctypes.byref(original), handle)
        call('cuDeviceGetUuid_v2', ctypes.byref(current), handle)
        call('cuDeviceGetAttribute', ctypes.byref(integrated), 18, handle)
        call('cuDeviceTotalMem_v2', ctypes.byref(total), handle)
        uuid = bytes(current.bytes).hex()
        if bytes(original.bytes) != bytes(current.bytes):
            raise CudaDiscoveryError('Partitioned CUDA devices need separate physical-memory qualification')
        if uuid in seen:
            raise CudaDiscoveryError('CUDA discovery repeated a physical GPU')
        if integrated.value not in (0, 1) or not 0 < total.value <= 2**60:
            raise CudaDiscoveryError('CUDA device memory topology or capacity is invalid')
        try:
            if b'\0' not in name.raw:
                raise ValueError('unbounded name')
            device = ExecutionDevice('cuda', index, uuid, name.value.decode('utf-8'),
                'shared' if integrated.value else 'dedicated')
        except (ValueError, UnicodeError):
            raise CudaDiscoveryError('CUDA device identity is invalid') from None
        seen.add(uuid)
        observations.append(CudaDeviceObservation(device, total.value))
    return tuple(observations)


def serve_discovery(input_stream, output_stream, *, discover=discover_cuda_devices):
    """Wait for the parent's limit handshake before touching the driver."""
    from .cuda_worker_entry import read_command
    def send(packet):
        output_stream.write(json.dumps(packet, allow_nan=False).encode('utf-8') + b'\n')
        output_stream.flush()
    try:
        if read_command(input_stream) != {'operation': 'discover'}:
            raise CudaDiscoveryError('Discovery handshake is required')
        observations = discover()
        if not isinstance(observations, tuple) or len(observations) > 32:
            raise CudaDiscoveryError('CUDA discovery returned an invalid inventory')
        for observation in observations:
            if type(observation) is not CudaDeviceObservation:
                raise CudaDiscoveryError('CUDA discovery returned an invalid observation')
            observation.__post_init__()
        send({'event': 'discovered', 'protocol': 1,
              'devices': [asdict(observation) for observation in observations]})
        if read_command(input_stream) != {'operation': 'unload'}:
            raise CudaDiscoveryError('Discovery release handshake is required')
        send({'event': 'released', 'protocol': 1})
        return 0
    except Exception:
        send({'event': 'error', 'code': 'discovery_failed'})
        return 1


if __name__ == '__main__':
    import sys
    raise SystemExit(serve_discovery(sys.stdin.buffer, sys.stdout.buffer))
