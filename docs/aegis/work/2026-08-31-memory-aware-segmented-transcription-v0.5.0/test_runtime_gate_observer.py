"""Focused regressions for the owner-operated Task 11B runtime observer."""

from __future__ import annotations

import ast
import copy
import errno
import hashlib
import inspect
import json
import os
import stat
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest


WORK_DIR = Path(__file__).parent
sys.path.insert(0, str(WORK_DIR))
import gate_health_sampler as health  # noqa: E402
import runtime_gate_observer as observer  # noqa: E402

observer.health = health


def worker_event(
    event: str,
    path: str,
    *,
    source_identity: tuple[int, int, int, int, int] = (1, 2, 3, 4, 5),
) -> dict[str, object]:
    return {
        "event": event,
        "task_id": hashlib.sha256(f"transcribe:{path}".encode()).hexdigest()[:16],
        "task_type": "transcribe",
        "path": path,
        "source_identity": list(source_identity),
    }


def healthy_status() -> dict[str, object]:
    return {
        "resource_management": {
            "controller_state": "normal",
            "recovery_reason": None,
            "admission_open": True,
            "capacity_source": "cgroup_v2",
            "requested_model": "auto",
            "envelope_key": {
                "catalog_payload_sha256": "sha256:" + "4" * 64,
                "entry_index": 0,
            },
            "envelope_disposition": "exact_match",
            "envelope_reason": None,
            "selected_model": "medium",
            "model_explicit": False,
            "automatic_ceiling": "medium",
            "decision_reason": "selected",
            "decision_provenance": "envelope",
            "gpu_total_bytes": 24 * health.GIB,
            "gpu_stabilized_free_bytes": 18 * health.GIB,
            "gpu_reserve_bytes": 8 * health.GIB,
            "gpu_allocatable_bytes": 10 * health.GIB,
            "priority_pressure": {
                "configured": True,
                "state": "clear",
                "heartbeat_age_ms": 100,
                "source_age_ms": 200,
                "policy_sha256": "3" * 64,
                "observation_digest": "7" * 64,
                "transition_observation_digest": "8" * 64,
                "transition_sequence": 4,
                "controller_phase": "normal",
                "recovery_reason": None,
                "distinct_clear_count": 3,
                "model_resident": True,
                "model_load_generation": 1,
                "model_unload_generation": 0,
            },
            "workload": {
                "active": True,
                "chunk_uncommitted": True,
                "completion_generation": 0,
            },
            "runtime_identity": {
                "epoch": "5" * 32,
                "started_monotonic_ns": 1,
            },
            "failure_counters": {
                "cuda_oom_generation": 0,
                "media_failure_generation": 0,
            },
        }
    }


def boundary(media_root: Path) -> health.BoundaryExpectation:
    document = {
        "schema": 4,
        "docker_daemon_identity": health.docker_daemon_identity_document(
            "6" * 64, "7" * 64
        ),
        "user": "1000:1000",
        "mounts": [
            {
                "source": str(media_root),
                "destination": "/media",
                "read_write": True,
                "mode": "rw",
            }
        ],
    }
    return health.BoundaryExpectation(document, "a" * 64, "b" * 64)


def fixture_document() -> dict[str, object]:
    return {
        "schema": observer.FIXTURE_SCHEMA,
        "long": {"media": "long/source.mkv", "subtitle": "long/source.en.srt"},
        "short_resident": [
            {"media": "short/one.mkv", "subtitle": "short/one.en.srt"},
            {"media": "short/two.mkv", "subtitle": "short/two.en.srt"},
        ],
        "reload": {
            "media": "reload/source.mkv",
            "subtitle": "reload/source.en.srt",
        },
        "invalid": {"media": "invalid/source.mkv"},
        "silent": {"media": "silent/source.mkv"},
    }


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def priority_assertion_document(*, sequence: int = 7) -> dict[str, object]:
    observation_id = "ab" * 32
    return {
        "schema": 1,
        "boot_id_sha256": "1" * 64,
        "producer_epoch": "2" * 32,
        "sequence": sequence,
        "observed_monotonic_ns": 2_000_000_000,
        "source_generation": 17,
        "source_observed_monotonic_ns": 1_000_000_000,
        "observation_id": observation_id,
        "policy_sha256": "3" * 64,
        "pressure": True,
        "clear_eligible": False,
        "reason_codes": ["higher_priority_busy"],
    }


def runtime_receipt(
    sequence: int,
    *,
    observed_monotonic_ns: int | None = None,
    workload_sha256: str | None = "4" * 64,
    priority_state: str = "asserted",
    controller_phase: str = "yielding",
    recovery_reason: str | None = "priority_pressure",
    model_resident: bool = True,
) -> dict[str, object]:
    return {
        "schema": "subgen.task11b.runtime-receipt/v1",
        "runtime_epoch": "5" * 32,
        "gate_token_sha256": "6" * 64,
        "sequence": sequence,
        "observed_monotonic_ns": observed_monotonic_ns or sequence * 1_000_000_000,
        "workload_sha256": workload_sha256,
        "source_generation": 100 + sequence,
        "observation_digest": "7" * 64,
        "transition_observation_digest": "8" * 64,
        "transition_sequence": 3,
        "heartbeat_age_ms": 100,
        "source_age_ms": 200,
        "policy_sha256": "3" * 64,
        "priority_state": priority_state,
        "controller_phase": controller_phase,
        "recovery_reason": recovery_reason,
        "admission_open": controller_phase == "normal",
        "distinct_clear_count": 0,
        "model_resident": model_resident,
        "model_load_generation": 1,
        "model_unload_generation": 0,
        "active": workload_sha256 is not None,
        "chunk_uncommitted": workload_sha256 is not None,
        "active_cursor_ms": 0 if workload_sha256 is not None else None,
        "completed_cursor_ms": None,
        "completion_generation": 0,
        "model_identity_sha256": "9" * 64 if model_resident else None,
        "cuda_oom_generation": 0,
        "media_failure_generation": 0,
    }


def phase_event_from_receipt(
    index: int, receipt: dict[str, object], *, monotonic_ns: int | None = None
) -> dict[str, object]:
    mapping = {
        "source_generation": "source_generation",
        "observation_digest": "observation_digest",
        "transition_observation_digest": "transition_observation_digest",
        "transition_sequence": "transition_sequence",
        "heartbeat_age_ms": "heartbeat_age_ms",
        "source_age_ms": "source_age_ms",
        "policy_sha256": "policy_sha256",
        "priority_state": "priority_state",
        "controller_phase": "controller_phase",
        "recovery_reason": "recovery_reason",
        "admission_open": "admission_open",
        "distinct_clear_count": "distinct_clear_count",
        "model_resident": "model_resident",
        "model_load_generation": "model_load_generation",
        "model_unload_generation": "model_unload_generation",
        "model_identity_sha256": "model_identity_sha256",
        "cuda_oom_generation": "cuda_oom_generation",
        "media_failure_generation": "media_failure_generation",
        "workload_active": "active",
        "chunk_uncommitted": "chunk_uncommitted",
        "cursor_ms": "active_cursor_ms",
        "last_completed_cursor_ms": "completed_cursor_ms",
        "completion_generation": "completion_generation",
    }
    event = {target: receipt[source] for target, source in mapping.items()}
    event.update(
        {
            "event_index": index,
            "monotonic_ns": monotonic_ns or receipt["observed_monotonic_ns"],
            "gate_receipt_sha256": hashlib.sha256(canonical_json(receipt)).hexdigest(),
        }
    )
    return event


def phase_b_sample_from_receipt(
    index: int,
    receipt: dict[str, object],
    *,
    started_monotonic_ns: int,
) -> dict[str, object]:
    mapping = {
        "source_generation": "source_generation",
        "observation_digest": "observation_digest",
        "transition_observation_digest": "transition_observation_digest",
        "transition_sequence": "transition_sequence",
        "heartbeat_age_ms": "heartbeat_age_ms",
        "source_age_ms": "source_age_ms",
        "policy_sha256": "policy_sha256",
        "priority_state": "priority_state",
        "controller_phase": "controller_phase",
        "recovery_reason": "recovery_reason",
        "admission_open": "admission_open",
        "distinct_clear_count": "distinct_clear_count",
        "model_resident": "model_resident",
        "model_load_generation": "model_load_generation",
        "model_unload_generation": "model_unload_generation",
        "model_identity_sha256": "model_identity_sha256",
        "cuda_oom_generation": "cuda_oom_generation",
        "media_failure_generation": "media_failure_generation",
        "workload_active": "active",
        "completion_generation": "completion_generation",
    }
    sample = {target: receipt[source] for target, source in mapping.items()}
    sample.update(
        {
            "sample_index": index,
            "scheduled_offset_seconds": index * 5,
            "captured_monotonic_ns": started_monotonic_ns + index * 5_000_000_000,
            "gate_receipt_sha256": hashlib.sha256(canonical_json(receipt)).hexdigest(),
        }
    )
    return sample


def model_catalog_entry() -> dict[str, object]:
    return {
        "image_identity": {
            "config_digest": "sha256:" + "a" * 64,
            "layer_diff_ids": ["sha256:" + "b" * 64],
        },
        "runtime": {
            "stable_ts_version": "2.19.1",
            "faster_whisper_version": "1.2.0",
            "ctranslate2_version": "4.6.0",
            "cuda_runtime_version": "12.8",
            "driver_version": "580.0",
            "device_name": "RTX 3090",
            "compute_capability": "8.6",
            "total_vram_bytes": 24 * health.GIB,
        },
        "policy": {
            "model": "medium",
            "model_revision": "hf:" + "c" * 40,
            "compute_type": "float16",
            "task": "translate",
            "inference_concurrency": 1,
            "chunk_minutes": 5,
            "decoder_options_sha256": "sha256:" + "d" * 64,
        },
        "measurements": {
            "runs": 30,
            "host_preload_used_bytes": 1,
            "host_peak_used_bytes": 2,
            "cgroup_preload_used_bytes": 1,
            "cgroup_peak_used_bytes": 2,
            "device_preload_used_bytes": 1,
            "device_peak_used_bytes": 2,
            "host_incremental_peak_bytes": 1,
            "cgroup_incremental_peak_bytes": 1,
            "device_incremental_peak_bytes": 1,
            "host_margin_bytes": 1,
            "device_margin_bytes": 2 * health.GIB,
        },
    }


