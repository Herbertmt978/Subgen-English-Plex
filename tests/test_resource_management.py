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
from subgen_core.priority_pressure import PriorityObservation
from subgen_core.resource_management import (
    AdaptiveChunkState,
    CapacityProfile,
    GIB,
    MIB,
    MemoryPressureYield,
    ModelDecision,
    PressureController,
    PressureSample,
    automatic_host_reserve_bytes,
    automatic_subgen_memory_limit_bytes,
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


def priority_observation(
    state="clear",
    *,
    sequence=1,
    source_generation=1,
    new_publication=True,
    epoch_changed=False,
):
    accepted = state != "unavailable"
    return PriorityObservation(
        state=state,
        configured=True,
        heartbeat_age_ms=100 if accepted else None,
        source_age_ms=200 if accepted else None,
        policy_sha256="1" * 64 if accepted else None,
        observation_digest=f"{sequence:064x}" if accepted else None,
        producer_epoch="2" * 32 if accepted else None,
        sequence=sequence if accepted else None,
        observed_monotonic_ns=1 if accepted else None,
        source_generation=source_generation if accepted else None,
        source_observed_monotonic_ns=1 if accepted else None,
        reason_codes=("higher_priority_busy",) if state == "asserted" else (),
        accepted=accepted,
        new_publication=new_publication if accepted else False,
        producer_epoch_changed=epoch_changed if accepted else False,
    )


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


@pytest.mark.parametrize("version", [1, 2])
def test_discover_capacity_clamps_effective_cgroup_to_physical_memory(version):
    values = (
        {"/v2/max": str(128 * GIB)} if version == 2 else {"/v1/max": str(128 * GIB)}
    )
    discovered = discover_capacity(
        read_text=mapping_reader(values),
        physical_memory_reader=lambda: 64 * GIB,
        cgroup_v2_path="/v2/max",
        cgroup_v1_paths=("/v1/max",),
    )

    assert discovered.effective_bytes == 64 * GIB
    assert discovered.cgroup_limit_bytes == 128 * GIB
    assert "clamped" in discovered.warning


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


@pytest.mark.parametrize(
    ("host_gib", "limit_mib", "expected_seconds"),
    [
        (4, 3072, 5 * 60),
        (6, 5120, 10 * 60),
        (9, 7680, 10 * 60),
        (12, 10240, 20 * 60),
        (16, 13824, 20 * 60),
        (24, 20736, 30 * 60),
        (32, 24576, 30 * 60),
        (64, 24576, 30 * 60),
        (128, 24576, 30 * 60),
    ],
)
def test_hardware_matrix_drives_chunks_from_effective_cgroup(
    host_gib, limit_mib, expected_seconds
):
    capacity = CapacityProfile(
        limit_mib * MIB,
        host_gib * GIB,
        limit_mib * MIB,
        "cgroup_v2",
        2,
    )

    assert initial_chunk_seconds(capacity) == expected_seconds


@pytest.mark.parametrize("minutes", [5, 60])
def test_manual_chunk_boundaries_are_accepted(minutes):
    assert initial_chunk_seconds(profile(1), minutes) == minutes * 60


@pytest.mark.parametrize("invalid", [4, 61, 10.0, "10", True])
def test_manual_chunk_must_be_an_integer_from_five_to_sixty(invalid):
    with pytest.raises(ValueError):
        initial_chunk_seconds(profile(16), invalid)


def test_host_reserve_applies_host_minimum_capacity_cap_and_unknown_fallback():
    assert host_reserve_bytes(32 * GIB, 32 * GIB) == 5 * GIB
    assert host_reserve_bytes(16 * GIB, 2 * GIB) == int(2.5 * GIB)
    assert host_reserve_bytes(None, None) == GIB
    assert host_reserve_bytes(4 * GIB, None) == GIB


def test_explicit_reserve_replaces_host_reserve_only():
    assert host_reserve_bytes(profile(8), explicit_reserve_gib=1.5) == int(1.5 * GIB)
    assert host_reserve_bytes(profile(8), explicit_reserve_gib=0.5) == int(1.25 * GIB)


@pytest.mark.parametrize(
    ("host_gib", "reserve_mib", "limit_mib"),
    [
        (4, 1024, 3072),
        (6, 1024, 5120),
        (9, 1536, 7680),
        (12, 2048, 10240),
        (16, 2560, 13824),
        (24, 3840, 20736),
        (32, 5120, 24576),
        (64, 9984, 24576),
        (128, 19712, 24576),
    ],
)
def test_hardware_matrix_reserve_and_automatic_limit(host_gib, reserve_mib, limit_mib):
    assert automatic_host_reserve_bytes(host_gib * GIB) == reserve_mib * MIB
    assert automatic_subgen_memory_limit_bytes(host_gib * GIB) == limit_mib * MIB


def test_twelve_gib_profile_uses_one_shared_ten_gib_cgroup_budget():
    capacity = CapacityProfile(
        10 * GIB,
        12 * GIB,
        10 * GIB,
        "cgroup_v2",
        2,
    )
    current_at_medium_boundary = 7 * GIB // 2
    sample = healthy_sample(
        host_available_bytes=12 * GIB,
        host_total_bytes=12 * GIB,
        cgroup_limit_bytes=10 * GIB,
        cgroup_current_bytes=current_at_medium_boundary,
    )

    decision = resource_policy.select_model(
        "auto",
        capacity,
        admission_sample=sample,
        require_cgroup=True,
        now=0.0,
    )

    assert host_reserve_bytes(capacity) == 2 * GIB
    assert cgroup_headroom_floor(10 * GIB) == GIB
    assert initial_chunk_seconds(capacity) == 20 * 60
    assert decision.selected_model == "medium"
    assert decision.requirement is not None
    assert decision.requirement.required_host_bytes == 11 * GIB // 2
    assert decision.admission is not None
    assert decision.admission.cgroup_admission_bytes == 11 * GIB // 2
    assert decision.admission.effective_host_admission_bytes == 11 * GIB // 2

    one_byte_over = replace(
        sample,
        cgroup_current_bytes=current_at_medium_boundary + 1,
    )
    medium_denial = resource_policy.evaluate_admission(
        decision.requirement,
        one_byte_over,
        host_reserve_bytes=2 * GIB,
        require_cgroup=True,
        now=0.0,
    )
    fallback = resource_policy.select_model(
        "auto",
        capacity,
        admission_sample=one_byte_over,
        require_cgroup=True,
        now=0.0,
    )

    assert medium_denial.cgroup_admission_bytes == 11 * GIB // 2 - 1
    assert medium_denial.admitted is False
    assert medium_denial.reasons == ("insufficient_host",)
    assert fallback.selected_model == "small"


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
    pressured = healthy_sample(psi_some_avg10=10.0)
    controller = PressureController(
        lambda: reads.append(now[0]) or replace(pressured, observed_at=now[0]),
        reserve_bytes=GIB,
        clock=lambda: now[0],
        sleep=lambda _delay: None,
    )

    assert controller.poll() == "normal"
    now[0] = 0.99
    assert controller.poll() == "normal"
    assert reads == [0.0]
    now[0] = 1.0
    assert controller.poll() == "yielding"
    assert reads == [0.0, 1.0]


def test_poll_token_cannot_make_a_nonadvancing_observation_distinct():
    now = [0.0]
    observed_at = [0.0]
    reads = []

    def read():
        reads.append(now[0])
        return healthy_sample(
            observed_at=observed_at[0],
            psi_some_avg10=10.0,
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
        healthy_sample(cgroup_current_bytes=8 * GIB - 600 * MIB),
        healthy_sample(psi_full_avg10=1.0),
        healthy_sample(psi_some_avg10=10.0),
    ],
)
def test_two_consecutive_pressure_samples_enter_yielding(pressured):
    controller = PressureController(reserve_bytes=GIB)

    assert controller.observe(pressured) == "normal"
    assert controller.observe(replace(pressured, observed_at=5.0)) == "yielding"


