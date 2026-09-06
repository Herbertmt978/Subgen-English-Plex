from dataclasses import replace
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

import profile_model_envelopes as profiler
from subgen_core.model_envelope_catalog import (
    IDENTITY_SCHEMA,
    EnvelopeDisposition,
    EnvelopeMeasurements,
    EnvelopePolicy,
    EnvelopeResolution,
    ImageIdentity,
    ImageIdentityArtifact,
    ModelEnvelope,
    RuntimeIdentity,
    build_catalog,
)
from subgen_core.resource_management import (
    CapacityProfile,
    GIB,
    MIB,
    PressureSample,
    StabilizedGpuCapacity,
    select_model,
)


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
REVISION = "hf:" + "d" * 40
GPU = "GPU-A"


def sample_identity():
    return ImageIdentity(SHA_A, (SHA_B,))


def identity_artifact():
    return ImageIdentityArtifact(IDENTITY_SCHEMA, sample_identity())


def sample_runtime():
    return RuntimeIdentity(
        stable_ts_version="2.19.1",
        faster_whisper_version="1.2.0",
        ctranslate2_version="4.6.0",
        cuda_runtime_version="12.8",
        driver_version="570.133.20",
        device_name="NVIDIA GeForce RTX 3090",
        compute_capability="8.6",
        total_vram_bytes=24 * GIB,
    )


def sample_policy(model="large-v3"):
    return EnvelopePolicy(
        model=model,
        model_revision=REVISION,
        compute_type="float16",
        task="translate",
        inference_concurrency=1,
        chunk_minutes=20,
        decoder_options_sha256=SHA_C,
    )


def sample_measurements(
    *,
    host_incremental=5 * GIB,
    cgroup_incremental=5 * GIB,
    device_incremental=7 * GIB,
    host_margin=512 * MIB,
    device_margin=2 * GIB,
):
    return EnvelopeMeasurements(
        runs=3,
        host_preload_used_bytes=GIB,
        host_peak_used_bytes=GIB + host_incremental,
        cgroup_preload_used_bytes=GIB,
        cgroup_peak_used_bytes=GIB + cgroup_incremental,
        device_preload_used_bytes=6 * GIB,
        device_peak_used_bytes=6 * GIB + device_incremental,
        host_incremental_peak_bytes=host_incremental,
        cgroup_incremental_peak_bytes=cgroup_incremental,
        device_incremental_peak_bytes=device_incremental,
        host_margin_bytes=host_margin,
        device_margin_bytes=device_margin,
    )


def sample_envelope(model="medium", **measurement_changes):
    return ModelEnvelope(
        sample_identity(),
        sample_runtime(),
        sample_policy(model),
        sample_measurements(**measurement_changes),
    )


def healthy_admission(*, cgroup_limit=12 * GIB, cgroup_current=GIB, now=100.0):
    sample = PressureSample(
        observed_at=now,
        host_available_bytes=32 * GIB,
        host_total_bytes=64 * GIB,
        cgroup_current_bytes=cgroup_current,
        cgroup_limit_bytes=cgroup_limit,
        cgroup_oom_events=0,
        cgroup_oom_kill_events=0,
        gpu_total_bytes=24 * GIB,
        gpu_free_bytes=20 * GIB,
        gpu_device_id=GPU,
        gpu_observed_at=now,
    )
    stabilized = StabilizedGpuCapacity(GPU, 24 * GIB, 20 * GIB, now, 3)
    return profiler.AdmissionInputs(
        capacity=CapacityProfile(
            12 * GIB,
            64 * GIB,
            cgroup_limit,
            "cgroup_v2",
            cgroup_version=2,
        ),
        device="cuda",
        sample=sample,
        stabilized_gpu=stabilized,
        expected_gpu_device=GPU,
        now=now,
    )


def fresh_load(*, cgroup_limit=12 * GIB, cgroup_current=GIB, now=100.0):
    context = healthy_admission(
        cgroup_limit=cgroup_limit,
        cgroup_current=cgroup_current,
        now=now,
    )
    return profiler.FreshLoadInputs(
        capacity=context.capacity,
        sample=context.sample,
        now=now,
    )


def controller_admission(requirement, sample):
    return profiler.resource_owner.evaluate_admission(
        requirement,
        sample,
        host_reserve_bytes=GIB,
        gpu_priority_reserve_bytes=4 * GIB,
        require_cgroup=True,
        require_gpu=True,
        expected_gpu_device=GPU,
        now=sample.observed_at,
    )


def cycle(
    host_preload,
    host_peak,
    cgroup_preload,
    cgroup_peak,
    device_preload,
    device_peak,
):
    return profiler.ColdCycleMeasurement(
        profiler.ResourceUsage(host_preload, cgroup_preload, device_preload),
        profiler.ResourceUsage(host_peak, cgroup_peak, device_peak),
    )


DEFAULT_CYCLES = (
    cycle(GIB, 4 * GIB, GIB, 4 * GIB, 6 * GIB, 9 * GIB),
    cycle(5 * GIB, 6 * GIB, 2 * GIB, 7 * GIB, 7 * GIB, 9 * GIB),
    cycle(2 * GIB, 8 * GIB, 3 * GIB, 7 * GIB, 5 * GIB, 12 * GIB),
)


class FakeAdapter:
    def __init__(
        self,
        *,
        contexts=None,
        fresh_contexts=None,
        cycles=DEFAULT_CYCLES,
        runtime=None,
    ):
        self.contexts = list(contexts or [healthy_admission()])
        self.fresh_contexts = list(fresh_contexts or [fresh_load()])
        self.cycles = list(cycles)
        self.runtime = runtime or sample_runtime()
        self.admission_calls = 0
        self.fresh_calls = 0
        self.measure_calls = []
        self.release_calls = 0
        self.assert_calls = 0
        self.resident = False

    def runtime_identity(self):
        return self.runtime

    def admission_inputs(self):
        index = min(self.admission_calls, len(self.contexts) - 1)
        self.admission_calls += 1
        return self.contexts[index]

    def fresh_load_inputs(self):
        index = min(self.fresh_calls, len(self.fresh_contexts) - 1)
        self.fresh_calls += 1
        return self.fresh_contexts[index]

    def pressure_sample(self):
        index = min(max(self.fresh_calls - 1, 0), len(self.fresh_contexts) - 1)
        return self.fresh_contexts[index].sample

    def priority_pressure_reader(self):
        return None

    def pressure_clock(self):
        index = min(max(self.fresh_calls - 1, 0), len(self.fresh_contexts) - 1)
        return self.fresh_contexts[index].now

    def assert_released(self):
        self.assert_calls += 1
        if self.resident:
            raise RuntimeError("resident")

    def validate_workload(self, **kwargs):
        self.validated_workload = kwargs

    def measure_cold_cycle(self, **kwargs):
        self.resident = True
        self.measure_calls.append(kwargs)
        return self.cycles[len(self.measure_calls) - 1]

    def release_model(self):
        self.release_calls += 1
        self.resident = False


class AllocationFailureAdapter(FakeAdapter):
    def measure_cold_cycle(self, **kwargs):
        self.resident = True
        self.measure_calls.append(kwargs)
        raise RuntimeError("CUDA out of memory while allocating model")


class UnexpectedFailureAdapter(FakeAdapter):
    def measure_cold_cycle(self, **kwargs):
        self.resident = True
        self.measure_calls.append(kwargs)
        raise LookupError("unexpected backend fault")


def packaged_adapter(
    tmp_path,
    monkeypatch,
    *,
    loader,
    sample_interval_seconds=0.001,
    priority_watch_interval_seconds=0.001,
):
    properties = SimpleNamespace(
        name="NVIDIA GeForce RTX 3090",
        total_memory=24 * GIB,
    )
    cuda = SimpleNamespace(
        is_available=lambda: True,
        get_device_properties=lambda _index: properties,
        get_device_capability=lambda _index: (8, 6),
        synchronize=lambda _index: None,
        empty_cache=lambda: None,
        mem_get_info=lambda _index: (20 * GIB, 24 * GIB),
    )
    modules = {
        "stable_whisper": SimpleNamespace(
            __version__="2.19.1",
            load_faster_whisper=loader,
        ),
        "faster_whisper": SimpleNamespace(__version__="1.2.0"),
        "ctranslate2": SimpleNamespace(__version__="4.6.0"),
        "torch": SimpleNamespace(cuda=cuda, version=SimpleNamespace(cuda="12.8")),
    }
    monkeypatch.setattr(
        profiler.importlib,
        "import_module",
        lambda name: modules[name],
    )
    adapter = profiler.StableWhisperMeasurementAdapter(
        model="large-v3",
        model_revision=REVISION,
        model_path=tmp_path / "models",
        device="cuda",
        compute_type="float16",
        cpu_threads=4,
        decoder_options={},
        sample_interval_seconds=sample_interval_seconds,
        priority_watch_interval_seconds=priority_watch_interval_seconds,
        gpu_stabilization_interval_seconds=0.001,
    )
    adapter._expected_gpu_device = GPU
    sample = healthy_admission().sample
    monkeypatch.setattr(adapter, "_pressure_sample", lambda: sample)
    monkeypatch.setattr(
        adapter,
        "_media_duration_seconds",
        lambda _path: 20 * 60 + 10,
    )
    media = tmp_path / "profile.wav"
    media.write_bytes(b"disposable")
    policy = replace(
        sample_policy(),
        decoder_options_sha256=profiler._decoder_options_digest({}),
    )
    return adapter, media, policy, sample


