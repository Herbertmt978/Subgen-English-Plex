"""Runner/lifecycle integration with deterministic workers, not GPU qualification."""
from dataclasses import replace
import hashlib
import threading
import time

import pytest

from subgen_core.cohort_runtime import CohortModelRuntime, CohortWorkerSpec, CohortCancelled, CohortReleaseError
from subgen_core.execution_policy import ExecutionDevice
from subgen_core.model_envelope_catalog import ModelArtifactIdentity
from subgen_core.media import MediaProcessReleaseUnconfirmed
from subgen_core.parallel_transcription import run_parallel_segmented_transcription
from subgen_core.resident_worker import WorkerCancelled, WorkerAllocationFailure
from subgen_core.resource_management import AdaptiveChunkState, CohortAdmissionDecision, CohortReservation, MemoryPressureYield
from subgen_core.segmented_result import PendingChunkStore


class Harness:
    def __init__(self, path, count=2, duration=80):
        weights = path / 'weights.bin'
        weights.write_bytes(b'test weights')
        self.specs = tuple(CohortWorkerSpec(
            ExecutionDevice('cuda', i, f'{i+1:032x}', f'GPU {i}', 'dedicated'),
            ModelArtifactIdentity('base', 'ctranslate2', 'float16',
                'sha256:'+hashlib.sha256(b'test weights').hexdigest(), 12, 'sha256:'+'a'*64),
            weights, self.worker) for i in range(count))
        self.reservation = CohortReservation()
        self.cohorts, self.handles, self.commits, self.events, self.attempts = [], [], [], [], []
        self.healthy = True
        self.cancelled = False
        self.recoveries = 0
        self.finalized = False
        self.hook = lambda *args: None
        self.load_hook = lambda *args: None
        self.extract_hook = lambda *args: None
        self.event_hook = lambda *args: None
        self.recovery_hook = lambda *args: None
        self.budgets = {s.device.selector: AdaptiveChunkState(20, minimum_seconds=5) for s in self.specs}
        self.duration = duration
        self.store = PendingChunkStore(directory=path, maximum_entries=count*2)

    def worker(self, spec):
        owner = self
        class Worker:
            model_is_loaded = False
            def load(self, *, timeout, cancel):
                self.model_is_loaded = True
                owner.load_hook(len(owner.cohorts), spec.device.selector, cancel)
            def release(self, *, timeout):
                self.model_is_loaded = False
                return True
            def transcribe(self, audio, *, timeout, cancel, **options):
                generation = len(owner.cohorts)
                owner.attempts.append((generation, spec.device.selector, audio))
                owner.hook(generation, spec.device.selector, audio, cancel)
                options['progress_callback'](1, 1)
                offset = audio.core_start-audio.extract_start
                return {'language':'en', 'segments':[{'start':offset+.1, 'end':offset+.9, 'text':'test cue'}]}
        handle = Worker()
        self.handles.append(handle)
        return handle

    def factory(self):
        cohort = CohortModelRuntime(self.specs, reservation=self.reservation,
            decide_admission=lambda: CohortAdmissionDecision(True, (), 10, 20, ()),
            check_healthy=lambda: self.healthy)
        self.cohorts.append(cohort)
        return cohort

    def check_cancelled(self):
        if self.cancelled:
            raise CohortCancelled('User stopped this file')

    def extract(self, window, *, timeout_seconds, check_cancelled):
        check_cancelled()
        self.extract_hook(window)
        check_cancelled()
        return window

    def event(self, name, **details):
        self.events.append((name, details))
        self.event_hook(name, details)

    def recover(self, error):
        assert all(c.state == 'released' for c in self.cohorts)
        assert not any(w.model_is_loaded for w in self.handles)
        assert isinstance(error, (MemoryPressureYield, WorkerAllocationFailure))
        self.recoveries += 1
        self.healthy = True
        return self.recovery_hook(error)

    def finalize(self, state):
        assert state.complete
        assert not any(w.model_is_loaded for w in self.handles)
        self.finalized = True
        return state

    def run(self, **overrides):
        args = dict(media_duration=self.duration, adaptive_by_worker=self.budgets,
            cohort_factory=self.factory, extract_chunk=self.extract, transcription_options={'language':'en'},
            store_result=self.store.store, read_result=self.store.read, discard_result=self.store.discard,
            persist_chunk=lambda w, r, s: self.commits.append(w), finalize_assembly=self.finalize,
            check_cancelled=self.check_cancelled, check_healthy=lambda:self.healthy,
            wait_for_recovery=self.recover, on_event=self.event, release_timeout=2)
        args.update(overrides)
        try:
            return run_parallel_segmented_transcription(**args)
        finally:
            self.store.close()