def test_host_reserve_crossing_yields_immediately():
    controller = PressureController(reserve_bytes=GIB)

    assert (
        controller.observe(healthy_sample(host_available_bytes=GIB - 1)) == "yielding"
    )
    assert controller.last_critical_reasons == ("host_headroom",)


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
    pressured = healthy_sample(psi_some_avg10=10.0)
    controller = PressureController(reserve_bytes=GIB)

    assert controller.observe(pressured) == "normal"
    assert controller.observe(pressured) == "normal"
    assert controller.observe(replace(pressured, observed_at=5.0)) == "yielding"


def test_healthy_sample_breaks_sustained_pressure_sequence():
    controller = PressureController(reserve_bytes=GIB)
    pressured = healthy_sample(psi_some_avg10=10.0)

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
    sample = healthy_sample(psi_some_avg10=10.0)
    controller = PressureController(
        lambda: replace(sample, observed_at=now[0]),
        reserve_bytes=GIB,
        clock=lambda: now[0],
    )

    assert controller.check_or_raise() == "normal"
    now[0] = 1.0
    with pytest.raises(MemoryPressureYield, match="psi_some"):
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
            psi_some_avg10=10.0,
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
    now[0] = 1.0
    assert poll_batch() == ["yielding"] * 8
    assert reads == [0.0, 1.0]


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


