"""Bounded discovery transport and conversion into canonical device selectors.

Reuse resident-worker pipe limits, deadlines and verified release. Discovery
neither admits model memory nor substitutes capacity for a free-memory sample.
"""
import time
import json

from .cuda_discovery import CudaDeviceObservation, CudaDiscoveryError
from .execution_policy import ExecutionDevice, resolve_execution_devices
from .resident_worker import ResidentPipeWorker, WorkerProtocolError, _seconds
from .vulkan_probe import VulkanDeviceObservation, VulkanProbeError, decode_vulkan_observations


def decode_cuda_inventory(packet):
    if (not isinstance(packet, dict) or set(packet) != {'event', 'protocol', 'devices'}
            or packet['event'] != 'discovered' or type(packet['protocol']) is not int
            or packet['protocol'] != 1 or not isinstance(packet['devices'], list)
            or len(packet['devices']) > 32):
        raise CudaDiscoveryError('CUDA discovery response has an invalid shape')
    observations = []
    try:
        for item in packet['devices']:
            if not isinstance(item, dict) or set(item) != {'device', 'total_bytes'}:
                raise ValueError('observation shape')
            device = item['device']
            if not isinstance(device, dict) or set(device) != {
                'backend', 'index', 'physical_uuid', 'name', 'memory_topology'}:
                raise ValueError('device shape')
            observations.append(CudaDeviceObservation(ExecutionDevice(**device), item['total_bytes']))
        devices = tuple(o.device for o in observations)
        resolve_execution_devices(','.join(d.selector for d in devices), devices)
    except (TypeError, ValueError):
        raise CudaDiscoveryError('CUDA discovery response has invalid or repeated identities') from None
    return tuple(observations)


def vulkan_execution_devices(observations):
    """Translate decoded topology/UUID only; never pool or promote heap budgets."""
    if not isinstance(observations, tuple) or len(observations) > 32:
        raise ValueError('Vulkan discovery requires a bounded decoded inventory')
    if any(type(observation) is not VulkanDeviceObservation for observation in observations):
        raise TypeError('Vulkan discovery requires validated observations')
    devices = tuple(ExecutionDevice('vulkan', o.physical_index, o.uuid, o.name,
                                   o.memory_topology) for o in observations)
    return resolve_execution_devices(','.join(d.selector for d in devices), devices)


def decode_vulkan_inventory(packet, *, observed_at):
    if (not isinstance(packet, dict) or set(packet) != {'event', 'protocol', 'observation'}
            or packet['event'] != 'discovered' or type(packet['protocol']) is not int
            or packet['protocol'] != 1):
        raise VulkanProbeError('Vulkan discovery response has an invalid shape')
    try:
        payload = json.dumps(packet['observation'], allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise VulkanProbeError('Vulkan discovery response is invalid') from None
    return decode_vulkan_observations(payload, observed_at=observed_at)


class GpuDiscoveryWorker(ResidentPipeWorker):
    """One discovery child; the caller retains this handle until release is proven."""
    def __init__(self, command, *, backend, establish_limits, env=None, cwd=None):
        if (not isinstance(command, (tuple, list)) or not command
                or any(not isinstance(part, str) or not part for part in command)
                or not callable(establish_limits) or backend not in ('cuda', 'vulkan')):
            raise ValueError('GPU discovery needs a supported backend, explicit command and process-limit owner')
        super().__init__(max_result_bytes=262144)
        self.command, self.establish_limits = tuple(command), establish_limits
        self.backend = backend
        self.env, self.cwd = None if env is None else dict(env), cwd

    def _accept_memory(self, packet):
        if 'memory' in packet:
            raise WorkerProtocolError('GPU discovery cannot provide resident memory evidence')

    def _raise_remote_error(self, packet):
        error = CudaDiscoveryError if self.backend == 'cuda' else VulkanProbeError
        raise error('GPU discovery failed; check driver support and device visibility')

    def discover(self, *, timeout=15, cancel=None):
        deadline = time.monotonic() + _seconds(timeout)
        if not self._busy.acquire(blocking=False):
            raise WorkerProtocolError('GPU discovery is already active')
        try:
            self._check(deadline, cancel)
            if self._attempted_load or self._released:
                raise WorkerProtocolError('Create a new handle for another discovery')
            self._attempted_load = True
            self._spawn(self.command, establish_limits=self.establish_limits, env=self.env, cwd=self.cwd)
            self._check(deadline, cancel)
            observed_at = time.monotonic()
            self._send({'operation': 'discover'})
            packet = self._receive(deadline, cancel)
            observations = (decode_cuda_inventory(packet) if self.backend == 'cuda'
                            else decode_vulkan_inventory(packet, observed_at=observed_at))
        except BaseException:
            self._terminate()
            raise
        finally:
            self._busy.release()
        # No result becomes usable until the real child and its pipes have exited.
        self.unload_model(timeout=max(.001, deadline-time.monotonic()))
        self._check(deadline, cancel)
        return observations