def model_catalog_document() -> dict[str, object]:
    document: dict[str, object] = {
        "schema": "subgen.model-envelope.catalog/v1",
        "catalog_version": 1,
        "entries": [model_catalog_entry()],
    }
    payload = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    document["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }
    return document


def unloaded_gpu_envelope_document() -> dict[str, object]:
    cycles = []
    for index in range(1, 4):
        cycles.append(
            {
                "cycle_index": index,
                "container_id_sha256": f"{index}" * 64,
                "load_generation_before": 0,
                "load_generation_after": 1,
                "inference_completed": True,
                "inference_result_sha256": "e" * 64,
                "unload_generation_before": 0,
                "unload_generation_after": 1,
                "candidate_bytes_samples": [0] * 10,
            }
        )
    return {
        "schema": "subgen.unloaded-gpu-envelope/v1",
        "runtime_commit": "f" * 40,
        "image": {
            "oci_index": "sha256:" + "1" * 64,
            "config_digest": "sha256:" + "a" * 64,
            "layer_diff_ids": ["sha256:" + "b" * 64],
        },
        "gpu": {
            "uuid": "GPU-00000000-0000-4000-8000-000000000000",
            "driver_version": "580.0",
        },
        "backend": {
            "cuda_version": "12.8",
            "ctranslate2_version": "4.6.0",
            "stable_ts_version": "2.19.1",
            "generator_sha256": "2" * 64,
        },
        "model_policy": {
            "selected_model": "medium",
            "model_revision": "hf:" + "c" * 40,
            "compute_type": "float16",
            "device": "cuda",
            "device_index": 0,
            "task": "translate",
            "language": "en",
            "chunk_seconds": 300,
            "overlap_seconds": 5,
            "fixture_sha256": "3" * 64,
            "priority_policy_sha256": "4" * 64,
        },
        "measurement": {
            "cycles": cycles,
            "cycle_count": 3,
            "samples_per_cycle": 10,
            "interval_seconds": 1,
            "margin_bytes": 134_217_728,
            "max_observed_candidate_bytes": 0,
            "allowed_unloaded_bytes": 134_217_728,
        },
    }


def priority_policy_document() -> dict[str, object]:
    return {
        "schema": 1,
        "frigate_version": "0.17.2",
        "detection_fps_limit": 80.0,
        "source_max_age_seconds": 30,
        "cameras": {"private_camera": 8.0},
        "detectors": ["private_detector"],
        "required_embedding_speeds": ["private_embedding"],
        "conditional_embedding_pairs": [["private_embedding_a", "private_embedding_b"]],
        "frigate_config_sha256": "a" * 64,
        "gpu_uuid": "GPU-00000000-0000-4000-8000-000000000000",
        "nvidia_driver_version": "580.0",
        "gpu_index": 0,
    }


def profiler_priority_document(
    sequence: int,
    *,
    pressure: bool = False,
    reasons: list[str] | None = None,
    observation_id: str | None = None,
    policy_sha256: str = "3" * 64,
    producer_epoch: str = "2" * 32,
) -> dict[str, object]:
    reason_codes = [] if reasons is None else reasons
    observed = sequence * 1_000_000_000
    host_boot_id = "00000000-0000-4000-8000-000000000000"
    return {
        "schema": 1,
        "boot_id_sha256": hashlib.sha256(host_boot_id.encode("ascii")).hexdigest(),
        "producer_epoch": producer_epoch,
        "sequence": sequence,
        "observed_monotonic_ns": observed,
        "source_generation": sequence,
        "source_observed_monotonic_ns": observed,
        "observation_id": observation_id or f"{sequence:064x}",
        "policy_sha256": policy_sha256,
        "pressure": pressure,
        "clear_eligible": not pressure,
        "reason_codes": reason_codes,
    }


def profiler_observer_args() -> SimpleNamespace:
    args = observer.parser().parse_args([])
    host_boot_id = "00000000-0000-4000-8000-000000000000"
    policy_sha256 = hashlib.sha256(
        canonical_json(priority_policy_document())
    ).hexdigest()
    values = {
        "container": "subgen-task11b-profile-large-v3",
        "output": Path("C:/private/profiler-evidence.jsonl"),
        "duration_seconds": 900,
        "interval_seconds": 5,
        "start_timeout_seconds": 120,
        "expected_memory_bytes": 12 * health.GIB,
        "expected_chunk_minutes": 5,
        "gpu_free_floor_bytes": 8 * health.GIB,
        "host_reserve_bytes": 4 * health.GIB,
        "candidate_mode": "profiler",
        "expected_model": "large-v3",
        "expected_profiler_returncode": 3,
        "expected_container_id": "a" * 64,
        "expected_image_config": "sha256:" + "b" * 64,
        "candidate_oci_index": "sha256:" + "b" * 64,
        "candidate_config_digest": "sha256:" + "c" * 64,
        "candidate_layer_diff_ids": ["sha256:" + "d" * 64],
        "model_envelope_catalog_sha256": "e" * 64,
        "phase_a_fixture_record_sha256": "f" * 64,
        "phase_b_fixture_record_sha256": "0" * 64,
        "model_revision": "hf:" + "1" * 40,
        "expected_command_sha256": "2" * 64,
        "runtime_commit": "3" * 40,
        "gate_token": "0123456789abcdef0123456789abcdef",
        "gate_role": "profile-large-v3",
        "camera_expectations": Path("C:/private/cameras.json"),
        "sampler_sha256": "4" * 64,
        "observer_sha256": "5" * 64,
        "expected_docker_daemon_id": "daemon-id",
        "expected_host_boot_id": host_boot_id,
        "boundary_manifest": Path("C:/private/boundary.json"),
        "boundary_manifest_sha256": "6" * 64,
        "disposable_root": "/var/lib/subgen-v05-gate",
        "priority_signal": Path("C:/private/pressure.json"),
        "priority_policy": Path("C:/private/policy.json"),
        "priority_policy_sha256": policy_sha256,
        "priority_producer_epoch": "2" * 32,
        "priority_boot_id_sha256": hashlib.sha256(
            host_boot_id.encode("ascii")
        ).hexdigest(),
        "sampler_test_sha256": "7" * 64,
        "observer_test_sha256": "8" * 64,
        "producer_sha256": "9" * 64,
        "emit_systemd_run_script": None,
        "expected_systemd_unit": None,
        "supervisor_armed": True,
    }
    for key, value in values.items():
        setattr(args, key, value)
    return args


def profiler_boundary(priority_source: str = "C:/private") -> SimpleNamespace:
    return SimpleNamespace(
        document={
            "mounts": [
                {
                    "source": priority_source,
                    "destination": "/run/subgen-priority",
                    "type": "bind",
                    "mode": "ro",
                    "read_write": False,
                    "propagation": "rprivate",
                }
            ]
        }
    )


class _ProfilerGuardClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _priority_guard_reader(
    args: SimpleNamespace,
    signals: list[dict[str, object]],
):
    policy_payload = canonical_json(priority_policy_document())
    assert hashlib.sha256(policy_payload).hexdigest() == args.priority_policy_sha256
    signal_payloads = [canonical_json(signal) for signal in signals]

    def read(_path: Path, *, maximum: int, label: str) -> bytes:
        if label == "profiler priority policy":
            assert maximum == 32 * 1024
            return policy_payload
        assert label == "profiler priority signal"
        assert maximum == observer.MAX_PRIORITY_SIGNAL_BYTES
        if not signal_payloads:
            raise AssertionError("priority signal fixture was exhausted")
        return signal_payloads.pop(0)

    return read


def test_profiler_cli_contract_round_trips_mode_and_expected_returncode() -> None:
    actions = {action.dest: action for action in observer.parser()._actions}
    assert set(actions["candidate_mode"].choices or ()) == {"runtime", "profiler"}
    assert "expected_profiler_returncode" in actions

    args = profiler_observer_args()
    arguments = observer._observer_cli_arguments(args, supervisor_armed=True)
    parsed = observer.parser().parse_args(arguments)
    assert parsed.candidate_mode == "profiler"
    assert parsed.expected_profiler_returncode == 3
    assert parsed.supervisor_armed is True
    assert "--api-key-file" not in arguments
    for runtime_only in (
        "--runtime-receipt-journal",
        "--model-envelope-catalog",
        "--unloaded-gpu-envelope",
        "--phase-a-fixture-record",
        "--phase-b-fixture-record",
        "--candidate-identity-record",
        "--assertion-observation",
        "--phase-a-receipt-trace",
        "--phase-a-seal",
        "--phase-b-receipt-trace",
        "--phase-b-seal",
    ):
        assert runtime_only not in arguments


def test_runtime_cli_preserves_ordered_profiler_evidence_pairs() -> None:
    args = profiler_observer_args()
    args.candidate_mode = "runtime"
    args.expected_profiler_returncode = None
    args.profiler_evidence = [
        Path("C:/private/large.jsonl"),
        Path("C:/private/medium.jsonl"),
    ]
    args.profiler_evidence_seal = [
        Path("C:/private/large.seal.json"),
        Path("C:/private/medium.seal.json"),
    ]
    args.profiler_boundary_manifest = [
        Path("C:/private/large.boundary.json"),
        Path("C:/private/medium.boundary.json"),
    ]
    arguments = observer._observer_cli_arguments(args, supervisor_armed=True)
    parsed = observer.parser().parse_args(arguments)
    assert parsed.profiler_evidence == args.profiler_evidence
    assert parsed.profiler_evidence_seal == args.profiler_evidence_seal
    assert parsed.profiler_boundary_manifest == args.profiler_boundary_manifest
    pair_options = [
        (arguments[index], arguments[index + 1])
        for index in range(len(arguments) - 1)
        if arguments[index]
        in {
            "--profiler-evidence",
            "--profiler-evidence-seal",
            "--profiler-boundary-manifest",
        }
    ]
    assert pair_options == [
        ("--profiler-evidence", "C:\\private\\large.jsonl"),
        ("--profiler-evidence-seal", "C:\\private\\large.seal.json"),
        ("--profiler-boundary-manifest", "C:\\private\\large.boundary.json"),
        ("--profiler-evidence", "C:\\private\\medium.jsonl"),
        ("--profiler-evidence-seal", "C:\\private\\medium.seal.json"),
        ("--profiler-boundary-manifest", "C:\\private\\medium.boundary.json"),
    ]


def test_profiler_argument_validation_cross_binds_priority_boot_identity() -> None:
    args = profiler_observer_args()
    args.emit_systemd_run_script = Path("C:/private/run-profiler.sh")
    args.supervisor_armed = False
    observer.validate_args(args)

    drifted = copy.deepcopy(args)
    drifted.priority_boot_id_sha256 = "f" * 64
    with pytest.raises(health.GateAbort, match="boot_identity"):
        observer.validate_args(drifted)

    extended = copy.deepcopy(args)
    extended.start_timeout_seconds = 121
    with pytest.raises(health.GateAbort, match="start_timeout"):
        observer.validate_args(extended)

    chain_inputs = copy.deepcopy(args)
    chain_inputs.profiler_evidence = [Path("C:/private/large.jsonl")]
    chain_inputs.profiler_evidence_seal = [Path("C:/private/large.seal.json")]
    chain_inputs.profiler_boundary_manifest = [Path("C:/private/large.boundary.json")]
    with pytest.raises(health.GateAbort, match="runtime_profiler_chain_inputs"):
        observer.validate_args(chain_inputs)


def test_profiler_priority_guard_requires_a_fresh_successor_and_redacts_identity() -> (
    None
):
    args = profiler_observer_args()
    signals = [
        profiler_priority_document(7, policy_sha256=args.priority_policy_sha256),
        profiler_priority_document(7, policy_sha256=args.priority_policy_sha256),
        profiler_priority_document(
            8,
            pressure=True,
            reasons=["higher_priority_busy"],
            policy_sha256=args.priority_policy_sha256,
        ),
        profiler_priority_document(
            8,
            pressure=True,
            reasons=["higher_priority_busy"],
            policy_sha256=args.priority_policy_sha256,
        ),
    ]
    clock = _ProfilerGuardClock()
    with (
        mock.patch.object(
            observer,
            "_require_private_file",
            side_effect=_priority_guard_reader(args, signals),
        ),
        mock.patch.object(observer.time, "monotonic_ns", return_value=9_000_000_000),
        mock.patch.object(
            observer,
            "_priority_mount_directory_identity",
            return_value=(11, 22),
        ),
    ):
        guard = observer.ProfilerPriorityGuard(
            args, mount_source=args.priority_signal.parent
        )
        armed = guard.arm(
            deadline=1.0,
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )
        repeated = guard.sample()

    assert armed["sequence"] == 8
    assert repeated["sequence"] == 8
    assert armed["pressure"] is True
    assert armed["reason_codes"] == ["higher_priority_busy"]
    assert guard.gpu_uuid == priority_policy_document()["gpu_uuid"]
    serialized = json.dumps({"armed": armed, "repeated": repeated})
    assert args.priority_producer_epoch not in serialized
    assert guard.gpu_uuid not in serialized
    assert signals[-1]["observation_id"] not in serialized


def test_profiler_priority_guard_rejects_mutation_and_unavailable_state() -> None:
    args = profiler_observer_args()
    first = profiler_priority_document(7, policy_sha256=args.priority_policy_sha256)
    mutated = profiler_priority_document(
        7,
        policy_sha256=args.priority_policy_sha256,
        observation_id="f" * 64,
    )
    with (
        mock.patch.object(
            observer,
            "_require_private_file",
            side_effect=_priority_guard_reader(args, [first, mutated]),
        ),
        mock.patch.object(observer.time, "monotonic_ns", return_value=8_000_000_000),
        mock.patch.object(
            observer,
            "_priority_mount_directory_identity",
            return_value=(11, 22),
        ),
    ):
        guard = observer.ProfilerPriorityGuard(
            args, mount_source=args.priority_signal.parent
        )
        guard.sample()
        with pytest.raises(health.GateAbort, match="mutat"):
            guard.sample()

    unavailable = profiler_priority_document(
        9,
        pressure=True,
        reasons=["higher_priority_unavailable"],
        policy_sha256=args.priority_policy_sha256,
    )
    with (
        mock.patch.object(
            observer,
            "_require_private_file",
            side_effect=_priority_guard_reader(args, [unavailable]),
        ),
        mock.patch.object(observer.time, "monotonic_ns", return_value=10_000_000_000),
        mock.patch.object(
            observer,
            "_priority_mount_directory_identity",
            return_value=(11, 22),
        ),
        pytest.raises(health.GateAbort, match="unavailable"),
    ):
        observer.ProfilerPriorityGuard(
            args, mount_source=args.priority_signal.parent
        ).sample()


def test_profiler_priority_guard_accepts_later_gaps_but_rejects_rollbacks() -> None:
    args = profiler_observer_args()
    signals = [
        profiler_priority_document(7, policy_sha256=args.priority_policy_sha256),
        profiler_priority_document(10, policy_sha256=args.priority_policy_sha256),
        profiler_priority_document(9, policy_sha256=args.priority_policy_sha256),
    ]
    with (
        mock.patch.object(
            observer,
            "_require_private_file",
            side_effect=_priority_guard_reader(args, signals),
        ),
        mock.patch.object(observer.time, "monotonic_ns", return_value=11_000_000_000),
        mock.patch.object(
            observer,
            "_priority_mount_directory_identity",
            return_value=(11, 22),
        ),
    ):
        guard = observer.ProfilerPriorityGuard(
            args, mount_source=args.priority_signal.parent
        )
        assert guard.sample()["sequence"] == 7
        assert guard.sample()["sequence"] == 10
        with pytest.raises(health.GateAbort, match="sequence_rolled_back"):
            guard.sample()


def test_profiler_priority_signal_is_cross_bound_to_candidate_mount() -> None:
    args = profiler_observer_args()
    boundary = SimpleNamespace(
        document={
            "mounts": [
                {
                    "source": "C:/candidate-priority",
                    "destination": "/run/subgen-priority",
                    "type": "bind",
                    "mode": "ro",
                    "read_write": False,
                    "propagation": "rprivate",
                }
            ]
        }
    )

    args.priority_signal = Path("C:/candidate-priority/pressure.json")
    assert observer._profiler_priority_mount_source(args, boundary) == Path(
        "C:/candidate-priority"
    )

    args.priority_signal = Path("C:/observer-only/pressure.json")
    with pytest.raises(health.GateAbort, match="signal_mount_binding"):
        observer._profiler_priority_mount_source(args, boundary)


def test_profiler_priority_guard_rejects_mount_inode_replacement() -> None:
    args = profiler_observer_args()
    signal = profiler_priority_document(7, policy_sha256=args.priority_policy_sha256)
    with (
        mock.patch.object(
            observer,
            "_require_private_file",
            side_effect=_priority_guard_reader(args, [signal]),
        ),
        mock.patch.object(observer.time, "monotonic_ns", return_value=8_000_000_000),
        mock.patch.object(
            observer,
            "_priority_mount_directory_identity",
            side_effect=[(11, 22), (11, 22), (33, 44)],
        ),
    ):
        guard = observer.ProfilerPriorityGuard(
            args, mount_source=args.priority_signal.parent
        )
        with pytest.raises(health.GateAbort, match="mount_identity_changed"):
            guard.sample()


def test_profiler_supervisor_is_bounded_and_preserves_exact_cleanup() -> None:
    args = profiler_observer_args()
    args.emit_systemd_run_script = Path("C:/private/run-profiler.sh")
    binding = health.CandidateBinding(
        name=args.container,
        container_id=args.expected_container_id,
        image_config=args.expected_image_config,
        runtime_commit=args.runtime_commit,
        gate_role=args.gate_role,
        gate_token_digest=health.sha256_bytes(args.gate_token.encode("ascii")),
        command_digest=args.expected_command_sha256,
        boundary_digest=args.boundary_manifest_sha256,
    )
    client = mock.Mock()
    client.inspect.return_value = {"Id": binding.container_id}
    written: dict[str, object] = {}

    def capture(path: Path, payload: bytes, mode: int) -> None:
        written.update(path=path, payload=payload, mode=mode)

    with (
        mock.patch.object(observer, "_verified_runtime_identities"),
        mock.patch.object(observer, "_verify_adjacent_frozen_sampler"),
        mock.patch.object(health, "ensure_boundary_expectation"),
        mock.patch.object(health, "DockerClient", return_value=client),
        mock.patch.object(health, "bind_candidate", return_value=binding),
        mock.patch.object(observer, "validate_runtime_chunk_policy") as runtime_policy,
        mock.patch.object(observer, "_read_api_key", return_value="unused"),
        mock.patch.object(observer, "validate_request_isolation") as request_isolation,
        mock.patch.object(
            observer,
            "_create_verified_runtime_bundle",
            return_value=(
                Path("C:/private/bundle/runtime_gate_observer.py"),
                Path("C:/private/bundle/gate_health_sampler.py"),
            ),
        ),
        mock.patch.object(
            observer,
            "_cleanup_command",
            return_value=[
                "C:/Python/python.exe",
                "C:/private/bundle/gate_health_sampler.py",
                "--cleanup-only",
                "--systemd-stop-post",
            ],
        ) as cleanup_command,
        mock.patch.object(health, "_write_private_create_only", side_effect=capture),
    ):
        assert observer.emit_systemd_run_script(args) == 0

    runtime_policy.assert_not_called()
    request_isolation.assert_not_called()
    cleanup_command.assert_called_once_with(
        args, Path("C:/private/bundle/gate_health_sampler.py")
    )
    script = bytes(written["payload"]).decode("utf-8")
    unit = f"subgen-task11b-profiler-{binding.gate_token_digest[:16]}"
    assert f"--unit={unit}" in script
    assert "--property=RuntimeMaxSec=1320s" in script
    assert "--candidate-mode profiler" in script
    assert "--expected-profiler-returncode 3" in script
    assert script.count("--systemd-stop-post") == 1


def test_main_dispatches_profiler_only_after_supervisor_verification() -> None:
    args = SimpleNamespace(
        emit_systemd_run_script=None,
        candidate_mode="profiler",
    )
    parsed = mock.Mock()
    parsed.parse_args.return_value = args
    order: list[str] = []
    with (
        mock.patch.object(observer, "_bootstrap_verified_runtime"),
        mock.patch.object(observer, "parser", return_value=parsed),
        mock.patch.object(observer, "validate_args"),
        mock.patch.object(
            observer,
            "verify_systemd_supervisor",
            side_effect=lambda _args: order.append("verify"),
        ),
        mock.patch.object(
            observer,
            "run_profiler_observer",
            create=True,
            side_effect=lambda _args: order.append("profiler") or 0,
        ) as profiler,
        mock.patch.object(observer, "run_observer") as runtime,
    ):
        assert observer.main([]) == 0

    assert order == ["verify", "profiler"]
    profiler.assert_called_once_with(args)
    runtime.assert_not_called()


def test_profiler_observer_stops_exact_id_drains_then_seals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = profiler_observer_args()
    binding = health.CandidateBinding(
        name=args.container,
        container_id=args.expected_container_id,
        image_config=args.expected_image_config,
        runtime_commit=args.runtime_commit,
        gate_role=args.gate_role,
        gate_token_digest=health.sha256_bytes(args.gate_token.encode("ascii")),
        command_digest=args.expected_command_sha256,
        boundary_digest=args.boundary_manifest_sha256,
    )
    frigate = health.ObservedBinding("frigate", "9" * 64, "sha256:" + "a" * 64)
    observation = health.ObservationOutcome(181, 900.0, 100.0, 0, 0)
    client = mock.Mock()
    logs = mock.Mock()
    order: list[str] = []

    class Evidence(FakeEvidenceWriter):
        def seal(self, **record: object) -> str:
            order.append("seal")
            return super().seal(**record)

    raw_evidence = Evidence()
    guard = mock.Mock()
    guard.gpu_uuid = priority_policy_document()["gpu_uuid"]
    guard.arm.side_effect = lambda **_kwargs: order.append("arm") or {"sequence": 2}
    guard.sample.side_effect = lambda: order.append("priority") or {"sequence": 3}
    cleanup = {"verified_stopped": True, "already_absent": False}
    completion = {
        "candidate": {"running": False},
        "frigate": {"running": True},
        "logs_drained_through_wall": 101.0,
    }
    observed_calls: list[tuple[SimpleNamespace, dict[str, object]]] = []

    def observe(*call_args: object, **kwargs: object) -> health.ObservationOutcome:
        order.append("observe")
        observed_calls.append((call_args[0], kwargs))
        priority_revalidate = kwargs["priority_revalidate"]
        assert callable(priority_revalidate)
        priority_revalidate()
        return observation

    with (
        mock.patch.object(
            observer,
            "_verified_runtime_identities",
            return_value=(args.observer_sha256, args.sampler_sha256),
        ),
        mock.patch.object(observer, "_verify_adjacent_frozen_sampler"),
        mock.patch.object(health, "DockerClient", return_value=client),
        mock.patch.object(health, "install_signal_handlers", return_value={}),
        mock.patch.object(
            health,
            "ensure_boundary_expectation",
            return_value=profiler_boundary(),
        ),
        mock.patch.object(health, "bind_candidate", return_value=binding),
        mock.patch.object(health, "bind_observed_container", return_value=frigate),
        mock.patch.object(
            health, "load_camera_expectations", return_value={"camera": 8.0}
        ),
        mock.patch.object(
            health, "verify_bound_docker_daemon", return_value=("daemon", "boot")
        ),
        mock.patch.object(observer, "ProfilerPriorityGuard", return_value=guard),
        mock.patch.object(observer.time, "monotonic", return_value=100.0),
        mock.patch.object(health.EvidenceWriter, "open", return_value=raw_evidence),
        mock.patch.object(health, "IncrementalLogScanner", return_value=logs),
        mock.patch.object(
            health,
            "observe_gate",
            side_effect=observe,
        ),
        mock.patch.object(
            health,
            "stop_bound_candidate",
            side_effect=lambda *_args: order.append("stop") or cleanup,
        ) as stop,
        mock.patch.object(
            health,
            "validate_stop_completion",
            side_effect=lambda *_args: order.append("drain") or completion,
        ),
        mock.patch.object(
            observer,
            "_read_source_bytes_independently",
            return_value=(b"sealed-profiler-evidence", "e" * 64),
        ),
    ):
        assert observer.run_profiler_observer(args) == 0

    assert order == ["arm", "observe", "priority", "stop", "drain", "priority", "seal"]
    assert guard.arm.call_args.kwargs["deadline"] == 220.0
    assert observed_calls[0][0] is args
    assert observed_calls[0][1]["startup_deadline"] == 220.0
    stop.assert_called_once_with(client, binding, args)
    assert raw_evidence.seals[0]["cleanup"]["verified_stopped"] is True
    assert raw_evidence.seals[0]["cleanup"]["completion"] == completion
    output = capsys.readouterr().out
    assert "profiler_evidence_sha256=" + "f" * 64 in output
    assert "profiler_evidence_seal_sha256=" + "e" * 64 in output


def test_profiler_observer_failure_still_stops_exact_id_without_pass_seal() -> None:
    args = profiler_observer_args()
    binding = health.CandidateBinding(
        name=args.container,
        container_id=args.expected_container_id,
        image_config=args.expected_image_config,
        runtime_commit=args.runtime_commit,
        gate_role=args.gate_role,
        gate_token_digest=health.sha256_bytes(args.gate_token.encode("ascii")),
        command_digest=args.expected_command_sha256,
        boundary_digest=args.boundary_manifest_sha256,
    )
    frigate = health.ObservedBinding("frigate", "9" * 64, "sha256:" + "a" * 64)
    raw_evidence = FakeEvidenceWriter()
    guard = mock.Mock()
    guard.gpu_uuid = priority_policy_document()["gpu_uuid"]
    guard.arm.return_value = {"sequence": 2}
    client = mock.Mock()
    with (
        mock.patch.object(
            observer,
            "_verified_runtime_identities",
            return_value=(args.observer_sha256, args.sampler_sha256),
        ),
        mock.patch.object(observer, "_verify_adjacent_frozen_sampler"),
        mock.patch.object(health, "DockerClient", return_value=client),
        mock.patch.object(health, "install_signal_handlers", return_value={}),
        mock.patch.object(
            health,
            "ensure_boundary_expectation",
            return_value=profiler_boundary(),
        ),
        mock.patch.object(health, "bind_candidate", return_value=binding),
        mock.patch.object(health, "bind_observed_container", return_value=frigate),
        mock.patch.object(health, "load_camera_expectations", return_value={}),
        mock.patch.object(
            health, "verify_bound_docker_daemon", return_value=("daemon", "boot")
        ),
        mock.patch.object(observer, "ProfilerPriorityGuard", return_value=guard),
        mock.patch.object(health.EvidenceWriter, "open", return_value=raw_evidence),
        mock.patch.object(health, "IncrementalLogScanner", return_value=mock.Mock()),
        mock.patch.object(
            health, "observe_gate", side_effect=health.GateAbort("profiler failed")
        ),
        mock.patch.object(
            health,
            "stop_bound_candidate",
            return_value={"verified_stopped": True},
        ) as stop,
        pytest.raises(health.GateAbort, match="profiler_failed"),
    ):
        observer.run_profiler_observer(args)

    stop.assert_called_once_with(client, binding, args)
    assert raw_evidence.seals == []


def sampler_binding_document() -> dict[str, object]:
    return {
        "schema": "subgen.task11b.sampler-binding/v1",
        "sampler_commit": "1" * 40,
        "sampler_blob": "2" * 40,
        "sampler_sha256": "3" * 64,
        "test_blob": "4" * 40,
        "test_sha256": "5" * 64,
        "observer_blob": "6" * 40,
        "observer_sha256": "7" * 64,
        "observer_test_blob": "8" * 40,
        "observer_test_sha256": "9" * 64,
        "gate_seal_sha256": "a" * 64,
        "producer_sha256": "b" * 64,
        "policy_sha256": "c" * 64,
        "unloaded_gpu_envelope_sha256": "d" * 64,
        "model_envelope_catalog_sha256": "e" * 64,
        "execution_boundary_manifest_sha256": "f" * 64,
    }


def create_fixture_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[observer.FixtureSet, Path]:
    monkeypatch.setattr(observer, "_owner_id", lambda: None)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    media = tmp_path / "media"
    media.mkdir(mode=0o700)
    for directory in sorted(observer.EXPECTED_FIXTURE_DIRECTORIES):
        (media / directory).mkdir(mode=0o700)
    document = fixture_document()
    manifest = private / "fixtures.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    manifest.chmod(0o600)
    for role, content in {
        "long/source.mkv": b"long-source",
        "short/one.mkv": b"short-one",
        "short/two.mkv": b"short-two",
        "reload/source.mkv": b"reload-source",
        "invalid/source.mkv": b"invalid-source",
        "silent/source.mkv": b"silent-source",
    }.items():
        target = media / role
        target.write_bytes(content)
        target.chmod(0o600)
    fixtures = observer.load_fixture_manifest(
        manifest.resolve(), boundary(media.resolve())
    )
    return fixtures, manifest


class FakeEvidenceWriter:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.seals: list[dict[str, object]] = []
        self.closed = False

    def write(self, record: dict[str, object]) -> None:
        self.records.append(record)

    def seal(self, **record: object) -> str:
        self.seals.append(record)
        self.closed = True
        return "f" * 64

    def close(self) -> None:
        self.closed = True


def test_runtime_status_preserves_and_validates_exact_priority_transition() -> None:
    healthy = observer.validate_runtime_status(
        healthy_status(),
        expected_model="medium",
        expected_reserve_bytes=8 * health.GIB,
        observed_gpu_total_bytes=24 * health.GIB,
        expected_priority_state="clear",
        expected_policy_sha256="3" * 64,
        expected_controller_phase="normal",
        expected_recovery_reason=None,
        expected_admission_open=True,
        expected_model_resident=True,
    )
    assert (healthy["controller_state"], healthy["admission_open"]) == (
        "normal",
        True,
    )

    asserted_payload = copy.deepcopy(healthy_status())
    asserted_resource = asserted_payload["resource_management"]
    assert isinstance(asserted_resource, dict)
    asserted_priority = asserted_resource["priority_pressure"]
    assert isinstance(asserted_priority, dict)
    asserted_resource.update(
        {
            "controller_state": "yielding",
            "recovery_reason": "priority_pressure",
            "admission_open": False,
        }
    )
    asserted_priority.update(
        {
            "state": "asserted",
            "controller_phase": "yielding",
            "recovery_reason": "priority_pressure",
            "distinct_clear_count": 0,
        }
    )
    asserted = observer.validate_runtime_status(
        asserted_payload,
        expected_model="medium",
        expected_reserve_bytes=8 * health.GIB,
        observed_gpu_total_bytes=24 * health.GIB,
        expected_priority_state="asserted",
        expected_policy_sha256="3" * 64,
        expected_controller_phase="yielding",
        expected_recovery_reason="priority_pressure",
        expected_admission_open=False,
        expected_model_resident=True,
    )
    assert asserted["controller_state"] == "yielding"
    assert asserted["priority_pressure"]["state"] == "asserted"
    assert asserted["priority_pressure"]["transition_observation_digest"] == "8" * 64

    mismatched = copy.deepcopy(asserted_payload)
    resource = mismatched["resource_management"]
    assert isinstance(resource, dict)
    priority = resource["priority_pressure"]
    assert isinstance(priority, dict)
    priority["controller_phase"] = "recovering"
    with pytest.raises(health.GateAbort, match="inconsistent|phase"):
        observer.validate_runtime_status(
            mismatched,
            expected_model="medium",
            expected_reserve_bytes=8 * health.GIB,
            observed_gpu_total_bytes=24 * health.GIB,
            expected_priority_state="asserted",
            expected_policy_sha256="3" * 64,
            expected_controller_phase="yielding",
            expected_recovery_reason="priority_pressure",
            expected_admission_open=False,
            expected_model_resident=True,
        )


def test_runtime_chunk_policy_is_explicit_and_exact() -> None:
    item = {
        "Env": [
            "AUTO_DELETE_INVALID_MEDIA=false",
            "AUTO_DELETE_FAILED_FILES=false",
            "SKIP_STARTUP_SCAN=True",
            "MONITOR=False",
            "PROCESS_ADDED_MEDIA=False",
            "PROCESS_MEDIA_ON_PLAY=False",
            "SEGMENTATION_ENABLED=True",
            "SEGMENTATION_CHUNK_MINUTES=5",
        ]
    }
    assert observer.validate_runtime_chunk_policy(item, expected_chunk_minutes=5) == {
        "segmentation_enabled": True,
        "chunk_minutes": 5,
        "skip_startup_scan": True,
        "monitor": False,
        "process_added_media": False,
        "process_media_on_play": False,
    }

    for replacement in (
        "SEGMENTATION_CHUNK_MINUTES=auto",
        "SEGMENTATION_CHUNK_MINUTES=20",
        "SEGMENTATION_ENABLED=False",
    ):
        changed = copy.deepcopy(item)
        environment = changed["Env"]
        assert isinstance(environment, list)
        key = replacement.split("=", 1)[0] + "="
        environment[:] = [
            replacement if value.startswith(key) else value for value in environment
        ]
        with pytest.raises(health.GateAbort, match="segmentation|chunk_policy"):
            observer.validate_runtime_chunk_policy(changed, expected_chunk_minutes=5)

    for key, bad_value in (
        ("SKIP_STARTUP_SCAN", None),
        ("SKIP_STARTUP_SCAN", "False"),
        ("SKIP_STARTUP_SCAN", "true"),
        ("MONITOR", None),
        ("MONITOR", "True"),
        ("PROCESS_ADDED_MEDIA", None),
        ("PROCESS_ADDED_MEDIA", "True"),
        ("PROCESS_MEDIA_ON_PLAY", None),
        ("PROCESS_MEDIA_ON_PLAY", "True"),
    ):
        changed = copy.deepcopy(item)
        environment = changed["Env"]
        assert isinstance(environment, list)
        environment[:] = [
            value for value in environment if not value.startswith(key + "=")
        ]
        if bad_value is not None:
            environment.append(f"{key}={bad_value}")
        with pytest.raises(health.GateAbort, match="isolation|startup"):
            observer.validate_runtime_chunk_policy(changed, expected_chunk_minutes=5)

    for alias in ("PROCADDEDMEDIA=False", "PROCMEDIAONPLAY=False"):
        changed = copy.deepcopy(item)
        environment = changed["Env"]
        assert isinstance(environment, list)
        environment.append(alias)
        with pytest.raises(health.GateAbort, match="isolation"):
            observer.validate_runtime_chunk_policy(changed, expected_chunk_minutes=5)


def test_request_isolation_requires_matching_private_candidate_api_key() -> None:
    secret = "owner-only-request-key"
    item = {
        "Env": [
            "AUTO_DELETE_INVALID_MEDIA=false",
            "AUTO_DELETE_FAILED_FILES=false",
            f"SUBGEN_API_KEY={secret}",
        ]
    }
    observer.validate_request_isolation(item, secret)

    for changed in (
        {"Env": item["Env"][:-1]},
        {
            "Env": [
                "AUTO_DELETE_INVALID_MEDIA=false",
                "AUTO_DELETE_FAILED_FILES=false",
                "SUBGEN_API_KEY=different-owner-key",
            ]
        },
    ):
        with pytest.raises(health.GateAbort, match="request_isolation") as caught:
            observer.validate_request_isolation(changed, secret)
        assert secret not in str(caught.value)

    with pytest.raises(health.GateAbort, match="api_key_file"):
        observer._read_api_key(None)


def test_idle_recovery_proof_requires_three_complete_spaced_polls() -> None:
    tracker = observer.RuntimeCycleTracker()
    mark = tracker.mark()
    tracker.observe(
        {
            "controller_state": "recovering",
            "recovery_reason": "idle_cleanup",
            "admission_open": False,
        },
        100.0,
    )
    for moment in (105.0, 110.0):
        tracker.observe(
            {
                "controller_state": "recovering",
                "recovery_reason": "idle_cleanup",
                "admission_open": False,
            },
            moment,
        )
    assert tracker.idle_recovery_proof(mark) is None
    tracker.observe(
        {
            "controller_state": "normal",
            "recovery_reason": None,
            "admission_open": True,
        },
        115.0,
    )
    proof = tracker.idle_recovery_proof(mark)
    assert proof is not None
    assert proof.complete_health_polls == 3
    assert proof.elapsed_seconds == 15.0


def test_idle_recovery_rejects_reopening_before_three_health_polls() -> None:
    tracker = observer.RuntimeCycleTracker()
    mark = tracker.mark()
    tracker.observe(
        {
            "controller_state": "recovering",
            "recovery_reason": "idle_cleanup",
            "admission_open": False,
        },
        100.0,
    )
    tracker.observe(
        {
            "controller_state": "normal",
            "recovery_reason": None,
            "admission_open": True,
        },
        105.0,
    )
    with pytest.raises(health.GateAbort, match="three_complete"):
        tracker.idle_recovery_proof(mark)


def test_evidence_binds_separate_observer_and_sampler_without_private_values() -> None:
    raw = FakeEvidenceWriter()
    token = "task11b-private-token"
    host_path = "/var/lib/private/fixture"
    camera_name = "Private Camera"
    evidence = observer.LockedEvidence(raw, (token, host_path, camera_name))
    evidence.write(
        {
            "event": "observer_start",
            "observer_sha256": "1" * 64,
            "sampler_sha256": "2" * 64,
            "gate_token_sha256": "3" * 64,
            "camera_count": 15,
        }
    )
    evidence.seal(
        outcome="pass",
        sampler_sha256="2" * 64,
        image_config="sha256:" + "4" * 64,
        cleanup={"verified_stopped": True},
    )
    serialized = json.dumps(raw.records)
    assert "1" * 64 in serialized
    assert "2" * 64 in serialized
    assert token not in serialized
    assert host_path not in serialized
    assert camera_name not in serialized
    assert raw.seals[0]["sampler_sha256"] == "2" * 64

    with pytest.raises(health.GateAbort, match="privacy"):
        observer.LockedEvidence(FakeEvidenceWriter(), (token,)).write(
            {"event": "bad", "value": f"prefix-{token}"}
        )


def test_sampler_checksum_is_checked_before_sampler_bytes_execute() -> None:
    sentinel = object()

    def fake_read(path: Path, *, maximum: int, label: str):
        assert maximum == observer.MAX_SAMPLER_SOURCE_BYTES
        if path.name == "runtime_gate_observer.py":
            return b"observer", "1" * 64
        assert label == "frozen health sampler"
        return b"raise AssertionError('must not execute')", "2" * 64

    with (
        mock.patch.object(observer, "health", sentinel),
        mock.patch.object(
            observer, "_read_source_bytes_independently", side_effect=fake_read
        ),
        pytest.raises(observer.ObserverBootstrapAbort, match="sampler_checksum"),
    ):
        observer._bootstrap_verified_runtime(
            ["--observer-sha256", "1" * 64, "--sampler-sha256", "3" * 64]
        )
    assert observer.health is health


def test_failure_after_binding_stops_only_the_bound_candidate() -> None:
    args = SimpleNamespace(
        expected_docker_daemon_id="daemon",
        expected_host_boot_id="00000000-0000-4000-8000-000000000000",
        observer_sha256="1" * 64,
        sampler_sha256="2" * 64,
        expected_chunk_minutes=5,
    )
    binding = health.CandidateBinding(
        name="subgen-task11b-runtime-test",
        container_id="a" * 64,
        image_config="sha256:" + "b" * 64,
        runtime_commit="c" * 40,
        gate_role="runtime-auto",
        gate_token_digest="d" * 64,
        command_digest="e" * 64,
        boundary_digest="f" * 64,
    )
    stopped = {"verified_stopped": True}
    client = mock.Mock()
    client.inspect.return_value = {"Id": binding.container_id}
    with (
        mock.patch.object(
            observer,
            "_verified_runtime_identities",
            return_value=(args.observer_sha256, args.sampler_sha256),
        ),
        mock.patch.object(health, "DockerClient", return_value=client),
        mock.patch.object(health, "install_signal_handlers", return_value={}),
        mock.patch.object(
            health, "ensure_boundary_expectation", return_value=mock.Mock()
        ),
        mock.patch.object(health, "bind_candidate", return_value=binding),
        mock.patch.object(
            observer,
            "validate_runtime_chunk_policy",
            side_effect=health.GateAbort("policy failure"),
        ),
        mock.patch.object(health, "stop_bound_candidate", return_value=stopped) as stop,
        pytest.raises(health.GateAbort, match="policy_failure"),
    ):
        observer.run_observer(args)
    stop.assert_called_once()
    assert stop.call_args.args[1] is binding


def test_invalid_and_silent_events_attest_retention_without_paths_in_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures, _manifest = create_fixture_tree(tmp_path, monkeypatch)
    originals = observer.capture_original_snapshots(fixtures)
    scanner = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        (item.container_media for item in fixtures.all_items),
    )
    invalid_event = {
        "event": "media_validation_failed",
        "path": fixtures.invalid.container_media,
        "failure_class": "invalid_media",
        "validator_outcomes": {
            "ffprobe": "invalid_format",
            "pyav": "invalid_format",
        },
    }
    scanner.parse_lines(
        "INFO SUBGEN_EVENT "
        + json.dumps(invalid_event, separators=(",", ":"))
        + "\nINFO MEDIA_VALIDATION outcome=no_audio ffprobe=no_audio "
        f"pyav=no_audio path={fixtures.silent.container_media}\n"
    )
    assert scanner.matching_events(
        "media_validation_failed", [fixtures.invalid.container_media]
    ) == [invalid_event]
    assert fixtures.silent.container_media in scanner.silent_paths
    observer.attest_originals_unchanged(fixtures, originals)
    observer.assert_fixture_directory_exact(fixtures, "invalid", allow_outputs=False)
    observer.assert_fixture_directory_exact(fixtures, "silent", allow_outputs=False)

    mixed = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        [fixtures.silent.container_media],
    )
    mixed.parse_lines(
        "INFO MEDIA_VALIDATION outcome=no_audio ffprobe=timeout "
        f"pyav=no_audio path={fixtures.silent.container_media}\n"
    )
    assert mixed.silent_paths == set()


