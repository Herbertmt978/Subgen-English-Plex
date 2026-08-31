import errno
from dataclasses import FrozenInstanceError, replace
import threading

import pytest

import subgen_core.resource_management as resource_policy
from subgen_core.model_envelope_catalog import (
    EnvelopeDisposition,
    EnvelopeMeasurements,
    EnvelopePolicy,
    EnvelopeResolution,
    ImageIdentity,
    ModelEnvelope,
    RuntimeIdentity,
)
from subgen_core.resource_management import (
    AdaptiveChunkState,
    CapacityProfile,
    GIB,
    MIB,
    MemoryPressureYield,
    ModelDecision,
    PressureController,
    PressureSample,
    cgroup_headroom_floor,
    discover_capacity,
    host_reserve_bytes,
    initial_chunk_seconds,
    is_allocation_failure,
    select_model,
)


def mapping_reader(values):
    def read(path):
        key = str(path).replace("\\", "/")
        if key not in values:
            raise FileNotFoundError(key)
        return values[key]

    return read


def profile(capacity_gib):
    capacity = None if capacity_gib is None else int(capacity_gib * GIB)
    return CapacityProfile(
        capacity, capacity, None, "physical" if capacity else "unknown"
    )


def healthy_sample(**changes):
    values = {
        "observed_at": 0.0,
        "host_available_bytes": 4 * GIB,
        "host_total_bytes": 16 * GIB,
        "cgroup_current_bytes": 2 * GIB,
        "cgroup_limit_bytes": 8 * GIB,
        "cgroup_oom_events": 0,
        "cgroup_oom_kill_events": 0,
    }
    values.update(changes)
    return PressureSample(**values)


def make_envelope(
    model="large-v3",
    *,
    host_incremental=4 * GIB,
    cgroup_incremental=5 * GIB,
    device_incremental=8 * GIB,
    host_margin=768 * MIB,
    device_margin=2 * GIB,
):
    host_preload = GIB
    cgroup_preload = GIB
    device_preload = GIB
    return ModelEnvelope(
        image_identity=ImageIdentity(
            "sha256:" + "1" * 64,
            ("sha256:" + "2" * 64,),
        ),
        runtime=RuntimeIdentity(
            "1.0",
            "1.0",
            "1.0",
            "12.0",
            "550.0",
            "GPU-A",
            "8.6",
            24 * GIB,
        ),
        policy=EnvelopePolicy(
            model,
            "hf:" + "3" * 40,
            "float16",
            "translate",
            1,
            20,
            "sha256:" + "4" * 64,
        ),
        measurements=EnvelopeMeasurements(
            3,
            host_preload,
            host_preload + host_incremental,
            cgroup_preload,
            cgroup_preload + cgroup_incremental,
            device_preload,
            device_preload + device_incremental,
            host_incremental,
            cgroup_incremental,
            device_incremental,
            host_margin,
            device_margin,
        ),
    )


def exact_resolution(envelope):
    return EnvelopeResolution(
        envelope,
        EnvelopeDisposition.EXACT_MATCH,
        None,
    )


def test_capacity_values_are_immutable():
    values = (
        profile(8),
        ModelDecision("small", "small", False),
        PressureSample(host_available_bytes=GIB),
    )
    for value in values:
        with pytest.raises(FrozenInstanceError):
            value.warning = "changed"


def test_discover_capacity_prefers_finite_cgroup_v2_and_retains_host_total():
    discovered = discover_capacity(
        read_text=mapping_reader({"/v2/max": str(6 * GIB), "/v1/max": str(3 * GIB)}),
        physical_memory_reader=lambda: 32 * GIB,
        cgroup_v2_path="/v2/max",
        cgroup_v1_paths=("/v1/max",),
    )

    assert discovered.effective_bytes == 6 * GIB
    assert discovered.host_total_bytes == 32 * GIB
    assert discovered.cgroup_limit_bytes == 6 * GIB
    assert discovered.source == "cgroup_v2"
    assert discovered.cgroup_version == 2


def test_discover_capacity_uses_finite_cgroup_v1_after_missing_v2():
    discovered = discover_capacity(
        read_text=mapping_reader({"/v1/max": str(5 * GIB)}),
        physical_memory_reader=lambda: 24 * GIB,
        cgroup_v2_path="/v2/max",
        cgroup_v1_paths=("/v1/max",),
    )

    assert discovered.effective_bytes == 5 * GIB
    assert discovered.source == "cgroup_v1"
    assert discovered.cgroup_version == 1


@pytest.mark.parametrize("unbounded", ["max\n", str((1 << 63) - 4096)])
def test_unbounded_cgroup_values_fall_back_to_physical_memory(unbounded):
    discovered = discover_capacity(
        read_text=mapping_reader({"/v2/max": unbounded, "/v1/max": unbounded}),
        physical_memory_reader=lambda: 12 * GIB,
        cgroup_v2_path="/v2/max",
        cgroup_v1_paths=("/v1/max",),
    )

    assert discovered.effective_bytes == 12 * GIB
    assert discovered.source == "physical"
    assert discovered.cgroup_unbounded is True


def test_unknown_capacity_records_unbounded_reason_without_free_memory_guessing():
    discovered = discover_capacity(
        read_text=mapping_reader({"/v2/max": "max"}),
        physical_memory_reader=lambda: None,
        cgroup_v2_path="/v2/max",
        cgroup_v1_paths=("/v1/max",),
    )

    assert discovered.effective_bytes is None
    assert discovered.source == "unknown"
    assert discovered.cgroup_unbounded is True
    assert "unbounded" in discovered.warning.casefold()


@pytest.mark.parametrize(
    ("capacity_gib", "expected"),
    [
        (1.999, "tiny"),
        (2, "base"),
        (3.999, "base"),
        (4, "small"),
        (7.999, "small"),
        (8, "medium"),
        (15.999, "medium"),
        (16, "large-v3"),
    ],
)
def test_cpu_automatic_model_tier_boundaries(capacity_gib, expected):
    assert resource_policy.fallback_model_ceiling(profile(capacity_gib)) == expected


@pytest.mark.parametrize(
    ("vram_gib", "expected"),
    [
        (1.999, "tiny"),
        (2, "base"),
        (2.999, "base"),
        (3, "small"),
        (6.999, "small"),
        (7, "medium"),
        (11.999, "medium"),
        (12, "large-v3"),
    ],
)
def test_gpu_vram_tier_boundaries(vram_gib, expected):
    assert (
        resource_policy.fallback_model_ceiling(
            profile(64),
            device="cuda",
            allocatable_vram_bytes=int(vram_gib * GIB),
        )
        == expected
    )


def test_gpu_uses_lower_of_system_and_vram_ceilings():
    ceiling = resource_policy.fallback_model_ceiling
    ram_limited = ceiling(profile(4), device="cuda", allocatable_vram_bytes=16 * GIB)
    vram_limited = ceiling(profile(32), device="cuda", allocatable_vram_bytes=3 * GIB)

    assert ram_limited == "small"
    assert vram_limited == "small"


def test_unknown_capacity_or_allocatable_vram_uses_conservative_small_ceiling():
    assert resource_policy.fallback_model_ceiling(profile(None)) == "small"
    assert (
        resource_policy.fallback_model_ceiling(
            profile(32), device="cuda:0", allocatable_vram_bytes=None
        )
        == "small"
    )


def test_known_zero_allocatable_vram_is_tiny_not_unknown():
    assert (
        resource_policy.fallback_model_ceiling(
            profile(32), device="cuda", allocatable_vram_bytes=0
        )
        == "tiny"
    )


def test_automatic_selection_without_fresh_admission_telemetry_fails_closed():
    decision = select_model("auto", profile(32))

    assert decision.selected_model is None
    assert decision.reason == "no_safe_model"
    assert "telemetry" in decision.warning.casefold()


def test_explicit_model_wins_and_warns_only_when_above_automatic_ceiling():
    roomy = healthy_sample(
        host_available_bytes=32 * GIB,
        host_total_bytes=32 * GIB,
        cgroup_limit_bytes=32 * GIB,
        cgroup_current_bytes=GIB,
    )
    above = select_model("large-v3", profile(4), admission_sample=roomy, now=0.0)
    within = select_model("base", profile(4), admission_sample=roomy, now=0.0)
    custom = select_model(
        "my-private-model", profile(1), admission_sample=roomy, now=0.0
    )

    assert above.selected_model == "large-v3"
    assert above.explicit is True
    assert above.admitted is True
    assert "above" in above.warning.casefold()
    assert within.warning is None
    assert custom.selected_model == "my-private-model"
    assert custom.admitted is False
    assert custom.reason == "no_load_budget"


