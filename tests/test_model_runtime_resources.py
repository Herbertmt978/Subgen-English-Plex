import asyncio
import gc
import json
import runpy
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import subgen

from subgen_core import model_envelope_catalog as catalog_owner
from subgen_core import (
    model_runtime,
    resource_management,
    runtime_receipts,
    segmentation,
)
from subgen_core.priority_pressure import PriorityObservation

GIB = resource_management.GIB
ROOT = Path(__file__).resolve().parents[1]


class RecordingLock:
    def __init__(self, events, name):
        self.events = events
        self.name = name
        self.held = False

    def __enter__(self):
        assert not self.held
        self.held = True
        self.events.append(f"{self.name}.enter")
        return self

    def __exit__(self, *_args):
        self.events.append(f"{self.name}.exit")
        self.held = False


class RecordingSemaphore:
    def __init__(self, value, events):
        self._semaphore = threading.BoundedSemaphore(value)
        self.events = events

    def acquire(self, timeout=None):
        result = self._semaphore.acquire(timeout=timeout)
        self.events.append("permit.acquire")
        return result

    def release(self):
        self.events.append("permit.release")
        return self._semaphore.release()


class RuntimeReceiptRecorder:
    gate_enabled = True

    def __init__(self):
        self.states = []

    def record_runtime_change_locked(self, state):
        self.states.append(dict(state))


class FailingGateReceiptRecorder:
    gate_enabled = True

    def __init__(self, error=None):
        self.error = error or OSError("receipt fsync failed")
        self.calls = 0

    def record_runtime_change_locked(self, _state):
        self.calls += 1
        raise self.error


def coordinated_runtime(*, permits=2, unload=None):
    events = []
    unload = unload or MagicMock(side_effect=lambda: events.append("model.unload"))
    backend = SimpleNamespace(unload_model=unload, model_is_loaded=False)
    controller = SimpleNamespace(
        NORMAL="normal",
        state="normal",
        admission_open=True,
        recovery_reason=None,
        mark_released=MagicMock(
            side_effect=lambda reason=None: (
                setattr(controller, "state", "recovering"),
                setattr(controller, "admission_open", False),
                setattr(controller, "recovery_reason", reason),
            )
        ),
    )
    runtime = SimpleNamespace(
        model=SimpleNamespace(model=backend),
        model_load_lock=RecordingLock(events, "load_lock"),
        model_inference_semaphore=RecordingSemaphore(permits, events),
        model_inference_permit_count=permits,
        model_runtime_condition=threading.Condition(threading.Lock()),
        model_admission_closed=False,
        model_release_generation=0,
        model_release_transition=None,
        model_active_inferences=0,
        model_load_generation=0,
        model_unload_generation=0,
        cuda_oom_generation=0,
        media_failure_generation=0,
        model_pressure_controller=controller,
        model_runtime_cancel_event=threading.Event(),
        model_permit_wait_seconds=0.01,
        model_load_allocation_failures=0,
        model_profile_unhealthy=False,
        model_profile_unhealthy_reason=None,
        model_runtime_status={},
        _resource_management=resource_management,
        transcribe_device="cuda",
        cuda_device_index=0,
        torch=SimpleNamespace(
            cuda=SimpleNamespace(
                is_available=lambda: True,
                synchronize=MagicMock(
                    side_effect=lambda _index: events.append("cuda.synchronize")
                ),
                empty_cache=MagicMock(
                    side_effect=lambda: events.append("cuda.empty_cache")
                ),
            )
        ),
        gc=SimpleNamespace(
            collect=MagicMock(side_effect=lambda: events.append("gc.collect"))
        ),
        os=SimpleNamespace(name="nt"),
        ctypes=SimpleNamespace(),
        logging=MagicMock(),
    )
    return runtime, controller, backend, events


def configure_model_loading(runtime, controller, loader):
    runtime.model = None
    runtime.model_requirement = object()
    runtime.initialize_model_runtime = lambda: None
    runtime.read_pressure_sample = MagicMock()
    runtime.stable_whisper = SimpleNamespace(load_faster_whisper=loader)
    runtime.whisper_model = "medium"
    runtime.model_location = "/models"
    runtime.whisper_threads = 4
    runtime.concurrent_transcriptions = 1
    runtime.compute_type = "float16"
    runtime.whisper_model_revision_commit = None
    controller.immediate_load_admission = MagicMock(
        return_value=SimpleNamespace(admitted=True)
    )

    def recover(_cancelled=None):
        controller.state = "normal"
        controller.admission_open = True
        return True

    controller.wait_for_recovery = MagicMock(side_effect=recover)


def test_model_load_and_unload_publish_exact_resident_identity_transitions():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    loaded_backend = SimpleNamespace(
        unload_model=MagicMock(),
        model_is_loaded=False,
    )
    loaded = SimpleNamespace(model=loaded_backend)
    recorder = RuntimeReceiptRecorder()
    runtime.runtime_receipt_coordinator = recorder
    runtime.selected_model_identity_sha256 = "a" * 64
    runtime.resident_model_identity_sha256 = None
    configure_model_loading(runtime, controller, MagicMock(return_value=loaded))

    assert model_runtime._load_model_once(runtime) is True
    assert runtime.model is loaded
    assert recorder.states[-1]["model_resident"] is True
    assert recorder.states[-1]["model_identity_sha256"] == "a" * 64
    assert recorder.states[-1]["model_load_generation"] == 1

    assert model_runtime.release_model(runtime, reason="priority_pressure") is True
    assert runtime.model is None
    assert recorder.states[-1]["model_resident"] is False
    assert recorder.states[-1]["model_identity_sha256"] is None
    assert recorder.states[-1]["model_unload_generation"] == 1
    assert any(
        state["model_resident"] is True and state["admission_open"] is False
        for state in recorder.states
    )


def test_direct_receipt_failure_latches_runtime_and_controller_without_recursion():
    condition = threading.Condition(threading.RLock())
    controller = resource_management.PressureController(reserve_bytes=GIB)
    recorder = FailingGateReceiptRecorder()
    runtime = SimpleNamespace(
        model=None,
        model_runtime_condition=condition,
        model_admission_closed=True,
        model_release_generation=0,
        model_release_transition=None,
        model_active_inferences=0,
        model_inference_permit_count=1,
        model_pressure_controller=controller,
        runtime_receipt_coordinator=recorder,
        model_runtime_status={
            "controller_state": "normal",
            "recovery_reason": None,
            "admission_open": True,
        },
        model_load_generation=0,
        model_unload_generation=0,
        cuda_oom_generation=0,
        media_failure_generation=0,
        resident_model_identity_sha256=None,
    )

    with pytest.raises(OSError, match="receipt fsync failed"):
        model_runtime.reopen_model_admission(runtime)

    assert recorder.calls == 1
    assert runtime.model_admission_closed is True
    assert controller.state == controller.RECOVERING
    assert controller.admission_open is False
    assert controller.recovery_reason == "receipt_unavailable"
    assert runtime.model_runtime_status["controller_state"] == controller.RECOVERING
    assert runtime.model_runtime_status["admission_open"] is False
    assert runtime.model_runtime_status["recovery_reason"] == "receipt_unavailable"


def test_load_receipt_failure_unloads_model_and_keeps_admission_closed():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    loaded_backend = SimpleNamespace(
        unload_model=MagicMock(),
        model_is_loaded=False,
    )
    loaded = SimpleNamespace(model=loaded_backend)
    recorder = FailingGateReceiptRecorder()
    runtime.runtime_receipt_coordinator = recorder
    runtime.selected_model_identity_sha256 = "a" * 64
    runtime.resident_model_identity_sha256 = None
    configure_model_loading(runtime, controller, MagicMock(return_value=loaded))

    with pytest.raises(OSError, match="receipt fsync failed"):
        model_runtime._load_model_once(runtime)

    assert recorder.calls == 1
    loaded_backend.unload_model.assert_called_once_with()
    assert runtime.model is None
    assert runtime.resident_model_identity_sha256 is None
    assert runtime.model_load_generation == 1
    assert runtime.model_unload_generation == 1
    assert runtime.model_admission_closed is True
    assert controller.state == "recovering"
    assert controller.admission_open is False
    assert controller.recovery_reason == "receipt_unavailable"


def test_load_receipt_and_backend_unload_failures_quarantine_resident_model():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    unload_failure = RuntimeError("backend unload failed")
    loaded_backend = SimpleNamespace(
        unload_model=MagicMock(side_effect=unload_failure),
        model_is_loaded=True,
    )
    loaded = SimpleNamespace(model=loaded_backend)
    loader = MagicMock(return_value=loaded)
    recorder = FailingGateReceiptRecorder()
    runtime.runtime_receipt_coordinator = recorder
    runtime.selected_model_identity_sha256 = "a" * 64
    runtime.resident_model_identity_sha256 = None
    configure_model_loading(runtime, controller, loader)

    with pytest.raises(OSError, match="receipt fsync failed"):
        model_runtime._load_model_once(runtime)

    assert recorder.calls == 1
    loaded_backend.unload_model.assert_called_once_with()
    assert runtime.model is loaded
    assert runtime.resident_model_identity_sha256 == "a" * 64
    assert runtime.model_load_generation == 1
    assert runtime.model_unload_generation == 0
    assert runtime.model_admission_closed is True
    assert controller.state == "recovering"
    assert controller.admission_open is False
    assert controller.recovery_reason == "receipt_unavailable"

    assert (
        model_runtime._load_model_once(runtime)
        is model_runtime._LOAD_DEFERRED_STALE_GENERATION
    )
    loader.assert_called_once_with(
        "medium",
        download_root="/models",
        device="cuda",
        device_index=0,
        cpu_threads=4,
        num_workers=1,
        compute_type="float16",
    )


def test_load_receipt_failure_quarantines_backend_without_exact_unload_confirmation():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    loaded_backend = SimpleNamespace(unload_model=MagicMock())
    loaded = SimpleNamespace(model=loaded_backend)
    recorder = FailingGateReceiptRecorder()
    runtime.runtime_receipt_coordinator = recorder
    runtime.selected_model_identity_sha256 = "a" * 64
    runtime.resident_model_identity_sha256 = None
    configure_model_loading(runtime, controller, MagicMock(return_value=loaded))

    with pytest.raises(OSError, match="receipt fsync failed"):
        model_runtime._load_model_once(runtime)

    loaded_backend.unload_model.assert_called_once_with()
    assert runtime.model is loaded
    assert runtime.resident_model_identity_sha256 == "a" * 64
    assert runtime.model_unload_generation == 0
    assert runtime.model_admission_closed is True
    assert controller.admission_open is False
    assert controller.recovery_reason == "receipt_unavailable"


def required_inference_allocation_api():
    """Return the Task 5 allocation control API with a useful RED failure."""

    failure_type = getattr(model_runtime, "ModelInferenceAllocationFailure", None)
    release = getattr(model_runtime, "release_after_inference_failure", None)
    assert isinstance(failure_type, type), (
        "model_runtime.ModelInferenceAllocationFailure is required"
    )
    assert callable(release), (
        "model_runtime.release_after_inference_failure is required"
    )
    return failure_type, release


@pytest.mark.parametrize(
    "raw, expected", [("auto", None), ("AUTO", None), ("5", 5), ("60", 60)]
)
def test_chunk_setting_is_snapshotted_strictly(raw, expected):
    assert subgen._chunk_minutes_setting(raw) == expected


@pytest.mark.parametrize("raw", ["", "4", "61", "5.5", "invalid"])
def test_chunk_setting_rejects_unsafe_values(raw):
    with pytest.raises(ValueError, match="SEGMENTATION_CHUNK_MINUTES"):
        subgen._chunk_minutes_setting(raw)


