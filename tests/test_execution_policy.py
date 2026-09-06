from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import runpy
import threading

import pytest

from subgen_core.execution_policy import resolve_execution_policy


@pytest.mark.parametrize("run_mode", ["adaptive", "dedicated"])
@pytest.mark.parametrize("activity,percent,ceiling,delay", [
    ("passive", 50, 10, 5), ("balanced", 75, 20, 1), ("max", 100, 30, 0),
])
def test_six_profiles(activity, run_mode, percent, ceiling, delay):
    policy = resolve_execution_policy({
        "SUBGEN_ACTIVITY": activity, "SUBGEN_RUN_MODE": run_mode,
    })
    assert policy.status_snapshot() == {
        "activity": activity, "run_mode": run_mode,
        "working_budget_percent": percent,
        "automatic_chunk_ceiling_minutes": ceiling,
        "inter_file_delay_seconds": delay,
        "priority_signal_enabled": False,
        "adaptive_segmentation_enabled": True,
    }
    assert policy.retain_model_for_queued_work is (run_mode == "dedicated")


def test_defaults_and_immutable_resolution():
    environment = {}
    policy = resolve_execution_policy(environment)
    environment["SUBGEN_ACTIVITY"] = "max"
    assert policy.activity == "balanced"
    assert policy.run_mode == "adaptive"
    with pytest.raises(FrozenInstanceError):
        policy.activity = "max"
    snapshot = policy.status_snapshot()
    snapshot["activity"] = "max"
    assert policy.activity == "balanced"


def test_normalization():
    policy = resolve_execution_policy({
        "SUBGEN_ACTIVITY": " PASSIVE ", "SUBGEN_RUN_MODE": " AdApTiVe ",
    })
    assert (policy.activity, policy.run_mode) == ("passive", "adaptive")


@pytest.mark.parametrize("name", ["SUBGEN_ACTIVITY", "SUBGEN_RUN_MODE"])
@pytest.mark.parametrize("value", ["", " ", "unknown-secret-value", None, 1, True])
def test_invalid_choice_fails_without_echoing_value(name, value):
    with pytest.raises(ValueError, match=name) as error:
        resolve_execution_policy({name: value})
    assert "unknown-secret-value" not in str(error.value)


def test_dedicated_cannot_claim_application_priority_acceptance():
    with pytest.raises(ValueError, match="acceptance gate requires"):
        resolve_execution_policy({"SUBGEN_RUN_MODE": "dedicated"}, shared_host_gate_enabled=True)
    assert resolve_execution_policy({}, shared_host_gate_enabled=True).run_mode == "adaptive"


def test_shared_cuda_allows_explicit_full_force_without_changing_memory_safety():
    policy = resolve_execution_policy(
        {"SUBGEN_ACTIVITY": "max", "SUBGEN_RUN_MODE": "dedicated"},
        canonical_shared_cuda=True, priority_pressure_file="/private/signal",
    )
    assert policy.working_budget_percent == 100
    assert not policy.priority_signal_enabled
    assert policy.adaptive_segmentation_enabled
    with pytest.raises(ValueError, match="MEMORY_PRESSURE_YIELD"):
        resolve_execution_policy(
            {"SUBGEN_RUN_MODE": "dedicated"}, canonical_shared_cuda=True,
            memory_pressure_yield=False,
        )


def test_dedicated_explicitly_ignores_optional_priority_signal():
    dedicated = resolve_execution_policy(
        {"SUBGEN_RUN_MODE": "dedicated"}, priority_pressure_file="/private/signal",
    )
    assert not dedicated.priority_signal_enabled
    from subgen_core.human_progress import execution_policy_lines
    lines = "\n".join(execution_policy_lines(dedicated))
    assert "ignored in dedicated mode; RAM/VRAM safeguards remain active" in lines
    assert "camera FPS drops will not pause Subgen" in lines
    assert "/private/signal" not in lines
    policy = resolve_execution_policy({}, priority_pressure_file="/private/signal")
    assert policy.priority_signal_enabled
    assert "/private/signal" not in str(policy.status_snapshot())
    assert not resolve_execution_policy(
        {"SUBGEN_RUN_MODE": "dedicated"}, priority_pressure_file=" ",
    ).priority_signal_enabled


@pytest.mark.parametrize("activity", ["passive", "balanced", "max"])
@pytest.mark.parametrize("run_mode", ["adaptive", "dedicated"])
@pytest.mark.parametrize("flag", ["segmentation_enabled", "memory_pressure_yield"])
def test_no_profile_disables_core_protections(activity, run_mode, flag):
    with pytest.raises(ValueError, match="All execution modes require"):
        resolve_execution_policy({
            "SUBGEN_ACTIVITY": activity, "SUBGEN_RUN_MODE": run_mode,
        }, **{flag: False})


@pytest.mark.parametrize("flag", [
    "segmentation_enabled", "memory_pressure_yield",
    "canonical_shared_cuda", "shared_host_gate_enabled",
])
@pytest.mark.parametrize("value", ["False", "True", 0, 1, None])
def test_unparsed_flags_never_gain_truthiness(flag, value):
    with pytest.raises(ValueError, match="parsed boolean"):
        resolve_execution_policy({}, **{flag: value})