@pytest.mark.parametrize(
    ("host_gib", "limit_mib", "expected_model"),
    [
        (4, 3072, "small"),
        (6, 5120, "small"),
        (9, 7680, "medium"),
        (12, 10240, "medium"),
        (16, 13824, "large-v3"),
        (24, 20736, "large-v3"),
        (32, 24576, "large-v3"),
        (64, 24576, "large-v3"),
        (128, 24576, "large-v3"),
    ],
)
def test_hardware_matrix_reports_gross_zero_current_model_candidate(
    host_gib, limit_mib, expected_model
):
    capacity = CapacityProfile(
        limit_mib * MIB,
        host_gib * GIB,
        limit_mib * MIB,
        "cgroup_v2",
        2,
    )
    decision = resource_policy.select_model(
        "auto",
        capacity,
        admission_sample=healthy_sample(
            host_available_bytes=host_gib * GIB,
            host_total_bytes=host_gib * GIB,
            cgroup_limit_bytes=limit_mib * MIB,
            cgroup_current_bytes=0,
        ),
        require_cgroup=True,
        now=0.0,
    )

    assert decision.automatic_ceiling == expected_model
    assert decision.selected_model == expected_model
    assert decision.admitted is True


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


def _priority_controller(*, now, priority_reader=None, recovery_sample_count=3):
    requirement = resource_policy.model_load_requirement("tiny")
    return PressureController(
        lambda: healthy_sample(observed_at=now[0]),
        reserve_bytes=GIB,
        selected_requirement=requirement,
        recovery_requirements=(requirement,),
        priority_reader=priority_reader or (lambda: priority_observation()),
        clock=lambda: now[0],
        recovery_sample_count=recovery_sample_count,
    )


def _priority_sample(now, observation):
    return healthy_sample(
        observed_at=now,
        priority_observation=observation,
    )


def test_configured_priority_starts_unavailable_recovering_and_admission_closed():
    now = [0.0]
    controller = _priority_controller(now=now)

    snapshot = controller.runtime_status_snapshot(
        {
            "model_resident": False,
            "model_load_generation": 0,
            "model_unload_generation": 0,
        }
    )

    assert controller.state == "recovering"
    assert controller.admission_open is False
    assert snapshot["priority_pressure"] == {
        "configured": True,
        "state": "unavailable",
        "heartbeat_age_ms": None,
        "source_age_ms": None,
        "policy_sha256": None,
        "observation_digest": None,
        "transition_observation_digest": None,
        "transition_sequence": 0,
        "controller_phase": "recovering",
        "recovery_reason": "priority_pressure",
        "distinct_clear_count": 0,
        "model_resident": False,
        "model_load_generation": 0,
        "model_unload_generation": 0,
    }


