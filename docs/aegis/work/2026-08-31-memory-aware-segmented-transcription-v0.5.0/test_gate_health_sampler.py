"""Regression tests for the Task 11B fail-closed health sampler.

These tests exercise owner-operated release-gate tooling only.  They are kept
beside the sampler so they cannot be mistaken for runtime image tests.
"""

from __future__ import annotations

import copy
import contextlib
import hashlib
import importlib.util
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SAMPLER_PATH = Path(__file__).with_name("gate_health_sampler.py")
MODULE_NAME = "subgen_task11b_gate_health_sampler"
MODULE_SPEC = importlib.util.spec_from_file_location(MODULE_NAME, SAMPLER_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("could not load gate health sampler")
sampler = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_NAME] = sampler
MODULE_SPEC.loader.exec_module(sampler)


def frigate_stats(
    *, process_fps: float = 10.0, skipped_fps: float = 0.0, now_wall: float
) -> dict[str, object]:
    """Return the smallest exact Frigate payload accepted by the validator."""
    return {
        "cameras": {
            "camera": {
                "process_fps": process_fps,
                "skipped_fps": skipped_fps,
            }
        },
        "detectors": {
            name: {"inference_speed": 12.5, "detection_start": 0}
            for name in sampler.REQUIRED_DETECTORS
        },
        "embeddings": {
            **{name: 4.5 for name in sampler.REQUIRED_EMBEDDING_SPEEDS},
            "yolov9_plate_detection_speed": 4.5,
            "yolov9_plate_detection": 1.0,
        },
        "service": {"last_updated": now_wall},
    }


def healthy_candidate_status(
    *,
    selected_model: str = "medium",
    reserve_bytes: int = 8 * sampler.GIB,
    priority_state: str = "clear",
    controller_phase: str = "normal",
    recovery_reason: str | None = None,
    admission_open: bool = True,
    model_resident: bool = True,
) -> dict[str, object]:
    priority = {
        "configured": True,
        "state": priority_state,
        "heartbeat_age_ms": 1_000,
        "source_age_ms": 2_000,
        "policy_sha256": "1" * 64,
        "observation_digest": "2" * 64,
        "transition_observation_digest": "3" * 64,
        "transition_sequence": 7,
        "controller_phase": controller_phase,
        "recovery_reason": recovery_reason,
        "distinct_clear_count": 3 if priority_state == "clear" else 0,
        "model_resident": model_resident,
        "model_load_generation": 4,
        "model_unload_generation": 3,
    }
    return {
        "resource_management": {
            "controller_state": controller_phase,
            "recovery_reason": recovery_reason,
            "admission_open": admission_open,
            "capacity_source": "cgroup_v2",
            "requested_model": "auto",
            "envelope_key": {
                "catalog_payload_sha256": "sha256:" + "4" * 64,
                "entry_index": 0,
            },
            "envelope_disposition": "exact_match",
            "envelope_reason": None,
            "selected_model": selected_model,
            "model_explicit": False,
            "automatic_ceiling": selected_model,
            "decision_reason": "selected",
            "decision_provenance": "envelope",
            "gpu_total_bytes": 24 * sampler.GIB,
            "gpu_stabilized_free_bytes": 18 * sampler.GIB,
            "gpu_reserve_bytes": reserve_bytes,
            "gpu_allocatable_bytes": 18 * sampler.GIB - reserve_bytes,
            "priority_pressure": priority,
            "workload": {
                "active": True,
                "chunk_uncommitted": False,
                "completion_generation": 0,
            },
            "runtime_identity": {
                "epoch": "4" * 32,
                "started_monotonic_ns": 123_456_789,
            },
            "failure_counters": {
                "cuda_oom_generation": 0,
                "media_failure_generation": 0,
            },
        }
    }


def healthy_memory(limit_bytes: int = 10 * sampler.GIB) -> dict[str, object]:
    return {
        "memory.current": 1024,
        "memory.peak": 2048,
        "memory.max": limit_bytes,
        "memory.swap.current": 0,
        "memory.swap.max": 0,
        "events": {key: 0 for key in sampler.REQUIRED_MEMORY_EVENTS},
        "pressure_observed_only": {
            category: {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0, "total": 0}
            for category in ("some", "full")
        },
    }


def runtime_receipt(
    sequence: int,
    *,
    observed_monotonic_ns: int | None = None,
    workload_sha256: str | None = "a" * 64,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema": "subgen.task11b.runtime-receipt/v1",
        "runtime_epoch": "4" * 32,
        "gate_token_sha256": "5" * 64,
        "sequence": sequence,
        "observed_monotonic_ns": observed_monotonic_ns or sequence * 1_000,
        "workload_sha256": workload_sha256,
        "source_generation": sequence,
        "observation_digest": "6" * 64,
        "transition_observation_digest": "7" * 64,
        "transition_sequence": 1,
        "heartbeat_age_ms": 100,
        "source_age_ms": 200,
        "policy_sha256": "8" * 64,
        "priority_state": "clear",
        "controller_phase": "normal",
        "recovery_reason": None,
        "admission_open": True,
        "distinct_clear_count": 3,
        "model_resident": True,
        "model_load_generation": 1,
        "model_unload_generation": 0,
        "active": True,
        "chunk_uncommitted": False,
        "active_cursor_ms": 0,
        "completed_cursor_ms": None,
        "completion_generation": 0,
        "model_identity_sha256": "9" * 64,
        "cuda_oom_generation": 0,
        "media_failure_generation": 0,
    }
    if workload_sha256 is None:
        receipt.update(
            active=False,
            chunk_uncommitted=False,
            active_cursor_ms=None,
        )
    return receipt


def valid_phase_a_document() -> dict[str, object]:
    runtime_epoch = "4" * 32
    policy = "d" * 64
    assertion_digest = "b" * 64
    model_identity = "c" * 64
    workload_identity = {
        "fixture_sha256": "1" * 64,
        "task": "translate",
        "language": "en",
        "cursor_start_ms": 100,
        "total_duration_ms": 1_000,
    }
    workload_sha256 = sampler.sha256_bytes(
        sampler.canonical_json_line(workload_identity)
    )
    times = [(index + 1) * 1_000_000_000 for index in range(10)]
    source_generations = [10, 11, 11, 11, 11, 12, 13, 14, 14, 14]
    clear_counts = [3, 0, 0, 0, 0, 1, 2, 3, 3, 3]
    states = ["clear"] + ["asserted"] * 4 + ["clear"] * 5
    phases = ["normal", "yielding"] + ["recovering"] * 5 + ["normal"] * 3
    admissions = [True, False, False, False, False, False, False, True, True, True]
    transition_sequences = [5] + [6] * 4 + [7] * 5
    event5_observation = "6" * 64
    transition_digests = ["0" * 64] + [assertion_digest] * 4 + [event5_observation] * 5
    events: list[dict[str, object]] = []
    kinds = (
        "pre_assertion",
        "assertion_consumed",
        "yielded",
        "unloaded",
        "unloaded_gpu",
        "clear_1",
        "clear_2",
        "clear_3",
        "reloaded",
        "completed",
    )
    for index, kind in enumerate(kinds):
        resident = index <= 2 or index >= 8
        observation = assertion_digest if 1 <= index <= 4 else f"{index + 1:x}" * 64
        if index == 5:
            observation = event5_observation
        events.append(
            {
                "event_index": index,
                "kind": kind,
                "monotonic_ns": times[index],
                "source_generation": source_generations[index],
                "observation_digest": observation,
                "runtime_epoch": runtime_epoch,
                "runtime_started_monotonic_ns": 1,
                "gate_receipt_sha256": f"{index:x}" * 64,
                "transition_observation_digest": transition_digests[index],
                "transition_sequence": transition_sequences[index],
                "heartbeat_age_ms": 100,
                "source_age_ms": 200,
                "policy_sha256": policy,
                "priority_state": states[index],
                "controller_phase": phases[index],
                "recovery_reason": None
                if phases[index] == "normal"
                else "priority_pressure",
                "admission_open": admissions[index],
                "distinct_clear_count": clear_counts[index],
                "model_resident": resident,
                "model_load_generation": 1 if index <= 7 else 2,
                "model_unload_generation": 0 if index <= 2 else 1,
                "cursor_ms": 100 if index < 9 else None,
                "last_completed_cursor_ms": 1_000 if index == 9 else None,
                "completion_generation": 6 if index == 9 else 5,
                "workload_active": index < 9,
                "chunk_uncommitted": index in {0, 1},
                "output_count": 1 if index == 9 else 0,
                "marker_count": 0,
                "output_create_count": 1 if index == 9 else 0,
                "marker_create_count": 0,
                "threshold_masking_allowed": 4 <= index <= 7,
                "candidate_bytes": 64 if index == 4 else 0,
                "model_identity_sha256": model_identity if resident else None,
                "cuda_oom_generation": 0,
                "media_failure_generation": 0,
            }
        )
    return {
        "schema": "subgen.task11b.phase-a/v1",
        "outcome": "pass",
        "policy_sha256": policy,
        "unloaded_gpu_envelope_sha256": "2" * 64,
        "workload_sha256": workload_sha256,
        "workload_identity": workload_identity,
        "candidate_identity_sha256": "3" * 64,
        "execution_boundary_manifest_sha256": "4" * 64,
        "gate_receipt_trace_sha256": "5" * 64,
        "runtime_epoch": runtime_epoch,
        "runtime_started_monotonic_ns": 1,
        "assertion_reason_codes": ["higher_priority_busy"],
        "assertion_observation_digest": assertion_digest,
        "assertion_observation_sha256": "6" * 64,
        "assertion_observed_monotonic_ns": 1_500_000_000,
        "t0_monotonic_ns": 2_000_000_000,
        "sealed_monotonic_ns": 11_000_000_000,
        "allowed_unloaded_bytes": 128,
        "events": events,
        "final_output_sha256": "7" * 64,
        "protected_first_sample_monotonic_ns": 1_000_000_000,
        "protected_last_sample_monotonic_ns": 5_000_000_000,
        "protected_sample_count": 3,
        "protected_blind_interval_count": 0,
        "protected_threshold_failure_count": 0,
        "candidate_restart_delta": 0,
        "candidate_oom_killed": False,
        "cgroup_oom_delta": 0,
        "cgroup_oom_kill_delta": 0,
        "cgroup_oom_group_kill_delta": 0,
        "runtime_cuda_oom_generation_delta": 0,
        "runtime_media_failure_generation_delta": 0,
        "candidate_cuda_oom_log_match_delta": 0,
        "nvidia_xid_log_match_delta": 0,
    }


def valid_phase_b_document() -> dict[str, object]:
    candidate_identity = CanonicalGateArtifactTests.candidate_identity()
    candidate_sha256 = sampler.sha256_bytes(
        sampler.canonical_json_line(candidate_identity)
    )
    workload_identity = {
        "fixture_sha256": "8" * 64,
        "task": "translate",
        "language": "en",
        "cursor_start_ms": 0,
        "total_duration_ms": 2_000_000,
    }
    workload_sha256 = sampler.sha256_bytes(
        sampler.canonical_json_line(workload_identity)
    )
    started = 20_000_000_000
    runtime_epoch = "4" * 32
    producer_epoch = "5" * 32
    policy = "d" * 64
    model_identity = "c" * 64
    samples: list[dict[str, object]] = []
    for index in range(181):
        samples.append(
            {
                "sample_index": index,
                "scheduled_offset_seconds": index * 5,
                "captured_monotonic_ns": started + index * 5_000_000_000,
                "source_generation": 100 + index,
                "policy_sha256": policy,
                "producer_epoch": producer_epoch,
                "runtime_epoch": runtime_epoch,
                "runtime_started_monotonic_ns": 1,
                "candidate_identity_sha256": candidate_sha256,
                "gate_receipt_sha256": f"{(index % 15) + 1:x}" * 64,
                "model_identity_sha256": model_identity,
                "observation_digest": f"{(index % 15) + 1:x}" * 64,
                "transition_observation_digest": "a" * 64,
                "transition_sequence": 7,
                "heartbeat_age_ms": 100,
                "source_age_ms": 200,
                "priority_state": "clear",
                "controller_phase": "normal",
                "recovery_reason": None,
                "admission_open": True,
                "candidate_running": True,
                "workload_active": True,
                "distinct_clear_count": 3,
                "model_resident": True,
                "model_load_generation": 2,
                "model_unload_generation": 1,
                "completion_generation": 6,
                "cuda_oom_generation": 0,
                "media_failure_generation": 0,
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
        )
    return {
        "schema": "subgen.task11b.phase-b/v1",
        "outcome": "pass",
        "started_monotonic_ns": started,
        "ended_monotonic_ns": started + 900_000_000_000,
        "phase_a_seal_sha256": "1" * 64,
        "phase_a_durable_monotonic_ns": 18_000_000_000,
        "reset_completed_monotonic_ns": 19_000_000_000,
        "runtime_epoch": runtime_epoch,
        "runtime_started_monotonic_ns": 1,
        "sample_interval_seconds": 5,
        "policy_sha256": policy,
        "producer_epoch_digest": sampler.sha256_bytes(producer_epoch.encode("ascii")),
        "producer_epoch": producer_epoch,
        "candidate_identity_sha256": candidate_sha256,
        "candidate_identity": candidate_identity,
        "execution_boundary_manifest_sha256": "2" * 64,
        "workload_sha256": workload_sha256,
        "workload_identity": workload_identity,
        "gate_receipt_trace_sha256": "3" * 64,
        "model_identity_sha256": model_identity,
        "samples": samples,
    }


def valid_unloaded_gpu_envelope() -> dict[str, object]:
    samples = [0] * 9 + [index * 10 for index in (1, 2, 3)]
    cycles = []
    for index in range(3):
        cycle_samples = [0] * 9 + [samples[9 + index]]
        cycles.append(
            {
                "cycle_index": index + 1,
                "container_id_sha256": f"{index + 1:x}" * 64,
                "load_generation_before": 0,
                "load_generation_after": 1,
                "inference_completed": True,
                "inference_result_sha256": f"{index + 4:x}" * 64,
                "unload_generation_before": 0,
                "unload_generation_after": 1,
                "candidate_bytes_samples": cycle_samples,
            }
        )
    maximum = 30
    return {
        "schema": "subgen.unloaded-gpu-envelope/v1",
        "runtime_commit": "a" * 40,
        "image": {
            "oci_index": "sha256:" + "b" * 64,
            "config_digest": "sha256:" + "c" * 64,
            "layer_diff_ids": ["sha256:" + "d" * 64],
        },
        "gpu": {
            "uuid": "GPU-11111111-2222-3333-4444-555555555555",
            "driver_version": "580.97",
        },
        "backend": {
            "cuda_version": "12.8",
            "ctranslate2_version": "4.6.0",
            "stable_ts_version": "2.19.1",
            "generator_sha256": "e" * 64,
        },
        "model_policy": {
            "selected_model": "large-v3",
            "model_revision": "hf:" + "f" * 40,
            "compute_type": "float16",
            "device": "cuda",
            "device_index": 0,
            "task": "translate",
            "language": "en",
            "chunk_seconds": 300,
            "overlap_seconds": 5,
            "fixture_sha256": "6" * 64,
            "priority_policy_sha256": "7" * 64,
        },
        "measurement": {
            "cycles": cycles,
            "cycle_count": 3,
            "samples_per_cycle": 10,
            "interval_seconds": 1,
            "margin_bytes": 134_217_728,
            "max_observed_candidate_bytes": maximum,
            "allowed_unloaded_bytes": maximum + 134_217_728,
        },
    }


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, delay: float) -> None:
        self.value += delay


class DockerInspectTemplateTests(unittest.TestCase):
    def test_optional_docker_29_map_keys_use_missing_key_safe_indexing(self) -> None:
        template = sampler.INSPECT_TEMPLATE

        self.assertNotIn(".State.Health", template)
        self.assertNotIn(".HostConfig.Tmpfs", template)
        self.assertIn('{{with (index .State "Health")}}', template)
        self.assertIn('{{json (index . "Status")}}', template)
        self.assertIn('{{json (index .HostConfig "Tmpfs")}}', template)


class FrigateTimingTests(unittest.TestCase):
    def test_idle_conditional_embedding_metrics_may_be_absent_as_a_pair(self) -> None:
        stats = frigate_stats(now_wall=1_000.0)
        del stats["embeddings"]["yolov9_plate_detection_speed"]
        del stats["embeddings"]["yolov9_plate_detection"]

        metrics = sampler.validate_frigate_stats(
            stats,
            {"camera": 10.0},
            {"camera": None},
            now_monotonic=100.0,
            now_wall=1_000.0,
        )

        self.assertEqual(metrics["embedding_metric_count"], 2)
        self.assertEqual(metrics["embedding_conditional_idle_count"], 1)

    def test_active_conditional_embedding_metrics_are_validated_as_a_pair(self) -> None:
        metrics = sampler.validate_frigate_stats(
            frigate_stats(now_wall=1_000.0),
            {"camera": 10.0},
            {"camera": None},
            now_monotonic=100.0,
            now_wall=1_000.0,
        )

        self.assertEqual(metrics["embedding_metric_count"], 3)
        self.assertEqual(metrics["embedding_conditional_idle_count"], 0)

    def test_half_present_conditional_embedding_metrics_abort(self) -> None:
        for missing in (
            "yolov9_plate_detection_speed",
            "yolov9_plate_detection",
        ):
            with self.subTest(missing=missing):
                stats = frigate_stats(now_wall=1_000.0)
                del stats["embeddings"][missing]

                with self.assertRaisesRegex(
                    sampler.GateAbort, "conditional_embedding_telemetry_was_incomplete"
                ):
                    sampler.validate_frigate_stats(
                        stats,
                        {"camera": 10.0},
                        {"camera": None},
                        now_monotonic=100.0,
                        now_wall=1_000.0,
                    )

    def test_invalid_active_conditional_embedding_metrics_abort(self) -> None:
        for key in (
            "yolov9_plate_detection_speed",
            "yolov9_plate_detection",
        ):
            for value in (None, 0.0, -1.0, float("inf"), float("nan"), True, "bad"):
                with self.subTest(key=key, value=value):
                    stats = frigate_stats(now_wall=1_000.0)
                    stats["embeddings"][key] = value
                    with self.assertRaises(sampler.GateAbort):
                        sampler.validate_frigate_stats(
                            stats,
                            {"camera": 10.0},
                            {"camera": None},
                            now_monotonic=100.0,
                            now_wall=1_000.0,
                        )

    def test_low_fps_aborts_only_after_strictly_more_than_30_seconds(self) -> None:
        expectations = {"camera": 10.0}
        low_since: dict[str, float | None] = {"camera": None}

        first = sampler.validate_frigate_stats(
            frigate_stats(process_fps=8.9, now_wall=1_000.0),
            expectations,
            low_since,
            now_monotonic=100.0,
            now_wall=1_000.0,
        )
        exact_boundary = sampler.validate_frigate_stats(
            frigate_stats(process_fps=8.9, now_wall=1_030.0),
            expectations,
            low_since,
            now_monotonic=130.0,
            now_wall=1_030.0,
        )

        self.assertEqual(first["camera_longest_low_seconds"], 0.0)
        self.assertEqual(exact_boundary["camera_longest_low_seconds"], 30.0)
        with self.assertRaisesRegex(sampler.GateAbort, "over_30_seconds"):
            sampler.validate_frigate_stats(
                frigate_stats(process_fps=8.9, now_wall=1_030.001),
                expectations,
                low_since,
                now_monotonic=130.001,
                now_wall=1_030.001,
            )

    def test_immediate_repeated_low_fps_calls_do_not_count_as_elapsed_time(
        self,
    ) -> None:
        expectations = {"camera": 10.0}
        low_since: dict[str, float | None] = {"camera": None}

        for _ in range(100):
            metrics = sampler.validate_frigate_stats(
                frigate_stats(process_fps=8.0, now_wall=5_000.0),
                expectations,
                low_since,
                now_monotonic=250.0,
                now_wall=5_000.0,
            )

        self.assertEqual(low_since["camera"], 250.0)
        self.assertEqual(metrics["camera_longest_low_seconds"], 0.0)

    def test_recovery_resets_the_continuous_low_fps_timer(self) -> None:
        expectations = {"camera": 10.0}
        low_since: dict[str, float | None] = {"camera": None}
        sampler.validate_frigate_stats(
            frigate_stats(process_fps=8.0, now_wall=2_000.0),
            expectations,
            low_since,
            now_monotonic=10.0,
            now_wall=2_000.0,
        )
        sampler.validate_frigate_stats(
            frigate_stats(process_fps=10.0, now_wall=2_020.0),
            expectations,
            low_since,
            now_monotonic=30.0,
            now_wall=2_020.0,
        )
        recovered = sampler.validate_frigate_stats(
            frigate_stats(process_fps=8.0, now_wall=2_040.0),
            expectations,
            low_since,
            now_monotonic=50.0,
            now_wall=2_040.0,
        )

        self.assertEqual(low_since["camera"], 50.0)
        self.assertEqual(recovered["camera_longest_low_seconds"], 0.0)


