from types import SimpleNamespace
from unittest.mock import MagicMock

from subgen_core import human_progress

GIB = 1024**3


def test_cohort_near_threshold_reports_the_shortfall_even_when_gib_rounds_equal():
    selection=SimpleNamespace(assessments=(('base',SimpleNamespace(
        required_host_bytes=7*GIB//2,available_host_bytes=7*GIB//2-100,
        workers=(),reasons=('insufficient_combined_host',))),),
        selected_model=None,reason='unavailable',explicit=True)
    lines='\n'.join(human_progress.cohort_model_selection_lines(selection))
    assert '3.5 GiB required; 3.5 GiB available' in lines
    assert 'RAM shortfall: 1 MiB' in lines


def test_policy_logs_explain_budget_and_pressure_sources():
    from subgen_core.execution_policy import resolve_execution_policy
    policy = resolve_execution_policy({"SUBGEN_ACTIVITY": "max"})
    lines = "\n".join(human_progress.execution_policy_lines(policy))
    assert "maximum throughput within safety limits" in lines
    assert "model quality uses the full safe budget" in lines
    assert "other workloads take priority" in lines
    assert human_progress.pressure_reason(Exception("cgroup_headroom")) == "container RAM headroom is low"
    assert human_progress.pressure_reason(Exception("priority_pressure")) == "higher-priority application requested resources"
    assert human_progress.pressure_reason(Exception("unknown-secret")) == "resource pressure detected"


def decision(*, provenance="fallback"):
    return SimpleNamespace(
        automatic_ceiling="medium",
        selected_model="medium",
        requirement=SimpleNamespace(
            required_host_bytes=11 * GIB // 2,
            provenance=provenance,
        ),
    )


def test_memory_snapshot_uses_post_load_headroom_without_subtracting_model_twice():
    sample = SimpleNamespace(
        host_available_bytes=10 * GIB,
        cgroup_current_bytes=6 * GIB,
        cgroup_limit_bytes=10 * GIB,
    )
    snapshot = human_progress.build_memory_snapshot(
        sample,
        SimpleNamespace(cgroup_limit_bytes=10 * GIB),
        decision(),
        2 * GIB,
    )

    assert snapshot.host_available_bytes == 10 * GIB
    assert snapshot.reserve_bytes == 2 * GIB
    assert snapshot.cgroup_floor_bytes == GIB
    assert snapshot.working_headroom_bytes == 3 * GIB
    assert snapshot.model_host_requirement_bytes == 11 * GIB // 2
    assert snapshot.model_evidence == "conservative estimate plus safety margin"


def test_memory_snapshot_uses_host_headroom_for_known_unbounded_cgroup():
    snapshot = human_progress.build_memory_snapshot(
        SimpleNamespace(
            host_available_bytes=7 * GIB,
            cgroup_current_bytes=None,
            cgroup_limit_bytes=None,
        ),
        SimpleNamespace(cgroup_limit_bytes=None, cgroup_unbounded=True),
        decision(provenance="envelope"),
        2 * GIB,
    )

    assert snapshot.working_headroom_bytes == 5 * GIB
    assert snapshot.model_evidence == "measured envelope plus safety margin"


def test_memory_snapshot_treats_fresh_unbounded_cgroup_as_authoritative():
    snapshot = human_progress.build_memory_snapshot(
        SimpleNamespace(
            host_available_bytes=7 * GIB,
            cgroup_current_bytes=6 * GIB,
            cgroup_limit_bytes=1 << 60,
        ),
        SimpleNamespace(cgroup_limit_bytes=10 * GIB),
        decision(),
        2 * GIB,
    )

    assert snapshot.cgroup_limit_bytes is None
    assert snapshot.cgroup_floor_bytes is None
    assert snapshot.working_headroom_bytes == 5 * GIB