def test_priority_recovery_requires_new_epoch_barrier_three_distinct_clears_and_resources():
    now = [0.0]
    controller = _priority_controller(now=now)

    first = priority_observation(epoch_changed=True)
    assert (
        controller.observe(_priority_sample(0.0, first), model_resident=True)
        == "yielding"
    )
    controller.mark_released("priority_pressure")

    publications = ((2, 1), (3, 2), (4, 3), (5, 4))
    expected_counts = (0, 1, 2, 3)
    for (sequence, source_generation), expected_count in zip(
        publications, expected_counts
    ):
        now[0] = float(sequence * 5)
        state = controller.observe(
            _priority_sample(
                now[0],
                priority_observation(
                    sequence=sequence,
                    source_generation=source_generation,
                ),
            ),
            model_resident=False,
        )
        assert state == ("normal" if expected_count == 3 else "recovering")
        status = controller.priority_status_snapshot(
            {
                "model_resident": False,
                "model_load_generation": 1,
                "model_unload_generation": 1,
            }
        )
        assert status["distinct_clear_count"] == expected_count

    status = controller.priority_status_snapshot(
        {
            "model_resident": False,
            "model_load_generation": 1,
            "model_unload_generation": 1,
        }
    )
    assert status["distinct_clear_count"] == 3
    assert controller.admission_open is True


def test_external_pressure_recovery_generation_counts_only_complete_critical_episode():
    now = [0.0]
    controller = _priority_controller(now=now, recovery_sample_count=1)
    assert controller.external_pressure_recovery_generation == 0

    for sequence in (1, 2, 3):
        now[0] += 5.0
        controller.observe(
            _priority_sample(
                now[0],
                priority_observation(
                    sequence=sequence,
                    source_generation=sequence,
                ),
            ),
            model_resident=False,
        )
    assert controller.state == "normal"
    assert controller.external_pressure_recovery_generation == 0

    now[0] += 5.0
    controller.observe(
        _priority_sample(
            now[0],
            priority_observation(
                state="asserted",
                sequence=4,
                source_generation=4,
            ),
        ),
        model_resident=False,
    )
    assert controller.external_pressure_recovery_generation == 0

    for sequence in (5, 6, 7):
        now[0] += 5.0
        controller.observe(
            _priority_sample(
                now[0],
                priority_observation(
                    sequence=sequence,
                    source_generation=sequence,
                ),
            ),
            model_resident=False,
        )
    assert controller.state == "normal"
    assert controller.external_pressure_recovery_generation == 1

    now[0] += 5.0
    controller.observe(
        _priority_sample(now[0], priority_observation(state="unavailable")),
        model_resident=False,
    )
    for sequence in (8, 9, 10):
        now[0] += 5.0
        controller.observe(
            _priority_sample(
                now[0],
                priority_observation(
                    sequence=sequence,
                    source_generation=sequence,
                ),
            ),
            model_resident=False,
        )
    assert controller.state == "normal"
    assert controller.external_pressure_recovery_generation == 2

    controller.enter_no_safe_model()
    now[0] += 5.0
    controller.observe(
        _priority_sample(
            now[0],
            priority_observation(
                sequence=11,
                source_generation=11,
                new_publication=False,
            ),
        ),
        model_resident=False,
    )
    assert controller.external_pressure_recovery_generation == 2


