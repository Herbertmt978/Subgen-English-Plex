import runpy
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from subgen_core import runtime_receipts


ROOT = Path(__file__).resolve().parents[1]
STARTUP = ROOT / "subgen_override.py"


def _run_startup(monkeypatch, **overrides):
    settings = {
        "PRIORITY_PRESSURE_FILE": "",
        "MEMORY_PRESSURE_YIELD": "True",
        "CANONICAL_SHARED_CUDA": "False",
        "TRANSCRIBE_DEVICE": "cpu",
        "GPU_MEMORY_RESERVE_GIB": "auto",
        "CONCURRENT_TRANSCRIPTIONS": "1",
        "TASK11B_GATE_RECEIPT_FILE": "",
        "TASK11B_GATE_TOKEN_SHA256": "",
        "TASK11B_PHASE_A_WORKLOAD_SHA256": "",
        "TASK11B_PHASE_B_WORKLOAD_SHA256": "",
    }
    settings.update(overrides)
    for name, value in settings.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    start = MagicMock()
    monkeypatch.setattr(threading.Thread, "start", start)
    namespace = runpy.run_path(
        str(STARTUP),
        run_name="priority_startup_probe",
    )
    return namespace, start


@pytest.mark.parametrize("raw", [None, "", " \t "])
@pytest.mark.parametrize("memory_pressure_yield", ["True", "False"])
def test_empty_priority_path_is_trimmed_and_disabled_with_either_yield_setting(
    monkeypatch,
    raw,
    memory_pressure_yield,
):
    namespace, _start = _run_startup(
        monkeypatch,
        PRIORITY_PRESSURE_FILE=raw,
        MEMORY_PRESSURE_YIELD=memory_pressure_yield,
    )

    assert namespace["priority_pressure_file"] == ""
    assert namespace["priority_pressure_probe"].configured is False
    assert callable(namespace["priority_pressure_reader"])
    observation = namespace["priority_pressure_reader"]()
    assert observation.configured is False
    assert observation.state == "disabled"


def test_configured_priority_path_is_trimmed_validated_and_wired_once(monkeypatch):
    namespace, _start = _run_startup(
        monkeypatch,
        PRIORITY_PRESSURE_FILE="  /run/subgen-priority/pressure.json\t",
    )

    assert namespace["priority_pressure_file"] == ("/run/subgen-priority/pressure.json")
    probe = namespace["priority_pressure_probe"]
    reader = namespace["priority_pressure_reader"]
    assert probe.configured is True
    assert probe.signal_path == "/run/subgen-priority/pressure.json"
    assert reader.__self__ is probe

    expected = object()
    read_pressure_sample = MagicMock(return_value=expected)
    monkeypatch.setattr(
        namespace["_resource_probes"],
        "read_pressure_sample",
        read_pressure_sample,
    )

    assert namespace["read_pressure_sample"]() is expected
    read_pressure_sample.assert_called_once_with(
        gpu_memory_reader=None,
        priority_reader=reader,
    )


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("priority.json", "absolute"),
        ("/run/subgen/../priority.json", "canonical"),
    ],
)
def test_configured_priority_path_rejects_unsafe_spelling_before_workers(
    monkeypatch,
    path,
    message,
):
    start = MagicMock()
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setenv("PRIORITY_PRESSURE_FILE", path)
    monkeypatch.setenv("MEMORY_PRESSURE_YIELD", "True")
    monkeypatch.setenv("CANONICAL_SHARED_CUDA", "False")

    with pytest.raises(ValueError, match=message):
        runpy.run_path(str(STARTUP), run_name="invalid_priority_startup_probe")

    start.assert_not_called()


def test_configured_priority_path_requires_memory_pressure_yield_before_workers(
    monkeypatch,
):
    start = MagicMock()
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setenv(
        "PRIORITY_PRESSURE_FILE",
        "/run/subgen-priority/pressure.json",
    )
    monkeypatch.setenv("MEMORY_PRESSURE_YIELD", "False")
    monkeypatch.setenv("CANONICAL_SHARED_CUDA", "False")

    with pytest.raises(ValueError, match="MEMORY_PRESSURE_YIELD"):
        runpy.run_path(str(STARTUP), run_name="disabled_priority_startup_probe")

    start.assert_not_called()


