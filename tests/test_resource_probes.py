import pytest

import subgen_core.resource_management as resource_policy
import subgen_core.resource_probes as probes
from subgen_core.priority_pressure import PriorityObservation


GIB = 1024**3


def mapping_reader(values):
    def read(path):
        key = str(path).replace("\\", "/")
        if key not in values:
            raise FileNotFoundError(key)
        return values[key]

    return read


def test_pressure_reader_uses_injected_cgroup_meminfo_psi_and_clock():
    reader = mapping_reader(
        {
            "/proc/meminfo": "MemTotal: 16777216 kB\nMemAvailable: 4194304 kB\n",
            "/cg/memory.max": str(8 * GIB),
            "/cg/memory.current": str(3 * GIB),
            "/cg/memory.events": "oom 2\noom_kill 1\n",
            "/cg/memory.pressure": "some avg10=11.5 avg60=0 total=1\nfull avg10=0.5 avg60=0 total=1\n",
            "/proc/pressure/memory": "some avg10=3.0 avg60=0 total=1\nfull avg10=1.25 avg60=0 total=1\n",
        }
    )

    sample = probes.read_pressure_sample(
        read_text=reader,
        clock=lambda: 123.0,
        platform_memory_reader=lambda: (_ for _ in ()).throw(AssertionError("unused")),
        cgroup_v2_root="/cg",
        cgroup_v1_roots=(),
    )

    assert sample.observed_at == 123.0
    assert sample.host_total_bytes == 16 * GIB
    assert sample.host_available_bytes == 4 * GIB
    assert sample.cgroup_limit_bytes == 8 * GIB
    assert sample.cgroup_current_bytes == 3 * GIB
    assert sample.psi_some_avg10 == 11.5
    assert sample.psi_full_avg10 == 1.25
    assert sample.cgroup_oom_events == 2
    assert sample.cgroup_oom_kill_events == 1


def test_pressure_reader_accepts_an_injected_exact_gpu_query():
    sample = probes.read_pressure_sample(
        read_text=mapping_reader({}),
        clock=lambda: 25.0,
        platform_memory_reader=lambda: (4 * GIB, 16 * GIB),
        cgroup_v1_roots=(),
        gpu_memory_reader=lambda: ("GPU-A", 24 * GIB, 11 * GIB),
    )

    assert sample.gpu_device_id == "GPU-A"
    assert sample.gpu_total_bytes == 24 * GIB
    assert sample.gpu_free_bytes == 11 * GIB
    assert sample.gpu_observed_at == 25.0


@pytest.mark.parametrize(
    ("root", "values"),
    [
        (
            "/v2",
            {
                "/v2/memory.max": str(4 * GIB),
                "/v2/memory.current": "0",
            },
        ),
        (
            "/missing",
            {
                "/v1/memory.limit_in_bytes": str(4 * GIB),
                "/v1/memory.usage_in_bytes": "0",
                "/v1/memory.failcnt": "0",
            },
        ),
    ],
)
def test_zero_cgroup_usage_is_valid_and_missing_oom_stays_unavailable(root, values):
    sample = probes.read_pressure_sample(
        read_text=mapping_reader(values),
        clock=lambda: 1.0,
        platform_memory_reader=lambda: (GIB, 8 * GIB),
        cgroup_v2_root=root,
        cgroup_v1_roots=("/v1",),
    )

    assert sample.cgroup_current_bytes == 0
    if "/v1/memory.failcnt" in values:
        assert sample.cgroup_oom_events == 0
    else:
        assert sample.cgroup_oom_events is None
    assert sample.cgroup_oom_kill_events is None


@pytest.mark.parametrize(
    "gpu_result",
    [
        ("GPU-A", True, 0),
        ("GPU-A", 24 * GIB, False),
        ("GPU-A", 24 * GIB, 25 * GIB),
    ],
)
def test_malformed_or_boolean_gpu_telemetry_is_unavailable(gpu_result):
    sample = probes.read_pressure_sample(
        read_text=mapping_reader({}),
        clock=lambda: 1.0,
        platform_memory_reader=lambda: (GIB, 8 * GIB),
        cgroup_v1_roots=(),
        gpu_memory_reader=lambda: gpu_result,
    )

    assert sample.gpu_device_id is None
    assert sample.gpu_total_bytes is None
    assert sample.gpu_free_bytes is None
    assert sample.gpu_observed_at is None


def test_timed_out_gpu_probe_degrades_to_unavailable():
    def timed_out():
        raise TimeoutError("bounded query timed out")

    sample = probes.read_pressure_sample(
        read_text=mapping_reader({}),
        clock=lambda: 1.0,
        platform_memory_reader=lambda: (GIB, 8 * GIB),
        cgroup_v1_roots=(),
        gpu_memory_reader=timed_out,
    )

    assert sample.gpu_total_bytes is None