def test_priority_asserted_generation_is_a_recovery_floor_until_three_new_generations():
    now = [0.0]
    controller = _priority_controller(now=now, recovery_sample_count=1)
    controller.observe(
        _priority_sample(
            0.0,
            priority_observation(
                state="asserted",
                sequence=1,
                source_generation=10,
                epoch_changed=True,
            ),
        ),
        model_resident=False,
    )

    publications = ((2, 10), (3, 11), (4, 12), (5, 13))
    expected_counts = (0, 1, 2, 3)
    for (sequence, source_generation), expected_count in zip(
        publications, expected_counts
    ):
        now[0] += 5.0
        controller.observe(
            _priority_sample(
                now[0],
                priority_observation(
                    sequence=sequence,
                    source_generation=source_generation,
                ),
            ),
            model_resident=False,
        )
        status = controller.priority_status_snapshot(
            {
                "model_resident": False,
                "model_load_generation": 0,
                "model_unload_generation": 0,
            }
        )
        assert status["distinct_clear_count"] == expected_count

    assert controller.state == "normal"


def test_priority_duplicate_clear_generation_does_not_advance_recovery():
    now = [0.0]
    controller = _priority_controller(now=now, recovery_sample_count=1)
    controller.observe(
        _priority_sample(0.0, priority_observation(epoch_changed=True)),
        model_resident=False,
    )

    publications = ((2, 2), (3, 2), (4, 3), (5, 4))
    expected_counts = (1, 1, 2, 3)
    for (sequence, source_generation), expected in zip(publications, expected_counts):
        now[0] += 5.0
        controller.observe(
            _priority_sample(
                now[0],
                priority_observation(
                    sequence=sequence,
                    source_generation=source_generation,
                ),
            ),
            model_resident=False,
        )
        status = controller.priority_status_snapshot(
            {
                "model_resident": False,
                "model_load_generation": 0,
                "model_unload_generation": 0,
            }
        )
        assert status["distinct_clear_count"] == expected

    assert controller.state == "normal"


def test_priority_assertion_yields_resident_but_neutral_only_enters_recovery():
    now = [0.0]
    asserted = _priority_controller(now=now)
    assert (
        asserted.observe(
            _priority_sample(
                0.0,
                priority_observation(state="asserted", epoch_changed=True),
            ),
            model_resident=True,
        )
        == "yielding"
    )
    assert asserted.admission_open is False

    neutral = _priority_controller(
        now=now,
        recovery_sample_count=1,
    )
    neutral.observe(
        _priority_sample(0.0, priority_observation(epoch_changed=True)),
        model_resident=False,
    )
    for sequence in (2, 3, 4):
        now[0] += 5.0
        neutral.observe(
            _priority_sample(
                now[0],
                priority_observation(
                    sequence=sequence,
                    source_generation=sequence,
                ),
            ),
            model_resident=False,
        )
    assert neutral.state == "normal"

    now[0] += 5.0
    assert (
        neutral.observe(
            _priority_sample(
                now[0],
                priority_observation(
                    state="neutral",
                    sequence=5,
                    source_generation=5,
                ),
            ),
            model_resident=True,
        )
        == "recovering"
    )
    assert neutral.should_yield is False
    assert neutral.admission_open is False


def test_priority_poll_cadence_is_independent_of_generic_sample_cache():
    now = [0.0]
    generic_reads = []
    priority_reads = []

    def read_sample():
        generic_reads.append(now[0])
        return healthy_sample(observed_at=now[0])

    def read_priority():
        priority_reads.append(now[0])
        return priority_observation(
            sequence=len(priority_reads),
            source_generation=len(priority_reads),
            epoch_changed=len(priority_reads) == 1,
        )

    controller = PressureController(
        read_sample,
        reserve_bytes=GIB,
        selected_requirement=resource_policy.model_load_requirement("tiny"),
        priority_reader=read_priority,
        clock=lambda: now[0],
    )

    for observed_at in (0.0, 0.5, 1.0, 2.0, 4.9, 5.0, 5.9):
        now[0] = observed_at
        controller.poll(model_resident=False)

    assert generic_reads == [0.0, 1.0, 2.0, 4.9, 5.9]
    assert priority_reads == [0.0, 1.0, 2.0, 4.9, 5.9]
    assert controller.poll_interval_seconds == 1.0