def request(tmp_path, **changes):
    values = {
        "catalog_input": tmp_path / "canonical" / "catalog.json",
        "catalog_output": tmp_path / "staged" / "catalog.json",
        "identity_path": tmp_path / "canonical" / "identity.json",
        "media_path": tmp_path / "input" / "long.wav",
        "model": "large-v3",
        "runs": 3,
        "policy": sample_policy(),
        "host_margin_bytes": 768 * MIB,
        "device_margin_bytes": 2 * GIB,
        "expected_uid": 1234,
        "host_reserve_bytes": GIB,
        "gpu_reserve_bytes": 4 * GIB,
        "canonical_shared_cuda": False,
        "require_cgroup": True,
    }
    values.update(changes)
    return profiler.ProfilerRequest(**values)


def install_catalog_stubs(monkeypatch, req, *, input_catalog=None):
    input_catalog = input_catalog or build_catalog(catalog_version=1, entries=())
    calls = {"load_identity": [], "load_catalog": [], "write_catalog": []}
    staged = {}

    def load_identity(path, *, expected_uid=None):
        calls["load_identity"].append((path, expected_uid))
        return identity_artifact()

    def load_catalog(path, *, expected_uid=None):
        calls["load_catalog"].append((path, expected_uid))
        if Path(path) == req.catalog_input:
            return input_catalog
        assert Path(path) == req.catalog_output
        return staged["catalog"]

    def write_catalog(path, catalog, *, expected_uid=None):
        calls["write_catalog"].append((path, catalog, expected_uid))
        staged["catalog"] = catalog

    monkeypatch.setattr(profiler.catalog_owner, "load_identity", load_identity)
    monkeypatch.setattr(profiler.catalog_owner, "load_catalog", load_catalog)
    monkeypatch.setattr(profiler.catalog_owner, "write_catalog", write_catalog)
    return calls, staged


def test_success_profiles_three_cold_cycles_through_owners_and_stages_catalog(
    tmp_path, monkeypatch
):
    req = request(tmp_path)
    calls, staged = install_catalog_stubs(monkeypatch, req)
    adapter = FakeAdapter()
    paired_calls = []
    original = profiler.resource_owner.paired_incremental_peak_bytes

    def paired(preloads, peaks):
        paired_calls.append((tuple(preloads), tuple(peaks)))
        return original(preloads, peaks)

    monkeypatch.setattr(
        profiler.resource_owner, "paired_incremental_peak_bytes", paired
    )

    result = profiler.profile_model_envelope(req, adapter)

    assert result.succeeded is True
    assert result.catalog_version == 2
    assert result.envelope.image_identity == identity_artifact().image_identity
    assert result.envelope.runtime == sample_runtime()
    assert result.envelope.policy == sample_policy()
    measured = result.envelope.measurements
    assert measured.runs == 3
    assert measured.host_preload_used_bytes == 5 * GIB
    assert measured.host_peak_used_bytes == 8 * GIB
    assert measured.host_incremental_peak_bytes == 6 * GIB
    assert measured.cgroup_incremental_peak_bytes == 5 * GIB
    assert measured.device_incremental_peak_bytes == 7 * GIB
    assert measured.host_margin_bytes == 768 * MIB
    assert measured.device_margin_bytes == 2 * GIB
    assert len(paired_calls) == 3
    assert adapter.admission_calls == 2
    assert adapter.fresh_calls == 3
    assert adapter.release_calls == 3
    assert [call["run_number"] for call in adapter.measure_calls] == [1, 2, 3]
    assert all(call["model"] == "large-v3" for call in adapter.measure_calls)
    assert calls["load_identity"] == [(req.identity_path, 1234)]
    assert calls["load_catalog"] == [
        (req.catalog_input, 1234),
        (req.catalog_output, 1234),
    ]
    assert calls["write_catalog"][0][0] == req.catalog_output
    assert calls["write_catalog"][0][2] == 1234
    assert staged["catalog"].entries == (result.envelope,)


def test_request_requires_three_runs_distinct_staging_and_explicit_margins(tmp_path):
    with pytest.raises(ValueError, match="at least three"):
        request(tmp_path, runs=2)
    with pytest.raises(ValueError, match="catalog input"):
        request(tmp_path, catalog_output=tmp_path / "canonical" / "catalog.json")
    with pytest.raises(ValueError, match="identity input"):
        request(tmp_path, catalog_output=tmp_path / "canonical" / "identity.json")
    with pytest.raises(ValueError, match="host_margin"):
        request(tmp_path, host_margin_bytes=0)
    with pytest.raises(ValueError, match="device_margin"):
        request(tmp_path, device_margin_bytes=0)
    with pytest.raises(ValueError, match="host_reserve_bytes"):
        request(tmp_path, host_reserve_bytes=0)
    with pytest.raises(ValueError, match="gpu_reserve_bytes"):
        request(tmp_path, gpu_reserve_bytes=0)
    with pytest.raises(ValueError, match="inference_concurrency=1"):
        request(
            tmp_path,
            policy=replace(sample_policy(), inference_concurrency=2),
        )
    with pytest.raises(ValueError, match="chunk_minutes"):
        request(tmp_path, policy=replace(sample_policy(), chunk_minutes=4))


def test_refusal_explanation_uses_the_decision_budgets_without_reclassifying(capsys):
    resources = profiler.resource_owner
    requirement = resources.model_load_requirement(
        "large-v3", fallback_profile=resources.CUDA_CALIBRATION_PROFILE
    )
    decision = resources.AdmissionDecision(
        admitted=False, reasons=("insufficient_host",),
        host_admission_bytes=int(7.4 * GIB), cgroup_admission_bytes=12 * GIB,
        effective_host_admission_bytes=int(7.4 * GIB), device_admission_bytes=None,
        requirement=requirement,
    )
    before = repr(decision)
    profiler._report_capacity_refusal(decision)
    explanation = capsys.readouterr().err
    assert "Additional RAM required: 7.5 GiB" in explanation
    assert "host 7.4 GiB, container 12.0 GiB; limiting budget 7.4 GiB" in explanation
    assert "Additional VRAM required: 8.0 GiB" in explanation
    assert "available after reserve: unavailable" in explanation
    assert repr(decision) == before


def test_initial_admission_failure_is_safe_writes_nothing_and_names_next_model(
    tmp_path, monkeypatch, capsys
):
    req = request(tmp_path)
    calls, _ = install_catalog_stubs(monkeypatch, req)
    adapter = FakeAdapter(
        contexts=[healthy_admission(cgroup_limit=4 * GIB, cgroup_current=3 * GIB)]
    )

    result = profiler.profile_model_envelope(req, adapter)

    assert result.status == "safe_failure"
    assert result.next_model == "medium"
    assert "insufficient" in result.reason
    assert adapter.measure_calls == []
    assert adapter.release_calls == 0
    assert calls["write_catalog"] == []
    explanation = capsys.readouterr().err
    assert "Additional RAM required:" in explanation
    assert "planning estimate, including safety margin" in explanation
    assert "Available after reserves: host" in explanation
    assert "limiting budget" in explanation
    assert "Additional VRAM required:" in explanation
    assert "A suggested smaller model is not run automatically" in explanation


def test_stabilized_and_final_gpu_free_may_differ_when_both_admit(
    tmp_path, monkeypatch
):
    req = request(tmp_path)
    calls, _ = install_catalog_stubs(monkeypatch, req)
    context = healthy_admission()
    context = replace(
        context,
        stabilized_gpu=replace(context.stabilized_gpu, free_bytes=19 * GIB),
    )

    result = profiler.profile_model_envelope(
        req,
        FakeAdapter(contexts=[context]),
    )

    assert result.succeeded is True
    assert len(calls["write_catalog"]) == 1


def test_canonical_shared_profiler_requires_configured_priority_reader_before_io(
    tmp_path,
):
    req = request(tmp_path, canonical_shared_cuda=True)
    adapter = FakeAdapter()

    with pytest.raises(ValueError, match="PRIORITY_PRESSURE_FILE"):
        profiler.profile_model_envelope(req, adapter)

    assert adapter.admission_calls == 0
    assert adapter.measure_calls == []