@pytest.mark.parametrize("raw", ["", "0", "-1", "nan", "inf", "1e-20"])
def test_reserve_setting_rejects_nonpositive_or_nonfinite(monkeypatch, raw):
    monkeypatch.setenv("TEST_RESERVE_GIB", raw)
    with pytest.raises(ValueError, match="TEST_RESERVE_GIB"):
        subgen._auto_or_positive_gib("TEST_RESERVE_GIB")


def test_reserve_setting_accepts_auto_and_positive_fraction(monkeypatch):
    monkeypatch.setenv("TEST_RESERVE_GIB", "auto")
    assert subgen._auto_or_positive_gib("TEST_RESERVE_GIB") is None
    monkeypatch.setenv("TEST_RESERVE_GIB", "0.5")
    assert subgen._auto_or_positive_gib("TEST_RESERVE_GIB") == 0.5


@pytest.mark.parametrize(
    "settings, message",
    [
        ({"SEGMENTATION_CHUNK_MINUTES": ""}, "SEGMENTATION_CHUNK_MINUTES"),
        (
            {
                "CANONICAL_SHARED_CUDA": "true",
                "TRANSCRIBE_DEVICE": "cuda",
                "GPU_MEMORY_RESERVE_GIB": "auto",
            },
            "GPU_MEMORY_RESERVE_GIB",
        ),
        (
            {
                "WHISPER_MODEL": "auto",
                "WHISPER_MODEL_REVISION": "a" * 40,
            },
            "only valid with an explicit",
        ),
    ],
)
def test_invalid_startup_settings_fail_before_any_thread_starts(
    monkeypatch,
    settings,
    message,
):
    for name in (
        "SEGMENTATION_CHUNK_MINUTES",
        "CANONICAL_SHARED_CUDA",
        "TRANSCRIBE_DEVICE",
        "GPU_MEMORY_RESERVE_GIB",
        "WHISPER_MODEL",
        "WHISPER_MODEL_REVISION",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    start = MagicMock()
    monkeypatch.setattr(threading.Thread, "start", start)

    with pytest.raises(ValueError, match=message):
        runpy.run_path(str(ROOT / "subgen_override.py"), run_name="startup_probe")

    start.assert_not_called()


def test_startup_normalizes_case_insensitive_gpu_alias_before_indexing(monkeypatch):
    for name in (
        "CANONICAL_SHARED_CUDA",
        "GPU_MEMORY_RESERVE_GIB",
        "SEGMENTATION_CHUNK_MINUTES",
        "WHISPER_MODEL_REVISION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRANSCRIBE_DEVICE", " GPU ")
    start = MagicMock()
    monkeypatch.setattr(threading.Thread, "start", start)

    namespace = runpy.run_path(
        str(ROOT / "subgen_override.py"),
        run_name="gpu_alias_startup_probe",
    )

    assert namespace["transcribe_device"] == "cuda"
    assert namespace["cuda_device_index"] == 0


@pytest.mark.parametrize(
    "device, expected",
    [("gpu", 0), ("cuda", 0), ("CUDA:0", 0), ("cuda:3", 3)],
)
def test_cuda_index_is_exact_and_never_sums_devices(device, expected):
    assert subgen._cuda_index(device) == expected


def test_exact_gpu_reader_binds_nvidia_identity_to_torch_byte_counts(monkeypatch):
    completed = SimpleNamespace(
        returncode=0,
        stdout="GPU-exact, 580.12\n",
    )
    run = MagicMock(return_value=completed)
    monkeypatch.setattr(subgen.subprocess, "run", run)
    monkeypatch.setattr(subgen, "cuda_device_index", 2)
    monkeypatch.setattr(
        subgen.torch.cuda,
        "mem_get_info",
        MagicMock(return_value=(18 * GIB, 24 * GIB)),
    )
    monkeypatch.setattr(
        subgen.torch.cuda,
        "get_device_properties",
        MagicMock(return_value=SimpleNamespace(total_memory=24 * GIB)),
    )

    assert subgen._read_exact_gpu_memory() == ("GPU-exact", 24 * GIB, 18 * GIB)
    command = run.call_args.args[0]
    assert "--id=2" in command
    assert "--query-gpu=uuid,driver_version" in command


def test_runtime_identity_fails_closed_if_exact_device_changes(monkeypatch):
    monkeypatch.setattr(subgen, "cuda_device_index", 0)
    monkeypatch.setattr(
        subgen,
        "_read_exact_gpu_memory",
        MagicMock(return_value=("GPU-A", 24 * GIB, 18 * GIB)),
    )
    monkeypatch.setattr(
        subgen,
        "_nvidia_snapshot",
        MagicMock(return_value={"device_id": "GPU-B", "driver_version": "580.12"}),
    )

    with pytest.raises(RuntimeError, match="identity changed"):
        subgen.build_runtime_identity(24 * GIB, "GPU-A")


def test_runtime_identity_must_match_stabilized_device(monkeypatch):
    monkeypatch.setattr(subgen, "cuda_device_index", 0)
    monkeypatch.setattr(
        subgen,
        "_read_exact_gpu_memory",
        MagicMock(return_value=("GPU-B", 24 * GIB, 18 * GIB)),
    )
    monkeypatch.setattr(
        subgen,
        "_nvidia_snapshot",
        MagicMock(return_value={"device_id": "GPU-B", "driver_version": "580.12"}),
    )

    with pytest.raises(RuntimeError, match="identity changed"):
        subgen.build_runtime_identity(24 * GIB, "GPU-A")


def test_canonical_initialization_waits_when_cuda_identity_is_unavailable(monkeypatch):
    sample = resource_management.PressureSample(
        observed_at=100.0,
        host_available_bytes=16 * GIB,
        host_total_bytes=32 * GIB,
        cgroup_current_bytes=2 * GIB,
        cgroup_limit_bytes=20 * GIB,
        cgroup_oom_events=0,
        cgroup_oom_kill_events=0,
    )
    capacity = resource_management.CapacityProfile(
        20 * GIB,
        32 * GIB,
        20 * GIB,
        "cgroup_v2",
        cgroup_version=2,
    )
    runtime = SimpleNamespace(
        model_selection_lock=threading.Lock(),
        model_runtime_initialized=False,
        _resource_management=resource_management,
        segmentation_chunk_minutes=None,
        cuda_device_index=0,
        read_pressure_sample=MagicMock(return_value=sample),
        gpu_memory_reserve_gib=4.0,
        memory_pressure_reserve_gib=None,
        requested_whisper_model="auto",
        transcribe_device="cuda",
        canonical_shared_cuda=True,
        compute_type="float16",
        transcribe_or_translate="translate",
        concurrent_transcriptions=1,
        model_runtime_clock=lambda: 100.0,
        model_runtime_sleep=MagicMock(),
        model_runtime_condition=threading.Condition(threading.Lock()),
        model_admission_closed=False,
        model_release_generation=0,
        model_release_transition=None,
        model_runtime_cancel_event=threading.Event(),
        model_profile_unhealthy=False,
        model_runtime_status={},
        logging=MagicMock(),
    )
    monkeypatch.setattr(
        resource_management,
        "discover_capacity",
        MagicMock(return_value=capacity),
    )
    monkeypatch.setattr(
        model_runtime,
        "_exact_envelope_resolutions",
        MagicMock(return_value=((), None, "catalog_missing", {})),
    )

    decision = model_runtime.initialize_model_runtime(runtime)

    assert decision.selected_model is None
    assert decision.admitted is False
    assert runtime.model_requirement is None
    assert runtime.model_pressure_controller.recovery_requirements == ()
    assert runtime.model_pressure_controller.admission_open is False
    assert runtime.model_runtime_status["envelope_disposition"] == "fail_closed"


def test_cuda_bootstrap_replays_first_priority_epoch_before_opening_admission(
    monkeypatch,
):
    requirement = resource_management.model_load_requirement("tiny")
    decision = SimpleNamespace(
        selected_model="tiny",
        requirement=requirement,
        recovery_requirements=(requirement,),
        explicit=False,
        admitted=True,
        automatic_ceiling="tiny",
        reason="selected",
        provenance="fallback",
        admission=SimpleNamespace(device_admission_bytes=18 * GIB),
        warning=None,
    )
    capacity = resource_management.CapacityProfile(
        20 * GIB,
        32 * GIB,
        20 * GIB,
        "cgroup_v2",
        cgroup_version=2,
    )
    first_priority = PriorityObservation(
        state="clear",
        configured=True,
        heartbeat_age_ms=100,
        source_age_ms=200,
        policy_sha256="1" * 64,
        observation_digest="2" * 64,
        producer_epoch="3" * 32,
        sequence=1,
        observed_monotonic_ns=1,
        source_generation=1,
        source_observed_monotonic_ns=1,
        accepted=True,
        new_publication=True,
        producer_epoch_changed=True,
    )
    duplicate_priority = PriorityObservation(
        **{
            **first_priority.__dict__,
            "new_publication": False,
            "producer_epoch_changed": False,
        }
    )

    def sample(priority):
        return resource_management.PressureSample(
            observed_at=100.0,
            host_available_bytes=16 * GIB,
            host_total_bytes=32 * GIB,
            cgroup_current_bytes=2 * GIB,
            cgroup_limit_bytes=20 * GIB,
            cgroup_oom_events=0,
            cgroup_oom_kill_events=0,
            gpu_total_bytes=24 * GIB,
            gpu_free_bytes=20 * GIB,
            gpu_device_id="GPU-A",
            gpu_observed_at=100.0,
            priority_observation=priority,
        )

    bootstrap_samples = [sample(first_priority)] + [
        sample(duplicate_priority) for _ in range(3)
    ]

    def stabilize(sample_reader, **_kwargs):
        for _ in range(3):
            sample_reader()

    resources = SimpleNamespace(
        discover_capacity=MagicMock(return_value=capacity),
        initial_chunk_seconds=MagicMock(return_value=20 * 60),
        stabilize_gpu_capacity=stabilize,
        gpu_priority_reserve_bytes=MagicMock(return_value=4 * GIB),
        host_reserve_bytes=MagicMock(return_value=GIB),
        select_model=MagicMock(return_value=decision),
        PressureController=resource_management.PressureController,
    )
    runtime = SimpleNamespace(
        model_selection_lock=threading.Lock(),
        model_runtime_condition=threading.Condition(threading.Lock()),
        model_runtime_initialized=False,
        model_release_generation=0,
        model_release_transition=None,
        model_admission_closed=False,
        model_runtime_cancel_event=threading.Event(),
        model_profile_unhealthy=False,
        _resource_management=resources,
        segmentation_chunk_minutes=None,
        cuda_device_index=0,
        read_pressure_sample=MagicMock(side_effect=bootstrap_samples),
        read_resource_pressure_sample=MagicMock(return_value=sample(None)),
        priority_pressure_probe=SimpleNamespace(configured=True),
        priority_pressure_reader=MagicMock(return_value=duplicate_priority),
        gpu_memory_reserve_gib=4.0,
        memory_pressure_reserve_gib=None,
        requested_whisper_model="auto",
        transcribe_device="cuda",
        canonical_shared_cuda=False,
        compute_type="float16",
        transcribe_or_translate="translate",
        concurrent_transcriptions=1,
        model_runtime_clock=lambda: 100.0,
        model_runtime_sleep=MagicMock(),
        model_runtime_status={},
        logging=MagicMock(),
    )
    monkeypatch.setattr(
        model_runtime,
        "_exact_envelope_resolutions",
        MagicMock(return_value=((), None, "catalog_missing", {})),
    )

    assert model_runtime.initialize_model_runtime(runtime) is decision

    controller = runtime.model_pressure_controller
    priority = controller.priority_status_snapshot(
        {
            "model_resident": False,
            "model_load_generation": 0,
            "model_unload_generation": 0,
        }
    )
    assert runtime.read_pressure_sample.call_count == 4
    assert controller.state == "recovering"
    assert controller.admission_open is False
    assert runtime.model_admission_closed is True
    assert priority["transition_sequence"] == 1
    assert priority["transition_observation_digest"] == "2" * 64
    assert priority["distinct_clear_count"] == 0


def test_selection_published_during_release_remains_closed_for_recovery(monkeypatch):
    publication_waiting = threading.Event()

    class ObservedCondition:
        def __init__(self):
            self._condition = threading.Condition(threading.Lock())

        def __enter__(self):
            self._condition.acquire()
            return self

        def __exit__(self, *_args):
            self._condition.release()

        def wait(self, timeout=None):
            publication_waiting.set()
            return self._condition.wait(timeout)

        def notify_all(self):
            self._condition.notify_all()

    class FakeController:
        NORMAL = "normal"

        def __init__(self, **kwargs):
            self.state = self.NORMAL
            self.admission_open = False
            self.recovery_reason = None
            self.recovery_requirements = kwargs["recovery_requirements"]

        def enter_no_safe_model(self, requirements):
            self.recovery_requirements = tuple(requirements)
            self.state = "recovering"
            self.admission_open = False

        def mark_released(self, reason=None):
            self.state = "recovering"
            self.admission_open = False
            self.recovery_reason = reason

    requirement = SimpleNamespace(envelope_resolution=None)
    decision = SimpleNamespace(
        selected_model="medium",
        automatic_ceiling="medium",
        explicit=False,
        warning=None,
        admitted=True,
        reason="selected",
        provenance="fallback",
        requirement=requirement,
        admission=None,
        recovery_requirements=(requirement,),
    )
    capacity = SimpleNamespace(source="physical", cgroup_limit_bytes=None)
    resources = SimpleNamespace(
        discover_capacity=MagicMock(return_value=capacity),
        initial_chunk_seconds=MagicMock(return_value=20 * 60),
        host_reserve_bytes=MagicMock(return_value=GIB),
        select_model=MagicMock(return_value=decision),
        PressureController=FakeController,
    )
    condition = ObservedCondition()
    transition = model_runtime._ReleaseTransition(1, "memory_pressure")
    runtime = SimpleNamespace(
        model_selection_lock=threading.Lock(),
        model_runtime_condition=condition,
        model_runtime_initialized=False,
        model_release_generation=1,
        model_release_transition=transition,
        model_admission_closed=True,
        model_runtime_cancel_event=threading.Event(),
        model_profile_unhealthy=False,
        _resource_management=resources,
        segmentation_chunk_minutes=None,
        cuda_device_index=None,
        read_pressure_sample=MagicMock(
            return_value=resource_management.PressureSample(observed_at=10.0)
        ),
        memory_pressure_reserve_gib=None,
        requested_whisper_model="auto",
        transcribe_device="cpu",
        canonical_shared_cuda=False,
        compute_type="int8",
        transcribe_or_translate="translate",
        concurrent_transcriptions=1,
        model_runtime_clock=lambda: 10.0,
        model_runtime_sleep=MagicMock(),
        model_runtime_status={},
        logging=MagicMock(),
    )
    monkeypatch.setattr(
        model_runtime,
        "_exact_envelope_resolutions",
        MagicMock(return_value=((), None, "catalog_missing", {})),
    )
    results = []
    errors = []

    def initialize():
        try:
            results.append(model_runtime.initialize_model_runtime(runtime))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=initialize)
    worker.start()
    assert publication_waiting.wait(1)
    assert runtime.model_runtime_initialized is False
    assert runtime.model_admission_closed is True

    with condition:
        transition.complete = True
        condition.notify_all()
    worker.join(2)

    assert not worker.is_alive()
    assert errors == []
    assert results == [decision]
    assert runtime.model_runtime_initialized is True
    assert runtime.model_pressure_controller.state == "recovering"
    assert runtime.model_pressure_controller.admission_open is False
    assert runtime.model_admission_closed is True
    assert runtime.model_runtime_status["admission_open"] is False