@pytest.mark.parametrize('count', [1, 2, 3, 4, 5])
def test_parallel_runner_orders_and_releases(tmp_path, count):
    h = Harness(tmp_path, count=count, duration=100)
    if count > 1:
        barrier = threading.Barrier(count)
        def hook(generation, worker, window, cancel):
            if window.core_start < count*20:
                barrier.wait(timeout=2)
        h.hook = hook
    result = h.run()
    assert result.complete and h.finalized
    assert h.commits[0].core_start == 0 and h.commits[-1].core_end == 100
    assert all(left.core_end == right.core_start for left, right in zip(h.commits, h.commits[1:]))
    assert all(0 < w.core_end-w.core_start <= 20 for w in h.commits)
    assert len(h.cohorts) == 1 and h.cohorts[0].state == 'released'
    assert set(p.name for p in tmp_path.iterdir()) == {'weights.bin'}


def test_bounded_result_transform_runs_before_strict_staging(tmp_path):
    h = Harness(tmp_path, count=1, duration=20)
    def invalid(result):
        result['segments'][0]['end'] = -1
        return result
    with pytest.raises(Exception, match='start|end|time|Segment'):
        h.run(transform_result=invalid)
    assert not h.commits and not h.finalized
    assert all(c.state == 'released' for c in h.cohorts)


def test_result_transform_cancellation_prevents_publication(tmp_path):
    h = Harness(tmp_path, count=1, duration=20)
    def transform(result):
        h.cancelled = True
        return result
    with pytest.raises(CohortCancelled):
        h.run(transform_result=transform)
    assert not h.commits and not h.finalized
    assert all(c.state == 'released' for c in h.cohorts)


def test_result_transform_memory_failure_shrinks_and_retries(tmp_path):
    h = Harness(tmp_path, count=1, duration=20)
    calls = []
    def transform(result):
        calls.append(1)
        if len(calls) == 1:
            raise MemoryError('bounded result allocation failed')
        return result
    assert h.run(transform_result=transform).complete
    assert h.recoveries == 1 and h.budgets['cuda:0'].current_seconds == 10
    assert [(g, w.core_start, w.core_end) for g, _, w in h.attempts][:2] == [(1,0,20),(2,0,10)]
    assert all(c.state == 'released' for c in h.cohorts)


def test_pressure_retains_commits_shrinks_and_reloads_same_model(tmp_path):
    h = Harness(tmp_path, count=1)
    def hook(generation, worker, window, cancel):
        if generation == 1 and window.core_start == 20:
            h.healthy = False
            assert cancel.wait(2)
            raise WorkerCancelled('Interrupted inference')
    h.hook = hook
    h.run()
    assert h.recoveries == 1 and len(h.cohorts) == 2
    assert [(g, w.core_start, w.core_end) for g, _, w in h.attempts][:3] == [(1,0,20), (1,20,40), (2,20,30)]
    assert [w.core_start for w in h.commits] == [0,20,30,40,50,70]
    assert all(c.state == 'released' for c in h.cohorts)


def test_pressure_preserves_finished_later_sections(tmp_path):
    h = Harness(tmp_path, duration=40)
    def hook(generation, worker, window, cancel):
        if generation == 1 and window.core_start == 0:
            assert cancel.wait(2)
            raise WorkerCancelled('Interrupted first section')
    h.hook = hook
    # Both initial windows are claimed. Let the later result reach the spool,
    # then introduce pressure during the next main-thread health check.
    original_store = h.store.store
    def stage(result):
        handle = original_store(result)
        h.healthy = False
        return handle
    calls = 0
    def stage_once(result):
        nonlocal calls
        calls += 1
        return stage(result) if calls == 1 else original_store(result)
    h.run(store_result=stage_once)
    assert h.recoveries == 1
    assert sum(w.core_start == 20 for _, _, w in h.attempts) == 1
    assert [(w.core_start, w.core_end) for w in h.commits] == [(0,10), (10,20), (20,40)]