def test_initial_priority_contention_cannot_authorize_model_descent(
    tmp_path, monkeypatch
):
    req = request(tmp_path, canonical_shared_cuda=True)
    calls, _ = install_catalog_stubs(monkeypatch, req)
    constrained = healthy_admission(cgroup_limit=4 * GIB, cgroup_current=3 * GIB)
    healthy = healthy_admission()

    class PriorityAdapter(FakeAdapter):
        def __init__(self):
            super().__init__(contexts=[healthy, constrained, healthy])

        def priority_pressure_reader(self):
            return lambda: profiler.priority_owner.PriorityObservation(
                state="unavailable",
                configured=True,
            )

    class PriorityFirstController:
        def __init__(self, *args, **kwargs):
            self.recovery_reason = "priority_pressure"
            self.admission_calls = 0

        def wait_for_recovery(self, *, refuse_unfit_explicit_model=False, heartbeat=None):
            return True

        def immediate_load_admission(self, requirement, *, sample_reader):
            self.admission_calls += 1
            sample = sample_reader()
            if self.admission_calls == 1:
                assert sample.cgroup_limit_bytes == 4 * GIB
                return SimpleNamespace(
                    admitted=False,
                    reasons=("controller_recovering",),
                    requirement=requirement,
                )
            return controller_admission(requirement, sample)

        def check_or_raise(self, *, force_priority=False):
            return "normal"

        def mark_released(self, reason=None):
            self.recovery_reason = reason
            return "recovering"

    decisions = []
    original_decision = profiler._explicit_admission_decision

    def record_decision(profile_request, inputs):
        decisions.append(inputs.sample.cgroup_limit_bytes)
        return original_decision(profile_request, inputs)

    monkeypatch.setattr(
        profiler.resource_owner,
        "PressureController",
        PriorityFirstController,
    )
    monkeypatch.setattr(profiler, "_explicit_admission_decision", record_decision)
    adapter = PriorityAdapter()

    result = profiler.profile_model_envelope(req, adapter)

    assert result.succeeded is True
    assert decisions == [12 * GIB]
    assert adapter.admission_calls == 3
    assert calls["write_catalog"]


def test_fresh_load_priority_contention_precedes_capacity_classification(
    tmp_path, monkeypatch
):
    req = request(tmp_path, canonical_shared_cuda=True)
    calls, _ = install_catalog_stubs(monkeypatch, req)
    constrained = fresh_load(cgroup_limit=4 * GIB, cgroup_current=3 * GIB)
    healthy = fresh_load()

    class PriorityAdapter(FakeAdapter):
        def __init__(self):
            super().__init__(fresh_contexts=[constrained, healthy, healthy, healthy])

        def priority_pressure_reader(self):
            return lambda: profiler.priority_owner.PriorityObservation(
                state="unavailable",
                configured=True,
            )

    class PriorityFirstController:
        def __init__(self, *args, **kwargs):
            self.recovery_reason = "priority_pressure"
            self.admission_calls = 0

        def wait_for_recovery(self, *, refuse_unfit_explicit_model=False, heartbeat=None):
            return True

        def immediate_load_admission(self, requirement, *, sample_reader):
            self.admission_calls += 1
            sample = sample_reader()
            if self.admission_calls == 1:
                return controller_admission(requirement, sample)
            if self.admission_calls == 2:
                assert sample.cgroup_limit_bytes == 4 * GIB
                return SimpleNamespace(
                    admitted=False,
                    reasons=("controller_recovering",),
                    requirement=requirement,
                )
            return controller_admission(requirement, sample)

        def check_or_raise(self, *, force_priority=False):
            return "normal"

        def mark_released(self, reason=None):
            self.recovery_reason = reason
            return "recovering"

    monkeypatch.setattr(
        profiler.resource_owner,
        "PressureController",
        PriorityFirstController,
    )
    adapter = PriorityAdapter()

    result = profiler.profile_model_envelope(req, adapter)

    assert result.succeeded is True
    assert adapter.fresh_calls == 4
    assert len(adapter.measure_calls) == 3
    assert calls["write_catalog"]


def test_every_actual_cycle_gets_a_fresh_resource_owner_admission(
    tmp_path, monkeypatch, capsys
):
    req = request(tmp_path)
    calls, _ = install_catalog_stubs(monkeypatch, req)
    constrained = fresh_load(cgroup_limit=4 * GIB, cgroup_current=3 * GIB)
    adapter = FakeAdapter(
        contexts=[healthy_admission()],
        fresh_contexts=[fresh_load(), constrained],
    )

    result = profiler.profile_model_envelope(req, adapter)

    assert result.status == "safe_failure"
    assert result.next_model == "medium"
    assert adapter.admission_calls == 2
    assert adapter.fresh_calls == 2
    assert len(adapter.measure_calls) == 1
    assert adapter.release_calls == 1
    assert calls["write_catalog"] == []
    assert "cannot start another large-v3 load" in capsys.readouterr().err


def test_missing_or_stale_gpu_evidence_is_not_a_safe_model_descent(
    tmp_path, monkeypatch
):
    req = request(tmp_path)
    calls, _ = install_catalog_stubs(monkeypatch, req)
    healthy = healthy_admission()
    missing_stabilization = replace(healthy, stabilized_gpu=None)

    with pytest.raises(profiler.ProfilingTelemetryError, match="three stabilized"):
        profiler.profile_model_envelope(
            req,
            FakeAdapter(contexts=[missing_stabilization]),
        )

    stale_sample = replace(
        healthy.sample,
        observed_at=80.0,
        gpu_observed_at=80.0,
    )
    stale = replace(healthy, sample=stale_sample)
    with pytest.raises(profiler.ProfilingTelemetryError, match="cannot descend"):
        profiler.profile_model_envelope(req, FakeAdapter(contexts=[stale]))

    assert calls["write_catalog"] == []


def test_fresh_gpu_total_change_is_hard_telemetry_failure(tmp_path, monkeypatch):
    req = request(tmp_path)
    calls, _ = install_catalog_stubs(monkeypatch, req)
    fresh = fresh_load()
    mismatched = replace(
        fresh,
        sample=replace(fresh.sample, gpu_total_bytes=23 * GIB),
    )

    with pytest.raises(profiler.ProfilingTelemetryError, match="fresh GPU total"):
        profiler.profile_model_envelope(
            req,
            FakeAdapter(fresh_contexts=[mismatched]),
        )

    assert calls["write_catalog"] == []


def test_allocation_failure_releases_model_and_never_writes(tmp_path, monkeypatch):
    req = request(tmp_path)
    calls, _ = install_catalog_stubs(monkeypatch, req)
    adapter = AllocationFailureAdapter()

    result = profiler.profile_model_envelope(req, adapter)

    assert result.status == "safe_failure"
    assert result.reason == "safe_allocation_failure"
    assert result.next_model == "medium"
    assert adapter.release_calls == 1
    assert adapter.resident is False
    assert calls["write_catalog"] == []


def test_unexpected_cycle_failure_releases_model_and_propagates(tmp_path, monkeypatch):
    req = request(tmp_path)
    calls, _ = install_catalog_stubs(monkeypatch, req)
    adapter = UnexpectedFailureAdapter()

    with pytest.raises(LookupError, match="unexpected backend fault"):
        profiler.profile_model_envelope(req, adapter)

    assert adapter.release_calls == 1
    assert adapter.resident is False
    assert calls["write_catalog"] == []


def test_pressure_yield_releases_and_retries_same_run_before_catalog_promotion(
    tmp_path, monkeypatch
):
    req = request(tmp_path)
    calls, _ = install_catalog_stubs(monkeypatch, req)

    class YieldOnceAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.completed = 0

        def measure_cold_cycle(self, **kwargs):
            self.resident = True
            self.measure_calls.append(kwargs)
            kwargs["progress_callback"]()
            measured = self.cycles[self.completed]
            self.completed += 1
            return measured

    controllers = []

    class ScriptedController:
        def __init__(self, *args, **kwargs):
            self.constructor_args = args
            self.constructor_kwargs = kwargs
            self.recovery_reason = "priority_pressure"
            self.check_calls = 0
            self.wait_calls = 0
            self.mark_calls = []
            controllers.append(self)

        def wait_for_recovery(self, *, refuse_unfit_explicit_model=False, heartbeat=None):
            self.wait_calls += 1
            return True

        def immediate_load_admission(self, requirement, *, sample_reader):
            sample = sample_reader()
            return profiler.resource_owner.evaluate_admission(
                requirement,
                sample,
                host_reserve_bytes=GIB,
                gpu_priority_reserve_bytes=4 * GIB,
                require_cgroup=True,
                require_gpu=True,
                expected_gpu_device=GPU,
                now=sample.observed_at,
            )

        def check_or_raise(self, *, force_priority=False):
            assert type(force_priority) is bool
            self.check_calls += 1
            if self.check_calls == 1:
                raise profiler.resource_owner.MemoryPressureYield(
                    "priority pressure"
                )
            return "normal"

        def mark_released(self, reason=None):
            self.mark_calls.append(reason)
            return "recovering"

    monkeypatch.setattr(
        profiler.resource_owner,
        "PressureController",
        ScriptedController,
    )
    adapter = YieldOnceAdapter()

    result = profiler.profile_model_envelope(req, adapter)

    assert result.succeeded is True
    assert len(controllers) == 1
    assert [item["run_number"] for item in adapter.measure_calls] == [1, 1, 2, 3]
    assert adapter.release_calls == 4
    assert adapter.resident is False
    assert controllers[0].mark_calls == ["priority_pressure"]
    assert len(calls["write_catalog"]) == 1