class SamplingCadenceTests(unittest.TestCase):
    def test_gate_takes_a_fresh_sample_at_exactly_900_seconds(self) -> None:
        clock = FakeClock()
        observed: list[float] = []

        count, elapsed = sampler.run_sampling_loop(
            duration_seconds=900.0,
            interval_seconds=5.0,
            sample=lambda _number, value: not observed.append(value),
            clock=clock.now,
            sleeper=clock.sleep,
        )

        self.assertEqual(count, 181)
        self.assertEqual(elapsed, 900.0)
        self.assertEqual(observed[-1], 900.0)

    def test_stale_candidate_status_at_900_requires_a_later_fresh_sample(self) -> None:
        clock = FakeClock()
        observed: list[float] = []

        def sample(_number: int, elapsed: float) -> bool:
            observed.append(elapsed)
            return elapsed != 900.0

        count, elapsed = sampler.run_sampling_loop(
            duration_seconds=900.0,
            interval_seconds=5.0,
            sample=sample,
            clock=clock.now,
            sleeper=clock.sleep,
        )

        self.assertEqual(count, 182)
        self.assertEqual(elapsed, 905.0)
        self.assertEqual(observed[-2:], [900.0, 905.0])

    def test_missed_cadence_aborts_instead_of_catching_up(self) -> None:
        clock = FakeClock()

        def slow_sample(_number: int, _elapsed: float) -> bool:
            clock.value += 8.0
            return True

        with self.assertRaisesRegex(sampler.GateAbort, "work_exceeded_cadence"):
            sampler.run_sampling_loop(
                duration_seconds=900.0,
                interval_seconds=5.0,
                sample=slow_sample,
                clock=clock.now,
                sleeper=clock.sleep,
            )

    def test_oversleeping_the_schedule_aborts_before_an_extra_sample(self) -> None:
        clock = FakeClock()
        calls = 0

        def sample(_number: int, _elapsed: float) -> bool:
            nonlocal calls
            calls += 1
            return True

        def oversleep(delay: float) -> None:
            clock.value += delay + sampler.MAX_SAMPLE_LAG_SECONDS + 0.001

        with self.assertRaisesRegex(sampler.GateAbort, "cadence_lag_exceeded"):
            sampler.run_sampling_loop(
                duration_seconds=900.0,
                interval_seconds=5.0,
                sample=sample,
                clock=clock.now,
                sleeper=oversleep,
            )
        self.assertEqual(calls, 1)


class StrictTelemetryParserTests(unittest.TestCase):
    def test_memory_events_require_each_exact_key_once(self) -> None:
        valid = "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"
        parsed = sampler.parse_key_value_lines(
            valid,
            "memory events",
            required_keys=sampler.REQUIRED_MEMORY_EVENTS,
        )
        self.assertEqual(set(parsed), sampler.REQUIRED_MEMORY_EVENTS)

        malformed = {
            "duplicate": valid + "low 1\n",
            "missing": valid.replace("oom_group_kill 0\n", ""),
            "unexpected": valid + "pressure 0\n",
        }
        for label, payload in malformed.items():
            with self.subTest(label=label), self.assertRaises(sampler.GateAbort):
                sampler.parse_key_value_lines(
                    payload,
                    "memory events",
                    required_keys=sampler.REQUIRED_MEMORY_EVENTS,
                )

    def test_baseline_memory_max_event_must_be_zero(self) -> None:
        memory = {
            "memory.max": 10 * sampler.GIB,
            "memory.swap.max": 0,
            "events": {key: 0 for key in sampler.REQUIRED_MEMORY_EVENTS},
        }
        sampler.validate_candidate_memory_snapshot(
            memory, expected_memory_bytes=10 * sampler.GIB
        )
        memory["events"]["max"] = 1
        with self.assertRaisesRegex(sampler.GateAbort, "memory_limit_event"):
            sampler.validate_candidate_memory_snapshot(
                memory, expected_memory_bytes=10 * sampler.GIB
            )

    def test_malformed_psi_is_rejected_fail_closed(self) -> None:
        malformed = {
            "missing row": "some avg10=0 avg60=0 avg300=0 total=0\n",
            "duplicate row": (
                "some avg10=0 avg60=0 avg300=0 total=0\n"
                "some avg10=0 avg60=0 avg300=0 total=0\n"
            ),
            "non finite": (
                "some avg10=nan avg60=0 avg300=0 total=0\n"
                "full avg10=0 avg60=0 avg300=0 total=0\n"
            ),
            "negative": (
                "some avg10=0 avg60=0 avg300=0 total=-1\n"
                "full avg10=0 avg60=0 avg300=0 total=0\n"
            ),
            "wrong field": (
                "some avg10=0 avg60=0 avg900=0 total=0\n"
                "full avg10=0 avg60=0 avg300=0 total=0\n"
            ),
        }
        for label, payload in malformed.items():
            with self.subTest(label=label), self.assertRaises(sampler.GateAbort):
                sampler.parse_psi(payload, "test PSI")

    def test_url_confinement_is_exact_and_precedes_network_access(self) -> None:
        exact = sampler.EXACT_ENDPOINTS["candidate"]
        sampler.require_exact_endpoint(exact, "candidate")
        variants = (
            "http://localhost:19000/status",
            "http://127.0.0.1:19000/status/",
            "http://127.0.0.1:19000/status?full=true",
            "http://127.0.0.1:19000/status#fragment",
            "http://user@127.0.0.1:19000/status",
            "http://127.0.0.1:19001/status",
            "https://127.0.0.1:19000/status",
        )
        for url in variants:
            with self.subTest(url=url), self.assertRaises(sampler.GateAbort):
                sampler.require_exact_endpoint(url, "candidate")

        with mock.patch.object(sampler.socket, "socket") as socket_factory:
            with self.assertRaises(sampler.GateAbort):
                sampler.fetch_json(variants[0], endpoint="candidate")
            socket_factory.assert_not_called()

    def test_json_rejects_duplicate_keys_and_oversized_payloads(self) -> None:
        with self.assertRaisesRegex(sampler.GateAbort, "duplicate_keys"):
            sampler.strict_json_object(b'{"outer":{"key":1,"key":2}}', label="test")
        with self.assertRaisesRegex(sampler.GateAbort, "byte_limit"):
            sampler.strict_json_object(b'{"key":1}', label="test", max_bytes=4)
        with self.assertRaisesRegex(sampler.GateAbort, "not_an_object"):
            sampler.strict_json_object(b"[]", label="test")

    def test_http_slow_drip_cannot_extend_the_total_deadline(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}/status"

        def serve() -> None:
            try:
                connection, _address = listener.accept()
                with connection:
                    connection.recv(4096)
                    body = b'{"ok":true}'
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: "
                        + str(len(body)).encode("ascii")
                        + b"\r\nConnection: close\r\n\r\n"
                    )
                    for byte in body:
                        time.sleep(0.08)
                        connection.sendall(bytes((byte,)))
            except OSError:
                pass
            finally:
                listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        started = time.monotonic()
        with mock.patch.dict(sampler.EXACT_ENDPOINTS, {"candidate": url}, clear=False):
            with self.assertRaisesRegex(
                sampler.TelemetryUnavailable, "deadline_expired"
            ):
                sampler.fetch_json(url, endpoint="candidate", timeout=0.15)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.6)
        thread.join(timeout=1.0)

    def test_http_nonblocking_reader_accepts_one_bounded_exact_response(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}/status"

        def serve() -> None:
            try:
                connection, _address = listener.accept()
                with connection:
                    connection.recv(4096)
                    body = b'{"ok":true}'
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: "
                        + str(len(body)).encode("ascii")
                        + b"\r\nConnection: close\r\n\r\n"
                        + body
                    )
            finally:
                listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with mock.patch.dict(sampler.EXACT_ENDPOINTS, {"candidate": url}, clear=False):
            self.assertEqual(
                sampler.fetch_json(url, endpoint="candidate", timeout=1.0),
                {"ok": True},
            )
        thread.join(timeout=1.0)


class CandidateStatusTests(unittest.TestCase):
    def test_exact_automatic_medium_envelope_is_accepted(self) -> None:
        result = sampler.validate_candidate_status(
            healthy_candidate_status(),
            expected_model="medium",
            expected_reserve_bytes=8 * sampler.GIB,
        )

        self.assertEqual(result["selected_model"], "medium")
        self.assertEqual(result["requested_model"], "auto")
        self.assertFalse(result["model_explicit"])
        self.assertTrue(result["admission_open"])

    def test_atomic_priority_runtime_workload_and_failure_schema_is_required(
        self,
    ) -> None:
        result = sampler.validate_candidate_status(
            healthy_candidate_status(),
            expected_model="medium",
            expected_reserve_bytes=8 * sampler.GIB,
            expected_priority_state="clear",
            expected_policy_sha256="1" * 64,
            require_gate_runtime=True,
        )

        self.assertEqual(result["priority_pressure"]["state"], "clear")
        self.assertEqual(result["runtime_identity"]["epoch"], "4" * 32)
        self.assertEqual(result["failure_counters"]["cuda_oom_generation"], 0)
        for missing in (
            "priority_pressure",
            "workload",
            "runtime_identity",
            "failure_counters",
        ):
            payload = healthy_candidate_status()
            del payload["resource_management"][missing]
            with self.subTest(missing=missing), self.assertRaises(sampler.GateAbort):
                sampler.validate_candidate_status(
                    payload,
                    expected_model="medium",
                    expected_reserve_bytes=8 * sampler.GIB,
                    require_gate_runtime=True,
                )


class RuntimeReceiptTests(unittest.TestCase):
    @staticmethod
    def encoded(receipt: dict[str, object]) -> bytes:
        return sampler.canonical_json_line(receipt)

    def test_receipt_schema_rejects_boolean_integer_and_noncanonical_bytes(
        self,
    ) -> None:
        valid = runtime_receipt(1)
        parsed = sampler.validate_runtime_receipt(self.encoded(valid))
        self.assertEqual(parsed["sequence"], 1)

        invalid = copy.deepcopy(valid)
        invalid["sequence"] = True
        with self.assertRaisesRegex(sampler.GateAbort, "receipt.*integer"):
            sampler.validate_runtime_receipt(self.encoded(invalid))
        with self.assertRaisesRegex(sampler.GateAbort, "canonical"):
            sampler.validate_runtime_receipt(
                json.dumps(valid, indent=2).encode("ascii") + b"\n"
            )
        with self.assertRaisesRegex(sampler.GateAbort, "duplicate"):
            sampler.validate_runtime_receipt(
                self.encoded(valid).replace(
                    b'"sequence":1', b'"sequence":1,"sequence":1'
                )
            )

    @unittest.skipUnless(os.name == "posix", "inode/mode journal proof requires POSIX")
    def test_journal_tails_every_record_between_polls_and_rejects_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "private"
            parent.mkdir(mode=0o700)
            os.chmod(parent, 0o700)
            path = parent / "runtime-receipts.jsonl"
            path.write_bytes(self.encoded(runtime_receipt(1, workload_sha256=None)))
            os.chmod(path, 0o600)
            journal = sampler.RuntimeReceiptJournal.open(
                path,
                expected_runtime_epoch="4" * 32,
                expected_token_sha256="5" * 64,
            )
            try:
                self.assertEqual(
                    [item["sequence"] for item in journal.read_available()], [1]
                )
                with path.open("ab", buffering=0) as destination:
                    destination.write(self.encoded(runtime_receipt(2)))
                    destination.write(self.encoded(runtime_receipt(3)))
                    os.fsync(destination.fileno())
                self.assertEqual(
                    [item["sequence"] for item in journal.read_available()],
                    [2, 3],
                )
                replacement = parent / "replacement"
                replacement.write_bytes(self.encoded(runtime_receipt(4)))
                os.chmod(replacement, 0o600)
                os.replace(replacement, path)
                with self.assertRaisesRegex(sampler.GateAbort, "replaced"):
                    journal.read_available()
            finally:
                journal.close()

    @unittest.skipUnless(os.name == "posix", "inode/mode journal proof requires POSIX")
    def test_journal_rejects_partial_gap_and_truncation(self) -> None:
        corruptions = {
            "partial": self.encoded(runtime_receipt(1)) + b'{"schema":',
            "gap": self.encoded(runtime_receipt(1)) + self.encoded(runtime_receipt(3)),
        }
        for label, payload in corruptions.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                parent = Path(temporary) / "private"
                parent.mkdir(mode=0o700)
                os.chmod(parent, 0o700)
                path = parent / "runtime-receipts.jsonl"
                path.write_bytes(payload)
                os.chmod(path, 0o600)
                journal = sampler.RuntimeReceiptJournal.open(
                    path,
                    expected_runtime_epoch="4" * 32,
                    expected_token_sha256="5" * 64,
                )
                try:
                    with self.assertRaisesRegex(sampler.GateAbort, label):
                        journal.read_available()
                finally:
                    journal.close()

    def test_asserted_status_is_validated_without_normalizing_it_to_clear(self) -> None:
        payload = healthy_candidate_status(
            priority_state="asserted",
            controller_phase="yielding",
            recovery_reason="priority_pressure",
            admission_open=False,
        )

        result = sampler.validate_candidate_status(
            payload,
            expected_model="medium",
            expected_reserve_bytes=8 * sampler.GIB,
            expected_priority_state="asserted",
            expected_controller_phase="yielding",
            expected_recovery_reason="priority_pressure",
            expected_admission_open=False,
            require_gate_runtime=True,
        )

        self.assertEqual(result["priority_pressure"]["state"], "asserted")
        self.assertEqual(result["controller_state"], "yielding")

    def test_highest_qualified_model_and_audited_reserve_are_arguments(self) -> None:
        reserve = 7 * sampler.GIB
        result = sampler.validate_candidate_status(
            healthy_candidate_status(
                selected_model="large-v3",
                reserve_bytes=reserve,
            ),
            expected_model="large-v3",
            expected_reserve_bytes=reserve,
            require_gate_runtime=True,
        )

        self.assertEqual(result["selected_model"], "large-v3")
        self.assertEqual(result["gpu_reserve_bytes"], reserve)

    def test_uninitialized_candidate_status_is_distinct_from_unhealthy(self) -> None:
        for payload in (
            {},
            {"resource_management": {}},
            {"resource_management": {"selected_model": None}},
        ):
            with (
                self.subTest(payload=payload),
                self.assertRaises(sampler.CandidateNotReady),
            ):
                sampler.validate_candidate_status(
                    payload,
                    expected_model="medium",
                    expected_reserve_bytes=8 * sampler.GIB,
                )

    def test_any_departure_from_exact_healthy_envelope_aborts(self) -> None:
        changes = {
            "selected_model": "small",
            "requested_model": "medium",
            "model_explicit": True,
            "envelope_disposition": "degraded",
            "decision_provenance": "heuristic",
            "gpu_reserve_bytes": 0,
            "controller_state": "yielding",
            "recovery_reason": "pressure",
            "admission_open": False,
            "envelope_reason": "fallback",
            "capacity_source": "host",
            "envelope_key": None,
            "automatic_ceiling": "small",
            "decision_reason": "fallback",
            "gpu_total_bytes": 0,
            "gpu_stabilized_free_bytes": 25 * sampler.GIB,
            "gpu_allocatable_bytes": 9 * sampler.GIB,
        }
        for key, value in changes.items():
            payload = copy.deepcopy(healthy_candidate_status())
            resource = payload["resource_management"]
            assert isinstance(resource, dict)
            resource[key] = value
            with self.subTest(key=key), self.assertRaises(sampler.GateAbort):
                sampler.validate_candidate_status(
                    payload,
                    expected_model="medium",
                    expected_reserve_bytes=8 * sampler.GIB,
                )