def test_completed_same_generation_release_is_not_missed_by_selection(monkeypatch):
    selection_started = threading.Event()
    allow_selection = threading.Event()

    class FakeController:
        NORMAL = "normal"

        def __init__(self, **kwargs):
            self.state = self.NORMAL
            self.admission_open = False
            self.recovery_reason = None
            self.recovery_requirements = kwargs["recovery_requirements"]

        def enter_no_safe_model(self, requirements):
            self.recovery_requirements = tuple(requirements)
            self.state = "recovering"
            self.admission_open = False

        def mark_released(self, reason=None):
            self.state = "recovering"
            self.admission_open = False
            self.recovery_reason = reason

    requirement = SimpleNamespace(envelope_resolution=None)
    decision = SimpleNamespace(
        selected_model="medium",
        automatic_ceiling="medium",
        explicit=False,
        warning=None,
        admitted=True,
        reason="selected",
        provenance="fallback",
        requirement=requirement,
        admission=None,
        recovery_requirements=(requirement,),
    )
    capacity = SimpleNamespace(source="physical", cgroup_limit_bytes=None)

    def discover_capacity():
        selection_started.set()
        assert allow_selection.wait(2)
        return capacity

    resources = SimpleNamespace(
        discover_capacity=discover_capacity,
        initial_chunk_seconds=MagicMock(return_value=20 * 60),
        host_reserve_bytes=MagicMock(return_value=GIB),
        select_model=MagicMock(return_value=decision),
        PressureController=FakeController,
    )
    runtime, old_controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model_admission_closed = True
    runtime.model_runtime_initialized = False
    runtime.model_selection_lock = threading.Lock()
    runtime._resource_management = resources
    runtime.segmentation_chunk_minutes = None
    runtime.cuda_device_index = None
    runtime.read_pressure_sample = MagicMock(
        return_value=resource_management.PressureSample(observed_at=10.0)
    )
    runtime.memory_pressure_reserve_gib = None
    runtime.requested_whisper_model = "auto"
    runtime.transcribe_device = "cpu"
    runtime.canonical_shared_cuda = False
    runtime.compute_type = "int8"
    runtime.transcribe_or_translate = "translate"
    runtime.concurrent_transcriptions = 1
    runtime.model_runtime_clock = lambda: 10.0
    runtime.model_runtime_sleep = MagicMock()
    monkeypatch.setattr(
        model_runtime,
        "_exact_envelope_resolutions",
        MagicMock(return_value=((), None, "catalog_missing", {})),
    )
    errors = []

    def initialize():
        try:
            model_runtime.initialize_model_runtime(runtime)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=initialize)
    worker.start()
    assert selection_started.wait(1)
    starting_generation = runtime.model_release_generation

    assert model_runtime.release_model(runtime, reason="memory_pressure") is True
    assert runtime.model_release_generation == starting_generation
    assert runtime.model_release_transition.complete is True
    old_controller.mark_released.assert_called_once_with("memory_pressure")
    allow_selection.set()
    worker.join(2)

    assert not worker.is_alive()
    assert errors == []
    assert runtime.model_runtime_initialized is True
    assert runtime.model_pressure_controller is not old_controller
    assert runtime.model_pressure_controller.state == "recovering"
    assert runtime.model_pressure_controller.admission_open is False
    assert runtime.model_admission_closed is True


@pytest.mark.parametrize(
    "completed",
    [
        SimpleNamespace(returncode=1, stdout=""),
        SimpleNamespace(returncode=0, stdout="GPU-A, 1\nGPU-B, 1\n"),
        SimpleNamespace(returncode=0, stdout="x" * 4097),
    ],
)
def test_nvidia_identity_probe_fails_closed_on_ambiguous_output(monkeypatch, completed):
    monkeypatch.setattr(subgen, "cuda_device_index", 0)
    monkeypatch.setattr(subgen.subprocess, "run", MagicMock(return_value=completed))
    with pytest.raises(RuntimeError, match="NVIDIA"):
        subgen._nvidia_snapshot()


def test_release_drains_every_permit_before_load_lock_and_marks_recovery():
    runtime, controller, _backend, events = coordinated_runtime(permits=2)

    assert model_runtime.release_model(runtime, reason="memory_pressure") is True

    assert runtime.model is None
    assert events[:3] == ["permit.acquire", "permit.acquire", "load_lock.enter"]
    assert events.index("model.unload") > events.index("load_lock.enter")
    assert events.count("permit.release") == 2
    controller.mark_released.assert_called_once_with("memory_pressure")
    assert runtime.model_admission_closed is True
    assert runtime.model_unload_generation == 1


def test_delayed_same_epoch_release_joins_completed_transition():
    runtime, controller, backend, events = coordinated_runtime(permits=2)
    source_generation = runtime.model_release_generation
    assert model_runtime.close_model_admission(runtime) == source_generation + 1

    first = model_runtime._release_model_once(
        runtime,
        reason="memory_pressure",
        source_generation=source_generation,
    )
    second = model_runtime._release_model_once(
        runtime,
        reason="memory_pressure",
        source_generation=source_generation,
    )

    assert first is True
    assert second is True
    backend.unload_model.assert_called_once_with()
    assert events.count("cuda.empty_cache") == 1
    controller.mark_released.assert_called_once_with("memory_pressure")
    assert runtime.model_release_generation == source_generation + 1
    assert runtime.model_unload_generation == 1


def test_source_less_release_joins_successful_closed_epoch():
    runtime, controller, backend, events = coordinated_runtime(permits=1)

    assert model_runtime.release_model(runtime, reason="memory_pressure") is True
    assert model_runtime.release_model(runtime, reason="idle_cleanup") is True

    backend.unload_model.assert_called_once_with()
    assert events.count("cuda.empty_cache") == 1
    controller.mark_released.assert_called_once_with("memory_pressure")
    assert runtime.model_release_generation == 1
    assert runtime.model_unload_generation == 1


def test_failed_closed_epoch_can_be_retried_by_a_new_release_owner():
    first_failure = RuntimeError("transient unload failure")
    unload = MagicMock(side_effect=[first_failure, None])
    runtime, controller, _backend, events = coordinated_runtime(
        permits=1,
        unload=unload,
    )

    with pytest.raises(model_runtime.ModelReleaseError):
        model_runtime.release_model(runtime, reason="memory_pressure")

    assert model_runtime.release_model(runtime, reason="memory_pressure") is True
    assert runtime.model is None
    assert unload.call_count == 2
    assert events.count("cuda.empty_cache") == 1
    controller.mark_released.assert_called_once_with("memory_pressure")
    assert runtime.model_release_generation == 2


def test_release_failure_retains_model_and_never_claims_recovery():
    failure = RuntimeError("unload failed")
    runtime, controller, _backend, events = coordinated_runtime(
        unload=MagicMock(side_effect=failure)
    )
    resident = runtime.model

    with pytest.raises(RuntimeError, match="unload failed") as raised:
        model_runtime.release_model(runtime, reason="memory_pressure")

    assert isinstance(raised.value, model_runtime.ModelReleaseError)
    assert raised.value is not failure
    assert raised.value.__cause__ is None
    assert runtime.model is resident
    assert runtime.model_admission_closed is True
    assert events.count("permit.release") == runtime.model_inference_permit_count
    controller.mark_released.assert_not_called()
    assert runtime.model_unload_generation == 0


