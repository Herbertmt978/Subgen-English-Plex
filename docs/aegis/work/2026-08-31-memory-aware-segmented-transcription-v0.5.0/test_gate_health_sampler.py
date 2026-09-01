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
        "embeddings": {name: 4.5 for name in sampler.REQUIRED_EMBEDDING_SPEEDS},
        "service": {"last_updated": now_wall},
    }


def healthy_candidate_status() -> dict[str, object]:
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
            "gpu_total_bytes": 24 * sampler.GIB,
            "gpu_stabilized_free_bytes": 18 * sampler.GIB,
            "gpu_reserve_bytes": 8 * sampler.GIB,
            "gpu_allocatable_bytes": 10 * sampler.GIB,
        }
    }


def healthy_memory() -> dict[str, object]:
    return {
        "memory.current": 1024,
        "memory.peak": 2048,
        "memory.max": 10 * sampler.GIB,
        "memory.swap.current": 0,
        "memory.swap.max": 0,
        "events": {key: 0 for key in sampler.REQUIRED_MEMORY_EVENTS},
        "pressure_observed_only": {
            category: {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0, "total": 0}
            for category in ("some", "full")
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


class CandidateIdentityTests(unittest.TestCase):
    CONTAINER_ID = "a" * 64
    REBOUND_ID = "b" * 64
    IMAGE_CONFIG = "sha256:" + "c" * 64
    RUNTIME_COMMIT = "d" * 40
    TOKEN = "task11b-regression-token"
    NAME = "subgen-task11b-runtime-auto"
    DISPOSABLE_ROOT = "/var/lib/subgen-v05-gate"

    @classmethod
    def candidate_item(cls) -> dict[str, object]:
        item: dict[str, object] = {
            "Id": cls.CONTAINER_ID,
            "Name": f"/{cls.NAME}",
            "Image": cls.IMAGE_CONFIG,
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
                "WHISPER_MODEL=auto",
                "TRANSCRIBE_DEVICE=cuda",
                "COMPUTE_TYPE=float16",
                "CONCURRENT_TRANSCRIPTIONS=1",
                "MODEL_PATH=/subgen/models",
                "MODEL_ENVELOPE_CATALOG=/opt/subgen/model-envelopes/catalog.json",
                "MODEL_ENVELOPE_IDENTITY=/opt/subgen/model-envelopes/image-identity.json",
                "CANONICAL_SHARED_CUDA=true",
                "GPU_MEMORY_RESERVE_GIB=8",
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
                "Memory": 10 * sampler.GIB,
                "MemorySwap": 10 * sampler.GIB,
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
                    "Source": f"{cls.DISPOSABLE_ROOT}/media",
                    "Destination": "/media",
                    "Mode": "rw",
                    "RW": True,
                    "Propagation": "rprivate",
                },
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
                    f"{cls.DISPOSABLE_ROOT}/media:/media:rw",
                    f"{cls.DISPOSABLE_ROOT}/models:/subgen/models:rw",
                    f"{cls.DISPOSABLE_ROOT}/monitor:/opt/subgen/monitor:rw",
                    f"{cls.DISPOSABLE_ROOT}/model-envelopes:/opt/subgen/model-envelopes:ro",
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
            "--model-revision",
            "e" * 40,
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
            filesystem_check=False,
        )
        return SimpleNamespace(
            expected_memory_bytes=12 * sampler.GIB,
            candidate_mode="profiler",
            expected_model=model,
            expected_profiler_returncode=expected_returncode,
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
            filesystem_check=False,
        )
        return sampler.BoundaryExpectation(
            document=boundary,
            file_sha256="1" * 64,
            canonical_sha256=sampler.execution_boundary_digest(boundary),
        )

    @classmethod
    def args(cls) -> object:
        return SimpleNamespace(
            expected_memory_bytes=10 * sampler.GIB,
            candidate_mode="runtime",
            expected_model="medium",
            expected_profiler_returncode=None,
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
            image_config=cls.IMAGE_CONFIG,
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
            filesystem_check=False,
        )
        after_boundary = sampler.canonical_execution_boundary(
            after_start,
            disposable_root=self.DISPOSABLE_ROOT,
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
            filesystem_check=False,
        )
        changed_boundary = sampler.canonical_execution_boundary(
            changed,
            disposable_root=self.DISPOSABLE_ROOT,
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
                    filesystem_check=False,
                )

    def test_medium_profiler_accepts_required_thirty_runs(self) -> None:
        item = self.profiler_item("medium")
        command = item["Cmd"]
        assert isinstance(command, list)
        command[command.index("--runs") + 1] = "30"
        config = item["ConfigFull"]
        assert isinstance(config, dict)
        config["Cmd"] = copy.deepcopy(command)

        args = self.profiler_args("medium", expected_returncode=0)
        boundary = sampler.canonical_execution_boundary(
            item,
            disposable_root=self.DISPOSABLE_ROOT,
            filesystem_check=False,
        )
        args.boundary_expectation = sampler.BoundaryExpectation(
            document=boundary,
            file_sha256="1" * 64,
            canonical_sha256=sampler.execution_boundary_digest(boundary),
        )

        sampler._validate_candidate_boundaries(item, args)

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

    def test_cleanup_ignores_disappeared_mount_source_but_evidence_does_not(
        self,
    ) -> None:
        item = self.candidate_item()
        binding = self.binding()
        args = self.args()

        class FakeClient:
            def verify_local_daemon(self) -> tuple[str, str]:
                return ("daemon", "boot")

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
                return ("daemon", "boot")

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
            "entries": [{"policy": {"model": model}}],
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
                "reason": "capacity_envelope",
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
                self.encoded(catalog), expected_model="medium", expected_version=2
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
            expected_profiler_returncode=None,
            gpu_free_floor_bytes=8 * sampler.GIB,
            host_reserve_bytes=4 * sampler.GIB,
            expected_memory_bytes=10 * sampler.GIB,
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
                    side_effect=lambda *_args: copy.deepcopy(healthy_memory()),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    sampler.DockerClient,
                    "verify_local_daemon",
                    return_value=("daemon", "boot"),
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
                mock.Mock(spec=sampler.DockerClient),
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


class RunGateCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = CandidateIdentityTests.binding()
        self.client = mock.Mock(name="docker_client")
        self.client.verify_local_daemon.return_value = ("daemon-digest", "boot-digest")
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
            expected_memory_bytes=10 * sampler.GIB,
            gpu_free_floor_bytes=8 * sampler.GIB,
            host_reserve_bytes=4 * sampler.GIB,
            frigate_container="frigate",
            frigate_stats_url=sampler.EXACT_ENDPOINTS["frigate"],
            ollama_url=sampler.EXACT_ENDPOINTS["ollama"],
            candidate_status_url=sampler.EXACT_ENDPOINTS["candidate"],
            candidate_mode="runtime",
            expected_model="medium",
            expected_profiler_returncode=None,
            expected_container_id=binding.container_id,
            expected_image_config=binding.image_config,
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
            filesystem_check=False,
        )
        binding = sampler.CandidateBinding(
            name=f"subgen-task11b-profile-{model}",
            container_id=CandidateIdentityTests.CONTAINER_ID,
            image_config=CandidateIdentityTests.IMAGE_CONFIG,
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
        self.assertNotIn(
            args.expected_container_id, script.split("--unit=", 1)[1].split()[0]
        )

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
            self.assertNotIn("--leave-running-on-pass", script)
            self.assertIn("--property=RuntimeMaxSec=1320s", script)

        unsafe = self.profiler_args("large-v3", 3)
        unsafe.leave_running_on_pass = True
        with self.assertRaisesRegex(sampler.GateAbort, "cannot_retain"):
            sampler.validate_args(unsafe)

    def test_boundary_manifest_generator_is_create_only_and_secret_safe(self) -> None:
        args = self.args()
        args.emit_boundary_manifest = Path("/root/subgen-gate/boundary.json")
        client = mock.Mock(name="docker")
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
        self.assertEqual(parsed["schema"], 3)
        self.assertIn("environment_sha256", parsed)
        self.assertNotIn("AUTO_DELETE", payload.decode("utf-8"))

    def test_killed_sampler_stop_post_stops_only_exact_revalidated_id(self) -> None:
        args = self.args()
        args.systemd_stop_post = True
        item = CandidateIdentityTests.candidate_item()

        class FakeClient:
            def verify_local_daemon(self) -> tuple[str, str]:
                return ("daemon", "boot")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