class CanonicalGateArtifactTests(unittest.TestCase):
    @staticmethod
    def candidate_identity() -> dict[str, object]:
        return {
            "container_id": "a" * 64,
            "runtime_commit": "b" * 40,
            "oci_index": "sha256:" + "c" * 64,
            "config_digest": "sha256:" + "d" * 64,
            "layer_diff_ids": ["sha256:" + "e" * 64],
            "selected_model": "large-v3",
            "model_revision": "hf:" + "f" * 40,
        }

    @classmethod
    def candidate_document(cls) -> dict[str, object]:
        return {
            "schema": "subgen.task11b.candidate-identity/v2",
            "candidate_identity": cls.candidate_identity(),
            "docker_daemon_identity_sha256": "4" * 64,
            "execution_boundary_manifest_sha256": "1" * 64,
            "gate_token_sha256": "2" * 64,
            "intended_command_sha256": "3" * 64,
            "created_stopped": True,
        }

    @staticmethod
    def final_document() -> dict[str, object]:
        document: dict[str, object] = {
            key: str(index % 10) * 64
            for index, key in enumerate(
                sorted(
                    sampler.FINAL_GATE_KEYS
                    - {
                        "schema",
                        "outcome",
                        "runtime_commit",
                        "candidate_oci_index",
                        "candidate_config_digest",
                        "cleanup",
                    }
                ),
                start=1,
            )
        }
        document.update(
            schema="subgen.task11b.shared-gpu-gate/v4",
            outcome="pass",
            runtime_commit="a" * 40,
            candidate_oci_index="sha256:" + "b" * 64,
            candidate_config_digest="sha256:" + "c" * 64,
            cleanup={
                "verified_stopped": True,
                "candidate_pid_count": 0,
                "execution_boundary_revalidated": True,
            },
        )
        return document

    def test_candidate_final_and_trace_schemas_are_exact(self) -> None:
        candidate = self.candidate_document()
        self.assertIs(
            sampler.validate_candidate_identity_document(candidate), candidate
        )
        final = self.final_document()
        self.assertIs(sampler.validate_final_gate_document(final), final)
        self.assertIn("model_envelope_catalog_sha256", final)

        phase_a_trace = {
            "schema": "subgen.task11b.runtime-receipt-trace/v1",
            "runtime_epoch": "4" * 32,
            "gate_token_sha256": "5" * 64,
            "workload_sha256": "a" * 64,
            "receipts": [
                runtime_receipt(1, workload_sha256=None),
                runtime_receipt(2),
            ],
        }
        self.assertIs(
            sampler.validate_runtime_receipt_trace_document(phase_a_trace),
            phase_a_trace,
        )
        corrupted = copy.deepcopy(phase_a_trace)
        corrupted["receipts"][1]["sequence"] = 3
        with self.assertRaisesRegex(sampler.GateAbort, "gap"):
            sampler.validate_runtime_receipt_trace_document(corrupted)

        phase_b_pre_admission = runtime_receipt(3, workload_sha256="b" * 64)
        phase_b_pre_admission.update(
            active=False,
            chunk_uncommitted=False,
            active_cursor_ms=None,
            completed_cursor_ms=1_000,
        )
        phase_b_trace = {
            "schema": "subgen.task11b.phase-b-runtime-receipt-trace/v1",
            "runtime_epoch": "4" * 32,
            "gate_token_sha256": "5" * 64,
            "phase_a_trace_sha256": "6" * 64,
            "phase_a_last_sequence": 2,
            "workload_sha256": "a" * 64,
            "receipts": [phase_b_pre_admission, runtime_receipt(4)],
        }
        self.assertIs(
            sampler.validate_phase_b_receipt_trace_document(phase_b_trace),
            phase_b_trace,
        )

    def test_installed_catalog_canonical_form_has_no_trailing_newline(self) -> None:
        catalog = {"entries": [], "schema": "subgen.model-envelope.catalog/v1"}
        payload = sampler._canonical_json_bytes(catalog)

        self.assertEqual(
            sampler.validate_model_envelope_catalog_bytes(payload), catalog
        )
        with self.assertRaisesRegex(sampler.GateAbort, "canonical"):
            sampler.validate_model_envelope_catalog_bytes(payload + b"\n")

    def test_bool_coercions_extra_keys_and_missing_catalog_binding_fail(self) -> None:
        candidate = self.candidate_document()
        candidate["created_stopped"] = 1
        with self.assertRaises(sampler.GateAbort):
            sampler.validate_candidate_identity_document(candidate)

        final = self.final_document()
        del final["model_envelope_catalog_sha256"]
        with self.assertRaises(sampler.GateAbort):
            sampler.validate_final_gate_document(final)

        final = self.final_document()
        final["unexpected"] = "0" * 64
        with self.assertRaises(sampler.GateAbort):
            sampler.validate_final_gate_document(final)

        candidate = self.candidate_document()
        identity = candidate["candidate_identity"]
        assert isinstance(identity, dict)
        identity["model_revision"] = "f" * 40
        with self.assertRaisesRegex(sampler.GateAbort, "model_revision"):
            sampler.validate_candidate_identity_document(candidate)

    def test_phase_a_and_phase_b_exact_causal_documents_validate(self) -> None:
        phase_a = valid_phase_a_document()
        phase_b = valid_phase_b_document()

        self.assertIs(sampler.validate_phase_a_document(phase_a), phase_a)
        self.assertIs(sampler.validate_phase_b_document(phase_b), phase_b)

    def test_phase_a_rejects_each_independent_failure_source_and_masking_bypass(
        self,
    ) -> None:
        for key in (
            "candidate_restart_delta",
            "cgroup_oom_delta",
            "cgroup_oom_kill_delta",
            "cgroup_oom_group_kill_delta",
            "runtime_cuda_oom_generation_delta",
            "runtime_media_failure_generation_delta",
            "candidate_cuda_oom_log_match_delta",
            "nvidia_xid_log_match_delta",
        ):
            phase = valid_phase_a_document()
            phase[key] = 1
            with self.subTest(key=key), self.assertRaises(sampler.GateAbort):
                sampler.validate_phase_a_document(phase)
        phase = valid_phase_a_document()
        events = phase["events"]
        assert isinstance(events, list) and isinstance(events[3], dict)
        events[3]["threshold_masking_allowed"] = True
        with self.assertRaisesRegex(sampler.GateAbort, "masking"):
            sampler.validate_phase_a_document(phase)

        phase = valid_phase_a_document()
        phase["protected_sample_count"] = 2
        with self.assertRaisesRegex(sampler.GateAbort, "protected.*count"):
            sampler.validate_phase_a_document(phase)

        phase = valid_phase_a_document()
        events = phase["events"]
        assert isinstance(events, list)
        assert isinstance(events[5], dict) and isinstance(events[6], dict)
        events[6]["observation_digest"] = events[5]["observation_digest"]
        with self.assertRaisesRegex(sampler.GateAbort, "clear.*digest"):
            sampler.validate_phase_a_document(phase)

    def test_phase_b_rejects_catch_up_health_relaxation_and_bool_integer(self) -> None:
        mutations = {
            "catch_up": (2, "captured_monotonic_ns", 25_000_000_000),
            "detection": (10, "detection_fps", 80.0),
            "ratio": (10, "camera_min_process_ratio", 0.979),
            "skip": (10, "camera_max_skipped_fps", 0.1),
            "numeric_string": (10, "detection_fps", "79.0"),
            "bool_integer": (1, "sample_index", True),
            "restart": (10, "candidate_restart_delta", 1),
        }
        for label, (index, key, value) in mutations.items():
            phase = valid_phase_b_document()
            samples = phase["samples"]
            assert isinstance(samples, list) and isinstance(samples[index], dict)
            samples[index][key] = value
            with self.subTest(label=label), self.assertRaises(sampler.GateAbort):
                sampler.validate_phase_b_document(phase)

    def test_unloaded_envelope_and_model_identity_are_recomputed(self) -> None:
        envelope = valid_unloaded_gpu_envelope()
        self.assertIs(
            sampler.validate_unloaded_gpu_envelope_document(envelope), envelope
        )
        entry = {"model": "large-v3", "policy": {"compute_type": "float16"}}
        policy = entry["policy"]
        assert isinstance(policy, dict)
        expected = sampler.sha256_bytes(
            sampler.canonical_json_line(
                {
                    "catalog_entry_sha256": sampler.sha256_bytes(
                        sampler.canonical_json_line(entry)
                    ),
                    "model_policy_sha256": sampler.sha256_bytes(
                        sampler.canonical_json_line(policy)
                    ),
                    "model_revision": "hf:" + "f" * 40,
                    "selected_model": "large-v3",
                }
            )
        )
        self.assertEqual(
            sampler.compute_model_identity_sha256(
                entry,
                policy,
                model_revision="hf:" + "f" * 40,
                selected_model="large-v3",
            ),
            expected,
        )

    def test_unloaded_envelope_rejects_measurement_substitution(self) -> None:
        mutations = {
            "allowed": ("allowed_unloaded_bytes", 134_217_728),
            "maximum": ("max_observed_candidate_bytes", 0),
            "bool_count": ("cycle_count", True),
        }
        for label, (key, value) in mutations.items():
            envelope = valid_unloaded_gpu_envelope()
            measurement = envelope["measurement"]
            assert isinstance(measurement, dict)
            measurement[key] = value
            with self.subTest(label=label), self.assertRaises(sampler.GateAbort):
                sampler.validate_unloaded_gpu_envelope_document(envelope)
        envelope = valid_unloaded_gpu_envelope()
        measurement = envelope["measurement"]
        assert isinstance(measurement, dict)
        cycles = measurement["cycles"]
        assert isinstance(cycles, list) and isinstance(cycles[1], dict)
        cycles[1]["container_id_sha256"] = "1" * 64
        with self.assertRaisesRegex(sampler.GateAbort, "distinct"):
            sampler.validate_unloaded_gpu_envelope_document(envelope)

    def test_gpu_attribution_requires_stable_pid_set_and_bound_uuid(self) -> None:
        raw = (
            "101, GPU-11111111-2222-3333-4444-555555555555, 10\n"
            "999, GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee, 20\n"
        )
        self.assertEqual(
            sampler.attribute_candidate_gpu_bytes(
                raw,
                before_pids={101},
                after_pids={101},
                expected_gpu_uuid="GPU-11111111-2222-3333-4444-555555555555",
            ),
            10 * sampler.MIB,
        )
        with self.assertRaisesRegex(sampler.GateAbort, "changed"):
            sampler.attribute_candidate_gpu_bytes(
                raw,
                before_pids={101},
                after_pids={101, 102},
                expected_gpu_uuid="GPU-11111111-2222-3333-4444-555555555555",
            )
        with self.assertRaisesRegex(sampler.GateAbort, "another_gpu"):
            sampler.attribute_candidate_gpu_bytes(
                raw.replace(
                    "GPU-11111111-2222-3333-4444-555555555555",
                    "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    1,
                ),
                before_pids={101},
                after_pids={101},
                expected_gpu_uuid="GPU-11111111-2222-3333-4444-555555555555",
            )

    def test_phase_a_assertion_requires_valid_fresh_busy_or_degraded_signal(
        self,
    ) -> None:
        signal = {
            "schema": 1,
            "boot_id_sha256": "1" * 64,
            "producer_epoch": "2" * 32,
            "sequence": 3,
            "observed_monotonic_ns": 9_000_000_000,
            "source_generation": 4,
            "source_observed_monotonic_ns": 8_000_000_000,
            "observation_id": "3" * 64,
            "policy_sha256": "4" * 64,
            "pressure": True,
            "clear_eligible": False,
            "reason_codes": ["higher_priority_busy"],
        }
        payload = sampler.canonical_json_line(signal)
        parsed = sampler.validate_priority_signal_bytes(
            payload,
            expected_boot_sha256="1" * 64,
            expected_policy_sha256="4" * 64,
            expected_producer_epoch="2" * 32,
            now_monotonic_ns=10_000_000_000,
        )
        self.assertEqual(
            sampler.validate_phase_a_assertion(parsed),
            sampler.sha256_bytes(("3" * 64).encode("ascii")),
        )
        for reason in ("higher_priority_unavailable", "policy_drift"):
            invalid = copy.deepcopy(signal)
            invalid["reason_codes"] = [reason]
            with self.subTest(reason=reason), self.assertRaises(sampler.GateAbort):
                sampler.validate_phase_a_assertion(invalid)
        stale = copy.deepcopy(signal)
        stale["observed_monotonic_ns"] = 1
        stale["source_observed_monotonic_ns"] = 1
        with self.assertRaisesRegex(sampler.GateAbort, "stale"):
            sampler.validate_priority_signal_bytes(
                sampler.canonical_json_line(stale),
                expected_boot_sha256="1" * 64,
                expected_policy_sha256="4" * 64,
                expected_producer_epoch="2" * 32,
                now_monotonic_ns=20_000_000_000,
            )
        with self.assertRaisesRegex(sampler.GateAbort, "producer_epoch"):
            sampler.validate_priority_signal_bytes(
                payload,
                expected_boot_sha256="1" * 64,
                expected_policy_sha256="4" * 64,
                expected_producer_epoch="5" * 32,
                now_monotonic_ns=10_000_000_000,
            )

    def test_latest_receipt_binding_and_protected_cadence_are_exact(self) -> None:
        receipts = [
            runtime_receipt(1, observed_monotonic_ns=1_000),
            runtime_receipt(2, observed_monotonic_ns=2_000),
            runtime_receipt(3, observed_monotonic_ns=3_000),
        ]
        binding = sampler.bind_latest_runtime_receipt(receipts, 2_500)
        self.assertEqual(binding.receipt["sequence"], 2)
        self.assertEqual(binding.next_observed_monotonic_ns, 3_000)
        self.assertEqual(
            binding.receipt_sha256,
            sampler.sha256_bytes(sampler.canonical_json_line(receipts[1])),
        )
        with self.assertRaisesRegex(sampler.GateAbort, "predates"):
            sampler.bind_latest_runtime_receipt(receipts, 999)

        cadence = sampler.validate_protected_sample_cadence(
            [1_000_000_000, 3_000_000_000, 5_000_000_000],
            t0_monotonic_ns=2_000_000_000,
            gpu_proof_monotonic_ns=4_000_000_000,
        )
        self.assertEqual(cadence["protected_sample_count"], 3)
        with self.assertRaisesRegex(sampler.GateAbort, "blind"):
            sampler.validate_protected_sample_cadence(
                [1_000_000_000, 4_000_000_001, 5_000_000_000],
                t0_monotonic_ns=2_000_000_000,
                gpu_proof_monotonic_ns=4_000_000_000,
            )

    @unittest.skipUnless(os.name == "posix", "owner/mode proof requires POSIX")
    def test_create_once_writer_round_trips_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "private"
            parent.mkdir(mode=0o700)
            os.chmod(parent, 0o700)
            path = parent / "candidate.json"
            document = self.candidate_document()
            artifact = sampler.write_canonical_artifact(
                path,
                document,
                validator=sampler.validate_candidate_identity_document,
            )
            self.assertEqual(artifact.document, document)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                sampler.load_canonical_artifact(
                    path,
                    validator=sampler.validate_candidate_identity_document,
                    expected_sha256=artifact.file_sha256,
                ),
                document,
            )
            with self.assertRaises(sampler.GateAbort):
                sampler.write_canonical_artifact(
                    path,
                    document,
                    validator=sampler.validate_candidate_identity_document,
                )