def test_incremental_event_overlap_does_not_duplicate_events_or_unloads() -> None:
    scanner = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        ["/media/short/one.mkv"],
    )
    line = (
        "2026-09-01T00:00:00Z INFO SUBGEN_EVENT "
        + json.dumps(worker_event("worker_finish", "/media/short/one.mkv"))
        + "\n2026-09-01T00:00:01Z INFO Model unloaded from memory\n"
    )
    scanner.parse_lines(line)
    scanner.parse_lines(line)
    assert len(scanner.events) == 1
    assert scanner.unload_count == 1


def test_phase_freshness_requires_new_worker_start_and_finish_after_mark() -> None:
    path = "/media/reload/source.mkv"
    item = observer.FixtureItem(
        role="reload",
        index=0,
        media_relative="reload/source.mkv",
        subtitle_relative="reload/source.en.srt",
    )
    scanner = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        [path],
    )
    scanner.parse_lines(
        "2026-09-01T00:00:00Z SUBGEN_EVENT "
        + json.dumps(worker_event("worker_start", path))
        + "\n2026-09-01T00:00:01Z SUBGEN_EVENT "
        + json.dumps(worker_event("worker_finish", path))
        + "\n"
    )
    mark = scanner.mark()
    assert not observer._worker_finished(scanner, (item,), after_mark=mark)

    scanner.parse_lines(
        "2026-09-01T00:01:00Z SUBGEN_EVENT "
        + json.dumps(worker_event("worker_start", path))
        + "\n2026-09-01T00:01:01Z SUBGEN_EVENT "
        + json.dumps(worker_event("worker_finish", path))
        + "\n"
    )
    assert observer._worker_finished(scanner, (item,), after_mark=mark)


def test_worker_lifecycle_rejects_a_changed_source_generation() -> None:
    path = "/media/reload/source.mkv"
    item = observer.FixtureItem(
        role="reload",
        index=0,
        media_relative="reload/source.mkv",
        subtitle_relative="reload/source.en.srt",
    )
    scanner = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        [path],
    )
    mark = scanner.mark()
    scanner.parse_lines(
        "2026-09-01T00:00:00Z SUBGEN_EVENT "
        + json.dumps(worker_event("worker_start", path))
        + "\n2026-09-01T00:00:01Z SUBGEN_EVENT "
        + json.dumps(
            worker_event("worker_finish", path, source_identity=(1, 2, 3, 4, 6))
        )
        + "\n"
    )
    with pytest.raises(health.GateAbort, match="identity"):
        observer._worker_finished(scanner, (item,), after_mark=mark)


def test_worker_event_outside_the_declared_workload_fails_closed() -> None:
    scanner = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        ["/media/short/one.mkv"],
    )
    with pytest.raises(health.GateAbort, match="allowlist"):
        scanner.parse_lines(
            "SUBGEN_EVENT "
            + json.dumps(worker_event("worker_start", "/media/short/two.mkv"))
        )


def test_event_retention_discards_unbounded_unneeded_fields() -> None:
    path = "/media/short/one.mkv"
    scanner = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        [path],
    )
    scanner.parse_lines(
        "SUBGEN_EVENT "
        + json.dumps({**worker_event("worker_finish", path), "ignored": "x" * 100_000})
    )
    assert scanner.events == [worker_event("worker_finish", path)]
    assert scanner._retained_event_bytes < 256

    scanner.parse_lines(
        "SUBGEN_EVENT " + json.dumps(worker_event("worker_error", path))
    )
    with pytest.raises(health.GateAbort, match="worker_error"):
        scanner.assert_no_worker_errors([path])


def test_shutdown_drain_checks_structured_errors_only_after_final_scan() -> None:
    order: list[object] = []

    class Scanner:
        def scan(self, until_wall: float) -> None:
            assert until_wall > 0
            order.append("scan")

        def assert_no_worker_errors(self, paths) -> None:
            order.append(tuple(paths))

    item = observer.FixtureItem(
        role="invalid",
        index=0,
        media_relative="invalid/source.mkv",
        subtitle_relative=None,
    )
    observer.drain_workload_events_after_stop(Scanner(), (item,))
    assert order == ["scan", ("/media/invalid/source.mkv",)]


def test_fixture_manifest_rejects_duplicate_json_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "_owner_id", lambda: None)
    private = tmp_path / "private"
    private.mkdir()
    media = tmp_path / "media"
    media.mkdir()
    payload = json.dumps(fixture_document())
    payload = payload[:-1] + ',"schema":"subgen.task11b.runtime-fixtures/v1"}'
    manifest = private / "fixtures.json"
    manifest.write_text(payload, encoding="utf-8")
    with pytest.raises(health.GateAbort, match="duplicate"):
        observer.load_fixture_manifest(manifest.resolve(), boundary(media.resolve()))


def test_fixture_snapshot_rejects_symlink_and_oversize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures, _manifest = create_fixture_tree(tmp_path, monkeypatch)
    source = fixtures.media_root / fixtures.long.media_relative
    source.unlink()
    target = fixtures.media_root / "target.mkv"
    target.write_bytes(b"target")
    try:
        source.symlink_to(target)
    except OSError:
        source.write_bytes(b"placeholder")
        real_lstat = Path.lstat

        def lstat_with_fixture_symlink(path: Path):
            item = real_lstat(path)
            if path == source:
                return SimpleNamespace(st_mode=stat.S_IFLNK)
            return item

        with (
            mock.patch.object(Path, "lstat", lstat_with_fixture_symlink),
            pytest.raises(health.GateAbort, match="symlink"),
        ):
            observer.snapshot_fixture_file(fixtures, fixtures.long.media_relative)
    else:
        with pytest.raises(health.GateAbort, match="symlink"):
            observer.snapshot_fixture_file(fixtures, fixtures.long.media_relative)

    source.unlink()
    source.write_bytes(b"12345")
    with pytest.raises(health.GateAbort, match="byte limit|size"):
        observer.snapshot_fixture_file(
            fixtures, fixtures.long.media_relative, maximum_bytes=4
        )


def test_partial_or_undeclared_output_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures, _manifest = create_fixture_tree(tmp_path, monkeypatch)
    partial = fixtures.media_root / "long" / ".source.en.srt.abc.tmp.srt"
    partial.write_text("partial", encoding="utf-8")
    with pytest.raises(health.GateAbort, match="partial|undeclared"):
        observer.assert_fixture_directory_exact(fixtures, "long", allow_outputs=True)


def test_atomic_publication_ledger_requires_paired_staging_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures, _manifest = create_fixture_tree(tmp_path, monkeypatch)
    temporary = ".source.en.srt.abcdef12.tmp.srt"
    final = "source.en.srt"
    ledger = observer.AtomicPublicationLedger(fixtures)
    ledger.observe("long", temporary, ledger.IN_CREATE, 0)
    ledger.observe("long", temporary, 0x00000002, 0)
    ledger.observe("long", temporary, ledger.IN_CLOSE_WRITE, 0)
    ledger.observe("long", temporary, ledger.IN_MOVED_FROM, 73)
    ledger.observe("long", final, ledger.IN_MOVED_TO, 73)
    ledger.assert_published((fixtures.long,))
    stale_mark = ledger.mark()
    with pytest.raises(health.GateAbort, match="publication"):
        ledger.assert_published((fixtures.long,), after_mark=stale_mark)

    direct = observer.AtomicPublicationLedger(fixtures)
    with pytest.raises(health.GateAbort, match="non_atomically"):
        direct.observe("long", final, direct.IN_CREATE, 0)

    transient = observer.AtomicPublicationLedger(fixtures)
    with pytest.raises(health.GateAbort, match="undeclared"):
        transient.observe("long", ".unbound.partial", transient.IN_CREATE, 0)


def test_subtitle_attestation_requires_final_bounded_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures, _manifest = create_fixture_tree(tmp_path, monkeypatch)
    output = fixtures.media_root / (fixtures.long.subtitle_relative or "")
    output.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        encoding="utf-8",
    )
    output.chmod(0o600)
    result = observer.snapshot_fixture_file(
        fixtures,
        fixtures.long.subtitle_relative or "",
        maximum_bytes=observer.MAX_SUBTITLE_BYTES,
        expected_subtitle=True,
        expected_duration_seconds=1.0,
    )
    assert result.size > 0
    assert result.sha256 == health.sha256_file(output)

    output.write_text("not an srt", encoding="utf-8")
    with pytest.raises(health.GateAbort, match="subtitle"):
        observer.snapshot_fixture_file(
            fixtures,
            fixtures.long.subtitle_relative or "",
            maximum_bytes=observer.MAX_SUBTITLE_BYTES,
            expected_subtitle=True,
            expected_duration_seconds=1.0,
        )


def test_long_subtitle_attestation_rejects_truncation_and_accepts_full_timeline() -> (
    None
):
    truncated = b"1\n00:00:00,000 --> 00:00:01,000\nOnly one cue\n"
    with pytest.raises(health.GateAbort, match="cover"):
        observer.validate_srt_payload(
            truncated,
            expected_duration_seconds=31 * 60,
        )

    cues = [
        ("00:00:00,000", "00:00:10,000"),
        ("00:05:00,000", "00:05:10,000"),
        ("00:10:00,000", "00:10:10,000"),
        ("00:15:00,000", "00:15:10,000"),
        ("00:20:00,000", "00:20:10,000"),
        ("00:25:00,000", "00:25:10,000"),
        ("00:30:50,000", "00:31:00,000"),
    ]
    payload = "\n\n".join(
        f"{index}\n{start} --> {end}\nCue {index}"
        for index, (start, end) in enumerate(cues, start=1)
    ).encode("utf-8")
    assert observer.validate_srt_payload(
        payload,
        expected_duration_seconds=31 * 60,
    ) == (7, 0, 31 * 60 * 1000)


def test_wait_until_uses_hard_timeout_without_extra_attempt() -> None:
    class Clock:
        def __init__(self) -> None:
            self.value = 0.0
            self.attempts = 0

        def now(self) -> float:
            return self.value

        def sleep(self, delay: float) -> None:
            self.value += delay

        def predicate(self) -> bool:
            self.attempts += 1
            return False

    clock = Clock()
    with pytest.raises(health.GateAbort, match="timed_out"):
        observer.wait_until(
            clock.predicate,
            timeout=3.0,
            failure_message="fixture timed out",
            clock=clock.now,
            sleeper=clock.sleep,
        )
    assert clock.value == 3.0
    assert clock.attempts == 4


def test_post_batch_fails_closed_on_timeout_without_retrying() -> None:
    connection = mock.Mock()
    connection.request.side_effect = TimeoutError("bounded")
    with (
        mock.patch.object(
            observer.http.client, "HTTPConnection", return_value=connection
        ),
        pytest.raises(health.GateAbort, match="timed_out"),
    ):
        observer.post_batch(
            "/media/long", api_key="owner-only-request-key", timeout=1.0
        )
    connection.request.assert_called_once()
    connection.close.assert_called_once()


def runtime_supervisor_args(token: str) -> SimpleNamespace:
    return SimpleNamespace(
        gate_token=token,
        expected_systemd_unit=(
            "subgen-task11b-runtime-" + health.sha256_bytes(token.encode())[:16]
        ),
        candidate_mode="runtime",
        start_timeout_seconds=120,
        long_timeout_seconds=7200,
        short_timeout_seconds=1800,
        recovery_timeout_seconds=300,
        reload_timeout_seconds=1800,
        retention_timeout_seconds=120,
        duration_seconds=900,
    )


def test_systemd_runtime_max_parser_accepts_generated_compound_format() -> None:
    assert observer._parse_systemd_timespan_microseconds("3h 36min") == (
        12_960 * 1_000_000
    )
    assert observer._parse_systemd_timespan_microseconds("22min") == (1_320 * 1_000_000)


def test_main_refuses_to_run_without_systemd_cleanup_supervisor() -> None:
    args = SimpleNamespace(
        emit_systemd_run_script=None,
        supervisor_armed=False,
        expected_systemd_unit=None,
    )
    parser = mock.Mock()
    parser.parse_args.return_value = args
    with (
        mock.patch.object(observer, "_bootstrap_verified_runtime"),
        mock.patch.object(observer, "parser", return_value=parser),
        mock.patch.object(observer, "validate_args"),
        mock.patch.object(
            observer,
            "verify_systemd_supervisor",
            side_effect=health.GateAbort("supervisor missing"),
        ),
        mock.patch.object(observer, "run_observer") as run,
        pytest.raises(health.GateAbort, match="supervisor"),
    ):
        observer.main([])
    run.assert_not_called()