def test_forced_priority_check_bypasses_the_one_second_poll_cache():
    now = [10.0]
    observations = [
        priority_observation("clear", sequence=1, source_generation=1),
        priority_observation("asserted", sequence=2, source_generation=2),
    ]
    reads = []

    def read_priority():
        reads.append(now[0])
        return observations.pop(0)

    controller = _priority_controller(now=now, priority_reader=read_priority)

    assert controller.poll_priority(model_resident=True, force=True) == "recovering"
    with pytest.raises(MemoryPressureYield):
        controller.check_or_raise(force_priority=True)

    assert reads == [10.0, 10.0]


def test_priority_observer_receives_exact_gate_snapshot_after_observation():
    now = [0.0]
    snapshots = []
    controller = PressureController(
        lambda: healthy_sample(observed_at=now[0]),
        reserve_bytes=GIB,
        priority_reader=lambda: priority_observation(
            state="asserted",
            source_generation=7,
            epoch_changed=True,
        ),
        priority_observer=snapshots.append,
        priority_transition_lock=threading.RLock(),
        clock=lambda: now[0],
    )

    assert controller.poll_priority(model_resident=True) == "yielding"
    assert snapshots == [
        {
            "configured": True,
            "state": "asserted",
            "heartbeat_age_ms": 100,
            "source_age_ms": 200,
            "policy_sha256": "1" * 64,
            "observation_digest": f"{1:064x}",
            "transition_observation_digest": f"{1:064x}",
            "transition_sequence": 1,
            "controller_phase": "yielding",
            "recovery_reason": "priority_pressure",
            "distinct_clear_count": 0,
            "source_generation": 7,
            "admission_open": False,
        }
    ]


def test_priority_observer_must_be_callable():
    with pytest.raises(TypeError, match="priority_observer"):
        PressureController(priority_observer="not-callable")


def test_priority_observer_and_transition_lock_are_one_gate_contract():
    with pytest.raises(ValueError, match="configured together"):
        PressureController(priority_observer=lambda _snapshot: None)
    with pytest.raises(ValueError, match="configured together"):
        PressureController(priority_transition_lock=threading.RLock())


def test_priority_transition_is_not_visible_until_receipt_observer_returns():
    observer_entered = threading.Event()
    allow_receipt = threading.Event()
    reader_started = threading.Event()
    reader_finished = threading.Event()
    observed_states = []

    def observer(_snapshot):
        observer_entered.set()
        assert allow_receipt.wait(timeout=1.0)

    controller = PressureController(
        reserve_bytes=GIB,
        priority_reader=lambda: priority_observation(
            state="asserted",
            source_generation=7,
            epoch_changed=True,
        ),
        priority_observer=observer,
        priority_transition_lock=threading.RLock(),
        clock=lambda: 0.0,
    )
    polling = threading.Thread(
        target=controller.poll_priority,
        kwargs={"model_resident": True},
    )
    polling.start()
    assert observer_entered.wait(timeout=1.0)

    def read_state():
        reader_started.set()
        observed_states.append(controller.state)
        reader_finished.set()

    reading = threading.Thread(target=read_state)
    reading.start()
    assert reader_started.wait(timeout=1.0)
    assert not reader_finished.wait(timeout=0.05)

    allow_receipt.set()
    polling.join(timeout=1.0)
    reading.join(timeout=1.0)
    assert not polling.is_alive()
    assert not reading.is_alive()
    assert observed_states == ["yielding"]


def test_priority_receipt_failure_latches_controller_fail_closed():
    fail_receipt = [False]

    def observer(_snapshot):
        if fail_receipt[0]:
            raise OSError("receipt fsync failed")

    controller = PressureController(
        reserve_bytes=GIB,
        priority_observer=observer,
        priority_transition_lock=threading.RLock(),
    )
    controller.close_admission()
    fail_receipt[0] = True

    with pytest.raises(OSError, match="receipt fsync failed"):
        controller.open_admission_if_normal()

    assert controller.state == "recovering"
    assert controller.admission_open is False
    assert controller.recovery_reason == "receipt_unavailable"