def test_real_controller_priority_assertion_releases_then_recovers_and_retries(
    tmp_path, monkeypatch
):
    req = request(tmp_path, canonical_shared_cuda=True)
    calls, _ = install_catalog_stubs(monkeypatch, req)
    events = []

    class FakeClock:
        def __init__(self):
            self.now = 100.0

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

        def sleep(self, seconds):
            self.advance(seconds)

    clock = FakeClock()

    def priority(state, generation, sequence, *, new_publication=True):
        return profiler.priority_owner.PriorityObservation(
            state=state,
            configured=True,
            observation_digest=f"{sequence:064x}",
            producer_epoch="a" * 32,
            sequence=sequence,
            observed_monotonic_ns=sequence,
            source_generation=generation,
            source_observed_monotonic_ns=sequence,
            reason_codes=("higher_priority_busy",) if state == "asserted" else (),
            accepted=True,
            new_publication=new_publication,
        )

    initial_clears = [
        priority("clear", 1, 1),
        priority("clear", 2, 2),
        priority("clear", 3, 3),
    ]
    repeated_initial_clear = priority(
        "clear", 3, 3, new_publication=False
    )
    asserted = priority("asserted", 4, 4)
    recovery_clears = [
        priority("clear", 5, 5),
        priority("clear", 6, 6),
        priority("clear", 7, 7),
    ]
    repeated_recovery_clear = priority(
        "clear", 7, 7, new_publication=False
    )

    class RealControllerAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.initial_clear_index = 0
            self.assertion_served = False
            self.recovery_clear_index = 0
            self.completed = 0

        def pressure_sample(self):
            sample = self.fresh_contexts[0].sample
            return replace(
                sample,
                observed_at=clock(),
                gpu_observed_at=clock(),
            )

        def fresh_load_inputs(self):
            self.fresh_calls += 1
            return replace(
                self.fresh_contexts[0],
                sample=self.pressure_sample(),
                now=clock(),
            )

        def admission_inputs(self):
            self.admission_calls += 1
            source = self.contexts[0]
            return replace(
                source,
                sample=replace(
                    source.sample,
                    observed_at=clock(),
                    gpu_observed_at=clock(),
                ),
                stabilized_gpu=replace(
                    source.stabilized_gpu,
                    observed_at=clock(),
                ),
                now=clock(),
            )

        def priority_pressure_reader(self):
            def read_priority():
                if self.initial_clear_index < len(initial_clears):
                    observation = initial_clears[self.initial_clear_index]
                    self.initial_clear_index += 1
                elif self.resident and not self.assertion_served:
                    observation = asserted
                    self.assertion_served = True
                elif not self.assertion_served:
                    observation = repeated_initial_clear
                elif self.recovery_clear_index < len(recovery_clears):
                    observation = recovery_clears[self.recovery_clear_index]
                    self.recovery_clear_index += 1
                else:
                    observation = repeated_recovery_clear
                events.append(
                    (
                        "priority",
                        observation.state,
                        observation.source_generation,
                        self.resident,
                    )
                )
                return observation

            return read_priority

        def measure_cold_cycle(self, **kwargs):
            self.resident = True
            self.measure_calls.append(kwargs)
            events.append(("measure", kwargs["run_number"], kwargs["model"]))
            assert calls["write_catalog"] == []
            clock.advance(1.0)
            kwargs["progress_callback"]()
            measured = self.cycles[self.completed]
            self.completed += 1
            events.append(("completed", kwargs["run_number"], kwargs["model"]))
            return measured

        def release_model(self):
            events.append(("release", self.resident))
            super().release_model()

    real_controller = profiler.resource_owner.PressureController
    controllers = []

    def construct_real_controller(*args, **kwargs):
        kwargs["clock"] = clock
        kwargs["sleep"] = clock.sleep
        controller = real_controller(*args, **kwargs)
        controllers.append(controller)
        return controller

    original_write = profiler.catalog_owner.write_catalog

    def record_write(*args, **kwargs):
        events.append(("write_catalog",))
        return original_write(*args, **kwargs)

    monkeypatch.setattr(
        profiler.resource_owner,
        "PressureController",
        construct_real_controller,
    )
    monkeypatch.setattr(profiler.catalog_owner, "write_catalog", record_write)
    adapter = RealControllerAdapter()

    result = profiler.profile_model_envelope(req, adapter)

    assert result.succeeded is True
    assert len(controllers) == 1
    assert [
        (call["run_number"], call["model"]) for call in adapter.measure_calls
    ] == [
        (1, "large-v3"),
        (1, "large-v3"),
        (2, "large-v3"),
        (3, "large-v3"),
    ]
    assertion_index = events.index(("priority", "asserted", 4, True))
    release_index = events.index(("release", True))
    clear_indices = [
        events.index(("priority", "clear", generation, False))
        for generation in (5, 6, 7)
    ]
    assert assertion_index < release_index < min(clear_indices)
    assert clear_indices == sorted(clear_indices)
    assert adapter.completed == 3
    assert events[-1] == ("write_catalog",)
    assert len(calls["write_catalog"]) == 1


def test_unavailable_configured_priority_state_fails_closed_before_any_load(
    tmp_path, monkeypatch
):
    req = request(tmp_path, canonical_shared_cuda=True)
    calls, _ = install_catalog_stubs(monkeypatch, req)
    unavailable = profiler.priority_owner.PriorityObservation(
        state="unavailable",
        configured=True,
    )

    class PriorityAdapter(FakeAdapter):
        def priority_pressure_reader(self):
            return lambda: unavailable

    class FailClosedController:
        def __init__(self, *args, priority_reader=None, **kwargs):
            assert priority_reader is not None
            assert priority_reader() == unavailable

        def wait_for_recovery(self, *, refuse_unfit_explicit_model=False, heartbeat=None):
            raise RuntimeError("configured priority state is unavailable")

    monkeypatch.setattr(
        profiler.resource_owner,
        "PressureController",
        FailClosedController,
    )
    adapter = PriorityAdapter()

    with pytest.raises(RuntimeError, match="priority state is unavailable"):
        profiler.profile_model_envelope(req, adapter)

    assert adapter.fresh_calls == 0
    assert adapter.measure_calls == []
    assert adapter.release_calls == 0
    assert calls["write_catalog"] == []


@pytest.mark.parametrize(
    "condition",
    [
        "clear", "asserted", "unavailable", "repeated_clear", "stale_gpu",
        "missing_gpu", "missing_cgroup", "psi_pressure", "host_pressure",
    ],
)
def test_real_profiler_refuses_unfit_model_only_after_healthy_priority_recovery(
    tmp_path, monkeypatch, condition
):
    req = request(tmp_path, canonical_shared_cuda=True, gpu_reserve_bytes=8 * GIB)
    calls, _ = install_catalog_stubs(monkeypatch, req)

    class BoundedAdapter(FakeAdapter):
        now = 100.0
        sequence = 0

        def sleep(self, seconds):
            self.now += seconds
            if self.now >= 112.0:
                raise TimeoutError("test observation bound")

        def pressure_clock(self):
            return self.now

        def pressure_sample(self):
            changes = dict(
                observed_at=self.now, gpu_observed_at=self.now,
                gpu_free_bytes=18 * GIB,
            )
            if condition == "stale_gpu":
                changes["gpu_observed_at"] = self.now - 60
            elif condition == "missing_gpu":
                changes["gpu_free_bytes"] = None
            elif condition == "missing_cgroup":
                changes["cgroup_current_bytes"] = None
            elif condition == "psi_pressure":
                changes["psi_full_avg10"] = 2.0
            elif condition == "host_pressure":
                changes["host_available_bytes"] = GIB // 2
            return replace(self.fresh_contexts[0].sample, **changes)

        def priority_pressure_reader(self):
            def read():
                self.sequence += 1
                if condition == "unavailable":
                    return profiler.priority_owner.PriorityObservation(
                        state="unavailable", configured=True
                    )
                sequence = 1 if condition == "repeated_clear" else self.sequence
                state = "asserted" if condition == "asserted" else "clear"
                return profiler.priority_owner.PriorityObservation(
                    state=state, configured=True, accepted=True,
                    new_publication=condition != "repeated_clear" or self.sequence == 1,
                    observation_digest=f"{sequence:064x}", producer_epoch="a" * 32,
                    sequence=sequence, observed_monotonic_ns=sequence,
                    source_generation=sequence, source_observed_monotonic_ns=sequence,
                    reason_codes=("higher_priority_busy",) if state == "asserted" else (),
                )
            return read

    adapter = BoundedAdapter()
    real_controller = profiler.resource_owner.PressureController
    controllers = []

    def construct(*args, **kwargs):
        kwargs["sleep"] = adapter.sleep
        controller = real_controller(*args, **kwargs)
        controllers.append(controller)
        return controller

    monkeypatch.setattr(profiler.resource_owner, "PressureController", construct)
    if condition == "clear":
        result = profiler.profile_model_envelope(req, adapter)
        assert result.status == "safe_failure"
        assert result.reason == "insufficient_device"
        assert result.next_model == "medium"
        assert adapter.sequence >= 3
        assert adapter.now < 112.0
    else:
        with pytest.raises(TimeoutError, match="test observation bound"):
            profiler.profile_model_envelope(req, adapter)
    assert not controllers[0].admission_open
    assert adapter.measure_calls == []
    assert not adapter.resident
    assert calls["write_catalog"] == []