@pytest.mark.parametrize(
    "confirmation",
    [
        pytest.param(None, id="missing-or-none"),
        pytest.param(0, id="integer-zero"),
        pytest.param(lambda: False, id="callable"),
        pytest.param(True, id="still-loaded"),
        pytest.param("false", id="string-false"),
    ],
)
def test_release_requires_exact_false_backend_confirmation(confirmation):
    runtime, controller, backend, _events = coordinated_runtime(permits=1)
    if confirmation is None:
        del backend.model_is_loaded
    else:
        backend.model_is_loaded = confirmation
    resident = runtime.model
    runtime.resident_model_identity_sha256 = "a" * 64

    with pytest.raises(
        model_runtime.ModelReleaseError,
        match="did not confirm release",
    ):
        model_runtime.release_model(runtime, reason="memory_pressure")

    assert runtime.model is resident
    assert runtime.resident_model_identity_sha256 == "a" * 64
    assert runtime.model_unload_generation == 0
    assert runtime.model_admission_closed is True
    assert controller.state == "yielding"
    assert controller.admission_open is False
    assert controller.recovery_reason == "model_release_failed"
    controller.mark_released.assert_not_called()


def test_failed_release_error_keeps_late_admission_waiters_fail_closed():
    failure = RuntimeError("unload failed")
    runtime, _controller, _backend, _events = coordinated_runtime(
        unload=MagicMock(side_effect=failure)
    )
    with pytest.raises(RuntimeError) as owner:
        model_runtime.release_model(runtime, reason="memory_pressure")

    with pytest.raises(RuntimeError) as waiter:
        model_runtime.wait_for_model_admission(runtime)

    assert isinstance(owner.value, model_runtime.ModelReleaseError)
    assert isinstance(waiter.value, model_runtime.ModelReleaseError)
    assert owner.value is not waiter.value
    assert owner.value is not failure
    assert waiter.value is not failure
    assert owner.value.__cause__ is None
    assert waiter.value.__cause__ is None
    assert runtime.model_admission_closed is True


def test_stale_loader_waits_for_yield_release_before_controller_recovery():
    wait_entered = threading.Event()

    class ObservedCondition:
        def __init__(self):
            self._condition = threading.Condition(threading.Lock())

        def __enter__(self):
            self._condition.acquire()
            return self

        def __exit__(self, *_args):
            self._condition.release()

        def wait(self, timeout=None):
            wait_entered.set()
            return self._condition.wait(timeout)

        def notify_all(self):
            self._condition.notify_all()

    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model_runtime_condition = ObservedCondition()
    runtime.model = None
    runtime.stable_whisper = SimpleNamespace(load_faster_whisper=MagicMock())
    controller.YIELDING = "yielding"
    controller.state = controller.YIELDING
    controller.admission_open = False
    recovery_called = threading.Event()

    def recover(_cancelled=None):
        recovery_called.set()
        controller.state = controller.NORMAL
        controller.admission_open = True
        return True

    controller.wait_for_recovery = MagicMock(side_effect=recover)
    source_generation = runtime.model_release_generation
    model_runtime.close_model_admission(runtime)

    load_result = model_runtime._load_model_once(runtime, source_generation)
    assert load_result is model_runtime._LOAD_DEFERRED_STALE_GENERATION
    runtime.stable_whisper.load_faster_whisper.assert_not_called()

    results = []
    waiter = threading.Thread(
        target=lambda: results.append(
            model_runtime.wait_for_model_admission(
                runtime,
                runtime.model_runtime_cancel_event,
            )
        )
    )
    waiter.start()
    assert wait_entered.wait(1)
    assert not recovery_called.is_set()

    model_runtime._release_model_once(
        runtime,
        reason="memory_pressure",
        source_generation=source_generation,
    )
    waiter.join(2)

    assert not waiter.is_alive()
    assert results == [True]
    assert recovery_called.is_set()
    controller.wait_for_recovery.assert_called_once_with(
        runtime.model_runtime_cancel_event
    )


def test_recovery_waiter_rechecks_after_a_superseding_release():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model_admission_closed = True
    controller.state = "recovering"
    controller.admission_open = False
    superseding_started = threading.Event()
    recovery_calls = 0

    def recover(_cancelled=None):
        nonlocal recovery_calls
        recovery_calls += 1
        controller.state = controller.NORMAL
        controller.admission_open = True
        if recovery_calls == 1:
            with runtime.model_runtime_condition:
                runtime.model_release_generation += 1
                runtime.model_release_transition = model_runtime._ReleaseTransition(
                    runtime.model_release_generation,
                    "memory_pressure",
                )
                runtime.model_admission_closed = True
                runtime.model_runtime_condition.notify_all()
            superseding_started.set()
        return True

    controller.wait_for_recovery = MagicMock(side_effect=recover)
    results = []
    errors = []

    def wait():
        try:
            results.append(
                model_runtime.wait_for_model_recovery(
                    runtime,
                    runtime.model_runtime_cancel_event,
                )
            )
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=wait)
    worker.start()
    assert superseding_started.wait(1)
    assert worker.is_alive()

    with runtime.model_runtime_condition:
        controller.state = "recovering"
        controller.admission_open = False
        runtime.model_release_transition.complete = True
        runtime.model_runtime_condition.notify_all()
    worker.join(2)

    assert not worker.is_alive()
    assert errors == []
    assert results == [True]
    assert recovery_calls == 2


def test_recovery_waiter_never_advances_yielding_before_release():
    wait_entered = threading.Event()

    class ObservedCondition:
        def __init__(self):
            self._condition = threading.Condition(threading.Lock())

        def __enter__(self):
            self._condition.acquire()
            return self

        def __exit__(self, *_args):
            self._condition.release()

        def wait(self, timeout=None):
            wait_entered.set()
            return self._condition.wait(timeout)

        def notify_all(self):
            self._condition.notify_all()

    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model_runtime_condition = ObservedCondition()
    runtime.model_admission_closed = True
    controller.YIELDING = "yielding"
    controller.state = controller.YIELDING
    controller.admission_open = False

    def recover(_cancelled=None):
        controller.state = controller.NORMAL
        controller.admission_open = True
        return True

    controller.wait_for_recovery = MagicMock(side_effect=recover)
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            model_runtime.wait_for_model_recovery(
                runtime,
                runtime.model_runtime_cancel_event,
            )
        )
    )
    worker.start()
    assert wait_entered.wait(1)
    controller.wait_for_recovery.assert_not_called()

    model_runtime.release_model(runtime, reason="memory_pressure")
    worker.join(2)

    assert not worker.is_alive()
    assert results == [True]
    controller.wait_for_recovery.assert_called_once_with(
        runtime.model_runtime_cancel_event
    )


def test_reopen_validates_the_published_controller_under_the_runtime_lock():
    enter_attempted = threading.Event()

    class ObservedCondition:
        def __init__(self):
            self._condition = threading.Condition(threading.Lock())

        def __enter__(self):
            enter_attempted.set()
            self._condition.acquire()
            return self

        def __exit__(self, *_args):
            self._condition.release()

        def wait(self, timeout=None):
            return self._condition.wait(timeout)

        def notify_all(self):
            self._condition.notify_all()

    runtime, old_controller, _backend, _events = coordinated_runtime(permits=1)
    condition = ObservedCondition()
    runtime.model_runtime_condition = condition
    runtime.model_admission_closed = True
    old_controller.state = old_controller.NORMAL
    old_controller.admission_open = True
    replacement = SimpleNamespace(
        NORMAL="normal",
        state="recovering",
        admission_open=False,
    )
    results = []

    with condition:
        enter_attempted.clear()
        worker = threading.Thread(
            target=lambda: results.append(model_runtime.reopen_model_admission(runtime))
        )
        worker.start()
        assert enter_attempted.wait(1)
        runtime.model_pressure_controller = replacement
    worker.join(1)

    assert not worker.is_alive()
    assert results == [False]
    assert runtime.model_admission_closed is True


def test_cancelled_worker_stops_waiting_for_an_inference_permit():
    runtime, _controller, _backend, _events = coordinated_runtime(permits=1)
    attempted = threading.Event()

    class UnavailableSemaphore:
        def acquire(self, timeout=None):
            attempted.set()
            runtime.model_runtime_cancel_event.wait(timeout)
            return False

        def release(self):
            raise AssertionError("an unavailable permit must not be released")

    runtime.model_inference_semaphore = UnavailableSemaphore()
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            model_runtime._acquire_inference_slot(
                runtime,
                runtime.model_runtime_cancel_event,
            )
        )
    )
    worker.start()
    assert attempted.wait(1)
    runtime.model_runtime_cancel_event.set()
    worker.join(1)

    assert not worker.is_alive()
    assert result == [None]


def test_start_model_does_not_initialize_after_shutdown_cancellation():
    runtime, _controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model = None
    runtime.model_runtime_cancel_event.set()
    runtime.initialize_model_runtime = MagicMock()
    runtime.stable_whisper = SimpleNamespace(load_faster_whisper=MagicMock())

    with pytest.raises(
        model_runtime.ModelRuntimeCancelled,
        match="admission was cancelled",
    ):
        model_runtime.start_model(runtime)

    runtime.initialize_model_runtime.assert_not_called()
    runtime.stable_whisper.load_faster_whisper.assert_not_called()


def test_permit_cancelled_during_acquire_is_returned_without_admission():
    runtime, _controller, _backend, events = coordinated_runtime(permits=1)

    class CancelOnAcquire:
        def acquire(self, timeout=None):
            assert timeout == runtime.model_permit_wait_seconds
            events.append("permit.acquire")
            runtime.model_runtime_cancel_event.set()
            return True

        def release(self):
            events.append("permit.release")

    runtime.model_inference_semaphore = CancelOnAcquire()

    generation = model_runtime._acquire_inference_slot(
        runtime,
        runtime.model_runtime_cancel_event,
    )

    assert generation is None
    assert events[-2:] == ["permit.acquire", "permit.release"]
    assert runtime.model_active_inferences == 0


def test_cache_release_failure_stays_closed_after_backend_unloads():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    recorder = RuntimeReceiptRecorder()
    runtime.runtime_receipt_coordinator = recorder
    runtime.resident_model_identity_sha256 = "d" * 64
    failure = RuntimeError("cache failed")
    runtime.torch.cuda.empty_cache.side_effect = failure

    with pytest.raises(RuntimeError, match="cache failed") as raised:
        model_runtime.release_model(runtime, reason="memory_pressure")

    assert isinstance(raised.value, model_runtime.ModelReleaseError)
    assert raised.value is not failure
    assert raised.value.__cause__ is None
    assert runtime.model is None
    assert runtime.resident_model_identity_sha256 is None
    assert runtime.model_unload_generation == 1
    assert runtime.model_admission_closed is True
    controller.mark_released.assert_called_once_with("memory_pressure")
    assert controller.state == "recovering"
    assert controller.admission_open is False
    assert any(
        state["model_resident"] is False
        and state["model_identity_sha256"] is None
        and state["model_unload_generation"] == 1
        and state["admission_open"] is False
        for state in recorder.states
    )


def test_post_unload_logging_failure_preserves_lossless_receipt_transition(
    tmp_path, monkeypatch
):
    journals = []

    class MemoryJournal:
        def __init__(self, path):
            self.path = path
            self.payloads = []
            journals.append(self)

        def append(self, payload):
            self.payloads.append(payload)

        def close(self):
            pass

    monkeypatch.setattr(runtime_receipts, "_SecureJournal", MemoryJournal)
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    controller.gate_priority_status_snapshot = MagicMock(
        return_value={
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
            "source_generation": None,
            "admission_open": False,
        }
    )
    runtime.resident_model_identity_sha256 = "d" * 64
    coordinator = runtime_receipts.RuntimeReceiptCoordinator(
        identity=runtime_receipts.RuntimeIdentity(
            epoch="1" * 32,
            started_monotonic_ns=1,
        ),
        config=runtime_receipts.GateReceiptConfig(
            receipt_file=(tmp_path / "runtime-receipts.jsonl").resolve(),
            gate_token_sha256="2" * 64,
            phase_a_workload_sha256="3" * 64,
            phase_b_workload_sha256="4" * 64,
        ),
        condition=runtime.model_runtime_condition,
    )
    with runtime.model_runtime_condition:
        coordinator.initialize_locked(
            model_runtime.runtime_receipt_state_locked(runtime)
        )
    runtime.runtime_receipt_coordinator = coordinator
    receipt_seen_before_logging_failure = []

    def fail_after_observing_receipt(*_args, **_kwargs):
        documents = [json.loads(payload) for payload in journals[0].payloads]
        receipt_seen_before_logging_failure.append(documents[-1])
        raise RuntimeError("logging failed")

    runtime.logging.info.side_effect = fail_after_observing_receipt

    with pytest.raises(model_runtime.ModelReleaseError, match="logging failed"):
        model_runtime.release_model(runtime, reason="memory_pressure")

    assert runtime.model is None
    assert runtime.resident_model_identity_sha256 is None
    assert runtime.model_unload_generation == 1
    documents = [json.loads(payload) for payload in journals[0].payloads]
    assert receipt_seen_before_logging_failure == [documents[-1]]
    assert documents[-1]["model_resident"] is False
    assert documents[-1]["model_identity_sha256"] is None
    assert documents[-1]["model_unload_generation"] == 1
    assert documents[-1]["admission_open"] is False


