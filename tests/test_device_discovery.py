"""Real bounded child transport with synthetic device data, never GPU calls."""
import copy
from dataclasses import asdict
import io
import json
from pathlib import Path
import sys
import threading
from unittest.mock import MagicMock

import pytest

from subgen_core.cuda_discovery import CudaDeviceObservation, CudaDiscoveryError, serve_discovery
from subgen_core.device_discovery import GpuDiscoveryWorker, decode_cuda_inventory, decode_vulkan_inventory, vulkan_execution_devices
from subgen_core.execution_policy import ExecutionDevice, resolve_execution_devices
from subgen_core.resident_worker import WorkerCancelled, WorkerProtocolError
from tests.test_vulkan_probe import document, decode


def observation():
    return CudaDeviceObservation(ExecutionDevice('cuda', 0, 'a'*32, 'Test GPU', 'dedicated'), 24*1024**3)


def packet():
    return {'event': 'discovered', 'protocol': 1, 'devices': [asdict(observation())]}


PROGRAM = r'''
import sys, time
from subgen_core.cuda_discovery import CudaDeviceObservation, serve_discovery
from subgen_core.execution_policy import ExecutionDevice
mode = sys.argv[1]
def discover():
    if mode == 'hang': time.sleep(20)
    if mode == 'fail': raise RuntimeError('private driver detail')
    if mode == 'flood':
        sys.stdout.write('x' * 300000)
        sys.stdout.flush()
        time.sleep(20)
    return (CudaDeviceObservation(ExecutionDevice('cuda', 0, 'a'*32, 'Test GPU', 'dedicated'), 24*1024**3),)
raise SystemExit(serve_discovery(sys.stdin.buffer, sys.stdout.buffer, discover=discover))
'''


def worker(mode='ok', establish_limits=None):
    processes = []
    result = GpuDiscoveryWorker([sys.executable, '-u', '-c', PROGRAM, mode], backend='cuda',
        establish_limits=establish_limits or processes.append,
        cwd=Path(__file__).resolve().parents[1])
    return result, processes


def test_inventory_is_returned_only_after_real_child_and_pipes_exit():
    w, processes = worker()
    assert w.pid is None
    assert w.discover(timeout=3) == (observation(),)
    assert len(processes) == 1 and processes[0].returncode == 0
    assert w.release_confirmed and not w.model_is_loaded
    assert all(not t.is_alive() for t in w._threads)
    with pytest.raises(WorkerProtocolError, match='new handle'):
        w.discover()


@pytest.mark.parametrize('mode,error', [('hang', TimeoutError), ('fail', CudaDiscoveryError),
    ('flood', WorkerProtocolError)])
def test_discovery_failure_terminates_the_owned_child(mode, error):
    w, processes = worker(mode)
    with pytest.raises(error) as raised:
        w.discover(timeout=.5)
    assert 'private driver detail' not in str(raised.value)
    assert processes[0].poll() is not None and w.release_confirmed
    assert all(not t.is_alive() for t in w._threads)


def test_cancelled_discovery_does_not_launch():
    w, processes = worker()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(WorkerCancelled):
        w.discover(cancel=cancel)
    assert not processes and w.pid is None


def test_limit_setup_failure_terminates_child_before_discovery():
    processes = []
    def fail(process):
        processes.append(process)
        raise OSError('test containment unavailable')
    w, _ = worker(establish_limits=fail)
    with pytest.raises(OSError):
        w.discover()
    assert processes[0].poll() is not None and w.release_confirmed


def test_cancellation_during_limit_handshake_terminates_before_request():
    cancel = threading.Event()
    processes = []
    def limits(process):
        processes.append(process)
        cancel.set()
    w, _ = worker(establish_limits=limits)
    with pytest.raises(WorkerCancelled):
        w.discover(cancel=cancel)
    assert processes[0].poll() is not None and w.release_confirmed


def test_cancellation_during_release_does_not_return_inventory(monkeypatch):
    w, _ = worker()
    cancel = threading.Event()
    release = w.unload_model
    def cancelling_release(**kwargs):
        release(**kwargs)
        cancel.set()
    monkeypatch.setattr(w, 'unload_model', cancelling_release)
    with pytest.raises(WorkerCancelled):
        w.discover(cancel=cancel)
    assert w.release_confirmed