def test_due_priority_assertion_is_consumed_before_any_generic_probe():
    generic_reads = []
    controller = PressureController(
        lambda: generic_reads.append(True) or healthy_sample(),
        reserve_bytes=GIB,
        priority_reader=lambda: priority_observation(
            state="asserted",
            epoch_changed=True,
        ),
        clock=lambda: 0.0,
    )

    assert controller.poll(model_resident=True) == "yielding"
    assert controller.admission_open is False
    assert generic_reads == []


def test_canonical_admission_does_not_reopen_after_one_missing_gpu_sample():
    controller = PressureController(
        reserve_bytes=GIB,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        canonical_shared_cuda=True,
        clock=lambda: 0.0,
    )

    assert controller.observe(healthy_sample(), model_resident=False) == "normal"
    assert controller.open_admission_if_normal() is False
    assert controller.admission_open is False


def test_canonical_admission_does_not_reopen_from_stale_gpu_telemetry():
    now = [0.0]
    controller = PressureController(
        reserve_bytes=GIB,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device="GPU-A",
        canonical_shared_cuda=True,
        clock=lambda: now[0],
    )
    sample = gpu_sample(0.0, free_gib=8)

    assert controller.observe(sample, model_resident=False) == "normal"
    assert controller.admission_open is True
    controller.close_admission()
    now[0] = 20.0

    assert controller.open_admission_if_normal() is False
    assert controller.admission_open is False


def test_immediate_load_consumes_embedded_priority_once_and_fails_closed():
    now = [0.0]
    priority_reads = []
    controller = _priority_controller(
        now=now,
        priority_reader=lambda: priority_reads.append(True) or priority_observation(),
        recovery_sample_count=1,
    )
    controller.observe(
        _priority_sample(0.0, priority_observation(epoch_changed=True)),
        model_resident=False,
    )
    for sequence in (2, 3, 4):
        now[0] += 5.0
        controller.observe(
            _priority_sample(
                now[0],
                priority_observation(
                    sequence=sequence,
                    source_generation=sequence,
                ),
            ),
            model_resident=False,
        )
    assert controller.state == "normal"

    sample_reads = []
    decision = controller.immediate_load_admission(
        sample_reader=lambda: (
            sample_reads.append(True)
            or _priority_sample(
                now[0],
                priority_observation(
                    state="asserted",
                    sequence=5,
                    source_generation=5,
                ),
            )
        )
    )

    assert decision.admitted is False
    assert decision.reasons == ("controller_recovering",)
    assert sample_reads == [True]
    assert priority_reads == []
    assert controller.admission_open is False


def test_immediate_load_reads_priority_directly_once_when_sample_has_no_observation():
    now = [0.0]
    priority_reads = []
    controller = _priority_controller(
        now=now,
        priority_reader=lambda: (
            priority_reads.append(True)
            or priority_observation(
                state="asserted",
                sequence=5,
                source_generation=5,
            )
        ),
        recovery_sample_count=1,
    )
    controller.observe(
        _priority_sample(0.0, priority_observation(epoch_changed=True)),
        model_resident=False,
    )
    for sequence in (2, 3, 4):
        now[0] += 5.0
        controller.observe(
            _priority_sample(
                now[0],
                priority_observation(
                    sequence=sequence,
                    source_generation=sequence,
                ),
            ),
            model_resident=False,
        )
    assert controller.state == "normal"

    decision = controller.immediate_load_admission(
        sample_reader=lambda: healthy_sample(observed_at=now[0])
    )

    assert decision.admitted is False
    assert decision.reasons == ("controller_recovering",)
    assert priority_reads == [True]