def test_pressure_during_extraction_retries_without_inference_on_that_audio(tmp_path):
    h = Harness(tmp_path, count=1, duration=20)
    def extract(window):
        if len(h.cohorts) == 1:
            h.healthy = False
    h.extract_hook = extract
    h.run()
    assert h.recoveries == 1
    assert all(g == 2 for g, _, _ in h.attempts)
    assert [(w.core_start,w.core_end) for w in h.commits] == [(0,10),(10,20)]


def test_stop_never_recovers_or_finalizes(tmp_path):
    h = Harness(tmp_path, count=1)
    def hook(generation, worker, window, cancel):
        h.cancelled = True
        assert cancel.wait(2)
        raise WorkerCancelled('Stopped')
    h.hook = hook
    with pytest.raises(CohortCancelled):
        h.run()
    assert not h.finalized and not h.recoveries
    assert h.cohorts[0].state == 'released'


def test_terminal_inference_error_not_hidden_by_sibling_cancel(tmp_path):
    h = Harness(tmp_path)
    barrier = threading.Barrier(2)
    def hook(generation, worker, window, cancel):
        barrier.wait(timeout=2)
        if worker == 'cuda:1':
            raise ValueError('Invalid transcript')
        assert cancel.wait(2)
        raise WorkerCancelled('Sibling cancelled')
    h.hook = hook
    with pytest.raises(ValueError, match='Invalid transcript'):
        h.run()
    assert not h.finalized and not h.recoveries
    assert h.cohorts[0].state == 'released'


def test_model_identity_cannot_change_after_pressure(tmp_path):
    h = Harness(tmp_path, count=1)
    h.extract_hook = lambda window: setattr(h, 'healthy', False)
    def recover(error):
        h.specs = tuple(replace(s, artifact=replace(s.artifact, model='small')) for s in h.specs)
    h.recovery_hook = recover
    with pytest.raises(ValueError, match='identity changed'):
        h.run()
    assert len(h.handles) == 1 and all(c.state == 'released' for c in h.cohorts)