class CandidateIdentityTests(unittest.TestCase):
    CONTAINER_ID = "a" * 64
    REBOUND_ID = "b" * 64
    IMAGE_CONFIG = "sha256:" + "c" * 64
    OCI_INDEX = "sha256:" + "d" * 64
    LAYER_DIFF_IDS = ["sha256:" + "e" * 64]
    MODEL_REVISION = "hf:" + "f" * 40
    RUNTIME_COMMIT = "d" * 40
    TOKEN = "0123456789abcdef0123456789abcdef"
    PHASE_A_SHA256 = "a" * 64
    PHASE_B_SHA256 = "b" * 64
    PHASE_A_FIXTURE_RECORD_SHA256 = "1" * 64
    PHASE_B_FIXTURE_RECORD_SHA256 = "2" * 64
    NAME = "subgen-task11b-runtime-auto"
    DISPOSABLE_ROOT = "/var/lib/subgen-v05-gate"
    CATALOG_SHA256 = "f" * 64

    @staticmethod
    def docker_daemon_identity() -> dict[str, object]:
        return sampler.docker_daemon_identity_document("6" * 64, "7" * 64)

    @classmethod
    def boundary_candidate_identity(
        cls,
        item: dict[str, object] | None = None,
        *,
        model: str = "medium",
    ) -> dict[str, object]:
        candidate = item or cls.candidate_item()
        return {
            "container_id": candidate["Id"],
            "runtime_commit": cls.RUNTIME_COMMIT,
            "oci_index": candidate["Image"],
            "config_digest": cls.IMAGE_CONFIG,
            "layer_diff_ids": copy.deepcopy(cls.LAYER_DIFF_IDS),
            "selected_model": model,
            "model_revision": cls.MODEL_REVISION,
        }

    @classmethod
    def candidate_item(cls) -> dict[str, object]:
        item: dict[str, object] = {
            "Id": cls.CONTAINER_ID,
            "Name": f"/{cls.NAME}",
            "Image": cls.OCI_INDEX,
            "RestartCount": 0,
            "State": {
                "Status": "running",
                "Running": True,
                "OOMKilled": False,
                "HealthStatus": "healthy",
            },
            "Labels": {
                sampler.GATE_LABEL: "true",
                sampler.TOKEN_LABEL: cls.TOKEN,
                sampler.ROLE_LABEL: "runtime-auto",
                sampler.RUNTIME_LABEL: cls.RUNTIME_COMMIT,
            },
            "Entrypoint": ["/usr/bin/python3"],
            "Cmd": copy.deepcopy(sampler.RUNTIME_COMMAND),
            "Env": [
                "PATH=/usr/local/bin:/usr/bin",
                "PUID=1000",
                "PGID=1000",
                "AUTO_DELETE_INVALID_MEDIA=false",
                "AUTO_DELETE_FAILED_FILES=false",
                "SUBGEN_REPAIR_ACTION=report",
                "SUBTITLE_LANGUAGE_NAME=en",
                "SHOW_IN_SUBNAME_SUBGEN=false",
                "SHOW_IN_SUBNAME_MODEL=false",
                "WHISPER_MODEL=auto",
                "TRANSCRIBE_DEVICE=cuda",
                "COMPUTE_TYPE=float16",
                "CONCURRENT_TRANSCRIPTIONS=1",
                "MODEL_PATH=/subgen/models",
                "MODEL_ENVELOPE_CATALOG=/opt/subgen/model-envelopes/catalog.json",
                "MODEL_ENVELOPE_IDENTITY=/opt/subgen/model-envelopes/image-identity.json",
                "SUBGEN_FAILURE_MARKER_PATH=" + sampler.FAILURE_MARKER_CONTAINER_PATH,
                "SEGMENTATION_ENABLED=True",
                "SEGMENTATION_CHUNK_MINUTES=5",
                "CANONICAL_SHARED_CUDA=true",
                "GPU_MEMORY_RESERVE_GIB=8",
                "PRIORITY_PRESSURE_FILE=/run/subgen-priority/pressure.json",
                "MEMORY_PRESSURE_YIELD=True",
                "TASK11B_GATE_RECEIPT_FILE=/run/subgen-task11b/runtime-receipts.jsonl",
                "TASK11B_GATE_TOKEN_SHA256="
                + sampler.sha256_bytes(cls.TOKEN.encode("ascii")),
                "TASK11B_PHASE_A_WORKLOAD_SHA256=" + cls.PHASE_A_SHA256,
                "TASK11B_PHASE_B_WORKLOAD_SHA256=" + cls.PHASE_B_SHA256,
            ],
            "User": "1000:1000",
            "WorkingDir": "/subgen",
            "NetworkSettings": {
                "Networks": {
                    "bridge": {
                        "IPAMConfig": None,
                        "Links": None,
                        "Aliases": None,
                        "DriverOpts": None,
                        "GwPriority": 0,
                        "NetworkID": "f" * 64,
                    }
                }
            },
            "HostConfig": {
                "RestartPolicy": {"Name": "no"},
                "NetworkMode": "bridge",
                "Memory": 17 * sampler.GIB,
                "MemorySwap": 17 * sampler.GIB,
                "PortBindings": {
                    "9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19000"}]
                },
                "Privileged": False,
                "PidMode": "",
                "IpcMode": "private",
                "CgroupnsMode": "private",
                "UTSMode": "",
                "UsernsMode": "",
                "ReadonlyRootfs": True,
                "CapAdd": None,
                "CapDrop": ["ALL"],
                "Devices": [],
                "DeviceRequests": [
                    {
                        "Driver": "nvidia",
                        "Count": -1,
                        "DeviceIDs": None,
                        "Capabilities": [["gpu"]],
                        "Options": {},
                    }
                ],
                "SecurityOpt": ["no-new-privileges"],
                "GroupAdd": None,
                "PidsLimit": 512,
                "NanoCpus": 4_000_000_000,
                "CpuPeriod": 0,
                "CpuQuota": 0,
                "Tmpfs": copy.deepcopy(sampler.SAFE_TMPFS),
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": f"{cls.DISPOSABLE_ROOT}/models",
                    "Destination": "/subgen/models",
                    "Mode": "rw",
                    "RW": True,
                    "Propagation": "rprivate",
                },
                {
                    "Type": "bind",
                    "Source": f"{cls.DISPOSABLE_ROOT}/monitor",
                    "Destination": "/opt/subgen/monitor",
                    "Mode": "rw",
                    "RW": True,
                    "Propagation": "rprivate",
                },
                {
                    "Type": "bind",
                    "Source": f"{cls.DISPOSABLE_ROOT}/model-envelopes",
                    "Destination": "/opt/subgen/model-envelopes",
                    "Mode": "ro",
                    "RW": False,
                    "Propagation": "rprivate",
                },
                {
                    "Type": "bind",
                    "Source": f"{cls.DISPOSABLE_ROOT}/fixtures/phase-a",
                    "Destination": "/fixtures/phase-a",
                    "Mode": "ro",
                    "RW": False,
                    "Propagation": "rprivate",
                },
                {
                    "Type": "bind",
                    "Source": f"{cls.DISPOSABLE_ROOT}/fixtures/phase-b",
                    "Destination": "/fixtures/phase-b",
                    "Mode": "ro",
                    "RW": False,
                    "Propagation": "rprivate",
                },
                {
                    "Type": "bind",
                    "Source": f"{cls.DISPOSABLE_ROOT}/task11b-output/phase-a",
                    "Destination": "/task11b-output/phase-a",
                    "Mode": "rw",
                    "RW": True,
                    "Propagation": "rprivate",
                },
                {
                    "Type": "bind",
                    "Source": f"{cls.DISPOSABLE_ROOT}/task11b-output/phase-b",
                    "Destination": "/task11b-output/phase-b",
                    "Mode": "rw",
                    "RW": True,
                    "Propagation": "rprivate",
                },
                {
                    "Type": "bind",
                    "Source": "/run/subgen-priority",
                    "Destination": "/run/subgen-priority",
                    "Mode": "ro",
                    "RW": False,
                    "Propagation": "rprivate",
                },
                {
                    "Type": "bind",
                    "Source": f"{cls.DISPOSABLE_ROOT}/receipts",
                    "Destination": "/run/subgen-task11b",
                    "Mode": "rw",
                    "RW": True,
                    "Propagation": "rprivate",
                },
            ],
        }
        item["ConfigFull"] = {
            "Env": copy.deepcopy(item["Env"]),
            "User": item["User"],
            "WorkingDir": item["WorkingDir"],
            "Entrypoint": copy.deepcopy(item["Entrypoint"]),
            "Cmd": copy.deepcopy(item["Cmd"]),
            "Labels": copy.deepcopy(item["Labels"]),
            "Healthcheck": None,
            "StopSignal": "SIGTERM",
        }
        host_full = copy.deepcopy(item["HostConfig"])
        assert isinstance(host_full, dict)
        host_full.update(
            {
                "LogConfig": copy.deepcopy(sampler.SAFE_LOG_CONFIG),
                "Binds": [
                    f"{cls.DISPOSABLE_ROOT}/models:/subgen/models:rw",
                    f"{cls.DISPOSABLE_ROOT}/monitor:/opt/subgen/monitor:rw",
                    f"{cls.DISPOSABLE_ROOT}/model-envelopes:/opt/subgen/model-envelopes:ro",
                    f"{cls.DISPOSABLE_ROOT}/fixtures/phase-a:/fixtures/phase-a:ro",
                    f"{cls.DISPOSABLE_ROOT}/fixtures/phase-b:/fixtures/phase-b:ro",
                    f"{cls.DISPOSABLE_ROOT}/task11b-output/phase-a:/task11b-output/phase-a:rw",
                    f"{cls.DISPOSABLE_ROOT}/task11b-output/phase-b:/task11b-output/phase-b:rw",
                    "/run/subgen-priority:/run/subgen-priority:ro",
                    f"{cls.DISPOSABLE_ROOT}/receipts:/run/subgen-task11b:rw",
                ],
                "OomKillDisable": False,
                "OomScoreAdj": 0,
                "Runtime": "runc",
                "Init": False,
                "Ulimits": None,
                "Sysctls": {},
                "ShmSize": 64 * 1024 * 1024,
                "CgroupParent": "",
                "DNS": [],
                "DNSOptions": [],
                "DNSSearch": [],
                "ExtraHosts": [],
            }
        )
        item["HostConfigFull"] = host_full
        return item

    @classmethod
    def profiler_item(cls, model: str = "large-v3") -> dict[str, object]:
        item = cls.candidate_item()
        item["Name"] = f"/subgen-task11b-profile-{model}"
        labels = item["Labels"]
        assert isinstance(labels, dict)
        labels[sampler.ROLE_LABEL] = f"profile-{model}"
        command = [
            "-c",
            sampler.PROFILER_HOLD_PROGRAM,
            sampler.PROFILER_STDOUT_PATH,
            sampler.PROFILER_RESULT_PATH,
            "--",
            "/subgen/profile_model_envelopes.py",
            "--catalog-input",
            sampler.PROFILER_INPUT_CATALOG_PATH,
            "--catalog-output",
            sampler.PROFILER_CATALOG_PATH,
            "--identity",
            sampler.PROFILER_IDENTITY_PATH,
            "--media",
            sampler.PROFILER_MEDIA_PATH,
            "--model",
            model,
            "--device",
            "cuda",
            "--compute-type",
            "float16",
            "--task",
            "translate",
            "--inference-concurrency",
            "1",
            "--model-path",
            "/subgen/models",
            "--cpu-threads",
            "4",
            "--gpu-reserve-gib",
            "8",
            "--chunk-minutes",
            "5",
            "--model-revision",
            cls.MODEL_REVISION,
            "--runs",
            "3",
            "--host-margin-mib",
            "512",
            "--device-margin-mib",
            "512",
            "--host-reserve-gib",
            "4",
        ]
        index = sampler.MODEL_DESCENT.index(model)
        if index:
            command.extend(("--after-safe-failure", sampler.MODEL_DESCENT[index - 1]))
        command.extend(("--canonical-shared-cuda", "--require-cgroup"))
        item["Cmd"] = command
        item["Env"] = [
            "PATH=/usr/local/bin:/usr/bin",
            "PUID=1000",
            "PGID=1000",
            "AUTO_DELETE_INVALID_MEDIA=false",
            "AUTO_DELETE_FAILED_FILES=false",
            "SUBGEN_REPAIR_ACTION=report",
            "PRIORITY_PRESSURE_FILE=/run/subgen-priority/pressure.json",
            "MEMORY_PRESSURE_YIELD=True",
        ]
        host = item["HostConfig"]
        assert isinstance(host, dict)
        host["NetworkMode"] = "none"
        host["Memory"] = 12 * sampler.GIB
        host["MemorySwap"] = 12 * sampler.GIB
        host["PortBindings"] = None
        item["NetworkSettings"] = {"Networks": {}}
        item["Mounts"] = [
            {
                "Type": "bind",
                "Source": f"{cls.DISPOSABLE_ROOT}/models",
                "Destination": "/subgen/models",
                "Mode": "rw",
                "RW": True,
                "Propagation": "rprivate",
            },
            {
                "Type": "bind",
                "Source": "/run/subgen-priority",
                "Destination": "/run/subgen-priority",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
            {
                "Type": "bind",
                "Source": f"{cls.DISPOSABLE_ROOT}/profile-input",
                "Destination": "/profile/input",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
            {
                "Type": "bind",
                "Source": f"{cls.DISPOSABLE_ROOT}/profile-output",
                "Destination": "/profile/output",
                "Mode": "rw",
                "RW": True,
                "Propagation": "rprivate",
            },
        ]
        config_full = item["ConfigFull"]
        assert isinstance(config_full, dict)
        for key in ("Env", "User", "WorkingDir", "Entrypoint", "Cmd", "Labels"):
            config_full[key] = copy.deepcopy(item[key])
        host_full = item["HostConfigFull"]
        assert isinstance(host_full, dict)
        for key, value in host.items():
            host_full[key] = copy.deepcopy(value)
        host_full["Binds"] = [
            f"{cls.DISPOSABLE_ROOT}/models:/subgen/models:rw",
            f"{cls.DISPOSABLE_ROOT}/profile-input:/profile/input:ro",
            f"{cls.DISPOSABLE_ROOT}/profile-output:/profile/output:rw",
            "/run/subgen-priority:/run/subgen-priority:ro",
        ]
        return item

    @classmethod
    def profiler_args(
        cls, model: str = "large-v3", *, expected_returncode: int = 3
    ) -> object:
        item = cls.profiler_item(model)
        boundary = sampler.canonical_execution_boundary(
            item,
            disposable_root=cls.DISPOSABLE_ROOT,
            model_envelope_catalog_sha256=cls.CATALOG_SHA256,
            phase_a_fixture_record_sha256=cls.PHASE_A_FIXTURE_RECORD_SHA256,
            phase_b_fixture_record_sha256=cls.PHASE_B_FIXTURE_RECORD_SHA256,
            candidate_identity=cls.boundary_candidate_identity(item, model=model),
            docker_daemon_identity=cls.docker_daemon_identity(),
            filesystem_check=False,
        )
        return SimpleNamespace(
            expected_memory_bytes=12 * sampler.GIB,
            candidate_mode="profiler",
            expected_model=model,
            expected_chunk_minutes=5,
            expected_profiler_returncode=expected_returncode,
            expected_container_id=cls.CONTAINER_ID,
            expected_image_config=cls.OCI_INDEX,
            runtime_commit=cls.RUNTIME_COMMIT,
            gpu_free_floor_bytes=8 * sampler.GIB,
            candidate_oci_index=cls.OCI_INDEX,
            candidate_config_digest=cls.IMAGE_CONFIG,
            candidate_layer_diff_ids=copy.deepcopy(cls.LAYER_DIFF_IDS),
            model_envelope_catalog_sha256=cls.CATALOG_SHA256,
            phase_a_fixture_record_sha256=cls.PHASE_A_FIXTURE_RECORD_SHA256,
            phase_b_fixture_record_sha256=cls.PHASE_B_FIXTURE_RECORD_SHA256,
            model_revision=cls.MODEL_REVISION,
            disposable_root=cls.DISPOSABLE_ROOT,
            boundary_expectation=sampler.BoundaryExpectation(
                document=boundary,
                file_sha256="1" * 64,
                canonical_sha256=sampler.execution_boundary_digest(boundary),
            ),
            _test_skip_disposable_filesystem_check=True,
        )

    @classmethod
    def boundary_expectation(cls, item: dict[str, object] | None = None) -> object:
        boundary = sampler.canonical_execution_boundary(
            item or cls.candidate_item(),
            disposable_root=cls.DISPOSABLE_ROOT,
            model_envelope_catalog_sha256=cls.CATALOG_SHA256,
            phase_a_fixture_record_sha256=cls.PHASE_A_FIXTURE_RECORD_SHA256,
            phase_b_fixture_record_sha256=cls.PHASE_B_FIXTURE_RECORD_SHA256,
            candidate_identity=cls.boundary_candidate_identity(
                item or cls.candidate_item()
            ),
            docker_daemon_identity=cls.docker_daemon_identity(),
            filesystem_check=False,
        )
        return sampler.BoundaryExpectation(
            document=boundary,
            file_sha256="1" * 64,
            canonical_sha256=sampler.execution_boundary_digest(boundary),
        )

    def test_live_daemon_must_match_identity_sealed_in_boundary(self) -> None:
        args = self.args()
        client = mock.Mock(spec=sampler.DockerClient)
        client.verify_local_daemon.return_value = ("6" * 64, "7" * 64)
        self.assertEqual(
            sampler.verify_bound_docker_daemon(client, args),
            ("6" * 64, "7" * 64),
        )

        client.verify_local_daemon.return_value = ("8" * 64, "7" * 64)
        with self.assertRaisesRegex(sampler.GateAbort, "sealed_boundary"):
            sampler.verify_bound_docker_daemon(client, args)

    @classmethod
    def args(cls) -> object:
        return SimpleNamespace(
            expected_memory_bytes=17 * sampler.GIB,
            candidate_mode="runtime",
            expected_model="medium",
            expected_chunk_minutes=5,
            expected_profiler_returncode=None,
            expected_container_id=cls.CONTAINER_ID,
            expected_image_config=cls.OCI_INDEX,
            runtime_commit=cls.RUNTIME_COMMIT,
            gate_token=cls.TOKEN,
            gpu_free_floor_bytes=8 * sampler.GIB,
            candidate_oci_index=cls.OCI_INDEX,
            candidate_config_digest=cls.IMAGE_CONFIG,
            candidate_layer_diff_ids=copy.deepcopy(cls.LAYER_DIFF_IDS),
            model_envelope_catalog_sha256=cls.CATALOG_SHA256,
            phase_a_fixture_record_sha256=cls.PHASE_A_FIXTURE_RECORD_SHA256,
            phase_b_fixture_record_sha256=cls.PHASE_B_FIXTURE_RECORD_SHA256,
            model_revision=cls.MODEL_REVISION,
            disposable_root=cls.DISPOSABLE_ROOT,
            boundary_expectation=cls.boundary_expectation(),
            _test_skip_disposable_filesystem_check=True,
        )

    @classmethod
    def binding(cls) -> object:
        item = cls.candidate_item()
        expectation = cls.boundary_expectation(item)
        return sampler.CandidateBinding(
            name=cls.NAME,
            container_id=cls.CONTAINER_ID,
            image_config=cls.OCI_INDEX,
            runtime_commit=cls.RUNTIME_COMMIT,
            gate_role="runtime-auto",
            gate_token_digest=sampler.sha256_bytes(cls.TOKEN.encode("utf-8")),
            command_digest=sampler._command_digest(item),
            boundary_digest=expectation.canonical_sha256,
        )

    def test_name_rebinding_race_is_rejected_after_immutable_id_check(self) -> None:
        by_id = self.candidate_item()
        by_name = copy.deepcopy(by_id)
        by_name["Id"] = self.REBOUND_ID

        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def inspect(self, reference: str) -> dict[str, object]:
                self.calls.append(reference)
                return (
                    by_id
                    if reference == CandidateIdentityTests.CONTAINER_ID
                    else by_name
                )

        client = FakeClient()
        args = self.args()
        with self.assertRaisesRegex(sampler.GateAbort, "name_was_rebound"):
            sampler.candidate_state(client, self.binding(), args)
        self.assertEqual(client.calls, [self.CONTAINER_ID, self.NAME])

    def test_priority_source_is_the_exact_live_host_directory(self) -> None:
        item = self.candidate_item()
        boundary = sampler.canonical_execution_boundary(
            item,
            disposable_root=self.DISPOSABLE_ROOT,
            model_envelope_catalog_sha256=self.CATALOG_SHA256,
            phase_a_fixture_record_sha256=self.PHASE_A_FIXTURE_RECORD_SHA256,
            phase_b_fixture_record_sha256=self.PHASE_B_FIXTURE_RECORD_SHA256,
            candidate_identity=self.boundary_candidate_identity(item),
            docker_daemon_identity=self.docker_daemon_identity(),
            filesystem_check=False,
        )
        priority_mounts = [
            mount
            for mount in boundary["mounts"]
            if mount["destination"] == "/run/subgen-priority"
        ]
        self.assertEqual(len(priority_mounts), 1)
        self.assertEqual(priority_mounts[0]["source"], "/run/subgen-priority")
        self.assertEqual(
            set(boundary["ownership_labels"]),
            {
                sampler.GATE_LABEL,
                sampler.TOKEN_LABEL,
                sampler.ROLE_LABEL,
                sampler.RUNTIME_LABEL,
            },
        )
        self.assertEqual(boundary["ownership_labels"][sampler.TOKEN_LABEL], self.TOKEN)

    def test_boundary_binds_both_fixture_records_and_exact_read_only_mounts(
        self,
    ) -> None:
        boundary = self.boundary_expectation().document
        self.assertEqual(
            boundary["phase_a_fixture_record_sha256"],
            self.PHASE_A_FIXTURE_RECORD_SHA256,
        )
        self.assertEqual(
            boundary["phase_b_fixture_record_sha256"],
            self.PHASE_B_FIXTURE_RECORD_SHA256,
        )
        fixture_mounts = {
            mount["destination"]: mount
            for mount in boundary["mounts"]
            if mount["destination"] in {"/fixtures/phase-a", "/fixtures/phase-b"}
        }
        self.assertEqual(
            set(fixture_mounts), {"/fixtures/phase-a", "/fixtures/phase-b"}
        )
        for mount in fixture_mounts.values():
            self.assertEqual(mount["mode"], "ro")
            self.assertIs(mount["read_write"], False)
            self.assertEqual(mount["propagation"], "rprivate")
        output_mounts = {
            mount["destination"]: mount
            for mount in boundary["mounts"]
            if mount["destination"]
            in {"/task11b-output/phase-a", "/task11b-output/phase-b"}
        }
        self.assertEqual(
            set(output_mounts),
            {"/task11b-output/phase-a", "/task11b-output/phase-b"},
        )
        for mount in output_mounts.values():
            self.assertEqual(mount["mode"], "rw")
            self.assertIs(mount["read_write"], True)
            self.assertEqual(mount["propagation"], "rprivate")

        with self.assertRaisesRegex(sampler.GateAbort, "fixture_record_digests"):
            sampler.canonical_execution_boundary(
                self.candidate_item(),
                disposable_root=self.DISPOSABLE_ROOT,
                model_envelope_catalog_sha256=self.CATALOG_SHA256,
                phase_a_fixture_record_sha256=self.PHASE_A_FIXTURE_RECORD_SHA256,
                phase_b_fixture_record_sha256=self.PHASE_A_FIXTURE_RECORD_SHA256,
                candidate_identity=self.boundary_candidate_identity(),
                docker_daemon_identity=self.docker_daemon_identity(),
                filesystem_check=False,
            )

    def test_runtime_mount_policy_rejects_obsolete_writable_media_root(self) -> None:
        mounts = copy.deepcopy(self.boundary_expectation().document["mounts"])
        mounts.append(
            {
                "type": "bind",
                "source": f"{self.DISPOSABLE_ROOT}/media",
                "destination": "/media",
                "mode": "rw",
                "read_write": True,
                "propagation": "rprivate",
            }
        )
        with self.assertRaisesRegex(sampler.GateAbort, "least_privilege"):
            sampler._validate_mount_policy(mounts, candidate_mode="runtime")

    def test_execution_boundary_rejects_writable_alias_of_read_only_fixture(
        self,
    ) -> None:
        mounts = copy.deepcopy(self.boundary_expectation().document["mounts"])
        by_destination = {
            mount["destination"]: mount for mount in mounts if isinstance(mount, dict)
        }
        by_destination["/task11b-output/phase-a"]["source"] = by_destination[
            "/fixtures/phase-b"
        ]["source"]
        with self.assertRaisesRegex(sampler.GateAbort, "mount_sources_overlapped"):
            sampler._validate_mount_source_disjointness(mounts, filesystem_check=False)

    def test_execution_boundary_rejects_alternate_monitor_source(self) -> None:
        item = self.candidate_item()
        mounts = item["Mounts"]
        assert isinstance(mounts, list)
        monitor = next(
            mount
            for mount in mounts
            if isinstance(mount, dict) and mount["Destination"] == "/opt/subgen/monitor"
        )
        monitor["Source"] = f"{self.DISPOSABLE_ROOT}/alternate-monitor"
        with self.assertRaisesRegex(sampler.GateAbort, "exact_contract"):
            sampler.canonical_execution_boundary(
                item,
                disposable_root=self.DISPOSABLE_ROOT,
                model_envelope_catalog_sha256=self.CATALOG_SHA256,
                phase_a_fixture_record_sha256=self.PHASE_A_FIXTURE_RECORD_SHA256,
                phase_b_fixture_record_sha256=self.PHASE_B_FIXTURE_RECORD_SHA256,
                candidate_identity=self.boundary_candidate_identity(item),
                docker_daemon_identity=self.docker_daemon_identity(),
                filesystem_check=False,
            )

    def test_priority_reserve_has_no_implicit_parser_default(self) -> None:
        self.assertIsNone(sampler.parser().parse_args([]).gpu_free_floor_bytes)

    def test_image_identity_change_is_rejected_before_state_is_returned(self) -> None:
        changed = self.candidate_item()
        changed["Image"] = "sha256:" + "e" * 64

        class FakeClient:
            def inspect(self, _reference: str) -> dict[str, object]:
                return changed

        args = self.args()
        with self.assertRaisesRegex(sampler.GateAbort, "identity_changed"):
            sampler.candidate_state(FakeClient(), self.binding(), args)

    def test_every_execution_boundary_mutation_is_rejected(self) -> None:
        def mutate_host(item: dict[str, object], key: str, value: object) -> None:
            host = item["HostConfig"]
            assert isinstance(host, dict)
            host[key] = value

        def mutate_env(item: dict[str, object]) -> None:
            env = item["Env"]
            assert isinstance(env, list)
            index = env.index("AUTO_DELETE_INVALID_MEDIA=false")
            env[index] = "AUTO_DELETE_INVALID_MEDIA=true"

        def mutate_other_env(item: dict[str, object]) -> None:
            env = item["Env"]
            assert isinstance(env, list)
            env[0] = "PATH=/tmp"

        def mutate_user(item: dict[str, object]) -> None:
            item["User"] = "0"

        def mutate_workdir(item: dict[str, object]) -> None:
            item["WorkingDir"] = "/tmp"

        def mutate_mount_source(item: dict[str, object]) -> None:
            mounts = item["Mounts"]
            assert isinstance(mounts, list) and isinstance(mounts[0], dict)
            mounts[0]["Source"] = "/srv/media"

        def mutate_mount_mode(item: dict[str, object]) -> None:
            mounts = item["Mounts"]
            assert isinstance(mounts, list) and isinstance(mounts[0], dict)
            mounts[0]["Mode"] = "ro"

        def add_mount(item: dict[str, object]) -> None:
            mounts = item["Mounts"]
            assert isinstance(mounts, list)
            mounts.append(
                {
                    "Type": "bind",
                    "Source": f"{self.DISPOSABLE_ROOT}/extra",
                    "Destination": "/extra",
                    "Mode": "ro",
                    "RW": False,
                    "Propagation": "rprivate",
                }
            )

        def mutate_full_log_config(item: dict[str, object]) -> None:
            host = item["HostConfigFull"]
            assert isinstance(host, dict)
            host["LogConfig"] = {"Type": "none", "Config": {}}

        def add_network_attachment(item: dict[str, object]) -> None:
            settings = item["NetworkSettings"]
            assert isinstance(settings, dict)
            networks = settings["Networks"]
            assert isinstance(networks, dict)
            networks["unexpected"] = {
                "IPAMConfig": None,
                "Links": None,
                "Aliases": None,
                "DriverOpts": None,
                "GwPriority": 0,
                "NetworkID": "e" * 64,
            }

        mutations = {
            "deletion env": mutate_env,
            "environment digest": mutate_other_env,
            "user": mutate_user,
            "workdir": mutate_workdir,
            "privileged": lambda item: mutate_host(item, "Privileged", True),
            "pid namespace": lambda item: mutate_host(item, "PidMode", "host"),
            "ipc namespace": lambda item: mutate_host(item, "IpcMode", "host"),
            "cgroup namespace": lambda item: mutate_host(item, "CgroupnsMode", "host"),
            "capabilities": lambda item: mutate_host(item, "CapAdd", ["SYS_ADMIN"]),
            "devices": lambda item: mutate_host(
                item,
                "Devices",
                [{"PathOnHost": "/dev/sda", "PathInContainer": "/dev/sda"}],
            ),
            "device requests": lambda item: mutate_host(item, "DeviceRequests", []),
            "read only rootfs": lambda item: mutate_host(item, "ReadonlyRootfs", False),
            "cap drop": lambda item: mutate_host(item, "CapDrop", []),
            "security": lambda item: mutate_host(item, "SecurityOpt", []),
            "tmpfs": lambda item: mutate_host(item, "Tmpfs", {}),
            "process limit": lambda item: mutate_host(item, "PidsLimit", 0),
            "mount source": mutate_mount_source,
            "mount access": mutate_mount_mode,
            "mount allowlist": add_mount,
            "full host logging config": mutate_full_log_config,
            "runtime network attachment": add_network_attachment,
        }
        args = self.args()
        for label, mutation in mutations.items():
            item = copy.deepcopy(self.candidate_item())
            mutation(item)
            with self.subTest(label=label), self.assertRaises(sampler.GateAbort):
                sampler._validate_candidate_boundaries(item, args)

    def test_missing_projected_tmpfs_remains_rejected_for_candidate(self) -> None:
        item = self.candidate_item()
        host = item["HostConfig"]
        host_full = item["HostConfigFull"]
        assert isinstance(host, dict) and isinstance(host_full, dict)
        host["Tmpfs"] = None
        host_full["Tmpfs"] = None

        with self.assertRaisesRegex(sampler.GateAbort, "tmpfs_policy_was_not_exact"):
            sampler._validate_candidate_boundaries(item, self.args())

    def test_safe_candidate_logging_is_explicitly_blocking_and_nonrotating(
        self,
    ) -> None:
        self.assertEqual(
            sampler.SAFE_LOG_CONFIG,
            {"Type": "json-file", "Config": {"mode": "blocking"}},
        )

    def test_oom_kill_disable_lifecycle_forms_have_same_boundary(self) -> None:
        before_start = self.candidate_item()
        before_host = before_start["HostConfigFull"]
        assert isinstance(before_host, dict)
        before_host["OomKillDisable"] = None

        after_start = copy.deepcopy(before_start)
        after_host = after_start["HostConfigFull"]
        assert isinstance(after_host, dict)
        after_host["OomKillDisable"] = False

        before_boundary = sampler.canonical_execution_boundary(
            before_start,
            disposable_root=self.DISPOSABLE_ROOT,
            model_envelope_catalog_sha256=self.CATALOG_SHA256,
            phase_a_fixture_record_sha256=self.PHASE_A_FIXTURE_RECORD_SHA256,
            phase_b_fixture_record_sha256=self.PHASE_B_FIXTURE_RECORD_SHA256,
            candidate_identity=self.boundary_candidate_identity(before_start),
            docker_daemon_identity=self.docker_daemon_identity(),
            filesystem_check=False,
        )
        after_boundary = sampler.canonical_execution_boundary(
            after_start,
            disposable_root=self.DISPOSABLE_ROOT,
            model_envelope_catalog_sha256=self.CATALOG_SHA256,
            phase_a_fixture_record_sha256=self.PHASE_A_FIXTURE_RECORD_SHA256,
            phase_b_fixture_record_sha256=self.PHASE_B_FIXTURE_RECORD_SHA256,
            candidate_identity=self.boundary_candidate_identity(after_start),
            docker_daemon_identity=self.docker_daemon_identity(),
            filesystem_check=False,
        )

        self.assertEqual(before_boundary, after_boundary)

    def test_oom_kill_disable_rejects_true_and_every_other_type(self) -> None:
        for value in (True, 0, 1, "false", [], {}):
            item = self.candidate_item()
            host_full = item["HostConfigFull"]
            assert isinstance(host_full, dict)
            host_full["OomKillDisable"] = value

            with (
                self.subTest(value=value),
                self.assertRaisesRegex(sampler.GateAbort, "oom_kill_disable"),
            ):
                sampler.canonical_execution_boundary(
                    item,
                    disposable_root=self.DISPOSABLE_ROOT,
                    model_envelope_catalog_sha256=self.CATALOG_SHA256,
                    phase_a_fixture_record_sha256=(self.PHASE_A_FIXTURE_RECORD_SHA256),
                    phase_b_fixture_record_sha256=(self.PHASE_B_FIXTURE_RECORD_SHA256),
                    candidate_identity=self.boundary_candidate_identity(item),
                    docker_daemon_identity=self.docker_daemon_identity(),
                    filesystem_check=False,
                )

    def test_other_full_host_config_fields_remain_exactly_hash_bound(self) -> None:
        baseline = self.candidate_item()
        changed = copy.deepcopy(baseline)
        changed_host = changed["HostConfigFull"]
        assert isinstance(changed_host, dict)
        changed_host["OomScoreAdj"] = 1

        baseline_boundary = sampler.canonical_execution_boundary(
            baseline,
            disposable_root=self.DISPOSABLE_ROOT,
            model_envelope_catalog_sha256=self.CATALOG_SHA256,
            phase_a_fixture_record_sha256=self.PHASE_A_FIXTURE_RECORD_SHA256,
            phase_b_fixture_record_sha256=self.PHASE_B_FIXTURE_RECORD_SHA256,
            candidate_identity=self.boundary_candidate_identity(baseline),
            docker_daemon_identity=self.docker_daemon_identity(),
            filesystem_check=False,
        )
        changed_boundary = sampler.canonical_execution_boundary(
            changed,
            disposable_root=self.DISPOSABLE_ROOT,
            model_envelope_catalog_sha256=self.CATALOG_SHA256,
            phase_a_fixture_record_sha256=self.PHASE_A_FIXTURE_RECORD_SHA256,
            phase_b_fixture_record_sha256=self.PHASE_B_FIXTURE_RECORD_SHA256,
            candidate_identity=self.boundary_candidate_identity(changed),
            docker_daemon_identity=self.docker_daemon_identity(),
            filesystem_check=False,
        )

        self.assertNotEqual(
            baseline_boundary["host_config_sha256"],
            changed_boundary["host_config_sha256"],
        )

    def test_lossy_log_driver_options_are_rejected_before_manifest(self) -> None:
        unsafe_log_configs = (
            {"Type": "local", "Config": {}},
            {"Type": "json-file", "Config": {}},
            {
                "Type": "json-file",
                "Config": {"max-size": "1m", "mode": "blocking"},
            },
            {
                "Type": "json-file",
                "Config": {"max-file": "2", "mode": "blocking"},
            },
            {"Type": "json-file", "Config": {"mode": "non-blocking"}},
            {
                "Type": "json-file",
                "Config": {"max-size": "-1", "mode": "blocking"},
            },
        )
        for log_config in unsafe_log_configs:
            item = copy.deepcopy(self.candidate_item())
            host = item["HostConfigFull"]
            assert isinstance(host, dict)
            host["LogConfig"] = log_config
            with (
                self.subTest(log_config=log_config),
                self.assertRaisesRegex(sampler.GateAbort, "complete_docker_logs"),
            ):
                sampler.canonical_execution_boundary(
                    item,
                    disposable_root=self.DISPOSABLE_ROOT,
                    model_envelope_catalog_sha256=self.CATALOG_SHA256,
                    phase_a_fixture_record_sha256=(self.PHASE_A_FIXTURE_RECORD_SHA256),
                    phase_b_fixture_record_sha256=(self.PHASE_B_FIXTURE_RECORD_SHA256),
                    candidate_identity=self.boundary_candidate_identity(item),
                    docker_daemon_identity=self.docker_daemon_identity(),
                    filesystem_check=False,
                )

    def test_profiler_requires_model_specific_exact_run_count(self) -> None:
        for model, expected_runs, rejected_runs in (
            ("large-v3", "3", "30"),
            ("medium", "30", "3"),
            ("small", "30", "3"),
            ("base", "30", "3"),
            ("tiny", "30", "3"),
        ):
            item = self.profiler_item(model)
            command = item["Cmd"]
            assert isinstance(command, list)
            command[command.index("--runs") + 1] = expected_runs
            args = self.profiler_args(
                model,
                expected_returncode=3 if model != "tiny" else 0,
            )
            with self.subTest(model=model, runs=expected_runs):
                sampler._validate_profiler_command(item, args)

            command[command.index("--runs") + 1] = rejected_runs
            with (
                self.subTest(model=model, runs=rejected_runs),
                self.assertRaisesRegex(sampler.GateAbort, "run_count"),
            ):
                sampler._validate_profiler_command(item, args)

    def test_profiler_command_cross_binds_exact_model_revision(self) -> None:
        item = self.profiler_item("medium")
        args = self.profiler_args("medium", expected_returncode=0)
        command = item["Cmd"]
        assert isinstance(command, list)
        command[command.index("--runs") + 1] = "30"
        sampler._validate_profiler_command(item, args)

        command[command.index("--model-revision") + 1] = "hf:" + "d" * 40
        with self.assertRaisesRegex(sampler.GateAbort, "model_revision"):
            sampler._validate_profiler_command(item, args)

    def test_semantic_policy_rejects_unsafe_runtime_and_profiler_before_manifest(
        self,
    ) -> None:
        def host_value(item: dict[str, object], key: str, value: object) -> None:
            host = item["HostConfig"]
            assert isinstance(host, dict)
            host[key] = value
            host_full = item["HostConfigFull"]
            assert isinstance(host_full, dict)
            host_full[key] = copy.deepcopy(value)

        def config_value(item: dict[str, object], key: str, value: object) -> None:
            item[key] = value
            config = item["ConfigFull"]
            assert isinstance(config, dict)
            config[key] = copy.deepcopy(value)

        def extended_profiler_command(item: dict[str, object]) -> None:
            command = item["Cmd"]
            assert isinstance(command, list)
            command.extend(("--credential", "secret"))
            config_value(item, "Cmd", command)

        def environment_value(item: dict[str, object], key: str, value: str) -> None:
            environment = item["Env"]
            assert isinstance(environment, list)
            prefix = f"{key}="
            item["Env"] = [
                f"{key}={value}" if entry.startswith(prefix) else entry
                for entry in environment
            ]
            config_value(item, "Env", item["Env"])

        def profiler_chunk_value(item: dict[str, object], value: str) -> None:
            command = item["Cmd"]
            assert isinstance(command, list)
            index = command.index("--chunk-minutes")
            command[index + 1] = value
            config_value(item, "Cmd", command)

        def mount_metadata(item: dict[str, object], key: str, value: object) -> None:
            mounts = item["Mounts"]
            assert isinstance(mounts, list) and isinstance(mounts[0], dict)
            mounts[0][key] = value

        def add_runtime_network(item: dict[str, object]) -> None:
            settings = item["NetworkSettings"]
            assert isinstance(settings, dict)
            networks = settings["Networks"]
            assert isinstance(networks, dict)
            networks["extra"] = copy.deepcopy(networks["bridge"])

        runtime_args = self.args()
        profiler_args = self.profiler_args()
        cases = [
            ("root user", "runtime", lambda item: config_value(item, "User", "0:0")),
            (
                "writable root",
                "runtime",
                lambda item: host_value(item, "ReadonlyRootfs", False),
            ),
            (
                "added capability",
                "runtime",
                lambda item: host_value(item, "CapAdd", ["SYS_ADMIN"]),
            ),
            (
                "host device",
                "runtime",
                lambda item: host_value(
                    item,
                    "Devices",
                    [{"PathOnHost": "/dev/sda", "PathInContainer": "/dev/sda"}],
                ),
            ),
            (
                "wrong NVIDIA request",
                "runtime",
                lambda item: host_value(item, "DeviceRequests", []),
            ),
            (
                "host PID namespace",
                "runtime",
                lambda item: host_value(item, "PidMode", "host"),
            ),
            (
                "unbounded tmpfs",
                "runtime",
                lambda item: host_value(item, "Tmpfs", {"/tmp": "rw"}),
            ),
            (
                "profiler network",
                "profiler",
                lambda item: host_value(item, "NetworkMode", "bridge"),
            ),
            (
                "profiler command extension",
                "profiler",
                extended_profiler_command,
            ),
            (
                "runtime segmentation disabled",
                "runtime",
                lambda item: environment_value(item, "SEGMENTATION_ENABLED", "False"),
            ),
            (
                "runtime chunk mismatch",
                "runtime",
                lambda item: environment_value(
                    item, "SEGMENTATION_CHUNK_MINUTES", "20"
                ),
            ),
            (
                "runtime marker registry mismatch",
                "runtime",
                lambda item: environment_value(
                    item,
                    "SUBGEN_FAILURE_MARKER_PATH",
                    "/task11b-output/phase-a/caller-selected.json",
                ),
            ),
            (
                "profiler chunk mismatch",
                "profiler",
                lambda item: profiler_chunk_value(item, "20"),
            ),
            (
                "mount mode extension",
                "runtime",
                lambda item: mount_metadata(item, "Mode", "rw,Z"),
            ),
            (
                "mount propagation",
                "runtime",
                lambda item: mount_metadata(item, "Propagation", "rshared"),
            ),
            ("extra runtime network", "runtime", add_runtime_network),
        ]
        for label, mode, mutation in cases:
            item = self.candidate_item() if mode == "runtime" else self.profiler_item()
            mutation(item)
            with self.subTest(label=label), self.assertRaises(sampler.GateAbort):
                sampler._candidate_boundary_after_basic_validation(
                    item,
                    runtime_args if mode == "runtime" else profiler_args,
                    filesystem_check=False,
                )

        accepted = self.profiler_item()
        digest = sampler._validate_candidate_boundaries(
            accepted, profiler_args, filesystem_check=False
        )
        self.assertEqual(digest, profiler_args.boundary_expectation.canonical_sha256)

    def test_runtime_and_profiler_require_exact_memory_pressure_yield(self) -> None:
        for mode in ("runtime", "profiler"):
            args = self.args() if mode == "runtime" else self.profiler_args()
            original = (
                self.candidate_item() if mode == "runtime" else self.profiler_item()
            )
            for value in (None, "False", "true", "1"):
                item = copy.deepcopy(original)
                environment = item["Env"]
                assert isinstance(environment, list)
                environment = [
                    entry
                    for entry in environment
                    if not entry.startswith("MEMORY_PRESSURE_YIELD=")
                ]
                if value is not None:
                    environment.append(f"MEMORY_PRESSURE_YIELD={value}")
                item["Env"] = environment
                config = item["ConfigFull"]
                assert isinstance(config, dict)
                config["Env"] = copy.deepcopy(environment)
                with (
                    self.subTest(mode=mode, value=value),
                    self.assertRaisesRegex(sampler.GateAbort, "memory_pressure_yield"),
                ):
                    sampler._candidate_boundary_after_basic_validation(
                        item,
                        args,
                        filesystem_check=False,
                    )

    def test_cleanup_ignores_disappeared_mount_source_but_evidence_does_not(
        self,
    ) -> None:
        item = self.candidate_item()
        binding = self.binding()
        args = self.args()

        class FakeClient:
            def verify_local_daemon(self) -> tuple[str, str]:
                return ("6" * 64, "7" * 64)

            def inspect(
                self, _reference: str, *, missing_ok: bool = False
            ) -> dict[str, object] | None:
                del missing_ok
                return item

            def command(self, *parts: str, **_kwargs: object) -> object:
                self.assert_stop = parts
                state = item["State"]
                assert isinstance(state, dict)
                state["Status"] = "exited"
                state["Running"] = False
                return sampler.CommandResult(0, "")

        unavailable = sampler.GateAbort("mount source vanished")
        with mock.patch.object(
            sampler, "_validate_disposable_source", side_effect=unavailable
        ) as source_check:
            with self.assertRaisesRegex(sampler.GateAbort, "mount_source_vanished"):
                sampler._validate_candidate_boundaries(
                    item, args, filesystem_check=True
                )
            outcome = sampler.stop_bound_candidate(FakeClient(), binding, args)
        self.assertTrue(outcome["verified_stopped"])
        self.assertEqual(source_check.call_count, 1)

    def test_cleanup_stops_renamed_original_by_id_not_name_replacement(self) -> None:
        binding = self.binding()
        original = self.candidate_item()
        original["Name"] = "/subgen-task11b-renamed-original"
        replacement = self.candidate_item()
        replacement["Id"] = self.REBOUND_ID
        replacement["Name"] = f"/{self.NAME}"

        class FakeClient:
            def __init__(self) -> None:
                self.inspect_references: list[str] = []
                self.commands: list[tuple[str, ...]] = []

            def verify_local_daemon(self) -> tuple[str, str]:
                return ("6" * 64, "7" * 64)

            def inspect(
                self, reference: str, *, missing_ok: bool = False
            ) -> dict[str, object] | None:
                del missing_ok
                self.inspect_references.append(reference)
                if reference == CandidateIdentityTests.CONTAINER_ID:
                    return original
                if reference == CandidateIdentityTests.NAME:
                    return replacement
                return None

            def command(self, *parts: str, **_kwargs: object) -> object:
                self.commands.append(parts)
                if (
                    parts[0] == "stop"
                    and parts[-1] == CandidateIdentityTests.CONTAINER_ID
                ):
                    state = original["State"]
                    assert isinstance(state, dict)
                    state["Status"] = "exited"
                    state["Running"] = False
                    return sampler.CommandResult(0, "")
                raise AssertionError(f"unexpected cleanup command: {parts!r}")

        client = FakeClient()
        args = self.args()

        outcome = sampler.stop_bound_candidate(client, binding, args)

        original_state = original["State"]
        replacement_state = replacement["State"]
        assert isinstance(original_state, dict)
        assert isinstance(replacement_state, dict)
        self.assertFalse(original_state["Running"])
        self.assertTrue(replacement_state["Running"])
        self.assertTrue(outcome["attempted"])
        self.assertTrue(outcome["verified_stopped"])
        self.assertFalse(outcome["kill_escalated"])
        self.assertEqual(
            client.commands,
            [("stop", "--time", "10", self.CONTAINER_ID)],
        )
        self.assertEqual(
            client.inspect_references,
            [self.CONTAINER_ID, self.CONTAINER_ID, self.CONTAINER_ID],
        )
        self.assertNotIn(
            self.REBOUND_ID, {part for call in client.commands for part in call}
        )
        self.assertNotIn(self.NAME, client.inspect_references)


class FrigateLogCompletenessTests(unittest.TestCase):
    @staticmethod
    def item(log_config: dict[str, object] | None = None) -> dict[str, object]:
        return {
            "Id": "9" * 64,
            "Name": "/frigate",
            "Image": "sha256:" + "8" * 64,
            "RestartCount": 0,
            "State": {
                "Status": "running",
                "Running": True,
                "OOMKilled": False,
                "ExitCode": 0,
                "HealthStatus": "healthy",
            },
            "HostConfigFull": {
                "LogConfig": copy.deepcopy(
                    sampler.SAFE_FRIGATE_LOG_CONFIG
                    if log_config is None
                    else log_config
                )
            },
        }

    def test_frigate_default_blocking_json_file_is_bound_and_revalidated(self) -> None:
        item = self.item()

        class FakeClient:
            def inspect(self, _reference: str) -> dict[str, object]:
                return item

        binding = sampler.bind_observed_container(FakeClient(), "frigate")
        self.assertEqual(binding.log_config_digest, sampler.SAFE_FRIGATE_LOG_SHA256)
        self.assertTrue(sampler.observed_state(FakeClient(), binding)["running"])

    def test_frigate_rotating_or_nonblocking_logs_are_rejected(self) -> None:
        unsafe = (
            {"Type": "local", "Config": {}},
            {"Type": "json-file", "Config": {"max-size": "10m"}},
            {"Type": "json-file", "Config": {"mode": "non-blocking"}},
        )

        class FakeClient:
            def __init__(self, item: dict[str, object]) -> None:
                self.item = item

            def inspect(self, _reference: str) -> dict[str, object]:
                return self.item

        for log_config in unsafe:
            with (
                self.subTest(log_config=log_config),
                self.assertRaisesRegex(sampler.GateAbort, "not_complete_and_non_lossy"),
            ):
                sampler.bind_observed_container(
                    FakeClient(self.item(log_config)), "frigate"
                )
        safe = self.item()
        binding = sampler.bind_observed_container(FakeClient(safe), "frigate")
        safe["HostConfigFull"] = {"LogConfig": unsafe[-1]}
        with self.assertRaisesRegex(sampler.GateAbort, "not_complete_and_non_lossy"):
            sampler.observed_state(FakeClient(safe), binding)


class ProfilerResultValidationTests(unittest.TestCase):
    @staticmethod
    def encoded(value: object) -> bytes:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")

    @classmethod
    def receipt(cls, stdout: bytes, returncode: int) -> bytes:
        return cls.encoded(
            {
                "schema": 1,
                "returncode": returncode,
                "stdout_bytes": len(stdout),
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            }
        )

    @classmethod
    def catalog(cls, model: str, version: int = 2) -> bytes:
        base = {
            "schema": "subgen.model-envelope.catalog/v1",
            "catalog_version": version,
            "entries": [{"policy": {"model": model, "chunk_minutes": 5}}],
        }
        canonical = json.dumps(
            base,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return cls.encoded(
            {
                **base,
                "integrity": {
                    "algorithm": "sha256",
                    "canonical_payload_sha256": "sha256:"
                    + hashlib.sha256(canonical).hexdigest(),
                },
            }
        )

    def test_large_v3_safe_failure_rc3_is_bound_and_catalog_free(self) -> None:
        args = CandidateIdentityTests.profiler_args("large-v3", expected_returncode=3)
        stdout = self.encoded(
            {
                "status": "safe_failure",
                "model": "large-v3",
                "catalog_version": None,
                "next_model": "medium",
                "reason": "insufficient_host",
                "replaced_existing": False,
            }
        )
        receipt = self.receipt(stdout, 3)
        with (
            mock.patch.object(sampler, "_profiler_output_source", return_value="/out"),
            mock.patch.object(
                sampler, "_open_profiler_output_directory", return_value=7
            ),
            mock.patch.object(
                sampler,
                "_read_private_profiler_artifact",
                side_effect=[receipt, stdout],
            ),
            mock.patch.object(sampler, "_profiler_artifact_exists", return_value=False),
            mock.patch.object(sampler.os, "close"),
        ):
            result = sampler.validate_profiler_completion(args)
        self.assertEqual(result["status"], "safe_failure")
        self.assertEqual(result["returncode"], 3)
        self.assertEqual(result["next_model"], "medium")
        self.assertNotIn("reason", result)

    def test_profiler_safe_failure_accepts_only_runtime_emitted_capacity_reasons(
        self,
    ) -> None:
        args = CandidateIdentityTests.profiler_args("large-v3", expected_returncode=3)

        def validate(reason: str) -> dict[str, object]:
            stdout = self.encoded(
                {
                    "status": "safe_failure",
                    "model": "large-v3",
                    "catalog_version": None,
                    "next_model": "medium",
                    "reason": reason,
                    "replaced_existing": False,
                }
            )
            receipt = self.receipt(stdout, 3)
            with (
                mock.patch.object(
                    sampler, "_profiler_output_source", return_value="/out"
                ),
                mock.patch.object(
                    sampler, "_open_profiler_output_directory", return_value=7
                ),
                mock.patch.object(
                    sampler,
                    "_read_private_profiler_artifact",
                    side_effect=[receipt, stdout],
                ),
                mock.patch.object(
                    sampler, "_profiler_artifact_exists", return_value=False
                ),
                mock.patch.object(sampler.os, "close"),
            ):
                return sampler.validate_profiler_completion(args)

        accepted = (
            "insufficient_host",
            "insufficient_device",
            "insufficient_host,insufficient_device",
            "safe_allocation_failure",
        )
        for reason in accepted:
            with self.subTest(accepted=reason):
                self.assertEqual(validate(reason)["status"], "safe_failure")

        rejected = (
            "capacity_envelope",
            "priority_pressure",
            "higher_priority_unavailable",
            "controller_recovering",
            "insufficient_device,insufficient_host",
            "insufficient_host,insufficient_host",
        )
        for reason in rejected:
            with (
                self.subTest(rejected=reason),
                self.assertRaisesRegex(sampler.GateAbort, "safe_failure_result"),
            ):
                validate(reason)

    def test_medium_success_rc0_requires_matching_integrity_catalog(self) -> None:
        args = CandidateIdentityTests.profiler_args("medium", expected_returncode=0)
        stdout = self.encoded(
            {
                "status": "profiled",
                "model": "medium",
                "catalog_version": 2,
                "next_model": None,
                "reason": None,
                "replaced_existing": False,
            }
        )
        receipt = self.receipt(stdout, 0)
        catalog = self.catalog("medium")
        with (
            mock.patch.object(sampler, "_profiler_output_source", return_value="/out"),
            mock.patch.object(
                sampler, "_open_profiler_output_directory", return_value=7
            ),
            mock.patch.object(
                sampler,
                "_read_private_profiler_artifact",
                side_effect=[receipt, stdout, catalog],
            ),
            mock.patch.object(sampler.os, "close"),
        ):
            result = sampler.validate_profiler_completion(args)
        self.assertEqual(result["status"], "profiled")
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["catalog_version"], 2)
        self.assertEqual(result["entry_count"], 1)

    def test_profiler_receipt_or_catalog_mutation_aborts(self) -> None:
        args = CandidateIdentityTests.profiler_args("medium", expected_returncode=0)
        stdout = self.encoded(
            {
                "status": "profiled",
                "model": "medium",
                "catalog_version": 2,
                "next_model": None,
                "reason": None,
                "replaced_existing": False,
            }
        )
        bad_receipt = json.loads(self.receipt(stdout, 0))
        bad_receipt["stdout_sha256"] = "0" * 64
        with (
            mock.patch.object(sampler, "_profiler_output_source", return_value="/out"),
            mock.patch.object(
                sampler, "_open_profiler_output_directory", return_value=7
            ),
            mock.patch.object(
                sampler,
                "_read_private_profiler_artifact",
                side_effect=[self.encoded(bad_receipt), stdout],
            ),
            mock.patch.object(sampler.os, "close"),
            self.assertRaisesRegex(sampler.GateAbort, "receipt_did_not_bind"),
        ):
            sampler.validate_profiler_completion(args)

        catalog = json.loads(self.catalog("medium"))
        catalog["integrity"]["canonical_payload_sha256"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(sampler.GateAbort, "integrity_did_not_match"):
            sampler._validate_profiler_catalog(
                self.encoded(catalog),
                expected_model="medium",
                expected_version=2,
                expected_chunk_minutes=5,
            )

        wrong_chunk = json.loads(self.catalog("medium"))
        wrong_chunk["entries"][0]["policy"]["chunk_minutes"] = 20
        base = {
            key: wrong_chunk[key] for key in ("schema", "catalog_version", "entries")
        }
        canonical = json.dumps(
            base,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        wrong_chunk["integrity"]["canonical_payload_sha256"] = (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
        )
        with self.assertRaisesRegex(sampler.GateAbort, "chunk_policy"):
            sampler._validate_profiler_catalog(
                self.encoded(wrong_chunk),
                expected_model="medium",
                expected_version=2,
                expected_chunk_minutes=5,
            )

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 1000,
        "exact profiler artifact ownership requires POSIX uid 1000",
    )
    def test_profiler_artifacts_require_stable_uid1000_mode0600_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            output.mkdir(mode=0o700)
            os.chmod(output, 0o700)
            artifact = output / "result.json"
            artifact.write_bytes(b'{"schema":1}\n')
            os.chmod(artifact, 0o600)
            directory_fd = sampler._open_profiler_output_directory(str(output))
            try:
                self.assertEqual(
                    sampler._read_private_profiler_artifact(
                        directory_fd, artifact.name, label="test profiler"
                    ),
                    artifact.read_bytes(),
                )
                os.chmod(artifact, 0o640)
                with self.assertRaisesRegex(sampler.GateAbort, "not_private"):
                    sampler._read_private_profiler_artifact(
                        directory_fd, artifact.name, label="test profiler"
                    )
            finally:
                os.close(directory_fd)


class FakeEvidence:
    """Small EvidenceWriter double that records whether a pass was attempted."""

    def __init__(self, *, fail_writes: bool = False) -> None:
        self.fail_writes = fail_writes
        self.closed = False
        self.records: list[dict[str, object]] = []
        self.seals: list[dict[str, object]] = []

    def write(self, record: dict[str, object]) -> None:
        if self.fail_writes:
            raise OSError("simulated write or fsync failure")
        self.records.append(record)

    def seal(self, **details: object) -> str:
        self.seals.append(details)
        return "f" * 64

    def close(self) -> None:
        self.closed = True


class LogAndFinalizationTests(unittest.TestCase):
    def test_log_windows_have_no_line_truncation_and_overflow_aborts(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def command(self, *parts: str, **_kwargs: object) -> object:
                self.calls.append(parts)
                return sampler.CommandResult(0, "")

        client = FakeClient()
        kernel_calls: list[list[str]] = []

        def kernel(argv: list[str], **_kwargs: object) -> object:
            kernel_calls.append(argv)
            return sampler.CommandResult(0, "")

        scanner = sampler.IncrementalLogScanner(
            client,
            CandidateIdentityTests.binding(),
            sampler.ObservedBinding("frigate", "9" * 64, "sha256:" + "8" * 64),
            100.0,
        )
        with mock.patch.object(sampler, "bounded_command", side_effect=kernel):
            scanner.scan(105.0)
        flattened = [part for call in client.calls for part in call]
        self.assertNotIn("--tail", flattened)
        self.assertNotIn("--lines", [part for call in kernel_calls for part in call])
        self.assertEqual(scanner.cursor_wall, 105.0)

    @unittest.skipUnless(os.name == "posix", "pipe selectors require POSIX")
    def test_high_volume_command_aborts_instead_of_truncating(self) -> None:
        with self.assertRaisesRegex(sampler.GateAbort, "byte_limit"):
            sampler.bounded_command(
                [
                    sys.executable,
                    "-c",
                    "import os; os.write(1, b'x' * 4096)",
                ],
                label="high volume log regression",
                max_bytes=128,
            )

    def test_final_sample_is_followed_by_fresh_state_memory_and_log_drain(self) -> None:
        args = SimpleNamespace(
            start_timeout_seconds=1,
            candidate_mode="runtime",
            expected_model="medium",
            expected_chunk_minutes=5,
            expected_profiler_returncode=None,
            gpu_free_floor_bytes=8 * sampler.GIB,
            host_reserve_bytes=4 * sampler.GIB,
            expected_memory_bytes=17 * sampler.GIB,
            duration_seconds=0,
            interval_seconds=5,
            ollama_url=sampler.EXACT_ENDPOINTS["ollama"],
            frigate_stats_url=sampler.EXACT_ENDPOINTS["frigate"],
            candidate_status_url=sampler.EXACT_ENDPOINTS["candidate"],
            boundary_expectation=CandidateIdentityTests.boundary_expectation(),
        )
        candidate = CandidateIdentityTests.binding()
        frigate = sampler.ObservedBinding("frigate", "9" * 64, "sha256:" + "8" * 64)
        candidate_state = {
            "status": "running",
            "running": True,
            "oom_killed": False,
            "restart_count": 0,
            "health": "healthy",
        }
        frigate_state = {
            "status": "running",
            "running": True,
            "oom_killed": False,
            "restart_count": 0,
            "health": "healthy",
        }
        evidence = FakeEvidence()
        logs = mock.Mock(name="logs")
        client = mock.Mock(spec=sampler.DockerClient)
        client.verify_local_daemon.return_value = ("6" * 64, "7" * 64)

        def endpoint_payload(
            _url: str, *, endpoint: str, timeout: float = 3.0
        ) -> dict[str, object]:
            del timeout
            if endpoint == "candidate":
                return healthy_candidate_status()
            if endpoint == "ollama":
                return {"models": []}
            return frigate_stats(now_wall=100.0)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sampler, "start_bound_candidate"))
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "wait_for_running",
                    return_value=candidate_state,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "observed_state",
                    return_value=frigate_state,
                )
            )
            candidate_state_call = stack.enter_context(
                mock.patch.object(
                    sampler,
                    "candidate_state",
                    return_value=candidate_state,
                )
            )
            memory_call = stack.enter_context(
                mock.patch.object(
                    sampler,
                    "candidate_memory",
                    side_effect=lambda *_args: copy.deepcopy(
                        healthy_memory(17 * sampler.GIB)
                    ),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler, "read_mem_available_bytes", return_value=8 * sampler.GIB
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "read_host_pressure",
                    return_value={"some": {}, "full": {}},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "gpu_telemetry",
                    return_value={"free_mib": 12 * 1024, "total_mib": 24 * 1024},
                )
            )
            stack.enter_context(
                mock.patch.object(sampler, "fetch_json", side_effect=endpoint_payload)
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "validate_frigate_stats",
                    return_value={"camera_count": 15},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler.time, "time", side_effect=[100.0, 101.0, 102.0]
                )
            )
            outcome = sampler.observe_gate(
                args,
                evidence,
                client,
                candidate,
                frigate,
                {"camera": 10.0},
                "daemon",
                "boot",
                "7" * 64,
                logs,
            )

        self.assertEqual(candidate_state_call.call_count, 2)
        self.assertEqual(memory_call.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in logs.scan.call_args_list], [101.0, 102.0]
        )
        self.assertEqual(evidence.records[-1]["event"], "gate_observation_final")
        self.assertEqual(outcome.final_log_wall, 102.0)

    def test_profiler_revalidates_priority_and_proves_release_before_final_record(
        self,
    ) -> None:
        args = CandidateIdentityTests.profiler_args("large-v3", expected_returncode=3)
        args.start_timeout_seconds = 1
        args.host_reserve_bytes = 4 * sampler.GIB
        args.duration_seconds = 0
        args.interval_seconds = 5
        args.ollama_url = sampler.EXACT_ENDPOINTS["ollama"]
        args.frigate_stats_url = sampler.EXACT_ENDPOINTS["frigate"]
        args.candidate_status_url = sampler.EXACT_ENDPOINTS["candidate"]
        item = CandidateIdentityTests.profiler_item("large-v3")
        candidate = sampler.CandidateBinding(
            name="subgen-task11b-profile-large-v3",
            container_id=CandidateIdentityTests.CONTAINER_ID,
            image_config=CandidateIdentityTests.OCI_INDEX,
            runtime_commit=CandidateIdentityTests.RUNTIME_COMMIT,
            gate_role="profile-large-v3",
            gate_token_digest=sampler.sha256_bytes(
                CandidateIdentityTests.TOKEN.encode("ascii")
            ),
            command_digest=sampler._command_digest(item),
            boundary_digest=args.boundary_expectation.canonical_sha256,
        )
        frigate = sampler.ObservedBinding("frigate", "9" * 64, "sha256:" + "8" * 64)
        candidate_state = {
            "status": "running",
            "running": True,
            "oom_killed": False,
            "restart_count": 0,
            "health": None,
        }
        frigate_state = {
            "status": "running",
            "running": True,
            "oom_killed": False,
            "restart_count": 0,
            "health": "healthy",
        }
        evidence = FakeEvidence()
        logs = mock.Mock(name="logs")
        client = mock.Mock(spec=sampler.DockerClient)
        gpu_uuid = "GPU-11111111-2222-4333-8444-555555555555"
        events: list[str] = []

        def priority_revalidate() -> dict[str, object]:
            events.append("priority")
            return {
                "policy_sha256": "3" * 64,
                "signal_payload_sha256": "4" * 64,
                "producer_epoch_sha256": "5" * 64,
                "boot_id_sha256": "6" * 64,
                "observation_id_sha256": "8" * 64,
                "sequence": len(events),
                "source_generation": len(events),
                "pressure": False,
                "clear_eligible": True,
                "reason_codes": [],
            }

        release = {
            "hold_pid_count": 1,
            "candidate_gpu_bytes": 0,
            "pid_set_sha256": "7" * 64,
            "gpu_uuid_sha256": sampler.sha256_bytes(gpu_uuid.encode("ascii")),
            "validated_monotonic_ns": 99,
        }
        cgroup_probe = mock.Mock(name="candidate-cgroup-probe")
        cgroup_probe.profiler_release_attestation.side_effect = lambda _gpu_uuid: (
            events.append("release") or release
        )

        def sample_once(*, sample: object, **_kwargs: object) -> tuple[int, float]:
            assert callable(sample)
            sample(0, 0.0)
            return 1, 0.0

        def endpoint_payload(
            _url: str, *, endpoint: str, timeout: float = 3.0
        ) -> dict[str, object]:
            del timeout
            if endpoint == "ollama":
                return {"models": []}
            return frigate_stats(now_wall=100.0)

        profiler_memory = healthy_memory()
        profiler_memory["memory.max"] = 12 * sampler.GIB
        startup_deadline = 321.0

        with contextlib.ExitStack() as stack:
            start = stack.enter_context(
                mock.patch.object(
                    sampler,
                    "start_bound_candidate",
                    side_effect=lambda *_args, **_kwargs: events.append("start"),
                )
            )
            wait = stack.enter_context(
                mock.patch.object(
                    sampler, "wait_for_running", return_value=candidate_state
                )
            )
            stack.enter_context(
                mock.patch.object(sampler, "observed_state", return_value=frigate_state)
            )
            stack.enter_context(
                mock.patch.object(
                    sampler, "candidate_state", return_value=candidate_state
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "candidate_memory",
                    side_effect=lambda *_args: copy.deepcopy(profiler_memory),
                )
            )
            stack.enter_context(
                mock.patch.object(sampler, "verify_bound_docker_daemon")
            )
            stack.enter_context(
                mock.patch.object(
                    sampler, "read_mem_available_bytes", return_value=8 * sampler.GIB
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "read_host_pressure",
                    return_value={"some": {}, "full": {}},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "gpu_telemetry",
                    return_value={"free_mib": 12 * 1024, "total_mib": 24 * 1024},
                )
            )
            stack.enter_context(
                mock.patch.object(sampler, "fetch_json", side_effect=endpoint_payload)
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "validate_frigate_stats",
                    return_value={"camera_count": 15},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "validate_profiler_completion",
                    return_value={"status": "safe_failure", "returncode": 3},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler, "CandidateCgroupProbe", return_value=cgroup_probe
                )
            )
            stack.enter_context(
                mock.patch.object(sampler, "run_sampling_loop", side_effect=sample_once)
            )
            stack.enter_context(
                mock.patch.object(sampler.time, "time", return_value=100.0)
            )
            sampler.observe_gate(
                args,
                evidence,
                client,
                candidate,
                frigate,
                {"camera": 10.0},
                "daemon",
                "boot",
                "7" * 64,
                logs,
                priority_revalidate=priority_revalidate,
                profiler_gpu_uuid=gpu_uuid,
                startup_deadline=startup_deadline,
            )

        self.assertEqual(
            events, ["priority", "start", "priority", "priority", "release"]
        )
        start.assert_called_once_with(
            client, candidate, args, deadline=startup_deadline
        )
        wait.assert_called_once_with(client, candidate, args, startup_deadline)
        final = evidence.records[-1]
        self.assertEqual(final["candidate_resource"]["release"], release)

    def test_profiler_start_uses_and_enforces_shared_absolute_deadline(self) -> None:
        args = CandidateIdentityTests.profiler_args("large-v3", expected_returncode=3)
        candidate = CandidateIdentityTests.binding()
        client = mock.Mock(spec=sampler.DockerClient)
        client.command.return_value = sampler.CommandResult(0, "")
        created = {
            "status": "created",
            "running": False,
            "oom_killed": False,
            "restart_count": 0,
            "health": None,
        }

        with (
            mock.patch.object(sampler, "candidate_state", return_value=created),
            mock.patch.object(sampler.time, "monotonic", side_effect=[100.0, 104.0]),
        ):
            sampler.start_bound_candidate(client, candidate, args, deadline=105.0)
        self.assertEqual(client.command.call_args.kwargs["timeout"], 5.0)

        client.reset_mock()
        with (
            mock.patch.object(sampler, "candidate_state", return_value=created),
            mock.patch.object(sampler.time, "monotonic", return_value=106.0),
            self.assertRaisesRegex(sampler.GateAbort, "startup_deadline"),
        ):
            sampler.start_bound_candidate(client, candidate, args, deadline=105.0)
        client.command.assert_not_called()

        with (
            mock.patch.object(sampler, "candidate_state", return_value=created),
            self.assertRaisesRegex(sampler.GateAbort, "deadline_was_invalid"),
        ):
            sampler.start_bound_candidate(client, candidate, args, deadline=True)

    def test_wait_for_running_rejects_state_observed_after_absolute_deadline(
        self,
    ) -> None:
        args = CandidateIdentityTests.profiler_args("large-v3", expected_returncode=3)
        candidate = CandidateIdentityTests.binding()
        client = mock.Mock(spec=sampler.DockerClient)
        running = {
            "status": "running",
            "running": True,
            "oom_killed": False,
            "restart_count": 0,
            "health": None,
        }
        with (
            mock.patch.object(sampler, "candidate_state", return_value=running),
            mock.patch.object(sampler.time, "monotonic", side_effect=[100.0, 106.0]),
            self.assertRaisesRegex(sampler.GateAbort, "start_within_boundary"),
        ):
            sampler.wait_for_running(client, candidate, args, deadline=105.0)

    def test_stop_completion_gets_a_new_state_and_log_end_time(self) -> None:
        args = CandidateIdentityTests.args()
        candidate = CandidateIdentityTests.binding()
        frigate = sampler.ObservedBinding("frigate", "9" * 64, "sha256:" + "8" * 64)
        stopped = {
            "status": "exited",
            "running": False,
            "oom_killed": False,
            "restart_count": 0,
            "health": None,
        }
        healthy_frigate = {
            "status": "running",
            "running": True,
            "oom_killed": False,
            "restart_count": 0,
            "health": "healthy",
        }
        logs = mock.Mock(name="logs")
        client = mock.Mock(spec=sampler.DockerClient)
        client.verify_local_daemon.return_value = ("6" * 64, "7" * 64)
        observation = sampler.ObservationOutcome(181, 900.0, 100.0, 0, 0)
        with (
            mock.patch.object(sampler, "candidate_state", return_value=stopped),
            mock.patch.object(sampler, "observed_state", return_value=healthy_frigate),
            mock.patch.object(sampler.time, "time", return_value=101.0),
        ):
            result = sampler.validate_stop_completion(
                args, client, candidate, frigate, logs, observation
            )
        logs.scan.assert_called_once_with(101.0)
        self.assertEqual(result["candidate"], stopped)