def test_cache_failure_does_not_retain_unloaded_resident_through_traceback():
    runtime, _controller, backend, _events = coordinated_runtime(permits=1)
    finalized = threading.Event()

    class Resident:
        def __init__(self, model):
            self.model = model

    runtime.model = Resident(backend)
    weakref.finalize(runtime.model, finalized.set)
    runtime.gc = gc

    def empty_cache():
        failure = RuntimeError("cache cycle failed")
        failure.self_cycle = failure
        raise failure

    runtime.torch.cuda.empty_cache = empty_cache

    with pytest.raises(
        model_runtime.ModelReleaseError,
        match="cache cycle failed",
    ) as raised:
        model_runtime.release_model(runtime, reason="memory_pressure")

    assert runtime.model is None
    assert finalized.is_set()
    assert raised.value.__cause__ is None


def test_concurrent_release_callers_join_one_transition_and_one_error():
    entered = threading.Event()
    continue_unload = threading.Event()
    failure = RuntimeError("shared unload failure")

    def unload():
        entered.set()
        assert continue_unload.wait(2)
        raise failure

    runtime, _controller, _backend, _events = coordinated_runtime(
        unload=MagicMock(side_effect=unload)
    )
    errors = []

    def release():
        try:
            model_runtime.release_model(runtime, reason="memory_pressure")
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=release)
    second = threading.Thread(target=release)
    first.start()
    assert entered.wait(2)
    second.start()
    continue_unload.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert len(errors) == 2
    assert all(isinstance(error, model_runtime.ModelReleaseError) for error in errors)
    assert all("shared unload failure" in str(error) for error in errors)
    assert errors[0] is not errors[1]
    assert all(error.__cause__ is None for error in errors)


def test_pressure_callback_unwinds_gate_before_propagating_to_caller(monkeypatch):
    events = []
    pressure = resource_management.MemoryPressureYield("pressure")

    class Gate:
        def __enter__(self):
            events.append("gate.enter")

        def __exit__(self, *_args):
            events.append("gate.exit")

    original = MagicMock(side_effect=lambda *_args: events.append("progress"))

    def transcribe(*_args, **kwargs):
        kwargs["progress_callback"](1, 2)

    controller = SimpleNamespace(
        check_or_raise=MagicMock(side_effect=pressure),
        admission_open=False,
        recovery_reason="memory_pressure",
    )
    runtime = SimpleNamespace(
        model_inference_semaphore=Gate(),
        model=SimpleNamespace(transcribe=transcribe),
        memory_pressure_yield=True,
        model_pressure_controller=controller,
        _resource_management=resource_management,
    )
    release = MagicMock(side_effect=lambda *_args, **_kwargs: events.append("release"))
    monkeypatch.setattr(model_runtime, "release_model", release)

    with pytest.raises(resource_management.MemoryPressureYield) as raised:
        model_runtime.transcribe_with_model(
            runtime,
            "audio",
            progress_callback=original,
        )

    assert raised.value is not pressure
    assert str(raised.value) == str(pressure)
    assert events == ["gate.enter", "progress", "gate.exit"]
    original.assert_called_once_with(1, 2)
    release.assert_not_called()

    assert model_runtime.release_after_pressure(runtime, raised.value) is None
    assert events == ["gate.enter", "progress", "gate.exit", "release"]


def test_segmented_caller_releases_after_pressure_audio_is_collectable():
    class Audio:
        pass

    runtime, controller, _backend, events = coordinated_runtime(permits=1)
    runtime.memory_pressure_yield = True
    pressure = resource_management.MemoryPressureYield("memory pressure")
    references = []
    attempts = 0
    cache_observations = []

    def extract(_window):
        audio = Audio()
        references.append(weakref.ref(audio))
        return audio

    def transcribe(audio, **kwargs):
        nonlocal attempts
        attempts += 1
        assert references[-1]() is audio
        if attempts == 1:
            kwargs["progress_callback"](1, 1)
        return SimpleNamespace(language=None, segments=[])

    runtime.model.transcribe = transcribe
    controller.check_or_raise = MagicMock(side_effect=pressure)

    def empty_cache():
        gc.collect()
        cache_observations.append(references[0]() is None)
        events.append("cuda.empty_cache")

    runtime.torch.cuda.empty_cache = MagicMock(side_effect=empty_cache)

    def release(error, _window):
        assert error is not pressure
        assert str(error) == str(pressure)
        gc.collect()
        assert references[0]() is None
        assert model_runtime.release_after_pressure(runtime, error) is True

    def wait(_error, _window):
        runtime.model = SimpleNamespace(
            model=SimpleNamespace(
                unload_model=MagicMock(),
                model_is_loaded=False,
            ),
            transcribe=transcribe,
        )
        controller.state = controller.NORMAL
        controller.admission_open = True
        assert model_runtime.reopen_model_admission(runtime) is True

    result = segmentation.run_segmented_transcription(
        media_duration=600,
        adaptive=resource_management.AdaptiveChunkState(600),
        extract_chunk=extract,
        transcribe_chunk=lambda audio, _window, progress: (
            model_runtime.transcribe_with_model(
                runtime,
                audio,
                progress_callback=progress,
            )
        ),
        release_failure=release,
        wait_for_recovery=wait,
        result_factory=lambda payload: SimpleNamespace(
            language=payload["language"],
            segments=[],
        ),
    )

    assert result.segments == []
    assert attempts == 3
    assert cache_observations == [True]
    assert events.count("cuda.empty_cache") == 1


@pytest.mark.parametrize(
    ("backend_failure", "expected_cuda_oom_generation"),
    [
        (MemoryError("host allocation failed"), 0),
        (RuntimeError("CUDA out of memory. Tried to allocate 64 MiB"), 1),
    ],
    ids=("python-memory-error", "cuda-oom"),
)
def test_inference_allocation_failure_is_fresh_ticketed_and_released_only_by_caller(
    backend_failure,
    expected_cuda_oom_generation,
):
    failure_type, release_after_failure = required_inference_allocation_api()
    runtime, controller, backend, events = coordinated_runtime(permits=1)
    resident = runtime.model
    initial_generation = runtime.model_release_generation
    runtime.model.transcribe = MagicMock(side_effect=backend_failure)

    with pytest.raises(failure_type) as raised:
        model_runtime.transcribe_with_model(runtime, "chunk-audio")

    propagated = raised.value
    assert propagated is not backend_failure
    assert backend_failure.__traceback__ is None
    assert propagated.__cause__ is None
    assert runtime.model is resident
    assert runtime.model_admission_closed is True
    assert runtime.model_release_generation == initial_generation + 1
    backend.unload_model.assert_not_called()
    controller.mark_released.assert_not_called()
    assert "cuda.empty_cache" not in events
    assert events.count("permit.release") == 1
    assert runtime.cuda_oom_generation == expected_cuda_oom_generation

    assert release_after_failure(runtime, propagated) is True

    backend.unload_model.assert_called_once_with()
    controller.mark_released.assert_called_once_with("inference_allocation_failure")
    assert events.count("cuda.empty_cache") == 1
    assert events.index("permit.release") < events.index("model.unload")


def test_cuda_oom_classifier_accepts_explicit_backend_signals_only():
    class OutOfMemoryError(RuntimeError):
        __module__ = "torch.cuda"

    assert model_runtime.is_cuda_oom_failure(OutOfMemoryError()) is True
    assert (
        model_runtime.is_cuda_oom_failure(RuntimeError("CUDA error:\n  out of memory"))
        is True
    )
    assert model_runtime.is_cuda_oom_failure(MemoryError("host allocation")) is False
    assert model_runtime.is_cuda_oom_failure(OSError("host ENOMEM")) is False


def test_user_progress_error_is_not_misclassified_as_pressure(monkeypatch):
    failure = LookupError("user callback failed")

    def transcribe(*_args, **kwargs):
        kwargs["progress_callback"](1, 2)

    runtime = SimpleNamespace(
        model_inference_semaphore=threading.BoundedSemaphore(1),
        model=SimpleNamespace(transcribe=transcribe),
        memory_pressure_yield=True,
        model_pressure_controller=SimpleNamespace(
            check_or_raise=MagicMock(),
            admission_open=True,
        ),
        _resource_management=resource_management,
    )
    release = MagicMock()
    monkeypatch.setattr(model_runtime, "release_model", release)

    with pytest.raises(LookupError, match="user callback failed") as raised:
        model_runtime.transcribe_with_model(
            runtime,
            "audio",
            progress_callback=MagicMock(side_effect=failure),
        )

    assert raised.value is failure
    release.assert_not_called()


def test_fresh_capacity_drop_blocks_loader_before_any_backend_attempt():
    events = []
    decision = SimpleNamespace(admitted=False)

    def deny_load(*_args, **_kwargs):
        controller.state = "recovering"
        controller.admission_open = False
        return decision

    controller = SimpleNamespace(
        NORMAL="normal",
        state="normal",
        admission_open=True,
        immediate_load_admission=MagicMock(side_effect=deny_load),
        wait_for_recovery=MagicMock(return_value=False),
    )
    runtime = SimpleNamespace(
        model=None,
        model_requirement=object(),
        model_pressure_controller=controller,
        read_pressure_sample=MagicMock(),
        model_runtime_condition=threading.Condition(threading.Lock()),
        model_admission_closed=False,
        model_release_generation=0,
        model_release_transition=None,
        model_active_inferences=0,
        model_inference_permit_count=1,
        model_inference_semaphore=threading.BoundedSemaphore(1),
        model_load_lock=RecordingLock(events, "load_lock"),
        stable_whisper=SimpleNamespace(load_faster_whisper=MagicMock()),
        whisper_model="large-v3",
        model_location="/models",
        transcribe_device="cuda",
        cuda_device_index=0,
        whisper_threads=4,
        concurrent_transcriptions=1,
        compute_type="float16",
        whisper_model_revision_commit="a" * 40,
        logging=MagicMock(),
    )

    with pytest.raises(RuntimeError, match="recovery was cancelled"):
        model_runtime.start_model(runtime)

    runtime.stable_whisper.load_faster_whisper.assert_not_called()
    assert runtime.model is None
    assert runtime.model_admission_closed is True