def test_priority_status_has_exact_keys_transition_semantics_and_one_clock_read():
    now = [0.0]
    clock_reads = []

    def clock():
        clock_reads.append(now[0])
        return now[0]

    controller = PressureController(
        reserve_bytes=GIB,
        priority_reader=lambda: priority_observation(),
        clock=clock,
    )
    controller.observe(
        _priority_sample(0.0, priority_observation(epoch_changed=True)),
        model_resident=False,
    )
    controller.observe(
        _priority_sample(
            1.0,
            priority_observation(sequence=2, source_generation=2),
        ),
        model_resident=False,
    )
    controller.observe(
        _priority_sample(
            2.0,
            priority_observation(
                state="asserted",
                sequence=3,
                source_generation=3,
            ),
        ),
        model_resident=False,
    )
    retained_unavailable = replace(
        priority_observation(
            state="asserted",
            sequence=3,
            source_generation=3,
        ),
        state="unavailable",
        accepted=False,
        new_publication=False,
        reason_codes=(),
    )
    controller.observe(
        _priority_sample(3.0, retained_unavailable),
        model_resident=False,
    )
    clock_reads.clear()
    now[0] = 4.0

    snapshot = controller.runtime_status_snapshot(
        {
            "model_resident": False,
            "model_load_generation": 4,
            "model_unload_generation": 3,
        }
    )
    priority = snapshot["priority_pressure"]
    assert set(priority) == {
        "configured",
        "state",
        "heartbeat_age_ms",
        "source_age_ms",
        "policy_sha256",
        "observation_digest",
        "transition_observation_digest",
        "transition_sequence",
        "controller_phase",
        "recovery_reason",
        "distinct_clear_count",
        "model_resident",
        "model_load_generation",
        "model_unload_generation",
    }
    assert priority["transition_sequence"] == 3
    assert priority["transition_observation_digest"] is None
    assert priority["observation_digest"] == f"{3:064x}"
    assert clock_reads == [4.0]

    gate_priority = controller.gate_priority_status_snapshot()
    assert gate_priority["source_generation"] == 3
    assert gate_priority["admission_open"] is False


def test_same_state_heartbeat_is_stable_but_same_state_new_epoch_transitions():
    now = [0.0]
    controller = _priority_controller(now=now)
    model_snapshot = {
        "model_resident": False,
        "model_load_generation": 0,
        "model_unload_generation": 0,
    }
    first = priority_observation(epoch_changed=True)
    controller.observe(
        _priority_sample(0.0, first),
        model_resident=False,
    )
    initial = controller.priority_status_snapshot(model_snapshot)

    now[0] = 1.0
    heartbeat = priority_observation(
        sequence=1,
        source_generation=1,
        new_publication=False,
    )
    controller.observe(
        _priority_sample(1.0, heartbeat),
        model_resident=False,
    )
    repeated = controller.priority_status_snapshot(model_snapshot)

    now[0] = 2.0
    new_epoch = priority_observation(
        sequence=1,
        source_generation=2,
        epoch_changed=True,
    )
    controller.observe(
        _priority_sample(2.0, new_epoch),
        model_resident=False,
    )
    transitioned = controller.priority_status_snapshot(model_snapshot)

    assert repeated["transition_sequence"] == initial["transition_sequence"] == 1
    assert (
        repeated["transition_observation_digest"]
        == (initial["transition_observation_digest"])
    )
    assert transitioned["transition_sequence"] == 2
    assert transitioned["transition_observation_digest"] == (
        new_epoch.observation_digest
    )
    assert transitioned["distinct_clear_count"] == 0


def test_disabled_priority_status_does_not_fabricate_clear_evidence():
    controller = PressureController(reserve_bytes=GIB, clock=lambda: 0.0)

    priority = controller.priority_status_snapshot(
        {
            "model_resident": False,
            "model_load_generation": 0,
            "model_unload_generation": 0,
        }
    )

    assert priority["configured"] is False
    assert priority["state"] == "disabled"
    assert priority["heartbeat_age_ms"] is None
    assert priority["source_age_ms"] is None
    assert priority["policy_sha256"] is None
    assert priority["observation_digest"] is None
    assert priority["transition_observation_digest"] is None
    assert priority["transition_sequence"] == 0
    assert priority["distinct_clear_count"] == 0