class RetiredSamplerOrchestrationTests(unittest.TestCase):
    def test_sampler_cli_does_not_expose_retired_gate_or_supervisor(self) -> None:
        option_strings = {
            option
            for action in sampler.parser()._actions
            for option in action.option_strings
        }
        self.assertNotIn("--emit-systemd-run-script", option_strings)
        self.assertFalse(hasattr(sampler, "emit_systemd_run_script"))
        self.assertFalse(hasattr(sampler, "run_gate"))


@unittest.skip("pre-amendment sampler orchestration is intentionally retired")
class RunGateCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = CandidateIdentityTests.binding()
        self.client = mock.Mock(name="docker_client")
        self.client.verify_local_daemon.return_value = ("6" * 64, "7" * 64)
        self.frigate_binding = sampler.ObservedBinding(
            "frigate",
            "9" * 64,
            "sha256:" + "8" * 64,
        )
        self.args = SimpleNamespace(
            expected_docker_daemon_id="daemon-id",
            expected_host_boot_id="11111111-1111-1111-1111-111111111111",
            sampler_sha256="7" * 64,
            camera_expectations=Path("unused-camera-expectations.json"),
            frigate_container="frigate",
            output=Path("unused-evidence.jsonl"),
            leave_running_on_pass=False,
            boundary_expectation=CandidateIdentityTests.boundary_expectation(),
            disposable_root=CandidateIdentityTests.DISPOSABLE_ROOT,
        )

    @contextlib.contextmanager
    def patched_gate(
        self,
        *,
        evidence: FakeEvidence | None,
        open_error: BaseException | None = None,
        observation_result: object | None = None,
        observation_error: BaseException | None = None,
        stop_error: BaseException | None = None,
    ):
        with contextlib.ExitStack() as stack:
            docker_class = stack.enter_context(
                mock.patch.object(sampler, "DockerClient", return_value=self.client)
            )
            bind = stack.enter_context(
                mock.patch.object(
                    sampler,
                    "bind_candidate",
                    return_value=self.binding,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "sha256_file",
                    return_value=self.args.sampler_sha256,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "load_camera_expectations",
                    return_value={"camera": 10.0},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "bind_observed_container",
                    return_value=self.frigate_binding,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "observed_state",
                    return_value={"running": True, "health": "healthy"},
                )
            )
            stack.enter_context(
                mock.patch.object(sampler, "install_signal_handlers", return_value={})
            )
            stack.enter_context(
                mock.patch.object(
                    sampler,
                    "IncrementalLogScanner",
                    return_value=mock.Mock(name="log_scanner"),
                )
            )
            stop_completion = stack.enter_context(
                mock.patch.object(
                    sampler,
                    "validate_stop_completion",
                    return_value={"verified": True},
                )
            )
            if open_error is None:
                evidence_open = stack.enter_context(
                    mock.patch.object(
                        sampler.EvidenceWriter,
                        "open",
                        return_value=evidence,
                    )
                )
            else:
                evidence_open = stack.enter_context(
                    mock.patch.object(
                        sampler.EvidenceWriter,
                        "open",
                        side_effect=open_error,
                    )
                )
            if observation_error is None:
                observe = stack.enter_context(
                    mock.patch.object(
                        sampler,
                        "observe_gate",
                        return_value=observation_result
                        or sampler.ObservationOutcome(181, 900.0, 1_000.0, 0, 0),
                    )
                )
            else:
                observe = stack.enter_context(
                    mock.patch.object(
                        sampler,
                        "observe_gate",
                        side_effect=observation_error,
                    )
                )
            if stop_error is None:
                stop = stack.enter_context(
                    mock.patch.object(
                        sampler,
                        "stop_bound_candidate",
                        return_value={"verified_stopped": True},
                    )
                )
            else:
                stop = stack.enter_context(
                    mock.patch.object(
                        sampler,
                        "stop_bound_candidate",
                        side_effect=stop_error,
                    )
                )
            yield {
                "docker_class": docker_class,
                "bind": bind,
                "evidence_open": evidence_open,
                "observe": observe,
                "stop": stop,
                "stop_completion": stop_completion,
            }

    def assert_exact_cleanup(self, stop: mock.Mock, *, calls: int = 1) -> None:
        self.assertEqual(stop.call_count, calls)
        for invocation in stop.call_args_list:
            self.assertIs(invocation.args[0], self.client)
            self.assertIs(invocation.args[1], self.binding)
            self.assertIs(invocation.args[2], self.args)

    def assert_no_pass(self, evidence: FakeEvidence | None) -> None:
        if evidence is None:
            return
        self.assertFalse(
            any(record.get("event") == "gate_pass" for record in evidence.records)
        )
        self.assertFalse(any(seal.get("outcome") == "pass" for seal in evidence.seals))

    def test_evidence_open_failure_after_binding_still_stops_exact_candidate(
        self,
    ) -> None:
        with self.patched_gate(
            evidence=None,
            open_error=OSError("simulated evidence open failure"),
        ) as patched:
            with self.assertRaisesRegex(sampler.GateAbort, "unexpected_oserror"):
                sampler.run_gate(self.args)

        patched["bind"].assert_called_once_with(self.client, self.args)
        patched["evidence_open"].assert_called_once()
        patched["observe"].assert_not_called()
        self.assert_exact_cleanup(patched["stop"])

    def test_evidence_write_or_fsync_failure_still_stops_and_never_passes(self) -> None:
        evidence = FakeEvidence(fail_writes=True)

        def fail_during_observation(*arguments: object) -> object:
            observed_evidence = arguments[1]
            assert isinstance(observed_evidence, FakeEvidence)
            observed_evidence.write({"event": "gate_start"})
            return sampler.ObservationOutcome(181, 900.0, 1_000.0, 0, 0)

        with self.patched_gate(evidence=evidence) as patched:
            patched["observe"].side_effect = fail_during_observation
            with self.assertRaisesRegex(sampler.GateAbort, "unexpected_oserror"):
                sampler.run_gate(self.args)

        self.assert_exact_cleanup(patched["stop"])
        self.assert_no_pass(evidence)
        self.assertTrue(evidence.closed)

    def test_observation_exception_stops_exact_candidate_and_seals_abort(self) -> None:
        evidence = FakeEvidence()
        with self.patched_gate(
            evidence=evidence,
            observation_error=sampler.GateAbort("observation failed"),
        ) as patched:
            with self.assertRaisesRegex(sampler.GateAbort, "observation_failed"):
                sampler.run_gate(self.args)

        self.assert_exact_cleanup(patched["stop"])
        self.assert_no_pass(evidence)
        self.assertEqual(evidence.records[-1]["event"], "gate_abort")
        self.assertEqual(evidence.seals[-1]["outcome"], "abort")
        self.assertTrue(evidence.closed)

    def test_keyboard_interrupt_and_sigterm_abort_both_cleanup(self) -> None:
        failures = (
            (KeyboardInterrupt(), "operator_interrupt"),
            (sampler.GateAbort("sampler received SIGTERM"), "sampler_received_sigterm"),
        )
        for failure, reason in failures:
            with self.subTest(reason=reason):
                evidence = FakeEvidence()
                with self.patched_gate(
                    evidence=evidence,
                    observation_error=failure,
                ) as patched:
                    with self.assertRaisesRegex(sampler.GateAbort, reason):
                        sampler.run_gate(self.args)
                self.assert_exact_cleanup(patched["stop"])
                self.assert_no_pass(evidence)
                self.assertEqual(evidence.records[-1]["event"], "gate_abort")
                self.assertEqual(evidence.records[-1]["reason"], reason)

    def test_stop_verification_failure_cannot_be_recorded_as_pass(self) -> None:
        evidence = FakeEvidence()
        stop_error = sampler.GateAbort("candidate remained running after cleanup")
        with self.patched_gate(
            evidence=evidence,
            observation_result=sampler.ObservationOutcome(181, 900.0, 1_000.0, 0, 0),
            stop_error=stop_error,
        ) as patched:
            with self.assertRaisesRegex(sampler.GateAbort, "cleanup_unverified"):
                sampler.run_gate(self.args)

        self.assert_exact_cleanup(patched["stop"], calls=2)
        self.assert_no_pass(evidence)
        self.assertEqual(evidence.records[-1]["event"], "gate_abort")
        self.assertFalse(evidence.records[-1]["cleanup"]["verified_stopped"])
        self.assertEqual(evidence.seals[-1]["outcome"], "abort")