@pytest.mark.parametrize(
    ("requested", "family", "host_load", "device_load"),
    [
        ("tiny.en", "tiny", 3 * GIB // 4, GIB),
        ("base.en", "base", GIB, 2 * GIB),
        ("small.en", "small", 2 * GIB, 3 * GIB),
        ("medium.en", "medium", 5 * GIB, 7 * GIB),
        ("large-v2", "large-v3", 9 * GIB, 12 * GIB),
        ("turbo", "large-v3", 9 * GIB, 12 * GIB),
        ("large-v3-turbo", "large-v3", 9 * GIB, 12 * GIB),
    ],
)
def test_recognized_explicit_aliases_stay_fixed_with_family_budgets(
    requested,
    family,
    host_load,
    device_load,
):
    roomy = healthy_sample(
        host_available_bytes=48 * GIB,
        host_total_bytes=64 * GIB,
        cgroup_limit_bytes=64 * GIB,
        cgroup_current_bytes=GIB,
    )

    decision = select_model(requested, profile(64), admission_sample=roomy, now=0.0)

    assert resource_policy._model_family(requested) == family
    assert decision.selected_model == requested
    assert decision.admitted is True
    assert decision.requirement.host_incremental_bytes == host_load
    assert decision.requirement.device_incremental_bytes == device_load


@pytest.mark.parametrize(
    ("capacity_gib", "seconds"),
    [
        (3.999, 5 * 60),
        (4, 10 * 60),
        (7.999, 10 * 60),
        (8, 20 * 60),
        (15.999, 20 * 60),
        (16, 30 * 60),
        (None, 10 * 60),
    ],
)
def test_initial_chunk_tier_boundaries(capacity_gib, seconds):
    assert initial_chunk_seconds(profile(capacity_gib)) == seconds


@pytest.mark.parametrize("minutes", [5, 60])
def test_manual_chunk_boundaries_are_accepted(minutes):
    assert initial_chunk_seconds(profile(1), minutes) == minutes * 60


@pytest.mark.parametrize("invalid", [4, 61, 10.0, "10", True])
def test_manual_chunk_must_be_an_integer_from_five_to_sixty(invalid):
    with pytest.raises(ValueError):
        initial_chunk_seconds(profile(16), invalid)


def test_host_reserve_applies_host_minimum_capacity_cap_and_unknown_fallback():
    assert host_reserve_bytes(32 * GIB, 32 * GIB) == int(32 * GIB * 0.15)
    assert host_reserve_bytes(16 * GIB, 2 * GIB) == int(2 * GIB * 0.25)
    assert host_reserve_bytes(None, None) == GIB
    assert host_reserve_bytes(4 * GIB, None) == GIB


def test_explicit_reserve_replaces_host_reserve_only():
    assert host_reserve_bytes(profile(8), explicit_reserve_gib=1.5) == int(1.5 * GIB)


@pytest.mark.parametrize("invalid", [-1, 0, False, True])
def test_explicit_host_reserve_must_be_positive_and_non_boolean(invalid):
    with pytest.raises(ValueError, match="positive"):
        host_reserve_bytes(profile(8), explicit_reserve_gib=invalid)


def test_cgroup_floor_is_ten_percent_with_a_512_mib_minimum():
    assert cgroup_headroom_floor(2 * GIB) == 512 * MIB
    assert cgroup_headroom_floor(8 * GIB) == int(8 * GIB * 0.10)
    assert cgroup_headroom_floor(None) is None
    with pytest.raises(ValueError, match="bounded"):
        cgroup_headroom_floor(1 << 63)


def test_pressure_controller_throttles_samples_and_counts_each_only_once():
    now = [0.0]
    reads = []
    pressured = healthy_sample(host_available_bytes=100 * MIB)
    controller = PressureController(
        lambda: reads.append(now[0]) or replace(pressured, observed_at=now[0]),
        reserve_bytes=GIB,
        clock=lambda: now[0],
        sleep=lambda _delay: None,
    )

    assert controller.poll() == "normal"
    now[0] = 4.99
    assert controller.poll() == "normal"
    assert reads == [0.0]
    now[0] = 5.0
    assert controller.poll() == "yielding"
    assert reads == [0.0, 5.0]


def test_poll_token_cannot_make_a_nonadvancing_observation_distinct():
    now = [0.0]
    observed_at = [0.0]
    reads = []

    def read():
        reads.append(now[0])
        return healthy_sample(
            observed_at=observed_at[0],
            host_available_bytes=100 * MIB,
        )

    controller = PressureController(
        read,
        reserve_bytes=GIB,
        clock=lambda: now[0],
    )

    assert controller.poll() == "normal"
    now[0] = 5.0
    assert controller.poll() == "normal"
    now[0] = 10.0
    observed_at[0] = 10.0
    assert controller.poll() == "yielding"
    assert reads == [0.0, 5.0, 10.0]


@pytest.mark.parametrize(
    "pressured",
    [
        healthy_sample(host_available_bytes=100 * MIB),
        healthy_sample(cgroup_current_bytes=8 * GIB - 600 * MIB),
        healthy_sample(psi_full_avg10=1.0),
        healthy_sample(psi_some_avg10=10.0),
    ],
)
def test_two_consecutive_pressure_samples_enter_yielding(pressured):
    controller = PressureController(reserve_bytes=GIB)

    assert controller.observe(pressured) == "normal"
    assert controller.observe(replace(pressured, observed_at=5.0)) == "yielding"


@pytest.mark.parametrize(
    "field",
    [
        "psi_some_avg10",
        "psi_full_avg10",
        "host_psi_some_avg10",
        "host_psi_full_avg10",
        "cgroup_psi_some_avg10",
        "cgroup_psi_full_avg10",
    ],
)
@pytest.mark.parametrize(
    "invalid",
    [float("nan"), "10.0", False, True, float("inf"), float("-inf"), -1.0, 100.1],
)
def test_invalid_injected_psi_is_unavailable_and_cannot_advance_pressure(
    field,
    invalid,
):
    controller = PressureController(reserve_bytes=GIB)
    first = healthy_sample()
    second = healthy_sample(observed_at=5.0)
    object.__setattr__(first, field, invalid)
    object.__setattr__(second, field, invalid)

    assert controller.observe(first) == "normal"
    assert controller.observe(second) == "normal"
    assert controller.last_pressure_reasons == ()
    assert controller.last_critical_reasons == ()


def test_invalid_aggregate_psi_cannot_mask_valid_host_pressure():
    controller = PressureController(reserve_bytes=GIB)
    first = healthy_sample(
        psi_some_avg10=float("nan"),
        host_psi_some_avg10=10.0,
    )
    second = replace(first, observed_at=5.0)

    assert controller.observe(first) == "normal"
    assert controller.last_pressure_reasons == ("psi_some",)
    assert controller.observe(second) == "yielding"


def test_reusing_the_same_observation_cannot_satisfy_a_two_sample_threshold():
    pressured = healthy_sample(host_available_bytes=100 * MIB)
    controller = PressureController(reserve_bytes=GIB)

    assert controller.observe(pressured) == "normal"
    assert controller.observe(pressured) == "normal"
    assert controller.observe(replace(pressured, observed_at=5.0)) == "yielding"


def test_healthy_sample_breaks_sustained_pressure_sequence():
    controller = PressureController(reserve_bytes=GIB)
    pressured = healthy_sample(host_available_bytes=100 * MIB)

    assert controller.observe(pressured) == "normal"
    assert controller.observe(healthy_sample(observed_at=5.0)) == "normal"
    assert controller.observe(replace(pressured, observed_at=10.0)) == "normal"


def test_inconsistent_host_telemetry_is_immediately_critical():
    controller = PressureController(reserve_bytes=GIB)

    assert (
        controller.observe(
            healthy_sample(
                host_available_bytes=64 * GIB,
                host_total_bytes=8 * GIB,
            )
        )
        == "yielding"
    )
    assert controller.last_critical_reasons == ("host_inconsistent",)


def test_critical_cgroup_headroom_yields_immediately():
    controller = PressureController(reserve_bytes=GIB)
    # 8 GiB floor is 0.8 GiB; 300 MiB is below half that floor.
    critical = healthy_sample(cgroup_current_bytes=8 * GIB - 300 * MIB)

    assert controller.observe(critical) == "yielding"
    assert controller.last_critical_reasons == ("critical_cgroup_headroom",)


def test_only_a_new_cgroup_oom_event_yields_immediately():
    controller = PressureController(reserve_bytes=GIB)

    assert controller.observe(healthy_sample(cgroup_oom_events=7)) == "normal"
    assert (
        controller.observe(healthy_sample(observed_at=5.0, cgroup_oom_events=7))
        == "normal"
    )
    assert (
        controller.observe(healthy_sample(observed_at=10.0, cgroup_oom_events=8))
        == "yielding"
    )
    assert controller.last_critical_reasons == ("cgroup_oom",)


def test_missing_or_reset_oom_telemetry_does_not_create_a_false_new_event():
    controller = PressureController(reserve_bytes=GIB)

    assert controller.observe(healthy_sample(cgroup_oom_events=7)) == "normal"
    assert (
        controller.observe(healthy_sample(observed_at=5.0, cgroup_oom_events=None))
        == "normal"
    )
    assert (
        controller.observe(healthy_sample(observed_at=10.0, cgroup_oom_events=7))
        == "normal"
    )
    assert (
        controller.observe(healthy_sample(observed_at=15.0, cgroup_oom_events=3))
        == "normal"
    )
    assert (
        controller.observe(healthy_sample(observed_at=20.0, cgroup_oom_events=4))
        == "yielding"
    )


def test_check_raises_private_control_exception_after_sustained_pressure():
    now = [0.0]
    sample = healthy_sample(host_available_bytes=0)
    controller = PressureController(
        lambda: replace(sample, observed_at=now[0]),
        reserve_bytes=GIB,
        clock=lambda: now[0],
    )

    assert controller.check_or_raise() == "normal"
    now[0] = 5.0
    with pytest.raises(MemoryPressureYield, match="host_headroom"):
        controller.check_or_raise()


def test_recovery_requires_three_consecutive_healthy_samples():
    now = [0.0]
    controller = PressureController(
        reserve_bytes=GIB,
        recovery_requirements=(resource_policy.model_load_requirement("tiny"),),
        clock=lambda: now[0],
    )
    controller.observe(healthy_sample(host_available_bytes=100 * MIB))
    now[0] = 5.0
    controller.observe(healthy_sample(observed_at=5.0, host_available_bytes=100 * MIB))
    controller.mark_released()

    now[0] = 10.0
    assert controller.observe(healthy_sample(observed_at=10.0)) == "recovering"
    now[0] = 15.0
    assert (
        controller.observe(
            healthy_sample(observed_at=15.0, host_available_bytes=100 * MIB)
        )
        == "recovering"
    )
    assert controller.healthy_recovery_samples == 0
    now[0] = 20.0
    assert controller.observe(healthy_sample(observed_at=20.0)) == "recovering"
    now[0] = 25.0
    assert controller.observe(healthy_sample(observed_at=25.0)) == "recovering"
    now[0] = 30.0
    assert controller.observe(healthy_sample(observed_at=30.0)) == "normal"


def test_no_safe_cpu_recovery_requires_a_real_fresh_candidate_admission_floor():
    now = [0.0]
    candidate = resource_policy.model_load_requirement("tiny")
    controller = PressureController(
        reserve_bytes=GIB,
        recovery_requirements=(candidate,),
        require_cgroup=True,
        clock=lambda: now[0],
    )
    controller.enter_no_safe_model()

    for observed in (0.0, 5.0, 10.0):
        now[0] = observed
        state = controller.observe(
            healthy_sample(
                observed_at=observed,
                host_available_bytes=8 * GIB,
                cgroup_limit_bytes=2 * GIB,
                cgroup_current_bytes=GIB,
            )
        )
        assert state == "recovering"
        assert controller.healthy_recovery_samples == 0

    for index, observed in enumerate((15.0, 20.0, 25.0), start=1):
        now[0] = observed
        state = controller.observe(
            healthy_sample(
                observed_at=observed,
                host_available_bytes=8 * GIB,
                cgroup_limit_bytes=4 * GIB,
                cgroup_current_bytes=GIB,
            )
        )
        assert state == ("normal" if index == 3 else "recovering")


def test_pressure_controller_serializes_concurrent_polling_and_observes_once():
    now = [0.0]
    reads = []

    def read():
        reads.append(now[0])
        return healthy_sample(
            observed_at=now[0],
            host_available_bytes=100 * MIB,
        )

    controller = PressureController(
        read,
        reserve_bytes=GIB,
        clock=lambda: now[0],
    )

    def poll_batch():
        barrier = threading.Barrier(9)
        states = []
        errors = []

        def worker():
            try:
                barrier.wait()
                states.append(controller.poll())
            except BaseException as error:  # pragma: no cover - assertion receipt
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2.0)
        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        return states

    assert poll_batch() == ["normal"] * 8
    now[0] = 5.0
    assert poll_batch() == ["yielding"] * 8
    assert reads == [0.0, 5.0]


