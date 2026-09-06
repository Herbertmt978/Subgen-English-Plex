"""Cohort lifecycle race and failure checks; no hardware qualification claim."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import threading

import pytest

from subgen_core.execution_policy import ExecutionDevice
from subgen_core.model_envelope_catalog import ArtifactValidationError, ModelArtifactIdentity
from subgen_core.model_runtime import CohortModelRuntime, CohortWorkerSpec
from subgen_core.cohort_runtime import CohortCancelled, CohortCapacityDeferred, CohortReleaseError
from subgen_core.resource_management import CohortAdmissionDecision, CohortReservation, MemoryPressureYield


ADMIT = CohortAdmissionDecision(True, (), 10, 20, ())


class Worker:
    def __init__(self, spec, events):
        self.spec = spec
        self.events = events
        self.model_is_loaded = False
        self.load_hook = None
        self.transcribe_hook = None
        self.release_failures = 0

    def load(self, *, timeout, cancel):
        assert timeout > 0
        self.events.append(('load', self.spec.device.selector))
        self.model_is_loaded = True
        if self.load_hook:
            self.load_hook(cancel)

    def transcribe(self, audio, *, timeout, cancel, **options):
        assert timeout > 0 and self.model_is_loaded
        if self.transcribe_hook:
            return self.transcribe_hook(cancel)
        return {'audio': audio, 'options': options}

    def release(self, *, timeout):
        assert timeout > 0
        self.events.append(('release', self.spec.device.selector))
        if self.release_failures:
            self.release_failures -= 1
            return False
        self.model_is_loaded = False
        return True


def setup(tmp_path, count=2, *, healthy=lambda: True, decide=lambda: ADMIT, reservation=None, configure=None):
    path = tmp_path / 'weights.bin'
    path.write_bytes(b'test weights')
    events, workers, specs = [], {}, []
    def factory(spec):
        worker = Worker(spec, events)
        workers[spec.device.selector] = worker
        if configure:
            configure(worker)
        return worker
    for index in range(count):
        backend = 'cuda' if index % 2 == 0 else 'vulkan'
        device = ExecutionDevice(backend, index, f'{index + 1:032x}', 'Same device name',
                                 'dedicated' if backend == 'cuda' else 'shared')
        identity = ModelArtifactIdentity('base', 'ctranslate2' if backend == 'cuda' else 'ggml',
            'float16', 'sha256:' + hashlib.sha256(b'test weights').hexdigest(), 12, 'sha256:' + 'a' * 64)
        specs.append(CohortWorkerSpec(device, identity, path, factory))
    lease = reservation or CohortReservation()
    runtime = CohortModelRuntime(tuple(specs), reservation=lease, decide_admission=decide, check_healthy=healthy)
    return runtime, tuple(specs), lease, workers, events


@pytest.mark.parametrize('count', [1, 2, 3, 4, 5, 32])
def test_load_and_release_arbitrary_count(tmp_path, count):
    runtime, specs, lease, workers, events = setup(tmp_path, count)
    assert runtime.load(timeout=5) is ADMIT and runtime.state == 'ready'
    assert len(workers) == count
    for spec in specs:
        assert runtime.transcribe(spec.device.selector, b'audio', timeout=2, language='en')['options'] == {'language': 'en'}
    runtime.release(timeout=5)
    assert runtime.state == 'released' and all(not w.model_is_loaded for w in workers.values())
    assert len(events) == count * 2
    runtime.release(timeout=1)  # Idempotent; never releases another generation's lease.
    assert len(events) == count * 2
    with pytest.raises(RuntimeError, match='not released'):
        runtime.load(timeout=1)
    new = CohortModelRuntime(specs, reservation=lease, decide_admission=lambda: ADMIT, check_healthy=lambda: True)
    new.load(timeout=2)
    runtime.release(timeout=1)
    assert new.state == 'ready'
    new.release(timeout=2)


def test_corrupt_artifact_prevents_all_loads(tmp_path):
    runtime, specs, lease, workers, events = setup(tmp_path)
    specs[0].artifact_path.write_bytes(b'other bytes!')
    with pytest.raises(ArtifactValidationError):
        runtime.load(timeout=1)
    assert not events and not workers and runtime.state == 'released'


def test_mismatch_and_duplicate_device_refused_at_construction(tmp_path):
    _, specs, lease, _, _ = setup(tmp_path)
    for invalid in ((specs[0], specs[0]), (specs[0], replace(specs[1], artifact=replace(specs[1].artifact, model='small')))):
        with pytest.raises(ValueError):
            CohortModelRuntime(invalid, reservation=lease, decide_admission=lambda: ADMIT, check_healthy=lambda: True)


def test_capacity_refusal_never_calls_factory(tmp_path):
    denied = replace(ADMIT, admitted=False, reasons=('insufficient_combined_host',))
    runtime, _, _, workers, _ = setup(tmp_path, decide=lambda: denied)
    with pytest.raises(CohortCapacityDeferred) as failure:
        runtime.load(timeout=1)
    assert failure.value.decision is denied and not workers


def test_duplicate_selector_with_different_physical_identity_is_refused(tmp_path):
    _, specs, lease, _, _ = setup(tmp_path)
    duplicate = replace(specs[0], device=replace(specs[0].device, physical_uuid='f' * 32))
    with pytest.raises(ValueError, match='backend selector'):
        CohortModelRuntime((specs[0], duplicate), reservation=lease,
                           decide_admission=lambda: ADMIT, check_healthy=lambda: True)


def test_second_cohort_cannot_spend_held_reservation(tmp_path):
    first, specs, lease, workers, events = setup(tmp_path)
    first.load(timeout=2)
    second = CohortModelRuntime(specs, reservation=lease,
                                decide_admission=lambda: ADMIT, check_healthy=lambda: True)
    with pytest.raises(RuntimeError, match='still owns'):
        second.load(timeout=2)
    assert second.state == 'released' and first.state == 'ready'
    assert len(events) == 2 and all(w.model_is_loaded for w in workers.values())
    first.release(timeout=2)


def test_artifact_changed_during_load_is_released(tmp_path):
    def configure(worker):
        worker.load_hook = lambda cancel: worker.spec.artifact_path.write_bytes(b'other bytes!')
    runtime, _, _, workers, _ = setup(tmp_path, configure=configure)
    with pytest.raises(ArtifactValidationError):
        runtime.load(timeout=2)
    assert len(workers) == 1 and all(not w.model_is_loaded for w in workers.values())
    assert runtime.state == 'released'


def test_release_cancels_loading_before_waiting_for_lifecycle_lock(tmp_path):
    started = threading.Event()
    def configure(worker):
        def loading(cancel):
            started.set()
            assert cancel.wait(3)
        worker.load_hook = loading
    runtime, _, _, workers, events = setup(tmp_path, configure=configure)
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(runtime.load, timeout=4)
        assert started.wait(2)
        runtime.release(timeout=2)
        with pytest.raises(CohortCancelled):
            task.result(timeout=2)
    assert len(workers) == 1 and runtime.state == 'released'
    assert [kind for kind, _ in events] == ['load', 'release']


def test_unconfirmed_load_is_cleaned_up(tmp_path):
    def configure(worker):
        worker.load_hook = lambda cancel: setattr(worker, 'model_is_loaded', None)
    runtime, _, _, workers, events = setup(tmp_path, configure=configure)
    with pytest.raises(RuntimeError, match='confirm model loading'):
        runtime.load(timeout=2)
    assert len(workers) == 1 and runtime.state == 'released'
    assert [kind for kind, _ in events] == ['load', 'release']


def test_partial_second_load_releases_both_handles(tmp_path):
    def configure(worker):
        if worker.spec.device.index == 1:
            def fail(cancel):
                raise RuntimeError('backend load failed after allocation')
            worker.load_hook = fail
    runtime, _, _, workers, events = setup(tmp_path, configure=configure)
    with pytest.raises(RuntimeError, match='backend load failed'):
        runtime.load(timeout=2)
    assert runtime.state == 'released'
    assert all(not w.model_is_loaded for w in workers.values())
    assert [kind for kind, _ in events] == ['load', 'load', 'release', 'release']


def test_failed_release_retains_lease_and_attempts_other_workers(tmp_path):
    runtime, specs, lease, workers, events = setup(tmp_path)
    runtime.load(timeout=2)
    workers[specs[0].device.selector].release_failures = 1
    with pytest.raises(CohortReleaseError):
        runtime.release(timeout=2)
    assert runtime.state == 'blocked'
    assert not workers[specs[1].device.selector].model_is_loaded
    with pytest.raises(RuntimeError, match='still owns'):
        lease.acquire(lambda: ADMIT)
    runtime.release(timeout=2)
    assert runtime.state == 'released'
    assert events.count(('release', specs[1].device.selector)) == 1


def test_pressure_after_load_unloads_instead_of_loading_next(tmp_path):
    pressured = threading.Event()
    runtime, _, _, workers, _ = setup(tmp_path, healthy=lambda: not pressured.is_set(),
                                     configure=lambda worker: setattr(worker, 'load_hook', lambda cancel: pressured.set()))
    with pytest.raises(MemoryPressureYield):
        runtime.load(timeout=2)
    assert len(workers) == 1 and not next(iter(workers.values())).model_is_loaded


def test_same_device_concurrency_refused_but_other_device_can_work(tmp_path):
    runtime, specs, _, workers, _ = setup(tmp_path)
    runtime.load(timeout=2)
    started, finish = threading.Event(), threading.Event()
    def hold(cancel):
        started.set()
        assert finish.wait(3)
        return 'result'
    workers[specs[0].device.selector].transcribe_hook = hold
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(runtime.transcribe, specs[0].device.selector, b'x', timeout=4)
        try:
            assert started.wait(2)
            with pytest.raises(RuntimeError, match='active chunk'):
                runtime.transcribe(specs[0].device.selector, b'y', timeout=1)
            assert runtime.transcribe(specs[1].device.selector, b'z', timeout=1)['audio'] == b'z'
        finally:
            finish.set()
        assert task.result(timeout=2) == 'result'
    runtime.release(timeout=2)


def test_release_waits_for_inference_unwind_and_rejects_late_result(tmp_path):
    runtime, specs, _, workers, events = setup(tmp_path)
    runtime.load(timeout=2)
    started, unwind = threading.Event(), threading.Event()
    def active(cancel):
        started.set()
        assert cancel.wait(3)
        assert not any(kind == 'release' for kind, _ in events)
        unwind.set()
        return 'late result'
    workers[specs[0].device.selector].transcribe_hook = active
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(runtime.transcribe, specs[0].device.selector, b'x', timeout=4)
        assert started.wait(2)
        runtime.release(timeout=2)
        assert unwind.is_set()
        with pytest.raises(CohortCancelled):
            task.result(timeout=1)
    assert runtime.state == 'released'


def test_release_timeout_never_unloads_active_worker(tmp_path):
    runtime, specs, lease, workers, events = setup(tmp_path)
    runtime.load(timeout=2)
    started, finish = threading.Event(), threading.Event()
    def active(cancel):
        started.set()
        assert finish.wait(3)
        return 'late'
    workers[specs[0].device.selector].transcribe_hook = active
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(runtime.transcribe, specs[0].device.selector, b'x', timeout=4)
        try:
            assert started.wait(2)
            with pytest.raises(CohortReleaseError, match='not unwound'):
                runtime.release(timeout=.02)
            assert not any(kind == 'release' for kind, _ in events)
            with pytest.raises(RuntimeError, match='still owns'):
                lease.acquire(lambda: ADMIT)
        finally:
            finish.set()
        with pytest.raises(CohortCancelled):
            task.result(timeout=2)
    runtime.release(timeout=2)


def test_cancel_before_load_is_not_cleared(tmp_path):
    runtime, _, _, workers, _ = setup(tmp_path)
    runtime.cancel()
    with pytest.raises(CohortCancelled):
        runtime.load(timeout=1)
    assert not workers


def test_inference_error_cancels_sibling_dispatch(tmp_path):
    runtime, specs, _, workers, _ = setup(tmp_path)
    runtime.load(timeout=2)
    def fail(cancel):
        raise ValueError('invalid result')
    workers[specs[0].device.selector].transcribe_hook = fail
    with pytest.raises(ValueError, match='invalid result'):
        runtime.transcribe(specs[0].device.selector, b'x', timeout=1)
    with pytest.raises(CohortCancelled):
        runtime.transcribe(specs[1].device.selector, b'x', timeout=1)
    runtime.release(timeout=2)


@pytest.mark.parametrize('reason,error_type', [('pressure', MemoryPressureYield), ('stop', CohortCancelled)])
def test_native_cancellation_retains_control_reason(tmp_path, reason, error_type):
    from subgen_core.resident_worker import WorkerCancelled
    runtime, specs, _, workers, _ = setup(tmp_path)
    runtime.load(timeout=2)
    def interrupted(cancel):
        runtime.cancel(reason=reason)
        raise WorkerCancelled('owned backend unwound')
    workers[specs[0].device.selector].transcribe_hook = interrupted
    with pytest.raises(error_type):
        runtime.transcribe(specs[0].device.selector, b'x', timeout=2)
    with pytest.raises(error_type):
        runtime.transcribe(specs[1].device.selector, b'x', timeout=2)
    runtime.release(timeout=2)
    with pytest.raises(error_type):
        runtime.transcribe(specs[0].device.selector, b'x', timeout=2)


def test_load_pressure_cancellation_is_not_a_media_error(tmp_path):
    from subgen_core.resident_worker import WorkerCancelled
    def configure(worker):
        def interrupted(cancel):
            runtime.cancel(reason='pressure')
            raise WorkerCancelled('load interrupted')
        worker.load_hook = interrupted
    runtime, _, _, workers, _ = setup(tmp_path, configure=configure)
    with pytest.raises(MemoryPressureYield):
        runtime.load(timeout=2)
    assert runtime.state == 'released' and all(not w.model_is_loaded for w in workers.values())


@pytest.mark.parametrize('timeout', [0, -1, True, float('nan'), float('inf'), '2'])
def test_invalid_deadlines(tmp_path, timeout):
    runtime, specs, _, _, _ = setup(tmp_path)
    with pytest.raises(ValueError):
        runtime.load(timeout=timeout)
    with pytest.raises(ValueError):
        runtime.release(timeout=timeout)
    with pytest.raises(ValueError):
        runtime.transcribe(specs[0].device.selector, b'x', timeout=timeout)