class OuterSupervisorTests(unittest.TestCase):
    @staticmethod
    def args() -> object:
        binding = CandidateIdentityTests.binding()
        return SimpleNamespace(
            container=binding.name,
            output=Path("/root/subgen-gate/evidence.jsonl"),
            duration_seconds=900,
            interval_seconds=5,
            start_timeout_seconds=120,
            expected_memory_bytes=17 * sampler.GIB,
            gpu_free_floor_bytes=8 * sampler.GIB,
            host_reserve_bytes=4 * sampler.GIB,
            frigate_container="frigate",
            frigate_stats_url=sampler.EXACT_ENDPOINTS["frigate"],
            ollama_url=sampler.EXACT_ENDPOINTS["ollama"],
            candidate_status_url=sampler.EXACT_ENDPOINTS["candidate"],
            candidate_mode="runtime",
            expected_model="medium",
            expected_chunk_minutes=5,
            expected_profiler_returncode=None,
            expected_container_id=binding.container_id,
            expected_image_config=binding.image_config,
            candidate_oci_index=CandidateIdentityTests.OCI_INDEX,
            candidate_config_digest=CandidateIdentityTests.IMAGE_CONFIG,
            candidate_layer_diff_ids=copy.deepcopy(
                CandidateIdentityTests.LAYER_DIFF_IDS
            ),
            model_envelope_catalog_sha256=CandidateIdentityTests.CATALOG_SHA256,
            phase_a_fixture_record_sha256=(
                CandidateIdentityTests.PHASE_A_FIXTURE_RECORD_SHA256
            ),
            phase_b_fixture_record_sha256=(
                CandidateIdentityTests.PHASE_B_FIXTURE_RECORD_SHA256
            ),
            model_revision=CandidateIdentityTests.MODEL_REVISION,
            expected_command_sha256=binding.command_digest,
            runtime_commit=binding.runtime_commit,
            gate_token=CandidateIdentityTests.TOKEN,
            gate_role=binding.gate_role,
            camera_expectations=Path("/root/subgen-gate/cameras.json"),
            sampler_sha256="7" * 64,
            expected_docker_daemon_id="daemon-id",
            expected_host_boot_id="11111111-1111-1111-1111-111111111111",
            boundary_manifest=Path("/root/subgen-gate/boundary.json"),
            boundary_manifest_sha256="2" * 64,
            disposable_root=CandidateIdentityTests.DISPOSABLE_ROOT,
            _test_skip_disposable_filesystem_check=True,
            boundary_expectation=CandidateIdentityTests.boundary_expectation(),
            leave_running_on_pass=False,
            cleanup_only=False,
            systemd_stop_post=False,
            emit_systemd_run_script=Path("/root/subgen-gate/run.sh"),
            emit_boundary_manifest=None,
        )

    @classmethod
    def profiler_args(cls, model: str, expected_returncode: int) -> object:
        args = cls.args()
        item = CandidateIdentityTests.profiler_item(model)
        expectation = sampler.canonical_execution_boundary(
            item,
            disposable_root=CandidateIdentityTests.DISPOSABLE_ROOT,
            model_envelope_catalog_sha256=CandidateIdentityTests.CATALOG_SHA256,
            phase_a_fixture_record_sha256=(
                CandidateIdentityTests.PHASE_A_FIXTURE_RECORD_SHA256
            ),
            phase_b_fixture_record_sha256=(
                CandidateIdentityTests.PHASE_B_FIXTURE_RECORD_SHA256
            ),
            candidate_identity=CandidateIdentityTests.boundary_candidate_identity(
                item, model=model
            ),
            docker_daemon_identity=CandidateIdentityTests.docker_daemon_identity(),
            filesystem_check=False,
        )
        binding = sampler.CandidateBinding(
            name=f"subgen-task11b-profile-{model}",
            container_id=CandidateIdentityTests.CONTAINER_ID,
            image_config=CandidateIdentityTests.OCI_INDEX,
            runtime_commit=CandidateIdentityTests.RUNTIME_COMMIT,
            gate_role=f"profile-{model}",
            gate_token_digest=sampler.sha256_bytes(
                CandidateIdentityTests.TOKEN.encode("utf-8")
            ),
            command_digest=sampler._command_digest(item),
            boundary_digest=sampler.execution_boundary_digest(expectation),
        )
        args.container = binding.name
        args.expected_memory_bytes = 12 * sampler.GIB
        args.candidate_mode = "profiler"
        args.expected_model = model
        args.expected_profiler_returncode = expected_returncode
        args.expected_command_sha256 = binding.command_digest
        args.gate_role = binding.gate_role
        args.boundary_expectation = sampler.BoundaryExpectation(
            document=expectation,
            file_sha256="2" * 64,
            canonical_sha256=binding.boundary_digest,
        )
        args.leave_running_on_pass = False
        args._binding = binding
        return args

    @unittest.skip("sampler-owned supervisor is intentionally retired")
    def test_generator_registers_exact_exec_stop_post_before_start(self) -> None:
        args = self.args()
        payloads: list[bytes] = []

        def capture(_path: Path, payload: bytes, mode: int) -> None:
            self.assertEqual(mode, 0o700)
            payloads.append(payload)

        with (
            mock.patch.object(sampler, "sha256_file", return_value="7" * 64),
            mock.patch.object(
                sampler, "DockerClient", return_value=mock.Mock(name="docker")
            ),
            mock.patch.object(
                sampler,
                "bind_candidate",
                return_value=CandidateIdentityTests.binding(),
            ),
            mock.patch.object(
                sampler, "_write_private_create_only", side_effect=capture
            ),
        ):
            sampler.emit_systemd_run_script(args)

        self.assertEqual(len(payloads), 1)
        script = payloads[0].decode("utf-8")
        self.assertIn("/usr/bin/systemd-run", script)
        self.assertIn("ExecStopPost=", script)
        self.assertIn("--cleanup-only", script)
        self.assertIn("--systemd-stop-post", script)
        self.assertIn("--property=Restart=no", script)
        self.assertIn("--property=KillMode=mixed", script)
        self.assertIn("--property=TimeoutStopSec=300s", script)
        self.assertIn("--property=RuntimeMaxSec=1320s", script)
        self.assertEqual(script.count("ExecStopPost="), 1)
        self.assertEqual(script.count("--expected-chunk-minutes"), 2)
        self.assertIn("--expected-chunk-minutes 5", script)
        self.assertNotIn(
            args.expected_container_id, script.split("--unit=", 1)[1].split()[0]
        )

    @unittest.skip("sampler-owned supervisor is intentionally retired")
    def test_profiler_wrapper_keeps_cleanup_armed_through_rc_validation(self) -> None:
        for model, returncode in (("large-v3", 3), ("medium", 0)):
            args = self.profiler_args(model, returncode)
            payloads: list[bytes] = []
            with (
                self.subTest(model=model),
                mock.patch.object(sampler, "sha256_file", return_value="7" * 64),
                mock.patch.object(
                    sampler, "DockerClient", return_value=mock.Mock(name="docker")
                ),
                mock.patch.object(
                    sampler, "bind_candidate", return_value=args._binding
                ),
                mock.patch.object(
                    sampler,
                    "_write_private_create_only",
                    side_effect=lambda _path, payload, _mode: payloads.append(payload),
                ),
            ):
                sampler.validate_args(args)
                sampler.emit_systemd_run_script(args)
            script = payloads[0].decode("utf-8")
            self.assertIn("ExecStopPost=", script)
            self.assertEqual(script.count("--expected-profiler-returncode"), 2)
            self.assertIn(f"--expected-profiler-returncode {returncode}", script)
            self.assertEqual(script.count("--expected-chunk-minutes"), 2)
            self.assertIn("--expected-chunk-minutes 5", script)
            self.assertNotIn("--leave-running-on-pass", script)
            self.assertIn("--property=RuntimeMaxSec=1320s", script)

        unsafe = self.profiler_args("large-v3", 3)
        unsafe.leave_running_on_pass = True
        with self.assertRaisesRegex(sampler.GateAbort, "cannot_retain"):
            sampler.validate_args(unsafe)

        wrong_chunk = self.profiler_args("large-v3", 3)
        wrong_chunk.expected_chunk_minutes = 20
        with self.assertRaisesRegex(sampler.GateAbort, "five_minute_chunks"):
            sampler.validate_args(wrong_chunk)

    def test_boundary_manifest_generator_is_create_only_with_full_preimages(
        self,
    ) -> None:
        args = self.args()
        args.emit_boundary_manifest = Path("/root/subgen-gate/boundary.json")
        client = mock.Mock(name="docker")
        client.verify_local_daemon.return_value = ("6" * 64, "7" * 64)
        item = CandidateIdentityTests.candidate_item()
        state = item["State"]
        assert isinstance(state, dict)
        state["Status"] = "created"
        state["Running"] = False
        state["HealthStatus"] = None
        client.inspect.return_value = item
        writes: list[tuple[Path, bytes, int]] = []

        with (
            mock.patch.object(sampler, "sha256_file", return_value="7" * 64),
            mock.patch.object(sampler, "DockerClient", return_value=client),
            mock.patch.object(
                sampler,
                "_write_private_create_only",
                side_effect=lambda path, payload, mode: writes.append(
                    (path, payload, mode)
                ),
            ),
        ):
            sampler.emit_boundary_manifest(args)

        self.assertEqual(len(writes), 1)
        path, payload, mode = writes[0]
        self.assertEqual(path, args.emit_boundary_manifest)
        self.assertEqual(mode, 0o600)
        parsed = json.loads(payload)
        self.assertEqual(parsed["schema"], 4)
        self.assertEqual(
            parsed["docker_daemon_identity"],
            CandidateIdentityTests.docker_daemon_identity(),
        )
        self.assertEqual(
            parsed["model_envelope_catalog_sha256"],
            CandidateIdentityTests.CATALOG_SHA256,
        )
        self.assertIn("environment", parsed)
        self.assertIn("config", parsed)
        self.assertIn("host_config", parsed)
        self.assertIn("environment_sha256", parsed)
        self.assertEqual(
            sampler.sha256_bytes(sampler._canonical_json_bytes(parsed["environment"])),
            parsed["environment_sha256"],
        )

    def test_killed_sampler_stop_post_stops_only_exact_revalidated_id(self) -> None:
        args = self.args()
        args.systemd_stop_post = True
        item = CandidateIdentityTests.candidate_item()

        class FakeClient:
            def verify_local_daemon(self) -> tuple[str, str]:
                return ("6" * 64, "7" * 64)

            def inspect(
                self, reference: str, *, missing_ok: bool = False
            ) -> dict[str, object] | None:
                del missing_ok
                self.assert_reference = reference
                return item

        client = FakeClient()
        stopped: list[str] = []

        def stop(
            observed_client: object, binding: object, observed_args: object
        ) -> dict[str, object]:
            self.assertIs(observed_client, client)
            self.assertIs(observed_args, args)
            assert isinstance(binding, sampler.CandidateBinding)
            stopped.append(binding.container_id)
            return {"verified_stopped": True}

        worker = sampler.subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=sampler.subprocess.DEVNULL,
            stdout=sampler.subprocess.DEVNULL,
            stderr=sampler.subprocess.DEVNULL,
        )
        worker.kill()
        worker.wait(timeout=5)
        self.assertNotEqual(worker.returncode, 0)
        with (
            mock.patch.object(sampler, "sha256_file", return_value="7" * 64),
            mock.patch.object(sampler, "DockerClient", return_value=client),
            mock.patch.object(sampler, "stop_bound_candidate", side_effect=stop),
            mock.patch.dict(os.environ, {"SERVICE_RESULT": "signal"}),
        ):
            sampler.cleanup_only(args)

        self.assertEqual(stopped, [args.expected_container_id])
        self.assertEqual(client.assert_reference, args.expected_container_id)

    def test_cleanup_identity_mismatch_causes_no_mutation(self) -> None:
        args = self.args()
        item = CandidateIdentityTests.candidate_item()
        item["Image"] = "sha256:" + "e" * 64
        client = mock.Mock(name="docker")
        client.verify_local_daemon.return_value = ("6" * 64, "7" * 64)
        client.inspect.return_value = item
        stop = mock.Mock(name="stop")
        with (
            mock.patch.object(sampler, "sha256_file", return_value="7" * 64),
            mock.patch.object(sampler, "DockerClient", return_value=client),
            mock.patch.object(sampler, "stop_bound_candidate", stop),
        ):
            with self.assertRaisesRegex(sampler.GateAbort, "identity_changed"):
                sampler.cleanup_only(args)
        stop.assert_not_called()


