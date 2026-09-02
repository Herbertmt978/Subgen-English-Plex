#!/usr/bin/env python3
"""Fail-closed health primitives for the isolated Frigate v0.5.0 gate.

This file is owner-operated evidence tooling and is excluded from the runtime
image. It observes Frigate, Ollama, NVIDIA, host/cgroup memory, and one stopped
disposable Task 11B container. It binds that container's full ID, image config,
and dedicated ownership labels before starting it. Every non-pass exit stops
and verifies only that immutable ID.

The runtime observer owns gate orchestration and imports these primitives. This
module's command line is limited to self-test, boundary-manifest generation,
and the observer's immutable-ID cleanup callback; it does not run or supervise
the gate itself.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import posixpath
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO


GIB = 1024**3
MIB = 1024**2
DOCKER_SOCKET = "/run/docker.sock"
DOCKER_HOST = f"unix://{DOCKER_SOCKET}"
MAX_COMMAND_BYTES = 256 * 1024
MAX_LOG_BYTES = 512 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_CAMERA_CONFIG_BYTES = 32 * 1024
MAX_BOUNDARY_CONFIG_BYTES = 256 * 1024
MAX_PROFILER_RESULT_BYTES = 256 * 1024
MAX_RUNTIME_RECEIPT_BYTES = 4 * 1024
MAX_RECEIPT_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_CANDIDATE_LOG_BYTES = 16 * MIB
MAX_KERNEL_JOURNAL_BYTES = 16 * MIB
CANDIDATE_LOG_CLOSE_TIMEOUT_SECONDS = 5.0
MAX_HTTP_HEADER_BYTES = 64 * 1024
MAX_HTTP_WIRE_BYTES = MAX_HTTP_HEADER_BYTES + MAX_JSON_BYTES + 256 * 1024
MAX_SAMPLE_LAG_SECONDS = 2.0
LOG_OVERLAP_SECONDS = 1.0
SAFE_LOG_CONFIG = {
    "Type": "json-file",
    "Config": {"mode": "blocking"},
}
SAFE_FRIGATE_LOG_CONFIG = {"Type": "json-file", "Config": {}}
SAFE_FRIGATE_LOG_SHA256 = hashlib.sha256(
    json.dumps(SAFE_FRIGATE_LOG_CONFIG, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
).hexdigest()
SAFE_TMPFS = {"/tmp": "rw,noexec,nosuid,nodev,size=64m"}
SAFE_NVIDIA_DEVICE_REQUESTS = [
    {
        "Driver": "nvidia",
        "Count": -1,
        "DeviceIDs": None,
        "Capabilities": [["gpu"]],
        "Options": {},
    }
]
SAFE_SECURITY_OPTIONS = {
    ("no-new-privileges",),
    ("no-new-privileges:true",),
}
DIRECT_PYTHON_ENTRYPOINT = ["/usr/bin/python3"]
RUNTIME_COMMAND = ["/subgen/launcher.py"]
PROFILER_STDOUT_PATH = "/profile/output/profiler-stdout.json"
PROFILER_RESULT_PATH = "/profile/output/profiler-result.json"
PROFILER_CATALOG_PATH = "/profile/output/catalog.json"
PROFILER_INPUT_CATALOG_PATH = "/profile/input/catalog.json"
PROFILER_IDENTITY_PATH = "/profile/input/image-identity.json"
PROFILER_MEDIA_PATH = "/profile/input/media"
MODEL_DESCENT = ("large-v3", "medium", "small", "base", "tiny")
PHASE_FIXTURE_DESTINATIONS = {
    "a": "/fixtures/phase-a",
    "b": "/fixtures/phase-b",
}
PHASE_OUTPUT_DESTINATIONS = {
    "a": "/task11b-output/phase-a",
    "b": "/task11b-output/phase-b",
}
FAILURE_MARKER_CONTAINER_PATH = "/opt/subgen/monitor/subgen_failure_markers.json"
FAILURE_MARKER_FILENAME = posixpath.basename(FAILURE_MARKER_CONTAINER_PATH)
EXACT_DISPOSABLE_MOUNT_SUFFIXES = {
    "/subgen/models": "models",
    "/opt/subgen/monitor": "monitor",
    "/opt/subgen/model-envelopes": "model-envelopes",
    "/fixtures/phase-a": "fixtures/phase-a",
    "/fixtures/phase-b": "fixtures/phase-b",
    "/task11b-output/phase-a": "task11b-output/phase-a",
    "/task11b-output/phase-b": "task11b-output/phase-b",
    "/run/subgen-task11b": "receipts",
    "/profile/input": "profile-input",
    "/profile/output": "profile-output",
}

# This process remains PID 1 after the isolated profiler child exits. The
# command is frozen by both the full Config hash and the independently supplied
# command SHA. It publishes a durable child-exit receipt, then holds the
# container alive while the sampler validates stdout/catalog evidence and
# completes the 900-second higher-priority Frigate observation.
PROFILER_HOLD_PROGRAM = r"""import hashlib
import json
import os
import stat
import subprocess
import sys
import time


def write_all(fd, payload):
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise RuntimeError("write_stalled")
        written += count