def test_successful_loader_uses_fixed_revision_and_exact_cuda_index():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model = None
    runtime.model_requirement = object()
    controller.immediate_load_admission = MagicMock(
        return_value=SimpleNamespace(admitted=True)
    )
    runtime.read_pressure_sample = MagicMock()
    loaded = object()
    loader = MagicMock(return_value=loaded)
    runtime.stable_whisper = SimpleNamespace(load_faster_whisper=loader)
    runtime.whisper_model = "large-v3"
    runtime.model_location = "/models"
    runtime.whisper_threads = 6
    runtime.concurrent_transcriptions = 1
    runtime.compute_type = "float16"
    runtime.whisper_model_revision_commit = "a" * 40

    model_runtime.start_model(runtime)

    assert runtime.model is loaded
    assert runtime.model_load_generation == 1
    model_runtime.start_model(runtime)
    assert runtime.model_load_generation == 1
    loader.assert_called_once_with(
        "large-v3",
        download_root="/models",
        device="cuda",
        device_index=0,
        cpu_threads=6,
        num_workers=1,
        compute_type="float16",
        revision="a" * 40,
    )


def test_model_load_allocation_failure_releases_waits_and_retries_same_model():
    runtime, controller, _backend, events = coordinated_runtime(permits=1)
    loaded = object()
    loader = MagicMock(side_effect=[MemoryError("CUDA out of memory"), loaded])
    configure_model_loading(runtime, controller, loader)

    model_runtime.start_model(runtime)

    assert runtime.model is loaded
    assert loader.call_count == 2
    assert runtime.model_load_allocation_failures == 0
    assert runtime.cuda_oom_generation == 1
    assert runtime.model_load_generation == 1
    assert events.count("cuda.empty_cache") == 1
    controller.mark_released.assert_called_once_with("model_load_allocation_failure")
    controller.wait_for_recovery.assert_called_once_with(
        runtime.model_runtime_cancel_event
    )


def test_loader_exception_payload_is_collected_before_cuda_cache_release():
    runtime, controller, _backend, events = coordinated_runtime(permits=1)
    loaded = object()
    finalized = threading.Event()
    attempts = 0

    class Payload:
        pass

    def loader(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            payload = Payload()
            weakref.finalize(payload, finalized.set)
            failure = MemoryError("CUDA out of memory")
            failure.payload = payload
            raise failure
        return loaded

    def collect():
        events.append("gc.collect")
        return gc.collect()

    def empty_cache():
        assert finalized.is_set()
        events.append("cuda.empty_cache")

    runtime.gc.collect = MagicMock(side_effect=collect)
    runtime.torch.cuda.empty_cache = MagicMock(side_effect=empty_cache)
    configure_model_loading(runtime, controller, loader)

    model_runtime.start_model(runtime)

    assert runtime.model is loaded
    assert attempts == 2
    assert finalized.is_set()
    assert events.index("gc.collect") < events.index("cuda.empty_cache")


def test_two_admitted_model_load_allocation_failures_require_operator_attention():
    runtime, controller, _backend, events = coordinated_runtime(permits=1)
    loader = MagicMock(side_effect=MemoryError("CUDA out of memory"))
    configure_model_loading(runtime, controller, loader)

    with pytest.raises(
        model_runtime.ModelLoadProfileUnhealthy,
        match="operator attention",
    ) as raised:
        model_runtime.start_model(runtime)

    assert loader.call_count == 2
    assert events.count("cuda.empty_cache") == 2
    assert runtime.cuda_oom_generation == 2
    assert runtime.model_load_generation == 0
    assert runtime.model_profile_unhealthy is True
    assert runtime.model_profile_unhealthy_reason == "model_load_profile_unhealthy"
    assert runtime.model_admission_closed is True
    assert controller.wait_for_recovery.call_count == 1
    with pytest.raises(model_runtime.ModelLoadProfileUnhealthy) as waiter:
        model_runtime.wait_for_model_admission(runtime)
    assert waiter.value is not raised.value
    assert raised.value.__cause__ is None
    assert waiter.value.__cause__ is None
    status = model_runtime.runtime_status(runtime)
    assert status["recovery_reason"] == "model_load_profile_unhealthy"
    assert status["admission_open"] is False


def test_terminal_model_profile_cannot_be_reopened_after_controller_recovery():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    model_runtime._mark_model_profile_unhealthy(runtime)
    controller.state = controller.NORMAL
    controller.admission_open = True

    assert model_runtime.reopen_model_admission(runtime) is False
    with pytest.raises(
        model_runtime.ModelLoadProfileUnhealthy,
        match="operator attention",
    ):
        model_runtime.wait_for_model_recovery(
            runtime,
            runtime.model_runtime_cancel_event,
        )

    assert runtime.model_admission_closed is True


def test_terminal_profile_interrupts_waiter_already_inside_controller_recovery():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model_admission_closed = True
    controller.state = "recovering"
    controller.admission_open = False
    recovery_entered = threading.Event()

    def recover(cancelled=None):
        recovery_entered.set()
        assert cancelled.wait(2)
        return False

    controller.wait_for_recovery = MagicMock(side_effect=recover)
    errors = []

    def wait():
        try:
            model_runtime.wait_for_model_recovery(
                runtime,
                runtime.model_runtime_cancel_event,
            )
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=wait)
    worker.start()
    assert recovery_entered.wait(1)

    model_runtime._mark_model_profile_unhealthy(runtime)
    worker.join(2)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], model_runtime.ModelLoadProfileUnhealthy)
    assert runtime.model_admission_closed is True
    controller.wait_for_recovery.assert_called_once_with(
        runtime.model_runtime_cancel_event
    )


def test_nonallocation_model_load_error_propagates_without_resource_retry():
    runtime, controller, _backend, events = coordinated_runtime(permits=1)
    failure = RuntimeError("model metadata is invalid")
    loader = MagicMock(side_effect=failure)
    configure_model_loading(runtime, controller, loader)

    with pytest.raises(RuntimeError) as raised:
        model_runtime.start_model(runtime)

    assert raised.value is failure
    loader.assert_called_once()
    assert runtime.model_load_allocation_failures == 0
    assert "cuda.empty_cache" not in events
    controller.wait_for_recovery.assert_not_called()


def test_concurrent_no_safe_waiters_share_one_reselection_and_controller():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model = None
    runtime.model_admission_closed = True
    runtime.model_runtime_initialized = True
    runtime.model_requirement = None
    controller.state = "recovering"
    controller.admission_open = False
    controller.recovery_requirements = (object(),)
    waiters = threading.Barrier(2)
    initialization_count = 0
    selected_requirement = object()
    loaded = object()

    def recover(_cancelled=None):
        waiters.wait(timeout=2)
        controller.state = "normal"
        controller.admission_open = True
        return True

    controller.wait_for_recovery = recover

    def initialize():
        nonlocal initialization_count
        with runtime.model_selection_lock:
            if runtime.model_runtime_initialized:
                return
            initialization_count += 1
            runtime.model_runtime_initialized = True
            runtime.model_requirement = selected_requirement

    runtime.model_selection_lock = threading.Lock()
    runtime.initialize_model_runtime = initialize
    runtime.read_pressure_sample = MagicMock()
    controller.immediate_load_admission = MagicMock(
        return_value=SimpleNamespace(admitted=True)
    )
    runtime.stable_whisper = SimpleNamespace(
        load_faster_whisper=MagicMock(return_value=loaded)
    )
    runtime.whisper_model = "tiny"
    runtime.model_location = "/models"
    runtime.whisper_threads = 2
    runtime.concurrent_transcriptions = 1
    runtime.compute_type = "float16"
    runtime.whisper_model_revision_commit = None
    errors = []

    def run():
        try:
            model_runtime.start_model(runtime)
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=run) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert initialization_count == 1
    assert runtime.model_pressure_controller is controller
    assert runtime.model is loaded


def test_empty_recovery_bootstrap_reselects_after_one_bounded_wait():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model = None
    runtime.model_runtime_initialized = False
    runtime.model_requirement = None
    runtime.model_admission_closed = True
    runtime.model_selection_lock = threading.Lock()
    controller.state = "recovering"
    controller.admission_open = False
    controller.recovery_requirements = ()
    controller.sample_interval_seconds = 5.0
    selected_requirement = object()
    loaded = object()
    initialization_count = 0
    waits = []

    class Cancellation:
        @staticmethod
        def is_set():
            return False

        @staticmethod
        def wait(timeout):
            waits.append(timeout)
            return False

    runtime.model_runtime_cancel_event = Cancellation()
    runtime.model_runtime_sleep = MagicMock()

    def initialize():
        nonlocal initialization_count
        with runtime.model_selection_lock:
            if runtime.model_runtime_initialized:
                return
            initialization_count += 1
            runtime.model_runtime_initialized = True
            if initialization_count == 1:
                runtime.model_requirement = None
                controller.recovery_requirements = ()
                controller.state = "recovering"
                controller.admission_open = False
                runtime.model_admission_closed = True
                return
            runtime.model_requirement = selected_requirement
            controller.recovery_requirements = (selected_requirement,)
            controller.state = controller.NORMAL
            controller.admission_open = True
            runtime.model_admission_closed = False

    runtime.initialize_model_runtime = initialize
    runtime.read_pressure_sample = MagicMock()
    controller.immediate_load_admission = MagicMock(
        return_value=SimpleNamespace(admitted=True)
    )
    runtime.stable_whisper = SimpleNamespace(
        load_faster_whisper=MagicMock(return_value=loaded)
    )
    runtime.whisper_model = "large-v3"
    runtime.model_location = "/models"
    runtime.whisper_threads = 4
    runtime.concurrent_transcriptions = 1
    runtime.compute_type = "float16"
    runtime.whisper_model_revision_commit = "a" * 40

    model_runtime.start_model(runtime)

    assert initialization_count == 2
    assert waits == [5.0]
    assert runtime.model_runtime_sleep.call_count == 0
    assert runtime.model is loaded
    runtime.stable_whisper.load_faster_whisper.assert_called_once()


def test_queued_inference_waits_through_recovery_then_reloads_once():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model = None
    runtime.model_admission_closed = True
    runtime.model_requirement = object()
    controller.state = "recovering"
    controller.admission_open = False
    wait_entered = threading.Event()
    allow_recovery = threading.Event()

    def wait_for_recovery(_cancelled=None):
        wait_entered.set()
        assert allow_recovery.wait(2)
        controller.state = "normal"
        controller.admission_open = True
        return True

    controller.wait_for_recovery = wait_for_recovery
    controller.immediate_load_admission = MagicMock(
        return_value=SimpleNamespace(admitted=True)
    )
    runtime.read_pressure_sample = MagicMock()
    transcribe = MagicMock(return_value="done")
    loaded = SimpleNamespace(transcribe=transcribe)
    loader = MagicMock(return_value=loaded)
    runtime.stable_whisper = SimpleNamespace(load_faster_whisper=loader)
    runtime.whisper_model = "medium"
    runtime.model_location = "/models"
    runtime.whisper_threads = 4
    runtime.concurrent_transcriptions = 1
    runtime.compute_type = "float16"
    runtime.whisper_model_revision_commit = None
    runtime.memory_pressure_yield = False
    results = []

    worker = threading.Thread(
        target=lambda: results.append(
            model_runtime.transcribe_with_model(runtime, "audio")
        )
    )
    worker.start()
    assert wait_entered.wait(2)
    transcribe.assert_not_called()
    loader.assert_not_called()
    allow_recovery.set()
    worker.join(2)

    assert not worker.is_alive()
    assert results == ["done"]
    loader.assert_called_once()
    transcribe.assert_called_once_with("audio")


