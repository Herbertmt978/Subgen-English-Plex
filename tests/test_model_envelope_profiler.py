from dataclasses import replace
import json
from pathlib import Path
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
        "canonical_shared_cuda": True,
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
    assert adapter.admission_calls == 1
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


def test_initial_admission_failure_is_safe_writes_nothing_and_names_next_model(
    tmp_path, monkeypatch
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


def test_every_actual_cycle_gets_a_fresh_resource_owner_admission(
    tmp_path, monkeypatch
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
    assert adapter.admission_calls == 1
    assert adapter.fresh_calls == 2
    assert len(adapter.measure_calls) == 1
    assert adapter.release_calls == 1
    assert calls["write_catalog"] == []


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
    )

    load_call = next(call for call in calls if call[0] == "load")
    assert load_call[2]["revision"] == "d" * 40
    assert load_call[2]["num_workers"] == 1
    assert measured.preload == measured.peak
    with pytest.raises(RuntimeError, match="release has not been verified"):
        adapter.assert_released()
    adapter.release_model()
    adapter.assert_released()
    assert loaded.model.model_is_loaded is False


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
        "--canonical-shared-cuda",
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
        "--canonical-shared-cuda",
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