def test_wait_for_recovery_uses_bounded_backoff_and_heartbeats():
    now = [0.0]
    sleeps = []
    heartbeats = []

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    controller = PressureController(
        lambda: healthy_sample(observed_at=now[0]),
        reserve_bytes=GIB,
        recovery_requirements=(resource_policy.model_load_requirement("tiny"),),
        clock=lambda: now[0],
        sleep=sleep,
    )
    controller.mark_released()

    assert (
        controller.wait_for_recovery(
            lambda: False,
            heartbeat=lambda state, delay: heartbeats.append((state, delay)),
        )
        is True
    )
    assert sleeps == [5.0, 10.0, 20.0]
    assert heartbeats == [
        ("recovering", 5.0),
        ("recovering", 10.0),
        ("recovering", 20.0),
    ]


def test_wait_for_recovery_stops_at_sixty_seconds_and_honors_cancellation():
    now = [0.0]
    sleeps = []
    cancelled = [False]

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
        if len(sleeps) == 6:
            cancelled[0] = True

    controller = PressureController(
        lambda: healthy_sample(host_available_bytes=100 * MIB),
        reserve_bytes=GIB,
        clock=lambda: now[0],
        sleep=sleep,
    )
    controller.mark_released()

    assert controller.wait_for_recovery(lambda: cancelled[0]) is False
    assert sleeps == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]


class OutOfMemoryError(RuntimeError):
    __module__ = "torch.cuda"


@pytest.mark.parametrize(
    "error",
    [
        MemoryError(),
        OutOfMemoryError(),
        OSError(errno.ENOMEM, "Cannot allocate memory"),
        RuntimeError("CUDA out of memory. Tried to allocate 64 MiB"),
        RuntimeError("CUDA error: out of memory"),
        RuntimeError("CUBLAS_STATUS_ALLOC_FAILED"),
        RuntimeError("CTranslate2: failed to allocate device memory"),
        RuntimeError("std::bad_alloc"),
    ],
)
def test_recognizes_strong_allocation_failure_signals(error):
    assert is_allocation_failure(error) is True


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("The operation ran out of memory labels in a parser"),
        ValueError("CUDA out of memory appeared in input text"),
        RuntimeError("Log parser saw 'CUDA out of memory' in user-provided text"),
        RuntimeError("not an out of memory condition"),
        RuntimeError("allocation failed validation"),
        Exception("std::bad_alloc"),
    ],
)
def test_does_not_treat_arbitrary_oom_text_as_allocation_failure(error):
    assert is_allocation_failure(error) is False


def test_recognizes_allocation_failure_in_exception_chain():
    outer = RuntimeError("transcription failed")
    outer.__cause__ = MemoryError("allocation")

    assert is_allocation_failure(outer) is True


def test_adaptive_chunk_halves_to_minimum_without_counting_pressure_as_failure():
    state = AdaptiveChunkState(20 * 60)

    assert state.record_pressure_yield() == 10 * 60
    assert state.record_pressure_yield() == 5 * 60
    assert state.record_pressure_yield() == 5 * 60
    assert state.minimum_allocation_failures == 0
    assert state.exhausted is False


def test_two_allocation_failures_at_minimum_exhaust_the_profile():
    state = AdaptiveChunkState(10 * 60)

    assert state.record_allocation_failure() is False
    assert state.current_seconds == 5 * 60
    assert state.record_allocation_failure() is False
    assert state.record_allocation_failure() is True
    assert state.exhausted is True


def test_external_pressure_recovery_separates_minimum_allocation_failures():
    state = AdaptiveChunkState(5 * 60)

    assert state.record_allocation_failure() is False
    state.record_external_pressure_recovery()
    assert state.record_allocation_failure() is False
    assert state.minimum_allocation_failures == 1


def test_three_healthy_successes_double_toward_but_never_above_baseline():
    state = AdaptiveChunkState(20 * 60)
    state.record_pressure_yield()
    state.record_pressure_yield()

    assert [state.record_success(healthy=True) for _ in range(3)] == [300, 300, 600]
    assert state.record_success(healthy=False) == 600
    assert [state.record_success(healthy=True) for _ in range(3)] == [600, 600, 1200]
    assert state.record_success(healthy=True) == 1200