def test_two_pressure_workers_share_one_release_while_queued_work_waits():
    runtime, controller, backend, events = coordinated_runtime(permits=2)
    runtime.memory_pressure_yield = True
    pressure = resource_management.MemoryPressureYield("memory pressure")
    both_callbacks_entered = threading.Event()
    allow_callbacks_to_raise = threading.Event()
    recovery_entered = threading.Event()
    allow_recovery = threading.Event()
    delayed_caught = threading.Event()
    allow_delayed_release = threading.Event()
    callback_count = 0
    callback_lock = threading.Lock()

    def check_or_raise():
        nonlocal callback_count
        controller.state = "yielding"
        controller.admission_open = False
        with callback_lock:
            callback_count += 1
            if callback_count == 2:
                both_callbacks_entered.set()
        assert allow_callbacks_to_raise.wait(2)
        raise pressure

    def transcribe_under_pressure(*_args, **kwargs):
        kwargs["progress_callback"](1, 1)

    def mark_released(reason=None):
        controller.state = "recovering"
        controller.admission_open = False
        controller.recovery_reason = reason

    def recover(_cancelled=None):
        recovery_entered.set()
        assert allow_recovery.wait(2)
        controller.state = controller.NORMAL
        controller.admission_open = True
        return True

    runtime.model.transcribe = transcribe_under_pressure
    controller.check_or_raise = check_or_raise
    controller.mark_released = MagicMock(side_effect=mark_released)
    controller.wait_for_recovery = MagicMock(side_effect=recover)
    controller.immediate_load_admission = MagicMock(
        return_value=SimpleNamespace(admitted=True)
    )
    runtime.model_requirement = object()
    runtime.read_pressure_sample = MagicMock()
    reloaded_transcribe = MagicMock(return_value="queued-done")
    reloaded = SimpleNamespace(
        model=SimpleNamespace(unload_model=MagicMock(), model_is_loaded=False),
        transcribe=reloaded_transcribe,
    )
    loader = MagicMock(return_value=reloaded)
    runtime.stable_whisper = SimpleNamespace(load_faster_whisper=loader)
    runtime.whisper_model = "medium"
    runtime.model_location = "/models"
    runtime.whisper_threads = 4
    runtime.concurrent_transcriptions = 2
    runtime.compute_type = "float16"
    runtime.whisper_model_revision_commit = None
    active_errors = []
    queued_results = []

    def active_worker(*, delayed=False):
        pressure_error = None
        try:
            model_runtime.transcribe_with_model(runtime, "active")
        except resource_management.MemoryPressureYield as exc:
            pressure_error = exc.with_traceback(None)
        if pressure_error is not None:
            if delayed:
                delayed_caught.set()
                assert allow_delayed_release.wait(2)
            model_runtime.release_after_pressure(runtime, pressure_error)
            active_errors.append(pressure_error)

    active_workers = [
        threading.Thread(target=active_worker),
        threading.Thread(target=active_worker, kwargs={"delayed": True}),
    ]
    for worker in active_workers:
        worker.start()
    assert both_callbacks_entered.wait(2)

    queued_worker = threading.Thread(
        target=lambda: queued_results.append(
            model_runtime.transcribe_with_model(runtime, "queued")
        )
    )
    queued_worker.start()
    allow_callbacks_to_raise.set()
    assert delayed_caught.wait(2)
    assert recovery_entered.wait(2)
    loader.assert_not_called()
    assert queued_worker.is_alive()

    allow_recovery.set()
    queued_worker.join(3)
    assert not queued_worker.is_alive()
    reloaded_model = runtime.model
    generation_after_reload = runtime.model_release_generation
    allow_delayed_release.set()
    for worker in active_workers:
        worker.join(3)

    assert all(not worker.is_alive() for worker in (*active_workers, queued_worker))
    assert len(active_errors) == 2
    assert all(
        isinstance(error, resource_management.MemoryPressureYield)
        and str(error) == str(pressure)
        for error in active_errors
    )
    assert active_errors[0] is not active_errors[1]
    assert queued_results == ["queued-done"]
    assert runtime.model is reloaded_model
    backend.unload_model.assert_called_once_with()
    assert events.count("cuda.empty_cache") == 1
    controller.mark_released.assert_called_once_with("memory_pressure")
    assert runtime.model_release_generation == generation_after_reload
    controller.wait_for_recovery.assert_called_once_with(
        runtime.model_runtime_cancel_event
    )
    loader.assert_called_once()
    reloaded_transcribe.assert_called_once_with(
        "queued",
        progress_callback=reloaded_transcribe.call_args.kwargs["progress_callback"],
    )


def test_delayed_inference_allocation_ticket_cannot_unload_reloaded_generation():
    failure_type, release_after_failure = required_inference_allocation_api()
    runtime, controller, backend, events = coordinated_runtime(permits=2)
    both_inferences_entered = threading.Event()
    allow_allocations_to_fail = threading.Event()
    recovery_entered = threading.Event()
    allow_recovery = threading.Event()
    delayed_caught = threading.Event()
    allow_delayed_release = threading.Event()
    inference_count = 0
    inference_lock = threading.Lock()

    def fail_allocation(*_args, **_kwargs):
        nonlocal inference_count
        with inference_lock:
            inference_count += 1
            if inference_count == 2:
                both_inferences_entered.set()
        assert allow_allocations_to_fail.wait(2)
        raise MemoryError("inference allocation failed")

    def recover(_cancelled=None):
        recovery_entered.set()
        assert allow_recovery.wait(2)
        controller.state = controller.NORMAL
        controller.admission_open = True
        return True

    runtime.model.transcribe = fail_allocation
    controller.wait_for_recovery = MagicMock(side_effect=recover)
    controller.immediate_load_admission = MagicMock(
        return_value=SimpleNamespace(admitted=True)
    )
    runtime.model_requirement = object()
    runtime.read_pressure_sample = MagicMock()
    reloaded_transcribe = MagicMock(return_value="queued-done")
    reloaded = SimpleNamespace(
        model=SimpleNamespace(unload_model=MagicMock(), model_is_loaded=False),
        transcribe=reloaded_transcribe,
    )
    loader = MagicMock(return_value=reloaded)
    runtime.stable_whisper = SimpleNamespace(load_faster_whisper=loader)
    runtime.whisper_model = "medium"
    runtime.model_location = "/models"
    runtime.whisper_threads = 4
    runtime.concurrent_transcriptions = 2
    runtime.compute_type = "float16"
    runtime.whisper_model_revision_commit = None
    active_errors = []
    queued_results = []

    def active_worker(*, delayed=False):
        allocation_error = None
        try:
            model_runtime.transcribe_with_model(runtime, "active")
        except BaseException as exc:
            allocation_error = exc.with_traceback(None)
        if not isinstance(allocation_error, failure_type):
            active_errors.append(allocation_error)
            if delayed:
                delayed_caught.set()
            return
        if delayed:
            delayed_caught.set()
            assert allow_delayed_release.wait(2)
        release_after_failure(runtime, allocation_error)
        if not delayed:
            assert model_runtime.wait_for_model_recovery(
                runtime,
                runtime.model_runtime_cancel_event,
            )
        active_errors.append(allocation_error)

    active_workers = [
        threading.Thread(target=active_worker),
        threading.Thread(target=active_worker, kwargs={"delayed": True}),
    ]
    for worker in active_workers:
        worker.start()
    assert both_inferences_entered.wait(2)

    queued_worker = threading.Thread(
        target=lambda: queued_results.append(
            model_runtime.transcribe_with_model(runtime, "queued")
        )
    )
    queued_worker.start()
    allow_allocations_to_fail.set()
    assert delayed_caught.wait(2)
    assert recovery_entered.wait(2)
    loader.assert_not_called()
    assert queued_worker.is_alive()

    allow_recovery.set()
    queued_worker.join(3)
    assert not queued_worker.is_alive()
    reloaded_model = runtime.model
    generation_after_reload = runtime.model_release_generation

    allow_delayed_release.set()
    for worker in active_workers:
        worker.join(3)

    assert all(not worker.is_alive() for worker in (*active_workers, queued_worker))
    assert len(active_errors) == 2
    assert all(isinstance(error, failure_type) for error in active_errors)
    assert active_errors[0] is not active_errors[1]
    assert queued_results == ["queued-done"]
    assert runtime.model is reloaded_model
    assert runtime.model_release_generation == generation_after_reload
    backend.unload_model.assert_called_once_with()
    reloaded.model.unload_model.assert_not_called()
    assert events.count("cuda.empty_cache") == 1
    controller.mark_released.assert_called_once_with("inference_allocation_failure")
    loader.assert_called_once()
    reloaded_transcribe.assert_called_once_with("queued")