class CompleteWriteTests(unittest.TestCase):
    def test_write_all_retries_partial_writes_until_every_byte_is_written(self) -> None:
        captured = bytearray()

        def partial(_fd: int, payload: object) -> int:
            chunk = bytes(payload)[:3]
            captured.extend(chunk)
            return len(chunk)

        with mock.patch.object(sampler.os, "write", side_effect=partial) as writer:
            sampler._write_all_fd(123, b"abcdefghij")
        self.assertEqual(bytes(captured), b"abcdefghij")
        self.assertGreater(writer.call_count, 1)


@unittest.skipUnless(
    os.name == "posix" and hasattr(os, "geteuid"),
    "Boundary manifest ownership requires POSIX",
)
class BoundaryManifestTests(unittest.TestCase):
    def test_disposable_mount_source_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "gate"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            real = root / "media"
            real.mkdir()
            sampler._validate_disposable_source(str(real), str(root))
            outside = parent / "production-media"
            outside.mkdir()
            link = root / "media-link"
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(sampler.GateAbort, "not_private_and_real"):
                sampler._validate_disposable_source(str(link), str(root))

    def test_manifest_requires_independent_sha_and_owner_only_file(self) -> None:
        boundary = CandidateIdentityTests.boundary_expectation().document
        payload = (
            json.dumps(boundary, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "private"
            parent.mkdir(mode=0o700)
            os.chmod(parent, 0o700)
            manifest = parent / "boundary.json"
            manifest.write_bytes(payload)
            os.chmod(manifest, 0o600)
            expectation = sampler.load_boundary_expectation(
                manifest, hashlib.sha256(payload).hexdigest()
            )
            self.assertEqual(expectation.document, boundary)
            with self.assertRaisesRegex(sampler.GateAbort, "checksum_mismatch"):
                sampler.load_boundary_expectation(manifest, "0" * 64)
            os.chmod(manifest, 0o640)
            with self.assertRaisesRegex(sampler.GateAbort, "not_private"):
                sampler.load_boundary_expectation(
                    manifest, hashlib.sha256(payload).hexdigest()
                )


@unittest.skipUnless(
    os.name == "posix" and hasattr(os, "geteuid"),
    "EvidenceWriter ownership and dir-fd semantics require POSIX",
)
class EvidenceWriterTests(unittest.TestCase):
    def test_seal_is_verified_before_final_evidence_name_is_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "private"
            parent.mkdir(mode=0o700)
            os.chmod(parent, 0o700)
            output = parent / "gate.jsonl"
            seal_path = output.with_name(output.name + ".seal.json")
            writer = sampler.EvidenceWriter.open(output, "d" * 64)
            writer.write({"event": "sample"})
            real_link = os.link
            observed = False

            def assert_sealed_before_link(
                source: object, destination: object, **kwargs: object
            ) -> None:
                nonlocal observed
                if destination == output.name:
                    self.assertTrue(seal_path.is_file())
                    seal = json.loads(seal_path.read_text(encoding="utf-8"))
                    self.assertEqual(seal["outcome"], "pass")
                    self.assertFalse(output.exists())
                    observed = True
                real_link(source, destination, **kwargs)

            with mock.patch.object(
                sampler.os, "link", side_effect=assert_sealed_before_link
            ):
                writer.seal(
                    outcome="pass",
                    sampler_sha256="1" * 64,
                    image_config="sha256:" + "2" * 64,
                    cleanup={"verified_stopped": True},
                )
            writer.close()
            self.assertTrue(observed)
            self.assertTrue(output.is_file())

    def test_final_evidence_metadata_is_reverified_before_seal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "private"
            parent.mkdir(mode=0o700)
            os.chmod(parent, 0o700)
            writer = sampler.EvidenceWriter.open(parent / "gate.jsonl", "e" * 64)
            writer.write({"event": "sample"})
            real_fstat = os.fstat
            calls = 0

            def mutate_final_metadata(fd: int) -> object:
                nonlocal calls
                calls += 1
                observed = real_fstat(fd)
                # Three partial-file identity checks, then the newly required
                # seal verification, precede the final-evidence verification.
                if calls != 5:
                    return observed
                return SimpleNamespace(
                    st_mode=(observed.st_mode & ~0o777) | 0o644,
                    st_uid=observed.st_uid,
                    st_dev=observed.st_dev,
                    st_ino=observed.st_ino,
                    st_size=observed.st_size,
                )

            try:
                with mock.patch.object(
                    sampler.os, "fstat", side_effect=mutate_final_metadata
                ):
                    with self.assertRaisesRegex(
                        sampler.GateAbort, "final_evidence_verification_failed"
                    ):
                        writer.seal(
                            outcome="abort",
                            sampler_sha256="1" * 64,
                            image_config="sha256:" + "2" * 64,
                            cleanup={"verified_stopped": True},
                        )
            finally:
                writer.close()

    def test_seal_is_atomic_hash_bound_and_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "private"
            parent.mkdir(mode=0o700)
            os.chmod(parent, 0o700)
            output = parent / "gate.jsonl"
            writer = sampler.EvidenceWriter.open(output, "f" * 64)
            partial = parent / writer.partial_name
            self.assertEqual(stat.S_IMODE(partial.stat().st_mode), 0o600)

            first = {"event": "sample", "sample": 1}
            first_line = (
                json.dumps(first, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            writer.write(first)
            real_write = os.write

            def partial_write(fd: int, payload: object) -> int:
                raw = bytes(payload)
                return real_write(fd, raw[: max(1, len(raw) // 3)])

            with mock.patch.object(
                sampler.os, "write", side_effect=partial_write
            ) as write_call:
                digest = writer.seal(
                    outcome="pass",
                    sampler_sha256="1" * 64,
                    image_config="sha256:" + "2" * 64,
                    cleanup={"verified_stopped": True},
                )
            self.assertGreater(write_call.call_count, 1)
            writer.close()

            seal_path = output.with_name(output.name + ".seal.json")
            self.assertFalse(partial.exists())
            self.assertTrue(output.is_file())
            self.assertTrue(seal_path.is_file())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(seal_path.stat().st_mode), 0o600)
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), digest)

            records = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0], first)
            self.assertEqual(records[1]["event"], "evidence_seal_record")
            self.assertEqual(records[1]["records_before_seal"], 1)
            self.assertEqual(
                records[1]["prefix_sha256"], hashlib.sha256(first_line).hexdigest()
            )

            seal = json.loads(seal_path.read_text(encoding="utf-8"))
            self.assertEqual(seal["outcome"], "pass")
            self.assertEqual(seal["evidence_sha256"], digest)
            self.assertEqual(seal["record_count"], 2)
            self.assertEqual(seal["evidence_bytes"], output.stat().st_size)
            self.assertEqual(seal["cleanup"], {"verified_stopped": True})


class AmendedLiveProbeTests(unittest.TestCase):
    @staticmethod
    def fixture_record(media: Path, output: Path, marker: Path) -> dict[str, object]:
        metadata = media.stat()
        fixture_sha256 = hashlib.sha256(media.read_bytes()).hexdigest()
        return {
            "schema": "subgen.task11b.fixture-record/v1",
            "phase": "a",
            "workload_identity": {
                "fixture_sha256": fixture_sha256,
                "task": "translate",
                "language": "en",
                "cursor_start_ms": 0,
                "total_duration_ms": 310_000,
            },
            "host_media": str(media),
            "container_media": "/fixtures/phase-a/input.mkv",
            "host_output": str(output),
            "container_output": "/task11b-output/phase-a/input.en.srt",
            "host_marker": str(marker),
            "file_identity": {
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "size_bytes": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "ctime_ns": metadata.st_ctime_ns,
                "owner_uid": metadata.st_uid,
                "mode": stat.S_IMODE(metadata.st_mode),
                "link_count": metadata.st_nlink,
                "sha256": fixture_sha256,
            },
        }

    def test_fixture_record_is_exact_and_revalidated_against_read_only_mount(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture_dir = root / "fixtures" / "phase-a"
            fixture_dir.mkdir(parents=True)
            media = fixture_dir / "input.mkv"
            media.write_bytes(b"fixture audio")
            output_root = root / "task11b-output" / "phase-a"
            output = output_root / "input.en.srt"
            marker = root / "monitor" / sampler.FAILURE_MARKER_FILENAME
            output.parent.mkdir(parents=True)
            marker.parent.mkdir(parents=True)
            record = self.fixture_record(media, output, marker)
            self.assertIs(sampler.validate_fixture_record_document(record), record)
            fixture_mount = {
                "type": "bind",
                "source": str(fixture_dir),
                "destination": "/fixtures/phase-a",
                "mode": "ro",
                "read_write": False,
                "propagation": "rprivate",
            }
            output_mount = {
                "type": "bind",
                "source": str(output_root),
                "destination": "/task11b-output/phase-a",
                "mode": "rw",
                "read_write": True,
                "propagation": "rprivate",
            }
            binding = sampler.revalidate_fixture_record(
                record,
                fixture_mount,
                output_mount,
            )
            self.assertEqual(binding.duration_ms, 310_000)
            self.assertEqual(binding.host_media, media)
            self.assertEqual(binding.container_media, "/fixtures/phase-a/input.mkv")
            self.assertEqual(
                binding.container_output,
                "/task11b-output/phase-a/input.en.srt",
            )
            self.assertEqual(
                binding.container_marker,
                sampler.FAILURE_MARKER_CONTAINER_PATH,
            )
            self.assertEqual(binding.output_boundary_mount, output_mount)
            self.assertEqual(
                binding.workload_sha256,
                sampler.sha256_bytes(
                    sampler.canonical_json_line(record["workload_identity"])
                ),
            )

            media.write_bytes(b"replacement")
            with self.assertRaisesRegex(sampler.GateAbort, "fixture.*changed"):
                sampler.revalidate_fixture_record(
                    record,
                    binding.boundary_mount,
                    binding.output_boundary_mount,
                )

    def test_fixture_output_paths_require_separate_exact_writable_mount(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture_dir = root / "fixtures" / "phase-a"
            fixture_dir.mkdir(parents=True)
            media = fixture_dir / "input.mkv"
            media.write_bytes(b"fixture audio")
            output_root = root / "task11b-output" / "phase-a"
            output_root.mkdir(parents=True)
            output = output_root / "input.en.srt"
            marker_root = root / "monitor"
            marker_root.mkdir()
            marker = marker_root / sampler.FAILURE_MARKER_FILENAME
            record = self.fixture_record(media, output, marker)
            fixture_mount = {
                "type": "bind",
                "source": str(fixture_dir),
                "destination": "/fixtures/phase-a",
                "mode": "ro",
                "read_write": False,
                "propagation": "rprivate",
            }
            output_mount = {
                "type": "bind",
                "source": str(output_root),
                "destination": "/task11b-output/phase-a",
                "mode": "rw",
                "read_write": True,
                "propagation": "rprivate",
            }
            read_only_output = dict(output_mount, mode="ro", read_write=False)
            with self.assertRaisesRegex(sampler.GateAbort, "writable"):
                sampler.revalidate_fixture_record(
                    record, fixture_mount, read_only_output
                )

            wrong_fixture_destination = dict(
                fixture_mount, destination="/fixtures/phase-b"
            )
            with self.assertRaisesRegex(sampler.GateAbort, "destination"):
                sampler.revalidate_fixture_record(
                    record, wrong_fixture_destination, output_mount
                )

            wrong_destination = dict(
                output_mount, destination="/task11b-output/caller-selected"
            )
            with self.assertRaisesRegex(sampler.GateAbort, "destination"):
                sampler.revalidate_fixture_record(
                    record, fixture_mount, wrong_destination
                )

            arbitrary_output = copy.deepcopy(record)
            arbitrary_output["container_output"] = (
                "/task11b-output/phase-a/arbitrary.en.srt"
            )
            with self.assertRaisesRegex(sampler.GateAbort, "deterministic"):
                sampler.revalidate_fixture_record(
                    arbitrary_output, fixture_mount, output_mount
                )

            caller_selected_marker = copy.deepcopy(record)
            caller_selected_marker["host_marker"] = str(
                output_root / sampler.FAILURE_MARKER_FILENAME
            )
            with self.assertRaisesRegex(sampler.GateAbort, "runtime_registry"):
                sampler.revalidate_fixture_record(
                    caller_selected_marker, fixture_mount, output_mount
                )

            overlapping_output = dict(
                output_mount,
                source=str(root),
            )
            with self.assertRaisesRegex(sampler.GateAbort, "overlapped"):
                sampler.revalidate_fixture_record(
                    record, fixture_mount, overlapping_output
                )

    def test_continuous_candidate_log_counts_split_cuda_oom_without_disclosure(
        self,
    ) -> None:
        stream = sampler.ContinuousCandidateLog(
            mock.Mock(name="docker"),
            CandidateIdentityTests.binding(),
            mock.Mock(name="process"),
            max_bytes=128,
        )
        stream._consume_stdout(b"prefix CUDA out of mem")
        stream._consume_stdout(b"ory\nCUDA error: out of memory\n")
        snapshot = stream._snapshot_without_io()
        self.assertEqual(snapshot.cuda_oom_matches, 2)
        self.assertEqual(snapshot.byte_cursor, 52)
        self.assertTrue(snapshot.continuous)
        self.assertNotIn("CUDA", repr(snapshot))
        with self.assertRaisesRegex(sampler.GateAbort, "byte_limit"):
            stream._consume_stdout(b"x" * 128)

    def test_continuous_candidate_log_attaches_from_container_start(self) -> None:
        client = mock.Mock(name="docker")
        client._argv.return_value = ["docker", "logs", "candidate"]
        process = mock.Mock(name="log-follower")
        process.stdout = mock.Mock(name="stdout")
        process.stderr = mock.Mock(name="stderr")
        process.stdout.fileno.return_value = 10
        process.stderr.fileno.return_value = 11
        with (
            mock.patch.object(sampler.ContinuousCandidateLog, "_assert_bound_source"),
            mock.patch.object(sampler.subprocess, "Popen", return_value=process),
            mock.patch.object(sampler.os, "set_blocking"),
        ):
            sampler.ContinuousCandidateLog.open(
                client, CandidateIdentityTests.binding()
            )
        requested = client._argv.call_args.args
        self.assertEqual(requested[:3], ("logs", "--follow", "--timestamps"))
        self.assertEqual(requested[-1], CandidateIdentityTests.CONTAINER_ID)
        self.assertNotIn("--since", requested)

    def test_candidate_log_seal_waits_for_clean_follower_eof(self) -> None:
        process = mock.Mock(name="log-follower")
        process.stdout = mock.Mock(name="stdout")
        process.stderr = mock.Mock(name="stderr")
        process.poll.side_effect = [None, 0, 0]
        stream = sampler.ContinuousCandidateLog(
            mock.Mock(name="docker"),
            CandidateIdentityTests.binding(),
            process,
        )
        drains = 0

        def drain() -> None:
            nonlocal drains
            drains += 1
            if drains == 1:
                stream._consume_stdout(b"complete log\n")
            else:
                stream._stdout_eof = True
                stream._stderr_eof = True

        with (
            mock.patch.object(stream, "_assert_bound_source"),
            mock.patch.object(stream, "_drain", side_effect=drain),
            mock.patch.object(sampler.time, "monotonic", side_effect=[0.0, 0.1]),
            mock.patch.object(sampler.time, "sleep") as sleep,
        ):
            snapshot = stream.close_after_stop(timeout_seconds=1.0)
        self.assertTrue(snapshot.continuous)
        self.assertEqual(snapshot.byte_cursor, len(b"complete log\n"))
        self.assertEqual(drains, 2)
        sleep.assert_called_once()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_candidate_log_seal_rejects_missing_follower_eof(self) -> None:
        process = mock.Mock(name="log-follower")
        process.stdout = mock.Mock(name="stdout")
        process.stderr = mock.Mock(name="stderr")
        process.poll.return_value = 0
        stream = sampler.ContinuousCandidateLog(
            mock.Mock(name="docker"),
            CandidateIdentityTests.binding(),
            process,
        )
        with (
            mock.patch.object(stream, "_assert_bound_source"),
            mock.patch.object(stream, "_drain"),
            mock.patch.object(sampler.time, "monotonic", side_effect=[0.0, 1.0]),
        ):
            with self.assertRaisesRegex(sampler.GateAbort, "did_not_reach_eof"):
                stream.close_after_stop(timeout_seconds=0.5)

    def test_kernel_journal_cursor_advances_without_reopening(self) -> None:
        event = json.dumps(
            {
                "__CURSOR": "s=entry1",
                "MESSAGE": "NVRM: Xid (PCI:0000:01:00): 31",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        results = [
            sampler.CommandResult(0, "-- cursor: s=tail\n"),
            sampler.CommandResult(0, event + "\n-- cursor: s=entry1\n"),
            sampler.CommandResult(0, "-- cursor: s=entry1\n"),
        ]
        with mock.patch.object(sampler, "bounded_command", side_effect=results) as run:
            cursor = sampler.KernelJournalCursor.open_at_tail()
            first = cursor.snapshot()
            second = cursor.snapshot()
        self.assertEqual(first.xid_matches, 1)
        self.assertEqual(second.xid_matches, 1)
        self.assertTrue(first.continuous and second.continuous)
        self.assertEqual(run.call_count, 3)

    def test_kernel_journal_rejects_empty_cursor_jump(self) -> None:
        results = [
            sampler.CommandResult(0, "-- cursor: s=tail\n"),
            sampler.CommandResult(0, "-- cursor: s=jump\n"),
        ]
        with mock.patch.object(sampler, "bounded_command", side_effect=results):
            cursor = sampler.KernelJournalCursor.open_at_tail()
            with self.assertRaisesRegex(sampler.GateAbort, "without_a_record"):
                cursor.snapshot()

    def test_cgroup_probe_rebinds_pid_path_events_and_stable_gpu_process_set(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc_root = root / "proc"
            cgroup_root = root / "cgroup"
            pid = 4242
            proc_pid = proc_root / str(pid)
            proc_pid.mkdir(parents=True)
            (proc_pid / "cgroup").write_text("0::/docker/candidate\n", encoding="ascii")
            cgroup = cgroup_root / "docker" / "candidate"
            cgroup.mkdir(parents=True)
            (cgroup / "cgroup.procs").write_text("4242\n", encoding="ascii")
            worker_cgroup = cgroup / "workers"
            worker_cgroup.mkdir()
            (worker_cgroup / "cgroup.procs").write_text("4243\n", encoding="ascii")
            (cgroup / "memory.events").write_text(
                "low 0\nhigh 0\nmax 1\noom 2\noom_kill 3\noom_group_kill 4\n",
                encoding="ascii",
            )
            item = CandidateIdentityTests.candidate_item()
            state = item["State"]
            assert isinstance(state, dict)
            state["Pid"] = pid
            client = mock.Mock(name="docker")
            client.inspect.return_value = item
            probe = sampler.CandidateCgroupProbe(
                client,
                CandidateIdentityTests.binding(),
                CandidateIdentityTests.args(),
            )
            gpu_uuid = "GPU-11111111-2222-3333-4444-555555555555"
            query = sampler.CommandResult(
                0,
                (
                    f"4242, {gpu_uuid}, 256\n"
                    f"4243, {gpu_uuid}, 128\n"
                    f"9999, {gpu_uuid}, 512\n"
                ),
            )
            with (
                mock.patch.object(sampler, "PROC_ROOT", proc_root),
                mock.patch.object(sampler, "CGROUP_ROOT", cgroup_root),
                mock.patch.object(sampler, "verify_candidate_item"),
            ):
                memory = probe.memory_events()
                with (
                    mock.patch.object(sampler, "bounded_command", return_value=query),
                    mock.patch.object(
                        sampler.time,
                        "monotonic_ns",
                        return_value=99_000_000_000,
                    ),
                ):
                    gpu = probe.attributed_gpu_bytes(gpu_uuid)
        self.assertEqual(memory.oom, 2)
        self.assertEqual(memory.oom_kill, 3)
        self.assertEqual(memory.oom_group_kill, 4)
        self.assertEqual(gpu.candidate_bytes, 384 * sampler.MIB)
        self.assertEqual(gpu.validated_monotonic_ns, 99_000_000_000)

    def test_profiler_release_attestation_requires_hold_pid_only_and_zero_gpu(
        self,
    ) -> None:
        probe = sampler.CandidateCgroupProbe(
            mock.Mock(name="docker"),
            CandidateIdentityTests.binding(),
            CandidateIdentityTests.args(),
        )
        method = getattr(probe, "profiler_release_attestation", None)
        self.assertIsNotNone(method, "profiler release attestation API is required")
        gpu_uuid = "GPU-11111111-2222-4333-8444-555555555555"
        cgroup = Path("/sys/fs/cgroup/private-candidate")
        with (
            mock.patch.object(
                probe,
                "_candidate_pid_and_cgroup",
                side_effect=[(4242, cgroup), (4242, cgroup)],
            ),
            mock.patch.object(probe, "_pid_set", side_effect=[{4242}, {4242}]),
            mock.patch.object(
                sampler, "bounded_command", return_value=sampler.CommandResult(0, "")
            ),
            mock.patch.object(sampler.time, "monotonic_ns", return_value=99),
        ):
            attestation = method(gpu_uuid)
        self.assertEqual(attestation["hold_pid_count"], 1)
        self.assertEqual(attestation["candidate_gpu_bytes"], 0)
        self.assertNotIn("pid", attestation)

        with (
            mock.patch.object(
                probe,
                "_candidate_pid_and_cgroup",
                side_effect=[(4242, cgroup), (4242, cgroup)],
            ),
            mock.patch.object(
                probe, "_pid_set", side_effect=[{4242, 4243}, {4242, 4243}]
            ),
            mock.patch.object(
                sampler, "bounded_command", return_value=sampler.CommandResult(0, "")
            ),
            self.assertRaisesRegex(sampler.GateAbort, "hold_process"),
        ):
            method(gpu_uuid)

        with (
            mock.patch.object(
                probe,
                "_candidate_pid_and_cgroup",
                side_effect=[(4242, cgroup), (4242, cgroup)],
            ),
            mock.patch.object(probe, "_pid_set", side_effect=[{4242}, {4242}]),
            mock.patch.object(
                sampler,
                "bounded_command",
                return_value=sampler.CommandResult(0, f"4242, {gpu_uuid}, 1\n"),
            ),
            self.assertRaisesRegex(sampler.GateAbort, "gpu_memory_was_not_released"),
        ):
            method(gpu_uuid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