def test_supervisor_attestation_rejects_forged_environment_outside_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "private-gate-token"
    args = runtime_supervisor_args(token)
    cleanup = ["/usr/bin/python3", "/private/gate_health_sampler.py"]
    digest = observer._cleanup_command_sha256(cleanup)
    monkeypatch.setenv("INVOCATION_ID", "a" * 32)
    monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid()))
    monkeypatch.setenv("TASK11B_CLEANUP_COMMAND_SHA256", digest)
    with (
        mock.patch.object(observer, "_cleanup_command", return_value=cleanup),
        mock.patch.object(observer.os, "open", return_value=71),
        mock.patch.object(observer.os, "close"),
        mock.patch.object(
            observer,
            "_read_all_fd",
            return_value=b"0::/user.slice/forged.service\n",
        ),
        mock.patch.object(health, "bounded_command") as systemctl,
        pytest.raises(health.GateAbort, match="outside_its_supervisor_cgroup"),
    ):
        observer.verify_systemd_supervisor(args)
    systemctl.assert_not_called()


def test_supervisor_attestation_binds_pid_unit_and_exact_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "private-gate-token"
    args = runtime_supervisor_args(token)
    unit = args.expected_systemd_unit
    cleanup = [
        "/usr/bin/python3",
        "/private/gate_health_sampler.py",
        "--cleanup-only",
        "--systemd-stop-post",
    ]
    digest = observer._cleanup_command_sha256(cleanup)
    invocation = "a" * 32
    cgroup = f"/system.slice/{unit}.service"
    monkeypatch.setenv("INVOCATION_ID", invocation)
    monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid()))
    monkeypatch.setenv("TASK11B_CLEANUP_COMMAND_SHA256", digest)
    properties = "\n".join(
        (
            f"InvocationID={invocation}",
            f"ControlGroup={cgroup}",
            f"MainPID={os.getpid()}",
            "ActiveState=active",
            "RuntimeMaxUSec=12960s",
            "ExecStopPost={ path=/usr/bin/python3 ; "
            f"argv[]={' '.join(cleanup)} ; ignore_errors=no ; "
            "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; "
            "status=0/0 ; }",
        )
    )
    with (
        mock.patch.object(observer, "_cleanup_command", return_value=cleanup),
        mock.patch.object(observer.os, "open", return_value=71),
        mock.patch.object(observer.os, "close"),
        mock.patch.object(
            observer,
            "_read_all_fd",
            return_value=f"0::{cgroup}\n".encode("ascii"),
        ),
        mock.patch.object(
            health,
            "bounded_command",
            return_value=SimpleNamespace(output=properties),
        ),
    ):
        observer.verify_systemd_supervisor(args)


@pytest.mark.parametrize("runtime_max", ("infinity", "12960s"))
def test_profiler_supervisor_rejects_unbounded_or_cross_mode_runtime_max(
    runtime_max: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = profiler_observer_args()
    args.expected_systemd_unit = observer._expected_systemd_unit(args)
    cleanup = [
        "/usr/bin/python3",
        "/private/gate_health_sampler.py",
        "--cleanup-only",
        "--systemd-stop-post",
    ]
    digest = observer._cleanup_command_sha256(cleanup)
    invocation = "a" * 32
    cgroup = f"/system.slice/{args.expected_systemd_unit}.service"
    monkeypatch.setenv("INVOCATION_ID", invocation)
    monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid()))
    monkeypatch.setenv("TASK11B_CLEANUP_COMMAND_SHA256", digest)
    properties = "\n".join(
        (
            f"InvocationID={invocation}",
            f"ControlGroup={cgroup}",
            f"MainPID={os.getpid()}",
            "ActiveState=active",
            f"RuntimeMaxUSec={runtime_max}",
            "ExecStopPost={ path=/usr/bin/python3 ; "
            f"argv[]={' '.join(cleanup)} ; ignore_errors=no ; "
            "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; "
            "status=0/0 ; }",
        )
    )
    with (
        mock.patch.object(observer, "_cleanup_command", return_value=cleanup),
        mock.patch.object(observer.os, "open", return_value=71),
        mock.patch.object(observer.os, "close"),
        mock.patch.object(
            observer,
            "_read_all_fd",
            return_value=f"0::{cgroup}\n".encode("ascii"),
        ),
        mock.patch.object(
            health,
            "bounded_command",
            return_value=SimpleNamespace(output=properties),
        ),
        pytest.raises(health.GateAbort, match="runtime_max"),
    ):
        observer.verify_systemd_supervisor(args)


@pytest.mark.parametrize(
    "exec_stop_post",
    (
        "{ path=/usr/bin/false ; argv[]=/usr/bin/python3 "
        "/private/gate_health_sampler.py --cleanup-only --systemd-stop-post ; "
        "ignore_errors=no ; }",
        "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 "
        "/private/gate_health_sampler.py --cleanup-only --systemd-stop-post ; "
        "ignore_errors=yes ; }",
        "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 "
        "/private/gate_health_sampler.py --cleanup-only --systemd-stop-post ; "
        "ignore_errors=no ; } { path=/usr/bin/true ; argv[]=/usr/bin/true ; "
        "ignore_errors=no ; }",
        "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 "
        "/private/gate_health_sampler.py --cleanup-only --systemd-stop-post "
        "--extra ; ignore_errors=no ; }",
    ),
)
def test_supervisor_attestation_rejects_inexact_cleanup_command(
    exec_stop_post: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "private-gate-token"
    args = runtime_supervisor_args(token)
    unit = args.expected_systemd_unit
    cleanup = [
        "/usr/bin/python3",
        "/private/gate_health_sampler.py",
        "--cleanup-only",
        "--systemd-stop-post",
    ]
    digest = observer._cleanup_command_sha256(cleanup)
    invocation = "a" * 32
    cgroup = f"/system.slice/{unit}.service"
    monkeypatch.setenv("INVOCATION_ID", invocation)
    monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid()))
    monkeypatch.setenv("TASK11B_CLEANUP_COMMAND_SHA256", digest)
    properties = "\n".join(
        (
            f"InvocationID={invocation}",
            f"ControlGroup={cgroup}",
            f"MainPID={os.getpid()}",
            "ActiveState=active",
            "RuntimeMaxUSec=12960s",
            f"ExecStopPost={exec_stop_post}",
        )
    )
    with (
        mock.patch.object(observer, "_cleanup_command", return_value=cleanup),
        mock.patch.object(observer.os, "open", return_value=71),
        mock.patch.object(observer.os, "close"),
        mock.patch.object(
            observer,
            "_read_all_fd",
            return_value=f"0::{cgroup}\n".encode("ascii"),
        ),
        mock.patch.object(
            health,
            "bounded_command",
            return_value=SimpleNamespace(output=properties),
        ),
        pytest.raises(health.GateAbort, match="cleanup"),
    ):
        observer.verify_systemd_supervisor(args)


def test_manifest_rejects_duplicate_fixture_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "_owner_id", lambda: None)
    private = tmp_path / "private"
    private.mkdir()
    media = tmp_path / "media"
    media.mkdir()
    document = fixture_document()
    short = document["short_resident"]
    assert isinstance(short, list)
    short[1] = copy.deepcopy(short[0])
    manifest = private / "fixtures.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(health.GateAbort, match="duplicate"):
        observer.load_fixture_manifest(manifest.resolve(), boundary(media.resolve()))


def test_manifest_parent_symlink_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "_owner_id", lambda: None)
    real = tmp_path / "real"
    real.mkdir()
    manifest = real / "fixtures.json"
    manifest.write_text(json.dumps(fixture_document()), encoding="utf-8")
    link = tmp_path / "linked"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        link.mkdir()
        linked_manifest = link / "fixtures.json"
        linked_manifest.write_text(
            manifest.read_text(encoding="utf-8"), encoding="utf-8"
        )
        real_lstat = Path.lstat

        def lstat_with_parent_symlink(path: Path):
            item = real_lstat(path)
            if path == link:
                return SimpleNamespace(st_mode=stat.S_IFLNK)
            return item

        with (
            mock.patch.object(Path, "lstat", lstat_with_parent_symlink),
            pytest.raises(health.GateAbort, match="symlink"),
        ):
            observer.load_fixture_manifest(linked_manifest, boundary(tmp_path))
    else:
        with pytest.raises(health.GateAbort, match="symlink"):
            observer.load_fixture_manifest(link / "fixtures.json", boundary(tmp_path))


def test_priority_assertion_requires_canonical_busy_or_degraded_observation() -> None:
    document = priority_assertion_document()
    validated = observer.validate_priority_assertion(
        document,
        canonical_json(document),
        expected_policy_sha256="3" * 64,
    )
    assert (
        validated["observation_digest"]
        == hashlib.sha256(("ab" * 32).encode("ascii")).hexdigest()
    )
    assert validated["source_generation"] == 17

    for mutation in (
        {**document, "pressure": 1},
        {**document, "reason_codes": ["higher_priority_unavailable"]},
        {**document, "reason_codes": ["policy_drift"]},
        {**document, "policy_sha256": "4" * 64},
    ):
        with pytest.raises(health.GateAbort):
            observer.validate_priority_assertion(
                mutation,
                canonical_json(mutation),
                expected_policy_sha256="3" * 64,
            )

    with pytest.raises(health.GateAbort, match="canonical"):
        observer.validate_priority_assertion(
            document,
            json.dumps(document, indent=2).encode("ascii") + b"\n",
            expected_policy_sha256="3" * 64,
        )


def test_priority_assertion_t0_is_after_final_path_identity_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "_owner_id", lambda: None)
    private = tmp_path / "priority"
    private.mkdir(mode=0o700)
    signal = private / "pressure.json"
    document = priority_assertion_document()
    signal.write_bytes(canonical_json(document))
    signal.chmod(0o600)

    calls: list[str] = []
    real_lstat = Path.lstat

    def tracking_lstat(path: Path):
        calls.append(f"lstat:{path.name}")
        return real_lstat(path)

    def capture_t0() -> int:
        calls.append("t0")
        return 2_100_000_000

    monkeypatch.setattr(Path, "lstat", tracking_lstat)
    assertion = observer.open_priority_assertion(
        signal.resolve(),
        expected_policy_sha256="3" * 64,
        expected_boot_id_sha256="1" * 64,
        expected_producer_epoch="2" * 32,
        monotonic_ns=capture_t0,
    )

    assert assertion.t0_monotonic_ns == 2_100_000_000
    assert assertion.document == document
    assert (
        assertion.attestation["observation_digest"]
        == hashlib.sha256(("ab" * 32).encode("ascii")).hexdigest()
    )
    assert calls[-1] == "t0"

    signal_lstats = 0

    def replaced_lstat(path: Path):
        nonlocal signal_lstats
        item = real_lstat(path)
        if path == signal:
            signal_lstats += 1
            if signal_lstats == 2:
                return SimpleNamespace(
                    st_mode=item.st_mode,
                    st_dev=item.st_dev,
                    st_ino=item.st_ino + 1,
                    st_uid=item.st_uid,
                    st_size=item.st_size,
                    st_mtime_ns=item.st_mtime_ns,
                    st_ctime_ns=item.st_ctime_ns,
                )
        return item

    monkeypatch.setattr(Path, "lstat", replaced_lstat)
    with pytest.raises(health.GateAbort, match="changed|replaced|identity"):
        observer.open_priority_assertion(
            signal.resolve(),
            expected_policy_sha256="3" * 64,
            expected_boot_id_sha256="1" * 64,
            expected_producer_epoch="2" * 32,
            monotonic_ns=capture_t0,
        )


def test_priority_assertion_open_rejects_stale_or_wrong_host_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "_owner_id", lambda: None)
    private = tmp_path / "priority"
    private.mkdir(mode=0o700)
    signal = private / "pressure.json"
    document = priority_assertion_document()
    signal.write_bytes(canonical_json(document))
    signal.chmod(0o600)

    for boot, epoch, now in (
        ("f" * 64, "2" * 32, 2_100_000_000),
        ("1" * 64, "e" * 32, 2_100_000_000),
        ("1" * 64, "2" * 32, 12_000_000_001),
    ):
        with pytest.raises(health.GateAbort, match="identity|stale|binding"):
            observer.open_priority_assertion(
                signal.resolve(),
                expected_policy_sha256="3" * 64,
                expected_boot_id_sha256=boot,
                expected_producer_epoch=epoch,
                monotonic_ns=lambda value=now: value,
            )


def test_receipt_journal_recovers_every_record_between_polls_and_buffers_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "_owner_id", lambda: None)
    parent = tmp_path / "receipts"
    parent.mkdir(mode=0o700)
    journal = parent / "runtime-receipts.jsonl"
    first = runtime_receipt(1, workload_sha256=None, model_resident=False)
    first.update(
        {
            "priority_state": "unavailable",
            "controller_phase": "recovering",
            "recovery_reason": "priority_pressure",
            "admission_open": False,
            "source_generation": None,
            "observation_digest": None,
            "transition_observation_digest": None,
            "heartbeat_age_ms": None,
            "source_age_ms": None,
            "policy_sha256": None,
        }
    )
    second = runtime_receipt(2)
    third = runtime_receipt(3)
    journal.write_bytes(canonical_json(first))
    journal.chmod(0o600)

    tailer = observer.RuntimeReceiptJournal(
        journal.resolve(),
        expected_runtime_epoch="5" * 32,
        expected_token_sha256="6" * 64,
    )
    try:
        assert [item["sequence"] for item in tailer.read_available()] == [1]
        with journal.open("ab") as target:
            target.write(canonical_json(second) + canonical_json(third)[:-7])
            target.flush()
            os.fsync(target.fileno())
        assert [item["sequence"] for item in tailer.read_available()] == [2]
        with journal.open("ab") as target:
            target.write(canonical_json(third)[-7:])
            target.flush()
            os.fsync(target.fileno())
        assert [item["sequence"] for item in tailer.read_available(final=True)] == [3]
        assert [item["sequence"] for item in tailer.receipts] == [1, 2, 3]
    finally:
        tailer.close()


def test_receipt_journal_rejects_gap_replacement_truncation_and_partial_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "_owner_id", lambda: None)

    def open_tailer(name: str, payload: bytes) -> observer.RuntimeReceiptJournal:
        parent = tmp_path / name
        parent.mkdir(mode=0o700)
        journal = parent / "runtime-receipts.jsonl"
        journal.write_bytes(payload)
        journal.chmod(0o600)
        return observer.RuntimeReceiptJournal(
            journal.resolve(),
            expected_runtime_epoch="5" * 32,
            expected_token_sha256="6" * 64,
        )

    gap = open_tailer(
        "gap", canonical_json(runtime_receipt(1)) + canonical_json(runtime_receipt(3))
    )
    with pytest.raises(health.GateAbort, match="sequence|gap"):
        gap.read_available(final=True)
    gap.close()

    partial = open_tailer("partial", canonical_json(runtime_receipt(1))[:-1])
    with pytest.raises(health.GateAbort, match="partial"):
        partial.read_available(final=True)
    partial.close()

    replaced = open_tailer("replace", canonical_json(runtime_receipt(1)))
    replaced.read_available()
    original_lstat = Path.lstat

    def replaced_lstat(path: Path):
        item = original_lstat(path)
        if path == replaced.path:
            return SimpleNamespace(
                st_mode=item.st_mode,
                st_dev=item.st_dev,
                st_ino=item.st_ino + 1,
            )
        return item

    with (
        mock.patch.object(Path, "lstat", replaced_lstat),
        pytest.raises(health.GateAbort, match="replaced|identity"),
    ):
        replaced.read_available()
    replaced.close()

    truncated = open_tailer("truncate", canonical_json(runtime_receipt(1)))
    truncated.read_available()
    with truncated.path.open("wb") as target:
        target.truncate(0)
    with pytest.raises(health.GateAbort, match="truncated|regressed"):
        truncated.read_available()
    truncated.close()

    mutated = open_tailer("mutate", canonical_json(runtime_receipt(1)))
    mutated.read_available()
    original = mutated.path.read_bytes()
    changed = original.replace(b'"sequence":1', b'"sequence":9', 1)
    assert len(changed) == len(original) and changed != original
    with mutated.path.open("r+b") as target:
        target.write(changed)
        target.flush()
        os.fsync(target.fileno())
    with pytest.raises(health.GateAbort, match="mutated|changed|prefix"):
        mutated.read_available()
    mutated.close()