def test_bounded_cuda_bootstrap_is_an_estimate_not_transferred_evidence(tmp_path):
    policy = replace(sample_policy(), chunk_minutes=5,
                     decoder_options_sha256=profiler._decoder_options_digest({}))
    req = request(tmp_path, policy=policy,
                  bootstrap_profile=profiler.resource_owner.CUDA_CALIBRATION_PROFILE,
                  host_reserve_bytes=4 * GIB, gpu_reserve_bytes=8 * GIB)
    context = healthy_admission(cgroup_limit=17 * GIB)
    context = replace(context, sample=replace(context.sample,
        host_available_bytes=12 * GIB, gpu_free_bytes=18 * GIB))
    decision = profiler._explicit_admission_decision(req, context)
    assert decision.admitted
    assert decision.requirement.required_host_bytes == 7 * GIB + 512 * MIB
    assert decision.requirement.required_device_bytes == 8 * GIB
    assert decision.requirement.provenance == "fallback"
    assert not decision.requirement.exact_match
    generic = profiler._explicit_admission_decision(
        replace(req, bootstrap_profile="generic"), context
    )
    assert not generic.admitted
    assert generic.requirement.required_host_bytes == 9 * GIB + 512 * MIB
    assert generic.requirement.required_device_bytes == 13 * GIB


@pytest.mark.parametrize("change", [
    {"chunk_minutes": 10}, {"compute_type": "float32"},
    {"task": "transcribe"}, {"decoder_options_sha256": SHA_C},
])
def test_bounded_cuda_bootstrap_rejects_unqualified_policy(tmp_path, change):
    policy = replace(sample_policy(), chunk_minutes=5,
                     decoder_options_sha256=profiler._decoder_options_digest({}))
    with pytest.raises(ValueError, match="Bounded CUDA bootstrap requires"):
        request(tmp_path, policy=replace(policy, **change),
                bootstrap_profile=profiler.resource_owner.CUDA_CALIBRATION_PROFILE)


def test_calibration_estimate_cannot_leak_into_cpu_or_auto_selection():
    for model, device in [("auto", "cuda"), ("large-v3", "cpu"), ("medium", "cuda")]:
        with pytest.raises(ValueError, match="Calibration fallback requires"):
            select_model(model, 24 * GIB, device=device,
                         fallback_profile=profiler.resource_owner.CUDA_CALIBRATION_PROFILE)
    generic = profiler.resource_owner.model_load_requirement("large-v3")
    with pytest.raises(ValueError, match="source evidence"):
        replace(generic, host_incremental_bytes=7 * GIB)


def test_runtime_recovery_does_not_opt_into_profiler_capacity_refusal(tmp_path):
    req = request(tmp_path, gpu_reserve_bytes=8 * GIB)
    context = healthy_admission()
    sample = replace(context.sample, gpu_free_bytes=18 * GIB)
    adapter = FakeAdapter(fresh_contexts=[replace(fresh_load(), sample=sample)])
    controller = profiler._build_pressure_controller(
        req, adapter, context, profiler.resource_owner.model_load_requirement("large-v3")
    )
    controller.mark_released("resource_pressure")
    polls = []

    def sleep(_seconds):
        polls.append(True)

    controller._sleep = sleep
    assert controller.wait_for_recovery(cancelled=lambda: len(polls) >= 4) is False
    assert len(polls) == 4
    assert not controller.admission_open


def test_real_controller_consumes_unavailable_priority_and_keeps_admission_closed():
    initial = healthy_admission()
    req = request(
        Path("C:/profiler-test"),
        expected_uid=None,
        canonical_shared_cuda=True,
    )
    decision = profiler._explicit_admission_decision(req, initial)
    unavailable = profiler.priority_owner.PriorityObservation(
        state="unavailable",
        configured=True,
    )

    class PriorityAdapter(FakeAdapter):
        def priority_pressure_reader(self):
            return lambda: unavailable

    controller = profiler._build_pressure_controller(
        req,
        PriorityAdapter(),
        initial,
        decision.requirement,
    )

    assert controller.poll_priority(model_resident=False) == controller.RECOVERING
    admission = controller.immediate_load_admission(
        decision.requirement,
        sample_reader=lambda: initial.sample,
    )
    assert admission.admitted is False
    assert admission.reasons == ("controller_recovering",)
    assert controller.admission_open is False


def test_packaged_adapter_retains_resident_handle_when_backend_unload_fails():
    class BrokenBackend:
        def unload_model(self):
            raise RuntimeError("unload failed")

    adapter = object.__new__(profiler.StableWhisperMeasurementAdapter)
    resident = SimpleNamespace(model=BrokenBackend())
    adapter._model = resident
    adapter.device = "cpu"

    with pytest.raises(RuntimeError, match="unload failed"):
        adapter.release_model()

    assert adapter._model is resident
    with pytest.raises(RuntimeError, match="release has not been verified"):
        adapter.assert_released()


def test_packaged_adapter_polls_priority_while_gpu_capacity_stabilizes(
    tmp_path, monkeypatch
):
    adapter, _media, _policy, sample = packaged_adapter(
        tmp_path,
        monkeypatch,
        loader=lambda *_args, **_kwargs: object(),
        priority_watch_interval_seconds=1.0,
    )
    observations = [
        profiler.priority_owner.PriorityObservation(
            state="clear",
            configured=True,
            accepted=True,
            new_publication=True,
            source_generation=1,
        ),
        profiler.priority_owner.PriorityObservation(
            state="asserted",
            configured=True,
            accepted=True,
            new_publication=True,
            source_generation=2,
        ),
        *[
            profiler.priority_owner.PriorityObservation(
                state="clear",
                configured=True,
                accepted=True,
                new_publication=True,
                source_generation=generation,
            )
            for generation in range(3, 12)
        ],
    ]

    class PriorityReader:
        configured = True

        def __init__(self):
            self.reads = 0

        def read(self):
            observation = observations[min(self.reads, len(observations) - 1)]
            self.reads += 1
            return observation

    priority_reader = PriorityReader()
    adapter._priority_pressure_reader = priority_reader
    sleeps = []
    monkeypatch.setattr(profiler.time, "sleep", sleeps.append)

    def stabilize(sample_reader, **kwargs):
        sampled = sample_reader()
        kwargs["sleep"](5.0)
        kwargs["sleep"](5.0)
        return profiler.resource_owner.StabilizedGpuCapacity(
            device_id=sampled.gpu_device_id,
            total_bytes=sampled.gpu_total_bytes,
            free_bytes=sampled.gpu_free_bytes,
            observed_at=sampled.gpu_observed_at,
            sample_count=3,
        )

    monkeypatch.setattr(
        profiler.resource_owner,
        "stabilize_gpu_capacity",
        stabilize,
    )

    admission = adapter.admission_inputs()
    read_priority = adapter.priority_pressure_reader()

    assert admission.stabilized_gpu is not None
    assert priority_reader.reads == 10
    assert sleeps == [1.0] * 10
    assert read_priority is not None
    assert read_priority().state == "asserted"
    assert read_priority().state == "clear"


