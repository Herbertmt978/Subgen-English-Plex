"""Synthetic aggregate pressure cases; no GPU or memory allocation."""
from dataclasses import replace

import pytest

from subgen_core.cohort_policy import CohortPressureController
from subgen_core.cohort_runtime import CohortCancelled, CohortReleaseError
from subgen_core.execution_policy import ExecutionDevice
from subgen_core.resource_management import (GIB, MIB, PressureSample,
    WorkerAdmissionRequest, model_load_requirement, PressureController,
    gpu_priority_reserve_bytes)


class Harness:
    def __init__(self, count=2, shared=False):
        self.now = 100.0
        self.reads = self.sleeps = 0
        self.cancelled = False
        self.released = True
        self.sleep_hook = lambda: None
        self.host = PressureSample(host_total_bytes=64*GIB, host_available_bytes=40*GIB,
            cgroup_limit_bytes=32*GIB, cgroup_current_bytes=2*GIB,
            cgroup_oom_events=0, cgroup_oom_kill_events=0)
        self.gpus = tuple(PressureSample(gpu_total_bytes=24*GIB, gpu_free_bytes=20*GIB,
            gpu_device_id=f'{i+1:032x}') for i in range(count))
        self.requests = tuple(WorkerAdmissionRequest(
            ExecutionDevice('vulkan' if shared and i == count-1 else 'cuda', i,
                f'{i+1:032x}', f'Test {i}', 'shared' if shared and i == count-1 else 'dedicated'),
            model_load_requirement('small'), sample, gpu_priority_reserve_bytes(24*GIB))
            for i,sample in enumerate(self.gpus))
        self.reader = self.sample
        self.policy = CohortPressureController(self.requests, sample_reader=lambda:self.reader(),
            host_reserve_bytes=4*GIB, staging_reserve_bytes=256*MIB,
            require_cgroup=True, clock=lambda:self.now, sleep=self.sleep,
            release_verified=lambda:self.released, check_cancelled=self.check)

    def sample(self):
        self.reads += 1
        return replace(self.host, observed_at=self.now), tuple(
            replace(gpu, gpu_observed_at=self.now) for gpu in self.gpus)

    def check(self):
        if self.cancelled:
            raise CohortCancelled('test stop')

    def sleep(self, delay):
        self.now += delay
        self.sleeps += 1
        assert self.sleeps < 100, 'recovery did not terminate'
        self.sleep_hook()

    def advance(self):
        self.now += 1


@pytest.mark.parametrize('count', [1,2,3,4,5,32])
def test_one_cached_host_bundle_for_every_worker(count):
    h = Harness(count)
    assert h.reads == 0
    h.policy.decide_admission()
    assert h.reads == 1
    for _ in range(10):
        h.policy.check_healthy()
    assert h.reads == 1
    h.advance()
    h.policy.check_healthy()
    assert h.reads == 2


def test_resident_workers_do_not_need_room_for_a_second_model_copy():
    h = Harness()
    h.host = replace(h.host, host_available_bytes=8*GIB)
    assert not h.policy.decide_admission().admitted
    assert h.policy.check_healthy()


@pytest.mark.parametrize('kind', ['host','gpu','cgroup_oom','cgroup_headroom'])
def test_any_critical_resource_stops_the_whole_cohort(kind):
    h = Harness()
    assert h.policy.check_healthy()
    h.advance()
    if kind == 'host':
        h.host = replace(h.host, host_available_bytes=GIB)
    elif kind == 'gpu':
        h.gpus = (h.gpus[0], replace(h.gpus[1], gpu_free_bytes=100*MIB))
    elif kind == 'cgroup_oom':
        h.host = replace(h.host, cgroup_oom_kill_events=1)
    else:
        h.host = replace(h.host, cgroup_current_bytes=32*GIB-1)
    assert not h.policy.check_healthy() and h.policy.last_reasons