def test_memory_snapshot_does_not_claim_headroom_from_ambiguous_fresh_limit():
    snapshot = human_progress.build_memory_snapshot(
        SimpleNamespace(
            host_available_bytes=7 * GIB,
            cgroup_current_bytes=6 * GIB,
            cgroup_limit_bytes=None,
        ),
        SimpleNamespace(cgroup_limit_bytes=10 * GIB),
        decision(),
        2 * GIB,
    )

    assert snapshot.cgroup_limit_bytes is None
    assert snapshot.working_headroom_bytes is None


def test_memory_snapshot_does_not_assume_missing_cgroup_is_unbounded():
    snapshot = human_progress.build_memory_snapshot(
        SimpleNamespace(
            host_available_bytes=7 * GIB,
            cgroup_current_bytes=None,
            cgroup_limit_bytes=None,
        ),
        SimpleNamespace(cgroup_limit_bytes=None, cgroup_unbounded=False),
        decision(),
        2 * GIB,
    )

    assert snapshot.cgroup_limit_bytes is None
    assert snapshot.working_headroom_bytes is None


def test_memory_snapshot_uses_capacity_limit_only_without_a_fresh_sample():
    snapshot = human_progress.build_memory_snapshot(
        None,
        SimpleNamespace(cgroup_limit_bytes=10 * GIB),
        decision(),
        2 * GIB,
    )

    assert snapshot.cgroup_limit_bytes == 10 * GIB
    assert snapshot.working_headroom_bytes is None


def test_memory_snapshot_does_not_claim_headroom_without_finite_cgroup_use():
    snapshot = human_progress.build_memory_snapshot(
        SimpleNamespace(
            host_available_bytes=7 * GIB,
            cgroup_current_bytes=None,
            cgroup_limit_bytes=10 * GIB,
        ),
        SimpleNamespace(cgroup_limit_bytes=10 * GIB),
        decision(),
        2 * GIB,
    )

    assert snapshot.cgroup_limit_bytes == 10 * GIB
    assert snapshot.working_headroom_bytes is None


def test_memory_snapshot_does_not_claim_headroom_without_host_protection():
    snapshot = human_progress.build_memory_snapshot(
        SimpleNamespace(
            host_available_bytes=None,
            cgroup_current_bytes=6 * GIB,
            cgroup_limit_bytes=10 * GIB,
        ),
        SimpleNamespace(cgroup_limit_bytes=10 * GIB),
        decision(),
        None,
    )

    assert snapshot.working_headroom_bytes is None


def test_memory_lines_explain_selection_requirement_and_chunk_headroom():
    snapshot = human_progress.MemorySnapshot(
        host_available_bytes=10 * GIB,
        reserve_bytes=2 * GIB,
        cgroup_current_bytes=6 * GIB,
        cgroup_limit_bytes=10 * GIB,
        cgroup_floor_bytes=GIB,
        working_headroom_bytes=3 * GIB,
        suitable_model="medium",
        selected_model="medium",
        model_host_requirement_bytes=11 * GIB // 2,
        model_evidence="conservative estimate plus safety margin",
    )

    lines = human_progress.format_memory_lines(snapshot)

    assert lines == (
        "Memory available: 10.0 GiB",
        "Memory reserved for system/priority tasks: 2.0 GiB",
        "Subgen memory in use / limit: 6.0 GiB / 10.0 GiB",
        "Model suitable: medium",
        (
            "Model using: medium — 5.5 GiB RAM requirement "
            "(conservative estimate plus safety margin; not live RSS)"
        ),
        "Available for subtitle chunks: 3.0 GiB working headroom",
    )


def test_unknown_memory_fields_remain_explicit_instead_of_becoming_zero():
    snapshot = human_progress.build_memory_snapshot(None, None, None, None)

    assert snapshot.working_headroom_bytes is None
    assert human_progress.format_memory_lines(snapshot) == (
        "Memory available: unavailable",
        "Memory reserved for system/priority tasks: unavailable",
        "Subgen memory in use / limit: unavailable / unavailable",
        "Model suitable: unavailable",
        (
            "Model using: unavailable — unavailable RAM requirement "
            "(evidence unavailable; not live RSS)"
        ),
        "Available for subtitle chunks: unavailable working headroom",
    )