def test_packaged_adapter_pins_revision_profiles_one_working_chunk_and_verifies_release(
    tmp_path, monkeypatch
):
    calls = []

    class Backend:
        model_is_loaded = True

        def unload_model(self):
            self.model_is_loaded = False

    class LoadedModel:
        def __init__(self):
            self.model = Backend()

        def transcribe(self, *args, **kwargs):
            calls.append(("transcribe", args, kwargs))
            kwargs["progress_callback"](1, 2)
            return object()

    loaded = LoadedModel()

    def loader(model, **kwargs):
        calls.append(("load", model, kwargs))
        return loaded

    properties = SimpleNamespace(name="NVIDIA GeForce RTX 3090", total_memory=24 * GIB)
    cuda = SimpleNamespace(
        is_available=lambda: True,
        get_device_properties=lambda _index: properties,
        get_device_capability=lambda _index: (8, 6),
        synchronize=lambda _index: None,
        empty_cache=lambda: None,
        mem_get_info=lambda _index: (20 * GIB, 24 * GIB),
    )
    modules = {
        "stable_whisper": SimpleNamespace(
            __version__="2.19.1",
            load_faster_whisper=loader,
        ),
        "faster_whisper": SimpleNamespace(__version__="1.2.0"),
        "ctranslate2": SimpleNamespace(__version__="4.6.0"),
        "torch": SimpleNamespace(cuda=cuda, version=SimpleNamespace(cuda="12.8")),
    }
    monkeypatch.setattr(
        profiler.importlib,
        "import_module",
        lambda name: modules[name],
    )
    adapter = profiler.StableWhisperMeasurementAdapter(
        model="large-v3",
        model_revision=REVISION,
        model_path=tmp_path / "models",
        device="cuda",
        compute_type="float16",
        cpu_threads=4,
        decoder_options={},
        sample_interval_seconds=0.001,
        gpu_stabilization_interval_seconds=0.001,
    )
    sample = replace(
        healthy_admission().sample,
        gpu_device_id=adapter._expected_gpu_device,
    )
    monkeypatch.setattr(adapter, "_pressure_sample", lambda: sample)
    monkeypatch.setattr(
        adapter,
        "_media_duration_seconds",
        lambda _path: 20 * 60 + 10,
    )
    media = tmp_path / "profile.wav"
    media.write_bytes(b"disposable")
    adapter.validate_workload(media_path=media, policy=sample_policy())

    measured = adapter.measure_cold_cycle(
        model="large-v3",
        media_path=media,
        policy=replace(
            sample_policy(),
            decoder_options_sha256=profiler._decoder_options_digest({}),
        ),
        admitted_load=replace(
            fresh_load(),
            sample=sample,
        ),
        run_number=1,
        progress_callback=lambda *_args, **_kwargs: None,
    )

    load_call = next(call for call in calls if call[0] == "load")
    assert load_call[2]["revision"] == "d" * 40
    assert load_call[2]["num_workers"] == 1
    transcribe_call = next(call for call in calls if call[0] == "transcribe")
    assert callable(transcribe_call[2]["progress_callback"])
    assert measured.preload == measured.peak
    with pytest.raises(RuntimeError, match="release has not been verified"):
        adapter.assert_released()
    adapter.release_model()
    adapter.assert_released()
    assert loaded.model.model_is_loaded is False


def test_packaged_adapter_preserves_wrapped_pressure_yield_and_retries_same_run(
    tmp_path, monkeypatch
):
    events = []
    loaded_models = []

    class Backend:
        model_is_loaded = True

        def unload_model(self):
            self.model_is_loaded = False
            events.append("unload")

    class LoadedModel:
        def __init__(self):
            self.model = Backend()

        def transcribe(self, *args, **kwargs):
            events.append("transcribe")
            try:
                kwargs["progress_callback"](1, 2)
            except profiler.resource_owner.MemoryPressureYield as exc:
                events.append("wrapped-pressure")
                raise RuntimeError("stable-ts wrapped callback") from exc
            events.append("completed")
            return object()

    def loader(*args, **kwargs):
        loaded = LoadedModel()
        loaded_models.append(loaded)
        return loaded

    adapter, media, policy, sample = packaged_adapter(
        tmp_path,
        monkeypatch,
        loader=loader,
        priority_watch_interval_seconds=1.0,
    )
    req = request(tmp_path, policy=policy)
    calls, _ = install_catalog_stubs(monkeypatch, req)
    monkeypatch.setattr(adapter, "runtime_identity", sample_runtime)
    monkeypatch.setattr(adapter, "admission_inputs", healthy_admission)
    monkeypatch.setattr(adapter, "fresh_load_inputs", fresh_load)
    monkeypatch.setattr(adapter, "pressure_sample", lambda: sample)
    monkeypatch.setattr(adapter, "pressure_clock", lambda: sample.observed_at)
    peak_sample = replace(
        sample,
        host_available_bytes=sample.host_available_bytes - GIB,
        cgroup_current_bytes=sample.cgroup_current_bytes + GIB,
        gpu_free_bytes=sample.gpu_free_bytes - GIB,
    )
    monkeypatch.setattr(adapter, "_pressure_sample", lambda: peak_sample)
    req = replace(req, media_path=media)

    class Controller:
        def __init__(self, *args, **kwargs):
            self.recovery_reason = "priority_pressure"
            self.yielded = False

        def wait_for_recovery(self, *, refuse_unfit_explicit_model=False, heartbeat=None):
            return True

        def immediate_load_admission(self, requirement, *, sample_reader):
            return controller_admission(requirement, sample_reader())

        def check_or_raise(self, *, force_priority=False):
            if not force_priority and not self.yielded:
                self.yielded = True
                raise profiler.resource_owner.MemoryPressureYield(
                    "priority pressure"
                )
            return "normal"

        def mark_released(self, reason=None):
            events.append("recover")
            self.recovery_reason = reason
            return "recovering"

    original_write = profiler.catalog_owner.write_catalog

    def record_write(*args, **kwargs):
        events.append("write-catalog")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(profiler.resource_owner, "PressureController", Controller)
    monkeypatch.setattr(profiler.catalog_owner, "write_catalog", record_write)

    result = profiler.profile_model_envelope(req, adapter)

    assert result.succeeded is True
    assert events.count("wrapped-pressure") == 1
    assert events.count("completed") == 3
    assert events.count("unload") == 4
    assert len(loaded_models) == 4
    assert all(model.model.model_is_loaded is False for model in loaded_models)
    assert events.index("write-catalog") > max(
        index for index, event in enumerate(events) if event == "completed"
    )
    assert len(calls["write_catalog"]) == 1


@pytest.mark.parametrize("assertion_point", ["after_load", "after_transcribe"])
def test_packaged_adapter_forces_priority_poll_around_transcription_boundaries(
    tmp_path, monkeypatch, assertion_point
):
    transcribe_calls = []
    loaded_models = []

    class Backend:
        model_is_loaded = True

        def unload_model(self):
            self.model_is_loaded = False

    class LoadedModel:
        def __init__(self):
            self.model = Backend()

        def transcribe(self, *args, **kwargs):
            transcribe_calls.append(True)
            return object()

    def loader(*args, **kwargs):
        loaded = LoadedModel()
        loaded_models.append(loaded)
        return loaded

    adapter, media, policy, sample = packaged_adapter(
        tmp_path,
        monkeypatch,
        loader=loader,
        priority_watch_interval_seconds=1.0,
    )
    adapter.validate_workload(media_path=media, policy=policy)
    forced_calls = 0

    def pressure_callback(*args, **kwargs):
        nonlocal forced_calls
        if kwargs.get("_subgen_force_priority_poll"):
            forced_calls += 1
            target = 1 if assertion_point == "after_load" else 2
            if forced_calls == target:
                raise profiler.resource_owner.MemoryPressureYield(assertion_point)

    with pytest.raises(
        profiler.resource_owner.MemoryPressureYield,
        match=assertion_point,
    ):
        adapter.measure_cold_cycle(
            model="large-v3",
            media_path=media,
            policy=policy,
            admitted_load=replace(fresh_load(), sample=sample),
            run_number=1,
            progress_callback=pressure_callback,
        )

    assert bool(transcribe_calls) is (assertion_point == "after_transcribe")
    adapter.release_model()
    adapter.assert_released()
    assert loaded_models[0].model.model_is_loaded is False


def test_packaged_adapter_load_failure_checks_priority_before_safe_descent(
    tmp_path, monkeypatch
):
    def loader(*args, **kwargs):
        raise MemoryError("load allocation failed")

    adapter, media, policy, sample = packaged_adapter(
        tmp_path,
        monkeypatch,
        loader=loader,
        priority_watch_interval_seconds=1.0,
    )
    adapter.validate_workload(media_path=media, policy=policy)
    forced_calls = []

    def pressure_callback(*args, **kwargs):
        if kwargs.get("_subgen_force_priority_poll"):
            forced_calls.append(True)
            raise profiler.resource_owner.MemoryPressureYield("load-time priority")

    with pytest.raises(
        profiler.resource_owner.MemoryPressureYield,
        match="load-time priority",
    ):
        adapter.measure_cold_cycle(
            model="large-v3",
            media_path=media,
            policy=policy,
            admitted_load=replace(fresh_load(), sample=sample),
            run_number=1,
            progress_callback=pressure_callback,
        )

    assert forced_calls == [True]
    adapter.release_model()
    adapter.assert_released()


def test_packaged_adapter_priority_watcher_latches_between_callbacks(
    tmp_path, monkeypatch
):
    watcher_calls = []

    class Backend:
        model_is_loaded = True

        def unload_model(self):
            self.model_is_loaded = False

    class LoadedModel:
        def __init__(self):
            self.model = Backend()

        def transcribe(self, *args, **kwargs):
            time.sleep(0.02)
            return object()

    loaded = LoadedModel()
    adapter, media, policy, sample = packaged_adapter(
        tmp_path,
        monkeypatch,
        loader=lambda *args, **kwargs: loaded,
    )
    adapter.validate_workload(media_path=media, policy=policy)

    def pressure_callback(*args, **kwargs):
        if threading.current_thread().name.endswith("priority-watcher"):
            watcher_calls.append(True)
            raise profiler.resource_owner.MemoryPressureYield("watcher pressure")

    with pytest.raises(
        profiler.resource_owner.MemoryPressureYield,
        match="watcher pressure",
    ):
        adapter.measure_cold_cycle(
            model="large-v3",
            media_path=media,
            policy=policy,
            admitted_load=replace(fresh_load(), sample=sample),
            run_number=1,
            progress_callback=pressure_callback,
        )

    assert watcher_calls
    adapter.release_model()
    adapter.assert_released()