def test_moderate_pressure_uses_existing_distinct_sample_hysteresis():
    h = Harness()
    h.host = replace(h.host, psi_some_avg10=12.0)
    assert h.policy.check_healthy()
    assert h.policy.check_healthy()  # Same reading cannot count twice.
    h.advance()
    assert not h.policy.check_healthy()


def test_missing_dedicated_gpu_telemetry_uses_canonical_two_sample_rule():
    h = Harness()
    h.gpus = (h.gpus[0], replace(h.gpus[1], gpu_free_bytes=None))
    assert not h.policy.decide_admission().admitted
    assert h.policy.check_healthy()
    h.advance()
    assert not h.policy.check_healthy()


def test_shared_gpu_capacity_is_host_memory_not_an_independent_empty_bank():
    h = Harness(shared=True)
    h.gpus = (h.gpus[0], PressureSample(gpu_free_bytes=0))
    assert h.policy.decide_admission().admitted
    assert h.policy.check_healthy()


def test_device_sample_cannot_replace_host_accounting():
    h = Harness()
    h.host = replace(h.host, host_available_bytes=GIB)
    h.gpus = tuple(replace(gpu, host_available_bytes=128*GIB, host_total_bytes=128*GIB) for gpu in h.gpus)
    assert not h.policy.check_healthy()


def test_recovery_requires_combined_capacity_and_three_distinct_good_samples():
    h = Harness()
    h.host = replace(h.host, host_available_bytes=8*GIB)
    def restore():
        if h.sleeps == 4:
            h.host = replace(h.host, host_available_bytes=40*GIB)
    h.sleep_hook = restore
    assert h.policy.wait_for_recovery(RuntimeError()) == (0,0)
    assert h.sleeps >= 6
    assert h.policy.decide_admission().admitted


def test_recovery_never_proceeds_after_unconfirmed_release():
    h = Harness()
    h.released = False
    with pytest.raises(CohortReleaseError):
        h.policy.wait_for_recovery(RuntimeError())
    assert h.reads == 0 and h.sleeps == 0


def test_stale_repeated_observations_cannot_complete_recovery_and_stop_is_honoured():
    h = Harness()
    frozen = h.sample()
    h.reader = lambda:frozen
    h.sleep_hook = lambda:setattr(h,'cancelled',h.sleeps >= 4)
    with pytest.raises(CohortCancelled):
        h.policy.wait_for_recovery(RuntimeError())
    assert h.sleeps == 4


@pytest.mark.parametrize('value', [None, (PressureSample(), ()), ('bad', (PressureSample(),))])
def test_missing_or_malformed_bundle_yields_without_marking_media(value):
    h = Harness()
    h.reader = lambda:value
    assert not h.policy.check_healthy()
    assert h.policy.last_reasons == ('telemetry_unavailable',)


def test_required_telemetry_cannot_be_enabled_without_device_identity_and_reserve():
    with pytest.raises(ValueError, match='reserve and exact device'):
        PressureController(require_gpu_telemetry=True)


@pytest.mark.parametrize('malformed', [False, True])
def test_failed_probe_is_cached_without_reusing_healthy_observation(malformed):
    h = Harness()
    assert h.policy.check_healthy()
    calls = []
    def unavailable():
        calls.append(h.now)
        if malformed:
            return None
        raise OSError('test probe unavailable')
    h.reader = unavailable
    h.advance()
    for _ in range(20):
        assert not h.policy.check_healthy()
    assert len(calls) == 1
    assert h.policy._bundle is None
    h.advance()
    assert not h.policy.check_healthy()
    assert len(calls) == 2
    h.reader = h.sample
    assert not h.policy.check_healthy()  # Wait for a fresh observation.
    h.advance()
    assert h.policy.check_healthy()


def test_clock_regression_refuses_cached_observation():
    h = Harness()
    assert h.policy.check_healthy()
    h.now -= 1
    h.host = replace(h.host, host_available_bytes=GIB)
    assert not h.policy.check_healthy()
    assert h.reads == 1
    assert h.policy.last_reasons == ('telemetry_unavailable',)