@pytest.mark.parametrize(
    "kwargs",
    [
        {"baseline_seconds": 299},
        {"baseline_seconds": 600, "minimum_seconds": 0},
        {"baseline_seconds": 600.0},
        {"baseline_seconds": 600, "successes_to_grow": True},
    ],
)
def test_adaptive_chunk_configuration_is_validated(kwargs):
    with pytest.raises(ValueError):
        AdaptiveChunkState(**kwargs)


@pytest.mark.parametrize(
    ("model", "host_load", "device_load"),
    [
        ("tiny", 3 * GIB // 4, 1 * GIB),
        ("base", 1 * GIB, 2 * GIB),
        ("small", 2 * GIB, 3 * GIB),
        ("medium", 5 * GIB, 7 * GIB),
        ("large-v3", 9 * GIB, 12 * GIB),
    ],
)
def test_fallback_requirements_have_nonzero_incremental_budgets_and_margins(
    model,
    host_load,
    device_load,
):
    requirement = resource_policy.model_load_requirement(model)

    assert requirement.host_incremental_bytes == host_load
    assert requirement.cgroup_incremental_bytes == host_load
    assert requirement.device_incremental_bytes == device_load
    assert requirement.host_load_bytes == host_load
    assert requirement.required_host_bytes == host_load + 512 * MIB
    assert requirement.required_device_bytes == device_load + GIB
    assert requirement.provenance == "fallback"


def test_paired_incremental_peak_uses_each_run_delta_before_the_maximum():
    # Aggregating peaks and preloads independently would incorrectly return 7.
    assert (
        resource_policy.paired_incremental_peak_bytes(
            preload_bytes=(9, 1, 5),
            peak_bytes=(10, 8, 3),
        )
        == 7
    )
    assert resource_policy.paired_incremental_peak_bytes((8, 4), (3, 4)) == 0

    with pytest.raises(ValueError, match="same non-zero length"):
        resource_policy.paired_incremental_peak_bytes((1,), (2, 3))


@pytest.fixture
def exact_envelope():
    return make_envelope()


def test_exact_envelope_requirement_uses_max_host_term_once_and_exact_margins(
    exact_envelope,
):
    requirement = resource_policy.model_load_requirement(
        "large-v3",
        resolution=exact_resolution(exact_envelope),
    )

    assert requirement.host_load_bytes == 5 * GIB
    assert requirement.required_host_bytes == 5 * GIB + 768 * MIB
    assert requirement.required_device_bytes == 10 * GIB
    assert requirement.provenance == "envelope"
    assert requirement.envelope_resolution is not None
    assert requirement.exact_match is True


def test_envelope_admission_accepts_only_task2a_values_and_rejects_duplicates(
    exact_envelope,
):
    class DuckEnvelope:
        policy = exact_envelope.policy
        measurements = exact_envelope.measurements

    with pytest.raises(TypeError, match="exact EnvelopeResolution"):
        resource_policy.model_load_requirement("large-v3", resolution=DuckEnvelope())
    with pytest.raises(ValueError, match="Duplicate envelope model"):
        select_model(
            "auto",
            profile(32),
            envelopes=(
                exact_resolution(exact_envelope),
                exact_resolution(exact_envelope),
            ),
        )


def test_derived_model_requirement_must_remain_within_the_bounded_domain():
    huge = make_envelope(
        host_incremental=(1 << 63) - 1,
        cgroup_incremental=GIB,
        host_margin=1,
    )

    with pytest.raises(ValueError, match="Derived host requirement"):
        resource_policy.model_load_requirement(
            "large-v3", resolution=exact_resolution(huge)
        )


def test_model_requirement_cannot_be_tampered_away_from_source_evidence():
    requirement = resource_policy.model_load_requirement("small")

    with pytest.raises(TypeError, match="exact_match"):
        replace(requirement, exact_match=True)
    with pytest.raises(ValueError, match="source evidence"):
        replace(
            requirement, host_incremental_bytes=requirement.host_incremental_bytes + 1
        )


def test_raw_envelopes_and_non_exact_or_malformed_resolutions_are_rejected(
    exact_envelope,
):
    non_exact = EnvelopeResolution(
        None,
        EnvelopeDisposition.PUBLIC_FALLBACK,
        "catalog_missing",
    )
    malformed = object.__new__(EnvelopeResolution)
    object.__setattr__(malformed, "envelope", None)
    object.__setattr__(malformed, "disposition", EnvelopeDisposition.EXACT_MATCH)
    object.__setattr__(malformed, "reason_code", None)

    with pytest.raises(TypeError, match="exact EnvelopeResolution"):
        select_model("auto", profile(32), envelopes=(exact_envelope,))
    for resolution in (non_exact, malformed):
        with pytest.raises(ValueError):
            resource_policy.model_load_requirement(
                "large-v3",
                resolution=resolution,
            )


@pytest.mark.parametrize(
    ("model", "limit_gib", "current_gib", "expected_cgroup_admission"),
    [
        ("small", 4, 1, int(2.5 * GIB)),
        ("medium", 9, 2, 7 * GIB - int(9 * GIB * 0.10)),
    ],
)
def test_exact_cgroup_acceptance_profiles_fit_increment_plus_margin(
    model,
    limit_gib,
    current_gib,
    expected_cgroup_admission,
):
    decision = resource_policy.evaluate_admission(
        resource_policy.model_load_requirement(model),
        healthy_sample(
            host_available_bytes=16 * GIB,
            cgroup_limit_bytes=limit_gib * GIB,
            cgroup_current_bytes=current_gib * GIB,
        ),
        host_reserve_bytes=GIB,
        require_cgroup=True,
        now=0.0,
    )

    assert decision.cgroup_admission_bytes == expected_cgroup_admission
    assert decision.effective_host_admission_bytes == expected_cgroup_admission
    assert decision.admitted is True


def test_admission_checks_host_cgroup_and_device_independently_and_clamps_zero():
    requirement = resource_policy.model_load_requirement("small")
    decision = resource_policy.evaluate_admission(
        requirement,
        healthy_sample(
            observed_at=50.0,
            host_available_bytes=2 * GIB,
            cgroup_limit_bytes=4 * GIB,
            cgroup_current_bytes=5 * GIB,
            gpu_total_bytes=24 * GIB,
            gpu_free_bytes=4 * GIB,
            gpu_device_id="GPU-0",
            gpu_observed_at=50.0,
        ),
        host_reserve_bytes=GIB,
        gpu_priority_reserve_bytes=3 * GIB,
        require_cgroup=True,
        require_gpu=True,
        expected_gpu_device="GPU-0",
        now=50.0,
    )

    assert decision.host_admission_bytes == GIB
    assert decision.cgroup_admission_bytes == 0
    assert decision.effective_host_admission_bytes == 0
    assert decision.device_admission_bytes == GIB
    assert decision.admitted is False
    assert set(decision.reasons) == {"insufficient_host", "insufficient_device"}


@pytest.mark.parametrize(
    ("sample", "require_cgroup", "reason"),
    [
        (healthy_sample(host_available_bytes=None), False, "host_unavailable"),
        (
            healthy_sample(cgroup_limit_bytes=None, cgroup_current_bytes=None),
            True,
            "cgroup_unavailable",
        ),
    ],
)
def test_admission_fails_closed_when_a_required_host_term_is_unavailable(
    sample,
    require_cgroup,
    reason,
):
    decision = resource_policy.evaluate_admission(
        resource_policy.model_load_requirement("tiny"),
        sample,
        host_reserve_bytes=GIB,
        require_cgroup=require_cgroup,
        now=0.0,
    )

    assert decision.admitted is False
    assert reason in decision.reasons
    assert decision.effective_host_admission_bytes is None


def test_admission_fails_closed_when_host_available_exceeds_host_total():
    decision = resource_policy.evaluate_admission(
        resource_policy.model_load_requirement("tiny"),
        healthy_sample(
            host_available_bytes=64 * GIB,
            host_total_bytes=8 * GIB,
            cgroup_limit_bytes=None,
            cgroup_current_bytes=None,
        ),
        host_reserve_bytes=GIB,
        now=0.0,
    )

    assert decision.admitted is False
    assert decision.reasons == ("host_inconsistent",)
    assert decision.host_admission_bytes is None
    assert decision.effective_host_admission_bytes is None


def test_unbounded_or_unavailable_optional_cgroup_omits_only_that_term():
    decision = resource_policy.evaluate_admission(
        resource_policy.model_load_requirement("tiny"),
        healthy_sample(cgroup_limit_bytes=None, cgroup_current_bytes=None),
        host_reserve_bytes=GIB,
        now=0.0,
    )

    assert decision.admitted is True
    assert decision.effective_host_admission_bytes == 3 * GIB


def test_admission_requires_a_caller_decision_time_and_fresh_host_sample():
    requirement = resource_policy.model_load_requirement("tiny")
    sample = healthy_sample(observed_at=100.0)

    omitted = resource_policy.evaluate_admission(
        requirement,
        sample,
        host_reserve_bytes=GIB,
    )
    stale = resource_policy.evaluate_admission(
        requirement,
        sample,
        host_reserve_bytes=GIB,
        now=111.0,
    )

    assert omitted.admitted is False
    assert "decision_time_unavailable" in omitted.reasons
    assert stale.admitted is False
    assert "sample_stale" in stale.reasons


def test_gpu_admission_validates_gpu_age_independently_of_host_sample_age():
    decision = resource_policy.evaluate_admission(
        resource_policy.model_load_requirement("tiny"),
        healthy_sample(
            observed_at=20.0,
            gpu_total_bytes=24 * GIB,
            gpu_free_bytes=20 * GIB,
            gpu_device_id="GPU-A",
            gpu_observed_at=9.0,
        ),
        host_reserve_bytes=GIB,
        gpu_priority_reserve_bytes=4 * GIB,
        require_gpu=True,
        expected_gpu_device="GPU-A",
        now=20.0,
    )

    assert decision.admitted is False
    assert decision.reasons == ("gpu_unavailable",)


@pytest.mark.parametrize(
    "sample",
    [
        healthy_sample(host_available_bytes=-1),
        healthy_sample(host_available_bytes=True),
        healthy_sample(cgroup_current_bytes=False),
        healthy_sample(gpu_total_bytes=True),
    ],
)
def test_admission_rejects_negative_and_boolean_byte_telemetry(sample):
    with pytest.raises(ValueError, match="bounded"):
        resource_policy.evaluate_admission(
            resource_policy.model_load_requirement("tiny"),
            sample,
            host_reserve_bytes=GIB,
            now=0.0,
        )


def test_huge_numeric_inputs_raise_validation_errors_without_float_overflow():
    huge = 10**400

    with pytest.raises(ValueError, match="positive"):
        host_reserve_bytes(profile(8), explicit_reserve_gib=huge)
    with pytest.raises(ValueError, match="bounded"):
        resource_policy.paired_incremental_peak_bytes((huge,), (huge,))
    with pytest.raises(ValueError, match="finite"):
        resource_policy.evaluate_admission(
            resource_policy.model_load_requirement("tiny"),
            healthy_sample(),
            host_reserve_bytes=GIB,
            now=huge,
        )


def test_gpu_priority_reserve_has_a_mandatory_floor_and_explicit_values_only_raise_it():
    reserve = resource_policy.gpu_priority_reserve_bytes

    assert reserve(8 * GIB) == GIB
    assert reserve(24 * GIB) == int(24 * GIB * 0.10)
    assert reserve(24 * GIB, explicit_reserve_gib=4) == 4 * GIB
    assert reserve(24 * GIB, explicit_reserve_gib=0.5) == int(24 * GIB * 0.10)
    with pytest.raises(ValueError, match="explicit positive"):
        reserve(24 * GIB, canonical_shared_cuda=True)
    with pytest.raises(ValueError, match="positive"):
        reserve(24 * GIB, explicit_reserve_gib=0)
    for invalid in (-1, False, True):
        with pytest.raises(ValueError, match="positive"):
            reserve(24 * GIB, explicit_reserve_gib=invalid)


def test_selection_enforces_the_current_gpu_reserve_floor_without_silent_promotion():
    floor = 24 * GIB // 10
    sample = healthy_sample(
        host_available_bytes=32 * GIB,
        host_total_bytes=32 * GIB,
        cgroup_current_bytes=None,
        cgroup_limit_bytes=None,
        gpu_total_bytes=24 * GIB,
        gpu_free_bytes=20 * GIB,
        gpu_device_id="GPU-A",
        gpu_observed_at=0.0,
    )
    stabilized = resource_policy.StabilizedGpuCapacity(
        "GPU-A",
        24 * GIB,
        20 * GIB,
        0.0,
        3,
    )
    common = {
        "device": "cuda",
        "admission_sample": sample,
        "stabilized_gpu": stabilized,
        "expected_gpu_device": "GPU-A",
        "now": 0.0,
    }

    for reserve in (1, GIB):
        with pytest.raises(ValueError, match="mandatory GPU reserve floor"):
            select_model(
                "auto",
                profile(32),
                gpu_reserve_bytes=reserve,
                **common,
            )
    with pytest.raises(ValueError, match="mandatory GPU reserve floor"):
        select_model(
            "auto",
            profile(32),
            gpu_reserve_bytes=1,
            canonical_shared_cuda=True,
            **common,
        )

    for reserve in (floor, 4 * GIB):
        decision = select_model(
            "auto",
            profile(32),
            gpu_reserve_bytes=reserve,
            **common,
        )
        assert decision.admitted is True
        assert decision.selected_model is not None


def test_direct_gpu_admission_rejects_absolute_and_current_reserve_floor_bypasses():
    requirement = resource_policy.model_load_requirement("tiny")
    sample = healthy_sample(
        host_available_bytes=32 * GIB,
        host_total_bytes=32 * GIB,
        cgroup_current_bytes=None,
        cgroup_limit_bytes=None,
        gpu_total_bytes=24 * GIB,
        gpu_free_bytes=20 * GIB,
        gpu_device_id="GPU-A",
        gpu_observed_at=0.0,
    )
    common = {
        "host_reserve_bytes": GIB,
        "require_gpu": True,
        "expected_gpu_device": "GPU-A",
        "now": 0.0,
    }

    with pytest.raises(ValueError, match="mandatory GPU reserve floor"):
        resource_policy.evaluate_admission(
            requirement,
            sample,
            gpu_priority_reserve_bytes=1,
            **common,
        )

    below_current_floor = resource_policy.evaluate_admission(
        requirement,
        sample,
        gpu_priority_reserve_bytes=GIB,
        **common,
    )
    assert below_current_floor.admitted is False
    assert below_current_floor.reasons == ("gpu_reserve_below_floor",)
    assert below_current_floor.device_admission_bytes is None

    for reserve in (24 * GIB // 10, 4 * GIB):
        admitted = resource_policy.evaluate_admission(
            requirement,
            sample,
            gpu_priority_reserve_bytes=reserve,
            **common,
        )
        assert admitted.admitted is True


def test_gpu_stabilization_uses_three_exact_device_samples_five_seconds_apart():
    now = [0.0]
    sleeps = []
    samples = iter(
        [
            healthy_sample(
                observed_at=now[0],
                gpu_total_bytes=24 * GIB,
                gpu_free_bytes=15 * GIB,
                gpu_device_id="GPU-A",
                gpu_observed_at=0.0,
            ),
            healthy_sample(
                observed_at=5.0,
                gpu_total_bytes=24 * GIB,
                gpu_free_bytes=13 * GIB,
                gpu_device_id="GPU-A",
                gpu_observed_at=5.0,
            ),
            healthy_sample(
                observed_at=10.0,
                gpu_total_bytes=24 * GIB,
                gpu_free_bytes=14 * GIB,
                gpu_device_id="GPU-A",
                gpu_observed_at=10.0,
            ),
        ]
    )

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    stabilized = resource_policy.stabilize_gpu_capacity(
        lambda: next(samples),
        expected_device="GPU-A",
        clock=lambda: now[0],
        sleep=sleep,
    )

    assert stabilized.device_id == "GPU-A"
    assert stabilized.total_bytes == 24 * GIB
    assert stabilized.free_bytes == 13 * GIB
    assert stabilized.sample_count == 3
    assert sleeps == [5.0, 5.0]


@pytest.mark.parametrize(
    "bad_sample",
    [
        healthy_sample(gpu_total_bytes=None, gpu_free_bytes=None, gpu_device_id=None),
        healthy_sample(
            gpu_total_bytes=24 * GIB,
            gpu_free_bytes=12 * GIB,
            gpu_device_id="GPU-B",
            gpu_observed_at=0.0,
        ),
        healthy_sample(
            gpu_total_bytes=24 * GIB,
            gpu_free_bytes=25 * GIB,
            gpu_device_id="GPU-A",
            gpu_observed_at=0.0,
        ),
        healthy_sample(
            gpu_total_bytes=24 * GIB,
            gpu_free_bytes=12 * GIB,
            gpu_device_id="GPU-A",
            gpu_observed_at=-11.0,
        ),
    ],
)
def test_gpu_stabilization_rejects_missing_wrong_malformed_or_stale_telemetry(
    bad_sample,
):
    assert (
        resource_policy.stabilize_gpu_capacity(
            lambda: bad_sample,
            expected_device="GPU-A",
            clock=lambda: 0.0,
            sleep=lambda _delay: None,
        )
        is None
    )


@pytest.mark.parametrize(
    ("allocatable_gib", "expected"),
    [
        (1.999, "tiny"),
        (2, "base"),
        (2.999, "base"),
        (3, "small"),
        (6.999, "small"),
        (7, "medium"),
        (11.999, "medium"),
        (12, "large-v3"),
        (None, "small"),
    ],
)
def test_gpu_fallback_ceiling_uses_allocatable_vram_boundaries(
    allocatable_gib,
    expected,
):
    allocatable = None if allocatable_gib is None else int(allocatable_gib * GIB)
    assert (
        resource_policy.fallback_model_ceiling(
            profile(64),
            device="cuda",
            allocatable_vram_bytes=allocatable,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("model", "limit_gib", "current_gib"),
    [("small", 4, 1), ("medium", 9, 2)],
)
def test_auto_selection_preserves_exact_constrained_host_feasibility(
    model,
    limit_gib,
    current_gib,
):
    decision = resource_policy.select_model(
        "auto",
        profile(limit_gib),
        admission_sample=healthy_sample(
            host_available_bytes=16 * GIB,
            cgroup_limit_bytes=limit_gib * GIB,
            cgroup_current_bytes=current_gib * GIB,
        ),
        require_cgroup=True,
        now=0.0,
    )

    assert decision.selected_model == model
    assert decision.admitted is True
    assert decision.provenance == "fallback"


def test_exact_envelope_can_promote_large_v3_above_generic_fallback_ceiling(
    exact_envelope,
):
    stabilized = resource_policy.StabilizedGpuCapacity(
        device_id="GPU-A",
        total_bytes=24 * GIB,
        free_bytes=15 * GIB,
        observed_at=10.0,
        sample_count=3,
    )
    decision = resource_policy.select_model(
        "auto",
        profile(8),
        device="cuda",
        admission_sample=healthy_sample(
            observed_at=10.0,
            host_available_bytes=16 * GIB,
            cgroup_limit_bytes=10 * GIB,
            cgroup_current_bytes=GIB,
            gpu_total_bytes=24 * GIB,
            gpu_free_bytes=15 * GIB,
            gpu_device_id="GPU-A",
            gpu_observed_at=10.0,
        ),
        stabilized_gpu=stabilized,
        gpu_reserve_bytes=3 * GIB,
        expected_gpu_device="GPU-A",
        envelopes=(exact_resolution(exact_envelope),),
        require_cgroup=True,
        now=10.0,
    )

    assert decision.automatic_ceiling == "medium"
    assert decision.selected_model == "large-v3"
    assert decision.provenance == "envelope"
    assert decision.admitted is True


def test_public_cuda_exact_envelope_cannot_promote_without_three_sample_stabilization(
    exact_envelope,
):
    exact_small = make_envelope(
        model="small",
        host_incremental=MIB,
        cgroup_incremental=MIB,
        device_incremental=MIB,
        host_margin=MIB,
        device_margin=MIB,
    )
    decision = resource_policy.select_model(
        "auto",
        profile(32),
        device="cuda",
        admission_sample=healthy_sample(
            host_available_bytes=32 * GIB,
            host_total_bytes=32 * GIB,
            cgroup_limit_bytes=16 * GIB,
            cgroup_current_bytes=GIB,
            gpu_total_bytes=24 * GIB,
            gpu_free_bytes=20 * GIB,
            gpu_device_id="GPU-A",
            gpu_observed_at=0.0,
        ),
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        envelopes=(exact_resolution(exact_envelope), exact_resolution(exact_small)),
        require_cgroup=True,
        now=0.0,
    )

    assert decision.automatic_ceiling == "small"
    assert decision.selected_model == "small"
    assert decision.provenance == "fallback"
    assert decision.admitted is True
    assert "stabilized" in decision.warning.casefold()


def test_selection_fails_closed_on_conflicting_fresh_gpu_total_and_device_telemetry(
    exact_envelope,
):
    stable_eight_gib = resource_policy.StabilizedGpuCapacity(
        "GPU-A",
        8 * GIB,
        7 * GIB,
        0.0,
        3,
    )
    fresh_twenty_four_gib = healthy_sample(
        host_available_bytes=32 * GIB,
        host_total_bytes=32 * GIB,
        cgroup_current_bytes=None,
        cgroup_limit_bytes=None,
        gpu_total_bytes=24 * GIB,
        gpu_free_bytes=20 * GIB,
        gpu_device_id="GPU-A",
        gpu_observed_at=0.0,
    )

    with pytest.raises(ValueError, match="mandatory GPU reserve floor"):
        select_model(
            "auto",
            profile(32),
            device="cuda",
            admission_sample=fresh_twenty_four_gib,
            stabilized_gpu=stable_eight_gib,
            gpu_reserve_bytes=GIB,
            expected_gpu_device="GPU-A",
            now=0.0,
        )

    total_conflict = select_model(
        "auto",
        profile(32),
        device="cuda",
        admission_sample=fresh_twenty_four_gib,
        stabilized_gpu=stable_eight_gib,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        envelopes=(exact_resolution(exact_envelope),),
        now=0.0,
    )

    assert total_conflict.selected_model is None
    assert total_conflict.admitted is False
    assert total_conflict.reason == "no_safe_model"
    assert "conflicts" in total_conflict.warning.casefold()

    wrong_fresh_device = replace(
        fresh_twenty_four_gib,
        gpu_device_id="GPU-B",
    )
    wrong_device = select_model(
        "auto",
        profile(32),
        device="cuda",
        admission_sample=wrong_fresh_device,
        stabilized_gpu=resource_policy.StabilizedGpuCapacity(
            "GPU-A",
            24 * GIB,
            20 * GIB,
            0.0,
            3,
        ),
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-B",
        envelopes=(exact_resolution(exact_envelope),),
        now=0.0,
    )

    assert wrong_device.selected_model is None
    assert wrong_device.admitted is False
    assert wrong_device.reason == "no_safe_model"
    assert "conflicts" in wrong_device.warning.casefold()

    explicit = select_model(
        "large-v3",
        profile(32),
        device="cuda",
        admission_sample=fresh_twenty_four_gib,
        stabilized_gpu=stable_eight_gib,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        now=0.0,
    )

    assert explicit.selected_model == "large-v3"
    assert explicit.explicit is True
    assert explicit.admitted is False
    assert explicit.reason == "no_safe_model"
    assert explicit.requirement is not None
    assert "conflicts" in explicit.warning.casefold()


def test_auto_enumerates_large_v3_down_and_enters_no_safe_model_below_tiny(
    exact_envelope,
):
    oversized_large = make_envelope(
        cgroup_incremental=7 * GIB,
    )
    large_does_not_fit = resource_policy.select_model(
        "auto",
        profile(9),
        admission_sample=healthy_sample(
            host_available_bytes=16 * GIB,
            cgroup_limit_bytes=9 * GIB,
            cgroup_current_bytes=2 * GIB,
        ),
        envelopes=(exact_resolution(oversized_large),),
        require_cgroup=True,
        now=0.0,
    )
    none_fit = resource_policy.select_model(
        "auto",
        profile(1),
        admission_sample=healthy_sample(
            host_available_bytes=GIB,
            cgroup_limit_bytes=GIB,
            cgroup_current_bytes=0,
        ),
        require_cgroup=True,
        now=0.0,
    )

    assert large_does_not_fit.selected_model == "medium"
    assert large_does_not_fit.provenance == "fallback"
    assert none_fit.selected_model is None
    assert none_fit.admitted is False
    assert none_fit.reason == "no_safe_model"


def test_explicit_model_is_fixed_but_waits_when_its_admission_floor_does_not_fit():
    decision = resource_policy.select_model(
        "large-v3",
        profile(4),
        admission_sample=healthy_sample(
            host_available_bytes=4 * GIB,
            cgroup_limit_bytes=4 * GIB,
            cgroup_current_bytes=GIB,
        ),
        require_cgroup=True,
        now=0.0,
    )

    assert decision.selected_model == "large-v3"
    assert decision.explicit is True
    assert decision.admitted is False
    assert decision.reason == "insufficient_capacity"


def test_canonical_shared_cuda_auto_fails_closed_without_a_valid_exact_envelope():
    decision = resource_policy.select_model(
        "auto",
        profile(32),
        device="cuda",
        admission_sample=healthy_sample(
            host_available_bytes=32 * GIB,
            cgroup_limit_bytes=16 * GIB,
            cgroup_current_bytes=GIB,
            gpu_total_bytes=24 * GIB,
            gpu_free_bytes=20 * GIB,
            gpu_device_id="GPU-A",
            gpu_observed_at=10.0,
        ),
        stabilized_gpu=resource_policy.StabilizedGpuCapacity(
            "GPU-A", 24 * GIB, 20 * GIB, 10.0, 3
        ),
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        canonical_shared_cuda=True,
        require_cgroup=True,
        now=10.0,
    )

    assert decision.selected_model is None
    assert decision.admitted is False
    assert decision.reason == "no_safe_model"


def test_canonical_explicit_model_uses_fallback_budget_while_auto_stays_closed():
    stabilized = resource_policy.StabilizedGpuCapacity(
        "GPU-A",
        24 * GIB,
        20 * GIB,
        10.0,
        3,
    )
    sample = healthy_sample(
        observed_at=10.0,
        host_available_bytes=32 * GIB,
        host_total_bytes=32 * GIB,
        cgroup_limit_bytes=16 * GIB,
        cgroup_current_bytes=GIB,
        gpu_total_bytes=24 * GIB,
        gpu_free_bytes=20 * GIB,
        gpu_device_id="GPU-A",
        gpu_observed_at=10.0,
    )
    common = {
        "capacity": profile(32),
        "device": "cuda",
        "admission_sample": sample,
        "stabilized_gpu": stabilized,
        "gpu_reserve_bytes": 4 * GIB,
        "expected_gpu_device": "GPU-A",
        "canonical_shared_cuda": True,
        "require_cgroup": True,
        "now": 10.0,
    }

    explicit = select_model("large-v3", **common)
    automatic = select_model("auto", **common)

    assert explicit.selected_model == "large-v3"
    assert explicit.explicit is True
    assert explicit.admitted is True
    assert explicit.provenance == "fallback"
    assert explicit.requirement.envelope_resolution is None
    assert automatic.selected_model is None
    assert automatic.reason == "no_safe_model"


def test_select_model_does_not_use_the_sample_timestamp_as_decision_time():
    decision = select_model(
        "tiny",
        profile(8),
        admission_sample=healthy_sample(observed_at=123.0),
    )

    assert decision.selected_model == "tiny"
    assert decision.admitted is False
    assert decision.admission is not None
    assert "decision_time_unavailable" in decision.admission.reasons


def test_canonical_shared_cuda_select_enforces_reserve_device_and_exact_resolution():
    envelope = make_envelope(
        "small",
        host_incremental=2 * GIB,
        cgroup_incremental=2 * GIB,
        device_incremental=3 * GIB,
        host_margin=512 * MIB,
        device_margin=GIB,
    )
    stabilized = resource_policy.StabilizedGpuCapacity(
        "GPU-A", 24 * GIB, 20 * GIB, 10.0, 3
    )
    sample = gpu_sample(
        10.0,
        free_gib=20,
        host_available_bytes=16 * GIB,
        cgroup_limit_bytes=16 * GIB,
        cgroup_current_bytes=GIB,
    )
    common = {
        "device": "cuda",
        "admission_sample": sample,
        "stabilized_gpu": stabilized,
        "require_cgroup": True,
        "now": 10.0,
    }

    missing_reserve = select_model(
        "auto",
        profile(32),
        expected_gpu_device="GPU-A",
        envelopes=(exact_resolution(envelope),),
        canonical_shared_cuda=True,
        **common,
    )
    missing_device = select_model(
        "auto",
        profile(32),
        gpu_reserve_bytes=4 * GIB,
        envelopes=(exact_resolution(envelope),),
        canonical_shared_cuda=True,
        **common,
    )
    with pytest.raises(TypeError, match="exact EnvelopeResolution"):
        select_model(
            "auto",
            profile(32),
            gpu_reserve_bytes=4 * GIB,
            expected_gpu_device="GPU-A",
            envelopes=(envelope,),
            canonical_shared_cuda=True,
            **common,
        )
    admitted = select_model(
        "auto",
        profile(32),
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        envelopes=(exact_resolution(envelope),),
        canonical_shared_cuda=True,
        **common,
    )

    assert missing_reserve.reason == "no_safe_model"
    assert missing_device.reason == "no_safe_model"
    assert admitted.selected_model == "small"
    assert admitted.admitted is True
    assert admitted.requirement.exact_match is True

    for invalid in (0, -1, False, True):
        with pytest.raises(ValueError, match="positive"):
            select_model(
                "auto",
                profile(32),
                gpu_reserve_bytes=invalid,
                expected_gpu_device="GPU-A",
                envelopes=(exact_resolution(envelope),),
                canonical_shared_cuda=True,
                **common,
            )


@pytest.mark.parametrize(
    "stabilized",
    [
        None,
        resource_policy.StabilizedGpuCapacity("GPU-A", 24 * GIB, 20 * GIB, 0.0, 3),
    ],
)
def test_canonical_no_safe_retains_exact_candidates_until_gpu_telemetry_recovers(
    stabilized,
):
    now = [20.0]
    resolution = exact_resolution(
        make_envelope(
            "small",
            host_incremental=2 * GIB,
            cgroup_incremental=2 * GIB,
            device_incremental=3 * GIB,
            host_margin=512 * MIB,
            device_margin=GIB,
        )
    )
    decision = select_model(
        "auto",
        profile(32),
        device="cuda",
        admission_sample=gpu_sample(
            20.0,
            free_gib=20,
            host_available_bytes=16 * GIB,
            cgroup_limit_bytes=16 * GIB,
            cgroup_current_bytes=GIB,
        ),
        stabilized_gpu=stabilized,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        envelopes=(resolution,),
        canonical_shared_cuda=True,
        require_cgroup=True,
        now=20.0,
    )

    assert decision.selected_model is None
    assert decision.reason == "no_safe_model"
    assert len(decision.recovery_requirements) == 1
    assert decision.recovery_requirements[0].envelope_resolution is resolution

    controller = PressureController(
        reserve_bytes=GIB,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        canonical_shared_cuda=True,
        recovery_requirements=decision.recovery_requirements,
        require_cgroup=True,
        clock=lambda: now[0],
    )
    controller.enter_no_safe_model()
    for index, observed in enumerate((25.0, 30.0, 35.0), start=1):
        now[0] = observed
        state = controller.observe(
            gpu_sample(
                observed,
                free_gib=20,
                host_available_bytes=16 * GIB,
                cgroup_limit_bytes=16 * GIB,
                cgroup_current_bytes=GIB,
            )
        )
        assert state == ("normal" if index == 3 else "recovering")


def test_public_cuda_selection_waits_when_exact_device_identity_is_missing():
    decision = select_model(
        "auto",
        profile(16),
        device="cuda",
        admission_sample=healthy_sample(
            observed_at=20.0,
            host_available_bytes=8 * GIB,
            cgroup_limit_bytes=12 * GIB,
            cgroup_current_bytes=GIB,
        ),
        stabilized_gpu=None,
        expected_gpu_device=None,
        now=20.0,
    )

    assert decision.selected_model is None
    assert decision.admitted is False
    assert decision.reason == "no_safe_model"
    assert decision.recovery_requirements


def gpu_sample(now, *, free_gib=8, device="GPU-A", total_gib=24, **changes):
    return healthy_sample(
        observed_at=now,
        gpu_total_bytes=None if total_gib is None else total_gib * GIB,
        gpu_free_bytes=None if free_gib is None else free_gib * GIB,
        gpu_device_id=device,
        gpu_observed_at=now,
        **changes,
    )


def test_canonical_explicit_fallback_can_recover_and_reload_but_auto_cannot_inject_it():
    now = [10.0]
    explicit = select_model(
        "large-v3",
        profile(32),
        device="cuda",
        admission_sample=gpu_sample(
            10.0,
            free_gib=20,
            host_available_bytes=32 * GIB,
            host_total_bytes=32 * GIB,
            cgroup_limit_bytes=16 * GIB,
            cgroup_current_bytes=GIB,
        ),
        stabilized_gpu=resource_policy.StabilizedGpuCapacity(
            "GPU-A",
            24 * GIB,
            20 * GIB,
            10.0,
            3,
        ),
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        canonical_shared_cuda=True,
        require_cgroup=True,
        now=10.0,
    )

    def fresh_sample(observed_at):
        return gpu_sample(
            observed_at,
            free_gib=20,
            host_available_bytes=32 * GIB,
            host_total_bytes=32 * GIB,
            cgroup_limit_bytes=16 * GIB,
            cgroup_current_bytes=GIB,
        )

    controller = PressureController(
        lambda: fresh_sample(now[0]),
        reserve_bytes=GIB,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        canonical_shared_cuda=True,
        explicit_model_authority=explicit.explicit,
        selected_requirement=explicit.requirement,
        require_cgroup=True,
        clock=lambda: now[0],
    )
    controller.enter_no_safe_model()
    for index, observed_at in enumerate((10.0, 15.0, 20.0), start=1):
        now[0] = observed_at
        state = controller.observe(fresh_sample(observed_at))
        assert state == ("normal" if index == 3 else "recovering")

    assert controller.immediate_load_admission().admitted is True

    fallback = resource_policy.model_load_requirement("small")
    with pytest.raises(ValueError, match="explicit model authority"):
        PressureController(
            reserve_bytes=GIB,
            gpu_reserve_bytes=4 * GIB,
            expected_gpu_device="GPU-A",
            canonical_shared_cuda=True,
            selected_requirement=fallback,
        )
    with pytest.raises(ValueError, match="fixed selected requirement"):
        PressureController(
            reserve_bytes=GIB,
            gpu_reserve_bytes=4 * GIB,
            expected_gpu_device="GPU-A",
            canonical_shared_cuda=True,
            explicit_model_authority=True,
        )


def test_pressure_controller_rejects_reserves_below_the_absolute_gpu_floor():
    for reserve in (1, GIB - 1):
        with pytest.raises(ValueError, match="mandatory GPU reserve floor"):
            PressureController(
                gpu_reserve_bytes=reserve,
                expected_gpu_device="GPU-A",
            )

    controller = PressureController(
        gpu_reserve_bytes=GIB,
        expected_gpu_device="GPU-A",
    )
    assert controller.gpu_reserve_bytes == GIB


def test_pressure_controller_fails_closed_when_observed_vram_raises_the_floor():
    now = [0.0]
    controller = PressureController(
        reserve_bytes=GIB,
        gpu_reserve_bytes=GIB,
        expected_gpu_device="GPU-A",
        canonical_shared_cuda=True,
        clock=lambda: now[0],
    )

    assert controller.observe(gpu_sample(0.0, free_gib=6, total_gib=8)) == "normal"
    assert controller.admission_open is True

    now[0] = 5.0
    assert controller.observe(gpu_sample(5.0, free_gib=20, total_gib=24)) == (
        "yielding"
    )
    assert controller.admission_open is False
    assert controller.last_critical_reasons == ("gpu_reserve_below_floor",)


def test_recovery_and_immediate_load_keep_a_new_higher_gpu_floor_fail_closed():
    now = [0.0]
    requirement = resource_policy.model_load_requirement("tiny")
    recovering = PressureController(
        reserve_bytes=GIB,
        gpu_reserve_bytes=GIB,
        expected_gpu_device="GPU-A",
        selected_requirement=requirement,
        clock=lambda: now[0],
    )
    recovering.enter_no_safe_model()

    assert recovering.observe(gpu_sample(0.0, free_gib=20, total_gib=24)) == (
        "recovering"
    )
    assert recovering.healthy_recovery_samples == 0
    assert recovering.admission_open is False

    immediate = PressureController(
        lambda: gpu_sample(0.0, free_gib=20, total_gib=24),
        reserve_bytes=GIB,
        gpu_reserve_bytes=GIB,
        expected_gpu_device="GPU-A",
        selected_requirement=requirement,
        clock=lambda: now[0],
    )
    decision = immediate.immediate_load_admission()
    assert decision.admitted is False
    assert decision.reasons == ("gpu_reserve_below_floor",)
    assert immediate.state == "recovering"
    assert immediate.admission_open is False


def test_gpu_resident_floor_is_sustained_and_half_floor_is_immediately_critical():
    now = [0.0]
    controller = PressureController(
        reserve_bytes=GIB,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        clock=lambda: now[0],
    )

    assert controller.observe(gpu_sample(0.0, free_gib=3)) == "normal"
    now[0] = 5.0
    assert controller.observe(gpu_sample(5.0, free_gib=3)) == "yielding"

    critical = PressureController(
        reserve_bytes=GIB,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        clock=lambda: 0.0,
    )
    assert critical.observe(gpu_sample(0.0, free_gib=1)) == "yielding"
    assert "critical_gpu_headroom" in critical.last_critical_reasons


def test_shared_cuda_one_missing_sample_closes_admission_and_two_request_unload():
    now = [0.0]
    missing = healthy_sample(observed_at=0.0)
    controller = PressureController(
        reserve_bytes=GIB,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        canonical_shared_cuda=True,
        clock=lambda: now[0],
    )

    assert controller.observe(missing) == "normal"
    assert controller.admission_open is False
    now[0] = 5.0
    assert controller.observe(replace(missing, observed_at=5.0)) == "yielding"
    assert controller.recovery_reason == "gpu_telemetry_unavailable"


def test_idle_resident_poll_applies_the_same_two_sample_fail_closed_rule():
    now = [0.0]
    reads = []

    def read():
        reads.append(now[0])
        return healthy_sample(observed_at=now[0])

    controller = PressureController(
        read,
        reserve_bytes=GIB,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        canonical_shared_cuda=True,
        clock=lambda: now[0],
    )

    assert controller.poll_idle_resident() is False
    now[0] = 5.0
    assert controller.poll_idle_resident() is True
    assert reads == [0.0, 5.0]


def test_shared_cuda_recovery_requires_three_fresh_selected_model_qualified_samples():
    now = [0.0]
    requirement = resource_policy.model_load_requirement(
        "small",
        resolution=exact_resolution(
            make_envelope(
                "small",
                host_incremental=2 * GIB,
                cgroup_incremental=2 * GIB,
                device_incremental=3 * GIB,
                host_margin=512 * MIB,
                device_margin=GIB,
            )
        ),
    )
    controller = PressureController(
        reserve_bytes=GIB,
        gpu_reserve_bytes=3 * GIB,
        expected_gpu_device="GPU-A",
        canonical_shared_cuda=True,
        selected_requirement=requirement,
        require_cgroup=True,
        clock=lambda: now[0],
    )
    controller.enter_no_safe_model()
    assert controller.recovery_reason == "no_safe_model"

    # Fresh and above the resident floor, but only 3 GiB is allocatable while
    # the selected small model requires 4 GiB including its device margin.
    assert controller.observe(gpu_sample(0.0, free_gib=6)) == "recovering"
    assert controller.healthy_recovery_samples == 0
    for index, observed in enumerate((5.0, 10.0, 15.0), start=1):
        now[0] = observed
        state = controller.observe(gpu_sample(observed, free_gib=7))
        assert state == ("normal" if index == 3 else "recovering")


def test_immediate_load_admission_bypasses_throttle_and_closes_on_capacity_drop():
    now = [10.0]
    requirement = resource_policy.model_load_requirement(
        "small",
        resolution=exact_resolution(
            make_envelope(
                "small",
                host_incremental=2 * GIB,
                cgroup_incremental=2 * GIB,
                device_incremental=3 * GIB,
                host_margin=512 * MIB,
                device_margin=GIB,
            )
        ),
    )
    samples = iter(
        [
            gpu_sample(10.0, free_gib=8),
            gpu_sample(10.0, free_gib=8, host_available_bytes=2 * GIB),
        ]
    )
    reads = []
    controller = PressureController(
        lambda: reads.append(True) or next(samples),
        reserve_bytes=GIB,
        gpu_reserve_bytes=3 * GIB,
        expected_gpu_device="GPU-A",
        canonical_shared_cuda=True,
        selected_requirement=requirement,
        require_cgroup=True,
        clock=lambda: now[0],
    )

    assert controller.immediate_load_admission().admitted is True
    rejected = controller.immediate_load_admission()
    assert rejected.admitted is False
    assert rejected.reasons == ("insufficient_host",)
    assert controller.state == "recovering"
    assert controller.admission_open is False
    assert len(reads) == 2

    blocked_while_recovering = controller.immediate_load_admission()
    assert blocked_while_recovering.admitted is False
    assert blocked_while_recovering.reasons == ("controller_recovering",)
    assert len(reads) == 2


def test_immediate_load_admission_is_closed_while_controller_is_yielding():
    reads = []
    requirement = resource_policy.model_load_requirement("tiny")
    controller = PressureController(
        lambda: reads.append(True) or healthy_sample(),
        reserve_bytes=GIB,
        selected_requirement=requirement,
    )
    controller.observe(healthy_sample(cgroup_current_bytes=8 * GIB - 300 * MIB))

    decision = controller.immediate_load_admission()

    assert controller.state == "yielding"
    assert decision.admitted is False
    assert decision.reasons == ("controller_yielding",)
    assert reads == []


def test_no_safe_model_enters_recovery_without_a_load_attempt():
    controller = PressureController(reserve_bytes=GIB)

    controller.enter_no_safe_model()

    assert controller.state == "recovering"
    assert controller.recovery_reason == "no_safe_model"
    assert controller.admission_open is False