def publish(dirfd, partial_name, final_name, payload):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(partial_name, flags, 0o600, dir_fd=dirfd)
    try:
        write_all(fd, payload)
        os.fsync(fd)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError("write_metadata")
    finally:
        os.close(fd)
    os.link(partial_name, final_name, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
    os.unlink(partial_name, dir_fd=dirfd)
    os.fsync(dirfd)


if len(sys.argv) < 6 or sys.argv[3] != "--":
    raise SystemExit(64)
stdout_path = sys.argv[1]
result_path = sys.argv[2]
command = sys.argv[4:]
if not command:
    raise SystemExit(64)
parent = os.path.dirname(stdout_path)
if os.path.dirname(result_path) != parent:
    raise SystemExit(64)
dirfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
try:
    directory = os.fstat(dirfd)
    if directory.st_uid != os.geteuid() or stat.S_IMODE(directory.st_mode) != 0o700:
        raise RuntimeError("output_directory")
    stdout_name = os.path.basename(stdout_path)
    result_name = os.path.basename(result_path)
    partial_name = "." + stdout_name + ".partial"
    for name in (stdout_name, result_name, partial_name):
        try:
            os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise RuntimeError("output_exists")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    stdout_fd = os.open(partial_name, flags, 0o600, dir_fd=dirfd)
    try:
        completed = subprocess.run(
            [sys.executable, *command],
            stdin=subprocess.DEVNULL,
            stdout=stdout_fd,
            stderr=None,
            close_fds=True,
            check=False,
        )
        os.fsync(stdout_fd)
        stdout_size = os.fstat(stdout_fd).st_size
        if stdout_size > 262144:
            raise RuntimeError("stdout_limit")
        os.lseek(stdout_fd, 0, os.SEEK_SET)
        stdout_payload = os.read(stdout_fd, stdout_size + 1)
        if len(stdout_payload) != stdout_size:
            raise RuntimeError("stdout_read")
    finally:
        os.close(stdout_fd)
    os.link(partial_name, stdout_name, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
    os.unlink(partial_name, dir_fd=dirfd)
    os.fsync(dirfd)
    receipt = json.dumps(
        {
            "schema": 1,
            "returncode": completed.returncode,
            "stdout_bytes": stdout_size,
            "stdout_sha256": hashlib.sha256(stdout_payload).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n"
    publish(dirfd, "." + result_name + ".partial", result_name, receipt)
finally:
    os.close(dirfd)
print("TASK11B_PROFILER_CHILD_COMPLETE", flush=True)
while True:
    time.sleep(30)
"""

GATE_LABEL = "io.github.herbertmt978.subgen.task11b-gate"
TOKEN_LABEL = "io.github.herbertmt978.subgen.gate-token"
ROLE_LABEL = "io.github.herbertmt978.subgen.gate-role"
RUNTIME_LABEL = "io.github.herbertmt978.subgen.runtime-commit"
OWNERSHIP_LABEL_KEYS = {GATE_LABEL, TOKEN_LABEL, ROLE_LABEL, RUNTIME_LABEL}

REQUIRED_DETECTORS = ("onnx_0", "onnx_1")
REQUIRED_EMBEDDING_SPEEDS = (
    "face_recognition_speed",
    "plate_recognition_speed",
)
CONDITIONAL_EMBEDDING_SPEEDS = (
    ("yolov9_plate_detection_speed", "yolov9_plate_detection"),
)
REQUIRED_MEMORY_EVENTS = {
    "low",
    "high",
    "max",
    "oom",
    "oom_kill",
    "oom_group_kill",
}
RESOURCE_STATUS_KEYS = {
    "controller_state",
    "recovery_reason",
    "admission_open",
    "capacity_source",
    "requested_model",
    "envelope_key",
    "envelope_disposition",
    "envelope_reason",
    "selected_model",
    "model_explicit",
    "automatic_ceiling",
    "decision_reason",
    "decision_provenance",
    "gpu_total_bytes",
    "gpu_stabilized_free_bytes",
    "gpu_reserve_bytes",
    "gpu_allocatable_bytes",
    "priority_pressure",
    "workload",
    "runtime_identity",
    "failure_counters",
}
PRIORITY_STATUS_KEYS = {
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
WORKLOAD_STATUS_KEYS = {"active", "chunk_uncommitted", "completion_generation"}
RUNTIME_IDENTITY_KEYS = {"epoch", "started_monotonic_ns"}
FAILURE_COUNTER_KEYS = {"cuda_oom_generation", "media_failure_generation"}
RUNTIME_RECEIPT_KEYS = {
    "schema",
    "runtime_epoch",
    "gate_token_sha256",
    "sequence",
    "observed_monotonic_ns",
    "workload_sha256",
    "source_generation",
    "observation_digest",
    "transition_observation_digest",
    "transition_sequence",
    "heartbeat_age_ms",
    "source_age_ms",
    "policy_sha256",
    "priority_state",
    "controller_phase",
    "recovery_reason",
    "admission_open",
    "distinct_clear_count",
    "model_resident",
    "model_load_generation",
    "model_unload_generation",
    "active",
    "chunk_uncommitted",
    "active_cursor_ms",
    "completed_cursor_ms",
    "completion_generation",
    "model_identity_sha256",
    "cuda_oom_generation",
    "media_failure_generation",
}
CANDIDATE_IDENTITY_KEYS = {
    "container_id",
    "runtime_commit",
    "oci_index",
    "config_digest",
    "layer_diff_ids",
    "selected_model",
    "model_revision",
}
DOCKER_DAEMON_IDENTITY_KEYS = {
    "schema",
    "engine_id_sha256",
    "host_boot_id_sha256",
    "docker_host",
    "os_type",
}
CANDIDATE_IDENTITY_DOCUMENT_KEYS = {
    "schema",
    "candidate_identity",
    "docker_daemon_identity_sha256",
    "execution_boundary_manifest_sha256",
    "gate_token_sha256",
    "intended_command_sha256",
    "created_stopped",
}
WORKLOAD_IDENTITY_KEYS = {
    "fixture_sha256",
    "task",
    "language",
    "cursor_start_ms",
    "total_duration_ms",
}
PHASE_A_EVENT_KEYS = {
    "event_index",
    "kind",
    "monotonic_ns",
    "source_generation",
    "observation_digest",
    "runtime_epoch",
    "runtime_started_monotonic_ns",
    "gate_receipt_sha256",
    "transition_observation_digest",
    "transition_sequence",
    "heartbeat_age_ms",
    "source_age_ms",
    "policy_sha256",
    "priority_state",
    "controller_phase",
    "recovery_reason",
    "admission_open",
    "distinct_clear_count",
    "model_resident",
    "model_load_generation",
    "model_unload_generation",
    "cursor_ms",
    "last_completed_cursor_ms",
    "completion_generation",
    "workload_active",
    "chunk_uncommitted",
    "output_count",
    "marker_count",
    "output_create_count",
    "marker_create_count",
    "threshold_masking_allowed",
    "candidate_bytes",
    "model_identity_sha256",
    "cuda_oom_generation",
    "media_failure_generation",
}
PHASE_A_KEYS = {
    "schema",
    "outcome",
    "policy_sha256",
    "unloaded_gpu_envelope_sha256",
    "workload_sha256",
    "workload_identity",
    "candidate_identity_sha256",
    "execution_boundary_manifest_sha256",
    "gate_receipt_trace_sha256",
    "runtime_epoch",
    "runtime_started_monotonic_ns",
    "assertion_reason_codes",
    "assertion_observation_digest",
    "assertion_observation_sha256",
    "assertion_observed_monotonic_ns",
    "t0_monotonic_ns",
    "sealed_monotonic_ns",
    "allowed_unloaded_bytes",
    "events",
    "final_output_sha256",
    "protected_first_sample_monotonic_ns",
    "protected_last_sample_monotonic_ns",
    "protected_sample_count",
    "protected_blind_interval_count",
    "protected_threshold_failure_count",
    "candidate_restart_delta",
    "candidate_oom_killed",
    "cgroup_oom_delta",
    "cgroup_oom_kill_delta",
    "cgroup_oom_group_kill_delta",
    "runtime_cuda_oom_generation_delta",
    "runtime_media_failure_generation_delta",
    "candidate_cuda_oom_log_match_delta",
    "nvidia_xid_log_match_delta",
}
PHASE_B_SAMPLE_KEYS = {
    "sample_index",
    "scheduled_offset_seconds",
    "captured_monotonic_ns",
    "source_generation",
    "policy_sha256",
    "producer_epoch",
    "runtime_epoch",
    "runtime_started_monotonic_ns",
    "candidate_identity_sha256",
    "gate_receipt_sha256",
    "model_identity_sha256",
    "observation_digest",
    "transition_observation_digest",
    "transition_sequence",
    "heartbeat_age_ms",
    "source_age_ms",
    "priority_state",
    "controller_phase",
    "recovery_reason",
    "admission_open",
    "candidate_running",
    "workload_active",
    "distinct_clear_count",
    "model_resident",
    "model_load_generation",
    "model_unload_generation",
    "completion_generation",
    "cuda_oom_generation",
    "media_failure_generation",
    "detection_fps",
    "camera_min_process_ratio",
    "camera_max_skipped_fps",
    "camera_low_ratio_elapsed_ms",
    "detector_count",
    "detector_stalled_count",
    "embedding_metric_count",
    "embedding_invalid_count",
    "candidate_oom_killed",
    "cgroup_oom_delta",
    "cgroup_oom_kill_delta",
    "cgroup_oom_group_kill_delta",
    "runtime_cuda_oom_generation_delta",
    "runtime_media_failure_generation_delta",
    "candidate_cuda_oom_log_match_delta",
    "nvidia_xid_log_match_delta",
    "candidate_restart_delta",
    "frigate_restart_delta",
    "ollama_loaded",
}
PHASE_B_KEYS = {
    "schema",
    "outcome",
    "started_monotonic_ns",
    "ended_monotonic_ns",
    "phase_a_seal_sha256",
    "phase_a_durable_monotonic_ns",
    "reset_completed_monotonic_ns",
    "runtime_epoch",
    "runtime_started_monotonic_ns",
    "sample_interval_seconds",
    "policy_sha256",
    "producer_epoch_digest",
    "producer_epoch",
    "candidate_identity_sha256",
    "candidate_identity",
    "execution_boundary_manifest_sha256",
    "workload_sha256",
    "workload_identity",
    "gate_receipt_trace_sha256",
    "model_identity_sha256",
    "samples",
}
FINAL_GATE_KEYS = {
    "schema",
    "outcome",
    "runtime_commit",
    "candidate_oci_index",
    "candidate_config_digest",
    "container_id_sha256",
    "candidate_identity_record_sha256",
    "docker_daemon_identity_sha256",
    "layer_diff_ids_sha256",
    "sampler_sha256",
    "sampler_test_sha256",
    "observer_sha256",
    "observer_test_sha256",
    "producer_sha256",
    "policy_sha256",
    "model_envelope_catalog_sha256",
    "unloaded_gpu_envelope_sha256",
    "execution_boundary_manifest_sha256",
    "phase_a_seal_sha256",
    "phase_b_seal_sha256",
    "cleanup",
}
CONTAINER_NAME_RE = re.compile(r"^subgen-task11b-[a-z0-9][a-z0-9_.-]*$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
CONFIG_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
LOWER_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EPOCH_RE = re.compile(r"^[0-9a-f]{32}$")
MODEL_REVISION_RE = re.compile(r"^hf:[0-9a-f]{40}$")
GPU_UUID_RE = re.compile(
    r"^GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
TOKEN_RE = EPOCH_RE
ROLE_RE = re.compile(r"^(?:runtime-auto|profile-(?:large-v3|medium|small|base|tiny))$")
BOOT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

KERNEL_FAILURE_RE = re.compile(
    r"NVRM:\s*Xid|oom-kill|Out of memory|Killed process", re.IGNORECASE
)
CANDIDATE_FAILURE_RE = re.compile(
    r"CUDA out of memory|CUDA error:\s*out of memory|MemoryError|"
    r"Killed process|SIGSEGV|segmentation fault|fatal Python error",
    re.IGNORECASE,
)
FRIGATE_FAILURE_RE = re.compile(
    r"(?:detector|embedding|face_recognition|plate_recognition|yolov9)"
    r"(?:.|\n){0,240}(?:error|failed|exception|stalled|timeout|traceback)|"
    r"(?:error|failed|exception|stalled|timeout|traceback)"
    r"(?:.|\n){0,240}(?:detector|embedding|face_recognition|"
    r"plate_recognition|yolov9)",
    re.IGNORECASE,
)

EXACT_ENDPOINTS = {
    "frigate": "http://127.0.0.1:5000/api/stats",
    "ollama": "http://127.0.0.1:11434/api/ps",
    "candidate": "http://127.0.0.1:19000/status",
}
PROC_ROOT = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
CUDA_OOM_LOG_RE = re.compile(
    r"CUDA out of memory|CUDA error:\s*out of memory|torch\.cuda\.OutOfMemoryError",
    re.IGNORECASE,
)
NVIDIA_XID_RE = re.compile(r"NVRM:\s*Xid", re.IGNORECASE)

INSPECT_TEMPLATE = """{
  "Id": {{json .Id}},
  "Name": {{json .Name}},
  "Image": {{json .Image}},
  "RestartCount": {{json .RestartCount}},
  "State": {
    "Status": {{json .State.Status}},
    "Running": {{json .State.Running}},
    "Pid": {{json .State.Pid}},
    "OOMKilled": {{json .State.OOMKilled}},
    "ExitCode": {{json .State.ExitCode}},
    "HealthStatus": {{with (index .State "Health")}}{{json (index . "Status")}}{{else}}null{{end}}
  },
  "Labels": {{json .Config.Labels}},
  "Entrypoint": {{json .Config.Entrypoint}},
  "Cmd": {{json .Config.Cmd}},
  "Env": {{json .Config.Env}},
  "User": {{json .Config.User}},
  "WorkingDir": {{json .Config.WorkingDir}},
  "ConfigFull": {{json .Config}},
  "HostConfigFull": {{json .HostConfig}},
  "NetworkSettings": {
    "Networks": {{json .NetworkSettings.Networks}}
  },
  "HostConfig": {
    "RestartPolicy": {{json .HostConfig.RestartPolicy}},
    "NetworkMode": {{json .HostConfig.NetworkMode}},
    "Memory": {{json .HostConfig.Memory}},
    "MemorySwap": {{json .HostConfig.MemorySwap}},
    "PortBindings": {{json .HostConfig.PortBindings}},
    "Privileged": {{json .HostConfig.Privileged}},
    "PidMode": {{json .HostConfig.PidMode}},
    "IpcMode": {{json .HostConfig.IpcMode}},
    "CgroupnsMode": {{json .HostConfig.CgroupnsMode}},
    "UTSMode": {{json .HostConfig.UTSMode}},
    "UsernsMode": {{json .HostConfig.UsernsMode}},
    "ReadonlyRootfs": {{json .HostConfig.ReadonlyRootfs}},
    "CapAdd": {{json .HostConfig.CapAdd}},
    "CapDrop": {{json .HostConfig.CapDrop}},
    "Devices": {{json .HostConfig.Devices}},
    "DeviceRequests": {{json .HostConfig.DeviceRequests}},
    "SecurityOpt": {{json .HostConfig.SecurityOpt}},
    "GroupAdd": {{json .HostConfig.GroupAdd}},
    "PidsLimit": {{json .HostConfig.PidsLimit}},
    "NanoCpus": {{json .HostConfig.NanoCpus}},
    "CpuPeriod": {{json .HostConfig.CpuPeriod}},
    "CpuQuota": {{json .HostConfig.CpuQuota}},
    "Tmpfs": {{json (index .HostConfig "Tmpfs")}}
  },
  "Mounts": {{json .Mounts}}
}""".replace("\n", "")


class GateAbort(RuntimeError):
    """A fail-closed condition represented by a privacy-safe reason code."""

    def __init__(self, message: str) -> None:
        code = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")[:96]
        self.code = code or "gate_abort"
        super().__init__(self.code)


class TelemetryUnavailable(GateAbort):
    """A bounded endpoint was temporarily unreachable."""


class CandidateNotReady(GateAbort):
    """The runtime status endpoint is healthy but selection is not ready."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str


@dataclass(frozen=True)
class CandidateBinding:
    name: str
    container_id: str
    image_config: str
    runtime_commit: str
    gate_role: str
    gate_token_digest: str
    command_digest: str
    boundary_digest: str


@dataclass(frozen=True)
class ObservedBinding:
    name: str
    container_id: str
    image_config: str
    log_config_digest: str = SAFE_FRIGATE_LOG_SHA256


@dataclass(frozen=True)
class BoundaryExpectation:
    document: dict[str, Any]
    file_sha256: str
    canonical_sha256: str


@dataclass(frozen=True)
class ObservationOutcome:
    sample_count: int
    observed_seconds: float
    final_log_wall: float
    candidate_restart_count: int
    frigate_restart_count: int


@dataclass(frozen=True)
class CanonicalArtifact:
    path: Path
    file_sha256: str
    size: int
    document: dict[str, Any]


@dataclass(frozen=True)
class ReceiptBinding:
    receipt: dict[str, Any]
    receipt_sha256: str
    next_observed_monotonic_ns: int | None


@dataclass(frozen=True)
class CandidateLogSnapshot:
    byte_cursor: int
    cuda_oom_matches: int
    source_container_id_sha256: str
    continuous: bool


@dataclass(frozen=True)
class KernelJournalSnapshot:
    cursor_sha256: str
    xid_matches: int
    continuous: bool


@dataclass(frozen=True)
class CgroupSnapshot:
    container_pid: int
    cgroup_path_sha256: str
    oom: int
    oom_kill: int
    oom_group_kill: int


@dataclass(frozen=True)
class GpuAttribution:
    candidate_bytes: int
    validated_monotonic_ns: int
    pid_set_sha256: str
    gpu_uuid_sha256: str


@dataclass(frozen=True)
class FixtureBinding:
    record_sha256: str
    workload_identity: dict[str, Any]
    workload_sha256: str
    host_media: Path
    container_media: str
    host_output: Path
    container_output: str
    host_marker: Path
    container_marker: str
    duration_ms: int
    file_identity: dict[str, Any]
    boundary_mount: dict[str, Any]
    output_boundary_mount: dict[str, Any]


@dataclass
class RuntimeReceiptJournal:
    """Fail-closed reader for the gate runtime's held append-only journal."""

    path: Path
    fd: int
    device: int
    inode: int
    owner: int
    expected_runtime_epoch: str
    expected_token_sha256: str
    offset: int = 0
    last_sequence: int = 0
    last_monotonic_ns: int = -1
    consumed_sha256: str = hashlib.sha256(b"").hexdigest()
    receipt_sha256s: set[str] | None = None

    def __post_init__(self) -> None:
        if self.receipt_sha256s is None:
            self.receipt_sha256s = set()

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        expected_runtime_epoch: str,
        expected_token_sha256: str,
    ) -> RuntimeReceiptJournal:
        path = Path(path)
        if not path.is_absolute():
            raise GateAbort("receipt journal path was not absolute")
        if EPOCH_RE.fullmatch(expected_runtime_epoch) is None:
            raise GateAbort("receipt journal runtime epoch was invalid")
        if LOWER_SHA256_RE.fullmatch(expected_token_sha256) is None:
            raise GateAbort("receipt journal token digest was invalid")
        if os.name != "posix":
            raise GateAbort("receipt journal inode proof requires POSIX")

        try:
            parent = path.parent.resolve(strict=True)
            parent_lstat = path.parent.lstat()
        except OSError as exc:
            raise GateAbort("receipt journal parent was unavailable") from exc
        if parent != path.parent.absolute() or stat.S_ISLNK(parent_lstat.st_mode):
            raise GateAbort("receipt journal parent used a symlink")
        owner = os.geteuid()
        if (
            not stat.S_ISDIR(parent_lstat.st_mode)
            or parent_lstat.st_uid != owner
            or stat.S_IMODE(parent_lstat.st_mode) != 0o700
        ):
            raise GateAbort("receipt journal parent was not owner only")

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise GateAbort("receipt journal could not be opened safely") from exc
        try:
            metadata = os.fstat(fd)
            path_metadata = path.lstat()
            cls._validate_metadata(metadata, owner=owner)
            if stat.S_ISLNK(path_metadata.st_mode) or (
                metadata.st_dev,
                metadata.st_ino,
            ) != (path_metadata.st_dev, path_metadata.st_ino):
                raise GateAbort("receipt journal was replaced while opening")
            if metadata.st_size > MAX_RECEIPT_JOURNAL_BYTES:
                raise GateAbort("receipt journal exceeded its byte limit")
            return cls(
                path=path,
                fd=fd,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                owner=owner,
                expected_runtime_epoch=expected_runtime_epoch,
                expected_token_sha256=expected_token_sha256,
            )
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _validate_metadata(metadata: os.stat_result, *, owner: int) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != owner
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise GateAbort("receipt journal was not an owner-only regular file")

    def _read_exact_at(self, offset: int, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            try:
                if hasattr(os, "pread"):
                    chunk = os.pread(self.fd, size - len(result), offset + len(result))
                else:  # pragma: no cover - the journal is POSIX-only
                    os.lseek(self.fd, offset + len(result), os.SEEK_SET)
                    chunk = os.read(self.fd, size - len(result))
            except OSError as exc:
                raise GateAbort("receipt journal read failed") from exc
            if not chunk:
                raise GateAbort("receipt journal size changed during read")
            result.extend(chunk)
        return bytes(result)

    def _revalidate_path(self) -> os.stat_result:
        try:
            held = os.fstat(self.fd)
            current = self.path.lstat()
        except OSError as exc:
            raise GateAbort("receipt journal was replaced or removed") from exc
        if (
            stat.S_ISLNK(current.st_mode)
            or (
                held.st_dev,
                held.st_ino,
            )
            != (self.device, self.inode)
            or (
                current.st_dev,
                current.st_ino,
            )
            != (self.device, self.inode)
        ):
            raise GateAbort("receipt journal was replaced")
        self._validate_metadata(held, owner=self.owner)
        if held.st_size < self.offset:
            raise GateAbort("receipt journal was truncated")
        if held.st_size > MAX_RECEIPT_JOURNAL_BYTES:
            raise GateAbort("receipt journal exceeded its byte limit")
        if self.offset:
            prefix = self._read_exact_at(0, self.offset)
            if sha256_bytes(prefix) != self.consumed_sha256:
                raise GateAbort(
                    "receipt journal previously consumed bytes were mutated"
                )
        return held

    def read_available(self) -> list[dict[str, Any]]:
        before = self._revalidate_path()
        size = before.st_size
        if size == self.offset:
            self._revalidate_path()
            return []
        payload = self._read_exact_at(self.offset, size - self.offset)
        if not payload.endswith(b"\n"):
            raise GateAbort("receipt journal ended with a partial record")

        receipts: list[dict[str, Any]] = []
        next_sequence = self.last_sequence
        next_monotonic_ns = self.last_monotonic_ns
        new_digests: list[str] = []
        for line in payload.splitlines(keepends=True):
            receipt = validate_runtime_receipt(line)
            sequence = receipt["sequence"]
            if sequence != next_sequence + 1:
                if sequence <= next_sequence:
                    raise GateAbort("receipt journal contained a duplicate or mutation")
                raise GateAbort("receipt journal contained a sequence gap")
            monotonic_ns = receipt["observed_monotonic_ns"]
            if monotonic_ns <= next_monotonic_ns:
                raise GateAbort("receipt journal monotonic time did not increase")
            if receipt["runtime_epoch"] != self.expected_runtime_epoch:
                raise GateAbort("receipt journal runtime epoch changed")
            if receipt["gate_token_sha256"] != self.expected_token_sha256:
                raise GateAbort("receipt journal token digest changed")
            digest = sha256_bytes(line)
            assert self.receipt_sha256s is not None
            if digest in self.receipt_sha256s or digest in new_digests:
                raise GateAbort("receipt journal contained a duplicate record digest")
            receipts.append(receipt)
            new_digests.append(digest)
            next_sequence = sequence
            next_monotonic_ns = monotonic_ns

        after = self._revalidate_path()
        if after.st_size < size:
            raise GateAbort("receipt journal was truncated during read")
        consumed = self._read_exact_at(0, size)
        if consumed[self.offset :] != payload:
            raise GateAbort("receipt journal bytes were mutated during read")
        self.offset = size
        self.last_sequence = next_sequence
        self.last_monotonic_ns = next_monotonic_ns
        self.consumed_sha256 = sha256_bytes(consumed)
        assert self.receipt_sha256s is not None
        self.receipt_sha256s.update(new_digests)
        return receipts

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class ContinuousCandidateLog:
    """One exact-ID Docker log attachment kept across both gate phases."""

    def __init__(
        self,
        client: DockerClient,
        binding: CandidateBinding,
        process: subprocess.Popen[bytes] | Any,
        *,
        max_bytes: int = MAX_CANDIDATE_LOG_BYTES,
    ) -> None:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 0 < max_bytes <= MAX_CANDIDATE_LOG_BYTES
        ):
            raise GateAbort("candidate log byte limit was invalid")
        self.client = client
        self.binding = binding
        self.process = process
        self.max_bytes = max_bytes
        self._payload = bytearray()
        self._stderr = bytearray()
        self._stdout_eof = False
        self._stderr_eof = False
        self._closed = False
        self._cuda_oom_matches = 0
        self._source_sha256 = sha256_bytes(binding.container_id.encode("ascii"))

    @classmethod
    def open(
        cls,
        client: DockerClient,
        binding: CandidateBinding,
        max_bytes: int = MAX_CANDIDATE_LOG_BYTES,
    ) -> ContinuousCandidateLog:
        cls._assert_bound_source(client, binding, require_running=True)
        try:
            process = subprocess.Popen(
                client._argv(
                    "logs",
                    "--follow",
                    "--timestamps",
                    binding.container_id,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise GateAbort("candidate log stream could not be attached") from exc
        if process.stdout is None or process.stderr is None:
            _kill_process(process)
            raise GateAbort("candidate log stream pipes were unavailable")
        try:
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stderr.fileno(), False)
        except (AttributeError, OSError) as exc:
            _kill_process(process)
            raise GateAbort(
                "candidate log stream could not be made nonblocking"
            ) from exc
        return cls(client, binding, process, max_bytes=max_bytes)

    @staticmethod
    def _assert_bound_source(
        client: DockerClient,
        binding: CandidateBinding,
        *,
        require_running: bool,
    ) -> dict[str, Any]:
        item = client.inspect(binding.container_id, missing_ok=not require_running)
        if item is None:
            if require_running:
                raise GateAbort("candidate log source disappeared")
            return {}
        labels = _container_labels(item)
        if (
            item.get("Id") != binding.container_id
            or item.get("Image") != binding.image_config
            or _command_digest(item) != binding.command_digest
            or labels.get(GATE_LABEL) != "true"
            or labels.get(ROLE_LABEL) != binding.gate_role
            or labels.get(RUNTIME_LABEL) != binding.runtime_commit
            or sha256_bytes(str(labels.get(TOKEN_LABEL, "")).encode("utf-8"))
            != binding.gate_token_digest
        ):
            raise GateAbort("candidate log source identity changed")
        running = _bounded_state(item)["running"]
        if require_running and running is not True:
            raise GateAbort("candidate log source stopped unexpectedly")
        if not require_running and running is not False:
            raise GateAbort("candidate log source was not stopped")
        return item

    def _consume_stdout(self, payload: bytes) -> None:
        if not isinstance(payload, bytes):
            raise GateAbort("candidate log payload was invalid")
        if len(self._payload) + len(payload) > self.max_bytes:
            raise GateAbort("candidate log exceeded its 16 MiB byte limit")
        self._payload.extend(payload)
        text = self._payload.decode("utf-8", errors="replace")
        self._cuda_oom_matches = sum(1 for _match in CUDA_OOM_LOG_RE.finditer(text))

    @staticmethod
    def _read_nonblocking(pipe: Any) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        eof = False
        while True:
            try:
                chunk = os.read(pipe.fileno(), 65536)
            except BlockingIOError:
                break
            except OSError as exc:
                raise GateAbort("candidate log stream read failed") from exc
            if not chunk:
                eof = True
                break
            chunks.append(chunk)
        return b"".join(chunks), eof

    def _drain(self) -> None:
        if self._closed:
            raise GateAbort("candidate log stream was already closed")
        stdout = getattr(self.process, "stdout", None)
        stderr = getattr(self.process, "stderr", None)
        if stdout is None or stderr is None:
            raise GateAbort("candidate log stream pipes disappeared")
        stdout_payload, stdout_eof = self._read_nonblocking(stdout)
        stderr_payload, stderr_eof = self._read_nonblocking(stderr)
        self._consume_stdout(stdout_payload)
        if len(self._stderr) + len(stderr_payload) > MAX_HTTP_HEADER_BYTES:
            raise GateAbort("candidate log control stream overflowed")
        self._stderr.extend(stderr_payload)
        self._stdout_eof = self._stdout_eof or stdout_eof
        self._stderr_eof = self._stderr_eof or stderr_eof

    def assert_healthy(self) -> None:
        self._drain()
        if (
            self._stderr
            or self._stdout_eof
            or self._stderr_eof
            or self.process.poll() is not None
        ):
            raise GateAbort("candidate log stream lost continuity")
        self._assert_bound_source(self.client, self.binding, require_running=True)

    def _snapshot_without_io(self) -> CandidateLogSnapshot:
        return CandidateLogSnapshot(
            byte_cursor=len(self._payload),
            cuda_oom_matches=self._cuda_oom_matches,
            source_container_id_sha256=self._source_sha256,
            continuous=True,
        )

    def snapshot(self) -> CandidateLogSnapshot:
        self.assert_healthy()
        return self._snapshot_without_io()

    def close_after_stop(
        self, *, timeout_seconds: float = CANDIDATE_LOG_CLOSE_TIMEOUT_SECONDS
    ) -> CandidateLogSnapshot:
        timeout = finite_number(
            timeout_seconds, "candidate log close timeout", positive=True
        )
        if timeout > CANDIDATE_LOG_CLOSE_TIMEOUT_SECONDS:
            raise GateAbort("candidate log close timeout exceeded its bound")
        self._assert_bound_source(self.client, self.binding, require_running=False)
        deadline = time.monotonic() + timeout
        try:
            while True:
                self._drain()
                returncode = self.process.poll()
                if returncode is not None and self._stdout_eof and self._stderr_eof:
                    if self._stderr or returncode != 0:
                        raise GateAbort("candidate log stream reported an error")
                    return self._snapshot_without_io()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GateAbort(
                        "candidate log stream did not reach EOF after candidate stop"
                    )
                time.sleep(min(0.01, remaining))
        finally:
            if self.process.poll() is None:
                _kill_process(self.process)
            for pipe_name in ("stdout", "stderr"):
                pipe = getattr(self.process, pipe_name, None)
                if pipe is not None:
                    pipe.close()
            self._closed = True


class KernelJournalCursor:
    """Monotonic kernel-journal reader that never resets between phases."""

    def __init__(self, cursor: str, *, max_bytes: int = MAX_KERNEL_JOURNAL_BYTES):
        self._cursor = self._validate_cursor(cursor)
        self._max_bytes = max_bytes
        self._bytes_seen = 0
        self._xid_matches = 0
        self._seen_record_cursors: set[str] = set()

    @staticmethod
    def _validate_cursor(cursor: Any) -> str:
        if (
            not isinstance(cursor, str)
            or not 1 <= len(cursor) <= 2048
            or any(
                ord(character) < 0x21 or ord(character) > 0x7E for character in cursor
            )
        ):
            raise GateAbort("kernel journal cursor was invalid")
        return cursor

    @classmethod
    def _parse_output(cls, output: str) -> tuple[list[dict[str, Any]], str]:
        if not isinstance(output, str):
            raise GateAbort("kernel journal output was invalid")
        lines = output.splitlines()
        cursor_lines = [
            (index, line[len("-- cursor: ") :])
            for index, line in enumerate(lines)
            if line.startswith("-- cursor: ")
        ]
        if len(cursor_lines) != 1 or cursor_lines[0][0] != len(lines) - 1:
            raise GateAbort("kernel journal output lacked one final cursor")
        cursor = cls._validate_cursor(cursor_lines[0][1])
        records: list[dict[str, Any]] = []
        for line in lines[:-1]:
            if not line:
                raise GateAbort("kernel journal output contained a blank record")
            record = strict_json_object(
                (line + "\n").encode("utf-8"),
                label="kernel journal record",
                max_bytes=MAX_JSON_BYTES,
            )
            record_cursor = cls._validate_cursor(record.get("__CURSOR"))
            message = record.get("MESSAGE")
            if not isinstance(message, str):
                raise GateAbort("kernel journal record message was invalid")
            record["__CURSOR"] = record_cursor
            records.append(record)
        if records and records[-1]["__CURSOR"] != cursor:
            raise GateAbort("kernel journal final cursor did not match its last record")
        return records, cursor

    @classmethod
    def open_at_tail(cls) -> KernelJournalCursor:
        result = bounded_command(
            [
                "journalctl",
                "--dmesg",
                "--boot=0",
                "--output=json",
                "--show-cursor",
                "--lines=0",
                "--no-pager",
            ],
            label="kernel journal tail cursor",
            max_bytes=MAX_HTTP_HEADER_BYTES,
        )
        records, cursor = cls._parse_output(result.output)
        if records:
            raise GateAbort("kernel journal tail cursor unexpectedly returned records")
        return cls(cursor)

    def snapshot(self) -> KernelJournalSnapshot:
        result = bounded_command(
            [
                "journalctl",
                "--dmesg",
                "--boot=0",
                "--output=json",
                "--show-cursor",
                f"--after-cursor={self._cursor}",
                "--no-pager",
            ],
            label="kernel journal continuation",
            max_bytes=min(MAX_COMMAND_BYTES, self._max_bytes + 1),
        )
        payload_size = len(result.output.encode("utf-8"))
        if self._bytes_seen + payload_size > self._max_bytes:
            raise GateAbort("kernel journal evidence overflowed")
        records, next_cursor = self._parse_output(result.output)
        if not records and next_cursor != self._cursor:
            raise GateAbort("kernel journal cursor advanced without a record")
        new_cursors = [record["__CURSOR"] for record in records]
        if len(new_cursors) != len(set(new_cursors)) or any(
            cursor in self._seen_record_cursors for cursor in new_cursors
        ):
            raise GateAbort("kernel journal replayed a record cursor")
        new_matches = sum(
            len(NVIDIA_XID_RE.findall(str(record["MESSAGE"]))) for record in records
        )
        self._seen_record_cursors.update(new_cursors)
        self._bytes_seen += payload_size
        self._xid_matches += new_matches
        self._cursor = next_cursor
        return KernelJournalSnapshot(
            cursor_sha256=sha256_bytes(self._cursor.encode("ascii")),
            xid_matches=self._xid_matches,
            continuous=True,
        )


class CandidateCgroupProbe:
    """Rebind exact candidate PID/cgroup state for each cgroup/GPU observation."""

    def __init__(
        self, client: DockerClient, binding: CandidateBinding, args: argparse.Namespace
    ) -> None:
        self.client = client
        self.binding = binding
        self.args = args

    @staticmethod
    def _read_bounded(path: Path, *, maximum: int, label: str) -> str:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise GateAbort(f"{label} was unavailable") from exc
        try:
            payload = bytearray()
            while len(payload) <= maximum:
                chunk = os.read(fd, min(65536, maximum + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
        finally:
            os.close(fd)
        if len(payload) > maximum:
            raise GateAbort(f"{label} exceeded its byte limit")
        try:
            return bytes(payload).decode("ascii")
        except UnicodeDecodeError as exc:
            raise GateAbort(f"{label} was not ASCII") from exc

    def _candidate_pid_and_cgroup(self) -> tuple[int, Path]:
        item = self.client.inspect(self.binding.container_id)
        if item is None:
            raise GateAbort("candidate cgroup source disappeared")
        verify_candidate_item(
            item,
            self.binding,
            self.args,
            require_name=False,
            filesystem_check=False,
        )
        state = item.get("State")
        if not isinstance(state, dict) or state.get("Running") is not True:
            raise GateAbort("candidate cgroup source was not running")
        pid = state.get("Pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise GateAbort("candidate container PID was invalid")
        raw = self._read_bounded(
            PROC_ROOT / str(pid) / "cgroup",
            maximum=64 * 1024,
            label="candidate cgroup membership",
        )
        lines = raw.splitlines()
        if len(lines) != 1 or not lines[0].startswith("0::/"):
            raise GateAbort("candidate was not in one cgroup-v2 hierarchy")
        relative_text = lines[0][3:]
        if posixpath.normpath(relative_text) != relative_text or relative_text in {
            "",
            "/",
        }:
            raise GateAbort("candidate cgroup path was invalid")
        relative = relative_text.lstrip("/")
        try:
            root = CGROUP_ROOT.resolve(strict=True)
            cgroup = (CGROUP_ROOT / relative).resolve(strict=True)
            cgroup.relative_to(root)
        except (OSError, ValueError) as exc:
            raise GateAbort("candidate cgroup path escaped its hierarchy") from exc
        if not cgroup.is_dir():
            raise GateAbort("candidate cgroup path was not a directory")
        return pid, cgroup

    def _pid_set(self, *, expected_pid: int, cgroup: Path) -> set[int]:
        try:
            root = cgroup.resolve(strict=True)
        except OSError as exc:
            raise GateAbort("candidate cgroup process tree was unavailable") from exc
        pending = [root]
        visited: set[Path] = set()
        pids: set[int] = set()
        while pending:
            current = pending.pop()
            if current in visited or len(visited) >= 4096:
                raise GateAbort("candidate cgroup process tree was invalid")
            try:
                metadata = current.lstat()
                resolved = current.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as exc:
                raise GateAbort("candidate cgroup process tree escaped") from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise GateAbort("candidate cgroup process tree was invalid")
            visited.add(current)
            raw = self._read_bounded(
                current / "cgroup.procs",
                maximum=1024 * 1024,
                label="candidate cgroup process set",
            )
            for line in raw.splitlines():
                if re.fullmatch(r"[1-9][0-9]*", line) is None:
                    raise GateAbort("candidate cgroup process set was malformed")
                pid = int(line)
                if pid > 2**63 - 1 or pid in pids:
                    raise GateAbort("candidate cgroup process set was invalid")
                pids.add(pid)
            try:
                children = list(current.iterdir())
            except OSError as exc:
                raise GateAbort("candidate cgroup process tree changed") from exc
            for child in children:
                try:
                    if child.is_symlink():
                        raise GateAbort("candidate cgroup process tree used a symlink")
                    if child.is_dir():
                        pending.append(child)
                except OSError as exc:
                    raise GateAbort("candidate cgroup process tree changed") from exc
        if expected_pid not in pids:
            raise GateAbort("candidate root PID was absent from its cgroup")
        return pids

    def memory_events(self) -> CgroupSnapshot:
        before_pid, before_cgroup = self._candidate_pid_and_cgroup()
        values = parse_key_value_lines(
            self._read_bounded(
                before_cgroup / "memory.events",
                maximum=64 * 1024,
                label="candidate cgroup memory events",
            ),
            "candidate cgroup memory events",
            required_keys=REQUIRED_MEMORY_EVENTS,
        )
        after_pid, after_cgroup = self._candidate_pid_and_cgroup()
        if (after_pid, after_cgroup) != (before_pid, before_cgroup):
            raise GateAbort("candidate PID or cgroup changed during memory observation")
        return CgroupSnapshot(
            container_pid=before_pid,
            cgroup_path_sha256=sha256_bytes(str(before_cgroup).encode("utf-8")),
            oom=values["oom"],
            oom_kill=values["oom_kill"],
            oom_group_kill=values["oom_group_kill"],
        )

    def attributed_gpu_bytes(self, gpu_uuid: str) -> GpuAttribution:
        if GPU_UUID_RE.fullmatch(gpu_uuid) is None:
            raise GateAbort("candidate GPU attribution UUID was invalid")
        before_pid, before_cgroup = self._candidate_pid_and_cgroup()
        before_pids = self._pid_set(expected_pid=before_pid, cgroup=before_cgroup)
        result = bounded_command(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,gpu_uuid,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            label="candidate GPU process-memory attribution",
            max_bytes=MAX_COMMAND_BYTES,
        )
        after_pid, after_cgroup = self._candidate_pid_and_cgroup()
        after_pids = self._pid_set(expected_pid=after_pid, cgroup=after_cgroup)
        if (after_pid, after_cgroup) != (before_pid, before_cgroup):
            raise GateAbort("candidate PID or cgroup changed during GPU attribution")
        candidate_bytes = attribute_candidate_gpu_bytes(
            result.output,
            before_pids=before_pids,
            after_pids=after_pids,
            expected_gpu_uuid=gpu_uuid,
        )
        validated_monotonic_ns = time.monotonic_ns()
        return GpuAttribution(
            candidate_bytes=candidate_bytes,
            validated_monotonic_ns=validated_monotonic_ns,
            pid_set_sha256=sha256_bytes(canonical_json_line(sorted(before_pids))),
            gpu_uuid_sha256=sha256_bytes(gpu_uuid.encode("ascii")),
        )


def utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_reason(exc: BaseException) -> str:
    if isinstance(exc, GateAbort):
        return exc.code
    if isinstance(exc, KeyboardInterrupt):
        return "operator_interrupt"
    if isinstance(exc, SystemExit):
        return "sampler_cancellation"
    return f"unexpected_{type(exc).__name__.lower()}"


def finite_number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise GateAbort(f"{label} is not numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise GateAbort(f"{label} is not numeric") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise GateAbort(f"{label} is not a finite positive value")
    return number


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        pass


def bounded_command(
    argv: list[str],
    *,
    label: str,
    timeout: float = 10.0,
    max_bytes: int = MAX_COMMAND_BYTES,
    allow_failure: bool = False,
) -> CommandResult:
    """Run a local command without buffering more than ``max_bytes``."""
    if not argv or max_bytes <= 0:
        raise GateAbort(f"invalid {label} command boundary")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise GateAbort(f"{label} command unavailable") from exc
    if process.stdout is None:
        _kill_process(process)
        raise GateAbort(f"{label} command pipe unavailable")

    selector = selectors.DefaultSelector()
    payload = bytearray()
    deadline = time.monotonic() + timeout
    eof = False
    try:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        while not eof or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process(process)
                raise GateAbort(f"{label} command timed out")
            events = selector.select(min(0.2, remaining))
            for key, _mask in events:
                available = max_bytes + 1 - len(payload)
                if available <= 0:
                    _kill_process(process)
                    raise GateAbort(f"{label} output exceeded its byte limit")
                chunk = os.read(key.fd, min(65536, available))
                if not chunk:
                    eof = True
                    selector.unregister(process.stdout)
                    break
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    _kill_process(process)
                    raise GateAbort(f"{label} output exceeded its byte limit")
            if process.poll() is not None and not events and not eof:
                available = max_bytes + 1 - len(payload)
                if available <= 0:
                    _kill_process(process)
                    raise GateAbort(f"{label} output exceeded its byte limit")
                chunk = os.read(process.stdout.fileno(), min(65536, available))
                if chunk:
                    payload.extend(chunk)
                    if len(payload) > max_bytes:
                        _kill_process(process)
                        raise GateAbort(f"{label} output exceeded its byte limit")
                else:
                    eof = True
        returncode = process.wait(timeout=1)
    except BaseException:
        _kill_process(process)
        raise
    finally:
        selector.close()
        process.stdout.close()

    result = CommandResult(returncode, payload.decode("utf-8", errors="replace"))
    if result.returncode != 0 and not allow_failure:
        raise GateAbort(f"{label} command failed")
    return result


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GateAbort("JSON object contained duplicate keys")
        result[key] = value
    return result


def strict_json_object(
    payload: bytes, *, label: str, max_bytes: int = MAX_JSON_BYTES
) -> dict[str, Any]:
    if len(payload) > max_bytes:
        raise GateAbort(f"{label} response exceeded its byte limit")
    try:
        parsed = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateAbort(f"{label} response was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise GateAbort(f"{label} response was not an object")
    return parsed


def canonical_json_line(value: Any) -> bytes:
    """Serialize one canonical ASCII JSONL record."""
    try:
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
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise GateAbort("canonical JSON serialization failed") from exc


def _runtime_receipt_integer(
    receipt: dict[str, Any],
    key: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
    nullable: bool = False,
) -> int | None:
    value = receipt[key]
    if nullable and value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise GateAbort(f"runtime receipt {key} was not a valid integer")
    return value


def _runtime_receipt_digest(
    receipt: dict[str, Any], key: str, *, nullable: bool = False
) -> str | None:
    value = receipt[key]
    if nullable and value is None:
        return None
    if not isinstance(value, str) or LOWER_SHA256_RE.fullmatch(value) is None:
        raise GateAbort(f"runtime receipt {key} was not a lowercase SHA-256")
    return value


def validate_runtime_receipt(payload: bytes) -> dict[str, Any]:
    """Validate one exact, canonical, gate-only runtime receipt record."""
    if not isinstance(payload, bytes):
        raise GateAbort("runtime receipt was not bytes")
    if len(payload) > MAX_RUNTIME_RECEIPT_BYTES:
        raise GateAbort("runtime receipt exceeded its byte limit")
    if not payload.endswith(b"\n") or payload.endswith(b"\n\n"):
        raise GateAbort("runtime receipt was not one complete JSON line")
    receipt = strict_json_object(
        payload,
        label="runtime receipt",
        max_bytes=MAX_RUNTIME_RECEIPT_BYTES,
    )
    if canonical_json_line(receipt) != payload:
        raise GateAbort("runtime receipt bytes were not canonical")
    if set(receipt) != RUNTIME_RECEIPT_KEYS:
        raise GateAbort("runtime receipt keys did not match the exact schema")
    if receipt["schema"] != "subgen.task11b.runtime-receipt/v1":
        raise GateAbort("runtime receipt schema was invalid")
    epoch = receipt["runtime_epoch"]
    if not isinstance(epoch, str) or EPOCH_RE.fullmatch(epoch) is None:
        raise GateAbort("runtime receipt epoch was invalid")

    for key in (
        "gate_token_sha256",
        "observation_digest",
        "transition_observation_digest",
        "policy_sha256",
        "model_identity_sha256",
        "workload_sha256",
    ):
        _runtime_receipt_digest(
            receipt,
            key,
            nullable=key
            in {
                "observation_digest",
                "transition_observation_digest",
                "policy_sha256",
                "model_identity_sha256",
                "workload_sha256",
            },
        )

    _runtime_receipt_integer(receipt, "sequence", minimum=1)
    _runtime_receipt_integer(receipt, "observed_monotonic_ns", minimum=1)
    _runtime_receipt_integer(receipt, "source_generation", minimum=1, nullable=True)
    _runtime_receipt_integer(receipt, "transition_sequence")
    _runtime_receipt_integer(receipt, "heartbeat_age_ms", maximum=60_000, nullable=True)
    _runtime_receipt_integer(receipt, "source_age_ms", maximum=60_000, nullable=True)
    clear_count = _runtime_receipt_integer(receipt, "distinct_clear_count", maximum=3)
    load_generation = _runtime_receipt_integer(receipt, "model_load_generation")
    unload_generation = _runtime_receipt_integer(receipt, "model_unload_generation")
    _runtime_receipt_integer(receipt, "active_cursor_ms", nullable=True)
    _runtime_receipt_integer(receipt, "completed_cursor_ms", nullable=True)
    _runtime_receipt_integer(receipt, "completion_generation")
    _runtime_receipt_integer(receipt, "cuda_oom_generation")
    _runtime_receipt_integer(receipt, "media_failure_generation")

    for key in ("admission_open", "model_resident", "active", "chunk_uncommitted"):
        if not isinstance(receipt[key], bool):
            raise GateAbort(f"runtime receipt {key} was not a boolean")

    priority_state = receipt["priority_state"]
    if priority_state not in {"clear", "neutral", "asserted", "unavailable"}:
        raise GateAbort("runtime receipt priority state was invalid")
    controller_phase = receipt["controller_phase"]
    if controller_phase not in {"normal", "yielding", "recovering"}:
        raise GateAbort("runtime receipt controller phase was invalid")
    recovery_reason = receipt["recovery_reason"]
    if controller_phase == "normal":
        if recovery_reason is not None or receipt["admission_open"] is not True:
            raise GateAbort("runtime receipt normal controller state was inconsistent")
    elif (
        recovery_reason
        not in {"priority_pressure", "resource_pressure", "model_admission"}
        or receipt["admission_open"] is not False
    ):
        raise GateAbort("runtime receipt recovery controller state was inconsistent")
    if priority_state in {"neutral", "asserted", "unavailable"} and clear_count != 0:
        raise GateAbort("runtime receipt clear count was inconsistent")
    if controller_phase == "normal" and not (
        (priority_state == "clear" and clear_count == 3)
        or (priority_state == "neutral" and clear_count == 0)
    ):
        raise GateAbort("runtime receipt admitted priority state was inconsistent")

    last_accepted = (
        receipt["source_generation"],
        receipt["observation_digest"],
        receipt["heartbeat_age_ms"],
        receipt["source_age_ms"],
        receipt["policy_sha256"],
    )
    if any(value is None for value in last_accepted) and not all(
        value is None for value in last_accepted
    ):
        raise GateAbort("runtime receipt last-accepted fields were inconsistent")

    model_resident = receipt["model_resident"]
    if model_resident:
        if (
            receipt["model_identity_sha256"] is None
            or load_generation != unload_generation + 1
        ):
            raise GateAbort("runtime receipt resident model state was inconsistent")
    elif (
        receipt["model_identity_sha256"] is not None
        or load_generation != unload_generation
    ):
        raise GateAbort("runtime receipt unloaded model state was inconsistent")

    active = receipt["active"]
    if active:
        if receipt["active_cursor_ms"] is None or receipt["workload_sha256"] is None:
            raise GateAbort("runtime receipt active workload state was inconsistent")
    elif receipt["active_cursor_ms"] is not None or receipt["chunk_uncommitted"]:
        raise GateAbort("runtime receipt inactive workload state was inconsistent")
    if receipt["chunk_uncommitted"] and not active:
        raise GateAbort("runtime receipt uncommitted chunk was not active")
    return receipt


def _require_exact_keys(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise GateAbort(f"{label} keys did not match the exact schema")
    return value


def _strict_integer_value(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise GateAbort(f"{label} was not a valid integer")
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or LOWER_SHA256_RE.fullmatch(value) is None:
        raise GateAbort(f"{label} was not a lowercase SHA-256")
    return value


def _require_epoch(value: Any, label: str) -> str:
    if not isinstance(value, str) or EPOCH_RE.fullmatch(value) is None:
        raise GateAbort(f"{label} was not a lowercase epoch")
    return value


def _require_oci_digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
    ):
        raise GateAbort(f"{label} was not a canonical OCI digest")
    return value


def _validate_workload_identity(value: Any, label: str) -> dict[str, Any]:
    identity = _require_exact_keys(value, WORKLOAD_IDENTITY_KEYS, label)
    _require_sha256(identity["fixture_sha256"], f"{label} fixture digest")
    for key in ("task", "language"):
        if not isinstance(identity[key], str) or not identity[key]:
            raise GateAbort(f"{label} {key} was invalid")
    cursor = _strict_integer_value(identity["cursor_start_ms"], f"{label} cursor")
    duration = _strict_integer_value(
        identity["total_duration_ms"], f"{label} duration", minimum=1
    )
    if duration <= cursor:
        raise GateAbort(f"{label} duration did not exceed its cursor")
    return identity


def _validate_candidate_identity(value: Any) -> dict[str, Any]:
    identity = _require_exact_keys(value, CANDIDATE_IDENTITY_KEYS, "candidate identity")
    if (
        not isinstance(identity["container_id"], str)
        or CONTAINER_ID_RE.fullmatch(identity["container_id"]) is None
    ):
        raise GateAbort("candidate identity container ID was invalid")
    if (
        not isinstance(identity["runtime_commit"], str)
        or COMMIT_RE.fullmatch(identity["runtime_commit"]) is None
    ):
        raise GateAbort("candidate identity runtime commit was invalid")
    _require_oci_digest(identity["oci_index"], "candidate OCI index")
    _require_oci_digest(identity["config_digest"], "candidate config digest")
    layers = identity["layer_diff_ids"]
    if not isinstance(layers, list) or not layers:
        raise GateAbort("candidate identity layer diff IDs were invalid")
    for layer in layers:
        _require_oci_digest(layer, "candidate layer diff ID")
    if identity["selected_model"] not in MODEL_DESCENT:
        raise GateAbort("candidate identity selected model was invalid")
    if (
        not isinstance(identity["model_revision"], str)
        or MODEL_REVISION_RE.fullmatch(identity["model_revision"]) is None
    ):
        raise GateAbort("candidate identity model revision was not immutable")
    return identity


def validate_docker_daemon_identity(value: Any) -> dict[str, Any]:
    """Validate the observed, secret-safe identity of one local Docker daemon."""
    identity = _require_exact_keys(
        value,
        DOCKER_DAEMON_IDENTITY_KEYS,
        "Docker daemon identity",
    )
    if identity["schema"] != "subgen.task11b.docker-daemon/v1":
        raise GateAbort("Docker daemon identity schema was invalid")
    _require_sha256(identity["engine_id_sha256"], "Docker Engine ID digest")
    _require_sha256(identity["host_boot_id_sha256"], "Docker host boot ID digest")
    if identity["docker_host"] != DOCKER_HOST or identity["os_type"] != "linux":
        raise GateAbort("Docker daemon endpoint or platform identity was invalid")
    return identity


def docker_daemon_identity_document(
    engine_id_sha256: str, host_boot_id_sha256: str
) -> dict[str, Any]:
    return validate_docker_daemon_identity(
        {
            "schema": "subgen.task11b.docker-daemon/v1",
            "engine_id_sha256": engine_id_sha256,
            "host_boot_id_sha256": host_boot_id_sha256,
            "docker_host": DOCKER_HOST,
            "os_type": "linux",
        }
    )


def docker_daemon_identity_sha256(value: Any) -> str:
    identity = validate_docker_daemon_identity(value)
    return sha256_bytes(_canonical_json_bytes(identity))


def validate_candidate_identity_document(document: dict[str, Any]) -> dict[str, Any]:
    result = _require_exact_keys(
        document,
        CANDIDATE_IDENTITY_DOCUMENT_KEYS,
        "candidate identity document",
    )
    if result["schema"] != "subgen.task11b.candidate-identity/v2":
        raise GateAbort("candidate identity document schema was invalid")
    _validate_candidate_identity(result["candidate_identity"])
    for key in (
        "docker_daemon_identity_sha256",
        "execution_boundary_manifest_sha256",
        "gate_token_sha256",
        "intended_command_sha256",
    ):
        _require_sha256(result[key], f"candidate identity {key}")
    if result["created_stopped"] is not True:
        raise GateAbort("candidate identity was not captured while stopped")
    return result


def _validated_trace_receipts(
    receipts: Any,
    *,
    runtime_epoch: str,
    gate_token_sha256: str,
    first_sequence: int,
    label: str,
) -> list[dict[str, Any]]:
    if not isinstance(receipts, list) or not receipts:
        raise GateAbort(f"{label} receipts were empty")
    validated: list[dict[str, Any]] = []
    digests: set[str] = set()
    previous_sequence = first_sequence - 1
    previous_monotonic_ns = -1
    for receipt_value in receipts:
        if not isinstance(receipt_value, dict):
            raise GateAbort(f"{label} receipt was not an object")
        line = canonical_json_line(receipt_value)
        receipt = validate_runtime_receipt(line)
        if receipt["sequence"] != previous_sequence + 1:
            raise GateAbort(f"{label} receipt sequence contained a gap")
        if receipt["observed_monotonic_ns"] <= previous_monotonic_ns:
            raise GateAbort(f"{label} receipt time did not increase")
        if receipt["runtime_epoch"] != runtime_epoch:
            raise GateAbort(f"{label} receipt runtime epoch changed")
        if receipt["gate_token_sha256"] != gate_token_sha256:
            raise GateAbort(f"{label} receipt gate token changed")
        digest = sha256_bytes(line)
        if digest in digests:
            raise GateAbort(f"{label} receipt digest was duplicated")
        digests.add(digest)
        validated.append(receipt)
        previous_sequence = receipt["sequence"]
        previous_monotonic_ns = receipt["observed_monotonic_ns"]
    if len(canonical_json_line({"receipts": validated})) > MAX_RECEIPT_JOURNAL_BYTES:
        raise GateAbort(f"{label} receipts exceeded their byte limit")
    return validated


def validate_runtime_receipt_trace_document(
    document: dict[str, Any],
) -> dict[str, Any]:
    keys = {
        "schema",
        "runtime_epoch",
        "gate_token_sha256",
        "workload_sha256",
        "receipts",
    }
    result = _require_exact_keys(document, keys, "Phase A receipt trace")
    if result["schema"] != "subgen.task11b.runtime-receipt-trace/v1":
        raise GateAbort("Phase A receipt trace schema was invalid")
    epoch = _require_epoch(result["runtime_epoch"], "Phase A receipt trace epoch")
    token = _require_sha256(result["gate_token_sha256"], "Phase A receipt trace token")
    workload = _require_sha256(
        result["workload_sha256"], "Phase A receipt trace workload"
    )
    receipts = _validated_trace_receipts(
        result["receipts"],
        runtime_epoch=epoch,
        gate_token_sha256=token,
        first_sequence=1,
        label="Phase A trace",
    )
    admitted = False
    for receipt in receipts:
        receipt_workload = receipt["workload_sha256"]
        if not admitted:
            if receipt_workload is None:
                if (
                    receipt["active"]
                    or receipt["chunk_uncommitted"]
                    or receipt["active_cursor_ms"] is not None
                    or receipt["completed_cursor_ms"] is not None
                ):
                    raise GateAbort("Phase A pre-admission receipt was not idle")
                continue
            admitted = True
        if receipt_workload != workload:
            raise GateAbort("Phase A receipt workload changed")
    if not admitted or receipts[0]["workload_sha256"] is not None:
        raise GateAbort("Phase A receipt trace lacked its initial gate receipt")
    return result


def validate_phase_b_receipt_trace_document(
    document: dict[str, Any],
) -> dict[str, Any]:
    keys = {
        "schema",
        "runtime_epoch",
        "gate_token_sha256",
        "phase_a_trace_sha256",
        "phase_a_last_sequence",
        "workload_sha256",
        "receipts",
    }
    result = _require_exact_keys(document, keys, "Phase B receipt trace")
    if result["schema"] != "subgen.task11b.phase-b-runtime-receipt-trace/v1":
        raise GateAbort("Phase B receipt trace schema was invalid")
    epoch = _require_epoch(result["runtime_epoch"], "Phase B receipt trace epoch")
    token = _require_sha256(result["gate_token_sha256"], "Phase B receipt trace token")
    _require_sha256(result["phase_a_trace_sha256"], "Phase A trace digest")
    last_sequence = _strict_integer_value(
        result["phase_a_last_sequence"], "Phase A last receipt sequence", minimum=1
    )
    workload = _require_sha256(result["workload_sha256"], "Phase B trace workload")
    receipts = _validated_trace_receipts(
        result["receipts"],
        runtime_epoch=epoch,
        gate_token_sha256=token,
        first_sequence=last_sequence + 1,
        label="Phase B trace",
    )
    admissions = [
        index
        for index, receipt in enumerate(receipts)
        if receipt["workload_sha256"] == workload and receipt["active"]
    ]
    if not admissions:
        raise GateAbort("Phase B receipt trace lacked workload admission")
    admission = admissions[0]
    if admission == 0:
        raise GateAbort("Phase B receipt trace lacked its post-Phase-A baseline")
    prior_workload: str | None = None
    for receipt in receipts[:admission]:
        receipt_workload = receipt["workload_sha256"]
        if (
            not isinstance(receipt_workload, str)
            or receipt_workload == workload
            or (prior_workload is not None and receipt_workload != prior_workload)
            or receipt["active"]
            or receipt["chunk_uncommitted"]
            or receipt["active_cursor_ms"] is not None
            or receipt["completed_cursor_ms"] is None
        ):
            raise GateAbort("Phase B pre-admission receipt was not idle")
        prior_workload = receipt_workload
    for receipt in receipts[admission:]:
        if receipt["workload_sha256"] != workload:
            raise GateAbort("Phase B receipt workload changed")
    return result


def bind_latest_runtime_receipt(
    receipts: list[dict[str, Any]], observed_monotonic_ns: int
) -> ReceiptBinding:
    timestamp = _strict_integer_value(
        observed_monotonic_ns, "receipt binding monotonic time", minimum=1
    )
    if not isinstance(receipts, list) or not receipts:
        raise GateAbort("receipt binding had no receipts")
    latest_index: int | None = None
    previous_time = -1
    for index, receipt in enumerate(receipts):
        validated = validate_runtime_receipt(canonical_json_line(receipt))
        current_time = validated["observed_monotonic_ns"]
        if current_time <= previous_time:
            raise GateAbort("receipt binding trace time did not increase")
        previous_time = current_time
        if current_time <= timestamp:
            latest_index = index
        elif latest_index is not None:
            break
    if latest_index is None:
        raise GateAbort("receipt binding predates the first receipt")
    latest = receipts[latest_index]
    next_time = (
        receipts[latest_index + 1]["observed_monotonic_ns"]
        if latest_index + 1 < len(receipts)
        else None
    )
    if next_time is not None and timestamp >= next_time:
        raise GateAbort("receipt binding did not select the latest receipt")
    payload = canonical_json_line(latest)
    return ReceiptBinding(
        receipt=latest,
        receipt_sha256=sha256_bytes(payload),
        next_observed_monotonic_ns=next_time,
    )


def validate_protected_sample_cadence(
    sample_monotonic_ns: list[int], *, t0_monotonic_ns: int, gpu_proof_monotonic_ns: int
) -> dict[str, int]:
    t0 = _strict_integer_value(t0_monotonic_ns, "protected sample T0", minimum=1)
    gpu_proof = _strict_integer_value(
        gpu_proof_monotonic_ns, "protected GPU proof time", minimum=1
    )
    if (
        gpu_proof < t0
        or not isinstance(sample_monotonic_ns, list)
        or not sample_monotonic_ns
    ):
        raise GateAbort("protected sample interval was invalid")
    validated = [
        _strict_integer_value(value, "protected sample time", minimum=1)
        for value in sample_monotonic_ns
    ]
    blind_intervals = sum(
        following - current > 2_000_000_000
        for current, following in zip(validated, validated[1:])
    )
    if any(
        current >= following for current, following in zip(validated, validated[1:])
    ):
        raise GateAbort("protected sample times did not strictly increase")
    if not (
        validated[0] <= t0 <= validated[0] + 2_000_000_000
        and gpu_proof <= validated[-1] <= gpu_proof + 2_000_000_000
    ):
        raise GateAbort("protected sample endpoints were outside two seconds")
    if blind_intervals:
        raise GateAbort("protected sampling contained a blind interval")
    return {
        "protected_first_sample_monotonic_ns": validated[0],
        "protected_last_sample_monotonic_ns": validated[-1],
        "protected_sample_count": len(validated),
        "protected_blind_interval_count": blind_intervals,
    }


def _validate_phase_a_event(event_value: Any, index: int) -> dict[str, Any]:
    event = _require_exact_keys(event_value, PHASE_A_EVENT_KEYS, "Phase A event")
    expected_kinds = (
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
    event_index = _strict_integer_value(
        event["event_index"], "Phase A event index", maximum=9
    )
    if event_index != index or event["kind"] != expected_kinds[index]:
        raise GateAbort("Phase A event order was invalid")
    for key in (
        "monotonic_ns",
        "source_generation",
        "runtime_started_monotonic_ns",
    ):
        _strict_integer_value(event[key], f"Phase A event {key}", minimum=1)
    for key in (
        "transition_sequence",
        "distinct_clear_count",
        "model_load_generation",
        "model_unload_generation",
        "completion_generation",
        "output_count",
        "marker_count",
        "output_create_count",
        "marker_create_count",
        "candidate_bytes",
        "cuda_oom_generation",
        "media_failure_generation",
    ):
        maximum = 3 if key == "distinct_clear_count" else 2**63 - 1
        _strict_integer_value(event[key], f"Phase A event {key}", maximum=maximum)
    for key, maximum in (("heartbeat_age_ms", 10_000), ("source_age_ms", 30_000)):
        _strict_integer_value(event[key], f"Phase A event {key}", maximum=maximum)
    for key in ("cursor_ms", "last_completed_cursor_ms"):
        if event[key] is not None:
            _strict_integer_value(event[key], f"Phase A event {key}")
    _require_epoch(event["runtime_epoch"], "Phase A event runtime epoch")
    for key in (
        "observation_digest",
        "gate_receipt_sha256",
        "policy_sha256",
    ):
        _require_sha256(event[key], f"Phase A event {key}")
    for key in ("transition_observation_digest", "model_identity_sha256"):
        if event[key] is not None:
            _require_sha256(event[key], f"Phase A event {key}")
    for key in (
        "admission_open",
        "model_resident",
        "workload_active",
        "chunk_uncommitted",
        "threshold_masking_allowed",
    ):
        if not isinstance(event[key], bool):
            raise GateAbort(f"Phase A event {key} was not a boolean")
    if event["priority_state"] not in {"clear", "neutral", "asserted", "unavailable"}:
        raise GateAbort("Phase A event priority state was invalid")
    if event["controller_phase"] not in {"normal", "yielding", "recovering"}:
        raise GateAbort("Phase A event controller phase was invalid")
    if event["controller_phase"] == "normal":
        if event["recovery_reason"] is not None or not event["admission_open"]:
            raise GateAbort("Phase A event normal state was inconsistent")
    elif (
        event["recovery_reason"]
        not in {"priority_pressure", "resource_pressure", "model_admission"}
        or event["admission_open"]
    ):
        raise GateAbort("Phase A event recovery state was inconsistent")
    if event["model_resident"] != (event["model_identity_sha256"] is not None):
        raise GateAbort("Phase A event model identity was inconsistent")
    return event


def validate_phase_a_document(document: dict[str, Any]) -> dict[str, Any]:
    phase = _require_exact_keys(document, PHASE_A_KEYS, "Phase A document")
    if phase["schema"] != "subgen.task11b.phase-a/v1" or phase["outcome"] != "pass":
        raise GateAbort("Phase A document header was invalid")
    for key in (
        "policy_sha256",
        "unloaded_gpu_envelope_sha256",
        "workload_sha256",
        "candidate_identity_sha256",
        "execution_boundary_manifest_sha256",
        "gate_receipt_trace_sha256",
        "assertion_observation_digest",
        "assertion_observation_sha256",
        "final_output_sha256",
    ):
        _require_sha256(phase[key], f"Phase A {key}")
    runtime_epoch = _require_epoch(phase["runtime_epoch"], "Phase A runtime epoch")
    workload = _validate_workload_identity(
        phase["workload_identity"], "Phase A workload identity"
    )
    if sha256_bytes(canonical_json_line(workload)) != phase["workload_sha256"]:
        raise GateAbort("Phase A workload digest did not match its identity")
    reasons = phase["assertion_reason_codes"]
    if (
        not isinstance(reasons, list)
        or not reasons
        or reasons != sorted(set(reasons))
        or not set(reasons) <= {"higher_priority_busy", "higher_priority_degraded"}
    ):
        raise GateAbort("Phase A assertion reasons were invalid")
    positive_times = (
        "runtime_started_monotonic_ns",
        "assertion_observed_monotonic_ns",
        "t0_monotonic_ns",
        "sealed_monotonic_ns",
        "protected_first_sample_monotonic_ns",
        "protected_last_sample_monotonic_ns",
    )
    for key in positive_times:
        _strict_integer_value(phase[key], f"Phase A {key}", minimum=1)
    _strict_integer_value(
        phase["allowed_unloaded_bytes"], "Phase A allowed unloaded bytes"
    )
    _strict_integer_value(
        phase["protected_sample_count"], "Phase A protected sample count", minimum=1
    )
    zero_counters = (
        "protected_blind_interval_count",
        "protected_threshold_failure_count",
        "candidate_restart_delta",
        "cgroup_oom_delta",
        "cgroup_oom_kill_delta",
        "cgroup_oom_group_kill_delta",
        "runtime_cuda_oom_generation_delta",
        "runtime_media_failure_generation_delta",
        "candidate_cuda_oom_log_match_delta",
        "nvidia_xid_log_match_delta",
    )
    for key in zero_counters:
        if _strict_integer_value(phase[key], f"Phase A {key}") != 0:
            raise GateAbort(f"Phase A {key} was nonzero")
    if phase["candidate_oom_killed"] is not False:
        raise GateAbort("Phase A candidate OOMKilled was not false")
    raw_events = phase["events"]
    if not isinstance(raw_events, list) or len(raw_events) != 10:
        raise GateAbort("Phase A did not contain exactly ten events")
    events = [
        _validate_phase_a_event(event, index) for index, event in enumerate(raw_events)
    ]
    if any(
        event["runtime_epoch"] != runtime_epoch
        or event["runtime_started_monotonic_ns"]
        != phase["runtime_started_monotonic_ns"]
        or event["policy_sha256"] != phase["policy_sha256"]
        for event in events
    ):
        raise GateAbort("Phase A event identity changed")
    event_times = [event["monotonic_ns"] for event in events]
    if any(
        current >= following for current, following in zip(event_times, event_times[1:])
    ):
        raise GateAbort("Phase A event times did not strictly increase")
    if not (
        phase["runtime_started_monotonic_ns"]
        < event_times[0]
        < phase["assertion_observed_monotonic_ns"]
        <= phase["t0_monotonic_ns"]
        <= event_times[1]
    ):
        raise GateAbort("Phase A assertion timing was not causal")
    t0 = phase["t0_monotonic_ns"]
    if (
        event_times[1] > t0 + 15_000_000_000
        or event_times[2] > t0 + 15_000_000_000
        or event_times[3] > t0 + 30_000_000_000
        or event_times[4] > t0 + 45_000_000_000
        or phase["sealed_monotonic_ns"] < event_times[9]
    ):
        raise GateAbort("Phase A deadline was exceeded")
    if not (
        phase["protected_first_sample_monotonic_ns"]
        <= t0
        <= phase["protected_first_sample_monotonic_ns"] + 2_000_000_000
        and event_times[4]
        <= phase["protected_last_sample_monotonic_ns"]
        <= event_times[4] + 2_000_000_000
    ):
        raise GateAbort("Phase A protected sampling cadence was invalid")
    protected_span = (
        phase["protected_last_sample_monotonic_ns"]
        - phase["protected_first_sample_monotonic_ns"]
    )
    minimum_protected_samples = (
        protected_span + 2_000_000_000 - 1
    ) // 2_000_000_000 + 1
    if phase["protected_sample_count"] < minimum_protected_samples:
        raise GateAbort("Phase A protected sample count could not prove its cadence")

    cursor = workload["cursor_start_ms"]
    duration = workload["total_duration_ms"]
    if any(event["cursor_ms"] != cursor for event in events[:9]) or (
        events[9]["cursor_ms"] is not None
        or events[9]["last_completed_cursor_ms"] != duration
    ):
        raise GateAbort("Phase A cursor sequence was invalid")
    if any(event["last_completed_cursor_ms"] is not None for event in events[:9]):
        raise GateAbort("Phase A completed cursor appeared early")
    if [event["workload_active"] for event in events] != [True] * 9 + [False]:
        raise GateAbort("Phase A workload activity sequence was invalid")
    if [event["chunk_uncommitted"] for event in events] != [True, True] + [False] * 8:
        raise GateAbort("Phase A chunk sequence was invalid")
    baseline_completion = events[0]["completion_generation"]
    if any(
        event["completion_generation"] != baseline_completion for event in events[:9]
    ) or (events[9]["completion_generation"] != baseline_completion + 1):
        raise GateAbort("Phase A completion generation was invalid")
    for failure_key in ("cuda_oom_generation", "media_failure_generation"):
        if any(event[failure_key] != events[0][failure_key] for event in events):
            raise GateAbort("Phase A failure generation changed")

    baseline_load = events[0]["model_load_generation"]
    baseline_unload = events[0]["model_unload_generation"]
    if (
        any(
            event["model_load_generation"] != baseline_load
            or event["model_unload_generation"] != baseline_unload
            for event in events[:3]
        )
        or any(
            event["model_load_generation"] != baseline_load
            or event["model_unload_generation"] != baseline_unload + 1
            for event in events[3:8]
        )
        or any(
            event["model_load_generation"] != baseline_load + 1
            or event["model_unload_generation"] != baseline_unload + 1
            for event in events[8:]
        )
    ):
        raise GateAbort("Phase A model generation sequence was invalid")
    model_identity = events[0]["model_identity_sha256"]
    if (
        model_identity is None
        or any(event["model_identity_sha256"] != model_identity for event in events[:3])
        or any(event["model_identity_sha256"] is not None for event in events[3:8])
        or any(event["model_identity_sha256"] != model_identity for event in events[8:])
    ):
        raise GateAbort("Phase A model identity sequence was invalid")
    if [event["threshold_masking_allowed"] for event in events] != (
        [False] * 4 + [True] * 4 + [False] * 2
    ):
        raise GateAbort("Phase A threshold masking sequence was invalid")
    if events[4]["candidate_bytes"] > phase["allowed_unloaded_bytes"]:
        raise GateAbort("Phase A unloaded GPU bound was exceeded")
    if any(
        event["output_count"] != 0 or event["marker_count"] != 0 for event in events[:9]
    ):
        raise GateAbort("Phase A output appeared before completion")
    if any(
        event["output_create_count"] != 0 or event["marker_create_count"] != 0
        for event in events[:9]
    ) or (
        events[9]["output_count"],
        events[9]["marker_count"],
        events[9]["output_create_count"],
        events[9]["marker_create_count"],
    ) != (1, 0, 1, 0):
        raise GateAbort("Phase A final output sequence was invalid")

    if not (
        events[0]["controller_phase"] == "normal"
        and events[0]["admission_open"]
        and (events[0]["priority_state"], events[0]["distinct_clear_count"])
        in {("clear", 3), ("neutral", 0)}
    ):
        raise GateAbort("Phase A pre-assertion state was invalid")
    if any(
        event["priority_state"] != "asserted" or event["distinct_clear_count"] != 0
        for event in events[1:5]
    ):
        raise GateAbort("Phase A asserted state sequence was invalid")
    if events[1]["controller_phase"] not in {"yielding", "recovering"} or any(
        event["controller_phase"] != "recovering"
        or event["recovery_reason"] != "priority_pressure"
        or event["admission_open"]
        for event in events[2:7]
    ):
        raise GateAbort("Phase A recovery state sequence was invalid")
    if (
        events[1]["recovery_reason"] != "priority_pressure"
        or events[1]["admission_open"]
    ):
        raise GateAbort("Phase A assertion consumption state was invalid")
    if [event["distinct_clear_count"] for event in events[5:]] != [
        1,
        2,
        3,
        3,
        3,
    ] or any(event["priority_state"] != "clear" for event in events[5:]):
        raise GateAbort("Phase A clear recovery sequence was invalid")
    if any(
        event["controller_phase"] != "normal"
        or event["recovery_reason"] is not None
        or not event["admission_open"]
        for event in events[7:]
    ):
        raise GateAbort("Phase A post-recovery state was invalid")
    if not (
        events[5]["source_generation"]
        < events[6]["source_generation"]
        < events[7]["source_generation"]
        and events[5]["source_generation"] > events[1]["source_generation"]
    ):
        raise GateAbort("Phase A clear generations were not distinct")
    if len({event["observation_digest"] for event in events[5:8]}) != 3:
        raise GateAbort("Phase A clear observation digests were not distinct")
    if any(
        current["source_generation"] > following["source_generation"]
        for current, following in zip(events, events[1:])
    ):
        raise GateAbort("Phase A source generation regressed")
    if (
        events[1]["observation_digest"] != phase["assertion_observation_digest"]
        or events[1]["transition_observation_digest"]
        != phase["assertion_observation_digest"]
        or events[1]["transition_sequence"] != events[0]["transition_sequence"] + 1
        or any(
            event["transition_sequence"] != events[1]["transition_sequence"]
            or event["transition_observation_digest"]
            != phase["assertion_observation_digest"]
            for event in events[2:5]
        )
        or events[5]["transition_sequence"] != events[1]["transition_sequence"] + 1
        or events[5]["transition_observation_digest"] != events[5]["observation_digest"]
        or any(
            event["transition_sequence"] != events[5]["transition_sequence"]
            or event["transition_observation_digest"]
            != events[5]["transition_observation_digest"]
            for event in events[6:]
        )
    ):
        raise GateAbort("Phase A transition evidence was invalid")
    return phase


def validate_phase_b_document(document: dict[str, Any]) -> dict[str, Any]:
    phase = _require_exact_keys(document, PHASE_B_KEYS, "Phase B document")
    if phase["schema"] != "subgen.task11b.phase-b/v1" or phase["outcome"] != "pass":
        raise GateAbort("Phase B document header was invalid")
    for key in (
        "phase_a_seal_sha256",
        "policy_sha256",
        "producer_epoch_digest",
        "candidate_identity_sha256",
        "execution_boundary_manifest_sha256",
        "workload_sha256",
        "gate_receipt_trace_sha256",
        "model_identity_sha256",
    ):
        _require_sha256(phase[key], f"Phase B {key}")
    runtime_epoch = _require_epoch(phase["runtime_epoch"], "Phase B runtime epoch")
    producer_epoch = _require_epoch(phase["producer_epoch"], "Phase B producer epoch")
    if sha256_bytes(producer_epoch.encode("ascii")) != phase["producer_epoch_digest"]:
        raise GateAbort("Phase B producer epoch digest did not match")
    for key in (
        "started_monotonic_ns",
        "ended_monotonic_ns",
        "phase_a_durable_monotonic_ns",
        "reset_completed_monotonic_ns",
        "runtime_started_monotonic_ns",
    ):
        _strict_integer_value(phase[key], f"Phase B {key}", minimum=1)
    if phase["sample_interval_seconds"] != 5:
        raise GateAbort("Phase B sample interval was not five seconds")
    if not (
        phase["phase_a_durable_monotonic_ns"]
        <= phase["reset_completed_monotonic_ns"]
        < phase["started_monotonic_ns"]
        <= phase["ended_monotonic_ns"]
    ):
        raise GateAbort("Phase B top-level timing was invalid")
    identity = _validate_candidate_identity(phase["candidate_identity"])
    if (
        sha256_bytes(canonical_json_line(identity))
        != phase["candidate_identity_sha256"]
    ):
        raise GateAbort("Phase B candidate identity digest did not match")
    workload = _validate_workload_identity(
        phase["workload_identity"], "Phase B workload identity"
    )
    if sha256_bytes(canonical_json_line(workload)) != phase["workload_sha256"]:
        raise GateAbort("Phase B workload digest did not match")
    raw_samples = phase["samples"]
    if not isinstance(raw_samples, list) or len(raw_samples) != 181:
        raise GateAbort("Phase B did not contain exactly 181 samples")
    stable: dict[str, Any] | None = None
    prior_capture = -1
    prior_source = -1
    zero_keys = {
        "camera_low_ratio_elapsed_ms",
        "detector_stalled_count",
        "embedding_invalid_count",
        "cgroup_oom_delta",
        "cgroup_oom_kill_delta",
        "cgroup_oom_group_kill_delta",
        "runtime_cuda_oom_generation_delta",
        "runtime_media_failure_generation_delta",
        "candidate_cuda_oom_log_match_delta",
        "nvidia_xid_log_match_delta",
        "candidate_restart_delta",
        "frigate_restart_delta",
    }
    stable_keys = {
        "transition_observation_digest",
        "transition_sequence",
        "model_load_generation",
        "model_unload_generation",
        "completion_generation",
        "cuda_oom_generation",
        "media_failure_generation",
        "model_identity_sha256",
    }
    for index, sample_value in enumerate(raw_samples):
        sample = _require_exact_keys(
            sample_value, PHASE_B_SAMPLE_KEYS, "Phase B sample"
        )
        sample_index = _strict_integer_value(
            sample["sample_index"], "Phase B sample index", maximum=180
        )
        scheduled_offset = _strict_integer_value(
            sample["scheduled_offset_seconds"],
            "Phase B sample scheduled offset",
            maximum=900,
        )
        if sample_index != index or scheduled_offset != index * 5:
            raise GateAbort("Phase B sample schedule index was invalid")
        captured = _strict_integer_value(
            sample["captured_monotonic_ns"], "Phase B sample capture", minimum=1
        )
        scheduled = phase["started_monotonic_ns"] + index * 5_000_000_000
        if not scheduled <= captured <= scheduled + 2_000_000_000:
            raise GateAbort("Phase B sample missed its schedule")
        if index and captured - prior_capture < 3_000_000_000:
            raise GateAbort("Phase B samples were caught up in a burst")
        prior_capture = captured
        source = _strict_integer_value(
            sample["source_generation"], "Phase B source generation", minimum=1
        )
        if source < prior_source:
            raise GateAbort("Phase B source generation regressed")
        prior_source = source
        for key in (
            "runtime_started_monotonic_ns",
            "transition_sequence",
            "distinct_clear_count",
            "model_load_generation",
            "model_unload_generation",
            "completion_generation",
            "cuda_oom_generation",
            "media_failure_generation",
            "camera_low_ratio_elapsed_ms",
            "detector_count",
            "detector_stalled_count",
            "embedding_metric_count",
            "embedding_invalid_count",
            "cgroup_oom_delta",
            "cgroup_oom_kill_delta",
            "cgroup_oom_group_kill_delta",
            "runtime_cuda_oom_generation_delta",
            "runtime_media_failure_generation_delta",
            "candidate_cuda_oom_log_match_delta",
            "nvidia_xid_log_match_delta",
            "candidate_restart_delta",
            "frigate_restart_delta",
        ):
            maximum = 3 if key == "distinct_clear_count" else 2**63 - 1
            value = _strict_integer_value(
                sample[key], f"Phase B sample {key}", maximum=maximum
            )
            if key in zero_keys and value != 0:
                raise GateAbort(f"Phase B sample {key} was nonzero")
        for key, maximum in (("heartbeat_age_ms", 10_000), ("source_age_ms", 30_000)):
            _strict_integer_value(sample[key], f"Phase B sample {key}", maximum=maximum)
        for key in (
            "policy_sha256",
            "candidate_identity_sha256",
            "gate_receipt_sha256",
            "model_identity_sha256",
            "observation_digest",
            "transition_observation_digest",
        ):
            _require_sha256(sample[key], f"Phase B sample {key}")
        _require_epoch(sample["producer_epoch"], "Phase B sample producer epoch")
        _require_epoch(sample["runtime_epoch"], "Phase B sample runtime epoch")
        for key in (
            "admission_open",
            "candidate_running",
            "workload_active",
            "model_resident",
            "candidate_oom_killed",
            "ollama_loaded",
        ):
            if not isinstance(sample[key], bool):
                raise GateAbort(f"Phase B sample {key} was not a boolean")
        for key in (
            "detection_fps",
            "camera_min_process_ratio",
            "camera_max_skipped_fps",
        ):
            if isinstance(sample[key], bool) or not isinstance(
                sample[key], (int, float)
            ):
                raise GateAbort(f"Phase B sample {key} was not a JSON number")
            number = finite_number(sample[key], f"Phase B sample {key}")
            if not 0 <= number <= 1_000_000:
                raise GateAbort(f"Phase B sample {key} was outside its range")
        if not (
            float(sample["detection_fps"]) < 80
            and float(sample["camera_min_process_ratio"]) >= 0.98
            and float(sample["camera_max_skipped_fps"]) == 0
            and sample["detector_count"] > 0
            and sample["embedding_metric_count"] > 0
        ):
            raise GateAbort("Phase B strict health threshold failed")
        if not (
            sample["priority_state"] == "clear"
            and sample["controller_phase"] == "normal"
            and sample["recovery_reason"] is None
            and sample["admission_open"]
            and sample["candidate_running"]
            and sample["workload_active"]
            and sample["distinct_clear_count"] == 3
            and sample["model_resident"]
            and not sample["candidate_oom_killed"]
            and not sample["ollama_loaded"]
        ):
            raise GateAbort("Phase B candidate state was not continuously healthy")
        if (
            sample["policy_sha256"] != phase["policy_sha256"]
            or sample["producer_epoch"] != producer_epoch
            or sample["runtime_epoch"] != runtime_epoch
            or sample["runtime_started_monotonic_ns"]
            != phase["runtime_started_monotonic_ns"]
            or sample["candidate_identity_sha256"] != phase["candidate_identity_sha256"]
            or sample["model_identity_sha256"] != phase["model_identity_sha256"]
        ):
            raise GateAbort("Phase B sample identity changed")
        if stable is None:
            stable = {key: sample[key] for key in stable_keys}
        elif any(sample[key] != stable[key] for key in stable_keys):
            raise GateAbort("Phase B runtime generation changed")
    assert stable is not None
    if phase["ended_monotonic_ns"] < raw_samples[-1]["captured_monotonic_ns"] or (
        phase["ended_monotonic_ns"] - phase["started_monotonic_ns"] < 900_000_000_000
    ):
        raise GateAbort("Phase B did not span the full 900 seconds")
    return phase


def validate_final_gate_document(document: dict[str, Any]) -> dict[str, Any]:
    result = _require_exact_keys(document, FINAL_GATE_KEYS, "final gate document")
    if (
        result["schema"] != "subgen.task11b.shared-gpu-gate/v3"
        or result["outcome"] != "pass"
    ):
        raise GateAbort("final gate document header was invalid")
    if (
        not isinstance(result["runtime_commit"], str)
        or COMMIT_RE.fullmatch(result["runtime_commit"]) is None
    ):
        raise GateAbort("final gate runtime commit was invalid")
    _require_oci_digest(result["candidate_oci_index"], "final candidate OCI index")
    _require_oci_digest(
        result["candidate_config_digest"], "final candidate config digest"
    )
    for key in FINAL_GATE_KEYS - {
        "schema",
        "outcome",
        "runtime_commit",
        "candidate_oci_index",
        "candidate_config_digest",
        "cleanup",
    }:
        _require_sha256(result[key], f"final gate {key}")
    cleanup = _require_exact_keys(
        result["cleanup"],
        {"verified_stopped", "candidate_pid_count", "execution_boundary_revalidated"},
        "final gate cleanup",
    )
    if (
        cleanup["verified_stopped"] is not True
        or cleanup["execution_boundary_revalidated"] is not True
        or _strict_integer_value(
            cleanup["candidate_pid_count"], "final gate candidate PID count"
        )
        != 0
    ):
        raise GateAbort("final gate cleanup proof was incomplete")
    return result


def _bounded_ascii(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value)
    ):
        raise GateAbort(f"{label} was not bounded printable ASCII")
    return value


def validate_unloaded_gpu_envelope_document(
    document: dict[str, Any],
) -> dict[str, Any]:
    envelope = _require_exact_keys(
        document,
        {
            "schema",
            "runtime_commit",
            "image",
            "gpu",
            "backend",
            "model_policy",
            "measurement",
        },
        "unloaded GPU envelope",
    )
    if envelope["schema"] != "subgen.unloaded-gpu-envelope/v1":
        raise GateAbort("unloaded GPU envelope schema was invalid")
    if (
        not isinstance(envelope["runtime_commit"], str)
        or COMMIT_RE.fullmatch(envelope["runtime_commit"]) is None
    ):
        raise GateAbort("unloaded GPU envelope runtime commit was invalid")
    image = _require_exact_keys(
        envelope["image"],
        {"oci_index", "config_digest", "layer_diff_ids"},
        "unloaded GPU image",
    )
    _require_oci_digest(image["oci_index"], "unloaded GPU OCI index")
    _require_oci_digest(image["config_digest"], "unloaded GPU config digest")
    layers = image["layer_diff_ids"]
    if not isinstance(layers, list) or not 1 <= len(layers) <= 256:
        raise GateAbort("unloaded GPU ordered layer list was invalid")
    for layer in layers:
        _require_oci_digest(layer, "unloaded GPU layer diff ID")

    gpu = _require_exact_keys(
        envelope["gpu"], {"uuid", "driver_version"}, "unloaded GPU identity"
    )
    if not isinstance(gpu["uuid"], str) or GPU_UUID_RE.fullmatch(gpu["uuid"]) is None:
        raise GateAbort("unloaded GPU UUID was invalid")
    _bounded_ascii(gpu["driver_version"], "unloaded GPU driver version")
    backend = _require_exact_keys(
        envelope["backend"],
        {
            "cuda_version",
            "ctranslate2_version",
            "stable_ts_version",
            "generator_sha256",
        },
        "unloaded GPU backend",
    )
    for key in ("cuda_version", "ctranslate2_version", "stable_ts_version"):
        _bounded_ascii(backend[key], f"unloaded GPU backend {key}")
    _require_sha256(backend["generator_sha256"], "unloaded GPU generator")

    policy = _require_exact_keys(
        envelope["model_policy"],
        {
            "selected_model",
            "model_revision",
            "compute_type",
            "device",
            "device_index",
            "task",
            "language",
            "chunk_seconds",
            "overlap_seconds",
            "fixture_sha256",
            "priority_policy_sha256",
        },
        "unloaded GPU model policy",
    )
    if policy["selected_model"] not in MODEL_DESCENT:
        raise GateAbort("unloaded GPU selected model was invalid")
    if (
        not isinstance(policy["model_revision"], str)
        or MODEL_REVISION_RE.fullmatch(policy["model_revision"]) is None
    ):
        raise GateAbort("unloaded GPU model revision was invalid")
    for key in ("compute_type", "language"):
        _bounded_ascii(policy[key], f"unloaded GPU {key}")
    if policy["device"] != "cuda":
        raise GateAbort("unloaded GPU device was not CUDA")
    _strict_integer_value(
        policy["device_index"], "unloaded GPU device index", maximum=31
    )
    if policy["task"] not in {"transcribe", "translate"}:
        raise GateAbort("unloaded GPU task was invalid")
    if policy["chunk_seconds"] != 300 or isinstance(policy["chunk_seconds"], bool):
        raise GateAbort("unloaded GPU chunk policy was invalid")
    if policy["overlap_seconds"] != 5 or isinstance(policy["overlap_seconds"], bool):
        raise GateAbort("unloaded GPU overlap policy was invalid")
    _require_sha256(policy["fixture_sha256"], "unloaded GPU fixture")
    _require_sha256(policy["priority_policy_sha256"], "unloaded GPU priority policy")

    measurement = _require_exact_keys(
        envelope["measurement"],
        {
            "cycles",
            "cycle_count",
            "samples_per_cycle",
            "interval_seconds",
            "margin_bytes",
            "max_observed_candidate_bytes",
            "allowed_unloaded_bytes",
        },
        "unloaded GPU measurement",
    )
    exact_metadata = {
        "cycle_count": 3,
        "samples_per_cycle": 10,
        "interval_seconds": 1,
        "margin_bytes": 134_217_728,
    }
    for key, expected in exact_metadata.items():
        value = _strict_integer_value(
            measurement[key], f"unloaded GPU measurement {key}"
        )
        if value != expected:
            raise GateAbort(f"unloaded GPU measurement {key} was not exact")
    cycles = measurement["cycles"]
    if not isinstance(cycles, list) or len(cycles) != 3:
        raise GateAbort("unloaded GPU measurement did not contain three cycles")
    all_samples: list[int] = []
    container_digests: set[str] = set()
    cycle_keys = {
        "cycle_index",
        "container_id_sha256",
        "load_generation_before",
        "load_generation_after",
        "inference_completed",
        "inference_result_sha256",
        "unload_generation_before",
        "unload_generation_after",
        "candidate_bytes_samples",
    }
    for index, cycle_value in enumerate(cycles, start=1):
        cycle = _require_exact_keys(
            cycle_value, cycle_keys, "unloaded GPU measurement cycle"
        )
        if (
            _strict_integer_value(
                cycle["cycle_index"], "unloaded GPU cycle index", minimum=1, maximum=3
            )
            != index
        ):
            raise GateAbort("unloaded GPU cycle order was invalid")
        container_digest = _require_sha256(
            cycle["container_id_sha256"], "unloaded GPU cycle container"
        )
        if container_digest in container_digests:
            raise GateAbort("unloaded GPU cycle container identities were not distinct")
        container_digests.add(container_digest)
        for key, expected in (
            ("load_generation_before", 0),
            ("load_generation_after", 1),
            ("unload_generation_before", 0),
            ("unload_generation_after", 1),
        ):
            if (
                _strict_integer_value(cycle[key], f"unloaded GPU cycle {key}")
                != expected
            ):
                raise GateAbort("unloaded GPU cycle generation was invalid")
        if cycle["inference_completed"] is not True:
            raise GateAbort("unloaded GPU inference was not completed")
        _require_sha256(
            cycle["inference_result_sha256"], "unloaded GPU inference result"
        )
        samples = cycle["candidate_bytes_samples"]
        if not isinstance(samples, list) or len(samples) != 10:
            raise GateAbort("unloaded GPU cycle sample count was invalid")
        for sample in samples:
            all_samples.append(
                _strict_integer_value(sample, "unloaded GPU candidate bytes sample")
            )
    observed_maximum = max(all_samples)
    recorded_maximum = _strict_integer_value(
        measurement["max_observed_candidate_bytes"],
        "unloaded GPU observed maximum",
    )
    allowed = _strict_integer_value(
        measurement["allowed_unloaded_bytes"], "unloaded GPU allowed bytes"
    )
    if recorded_maximum != observed_maximum:
        raise GateAbort("unloaded GPU recorded maximum did not match samples")
    if recorded_maximum > 2**63 - 1 - 134_217_728 or allowed != (
        recorded_maximum + 134_217_728
    ):
        raise GateAbort("unloaded GPU allowed bytes arithmetic was invalid")
    return envelope


def compute_model_identity_sha256(
    catalog_entry: dict[str, Any],
    model_policy: dict[str, Any],
    *,
    model_revision: str,
    selected_model: str,
) -> str:
    if not isinstance(catalog_entry, dict) or not isinstance(model_policy, dict):
        raise GateAbort("model identity preimages were not objects")
    if MODEL_REVISION_RE.fullmatch(model_revision) is None:
        raise GateAbort("model identity revision was invalid")
    if selected_model not in MODEL_DESCENT:
        raise GateAbort("model identity selected model was invalid")
    preimage = {
        "catalog_entry_sha256": sha256_bytes(canonical_json_line(catalog_entry)),
        "model_policy_sha256": sha256_bytes(canonical_json_line(model_policy)),
        "model_revision": model_revision,
        "selected_model": selected_model,
    }
    return sha256_bytes(canonical_json_line(preimage))


def attribute_candidate_gpu_bytes(
    raw: str,
    *,
    before_pids: set[int],
    after_pids: set[int],
    expected_gpu_uuid: str,
) -> int:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise GateAbort("GPU process-memory query was invalid or oversized")
    if GPU_UUID_RE.fullmatch(expected_gpu_uuid) is None:
        raise GateAbort("GPU process-memory expected UUID was invalid")
    for label, pids in (("before", before_pids), ("after", after_pids)):
        if not isinstance(pids, set) or any(
            isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            for pid in pids
        ):
            raise GateAbort(f"GPU process-memory {label} PID set was invalid")
    if before_pids != after_pids:
        raise GateAbort("GPU process-memory candidate PID set changed during query")
    seen: set[tuple[int, str]] = set()
    total_mib = 0
    for line in raw.splitlines():
        if not line.strip():
            raise GateAbort("GPU process-memory query contained an empty row")
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 3:
            raise GateAbort("GPU process-memory row field count was invalid")
        pid_text, gpu_uuid, used_text = fields
        if re.fullmatch(r"[1-9][0-9]*", pid_text) is None:
            raise GateAbort("GPU process-memory PID was invalid")
        pid = int(pid_text)
        if pid > 2**63 - 1 or GPU_UUID_RE.fullmatch(gpu_uuid) is None:
            raise GateAbort("GPU process-memory row identity was invalid")
        if re.fullmatch(r"(?:0|[1-9][0-9]*)", used_text) is None:
            raise GateAbort("GPU process-memory used memory was invalid")
        used_mib = int(used_text)
        if used_mib > (2**63 - 1) // MIB:
            raise GateAbort("GPU process-memory used memory overflowed")
        row = (pid, gpu_uuid)
        if row in seen:
            raise GateAbort("GPU process-memory query contained a duplicate row")
        seen.add(row)
        if pid in before_pids:
            if gpu_uuid != expected_gpu_uuid:
                raise GateAbort("candidate PID appeared on another GPU")
            total_mib += used_mib
            if total_mib > (2**63 - 1) // MIB:
                raise GateAbort("GPU process-memory sum overflowed")
    return total_mib * MIB


def validate_priority_signal_bytes(
    payload: bytes,
    *,
    expected_boot_sha256: str,
    expected_policy_sha256: str,
    now_monotonic_ns: int,
) -> dict[str, Any]:
    _require_sha256(expected_boot_sha256, "priority signal expected boot identity")
    _require_sha256(expected_policy_sha256, "priority signal expected policy")
    now = _strict_integer_value(
        now_monotonic_ns, "priority signal current monotonic time", minimum=1
    )
    if len(payload) > 4096:
        raise GateAbort("priority signal exceeded its byte limit")
    signal_document = strict_json_object(
        payload, label="priority signal", max_bytes=4096
    )
    if canonical_json_line(signal_document) != payload:
        raise GateAbort("priority signal bytes were not canonical")
    keys = {
        "schema",
        "boot_id_sha256",
        "producer_epoch",
        "sequence",
        "observed_monotonic_ns",
        "source_generation",
        "source_observed_monotonic_ns",
        "observation_id",
        "policy_sha256",
        "pressure",
        "clear_eligible",
        "reason_codes",
    }
    signal_document = _require_exact_keys(signal_document, keys, "priority signal")
    if signal_document["schema"] != 1 or isinstance(signal_document["schema"], bool):
        raise GateAbort("priority signal schema was invalid")
    if signal_document["boot_id_sha256"] != expected_boot_sha256:
        raise GateAbort("priority signal boot identity did not match")
    if signal_document["policy_sha256"] != expected_policy_sha256:
        raise GateAbort("priority signal policy did not match")
    _require_epoch(signal_document["producer_epoch"], "priority signal producer epoch")
    _require_sha256(signal_document["observation_id"], "priority signal observation ID")
    sequence = _strict_integer_value(
        signal_document["sequence"], "priority signal sequence", minimum=1
    )
    del sequence
    observed = _strict_integer_value(
        signal_document["observed_monotonic_ns"],
        "priority signal observation time",
        minimum=1,
    )
    _strict_integer_value(
        signal_document["source_generation"],
        "priority signal source generation",
        minimum=1,
    )
    source_observed = _strict_integer_value(
        signal_document["source_observed_monotonic_ns"],
        "priority signal source observation time",
        minimum=1,
    )
    if source_observed > observed or observed > now:
        raise GateAbort("priority signal monotonic ordering was invalid")
    if now - observed > 10_000_000_000 or now - source_observed > 30_000_000_000:
        raise GateAbort("priority signal was stale")
    if not isinstance(signal_document["pressure"], bool) or not isinstance(
        signal_document["clear_eligible"], bool
    ):
        raise GateAbort("priority signal decision flags were not booleans")
    if signal_document["pressure"] and signal_document["clear_eligible"]:
        raise GateAbort("priority signal decision flags were contradictory")
    reasons = signal_document["reason_codes"]
    allowed = {
        "higher_priority_busy",
        "higher_priority_degraded",
        "higher_priority_unavailable",
        "policy_drift",
    }
    if (
        not isinstance(reasons, list)
        or reasons != sorted(set(reasons))
        or not set(reasons) <= allowed
        or len(reasons) > 4
    ):
        raise GateAbort("priority signal reason codes were invalid")
    if signal_document["pressure"] != bool(reasons):
        raise GateAbort("priority signal reasons did not match pressure state")
    if signal_document["clear_eligible"] and reasons:
        raise GateAbort("priority signal clear state carried reasons")
    return signal_document


def validate_phase_a_assertion(signal_document: dict[str, Any]) -> str:
    _require_exact_keys(
        signal_document,
        {
            "schema",
            "boot_id_sha256",
            "producer_epoch",
            "sequence",
            "observed_monotonic_ns",
            "source_generation",
            "source_observed_monotonic_ns",
            "observation_id",
            "policy_sha256",
            "pressure",
            "clear_eligible",
            "reason_codes",
        },
        "Phase A assertion",
    )
    reasons = signal_document["reason_codes"]
    if (
        signal_document["pressure"] is not True
        or signal_document["clear_eligible"] is not False
        or not isinstance(reasons, list)
        or not set(reasons) & {"higher_priority_busy", "higher_priority_degraded"}
        or set(reasons) & {"higher_priority_unavailable", "policy_drift"}
    ):
        raise GateAbort("Phase A assertion was not eligible valid telemetry pressure")
    observation_id = signal_document["observation_id"]
    _require_sha256(observation_id, "Phase A assertion observation ID")
    return sha256_bytes(observation_id.encode("ascii"))


def load_canonical_artifact(
    path: Path,
    *,
    validator: Callable[[dict[str, Any]], dict[str, Any]],
    expected_sha256: str | None = None,
    max_bytes: int = MAX_JSON_BYTES,
) -> dict[str, Any]:
    """Read one owner-only canonical evidence document without disclosing it."""
    path = Path(path)
    if not path.is_absolute() or max_bytes <= 0:
        raise GateAbort("canonical artifact path or byte limit was invalid")
    if expected_sha256 is not None:
        _require_sha256(expected_sha256, "canonical artifact expected digest")
    try:
        parent = path.parent.resolve(strict=True)
        parent_lstat = path.parent.lstat()
    except OSError as exc:
        raise GateAbort("canonical artifact parent was unavailable") from exc
    if parent != path.parent.absolute() or stat.S_ISLNK(parent_lstat.st_mode):
        raise GateAbort("canonical artifact parent used a symlink")
    effective_uid = os.geteuid() if hasattr(os, "geteuid") else None
    if (
        not stat.S_ISDIR(parent_lstat.st_mode)
        or (effective_uid is not None and parent_lstat.st_uid != effective_uid)
        or (os.name == "posix" and stat.S_IMODE(parent_lstat.st_mode) != 0o700)
    ):
        raise GateAbort("canonical artifact parent was not owner only")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise GateAbort("canonical artifact could not be opened safely") from exc
    try:
        before = os.fstat(fd)
        current = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
            or (effective_uid is not None and before.st_uid != effective_uid)
            or (os.name == "posix" and stat.S_IMODE(before.st_mode) != 0o600)
            or before.st_nlink != 1
            or before.st_size > max_bytes
        ):
            raise GateAbort("canonical artifact was not owner-only and bounded")
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise GateAbort("canonical artifact changed during read")
    finally:
        os.close(fd)
    raw = bytes(payload)
    if len(raw) > max_bytes:
        raise GateAbort("canonical artifact exceeded its byte limit")
    digest = sha256_bytes(raw)
    if expected_sha256 is not None and digest != expected_sha256:
        raise GateAbort("canonical artifact digest did not match")
    document = strict_json_object(raw, label="canonical artifact", max_bytes=max_bytes)
    if canonical_json_line(document) != raw:
        raise GateAbort("canonical artifact bytes were not canonical")
    validated = validator(document)
    if validated is not document:
        raise GateAbort("canonical artifact validator did not preserve the document")
    return document


def write_canonical_artifact(
    path: Path,
    document: dict[str, Any],
    *,
    validator: Callable[[dict[str, Any]], dict[str, Any]],
) -> CanonicalArtifact:
    """Create, fsync, reopen, and verify one immutable mode-0600 artifact."""
    validated = validator(document)
    if validated is not document:
        raise GateAbort("canonical artifact validator did not preserve the document")
    payload = canonical_json_line(document)
    if len(payload) > MAX_JSON_BYTES:
        raise GateAbort("canonical artifact exceeded its byte limit")
    try:
        _write_private_create_only(Path(path), payload, 0o600)
    except FileExistsError as exc:
        raise GateAbort("canonical artifact already existed") from exc
    except OSError as exc:
        raise GateAbort("canonical artifact create-only write failed") from exc
    digest = sha256_bytes(payload)
    loaded = load_canonical_artifact(
        Path(path),
        validator=validator,
        expected_sha256=digest,
        max_bytes=len(payload),
    )
    return CanonicalArtifact(
        path=Path(path),
        file_sha256=digest,
        size=len(payload),
        document=loaded,
    )


def write_candidate_identity_document(
    path: Path, document: dict[str, Any]
) -> CanonicalArtifact:
    return write_canonical_artifact(
        path, document, validator=validate_candidate_identity_document
    )


def write_runtime_receipt_trace_document(
    path: Path, document: dict[str, Any]
) -> CanonicalArtifact:
    return write_canonical_artifact(
        path, document, validator=validate_runtime_receipt_trace_document
    )


def write_phase_b_receipt_trace_document(
    path: Path, document: dict[str, Any]
) -> CanonicalArtifact:
    return write_canonical_artifact(
        path, document, validator=validate_phase_b_receipt_trace_document
    )


def write_phase_a_document(path: Path, document: dict[str, Any]) -> CanonicalArtifact:
    return write_canonical_artifact(path, document, validator=validate_phase_a_document)


def write_phase_b_document(path: Path, document: dict[str, Any]) -> CanonicalArtifact:
    return write_canonical_artifact(path, document, validator=validate_phase_b_document)


def write_final_gate_document(
    path: Path, document: dict[str, Any]
) -> CanonicalArtifact:
    return write_canonical_artifact(
        path, document, validator=validate_final_gate_document
    )


def _normalized_absolute_path(value: Any, label: str, *, host: bool = False) -> str:
    native_absolute = (
        host
        and isinstance(value, str)
        and Path(value).is_absolute()
        and os.path.normpath(value) == value
    )
    posix_absolute = (
        isinstance(value, str)
        and value.startswith("/")
        and posixpath.normpath(value) == value
        and value != "/"
    )
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or "\x00" in value
        or not (native_absolute or posix_absolute)
    ):
        raise GateAbort(f"{label} was not a normalized absolute path")
    return value


def validate_fixture_record_document(document: dict[str, Any]) -> dict[str, Any]:
    record = _require_exact_keys(
        document,
        {
            "schema",
            "phase",
            "workload_identity",
            "host_media",
            "container_media",
            "host_output",
            "container_output",
            "host_marker",
            "file_identity",
        },
        "fixture record",
    )
    if record["schema"] != "subgen.task11b.fixture-record/v1":
        raise GateAbort("fixture record schema was invalid")
    if record["phase"] not in {"a", "b"}:
        raise GateAbort("fixture record phase was invalid")
    workload = _validate_workload_identity(
        record["workload_identity"], "fixture workload identity"
    )
    if workload["task"] not in {"transcribe", "translate"}:
        raise GateAbort("fixture workload task was invalid")
    for key in ("task", "language"):
        _bounded_ascii(workload[key], f"fixture workload {key}")
    if record["phase"] == "b" and (
        workload["total_duration_ms"] - workload["cursor_start_ms"] <= 900_000
    ):
        raise GateAbort("Phase B fixture did not extend beyond 900 seconds")
    paths = {
        key: _normalized_absolute_path(
            record[key], f"fixture {key}", host=key.startswith("host_")
        )
        for key in ("host_media", "container_media", "host_output", "host_marker")
    }
    paths["container_output"] = _normalized_absolute_path(
        record["container_output"], "fixture container_output"
    )
    if len(set(paths.values())) != len(paths):
        raise GateAbort("fixture paths were not distinct")
    fixture_root = PHASE_FIXTURE_DESTINATIONS[record["phase"]]
    output_root = PHASE_OUTPUT_DESTINATIONS[record["phase"]]
    media_prefix = fixture_root + "/"
    container_media = paths["container_media"]
    if not container_media.startswith(media_prefix):
        raise GateAbort("fixture media left its exact phase namespace")
    relative_media = container_media[len(media_prefix) :]
    if (
        not relative_media
        or relative_media.startswith("/")
        or posixpath.normpath(relative_media) != relative_media
    ):
        raise GateAbort("fixture relative media path was invalid")
    shadow_media = posixpath.join(output_root, relative_media)
    media_stem, media_extension = posixpath.splitext(shadow_media)
    if not media_extension:
        raise GateAbort("fixture media lacked a file extension")
    deterministic_output = media_stem + ".en.srt"
    if paths["container_output"] != deterministic_output:
        raise GateAbort("fixture output did not match its deterministic shadow name")
    if Path(paths["host_marker"]).name != FAILURE_MARKER_FILENAME:
        raise GateAbort("fixture marker did not use the runtime registry name")
    identity = _require_exact_keys(
        record["file_identity"],
        {
            "device",
            "inode",
            "size_bytes",
            "mtime_ns",
            "ctime_ns",
            "owner_uid",
            "mode",
            "link_count",
            "sha256",
        },
        "fixture file identity",
    )
    for key, minimum in (
        ("device", 0),
        ("inode", 1),
        ("size_bytes", 1),
        ("mtime_ns", 1),
        ("ctime_ns", 1),
        ("owner_uid", 0),
        ("mode", 0),
        ("link_count", 1),
    ):
        if key == "mode":
            maximum = 0o7777
        elif key in {"device", "inode"}:
            maximum = 2**64 - 1
        else:
            maximum = 2**63 - 1
        _strict_integer_value(
            identity[key],
            f"fixture file identity {key}",
            minimum=minimum,
            maximum=maximum,
        )
    if identity["link_count"] != 1:
        raise GateAbort("fixture media was not exclusively linked")
    fixture_sha256 = _require_sha256(identity["sha256"], "fixture file digest")
    if workload["fixture_sha256"] != fixture_sha256:
        raise GateAbort("fixture workload and file digest disagreed")
    return record


def load_fixture_record(path: Path, expected_sha256: str) -> dict[str, Any]:
    return load_canonical_artifact(
        path,
        validator=validate_fixture_record_document,
        expected_sha256=expected_sha256,
        max_bytes=MAX_JSON_BYTES,
    )


def revalidate_fixture_record(
    record: dict[str, Any],
    boundary_mount: dict[str, Any],
    output_boundary_mount: dict[str, Any],
) -> FixtureBinding:
    validate_fixture_record_document(record)
    mount = _require_exact_keys(
        boundary_mount,
        {"type", "source", "destination", "mode", "read_write", "propagation"},
        "fixture boundary mount",
    )
    source_text = _normalized_absolute_path(
        mount["source"], "fixture mount source", host=True
    )
    destination = _normalized_absolute_path(
        mount["destination"], "fixture mount destination"
    )
    expected_fixture_destination = PHASE_FIXTURE_DESTINATIONS[record["phase"]]
    if (
        mount["type"] != "bind"
        or destination != expected_fixture_destination
        or mount["mode"] != "ro"
        or mount["read_write"] is not False
        or mount["propagation"] != "rprivate"
    ):
        raise GateAbort(
            "fixture boundary mount destination was not exact and read only"
        )
    media = Path(record["host_media"])
    source = Path(source_text)
    try:
        media_lstat = media.lstat()
        resolved_media = media.resolve(strict=True)
        resolved_source = source.resolve(strict=True)
    except OSError as exc:
        raise GateAbort("fixture media or mount source was unavailable") from exc
    if (
        stat.S_ISLNK(media_lstat.st_mode)
        or not stat.S_ISREG(media_lstat.st_mode)
        or media_lstat.st_nlink != 1
        or resolved_media != media.absolute()
    ):
        raise GateAbort("fixture media was not one real regular file")
    try:
        if resolved_source.is_dir():
            relative = resolved_media.relative_to(resolved_source)
            mapped_container = posixpath.join(destination, relative.as_posix())
        elif resolved_source == resolved_media:
            mapped_container = destination
        else:
            raise ValueError
    except ValueError as exc:
        raise GateAbort("fixture media escaped its boundary mount") from exc
    if mapped_container != record["container_media"]:
        raise GateAbort("fixture container media path did not match its mount")
    actual_identity = {
        "device": media_lstat.st_dev,
        "inode": media_lstat.st_ino,
        "size_bytes": media_lstat.st_size,
        "mtime_ns": media_lstat.st_mtime_ns,
        "ctime_ns": media_lstat.st_ctime_ns,
        "owner_uid": media_lstat.st_uid,
        "mode": stat.S_IMODE(media_lstat.st_mode),
        "link_count": media_lstat.st_nlink,
        "sha256": sha256_file(resolved_media),
    }
    if actual_identity != record["file_identity"]:
        raise GateAbort("fixture media identity changed")

    output_mount = _require_exact_keys(
        output_boundary_mount,
        {"type", "source", "destination", "mode", "read_write", "propagation"},
        "fixture output boundary mount",
    )
    output_source_text = _normalized_absolute_path(
        output_mount["source"], "fixture output mount source", host=True
    )
    output_destination = _normalized_absolute_path(
        output_mount["destination"], "fixture output mount destination"
    )
    expected_output_destination = PHASE_OUTPUT_DESTINATIONS[record["phase"]]
    if (
        output_mount["type"] != "bind"
        or output_destination != expected_output_destination
        or output_mount["mode"] != "rw"
        or output_mount["read_write"] is not True
        or output_mount["propagation"] != "rprivate"
    ):
        raise GateAbort(
            "fixture output boundary mount destination was not exact and writable"
        )
    output_source = Path(output_source_text)
    try:
        output_source_lstat = output_source.lstat()
        resolved_output_source = output_source.resolve(strict=True)
    except OSError as exc:
        raise GateAbort("fixture output mount source was unavailable") from exc
    if (
        stat.S_ISLNK(output_source_lstat.st_mode)
        or not stat.S_ISDIR(output_source_lstat.st_mode)
        or resolved_output_source != output_source.absolute()
    ):
        raise GateAbort("fixture output mount source was not one real directory")
    try:
        resolved_source.relative_to(resolved_output_source)
        roots_overlap = True
    except ValueError:
        try:
            resolved_output_source.relative_to(resolved_source)
            roots_overlap = True
        except ValueError:
            roots_overlap = False
    if roots_overlap:
        raise GateAbort("fixture input and output boundary mounts overlapped")

    output = Path(record["host_output"])
    expected_disposable_root = resolved_output_source.parent.parent
    expected_output_source = (
        expected_disposable_root / "task11b-output" / f"phase-{record['phase']}"
    )
    if resolved_output_source != expected_output_source:
        raise GateAbort("fixture output mount source did not match its exact contract")
    marker_root = expected_disposable_root / "monitor"
    marker = marker_root / FAILURE_MARKER_FILENAME
    if Path(record["host_marker"]) != marker:
        raise GateAbort("fixture marker did not map the runtime registry contract")
    if output.exists() or output.is_symlink() or marker.exists() or marker.is_symlink():
        raise GateAbort("fixture output or marker already existed")
    try:
        resolved_output_parent = output.parent.resolve(strict=True)
        resolved_marker_root = marker_root.resolve(strict=True)
    except OSError as exc:
        raise GateAbort("fixture output or marker parent was unavailable") from exc
    if (
        not resolved_output_parent.is_dir()
        or resolved_output_parent != output.parent.absolute()
        or not resolved_marker_root.is_dir()
        or resolved_marker_root != marker_root.absolute()
    ):
        raise GateAbort("fixture output or marker parent was not one real directory")
    try:
        relative_output_parent = resolved_output_parent.relative_to(
            resolved_output_source
        )
    except ValueError as exc:
        raise GateAbort("fixture output escaped its writable boundary mount") from exc
    mapped_output = posixpath.join(
        output_destination,
        (relative_output_parent / output.name).as_posix(),
    )
    if mapped_output != record["container_output"]:
        raise GateAbort("fixture host output did not map to deterministic output")
    workload = record["workload_identity"]
    assert isinstance(workload, dict)
    return FixtureBinding(
        record_sha256=sha256_bytes(canonical_json_line(record)),
        workload_identity=workload,
        workload_sha256=sha256_bytes(canonical_json_line(workload)),
        host_media=resolved_media,
        container_media=record["container_media"],
        host_output=output,
        container_output=record["container_output"],
        host_marker=marker,
        container_marker=FAILURE_MARKER_CONTAINER_PATH,
        duration_ms=workload["total_duration_ms"],
        file_identity=actual_identity,
        boundary_mount=dict(mount),
        output_boundary_mount=dict(output_mount),
    )


def require_exact_endpoint(url: str, endpoint: str) -> None:
    expected = EXACT_ENDPOINTS[endpoint]
    parsed = urllib.parse.urlsplit(url)
    if (
        url != expected
        or parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise GateAbort(
            f"{endpoint} endpoint is outside the approved loopback boundary"
        )


def _parse_http_headers(
    header_block: bytes, *, endpoint: str
) -> tuple[int | None, bool]:
    """Return a content length and whether the body is chunked."""
    try:
        lines = header_block.decode("iso-8859-1").split("\r\n")
    except UnicodeDecodeError as exc:  # pragma: no cover - iso-8859-1 is total
        raise GateAbort(f"{endpoint} response headers were invalid") from exc
    if not lines or not re.fullmatch(r"HTTP/1\.[01] 200(?: .*)?", lines[0]):
        raise GateAbort(f"{endpoint} response status was not 200")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            raise GateAbort(f"{endpoint} response headers were invalid")
        name, value = line.split(":", 1)
        key = name.strip().lower()
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", key) or key in headers:
            raise GateAbort(f"{endpoint} response headers were invalid")
        headers[key] = value.strip()
    transfer = headers.get("transfer-encoding")
    chunked = transfer is not None
    if chunked and transfer.lower() != "chunked":
        raise GateAbort(f"{endpoint} response encoding was unsupported")
    length_text = headers.get("content-length")
    if chunked and length_text is not None:
        raise GateAbort(f"{endpoint} response framing was ambiguous")
    if length_text is None:
        return None, chunked
    try:
        length = int(length_text)
    except ValueError as exc:
        raise GateAbort(f"{endpoint} response length was invalid") from exc
    if length < 0 or length > MAX_JSON_BYTES:
        raise GateAbort(f"{endpoint} response exceeded its byte limit")
    return length, chunked


def _decode_chunked_body(payload: bytes, *, endpoint: str, eof: bool) -> bytes | None:
    decoded = bytearray()
    position = 0
    while True:
        line_end = payload.find(b"\r\n", position)
        if line_end < 0:
            if eof:
                raise GateAbort(f"{endpoint} chunked response was truncated")
            return None
        size_text = payload[position:line_end].split(b";", 1)[0]
        if not size_text or len(size_text) > 16:
            raise GateAbort(f"{endpoint} chunked response was invalid")
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise GateAbort(f"{endpoint} chunked response was invalid") from exc
        position = line_end + 2
        if size == 0:
            # Trailer fields are deliberately unsupported. The exact telemetry
            # endpoints do not use them, and rejecting them keeps framing small.
            if len(payload) < position + 2:
                if eof:
                    raise GateAbort(f"{endpoint} chunked response was truncated")
                return None
            if payload[position:] != b"\r\n":
                raise GateAbort(f"{endpoint} chunked response had trailing bytes")
            return bytes(decoded)
        if size > MAX_JSON_BYTES or len(decoded) + size > MAX_JSON_BYTES:
            raise GateAbort(f"{endpoint} response exceeded its byte limit")
        chunk_end = position + size
        if len(payload) < chunk_end + 2:
            if eof:
                raise GateAbort(f"{endpoint} chunked response was truncated")
            return None
        if payload[chunk_end : chunk_end + 2] != b"\r\n":
            raise GateAbort(f"{endpoint} chunked response was invalid")
        decoded.extend(payload[position:chunk_end])
        position = chunk_end + 2


def _complete_http_body(payload: bytes, *, endpoint: str, eof: bool) -> bytes | None:
    delimiter = payload.find(b"\r\n\r\n")
    if delimiter < 0:
        if len(payload) > MAX_HTTP_HEADER_BYTES:
            raise GateAbort(f"{endpoint} response headers exceeded their byte limit")
        if eof:
            raise GateAbort(f"{endpoint} response headers were truncated")
        return None
    if delimiter > MAX_HTTP_HEADER_BYTES:
        raise GateAbort(f"{endpoint} response headers exceeded their byte limit")
    declared, chunked = _parse_http_headers(payload[:delimiter], endpoint=endpoint)
    body = payload[delimiter + 4 :]
    if chunked:
        return _decode_chunked_body(body, endpoint=endpoint, eof=eof)
    if declared is not None:
        if len(body) < declared:
            if eof:
                raise GateAbort(f"{endpoint} response body was truncated")
            return None
        if len(body) != declared:
            raise GateAbort(f"{endpoint} response had trailing bytes")
        return body
    if len(body) > MAX_JSON_BYTES:
        raise GateAbort(f"{endpoint} response exceeded its byte limit")
    return body if eof else None


def _wait_socket(
    selector: selectors.BaseSelector,
    deadline: float,
    *,
    endpoint: str,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not selector.select(remaining):
        raise TelemetryUnavailable(f"{endpoint} telemetry deadline expired")


def fetch_json(url: str, *, endpoint: str, timeout: float = 3.0) -> dict[str, Any]:
    """Fetch exact loopback JSON with a hard end-to-end monotonic deadline."""
    require_exact_endpoint(url, endpoint)
    if not math.isfinite(timeout) or timeout <= 0:
        raise GateAbort(f"{endpoint} telemetry timeout was invalid")
    parsed = urllib.parse.urlsplit(url)
    assert parsed.hostname == "127.0.0.1" and parsed.port is not None
    path = parsed.path or "/"
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{parsed.port}\r\n"
        "Accept: application/json\r\n"
        "Accept-Encoding: identity\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    deadline = time.monotonic() + timeout
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    selector = selectors.DefaultSelector()
    wire = bytearray()
    try:
        sock.setblocking(False)
        result = sock.connect_ex(("127.0.0.1", parsed.port))
        in_progress = {
            0,
            errno.EINPROGRESS,
            errno.EWOULDBLOCK,
            errno.EALREADY,
        }
        if result not in in_progress:
            raise TelemetryUnavailable(f"{endpoint} telemetry unavailable")
        selector.register(sock, selectors.EVENT_WRITE)
        if result != 0:
            _wait_socket(selector, deadline, endpoint=endpoint)
            if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) != 0:
                raise TelemetryUnavailable(f"{endpoint} telemetry unavailable")
        sent = 0
        while sent < len(request):
            _wait_socket(selector, deadline, endpoint=endpoint)
            try:
                written = sock.send(request[sent:])
            except BlockingIOError:
                continue
            if written <= 0:
                raise TelemetryUnavailable(f"{endpoint} telemetry unavailable")
            sent += written
        selector.modify(sock, selectors.EVENT_READ)
        eof = False
        while True:
            complete = _complete_http_body(bytes(wire), endpoint=endpoint, eof=eof)
            if complete is not None:
                return strict_json_object(complete, label=endpoint)
            _wait_socket(selector, deadline, endpoint=endpoint)
            try:
                chunk = sock.recv(min(65536, MAX_HTTP_WIRE_BYTES + 1 - len(wire)))
            except BlockingIOError:
                continue
            if not chunk:
                eof = True
                continue
            wire.extend(chunk)
            if len(wire) > MAX_HTTP_WIRE_BYTES:
                raise GateAbort(f"{endpoint} response exceeded its wire byte limit")
    except GateAbort:
        raise
    except OSError as exc:
        raise TelemetryUnavailable(f"{endpoint} telemetry unavailable") from exc
    finally:
        selector.close()
        sock.close()


class DockerClient:
    """A Docker client pinned to the verified local Unix socket and engine."""

    def __init__(self, expected_daemon_id: str, expected_boot_id: str) -> None:
        self.expected_daemon_id = expected_daemon_id
        self.expected_boot_id = expected_boot_id

    @staticmethod
    def _argv(*parts: str) -> list[str]:
        return ["docker", "--host", DOCKER_HOST, *parts]

    def command(
        self,
        *parts: str,
        label: str,
        timeout: float = 10.0,
        max_bytes: int = MAX_COMMAND_BYTES,
        allow_failure: bool = False,
    ) -> CommandResult:
        return bounded_command(
            self._argv(*parts),
            label=label,
            timeout=timeout,
            max_bytes=max_bytes,
            allow_failure=allow_failure,
        )

    def verify_local_daemon(self) -> tuple[str, str]:
        if any(
            os.environ.get(name)
            for name in (
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
            )
        ):
            raise GateAbort("Docker environment overrides are not allowed")
        try:
            socket_stat = os.lstat(DOCKER_SOCKET)
        except OSError as exc:
            raise GateAbort("local Docker socket is unavailable") from exc
        if not stat.S_ISSOCK(socket_stat.st_mode):
            raise GateAbort("local Docker endpoint is not a Unix socket")
        boot_id = read_boot_id()
        daemon_id = self.command(
            "info", "--format", "{{.ID}}", label="Docker daemon identity"
        ).output.strip()
        os_type = self.command(
            "info", "--format", "{{.OSType}}", label="Docker daemon platform"
        ).output.strip()
        if (
            daemon_id != self.expected_daemon_id
            or boot_id != self.expected_boot_id
            or os_type != "linux"
        ):
            raise GateAbort("Docker daemon or host boot identity changed")
        return (
            sha256_bytes(daemon_id.encode("utf-8")),
            sha256_bytes(boot_id.encode("ascii")),
        )

    def inspect(
        self, reference: str, *, missing_ok: bool = False
    ) -> dict[str, Any] | None:
        result = self.command(
            "inspect",
            "--format",
            INSPECT_TEMPLATE,
            reference,
            label="Docker inspection",
            allow_failure=True,
        )
        if result.returncode != 0:
            if missing_ok and "No such object" in result.output:
                return None
            raise GateAbort("Docker inspection failed")
        try:
            item = json.loads(result.output)
        except json.JSONDecodeError as exc:
            raise GateAbort("Docker inspection returned invalid JSON") from exc
        if not isinstance(item, dict):
            raise GateAbort("Docker inspection returned an unexpected object")
        return item


def read_boot_id() -> str:
    try:
        boot_id = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        )
    except OSError as exc:
        raise GateAbort("host boot identity is unavailable") from exc
    if not BOOT_ID_RE.fullmatch(boot_id):
        raise GateAbort("host boot identity is malformed")
    return boot_id


def _container_labels(item: dict[str, Any]) -> dict[str, Any]:
    labels = item.get("Labels")
    if not isinstance(labels, dict):
        raise GateAbort("candidate Docker labels are missing")
    return labels


def _complete_log_config_digest(
    item: dict[str, Any], *, expected: dict[str, Any], label: str
) -> str:
    host = item.get("HostConfigFull")
    if not isinstance(host, dict) or host.get("LogConfig") != expected:
        raise GateAbort(f"{label} log configuration was not complete and non-lossy")
    return sha256_bytes(_canonical_json_bytes(expected))


def _bounded_state(item: dict[str, Any]) -> dict[str, Any]:
    state = item.get("State")
    if not isinstance(state, dict):
        raise GateAbort("Docker state is missing")
    restart_count = item.get("RestartCount")
    if isinstance(restart_count, bool) or not isinstance(restart_count, int):
        raise GateAbort("Docker restart count is invalid")
    return {
        "status": state.get("Status"),
        "running": state.get("Running"),
        "oom_killed": state.get("OOMKilled"),
        "restart_count": restart_count,
        "health": state.get("HealthStatus"),
    }


def _command_digest(item: dict[str, Any]) -> str:
    payload = json.dumps(
        [item.get("Entrypoint"), item.get("Cmd")],
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(payload)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise GateAbort("canonical JSON serialization failed") from exc


def _canonical_host_config_for_hash(host_config: dict[str, Any]) -> dict[str, Any]:
    if "OomKillDisable" not in host_config:
        raise GateAbort("candidate OOM kill disable telemetry was missing")
    oom_kill_disable = host_config["OomKillDisable"]
    if oom_kill_disable is not None and oom_kill_disable is not False:
        raise GateAbort("candidate OOM kill disable policy was unsafe")
    normalized = dict(host_config)
    normalized["OomKillDisable"] = False
    return normalized


def _normalized_environment(item: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    raw = item.get("Env")
    if not isinstance(raw, list):
        raise GateAbort("candidate environment is missing")
    values: dict[str, str] = {}
    for entry in raw:
        if (
            not isinstance(entry, str)
            or "=" not in entry
            or "\x00" in entry
            or len(entry) > 64 * 1024
        ):
            raise GateAbort("candidate environment is malformed")
        key, value = entry.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in values:
            raise GateAbort("candidate environment is malformed or duplicated")
        values[key] = value
    required_safe = {
        "AUTO_DELETE_INVALID_MEDIA": "false",
        "AUTO_DELETE_FAILED_FILES": "false",
    }
    if any(
        values.get(key, "").lower() != expected
        for key, expected in required_safe.items()
    ):
        raise GateAbort("candidate deletion controls were not explicitly off")
    repair_action = values.get("SUBGEN_REPAIR_ACTION")
    if repair_action is not None and repair_action.lower() != "report":
        raise GateAbort("candidate repair action was not report only")
    normalized = [f"{key}={values[key]}" for key in sorted(values)]
    return normalized, values


def _validate_disposable_source(source: str, disposable_root: str) -> None:
    root = Path(disposable_root)
    candidate = Path(source)
    try:
        root_lstat = root.lstat()
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise GateAbort("candidate disposable mount source was unavailable") from exc
    if (
        stat.S_ISLNK(root_lstat.st_mode)
        or not stat.S_ISDIR(root_lstat.st_mode)
        or root_lstat.st_uid != os.geteuid()
        or root_lstat.st_mode & 0o077
        or str(resolved_root) != disposable_root
        or str(resolved_candidate) != source
    ):
        raise GateAbort("candidate disposable root or source was not private and real")
    relative = candidate.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            current_stat = current.lstat()
        except OSError as exc:
            raise GateAbort(
                "candidate disposable mount source was unavailable"
            ) from exc
        if (
            stat.S_ISLNK(current_stat.st_mode)
            or current_stat.st_dev != root_lstat.st_dev
        ):
            raise GateAbort("candidate disposable mount source escaped its filesystem")
    source_stat = candidate.lstat()
    if not (stat.S_ISDIR(source_stat.st_mode) or stat.S_ISREG(source_stat.st_mode)):
        raise GateAbort("candidate disposable mount source type was unsupported")
    if stat.S_ISREG(source_stat.st_mode) and source_stat.st_nlink != 1:
        raise GateAbort("candidate disposable file mount was hard linked")


def _validate_priority_source(source: str) -> None:
    if source != "/run/subgen-priority":
        raise GateAbort("candidate priority mount source was not exact")
    candidate = Path(source)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise GateAbort("candidate priority mount source was unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or str(resolved) != source
    ):
        raise GateAbort("candidate priority mount source was not private and real")


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        try:
            right.relative_to(left)
            return True
        except ValueError:
            return False


def _validate_mount_source_disjointness(
    mounts: list[dict[str, Any]], *, filesystem_check: bool
) -> None:
    sources: list[tuple[str, Path, tuple[int, int] | None]] = []
    for mount in mounts:
        source = mount.get("source")
        destination = mount.get("destination")
        if not isinstance(source, str) or not isinstance(destination, str):
            raise GateAbort("candidate mount source identity was malformed")
        source_path = Path(source)
        inode: tuple[int, int] | None = None
        if filesystem_check:
            try:
                resolved = source_path.resolve(strict=True)
                metadata = source_path.stat()
            except OSError as exc:
                raise GateAbort(
                    "candidate mount source identity was unavailable"
                ) from exc
            source_path = resolved
            inode = (metadata.st_dev, metadata.st_ino)
        sources.append((destination, source_path, inode))

    for index, (left_destination, left_path, left_inode) in enumerate(sources):
        for right_destination, right_path, right_inode in sources[index + 1 :]:
            if _paths_overlap(left_path, right_path) or (
                left_inode is not None and left_inode == right_inode
            ):
                raise GateAbort(
                    "candidate mount sources overlapped across "
                    f"{left_destination} and {right_destination}"
                )


def _normalized_mounts(
    item: dict[str, Any], *, disposable_root: str, filesystem_check: bool
) -> list[dict[str, Any]]:
    mounts = item.get("Mounts")
    if not isinstance(mounts, list) or not mounts:
        raise GateAbort("candidate disposable mount allowlist is missing")
    normalized_root = posixpath.normpath(disposable_root)
    if not normalized_root.startswith("/") or normalized_root == "/":
        raise GateAbort("candidate disposable root is invalid")
    result: list[dict[str, Any]] = []
    destinations: set[str] = set()
    for mount in mounts:
        if not isinstance(mount, dict):
            raise GateAbort("candidate mount telemetry is malformed")
        mount_type = mount.get("Type")
        source = mount.get("Source")
        destination = mount.get("Destination")
        mode = mount.get("Mode")
        read_write = mount.get("RW")
        propagation = mount.get("Propagation")
        if (
            mount_type != "bind"
            or not isinstance(source, str)
            or not isinstance(destination, str)
            or not isinstance(mode, str)
            or not isinstance(read_write, bool)
            or not isinstance(propagation, str)
        ):
            raise GateAbort("candidate mount telemetry is malformed")
        normalized_source = posixpath.normpath(source)
        normalized_destination = posixpath.normpath(destination)
        is_priority_source = normalized_destination == "/run/subgen-priority"
        if (
            not normalized_source.startswith("/")
            or source != normalized_source
            or not normalized_destination.startswith("/")
            or normalized_destination == "/"
            or destination != normalized_destination
            or destination in destinations
        ):
            raise GateAbort("candidate mount escaped the disposable allowlist")
        if is_priority_source:
            if normalized_source != "/run/subgen-priority":
                raise GateAbort("candidate priority mount source was not exact")
        elif (
            posixpath.commonpath((normalized_source, normalized_root))
            != normalized_root
            or normalized_source == normalized_root
        ):
            raise GateAbort("candidate mount escaped the disposable allowlist")
        expected_suffix = EXACT_DISPOSABLE_MOUNT_SUFFIXES.get(normalized_destination)
        if expected_suffix is not None and normalized_source != posixpath.join(
            normalized_root, expected_suffix
        ):
            raise GateAbort("candidate mount source did not match its exact contract")
        if filesystem_check:
            if is_priority_source:
                _validate_priority_source(normalized_source)
            else:
                _validate_disposable_source(normalized_source, normalized_root)
        mode_parts = {part for part in mode.split(",") if part}
        if (read_write and "ro" in mode_parts) or (
            not read_write and "ro" not in mode_parts
        ):
            raise GateAbort("candidate mount mode and access disagreed")
        destinations.add(destination)
        result.append(
            {
                "type": mount_type,
                "source": normalized_source,
                "destination": normalized_destination,
                "mode": mode,
                "read_write": read_write,
                "propagation": propagation,
            }
        )
    normalized = sorted(result, key=lambda mount: mount["destination"])
    _validate_mount_source_disjointness(normalized, filesystem_check=filesystem_check)
    return normalized


def _profiler_command_options(command: list[str]) -> tuple[dict[str, str], set[str]]:
    if not command or command[0] != "/subgen/profile_model_envelopes.py":
        raise GateAbort("profiler command owner was invalid")
    value_options: dict[str, str] = {}
    flags: set[str] = set()
    index = 1
    while index < len(command):
        token = command[index]
        if not isinstance(token, str) or not token.startswith("--"):
            raise GateAbort("profiler command arguments were malformed")
        if token in {"--canonical-shared-cuda", "--require-cgroup"}:
            if token in flags:
                raise GateAbort("profiler command arguments were duplicated")
            flags.add(token)
            index += 1
            continue
        if token in value_options or index + 1 >= len(command):
            raise GateAbort("profiler command arguments were duplicated or incomplete")
        value = command[index + 1]
        if not isinstance(value, str) or value.startswith("--"):
            raise GateAbort("profiler command argument value was invalid")
        value_options[token] = value
        index += 2
    return value_options, flags


def _validate_profiler_command(item: dict[str, Any], args: argparse.Namespace) -> None:
    command = item.get("Cmd")
    expected_prefix = [
        "-c",
        PROFILER_HOLD_PROGRAM,
        PROFILER_STDOUT_PATH,
        PROFILER_RESULT_PATH,
        "--",
    ]
    if (
        not isinstance(command, list)
        or command[: len(expected_prefix)] != expected_prefix
    ):
        raise GateAbort("profiler hold protocol was not exact")
    profiler = command[len(expected_prefix) :]
    if not all(isinstance(value, str) for value in profiler):
        raise GateAbort("profiler command arguments were malformed")
    values, flags = _profiler_command_options(profiler)
    exact = {
        "--catalog-input": PROFILER_INPUT_CATALOG_PATH,
        "--catalog-output": PROFILER_CATALOG_PATH,
        "--identity": PROFILER_IDENTITY_PATH,
        "--media": PROFILER_MEDIA_PATH,
        "--model": args.expected_model,
        "--device": "cuda",
        "--compute-type": "float16",
        "--task": "translate",
        "--inference-concurrency": "1",
        "--model-path": "/subgen/models",
        "--cpu-threads": "4",
        "--chunk-minutes": str(args.expected_chunk_minutes),
    }
    allowed_values = set(exact) | {
        "--model-revision",
        "--runs",
        "--host-margin-mib",
        "--device-margin-mib",
        "--host-reserve-gib",
        "--gpu-reserve-gib",
    }
    if args.expected_model != MODEL_DESCENT[0]:
        allowed_values.add("--after-safe-failure")
    if set(values) != allowed_values:
        raise GateAbort("profiler command option allowlist was not exact")
    if any(values.get(option) != expected for option, expected in exact.items()):
        raise GateAbort("profiler command policy was not exact")
    if flags != {"--canonical-shared-cuda", "--require-cgroup"}:
        raise GateAbort("profiler safety flags were not exact")
    revision = values.get("--model-revision", "")
    if not MODEL_REVISION_RE.fullmatch(revision):
        raise GateAbort("profiler model revision was not immutable")
    try:
        runs = int(values.get("--runs", ""))
        host_margin = int(values.get("--host-margin-mib", ""))
        device_margin = int(values.get("--device-margin-mib", ""))
        host_reserve = float(values.get("--host-reserve-gib", ""))
        gpu_reserve = float(values.get("--gpu-reserve-gib", ""))
    except ValueError as exc:
        raise GateAbort("profiler numeric policy was malformed") from exc
    if (
        not 3 <= runs <= 30
        or host_margin <= 0
        or device_margin <= 0
        or host_reserve <= 0
        or gpu_reserve != args.gpu_free_floor_bytes / GIB
    ):
        raise GateAbort("profiler numeric policy was unsafe")
    try:
        model_index = MODEL_DESCENT.index(args.expected_model)
    except ValueError as exc:
        raise GateAbort("profiler model was invalid") from exc
    prior = values.get("--after-safe-failure")
    expected_prior = None if model_index == 0 else MODEL_DESCENT[model_index - 1]
    if prior != expected_prior:
        raise GateAbort("profiler safe-failure predecessor was not exact")


def _validate_mount_policy(
    mounts: list[dict[str, Any]], *, candidate_mode: str
) -> None:
    if candidate_mode == "runtime":
        policy = {
            "/subgen/models": True,
            "/opt/subgen/monitor": True,
            "/opt/subgen/model-envelopes": False,
            "/fixtures/phase-a": False,
            "/fixtures/phase-b": False,
            "/task11b-output/phase-a": True,
            "/task11b-output/phase-b": True,
            "/run/subgen-priority": False,
            "/run/subgen-task11b": True,
        }
        required = set(policy)
    else:
        policy = {
            "/subgen/models": True,
            "/profile/input": False,
            "/profile/output": True,
            "/run/subgen-priority": False,
        }
        required = set(policy)
    observed = {mount["destination"] for mount in mounts}
    if observed != required:
        raise GateAbort("candidate mount destinations left least privilege policy")
    for mount in mounts:
        writable = policy[mount["destination"]]
        if (
            mount["read_write"] is not writable
            or mount.get("mode") != ("rw" if writable else "ro")
            or mount.get("type") != "bind"
            or mount.get("propagation") != "rprivate"
        ):
            raise GateAbort("candidate mount access left least privilege policy")


def _validate_candidate_semantic_policy(
    item: dict[str, Any], boundary: dict[str, Any], args: argparse.Namespace
) -> None:
    host = item.get("HostConfig")
    host_full = item.get("HostConfigFull")
    if not isinstance(host, dict) or not isinstance(host_full, dict):
        raise GateAbort("candidate semantic boundary telemetry was missing")
    environment, values = _normalized_environment(item)
    del environment
    if values.get("PUID") != "1000" or values.get("PGID") != "1000":
        raise GateAbort("candidate runtime UID policy was not exact")
    if item.get("User") != "1000:1000" or item.get("WorkingDir") != "/subgen":
        raise GateAbort("candidate direct non-root identity was not exact")
    if item.get("Entrypoint") != DIRECT_PYTHON_ENTRYPOINT:
        raise GateAbort("candidate direct Python entrypoint was not exact")
    if host.get("Privileged") is not False or host.get("ReadonlyRootfs") is not True:
        raise GateAbort("candidate privilege or root filesystem policy was unsafe")
    if host.get("CapAdd") not in (None, []) or host.get("CapDrop") != ["ALL"]:
        raise GateAbort("candidate capability policy was unsafe")
    if (
        host.get("Devices") not in (None, [])
        or host.get("DeviceRequests") != SAFE_NVIDIA_DEVICE_REQUESTS
    ):
        raise GateAbort("candidate device policy was unsafe")
    security = host.get("SecurityOpt")
    if not isinstance(security, list) or tuple(security) not in SAFE_SECURITY_OPTIONS:
        raise GateAbort("candidate no-new-privileges policy was missing")
    if host.get("GroupAdd") not in (None, []):
        raise GateAbort("candidate supplementary groups were forbidden")
    exact_namespaces = {
        "PidMode": "",
        "IpcMode": "private",
        "CgroupnsMode": "private",
        "UTSMode": "",
        "UsernsMode": "",
    }
    if any(host.get(key) != value for key, value in exact_namespaces.items()):
        raise GateAbort("candidate namespace policy was not exact")
    if host_full.get("Tmpfs") != SAFE_TMPFS:
        raise GateAbort("candidate bounded tmpfs policy was not exact")
    if host.get("PidsLimit") != 512:
        raise GateAbort("candidate process limit was not exact")
    mounts = boundary.get("mounts")
    if not isinstance(mounts, list):
        raise GateAbort("candidate mount policy was unavailable")
    _validate_mount_policy(mounts, candidate_mode=args.candidate_mode)
    attachments = boundary.get("network_attachments")
    if not isinstance(attachments, dict):
        raise GateAbort("candidate network attachment policy was unavailable")
    if args.candidate_mode == "runtime":
        if item.get("Cmd") != RUNTIME_COMMAND or host.get("NetworkMode") != "bridge":
            raise GateAbort("runtime launch or network policy was not exact")
        if set(attachments) != {"bridge"}:
            raise GateAbort("runtime network attachment policy was not exact")
        runtime_environment = {
            "WHISPER_MODEL": "auto",
            "TRANSCRIBE_DEVICE": "cuda",
            "COMPUTE_TYPE": "float16",
            "CONCURRENT_TRANSCRIPTIONS": "1",
            "MODEL_PATH": "/subgen/models",
            "MODEL_ENVELOPE_CATALOG": "/opt/subgen/model-envelopes/catalog.json",
            "MODEL_ENVELOPE_IDENTITY": "/opt/subgen/model-envelopes/image-identity.json",
            "SUBGEN_FAILURE_MARKER_PATH": FAILURE_MARKER_CONTAINER_PATH,
            "SEGMENTATION_ENABLED": "True",
            "SEGMENTATION_CHUNK_MINUTES": str(args.expected_chunk_minutes),
            "SUBTITLE_LANGUAGE_NAME": "en",
            "SHOW_IN_SUBNAME_SUBGEN": "false",
            "SHOW_IN_SUBNAME_MODEL": "false",
        }
        if any(
            values.get(key) != expected for key, expected in runtime_environment.items()
        ):
            raise GateAbort("runtime exact-envelope environment was not exact")
        if values.get("CANONICAL_SHARED_CUDA", "").casefold() != "true":
            raise GateAbort("runtime shared-CUDA policy was not exact")
        try:
            reserve = float(values.get("GPU_MEMORY_RESERVE_GIB", ""))
        except ValueError as exc:
            raise GateAbort("runtime GPU reserve was malformed") from exc
        if reserve != args.gpu_free_floor_bytes / GIB:
            raise GateAbort("runtime GPU reserve was not exact")
        if values.get("PRIORITY_PRESSURE_FILE") != (
            "/run/subgen-priority/pressure.json"
        ):
            raise GateAbort("runtime priority signal path was not exact")
        gate_values = {
            name: values.get(name)
            for name in (
                "TASK11B_GATE_RECEIPT_FILE",
                "TASK11B_GATE_TOKEN_SHA256",
                "TASK11B_PHASE_A_WORKLOAD_SHA256",
                "TASK11B_PHASE_B_WORKLOAD_SHA256",
            )
        }
        if gate_values["TASK11B_GATE_RECEIPT_FILE"] != (
            "/run/subgen-task11b/runtime-receipts.jsonl"
        ):
            raise GateAbort("runtime gate receipt path was not exact")
        expected_token = sha256_bytes(args.gate_token.encode("ascii"))
        if gate_values["TASK11B_GATE_TOKEN_SHA256"] != expected_token:
            raise GateAbort("runtime gate token digest did not match ownership label")
        phase_a = gate_values["TASK11B_PHASE_A_WORKLOAD_SHA256"]
        phase_b = gate_values["TASK11B_PHASE_B_WORKLOAD_SHA256"]
        if (
            not isinstance(phase_a, str)
            or not LOWER_SHA256_RE.fullmatch(phase_a)
            or not isinstance(phase_b, str)
            or not LOWER_SHA256_RE.fullmatch(phase_b)
            or phase_a == phase_b
        ):
            raise GateAbort("runtime gate workload digests were invalid")
    else:
        if host.get("NetworkMode") != "none":
            raise GateAbort("profiler networking was not disabled")
        if attachments:
            raise GateAbort("profiler network attachments were not empty")
        if values.get("PRIORITY_PRESSURE_FILE") != (
            "/run/subgen-priority/pressure.json"
        ):
            raise GateAbort("profiler priority signal path was not exact")
        if any(
            values.get(name)
            for name in (
                "TASK11B_GATE_RECEIPT_FILE",
                "TASK11B_GATE_TOKEN_SHA256",
                "TASK11B_PHASE_A_WORKLOAD_SHA256",
                "TASK11B_PHASE_B_WORKLOAD_SHA256",
            )
        ):
            raise GateAbort("profiler gate receipt environment was not disabled")
        _validate_profiler_command(item, args)


def canonical_execution_boundary(
    item: dict[str, Any],
    *,
    disposable_root: str,
    model_envelope_catalog_sha256: str,
    phase_a_fixture_record_sha256: str,
    phase_b_fixture_record_sha256: str,
    candidate_identity: dict[str, Any],
    docker_daemon_identity: dict[str, Any],
    filesystem_check: bool = True,
) -> dict[str, Any]:
    """Return the secret-safe, exact execution boundary used by the gate."""
    catalog_sha256 = _require_sha256(
        model_envelope_catalog_sha256,
        "execution boundary model-envelope catalog",
    )
    phase_a_fixture_sha256 = _require_sha256(
        phase_a_fixture_record_sha256,
        "execution boundary Phase-A fixture record",
    )
    phase_b_fixture_sha256 = _require_sha256(
        phase_b_fixture_record_sha256,
        "execution boundary Phase-B fixture record",
    )
    if phase_a_fixture_sha256 == phase_b_fixture_sha256:
        raise GateAbort("execution boundary fixture record digests were not distinct")
    identity = _validate_candidate_identity(candidate_identity)
    daemon_identity = validate_docker_daemon_identity(docker_daemon_identity)
    labels = _container_labels(item)
    if (
        identity["container_id"] != item.get("Id")
        or identity["oci_index"] != item.get("Image")
        or identity["runtime_commit"] != labels.get(RUNTIME_LABEL)
    ):
        raise GateAbort("execution boundary candidate identity disagreed with Docker")
    ownership_labels = {key: labels.get(key) for key in sorted(OWNERSHIP_LABEL_KEYS)}
    if (
        ownership_labels[GATE_LABEL] != "true"
        or not isinstance(ownership_labels[TOKEN_LABEL], str)
        or TOKEN_RE.fullmatch(ownership_labels[TOKEN_LABEL]) is None
        or not isinstance(ownership_labels[ROLE_LABEL], str)
        or ROLE_RE.fullmatch(ownership_labels[ROLE_LABEL]) is None
        or ownership_labels[RUNTIME_LABEL] != identity["runtime_commit"]
    ):
        raise GateAbort("execution boundary ownership labels were invalid")
    if ownership_labels[ROLE_LABEL].startswith("profile-") and (
        ownership_labels[ROLE_LABEL] != f"profile-{identity['selected_model']}"
    ):
        raise GateAbort("execution boundary profiler model label was inconsistent")
    host = item.get("HostConfig")
    host_full = item.get("HostConfigFull")
    config_full = item.get("ConfigFull")
    if (
        not isinstance(host, dict)
        or not isinstance(host_full, dict)
        or not isinstance(config_full, dict)
    ):
        raise GateAbort("candidate Docker boundaries are missing")
    environment, _values = _normalized_environment(item)
    privileged = host.get("Privileged")
    if privileged is not False:
        raise GateAbort("candidate privileged mode is forbidden")
    for key in ("PidMode", "IpcMode", "CgroupnsMode", "UTSMode", "UsernsMode"):
        value = host.get(key)
        if not isinstance(value, str) or value == "host":
            raise GateAbort("candidate host namespace sharing is forbidden")
    readonly_rootfs = host.get("ReadonlyRootfs")
    if not isinstance(readonly_rootfs, bool):
        raise GateAbort("candidate root filesystem mode is invalid")
    user = item.get("User")
    working_dir = item.get("WorkingDir")
    if not isinstance(user, str) or not isinstance(working_dir, str):
        raise GateAbort("candidate user or working directory is invalid")
    exact_host_keys = (
        "RestartPolicy",
        "NetworkMode",
        "Memory",
        "MemorySwap",
        "PortBindings",
        "Privileged",
        "PidMode",
        "IpcMode",
        "CgroupnsMode",
        "UTSMode",
        "UsernsMode",
        "ReadonlyRootfs",
        "CapAdd",
        "CapDrop",
        "Devices",
        "DeviceRequests",
        "SecurityOpt",
        "GroupAdd",
        "PidsLimit",
        "NanoCpus",
        "CpuPeriod",
        "CpuQuota",
        "Tmpfs",
    )
    if any(key not in host for key in exact_host_keys):
        raise GateAbort("candidate execution boundary telemetry is incomplete")
    if any(
        key not in host_full or host_full[key] != host[key] for key in exact_host_keys
    ):
        raise GateAbort("candidate full HostConfig telemetry disagreed")
    config_projection = {
        "Env": item.get("Env"),
        "User": item.get("User"),
        "WorkingDir": item.get("WorkingDir"),
        "Entrypoint": item.get("Entrypoint"),
        "Cmd": item.get("Cmd"),
        "Labels": item.get("Labels"),
    }
    if any(
        key not in config_full or config_full[key] != value
        for key, value in config_projection.items()
    ):
        raise GateAbort("candidate full Config telemetry disagreed")
    log_config = host_full.get("LogConfig")
    if log_config != SAFE_LOG_CONFIG:
        raise GateAbort("candidate log driver cannot prove complete Docker logs")
    canonical_host_full = _canonical_host_config_for_hash(host_full)
    network_settings = item.get("NetworkSettings")
    if not isinstance(network_settings, dict):
        raise GateAbort("candidate network attachment telemetry is missing")
    networks = network_settings.get("Networks")
    if not isinstance(networks, dict):
        raise GateAbort("candidate network attachment telemetry is malformed")
    network_attachments: dict[str, dict[str, Any]] = {}
    for network_name, attachment in networks.items():
        if not isinstance(network_name, str) or not network_name:
            raise GateAbort("candidate network attachment name is malformed")
        if not isinstance(attachment, dict):
            raise GateAbort("candidate network attachment is malformed")
        network_id = attachment.get("NetworkID")
        if not isinstance(network_id, str) or not CONTAINER_ID_RE.fullmatch(network_id):
            raise GateAbort("candidate network attachment ID is malformed")
        network_attachments[network_name] = {
            "network_id": network_id,
            "ipam_config": attachment.get("IPAMConfig"),
            "links": attachment.get("Links"),
            "aliases": attachment.get("Aliases"),
            "driver_options": attachment.get("DriverOpts"),
            "gateway_priority": attachment.get("GwPriority"),
        }
    network_attachments = dict(sorted(network_attachments.items()))
    mounts = _normalized_mounts(
        item,
        disposable_root=disposable_root,
        filesystem_check=filesystem_check,
    )
    if ownership_labels[ROLE_LABEL] == "runtime-auto":
        for destination in PHASE_FIXTURE_DESTINATIONS.values():
            candidates = [
                mount for mount in mounts if mount["destination"] == destination
            ]
            if (
                len(candidates) != 1
                or candidates[0]["type"] != "bind"
                or candidates[0]["mode"] != "ro"
                or candidates[0]["read_write"] is not False
                or candidates[0]["propagation"] != "rprivate"
            ):
                raise GateAbort("runtime fixture mount was not exact and read only")
        for destination in PHASE_OUTPUT_DESTINATIONS.values():
            candidates = [
                mount for mount in mounts if mount["destination"] == destination
            ]
            if (
                len(candidates) != 1
                or candidates[0]["type"] != "bind"
                or candidates[0]["mode"] != "rw"
                or candidates[0]["read_write"] is not True
                or candidates[0]["propagation"] != "rprivate"
            ):
                raise GateAbort("runtime output mount was not exact and writable")
    return {
        "schema": 4,
        "model_envelope_catalog_sha256": catalog_sha256,
        "phase_a_fixture_record_sha256": phase_a_fixture_sha256,
        "phase_b_fixture_record_sha256": phase_b_fixture_sha256,
        "candidate_identity": identity,
        "docker_daemon_identity": daemon_identity,
        "ownership_labels": ownership_labels,
        "environment": environment,
        "environment_sha256": sha256_bytes(_canonical_json_bytes(environment)),
        "config": config_full,
        "config_sha256": sha256_bytes(_canonical_json_bytes(config_full)),
        "host_config": canonical_host_full,
        "host_config_sha256": sha256_bytes(_canonical_json_bytes(canonical_host_full)),
        "network_attachments": network_attachments,
        "network_attachments_sha256": sha256_bytes(
            _canonical_json_bytes(network_attachments)
        ),
        "entrypoint_command_sha256": _command_digest(item),
        "user": user,
        "working_directory": working_dir,
        "host": {key: host[key] for key in exact_host_keys},
        "mounts": mounts,
    }


def execution_boundary_digest(boundary: dict[str, Any]) -> str:
    return sha256_bytes(_canonical_json_bytes(boundary))


def _verify_bound_model_envelope_catalog(
    boundary: dict[str, Any], candidate_mode: str
) -> None:
    destination = (
        "/opt/subgen/model-envelopes"
        if candidate_mode == "runtime"
        else "/profile/input"
    )
    mounts = boundary.get("mounts")
    if not isinstance(mounts, list):
        raise GateAbort("catalog mount boundary was unavailable")
    candidates = [mount for mount in mounts if mount.get("destination") == destination]
    if len(candidates) != 1 or candidates[0].get("read_write") is not False:
        raise GateAbort("catalog mount was not exact and read only")
    source = candidates[0].get("source")
    if not isinstance(source, str):
        raise GateAbort("catalog mount source was invalid")
    path = Path(source) / "catalog.json"
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GateAbort("bound model-envelope catalog was unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_JSON_BYTES
    ):
        raise GateAbort("bound model-envelope catalog was not private and regular")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise GateAbort("bound model-envelope catalog could not be opened") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise GateAbort("bound model-envelope catalog was replaced")
        payload = bytearray()
        while len(payload) <= MAX_JSON_BYTES:
            chunk = os.read(fd, min(65536, MAX_JSON_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise GateAbort("bound model-envelope catalog changed during read")
    finally:
        os.close(fd)
    raw = bytes(payload)
    if len(raw) > MAX_JSON_BYTES:
        raise GateAbort("bound model-envelope catalog exceeded its byte limit")
    validate_model_envelope_catalog_bytes(raw)
    if sha256_bytes(raw) != boundary["model_envelope_catalog_sha256"]:
        raise GateAbort("bound model-envelope catalog digest did not match")


def validate_model_envelope_catalog_bytes(payload: bytes) -> dict[str, Any]:
    """Validate the installed catalog serializer's no-newline canonical form."""
    parsed = strict_json_object(
        payload,
        label="model-envelope catalog",
        max_bytes=MAX_JSON_BYTES,
    )
    if _canonical_json_bytes(parsed) != payload:
        raise GateAbort("model-envelope catalog bytes were not canonical")
    if parsed.get("schema") != "subgen.model-envelope.catalog/v1":
        raise GateAbort("model-envelope catalog schema was invalid")
    return parsed


def load_boundary_expectation(
    path: Path, expected_file_sha256: str
) -> BoundaryExpectation:
    if not path.is_absolute():
        raise GateAbort("boundary expectation path must be absolute")
    parent = path.parent.resolve(strict=True)
    if parent != path.parent.absolute():
        raise GateAbort("boundary expectation parent used a symlink")
    parent_stat = parent.stat()
    if parent_stat.st_uid != os.geteuid() or parent_stat.st_mode & 0o077:
        raise GateAbort("boundary expectation parent is not owner only")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise GateAbort("boundary expectation could not be opened") from exc
    try:
        file_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or file_stat.st_mode & 0o077
            or file_stat.st_size > MAX_BOUNDARY_CONFIG_BYTES
        ):
            raise GateAbort("boundary expectation is not private and bounded")
        chunks: list[bytes] = []
        remaining = MAX_BOUNDARY_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    payload = b"".join(chunks)
    if len(payload) > MAX_BOUNDARY_CONFIG_BYTES:
        raise GateAbort("boundary expectation exceeded byte limit")
    actual_file_sha256 = sha256_bytes(payload)
    if actual_file_sha256 != expected_file_sha256.lower():
        raise GateAbort("boundary expectation checksum mismatch")
    document = strict_json_object(
        payload,
        label="boundary expectation",
        max_bytes=MAX_BOUNDARY_CONFIG_BYTES,
    )
    if canonical_json_line(document) != payload:
        raise GateAbort("boundary expectation bytes were not canonical")
    required_keys = {
        "schema",
        "model_envelope_catalog_sha256",
        "phase_a_fixture_record_sha256",
        "phase_b_fixture_record_sha256",
        "candidate_identity",
        "docker_daemon_identity",
        "ownership_labels",
        "environment",
        "environment_sha256",
        "config",
        "config_sha256",
        "host_config",
        "host_config_sha256",
        "network_attachments",
        "network_attachments_sha256",
        "entrypoint_command_sha256",
        "user",
        "working_directory",
        "host",
        "mounts",
    }
    if document.get("schema") != 4 or set(document) != required_keys:
        raise GateAbort("boundary expectation schema was invalid")
    for value_key, digest_key in (
        ("environment", "environment_sha256"),
        ("config", "config_sha256"),
        ("host_config", "host_config_sha256"),
        ("network_attachments", "network_attachments_sha256"),
    ):
        _require_sha256(document[digest_key], f"boundary {digest_key}")
        if (
            sha256_bytes(_canonical_json_bytes(document[value_key]))
            != document[digest_key]
        ):
            raise GateAbort(f"boundary {value_key} preimage did not match its digest")
    _require_sha256(
        document["model_envelope_catalog_sha256"],
        "boundary model-envelope catalog",
    )
    phase_a_fixture_sha256 = _require_sha256(
        document["phase_a_fixture_record_sha256"],
        "boundary Phase-A fixture record",
    )
    phase_b_fixture_sha256 = _require_sha256(
        document["phase_b_fixture_record_sha256"],
        "boundary Phase-B fixture record",
    )
    if phase_a_fixture_sha256 == phase_b_fixture_sha256:
        raise GateAbort("boundary fixture record digests were not distinct")
    _validate_candidate_identity(document["candidate_identity"])
    validate_docker_daemon_identity(document["docker_daemon_identity"])
    ownership_labels = _require_exact_keys(
        document["ownership_labels"],
        OWNERSHIP_LABEL_KEYS,
        "boundary ownership labels",
    )
    candidate_identity = document["candidate_identity"]
    assert isinstance(candidate_identity, dict)
    if (
        ownership_labels[GATE_LABEL] != "true"
        or not isinstance(ownership_labels[TOKEN_LABEL], str)
        or TOKEN_RE.fullmatch(ownership_labels[TOKEN_LABEL]) is None
        or not isinstance(ownership_labels[ROLE_LABEL], str)
        or ROLE_RE.fullmatch(ownership_labels[ROLE_LABEL]) is None
        or ownership_labels[RUNTIME_LABEL] != candidate_identity["runtime_commit"]
    ):
        raise GateAbort("boundary ownership labels were invalid")
    config = document["config"]
    if (
        not isinstance(config, dict)
        or not isinstance(config.get("Labels"), dict)
        or any(
            config["Labels"].get(key) != value
            for key, value in ownership_labels.items()
        )
    ):
        raise GateAbort("boundary ownership labels disagreed with config")
    return BoundaryExpectation(
        document=document,
        file_sha256=actual_file_sha256,
        canonical_sha256=execution_boundary_digest(document),
    )


def _candidate_boundary_after_basic_validation(
    item: dict[str, Any],
    args: argparse.Namespace,
    *,
    filesystem_check: bool | None = None,
) -> dict[str, Any]:
    host = item.get("HostConfig")
    if not isinstance(host, dict):
        raise GateAbort("candidate Docker boundaries are missing")
    policy = host.get("RestartPolicy")
    if not isinstance(policy, dict) or policy.get("Name") not in {"", "no"}:
        raise GateAbort("candidate restart policy is not disabled")
    if host.get("NetworkMode") == "host":
        raise GateAbort("candidate host networking is forbidden")
    if host.get("Memory") != args.expected_memory_bytes:
        raise GateAbort("candidate Docker memory limit did not match")
    if host.get("MemorySwap") != args.expected_memory_bytes:
        raise GateAbort("candidate Docker swap boundary did not match")
    ports = host.get("PortBindings")
    if args.candidate_mode == "runtime":
        expected_ports = {"9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19000"}]}
        if ports != expected_ports:
            raise GateAbort("candidate status port binding changed")
    elif ports not in (None, {}):
        raise GateAbort("profiler container unexpectedly publishes a port")
    if filesystem_check is None:
        filesystem_check = not getattr(
            args, "_test_skip_disposable_filesystem_check", False
        )
    expectation = getattr(args, "boundary_expectation", None)
    if isinstance(expectation, BoundaryExpectation):
        daemon_identity = expectation.document.get("docker_daemon_identity")
    else:
        daemon_identity = getattr(args, "_observed_docker_daemon_identity", None)
    if not isinstance(daemon_identity, dict):
        raise GateAbort("candidate Docker daemon identity was not bound")
    boundary = canonical_execution_boundary(
        item,
        disposable_root=args.disposable_root,
        model_envelope_catalog_sha256=args.model_envelope_catalog_sha256,
        phase_a_fixture_record_sha256=args.phase_a_fixture_record_sha256,
        phase_b_fixture_record_sha256=args.phase_b_fixture_record_sha256,
        candidate_identity={
            "container_id": args.expected_container_id,
            "runtime_commit": args.runtime_commit,
            "oci_index": args.expected_image_config,
            "config_digest": args.candidate_config_digest,
            "layer_diff_ids": args.candidate_layer_diff_ids,
            "selected_model": args.expected_model,
            "model_revision": args.model_revision,
        },
        docker_daemon_identity=daemon_identity,
        filesystem_check=filesystem_check,
    )
    if filesystem_check:
        _verify_bound_model_envelope_catalog(boundary, args.candidate_mode)
    _validate_candidate_semantic_policy(item, boundary, args)
    return boundary


def _validate_candidate_boundaries(
    item: dict[str, Any],
    args: argparse.Namespace,
    *,
    filesystem_check: bool | None = None,
) -> str:
    expectation = getattr(args, "boundary_expectation", None)
    if not isinstance(expectation, BoundaryExpectation):
        raise GateAbort("candidate boundary expectation was not loaded")
    boundary = _candidate_boundary_after_basic_validation(
        item, args, filesystem_check=filesystem_check
    )
    if boundary != expectation.document:
        raise GateAbort("candidate execution boundary did not match allowlist")
    digest = execution_boundary_digest(boundary)
    if digest != expectation.canonical_sha256:
        raise GateAbort("candidate execution boundary checksum changed")
    return digest


def bind_candidate(client: DockerClient, args: argparse.Namespace) -> CandidateBinding:
    verify_bound_docker_daemon(client, args)
    item = client.inspect(args.container)
    assert item is not None
    if item.get("Id") != args.expected_container_id:
        raise GateAbort("candidate container ID did not match")
    if item.get("Name") != f"/{args.container}":
        raise GateAbort("candidate container name did not match")
    if item.get("Image") != args.expected_image_config:
        raise GateAbort("candidate image config digest did not match")
    labels = _container_labels(item)
    expected_labels = {
        GATE_LABEL: "true",
        TOKEN_LABEL: args.gate_token,
        ROLE_LABEL: args.gate_role,
        RUNTIME_LABEL: args.runtime_commit,
    }
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        raise GateAbort("candidate gate labels did not match")
    boundary_digest = _validate_candidate_boundaries(item, args)
    state = _bounded_state(item)
    if (
        state["status"] != "created"
        or state["running"] is not False
        or state["restart_count"] != 0
        or state["oom_killed"] is not False
    ):
        raise GateAbort("candidate was not a fresh stopped container")
    command_digest = _command_digest(item)
    if command_digest != args.expected_command_sha256.lower():
        raise GateAbort("candidate command digest did not match")
    return CandidateBinding(
        name=args.container,
        container_id=args.expected_container_id,
        image_config=args.expected_image_config,
        runtime_commit=args.runtime_commit,
        gate_role=args.gate_role,
        gate_token_digest=sha256_bytes(args.gate_token.encode("utf-8")),
        command_digest=command_digest,
        boundary_digest=boundary_digest,
    )


def verify_candidate_item(
    item: dict[str, Any],
    binding: CandidateBinding,
    args: argparse.Namespace,
    *,
    require_name: bool = True,
    filesystem_check: bool | None = None,
) -> None:
    if (
        item.get("Id") != binding.container_id
        or item.get("Image") != binding.image_config
        or _command_digest(item) != binding.command_digest
    ):
        raise GateAbort("candidate immutable Docker identity changed")
    if require_name and item.get("Name") != f"/{binding.name}":
        raise GateAbort("candidate immutable Docker name changed")
    labels = _container_labels(item)
    if (
        labels.get(GATE_LABEL) != "true"
        or labels.get(ROLE_LABEL) != binding.gate_role
        or labels.get(RUNTIME_LABEL) != binding.runtime_commit
        or sha256_bytes(str(labels.get(TOKEN_LABEL, "")).encode("utf-8"))
        != binding.gate_token_digest
    ):
        raise GateAbort("candidate immutable gate labels changed")
    if (
        _validate_candidate_boundaries(item, args, filesystem_check=filesystem_check)
        != binding.boundary_digest
    ):
        raise GateAbort("candidate immutable execution boundary changed")


def candidate_state(
    client: DockerClient, binding: CandidateBinding, args: argparse.Namespace
) -> dict[str, Any]:
    by_id = client.inspect(binding.container_id)
    by_name = client.inspect(binding.name)
    assert by_id is not None and by_name is not None
    verify_candidate_item(by_id, binding, args)
    if by_name.get("Id") != binding.container_id:
        raise GateAbort("candidate container name was rebound")
    return _bounded_state(by_id)


def bind_observed_container(client: DockerClient, name: str) -> ObservedBinding:
    item = client.inspect(name)
    assert item is not None
    container_id = item.get("Id")
    image_config = item.get("Image")
    if not isinstance(container_id, str) or not CONTAINER_ID_RE.fullmatch(container_id):
        raise GateAbort("observed container ID is invalid")
    if not isinstance(image_config, str) or not CONFIG_DIGEST_RE.fullmatch(
        image_config
    ):
        raise GateAbort("observed container image identity is invalid")
    if item.get("Name") != f"/{name}":
        raise GateAbort("observed container name changed")
    log_config_digest = _complete_log_config_digest(
        item, expected=SAFE_FRIGATE_LOG_CONFIG, label="Frigate"
    )
    return ObservedBinding(name, container_id, image_config, log_config_digest)


def observed_state(client: DockerClient, binding: ObservedBinding) -> dict[str, Any]:
    by_id = client.inspect(binding.container_id)
    by_name = client.inspect(binding.name)
    assert by_id is not None and by_name is not None
    if (
        by_id.get("Id") != binding.container_id
        or by_id.get("Name") != f"/{binding.name}"
        or by_id.get("Image") != binding.image_config
        or by_name.get("Id") != binding.container_id
    ):
        raise GateAbort("observed container identity changed")
    if (
        _complete_log_config_digest(
            by_id, expected=SAFE_FRIGATE_LOG_CONFIG, label="Frigate"
        )
        != binding.log_config_digest
    ):
        raise GateAbort("Frigate log configuration changed")
    return _bounded_state(by_id)


def stop_bound_candidate(
    client: DockerClient, binding: CandidateBinding, args: argparse.Namespace
) -> dict[str, Any]:
    """Stop by immutable ID after revalidation, then prove it stopped."""
    verify_bound_docker_daemon(client, args)
    item = client.inspect(binding.container_id, missing_ok=True)
    if item is None:
        return {"attempted": False, "verified_stopped": True, "already_absent": True}
    # Cleanup authority comes from the immutable ID, image, command, and four
    # ownership labels. A rename/name-reuse race must not prevent stopping the
    # owned original and must never redirect cleanup to the replacement.
    verify_candidate_item(
        item, binding, args, require_name=False, filesystem_check=False
    )
    before = _bounded_state(item)
    graceful_returncode: int | None = None
    killed = False
    if before["running"] is True:
        result = client.command(
            "stop",
            "--time",
            "10",
            binding.container_id,
            label="candidate fail-closed stop",
            timeout=20,
            allow_failure=True,
        )
        graceful_returncode = result.returncode
        after_stop = client.inspect(binding.container_id, missing_ok=True)
        if after_stop is not None:
            verify_candidate_item(
                after_stop,
                binding,
                args,
                require_name=False,
                filesystem_check=False,
            )
        if result.returncode != 0 or (
            after_stop is not None and _bounded_state(after_stop)["running"] is True
        ):
            kill_result = client.command(
                "kill",
                binding.container_id,
                label="candidate verified kill",
                timeout=10,
                allow_failure=True,
            )
            if kill_result.returncode != 0:
                raise GateAbort("candidate fail-closed stop and kill failed")
            killed = True
    after_item = client.inspect(binding.container_id, missing_ok=True)
    if after_item is not None:
        verify_candidate_item(
            after_item,
            binding,
            args,
            require_name=False,
            filesystem_check=False,
        )
        if _bounded_state(after_item)["running"] is not False:
            raise GateAbort("candidate remained running after cleanup")
    return {
        "attempted": before["running"] is True,
        "graceful_returncode": graceful_returncode,
        "kill_escalated": killed,
        "verified_stopped": True,
        "already_absent": after_item is None,
    }


def parse_key_value_lines(
    raw: str, label: str, *, required_keys: set[str] | None = None
) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) != 2:
            raise GateAbort(f"malformed {label} telemetry")
        key, text = parts
        if key in values:
            raise GateAbort(f"duplicate {label} telemetry key")
        try:
            value = int(text)
        except ValueError as exc:
            raise GateAbort(f"malformed {label} telemetry") from exc
        if value < 0:
            raise GateAbort(f"negative {label} telemetry")
        values[key] = value
    if required_keys is not None and set(values) != required_keys:
        raise GateAbort(f"unexpected {label} telemetry keys")
    return values


def parse_psi(raw: str, label: str) -> dict[str, dict[str, float | int]]:
    rows: dict[str, dict[str, float | int]] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) != 5 or parts[0] in rows:
            raise GateAbort(f"malformed {label} telemetry")
        category = parts[0]
        if category not in {"some", "full"}:
            raise GateAbort(f"malformed {label} telemetry")
        fields: dict[str, float | int] = {}
        for part in parts[1:]:
            if "=" not in part:
                raise GateAbort(f"malformed {label} telemetry")
            key, text = part.split("=", 1)
            if key in fields:
                raise GateAbort(f"malformed {label} telemetry")
            try:
                value: float | int = int(text) if key == "total" else float(text)
            except ValueError as exc:
                raise GateAbort(f"malformed {label} telemetry") from exc
            if not math.isfinite(float(value)) or value < 0:
                raise GateAbort(f"malformed {label} telemetry")
            fields[key] = value
        if set(fields) != {"avg10", "avg60", "avg300", "total"}:
            raise GateAbort(f"malformed {label} telemetry")
        rows[category] = fields
    if set(rows) != {"some", "full"}:
        raise GateAbort(f"incomplete {label} telemetry")
    return rows


def candidate_memory(client: DockerClient, binding: CandidateBinding) -> dict[str, Any]:
    script = """
for p in memory.current memory.peak memory.max memory.swap.current memory.swap.max memory.events memory.pressure; do
  printf '@@%s\\n' "$p"
  cat "/sys/fs/cgroup/$p"
done
""".strip()
    raw = client.command(
        "exec",
        binding.container_id,
        "/bin/sh",
        "-c",
        script,
        label="candidate cgroup telemetry",
        max_bytes=64 * 1024,
    ).output
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("@@"):
            if current is not None:
                if current in sections:
                    raise GateAbort("duplicate candidate cgroup section")
                sections[current] = "\n".join(lines).strip()
            current = line[2:]
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        if current in sections:
            raise GateAbort("duplicate candidate cgroup section")
        sections[current] = "\n".join(lines).strip()
    required = {
        "memory.current",
        "memory.peak",
        "memory.max",
        "memory.swap.current",
        "memory.swap.max",
        "memory.events",
        "memory.pressure",
    }
    if set(sections) != required:
        raise GateAbort("incomplete candidate cgroup telemetry")
    numeric: dict[str, int | str] = {}
    for key in (
        "memory.current",
        "memory.peak",
        "memory.max",
        "memory.swap.current",
        "memory.swap.max",
    ):
        value = sections[key]
        if value == "max":
            numeric[key] = value
        else:
            try:
                parsed = int(value)
            except ValueError as exc:
                raise GateAbort(f"malformed candidate cgroup value {key}") from exc
            if parsed < 0:
                raise GateAbort(f"negative candidate cgroup value {key}")
            numeric[key] = parsed
    return {
        **numeric,
        "events": parse_key_value_lines(
            sections["memory.events"],
            "memory events",
            required_keys=REQUIRED_MEMORY_EVENTS,
        ),
        "pressure_observed_only": parse_psi(
            sections["memory.pressure"], "candidate memory PSI"
        ),
    }


def validate_candidate_memory_snapshot(
    memory: dict[str, Any], *, expected_memory_bytes: int
) -> None:
    if memory.get("memory.max") != expected_memory_bytes:
        raise GateAbort("candidate cgroup memory max mismatch")
    if memory.get("memory.swap.max") != 0:
        raise GateAbort("candidate cgroup had extra swap")
    events = memory.get("events")
    if not isinstance(events, dict) or set(events) != REQUIRED_MEMORY_EVENTS:
        raise GateAbort("candidate cgroup memory events were incomplete")
    if any(events[key] != 0 for key in ("max", "oom", "oom_kill", "oom_group_kill")):
        raise GateAbort("candidate cgroup recorded memory limit event")


def read_mem_available_bytes() -> int:
    try:
        payload = Path("/proc/meminfo").read_text(encoding="ascii")
    except OSError as exc:
        raise GateAbort("host MemAvailable telemetry unavailable") from exc
    if len(payload) > 128 * 1024:
        raise GateAbort("host memory telemetry exceeded byte limit")
    for line in payload.splitlines():
        if line.startswith("MemAvailable:"):
            fields = line.split()
            if len(fields) == 3 and fields[2] == "kB":
                try:
                    return int(fields[1]) * 1024
                except ValueError as exc:
                    raise GateAbort("host MemAvailable telemetry malformed") from exc
    raise GateAbort("host MemAvailable telemetry unavailable")


def read_host_pressure() -> dict[str, dict[str, float | int]]:
    try:
        payload = Path("/proc/pressure/memory").read_text(encoding="ascii")
    except OSError as exc:
        raise GateAbort("host memory PSI unavailable") from exc
    if len(payload) > 16 * 1024:
        raise GateAbort("host memory PSI exceeded byte limit")
    return parse_psi(payload.strip(), "host memory PSI")


def gpu_telemetry() -> dict[str, Any]:
    raw = bounded_command(
        [
            "nvidia-smi",
            "--query-gpu=memory.total,memory.used,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        label="NVIDIA device telemetry",
        max_bytes=16 * 1024,
    ).output
    rows = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(rows) != 1:
        raise GateAbort("expected exactly one NVIDIA GPU")
    fields = [part.strip() for part in rows[0].split(",")]
    if len(fields) != 4:
        raise GateAbort("malformed NVIDIA device telemetry")
    try:
        total_mib, used_mib, free_mib, utilization = map(int, fields)
    except ValueError as exc:
        raise GateAbort("malformed NVIDIA device telemetry") from exc
    process_raw = bounded_command(
        [
            "nvidia-smi",
            "--query-compute-apps=used_memory",
            "--format=csv,noheader,nounits",
        ],
        label="NVIDIA process telemetry",
        max_bytes=16 * 1024,
    ).output
    process_memory: list[int] = []
    for line in process_raw.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            process_memory.append(int(text))
        except ValueError as exc:
            raise GateAbort("malformed NVIDIA process telemetry") from exc
    return {
        "total_mib": total_mib,
        "used_mib": used_mib,
        "free_mib": free_mib,
        "utilization_percent": utilization,
        "compute_process_count": len(process_memory),
        "compute_process_used_mib": sum(process_memory),
    }


def load_camera_expectations(path: Path) -> dict[str, float]:
    if not path.is_absolute():
        raise GateAbort("camera expectations path must be absolute")
    parent = path.parent.resolve(strict=True)
    if parent != path.parent.absolute():
        raise GateAbort("camera expectations parent used a symlink")
    parent_stat = parent.stat()
    if parent_stat.st_uid != os.geteuid() or parent_stat.st_mode & 0o077:
        raise GateAbort("camera expectations parent is not owner only")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise GateAbort("camera expectations file could not be opened") from exc
    try:
        file_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or file_stat.st_mode & 0o077
            or file_stat.st_size > MAX_CAMERA_CONFIG_BYTES
        ):
            raise GateAbort("camera expectations file is not private and bounded")
        payload = os.read(fd, MAX_CAMERA_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    if len(payload) > MAX_CAMERA_CONFIG_BYTES:
        raise GateAbort("camera expectations file exceeded byte limit")
    parsed = strict_json_object(
        payload, label="camera expectations", max_bytes=MAX_CAMERA_CONFIG_BYTES
    )
    if len(parsed) != 15:
        raise GateAbort("camera expectations did not contain 15 cameras")
    result: dict[str, float] = {}
    for name, value in parsed.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 128
            or any(ord(character) < 32 for character in name)
        ):
            raise GateAbort("camera expectation identifier was invalid")
        expected = finite_number(value, "camera expected FPS", positive=True)
        if expected > 60:
            raise GateAbort("camera expected FPS exceeded safety bound")
        result[name] = expected
    return result


def validate_frigate_stats(
    stats: dict[str, Any],
    expectations: dict[str, float],
    low_since: dict[str, float | None],
    now_monotonic: float,
    now_wall: float,
) -> dict[str, Any]:
    cameras = stats.get("cameras")
    if not isinstance(cameras, dict) or set(cameras) != set(expectations):
        raise GateAbort("Frigate camera set changed or is incomplete")
    ratios: list[float] = []
    skipped_values: list[float] = []
    low_durations: list[float] = []
    for name, expected in expectations.items():
        item = cameras.get(name)
        if not isinstance(item, dict):
            raise GateAbort("Frigate camera telemetry is malformed")
        process_fps = finite_number(item.get("process_fps"), "camera process FPS")
        skipped_fps = finite_number(item.get("skipped_fps"), "camera skipped FPS")
        if skipped_fps > 0.5:
            raise GateAbort("Frigate camera skipped FPS exceeded threshold")
        ratio = process_fps / expected
        ratios.append(ratio)
        skipped_values.append(skipped_fps)
        if ratio < 0.9:
            if low_since[name] is None:
                low_since[name] = now_monotonic
            duration = now_monotonic - float(low_since[name])
            low_durations.append(duration)
            if duration > 30.0:
                raise GateAbort("Frigate camera process FPS stayed low over 30 seconds")
        else:
            low_since[name] = None
    detectors = stats.get("detectors")
    if not isinstance(detectors, dict):
        raise GateAbort("Frigate detector telemetry unavailable")
    detector_speeds: list[float] = []
    for name in REQUIRED_DETECTORS:
        item = detectors.get(name)
        if not isinstance(item, dict):
            raise GateAbort("required Frigate detector missing")
        detector_speeds.append(
            finite_number(
                item.get("inference_speed"), "detector inference speed", positive=True
            )
        )
        started = finite_number(item.get("detection_start"), "detector start")
        if started < 0 or started > now_wall + 5:
            raise GateAbort("Frigate detector start telemetry invalid")
        if started and now_wall - started > 30.0:
            raise GateAbort("Frigate detector stalled over 30 seconds")
    embeddings = stats.get("embeddings")
    if not isinstance(embeddings, dict):
        raise GateAbort("Frigate embedding telemetry unavailable")
    embedding_speeds = [
        finite_number(embeddings.get(key), "embedding speed", positive=True)
        for key in REQUIRED_EMBEDDING_SPEEDS
    ]
    conditional_idle_count = 0
    for speed_key, activity_key in CONDITIONAL_EMBEDDING_SPEEDS:
        speed_present = speed_key in embeddings
        activity_present = activity_key in embeddings
        if speed_present != activity_present:
            raise GateAbort("conditional embedding telemetry was incomplete")
        if not speed_present:
            # Frigate 0.17.x omits both YOLOv9 LPR fields whenever its rolling
            # plate-detection throughput is zero.  That is an idle state, not
            # an embedding worker failure.
            conditional_idle_count += 1
            continue
        embedding_speeds.append(
            finite_number(embeddings.get(speed_key), "embedding speed", positive=True)
        )
        finite_number(embeddings.get(activity_key), "embedding activity", positive=True)
    service = stats.get("service")
    if not isinstance(service, dict):
        raise GateAbort("Frigate service telemetry unavailable")
    updated = finite_number(service.get("last_updated"), "Frigate last updated")
    if updated > now_wall + 5 or now_wall - updated > 30.0:
        raise GateAbort("Frigate stats stale")
    return {
        "camera_count": len(expectations),
        "camera_low_count": len(low_durations),
        "camera_min_process_ratio": min(ratios),
        "camera_max_skipped_fps": max(skipped_values),
        "camera_longest_low_seconds": max(low_durations, default=0.0),
        "detector_count": len(detector_speeds),
        "detector_inference_ms_min": min(detector_speeds),
        "detector_inference_ms_max": max(detector_speeds),
        "embedding_metric_count": len(embedding_speeds),
        "embedding_conditional_idle_count": conditional_idle_count,
        "embedding_speed_min": min(embedding_speeds),
        "embedding_speed_max": max(embedding_speeds),
        "service_age_seconds": now_wall - updated,
    }


def validate_candidate_status(
    payload: dict[str, Any],
    *,
    expected_model: str,
    expected_reserve_bytes: int,
    observed_gpu_total_bytes: int | None = None,
    expected_priority_state: str | None = None,
    expected_policy_sha256: str | None = None,
    expected_controller_phase: str = "normal",
    expected_recovery_reason: str | None = None,
    expected_admission_open: bool = True,
    expected_model_resident: bool | None = None,
    require_gate_runtime: bool = True,
) -> dict[str, Any]:
    resource = payload.get("resource_management")
    if not isinstance(resource, dict):
        raise CandidateNotReady("candidate resource status not initialized")
    if resource.get("selected_model") is None:
        raise CandidateNotReady("candidate model selection not initialized")
    if set(resource) != RESOURCE_STATUS_KEYS:
        raise GateAbort("candidate resource status was incomplete or extended")
    expected = {
        "selected_model": expected_model,
        "requested_model": "auto",
        "model_explicit": False,
        "envelope_disposition": "exact_match",
        "decision_provenance": "envelope",
        "gpu_reserve_bytes": expected_reserve_bytes,
        "controller_state": expected_controller_phase,
        "recovery_reason": expected_recovery_reason,
        "admission_open": expected_admission_open,
        "envelope_reason": None,
        "capacity_source": "cgroup_v2",
        "automatic_ceiling": expected_model,
        "decision_reason": "selected",
    }
    if any(resource.get(key) != value for key, value in expected.items()):
        raise GateAbort("candidate resource status left exact healthy envelope")
    envelope_key = resource.get("envelope_key")
    if (
        not isinstance(envelope_key, dict)
        or set(envelope_key) != {"catalog_payload_sha256", "entry_index"}
        or not isinstance(envelope_key.get("catalog_payload_sha256"), str)
        or not CONFIG_DIGEST_RE.fullmatch(envelope_key["catalog_payload_sha256"])
        or isinstance(envelope_key.get("entry_index"), bool)
        or not isinstance(envelope_key.get("entry_index"), int)
        or not 0 <= envelope_key["entry_index"] <= 4096
    ):
        raise GateAbort("candidate exact envelope key was invalid")
    numeric_names = (
        "gpu_total_bytes",
        "gpu_stabilized_free_bytes",
        "gpu_reserve_bytes",
        "gpu_allocatable_bytes",
    )
    numeric: dict[str, int] = {}
    for name in numeric_names:
        value = resource.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise GateAbort("candidate GPU admission telemetry was invalid")
        numeric[name] = value
    total = numeric["gpu_total_bytes"]
    stabilized = numeric["gpu_stabilized_free_bytes"]
    reserve = numeric["gpu_reserve_bytes"]
    allocatable = numeric["gpu_allocatable_bytes"]
    if (
        stabilized > total
        or reserve >= stabilized
        or allocatable != stabilized - reserve
        or allocatable > total - reserve
    ):
        raise GateAbort("candidate GPU admission telemetry was inconsistent")
    if observed_gpu_total_bytes is not None and total != observed_gpu_total_bytes:
        raise GateAbort("candidate GPU total disagreed with NVIDIA telemetry")
    priority = resource.get("priority_pressure")
    workload = resource.get("workload")
    runtime_identity = resource.get("runtime_identity")
    failure_counters = resource.get("failure_counters")
    if (
        not isinstance(priority, dict)
        or set(priority) != PRIORITY_STATUS_KEYS
        or not isinstance(workload, dict)
        or set(workload) != WORKLOAD_STATUS_KEYS
        or not isinstance(runtime_identity, dict)
        or set(runtime_identity) != RUNTIME_IDENTITY_KEYS
        or not isinstance(failure_counters, dict)
        or set(failure_counters) != FAILURE_COUNTER_KEYS
    ):
        raise GateAbort("candidate atomic gate status schema was incomplete")

    configured = priority.get("configured")
    state = priority.get("state")
    controller_phase = priority.get("controller_phase")
    recovery_reason = priority.get("recovery_reason")
    model_resident = priority.get("model_resident")
    if (
        configured is not True
        or state not in {"clear", "neutral", "asserted", "unavailable"}
        or controller_phase not in {"normal", "yielding", "recovering"}
        or recovery_reason
        not in {
            None,
            "priority_pressure",
            "resource_pressure",
            "model_admission",
        }
        or not isinstance(model_resident, bool)
        or controller_phase != resource["controller_state"]
        or recovery_reason != resource["recovery_reason"]
    ):
        raise GateAbort("candidate priority controller status was inconsistent")
    if expected_priority_state is not None and state != expected_priority_state:
        raise GateAbort("candidate priority state did not match gate phase")
    if controller_phase != expected_controller_phase:
        raise GateAbort("candidate controller phase did not match gate phase")
    if recovery_reason != expected_recovery_reason:
        raise GateAbort("candidate recovery reason did not match gate phase")
    if expected_model_resident is not None and model_resident is not (
        expected_model_resident
    ):
        raise GateAbort("candidate model residency did not match gate phase")

    for name, maximum in (("heartbeat_age_ms", 10_000), ("source_age_ms", 30_000)):
        value = priority.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= maximum
        ):
            raise GateAbort("candidate priority freshness was invalid")
    for name in (
        "policy_sha256",
        "observation_digest",
        "transition_observation_digest",
    ):
        value = priority.get(name)
        if not isinstance(value, str) or not LOWER_SHA256_RE.fullmatch(value):
            raise GateAbort("candidate priority digest was invalid")
    if (
        expected_policy_sha256 is not None
        and priority["policy_sha256"] != expected_policy_sha256
    ):
        raise GateAbort("candidate priority policy digest did not match")
    for name, maximum in (
        ("transition_sequence", 2**63 - 1),
        ("model_load_generation", 2**63 - 1),
        ("model_unload_generation", 2**63 - 1),
        ("distinct_clear_count", 3),
    ):
        value = priority.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= maximum
        ):
            raise GateAbort("candidate priority generation was invalid")
    if state in {"asserted", "neutral", "unavailable"} and (
        priority["distinct_clear_count"] != 0
    ):
        raise GateAbort("candidate priority recovery count was inconsistent")
    if (
        state == "clear"
        and controller_phase == "normal"
        and priority["distinct_clear_count"] != 3
    ):
        raise GateAbort("candidate clear status lacked completed recovery")

    active = workload.get("active")
    uncommitted = workload.get("chunk_uncommitted")
    completion_generation = workload.get("completion_generation")
    if (
        not isinstance(active, bool)
        or not isinstance(uncommitted, bool)
        or (uncommitted and not active)
        or isinstance(completion_generation, bool)
        or not isinstance(completion_generation, int)
        or not 0 <= completion_generation <= 2**63 - 1
    ):
        raise GateAbort("candidate workload status was invalid")
    epoch = runtime_identity.get("epoch")
    started = runtime_identity.get("started_monotonic_ns")
    if (
        not isinstance(epoch, str)
        or not EPOCH_RE.fullmatch(epoch)
        or isinstance(started, bool)
        or not isinstance(started, int)
        or not 1 <= started <= 2**63 - 1
    ):
        raise GateAbort("candidate runtime identity was invalid")
    for value in failure_counters.values():
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 2**63 - 1
        ):
            raise GateAbort("candidate failure generation was invalid")
    if require_gate_runtime and not active:
        raise CandidateNotReady("candidate gate workload was not active")
    return {key: resource[key] for key in sorted(RESOURCE_STATUS_KEYS)}


def _profiler_output_source(args: argparse.Namespace) -> str:
    expectation = getattr(args, "boundary_expectation", None)
    if not isinstance(expectation, BoundaryExpectation):
        raise GateAbort("profiler boundary expectation was unavailable")
    mounts = expectation.document.get("mounts")
    if not isinstance(mounts, list):
        raise GateAbort("profiler output mount was unavailable")
    matching = [
        mount
        for mount in mounts
        if isinstance(mount, dict)
        and mount.get("destination") == "/profile/output"
        and mount.get("read_write") is True
    ]
    if len(matching) != 1 or not isinstance(matching[0].get("source"), str):
        raise GateAbort("profiler output mount was not exact")
    source = matching[0]["source"]
    _validate_disposable_source(source, args.disposable_root)
    return source


def _open_profiler_output_directory(source: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        directory_fd = os.open(source, flags)
    except OSError as exc:
        raise GateAbort("profiler output directory was unavailable") from exc
    directory = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != 1000
        or stat.S_IMODE(directory.st_mode) != 0o700
    ):
        os.close(directory_fd)
        raise GateAbort("profiler output directory was not private")
    return directory_fd


def _read_private_profiler_artifact(
    directory_fd: int, name: str, *, label: str
) -> bytes:
    if not name or posixpath.basename(name) != name:
        raise GateAbort(f"{label} artifact name was invalid")
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise GateAbort(f"{label} artifact was unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != 1000
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
        or before.st_size < 1
        or before.st_size > MAX_PROFILER_RESULT_BYTES
    ):
        raise GateAbort(f"{label} artifact was not private and bounded")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise GateAbort(f"{label} artifact could not be opened") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ) or opened.st_size != before.st_size:
            raise GateAbort(f"{label} artifact changed before read")
        payload = _read_fd_from_start(fd, before.st_size)
        after = os.fstat(fd)
        if (
            len(payload) != before.st_size
            or after.st_size != before.st_size
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or after.st_uid != 1000
            or stat.S_IMODE(after.st_mode) != 0o600
            or after.st_nlink != 1
        ):
            raise GateAbort(f"{label} artifact changed during read")
    finally:
        os.close(fd)
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise GateAbort(f"{label} artifact changed after read") from exc
    if (current.st_dev, current.st_ino) != (
        before.st_dev,
        before.st_ino,
    ) or current.st_size != before.st_size:
        raise GateAbort(f"{label} artifact changed after read")
    return payload


def _profiler_artifact_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise GateAbort("profiler catalog state was unavailable") from exc
    return True


def _validate_profiler_catalog(
    payload: bytes,
    *,
    expected_model: str,
    expected_version: int,
    expected_chunk_minutes: int,
) -> dict[str, Any]:
    catalog = strict_json_object(
        payload, label="profiler catalog", max_bytes=MAX_PROFILER_RESULT_BYTES
    )
    if set(catalog) != {"schema", "catalog_version", "entries", "integrity"}:
        raise GateAbort("profiler catalog top level was not exact")
    version = catalog.get("catalog_version")
    entries = catalog.get("entries")
    integrity = catalog.get("integrity")
    if (
        catalog.get("schema") != "subgen.model-envelope.catalog/v1"
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version != expected_version
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= 4096
        or not isinstance(integrity, dict)
        or set(integrity) != {"algorithm", "canonical_payload_sha256"}
        or integrity.get("algorithm") != "sha256"
        or not isinstance(integrity.get("canonical_payload_sha256"), str)
        or not CONFIG_DIGEST_RE.fullmatch(integrity["canonical_payload_sha256"])
    ):
        raise GateAbort("profiler catalog metadata was invalid")
    try:
        canonical_payload = json.dumps(
            {
                "schema": catalog["schema"],
                "catalog_version": version,
                "entries": entries,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GateAbort("profiler catalog payload was not canonicalizable") from exc
    expected_digest = "sha256:" + sha256_bytes(canonical_payload)
    if integrity["canonical_payload_sha256"] != expected_digest:
        raise GateAbort("profiler catalog integrity did not match")
    model_entries = 0
    matching_policy_entries = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise GateAbort("profiler catalog entry was malformed")
        policy = entry.get("policy")
        if isinstance(policy, dict) and policy.get("model") == expected_model:
            model_entries += 1
            chunk_minutes = policy.get("chunk_minutes")
            if (
                not isinstance(chunk_minutes, bool)
                and isinstance(chunk_minutes, int)
                and chunk_minutes == expected_chunk_minutes
            ):
                matching_policy_entries += 1
    if model_entries != 1 or matching_policy_entries != 1:
        raise GateAbort("profiler catalog model and chunk policy were not exact")
    return {
        "catalog_version": version,
        "entry_count": len(entries),
        "matching_model_entry_count": model_entries,
        "catalog_sha256": sha256_bytes(payload),
        "canonical_payload_sha256": expected_digest,
    }


def validate_profiler_completion(args: argparse.Namespace) -> dict[str, Any]:
    """Validate the held profiler child result without trusting its exit alone."""
    source = _profiler_output_source(args)
    directory_fd = _open_profiler_output_directory(source)
    try:
        receipt_payload = _read_private_profiler_artifact(
            directory_fd,
            posixpath.basename(PROFILER_RESULT_PATH),
            label="profiler receipt",
        )
        stdout_payload = _read_private_profiler_artifact(
            directory_fd,
            posixpath.basename(PROFILER_STDOUT_PATH),
            label="profiler stdout",
        )
        receipt = strict_json_object(
            receipt_payload,
            label="profiler receipt",
            max_bytes=MAX_PROFILER_RESULT_BYTES,
        )
        if (
            set(receipt) != {"schema", "returncode", "stdout_bytes", "stdout_sha256"}
            or receipt.get("schema") != 1
            or isinstance(receipt.get("returncode"), bool)
            or receipt.get("returncode") != args.expected_profiler_returncode
            or isinstance(receipt.get("stdout_bytes"), bool)
            or receipt.get("stdout_bytes") != len(stdout_payload)
            or not isinstance(receipt.get("stdout_sha256"), str)
            or receipt.get("stdout_sha256") != sha256_bytes(stdout_payload)
        ):
            raise GateAbort("profiler receipt did not bind the expected child result")
        result = strict_json_object(
            stdout_payload,
            label="profiler stdout",
            max_bytes=MAX_PROFILER_RESULT_BYTES,
        )
        result_keys = {
            "status",
            "model",
            "catalog_version",
            "next_model",
            "reason",
            "replaced_existing",
        }
        if set(result) != result_keys or result.get("model") != args.expected_model:
            raise GateAbort("profiler result was not exact")
        catalog_name = posixpath.basename(PROFILER_CATALOG_PATH)
        if args.expected_profiler_returncode == 3:
            try:
                index = MODEL_DESCENT.index(args.expected_model)
            except ValueError as exc:
                raise GateAbort("profiler safe failure model was invalid") from exc
            next_model = (
                MODEL_DESCENT[index + 1] if index + 1 < len(MODEL_DESCENT) else None
            )
            reason = result.get("reason")
            if (
                result.get("status") != "safe_failure"
                or result.get("catalog_version") is not None
                or result.get("next_model") != next_model
                or not isinstance(reason, str)
                or not reason
                or len(reason) > 256
                or result.get("replaced_existing") is not False
                or _profiler_artifact_exists(directory_fd, catalog_name)
            ):
                raise GateAbort("profiler safe failure result was invalid")
            return {
                "status": "safe_failure",
                "model": args.expected_model,
                "next_model": next_model,
                "returncode": 3,
                "reason_sha256": sha256_bytes(reason.encode("utf-8")),
                "stdout_sha256": sha256_bytes(stdout_payload),
                "receipt_sha256": sha256_bytes(receipt_payload),
            }
        version = result.get("catalog_version")
        if (
            result.get("status") != "profiled"
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version <= 0
            or result.get("next_model") is not None
            or result.get("reason") is not None
            or not isinstance(result.get("replaced_existing"), bool)
        ):
            raise GateAbort("profiler success result was invalid")
        catalog_payload = _read_private_profiler_artifact(
            directory_fd, catalog_name, label="profiler catalog"
        )
        catalog = _validate_profiler_catalog(
            catalog_payload,
            expected_model=args.expected_model,
            expected_version=version,
            expected_chunk_minutes=args.expected_chunk_minutes,
        )
        return {
            "status": "profiled",
            "model": args.expected_model,
            "returncode": 0,
            "replaced_existing": result["replaced_existing"],
            "stdout_sha256": sha256_bytes(stdout_payload),
            "receipt_sha256": sha256_bytes(receipt_payload),
            **catalog,
        }
    finally:
        os.close(directory_fd)


class IncrementalLogScanner:
    def __init__(
        self,
        client: DockerClient,
        candidate: CandidateBinding,
        frigate: ObservedBinding,
        started_wall: float,
    ) -> None:
        self.client = client
        self.candidate = candidate
        self.frigate = frigate
        self.cursor_wall = started_wall

    def scan(self, until_wall: float) -> None:
        since = max(0.0, self.cursor_wall - LOG_OVERLAP_SECONDS)
        if until_wall < since:
            raise GateAbort("log sampling clock moved backwards")
        candidate_logs = self.client.command(
            "logs",
            "--since",
            f"{since:.6f}",
            "--until",
            f"{until_wall:.6f}",
            self.candidate.container_id,
            label="candidate incremental logs",
            timeout=10,
            max_bytes=MAX_LOG_BYTES,
        ).output
        if CANDIDATE_FAILURE_RE.search(candidate_logs):
            raise GateAbort("candidate failure signature appeared")
        frigate_logs = self.client.command(
            "logs",
            "--since",
            f"{since:.6f}",
            "--until",
            f"{until_wall:.6f}",
            self.frigate.container_id,
            label="Frigate incremental logs",
            timeout=10,
            max_bytes=MAX_LOG_BYTES,
        ).output
        if FRIGATE_FAILURE_RE.search(frigate_logs):
            raise GateAbort("Frigate detector or embedding failure appeared")
        kernel_logs = bounded_command(
            [
                "journalctl",
                "-k",
                "--since",
                f"@{since:.6f}",
                "--until",
                f"@{until_wall:.6f}",
                "--no-pager",
                "--output=cat",
            ],
            label="kernel incremental logs",
            timeout=10,
            max_bytes=MAX_LOG_BYTES,
        ).output
        if KERNEL_FAILURE_RE.search(kernel_logs):
            raise GateAbort("kernel Xid or OOM signature appeared")
        self.cursor_wall = until_wall


def _write_all_fd(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise GateAbort("evidence seal write made no progress")
        written += count


def _read_fd_from_start(fd: int, expected_size: int) -> bytes:
    if expected_size < 0 or expected_size > MAX_JSON_BYTES:
        raise GateAbort("evidence seal size was invalid")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = expected_size + 1
    while remaining:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class EvidenceWriter:
    """Owner-only JSONL partial writer with atomic finalization and seal."""

    def __init__(
        self,
        final_path: Path,
        partial_name: str,
        handle: TextIO,
        parent_fd: int,
        identity: tuple[int, int],
    ) -> None:
        self.final_path = final_path
        self.partial_name = partial_name
        self.handle = handle
        self.parent_fd = parent_fd
        self.identity = identity
        self.records = 0
        self.finalized = False

    @classmethod
    def open(cls, path: Path, token_digest: str) -> "EvidenceWriter":
        if not path.is_absolute():
            raise GateAbort("evidence output path must be absolute")
        parent = path.parent.resolve(strict=True)
        if parent != path.parent.absolute():
            raise GateAbort("evidence output parent used a symlink")
        parent_stat = parent.stat()
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
            or parent_stat.st_mode & 0o077
        ):
            raise GateAbort("evidence output parent is not owner only")
        dir_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            parent_fd = os.open(parent, dir_flags)
        except OSError as exc:
            raise GateAbort("evidence parent could not be opened") from exc
        partial_name = f".{path.name}.{token_digest[:16]}.partial"
        file_flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            try:
                os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise GateAbort("evidence final path already exists")
            fd = os.open(partial_name, file_flags, 0o600, dir_fd=parent_fd)
            file_stat = os.fstat(fd)
            os.fsync(parent_fd)
            handle = os.fdopen(fd, "w+", encoding="utf-8", newline="\n")
        except BaseException:
            os.close(parent_fd)
            raise
        return cls(
            path,
            partial_name,
            handle,
            parent_fd,
            (file_stat.st_dev, file_stat.st_ino),
        )

    @property
    def closed(self) -> bool:
        return self.handle.closed

    def _verify_identity(self) -> None:
        file_stat = os.fstat(self.handle.fileno())
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) != 0o600
            or (file_stat.st_dev, file_stat.st_ino) != self.identity
        ):
            raise GateAbort("evidence file identity or mode changed")

    def write(self, record: dict[str, Any]) -> None:
        if self.finalized or self.closed:
            raise GateAbort("evidence output already finalized")
        self._verify_identity()
        self.handle.write(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        )
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.records += 1

    def _current_digest_and_size(self) -> tuple[str, int]:
        self._verify_identity()
        self.handle.flush()
        os.fsync(self.handle.fileno())
        fd = self.handle.fileno()
        position = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        os.lseek(fd, position, os.SEEK_SET)
        return digest.hexdigest(), size

    def seal(
        self,
        *,
        outcome: str,
        sampler_sha256: str,
        image_config: str,
        cleanup: dict[str, Any],
    ) -> str:
        prefix_digest, _ = self._current_digest_and_size()
        self.write(
            {
                "event": "evidence_seal_record",
                "timestamp": utc_now(),
                "outcome": outcome,
                "prefix_sha256": prefix_digest,
                "records_before_seal": self.records,
            }
        )
        full_digest, byte_count = self._current_digest_and_size()
        record_count = self.records
        self.handle.close()
        seal_name = self.final_path.name + ".seal.json"
        seal_flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        seal_payload = (
            json.dumps(
                {
                    "schema": 1,
                    "outcome": outcome,
                    "evidence_sha256": full_digest,
                    "evidence_bytes": byte_count,
                    "record_count": record_count,
                    "sampler_sha256": sampler_sha256,
                    "candidate_image_config": image_config,
                    "cleanup": cleanup,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        seal_fd = os.open(seal_name, seal_flags, 0o600, dir_fd=self.parent_fd)
        try:
            _write_all_fd(seal_fd, seal_payload)
            os.fsync(seal_fd)
            seal_stat = os.fstat(seal_fd)
            if (
                not stat.S_ISREG(seal_stat.st_mode)
                or seal_stat.st_uid != os.geteuid()
                or stat.S_IMODE(seal_stat.st_mode) != 0o600
                or seal_stat.st_size != len(seal_payload)
                or _read_fd_from_start(seal_fd, len(seal_payload)) != seal_payload
            ):
                raise GateAbort("evidence seal verification failed")
        finally:
            os.close(seal_fd)
        # The seal name is durably published and verified before the evidence
        # gets its final name. A crash can leave an unusable orphan seal, but a
        # consumer can never observe a final-named pass JSONL without its
        # already-durable hash/size binding.
        os.fsync(self.parent_fd)
        # A hard-link finalization is create-only and atomic: unlike rename,
        # it cannot overwrite a file or symlink introduced after open.
        os.link(
            self.partial_name,
            self.final_path.name,
            src_dir_fd=self.parent_fd,
            dst_dir_fd=self.parent_fd,
            follow_symlinks=False,
        )
        os.fsync(self.parent_fd)
        final_flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        final_fd = os.open(self.final_path.name, final_flags, dir_fd=self.parent_fd)
        try:
            final_stat = os.fstat(final_fd)
            final_payload = _read_fd_from_start(final_fd, byte_count)
            if (
                not stat.S_ISREG(final_stat.st_mode)
                or final_stat.st_uid != os.geteuid()
                or stat.S_IMODE(final_stat.st_mode) != 0o600
                or (final_stat.st_dev, final_stat.st_ino) != self.identity
                or final_stat.st_size != byte_count
                or len(final_payload) != byte_count
                or sha256_bytes(final_payload) != full_digest
            ):
                raise GateAbort("final evidence verification failed")
        finally:
            os.close(final_fd)
        os.unlink(self.partial_name, dir_fd=self.parent_fd)
        os.fsync(self.parent_fd)
        os.close(self.parent_fd)
        self.parent_fd = -1
        self.finalized = True
        return full_digest

    def close(self) -> None:
        if not self.handle.closed:
            try:
                self.handle.flush()
                os.fsync(self.handle.fileno())
            finally:
                self.handle.close()
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1


def start_bound_candidate(
    client: DockerClient, binding: CandidateBinding, args: argparse.Namespace
) -> None:
    state = candidate_state(client, binding, args)
    if state["status"] != "created" or state["running"] is not False:
        raise GateAbort("candidate was not stopped immediately before start")
    result = client.command(
        "start",
        binding.container_id,
        label="candidate immutable start",
        timeout=20,
        allow_failure=True,
    )
    if result.returncode != 0:
        raise GateAbort("candidate immutable start failed")


def wait_for_running(
    client: DockerClient,
    binding: CandidateBinding,
    args: argparse.Namespace,
    deadline: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        state = candidate_state(client, binding, args)
        if state["running"] is True and state["status"] == "running":
            if state["restart_count"] != 0 or state["oom_killed"] is not False:
                raise GateAbort("candidate did not start from fresh state")
            return state
        if state["status"] in {"dead", "removing", "exited"}:
            raise GateAbort("candidate entered terminal state during start")
        time.sleep(1)
    raise GateAbort("candidate did not start within boundary")


def wait_for_runtime_ready(args: argparse.Namespace, deadline: float) -> dict[str, Any]:
    while time.monotonic() < deadline:
        try:
            return validate_candidate_status(
                fetch_json(args.candidate_status_url, endpoint="candidate"),
                expected_model=args.expected_model,
                expected_reserve_bytes=args.gpu_free_floor_bytes,
            )
        except (TelemetryUnavailable, CandidateNotReady):
            time.sleep(1)
    raise GateAbort("candidate runtime did not reach exact healthy envelope")


def run_sampling_loop(
    *,
    duration_seconds: float,
    interval_seconds: float,
    sample: Callable[[int, float], bool],
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[int, float]:
    """Sample at real cadence and finish only after a fresh final sample."""
    started = clock()
    scheduled = started
    sample_number = 0
    while True:
        now = clock()
        if now < scheduled:
            sleeper(scheduled - now)
            now = clock()
        if now - scheduled > MAX_SAMPLE_LAG_SECONDS:
            raise GateAbort("health sampler cadence lag exceeded")
        elapsed = now - started
        sample_number += 1
        status_fresh = sample(sample_number, elapsed)
        if elapsed >= duration_seconds and status_fresh:
            return sample_number, elapsed
        scheduled += interval_seconds
        after = clock()
        # Never catch up with an immediate burst after slow telemetry. A full
        # sample that consumes its next five-second boundary invalidates the
        # continuous gate and must be rerun.
        if after > scheduled:
            raise GateAbort("health sample work exceeded cadence")


def observe_gate(
    args: argparse.Namespace,
    evidence: EvidenceWriter,
    client: DockerClient,
    candidate_binding: CandidateBinding,
    frigate_binding: ObservedBinding,
    camera_expectations: dict[str, float],
    daemon_digest: str,
    boot_digest: str,
    sampler_sha256: str,
    logs: IncrementalLogScanner,
) -> ObservationOutcome:
    start_bound_candidate(client, candidate_binding, args)
    candidate_initial = wait_for_running(
        client,
        candidate_binding,
        args,
        time.monotonic() + args.start_timeout_seconds,
    )
    frigate_initial = observed_state(client, frigate_binding)
    if frigate_initial["running"] is not True or frigate_initial["health"] != "healthy":
        raise GateAbort("Frigate not healthy at gate start")
    if args.candidate_mode == "runtime":
        initial_resource = wait_for_runtime_ready(
            args, time.monotonic() + args.start_timeout_seconds
        )
    else:
        initial_resource = {
            "mode": "profiler",
            "external_result_validation_required": True,
        }
    baseline_memory = candidate_memory(client, candidate_binding)
    validate_candidate_memory_snapshot(
        baseline_memory, expected_memory_bytes=args.expected_memory_bytes
    )
    baseline_restart_candidate = candidate_initial["restart_count"]
    baseline_restart_frigate = frigate_initial["restart_count"]
    low_since = {name: None for name in camera_expectations}
    evidence.write(
        {
            "event": "gate_start",
            "timestamp": utc_now(),
            "candidate_mode": args.candidate_mode,
            "candidate_container_id_sha256": sha256_bytes(
                candidate_binding.container_id.encode("ascii")
            ),
            "candidate_image_config": candidate_binding.image_config,
            "candidate_command_sha256": candidate_binding.command_digest,
            "candidate_boundary_sha256": candidate_binding.boundary_digest,
            "boundary_manifest_sha256": args.boundary_expectation.file_sha256,
            "runtime_commit": candidate_binding.runtime_commit,
            "gate_role": candidate_binding.gate_role,
            "gate_token_sha256": candidate_binding.gate_token_digest,
            "docker_daemon_id_sha256": daemon_digest,
            "host_boot_id_sha256": boot_digest,
            "sampler_sha256": sampler_sha256,
            "duration_seconds": args.duration_seconds,
            "interval_seconds": args.interval_seconds,
            "expected_memory_bytes": args.expected_memory_bytes,
            "gpu_free_floor_bytes": args.gpu_free_floor_bytes,
            "host_reserve_bytes": args.host_reserve_bytes,
            "camera_count": len(camera_expectations),
            "candidate_initial_state": candidate_initial,
            "candidate_initial_resource": initial_resource,
            "frigate_container_id_sha256": sha256_bytes(
                frigate_binding.container_id.encode("ascii")
            ),
            "frigate_image_config": frigate_binding.image_config,
            "frigate_initial_state": frigate_initial,
            "psi_policy": "parsed_observation_only",
        }
    )

    def validate_state_and_memory() -> tuple[
        dict[str, Any], dict[str, Any], dict[str, Any]
    ]:
        verify_bound_docker_daemon(client, args)
        candidate = candidate_state(client, candidate_binding, args)
        frigate = observed_state(client, frigate_binding)
        if candidate["running"] is not True or candidate["status"] != "running":
            raise GateAbort("candidate stopped before observation ended")
        if candidate["oom_killed"] is not False:
            raise GateAbort("candidate OOM killed")
        if candidate["restart_count"] != baseline_restart_candidate:
            raise GateAbort("candidate restart count increased")
        if frigate["running"] is not True or frigate["health"] != "healthy":
            raise GateAbort("Frigate lost healthy state")
        if frigate["restart_count"] != baseline_restart_frigate:
            raise GateAbort("Frigate restart count increased")
        memory = candidate_memory(client, candidate_binding)
        validate_candidate_memory_snapshot(
            memory, expected_memory_bytes=args.expected_memory_bytes
        )
        return candidate, frigate, memory

    def sample(sample_number: int, elapsed: float) -> bool:
        now_monotonic = time.monotonic()
        now_wall = time.time()
        candidate, frigate, memory = validate_state_and_memory()
        host_available = read_mem_available_bytes()
        if host_available < args.host_reserve_bytes:
            raise GateAbort("host memory reserve breached")
        host_psi = read_host_pressure()
        gpu = gpu_telemetry()
        if gpu["free_mib"] * MIB < args.gpu_free_floor_bytes:
            raise GateAbort("GPU priority reserve breached")
        ollama = fetch_json(args.ollama_url, endpoint="ollama")
        models = ollama.get("models")
        if not isinstance(models, list) or models:
            raise GateAbort("Ollama model became loaded")
        stats = fetch_json(args.frigate_stats_url, endpoint="frigate")
        frigate_metrics = validate_frigate_stats(
            stats, camera_expectations, low_since, now_monotonic, now_wall
        )
        status_fresh = True
        candidate_resource: dict[str, Any]
        if args.candidate_mode == "runtime":
            # After readiness, every five-second sample must contain a fresh,
            # exact status. Any blind interval restarts the gate rather than
            # being counted toward 900 seconds.
            candidate_resource = validate_candidate_status(
                fetch_json(args.candidate_status_url, endpoint="candidate"),
                expected_model=args.expected_model,
                expected_reserve_bytes=args.gpu_free_floor_bytes,
                observed_gpu_total_bytes=gpu["total_mib"] * MIB,
            )
        else:
            candidate_resource = {
                "mode": "profiler",
                "external_result_validation_required": True,
            }
        # Capture the log end after every other observation so a failure racing
        # the state/memory/HTTP checks is still inside this complete window.
        log_end_wall = time.time()
        logs.scan(log_end_wall)
        evidence.write(
            {
                "event": "sample",
                "timestamp": utc_now(),
                "sample": sample_number,
                "elapsed_seconds": round(elapsed, 3),
                "candidate": candidate,
                "candidate_memory": memory,
                "candidate_resource": candidate_resource,
                "candidate_status_fresh": status_fresh,
                "frigate": frigate,
                "frigate_metrics": frigate_metrics,
                "gpu": gpu,
                "host_mem_available_bytes": host_available,
                "host_memory_psi_observed_only": host_psi,
                "ollama_loaded_models": 0,
            }
        )
        return status_fresh

    sample_count, observed_seconds = run_sampling_loop(
        duration_seconds=args.duration_seconds,
        interval_seconds=args.interval_seconds,
        sample=sample,
    )
    # The t=duration sample is followed by a separate fresh state/memory/status
    # check and a log drain through a timestamp captured only after those reads.
    # This closes the race between the final scheduled sample and gate success.
    final_candidate, final_frigate, final_memory = validate_state_and_memory()
    final_resource: dict[str, Any]
    final_gpu = gpu_telemetry()
    if final_gpu["free_mib"] * MIB < args.gpu_free_floor_bytes:
        raise GateAbort("GPU priority reserve breached at final drain")
    if args.candidate_mode == "runtime":
        final_resource = validate_candidate_status(
            fetch_json(args.candidate_status_url, endpoint="candidate"),
            expected_model=args.expected_model,
            expected_reserve_bytes=args.gpu_free_floor_bytes,
            observed_gpu_total_bytes=final_gpu["total_mib"] * MIB,
        )
    else:
        final_resource = validate_profiler_completion(args)
    final_log_wall = time.time()
    logs.scan(final_log_wall)
    evidence.write(
        {
            "event": "gate_observation_final",
            "timestamp": utc_now(),
            "candidate": final_candidate,
            "candidate_memory": final_memory,
            "candidate_resource": final_resource,
            "gpu": final_gpu,
            "frigate": final_frigate,
            "logs_drained_through_wall": round(final_log_wall, 6),
        }
    )
    return ObservationOutcome(
        sample_count=sample_count,
        observed_seconds=observed_seconds,
        final_log_wall=final_log_wall,
        candidate_restart_count=baseline_restart_candidate,
        frigate_restart_count=baseline_restart_frigate,
    )


def install_signal_handlers() -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def abort_on_signal(signum: int, _frame: Any) -> None:
        raise GateAbort(f"sampler received {signal.Signals(signum).name}")

    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    for signum in signals:
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, abort_on_signal)
    return previous


def restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def validate_stop_completion(
    args: argparse.Namespace,
    client: DockerClient,
    candidate_binding: CandidateBinding,
    frigate_binding: ObservedBinding,
    logs: IncrementalLogScanner,
    observation: ObservationOutcome,
) -> dict[str, Any]:
    """Prove stop completion and drain every log source past that proof."""
    verify_bound_docker_daemon(client, args)
    candidate = candidate_state(client, candidate_binding, args)
    frigate = observed_state(client, frigate_binding)
    if candidate["running"] is not False or candidate["status"] not in {
        "exited",
        "dead",
    }:
        raise GateAbort("candidate stop completion state was invalid")
    if (
        candidate["oom_killed"] is not False
        or candidate["restart_count"] != observation.candidate_restart_count
    ):
        raise GateAbort("candidate stop completion health changed")
    if (
        frigate["running"] is not True
        or frigate["health"] != "healthy"
        or frigate["restart_count"] != observation.frigate_restart_count
    ):
        raise GateAbort("Frigate changed during candidate stop")
    end_wall = time.time()
    logs.scan(end_wall)
    return {
        "candidate": candidate,
        "frigate": frigate,
        "logs_drained_through_wall": round(end_wall, 6),
    }


def ensure_boundary_expectation(args: argparse.Namespace) -> BoundaryExpectation:
    expectation = getattr(args, "boundary_expectation", None)
    if isinstance(expectation, BoundaryExpectation):
        return expectation
    expectation = load_boundary_expectation(
        args.boundary_manifest,
        args.boundary_manifest_sha256,
    )
    args.boundary_expectation = expectation
    return expectation


def verify_bound_docker_daemon(
    client: DockerClient, args: argparse.Namespace
) -> tuple[str, str]:
    """Re-observe the live daemon and match the identity sealed in the boundary."""
    expectation = ensure_boundary_expectation(args)
    expected = validate_docker_daemon_identity(
        expectation.document.get("docker_daemon_identity")
    )
    engine_digest, boot_digest = client.verify_local_daemon()
    observed = docker_daemon_identity_document(engine_digest, boot_digest)
    if observed != expected:
        raise GateAbort("Docker daemon identity changed from the sealed boundary")
    return engine_digest, boot_digest


def expected_binding_from_args(args: argparse.Namespace) -> CandidateBinding:
    expectation = ensure_boundary_expectation(args)
    return CandidateBinding(
        name=args.container,
        container_id=args.expected_container_id,
        image_config=args.expected_image_config,
        runtime_commit=args.runtime_commit,
        gate_role=args.gate_role,
        gate_token_digest=sha256_bytes(args.gate_token.encode("utf-8")),
        command_digest=args.expected_command_sha256.lower(),
        boundary_digest=expectation.canonical_sha256,
    )


def cleanup_only(args: argparse.Namespace) -> int:
    """ExecStopPost entry point that revalidates and stops only the bound ID."""
    if sha256_file(Path(__file__)) != args.sampler_sha256.lower():
        raise GateAbort("sampler checksum mismatch")
    ensure_boundary_expectation(args)
    client = DockerClient(args.expected_docker_daemon_id, args.expected_host_boot_id)
    verify_bound_docker_daemon(client, args)
    binding = expected_binding_from_args(args)
    item = client.inspect(binding.container_id, missing_ok=True)
    if item is None:
        print("TASK11B_CLEANUP_OK candidate_absent=true")
        return 0
    verify_candidate_item(
        item, binding, args, require_name=False, filesystem_check=False
    )
    if (
        args.systemd_stop_post
        and args.candidate_mode == "runtime"
        and args.leave_running_on_pass
        and os.environ.get("SERVICE_RESULT") == "success"
    ):
        state = _bounded_state(item)
        if (
            state["running"] is not True
            or state["status"] != "running"
            or state["restart_count"] != 0
            or state["oom_killed"] is not False
        ):
            raise GateAbort("passing runtime was not healthy at supervisor release")
        print(
            "TASK11B_CLEANUP_OK passing_runtime_retained=true "
            f"container_id_sha256={sha256_bytes(binding.container_id.encode('ascii'))}"
        )
        return 0
    outcome = stop_bound_candidate(client, binding, args)
    if outcome.get("verified_stopped") is not True:
        raise GateAbort("outer supervisor cleanup was not verified")
    print(
        "TASK11B_CLEANUP_OK verified_stopped=true "
        f"container_id_sha256={sha256_bytes(binding.container_id.encode('ascii'))}"
    )
    return 0


def _gate_cli_arguments(args: argparse.Namespace) -> list[str]:
    pairs: list[tuple[str, Any]] = [
        ("--duration-seconds", args.duration_seconds),
        ("--interval-seconds", args.interval_seconds),
        ("--start-timeout-seconds", args.start_timeout_seconds),
        ("--expected-memory-bytes", args.expected_memory_bytes),
        ("--gpu-free-floor-bytes", args.gpu_free_floor_bytes),
        ("--host-reserve-bytes", args.host_reserve_bytes),
        ("--frigate-container", args.frigate_container),
        ("--frigate-stats-url", args.frigate_stats_url),
        ("--ollama-url", args.ollama_url),
        ("--candidate-status-url", args.candidate_status_url),
        ("--candidate-mode", args.candidate_mode),
        ("--expected-model", args.expected_model),
        ("--expected-chunk-minutes", args.expected_chunk_minutes),
        ("--expected-container-id", args.expected_container_id),
        ("--expected-image-config", args.expected_image_config),
        ("--candidate-oci-index", args.candidate_oci_index),
        ("--candidate-config-digest", args.candidate_config_digest),
        ("--model-envelope-catalog-sha256", args.model_envelope_catalog_sha256),
        ("--phase-a-fixture-record-sha256", args.phase_a_fixture_record_sha256),
        ("--phase-b-fixture-record-sha256", args.phase_b_fixture_record_sha256),
        ("--model-revision", args.model_revision),
        ("--expected-command-sha256", args.expected_command_sha256),
        ("--runtime-commit", args.runtime_commit),
        ("--gate-token", args.gate_token),
        ("--gate-role", args.gate_role),
        ("--camera-expectations", args.camera_expectations),
        ("--sampler-sha256", args.sampler_sha256),
        ("--expected-docker-daemon-id", args.expected_docker_daemon_id),
        ("--expected-host-boot-id", args.expected_host_boot_id),
        ("--boundary-manifest", args.boundary_manifest),
        ("--boundary-manifest-sha256", args.boundary_manifest_sha256),
        ("--disposable-root", args.disposable_root),
    ]
    if args.expected_profiler_returncode is not None:
        pairs.append(
            ("--expected-profiler-returncode", args.expected_profiler_returncode)
        )
    result = [str(args.container), str(args.output)]
    for option, value in pairs:
        result.extend((option, str(value)))
    for diff_id in args.candidate_layer_diff_ids:
        result.extend(("--candidate-layer-diff-id", diff_id))
    if args.leave_running_on_pass:
        result.append("--leave-running-on-pass")
    return result


def _systemd_quote(argument: str) -> str:
    if "\x00" in argument or "\n" in argument or "\r" in argument:
        raise GateAbort("systemd supervisor argument was invalid")
    escaped = (
        argument.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def _write_private_create_only(path: Path, payload: bytes, mode: int) -> None:
    if not path.is_absolute():
        raise GateAbort("supervisor script path must be absolute")
    parent = path.parent.resolve(strict=True)
    if parent != path.parent.absolute():
        raise GateAbort("supervisor script parent used a symlink")
    parent_stat = parent.stat()
    if parent_stat.st_uid != os.geteuid() or parent_stat.st_mode & 0o077:
        raise GateAbort("supervisor script parent is not owner only")
    parent_fd = os.open(
        parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(path.name, flags, mode, dir_fd=parent_fd)
        try:
            _write_all_fd(fd, payload)
            os.fsync(fd)
            file_stat = os.fstat(fd)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.geteuid()
                or stat.S_IMODE(file_stat.st_mode) != mode
                or file_stat.st_size != len(payload)
                or _read_fd_from_start(fd, len(payload)) != payload
            ):
                raise GateAbort("supervisor script verification failed")
        finally:
            os.close(fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def emit_boundary_manifest(args: argparse.Namespace) -> int:
    """Create the reviewable exact mount/execution allowlist before the gate."""
    if sha256_file(Path(__file__)) != args.sampler_sha256.lower():
        raise GateAbort("sampler checksum mismatch")
    client = DockerClient(args.expected_docker_daemon_id, args.expected_host_boot_id)
    engine_digest, boot_digest = client.verify_local_daemon()
    args._observed_docker_daemon_identity = docker_daemon_identity_document(
        engine_digest, boot_digest
    )
    item = client.inspect(args.expected_container_id)
    assert item is not None
    if (
        item.get("Id") != args.expected_container_id
        or item.get("Name") != f"/{args.container}"
        or item.get("Image") != args.expected_image_config
        or _command_digest(item) != args.expected_command_sha256.lower()
    ):
        raise GateAbort("boundary source candidate identity did not match")
    labels = _container_labels(item)
    expected_labels = {
        GATE_LABEL: "true",
        TOKEN_LABEL: args.gate_token,
        ROLE_LABEL: args.gate_role,
        RUNTIME_LABEL: args.runtime_commit,
    }
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        raise GateAbort("boundary source candidate labels did not match")
    state = _bounded_state(item)
    if (
        state["status"] != "created"
        or state["running"] is not False
        or state["restart_count"] != 0
        or state["oom_killed"] is not False
    ):
        raise GateAbort("boundary source candidate was not fresh and stopped")
    boundary = _candidate_boundary_after_basic_validation(item, args)
    payload = _canonical_json_bytes(boundary) + b"\n"
    _write_private_create_only(args.emit_boundary_manifest, payload, 0o600)
    print(
        "TASK11B_BOUNDARY_READY "
        f"file_sha256={sha256_bytes(payload)} "
        f"boundary_sha256={execution_boundary_digest(boundary)}"
    )
    return 0


def self_test() -> None:
    assert CONTAINER_NAME_RE.fullmatch("subgen-task11b-profile-medium")
    assert not CONTAINER_NAME_RE.fullmatch("subgen")
    assert KERNEL_FAILURE_RE.search("NVRM: Xid (PCI:0000:01:00): 31")
    assert CANDIDATE_FAILURE_RE.search("MemoryError")
    assert FRIGATE_FAILURE_RE.search("detector worker error")
    events = "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"
    assert (
        set(parse_key_value_lines(events, "test", required_keys=REQUIRED_MEMORY_EVENTS))
        == REQUIRED_MEMORY_EVENTS
    )
    psi = parse_psi(
        "some avg10=0.00 avg60=0.01 avg300=0.02 total=10\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=1\n",
        "test PSI",
    )
    assert psi["some"]["total"] == 10

    class FakeClock:
        def __init__(self) -> None:
            self.value = 0.0

        def now(self) -> float:
            return self.value

        def sleep(self, delay: float) -> None:
            self.value += delay

    fake = FakeClock()
    seen: list[float] = []
    count, elapsed = run_sampling_loop(
        duration_seconds=10,
        interval_seconds=5,
        sample=lambda _number, value: not seen.append(value),
        clock=fake.now,
        sleeper=fake.sleep,
    )
    assert seen == [0.0, 5.0, 10.0]
    assert count == 3 and elapsed == 10.0
    print("TASK11B_HEALTH_SELF_TEST_OK")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("container", nargs="?")
    result.add_argument("output", nargs="?", type=Path)
    result.add_argument("--duration-seconds", type=int, default=900)
    result.add_argument("--interval-seconds", type=int, default=5)
    result.add_argument("--start-timeout-seconds", type=int, default=120)
    result.add_argument("--expected-memory-bytes", type=int)
    result.add_argument("--gpu-free-floor-bytes", type=int)
    result.add_argument("--host-reserve-bytes", type=int, default=4 * GIB)
    result.add_argument("--frigate-container", default="frigate")
    result.add_argument("--frigate-stats-url", default=EXACT_ENDPOINTS["frigate"])
    result.add_argument("--ollama-url", default=EXACT_ENDPOINTS["ollama"])
    result.add_argument("--candidate-status-url", default=EXACT_ENDPOINTS["candidate"])
    result.add_argument("--candidate-mode", choices=("runtime", "profiler"))
    result.add_argument("--expected-model")
    result.add_argument("--expected-chunk-minutes", type=int)
    result.add_argument("--expected-profiler-returncode", type=int)
    result.add_argument("--expected-container-id")
    result.add_argument("--expected-image-config")
    result.add_argument("--candidate-oci-index")
    result.add_argument("--candidate-config-digest")
    result.add_argument(
        "--candidate-layer-diff-id", dest="candidate_layer_diff_ids", action="append"
    )
    result.add_argument("--model-envelope-catalog-sha256")
    result.add_argument("--phase-a-fixture-record-sha256")
    result.add_argument("--phase-b-fixture-record-sha256")
    result.add_argument("--model-revision")
    result.add_argument("--expected-command-sha256")
    result.add_argument("--runtime-commit")
    result.add_argument("--gate-token")
    result.add_argument("--gate-role")
    result.add_argument("--camera-expectations", type=Path)
    result.add_argument("--sampler-sha256")
    result.add_argument("--expected-docker-daemon-id")
    result.add_argument("--expected-host-boot-id")
    result.add_argument("--boundary-manifest", type=Path)
    result.add_argument("--boundary-manifest-sha256")
    result.add_argument("--disposable-root")
    result.add_argument("--leave-running-on-pass", action="store_true")
    result.add_argument("--cleanup-only", action="store_true")
    result.add_argument("--systemd-stop-post", action="store_true")
    result.add_argument("--emit-boundary-manifest", type=Path)
    result.add_argument("--self-test", action="store_true")
    return result


def validate_args(args: argparse.Namespace) -> None:
    required = {
        "container": args.container,
        "output": args.output,
        "expected memory": args.expected_memory_bytes,
        "GPU priority reserve": args.gpu_free_floor_bytes,
        "candidate mode": args.candidate_mode,
        "expected chunk minutes": args.expected_chunk_minutes,
        "expected container ID": args.expected_container_id,
        "expected OCI index": args.expected_image_config,
        "candidate OCI index": args.candidate_oci_index,
        "candidate config digest": args.candidate_config_digest,
        "candidate layer diff IDs": args.candidate_layer_diff_ids,
        "model-envelope catalog checksum": args.model_envelope_catalog_sha256,
        "Phase-A fixture-record checksum": args.phase_a_fixture_record_sha256,
        "Phase-B fixture-record checksum": args.phase_b_fixture_record_sha256,
        "model revision": args.model_revision,
        "expected command checksum": args.expected_command_sha256,
        "runtime commit": args.runtime_commit,
        "gate token": args.gate_token,
        "gate role": args.gate_role,
        "camera expectations": args.camera_expectations,
        "sampler checksum": args.sampler_sha256,
        "Docker daemon ID": args.expected_docker_daemon_id,
        "host boot ID": args.expected_host_boot_id,
        "disposable root": args.disposable_root,
    }
    if args.emit_boundary_manifest is None:
        required.update(
            {
                "boundary manifest": args.boundary_manifest,
                "boundary manifest checksum": args.boundary_manifest_sha256,
            }
        )
    if any(value is None for value in required.values()):
        raise GateAbort("missing required gate arguments")
    if not CONTAINER_NAME_RE.fullmatch(args.container):
        raise GateAbort("refusing non Task11B container name")
    if args.frigate_container != "frigate":
        raise GateAbort("Frigate container name must remain exact")
    if not CONTAINER_ID_RE.fullmatch(args.expected_container_id):
        raise GateAbort("expected container ID must be full")
    if not CONFIG_DIGEST_RE.fullmatch(args.expected_image_config):
        raise GateAbort("expected image config must be full digest")
    if not CONFIG_DIGEST_RE.fullmatch(args.candidate_oci_index):
        raise GateAbort("candidate OCI index must be full digest")
    if args.candidate_oci_index != args.expected_image_config:
        raise GateAbort("candidate OCI index did not match Docker image identity")
    if not CONFIG_DIGEST_RE.fullmatch(args.candidate_config_digest):
        raise GateAbort("candidate config digest must be full digest")
    if (
        not isinstance(args.candidate_layer_diff_ids, list)
        or not args.candidate_layer_diff_ids
    ):
        raise GateAbort("candidate ordered layer diff IDs were missing")
    for diff_id in args.candidate_layer_diff_ids:
        if not CONFIG_DIGEST_RE.fullmatch(diff_id):
            raise GateAbort("candidate layer diff ID must be a full digest")
    if not LOWER_SHA256_RE.fullmatch(args.model_envelope_catalog_sha256):
        raise GateAbort("model-envelope catalog checksum must be SHA256")
    if not LOWER_SHA256_RE.fullmatch(args.phase_a_fixture_record_sha256):
        raise GateAbort("Phase-A fixture-record checksum must be SHA256")
    if not LOWER_SHA256_RE.fullmatch(args.phase_b_fixture_record_sha256):
        raise GateAbort("Phase-B fixture-record checksum must be SHA256")
    if args.phase_a_fixture_record_sha256 == args.phase_b_fixture_record_sha256:
        raise GateAbort("fixture-record checksums must be distinct")
    if not MODEL_REVISION_RE.fullmatch(args.model_revision):
        raise GateAbort("model revision must be a canonical immutable hf revision")
    if not SHA256_RE.fullmatch(args.expected_command_sha256):
        raise GateAbort("expected command checksum must be SHA256")
    if not COMMIT_RE.fullmatch(args.runtime_commit):
        raise GateAbort("runtime commit must be full Git SHA")
    if not TOKEN_RE.fullmatch(args.gate_token):
        raise GateAbort("gate token format invalid")
    if not ROLE_RE.fullmatch(args.gate_role):
        raise GateAbort("gate role format invalid")
    if not SHA256_RE.fullmatch(args.sampler_sha256):
        raise GateAbort("sampler checksum must be SHA256")
    if not BOOT_ID_RE.fullmatch(args.expected_host_boot_id):
        raise GateAbort("expected host boot ID invalid")
    if args.boundary_manifest_sha256 is not None and not SHA256_RE.fullmatch(
        args.boundary_manifest_sha256
    ):
        raise GateAbort("boundary manifest checksum must be SHA256")
    normalized_root = posixpath.normpath(args.disposable_root)
    if (
        not args.disposable_root.startswith("/")
        or args.disposable_root != normalized_root
        or normalized_root == "/"
    ):
        raise GateAbort("disposable root must be a normalized absolute path")
    if args.systemd_stop_post and not args.cleanup_only:
        raise GateAbort("systemd stop-post mode requires cleanup-only")
    if args.emit_boundary_manifest is not None and args.cleanup_only:
        raise GateAbort("boundary generation must be a separate step")
    if args.duration_seconds < 900:
        raise GateAbort("production gate requires 900 seconds")
    if args.interval_seconds != 5:
        raise GateAbort("production gate requires five second cadence")
    if (
        isinstance(args.gpu_free_floor_bytes, bool)
        or not isinstance(args.gpu_free_floor_bytes, int)
        or not 0 < args.gpu_free_floor_bytes < 24 * GIB
    ):
        raise GateAbort("gate GPU priority reserve must be explicit and positive")
    if args.host_reserve_bytes != 4 * GIB:
        raise GateAbort("approved host reserve is 4 GiB")
    if args.expected_chunk_minutes != 5:
        raise GateAbort("Frigate gate requires five-minute chunks")
    require_exact_endpoint(args.frigate_stats_url, "frigate")
    require_exact_endpoint(args.ollama_url, "ollama")
    if args.candidate_mode == "runtime":
        require_exact_endpoint(args.candidate_status_url, "candidate")
        if args.expected_model not in MODEL_DESCENT or args.gate_role != "runtime-auto":
            raise GateAbort("runtime gate must bind the highest-qualified model")
        if args.expected_memory_bytes != 10 * GIB:
            raise GateAbort("runtime gate memory must be 10 GiB")
        if args.expected_profiler_returncode is not None:
            raise GateAbort("runtime gate cannot expect a profiler return code")
    else:
        expected_role = f"profile-{args.expected_model}"
        if args.expected_model not in {
            "large-v3",
            "medium",
            "small",
            "base",
            "tiny",
        }:
            raise GateAbort("profiler gate model invalid")
        if args.gate_role != expected_role:
            raise GateAbort("profiler role did not match model")
        if args.expected_memory_bytes != 12 * GIB:
            raise GateAbort("profiler gate memory must be 12 GiB")
        if args.expected_profiler_returncode not in {0, 3}:
            raise GateAbort("profiler gate return code must be 0 or 3")
        if args.leave_running_on_pass:
            raise GateAbort("profiler gate cannot retain its holding container")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    validate_args(args)
    if args.emit_boundary_manifest is not None:
        return emit_boundary_manifest(args)
    if args.cleanup_only:
        return cleanup_only(args)
    raise GateAbort("standalone sampler gate is unavailable; use the runtime observer")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateAbort as exc:
        print(f"TASK11B_HEALTH_ABORT reason={exc.code}", file=sys.stderr)
        raise SystemExit(1)