def test_runtime_status_is_bounded_and_does_not_expose_device_identity():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model_runtime_status = {
        "selected_model": "medium",
        "gpu_total_bytes": 24 * GIB,
        "device_uuid": "must-not-leak",
        "artifact_path": "/private/catalog.json",
    }

    status = model_runtime.runtime_status(runtime)

    assert status["selected_model"] == "medium"
    assert status["gpu_total_bytes"] == 24 * GIB
    assert "device_uuid" not in status
    assert "artifact_path" not in status
    assert status["controller_state"] == controller.state
    assert status["failure_counters"] == {
        "cuda_oom_generation": 0,
        "media_failure_generation": 0,
    }
    assert set(status["priority_pressure"]) == {
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
    assert status["priority_pressure"]["configured"] is False
    assert status["priority_pressure"]["state"] == "disabled"
    assert status["priority_pressure"]["model_resident"] is True


def test_runtime_status_exposes_only_coarse_workload_and_process_identity():
    runtime, _controller, _backend, _events = coordinated_runtime(permits=1)
    identity = runtime_receipts.RuntimeIdentity(
        epoch="1" * 32,
        started_monotonic_ns=123,
    )
    coordinator = runtime_receipts.RuntimeReceiptCoordinator(
        identity=identity,
        config=runtime_receipts.GateReceiptConfig(),
        condition=runtime.model_runtime_condition,
    )
    coordinator.initialize()
    token = coordinator.begin_workload(None, cursor_ms=42)
    coordinator.record_chunk(token, cursor_ms=42, chunk_uncommitted=True)
    runtime.runtime_receipt_coordinator = coordinator

    status = model_runtime.runtime_status(runtime)

    assert status["workload"] == {
        "active": True,
        "chunk_uncommitted": True,
        "completion_generation": 0,
    }
    assert set(status["workload"]) == {
        "active",
        "chunk_uncommitted",
        "completion_generation",
    }
    assert status["runtime_identity"] == {
        "epoch": "1" * 32,
        "started_monotonic_ns": 123,
    }
    assert "cursor" not in status["workload"]
    assert "workload_sha256" not in status


def test_precontroller_configured_priority_status_is_exact_and_fail_closed():
    runtime = SimpleNamespace(
        model=None,
        model_runtime_condition=threading.Condition(threading.Lock()),
        model_runtime_status={},
        model_pressure_controller=None,
        model_admission_closed=True,
        model_load_generation=4,
        model_unload_generation=3,
        cuda_oom_generation=2,
        media_failure_generation=1,
        model_profile_unhealthy=False,
        priority_pressure_probe=SimpleNamespace(configured=True),
    )

    status = model_runtime.runtime_status(runtime)
    priority = status["priority_pressure"]

    assert priority == {
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
        "model_load_generation": 4,
        "model_unload_generation": 3,
    }


def test_runtime_status_holds_model_condition_for_combined_controller_snapshot():
    events = []

    class GuardedCondition:
        held = False

        def __enter__(self):
            assert self.held is False
            self.held = True
            events.append("model.enter")
            return self

        def __exit__(self, *_args):
            events.append("model.exit")
            self.held = False

    condition = GuardedCondition()
    priority = {
        "configured": False,
        "state": "disabled",
        "heartbeat_age_ms": None,
        "source_age_ms": None,
        "policy_sha256": None,
        "observation_digest": None,
        "transition_observation_digest": None,
        "transition_sequence": 0,
        "controller_phase": "normal",
        "recovery_reason": None,
        "distinct_clear_count": 0,
        "model_resident": True,
        "model_load_generation": 7,
        "model_unload_generation": 6,
    }

    def combined(model_snapshot):
        assert condition.held is True
        assert model_snapshot["model_resident"] is True
        assert model_snapshot["model_load_generation"] == 7
        assert model_snapshot["model_unload_generation"] == 6
        events.append("controller.snapshot")
        return {
            "controller_state": "normal",
            "recovery_reason": None,
            "admission_open": True,
            "priority_pressure": priority,
        }

    runtime = SimpleNamespace(
        model=object(),
        model_runtime_condition=condition,
        model_runtime_status={},
        model_pressure_controller=SimpleNamespace(runtime_status_snapshot=combined),
        model_admission_closed=False,
        model_load_generation=7,
        model_unload_generation=6,
        cuda_oom_generation=0,
        media_failure_generation=0,
        model_profile_unhealthy=False,
    )

    status = model_runtime.runtime_status(runtime)

    assert events == ["model.enter", "controller.snapshot", "model.exit"]
    assert status["admission_open"] is True
    assert status["priority_pressure"] is priority


def test_runtime_generation_snapshot_is_atomic_and_rejects_boolean_counters():
    runtime, _controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model_load_generation = 4
    runtime.model_unload_generation = 3
    runtime.cuda_oom_generation = 2
    runtime.media_failure_generation = 1

    assert model_runtime.runtime_generation_snapshot(runtime) == {
        "model_resident": True,
        "model_load_generation": 4,
        "model_unload_generation": 3,
        "cuda_oom_generation": 2,
        "media_failure_generation": 1,
    }

    runtime.cuda_oom_generation = True
    with pytest.raises(RuntimeError, match="cuda_oom_generation"):
        model_runtime.runtime_status(runtime)


def test_selection_status_distinguishes_public_fallback_from_canonical_failure():
    requirement = SimpleNamespace(envelope_resolution=None)
    decision = SimpleNamespace(
        requirement=requirement,
        admission=SimpleNamespace(device_admission_bytes=12 * GIB),
        selected_model="medium",
        explicit=True,
        automatic_ceiling="small",
        reason="insufficient_capacity",
        provenance="fallback",
    )
    capacity = SimpleNamespace(source="physical")
    controller = SimpleNamespace(
        state="recovering",
        recovery_reason="no_safe_model",
        admission_open=False,
    )
    runtime = SimpleNamespace(
        canonical_shared_cuda=False,
        model_admission_closed=True,
        requested_whisper_model="medium",
    )

    public_status = model_runtime._selection_status(
        runtime,
        decision,
        capacity,
        None,
        24 * GIB,
        4 * GIB,
        "envelope_missing",
        {},
        controller,
    )
    runtime.canonical_shared_cuda = True
    decision.explicit = False
    decision.selected_model = None
    canonical_status = model_runtime._selection_status(
        runtime,
        decision,
        capacity,
        None,
        24 * GIB,
        4 * GIB,
        "envelope_missing",
        {},
        controller,
    )

    assert public_status["envelope_disposition"] == "public_fallback"
    assert public_status["gpu_total_bytes"] == 24 * GIB
    assert public_status["gpu_stabilized_free_bytes"] is None
    assert public_status["gpu_reserve_bytes"] == 4 * GIB
    assert public_status["gpu_allocatable_bytes"] == 12 * GIB
    assert canonical_status["envelope_disposition"] == "fail_closed"


def test_idle_observer_closes_then_uses_single_release_owner(monkeypatch):
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    controller.poll_idle_resident = MagicMock(side_effect=[False, True])
    release = MagicMock(return_value=True)
    monkeypatch.setattr(model_runtime, "release_model", release)

    controller.admission_open = False
    assert model_runtime.observe_idle_once(runtime) is False
    assert runtime.model_admission_closed is True
    controller.recovery_reason = "gpu_telemetry_unavailable"
    assert model_runtime.observe_idle_once(runtime) is True

    release.assert_called_once_with(
        runtime,
        reason="gpu_telemetry_unavailable",
    )


def test_idle_observer_polls_priority_during_active_inference():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model_active_inferences = 1
    controller.priority_configured = True

    def assert_priority(*, model_resident):
        assert model_resident is True
        controller.state = "recovering"
        controller.admission_open = False

    controller.poll_priority = MagicMock(side_effect=assert_priority)
    controller.poll = MagicMock()

    assert model_runtime.observe_idle_once(runtime) is False

    controller.poll_priority.assert_called_once_with(model_resident=True)
    controller.poll.assert_not_called()
    assert runtime.model_admission_closed is True


def test_priority_neutral_closes_admission_without_unloading_resident_model(
    monkeypatch,
):
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    controller.priority_configured = True

    def observe_neutral(*, model_resident):
        assert model_resident is True
        controller.state = "recovering"
        controller.recovery_reason = "priority_pressure"
        controller.admission_open = False

    controller.poll_priority = MagicMock(side_effect=observe_neutral)
    controller.poll_idle_resident = MagicMock(return_value=False)
    release = MagicMock()
    monkeypatch.setattr(model_runtime, "release_model", release)

    assert model_runtime.observe_idle_once(runtime) is False

    assert runtime.model is not None
    assert runtime.model_admission_closed is True
    release.assert_not_called()


def test_nonresident_normal_controller_still_consumes_priority_assertion():
    runtime, controller, _backend, _events = coordinated_runtime(permits=1)
    runtime.model = None
    controller.priority_configured = True

    def assert_priority(*, model_resident):
        assert model_resident is False
        controller.state = "recovering"
        controller.recovery_reason = "priority_pressure"
        controller.admission_open = False

    controller.poll_priority = MagicMock(side_effect=assert_priority)
    controller.poll = MagicMock(return_value="recovering")

    assert model_runtime.observe_idle_once(runtime) is False

    controller.poll_priority.assert_called_once_with(model_resident=False)
    controller.poll.assert_called_once_with(model_resident=False)
    assert runtime.model_admission_closed is True


def test_precontroller_priority_observer_uses_one_second_cadence():
    waits = []
    stop = SimpleNamespace(wait=lambda interval: waits.append(interval) or True)
    runtime = SimpleNamespace(
        model_idle_observer_stop=stop,
        model_pressure_controller=None,
        priority_pressure_probe=SimpleNamespace(configured=True),
    )

    model_runtime.run_model_idle_observer(runtime)

    assert waits == [1.0]


def test_lifespan_starts_idle_pressure_observer_for_cpu_runtime(monkeypatch):
    created = []

    class FakeThread:
        def __init__(self, *, target, daemon, name=None, args=()):
            self.target = target
            self.daemon = daemon
            self.name = name
            self.args = args
            self.started = False
            self.joined = False
            created.append(self)

        def start(self):
            self.started = True

        def join(self, timeout=None):
            self.joined = True
            assert timeout == 6

    monkeypatch.setattr(subgen, "memory_pressure_yield", True)
    monkeypatch.setattr(subgen, "cuda_device_index", None)
    monkeypatch.setattr(subgen, "transcribe_folders", "")
    monkeypatch.setattr(subgen.threading, "Thread", FakeThread)
    monkeypatch.setattr(subgen, "model_runtime_cancel_event", threading.Event())
    monkeypatch.setattr(subgen, "model_idle_observer_stop", threading.Event())
    monkeypatch.setattr(subgen, "model_idle_observer_thread", None)

    async def exercise_lifespan():
        async with subgen.lifespan(subgen.app):
            assert len(created) == 1
            assert created[0].started is True
            assert created[0].target is subgen.run_model_idle_observer

    asyncio.run(exercise_lifespan())

    assert created[0].joined is True
    assert subgen.model_runtime_cancel_event.is_set()


def test_delayed_cleanup_calls_release_without_holding_cleanup_lock(monkeypatch):
    runtime, _controller, _backend, events = coordinated_runtime(permits=1)
    cleanup_lock = RecordingLock(events, "cleanup_lock")
    direct_lock = RecordingLock(events, "direct_lock")
    runtime.model_cleanup_lock = cleanup_lock
    runtime.model_cleanup_timer = object()
    runtime.clear_vram_on_complete = True
    runtime.active_direct_tasks_lock = direct_lock
    runtime.active_direct_tasks = 0
    runtime.task_queue = SimpleNamespace(is_idle=lambda: True)

    def release(_runtime, reason=None):
        assert not cleanup_lock.held
        assert not direct_lock.held
        events.append(("release", reason))
        return True

    monkeypatch.setattr(model_runtime, "release_model", release)

    assert model_runtime.perform_model_cleanup(runtime) is True
    assert runtime.model_cleanup_timer is None
    assert ("release", "idle_cleanup") in events


def test_idle_cleanup_rechecks_work_after_draining_inference_permits():
    runtime, controller, backend, events = coordinated_runtime(permits=1)
    runtime.model_cleanup_lock = threading.Lock()
    runtime.model_cleanup_timer = object()
    runtime.clear_vram_on_complete = True
    runtime.active_direct_tasks_lock = threading.Lock()
    runtime.active_direct_tasks = 0
    runtime.task_queue = SimpleNamespace(is_idle=MagicMock(side_effect=[True, False]))

    result = model_runtime.perform_model_cleanup(runtime)

    assert result is False
    assert runtime.model is not None
    backend.unload_model.assert_not_called()
    assert "cuda.empty_cache" not in events
    controller.mark_released.assert_not_called()
    assert runtime.model_admission_closed is False
    assert runtime.task_queue.is_idle.call_count == 2


def test_auto_derives_candidate_specific_revisions_before_exact_resolution(monkeypatch):
    runtime_identity = catalog_owner.RuntimeIdentity(
        "1", "1", "1", "12", "580", "GPU", "8.6", 24 * GIB
    )
    image = catalog_owner.ImageIdentity(
        "sha256:" + "1" * 64,
        ("sha256:" + "2" * 64,),
    )

    def policy(model, revision):
        return catalog_owner.EnvelopePolicy(
            model,
            revision,
            "float16",
            "translate",
            1,
            20,
            "sha256:" + "3" * 64,
        )

    entries = tuple(
        SimpleNamespace(
            image_identity=image,
            runtime=runtime_identity,
            policy=policy(model, "hf:" + digit * 40),
        )
        for model, digit in (("large-v3", "a"), ("medium", "b"))
    )
    catalog = SimpleNamespace(
        entries=entries,
        integrity=SimpleNamespace(canonical_payload_sha256="sha256:" + "4" * 64),
    )
    identity = SimpleNamespace(image_identity=image)
    monkeypatch.setattr(catalog_owner, "load_catalog", MagicMock(return_value=catalog))
    monkeypatch.setattr(
        catalog_owner, "load_identity", MagicMock(return_value=identity)
    )
    calls = []

    def resolve(*_args, policy, **_kwargs):
        calls.append((policy.model, policy.model_revision))
        entry = next(item for item in entries if item.policy == policy)
        return SimpleNamespace(
            matched=True,
            envelope=entry,
            reason_code=None,
        )

    monkeypatch.setattr(catalog_owner, "resolve_envelope", resolve)
    runtime = SimpleNamespace(
        _model_envelope_catalog=catalog_owner,
        model_envelope_expected_uid=0,
        model_envelope_catalog_path="catalog.json",
        model_envelope_identity_path="identity.json",
        decoder_options_sha256="sha256:" + "3" * 64,
        requested_whisper_model="auto",
        whisper_model_revision=None,
        compute_type="float16",
        transcribe_or_translate="translate",
        concurrent_transcriptions=1,
        canonical_shared_cuda=False,
    )

    resolutions, _catalog, reason, keys = model_runtime._exact_envelope_resolutions(
        runtime,
        runtime_identity,
        20,
    )

    assert len(resolutions) == 2
    assert calls == [("large-v3", "hf:" + "a" * 40), ("medium", "hf:" + "b" * 40)]
    assert reason is None
    assert keys["large-v3"]["entry_index"] == 0
    assert keys["medium"]["entry_index"] == 1