def test_sampler_telemetry_loss_dominates_concurrent_allocation_failure(
    tmp_path, monkeypatch
):
    class Backend:
        model_is_loaded = True

        def unload_model(self):
            self.model_is_loaded = False

    class LoadedModel:
        def __init__(self):
            self.model = Backend()

        def transcribe(self, *args, **kwargs):
            time.sleep(0.02)
            raise MemoryError("allocation failed too")

    loaded = LoadedModel()
    adapter, media, policy, sample = packaged_adapter(
        tmp_path,
        monkeypatch,
        loader=lambda *args, **kwargs: loaded,
        priority_watch_interval_seconds=1.0,
    )
    adapter.validate_workload(media_path=media, policy=policy)

    def pressure_sample():
        if threading.current_thread().name.endswith("profiler-sampler"):
            raise LookupError("telemetry disappeared")
        return sample

    monkeypatch.setattr(adapter, "_pressure_sample", pressure_sample)

    with pytest.raises(
        profiler.ProfilingTelemetryError,
        match="memory sampler lost required telemetry",
    ):
        adapter.measure_cold_cycle(
            model="large-v3",
            media_path=media,
            policy=policy,
            admitted_load=replace(fresh_load(), sample=sample),
            run_number=1,
            progress_callback=lambda *args, **kwargs: None,
        )

    adapter.release_model()
    adapter.assert_released()


def test_packaged_adapter_rejects_media_that_is_not_one_bounded_working_chunk(
    tmp_path,
):
    adapter = object.__new__(profiler.StableWhisperMeasurementAdapter)
    adapter._media_duration_seconds = lambda _path: 60.0

    with pytest.raises(profiler.ProfilingTelemetryError, match="working chunk"):
        adapter._validate_media_duration(tmp_path / "short.wav", 20)


def test_same_exact_key_is_replaced_once_while_unrelated_entries_are_preserved(
    tmp_path, monkeypatch
):
    req = request(tmp_path)
    old_exact = ModelEnvelope(
        sample_identity(),
        sample_runtime(),
        sample_policy("large-v3"),
        sample_measurements(host_incremental=6 * GIB),
    )
    unrelated = sample_envelope("medium")
    current = build_catalog(catalog_version=7, entries=(old_exact, unrelated))
    _, staged = install_catalog_stubs(monkeypatch, req, input_catalog=current)

    result = profiler.profile_model_envelope(req, FakeAdapter())

    assert result.catalog_version == 8
    assert result.replaced_existing is True
    entries = staged["catalog"].entries
    assert entries == (unrelated, result.envelope)
    matching = [
        entry
        for entry in entries
        if entry.image_identity == sample_identity()
        and entry.runtime == sample_runtime()
        and entry.policy == sample_policy("large-v3")
    ]
    assert matching == [result.envelope]
    assert matching[0].measurements != old_exact.measurements


def test_model_descent_is_deterministic_and_stops_after_tiny():
    assert [profiler.next_lower_model(model) for model in profiler.MODEL_DESCENT] == [
        "medium",
        "small",
        "base",
        "tiny",
        None,
    ]
    with pytest.raises(ValueError, match="unknown"):
        profiler.next_lower_model("custom")


@pytest.mark.parametrize(
    ("model", "prior"),
    [
        ("medium", "large-v3"),
        ("small", "medium"),
        ("base", "small"),
        ("tiny", "base"),
    ],
)
def test_lower_candidates_require_the_immediately_prior_clean_process_failure(
    tmp_path, model, prior
):
    accepted = request(
        tmp_path,
        model=model,
        policy=sample_policy(model),
        prior_safe_failure_model=prior,
    )
    assert accepted.prior_safe_failure_model == prior
    with pytest.raises(ValueError, match="requires a clean-process safe failure"):
        request(
            tmp_path,
            model=model,
            policy=sample_policy(model),
            prior_safe_failure_model=None,
        )


def test_large_v3_rejects_a_fabricated_prior_failure(tmp_path):
    with pytest.raises(ValueError, match="first profiler candidate"):
        request(tmp_path, prior_safe_failure_model="medium")


def test_profiled_12_gib_evidence_does_not_authorize_fresh_10_gib_auto_selection(
    tmp_path, monkeypatch
):
    large_cycles = (
        cycle(GIB, 10 * GIB, GIB, 10 * GIB, 6 * GIB, 15 * GIB),
        cycle(GIB, 9 * GIB, GIB, 9 * GIB, 6 * GIB, 14 * GIB),
        cycle(GIB, 8 * GIB, GIB, 8 * GIB, 6 * GIB, 13 * GIB),
    )
    req = request(tmp_path, host_margin_bytes=512 * MIB)
    install_catalog_stubs(monkeypatch, req)
    profiled_large = profiler.profile_model_envelope(
        req, FakeAdapter(cycles=large_cycles)
    ).envelope
    now = 200.0
    final_sample = PressureSample(
        observed_at=now,
        host_available_bytes=32 * GIB,
        host_total_bytes=64 * GIB,
        cgroup_current_bytes=GIB,
        cgroup_limit_bytes=10 * GIB,
        gpu_total_bytes=24 * GIB,
        gpu_free_bytes=20 * GIB,
        gpu_device_id=GPU,
        gpu_observed_at=now,
    )

    large_only = (
        EnvelopeResolution(profiled_large, EnvelopeDisposition.EXACT_MATCH, None),
    )
    large_rejected = select_model(
        "auto",
        CapacityProfile(10 * GIB, 64 * GIB, 10 * GIB, "cgroup_v2", 2),
        device="cuda",
        admission_sample=final_sample,
        stabilized_gpu=StabilizedGpuCapacity(GPU, 24 * GIB, 20 * GIB, now, 3),
        host_reserve=GIB,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device=GPU,
        envelopes=large_only,
        canonical_shared_cuda=True,
        require_cgroup=True,
        now=now,
    )

    assert profiled_large.measurements.cgroup_incremental_peak_bytes == 9 * GIB
    assert large_rejected.admitted is False
    assert large_rejected.selected_model is None

    # Task 11 host orchestration owns this clean-process handoff: it observes the
    # exit-3 result, destroys the large-v3 profiler, and passes only the named
    # predecessor to the next isolated process.  These distinct requests and
    # adapters model that ordering without writing a safe-failure artifact.
    large_catalog = build_catalog(catalog_version=2, entries=(profiled_large,))
    failure_req = request(
        tmp_path / "large-10g-safe-failure",
        host_margin_bytes=512 * MIB,
    )
    failure_calls, _ = install_catalog_stubs(
        monkeypatch,
        failure_req,
        input_catalog=large_catalog,
    )
    safe_failure = profiler.profile_model_envelope(
        failure_req,
        FakeAdapter(
            contexts=[healthy_admission(cgroup_limit=10 * GIB, cgroup_current=GIB)]
        ),
    )
    assert safe_failure.status == "safe_failure"
    assert safe_failure.model == "large-v3"
    assert safe_failure.next_model == "medium"
    assert failure_calls["write_catalog"] == []

    medium_cycles = (
        cycle(GIB, 5 * GIB, GIB, 5 * GIB, 6 * GIB, 11 * GIB),
        cycle(GIB, 6 * GIB, GIB, 6 * GIB, 6 * GIB, 13 * GIB),
        cycle(2 * GIB, 6 * GIB, 2 * GIB, 6 * GIB, 7 * GIB, 13 * GIB),
    )
    medium_req = request(
        tmp_path / "medium-process",
        model=safe_failure.next_model,
        policy=sample_policy(safe_failure.next_model),
        prior_safe_failure_model=safe_failure.model,
        host_margin_bytes=512 * MIB,
    )
    install_catalog_stubs(monkeypatch, medium_req, input_catalog=large_catalog)
    medium = profiler.profile_model_envelope(
        medium_req,
        FakeAdapter(cycles=medium_cycles),
    ).envelope
    envelopes = (
        large_only[0],
        EnvelopeResolution(medium, EnvelopeDisposition.EXACT_MATCH, None),
    )
    decision = select_model(
        "auto",
        CapacityProfile(10 * GIB, 64 * GIB, 10 * GIB, "cgroup_v2", 2),
        device="cuda",
        admission_sample=final_sample,
        stabilized_gpu=StabilizedGpuCapacity(GPU, 24 * GIB, 20 * GIB, now, 3),
        host_reserve=GIB,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device=GPU,
        envelopes=envelopes,
        canonical_shared_cuda=True,
        require_cgroup=True,
        now=now,
    )
    assert decision.admitted is True
    assert decision.selected_model == "medium"
    assert decision.requirement.envelope_resolution.envelope is medium

    stale_sample = replace(
        final_sample,
        observed_at=now - 11,
        gpu_observed_at=now - 11,
    )
    stale_decision = select_model(
        "auto",
        CapacityProfile(10 * GIB, 64 * GIB, 10 * GIB, "cgroup_v2", 2),
        device="cuda",
        admission_sample=stale_sample,
        stabilized_gpu=StabilizedGpuCapacity(
            GPU,
            24 * GIB,
            20 * GIB,
            now - 11,
            3,
        ),
        host_reserve=GIB,
        gpu_reserve_bytes=4 * GIB,
        expected_gpu_device=GPU,
        envelopes=envelopes,
        canonical_shared_cuda=True,
        require_cgroup=True,
        now=now,
    )
    assert stale_decision.admitted is False
    assert stale_decision.selected_model is None