def test_wrong_factory_type_is_not_masked_by_cleanup(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(TypeError, match='canonical cohort'):
        h.run(cohort_factory=lambda: object())


def test_final_commit_pressure_waits_without_retranscribing_or_reloading(tmp_path):
    h = Harness(tmp_path, count=1, duration=20)
    def event(name, details):
        if name == 'committed':
            h.healthy = False
    h.event_hook = event
    h.run()
    assert h.finalized and h.recoveries == 1
    assert len(h.cohorts) == 1 and len(h.attempts) == 1


def test_stop_between_ordered_commits_prevents_later_commit(tmp_path):
    h = Harness(tmp_path, duration=40)
    barrier = threading.Barrier(2)
    h.hook = lambda *args: barrier.wait(timeout=2)
    def event(name, details):
        if name == 'committed':
            h.cancelled = True
    h.event_hook = event
    with pytest.raises(CohortCancelled):
        h.run()
    assert len(h.commits) == 1 and not h.finalized and not h.recoveries


def test_extraction_timeout_releases_without_transcribing(tmp_path):
    h = Harness(tmp_path, count=1)
    h.extract_hook = lambda window: time.sleep(.02)
    with pytest.raises(TimeoutError, match='during extraction'):
        h.run(chunk_timeout=.01)
    assert not h.attempts and not h.finalized and not h.recoveries
    assert h.cohorts[0].state == 'released'


def test_unconfirmed_extraction_exit_retains_combined_lease(tmp_path):
    h = Harness(tmp_path, count=1)
    child = object()
    def extract(window):
        raise MediaProcessReleaseUnconfirmed(child)
    h.extract_hook = extract
    try:
        with pytest.raises(CohortReleaseError, match='extraction has not exited') as failure:
            h.run()
        assert failure.value.process is child and failure.value.cohort is h.cohorts[0]
        assert h.handles[0].model_is_loaded and not h.finalized
        with pytest.raises(RuntimeError, match='still owns'):
            h.factory().load(timeout=1)
    finally:
        # Test owner has resolved its fake extraction handle and now releases.
        h.cohorts[0].release(timeout=2)


def test_unresponsive_worker_keeps_reservation_until_explicit_release(tmp_path):
    h = Harness(tmp_path, count=1)
    finish = threading.Event()
    def hook(generation, worker, window, cancel):
        h.cancelled = True
        assert finish.wait(5)
    h.hook = hook
    try:
        with pytest.raises(CohortReleaseError, match='has not unwound') as failure:
            h.run(release_timeout=.05)
        assert failure.value.cohort is h.cohorts[0]
        assert h.handles[0].model_is_loaded and not h.finalized
        second = h.factory()
        with pytest.raises(RuntimeError, match='still owns'):
            second.load(timeout=1)
    finally:
        finish.set()
        h.cohorts[0].release(timeout=2)
    assert not any(w.model_is_loaded for w in h.handles)


def test_allocation_failure_shrinks_and_finishes_same_model(tmp_path):
    h = Harness(tmp_path, count=1, duration=40)
    def hook(generation, worker, window, cancel):
        if window.core_end-window.core_start > 10:
            raise WorkerAllocationFailure('transcribe')
    h.hook = hook
    h.run()
    assert h.finalized and h.recoveries >= 1
    assert all(w.core_end-w.core_start <= 10 for w in h.commits)
    assert all(c.state == 'released' for c in h.cohorts)


def test_allocation_exhausts_after_two_actual_minimum_attempts(tmp_path):
    h = Harness(tmp_path, count=1, duration=4)  # Short tail is already below the minimum budget.
    def hook(*args):
        raise WorkerAllocationFailure('transcribe')
    h.hook = hook
    with pytest.raises(WorkerAllocationFailure) as failure:
        h.run()
    assert failure.value.worker == 'cuda:0'
    assert len(h.attempts) == 2 and h.recoveries == 2
    assert not h.finalized and all(c.state == 'released' for c in h.cohorts)


def test_model_load_allocation_failure_does_not_shrink_audio_budget(tmp_path):
    h = Harness(tmp_path, count=1)
    def load(*args):
        raise WorkerAllocationFailure('load')
    h.load_hook = load
    with pytest.raises(WorkerAllocationFailure) as failure:
        h.run()
    assert failure.value.phase == 'load' and failure.value.worker == 'cuda:0'
    assert len(h.cohorts) == 2 and not h.attempts and not h.finalized
    assert h.budgets['cuda:0'].current_seconds == 20


def test_load_can_recover_on_fresh_admission_once(tmp_path):
    h = Harness(tmp_path, count=1, duration=20)
    def load(generation, worker, cancel):
        if generation == 1:
            raise WorkerAllocationFailure('load')
    h.load_hook = load
    h.run()
    assert h.finalized and h.recoveries == 1 and len(h.cohorts) == 2


def test_verified_external_pressure_recovery_resets_allocation_failures(tmp_path):
    h = Harness(tmp_path, count=1, duration=4)
    def hook(generation, *args):
        if generation <= 2:
            raise WorkerAllocationFailure('transcribe')
    h.hook = hook
    h.recovery_hook = lambda error: (1, 2) if h.recoveries == 2 else None
    h.run()
    assert h.finalized and h.recoveries == 2


def test_sibling_terminal_failure_wins_over_allocation_retry(tmp_path):
    h = Harness(tmp_path)
    barrier = threading.Barrier(2)
    def hook(generation, worker, window, cancel):
        barrier.wait(timeout=2)
        if worker == 'cuda:0':
            raise WorkerAllocationFailure('transcribe')
        raise ValueError('Invalid result')
    h.hook = hook
    with pytest.raises(ValueError, match='Invalid result'):
        h.run()
    assert not h.recoveries and not h.finalized


def test_pressure_during_load_is_observed_before_model_ready(tmp_path):
    h = Harness(tmp_path, count=1, duration=20)
    def load(generation, worker, cancel):
        if generation == 1:
            h.healthy = False
            assert cancel.wait(2)
            raise WorkerCancelled('Cold allocation cancelled')
    h.load_hook = load
    h.run()
    assert h.recoveries == 1 and h.finalized
    assert all(generation == 2 for generation, _, _ in h.attempts)


def test_stop_during_load_does_not_wait_for_full_load_deadline(tmp_path):
    h = Harness(tmp_path, count=1)
    def load(generation, worker, cancel):
        h.cancelled = True
        assert cancel.wait(2)
        raise WorkerCancelled('Cold allocation stopped')
    h.load_hook = load
    with pytest.raises(CohortCancelled):
        h.run()
    assert not h.attempts and not h.recoveries and h.cohorts[0].state == 'released'