def test_priority_reader_is_invoked_each_time_and_observation_is_carried_unchanged():
    observations = [
        PriorityObservation(
            state="asserted",
            sequence=1,
            accepted=True,
            new_publication=True,
        ),
        PriorityObservation(
            state="clear",
            sequence=2,
            accepted=True,
            new_publication=True,
        ),
    ]
    calls = []

    def read_priority():
        calls.append(None)
        return observations[len(calls) - 1]

    first = probes.read_pressure_sample(
        read_text=mapping_reader({}),
        clock=lambda: 1.0,
        platform_memory_reader=lambda: (GIB, 8 * GIB),
        cgroup_v1_roots=(),
        priority_reader=read_priority,
    )
    second = probes.read_pressure_sample(
        read_text=mapping_reader({}),
        clock=lambda: 2.0,
        platform_memory_reader=lambda: (GIB, 8 * GIB),
        cgroup_v1_roots=(),
        priority_reader=read_priority,
    )

    assert first.priority_observation is observations[0]
    assert second.priority_observation is observations[1]
    assert len(calls) == 2


def test_unconfigured_priority_reader_remains_unavailable_without_synthetic_state():
    sample = probes.read_pressure_sample(
        read_text=mapping_reader({}),
        clock=lambda: 1.0,
        platform_memory_reader=lambda: (GIB, 8 * GIB),
        cgroup_v1_roots=(),
    )

    assert sample.priority_observation is None


def test_unexpected_priority_reader_failure_is_not_reclassified_by_probe_bridge():
    def fail_priority():
        raise RuntimeError("unexpected priority reader failure")

    with pytest.raises(RuntimeError, match="unexpected priority reader failure"):
        probes.read_pressure_sample(
            read_text=mapping_reader({}),
            clock=lambda: 1.0,
            platform_memory_reader=lambda: (GIB, 8 * GIB),
            cgroup_v1_roots=(),
            priority_reader=fail_priority,
        )


def test_oversized_meminfo_and_nonfinite_psi_values_are_unavailable():
    enormous = "9" * 500
    sample = probes.read_pressure_sample(
        read_text=mapping_reader(
            {
                "/proc/meminfo": (
                    f"MemTotal: {enormous} kB\nMemAvailable: {enormous} kB\n"
                ),
                "/proc/pressure/memory": (
                    f"some avg10={enormous} avg60=0 total=1\n"
                    f"full avg10={enormous} avg60=0 total=1\n"
                ),
            }
        ),
        clock=lambda: 1.0,
        platform_memory_reader=lambda: (None, None),
        cgroup_v1_roots=(),
    )

    assert sample.host_available_bytes is None
    assert sample.host_total_bytes is None
    assert sample.psi_some_avg10 is None
    assert sample.psi_full_avg10 is None


@pytest.mark.parametrize(
    "psi",
    [
        "some avg10=nan avg60=0 total=1\nfull avg10=inf avg60=0 total=1\n",
        "some avg10=-1 avg60=0 total=1\nfull malformed\n",
        "some avg10=1e309 avg60=0 total=1\nfull avg10=1.2junk avg60=0 total=1\n",
        "some avg10=100.0001 avg60=0 total=1\nfull avg10=101 avg60=0 total=1\n",
        "not-psi\n",
    ],
)
def test_malformed_psi_remains_unavailable_instead_of_becoming_healthy_zero(psi):
    sample = probes.read_pressure_sample(
        read_text=mapping_reader({"/proc/pressure/memory": psi}),
        clock=lambda: 1.0,
        platform_memory_reader=lambda: (GIB, 8 * GIB),
        cgroup_v1_roots=(),
    )

    assert sample.psi_some_avg10 is None
    assert sample.psi_full_avg10 is None
    assert sample.host_psi_some_avg10 is None
    assert sample.host_psi_full_avg10 is None


def test_psi_parser_accepts_inclusive_zero_and_one_hundred_percent_bounds():
    sample = probes.read_pressure_sample(
        read_text=mapping_reader(
            {
                "/proc/pressure/memory": (
                    "some avg10=0 avg60=0 total=1\nfull avg10=100.0 avg60=0 total=1\n"
                )
            }
        ),
        clock=lambda: 1.0,
        platform_memory_reader=lambda: (GIB, 8 * GIB),
        cgroup_v1_roots=(),
    )

    assert sample.psi_some_avg10 == 0.0
    assert sample.psi_full_avg10 == 100.0
    assert sample.host_psi_some_avg10 == 0.0
    assert sample.host_psi_full_avg10 == 100.0


@pytest.mark.parametrize("clock_value", [10**400, float("inf"), float("nan"), -1, True])
def test_probe_clock_rejects_huge_or_nonfinite_values_with_bounded_validation(
    clock_value,
):
    with pytest.raises(ValueError, match="finite non-negative"):
        probes.read_pressure_sample(
            read_text=mapping_reader({}),
            clock=lambda: clock_value,
            platform_memory_reader=lambda: (GIB, 8 * GIB),
            cgroup_v1_roots=(),
        )


def test_resource_owner_reexports_the_probe_sampling_surface():
    assert resource_policy.PressureSample is probes.PressureSample
    assert resource_policy.read_pressure_sample is probes.read_pressure_sample