def test_profiler_imports_no_scanner_or_runtime_facade_and_contains_no_build_path():
    source = Path(profiler.__file__).read_text(encoding="utf-8")
    assert "import subgen_override" not in source
    assert "import subgen\n" not in source
    assert "docker build" not in source.casefold()
    assert "transcribe_existing" not in source
    assert "profile_model_envelope(" not in Path(
        profiler.catalog_owner.__file__
    ).read_text(encoding="utf-8")


def test_default_adapter_consumes_configured_priority_path(monkeypatch, tmp_path):
    configured = str((tmp_path / "priority.json").resolve())
    captured = {}

    def construct(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setenv("PRIORITY_PRESSURE_FILE", configured)
    monkeypatch.setattr(profiler, "StableWhisperMeasurementAdapter", construct)
    args = SimpleNamespace(
        model="large-v3",
        model_revision=REVISION,
        model_path=tmp_path / "models",
        device="cuda",
        compute_type="float16",
        cpu_threads=4,
    )

    profiler._default_adapter_factory(args, {})

    assert captured["priority_pressure_path"] == configured


def test_decoder_hash_is_canonical_and_revision_must_be_immutable():
    left = profiler._parse_decoder_options("{'beam_size': 5, 'vad': True}")
    right = profiler._parse_decoder_options("{'vad': True, 'beam_size': 5}")
    assert profiler._decoder_options_digest(left) == profiler._decoder_options_digest(
        right
    )
    assert profiler._normalized_revision("d" * 40) == REVISION
    assert profiler._normalized_revision(REVISION) == REVISION
    with pytest.raises(ValueError, match="immutable"):
        profiler._normalized_revision("main")
    with pytest.raises(ValueError, match="canonical JSON"):
        profiler._parse_decoder_options("{'invalid': {1, 2}}")
    assert profiler._chunk_setting("auto") == "auto"
    with pytest.raises(profiler.argparse.ArgumentTypeError, match="at least one byte"):
        profiler._positive_gib("1e-20")


def test_cli_usage_errors_are_fatal_and_never_emit_safe_failure_json(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("MODEL_ENVELOPE_HOST_MARGIN_MIB", raising=False)
    monkeypatch.delenv("MODEL_ENVELOPE_DEVICE_MARGIN_MIB", raising=False)
    arguments = [
        "--catalog-input",
        str(tmp_path / "catalog.json"),
        "--catalog-output",
        str(tmp_path / "staged.json"),
        "--identity",
        str(tmp_path / "identity.json"),
        "--media",
        str(tmp_path / "media.wav"),
        "--model",
        "large-v3",
        "--model-revision",
        "d" * 40,
    ]
    assert profiler.main(arguments, adapter_factory=lambda *_: FakeAdapter()) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert not (tmp_path / "staged.json").exists()


def test_cli_canonical_shared_cuda_rejects_missing_signal_or_disabled_yield(
    tmp_path, monkeypatch, capsys
):
    arguments = [
        "--catalog-input",
        str(tmp_path / "catalog.json"),
        "--catalog-output",
        str(tmp_path / "staged.json"),
        "--identity",
        str(tmp_path / "identity.json"),
        "--media",
        str(tmp_path / "media.wav"),
        "--model",
        "large-v3",
        "--model-revision",
        "d" * 40,
        "--host-margin-mib",
        "768",
        "--device-margin-mib",
        "2048",
        "--host-reserve-gib",
        "1",
        "--gpu-reserve-gib",
        "4",
        "--canonical-shared-cuda",
    ]
    factory_calls = []

    def factory(*args):
        factory_calls.append(args)
        return FakeAdapter()

    monkeypatch.delenv("PRIORITY_PRESSURE_FILE", raising=False)
    monkeypatch.setenv("MEMORY_PRESSURE_YIELD", "True")
    assert profiler.main(arguments, adapter_factory=factory) == 1
    assert "PRIORITY_PRESSURE_FILE" in capsys.readouterr().err
    assert factory_calls == []

    monkeypatch.setenv(
        "PRIORITY_PRESSURE_FILE",
        str((tmp_path / "priority.json").resolve()),
    )
    monkeypatch.setenv("MEMORY_PRESSURE_YIELD", "False")
    assert profiler.main(arguments, adapter_factory=factory) == 1
    assert "MEMORY_PRESSURE_YIELD" in capsys.readouterr().err
    assert factory_calls == []

    ordinary_arguments = [
        argument
        for argument in arguments
        if argument != "--canonical-shared-cuda"
    ]
    assert profiler.main(ordinary_arguments, adapter_factory=factory) == 1
    assert "MEMORY_PRESSURE_YIELD" in capsys.readouterr().err
    assert factory_calls == []

    monkeypatch.setenv("PRIORITY_PRESSURE_FILE", "")
    profiler._validate_cli_pressure_contract(
        SimpleNamespace(canonical_shared_cuda=False)
    )

    invalid = list(arguments)
    invalid[invalid.index("large-v3")] = "not-a-model"
    assert profiler.main(invalid, adapter_factory=lambda *_: FakeAdapter()) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert not (tmp_path / "staged.json").exists()


def test_cli_model_specific_admission_failure_has_unique_safe_exit_and_json(
    tmp_path, monkeypatch, capsys
):
    arguments = [
        "--catalog-input",
        str(tmp_path / "catalog.json"),
        "--catalog-output",
        str(tmp_path / "staged.json"),
        "--identity",
        str(tmp_path / "identity.json"),
        "--media",
        str(tmp_path / "media.wav"),
        "--model",
        "large-v3",
        "--model-revision",
        "d" * 40,
        "--host-margin-mib",
        "768",
        "--device-margin-mib",
        "2048",
        "--host-reserve-gib",
        "1",
        "--gpu-reserve-gib",
        "4",
    ]
    parsed_request = request(
        tmp_path,
        catalog_input=tmp_path / "catalog.json",
        catalog_output=tmp_path / "staged.json",
        identity_path=tmp_path / "identity.json",
        media_path=tmp_path / "media.wav",
        expected_uid=profiler._current_uid(),
    )
    calls, _ = install_catalog_stubs(monkeypatch, parsed_request)
    adapter = FakeAdapter(
        contexts=[healthy_admission(cgroup_limit=4 * GIB, cgroup_current=3 * GIB)]
    )

    exit_code = profiler.main(arguments, adapter_factory=lambda *_: adapter)

    assert exit_code == profiler.SAFE_FAILURE_EXIT == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "safe_failure"
    assert payload["model"] == "large-v3"
    assert payload["next_model"] == "medium"
    assert calls["write_catalog"] == []


def test_cli_with_injected_adapter_returns_profiled_json(tmp_path, monkeypatch, capsys):
    arguments = [
        "--catalog-input",
        str(tmp_path / "catalog.json"),
        "--catalog-output",
        str(tmp_path / "staged.json"),
        "--identity",
        str(tmp_path / "identity.json"),
        "--media",
        str(tmp_path / "media.wav"),
        "--model",
        "large-v3",
        "--model-revision",
        "d" * 40,
        "--host-margin-mib",
        "768",
        "--device-margin-mib",
        "2048",
        "--host-reserve-gib",
        "1",
        "--gpu-reserve-gib",
        "4",
    ]
    parsed_request = request(
        tmp_path,
        catalog_input=tmp_path / "catalog.json",
        catalog_output=tmp_path / "staged.json",
        identity_path=tmp_path / "identity.json",
        media_path=tmp_path / "media.wav",
        expected_uid=profiler._current_uid(),
    )
    install_catalog_stubs(monkeypatch, parsed_request)
    monkeypatch.setenv("SEGMENTATION_CHUNK_MINUTES", "auto")
    monkeypatch.setattr(
        profiler.resource_owner,
        "discover_capacity",
        lambda: CapacityProfile(12 * GIB, 64 * GIB, 12 * GIB, "cgroup_v2", 2),
    )
    adapter = FakeAdapter()

    result = profiler.main(arguments, adapter_factory=lambda *_: adapter)

    assert result == 0
    assert adapter.validated_workload["policy"].chunk_minutes == 20
    assert '"catalog_version":2' in capsys.readouterr().out
