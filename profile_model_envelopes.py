"""Owner-operated, isolated ModelEnvelope profiler.

This module is deliberately separate from Subgen's scanner and worker entry
points.  One invocation profiles one explicit model.  Admission, paired peak
arithmetic, artifact validation, and canonical writing remain owned by the
focused ``subgen_core`` modules.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import gc
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Mapping, Optional, Protocol, Sequence

from subgen_core import model_envelope_catalog as catalog_owner
from subgen_core import resource_management as resource_owner
from subgen_core.model_envelope_catalog import (
    EnvelopeMeasurements,
    EnvelopePolicy,
    ModelEnvelope,
    RuntimeIdentity,
)


MODEL_DESCENT = ("large-v3", "medium", "small", "base", "tiny")
MINIMUM_COLD_CYCLES = 3
SAFE_FAILURE_EXIT = 3
WORKING_OVERLAP_SECONDS = 5
MEDIA_DURATION_TOLERANCE_SECONDS = 1.0
_SAFE_DESCENT_REASONS = frozenset({"insufficient_host", "insufficient_device"})
_MAX_BYTES = (1 << 63) - 1
_HF_REVISION = re.compile(r"(?:hf:)?([0-9a-f]{40})\Z")


class SafeProfilingFailure(RuntimeError):
    """An admission/allocation failure that permits a clean lower-model run."""


class ProfilingTelemetryError(RuntimeError):
    """Required profiling telemetry was unavailable or inconsistent."""


@dataclass(frozen=True)
class ResourceUsage:
    """One simultaneous host, cgroup, and device used-memory observation."""

    host_used_bytes: int
    cgroup_used_bytes: int
    device_used_bytes: int

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            _require_positive_bytes(getattr(self, name), name)


@dataclass(frozen=True)
class ColdCycleMeasurement:
    """Pre-load and peak values from one cold load/inference/unload cycle."""

    preload: ResourceUsage
    peak: ResourceUsage

    def __post_init__(self) -> None:
        _require_exact_type(self.preload, ResourceUsage, "preload")
        _require_exact_type(self.peak, ResourceUsage, "peak")
        ResourceUsage.__post_init__(self.preload)
        ResourceUsage.__post_init__(self.peak)
        for domain in ("host", "cgroup", "device"):
            if getattr(self.peak, f"{domain}_used_bytes") < getattr(
                self.preload, f"{domain}_used_bytes"
            ):
                raise ValueError(f"{domain} peak cannot be below its paired preload")


@dataclass(frozen=True)
class AdmissionInputs:
    """Fresh inputs passed unchanged to the canonical resource-policy owner."""

    capacity: resource_owner.CapacityProfile
    device: str
    sample: resource_owner.PressureSample
    stabilized_gpu: Optional[resource_owner.StabilizedGpuCapacity]
    expected_gpu_device: Optional[str]
    now: float

    def __post_init__(self) -> None:
        _require_exact_type(self.capacity, resource_owner.CapacityProfile, "capacity")
        if type(self.device) is not str or not self.device.strip():
            raise ValueError("device must be a non-empty string")
        _require_exact_type(self.sample, resource_owner.PressureSample, "sample")
        if self.stabilized_gpu is not None:
            _require_exact_type(
                self.stabilized_gpu,
                resource_owner.StabilizedGpuCapacity,
                "stabilized_gpu",
            )
            self.stabilized_gpu.__post_init__()
        if self.expected_gpu_device is not None and (
            type(self.expected_gpu_device) is not str or not self.expected_gpu_device
        ):
            raise ValueError("expected_gpu_device must be a non-empty string")
        _require_nonnegative_time(self.now, "now")


@dataclass(frozen=True)
class FreshLoadInputs:
    """One immediate sample taken inside each cold model-load boundary."""

    capacity: resource_owner.CapacityProfile
    sample: resource_owner.PressureSample
    now: float

    def __post_init__(self) -> None:
        _require_exact_type(self.capacity, resource_owner.CapacityProfile, "capacity")
        _require_exact_type(self.sample, resource_owner.PressureSample, "sample")
        _require_nonnegative_time(self.now, "now")


@dataclass(frozen=True)
class ProfilerRequest:
    """Validated immutable inputs for one explicit-model profiling process."""

    catalog_input: Path
    catalog_output: Path
    identity_path: Path
    media_path: Path
    model: str
    runs: int
    policy: EnvelopePolicy
    host_margin_bytes: int
    device_margin_bytes: int
    prior_safe_failure_model: Optional[str] = None
    expected_uid: Optional[int] = field(default_factory=lambda: _current_uid())
    host_reserve_bytes: Optional[int] = None
    gpu_reserve_bytes: Optional[int] = None
    canonical_shared_cuda: bool = False
    require_cgroup: bool = True

    def __post_init__(self) -> None:
        for name in (
            "catalog_input",
            "catalog_output",
            "identity_path",
            "media_path",
        ):
            if not isinstance(getattr(self, name), Path):
                raise TypeError(f"{name} must be a pathlib Path")
        if _same_path(self.catalog_input, self.catalog_output):
            raise ValueError("catalog output must be distinct from catalog input")
        if _same_path(self.identity_path, self.catalog_output):
            raise ValueError("catalog output must be distinct from identity input")
        if self.model not in MODEL_DESCENT:
            raise ValueError("model must be one canonical profiler candidate")
        model_index = MODEL_DESCENT.index(self.model)
        required_prior = MODEL_DESCENT[model_index - 1] if model_index else None
        if self.prior_safe_failure_model != required_prior:
            if required_prior is None:
                raise ValueError("large-v3 must be the first profiler candidate")
            raise ValueError(
                f"{self.model} requires a clean-process safe failure from "
                f"{required_prior}"
            )
        if type(self.runs) is not int or self.runs < MINIMUM_COLD_CYCLES:
            raise ValueError("profiler requires at least three cold cycles")
        _require_exact_type(self.policy, EnvelopePolicy, "policy")
        self.policy.__post_init__()
        if self.policy.model != self.model:
            raise ValueError("policy model must equal the explicit profiler model")
        if self.policy.inference_concurrency != 1:
            raise ValueError("isolated profiling requires inference_concurrency=1")
        if not 5 <= self.policy.chunk_minutes <= 60:
            raise ValueError("profiler chunk_minutes must be between 5 and 60")
        _require_positive_bytes(self.host_margin_bytes, "host_margin_bytes")
        _require_positive_bytes(self.device_margin_bytes, "device_margin_bytes")
        if self.expected_uid is not None and (
            type(self.expected_uid) is not int or self.expected_uid < 0
        ):
            raise ValueError("expected_uid must be a non-negative integer")
        for name in ("host_reserve_bytes", "gpu_reserve_bytes"):
            value = getattr(self, name)
            if value is not None:
                _require_positive_bytes(value, name)
        if type(self.canonical_shared_cuda) is not bool:
            raise ValueError("canonical_shared_cuda must be a boolean")
        if type(self.require_cgroup) is not bool:
            raise ValueError("require_cgroup must be a boolean")


@dataclass(frozen=True)
class ProfilerResult:
    """Bounded result for an owner-operated invocation."""

    status: str
    model: str
    catalog_version: Optional[int] = None
    next_model: Optional[str] = None
    reason: Optional[str] = None
    envelope: Optional[ModelEnvelope] = None
    replaced_existing: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status == "profiled"


class MeasurementAdapter(Protocol):
    """Runtime-dependent seam; tests and the packaged adapter implement this."""

    def runtime_identity(self) -> RuntimeIdentity:
        """Return the exact immutable runtime/device identity."""

    def admission_inputs(self) -> AdmissionInputs:
        """Return fresh capacity and stabilized/immediate telemetry."""

    def fresh_load_inputs(self) -> FreshLoadInputs:
        """Return the immediate sample for the next actual cold load."""

    def assert_released(self) -> None:
        """Raise unless this process has no resident profiler model."""

    def validate_workload(
        self,
        *,
        media_path: Path,
        policy: EnvelopePolicy,
    ) -> None:
        """Validate and pin the disposable workload before fresh admission."""

    def measure_cold_cycle(
        self,
        *,
        model: str,
        media_path: Path,
        policy: EnvelopePolicy,
        admitted_load: FreshLoadInputs,
        run_number: int,
    ) -> ColdCycleMeasurement:
        """Cold-load, infer over the disposable media, and report paired use."""

    def release_model(self) -> None:
        """Synchronously unload the model and allocator caches."""


def next_lower_model(model: str) -> Optional[str]:
    """Return the only next candidate allowed after a safe clean-process failure."""

    try:
        index = MODEL_DESCENT.index(model)
    except ValueError as exc:
        raise ValueError("unknown profiler model") from exc
    return MODEL_DESCENT[index + 1] if index + 1 < len(MODEL_DESCENT) else None


def profile_model_envelope(
    request: ProfilerRequest,
    adapter: MeasurementAdapter,
) -> ProfilerResult:
    """Profile one explicit model and write one distinct staged catalog.

    A safe admission or allocation failure writes nothing and names the next
    lower candidate.  The caller must destroy this process before attempting
    that candidate.
    """

    _require_exact_type(request, ProfilerRequest, "request")
    request.__post_init__()

    identity = catalog_owner.load_identity(
        request.identity_path,
        expected_uid=request.expected_uid,
    )
    catalog = catalog_owner.load_catalog(
        request.catalog_input,
        expected_uid=request.expected_uid,
    )
    runtime = adapter.runtime_identity()
    _require_exact_type(runtime, RuntimeIdentity, "runtime identity")
    runtime.__post_init__()

    adapter.assert_released()
    admission_inputs = adapter.admission_inputs()
    _validate_initial_gpu_evidence(runtime, admission_inputs)
    decision = _explicit_admission_decision(request, admission_inputs)
    if not decision.admitted:
        adapter.assert_released()
        _require_model_specific_capacity_failure(
            decision.admission,
            boundary="initial",
        )
        return ProfilerResult(
            status="safe_failure",
            model=request.model,
            next_model=next_lower_model(request.model),
            reason=_decision_reason(decision),
        )

    adapter.validate_workload(media_path=request.media_path, policy=request.policy)
    measurements: list[ColdCycleMeasurement] = []
    for run_number in range(1, request.runs + 1):
        adapter.assert_released()
        fresh_load = adapter.fresh_load_inputs()
        load_admission = _fresh_load_admission(
            request,
            decision.requirement,
            admission_inputs.device,
            admission_inputs.expected_gpu_device,
            runtime.total_vram_bytes,
            fresh_load,
        )
        if not load_admission.admitted:
            _require_model_specific_capacity_failure(
                load_admission,
                boundary="fresh_load",
            )
            return ProfilerResult(
                status="safe_failure",
                model=request.model,
                next_model=next_lower_model(request.model),
                reason=",".join(load_admission.reasons)
                or "immediate_load_admission_failed",
            )
        try:
            measured = adapter.measure_cold_cycle(
                model=request.model,
                media_path=request.media_path,
                policy=request.policy,
                admitted_load=fresh_load,
                run_number=run_number,
            )
            _require_exact_type(measured, ColdCycleMeasurement, "cold cycle")
            measured.__post_init__()
            measurements.append(measured)
        except BaseException as exc:
            if isinstance(exc, SafeProfilingFailure) or (
                isinstance(exc, Exception) and resource_owner.is_allocation_failure(exc)
            ):
                return ProfilerResult(
                    status="safe_failure",
                    model=request.model,
                    next_model=next_lower_model(request.model),
                    reason="safe_allocation_failure",
                )
            raise
        finally:
            adapter.release_model()
            adapter.assert_released()

    envelope_measurements = _build_measurements(
        measurements,
        host_margin_bytes=request.host_margin_bytes,
        device_margin_bytes=request.device_margin_bytes,
    )
    envelope = ModelEnvelope(
        image_identity=identity.image_identity,
        runtime=runtime,
        policy=request.policy,
        measurements=envelope_measurements,
    )
    matching_entries = tuple(
        existing
        for existing in catalog.entries
        if (
            existing.image_identity == envelope.image_identity
            and existing.runtime == envelope.runtime
            and existing.policy == envelope.policy
        )
    )
    if len(matching_entries) > 1:
        raise RuntimeError("input catalog contains duplicate exact profiler evidence")
    preserved_entries = tuple(
        existing for existing in catalog.entries if existing not in matching_entries
    )
    staged_catalog = catalog_owner.build_catalog(
        catalog_version=catalog.catalog_version + 1,
        entries=(*preserved_entries, envelope),
    )
    catalog_owner.write_catalog(
        request.catalog_output,
        staged_catalog,
        expected_uid=request.expected_uid,
    )
    reloaded = catalog_owner.load_catalog(
        request.catalog_output,
        expected_uid=request.expected_uid,
    )
    if reloaded != staged_catalog:
        raise RuntimeError("staged catalog verification did not reproduce the write")
    return ProfilerResult(
        status="profiled",
        model=request.model,
        catalog_version=staged_catalog.catalog_version,
        envelope=envelope,
        replaced_existing=bool(matching_entries),
    )


def _explicit_admission_decision(
    request: ProfilerRequest,
    admission_inputs: AdmissionInputs,
) -> resource_owner.ModelDecision:
    _require_exact_type(admission_inputs, AdmissionInputs, "admission inputs")
    admission_inputs.__post_init__()
    decision = resource_owner.select_model(
        request.model,
        admission_inputs.capacity,
        device=admission_inputs.device,
        admission_sample=admission_inputs.sample,
        stabilized_gpu=admission_inputs.stabilized_gpu,
        host_reserve=request.host_reserve_bytes,
        gpu_reserve_bytes=request.gpu_reserve_bytes,
        expected_gpu_device=admission_inputs.expected_gpu_device,
        envelopes=(),
        canonical_shared_cuda=request.canonical_shared_cuda,
        require_cgroup=request.require_cgroup,
        now=admission_inputs.now,
    )
    if (
        not decision.explicit
        or decision.selected_model != request.model
        or decision.requirement is None
    ):
        raise RuntimeError("resource owner returned an invalid explicit decision")
    return decision


def _fresh_load_admission(
    request: ProfilerRequest,
    requirement: resource_owner.ModelLoadRequirement,
    device: str,
    expected_gpu_device: Optional[str],
    expected_gpu_total_bytes: int,
    inputs: FreshLoadInputs,
) -> resource_owner.AdmissionDecision:
    _require_exact_type(inputs, FreshLoadInputs, "fresh load inputs")
    inputs.__post_init__()
    require_gpu = _requires_gpu(device)
    if require_gpu and inputs.sample.gpu_total_bytes != expected_gpu_total_bytes:
        raise ProfilingTelemetryError(
            "fresh GPU total does not match the profiled runtime identity"
        )
    if (
        inputs.capacity.cgroup_limit_bytes is not None
        and inputs.sample.cgroup_limit_bytes is not None
        and inputs.capacity.cgroup_limit_bytes != inputs.sample.cgroup_limit_bytes
    ):
        raise ProfilingTelemetryError(
            "fresh cgroup capacity and pressure telemetry disagree"
        )
    host_reserve = request.host_reserve_bytes
    if host_reserve is None:
        host_reserve = resource_owner.host_reserve_bytes(inputs.capacity)
    gpu_reserve = request.gpu_reserve_bytes
    if require_gpu and gpu_reserve is None and not request.canonical_shared_cuda:
        gpu_reserve = resource_owner.gpu_priority_reserve_bytes(
            inputs.sample.gpu_total_bytes
        )
    return resource_owner.evaluate_admission(
        requirement,
        inputs.sample,
        host_reserve_bytes=host_reserve,
        gpu_priority_reserve_bytes=gpu_reserve,
        require_cgroup=request.require_cgroup,
        require_gpu=require_gpu,
        expected_gpu_device=expected_gpu_device,
        now=inputs.now,
    )


def _validate_initial_gpu_evidence(
    runtime: RuntimeIdentity,
    inputs: AdmissionInputs,
) -> None:
    """Require the exact three-sample CUDA evidence used for profiling."""

    if not _requires_gpu(inputs.device):
        raise ProfilingTelemetryError("schema-v1 profiling requires a CUDA device")
    stabilized = inputs.stabilized_gpu
    expected_device = inputs.expected_gpu_device
    if stabilized is None or expected_device is None:
        raise ProfilingTelemetryError(
            "profiling requires three stabilized exact-device GPU samples"
        )
    if (
        stabilized.device_id != expected_device
        or inputs.sample.gpu_device_id != expected_device
    ):
        raise ProfilingTelemetryError("initial GPU device identity is inconsistent")
    totals = (
        runtime.total_vram_bytes,
        stabilized.total_bytes,
        inputs.sample.gpu_total_bytes,
    )
    if any(total is None or total != totals[0] for total in totals[1:]):
        raise ProfilingTelemetryError(
            "runtime identity and initial GPU totals disagree"
        )


def _require_model_specific_capacity_failure(
    admission: Optional[resource_owner.AdmissionDecision],
    *,
    boundary: str,
) -> None:
    """Permit model descent only for fresh, model-specific capacity shortfalls."""

    if (
        admission is not None
        and admission.reasons
        and set(admission.reasons).issubset(_SAFE_DESCENT_REASONS)
    ):
        return
    reason = (
        ",".join(admission.reasons)
        if admission is not None and admission.reasons
        else "admission_evidence_unavailable"
    )
    raise ProfilingTelemetryError(f"{boundary} admission cannot descend: {reason}")


def _requires_gpu(device: str) -> bool:
    normalized = device.strip().casefold()
    return normalized == "gpu" or normalized.startswith("cuda")


def _build_measurements(
    values: Sequence[ColdCycleMeasurement],
    *,
    host_margin_bytes: int,
    device_margin_bytes: int,
) -> EnvelopeMeasurements:
    if len(values) < MINIMUM_COLD_CYCLES:
        raise ValueError("profiler requires at least three completed cold cycles")
    for value in values:
        _require_exact_type(value, ColdCycleMeasurement, "cold cycle")
        value.__post_init__()

    def domain_values(domain: str, phase: str) -> tuple[int, ...]:
        return tuple(
            getattr(getattr(value, phase), f"{domain}_used_bytes") for value in values
        )

    host_preloads = domain_values("host", "preload")
    host_peaks = domain_values("host", "peak")
    cgroup_preloads = domain_values("cgroup", "preload")
    cgroup_peaks = domain_values("cgroup", "peak")
    device_preloads = domain_values("device", "preload")
    device_peaks = domain_values("device", "peak")
    return EnvelopeMeasurements(
        runs=len(values),
        host_preload_used_bytes=max(host_preloads),
        host_peak_used_bytes=max(host_peaks),
        cgroup_preload_used_bytes=max(cgroup_preloads),
        cgroup_peak_used_bytes=max(cgroup_peaks),
        device_preload_used_bytes=max(device_preloads),
        device_peak_used_bytes=max(device_peaks),
        host_incremental_peak_bytes=resource_owner.paired_incremental_peak_bytes(
            host_preloads, host_peaks
        ),
        cgroup_incremental_peak_bytes=resource_owner.paired_incremental_peak_bytes(
            cgroup_preloads, cgroup_peaks
        ),
        device_incremental_peak_bytes=resource_owner.paired_incremental_peak_bytes(
            device_preloads, device_peaks
        ),
        host_margin_bytes=host_margin_bytes,
        device_margin_bytes=device_margin_bytes,
    )


def _decision_reason(decision: resource_owner.ModelDecision) -> str:
    if decision.admission is not None and decision.admission.reasons:
        return ",".join(decision.admission.reasons)
    return decision.reason


class StableWhisperMeasurementAdapter:
    """Packaged runtime adapter used only by the explicit profiler CLI."""

    def __init__(
        self,
        *,
        model: str,
        model_revision: str,
        model_path: Path,
        device: str,
        compute_type: str,
        cpu_threads: int,
        decoder_options: Mapping[str, object],
        sample_interval_seconds: float = 0.1,
        gpu_stabilization_interval_seconds: float = 5.0,
    ) -> None:
        if model not in MODEL_DESCENT:
            raise ValueError("adapter model must be one canonical profiler candidate")
        self.model_name = model
        self.model_revision = _normalized_revision(model_revision)
        self.model_revision_commit = self.model_revision.removeprefix("hf:")
        self.model_path = model_path
        self.device = "cuda" if device.casefold() == "gpu" else device
        self.compute_type = compute_type
        self.cpu_threads = _positive_int_value(cpu_threads, "CPU threads")
        self.decoder_options = dict(decoder_options)
        _canonical_decoder_bytes(self.decoder_options)
        self.sample_interval_seconds = _require_positive_number(
            sample_interval_seconds, "sample interval"
        )
        self.gpu_stabilization_interval_seconds = _require_positive_number(
            gpu_stabilization_interval_seconds, "GPU stabilization interval"
        )
        self.stable_whisper = importlib.import_module("stable_whisper")
        self.faster_whisper = importlib.import_module("faster_whisper")
        self.ctranslate2 = importlib.import_module("ctranslate2")
        self.torch = importlib.import_module("torch")
        self._model = None
        self._backend_release_verified = True
        self._validated_media_generation = None
        self._gpu_index = _cuda_index(self.device)
        self._expected_gpu_device = None
        if self.device.casefold().startswith("cuda"):
            if not self.torch.cuda.is_available():
                raise ProfilingTelemetryError(
                    "CUDA profiling requested but unavailable"
                )
            properties = self.torch.cuda.get_device_properties(self._gpu_index)
            self._expected_gpu_device = f"cuda:{self._gpu_index}:{properties.name}"

    def runtime_identity(self) -> RuntimeIdentity:
        if self._expected_gpu_device is None:
            raise ProfilingTelemetryError(
                "schema-v1 profiler requires exact CUDA runtime identity"
            )
        properties = self.torch.cuda.get_device_properties(self._gpu_index)
        capability = self.torch.cuda.get_device_capability(self._gpu_index)
        cuda_version = getattr(self.torch.version, "cuda", None)
        if not cuda_version:
            raise ProfilingTelemetryError("CUDA runtime version is unavailable")
        return RuntimeIdentity(
            stable_ts_version=_module_version(self.stable_whisper, "stable-ts"),
            faster_whisper_version=_module_version(
                self.faster_whisper, "faster-whisper"
            ),
            ctranslate2_version=_module_version(self.ctranslate2, "ctranslate2"),
            cuda_runtime_version=str(cuda_version),
            driver_version=self._driver_version(),
            device_name=str(properties.name),
            compute_capability=f"{capability[0]}.{capability[1]}",
            total_vram_bytes=int(properties.total_memory),
        )

    def admission_inputs(self) -> AdmissionInputs:
        capacity = resource_owner.discover_capacity()
        now = time.monotonic()
        if self._expected_gpu_device is None:
            sample = resource_owner.read_pressure_sample(clock=lambda: now)
            return AdmissionInputs(capacity, self.device, sample, None, None, now)
        stabilized = resource_owner.stabilize_gpu_capacity(
            self._pressure_sample,
            expected_device=self._expected_gpu_device,
            sample_count=3,
            interval_seconds=self.gpu_stabilization_interval_seconds,
            maximum_age_seconds=10.0,
        )
        sample = self._pressure_sample()
        now = time.monotonic()
        return AdmissionInputs(
            capacity,
            self.device,
            sample,
            stabilized,
            self._expected_gpu_device,
            now,
        )

    def fresh_load_inputs(self) -> FreshLoadInputs:
        capacity = resource_owner.discover_capacity()
        sample = self._pressure_sample()
        return FreshLoadInputs(
            capacity=capacity,
            sample=sample,
            now=time.monotonic(),
        )

    def assert_released(self) -> None:
        if self._model is not None or not self._backend_release_verified:
            raise RuntimeError("profiler model release has not been verified")

    def validate_workload(
        self,
        *,
        media_path: Path,
        policy: EnvelopePolicy,
    ) -> None:
        if not media_path.is_file():
            raise FileNotFoundError(media_path)
        self._validate_media_duration(media_path, policy.chunk_minutes)
        self._validated_media_generation = self._media_generation(media_path)

    def measure_cold_cycle(
        self,
        *,
        model: str,
        media_path: Path,
        policy: EnvelopePolicy,
        admitted_load: FreshLoadInputs,
        run_number: int,
    ) -> ColdCycleMeasurement:
        del run_number
        self.assert_released()
        if (
            model != self.model_name
            or policy.model != model
            or policy.model_revision != self.model_revision
            or policy.compute_type != self.compute_type
            or policy.inference_concurrency != 1
            or policy.decoder_options_sha256
            != _decoder_options_digest(self.decoder_options)
        ):
            raise ValueError("adapter model does not match profiler request")
        if self._validated_media_generation != self._media_generation(media_path):
            raise ProfilingTelemetryError(
                "profiling media changed after workload validation"
            )

        _require_exact_type(admitted_load, FreshLoadInputs, "admitted load")
        admitted_load.__post_init__()
        pre_sample = admitted_load.sample
        preload = self._usage_from_sample(pre_sample)
        peaks = [preload]
        stop = threading.Event()
        sampler_error: list[BaseException] = []

        def sample_until_stopped() -> None:
            while not stop.wait(self.sample_interval_seconds):
                try:
                    peaks.append(self._usage_from_sample(self._pressure_sample()))
                except BaseException as exc:
                    sampler_error.append(exc)
                    stop.set()

        sampler = threading.Thread(
            target=sample_until_stopped,
            name="subgen-model-envelope-profiler-sampler",
            daemon=True,
        )
        sampler.start()
        try:
            self._backend_release_verified = False
            self._model = self.stable_whisper.load_faster_whisper(
                model,
                download_root=str(self.model_path),
                device=self.device,
                cpu_threads=self.cpu_threads,
                num_workers=1,
                compute_type=policy.compute_type,
                revision=self.model_revision_commit,
            )
            self._require_loaded_backend()
            peaks.append(self._usage_from_sample(self._pressure_sample()))
            result = self._model.transcribe(
                str(media_path),
                task=policy.task,
                verbose=None,
                **self.decoder_options,
            )
            del result
            peaks.append(self._usage_from_sample(self._pressure_sample()))
        except BaseException as exc:
            if isinstance(exc, Exception) and resource_owner.is_allocation_failure(exc):
                raise SafeProfilingFailure(str(exc)) from exc
            raise
        finally:
            stop.set()
            sampler.join(timeout=max(1.0, self.sample_interval_seconds * 4))
        if sampler.is_alive():
            raise ProfilingTelemetryError("memory sampler did not stop")
        if sampler_error:
            raise ProfilingTelemetryError(
                "memory sampler lost required telemetry"
            ) from (sampler_error[0])
        return ColdCycleMeasurement(preload, _maximum_usage(peaks))

    def release_model(self) -> None:
        model = self._model
        if model is not None:
            backend = getattr(model, "model", None)
            unload = getattr(backend, "unload_model", None)
            if not callable(unload):
                raise RuntimeError("loaded profiler model has no unload operation")
            unload()
            if getattr(backend, "model_is_loaded", None) is not False:
                raise RuntimeError("profiler backend did not verify model unload")
            self._model = None
            del backend
            del model
        gc.collect()
        if self.device.casefold().startswith("cuda"):
            self.torch.cuda.synchronize(self._gpu_index)
            self.torch.cuda.empty_cache()
            self.torch.cuda.synchronize(self._gpu_index)
        gc.collect()
        self._backend_release_verified = True

    def _require_loaded_backend(self) -> None:
        backend = getattr(self._model, "model", None)
        if backend is None or getattr(backend, "model_is_loaded", None) is not True:
            raise ProfilingTelemetryError(
                "loaded profiler backend cannot prove model residency"
            )

    def _validate_media_duration(self, media_path: Path, chunk_minutes: int) -> None:
        expected_seconds = chunk_minutes * 60 + 2 * WORKING_OVERLAP_SECONDS
        duration = self._media_duration_seconds(media_path)
        if abs(duration - expected_seconds) > MEDIA_DURATION_TOLERANCE_SECONDS:
            raise ProfilingTelemetryError(
                "profiling media must match one worst-case working chunk duration"
            )

    def _media_duration_seconds(self, media_path: Path) -> float:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                os.fspath(media_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise ProfilingTelemetryError(
                "profiling media duration could not be established"
            )
        try:
            duration = float(completed.stdout.strip())
        except ValueError as exc:
            raise ProfilingTelemetryError(
                "profiling media duration could not be established"
            ) from exc
        if not math.isfinite(duration) or duration <= 0:
            raise ProfilingTelemetryError(
                "profiling media duration could not be established"
            )
        return duration

    @staticmethod
    def _media_generation(media_path: Path) -> tuple[int, int, int, int]:
        if media_path.is_symlink() or not media_path.is_file():
            raise ProfilingTelemetryError("profiling media is not a regular file")
        metadata = media_path.stat()
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )

    def _pressure_sample(self) -> resource_owner.PressureSample:
        return resource_owner.read_pressure_sample(
            gpu_memory_reader=self._gpu_memory,
        )

    def _gpu_memory(self) -> tuple[str, int, int]:
        if self._expected_gpu_device is None:
            raise ProfilingTelemetryError("GPU telemetry requested for a CPU adapter")
        free_bytes, total_bytes = self.torch.cuda.mem_get_info(self._gpu_index)
        return self._expected_gpu_device, int(total_bytes), int(free_bytes)

    def _usage_from_sample(
        self, sample: resource_owner.PressureSample
    ) -> ResourceUsage:
        if (
            sample.host_total_bytes is None
            or sample.host_available_bytes is None
            or sample.host_available_bytes > sample.host_total_bytes
            or sample.cgroup_current_bytes is None
            or sample.gpu_total_bytes is None
            or sample.gpu_free_bytes is None
            or sample.gpu_free_bytes > sample.gpu_total_bytes
            or sample.gpu_device_id != self._expected_gpu_device
        ):
            raise ProfilingTelemetryError("required profiler telemetry is unavailable")
        return ResourceUsage(
            host_used_bytes=sample.host_total_bytes - sample.host_available_bytes,
            cgroup_used_bytes=sample.cgroup_current_bytes,
            device_used_bytes=sample.gpu_total_bytes - sample.gpu_free_bytes,
        )

    def _driver_version(self) -> str:
        completed = subprocess.run(
            [
                "nvidia-smi",
                f"--id={self._gpu_index}",
                "--query-gpu=driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        value = completed.stdout.strip()
        if completed.returncode != 0 or not value or "\n" in value:
            raise ProfilingTelemetryError("NVIDIA driver version is unavailable")
        return value


def _maximum_usage(values: Sequence[ResourceUsage]) -> ResourceUsage:
    if not values:
        raise ValueError("at least one usage observation is required")
    return ResourceUsage(
        host_used_bytes=max(value.host_used_bytes for value in values),
        cgroup_used_bytes=max(value.cgroup_used_bytes for value in values),
        device_used_bytes=max(value.device_used_bytes for value in values),
    )


def _module_version(module: object, name: str) -> str:
    value = getattr(module, "__version__", None)
    if type(value) is not str or not value or not value.isascii():
        raise ProfilingTelemetryError(f"{name} version is unavailable")
    return value


def _parse_decoder_options(raw: str) -> dict[str, object]:
    try:
        value = ast.literal_eval(raw or "{}")
    except (SyntaxError, ValueError) as exc:
        raise ValueError("decoder options must be a Python mapping literal") from exc
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError("decoder options must be a string-keyed mapping")
    _canonical_decoder_bytes(value)
    return value


def _decoder_options_digest(options: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_decoder_bytes(options)).hexdigest()


def _canonical_decoder_bytes(options: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            dict(options),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("decoder options must be canonical JSON values") from exc


def _normalized_revision(raw: str) -> str:
    match = _HF_REVISION.fullmatch(raw.strip())
    if match is None:
        raise ValueError("model revision must be an immutable 40-hex commit")
    return "hf:" + match.group(1)


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return value


def _positive_gib(raw: str) -> int:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("reserve must be a positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("reserve must be a positive number")
    value_bytes = int(value * resource_owner.GIB)
    if value_bytes <= 0:
        raise argparse.ArgumentTypeError("reserve must be at least one byte")
    return value_bytes


def _chunk_setting(raw: str) -> int | str:
    if type(raw) is str and raw.strip().casefold() == "auto":
        return "auto"
    value = _positive_int(raw)
    if not 5 <= value <= 60:
        raise argparse.ArgumentTypeError("chunk minutes must be between 5 and 60")
    return value


def _environment_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Profile one explicit Whisper model in an isolated process and write "
            "a distinct staged ModelEnvelope catalog."
        )
    )
    parser.add_argument("--catalog-input", required=True, type=Path)
    parser.add_argument("--catalog-output", required=True, type=Path)
    parser.add_argument("--identity", required=True, type=Path)
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--model", required=True, choices=MODEL_DESCENT)
    parser.add_argument(
        "--after-safe-failure",
        choices=MODEL_DESCENT,
        default=None,
        help=(
            "trusted host assertion from the immediately preceding exit-3 profiler; "
            "that process/container must already be destroyed"
        ),
    )
    parser.add_argument("--runs", type=_positive_int, default=MINIMUM_COLD_CYCLES)
    parser.add_argument(
        "--model-revision",
        default=os.getenv("WHISPER_MODEL_REVISION", ""),
        help="immutable Hugging Face commit (or WHISPER_MODEL_REVISION)",
    )
    parser.add_argument("--device", default=os.getenv("TRANSCRIBE_DEVICE", "cuda"))
    parser.add_argument("--compute-type", default=os.getenv("COMPUTE_TYPE", "float16"))
    parser.add_argument(
        "--task",
        choices=("transcribe", "translate"),
        default=os.getenv("TRANSCRIBE_OR_TRANSLATE", "translate"),
    )
    parser.add_argument(
        "--inference-concurrency",
        type=_positive_int,
        choices=(1,),
        default=_positive_int(os.getenv("CONCURRENT_TRANSCRIPTIONS", "1")),
    )
    parser.add_argument(
        "--chunk-minutes",
        type=_chunk_setting,
        default=_chunk_setting(os.getenv("SEGMENTATION_CHUNK_MINUTES", "auto")),
        help="explicit 5-60 minute core or auto capacity tier",
    )
    parser.add_argument(
        "--decoder-options",
        default=os.getenv("SUBGEN_KWARGS", "{}"),
        help="the exact SUBGEN_KWARGS-compatible mapping literal",
    )
    parser.add_argument(
        "--model-path",
        type=Path,
        default=Path(os.getenv("MODEL_PATH", "./models")),
    )
    parser.add_argument(
        "--cpu-threads",
        type=_positive_int,
        default=_positive_int(os.getenv("WHISPER_THREADS", "4")),
    )
    parser.add_argument(
        "--host-reserve-gib",
        type=_positive_gib,
        default=None,
    )
    parser.add_argument(
        "--gpu-reserve-gib",
        type=_positive_gib,
        default=(
            _positive_gib(os.environ["GPU_MEMORY_RESERVE_GIB"])
            if os.getenv("GPU_MEMORY_RESERVE_GIB", "").strip().casefold()
            not in {"", "auto"}
            else None
        ),
    )
    parser.add_argument(
        "--host-margin-mib",
        type=_positive_int,
        default=(
            _positive_int(os.environ["MODEL_ENVELOPE_HOST_MARGIN_MIB"])
            if os.getenv("MODEL_ENVELOPE_HOST_MARGIN_MIB", "").strip()
            else None
        ),
        help="audited positive host margin (or MODEL_ENVELOPE_HOST_MARGIN_MIB)",
    )
    parser.add_argument(
        "--device-margin-mib",
        type=_positive_int,
        default=(
            _positive_int(os.environ["MODEL_ENVELOPE_DEVICE_MARGIN_MIB"])
            if os.getenv("MODEL_ENVELOPE_DEVICE_MARGIN_MIB", "").strip()
            else None
        ),
        help=("audited positive device margin (or MODEL_ENVELOPE_DEVICE_MARGIN_MIB)"),
    )
    parser.add_argument(
        "--canonical-shared-cuda",
        action=argparse.BooleanOptionalAction,
        default=_environment_bool("CANONICAL_SHARED_CUDA", False),
    )
    parser.add_argument(
        "--require-cgroup",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    adapter_factory: Optional[
        Callable[[argparse.Namespace, Mapping[str, object]], MeasurementAdapter]
    ] = None,
) -> int:
    try:
        parser = _parser()
    except (ValueError, argparse.ArgumentTypeError) as exc:
        print(f"model-envelope profiler configuration failed: {exc}", file=sys.stderr)
        return 1
    try:
        args = parser.parse_args(argv)
        if args.runs < MINIMUM_COLD_CYCLES:
            parser.error("--runs must be at least 3")
        if args.host_margin_mib is None or args.device_margin_mib is None:
            parser.error(
                "positive --host-margin-mib and --device-margin-mib are required"
            )
    except SystemExit as exc:
        return 0 if exc.code == 0 else 1
    try:
        revision = _normalized_revision(args.model_revision)
        decoder_options = _parse_decoder_options(args.decoder_options)
        chunk_minutes = args.chunk_minutes
        if chunk_minutes == "auto":
            chunk_minutes = (
                resource_owner.initial_chunk_seconds(
                    resource_owner.discover_capacity(),
                )
                // 60
            )
        policy = EnvelopePolicy(
            model=args.model,
            model_revision=revision,
            compute_type=args.compute_type,
            task=args.task,
            inference_concurrency=args.inference_concurrency,
            chunk_minutes=chunk_minutes,
            decoder_options_sha256=_decoder_options_digest(decoder_options),
        )
        expected_uid = os.geteuid() if hasattr(os, "geteuid") else None
        request = ProfilerRequest(
            catalog_input=args.catalog_input,
            catalog_output=args.catalog_output,
            identity_path=args.identity,
            media_path=args.media,
            model=args.model,
            runs=args.runs,
            policy=policy,
            host_margin_bytes=args.host_margin_mib * resource_owner.MIB,
            device_margin_bytes=args.device_margin_mib * resource_owner.MIB,
            prior_safe_failure_model=args.after_safe_failure,
            expected_uid=expected_uid,
            host_reserve_bytes=args.host_reserve_gib,
            gpu_reserve_bytes=args.gpu_reserve_gib,
            canonical_shared_cuda=args.canonical_shared_cuda,
            require_cgroup=args.require_cgroup,
        )
        factory = adapter_factory or _default_adapter_factory
        adapter = factory(args, decoder_options)
        result = profile_model_envelope(request, adapter)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"model-envelope profiler failed: {exc}", file=sys.stderr)
        return 1

    output = {
        "status": result.status,
        "model": result.model,
        "catalog_version": result.catalog_version,
        "next_model": result.next_model,
        "reason": result.reason,
        "replaced_existing": result.replaced_existing,
    }
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0 if result.succeeded else SAFE_FAILURE_EXIT


def _default_adapter_factory(
    args: argparse.Namespace,
    decoder_options: Mapping[str, object],
) -> StableWhisperMeasurementAdapter:
    return StableWhisperMeasurementAdapter(
        model=args.model,
        model_revision=args.model_revision,
        model_path=args.model_path,
        device=args.device,
        compute_type=args.compute_type,
        cpu_threads=args.cpu_threads,
        decoder_options=decoder_options,
    )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _current_uid() -> Optional[int]:
    return os.geteuid() if hasattr(os, "geteuid") else None


def _require_exact_type(value: object, expected: type, name: str) -> None:
    if type(value) is not expected:
        raise TypeError(f"{name} must be {expected.__name__}")


def _require_nonnegative_bytes(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_BYTES:
        raise ValueError(f"{name} must be a bounded non-negative integer")
    return value


def _require_positive_bytes(value: object, name: str) -> int:
    value = _require_nonnegative_bytes(value, name)
    if value == 0:
        raise ValueError(f"{name} must be positive")
    return value


def _require_nonnegative_time(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite non-negative number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return parsed


def _require_positive_number(value: object, name: str) -> float:
    parsed = _require_nonnegative_time(value, name)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _positive_int_value(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _cuda_index(device: str) -> int:
    normalized = device.strip().casefold()
    if normalized == "cuda":
        return 0
    if normalized.startswith("cuda:"):
        try:
            index = int(normalized.split(":", 1)[1])
        except ValueError as exc:
            raise ValueError("CUDA device must use an integer index") from exc
        if index >= 0:
            return index
    raise ValueError("packaged profiler currently requires an exact CUDA device")


if __name__ == "__main__":
    raise SystemExit(main())