def test_no_model_selection_or_capacity_override_in_public_policy():
    policy = resolve_execution_policy({
        "WHISPER_MODEL": "large-v3", "MEMORY_PRESSURE_RESERVE_GIB": "12",
        "CONCURRENT_TRANSCRIPTIONS": "8",
    })
    assert policy == resolve_execution_policy({})


@pytest.mark.parametrize("run_mode", ["adaptive", "dedicated"])
@pytest.mark.parametrize("activity", ["passive", "balanced", "max"])
@pytest.mark.parametrize("gib", [4, 6, 9, 12, 16, 24, 32, 64, 128])
def test_resource_owner_bounds_chunks_without_changing_model(activity, run_mode, gib):
    from subgen_core import resource_management as resources
    policy = resolve_execution_policy({
        "SUBGEN_ACTIVITY": activity, "SUBGEN_RUN_MODE": run_mode,
    })
    capacity = resources.CapacityProfile(
        gib * resources.GIB, gib * resources.GIB, gib * resources.GIB, "cgroup_v2", 2,
    )
    reserve = resources.host_reserve_bytes(capacity)
    before = resources.select_model("auto", capacity)
    seconds = resources.activity_chunk_seconds(capacity, None, policy, host_reserve=reserve)
    assert 300 <= seconds <= resources.initial_chunk_seconds(capacity)
    assert seconds <= policy.automatic_chunk_ceiling_minutes * 60
    assert resources.select_model("auto", capacity) == before
    assert resources.host_reserve_bytes(capacity) == reserve
    assert resources.activity_chunk_seconds(capacity, 5, policy, host_reserve=reserve) == 300
    assert resources.activity_chunk_seconds(capacity, 60, policy, host_reserve=reserve) <= seconds


def test_post_reserve_budget_uses_minimum_not_double_subtraction():
    from subgen_core import resource_management as resources
    policy = resolve_execution_policy({"SUBGEN_ACTIVITY": "max"})
    capacity = resources.CapacityProfile(
        10 * resources.GIB, 12 * resources.GIB, 10 * resources.GIB, "cgroup_v2", 2,
    )
    # min(host 12-3, cgroup 10-1) = 9 GiB, not 6 GiB.
    assert resources.activity_chunk_seconds(
        capacity, None, policy, host_reserve=3 * resources.GIB,
    ) == 1200


@pytest.mark.parametrize("activity,delay", [("passive", 5), ("balanced", 1), ("max", 0)])
def test_queue_cadence_only_delays_between_local_transcriptions(activity, delay):
    from subgen_core.transcription import wait_between_library_files
    runtime = SimpleNamespace(
        execution_policy=resolve_execution_policy({"SUBGEN_ACTIVITY": activity}),
        task_queue=SimpleNamespace(get_queued_tasks=lambda: ["next"]),
        model_runtime_cancel_event=MagicMock(),
    )
    wait_between_library_files(runtime, {"type": "transcribe", "path": "/media/a.mkv"})
    if delay:
        runtime.model_runtime_cancel_event.wait.assert_called_once_with(delay)
    else:
        runtime.model_runtime_cancel_event.wait.assert_not_called()
    runtime.model_runtime_cancel_event.reset_mock()
    for task in [{"type": "asr"}, {"type": "detect_language"}, {"audio_content": b"x"}]:
        wait_between_library_files(runtime, task)
    runtime.task_queue.get_queued_tasks = lambda: []
    wait_between_library_files(runtime, {"type": "transcribe"})
    runtime.model_runtime_cancel_event.wait.assert_not_called()


@pytest.mark.parametrize("activity", ["passive", "balanced", "max"])
@pytest.mark.parametrize("run_mode", ["adaptive", "dedicated"])
def test_startup_and_status_use_resolved_policy(monkeypatch, activity, run_mode):
    from subgen_core import model_runtime
    for name in ["CANONICAL_SHARED_CUDA", "PRIORITY_PRESSURE_FILE", "WHISPER_MODEL_REVISION"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SUBGEN_ACTIVITY", activity)
    monkeypatch.setenv("SUBGEN_RUN_MODE", run_mode)
    monkeypatch.setattr(threading.Thread, "start", MagicMock())
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "subgen_override.py"),
        run_name="execution_policy_probe",
    )
    runtime = SimpleNamespace(**namespace)
    assert model_runtime.runtime_status(runtime)["execution_policy"] == (
        resolve_execution_policy({"SUBGEN_ACTIVITY": activity, "SUBGEN_RUN_MODE": run_mode})
        .status_snapshot()
    )


@pytest.mark.parametrize("settings,reason", [
    ({"SUBGEN_ACTIVITY": ""}, "SUBGEN_ACTIVITY"),
    ({"SUBGEN_RUN_MODE": "invalid"}, "SUBGEN_RUN_MODE"),
    ({"MEMORY_PRESSURE_YIELD": "False"}, "MEMORY_PRESSURE_YIELD"),
    ({"SEGMENTATION_ENABLED": "False"}, "SEGMENTATION_ENABLED"),
])
def test_startup_rejects_policy_before_starting_threads(monkeypatch, settings, reason):
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    start = MagicMock()
    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(ValueError, match=reason):
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "subgen_override.py"))
    start.assert_not_called()