@pytest.mark.parametrize('command', [b'', b'{"operation":"load"}\n',
    b'{"operation":"discover","operation":"discover"}\n'])
def test_driver_is_not_touched_before_exact_handshake(command):
    discover = MagicMock()
    output = io.BytesIO()
    assert serve_discovery(io.BytesIO(command), output, discover=discover) == 1
    discover.assert_not_called()
    assert json.loads(output.getvalue()) == {'event':'error', 'code':'discovery_failed'}


def test_discovery_protocol_round_trip_and_release():
    output = io.BytesIO()
    commands = b'{"operation":"discover"}\n{"operation":"unload"}\n'
    assert serve_discovery(io.BytesIO(commands), output, discover=lambda:(observation(),)) == 0
    inventory, released = map(json.loads, output.getvalue().splitlines())
    assert decode_cuda_inventory(inventory) == (observation(),)
    assert released == {'event':'released', 'protocol':1}


@pytest.mark.parametrize('mutation', [
    lambda p:p.update(protocol=True), lambda p:p.update(event='ready'),
    lambda p:p.update(memory={}), lambda p:p.update(devices={}),
    lambda p:p['devices'].append(copy.deepcopy(p['devices'][0])),
    lambda p:p['devices'][0].update(total_bytes=True),
    lambda p:p['devices'][0].update(total_bytes=0),
    lambda p:p['devices'][0]['device'].update(backend='vulkan'),
    lambda p:p['devices'][0]['device'].update(physical_uuid='0'*32),
    lambda p:p['devices'][0]['device'].update(index=True),
])
def test_invalid_or_duplicate_discovery_response_is_refused(mutation):
    value = packet()
    mutation(value)
    with pytest.raises(CudaDiscoveryError):
        decode_cuda_inventory(value)


def test_empty_inventory_is_valid_but_cannot_satisfy_a_selected_gpu():
    assert decode_cuda_inventory({'event':'discovered', 'protocol':1, 'devices':[]}) == ()
    with pytest.raises(ValueError, match='unavailable'):
        resolve_execution_devices('cuda:0', ())


def test_vulkan_conversion_keeps_identity_and_shared_topology_not_heap_capacity():
    observations = decode(document())
    selected = vulkan_execution_devices(observations)
    assert selected[0] == ExecutionDevice('vulkan', 0, '1234'*8, 'Integrated graphics', 'shared')
    assert not hasattr(selected[0], 'free_bytes')
    with pytest.raises(ValueError, match='repeats'):
        vulkan_execution_devices(observations + observations)


def test_vulkan_managed_packet_uses_parent_timestamp_and_existing_parser():
    response = {'event':'discovered', 'protocol':1, 'observation':document()}
    observed = decode_vulkan_inventory(response, observed_at=123.0)
    assert observed[0].observed_at == 123.0
    assert vulkan_execution_devices(observed)[0].memory_topology == 'shared'
    for extra in ({'memory':{}}, {'protocol':True}, {'event':'ready'}):
        with pytest.raises(ValueError):
            decode_vulkan_inventory(dict(response, **extra), observed_at=123.0)


def test_vulkan_uses_same_direct_child_transport_and_verified_release():
    payload = json.dumps({'event':'discovered', 'protocol':1, 'observation':document()})
    program = ('import sys,json; assert json.loads(input()) == {"operation":"discover"}; '
        f'print({payload!r}, flush=True); '
        'assert json.loads(input()) == {"operation":"unload"}; '
        'print(json.dumps({"event":"released","protocol":1}), flush=True)')
    processes = []
    w = GpuDiscoveryWorker([sys.executable, '-u', '-c', program], backend='vulkan',
        establish_limits=processes.append)
    observed = w.discover(timeout=3)
    assert vulkan_execution_devices(observed)[0].physical_uuid == '1234'*8
    assert processes[0].returncode == 0 and w.release_confirmed