def test_catalog_integrity_and_unique_model_identity_are_recomputed() -> None:
    catalog = model_catalog_document()
    raw = json.dumps(
        catalog,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    unloaded = unloaded_gpu_envelope_document()
    result = observer.validate_model_envelope_catalog(
        catalog,
        raw,
        expected_file_sha256=hashlib.sha256(raw).hexdigest(),
        candidate_config_digest="sha256:" + "a" * 64,
        candidate_layer_diff_ids=["sha256:" + "b" * 64],
        unloaded_envelope=unloaded,
    )
    entry = model_catalog_entry()
    entry_sha = hashlib.sha256(canonical_json(entry)).hexdigest()
    policy = entry["policy"]
    policy_sha = hashlib.sha256(canonical_json(policy)).hexdigest()
    identity = {
        "catalog_entry_sha256": entry_sha,
        "model_policy_sha256": policy_sha,
        "model_revision": "hf:" + "c" * 40,
        "selected_model": "medium",
    }
    assert (
        result["model_identity_sha256"]
        == hashlib.sha256(canonical_json(identity)).hexdigest()
    )
    assert result["catalog_sha256"] == hashlib.sha256(raw).hexdigest()

    bad_integrity = copy.deepcopy(catalog)
    integrity = bad_integrity["integrity"]
    assert isinstance(integrity, dict)
    integrity["canonical_payload_sha256"] = "sha256:" + "0" * 64
    bad_integrity_raw = json.dumps(
        bad_integrity, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    with pytest.raises(health.GateAbort, match="integrity"):
        observer.validate_model_envelope_catalog(
            bad_integrity,
            bad_integrity_raw,
            expected_file_sha256=hashlib.sha256(bad_integrity_raw).hexdigest(),
            candidate_config_digest="sha256:" + "a" * 64,
            candidate_layer_diff_ids=["sha256:" + "b" * 64],
            unloaded_envelope=unloaded,
        )

    duplicate = copy.deepcopy(catalog)
    entries = duplicate["entries"]
    assert isinstance(entries, list)
    entries.append(copy.deepcopy(entries[0]))
    unsigned = {key: duplicate[key] for key in ("schema", "catalog_version", "entries")}
    integrity = duplicate["integrity"]
    assert isinstance(integrity, dict)
    integrity["canonical_payload_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
    )
    duplicate_raw = json.dumps(duplicate, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    with pytest.raises(health.GateAbort, match="unique|ambiguous"):
        observer.validate_model_envelope_catalog(
            duplicate,
            duplicate_raw,
            expected_file_sha256=hashlib.sha256(duplicate_raw).hexdigest(),
            candidate_config_digest="sha256:" + "a" * 64,
            candidate_layer_diff_ids=["sha256:" + "b" * 64],
            unloaded_envelope=unloaded,
        )


def test_release_policy_and_unloaded_envelope_are_fully_revalidated() -> None:
    policy = priority_policy_document()
    policy_raw = canonical_json(policy)
    policy_sha = hashlib.sha256(policy_raw).hexdigest()
    observer.validate_priority_policy(
        policy,
        policy_raw,
        expected_file_sha256=policy_sha,
    )

    envelope = unloaded_gpu_envelope_document()
    envelope["model_policy"]["priority_policy_sha256"] = policy_sha
    envelope_raw = canonical_json(envelope)
    envelope_sha = hashlib.sha256(envelope_raw).hexdigest()
    validated = observer.validate_unloaded_gpu_envelope(
        envelope,
        envelope_raw,
        expected_file_sha256=envelope_sha,
        expected_policy_sha256=policy_sha,
        expected_runtime_commit="f" * 40,
        expected_oci_index="sha256:" + "1" * 64,
        expected_config_digest="sha256:" + "a" * 64,
        expected_layer_diff_ids=["sha256:" + "b" * 64],
    )
    assert validated["measurement"]["allowed_unloaded_bytes"] == 134_217_728

    for mutation in (
        {**policy, "source_max_age_seconds": True},
        {**policy, "detection_fps_limit": 80},
        {**policy, "unexpected": "private-value"},
    ):
        raw = canonical_json(mutation)
        with pytest.raises(health.GateAbort):
            observer.validate_priority_policy(
                mutation,
                raw,
                expected_file_sha256=hashlib.sha256(raw).hexdigest(),
            )

    broken = copy.deepcopy(envelope)
    broken["measurement"]["allowed_unloaded_bytes"] += 1
    broken_raw = canonical_json(broken)
    with pytest.raises(health.GateAbort, match="allowed|arithmetic"):
        observer.validate_unloaded_gpu_envelope(
            broken,
            broken_raw,
            expected_file_sha256=hashlib.sha256(broken_raw).hexdigest(),
            expected_policy_sha256=policy_sha,
            expected_runtime_commit="f" * 40,
            expected_oci_index="sha256:" + "1" * 64,
            expected_config_digest="sha256:" + "a" * 64,
            expected_layer_diff_ids=["sha256:" + "b" * 64],
        )

    with pytest.raises(health.GateAbort, match="canonical"):
        observer.validate_priority_policy(
            policy,
            json.dumps(policy, indent=2).encode("ascii") + b"\n",
            expected_file_sha256=hashlib.sha256(policy_raw).hexdigest(),
        )


def test_execution_boundary_reconstructs_preimages_and_gate_identity() -> None:
    identity = {
        "container_id": "1" * 64,
        "runtime_commit": "2" * 40,
        "oci_index": "sha256:" + "3" * 64,
        "config_digest": "sha256:" + "4" * 64,
        "layer_diff_ids": ["sha256:" + "5" * 64],
        "selected_model": "medium",
        "model_revision": "hf:" + "6" * 40,
    }
    token = "7" * 32
    token_sha = hashlib.sha256(token.encode("ascii")).hexdigest()
    phase_a = {"workload_sha256": "8" * 64}
    phase_b = {"workload_sha256": "9" * 64}
    environment_values = {
        "AUTO_DELETE_FAILED_FILES": "false",
        "AUTO_DELETE_INVALID_MEDIA": "false",
        "MEMORY_PRESSURE_YIELD": "True",
        "SHOW_IN_SUBNAME_MODEL": "false",
        "SHOW_IN_SUBNAME_SUBGEN": "false",
        "SUBTITLE_LANGUAGE_NAME": "en",
        "COMPUTE_TYPE": "float16",
        "CONCURRENT_TRANSCRIPTIONS": "1",
        "MODEL_ENVELOPE_CATALOG": "/opt/subgen/model-envelopes/catalog.json",
        "MODEL_ENVELOPE_IDENTITY": "/opt/subgen/model-envelopes/image-identity.json",
        "PRIORITY_PRESSURE_FILE": "/run/subgen-priority/pressure.json",
        "TASK11B_GATE_RECEIPT_FILE": "/run/subgen-task11b/runtime-receipts.jsonl",
        "TASK11B_GATE_TOKEN_SHA256": token_sha,
        "TASK11B_PHASE_A_WORKLOAD_SHA256": phase_a["workload_sha256"],
        "TASK11B_PHASE_B_WORKLOAD_SHA256": phase_b["workload_sha256"],
        "TRANSCRIBE_DEVICE": "cuda",
        "WHISPER_MODEL": "auto",
    }
    environment = [
        f"{key}={environment_values[key]}" for key in sorted(environment_values)
    ]
    ownership_labels = {
        "io.github.herbertmt978.subgen.task11b-gate": "true",
        "io.github.herbertmt978.subgen.gate-token": token,
        "io.github.herbertmt978.subgen.gate-role": "runtime-auto",
        "io.github.herbertmt978.subgen.runtime-commit": identity["runtime_commit"],
    }
    config = {
        "Env": environment,
        "User": "1000:1000",
        "WorkingDir": "/subgen",
        "Entrypoint": ["/usr/bin/python3"],
        "Cmd": ["/subgen/launcher.py"],
        "Labels": ownership_labels,
    }
    host = {
        "Privileged": False,
        "ReadonlyRootfs": True,
        "CapDrop": ["ALL"],
        "NetworkMode": "bridge",
    }
    host_config = {**host, "LogConfig": {"Type": "local", "Config": {}}}
    networks = {"bridge": {"network_id": "a" * 64}}
    mount_policy = {
        "/subgen/models": True,
        "/opt/subgen/monitor": True,
        "/opt/subgen/model-envelopes": False,
        "/fixtures/phase-a": False,
        "/fixtures/phase-b": False,
        "/run/subgen-priority": False,
        "/run/subgen-task11b": True,
        "/task11b-output/phase-a": True,
        "/task11b-output/phase-b": True,
    }
    mounts = [
        {
            "type": "bind",
            "source": f"/private{destination}",
            "destination": destination,
            "mode": "rw" if writable else "ro",
            "read_write": writable,
            "propagation": "rprivate",
        }
        for destination, writable in sorted(mount_policy.items())
    ]

    def compact_sha(value: object) -> str:
        return hashlib.sha256(canonical_json(value)[:-1]).hexdigest()

    command_sha = hashlib.sha256(
        json.dumps([config["Entrypoint"], config["Cmd"]], separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()
    daemon_identity = health.docker_daemon_identity_document("6" * 64, "7" * 64)
    candidate_record = {
        "candidate_identity": identity,
        "docker_daemon_identity_sha256": health.docker_daemon_identity_sha256(
            daemon_identity
        ),
        "gate_token_sha256": token_sha,
        "intended_command_sha256": command_sha,
    }
    catalog_sha = "b" * 64
    boundary = {
        "schema": 4,
        "model_envelope_catalog_sha256": catalog_sha,
        "phase_a_fixture_record_sha256": "c" * 64,
        "phase_b_fixture_record_sha256": "d" * 64,
        "candidate_identity": identity,
        "docker_daemon_identity": daemon_identity,
        "ownership_labels": ownership_labels,
        "environment": environment,
        "environment_sha256": compact_sha(environment),
        "config": config,
        "config_sha256": compact_sha(config),
        "host_config": host_config,
        "host_config_sha256": compact_sha(host_config),
        "network_attachments": networks,
        "network_attachments_sha256": compact_sha(networks),
        "entrypoint_command_sha256": command_sha,
        "user": "1000:1000",
        "working_directory": "/subgen",
        "host": host,
        "mounts": mounts,
    }
    observer.validate_execution_boundary_document(
        boundary,
        candidate_record=candidate_record,
        phase_a=phase_a,
        phase_b=phase_b,
        expected_catalog_sha256=catalog_sha,
    )

    for memory_yield in (None, "False", "true"):
        changed_values = dict(environment_values)
        if memory_yield is None:
            del changed_values["MEMORY_PRESSURE_YIELD"]
        else:
            changed_values["MEMORY_PRESSURE_YIELD"] = memory_yield
        changed_environment = [
            f"{key}={changed_values[key]}" for key in sorted(changed_values)
        ]
        changed_config = {**config, "Env": changed_environment}
        changed_boundary = {
            **boundary,
            "environment": changed_environment,
            "environment_sha256": compact_sha(changed_environment),
            "config": changed_config,
            "config_sha256": compact_sha(changed_config),
        }
        with pytest.raises(health.GateAbort, match="runtime_environment"):
            observer.validate_execution_boundary_document(
                changed_boundary,
                candidate_record=candidate_record,
                phase_a=phase_a,
                phase_b=phase_b,
                expected_catalog_sha256=catalog_sha,
            )

    for mutation in (
        {**boundary, "schema": 3},
        {**boundary, "candidate_identity": {**identity, "container_id": "c" * 64}},
        {
            **boundary,
            "docker_daemon_identity": {
                **daemon_identity,
                "engine_id_sha256": "8" * 64,
            },
        },
        {**boundary, "environment_sha256": "d" * 64},
        {
            **boundary,
            "phase_b_fixture_record_sha256": boundary["phase_a_fixture_record_sha256"],
        },
    ):
        with pytest.raises(health.GateAbort):
            observer.validate_execution_boundary_document(
                mutation,
                candidate_record=candidate_record,
                phase_a=phase_a,
                phase_b=phase_b,
                expected_catalog_sha256=catalog_sha,
            )


def test_sampler_binding_is_unique_canonical_and_catalog_bound() -> None:
    binding = sampler_binding_document()
    prefix = "Task-11B-Sampler-Binding: "
    line = prefix.encode("ascii") + canonical_json(binding)
    parsed = observer.parse_sampler_binding(b"header\n" + line, prefix)
    assert parsed == binding
    assert parsed["model_envelope_catalog_sha256"] == "e" * 64

    with pytest.raises(health.GateAbort, match="duplicated"):
        observer.parse_sampler_binding(line + line, prefix)
    with pytest.raises(health.GateAbort, match="canonical"):
        observer.parse_sampler_binding(
            prefix.encode("ascii")
            + json.dumps(binding, sort_keys=False).encode("ascii")
            + b"\n",
            prefix,
        )
    duplicate_key = (
        prefix.encode("ascii")
        + canonical_json(binding)[:-2]
        + b',"schema":"subgen.task11b.sampler-binding/v1"}\n'
    )
    with pytest.raises(health.GateAbort, match="malformed|duplicate"):
        observer.parse_sampler_binding(duplicate_key, prefix)


def test_release_cross_binding_rejects_catalog_and_identity_hash_swaps() -> None:
    identity = {
        "container_id": "1" * 64,
        "runtime_commit": "2" * 40,
        "oci_index": "sha256:" + "3" * 64,
        "config_digest": "sha256:" + "4" * 64,
        "layer_diff_ids": ["sha256:" + "5" * 64],
        "selected_model": "medium",
        "model_revision": "hf:" + "6" * 40,
    }
    identity_sha = hashlib.sha256(canonical_json(identity)).hexdigest()
    container_sha = hashlib.sha256(identity["container_id"].encode("ascii")).hexdigest()
    layers_sha = hashlib.sha256(canonical_json(identity["layer_diff_ids"])).hexdigest()
    daemon_identity = health.docker_daemon_identity_document("6" * 64, "7" * 64)
    daemon_identity_sha = health.docker_daemon_identity_sha256(daemon_identity)
    hashes = {
        name: character * 64
        for name, character in (
            ("final", "a"),
            ("phase_a", "b"),
            ("phase_b", "c"),
            ("candidate", "d"),
            ("boundary", "e"),
            ("policy", "f"),
            ("envelope", "0"),
            ("catalog", "1"),
            ("assertion", "2"),
            ("phase_a_trace", "3"),
            ("phase_b_trace", "4"),
            ("output", "5"),
            ("observer", "6"),
            ("sampler", "7"),
            ("producer", "8"),
            ("sampler_test", "9"),
            ("observer_test", "a"),
            ("profiler_chain", "b"),
        )
    }
    binding = sampler_binding_document()
    binding.update(
        {
            "gate_seal_sha256": hashes["final"],
            "observer_sha256": hashes["observer"],
            "sampler_sha256": hashes["sampler"],
            "test_sha256": hashes["sampler_test"],
            "observer_test_sha256": hashes["observer_test"],
            "producer_sha256": hashes["producer"],
            "policy_sha256": hashes["policy"],
            "unloaded_gpu_envelope_sha256": hashes["envelope"],
            "model_envelope_catalog_sha256": hashes["catalog"],
            "execution_boundary_manifest_sha256": hashes["boundary"],
        }
    )
    candidate_record = {
        "candidate_identity": identity,
        "docker_daemon_identity_sha256": daemon_identity_sha,
        "execution_boundary_manifest_sha256": hashes["boundary"],
    }
    phase_a = {
        "candidate_identity_sha256": identity_sha,
        "execution_boundary_manifest_sha256": hashes["boundary"],
        "policy_sha256": hashes["policy"],
        "unloaded_gpu_envelope_sha256": hashes["envelope"],
        "assertion_observation_sha256": hashes["assertion"],
        "gate_receipt_trace_sha256": hashes["phase_a_trace"],
        "final_output_sha256": hashes["output"],
        "runtime_epoch": "9" * 32,
        "runtime_started_monotonic_ns": 1,
        "workload_sha256": "a" * 64,
        "events": [{}, {}, {}, {}, {}, {}, {}, {}, {}, {"monotonic_ns": 10}],
        "sealed_monotonic_ns": 11,
    }
    phase_b = {
        "phase_a_seal_sha256": hashes["phase_a"],
        "candidate_identity_sha256": identity_sha,
        "candidate_identity": identity,
        "execution_boundary_manifest_sha256": hashes["boundary"],
        "policy_sha256": hashes["policy"],
        "gate_receipt_trace_sha256": hashes["phase_b_trace"],
        "model_identity_sha256": "b" * 64,
        "runtime_epoch": "9" * 32,
        "runtime_started_monotonic_ns": 1,
        "workload_sha256": "c" * 64,
        "phase_a_durable_monotonic_ns": 12,
        "reset_completed_monotonic_ns": 13,
        "started_monotonic_ns": 14,
    }
    final = {
        "runtime_commit": identity["runtime_commit"],
        "candidate_oci_index": identity["oci_index"],
        "candidate_config_digest": identity["config_digest"],
        "container_id_sha256": container_sha,
        "candidate_identity_record_sha256": hashes["candidate"],
        "docker_daemon_identity_sha256": daemon_identity_sha,
        "layer_diff_ids_sha256": layers_sha,
        "model_envelope_catalog_sha256": hashes["catalog"],
        "sampler_sha256": hashes["sampler"],
        "sampler_test_sha256": hashes["sampler_test"],
        "observer_sha256": hashes["observer"],
        "observer_test_sha256": hashes["observer_test"],
        "producer_sha256": hashes["producer"],
        "policy_sha256": hashes["policy"],
        "unloaded_gpu_envelope_sha256": hashes["envelope"],
        "execution_boundary_manifest_sha256": hashes["boundary"],
        "phase_a_seal_sha256": hashes["phase_a"],
        "phase_b_seal_sha256": hashes["phase_b"],
        "profiler_chain_sha256": hashes["profiler_chain"],
    }
    catalog = {
        "catalog_sha256": hashes["catalog"],
        "model_identity_sha256": "b" * 64,
        "selected_model": identity["selected_model"],
        "model_revision": identity["model_revision"],
    }
    observer.verify_release_cross_bindings(
        binding=binding,
        final=final,
        phase_a=phase_a,
        phase_b=phase_b,
        candidate_record=candidate_record,
        boundary={
            "candidate_identity": identity,
            "docker_daemon_identity": daemon_identity,
        },
        catalog_attestation=catalog,
        hashes=hashes,
        expected_runtime_commit=identity["runtime_commit"],
        expected_oci_index=identity["oci_index"],
        expected_config_digest=identity["config_digest"],
    )

    changed_final = {**final, "profiler_chain_sha256": "0" * 64}
    with pytest.raises(health.GateAbort, match="subordinate_hash"):
        observer.verify_release_cross_bindings(
            binding=binding,
            final=changed_final,
            phase_a=phase_a,
            phase_b=phase_b,
            candidate_record=candidate_record,
            boundary={
                "candidate_identity": identity,
                "docker_daemon_identity": daemon_identity,
            },
            catalog_attestation=catalog,
            hashes=hashes,
            expected_runtime_commit=identity["runtime_commit"],
            expected_oci_index=identity["oci_index"],
            expected_config_digest=identity["config_digest"],
        )

    for target, value in (
        ("catalog", "f" * 64),
        ("model_identity", "e" * 64),
        (
            "candidate_record",
            {
                "candidate_identity": {**identity, "container_id": "f" * 64},
                "docker_daemon_identity_sha256": daemon_identity_sha,
                "execution_boundary_manifest_sha256": hashes["boundary"],
            },
        ),
    ):
        changed_catalog = copy.deepcopy(catalog)
        changed_record = copy.deepcopy(candidate_record)
        if target == "catalog":
            changed_catalog["catalog_sha256"] = value
        elif target == "model_identity":
            changed_catalog["model_identity_sha256"] = value
        else:
            changed_record = value
        with pytest.raises(health.GateAbort):
            observer.verify_release_cross_bindings(
                binding=binding,
                final=final,
                phase_a=phase_a,
                phase_b=phase_b,
                candidate_record=changed_record,
                boundary={
                    "candidate_identity": identity,
                    "docker_daemon_identity": daemon_identity,
                },
                catalog_attestation=changed_catalog,
                hashes=hashes,
                expected_runtime_commit=identity["runtime_commit"],
                expected_oci_index=identity["oci_index"],
                expected_config_digest=identity["config_digest"],
            )


def profiler_release_bundle(
    *,
    model: str = "medium",
    returncode: int = 0,
    next_model: str | None = None,
    boundary_canonical_sha256: str = "a" * 64,
    boundary_file_sha256: str = "9" * 64,
    priority_sequence: int = 1,
    priority_source_generation: int = 1,
) -> tuple[bytes, dict[str, object]]:
    priority = {
        "policy_sha256": "f" * 64,
        "signal_payload_sha256": "1" * 64,
        "producer_epoch_sha256": "2" * 64,
        "boot_id_sha256": "3" * 64,
        "observation_id_sha256": "4" * 64,
        "sequence": priority_sequence,
        "source_generation": priority_source_generation,
        "pressure": False,
        "clear_eligible": True,
        "reason_codes": [],
    }
    start = {
        "event": "profiler_observer_start",
        "timestamp": "2026-09-02T00:00:00Z",
        "observer_sha256": "6" * 64,
        "sampler_sha256": "7" * 64,
        "priority_preflight": priority,
        "profiler_binding": {
            "schema": "subgen.task11b.profiler-binding/v1",
            "candidate_config_digest": "sha256:" + "5" * 64,
            "layer_diff_ids_sha256": "8" * 64,
            "model": model,
            "model_revision": "hf:" + "4" * 40,
            "expected_returncode": returncode,
        },
    }
    gate_start = {
        "event": "gate_start",
        "timestamp": "2026-09-02T00:00:01Z",
        "candidate_mode": "profiler",
        "candidate_image_config": "sha256:" + "3" * 64,
        "candidate_command_sha256": "8" * 64,
        "candidate_boundary_sha256": boundary_canonical_sha256,
        "boundary_manifest_sha256": boundary_file_sha256,
        "runtime_commit": "1" * 40,
        "gate_role": f"profile-{model}",
        "sampler_sha256": "7" * 64,
        "docker_daemon_id_sha256": "0" * 64,
        "host_boot_id_sha256": "3" * 64,
        "duration_seconds": 900,
        "interval_seconds": 5,
        "expected_memory_bytes": 12 * health.GIB,
        "gpu_free_floor_bytes": 8 * health.GIB,
        "host_reserve_bytes": 4 * health.GIB,
        "candidate_initial_state": {
            "status": "running",
            "running": True,
            "oom_killed": False,
            "restart_count": 0,
            "health": "healthy",
        },
        "frigate_initial_state": {
            "status": "running",
            "running": True,
            "oom_killed": False,
            "restart_count": 0,
            "health": "healthy",
        },
        "priority_revalidation": priority,
    }
    samples = [
        {
            "event": "sample",
            "sample": index + 1,
            "elapsed_seconds": float(index * 5),
            "candidate": {
                "status": "running",
                "running": True,
                "oom_killed": False,
                "restart_count": 0,
                "health": "healthy",
            },
            "candidate_memory": {
                "memory.current": 2 * health.GIB,
                "memory.peak": 3 * health.GIB,
                "memory.max": 12 * health.GIB,
                "memory.swap.current": 0,
                "memory.swap.max": 0,
                "events": {key: 0 for key in health.REQUIRED_MEMORY_EVENTS},
                "pressure_observed_only": {},
            },
            "candidate_resource": {
                "mode": "profiler",
                "external_result_validation_required": True,
            },
            "candidate_status_fresh": True,
            "frigate": {
                "status": "running",
                "running": True,
                "oom_killed": False,
                "restart_count": 0,
                "health": "healthy",
            },
            "frigate_metrics": {
                "camera_count": 15,
                "camera_low_count": 0,
                "camera_min_process_ratio": 1.0,
                "camera_max_skipped_fps": 0.0,
                "camera_longest_low_seconds": 0.0,
                "detector_count": 2,
                "detector_inference_ms_min": 10.0,
                "detector_inference_ms_max": 12.0,
                "embedding_metric_count": 4,
                "embedding_conditional_idle_count": 0,
                "embedding_speed_min": 1.0,
                "embedding_speed_max": 2.0,
                "service_age_seconds": 1.0,
            },
            "gpu": {
                "total_mib": 24576,
                "used_mib": 6456,
                "free_mib": 18120,
                "utilization_percent": 20,
                "compute_process_count": 2,
                "compute_process_used_mib": 6456,
            },
            "host_mem_available_bytes": 8 * health.GIB,
            "ollama_loaded_models": 0,
            "priority_revalidation": priority,
        }
        for index in range(181)
    ]
    release = {
        "hold_pid_count": 1,
        "candidate_gpu_bytes": 0,
        "validated_monotonic_ns": 100,
        "pid_set_sha256": "a" * 64,
        "gpu_uuid_sha256": "b" * 64,
    }
    candidate_resource: dict[str, object]
    if returncode == 0:
        candidate_resource = {
            "status": "profiled",
            "model": model,
            "returncode": 0,
            "replaced_existing": False,
            "stdout_sha256": "c" * 64,
            "receipt_sha256": "d" * 64,
            "catalog_version": 1,
            "entry_count": 5,
            "matching_model_entry_count": 1,
            "catalog_sha256": "e" * 64,
            "canonical_payload_sha256": "sha256:" + "0" * 64,
            "release": release,
        }
    else:
        candidate_resource = {
            "status": "safe_failure",
            "model": model,
            "next_model": next_model,
            "returncode": 3,
            "reason_sha256": hashlib.sha256(
                sorted(health.SAFE_PROFILER_FAILURE_REASONS)[0].encode("utf-8")
            ).hexdigest(),
            "stdout_sha256": "c" * 64,
            "receipt_sha256": "d" * 64,
            "release": release,
        }
    final = {
        "event": "gate_observation_final",
        "timestamp": "2026-09-02T00:15:01Z",
        "candidate_resource": candidate_resource,
        "priority_revalidation": priority,
    }
    cleanup = {
        "verified_stopped": True,
        "already_absent": False,
        "profiler_release_attested": True,
        "priority_revalidation": priority,
        "completion": {
            "candidate": {"running": False, "status": "exited"},
            "frigate": {"running": True, "health": "healthy"},
            "logs_drained_through_wall": 100.0,
        },
    }
    records: list[dict[str, object]] = [
        start,
        gate_start,
        *samples,
        final,
        {
            "event": "profiler_observer_cleanup",
            "timestamp": "2026-09-02T00:15:02Z",
            "cleanup": cleanup,
        },
    ]
    prefix = b"".join(canonical_json(record) for record in records)
    records.append(
        {
            "event": "evidence_seal_record",
            "timestamp": "2026-09-02T00:15:03Z",
            "outcome": "pass",
            "prefix_sha256": hashlib.sha256(prefix).hexdigest(),
            "records_before_seal": len(records),
        }
    )
    payload = b"".join(canonical_json(record) for record in records)
    seal: dict[str, object] = {
        "schema": 1,
        "outcome": "pass",
        "evidence_sha256": hashlib.sha256(payload).hexdigest(),
        "evidence_bytes": len(payload),
        "record_count": len(records),
        "sampler_sha256": "7" * 64,
        "candidate_image_config": "sha256:" + "3" * 64,
        "cleanup": cleanup,
    }
    return payload, seal


def test_profiler_release_bundle_is_independently_validated_and_cross_bound() -> None:
    payload, seal = profiler_release_bundle()
    attestation = observer.validate_profiler_release_bundle(
        payload,
        seal,
        expected_evidence_sha256=hashlib.sha256(payload).hexdigest(),
        expected_sampler_sha256="7" * 64,
        expected_observer_sha256="6" * 64,
        expected_runtime_commit="1" * 40,
        expected_oci_index="sha256:" + "3" * 64,
        expected_boundary_sha256="a" * 64,
        expected_boundary_file_sha256="9" * 64,
        expected_command_sha256="8" * 64,
        expected_policy_sha256="f" * 64,
        expected_model="medium",
        expected_catalog_sha256="e" * 64,
        expected_gpu_uuid_sha256="b" * 64,
    )
    assert attestation == {
        "evidence_sha256": hashlib.sha256(payload).hexdigest(),
        "record_count": 186,
        "sample_count": 181,
        "model": "medium",
        "model_revision": "hf:" + "4" * 40,
        "returncode": 0,
        "status": "profiled",
        "next_model": None,
        "reason_sha256": None,
        "catalog_sha256": "e" * 64,
        "priority_first_sequence": 1,
        "priority_last_sequence": 1,
        "priority_first_source_generation": 1,
        "priority_last_source_generation": 1,
    }

    for target in (
        "catalog",
        "released_gpu",
        "sampler",
        "prefix",
        "boundary_canonical",
        "boundary_file",
    ):
        changed_payload, changed_seal = profiler_release_bundle()
        if target in {
            "catalog",
            "released_gpu",
            "prefix",
            "boundary_canonical",
            "boundary_file",
        }:
            lines = changed_payload.splitlines(keepends=True)
            records = [json.loads(line) for line in lines]
            if target == "catalog":
                records[-3]["candidate_resource"]["catalog_sha256"] = "0" * 64
            elif target == "released_gpu":
                records[-3]["candidate_resource"]["release"]["candidate_gpu_bytes"] = 1
            elif target == "prefix":
                records[-1]["prefix_sha256"] = "0" * 64
            elif target == "boundary_canonical":
                records[1]["candidate_boundary_sha256"] = "0" * 64
            else:
                records[1]["boundary_manifest_sha256"] = "0" * 64
            changed_payload = b"".join(canonical_json(record) for record in records)
            changed_seal["evidence_sha256"] = hashlib.sha256(
                changed_payload
            ).hexdigest()
            changed_seal["evidence_bytes"] = len(changed_payload)
        else:
            changed_seal["sampler_sha256"] = "0" * 64
        with pytest.raises(health.GateAbort):
            observer.validate_profiler_release_bundle(
                changed_payload,
                changed_seal,
                expected_evidence_sha256=hashlib.sha256(changed_payload).hexdigest(),
                expected_sampler_sha256="7" * 64,
                expected_observer_sha256="6" * 64,
                expected_runtime_commit="1" * 40,
                expected_oci_index="sha256:" + "3" * 64,
                expected_boundary_sha256="a" * 64,
                expected_boundary_file_sha256="9" * 64,
                expected_command_sha256="8" * 64,
                expected_policy_sha256="f" * 64,
                expected_model="medium",
                expected_catalog_sha256="e" * 64,
                expected_gpu_uuid_sha256="b" * 64,
            )


def test_profiler_chain_requires_every_higher_model_failure_before_success() -> None:
    large_payload, large_seal = profiler_release_bundle(
        model="large-v3",
        returncode=3,
        next_model="medium",
        boundary_file_sha256="5" * 64,
        priority_source_generation=2,
    )
    medium_payload, medium_seal = profiler_release_bundle(
        boundary_canonical_sha256="b" * 64,
        boundary_file_sha256="6" * 64,
        priority_sequence=2,
        priority_source_generation=2,
    )
    large_boundary = object()
    medium_boundary = object()
    bundles = [
        (large_payload, large_seal, "1" * 64, large_boundary),
        (medium_payload, medium_seal, "2" * 64, medium_boundary),
    ]
    expected = {
        "expected_sampler_sha256": "7" * 64,
        "expected_observer_sha256": "6" * 64,
        "expected_runtime_commit": "1" * 40,
        "expected_oci_index": "sha256:" + "3" * 64,
        "expected_candidate_config_digest": "sha256:" + "5" * 64,
        "expected_layer_diff_ids_sha256": "8" * 64,
        "expected_boundary_sha256": "9" * 64,
        "expected_policy_sha256": "f" * 64,
        "expected_producer_epoch_sha256": "2" * 64,
        "expected_selected_model": "medium",
        "expected_selected_model_revision": "hf:" + "4" * 40,
        "expected_catalog_sha256": "e" * 64,
        "expected_gpu_uuid_sha256": "b" * 64,
        "expected_docker_engine_sha256": "0" * 64,
        "expected_host_boot_sha256": "3" * 64,
    }
    boundaries = {
        large_boundary: {
            "model": "large-v3",
            "file_sha256": "5" * 64,
            "canonical_sha256": "a" * 64,
        },
        medium_boundary: {
            "model": "medium",
            "file_sha256": "6" * 64,
            "canonical_sha256": "b" * 64,
        },
    }

    def validate_boundary(boundary: object, *, expected_model: str, **_: object):
        attestation = boundaries[boundary]
        if attestation["model"] != expected_model:
            raise health.GateAbort("profiler boundary model did not match")
        return {
            "file_sha256": attestation["file_sha256"],
            "canonical_sha256": attestation["canonical_sha256"],
            "command_sha256": "8" * 64,
            "model_revision": "hf:" + "4" * 40,
        }

    with mock.patch.object(
        observer,
        "_validate_profiler_boundary_expectation",
        side_effect=validate_boundary,
    ):
        chain = observer.validate_profiler_release_chain(bundles, **expected)
    assert chain["document"]["selected_model"] == "medium"
    assert [attempt["model"] for attempt in chain["document"]["attempts"]] == [
        "large-v3",
        "medium",
    ]
    assert chain["document"]["attempts"][0]["returncode"] == 3
    assert chain["document"]["attempts"][1]["returncode"] == 0
    assert chain["document"]["attempts"][0]["priority_last_sequence"] == 1
    assert chain["document"]["attempts"][1]["priority_first_sequence"] == 2
    assert chain["document"]["attempts"][0]["boundary_file_sha256"] == "5" * 64
    assert chain["document"]["attempts"][1]["boundary_canonical_sha256"] == "b" * 64
    assert len(chain["chain_sha256"]) == 64

    with mock.patch.object(
        observer,
        "_validate_profiler_boundary_expectation",
        side_effect=validate_boundary,
    ):
        for invalid in ([bundles[1]], list(reversed(bundles))):
            with pytest.raises(health.GateAbort):
                observer.validate_profiler_release_chain(invalid, **expected)

    rollback_payloads = (
        profiler_release_bundle(
            boundary_canonical_sha256="b" * 64,
            boundary_file_sha256="6" * 64,
            priority_sequence=1,
            priority_source_generation=2,
        ),
        profiler_release_bundle(
            boundary_canonical_sha256="b" * 64,
            boundary_file_sha256="6" * 64,
            priority_sequence=3,
            priority_source_generation=1,
        ),
    )
    with mock.patch.object(
        observer,
        "_validate_profiler_boundary_expectation",
        side_effect=validate_boundary,
    ):
        for index, (rollback_payload, rollback_seal) in enumerate(rollback_payloads):
            rollback_bundles = [
                bundles[0],
                (rollback_payload, rollback_seal, str(index + 3) * 64, medium_boundary),
            ]
            with pytest.raises(health.GateAbort, match="priority_handoff"):
                observer.validate_profiler_release_chain(rollback_bundles, **expected)


def test_profiler_release_bundle_accepts_priority_gaps_and_rejects_rollback() -> None:
    payload, seal = profiler_release_bundle()
    records = [json.loads(line) for line in payload.splitlines()]
    for record in records[1:-1]:
        attestations: list[dict[str, object]] = []
        if isinstance(record.get("priority_revalidation"), dict):
            attestations.append(record["priority_revalidation"])
        cleanup = record.get("cleanup")
        if isinstance(cleanup, dict) and isinstance(
            cleanup.get("priority_revalidation"), dict
        ):
            attestations.append(cleanup["priority_revalidation"])
        for attestation in attestations:
            attestation["sequence"] = 3
            attestation["source_generation"] = 3

    def validate(changed_records: list[dict[str, object]]) -> dict[str, object]:
        prefix = b"".join(canonical_json(record) for record in changed_records[:-1])
        changed_records[-1]["prefix_sha256"] = hashlib.sha256(prefix).hexdigest()
        changed_payload = b"".join(canonical_json(record) for record in changed_records)
        changed_seal = {
            **seal,
            "evidence_sha256": hashlib.sha256(changed_payload).hexdigest(),
            "evidence_bytes": len(changed_payload),
            "cleanup": copy.deepcopy(changed_records[-2]["cleanup"]),
        }
        return observer.validate_profiler_release_bundle(
            changed_payload,
            changed_seal,
            expected_evidence_sha256=hashlib.sha256(changed_payload).hexdigest(),
            expected_sampler_sha256="7" * 64,
            expected_observer_sha256="6" * 64,
            expected_runtime_commit="1" * 40,
            expected_oci_index="sha256:" + "3" * 64,
            expected_boundary_sha256="a" * 64,
            expected_boundary_file_sha256="9" * 64,
            expected_command_sha256="8" * 64,
            expected_policy_sha256="f" * 64,
            expected_model="medium",
            expected_catalog_sha256="e" * 64,
            expected_gpu_uuid_sha256="b" * 64,
        )

    assert validate(records)["status"] == "profiled"
    records[0]["priority_preflight"]["source_generation"] = 4
    with pytest.raises(health.GateAbort, match="source_generation"):
        validate(records)


def test_verify_release_cli_requires_catalog_and_candidate_identity_record(
    tmp_path: Path,
) -> None:
    paths = {
        name: str((tmp_path / name).resolve())
        for name in (
            "evidence",
            "gate",
            "phase-a",
            "output",
            "phase-b",
            "assertion",
            "trace-a",
            "trace-b",
            "candidate",
            "boundary",
            "policy",
            "envelope",
            "catalog",
            "producer",
            "sampler",
            "sampler-test",
            "observer-test",
            "profiler-evidence",
            "profiler-seal",
            "profiler-boundary",
        )
    }
    arguments = [
        "--evidence",
        paths["evidence"],
        "--binding-prefix",
        "Task-11B-Sampler-Binding: ",
        "--gate-seal",
        paths["gate"],
        "--phase-a-seal",
        paths["phase-a"],
        "--phase-a-output",
        paths["output"],
        "--phase-b-seal",
        paths["phase-b"],
        "--assertion-observation",
        paths["assertion"],
        "--phase-a-receipt-trace",
        paths["trace-a"],
        "--phase-b-receipt-trace",
        paths["trace-b"],
        "--candidate-identity-record",
        paths["candidate"],
        "--execution-boundary-manifest",
        paths["boundary"],
        "--priority-policy",
        paths["policy"],
        "--unloaded-gpu-envelope",
        paths["envelope"],
        "--model-envelope-catalog",
        paths["catalog"],
        "--producer-source",
        paths["producer"],
        "--sampler-source",
        paths["sampler"],
        "--sampler-test-source",
        paths["sampler-test"],
        "--observer-test-source",
        paths["observer-test"],
        "--profiler-evidence",
        paths["profiler-evidence"],
        "--profiler-evidence-seal",
        paths["profiler-seal"],
        "--profiler-boundary-manifest",
        paths["profiler-boundary"],
        "--runtime-commit",
        "1" * 40,
        "--candidate-oci-index",
        "sha256:" + "2" * 64,
        "--candidate-config-digest",
        "sha256:" + "3" * 64,
    ]
    parsed = observer.release_parser().parse_args(arguments)
    assert parsed.candidate_identity_record == Path(paths["candidate"])
    assert parsed.model_envelope_catalog == Path(paths["catalog"])
    assert parsed.profiler_evidence == [Path(paths["profiler-evidence"])]
    assert parsed.profiler_evidence_seal == [Path(paths["profiler-seal"])]
    assert parsed.profiler_boundary_manifest == [Path(paths["profiler-boundary"])]

    for required in (
        "--candidate-identity-record",
        "--model-envelope-catalog",
        "--sampler-test-source",
        "--observer-test-source",
        "--profiler-evidence",
        "--profiler-evidence-seal",
        "--profiler-boundary-manifest",
    ):
        index = arguments.index(required)
        missing = arguments[:index] + arguments[index + 2 :]
        with pytest.raises(SystemExit):
            observer.release_parser().parse_args(missing)


def test_phase_a_events_are_bound_to_every_intervening_runtime_receipt() -> None:
    workload = "4" * 64
    model_identity = "9" * 64
    assertion_digest = hashlib.sha256(("ab" * 32).encode("ascii")).hexdigest()

    def make(
        sequence: int,
        *,
        state: str,
        phase: str,
        clear_count: int,
        active: bool,
        uncommitted: bool,
        resident: bool,
        load: int,
        unload: int,
        observation: str,
        transition: str,
        transition_sequence: int,
        completed: bool = False,
    ) -> dict[str, object]:
        receipt = runtime_receipt(
            sequence,
            observed_monotonic_ns=sequence * 1_000_000_000,
            workload_sha256=workload if sequence > 1 else None,
            priority_state=state,
            controller_phase=phase,
            recovery_reason=None if phase == "normal" else "priority_pressure",
            model_resident=resident,
        )
        receipt.update(
            {
                "source_generation": sequence + 10,
                "observation_digest": observation,
                "transition_observation_digest": transition,
                "transition_sequence": transition_sequence,
                "distinct_clear_count": clear_count,
                "model_load_generation": load,
                "model_unload_generation": unload,
                "active": active,
                "chunk_uncommitted": uncommitted,
                "active_cursor_ms": 0 if active else None,
                "completed_cursor_ms": 10_000 if completed else None,
                "completion_generation": 1 if completed else 0,
                "model_identity_sha256": model_identity if resident else None,
            }
        )
        return receipt

    receipts = [
        make(
            1,
            state="neutral",
            phase="normal",
            clear_count=0,
            active=False,
            uncommitted=False,
            resident=True,
            load=1,
            unload=0,
            observation="1" * 64,
            transition="1" * 64,
            transition_sequence=3,
        ),
        make(
            2,
            state="clear",
            phase="normal",
            clear_count=3,
            active=True,
            uncommitted=True,
            resident=True,
            load=1,
            unload=0,
            observation="2" * 64,
            transition="2" * 64,
            transition_sequence=3,
        ),
        make(
            3,
            state="asserted",
            phase="yielding",
            clear_count=0,
            active=True,
            uncommitted=True,
            resident=True,
            load=1,
            unload=0,
            observation=assertion_digest,
            transition=assertion_digest,
            transition_sequence=4,
        ),
        make(
            4,
            state="asserted",
            phase="recovering",
            clear_count=0,
            active=True,
            uncommitted=False,
            resident=True,
            load=1,
            unload=0,
            observation=assertion_digest,
            transition=assertion_digest,
            transition_sequence=4,
        ),
        make(
            5,
            state="asserted",
            phase="recovering",
            clear_count=0,
            active=True,
            uncommitted=False,
            resident=False,
            load=1,
            unload=1,
            observation=assertion_digest,
            transition=assertion_digest,
            transition_sequence=4,
        ),
        make(
            6,
            state="asserted",
            phase="recovering",
            clear_count=0,
            active=True,
            uncommitted=False,
            resident=False,
            load=1,
            unload=1,
            observation=assertion_digest,
            transition=assertion_digest,
            transition_sequence=4,
        ),
        make(
            7,
            state="clear",
            phase="recovering",
            clear_count=1,
            active=True,
            uncommitted=False,
            resident=False,
            load=1,
            unload=1,
            observation="5" * 64,
            transition="5" * 64,
            transition_sequence=5,
        ),
        make(
            8,
            state="clear",
            phase="recovering",
            clear_count=2,
            active=True,
            uncommitted=False,
            resident=False,
            load=1,
            unload=1,
            observation="6" * 64,
            transition="5" * 64,
            transition_sequence=5,
        ),
        make(
            9,
            state="clear",
            phase="normal",
            clear_count=3,
            active=True,
            uncommitted=False,
            resident=False,
            load=1,
            unload=1,
            observation="7" * 64,
            transition="5" * 64,
            transition_sequence=5,
        ),
        make(
            10,
            state="clear",
            phase="normal",
            clear_count=3,
            active=True,
            uncommitted=False,
            resident=True,
            load=2,
            unload=1,
            observation="8" * 64,
            transition="5" * 64,
            transition_sequence=5,
        ),
        make(
            11,
            state="clear",
            phase="normal",
            clear_count=3,
            active=False,
            uncommitted=False,
            resident=True,
            load=2,
            unload=1,
            observation="9" * 64,
            transition="5" * 64,
            transition_sequence=5,
            completed=True,
        ),
    ]
    trace = {
        "schema": "subgen.task11b.runtime-receipt-trace/v1",
        "runtime_epoch": "5" * 32,
        "gate_token_sha256": "6" * 64,
        "workload_sha256": workload,
        "receipts": receipts,
    }
    events = [
        phase_event_from_receipt(index, receipt)
        for index, receipt in enumerate(receipts[1:])
    ]
    events[4]["monotonic_ns"] = 6_500_000_000
    phase_a = {
        "runtime_epoch": "5" * 32,
        "workload_sha256": workload,
        "gate_receipt_trace_sha256": hashlib.sha256(canonical_json(trace)).hexdigest(),
        "assertion_observation_digest": assertion_digest,
        "assertion_observed_monotonic_ns": 2_500_000_000,
        "assertion_reason_codes": ["higher_priority_busy"],
        "allowed_unloaded_bytes": 134_217_728,
        "events": events,
    }
    assertion = {
        "source_generation": receipts[2]["source_generation"],
        "observed_monotonic_ns": 2_500_000_000,
        "reason_codes": ["higher_priority_busy"],
        "observation_digest": assertion_digest,
    }

    observer.verify_phase_a_receipt_bindings(
        phase_a,
        trace,
        assertion_attestation=assertion,
        expected_model_identity_sha256=model_identity,
    )

    stale = copy.deepcopy(phase_a)
    stale["events"][5]["gate_receipt_sha256"] = stale["events"][4][
        "gate_receipt_sha256"
    ]
    with pytest.raises(health.GateAbort, match="receipt"):
        observer.verify_phase_a_receipt_bindings(
            stale,
            trace,
            assertion_attestation=assertion,
            expected_model_identity_sha256=model_identity,
        )

    duplicate_clear = copy.deepcopy(phase_a)
    duplicate_clear["events"][6]["observation_digest"] = duplicate_clear["events"][5][
        "observation_digest"
    ]
    with pytest.raises(health.GateAbort, match="receipt|clear"):
        observer.verify_phase_a_receipt_bindings(
            duplicate_clear,
            trace,
            assertion_attestation=assertion,
            expected_model_identity_sha256=model_identity,
        )

    hidden_transition_trace = copy.deepcopy(trace)
    hidden_reload = copy.deepcopy(receipts[4])
    hidden_reload.update(
        {
            "sequence": 6,
            "observed_monotonic_ns": 5_200_000_000,
            "model_resident": True,
            "model_identity_sha256": model_identity,
            "model_load_generation": 2,
        }
    )
    hidden_restore = copy.deepcopy(receipts[4])
    hidden_restore.update(
        {
            "sequence": 7,
            "observed_monotonic_ns": 5_400_000_000,
        }
    )
    shifted: list[dict[str, object]] = []
    for receipt in receipts[5:]:
        changed = copy.deepcopy(receipt)
        changed["sequence"] = int(changed["sequence"]) + 2
        shifted.append(changed)
    hidden_transition_trace["receipts"] = [
        *copy.deepcopy(receipts[:5]),
        hidden_reload,
        hidden_restore,
        *shifted,
    ]
    hidden_phase = copy.deepcopy(phase_a)
    hidden_phase["gate_receipt_trace_sha256"] = hashlib.sha256(
        canonical_json(hidden_transition_trace)
    ).hexdigest()
    for event in hidden_phase["events"]:
        latest = [
            receipt
            for receipt in hidden_transition_trace["receipts"]
            if receipt["observed_monotonic_ns"] <= event["monotonic_ns"]
        ][-1]
        event["gate_receipt_sha256"] = hashlib.sha256(
            canonical_json(latest)
        ).hexdigest()
    with pytest.raises(health.GateAbort, match="hidden|transition"):
        observer.verify_phase_a_receipt_bindings(
            hidden_phase,
            hidden_transition_trace,
            assertion_attestation=assertion,
            expected_model_identity_sha256=model_identity,
        )


def test_phase_b_samples_bind_latest_receipt_and_require_post_end_sentinel() -> None:
    phase_a_trace_sha = "a" * 64
    phase_a_workload = "4" * 64
    phase_b_workload = "b" * 64
    model_identity = "9" * 64
    started = 13_000_000_000
    ended = started + 900_000_000_000

    idle = runtime_receipt(
        12,
        observed_monotonic_ns=12_000_000_000,
        workload_sha256=phase_a_workload,
        priority_state="clear",
        controller_phase="normal",
        recovery_reason=None,
        model_resident=True,
    )
    idle.update(
        {
            "active": False,
            "chunk_uncommitted": False,
            "active_cursor_ms": None,
            "completed_cursor_ms": 10_000,
            "completion_generation": 1,
            "distinct_clear_count": 3,
            "model_load_generation": 2,
            "model_unload_generation": 1,
        }
    )
    active = copy.deepcopy(idle)
    active.update(
        {
            "sequence": 13,
            "observed_monotonic_ns": started,
            "workload_sha256": phase_b_workload,
            "active": True,
            "active_cursor_ms": 0,
            "completed_cursor_ms": None,
        }
    )
    sentinel = copy.deepcopy(active)
    sentinel.update(
        {
            "sequence": 14,
            "observed_monotonic_ns": ended + 1,
        }
    )
    trace = {
        "schema": "subgen.task11b.phase-b-runtime-receipt-trace/v1",
        "runtime_epoch": "5" * 32,
        "gate_token_sha256": "6" * 64,
        "phase_a_trace_sha256": phase_a_trace_sha,
        "phase_a_last_sequence": 11,
        "workload_sha256": phase_b_workload,
        "receipts": [idle, active, sentinel],
    }
    samples = [
        phase_b_sample_from_receipt(
            index,
            active,
            started_monotonic_ns=started,
        )
        for index in range(181)
    ]
    phase_a = {
        "gate_receipt_trace_sha256": phase_a_trace_sha,
        "workload_sha256": phase_a_workload,
        "runtime_epoch": "5" * 32,
        "runtime_started_monotonic_ns": 1,
        "workload_identity": {"total_duration_ms": 10_000},
        "events": [*({} for _ in range(9)), phase_event_from_receipt(9, idle)],
        "sealed_monotonic_ns": 11_000_000_000,
    }
    phase_b = {
        "runtime_epoch": "5" * 32,
        "runtime_started_monotonic_ns": 1,
        "workload_sha256": phase_b_workload,
        "gate_receipt_trace_sha256": hashlib.sha256(canonical_json(trace)).hexdigest(),
        "model_identity_sha256": model_identity,
        "phase_a_seal_sha256": "c" * 64,
        "phase_a_durable_monotonic_ns": 11_500_000_000,
        "reset_completed_monotonic_ns": 12_000_000_000,
        "started_monotonic_ns": started,
        "ended_monotonic_ns": ended,
        "samples": samples,
    }

    observer.verify_phase_b_receipt_bindings(
        phase_a,
        phase_b,
        trace,
        expected_phase_a_sha256="c" * 64,
    )

    missing_sentinel = copy.deepcopy(trace)
    missing_sentinel["receipts"].pop()
    phase_b_missing = copy.deepcopy(phase_b)
    phase_b_missing["gate_receipt_trace_sha256"] = hashlib.sha256(
        canonical_json(missing_sentinel)
    ).hexdigest()
    with pytest.raises(health.GateAbort, match="sentinel|post-end"):
        observer.verify_phase_b_receipt_bindings(
            phase_a,
            phase_b_missing,
            missing_sentinel,
            expected_phase_a_sha256="c" * 64,
        )

    hidden_transition = copy.deepcopy(trace)
    asserted = copy.deepcopy(active)
    asserted.update(
        {
            "sequence": 14,
            "observed_monotonic_ns": started + 1_000_000_000,
            "priority_state": "asserted",
            "controller_phase": "yielding",
            "recovery_reason": "priority_pressure",
            "admission_open": False,
            "distinct_clear_count": 0,
        }
    )
    hidden_transition["receipts"] = [idle, active, asserted]
    for sequence, receipt in enumerate(trace["receipts"][2:], start=15):
        replacement = copy.deepcopy(receipt)
        replacement["sequence"] = sequence
        hidden_transition["receipts"].append(replacement)
    phase_b_hidden = copy.deepcopy(phase_b)
    phase_b_hidden["gate_receipt_trace_sha256"] = hashlib.sha256(
        canonical_json(hidden_transition)
    ).hexdigest()
    with pytest.raises(health.GateAbort, match="transition|continuously|receipt"):
        observer.verify_phase_b_receipt_bindings(
            phase_a,
            phase_b_hidden,
            hidden_transition,
            expected_phase_a_sha256="c" * 64,
        )


def phase_a_collector_fixture() -> tuple[
    observer.PhaseAEventCollector,
    list[dict[str, object]],
    list[dict[str, object]],
]:
    assertion_document = priority_assertion_document()
    assertion_payload = canonical_json(assertion_document)
    assertion = observer.PriorityAssertion(
        document=assertion_document,
        payload=assertion_payload,
        attestation=observer.validate_priority_assertion(
            assertion_document,
            assertion_payload,
            expected_policy_sha256="3" * 64,
        ),
        t0_monotonic_ns=2_100_000_000,
    )
    identity = {
        "fixture_sha256": "a" * 64,
        "task": "transcribe",
        "language": "en",
        "cursor_start_ms": 0,
        "total_duration_ms": 10_000,
    }
    workload_sha256 = hashlib.sha256(canonical_json(identity)).hexdigest()
    collector = observer.PhaseAEventCollector(
        runtime_epoch="5" * 32,
        runtime_started_monotonic_ns=100_000_000,
        gate_token_sha256="6" * 64,
        workload_sha256=workload_sha256,
        workload_identity=identity,
        policy_sha256="3" * 64,
        assertion=assertion,
        model_identity_sha256="9" * 64,
        allowed_unloaded_bytes=200,
    )
    assertion_digest = assertion.attestation["observation_digest"]

    def make(
        sequence: int,
        observed: int,
        *,
        state: str,
        phase: str,
        count: int,
        resident: bool,
        load: int,
        unload: int,
        source: int,
        observation: str,
        transition: str,
        transition_sequence: int,
        active: bool = True,
        uncommitted: bool = False,
        completed: bool = False,
    ) -> dict[str, object]:
        receipt = runtime_receipt(
            sequence,
            observed_monotonic_ns=observed,
            workload_sha256=workload_sha256,
            priority_state=state,
            controller_phase=phase,
            recovery_reason=None if phase == "normal" else "priority_pressure",
            model_resident=resident,
        )
        receipt.update(
            {
                "source_generation": source,
                "observation_digest": observation,
                "transition_observation_digest": transition,
                "transition_sequence": transition_sequence,
                "distinct_clear_count": count,
                "model_load_generation": load,
                "model_unload_generation": unload,
                "active": active,
                "chunk_uncommitted": uncommitted,
                "active_cursor_ms": 0 if active else None,
                "completed_cursor_ms": 10_000 if completed else None,
                "completion_generation": 1 if completed else 0,
            }
        )
        return receipt

    idle = runtime_receipt(
        1,
        observed_monotonic_ns=500_000_000,
        workload_sha256=None,
        priority_state="neutral",
        controller_phase="normal",
        recovery_reason=None,
        model_resident=True,
    )
    idle.update(
        {
            "source_generation": 15,
            "distinct_clear_count": 0,
            "chunk_uncommitted": False,
        }
    )
    receipts = [
        idle,
        make(
            2,
            1_500_000_000,
            state="clear",
            phase="normal",
            count=3,
            resident=True,
            load=1,
            unload=0,
            source=16,
            observation="1" * 64,
            transition="1" * 64,
            transition_sequence=3,
            uncommitted=True,
        ),
        make(
            3,
            3_000_000_000,
            state="asserted",
            phase="yielding",
            count=0,
            resident=True,
            load=1,
            unload=0,
            source=17,
            observation=assertion_digest,
            transition=assertion_digest,
            transition_sequence=4,
            uncommitted=True,
        ),
        make(
            4,
            4_000_000_000,
            state="asserted",
            phase="recovering",
            count=0,
            resident=True,
            load=1,
            unload=0,
            source=17,
            observation=assertion_digest,
            transition=assertion_digest,
            transition_sequence=4,
        ),
        make(
            5,
            5_000_000_000,
            state="asserted",
            phase="recovering",
            count=0,
            resident=False,
            load=1,
            unload=1,
            source=17,
            observation=assertion_digest,
            transition=assertion_digest,
            transition_sequence=4,
        ),
        make(
            6,
            6_000_000_000,
            state="clear",
            phase="recovering",
            count=1,
            resident=False,
            load=1,
            unload=1,
            source=18,
            observation="5" * 64,
            transition="5" * 64,
            transition_sequence=5,
        ),
        make(
            7,
            7_000_000_000,
            state="clear",
            phase="recovering",
            count=2,
            resident=False,
            load=1,
            unload=1,
            source=19,
            observation="6" * 64,
            transition="5" * 64,
            transition_sequence=5,
        ),
        make(
            8,
            8_000_000_000,
            state="clear",
            phase="normal",
            count=3,
            resident=False,
            load=1,
            unload=1,
            source=20,
            observation="7" * 64,
            transition="5" * 64,
            transition_sequence=5,
        ),
        make(
            9,
            9_000_000_000,
            state="clear",
            phase="normal",
            count=3,
            resident=True,
            load=2,
            unload=1,
            source=20,
            observation="8" * 64,
            transition="5" * 64,
            transition_sequence=5,
        ),
        make(
            10,
            10_000_000_000,
            state="clear",
            phase="normal",
            count=3,
            resident=True,
            load=2,
            unload=1,
            source=20,
            observation="9" * 64,
            transition="5" * 64,
            transition_sequence=5,
            active=False,
            completed=True,
        ),
    ]
    host: list[dict[str, object]] = []
    receipt_indices = (1, 2, 3, 4, 4, 5, 6, 7, 8, 9)
    for event_index, receipt_index in enumerate(receipt_indices):
        monotonic_ns = receipts[receipt_index]["observed_monotonic_ns"]
        if event_index == 4:
            monotonic_ns = 5_500_000_000
        host.append(
            {
                "monotonic_ns": monotonic_ns,
                "candidate_bytes": 100,
                "output_count": 1 if event_index == 9 else 0,
                "marker_count": 0,
                "output_create_count": 1 if event_index == 9 else 0,
                "marker_create_count": 0,
                "threshold_masking_allowed": event_index in {4, 5, 6, 7},
            }
        )
    return collector, receipts, host


def test_phase_a_collector_enforces_exact_ten_event_machine_and_deadlines() -> None:
    collector, receipts, host = phase_a_collector_fixture()
    prefix_lengths = (2, 3, 4, 5, 5, 6, 7, 8, 9, 10)
    for kind, length, observation in zip(
        observer.PHASE_A_EVENT_KINDS, prefix_lengths, host, strict=True
    ):
        collector.record_event(
            kind,
            receipts=receipts[:length],
            host_observation=observation,
        )
    assert [event["kind"] for event in collector.require_complete()] == list(
        observer.PHASE_A_EVENT_KINDS
    )
    assert (
        collector.events[4]["gate_receipt_sha256"]
        == collector.events[3]["gate_receipt_sha256"]
    )
    trace = observer.build_phase_a_receipt_trace_document(
        receipts=receipts,
        runtime_epoch="5" * 32,
        gate_token_sha256="6" * 64,
        workload_sha256=collector.workload_sha256,
        completion_event=collector.events[-1],
    )
    assert trace["receipts"][-1]["sequence"] == 10
    extended = copy.deepcopy(receipts)
    extra = copy.deepcopy(receipts[-1])
    extra.update({"sequence": 11, "observed_monotonic_ns": 11_000_000_000})
    extended.append(extra)
    with pytest.raises(health.GateAbort, match="completion|end"):
        observer.build_phase_a_receipt_trace_document(
            receipts=extended,
            runtime_epoch="5" * 32,
            gate_token_sha256="6" * 64,
            workload_sha256=collector.workload_sha256,
            completion_event=collector.events[-1],
        )

    wrong_order, receipts, host = phase_a_collector_fixture()
    with pytest.raises(health.GateAbort, match="order"):
        wrong_order.record_event(
            "assertion_consumed",
            receipts=receipts[:2],
            host_observation=host[0],
        )

    late, receipts, host = phase_a_collector_fixture()
    late.record_event("pre_assertion", receipts=receipts[:2], host_observation=host[0])
    receipts[2]["observed_monotonic_ns"] = 17_100_000_001
    host[1]["monotonic_ns"] = 17_100_000_001
    with pytest.raises(health.GateAbort, match="deadline"):
        late.record_event(
            "assertion_consumed",
            receipts=receipts[:3],
            host_observation=host[1],
        )


def test_priority_assertion_copy_is_exact_create_once_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "_owner_id", lambda: None)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    destination = (private / "assertion.json").resolve()
    document = priority_assertion_document()
    payload = canonical_json(document)
    assertion = observer.PriorityAssertion(
        document=document,
        payload=payload,
        attestation=observer.validate_priority_assertion(
            document, payload, expected_policy_sha256="3" * 64
        ),
        t0_monotonic_ns=2_100_000_000,
    )

    def portable_create_once(path: Path, body: bytes, mode: int) -> None:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            mode,
        )
        try:
            os.write(descriptor, body)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def portable_load(
        path: Path,
        *,
        validator,
        expected_sha256: str | None = None,
        max_bytes: int,
    ) -> dict[str, object]:
        body = path.read_bytes()
        assert len(body) <= max_bytes
        assert (
            expected_sha256 is None
            or hashlib.sha256(body).hexdigest() == expected_sha256
        )
        document = json.loads(body)
        return validator(document)

    with (
        mock.patch.object(
            health, "_write_private_create_only", side_effect=portable_create_once
        ),
        mock.patch.object(health, "load_canonical_artifact", side_effect=portable_load),
    ):
        artifact = observer.write_priority_assertion_observation(destination, assertion)
        assert destination.read_bytes() == payload
        assert artifact.file_sha256 == hashlib.sha256(payload).hexdigest()
        with pytest.raises(health.GateAbort, match="existed|create"):
            observer.write_priority_assertion_observation(destination, assertion)


def phase_b_host_sample() -> dict[str, object]:
    return {
        "candidate_running": True,
        "detection_fps": 50.0,
        "camera_min_process_ratio": 0.99,
        "camera_max_skipped_fps": 0.0,
        "camera_low_ratio_elapsed_ms": 0,
        "detector_count": 2,
        "detector_stalled_count": 0,
        "embedding_metric_count": 3,
        "embedding_invalid_count": 0,
        "candidate_oom_killed": False,
        "cgroup_oom_delta": 0,
        "cgroup_oom_kill_delta": 0,
        "cgroup_oom_group_kill_delta": 0,
        "runtime_cuda_oom_generation_delta": 0,
        "runtime_media_failure_generation_delta": 0,
        "candidate_cuda_oom_log_match_delta": 0,
        "nvidia_xid_log_match_delta": 0,
        "candidate_restart_delta": 0,
        "frigate_restart_delta": 0,
        "ollama_loaded": False,
    }


def gate_failure_baseline() -> observer.GateFailureBaseline:
    return observer.GateFailureBaseline(
        candidate_restart_count=0,
        candidate_oom_killed=False,
        frigate_restart_count=0,
        cgroup_pid=101,
        cgroup_path_sha256="1" * 64,
        cgroup_oom=0,
        cgroup_oom_kill=0,
        cgroup_oom_group_kill=0,
        runtime_cuda_oom_generation=0,
        runtime_media_failure_generation=0,
        candidate_log_byte_cursor=10,
        candidate_cuda_oom_log_matches=0,
        candidate_log_source_sha256="2" * 64,
        kernel_cursor_sha256="3" * 64,
        nvidia_xid_log_matches=0,
    )


def zero_failure_deltas() -> dict[str, object]:
    return {
        "candidate_restart_delta": 0,
        "candidate_oom_killed": False,
        "frigate_restart_delta": 0,
        "cgroup_oom_delta": 0,
        "cgroup_oom_kill_delta": 0,
        "cgroup_oom_group_kill_delta": 0,
        "runtime_cuda_oom_generation_delta": 0,
        "runtime_media_failure_generation_delta": 0,
        "candidate_cuda_oom_log_match_delta": 0,
        "nvidia_xid_log_match_delta": 0,
    }


def _write_fake_cgroup(
    path: Path, *, populated: int, subtree: str = "memory pids\n"
) -> None:
    path.mkdir(parents=True, exist_ok=False)
    (path / "cgroup.controllers").write_text("memory pids\n", encoding="ascii")
    (path / "cgroup.subtree_control").write_text(subtree, encoding="ascii")
    (path / "cgroup.type").write_text("domain\n", encoding="ascii")
    (path / "cgroup.procs").write_text("", encoding="ascii")
    (path / "cgroup.events").write_text(
        f"populated {populated}\nfrozen 0\n", encoding="ascii"
    )
    (path / "memory.events").write_text(
        "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n",
        encoding="ascii",
    )


def test_gate_cgroup_driver_bindings_are_explicit_and_token_bound() -> None:
    digest = "d" * 64
    name = observer.GATE_CGROUP_NAME_PREFIX + digest
    assert observer.GateOwnedCgroupParent._binding_for_driver("cgroupfs", digest) == (
        name,
        f"/{name}",
        None,
        None,
    )
    assert observer.GateOwnedCgroupParent._binding_for_driver("systemd", digest) == (
        name,
        f"{name}.slice",
        f"{name}.slice",
        f"{name}keeper.service",
    )
    with pytest.raises(health.GateAbort, match="driver_binding"):
        observer.GateOwnedCgroupParent._binding_for_driver("unknown", digest)


def test_systemd_keeper_explicitly_pins_memory_and_pids_controllers() -> None:
    with mock.patch.object(
        health,
        "bounded_command",
        return_value=health.CommandResult(0, ""),
    ) as command:
        observer.GateOwnedCgroupParent._create_systemd_parent(
            slice_unit="subgentask.slice",
            keeper_unit="subgentaskkeeper.service",
        )

    argv = command.call_args.args[0]
    assert "--property=MemoryAccounting=yes" in argv
    assert "--property=TasksAccounting=yes" in argv
    assert "--property=RemainAfterExit=yes" in argv
    assert argv[-1] == "/usr/bin/true"


def test_gate_cgroup_create_rejects_candidate_without_exact_driver_parent(
    tmp_path: Path,
) -> None:
    client = mock.Mock()
    client.command.return_value = health.CommandResult(0, "cgroupfs\n")
    binding = SimpleNamespace(gate_token_digest="d" * 64)
    item = {"HostConfigFull": {"CgroupParent": ""}}
    with (
        mock.patch.object(
            observer.GateOwnedCgroupParent, "_validate_root", return_value=tmp_path
        ),
        mock.patch.object(
            observer.GateOwnedCgroupParent, "_create_cgroupfs_parent"
        ) as create_parent,
        pytest.raises(health.GateAbort, match="parent_binding"),
    ):
        observer.GateOwnedCgroupParent.create(
            client, item, binding, cgroup_root=tmp_path
        )
    create_parent.assert_not_called()


def test_gate_owned_parent_is_the_only_live_candidate_memory_owner(
    tmp_path: Path,
) -> None:
    parent_path = tmp_path / "gate-parent"
    _write_fake_cgroup(parent_path, populated=1)
    candidate_path = parent_path / "docker-candidate"
    candidate_path.mkdir()
    (candidate_path / "cgroup.procs").write_text("101\n", encoding="ascii")
    metadata = parent_path.lstat()
    parent = observer.GateOwnedCgroupParent(
        driver="cgroupfs",
        root=tmp_path,
        path=parent_path,
        host_config_parent="/gate-parent",
        name="gate-parent",
        path_identity=(metadata.st_dev, metadata.st_ino),
        path_owner_uid=metadata.st_uid,
        subtree_controllers=frozenset({"memory", "pids"}),
    )
    candidate_probe = mock.Mock()
    candidate_probe._candidate_pid_and_cgroup.return_value = (101, candidate_path)
    candidate_probe._pid_set.return_value = {101}
    parent.bind_live(candidate_probe)

    wrapped = observer.GateOwnedCgroupProbe(parent, candidate_probe)
    snapshot = wrapped.memory_events()
    assert snapshot.container_pid == 101
    assert (
        snapshot.cgroup_path_sha256
        == hashlib.sha256(str(parent_path).encode("utf-8")).hexdigest()
    )
    assert wrapped.pinned_source() == (101, parent_path)

    unexpected = parent_path / "foreign-child"
    unexpected.mkdir()
    with pytest.raises(health.GateAbort, match="child_set|unexpected_child"):
        parent.assert_live_placement(candidate_probe)


def test_gate_owned_parent_cleanup_requires_child_removal_and_zero_population(
    tmp_path: Path,
) -> None:
    parent_path = tmp_path / "gate-parent"
    _write_fake_cgroup(parent_path, populated=1)
    candidate_path = parent_path / "docker-candidate"
    candidate_path.mkdir()
    metadata = parent_path.lstat()
    parent = observer.GateOwnedCgroupParent(
        driver="cgroupfs",
        root=tmp_path,
        path=parent_path,
        host_config_parent="/gate-parent",
        name="gate-parent",
        path_identity=(metadata.st_dev, metadata.st_ino),
        path_owner_uid=metadata.st_uid,
        subtree_controllers=frozenset({"memory", "pids"}),
    )
    with pytest.raises(health.GateAbort, match="populated"):
        parent.cleanup()

    candidate_path.rmdir()
    (parent_path / "cgroup.events").write_text(
        "populated 0\nfrozen 0\n", encoding="ascii"
    )
    real_rmdir = os.rmdir

    def emulate_control_write(path: Path, payload: bytes, *, label: str) -> None:
        assert path == parent_path / "cgroup.subtree_control"
        assert payload == b"-memory -pids\n"
        assert label == "gate cgroup controller cleanup"
        path.write_text("", encoding="ascii")

    def emulate_cgroup_rmdir(path: Path) -> None:
        assert Path(path) == parent_path
        for child in parent_path.iterdir():
            child.unlink()
        real_rmdir(parent_path)

    with (
        mock.patch.object(
            observer.GateOwnedCgroupParent,
            "_write_text",
            side_effect=emulate_control_write,
        ),
        mock.patch.object(observer.os, "rmdir", side_effect=emulate_cgroup_rmdir),
    ):
        parent.cleanup()
    assert parent._cleaned is True
    assert not parent_path.exists()


def test_gate_owned_systemd_parent_cleanup_stops_the_slice_as_one_unit(
    tmp_path: Path,
) -> None:
    parent_path = tmp_path / "gate-parent.slice"
    _write_fake_cgroup(parent_path, populated=0)
    metadata = parent_path.lstat()
    parent = observer.GateOwnedCgroupParent(
        driver="systemd",
        root=tmp_path,
        path=parent_path,
        host_config_parent="gate-parent.slice",
        name="gate-parent",
        path_identity=(metadata.st_dev, metadata.st_ino),
        path_owner_uid=metadata.st_uid,
        subtree_controllers=frozenset({"memory", "pids"}),
        slice_unit="gate-parent.slice",
        keeper_unit="gate-parentkeeper.service",
    )

    def stop_slice(argv: list[str], **_kwargs: object) -> health.CommandResult:
        assert argv == [
            "/usr/bin/systemctl",
            "stop",
            "gate-parent.slice",
            "--no-pager",
        ]
        for child in parent_path.iterdir():
            child.unlink()
        parent_path.rmdir()
        return health.CommandResult(0, "")

    with (
        mock.patch.object(parent, "_verify_systemd_owner"),
        mock.patch.object(health, "bounded_command", side_effect=stop_slice) as command,
    ):
        parent.cleanup()

    command.assert_called_once()
    assert parent._cleaned is True
    assert not parent_path.exists()


def test_failed_gate_parent_creation_never_swallows_unverified_cleanup(
    tmp_path: Path,
) -> None:
    client = mock.Mock()
    client.command.return_value = health.CommandResult(0, "cgroupfs\n")
    binding = SimpleNamespace(gate_token_digest="d" * 64)
    name, host_parent, _slice, _keeper = (
        observer.GateOwnedCgroupParent._binding_for_driver(
            "cgroupfs", binding.gate_token_digest
        )
    )
    item = {"HostConfigFull": {"CgroupParent": host_parent}}

    def create_parent(path: Path) -> None:
        path.mkdir()

    with (
        mock.patch.object(
            observer.GateOwnedCgroupParent, "_validate_root", return_value=tmp_path
        ),
        mock.patch.object(
            observer.GateOwnedCgroupParent,
            "_create_cgroupfs_parent",
            side_effect=create_parent,
        ),
        mock.patch.object(
            observer.GateOwnedCgroupParent,
            "_delegate_cgroupfs_controllers",
            side_effect=health.GateAbort("partial cgroup creation"),
        ),
        mock.patch.object(
            observer.GateOwnedCgroupParent,
            "_cleanup_failed_creation",
            side_effect=health.GateAbort("cleanup unverified"),
        ) as cleanup,
        pytest.raises(health.GateAbort, match="cleanup_unverified"),
    ):
        observer.GateOwnedCgroupParent.create(
            client, item, binding, cgroup_root=tmp_path
        )

    cleanup.assert_called_once()


def test_gate_parent_creation_race_never_cleans_an_unowned_path(
    tmp_path: Path,
) -> None:
    client = mock.Mock()
    client.command.return_value = health.CommandResult(0, "cgroupfs\n")
    binding = SimpleNamespace(gate_token_digest="d" * 64)
    name, host_parent, _slice, _keeper = (
        observer.GateOwnedCgroupParent._binding_for_driver(
            "cgroupfs", binding.gate_token_digest
        )
    )
    item = {"HostConfigFull": {"CgroupParent": host_parent}}
    parent_path = tmp_path / name

    def lose_creation_race(path: Path) -> None:
        path.mkdir()
        raise health.GateAbort("gate cgroup parent already existed")

    with (
        mock.patch.object(
            observer.GateOwnedCgroupParent, "_validate_root", return_value=tmp_path
        ),
        mock.patch.object(
            observer.GateOwnedCgroupParent,
            "_create_cgroupfs_parent",
            side_effect=lose_creation_race,
        ),
        mock.patch.object(
            observer.GateOwnedCgroupParent, "_cleanup_failed_creation"
        ) as cleanup,
        pytest.raises(health.GateAbort, match="already_existed"),
    ):
        observer.GateOwnedCgroupParent.create(
            client, item, binding, cgroup_root=tmp_path
        )

    cleanup.assert_not_called()
    assert parent_path.exists()


def test_phase_b_reset_time_is_recorded_only_after_all_baselines() -> None:
    calls: list[str] = []
    baseline = gate_failure_baseline()
    pinned = mock.Mock(spec=observer.PinnedCgroupEvidence)

    def capture(**_kwargs: object) -> observer.GateFailureBaseline:
        calls.append("baseline_complete")
        return baseline

    def timestamp() -> int:
        calls.append("reset_timestamp")
        return 12_345

    def pin(_probe: object, *, baseline: observer.GateFailureBaseline) -> mock.Mock:
        assert baseline is gate_failure_baseline_value
        calls.append("cgroup_pinned")
        return pinned

    gate_failure_baseline_value = baseline
    lifecycle = observer.PhaseBLifecycleOrder()

    with (
        mock.patch.object(
            observer, "capture_gate_failure_baseline", side_effect=capture
        ),
        mock.patch.object(observer.PinnedCgroupEvidence, "capture", side_effect=pin),
        mock.patch.object(observer.time, "monotonic_ns", side_effect=timestamp),
    ):
        observed_baseline, observed_pinned, reset_ns = (
            observer._capture_phase_b_reset_boundary(
                client=mock.Mock(),
                candidate=mock.Mock(),
                frigate=mock.Mock(),
                args=SimpleNamespace(),
                cgroup_probe=mock.Mock(),
                candidate_log=mock.Mock(),
                kernel_journal=mock.Mock(),
                receipt=runtime_receipt(1),
                lifecycle=lifecycle,
            )
        )

    assert observed_baseline is baseline
    assert observed_pinned is pinned
    assert reset_ns == 12_345
    assert calls == ["baseline_complete", "cgroup_pinned", "reset_timestamp"]


@pytest.mark.skipif(
    sys.platform != "linux", reason="directory-FD pinning is Linux-only"
)
def test_pinned_cgroup_evidence_survives_stop_and_rejects_reuse_or_oom(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "gate-parent"
    candidate_child = parent / "docker-candidate"
    candidate_child.mkdir(parents=True)
    memory_events = parent / "memory.events"
    cgroup_events = parent / "cgroup.events"
    memory_events.write_text(
        "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n",
        encoding="ascii",
    )
    cgroup_events.write_text("populated 1\nfrozen 0\n", encoding="ascii")
    initial = parent.lstat()

    baseline_values = dict(gate_failure_baseline().__dict__)
    baseline_values["cgroup_path_sha256"] = hashlib.sha256(
        str(parent).encode("utf-8")
    ).hexdigest()
    baseline = observer.GateFailureBaseline(**baseline_values)
    probe = mock.Mock()
    probe.pinned_source.return_value = (101, parent)
    with mock.patch.object(observer.sys, "platform", "linux"):
        evidence = observer.PinnedCgroupEvidence.capture(probe, baseline=baseline)
        candidate_child.rmdir()
        cgroup_events.write_text("populated 0\nfrozen 0\n", encoding="ascii")
        final = evidence.snapshot(expected_population=0)
        assert final.descendants_populated == 0
        observer._validate_stopped_cgroup_boundary(
            baseline=baseline, cgroup_evidence=evidence
        )

        memory_events.write_text(
            "low 0\nhigh 0\nmax 0\noom 1\noom_kill 0\noom_group_kill 0\n",
            encoding="ascii",
        )
        with pytest.raises(health.GateAbort, match="cgroup"):
            observer._validate_stopped_cgroup_boundary(
                baseline=baseline, cgroup_evidence=evidence
            )

        evidence._path_identity = (initial.st_dev, initial.st_ino + 1)
        with pytest.raises(health.GateAbort, match="changed|reused"):
            evidence.snapshot(expected_population=0)

        evidence._path_identity = (initial.st_dev, initial.st_ino)
        memory_events.unlink()
        cgroup_events.unlink()
        parent.rmdir()
        with pytest.raises(health.GateAbort, match="disappeared"):
            evidence.snapshot(expected_population=0)
        evidence.close()


def test_removed_kernfs_event_descriptor_enodev_fails_closed() -> None:
    metadata = SimpleNamespace(st_dev=7, st_ino=11, st_mode=stat.S_IFREG | 0o444)
    with (
        mock.patch.object(observer.os, "fstat", return_value=metadata),
        mock.patch.object(
            observer.os,
            "pread",
            side_effect=OSError(errno.ENODEV, "removed kernfs node"),
            create=True,
        ),
        pytest.raises(health.GateAbort, match="became_unavailable"),
    ):
        observer.PinnedCgroupEvidence._read_descriptor(
            11,
            observer.PinnedCgroupEvidence._identity(metadata),
            label="removed candidate cgroup events",
        )


def test_run_observer_declares_the_exact_phase_b_lifecycle_order() -> None:
    def stages(function: object, receiver: str) -> list[str]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        calls = sorted(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == receiver
                and node.func.attr in {"checkpoint", "perform"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ),
            key=lambda node: (node.lineno, node.col_offset),
        )
        return [str(node.args[0].value) for node in calls]

    source = inspect.getsource(observer.run_observer)
    assert "_capture_phase_b_reset_boundary(" in source
    actual = stages(observer._capture_phase_b_reset_boundary, "lifecycle") + stages(
        observer.run_observer, "phase_b_lifecycle"
    )
    assert actual == list(observer.PHASE_B_LIFECYCLE_STAGES)

    for operation in (
        "write_phase_b_artifacts(",
        "_validate_live_phase_b_failure_boundary(",
        "health.stop_bound_candidate(client, candidate, args)",
        "candidate_log.close_after_stop()",
        '"final_cgroup_drain"',
        '"final_kernel_drain"',
        '"final_receipt_drain"',
        '"cgroup_cleanup"',
        '"watcher_snapshot"',
        '"watcher_close"',
        '"final_gate_write"',
    ):
        assert operation in source

    lifecycle = observer.PhaseBLifecycleOrder()
    with pytest.raises(health.GateAbort, match="order"):
        lifecycle.checkpoint("reset_timestamp")


def test_post_seal_live_drain_rejects_post_t900_cuda_oom_or_xid() -> None:
    gpu_uuid = "GPU-12345678-1234-1234-1234-123456789abc"
    cgroup_probe = mock.Mock()
    cgroup_probe.attributed_gpu_bytes.return_value = health.GpuAttribution(
        candidate_bytes=1024,
        validated_monotonic_ns=99,
        pid_set_sha256="4" * 64,
        gpu_uuid_sha256=hashlib.sha256(gpu_uuid.encode("ascii")).hexdigest(),
    )
    args = SimpleNamespace(gpu_free_floor_bytes=8 * health.GIB)
    common = {
        "baseline": gate_failure_baseline(),
        "client": mock.Mock(),
        "candidate": mock.Mock(),
        "frigate": mock.Mock(),
        "args": args,
        "cgroup_probe": cgroup_probe,
        "candidate_log": mock.Mock(),
        "kernel_journal": mock.Mock(),
        "receipt": runtime_receipt(1),
        "gpu_uuid": gpu_uuid,
    }
    with (
        mock.patch.object(
            observer, "capture_gate_failure_deltas", return_value=zero_failure_deltas()
        ),
        mock.patch.object(health, "gpu_telemetry", return_value={"free_mib": 9 * 1024}),
    ):
        observer._validate_live_phase_b_failure_boundary(**common)

    for failure_key in (
        "runtime_cuda_oom_generation_delta",
        "candidate_cuda_oom_log_match_delta",
        "nvidia_xid_log_match_delta",
    ):
        failed = zero_failure_deltas()
        failed[failure_key] = 1
        with (
            mock.patch.object(
                observer, "capture_gate_failure_deltas", return_value=failed
            ),
            mock.patch.object(
                health, "gpu_telemetry", return_value={"free_mib": 9 * 1024}
            ),
            pytest.raises(health.GateAbort, match="failure_counter"),
        ):
            observer._validate_live_phase_b_failure_boundary(**common)


def test_stopped_eof_drain_rejects_cleanup_window_log_xid_and_runtime_failures() -> (
    None
):
    baseline = gate_failure_baseline()
    stopped_item = {
        "RestartCount": 0,
        "State": {"Running": False, "Pid": 0, "OOMKilled": False},
    }

    class Kernel:
        def __init__(self, xid_matches: int = 0) -> None:
            self.xid_matches = xid_matches

        def snapshot(self) -> health.KernelJournalSnapshot:
            return health.KernelJournalSnapshot(
                cursor_sha256="5" * 64,
                xid_matches=self.xid_matches,
                continuous=True,
            )

    class Journal:
        def __init__(self, *, cuda_oom_generation: int = 0) -> None:
            receipt = runtime_receipt(1)
            receipt["cuda_oom_generation"] = cuda_oom_generation
            self.receipts = [receipt]
            self.final_reads: list[bool] = []

        def read_available(self, *, final: bool = False) -> list[dict[str, object]]:
            self.final_reads.append(final)
            return []

    clean_log = health.CandidateLogSnapshot(
        byte_cursor=11,
        cuda_oom_matches=0,
        source_container_id_sha256=baseline.candidate_log_source_sha256,
        continuous=True,
    )
    common = {
        "baseline": baseline,
        "client": mock.Mock(),
        "frigate": mock.Mock(),
        "stopped_item": stopped_item,
    }
    observed = {"running": True, "health": "healthy", "restart_count": 0}
    clean_journal = Journal()
    clean_cgroup = mock.Mock(spec=observer.PinnedCgroupEvidence)
    clean_cgroup.snapshot.return_value = observer.PinnedCgroupSnapshot(
        container_pid=baseline.cgroup_pid,
        cgroup_path_sha256=baseline.cgroup_path_sha256,
        oom=0,
        oom_kill=0,
        oom_group_kill=0,
        descendants_populated=0,
        frozen=0,
    )
    with mock.patch.object(health, "observed_state", return_value=observed):
        observer._validate_stopped_phase_b_failure_boundary(
            **common,
            candidate_log_snapshot=clean_log,
            cgroup_evidence=clean_cgroup,
            kernel_journal=Kernel(),
            journal=clean_journal,  # type: ignore[arg-type]
        )
    assert clean_journal.final_reads == [True]

    failed_cases = (
        (
            health.CandidateLogSnapshot(
                byte_cursor=12,
                cuda_oom_matches=1,
                source_container_id_sha256=baseline.candidate_log_source_sha256,
                continuous=True,
            ),
            Kernel(),
            Journal(),
        ),
        (clean_log, Kernel(xid_matches=1), Journal()),
        (clean_log, Kernel(), Journal(cuda_oom_generation=1)),
    )
    for candidate_log, kernel, journal in failed_cases:
        with (
            mock.patch.object(health, "observed_state", return_value=observed),
            pytest.raises(health.GateAbort, match="final|failure"),
        ):
            observer._validate_stopped_phase_b_failure_boundary(
                **common,
                candidate_log_snapshot=candidate_log,
                cgroup_evidence=clean_cgroup,
                kernel_journal=kernel,
                journal=journal,  # type: ignore[arg-type]
            )


def test_phase_b_scheduler_collects_181_without_catchup_and_requires_sentinel() -> None:
    started = 10_000_000_000
    active = runtime_receipt(
        1,
        observed_monotonic_ns=started,
        workload_sha256="b" * 64,
        priority_state="clear",
        controller_phase="normal",
        recovery_reason=None,
        model_resident=True,
    )
    active.update(
        {
            "distinct_clear_count": 3,
            "chunk_uncommitted": False,
            "transition_observation_digest": "8" * 64,
        }
    )

    class FakeJournal:
        def __init__(self) -> None:
            self.receipts = [active]

        def read_available(self) -> list[dict[str, object]]:
            return []

    class FakeClock:
        def __init__(self) -> None:
            self.value = started

        def now(self) -> int:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += round(seconds * 1_000_000_000)

    collector = observer.PhaseBSampleCollector(
        started_monotonic_ns=started,
        runtime_epoch="5" * 32,
        runtime_started_monotonic_ns=1,
        gate_token_sha256="6" * 64,
        workload_sha256="b" * 64,
        policy_sha256="3" * 64,
        producer_epoch="2" * 32,
        candidate_identity_sha256="c" * 64,
        model_identity_sha256="9" * 64,
    )
    journal = FakeJournal()
    clock = FakeClock()
    samples = observer.run_phase_b_schedule(
        collector,
        journal,  # type: ignore[arg-type]
        capture_host_sample=lambda _index: phase_b_host_sample(),
        monotonic_ns=clock.now,
        sleeper=clock.sleep,
    )
    assert len(samples) == 181
    assert samples[-1]["scheduled_offset_seconds"] == 900
    ended = clock.now()
    sentinel = copy.deepcopy(active)
    sentinel.update(
        {
            "sequence": 2,
            "observed_monotonic_ns": ended + 1,
        }
    )
    journal.receipts.append(sentinel)
    assert (
        len(
            collector.require_complete(
                ended_monotonic_ns=ended, receipts=journal.receipts
            )
        )
        == 181
    )

    late_collector = observer.PhaseBSampleCollector(
        started_monotonic_ns=started,
        runtime_epoch="5" * 32,
        runtime_started_monotonic_ns=1,
        gate_token_sha256="6" * 64,
        workload_sha256="b" * 64,
        policy_sha256="3" * 64,
        producer_epoch="2" * 32,
        candidate_identity_sha256="c" * 64,
        model_identity_sha256="9" * 64,
    )
    with pytest.raises(health.GateAbort, match="late"):
        observer.run_phase_b_schedule(
            late_collector,
            FakeJournal(),  # type: ignore[arg-type]
            capture_host_sample=lambda _index: phase_b_host_sample(),
            monotonic_ns=lambda: started + 2_000_000_001,
            sleeper=lambda _seconds: None,
        )


def test_protected_phase_a_sampling_has_no_two_second_blind_interval() -> None:
    protected = observer.ProtectedPhaseASampleCollector()
    for captured in (1_000_000_000, 3_000_000_000, 5_000_000_000):
        protected.record(
            captured_monotonic_ns=captured,
            telemetry_valid=True,
            threshold_failed=False,
        )
    assert protected.proof(
        t0_monotonic_ns=2_000_000_000,
        gpu_proof_monotonic_ns=4_000_000_000,
    ) == {
        "protected_first_sample_monotonic_ns": 1_000_000_000,
        "protected_last_sample_monotonic_ns": 5_000_000_000,
        "protected_sample_count": 3,
        "protected_blind_interval_count": 0,
        "protected_threshold_failure_count": 0,
    }

    blind = observer.ProtectedPhaseASampleCollector()
    blind.record(
        captured_monotonic_ns=1_000_000_000,
        telemetry_valid=True,
        threshold_failed=False,
    )
    with pytest.raises(health.GateAbort, match="blind|cadence"):
        blind.record(
            captured_monotonic_ns=3_000_000_001,
            telemetry_valid=True,
            threshold_failed=False,
        )

    failed = observer.ProtectedPhaseASampleCollector()
    with pytest.raises(health.GateAbort, match="threshold"):
        failed.record(
            captured_monotonic_ns=1_000_000_000,
            telemetry_valid=True,
            threshold_failed=True,
        )


def test_cli_entrypoint_redacts_unexpected_private_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_value = "private-observation-id-do-not-print"
    with mock.patch.object(
        observer,
        "main",
        side_effect=RuntimeError(private_value),
    ):
        assert observer.cli_entrypoint(["verify-release"]) == 1

    captured = capsys.readouterr()
    assert private_value not in captured.out
    assert private_value not in captured.err
    assert captured.out == ""
    assert captured.err == (
        "TASK11B_RELEASE_VERIFY_ABORT reason=internal_verifier_failure\n"
    )


def test_runtime_status_is_cross_bound_to_latest_receipt_generations() -> None:
    status = healthy_status()["resource_management"]
    assert isinstance(status, dict)
    receipt = runtime_receipt(
        1,
        priority_state="clear",
        controller_phase="normal",
        recovery_reason=None,
    )
    receipt.update(
        {
            "distinct_clear_count": 3,
            "transition_sequence": 4,
            "model_load_generation": 1,
            "model_unload_generation": 0,
        }
    )
    assert observer.cross_bind_runtime_status_receipt(status, receipt) is status

    swapped = copy.deepcopy(status)
    swapped["priority_pressure"]["model_load_generation"] = 2
    with pytest.raises(health.GateAbort, match="receipt"):
        observer.cross_bind_runtime_status_receipt(swapped, receipt)


def test_artifact_creation_ledger_retains_transient_creation_counts() -> None:
    ledger = observer.ArtifactCreationLedger()
    assert ledger.snapshot(output_exists=False, marker_exists=False) == {
        "output_count": 0,
        "marker_count": 0,
        "output_create_count": 0,
        "marker_create_count": 0,
    }
    ledger.record("output")
    ledger.record("marker")
    assert ledger.snapshot(output_exists=False, marker_exists=False) == {
        "output_count": 0,
        "marker_count": 0,
        "output_create_count": 1,
        "marker_create_count": 1,
    }
    with pytest.raises(health.GateAbort, match="artifact"):
        ledger.record("foreign")