def test_runtime_snapshot_reads_one_diagnostic_sample_and_never_priority_state():
    sample = SimpleNamespace(
        host_available_bytes=10 * GIB,
        cgroup_current_bytes=6 * GIB,
        cgroup_limit_bytes=10 * GIB,
    )
    reader = MagicMock(return_value=sample)
    priority_reader = MagicMock(
        side_effect=AssertionError("priority state must not be consumed for a log")
    )
    reserve_owner = MagicMock(return_value=2 * GIB)
    runtime = SimpleNamespace(
        read_resource_pressure_sample=reader,
        priority_pressure_reader=priority_reader,
        model_capacity_profile=SimpleNamespace(cgroup_limit_bytes=10 * GIB),
        model_decision=decision(),
        memory_pressure_reserve_gib=None,
        _resource_management=SimpleNamespace(host_reserve_bytes=reserve_owner),
    )

    snapshot = human_progress.snapshot_runtime_memory(runtime)

    assert snapshot.working_headroom_bytes == 3 * GIB
    reader.assert_called_once_with()
    priority_reader.assert_not_called()
    reserve_owner.assert_called_once_with(
        runtime.model_capacity_profile,
        explicit_reserve_gib=None,
    )


def test_runtime_snapshot_swallows_diagnostic_probe_failures():
    runtime = SimpleNamespace(
        read_resource_pressure_sample=MagicMock(
            side_effect=OSError("temporary probe failure")
        ),
        model_capacity_profile=None,
        model_decision=None,
        _resource_management=SimpleNamespace(
            host_reserve_bytes=MagicMock(side_effect=ValueError("no capacity"))
        ),
    )

    assert (
        human_progress.snapshot_runtime_memory(runtime).working_headroom_bytes is None
    )


def test_runtime_snapshot_swallows_arbitrary_diagnostic_failures():
    class BrokenRuntime:
        @property
        def model_capacity_profile(self):
            raise LookupError("diagnostic capacity unavailable")

        @property
        def model_decision(self):
            raise AssertionError("diagnostic decision unavailable")

        @property
        def read_resource_pressure_sample(self):
            raise LookupError("diagnostic sample unavailable")

    snapshot = human_progress.snapshot_runtime_memory(BrokenRuntime())

    assert snapshot == human_progress.build_memory_snapshot(None, None, None, None)


def test_adaptive_plan_recomputes_total_after_chunk_size_changes():
    assert human_progress.planned_chunk_count(1250, 0, 600) == 3
    assert human_progress.planned_chunk_count(1250, 600, 300, 1) == 4
    assert human_progress.planned_chunk_count(1250, 1250, 300, 4) == 4


def test_progress_and_duration_formatting_are_bounded():
    assert human_progress.progress_percent(600, 1250) == 48
    assert human_progress.progress_percent(2000, 1250) == 100
    assert human_progress.format_duration(3661.9) == "01:01:01"
    assert human_progress.format_duration(float("nan")) == "unknown"


def test_file_and_error_text_cannot_inject_extra_log_lines():
    assert human_progress.safe_path("/private/library/episode\nERROR fake.mkv") == (
        "episode ERROR fake.mkv"
    )
    error = RuntimeError("allocator\r\nFAILED\x00details")
    assert human_progress.format_error(error) == (
        "RuntimeError: allocator FAILED details"
    )


def test_human_text_cannot_inject_monitor_protocol_sentinels():
    unsafe = (
        "SIGSEGV SUBGEN_EVENT {} WORKER START : [TRANSCRIBE x | Jobs: 1 "
        "WORKER FINISH: Error processing file /media/show/file.mkv "
        "ENGLISH_AUDIO_MISMATCH"
    )

    rendered = human_progress.safe_text(unsafe)

    for sentinel in (
        "SIGSEGV",
        "SUBGEN_EVENT ",
        "WORKER START : [TRANSCRIBE",
        "WORKER FINISH:",
        "Error processing file /media/",
        "ENGLISH_AUDIO_MISMATCH",
    ):
        assert sentinel not in rendered