def test_canonical_shared_cuda_requires_configured_priority_path_before_workers(
    monkeypatch,
):
    start = MagicMock()
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setenv("PRIORITY_PRESSURE_FILE", " \t")
    monkeypatch.setenv("MEMORY_PRESSURE_YIELD", "True")
    monkeypatch.setenv("CANONICAL_SHARED_CUDA", "True")
    monkeypatch.setenv("TRANSCRIBE_DEVICE", "cuda")
    monkeypatch.setenv("GPU_MEMORY_RESERVE_GIB", "4")

    with pytest.raises(ValueError, match="PRIORITY_PRESSURE_FILE"):
        runpy.run_path(str(STARTUP), run_name="canonical_priority_startup_probe")

    start.assert_not_called()


def test_task11b_gate_rejects_partial_configuration_before_workers(monkeypatch):
    start = MagicMock()
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setenv("TASK11B_GATE_RECEIPT_FILE", str(ROOT / "receipt.jsonl"))
    monkeypatch.delenv("TASK11B_GATE_TOKEN_SHA256", raising=False)
    monkeypatch.delenv("TASK11B_PHASE_A_WORKLOAD_SHA256", raising=False)
    monkeypatch.delenv("TASK11B_PHASE_B_WORKLOAD_SHA256", raising=False)
    monkeypatch.setenv("CONCURRENT_TRANSCRIPTIONS", "1")

    with pytest.raises(
        runtime_receipts.RuntimeReceiptError,
        match="entirely enabled or disabled",
    ):
        runpy.run_path(str(STARTUP), run_name="partial_task11b_startup_probe")

    start.assert_not_called()


@pytest.mark.parametrize(
    ("segmentation", "priority_path", "message"),
    [
        ("False", "/run/subgen-priority/pressure.json", "SEGMENTATION_ENABLED"),
        ("True", "", "PRIORITY_PRESSURE_FILE"),
    ],
)
def test_task11b_gate_requires_segmented_priority_runtime_before_workers(
    monkeypatch,
    segmentation,
    priority_path,
    message,
):
    start = MagicMock()
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setenv("TASK11B_GATE_RECEIPT_FILE", str(ROOT / "receipt.jsonl"))
    monkeypatch.setenv("TASK11B_GATE_TOKEN_SHA256", "1" * 64)
    monkeypatch.setenv("TASK11B_PHASE_A_WORKLOAD_SHA256", "2" * 64)
    monkeypatch.setenv("TASK11B_PHASE_B_WORKLOAD_SHA256", "3" * 64)
    monkeypatch.setenv("CONCURRENT_TRANSCRIPTIONS", "1")
    monkeypatch.setenv("SEGMENTATION_ENABLED", segmentation)
    monkeypatch.setenv("PRIORITY_PRESSURE_FILE", priority_path)
    monkeypatch.setenv("MEMORY_PRESSURE_YIELD", "True")

    with pytest.raises(ValueError, match=message):
        runpy.run_path(str(STARTUP), run_name="invalid_task11b_runtime_probe")

    start.assert_not_called()


def test_runtime_identity_is_generated_once_and_ignores_environment_injection(
    monkeypatch,
):
    monkeypatch.setenv("RUNTIME_EPOCH", "f" * 32)
    monkeypatch.setenv("RUNTIME_STARTED_MONOTONIC_NS", "1")

    first, _start = _run_startup(monkeypatch)
    second, _start = _run_startup(monkeypatch)

    first_identity = first["runtime_process_identity"].snapshot()
    second_identity = second["runtime_process_identity"].snapshot()
    assert first_identity["epoch"] != "f" * 32
    assert first_identity["started_monotonic_ns"] != 1
    assert first_identity != second_identity
