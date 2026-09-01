#!/usr/bin/env python3
"""Fail-closed health sampler for the isolated Frigate v0.5.0 gate.

This file is owner-operated evidence tooling and is excluded from the runtime
image. It observes Frigate, Ollama, NVIDIA, host/cgroup memory, and one stopped
disposable Task 11B container. It binds that container's full ID, image config,
and dedicated ownership labels before starting it. Every non-pass exit stops
and verifies only that immutable ID.

Use ``--emit-systemd-run-script`` after creating and independently hashing the
boundary manifest. The generated transient service registers an immutable-ID
``ExecStopPost`` cleanup before the sampler starts, covering SIGKILL and
interpreter failure. Host power loss remains outside an in-process gate.
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
import shlex
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

REQUIRED_DETECTORS = ("onnx_0", "onnx_1")
REQUIRED_EMBEDDING_SPEEDS = (
    "face_recognition_speed",
    "plate_recognition_speed",
    "yolov9_plate_detection_speed",
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
}
CONTAINER_NAME_RE = re.compile(r"^subgen-task11b-[a-z0-9][a-z0-9_.-]*$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
CONFIG_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{15,127}$")
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

INSPECT_TEMPLATE = """{
  "Id": {{json .Id}},
  "Name": {{json .Name}},
  "Image": {{json .Image}},
  "RestartCount": {{json .RestartCount}},
  "State": {
    "Status": {{json .State.Status}},
    "Running": {{json .State.Running}},
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
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
        if (
            not normalized_source.startswith("/")
            or posixpath.commonpath((normalized_source, normalized_root))
            != normalized_root
            or normalized_source == normalized_root
            or not normalized_destination.startswith("/")
            or normalized_destination == "/"
            or destination != normalized_destination
            or destination in destinations
        ):
            raise GateAbort("candidate mount escaped the disposable allowlist")
        if filesystem_check:
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
    return sorted(result, key=lambda mount: mount["destination"])


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
        "--gpu-reserve-gib": "8",
    }
    allowed_values = set(exact) | {
        "--model-revision",
        "--runs",
        "--host-margin-mib",
        "--device-margin-mib",
        "--host-reserve-gib",
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
    if not COMMIT_RE.fullmatch(revision):
        raise GateAbort("profiler model revision was not immutable")
    try:
        runs = int(values.get("--runs", ""))
        host_margin = int(values.get("--host-margin-mib", ""))
        device_margin = int(values.get("--device-margin-mib", ""))
        host_reserve = float(values.get("--host-reserve-gib", ""))
    except ValueError as exc:
        raise GateAbort("profiler numeric policy was malformed") from exc
    if (
        not 3 <= runs <= 30
        or host_margin <= 0
        or device_margin <= 0
        or host_reserve <= 0
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
            "/media": True,
            "/subgen/models": True,
            "/opt/subgen/monitor": True,
            "/opt/subgen/model-envelopes": False,
        }
        required = set(policy)
    else:
        policy = {
            "/subgen/models": True,
            "/profile/input": False,
            "/profile/output": True,
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
        if reserve != 8.0:
            raise GateAbort("runtime GPU reserve was not exact")
    else:
        if host.get("NetworkMode") != "none":
            raise GateAbort("profiler networking was not disabled")
        if attachments:
            raise GateAbort("profiler network attachments were not empty")
        _validate_profiler_command(item, args)


def canonical_execution_boundary(
    item: dict[str, Any], *, disposable_root: str, filesystem_check: bool = True
) -> dict[str, Any]:
    """Return the secret-safe, exact execution boundary used by the gate."""
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
    return {
        "schema": 3,
        "environment_sha256": sha256_bytes(_canonical_json_bytes(environment)),
        "config_sha256": sha256_bytes(_canonical_json_bytes(config_full)),
        "host_config_sha256": sha256_bytes(_canonical_json_bytes(canonical_host_full)),
        "network_attachments_sha256": sha256_bytes(
            _canonical_json_bytes(network_attachments)
        ),
        "entrypoint_command_sha256": _command_digest(item),
        "user": user,
        "working_directory": working_dir,
        "host": {key: host[key] for key in exact_host_keys},
        "network_attachments": network_attachments,
        "mounts": _normalized_mounts(
            item,
            disposable_root=disposable_root,
            filesystem_check=filesystem_check,
        ),
    }


def execution_boundary_digest(boundary: dict[str, Any]) -> str:
    return sha256_bytes(_canonical_json_bytes(boundary))


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
    if document.get("schema") != 3:
        raise GateAbort("boundary expectation schema was invalid")
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
    boundary = canonical_execution_boundary(
        item,
        disposable_root=args.disposable_root,
        filesystem_check=filesystem_check,
    )
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
    client.verify_local_daemon()
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
    client.verify_local_daemon()
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
        "controller_state": "normal",
        "recovery_reason": None,
        "admission_open": True,
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
    payload: bytes, *, expected_model: str, expected_version: int
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
    for entry in entries:
        if not isinstance(entry, dict):
            raise GateAbort("profiler catalog entry was malformed")
        policy = entry.get("policy")
        if isinstance(policy, dict) and policy.get("model") == expected_model:
            model_entries += 1
    if model_entries < 1:
        raise GateAbort("profiler catalog model entry was not exact")
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
        client.verify_local_daemon()
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
    client.verify_local_daemon()
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
    client.verify_local_daemon()
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
        ("--expected-container-id", args.expected_container_id),
        ("--expected-image-config", args.expected_image_config),
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
    client.verify_local_daemon()
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


def emit_systemd_run_script(args: argparse.Namespace) -> int:
    """Create an owner-only systemd-run wrapper with immutable ExecStopPost."""
    if sha256_file(Path(__file__)) != args.sampler_sha256.lower():
        raise GateAbort("sampler checksum mismatch")
    ensure_boundary_expectation(args)
    client = DockerClient(args.expected_docker_daemon_id, args.expected_host_boot_id)
    # Generation is permitted only while the exact candidate is still fresh.
    binding = bind_candidate(client, args)
    executable = str(Path(sys.executable).resolve())
    sampler_path = str(Path(__file__).resolve())
    common = _gate_cli_arguments(args)
    worker = [executable, sampler_path, *common]
    cleanup = [
        executable,
        sampler_path,
        *common,
        "--cleanup-only",
        "--systemd-stop-post",
    ]
    cleanup_property = "ExecStopPost=" + " ".join(
        _systemd_quote(part) for part in cleanup
    )
    unit = f"subgen-task11b-{binding.gate_token_digest[:16]}"
    runtime_max = args.duration_seconds + args.start_timeout_seconds + 300
    command = [
        "/usr/bin/systemd-run",
        f"--unit={unit}",
        "--collect",
        "--wait",
        "--service-type=exec",
        "--property=User=root",
        "--property=Group=root",
        "--property=UMask=0077",
        "--property=WorkingDirectory=/",
        "--property=StandardInput=null",
        "--property=NoNewPrivileges=yes",
        "--property=Restart=no",
        "--property=KillMode=mixed",
        "--property=SendSIGKILL=yes",
        "--property=TimeoutStopSec=300s",
        f"--property=RuntimeMaxSec={runtime_max}s",
        f"--property={cleanup_property}",
        "--",
        *worker,
    ]
    script = ("#!/bin/sh\nset -eu\nexec " + shlex.join(command) + "\n").encode("utf-8")
    _write_private_create_only(args.emit_systemd_run_script, script, 0o700)
    print(f"TASK11B_SUPERVISOR_READY unit={unit} script_sha256={sha256_bytes(script)}")
    return 0


def run_gate(args: argparse.Namespace) -> int:
    gate_started_wall = time.time()
    client = DockerClient(args.expected_docker_daemon_id, args.expected_host_boot_id)
    binding: CandidateBinding | None = None
    evidence: EvidenceWriter | None = None
    prior_handlers: dict[int, Any] = {}
    passed = False
    failure: BaseException | None = None
    stop_outcome: dict[str, Any] | None = None
    sampler_sha256 = "unverified"
    sample_count = 0
    observed_seconds = 0.0
    frigate_binding: ObservedBinding | None = None
    logs: IncrementalLogScanner | None = None
    observation: ObservationOutcome | None = None
    try:
        prior_handlers = install_signal_handlers()
        ensure_boundary_expectation(args)
        binding = bind_candidate(client, args)
        sampler_sha256 = sha256_file(Path(__file__))
        if sampler_sha256.lower() != args.sampler_sha256.lower():
            raise GateAbort("sampler checksum mismatch")
        camera_expectations = load_camera_expectations(args.camera_expectations)
        daemon_digest, boot_digest = client.verify_local_daemon()
        frigate_binding = bind_observed_container(client, args.frigate_container)
        frigate_before = observed_state(client, frigate_binding)
        if (
            frigate_before["running"] is not True
            or frigate_before["health"] != "healthy"
        ):
            raise GateAbort("Frigate unhealthy before candidate start")
        evidence = EvidenceWriter.open(args.output, binding.gate_token_digest)
        # Cursor and baselines are armed before this function starts the ID.
        logs = IncrementalLogScanner(
            client, binding, frigate_binding, gate_started_wall
        )
        observation = observe_gate(
            args,
            evidence,
            client,
            binding,
            frigate_binding,
            camera_expectations,
            daemon_digest,
            boot_digest,
            sampler_sha256,
            logs,
        )
        sample_count = observation.sample_count
        observed_seconds = observation.observed_seconds
        if not args.leave_running_on_pass:
            stop_outcome = stop_bound_candidate(client, binding, args)
            stop_completion = validate_stop_completion(
                args,
                client,
                binding,
                frigate_binding,
                logs,
                observation,
            )
            stop_outcome["completion"] = stop_completion
        else:
            stop_outcome = {
                "attempted": False,
                "verified_stopped": False,
                "left_running_by_explicit_pass_policy": True,
            }
        evidence.write(
            {
                "event": "gate_pass",
                "timestamp": utc_now(),
                "continuous_seconds": round(observed_seconds, 3),
                "samples": sample_count,
                "cleanup": stop_outcome,
            }
        )
        evidence.seal(
            outcome="pass",
            sampler_sha256=sampler_sha256,
            image_config=binding.image_config,
            cleanup=stop_outcome,
        )
        passed = True
    except BaseException as exc:
        failure = exc
    finally:
        if not passed and binding is not None:
            try:
                stop_outcome = stop_bound_candidate(client, binding, args)
            except BaseException as stop_exc:
                stop_outcome = {
                    "verified_stopped": False,
                    "error": safe_reason(stop_exc),
                }
                prior_failure = safe_reason(failure or GateAbort("unknown failure"))
                failure = GateAbort(f"{prior_failure} cleanup unverified")
        if not passed and evidence is not None and not evidence.closed:
            try:
                evidence.write(
                    {
                        "event": "gate_abort",
                        "timestamp": utc_now(),
                        "reason": safe_reason(failure or GateAbort("unknown failure")),
                        "elapsed_wall_seconds": round(
                            time.time() - gate_started_wall, 3
                        ),
                        "cleanup": stop_outcome,
                    }
                )
                evidence.seal(
                    outcome="abort",
                    sampler_sha256=sampler_sha256,
                    image_config=binding.image_config if binding else "unbound",
                    cleanup=stop_outcome or {"verified_stopped": False},
                )
            except BaseException:
                pass
        if evidence is not None:
            evidence.close()
        if prior_handlers:
            restore_signal_handlers(prior_handlers)
    if not passed:
        if isinstance(failure, GateAbort):
            raise failure
        raise GateAbort(
            safe_reason(failure or GateAbort("unknown failure"))
        ) from failure
    print(
        "TASK11B_HEALTH_PASS "
        f"container_id_sha256={sha256_bytes(binding.container_id.encode('ascii'))} "
        f"seconds={round(observed_seconds, 3)} samples={sample_count}"
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
    result.add_argument("--gpu-free-floor-bytes", type=int, default=8 * GIB)
    result.add_argument("--host-reserve-bytes", type=int, default=4 * GIB)
    result.add_argument("--frigate-container", default="frigate")
    result.add_argument("--frigate-stats-url", default=EXACT_ENDPOINTS["frigate"])
    result.add_argument("--ollama-url", default=EXACT_ENDPOINTS["ollama"])
    result.add_argument("--candidate-status-url", default=EXACT_ENDPOINTS["candidate"])
    result.add_argument("--candidate-mode", choices=("runtime", "profiler"))
    result.add_argument("--expected-model")
    result.add_argument("--expected-profiler-returncode", type=int)
    result.add_argument("--expected-container-id")
    result.add_argument("--expected-image-config")
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
    result.add_argument("--emit-systemd-run-script", type=Path)
    result.add_argument("--emit-boundary-manifest", type=Path)
    result.add_argument("--self-test", action="store_true")
    return result


def validate_args(args: argparse.Namespace) -> None:
    required = {
        "container": args.container,
        "output": args.output,
        "expected memory": args.expected_memory_bytes,
        "candidate mode": args.candidate_mode,
        "expected container ID": args.expected_container_id,
        "expected image config": args.expected_image_config,
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
    if args.cleanup_only and args.emit_systemd_run_script is not None:
        raise GateAbort("cleanup-only cannot emit a supervisor script")
    if args.emit_boundary_manifest is not None and (
        args.cleanup_only or args.emit_systemd_run_script is not None
    ):
        raise GateAbort("boundary generation must be a separate step")
    if args.duration_seconds < 900:
        raise GateAbort("production gate requires 900 seconds")
    if args.interval_seconds != 5:
        raise GateAbort("production gate requires five second cadence")
    if args.gpu_free_floor_bytes != 8 * GIB:
        raise GateAbort("approved GPU reserve is 8 GiB")
    if args.host_reserve_bytes != 4 * GIB:
        raise GateAbort("approved host reserve is 4 GiB")
    require_exact_endpoint(args.frigate_stats_url, "frigate")
    require_exact_endpoint(args.ollama_url, "ollama")
    if args.candidate_mode == "runtime":
        require_exact_endpoint(args.candidate_status_url, "candidate")
        if args.expected_model != "medium" or args.gate_role != "runtime-auto":
            raise GateAbort("runtime gate must require automatic medium")
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
    if args.emit_systemd_run_script is not None:
        return emit_systemd_run_script(args)
    if args.cleanup_only:
        return cleanup_only(args)
    return run_gate(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateAbort as exc:
        print(f"TASK11B_HEALTH_ABORT reason={exc.code}", file=sys.stderr)
        raise SystemExit(1)
