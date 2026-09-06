"""Synthetic device/resource cases; these are not physical GPU qualification."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from subgen_core.execution_policy import ExecutionDevice, resolve_execution_devices
from subgen_core.resource_management import (
    GIB, MIB, PressureSample, WorkerAdmissionRequest, CohortReservation,
    evaluate_cohort_admission, model_load_requirement,
    select_cohort_model,
)


def device(index=0, backend="cuda", topology="dedicated"):
    return ExecutionDevice(backend, index, f"{index + 1:032x}", f"Test {index}", topology)


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 32])
def test_selection_arbitrary_supported_count_preserves_user_order(count):
    inventory = [device(i) for i in range(count)]
    wanted = tuple(reversed(inventory))
    assert resolve_execution_devices(
        ", ".join(d.selector.upper() for d in wanted), inventory,
    ) == wanted


@pytest.mark.parametrize("value", [None, "", "  "])
def test_unset_preserves_legacy_path(value):
    assert resolve_execution_devices(value, []) == ()


@pytest.mark.parametrize("value", [
    True, 3, [], "cuda:0,", "cpu:0", "cuda:-1", "cuda:01", "cuda:32",
    "cuda:0,cuda:0", "cuda:1", "vulkan:0", "a" * 1025,
])
def test_invalid_or_unavailable_selection_never_substitutes(value):
    with pytest.raises(ValueError):
        resolve_execution_devices(value, [device()])


def test_same_physical_device_through_two_backends_is_rejected():
    with pytest.raises(ValueError, match="same physical GPU"):
        resolve_execution_devices("cuda:0,vulkan:0", [device(), device(backend="vulkan")])


@pytest.mark.parametrize("changes", [
    {"index": True}, {"index": 32}, {"physical_uuid": "0" * 32},
    {"physical_uuid": "GPU-unverified"}, {"name": "bad\nname"},
    {"memory_topology": "unknown"}, {"backend": "cpu"},
])
def test_discovery_contract(changes):
    with pytest.raises(ValueError):
        replace(device(), **changes)


def host(gib=32, **changes):
    sample = PressureSample(
        observed_at=100, host_total_bytes=gib * GIB,
        host_available_bytes=(gib - 2) * GIB,
        cgroup_limit_bytes=gib * GIB, cgroup_current_bytes=2 * GIB,
    )
    return replace(sample, **changes)


def request(index=0, model="small", topology="dedicated"):
    adapter = device(index, "vulkan" if topology == "shared" else "cuda", topology)
    return WorkerAdmissionRequest(
        adapter, model_load_requirement(model),
        PressureSample(gpu_total_bytes=24 * GIB, gpu_free_bytes=20 * GIB,
                       gpu_device_id=adapter.physical_uuid, gpu_observed_at=100),
        3 * GIB,
    )


def evaluate(requests, sample=None, **options):
    return evaluate_cohort_admission(
        tuple(requests), sample or host(), host_reserve_bytes=2 * GIB,
        staging_reserve_bytes=256 * MIB, require_cgroup=True, now=100, **options,
    )


def select_models(sample, *, requested='auto', count=2):
    candidates = {model:tuple(request(i, model) for i in range(count))
                  for model in ('tiny','base','small','medium','large-v3')}
    return select_cohort_model(candidates, sample, requested_model=requested,
        host_reserve_bytes=2*GIB, staging_reserve_bytes=256*MIB, require_cgroup=True, now=100)


def test_auto_quality_uses_combined_ram_not_the_best_gpu_alone():
    from subgen_core.human_progress import cohort_model_selection_lines
    chosen = select_models(host(20))
    assert chosen.selected_model == 'medium' and chosen.reason == 'highest_common_fit'
    large = chosen.assessments[0][1]
    assert all(worker.admitted for worker in large.workers)
    assert large.reasons == ('insufficient_combined_host',)
    text = '\n'.join(cohort_model_selection_lines(chosen))
    assert 'combined system RAM cannot fit all selected workers' in text
    assert 'Selected model: medium on all selected GPUs' in text
    assert 'Worker 1 memory requirement: conservative estimate, not measured usage' in text
    assert 'Worker 2 memory requirement: conservative estimate, not measured usage' in text


def test_explicit_large_model_waits_instead_of_falling_back():
    from subgen_core.human_progress import cohort_model_selection_lines
    chosen = select_models(host(20), requested='large-v3')
    assert chosen.selected_model is None and chosen.explicit
    assert chosen.reason == 'explicit_does_not_fit'
    assert 'no fallback model selected' in '\n'.join(cohort_model_selection_lines(chosen))


@pytest.mark.parametrize('capacity', [4,6,9,12,16,24,32,64,128,7,15,25,96])
def test_common_selection_has_no_additional_hardware_tier_ceiling(capacity):
    chosen = select_models(host(capacity))
    fitting = [model for model in ('tiny','base','small','medium','large-v3')
               if evaluate([request(0,model),request(1,model)], host(capacity)).admitted]
    assert chosen.selected_model == (fitting[-1] if fitting else None)


def test_auto_quality_not_limited_by_catalog_insertion_order():
    candidates = {model:(request(0,model),request(1,model)) for model in ('small','large-v3','medium')}
    chosen = select_cohort_model(candidates, host(64), host_reserve_bytes=2*GIB,
        staging_reserve_bytes=256*MIB, now=100)
    assert chosen.selected_model == 'large-v3'


def test_explicit_missing_artifact_variant_does_not_select_a_different_checkpoint():
    chosen = select_cohort_model({'base':(request(0,'base'),)}, host(), requested_model='base.en',
        host_reserve_bytes=2*GIB, staging_reserve_bytes=256*MIB, now=100)
    assert chosen.selected_model is None and chosen.reason == 'explicit_unavailable'


@pytest.mark.parametrize('change', ['device','observation','reserve','model'])
def test_model_candidates_cannot_change_observations_or_worker_identity(change):
    small = request(0,'small')
    large = request(0,'large-v3')
    if change == 'device':
        large = replace(large, device=device(1))
    elif change == 'observation':
        large = replace(large, gpu_sample=replace(large.gpu_sample, gpu_free_bytes=19*GIB))
    elif change == 'reserve':
        large = replace(large, device_reserve_bytes=4*GIB)
    else:
        large = small
    with pytest.raises(ValueError):
        select_cohort_model({'small':(small,), 'large-v3':(large,)}, host(),
            host_reserve_bytes=2*GIB, staging_reserve_bytes=256*MIB, now=100)


def test_individual_fit_does_not_imply_combined_fit():
    workers = [request(0, "medium"), request(1, "medium")]
    assert evaluate(workers[:1], host(12)).admitted
    decision = evaluate(workers, host(12))
    assert all(d.admitted for d in decision.workers)
    assert not decision.admitted
    assert decision.reasons == ("insufficient_combined_host",)


def test_shared_heap_counted_once_not_as_independent_ram_and_vram():
    shared = request(1, topology="shared")
    decision = evaluate([request(), shared])
    assert decision.admitted
    # CUDA host 2.5 + unified max(host 2.5, device 4) + staging .25.
    assert decision.required_host_bytes == 6 * GIB + 768 * MIB
    assert decision.workers[1].device_admission_bytes is None


def test_dedicated_vram_cannot_be_borrowed_from_another_gpu():
    small_gpu = request(1)
    small_gpu = replace(small_gpu, gpu_sample=replace(
        small_gpu.gpu_sample, gpu_free_bytes=3 * GIB,
    ))
    decision = evaluate([request(), small_gpu])
    assert not decision.admitted
    assert "cuda:1:insufficient_device" in decision.reasons


@pytest.mark.parametrize("changes,reason", [
    ({"gpu_observed_at": 0}, "gpu_unavailable"),
    ({"gpu_device_id": "wrong"}, "gpu_unavailable"),
])
def test_wrong_or_stale_gpu_observation(changes, reason):
    entry = request()
    decision = evaluate([replace(entry, gpu_sample=replace(entry.gpu_sample, **changes))])
    assert not decision.admitted
    assert f"cuda:0:{reason}" in decision.reasons


def test_gpu_sample_cannot_supply_a_larger_host_budget():
    entry = request()
    entry = replace(entry, gpu_sample=replace(
        entry.gpu_sample, host_available_bytes=128 * GIB, host_total_bytes=128 * GIB,
    ))
    assert not evaluate([entry], host(4)).admitted


@pytest.mark.parametrize("count", [3, 4, 5])
@pytest.mark.parametrize("capacity", [4, 6, 9, 12, 16, 24, 32, 64, 128])
def test_n_device_capacity_boundaries(count, capacity):
    decision = evaluate([request(i) for i in range(count)], host(capacity))
    assert decision.required_host_bytes == count * (2 * GIB + 512 * MIB) + 256 * MIB
    assert decision.admitted == (decision.required_host_bytes <= decision.available_host_bytes)


def test_refuses_duplicate_physical_workers_and_different_models():
    with pytest.raises(ValueError, match="physical GPU"):
        evaluate([request(), request()])
    with pytest.raises(ValueError, match="same model"):
        evaluate([request(), request(1, "medium")])


def test_atomic_reservation_samples_only_once_for_competing_callers():
    lease = CohortReservation()
    barrier = threading.Barrier(2)
    calls = []

    def acquire():
        barrier.wait(timeout=5)
        try:
            return lease.acquire(lambda: (calls.append(1), evaluate([request()]))[1])
        except RuntimeError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: acquire(), range(2)))
    winner = next(result for result in results if result is not None)
    assert results.count(None) == 1
    assert calls == [1]
    lease.release(winner[0], lambda: True)
    assert lease.acquire(lambda: evaluate([request()]))[0] is not None


def test_reservation_is_retained_until_verified_unload():
    lease = CohortReservation()
    token, _ = lease.acquire(lambda: evaluate([request()]))
    with pytest.raises(RuntimeError, match="not verified"):
        lease.release(token, lambda: False)
    with pytest.raises(RuntimeError, match="still owns"):
        lease.acquire(lambda: evaluate([request()]))
    with pytest.raises(ValueError, match="Wrong"):
        lease.release(object(), lambda: True)
    lease.release(token, lambda: True)


def test_refused_admission_does_not_claim_a_reservation():
    lease = CohortReservation()
    assert lease.acquire(lambda: evaluate([request()], host(4)))[0] is None
    assert lease.acquire(lambda: evaluate([request()]))[0] is not None
