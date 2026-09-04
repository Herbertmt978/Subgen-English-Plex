#!/usr/bin/env python3
"""Thin owner-operated collector/finalizer for Task 11B soak evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
import queue
import re
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows test host
    fcntl = None  # type: ignore[assignment]


RUNTIME_EVENT_PREFIX = b"SUBGEN_RUNTIME_EVENT "
RUNTIME_EVENT_SCHEMA = "subgen.runtime-event/v1"
RUNTIME_EVENT_NAME = "multichunk_transcription_completed"
RUNTIME_EVENT_KEYS = {
    "atomic_publish", "chunks_total", "event", "event_sequence",
    "monotonic_ns", "outcome", "schema", "workload_id",
}
DOCKER_TIMESTAMP_RE = re.compile(
    rb"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{1,9}Z) (.*\n)$"
)
APP_LOG_PREFIX_RE = re.compile(
    rb"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) INFO: (.*\n)$"
)
APP_ANY_PREFIX_RE = re.compile(
    rb"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [A-Z]+: (.*\n)$"
)
CONTAINER_RE = re.compile(r"^[0-9a-f]{64}$")
BOOT_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
GPU_RE = re.compile(r"^GPU-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
AUDIT_RE = re.compile(rb"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[([A-Z0-9_]{1,64})\] ")
CUDA_OOM_RE = re.compile(rb"CUDA (?:error:\s*)?out of memory|torch\.cuda\.OutOfMemoryError", re.I)
TRANSCRIPTION_FAILURE_RE = re.compile(rb"(?:transcription|transcribe).{0,120}(?:failed|failure|exception)|SIGSEGV|segmentation fault", re.I)
JOIN_FAILURE_RE = re.compile(rb"(?:join|atomic publish).{0,120}(?:failed|failure|exception)", re.I)
PARTIAL_OUTPUT_RE = re.compile(rb"partial (?:subtitle|output).{0,120}(?:left|remain|failed)", re.I)
FRIGATE_FAILURE_RE = re.compile(
    rb"(?:detector|embedding|face_recognition|plate_recognition|yolov9).{0,240}(?:error|failed|exception|stalled|timeout|traceback)|"
    rb"(?:error|failed|exception|stalled|timeout|traceback).{0,240}(?:detector|embedding|face_recognition|plate_recognition|yolov9)",
    re.I,
)
XID_RE = re.compile(r"NVRM:\s*Xid", re.I)
DOCKER_BINARY = "/usr/bin/docker"
JOURNALCTL_BINARY = "/usr/bin/journalctl"
SYSTEMCTL_BINARY = "/usr/bin/systemctl"
NVIDIA_BINARY = "/usr/bin/nvidia-smi"
DOCKER_SOCKET = "/run/docker.sock"
DOCKER_HOST = f"unix://{DOCKER_SOCKET}"
CANDIDATE_STATUS_PATH = "/status"
FRIGATE_STATS_PATH = "/api/stats"
PRESSURE_SIGNAL_PATH = Path("/run/subgen-priority/pressure.json")
MONITOR_ENV_PATH = Path("/opt/subgen/monitor.env")
MONITOR_HELPER_PATH = Path("/opt/subgen/monitor_subgen_failures.py")
REPAIR_HELPER_PATH = Path("/opt/subgen/repair_subgen_failures.py")
PRIORITY_ENV_PATH = Path("/opt/subgen/priority-monitor.env")
PRIORITY_HELPER_PATH = Path("/opt/subgen/monitor_frigate_priority.py")
MONITOR_AUDIT_PATH = Path("/opt/subgen/monitor/subgen_failed_events.log")
MARKER_REGISTRY_PATH = Path("/opt/subgen/monitor/subgen_failure_markers.json")
MONITOR_UNIT = "subgen-monitor.service"
PRIORITY_UNIT = "subgen-priority-monitor.service"
REPAIR_SERVICE = "subgen-repair.service"
REPAIR_TIMER = "subgen-repair.timer"
SYSTEMD_EXEC_FIELDS = (
    "ExecCondition", "ExecStartPre", "ExecStart", "ExecStartPost",
    "ExecReload", "ExecStop", "ExecStopPost",
)
SYSTEMD_BASE_FIELDS = (
    "Id", "ActiveState", "SubState", "InvocationID", "FragmentPath",
    "DropInPaths", "UnitFileState",
)
SYSTEMD_SERVICE_STATE_FIELDS = ("MainPID", "NRestarts")
SYSTEMD_EXEC_CONTEXT_FIELDS = (
    "WorkingDirectory", "Environment", "EnvironmentFiles", "PassEnvironment",
    "UnsetEnvironment",
)
SYSTEMD_SHOW_FIELDS = (
    *SYSTEMD_BASE_FIELDS, *SYSTEMD_SERVICE_STATE_FIELDS, *SYSTEMD_EXEC_FIELDS,
    *SYSTEMD_EXEC_CONTEXT_FIELDS, "Unit",
)
MAX_COMMAND_BYTES = 2 * 1024**2
MAX_HTTP_BYTES = 2 * 1024**2
MAX_MARKER_BYTES = 8 * 1024**2
MAX_AUDIT_DELTA_BYTES = 1024**2
MAX_LOG_LINE_BYTES = 64 * 1024
EVENT_QUEUE_SIZE = 1024
MQTT_REFRESH_SECONDS = 60
# Product cadence remains exactly 60 seconds; this separate 10-second allowance
# covers broker delivery and observer scheduling only. A sustained gap still aborts.
MQTT_MAX_STALE_NS = 70_000_000_000
MQTT_BOOTSTRAP_TIMEOUT_SECONDS = 75.0
MQTT_NETWORK_TIMEOUT_SECONDS = 5.0
MQTT_MAX_PACKET_BYTES = 2 * 1024**2
MQTT_SUBSCRIBE_PACKET_ID = 1
MQTT_OBJECT_IDS = ("subgen_items_left", "subgen_scan")
MQTT_MAX_LIBRARIES = 129
MQTT_BINDING_KEYS = {
    "enabled", "semantic_config_sha256", "refresh_seconds", "library_label_policy",
}
HEALTH_KEYS = {
    "schema", "identity_sha256", "candidate_running", "candidate_oom_killed",
    "frigate_healthy", "deletion_enabled", "active", "chunk_uncommitted",
    "completion_generation", "controller_phase", "counters", "mqtt_inventory",
}
MAX_RELEVANT_LOG_EVIDENCE = 100_000
CONFIG_KEYS = {
    "schema", "image", "model", "configuration", "deployment",
    "gate_seal_sha256", "candidate_identity_record_sha256", "rollback_record_sha256", "live",
}
LIVE_KEYS = {
    "schema", "candidate_container_id", "frigate_container_id", "docker_daemon_id",
    "host_boot_id", "candidate_runtime_config_sha256", "frigate_runtime_config_sha256",
    "model_catalog_path", "model_identity_path", "frigate_config_path", "priority_policy_path",
    "systemd_boundary_sha256", "host_memory_reserve_bytes", "gpu_uuid",
    "gpu_total_bytes", "gpu_free_reserve_bytes", "mqtt_inventory",
}
FINALIZATION_KEYS = {"schema", "outcome", "rollback"}
MAX_SOURCE_BYTES = 4 * 1024**2
class ObserverError(RuntimeError):
    def __init__(self, message: str) -> None:
        import re

        self.code = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")[:96] or "observer_error"
        super().__init__(self.code)


def _load_evidence() -> tuple[Any, bytes, str]:
    path = Path(__file__).resolve().with_name("soak_evidence.py")
    try:
        item = path.lstat()
        if not stat.S_ISREG(item.st_mode) or path.is_symlink() or not 0 < item.st_size <= MAX_SOURCE_BYTES:
            raise ObserverError("soak evidence source was unsafe")
        payload = path.read_bytes()
    except OSError as exc:
        raise ObserverError("soak evidence source was unavailable") from exc
    if len(payload) != item.st_size:
        raise ObserverError("soak evidence source changed during read")
    name = "_task11b_soak_evidence"
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        exec(compile(payload, str(path), "exec", dont_inherit=True), module.__dict__)
    except BaseException:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module, payload, hashlib.sha256(payload).hexdigest()


ev, _EVIDENCE_PAYLOAD, EVIDENCE_SHA256 = _load_evidence()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _owner() -> int | None:
    getter = getattr(os, "geteuid", None)
    return getter() if callable(getter) else None


def _absolute(path: Path, label: str) -> Path:
    result = Path(path)
    if not result.is_absolute():
        raise ObserverError(f"{label} path was not absolute")
    return result


def _private_parent(path: Path, label: str) -> None:
    try:
        item = path.parent.lstat()
    except OSError as exc:
        raise ObserverError(f"{label} parent was unavailable") from exc
    owner = _owner()
    if (
        not stat.S_ISDIR(item.st_mode)
        or path.parent.is_symlink()
        or (owner is not None and (item.st_uid != owner or stat.S_IMODE(item.st_mode) != 0o700))
    ):
        raise ObserverError(f"{label} parent was not owner only")


def _read(path: Path, maximum: int, label: str, *, private: bool) -> bytes:
    target = _absolute(path, label)
    try:
        before = target.lstat()
    except OSError as exc:
        raise ObserverError(f"{label} was unavailable") from exc
    owner = _owner()
    if (
        not stat.S_ISREG(before.st_mode)
        or target.is_symlink()
        or not 0 < before.st_size <= maximum
        or (private and owner is not None and (before.st_uid != owner or stat.S_IMODE(before.st_mode) != 0o600))
    ):
        raise ObserverError(f"{label} ownership type or size was unsafe")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ObserverError(f"{label} identity changed")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(fd, min(1024**2, remaining))
                if not chunk:
                    raise ObserverError(f"{label} ended early")
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
            if (after.st_dev, after.st_ino, after.st_size) != (before.st_dev, before.st_ino, before.st_size):
                raise ObserverError(f"{label} changed during read")
            return b"".join(chunks)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ObserverError(f"{label} could not be read") from exc


def _read_optional_bound(path: Path, maximum: int, label: str) -> bytes | None:
    """Read one exact absolute regular file without following a final symlink."""
    target = _absolute(path, label)
    try:
        before = target.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ObserverError(f"{label} was unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or target.is_symlink()
        or before.st_nlink != 1
        or not 0 <= before.st_size <= maximum
    ):
        raise ObserverError(f"{label} type or size was unsafe")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
                raise ObserverError(f"{label} identity changed")
            payload = bytearray()
            while len(payload) <= maximum:
                chunk = os.read(fd, min(65536, maximum + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            after = os.fstat(fd)
            if len(payload) > maximum or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
                raise ObserverError(f"{label} changed during read")
            return bytes(payload)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ObserverError(f"{label} could not be read") from exc


def _sha_file(path: Path, expected: str, maximum: int, label: str) -> bytes:
    payload = _read_optional_bound(path, maximum, label)
    if payload is None or hashlib.sha256(payload).hexdigest() != expected:
        raise ObserverError(f"{label} changed")
    return payload


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ObserverError("canonical live value was invalid") from exc


def _command(argv: list[str], label: str, *, maximum: int = MAX_COMMAND_BYTES, timeout: float = 4.0) -> bytes:
    if not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
        raise ObserverError(f"{label} command was invalid")
    safe_env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": "/"}
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False, env=safe_env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ObserverError(f"{label} command failed") from exc
    if result.returncode != 0 or result.stderr or len(result.stdout) > maximum:
        raise ObserverError(f"{label} command failed")
    return result.stdout


def _strict_json_bytes(payload: bytes, maximum: int, label: str) -> Any:
    if not isinstance(payload, bytes) or not payload or len(payload) > maximum:
        raise ObserverError(f"{label} JSON size was invalid")
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=ev._unique, parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ObserverError(f"{label} JSON was invalid") from exc


def _timestamp_ns(value: bytes, label: str) -> int:
    try:
        text_value = value.decode("ascii")
        head, fraction = text_value[:-1].split(".", 1)
        parsed = dt.datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp()) * 1_000_000_000 + int(fraction.ljust(9, "0"))
    except (UnicodeDecodeError, ValueError, OverflowError) as exc:
        raise ObserverError(f"{label} timestamp was invalid") from exc


def _format_utc_ns(value: int) -> str:
    seconds, nanos = divmod(value, 1_000_000_000)
    moment = dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{nanos // 1000:06d}Z"


def _fsync_parent(path: Path, label: str) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ObserverError(f"{label} parent fsync failed") from exc


def _write_all(fd: int, payload: bytes, label: str) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise ObserverError(f"{label} write was incomplete")
        offset += written


def _create(path: Path, payload: bytes, label: str) -> None:
    target = _absolute(path, label)
    _private_parent(target, label)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise ObserverError(f"{label} already existed") from exc
    except OSError as exc:
        raise ObserverError(f"{label} could not be created") from exc
    try:
        if os.name == "posix": os.fchmod(fd, 0o600)
        _write_all(fd, payload, label); os.fsync(fd)
    except BaseException:
        os.close(fd)
        try: target.unlink()
        except OSError: pass
        raise
    else:
        os.close(fd)
    _fsync_parent(target, label)


def _ensure_soak_record(path: Path, payload: bytes) -> None:
    """Create the derived record once, or accept only the identical prior write."""
    target = _absolute(path, "soak record")
    _private_parent(target, "soak record")
    try:
        target.lstat()
    except FileNotFoundError:
        _create(target, payload, "soak record")
        return
    except OSError as exc:
        raise ObserverError("soak record target could not be checked") from exc
    if _read(target, ev.MAX_RECORD_BYTES, "soak record", private=True) != payload:
        raise ObserverError("soak record already existed with different content")


def _source_hashes(test_source: Path) -> tuple[str, str, str]:
    observer = Path(__file__).resolve(strict=True)
    test = _absolute(test_source, "observer test source").resolve(strict=True)
    if test.parent != observer.parent or test.name != "test_soak_runtime_observer.py":
        raise ObserverError("observer test source was not adjacent and exact")
    return EVIDENCE_SHA256, hashlib.sha256(_read(observer, MAX_SOURCE_BYTES, "soak observer source", private=False)).hexdigest(), hashlib.sha256(_read(test, MAX_SOURCE_BYTES, "soak observer test source", private=False)).hexdigest()


def _load_json(path: Path, maximum: int, label: str, *, private: bool) -> dict[str, Any]:
    return ev.strict_line(_read(path, maximum, label, private=private), maximum)


def _validate_config(config: dict[str, Any], test_source: Path) -> dict[str, Any]:
    if not isinstance(config, dict) or set(config) != CONFIG_KEYS or config.get("schema") != "subgen.task11b.soak-config/v1":
        raise ObserverError("soak config schema was invalid")
    artifacts = {
        "gate_seal_sha256": config["gate_seal_sha256"],
        "candidate_identity_record_sha256": config["candidate_identity_record_sha256"],
    }
    evidence_hash, observer_hash, test_hash = _source_hashes(test_source)
    artifacts.update(evidence_sha256=evidence_hash, observer_sha256=observer_hash, observer_test_sha256=test_hash)
    identities = {key: config[key] for key in ("image", "model", "configuration", "deployment")}
    identities["artifacts"] = artifacts
    ev.validate_identities(identities)
    ev._hex(config["rollback_record_sha256"], "rollback record")
    live = config["live"]
    if not isinstance(live, dict) or set(live) != LIVE_KEYS or live.get("schema") != "subgen.task11b.soak-live/v1":
        raise ObserverError("live soak config schema was invalid")
    for key in ("candidate_container_id", "frigate_container_id"):
        if not isinstance(live[key], str) or CONTAINER_RE.fullmatch(live[key]) is None:
            raise ObserverError(f"live {key} was invalid")
    if not isinstance(live["docker_daemon_id"], str) or not 1 <= len(live["docker_daemon_id"]) <= 128 or any(ord(char) < 0x21 or ord(char) > 0x7e for char in live["docker_daemon_id"]):
        raise ObserverError("live Docker daemon ID was invalid")
    if not isinstance(live["host_boot_id"], str) or BOOT_RE.fullmatch(live["host_boot_id"]) is None:
        raise ObserverError("live host boot ID was invalid")
    for key in ("candidate_runtime_config_sha256", "frigate_runtime_config_sha256", "systemd_boundary_sha256"):
        ev._hex(live[key], f"live {key}")
    for key in ("model_catalog_path", "model_identity_path", "frigate_config_path", "priority_policy_path"):
        value = live[key]
        if not isinstance(value, str) or "\x00" in value or "\n" in value:
            raise ObserverError(f"live {key} was invalid")
        _absolute(Path(value), f"live {key}")
    for key in ("host_memory_reserve_bytes", "gpu_total_bytes", "gpu_free_reserve_bytes"):
        value = live[key]
        if type(value) is not int or value <= 0: raise ObserverError(f"live {key} was invalid")
    if live["gpu_free_reserve_bytes"] >= live["gpu_total_bytes"] or not isinstance(live["gpu_uuid"], str) or GPU_RE.fullmatch(live["gpu_uuid"]) is None:
        raise ObserverError("live GPU identity or reserve was invalid")
    _mqtt_binding(live["mqtt_inventory"])
    deployment = identities["deployment"]
    expected_deployment = {
        "host_boot_id_sha256": hashlib.sha256(live["host_boot_id"].encode("ascii")).hexdigest(),
        "docker_daemon_identity_sha256": hashlib.sha256(live["docker_daemon_id"].encode("utf-8")).hexdigest(),
        "container_id_sha256": hashlib.sha256(live["candidate_container_id"].encode("ascii")).hexdigest(),
        "frigate_container_id_sha256": hashlib.sha256(live["frigate_container_id"].encode("ascii")).hexdigest(),
    }
    if deployment != expected_deployment:
        raise ObserverError("live deployment identity did not match its raw bindings")
    if identities["configuration"]["runtime_config_sha256"] != live["candidate_runtime_config_sha256"]:
        raise ObserverError("live candidate configuration identity disagreed")
    if identities["configuration"]["monitored_config_sha256"] != hashlib.sha256(_canonical_bytes(live)).hexdigest():
        raise ObserverError("live monitored configuration digest disagreed")
    return identities


def _parse_runtime_payload(payload: bytes) -> dict[str, Any]:
    if not payload.endswith(b"\n"):
        raise ObserverError("runtime event was partial")
    document = ev.strict_line(payload, MAX_LOG_LINE_BYTES)
    if set(document) != RUNTIME_EVENT_KEYS or ev.canonical_line(document) != payload:
        raise ObserverError("runtime event was not exact canonical JSON")
    if (
        document["schema"] != RUNTIME_EVENT_SCHEMA
        or document["event"] != RUNTIME_EVENT_NAME
        or document["atomic_publish"] != "succeeded"
        or document["outcome"] != "success"
        or type(document["chunks_total"]) is not int
        or document["chunks_total"] <= 1
        or type(document["event_sequence"]) is not int
        or document["event_sequence"] <= 0
        or type(document["monotonic_ns"]) is not int
        or document["monotonic_ns"] < 0
        or not isinstance(document["workload_id"], str)
        or ev.HEX32.fullmatch(document["workload_id"]) is None
    ):
        raise ObserverError("runtime completion event contract failed")
    return document


def parse_runtime_event(line: bytes, *, started_utc_ns: int | None = None) -> tuple[dict[str, Any], int] | None:
    """Parse one Docker-timestamped, application-framed privacy-safe event."""
    if not isinstance(line, bytes) or not line.endswith(b"\n") or len(line) > MAX_LOG_LINE_BYTES:
        raise ObserverError("Docker log line framing was invalid")
    outer = DOCKER_TIMESTAMP_RE.fullmatch(line)
    if outer is None:
        raise ObserverError("Docker log timestamp framing was invalid")
    source_utc_ns = _timestamp_ns(outer.group(1), "Docker log")
    if started_utc_ns is not None and source_utc_ns < started_utc_ns:
        raise ObserverError("Docker log line preceded the soak cursor")
    application = outer.group(2)
    framed = APP_LOG_PREFIX_RE.fullmatch(application)
    if framed is not None:
        message = framed.group(2)
        if message.startswith(b"SUBGEN_RUNTIME_EVENT"):
            if not message.startswith(RUNTIME_EVENT_PREFIX):
                raise ObserverError("runtime event sentinel framing was malformed")
            return _parse_runtime_payload(message[len(RUNTIME_EVENT_PREFIX):]), source_utc_ns
        return None
    any_level = APP_ANY_PREFIX_RE.fullmatch(application)
    if (any_level is not None and any_level.group(1).startswith(b"SUBGEN_RUNTIME_EVENT")) or application.startswith(b"SUBGEN_RUNTIME_EVENT"):
        raise ObserverError("runtime event application framing was malformed")
    return None


def _fetch_loopback(port: int, path: str, label: str) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
    try:
        connection.request("GET", path, headers={"Accept": "application/json", "Accept-Encoding": "identity", "Connection": "close"})
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Encoding") not in (None, "identity"):
            raise ObserverError(f"{label} endpoint was unhealthy")
        payload = response.read(MAX_HTTP_BYTES + 1)
        if len(payload) > MAX_HTTP_BYTES or response.read(1):
            raise ObserverError(f"{label} endpoint exceeded its byte limit")
    except (OSError, http.client.HTTPException) as exc:
        raise ObserverError(f"{label} endpoint was unavailable") from exc
    finally:
        connection.close()
    value = _strict_json_bytes(payload, MAX_HTTP_BYTES, label)
    if not isinstance(value, dict):
        raise ObserverError(f"{label} endpoint root was invalid")
    return value


def _docker_json(*parts: str, label: str, maximum: int = MAX_COMMAND_BYTES) -> Any:
    payload = _command([DOCKER_BINARY, "--host", DOCKER_HOST, *parts], label, maximum=maximum)
    return _strict_json_bytes(payload, maximum, label)


def _container_boundary(item: dict[str, Any]) -> dict[str, Any]:
    network = item.get("NetworkSettings")
    if not isinstance(network, dict) or not isinstance(network.get("Networks"), dict):
        raise ObserverError("Docker network boundary was invalid")
    value = {key: item.get(key) for key in ("Id", "Name", "Image", "Config", "HostConfig", "Mounts")}
    value["Networks"] = network["Networks"]
    if not isinstance(value["Config"], dict) or not isinstance(value["HostConfig"], dict) or not isinstance(value["Mounts"], list):
        raise ObserverError("Docker execution boundary was invalid")
    return value


def _state(item: dict[str, Any], label: str) -> tuple[bool, bool, int, str | None, int]:
    state = item.get("State")
    restart = item.get("RestartCount")
    if not isinstance(state, dict) or type(restart) is not int or restart < 0:
        raise ObserverError(f"{label} state was invalid")
    health = state.get("Health")
    health_status = health.get("Status") if isinstance(health, dict) else None
    values = (state.get("Running"), state.get("OOMKilled"), state.get("Pid"))
    if any(type(value) not in (bool, int) for value in values) or type(values[0]) is not bool or type(values[1]) is not bool or type(values[2]) is not int:
        raise ObserverError(f"{label} state was invalid")
    return values[0], values[1], restart, health_status, values[2]


def _environment(item: dict[str, Any], label: str) -> dict[str, str]:
    config = item.get("Config")
    raw = config.get("Env") if isinstance(config, dict) else None
    if not isinstance(raw, list):
        raise ObserverError(f"{label} environment was unavailable")
    result: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, str) or "=" not in entry or "\x00" in entry:
            raise ObserverError(f"{label} environment was malformed")
        key, value = entry.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in result:
            raise ObserverError(f"{label} environment was malformed")
        result[key] = value
    return result


class _MqttSettings:
    """Runtime broker material whose representation is deliberately redacted."""

    __slots__ = (
        "enabled", "host", "port", "username", "password", "client_id",
        "state_topic", "availability_topic", "items_discovery_topic",
        "scan_discovery_topic", "node_id", "binding", "private_values",
    )

    def __init__(
        self,
        *,
        enabled: bool,
        host: str = "",
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        client_id: str = "",
        state_topic: str = "",
        availability_topic: str = "",
        items_discovery_topic: str = "",
        scan_discovery_topic: str = "",
        node_id: str = "",
        binding: dict[str, Any],
        private_values: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.enabled = enabled
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client_id = client_id
        self.state_topic = state_topic
        self.availability_topic = availability_topic
        self.items_discovery_topic = items_discovery_topic
        self.scan_discovery_topic = scan_discovery_topic
        self.node_id = node_id
        self.binding = binding
        self.private_values = private_values

    def __repr__(self) -> str:
        return "_MqttSettings(<redacted>)"


def _mqtt_environment_bool(value: object) -> bool:
    normalized = "false" if value is None else str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ObserverError("MQTT inventory enabled setting was invalid")


def _mqtt_topic(value: object, label: str) -> str:
    text = str(value).strip().strip("/")
    if (
        not text
        or len(text) > 256
        or not text.isascii()
        or re.fullmatch(r"[A-Za-z0-9_.\-/]+", text) is None
        or "//" in text
        or "+" in text
        or "#" in text
    ):
        raise ObserverError(f"MQTT {label} was invalid")
    return text


def _mqtt_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != MQTT_BINDING_KEYS:
        raise ObserverError("MQTT inventory binding was invalid")
    if type(value["enabled"]) is not bool:
        raise ObserverError("MQTT inventory binding enabled state was invalid")
    ev._hex(value["semantic_config_sha256"], "MQTT semantic configuration")
    if value["refresh_seconds"] != MQTT_REFRESH_SECONDS or type(value["refresh_seconds"]) is not int:
        raise ObserverError("MQTT inventory refresh was not fixed at 60 seconds")
    if value["library_label_policy"] not in {"generic", "custom"}:
        raise ObserverError("MQTT inventory library label policy was invalid")
    if not value["enabled"] and value["library_label_policy"] != "generic":
        raise ObserverError("disabled MQTT inventory label policy was invalid")
    return value


def _mqtt_settings_from_environment(environment: dict[str, str]) -> _MqttSettings:
    enabled = _mqtt_environment_bool(environment.get("MQTT_INVENTORY_ENABLED"))
    if not enabled:
        semantic = {
            "schema": "subgen.task11b.mqtt-semantic/v1",
            "enabled": False,
            "refresh_seconds": MQTT_REFRESH_SECONDS,
            "library_label_policy": "generic",
        }
        binding = {
            "enabled": False,
            "semantic_config_sha256": hashlib.sha256(_canonical_bytes(semantic)).hexdigest(),
            "refresh_seconds": MQTT_REFRESH_SECONDS,
            "library_label_policy": "generic",
        }
        return _MqttSettings(enabled=False, binding=binding)

    host = str(environment.get("MQTT_HOST", "")).strip()
    if not host or len(host) > 253 or any(ord(character) < 33 for character in host):
        raise ObserverError("MQTT broker host was invalid")
    try:
        port = int(environment.get("MQTT_PORT", "1883"))
    except (TypeError, ValueError) as exc:
        raise ObserverError("MQTT broker port was invalid") from exc
    if not 1 <= port <= 65535:
        raise ObserverError("MQTT broker port was invalid")
    username = environment.get("MQTT_USERNAME") or None
    password = environment.get("MQTT_PASSWORD") or None
    if password is not None and username is None:
        raise ObserverError("MQTT authentication configuration was invalid")
    if username is not None and (len(username) > 256 or "\x00" in username):
        raise ObserverError("MQTT authentication configuration was invalid")
    if password is not None and (len(password) > 1024 or "\x00" in password):
        raise ObserverError("MQTT authentication configuration was invalid")

    client_id = str(environment.get("MQTT_CLIENT_ID", "subgen-inventory")).strip()
    if (
        not client_id
        or len(client_id) > 64
        or not client_id.isascii()
        or re.fullmatch(r"[A-Za-z0-9_-]+", client_id) is None
    ):
        raise ObserverError("MQTT inventory client identity was invalid")
    topic_prefix = _mqtt_topic(environment.get("MQTT_TOPIC_PREFIX", "subgen"), "topic prefix")
    discovery_prefix = _mqtt_topic(
        environment.get("MQTT_DISCOVERY_PREFIX", "homeassistant"),
        "discovery prefix",
    )
    node_id = str(environment.get("MQTT_INVENTORY_NODE_ID", "subgen_inventory")).strip().casefold()
    if not node_id or len(node_id) > 64 or re.fullmatch(r"[a-z0-9_-]+", node_id) is None:
        raise ObserverError("MQTT inventory node identity was invalid")
    raw_library_names = str(environment.get("MQTT_INVENTORY_LIBRARY_NAMES", ""))
    if len(raw_library_names) > 4096 or (raw_library_names and len(raw_library_names.split("|")) > 128):
        raise ObserverError("MQTT inventory library label policy was invalid")
    label_policy = "custom" if raw_library_names else "generic"
    try:
        scan_timeout = float(environment.get("MQTT_INVENTORY_SCAN_TIMEOUT_SECONDS", "21600"))
    except (TypeError, ValueError) as exc:
        raise ObserverError("MQTT inventory scan timeout was invalid") from exc
    if not (scan_timeout == scan_timeout and 60.0 <= scan_timeout <= 86_400.0):
        raise ObserverError("MQTT inventory scan timeout was invalid")

    state_topic = f"{topic_prefix}/inventory/state"
    availability_topic = f"{topic_prefix}/availability"
    items_discovery_topic = f"{discovery_prefix}/sensor/{node_id}/items_left/config"
    scan_discovery_topic = f"{discovery_prefix}/sensor/{node_id}/scan_percent/config"
    semantic = {
        "schema": "subgen.task11b.mqtt-semantic/v1",
        "enabled": True,
        "broker_host_sha256": hashlib.sha256(host.encode("utf-8")).hexdigest(),
        "broker_port": port,
        "publisher_client_id_sha256": hashlib.sha256(client_id.encode("ascii")).hexdigest(),
        "state_topic_sha256": hashlib.sha256(state_topic.encode("ascii")).hexdigest(),
        "availability_topic_sha256": hashlib.sha256(availability_topic.encode("ascii")).hexdigest(),
        "items_discovery_topic_sha256": hashlib.sha256(items_discovery_topic.encode("ascii")).hexdigest(),
        "scan_discovery_topic_sha256": hashlib.sha256(scan_discovery_topic.encode("ascii")).hexdigest(),
        "node_id_sha256": hashlib.sha256(node_id.encode("ascii")).hexdigest(),
        "object_ids": list(MQTT_OBJECT_IDS),
        "refresh_seconds": MQTT_REFRESH_SECONDS,
        "scan_timeout_seconds": scan_timeout,
        "library_label_policy": label_policy,
    }
    binding = {
        "enabled": True,
        "semantic_config_sha256": hashlib.sha256(_canonical_bytes(semantic)).hexdigest(),
        "refresh_seconds": MQTT_REFRESH_SECONDS,
        "library_label_policy": label_policy,
    }
    path_values: list[str] = []
    for key in ("TRANSCRIBE_FOLDERS", "PATH_MAPPING_FROM", "PATH_MAPPING_TO"):
        raw = environment.get(key, "")
        parts = raw.split("|") if key == "TRANSCRIBE_FOLDERS" else [raw]
        path_values.extend(part.strip().replace("\\", "/") for part in parts if part.strip())
    private_values = tuple(
        ("credential", value)
        for value in (username, password)
        if isinstance(value, str) and value
    ) + tuple(("path", value) for value in path_values if value)
    return _MqttSettings(
        enabled=True,
        host=host,
        port=port,
        username=username,
        password=password,
        client_id=client_id,
        state_topic=state_topic,
        availability_topic=availability_topic,
        items_discovery_topic=items_discovery_topic,
        scan_discovery_topic=scan_discovery_topic,
        node_id=node_id,
        binding=binding,
        private_values=private_values,
    )


def _mqtt_expected_discovery(settings: _MqttSettings, *, scan: bool) -> dict[str, Any]:
    device = {
        "identifiers": [settings.node_id],
        "name": "Subgen",
        "manufacturer": "Subgen",
        "model": "Subtitle inventory",
    }
    common = {
        "state_topic": settings.state_topic,
        "json_attributes_topic": settings.state_topic,
        "availability_topic": settings.availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device,
        "entity_category": "diagnostic",
    }
    if scan:
        return {
            **common,
            "name": "Scan %",
            "unique_id": f"{settings.node_id}_scan_percent",
            "object_id": "subgen_scan",
            "value_template": "{{ value_json.scan_percent }}",
            "unit_of_measurement": "%",
            "icon": "mdi:progress-check",
        }
    return {
        **common,
        "name": "Items Left",
        "unique_id": f"{settings.node_id}_items_left",
        "object_id": "subgen_items_left",
        "value_template": "{{ value_json.items_left }}",
        "icon": "mdi:subtitles-outline",
    }


def _mqtt_json(payload: bytes, label: str) -> dict[str, Any]:
    value = _strict_json_bytes(payload, MQTT_MAX_PACKET_BYTES, label)
    if not isinstance(value, dict) or _canonical_bytes(value)[:-1] != payload:
        raise ObserverError(f"{label} was not exact canonical JSON")
    return value


def _validate_mqtt_state(payload: bytes, settings: _MqttSettings) -> None:
    state = _mqtt_json(payload, "MQTT inventory state")
    if set(state) != {"items_left", "scan_percent", "scan_complete", "scan_errors", "libraries"}:
        raise ObserverError("MQTT inventory state fields were invalid")
    if type(state["items_left"]) is not int or state["items_left"] < 0:
        raise ObserverError("MQTT inventory items-left field was invalid")
    percent = state["scan_percent"]
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        raise ObserverError("MQTT inventory scan-percent field was invalid")
    percent = float(percent)
    if not (percent == percent and 0.0 <= percent <= 100.0):
        raise ObserverError("MQTT inventory scan-percent field was invalid")
    if type(state["scan_complete"]) is not bool:
        raise ObserverError("MQTT inventory scan-complete field was invalid")
    if type(state["scan_errors"]) is not int or state["scan_errors"] < 0:
        raise ObserverError("MQTT inventory scan-errors field was invalid")
    libraries = state["libraries"]
    if not isinstance(libraries, dict) or len(libraries) > MQTT_MAX_LIBRARIES:
        raise ObserverError("MQTT inventory library fields were invalid")
    scanned_total = 0
    media_total = 0
    library_items_left = 0
    for label, item in libraries.items():
        if not isinstance(label, str) or not label or len(label) > 80:
            raise ObserverError("MQTT inventory library fields were invalid")
        safe_label = "".join(
            " " if ord(character) < 32 or ord(character) == 127 else character
            for character in label
        )
        safe_label = " ".join(safe_label.split()).strip()
        if label != safe_label:
            raise ObserverError("MQTT inventory library label was not producer-normalized")
        normalized_label = label.replace("\\", "/")
        if any(
            (kind == "credential" and private in label)
            or (
                kind == "path"
                and (
                    private == label
                    or private == normalized_label
                    or (
                        (private.startswith("/") or re.match(r"^[A-Za-z]:/", private))
                        and normalized_label.startswith(f"{private.rstrip('/')}/")
                    )
                )
            )
            for kind, private in settings.private_values
        ):
            raise ObserverError("MQTT inventory payload leaked a private value")
        if not isinstance(item, dict) or set(item) != {"scanned", "total", "items_left"}:
            raise ObserverError("MQTT inventory library fields were invalid")
        if any(type(item[key]) is not int or item[key] < 0 for key in item):
            raise ObserverError("MQTT inventory library fields were invalid")
        if item["scanned"] > item["total"]:
            raise ObserverError("MQTT inventory library counters were invalid")
        scanned_total += item["scanned"]
        media_total += item["total"]
        library_items_left += item["items_left"]
    if library_items_left != state["items_left"]:
        raise ObserverError("MQTT inventory aggregate item count was invalid")
    expected_percent = (
        100.0
        if state["scan_complete"]
        else 0.0
        if media_total <= 0
        else round(min(100.0, scanned_total * 100.0 / media_total), 1)
    )
    if percent != expected_percent:
        raise ObserverError("MQTT inventory aggregate scan percentage was invalid")


def _validate_mqtt_discovery(payload: bytes, settings: _MqttSettings, *, scan: bool) -> None:
    value = _mqtt_json(payload, "MQTT discovery")
    if value != _mqtt_expected_discovery(settings, scan=scan):
        raise ObserverError("MQTT discovery identity or topic was invalid")


class _MqttObservationState:
    """Payload-free retained/live state used by the journal health projection."""

    def __init__(self, settings: _MqttSettings) -> None:
        self.settings = settings
        self.subscribed = False
        self.availability_retained = False
        self.availability_online = False
        self.state_retained = False
        self.items_discovery_retained = False
        self.scan_discovery_retained = False
        self.last_live_state_ns: int | None = None

    def observe_suback(self) -> None:
        self.subscribed = True

    def observe(self, topic: str, payload: bytes, *, retained: bool, now_ns: int) -> None:
        if topic == self.settings.availability_topic:
            if payload != b"online":
                raise ObserverError("MQTT retained availability was not online")
            self.availability_online = True
            self.availability_retained = self.availability_retained or retained
            return
        if topic == self.settings.items_discovery_topic:
            _validate_mqtt_discovery(payload, self.settings, scan=False)
            self.items_discovery_retained = self.items_discovery_retained or retained
            return
        if topic == self.settings.scan_discovery_topic:
            _validate_mqtt_discovery(payload, self.settings, scan=True)
            self.scan_discovery_retained = self.scan_discovery_retained or retained
            return
        if topic == self.settings.state_topic:
            _validate_mqtt_state(payload, self.settings)
            if retained:
                self.state_retained = True
            else:
                self.last_live_state_ns = now_ns
            return
        raise ObserverError("MQTT broker returned an unexpected topic")

    def health(self, now_ns: int) -> dict[str, Any]:
        if not self.subscribed:
            raise ObserverError("MQTT inventory subscription was missing")
        if not self.availability_retained or not self.availability_online:
            raise ObserverError("MQTT retained availability was missing")
        if not self.items_discovery_retained or not self.scan_discovery_retained:
            raise ObserverError("MQTT retained discovery was missing")
        if not self.state_retained:
            raise ObserverError("MQTT retained state was missing")
        if self.last_live_state_ns is None:
            raise ObserverError("MQTT live state freshness proof was missing")
        age = now_ns - self.last_live_state_ns
        if age < 0 or age > MQTT_MAX_STALE_NS:
            raise ObserverError("MQTT live state exceeded its freshness bound")
        return {
            "enabled": True,
            "availability_retained": True,
            "availability_online": True,
            "discovery_exact_retained": True,
            "state_retained": True,
            "state_fresh": True,
            "state_parseable": True,
            "state_fields_valid": True,
        }


def _mqtt_utf8(value: str, label: str) -> bytes:
    try:
        payload = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ObserverError(f"MQTT {label} was not valid UTF-8") from exc
    if not payload or len(payload) > 65_535 or b"\x00" in payload:
        raise ObserverError(f"MQTT {label} length was invalid")
    return len(payload).to_bytes(2, "big") + payload


def _mqtt_remaining_length(value: int) -> bytes:
    if type(value) is not int or value < 0 or value > 268_435_455:
        raise ObserverError("MQTT packet length was invalid")
    encoded = bytearray()
    while True:
        byte = value % 128
        value //= 128
        if value:
            byte |= 0x80
        encoded.append(byte)
        if not value:
            return bytes(encoded)


def _mqtt_frame(first_byte: int, payload: bytes) -> bytes:
    if not 0 <= first_byte <= 255 or len(payload) > MQTT_MAX_PACKET_BYTES:
        raise ObserverError("MQTT outbound packet was invalid")
    return bytes((first_byte,)) + _mqtt_remaining_length(len(payload)) + payload


def _mqtt_connect_frame(settings: _MqttSettings) -> bytes:
    flags = 0x02
    if settings.username is not None:
        flags |= 0x80
    if settings.password is not None:
        flags |= 0x40
    client_id = f"subgen-soak-{secrets.token_hex(4)}"
    variable = _mqtt_utf8("MQTT", "protocol") + bytes((4, flags)) + (60).to_bytes(2, "big")
    payload = _mqtt_utf8(client_id, "observer client identity")
    if settings.username is not None:
        payload += _mqtt_utf8(settings.username, "username")
    if settings.password is not None:
        payload += _mqtt_utf8(settings.password, "password")
    return _mqtt_frame(0x10, variable + payload)


def _mqtt_subscribe_frame(settings: _MqttSettings) -> bytes:
    topics = (
        settings.availability_topic,
        settings.state_topic,
        settings.items_discovery_topic,
        settings.scan_discovery_topic,
    )
    payload = MQTT_SUBSCRIBE_PACKET_ID.to_bytes(2, "big")
    for topic in topics:
        payload += _mqtt_utf8(topic, "subscription topic") + b"\x01"
    return _mqtt_frame(0x82, payload)


def _mqtt_read_exact(connection: socket.socket, size: int) -> bytes:
    payload = bytearray()
    while len(payload) < size:
        chunk = connection.recv(size - len(payload))
        if not chunk:
            raise ObserverError("MQTT broker connection closed")
        payload.extend(chunk)
    return bytes(payload)


def _mqtt_read_frame(connection: socket.socket) -> tuple[int, bytes]:
    first = _mqtt_read_exact(connection, 1)[0]
    multiplier = 1
    remaining = 0
    for index in range(4):
        byte = _mqtt_read_exact(connection, 1)[0]
        remaining += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            break
        multiplier *= 128
    else:
        raise ObserverError("MQTT remaining length was malformed")
    if remaining > MQTT_MAX_PACKET_BYTES:
        raise ObserverError("MQTT packet exceeded its byte bound")
    return first, _mqtt_read_exact(connection, remaining)


class MqttInventoryProbe:
    """Persistent MQTT v3.1.1 subscriber that retains no application payloads."""

    def __init__(
        self,
        settings: _MqttSettings,
        *,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not settings.enabled:
            raise ObserverError("disabled MQTT inventory cannot open a broker probe")
        self.settings = settings
        self.monotonic_ns = monotonic_ns
        self.state = _MqttObservationState(settings)
        self.condition = threading.Condition()
        self.send_lock = threading.Lock()
        self.connection: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.stopping = False
        self.fault: ObserverError | None = None
        self.received_qos1: dict[int, tuple[str, str, bool]] = {}
        self.last_transmit_ns = monotonic_ns()
        try:
            connection = socket_factory(
                (settings.host, settings.port),
                timeout=MQTT_NETWORK_TIMEOUT_SECONDS,
            )
            self.connection = connection
            connection.settimeout(MQTT_NETWORK_TIMEOUT_SECONDS)
            self._send(_mqtt_connect_frame(settings))
            first, payload = _mqtt_read_frame(connection)
            if first != 0x20 or payload != b"\x00\x00":
                raise ObserverError("MQTT broker rejected the observer connection")
            self._send(_mqtt_subscribe_frame(settings))
            connection.settimeout(1.0)
            self.thread = threading.Thread(
                target=self._reader,
                name="subgen-soak-mqtt-observer",
                daemon=True,
            )
            self.thread.start()
            deadline = time.monotonic() + MQTT_BOOTSTRAP_TIMEOUT_SECONDS
            with self.condition:
                while True:
                    if self.fault is not None:
                        raise self.fault
                    try:
                        self.state.health(self.monotonic_ns())
                        break
                    except ObserverError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ObserverError("MQTT inventory bootstrap was incomplete")
                        self.condition.wait(min(1.0, remaining))
        except BaseException:
            self.close()
            raise

    def _send(self, payload: bytes) -> None:
        connection = self.connection
        if connection is None:
            raise ObserverError("MQTT broker connection was unavailable")
        try:
            with self.send_lock:
                connection.sendall(payload)
                self.last_transmit_ns = self.monotonic_ns()
        except OSError as exc:
            raise ObserverError("MQTT broker write failed") from exc

    def _publish(self, first: int, payload: bytes) -> None:
        qos = (first >> 1) & 0x03
        if qos != 1 or len(payload) < 4:
            raise ObserverError("MQTT broker publication framing was invalid")
        topic_size = int.from_bytes(payload[:2], "big")
        if topic_size <= 0 or len(payload) < 2 + topic_size + 2:
            raise ObserverError("MQTT broker publication topic was invalid")
        try:
            topic = payload[2 : 2 + topic_size].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ObserverError("MQTT broker publication topic was invalid") from exc
        packet_offset = 2 + topic_size
        packet_id = int.from_bytes(payload[packet_offset : packet_offset + 2], "big")
        if packet_id <= 0:
            raise ObserverError("MQTT broker publication identifier was invalid")
        body = payload[packet_offset + 2 :]
        signature = (topic, hashlib.sha256(body).hexdigest(), bool(first & 0x01))
        if first & 0x08:
            if self.received_qos1.get(packet_id) != signature:
                raise ObserverError("MQTT duplicate publication identity was invalid")
            self._send(_mqtt_frame(0x40, packet_id.to_bytes(2, "big")))
            return
        self.state.observe(
            topic,
            body,
            retained=bool(first & 0x01),
            now_ns=self.monotonic_ns(),
        )
        self.received_qos1[packet_id] = signature
        self._send(_mqtt_frame(0x40, packet_id.to_bytes(2, "big")))

    def _consume(self, first: int, payload: bytes) -> None:
        packet_type = first >> 4
        if packet_type == 3:
            self._publish(first, payload)
            return
        if packet_type == 9:
            if (
                first != 0x90
                or len(payload) != 6
                or int.from_bytes(payload[:2], "big") != MQTT_SUBSCRIBE_PACKET_ID
                or payload[2:] != b"\x01\x01\x01\x01"
            ):
                raise ObserverError("MQTT subscription acknowledgement was invalid")
            self.state.observe_suback()
            return
        if packet_type == 13 and first == 0xD0 and not payload:
            return
        raise ObserverError("MQTT broker returned an unexpected control packet")

    def _reader(self) -> None:
        assert self.connection is not None
        while not self.stopping:
            try:
                first, payload = _mqtt_read_frame(self.connection)
                with self.condition:
                    self._consume(first, payload)
                    self.condition.notify_all()
            except socket.timeout:
                continue
            except BaseException as exc:
                with self.condition:
                    if not self.stopping and self.fault is None:
                        self.fault = (
                            exc
                            if isinstance(exc, ObserverError)
                            else ObserverError("MQTT inventory observation failed")
                        )
                    self.condition.notify_all()
                return

    def sample(self) -> dict[str, Any]:
        with self.condition:
            if self.fault is not None:
                raise self.fault
            health = self.state.health(self.monotonic_ns())
        if self.monotonic_ns() - self.last_transmit_ns >= 30_000_000_000:
            self._send(_mqtt_frame(0xC0, b""))
        return health

    def close(self) -> None:
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        connection = self.connection
        self.connection = None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.thread = None


def _read_policy(path: Path, expected_sha256: str, expected_frigate_sha256: str) -> dict[str, Any]:
    payload = _sha_file(path, expected_sha256, 32 * 1024, "priority policy")
    value = _strict_json_bytes(payload, 32 * 1024, "priority policy")
    keys = {"schema", "frigate_version", "detection_fps_limit", "source_max_age_seconds", "cameras", "detectors", "required_embedding_speeds", "conditional_embedding_pairs", "frigate_config_sha256", "gpu_uuid", "nvidia_driver_version", "gpu_index"}
    if not isinstance(value, dict) or set(value) != keys or value.get("schema") != 1 or value.get("source_max_age_seconds") != 30 or value.get("frigate_config_sha256") != expected_frigate_sha256 or _canonical_bytes(value) != payload:
        raise ObserverError("priority policy contract changed")
    cameras = value["cameras"]; detectors = value["detectors"]; embeddings = value["required_embedding_speeds"]
    if not isinstance(cameras, dict) or not 1 <= len(cameras) <= 128 or list(cameras) != sorted(cameras) or any(IDENTIFIER_RE.fullmatch(str(key)) is None or type(item) is not float or not 0 < item <= 60 for key, item in cameras.items()):
        raise ObserverError("priority policy camera contract changed")
    for collection, label in ((detectors, "detectors"), (embeddings, "embeddings")):
        if not isinstance(collection, list) or not collection or collection != sorted(set(collection)) or any(not isinstance(item, str) or IDENTIFIER_RE.fullmatch(item) is None for item in collection):
            raise ObserverError(f"priority policy {label} changed")
    return value


def _finite(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObserverError(f"{label} was invalid")
    number = float(value)
    if not (number == number and abs(number) != float("inf")) or (positive and number <= 0):
        raise ObserverError(f"{label} was invalid")
    return number


def _validate_host_memory(meminfo: bytes, psi: bytes, reserve_bytes: int) -> None:
    try: lines = meminfo.decode("ascii").splitlines()
    except UnicodeDecodeError as exc: raise ObserverError("host memory telemetry was malformed") from exc
    available = [line for line in lines if line.startswith("MemAvailable:")]
    if len(available) != 1 or re.fullmatch(r"MemAvailable:\s+([0-9]+) kB", available[0]) is None: raise ObserverError("host MemAvailable was unavailable")
    match = re.fullmatch(r"MemAvailable:\s+([0-9]+) kB", available[0]); assert match is not None
    if int(match.group(1)) * 1024 < reserve_bytes: raise ObserverError("host memory reserve was breached")
    try: pressure = psi.decode("ascii").splitlines()
    except UnicodeDecodeError as exc: raise ObserverError("host memory PSI was malformed") from exc
    pattern = re.compile(r"^(some|full) avg10=([0-9]+\.[0-9]{2}) avg60=([0-9]+\.[0-9]{2}) avg300=([0-9]+\.[0-9]{2}) total=([0-9]+)$")
    matches = [pattern.fullmatch(line) for line in pressure]
    if not matches or any(item is None for item in matches) or {item.group(1) for item in matches if item is not None} != {"some", "full"}: raise ObserverError("host memory PSI was malformed")


def _validate_nvidia(payload: bytes, *, gpu_uuid: str, total_bytes: int, free_reserve_bytes: int) -> None:
    try: rows = payload.decode("ascii").strip().splitlines()
    except UnicodeDecodeError as exc: raise ObserverError("NVIDIA telemetry was malformed") from exc
    if len(rows) != 1: raise ObserverError("NVIDIA GPU topology changed")
    fields = [item.strip() for item in rows[0].split(",")]
    if len(fields) != 3 or fields[0] != gpu_uuid or not fields[1].isdigit() or not fields[2].isdigit(): raise ObserverError("NVIDIA telemetry was malformed")
    observed_total = int(fields[1]) * 1024**2; observed_free = int(fields[2]) * 1024**2
    if observed_total != total_bytes or observed_free > observed_total or observed_free < free_reserve_bytes: raise ObserverError("NVIDIA identity or free-memory reserve changed")


def _validate_ollama(value: dict[str, Any]) -> None:
    if set(value) != {"models"} or not isinstance(value["models"], list) or value["models"]: raise ObserverError("Ollama had a loaded model or invalid state")


class PrioritySignalTracker:
    KEYS = {"schema", "boot_id_sha256", "producer_epoch", "sequence", "observed_monotonic_ns", "source_generation", "source_observed_monotonic_ns", "observation_id", "policy_sha256", "pressure", "clear_eligible", "reason_codes"}
    REASONS = {"higher_priority_busy", "higher_priority_degraded", "higher_priority_unavailable", "policy_drift"}

    def __init__(self) -> None:
        self.epoch: str | None = None; self.sequence: int | None = None; self.payload_sha256: str | None = None
        self.source_generation: int | None = None; self.source_observed_ns: int | None = None

    def observe(self, payload: bytes, *, now_ns: int, boot_sha256: str, policy_sha256: str) -> None:
        value = _strict_json_bytes(payload, 4096, "priority signal")
        if not isinstance(value, dict) or set(value) != self.KEYS or _canonical_bytes(value) != payload or value["schema"] != 1: raise ObserverError("priority signal contract changed")
        if value["boot_id_sha256"] != boot_sha256 or value["policy_sha256"] != policy_sha256 or not isinstance(value["producer_epoch"], str) or ev.HEX32.fullmatch(value["producer_epoch"]) is None or not isinstance(value["observation_id"], str) or ev.HEX64.fullmatch(value["observation_id"]) is None: raise ObserverError("priority signal identity changed")
        integer_keys = ("sequence", "observed_monotonic_ns", "source_generation", "source_observed_monotonic_ns")
        if any(type(value[key]) is not int or value[key] <= 0 for key in integer_keys) or not value["source_observed_monotonic_ns"] <= value["observed_monotonic_ns"] <= now_ns: raise ObserverError("priority signal time or sequence was invalid")
        if now_ns - value["observed_monotonic_ns"] > 10_000_000_000 or now_ns - value["source_observed_monotonic_ns"] > 30_000_000_000: raise ObserverError("priority signal was stale")
        reasons = value["reason_codes"]
        if type(value["pressure"]) is not bool or type(value["clear_eligible"]) is not bool or not isinstance(reasons, list) or reasons != sorted(set(reasons)) or any(item not in self.REASONS for item in reasons) or (value["pressure"] and (value["clear_eligible"] or not reasons)) or (not value["pressure"] and value["clear_eligible"] and reasons) or (not value["pressure"] and not value["clear_eligible"] and reasons): raise ObserverError("priority signal state was invalid")
        digest = hashlib.sha256(payload).hexdigest()
        if self.epoch is not None:
            if value["producer_epoch"] != self.epoch or value["sequence"] not in {self.sequence, self.sequence + 1}: raise ObserverError("priority producer epoch or sequence changed discontinuously")
            if value["sequence"] == self.sequence and digest != self.payload_sha256: raise ObserverError("priority signal mutated at one sequence")
            if value["sequence"] > self.sequence and (value["source_generation"] < self.source_generation or value["source_observed_monotonic_ns"] < self.source_observed_ns): raise ObserverError("priority source telemetry regressed")
        self.epoch = value["producer_epoch"]; self.sequence = value["sequence"]; self.payload_sha256 = digest
        self.source_generation = value["source_generation"]; self.source_observed_ns = value["source_observed_monotonic_ns"]


class PriorityPhaseTracker:
    def __init__(self, phase: str, now_ns: int) -> None:
        if phase not in {"normal", "yielding", "recovering"}: raise ObserverError("priority controller phase was invalid")
        self.non_normal_since_ns = now_ns if phase != "normal" else None

    def observe(self, phase: str, now_ns: int, *, final: bool = False) -> None:
        if phase not in {"normal", "yielding", "recovering"} or type(now_ns) is not int or now_ns < 0:
            raise ObserverError("priority controller phase was invalid")
        if phase == "normal": self.non_normal_since_ns = None
        elif self.non_normal_since_ns is None: self.non_normal_since_ns = now_ns
        elif now_ns - self.non_normal_since_ns > ev.MAX_NON_NORMAL_NS:
            raise ObserverError("priority controller remained non-normal")
        if final and phase != "normal": raise ObserverError("final priority controller phase was not normal")


def _validate_frigate_stats(stats: dict[str, Any], policy: dict[str, Any], low_since: dict[str, float | None], now_mono: float) -> None:
    cameras = stats.get("cameras")
    expected = policy["cameras"]
    if not isinstance(cameras, dict) or set(cameras) != set(expected):
        raise ObserverError("Frigate camera set changed")
    for name, fps in expected.items():
        item = cameras.get(name)
        if not isinstance(item, dict): raise ObserverError("Frigate camera telemetry was invalid")
        ratio = _finite(item.get("process_fps"), "Frigate process FPS") / fps
        if _finite(item.get("skipped_fps"), "Frigate skipped FPS") > 0.5: raise ObserverError("Frigate skipped FPS breached")
        if ratio < 0.9:
            low_since[name] = low_since[name] or now_mono
            if now_mono - float(low_since[name]) > 30: raise ObserverError("Frigate process FPS stayed low")
        else: low_since[name] = None
    detectors = stats.get("detectors")
    if not isinstance(detectors, dict) or set(detectors) != set(policy["detectors"]): raise ObserverError("Frigate detector topology changed")
    for name in policy["detectors"]:
        if not isinstance(detectors[name], dict): raise ObserverError("Frigate detector telemetry was invalid")
        _finite(detectors[name].get("inference_speed"), "Frigate detector speed", positive=True)
    embedding = stats.get("embeddings")
    if not isinstance(embedding, dict): raise ObserverError("Frigate embedding telemetry was invalid")
    for name in policy["required_embedding_speeds"]: _finite(embedding.get(name), "Frigate embedding speed", positive=True)
    service = stats.get("service")
    if not isinstance(service, dict): raise ObserverError("Frigate service telemetry was invalid")
    updated = _finite(service.get("last_updated"), "Frigate update time")
    now_wall = time.time()
    if updated > now_wall + 5 or now_wall - updated > 30: raise ObserverError("Frigate telemetry was stale")


def _systemctl_show(unit: str) -> dict[str, str]:
    if unit not in {MONITOR_UNIT, PRIORITY_UNIT, REPAIR_SERVICE, REPAIR_TIMER}: raise ObserverError("systemd unit selector was invalid")
    fields = SYSTEMD_BASE_FIELDS + (("Unit",) if unit == REPAIR_TIMER else SYSTEMD_SERVICE_STATE_FIELDS + SYSTEMD_EXEC_FIELDS + SYSTEMD_EXEC_CONTEXT_FIELDS)
    payload = _command([SYSTEMCTL_BINARY, "show", "--all", unit, *[f"--property={item}" for item in fields]], f"{unit} state", maximum=256 * 1024)
    result: dict[str, str] = {}
    try: text_payload = payload.decode("utf-8")
    except UnicodeDecodeError as exc: raise ObserverError("systemd state was not UTF-8") from exc
    for line in text_payload.splitlines():
        if "=" not in line: raise ObserverError("systemd state framing was invalid")
        key, value = line.split("=", 1)
        if key in result: raise ObserverError("systemd state duplicated a property")
        result[key] = value
    if set(result) != set(fields) or result["Id"] != unit: raise ObserverError("systemd state was incomplete")
    normalized = {key: "" for key in SYSTEMD_SHOW_FIELDS}; normalized.update(result)
    return normalized


def _systemd_environment_files(raw: str) -> list[tuple[Path, bool]]:
    try: tokens = shlex.split(raw, posix=True)
    except ValueError as exc: raise ObserverError("systemd environment-file list was malformed") from exc
    if len(tokens) % 2: raise ObserverError("systemd environment-file list was malformed")
    result: list[tuple[Path, bool]] = []
    for index in range(0, len(tokens), 2):
        path = _absolute(Path(tokens[index]), "systemd environment file")
        marker = tokens[index + 1]
        if marker not in {"(ignore_errors=yes)", "(ignore_errors=no)"}: raise ObserverError("systemd environment-file list was malformed")
        result.append((path, marker == "(ignore_errors=yes)"))
    return result


def _systemd_environment_assignments(raw: str, label: str) -> dict[str, str]:
    try: tokens = shlex.split(raw, posix=True)
    except ValueError as exc: raise ObserverError(f"{label} was malformed") from exc
    result: dict[str, str] = {}
    for token in tokens:
        if "=" not in token: raise ObserverError(f"{label} was malformed")
        key, value = token.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None or key in result: raise ObserverError(f"{label} was malformed")
        result[key] = value
    return result


def _systemd_boundary(states: dict[str, dict[str, str]]) -> str:
    paths = {MONITOR_ENV_PATH, MONITOR_HELPER_PATH, REPAIR_HELPER_PATH, PRIORITY_ENV_PATH, PRIORITY_HELPER_PATH}
    for state in states.values():
        fragment = Path(state["FragmentPath"])
        if not fragment.is_absolute(): raise ObserverError("systemd fragment path was invalid")
        paths.add(fragment)
        if state["DropInPaths"]:
            for item in state["DropInPaths"].split(): paths.add(_absolute(Path(item), "systemd drop-in"))
        for environment_path, _ignore_missing in _systemd_environment_files(state["EnvironmentFiles"]): paths.add(environment_path)
    files = []
    for path in sorted(paths, key=str):
        payload = _read_optional_bound(path, MAX_COMMAND_BYTES, "systemd boundary file")
        if payload is None: raise ObserverError("systemd boundary file was unavailable")
        files.append({"path_sha256": hashlib.sha256(str(path).encode("utf-8")).hexdigest(), "content_sha256": hashlib.sha256(payload).hexdigest()})
    static_fields = (
        "Id", "FragmentPath", "DropInPaths", *SYSTEMD_EXEC_FIELDS,
        "WorkingDirectory", "Environment", "EnvironmentFiles", "PassEnvironment",
        "UnsetEnvironment", "UnitFileState", "Unit",
    )
    static_states = {unit: {key: state[key] for key in static_fields} for unit, state in states.items()}
    return hashlib.sha256(_canonical_bytes({"states": static_states, "files": files})).hexdigest()


def _process_environment(pid: int) -> dict[str, str]:
    if type(pid) is not int or pid <= 0: raise ObserverError("monitor process ID was invalid")
    payload = _read_optional_bound(Path(f"/proc/{pid}/environ"), 1024**2, "monitor process environment")
    if payload is None or not payload.endswith(b"\x00"): raise ObserverError("monitor process environment was unavailable")
    result: dict[str, str] = {}
    for raw in payload.split(b"\x00")[:-1]:
        try: entry = raw.decode("utf-8")
        except UnicodeDecodeError as exc: raise ObserverError("monitor process environment was malformed") from exc
        if "=" not in entry: raise ObserverError("monitor process environment was malformed")
        key, value = entry.split("=", 1)
        if key in result: raise ObserverError("monitor process environment was duplicated")
        result[key] = value
    return result


def _environment_file(path: Path) -> dict[str, str]:
    payload = _read_optional_bound(path, 1024**2, "systemd environment file")
    if payload is None: raise ObserverError("systemd environment file was unavailable")
    try: lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc: raise ObserverError("systemd environment file was malformed") from exc
    result: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"): continue
        if "=" not in line: raise ObserverError("systemd environment file was malformed")
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None or key in result or any(char in value for char in "\r\n\x00"):
            raise ObserverError("systemd environment file was malformed")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}: value = value[1:-1]
        result[key] = value
    return result


def _validate_repair_units(states: dict[str, dict[str, str]]) -> None:
    repair = states[REPAIR_SERVICE]; timer = states[REPAIR_TIMER]
    expected_env = {
        "AUTO_DELETE_INVALID_MEDIA": "false", "AUTO_DELETE_FAILED_FILES": "false",
        "AUTO_MARK_FAILED_FILES": "true", "AUTO_MARK_MIN_FAILURES": "1",
        "SUBGEN_REPAIR_ACTION": "report",
    }
    try: pass_environment = set(shlex.split(repair["PassEnvironment"], posix=True)); unset_environment = shlex.split(repair["UnsetEnvironment"], posix=True)
    except ValueError as exc: raise ObserverError("repair effective environment was malformed") from exc
    if pass_environment & set(expected_env): raise ObserverError("repair effective environment was unsafe")
    environment = _systemd_environment_assignments(repair["Environment"], "repair inline environment")
    environment_files = _systemd_environment_files(repair["EnvironmentFiles"])
    if MONITOR_ENV_PATH not in {path for path, _ignore in environment_files}: raise ObserverError("repair environment-file boundary was incomplete")
    for path, ignore_missing in environment_files:
        try: layer = _environment_file(path)
        except ObserverError:
            if ignore_missing and not path.exists(): continue
            raise
        environment.update(layer)
    for token in unset_environment:
        key, separator, value = token.partition("=")
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None: raise ObserverError("repair unset environment was malformed")
        if not separator or environment.get(key) == value: environment.pop(key, None)
    if any(environment.get(key, "").lower() != value for key, value in expected_env.items()):
        raise ObserverError("repair effective environment was unsafe")
    match = re.match(r"^\{\s*path=([^ ;]+)\s*;\s*argv\[\]=([^;]+?)\s*;", repair["ExecStart"])
    try: argv = shlex.split(match.group(2), posix=True) if match is not None else []
    except ValueError as exc: raise ObserverError("repair effective command was unsafe") from exc
    if match is None or match.group(1) != "/usr/bin/python3" or argv != ["/usr/bin/python3", REPAIR_HELPER_PATH.as_posix()] or repair["ExecStart"].count("argv[]=") != 1 or repair["WorkingDirectory"] != "/opt/subgen":
        raise ObserverError("repair effective command was unsafe")
    if any(repair[key] for key in SYSTEMD_EXEC_FIELDS if key != "ExecStart"):
        raise ObserverError("repair auxiliary command was unsafe")
    repair_pid = int(repair["MainPID"]) if repair["MainPID"].isdigit() else -1
    inactive = repair["ActiveState"] == "inactive" and repair["SubState"] == "dead" and repair_pid == 0
    running = repair["ActiveState"] == "active" and repair["SubState"] == "running" and repair_pid > 0
    if not inactive and not running:
        raise ObserverError("repair service state was unsafe")
    if (
        timer["ActiveState"] != "active"
        or timer["SubState"] != "waiting"
        or timer["Unit"] != REPAIR_SERVICE
        or any(timer[key] for key in (*SYSTEMD_EXEC_FIELDS, "WorkingDirectory", "Environment", "EnvironmentFiles", "PassEnvironment", "UnsetEnvironment"))
    ):
        raise ObserverError("repair timer state was unsafe")
    if running:
        process_environment = _process_environment(repair_pid)
        if any(process_environment.get(key, "").lower() != value for key, value in expected_env.items()):
            raise ObserverError("running repair process environment was unsafe")


class StreamFollower:
    """Continuously parse bounded streams and retain only counters/events."""

    def __init__(self, kind: str, container_id: str | None, since: str, *, until: str | None = None) -> None:
        if kind not in {"candidate", "frigate", "kernel"}: raise ObserverError("stream kind was invalid")
        if kind != "kernel" and (container_id is None or CONTAINER_RE.fullmatch(container_id) is None): raise ObserverError("stream container binding was invalid")
        self.kind = kind; self.start_ns = ev._utc(since, "stream start"); self.end_ns = ev._utc(until, "stream end") if until is not None else None
        if self.end_ns is not None and self.end_ns < self.start_ns: raise ObserverError("stream window was invalid")
        self.bounded = until is not None
        if kind == "kernel":
            argv = [JOURNALCTL_BINARY, "--dmesg", "--boot=0", "--output=json", "--since", since]
            if until is None: argv.append("--follow")
            else: argv.extend(("--until", until))
            argv.append("--no-pager")
        else:
            argv = [DOCKER_BINARY, "--host", DOCKER_HOST, "logs"]
            if until is None: argv.append("--follow")
            argv.extend(("--timestamps", "--since", since))
            if until is not None: argv.extend(("--until", until))
            argv.append(str(container_id))
        self.lock = threading.Lock(); self.stopping = False
        self.events: queue.Queue[tuple[bytes, dict[str, Any], int, str]] = queue.Queue(EVENT_QUEUE_SIZE)
        self.fault: ObserverError | None = None
        self.evidence = {key: set() for key in ("candidate_cuda_oom_log", "transcription_failure", "join_failure", "partial_output", "media_deleted", "marker_skipped", "frigate_health_breach", "nvidia_xid")}
        safe_env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": "/"}
        try: self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=safe_env)
        except OSError as exc: raise ObserverError(f"{kind} stream could not start") from exc
        assert self.process.stdout is not None and self.process.stderr is not None
        self.stdout_thread = threading.Thread(target=self._read_container_pipe, args=(self.process.stdout, "stdout"), name=f"soak-{kind}-stdout", daemon=True)
        target = self._read_diagnostic_stderr if kind == "kernel" else self._read_container_pipe
        arguments: tuple[Any, ...] = () if kind == "kernel" else (self.process.stderr, "stderr")
        self.stderr_thread = threading.Thread(target=target, args=arguments, name=f"soak-{kind}-stderr", daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _fail(self, message: str) -> None:
        with self.lock:
            if not self.stopping and self.fault is None: self.fault = ObserverError(message)

    def _record(self, key: str, line: bytes, source: str, count: int = 1) -> None:
        if not count: return
        with self.lock:
            target = self.evidence[key]
            for index in range(count):
                target.add(hashlib.sha256(source.encode("ascii") + b"\0" + key.encode("ascii") + b"\0" + str(index).encode("ascii") + b"\0" + line).hexdigest())
            if sum(len(items) for items in self.evidence.values()) > MAX_RELEVANT_LOG_EVIDENCE:
                raise ObserverError("relevant log evidence exceeded its memory bound")

    def _read_container_pipe(self, pipe: Any, source: str) -> None:
        try:
            while True:
                line = pipe.readline(MAX_LOG_LINE_BYTES + 1)
                if not line:
                    if not self.bounded and not self.stopping: self._fail(f"{self.kind} {source} stream ended unexpectedly")
                    return
                if len(line) > MAX_LOG_LINE_BYTES or not line.endswith(b"\n"):
                    self._fail(f"{self.kind} stream line was oversized or partial"); return
                try: self._consume(line, source)
                except ObserverError as exc: self._fail(str(exc)); return
        except BaseException:
            self._fail(f"{self.kind} stream reader failed")

    def _read_diagnostic_stderr(self) -> None:
        assert self.process.stderr is not None
        try:
            byte = self.process.stderr.read(1)
            if byte: self._fail(f"{self.kind} stream wrote stderr")
        except BaseException: self._fail(f"{self.kind} stderr reader failed")

    def _consume(self, line: bytes, source: str) -> None:
        if self.kind == "kernel":
            value = _strict_json_bytes(line, MAX_LOG_LINE_BYTES, "kernel journal line")
            timestamp = value.get("__REALTIME_TIMESTAMP") if isinstance(value, dict) else None
            cursor = value.get("__CURSOR") if isinstance(value, dict) else None
            if not isinstance(value, dict) or not isinstance(value.get("MESSAGE"), str) or not isinstance(timestamp, str) or not timestamp.isdigit() or not isinstance(cursor, str) or not cursor: raise ObserverError("kernel journal line was invalid")
            source_ns = int(timestamp) * 1000
            if source_ns < self.start_ns or (self.end_ns is not None and source_ns > self.end_ns): raise ObserverError("kernel journal timestamp was outside its window")
            self._record("nvidia_xid", line, source, len(XID_RE.findall(value["MESSAGE"]))); return
        outer = DOCKER_TIMESTAMP_RE.fullmatch(line)
        source_ns = _timestamp_ns(outer.group(1), f"{self.kind} Docker log") if outer is not None else -1
        if outer is None or source_ns < self.start_ns or (self.end_ns is not None and source_ns > self.end_ns): raise ObserverError(f"{self.kind} Docker log framing failed")
        application = outer.group(2)
        if self.kind == "candidate":
            parsed = parse_runtime_event(line, started_utc_ns=self.start_ns)
            if parsed is not None:
                if source != "stderr": raise ObserverError("runtime event did not originate from candidate stderr")
                event, source_ns = parsed
                try: self.events.put_nowait((line, event, source_ns, source))
                except queue.Full as exc: raise ObserverError("runtime event queue overflowed") from exc
            self._record("candidate_cuda_oom_log", line, source, len(CUDA_OOM_RE.findall(application)))
            self._record("transcription_failure", line, source, len(TRANSCRIPTION_FAILURE_RE.findall(application)))
            self._record("join_failure", line, source, len(JOIN_FAILURE_RE.findall(application)))
            self._record("partial_output", line, source, len(PARTIAL_OUTPUT_RE.findall(application)))
            if b"Skipping marked failed file" in application: self._record("marker_skipped", line, source)
            if re.search(rb"\bFILE_DELETED(?:_RECOVERED)?\b|Removed by monitor", application): self._record("media_deleted", line, source)
        else:
            any_frame = APP_ANY_PREFIX_RE.fullmatch(application)
            if (any_frame is not None and any_frame.group(1).startswith(b"SUBGEN_RUNTIME_EVENT")) or application.startswith(b"SUBGEN_RUNTIME_EVENT"):
                raise ObserverError("Frigate emitted a Subgen runtime sentinel")
            self._record("frigate_health_breach", line, source, len(FRIGATE_FAILURE_RE.findall(application)))

    def drain(self) -> tuple[list[tuple[bytes, dict[str, Any], int, str]], dict[str, set[str]]]:
        with self.lock:
            if self.fault is not None: raise self.fault
            evidence = {key: set(items) for key, items in self.evidence.items()}
        events = []
        while True:
            try: events.append(self.events.get_nowait())
            except queue.Empty: break
        return events, evidence

    def wait_complete(self, timeout: float = 5.0) -> tuple[list[tuple[bytes, dict[str, Any], int, str]], dict[str, set[str]]]:
        if not self.bounded: raise ObserverError("only a bounded stream can be awaited")
        try: return_code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self.close(); raise ObserverError(f"{self.kind} bounded stream timed out") from exc
        self.stdout_thread.join(timeout=1); self.stderr_thread.join(timeout=1)
        if self.stdout_thread.is_alive() or self.stderr_thread.is_alive() or return_code != 0: raise ObserverError(f"{self.kind} bounded stream failed")
        return self.drain()

    def close(self) -> None:
        with self.lock: self.stopping = True
        if self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=5)
        self.stdout_thread.join(timeout=1)
        self.stderr_thread.join(timeout=1)
        for pipe in (self.process.stdout, self.process.stderr):
            if pipe is not None: pipe.close()


class AuditTracker:
    def __init__(self) -> None:
        try: metadata = MONITOR_AUDIT_PATH.lstat()
        except OSError as exc: raise ObserverError("monitor audit log was unavailable") from exc
        if not stat.S_ISREG(metadata.st_mode) or MONITOR_AUDIT_PATH.is_symlink(): raise ObserverError("monitor audit log was unsafe")
        self.identity = (metadata.st_dev, metadata.st_ino); self.offset = metadata.st_size; self.partial = b""

    def sample(self) -> dict[str, int]:
        try: metadata = MONITOR_AUDIT_PATH.lstat()
        except OSError as exc: raise ObserverError("monitor audit log disappeared") from exc
        target_size = metadata.st_size
        if not stat.S_ISREG(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != self.identity or target_size < self.offset: raise ObserverError("monitor audit log rotated or regressed")
        if target_size - self.offset > MAX_AUDIT_DELTA_BYTES: raise ObserverError("monitor audit delta exceeded its bound")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try: fd = os.open(MONITOR_AUDIT_PATH, flags)
        except OSError as exc: raise ObserverError("monitor audit log could not be opened") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != self.identity or opened.st_size < target_size: raise ObserverError("monitor audit open identity changed")
            os.lseek(fd, self.offset, os.SEEK_SET); remaining = target_size - self.offset; chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk: raise ObserverError("monitor audit read was incomplete")
                chunks.append(chunk); remaining -= len(chunk)
            after = os.fstat(fd)
            if (after.st_dev, after.st_ino) != self.identity or after.st_size < target_size: raise ObserverError("monitor audit identity changed during read")
        finally: os.close(fd)
        try: path_after = MONITOR_AUDIT_PATH.lstat()
        except OSError as exc: raise ObserverError("monitor audit log disappeared after read") from exc
        if (path_after.st_dev, path_after.st_ino) != self.identity or path_after.st_size < target_size: raise ObserverError("monitor audit path changed during read")
        self.offset = target_size; payload = self.partial + b"".join(chunks)
        lines = payload.splitlines(keepends=True); self.partial = b""
        if lines and not lines[-1].endswith(b"\n"): self.partial = lines.pop()
        if len(self.partial) > MAX_LOG_LINE_BYTES: raise ObserverError("monitor audit line was oversized")
        counts = {"marker_created": 0, "marker_handled": 0, "media_deleted": 0}
        for line in lines:
            match = AUDIT_RE.match(line)
            if match is None: raise ObserverError("monitor audit line framing was invalid")
            kind = match.group(1).decode("ascii")
            if kind == "FAILURE_MARKER_CREATED": counts["marker_created"] += 1
            if kind in {"FAILURE_MARKER_CREATED", "FAILURE_MARKER_REFRESHED"}: counts["marker_handled"] += 1
            if kind.startswith("FILE_DELETED"): counts["media_deleted"] += 1
        return counts


class LiveProbe:
    """Own every live input used to synthesize privacy-safe health samples."""

    def __init__(self, config: dict[str, Any], identities: dict[str, Any], journal_parent: Path) -> None:
        if os.name != "posix" or any(os.environ.get(name) for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")):
            raise ObserverError("live observation requires an unredirected POSIX Docker host")
        for path, label, kind in ((Path(DOCKER_BINARY), "Docker binary", stat.S_ISREG), (Path(JOURNALCTL_BINARY), "journalctl binary", stat.S_ISREG), (Path(SYSTEMCTL_BINARY), "systemctl binary", stat.S_ISREG), (Path(NVIDIA_BINARY), "NVIDIA binary", stat.S_ISREG)):
            try: metadata = path.lstat()
            except OSError as exc: raise ObserverError(f"{label} was unavailable") from exc
            if path.is_symlink() or not kind(metadata.st_mode) or metadata.st_mode & 0o022: raise ObserverError(f"{label} was unsafe")
        try: socket_metadata = os.lstat(DOCKER_SOCKET)
        except OSError as exc: raise ObserverError("local Docker socket was unavailable") from exc
        if not stat.S_ISSOCK(socket_metadata.st_mode): raise ObserverError("local Docker endpoint was not a socket")
        self.config = config; self.ids = identities; self.live = config["live"]; self.journal_parent = journal_parent
        self.candidate_id = self.live["candidate_container_id"]; self.frigate_id = self.live["frigate_container_id"]
        self.mqtt_probe: MqttInventoryProbe | None = None
        self.counters = {key: 0 for key in ev.COUNTER_KEYS}; self.low_since: dict[str, float | None] = {}
        self.followers: list[StreamFollower] = []; self.stream_started_utc: str | None = None; self.replay_since_utc: str | None = None
        self.log_evidence = {key: set() for key in ("candidate_cuda_oom_log", "transcription_failure", "join_failure", "partial_output", "media_deleted", "marker_skipped", "frigate_health_breach", "nvidia_xid")}
        self.audit = AuditTracker(); self.marker_hashes = self._marker_hashes()
        self._verify_files_and_policy(); candidate, frigate, mqtt_settings = self._inspect_bound_containers()
        candidate_state = _state(candidate, "candidate"); frigate_state = _state(frigate, "Frigate")
        if not candidate_state[0] or candidate_state[1] or candidate_state[4] <= 0: raise ObserverError("candidate was not initially healthy")
        if not frigate_state[0] or frigate_state[1] or frigate_state[3] != "healthy" or frigate_state[4] <= 0: raise ObserverError("Frigate was not initially healthy")
        self.candidate_restart = candidate_state[2]; self.frigate_restart = frigate_state[2]; self.candidate_pid = candidate_state[4]
        self.cgroup_path, self.cgroup_baseline = self._cgroup_snapshot(self.candidate_pid, expected_path=None)
        resource = self._candidate_status(); self.runtime_epoch = resource["runtime_identity"]["epoch"]
        self.runtime_started = resource["runtime_identity"]["started_monotonic_ns"]
        self.cuda_generation = resource["failure_counters"]["cuda_oom_generation"]
        self.media_generation = resource["failure_counters"]["media_failure_generation"]
        self.previous_phase = resource["priority_pressure"]["controller_phase"]
        self.phase_tracker = PriorityPhaseTracker(self.previous_phase, time.monotonic_ns())
        self.monitor_baseline = self._verify_systemd(); self.priority_signal = PrioritySignalTracker(); self._observe_priority_signal()
        root = _docker_json("info", "--format", "{{json .DockerRootDir}}", label="Docker root directory")
        if not isinstance(root, str): raise ObserverError("Docker root directory was invalid")
        self.docker_root = _absolute(Path(root), "Docker root directory").resolve(strict=True)
        if not self.docker_root.is_dir(): raise ObserverError("Docker root directory was invalid")
        if mqtt_settings.enabled:
            self.mqtt_probe = MqttInventoryProbe(mqtt_settings)

    def _space(self, path: Path, label: str, minimum: int) -> None:
        try: values = os.statvfs(path)
        except OSError as exc: raise ObserverError(f"{label} free space was unavailable") from exc
        if values.f_bavail * values.f_frsize < minimum: raise ObserverError(f"{label} free space floor was breached")

    def _verify_files_and_policy(self) -> dict[str, Any]:
        model = self.ids["model"]; config = self.ids["configuration"]
        _sha_file(Path(self.live["model_catalog_path"]), model["catalog_sha256"], MAX_COMMAND_BYTES, "model catalog")
        _sha_file(Path(self.live["model_identity_path"]), model["model_identity_sha256"], MAX_COMMAND_BYTES, "model identity")
        _sha_file(Path(self.live["frigate_config_path"]), config["frigate_config_sha256"], MAX_COMMAND_BYTES, "Frigate configuration")
        policy = _read_policy(Path(self.live["priority_policy_path"]), config["policy_sha256"], config["frigate_config_sha256"])
        if policy.get("gpu_uuid") != self.live["gpu_uuid"]: raise ObserverError("priority policy GPU identity changed")
        self.low_since = {name: self.low_since.get(name) for name in policy["cameras"]}
        return policy

    def _docker_identity(self) -> None:
        daemon = _docker_json("info", "--format", "{{json .ID}}", label="Docker daemon identity")
        try: boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except OSError as exc: raise ObserverError("host boot identity was unavailable") from exc
        if daemon != self.live["docker_daemon_id"] or boot != self.live["host_boot_id"]: raise ObserverError("host boot or Docker daemon identity changed")

    def _inspect_bound_containers(self) -> tuple[dict[str, Any], dict[str, Any], _MqttSettings]:
        self._docker_identity()
        candidate = _docker_json("inspect", "--type", "container", "--format", "{{json .}}", self.candidate_id, label="candidate inspection")
        frigate = _docker_json("inspect", "--type", "container", "--format", "{{json .}}", self.frigate_id, label="Frigate inspection")
        if not isinstance(candidate, dict) or candidate.get("Id") != self.candidate_id or candidate.get("Image") != self.ids["image"]["config_digest"]: raise ObserverError("candidate immutable identity changed")
        if not isinstance(frigate, dict) or frigate.get("Id") != self.frigate_id: raise ObserverError("Frigate immutable identity changed")
        if hashlib.sha256(_canonical_bytes(_container_boundary(candidate))).hexdigest() != self.live["candidate_runtime_config_sha256"]: raise ObserverError("candidate runtime configuration changed")
        if hashlib.sha256(_canonical_bytes(_container_boundary(frigate))).hexdigest() != self.live["frigate_runtime_config_sha256"]: raise ObserverError("Frigate runtime configuration changed")
        layers = _docker_json("image", "inspect", "--format", "{{json .RootFS.Layers}}", self.ids["image"]["config_digest"], label="image layer inspection")
        if not isinstance(layers, list) or not layers or any(not isinstance(item, str) or ev.OCI.fullmatch(item) is None for item in layers) or ev.canonical_sha(layers) != self.ids["image"]["layer_diff_ids_sha256"]: raise ObserverError("candidate ordered layer identity changed")
        environment = _environment(candidate, "candidate")
        for key in ("AUTO_DELETE_INVALID_MEDIA", "AUTO_DELETE_FAILED_FILES"):
            if key in environment and environment[key].lower() != "false": raise ObserverError("candidate deletion setting was enabled")
        mqtt_settings = _mqtt_settings_from_environment(environment)
        if mqtt_settings.binding != self.live["mqtt_inventory"]:
            raise ObserverError("MQTT semantic configuration changed")
        return candidate, frigate, mqtt_settings

    def _candidate_status(self) -> dict[str, Any]:
        payload = _fetch_loopback(19000, CANDIDATE_STATUS_PATH, "candidate status")
        resource = payload.get("resource_management")
        if not isinstance(resource, dict): raise ObserverError("candidate resource status was unavailable")
        for key in ("selected_model", "envelope_key", "priority_pressure", "workload", "runtime_identity", "failure_counters"):
            if key not in resource: raise ObserverError("candidate resource status was incomplete")
        if resource["selected_model"] != self.ids["model"]["selected_model"]: raise ObserverError("candidate selected model changed")
        envelope = resource["envelope_key"]; priority = resource["priority_pressure"]
        workload = resource["workload"]; runtime = resource["runtime_identity"]; failures = resource["failure_counters"]
        catalog = envelope.get("catalog_payload_sha256") if isinstance(envelope, dict) else None
        if catalog not in {self.ids["model"]["catalog_sha256"], f"sha256:{self.ids['model']['catalog_sha256']}"}: raise ObserverError("candidate model catalog changed")
        if not isinstance(priority, dict) or priority.get("policy_sha256") != self.ids["configuration"]["policy_sha256"] or priority.get("controller_phase") not in {"normal", "yielding", "recovering"}: raise ObserverError("candidate priority controller was unhealthy")
        if not isinstance(workload, dict) or set(workload) != {"active", "chunk_uncommitted", "completion_generation"} or type(workload["active"]) is not bool or type(workload["chunk_uncommitted"]) is not bool or type(workload["completion_generation"]) is not int or workload["completion_generation"] < 0 or (workload["chunk_uncommitted"] and not workload["active"]): raise ObserverError("candidate workload status was invalid")
        if not isinstance(runtime, dict) or not isinstance(runtime.get("epoch"), str) or ev.HEX32.fullmatch(runtime["epoch"]) is None or type(runtime.get("started_monotonic_ns")) is not int or runtime["started_monotonic_ns"] <= 0: raise ObserverError("candidate runtime identity was invalid")
        if not isinstance(failures, dict) or set(failures) != {"cuda_oom_generation", "media_failure_generation"} or any(type(item) is not int or item < 0 for item in failures.values()): raise ObserverError("candidate failure status was invalid")
        return resource

    def _cgroup_snapshot(self, pid: int, expected_path: Path | None) -> tuple[Path, dict[str, int]]:
        payload = _read_optional_bound(Path(f"/proc/{pid}/cgroup"), 64 * 1024, "candidate cgroup membership")
        if payload is None:
            raise ObserverError("candidate cgroup membership disappeared")
        try: lines = payload.decode("ascii").splitlines()
        except UnicodeDecodeError as exc: raise ObserverError("candidate cgroup membership was malformed") from exc
        if len(lines) != 1 or not lines[0].startswith("0::/"): raise ObserverError("candidate cgroup-v2 membership was invalid")
        root = Path("/sys/fs/cgroup").resolve(strict=True); path = (root / lines[0][4:]).resolve(strict=True)
        if path == root or root not in path.parents or (expected_path is not None and path != expected_path): raise ObserverError("candidate cgroup identity changed")
        raw = _read_optional_bound(path / "memory.events", 64 * 1024, "candidate memory events")
        if raw is None: raise ObserverError("candidate memory events disappeared")
        result: dict[str, int] = {}
        try:
            for line in raw.decode("ascii").splitlines():
                key, value = line.split()
                if key in result or not value.isdigit(): raise ValueError
                result[key] = int(value)
        except (UnicodeDecodeError, ValueError) as exc: raise ObserverError("candidate memory events were malformed") from exc
        if not {"max", "oom", "oom_kill", "oom_group_kill"} <= set(result): raise ObserverError("candidate memory events were incomplete")
        return path, result

    def _verify_systemd(self) -> dict[str, dict[str, str]]:
        states = {unit: _systemctl_show(unit) for unit in (MONITOR_UNIT, PRIORITY_UNIT, REPAIR_SERVICE, REPAIR_TIMER)}
        monitor = states[MONITOR_UNIT]
        priority = states[PRIORITY_UNIT]
        if monitor["ActiveState"] != "active" or monitor["SubState"] != "running" or not monitor["MainPID"].isdigit() or int(monitor["MainPID"]) <= 0: raise ObserverError("failure monitor service was unhealthy")
        if priority["ActiveState"] != "active" or priority["SubState"] != "running" or not priority["MainPID"].isdigit() or int(priority["MainPID"]) <= 0: raise ObserverError("priority monitor service was unhealthy")
        _validate_repair_units(states)
        if _systemd_boundary(states) != self.live["systemd_boundary_sha256"]: raise ObserverError("systemd execution boundary changed")
        environment = _process_environment(int(monitor["MainPID"]))
        expected = {"AUTO_DELETE_INVALID_MEDIA": "false", "AUTO_DELETE_FAILED_FILES": "false", "AUTO_MARK_FAILED_FILES": "true", "AUTO_MARK_MIN_FAILURES": "1", "SUBGEN_REPAIR_ACTION": "report"}
        if any(environment.get(key, "").lower() != value for key, value in expected.items()): raise ObserverError("failure monitor effective policy was unsafe")
        priority_environment = _process_environment(int(priority["MainPID"]))
        priority_expected = {"FRIGATE_PRIORITY_SIGNAL_FILE": str(PRESSURE_SIGNAL_PATH), "FRIGATE_PRIORITY_POLICY_FILE": self.live["priority_policy_path"], "FRIGATE_PRIORITY_POLICY_SHA256": self.ids["configuration"]["policy_sha256"]}
        if any(priority_environment.get(key) != value for key, value in priority_expected.items()) or priority_environment.get("FRIGATE_PRIORITY_ORIGIN", "http://127.0.0.1:5000") != "http://127.0.0.1:5000": raise ObserverError("priority monitor effective policy changed")
        return states

    def _observe_priority_signal(self) -> None:
        try: parent = PRESSURE_SIGNAL_PATH.parent.lstat(); target = PRESSURE_SIGNAL_PATH.lstat()
        except OSError as exc: raise ObserverError("priority signal was unavailable") from exc
        if not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) != 0o700 or not stat.S_ISREG(target.st_mode) or stat.S_IMODE(target.st_mode) != 0o600 or parent.st_uid != target.st_uid or PRESSURE_SIGNAL_PATH.is_symlink(): raise ObserverError("priority signal ownership boundary changed")
        payload = _read_optional_bound(PRESSURE_SIGNAL_PATH, 4096, "priority signal")
        if payload is None: raise ObserverError("priority signal was unavailable")
        boot_hash = hashlib.sha256(self.live["host_boot_id"].encode("ascii")).hexdigest()
        self.priority_signal.observe(payload, now_ns=time.monotonic_ns(), boot_sha256=boot_hash, policy_sha256=self.ids["configuration"]["policy_sha256"])

    def _marker_hashes(self) -> set[str]:
        payload = _read_optional_bound(MARKER_REGISTRY_PATH, MAX_MARKER_BYTES, "failure marker registry")
        if payload is None: return set()
        value = _strict_json_bytes(payload, MAX_MARKER_BYTES, "failure marker registry")
        if not isinstance(value, dict) or set(value) != {"schema_version", "updated_utc", "markers"} or value["schema_version"] != 1 or not isinstance(value["markers"], list) or len(value["markers"]) > 10_000: raise ObserverError("failure marker registry was invalid")
        return {hashlib.sha256(_canonical_bytes(item)).hexdigest() for item in value["markers"]}

    def start_streams(self, started_utc: str) -> None:
        if self.followers: raise ObserverError("live streams were already started")
        self.stream_started_utc = started_utc; self.replay_since_utc = started_utc
        self.followers.append(StreamFollower("candidate", self.candidate_id, started_utc))
        self.followers.append(StreamFollower("frigate", self.frigate_id, started_utc))
        self.followers.append(StreamFollower("kernel", None, started_utc))

    def _merge_stream_result(self, result: tuple[list[tuple[bytes, dict[str, Any], int, str]], dict[str, set[str]]], events: dict[str, tuple[bytes, dict[str, Any], int, str]]) -> None:
        found, evidence = result
        for source_line, event, source_utc_ns, source_stream in found:
            event_key = hashlib.sha256(source_stream.encode("ascii") + b"\0" + source_line).hexdigest()
            events[event_key] = (source_line, event, source_utc_ns, source_stream)
        for key, hashes in evidence.items():
            self.log_evidence[key].update(hashes)
        if sum(len(items) for items in self.log_evidence.values()) > MAX_RELEVANT_LOG_EVIDENCE:
            raise ObserverError("relevant live log evidence exceeded its memory bound")

    def _bounded_stream_window(self, since_utc: str, until_utc: str, events: dict[str, tuple[bytes, dict[str, Any], int, str]]) -> None:
        bounded: list[StreamFollower] = []
        try:
            bounded.append(StreamFollower("candidate", self.candidate_id, since_utc, until=until_utc))
            bounded.append(StreamFollower("frigate", self.frigate_id, since_utc, until=until_utc))
            bounded.append(StreamFollower("kernel", None, since_utc, until=until_utc))
            for follower in bounded: self._merge_stream_result(follower.wait_complete(timeout=2), events)
        finally:
            for follower in bounded:
                try: follower.close()
                except BaseException: pass

    def _watermark_streams(self, cutoff_utc: str, *, final: bool) -> tuple[list[tuple[bytes, dict[str, Any], int, str]], tuple[int, str] | None]:
        if self.replay_since_utc is None: raise ObserverError("live stream watermark was not initialized")
        events: dict[str, tuple[bytes, dict[str, Any], int, str]] = {}
        self._bounded_stream_window(self.replay_since_utc, cutoff_utc, events)
        self.replay_since_utc = cutoff_utc
        if final:
            for follower in self.followers: follower.close()
        for follower in self.followers: self._merge_stream_result(follower.drain(), events)
        final_watermark = None
        if final:
            final_monotonic_ns = time.monotonic_ns(); final_utc = _utc_now()
            self._bounded_stream_window(cutoff_utc, final_utc, events)
            self.replay_since_utc = final_utc; final_watermark = (final_monotonic_ns, final_utc)
        for key, hashes in self.log_evidence.items(): self.counters[key] = len(hashes)
        if final: self.followers = []
        return list(events.values()), final_watermark

    def sample(self, *, final: bool = False) -> tuple[dict[str, Any], list[tuple[bytes, dict[str, Any], int, str]], tuple[int, str] | None]:
        self._space(self.journal_parent, "observer journal filesystem", 1024**3); self._space(self.docker_root, "Docker log filesystem", 2 * 1024**3)
        policy = self._verify_files_and_policy(); candidate, frigate, mqtt_settings = self._inspect_bound_containers()
        candidate_state = _state(candidate, "candidate"); frigate_state = _state(frigate, "Frigate")
        if candidate_state[:3] != (True, False, self.candidate_restart) or candidate_state[4] != self.candidate_pid: raise ObserverError("candidate state, PID, or restart count changed")
        if frigate_state[0] is not True or frigate_state[1] is not False or frigate_state[2] != self.frigate_restart or frigate_state[3] != "healthy": raise ObserverError("Frigate health or restart count changed")
        systemd = self._verify_systemd(); baseline_monitor = self.monitor_baseline[MONITOR_UNIT]; monitor = systemd[MONITOR_UNIT]
        if any(monitor[key] != baseline_monitor[key] for key in ("MainPID", "InvocationID", "NRestarts")): raise ObserverError("failure monitor restarted")
        baseline_priority = self.monitor_baseline[PRIORITY_UNIT]; priority_state = systemd[PRIORITY_UNIT]
        if any(priority_state[key] != baseline_priority[key] for key in ("MainPID", "InvocationID", "NRestarts")): raise ObserverError("priority monitor restarted")
        meminfo = _read_optional_bound(Path("/proc/meminfo"), 256 * 1024, "host memory telemetry")
        psi = _read_optional_bound(Path("/proc/pressure/memory"), 64 * 1024, "host memory PSI")
        if meminfo is None or psi is None: raise ObserverError("host memory telemetry was unavailable")
        _validate_host_memory(meminfo, psi, self.live["host_memory_reserve_bytes"])
        nvidia = _command([NVIDIA_BINARY, "--query-gpu=uuid,memory.total,memory.free", "--format=csv,noheader,nounits"], "NVIDIA telemetry", maximum=64 * 1024)
        _validate_nvidia(nvidia, gpu_uuid=self.live["gpu_uuid"], total_bytes=self.live["gpu_total_bytes"], free_reserve_bytes=self.live["gpu_free_reserve_bytes"])
        _validate_ollama(_fetch_loopback(11434, "/api/ps", "Ollama state")); self._observe_priority_signal()
        _, memory = self._cgroup_snapshot(self.candidate_pid, self.cgroup_path)
        for source, counter in (("max", "cgroup_max"), ("oom", "cgroup_oom"), ("oom_kill", "cgroup_oom_kill"), ("oom_group_kill", "cgroup_oom_group_kill")):
            delta = memory[source] - self.cgroup_baseline[source]
            if delta < 0: raise ObserverError("candidate memory event counter regressed")
            self.counters[counter] = delta
        resource = self._candidate_status()
        if resource["runtime_identity"]["epoch"] != self.runtime_epoch or resource["runtime_identity"]["started_monotonic_ns"] != self.runtime_started: raise ObserverError("candidate runtime identity changed")
        cuda = resource["failure_counters"]["cuda_oom_generation"] - self.cuda_generation
        media = resource["failure_counters"]["media_failure_generation"] - self.media_generation
        if cuda < 0 or media < 0: raise ObserverError("candidate failure generation regressed")
        self.counters["runtime_cuda_oom"] = cuda; self.counters["marker_handled"] = max(self.counters["marker_handled"], media)
        phase = resource["priority_pressure"]["controller_phase"]
        if phase != self.previous_phase:
            if phase == "yielding": self.counters["cooperative_yield"] += 1
            if phase == "normal" and self.previous_phase in {"yielding", "recovering"}: self.counters["cooperative_recovery"] += 1
            self.previous_phase = phase
        now_monotonic_ns = time.monotonic_ns()
        try: self.phase_tracker.observe(phase, now_monotonic_ns, final=final)
        except ObserverError:
            self.counters["unresolved_yield"] = 1
            raise
        _validate_frigate_stats(_fetch_loopback(5000, FRIGATE_STATS_PATH, "Frigate stats"), policy, self.low_since, time.monotonic())
        audit = self.audit.sample()
        for key, value in audit.items(): self.counters[key] += value
        current_markers = self._marker_hashes(); self.counters["marker_created"] += len(current_markers - self.marker_hashes); self.marker_hashes |= current_markers
        if mqtt_settings.enabled:
            if self.mqtt_probe is None:
                raise ObserverError("MQTT inventory probe was missing")
            mqtt_health = self.mqtt_probe.sample()
        else:
            if self.mqtt_probe is not None:
                raise ObserverError("disabled MQTT inventory opened a broker probe")
            mqtt_health = {"enabled": False}
        events, final_watermark = self._watermark_streams(_utc_now(), final=final)
        if final:
            final_audit = self.audit.sample()
            for key, value in final_audit.items(): self.counters[key] += value
            final_markers = self._marker_hashes(); self.counters["marker_created"] += len(final_markers - self.marker_hashes); self.marker_hashes |= final_markers
        health = {"schema": "subgen.task11b.soak-health/v1", "identity_sha256": ev.canonical_sha(self.ids), "candidate_running": True, "candidate_oom_killed": False, "frigate_healthy": True, "deletion_enabled": False, "active": resource["workload"]["active"], "chunk_uncommitted": resource["workload"]["chunk_uncommitted"], "completion_generation": resource["workload"]["completion_generation"], "controller_phase": phase, "counters": dict(self.counters), "mqtt_inventory": mqtt_health}
        return health, events, final_watermark

    def stop_candidate_exact(self) -> None:
        try:
            _stop_bound_candidate(self.live, self.ids["image"])
        finally:
            self.close()

    def close(self) -> None:
        if self.mqtt_probe is not None:
            try: self.mqtt_probe.close()
            except BaseException: pass
            self.mqtt_probe = None
        for follower in self.followers:
            try: follower.close()
            except BaseException: pass
        self.followers = []


def _stop_bound_candidate(live: dict[str, Any], image: dict[str, Any]) -> None:
    """Stop only the cryptographically bound full ID; config drift must not veto cleanup."""
    candidate_id = live.get("candidate_container_id") if isinstance(live, dict) else None
    config_digest = image.get("config_digest") if isinstance(image, dict) else None
    if not isinstance(candidate_id, str) or CONTAINER_RE.fullmatch(candidate_id) is None or not isinstance(config_digest, str) or ev.OCI.fullmatch(config_digest) is None:
        raise ObserverError("failure cleanup binding was invalid")
    item = _docker_json("inspect", "--type", "container", "--format", "{{json .}}", candidate_id, label="failure cleanup inspection")
    if not isinstance(item, dict) or item.get("Id") != candidate_id or item.get("Image") != config_digest:
        raise ObserverError("failure cleanup exact identity revalidation failed")
    if _state(item, "failure cleanup candidate")[0]:
        _command([DOCKER_BINARY, "--host", DOCKER_HOST, "stop", "--time", "120", candidate_id], "failure cleanup stop", maximum=4096, timeout=130)
    final = _docker_json("inspect", "--type", "container", "--format", "{{json .}}", candidate_id, label="failure cleanup verification")
    if not isinstance(final, dict) or final.get("Id") != candidate_id or _state(final, "failure cleanup candidate")[0]:
        raise ObserverError("failure cleanup did not stop the exact candidate")


class JournalWriter:
    def __init__(self, path: Path, config: dict[str, Any], test_source: Path, *, utc_now: Callable[[], str] = _utc_now, monotonic_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self.path = _absolute(path, "soak journal")
        self.utc_now, self.monotonic_ns = utc_now, monotonic_ns
        identities = _validate_config(config, test_source)
        self.header = {
            "schema": "subgen.task11b.soak-start/v1", "record_index": 0,
            "soak_id": secrets.token_hex(16), "started_utc": utc_now(),
            "started_monotonic_ns": monotonic_ns(), "interval_ns": ev.INTERVAL_NS,
            "max_gap_ns": ev.MAX_GAP_NS, "deletion_enabled": False,
            "identities": identities, "rollback_record_sha256": config["rollback_record_sha256"],
            "mqtt_inventory": config["live"]["mqtt_inventory"],
        }
        ev.validate_header(self.header)
        raw = ev.canonical_line(self.header); _create(self.path, raw, "soak journal")
        self.bytes_written = len(raw)
        self.record_index = 0; self.sample_index = 0; self.previous_hash = ev.sha(raw); self.closed = False
        self.last_sample_monotonic_ns = self.header["started_monotonic_ns"]
        self.last_sample_utc_ns = ev._utc(self.header["started_utc"], "soak start")
        self.baseline_counters: dict[str, int] | None = None
        self.previous_counters: dict[str, int] | None = None
        self.event_hashes: set[str] = set()
        self.last_event_sequence: int | None = None
        self.workload_count = 0
        try:
            self.fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
            if fcntl is not None: fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ObserverError("soak journal writer lock failed") from exc

    def _append(self, value: dict[str, Any]) -> None:
        raw = ev.canonical_line(value)
        if self.bytes_written + len(raw) > ev.MAX_JOURNAL_BYTES: raise ObserverError("soak journal exceeded its release byte limit")
        _write_all(self.fd, raw, "soak journal"); os.fsync(self.fd); self.bytes_written += len(raw)
        self.record_index += 1; self.previous_hash = ev.sha(raw)

    def append_health(self, health: dict[str, Any], *, captured_monotonic_ns: int | None = None, captured_utc: str | None = None) -> None:
        if set(health) != HEALTH_KEYS or health.get("schema") != "subgen.task11b.soak-health/v1":
            raise ObserverError("soak health snapshot schema was invalid")
        if (captured_monotonic_ns is None) != (captured_utc is None): raise ObserverError("sample watermark was incomplete")
        captured = self.monotonic_ns() if captured_monotonic_ns is None else captured_monotonic_ns
        captured_utc = self.utc_now() if captured_utc is None else captured_utc
        scheduled = self.header["started_monotonic_ns"] + self.sample_index * ev.INTERVAL_NS
        sample = {
            "schema": "subgen.task11b.soak-sample/v1", "record_index": self.record_index + 1,
            "previous_record_sha256": self.previous_hash, "sample_index": self.sample_index,
            "scheduled_monotonic_ns": scheduled, "captured_monotonic_ns": captured,
            "captured_utc": captured_utc, **{key: health[key] for key in HEALTH_KEYS - {"schema"}},
        }
        ev.validate_sample(
            sample,
            ev.canonical_sha(self.header["identities"]),
            self.header["mqtt_inventory"],
        )
        lag = captured - scheduled; gap = captured - self.last_sample_monotonic_ns
        captured_utc_ns = ev._utc(captured_utc, "sample capture")
        if lag < 0 or lag > ev.MAX_LAG_NS or (self.sample_index and not ev.MIN_GAP_NS <= gap <= ev.MAX_GAP_NS):
            raise ObserverError("live sample cadence failed")
        if (self.sample_index and captured_utc_ns <= self.last_sample_utc_ns) or abs((captured_utc_ns - ev._utc(self.header["started_utc"], "soak start")) - (captured - self.header["started_monotonic_ns"])) > ev.UTC_DRIFT_NS:
            raise ObserverError("live sample clocks disagreed")
        counters = sample["counters"]
        if self.previous_counters is not None and any(counters[key] < self.previous_counters[key] for key in ev.COUNTER_KEYS):
            raise ObserverError("live counter regressed")
        if self.baseline_counters is None:
            if any(counters[key] != 0 for key in ev.FAILURE_COUNTERS):
                raise ObserverError("failure occurred before the baseline sample")
            self.baseline_counters = dict(counters)
        elif any(counters[key] != self.baseline_counters[key] for key in ev.FAILURE_COUNTERS):
            raise ObserverError("live failure counter changed")
        self._append(sample); self.sample_index += 1
        self.last_sample_monotonic_ns = captured; self.last_sample_utc_ns = captured_utc_ns
        self.previous_counters = dict(counters)

    def append_runtime_event(self, source_line: bytes, event: dict[str, Any], source_utc_ns: int, source_stream: str, *, captured_monotonic_ns: int | None = None, captured_utc: str | None = None) -> None:
        if not self.sample_index:
            raise ObserverError("runtime event was before the soak baseline")
        reparsed = parse_runtime_event(source_line, started_utc_ns=ev._utc(self.header["started_utc"], "soak start"))
        if reparsed is None or reparsed != (event, source_utc_ns): raise ObserverError("runtime event did not match its source line")
        if source_stream != "stderr": raise ObserverError("runtime event source provenance was invalid")
        source_hash = hashlib.sha256(source_line).hexdigest()
        if source_hash in self.event_hashes or (self.last_event_sequence is not None and event["event_sequence"] <= self.last_event_sequence):
            raise ObserverError("runtime event was duplicated or reordered")
        if (captured_monotonic_ns is None) != (captured_utc is None): raise ObserverError("runtime event watermark was incomplete")
        captured_monotonic_ns = self.monotonic_ns() if captured_monotonic_ns is None else captured_monotonic_ns
        captured_utc = self.utc_now() if captured_utc is None else captured_utc
        captured_utc_ns = ev._utc(captured_utc, "workload capture")
        if source_utc_ns < ev._utc(self.header["started_utc"], "soak start") or source_utc_ns > captured_utc_ns:
            raise ObserverError("runtime event timestamp was outside the live window")
        record = {
            "schema": "subgen.task11b.soak-workload/v1", "record_index": self.record_index + 1,
            "previous_record_sha256": self.previous_hash, "captured_monotonic_ns": captured_monotonic_ns,
            "captured_utc": captured_utc, "source_utc": _format_utc_ns(source_utc_ns), "source_event_sha256": source_hash,
            "event_sequence": event["event_sequence"], "source_monotonic_ns": event["monotonic_ns"],
            "workload_id_sha256": hashlib.sha256(event["workload_id"].encode("ascii")).hexdigest(),
            "chunks_total": event["chunks_total"], "atomic_publish": event["atomic_publish"], "outcome": event["outcome"], "source_stream": source_stream,
        }
        ev.validate_workload(record); self._append(record)
        self.event_hashes.add(source_hash); self.last_event_sequence = event["event_sequence"]; self.workload_count += 1

    def close(self) -> None:
        if not self.closed: os.close(self.fd); self.closed = True

    def __enter__(self) -> "JournalWriter": return self
    def __exit__(self, *_args: Any) -> None: self.close()


def finalize(journal_path: Path, record_path: Path, finalization: dict[str, Any], *, utc_now: Callable[[], str] = _utc_now, monotonic_ns: Callable[[], int] = time.monotonic_ns, minimum_duration_ns: int = ev.MIN_DURATION_NS) -> dict[str, Any]:
    journal = _read(journal_path, ev.MAX_JOURNAL_BYTES, "soak journal", private=True)
    view = ev.validate_journal(journal, require_end=False, minimum_duration_ns=0)
    if set(finalization) != FINALIZATION_KEYS or finalization.get("schema") != "subgen.task11b.soak-finalization/v1" or finalization.get("outcome") != "pass":
        raise ObserverError("soak finalization schema was invalid")
    ev.validate_rollback(finalization["rollback"], view.header["rollback_record_sha256"])
    raw_end = b""
    prospective = journal
    if view.end is None:
        end = {
            "schema": "subgen.task11b.soak-end/v1", "record_index": view.record_count,
            "previous_record_sha256": view.last_sha256, "ended_monotonic_ns": view.samples[-1]["captured_monotonic_ns"],
            "ended_utc": view.samples[-1]["captured_utc"], "outcome": "pass", "rollback": finalization["rollback"],
        }
        raw_end = ev.canonical_line(end)
        prospective += raw_end
        if len(prospective) > ev.MAX_JOURNAL_BYTES: raise ObserverError("finalized soak journal exceeded its release byte limit")
    elif view.end["rollback"] != finalization["rollback"]:
        raise ObserverError("soak finalization did not match the finalized journal")
    record = ev.derive_record(prospective, minimum_duration_ns=minimum_duration_ns)
    ev.validate_record(record, minimum_duration_ns=minimum_duration_ns)
    record_payload = ev.canonical_line(record)
    record_target = _absolute(record_path, "soak record")
    try:
        fd = os.open(_absolute(journal_path, "soak journal"), os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            if fcntl is not None: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.fstat(fd).st_size != len(journal): raise ObserverError("soak journal changed before finalization")
            # The record is derived from the exact prospective journal.  Publish it
            # first so a create failure cannot leave a finalized journal without its
            # companion record.  An identical record is a recoverable prior attempt.
            _ensure_soak_record(record_target, record_payload)
            if raw_end:
                _write_all(fd, raw_end, "soak journal end"); os.fsync(fd)
        finally: os.close(fd)
    except OSError as exc: raise ObserverError("soak journal finalization failed") from exc
    return record


def verify_release(args: argparse.Namespace) -> int:
    evidence = _read(args.evidence, 2 * 1024**2, "release evidence", private=False)
    record_payload = _read(args.soak_record, ev.MAX_RECORD_BYTES, "soak record", private=True)
    journal = _read(args.soak_journal, ev.MAX_JOURNAL_BYTES, "soak journal", private=True)
    record = ev.verify_pair(record_payload, journal); binding = ev.parse_binding(evidence, args.binding_prefix)
    gate_payload = _read(args.gate_seal, 2 * 1024**2, "gate seal", private=False)
    candidate_payload = _read(args.candidate_identity_record, 2 * 1024**2, "candidate record", private=False)
    gate = ev.validate_gate(ev.strict_line(gate_payload, 2 * 1024**2)); candidate = ev.validate_candidate(ev.strict_line(candidate_payload, 2 * 1024**2))
    evidence_hash, observer_hash, test_hash = _source_hashes(args.observer_test_source)
    ev.verify_release_bindings(
        binding=binding, record=record, record_sha256=hashlib.sha256(record_payload).hexdigest(),
        gate=gate, gate_sha256=hashlib.sha256(gate_payload).hexdigest(), candidate=candidate,
        candidate_sha256=hashlib.sha256(candidate_payload).hexdigest(), evidence_sha256=evidence_hash,
        observer_sha256=observer_hash, observer_test_sha256=test_hash, runtime_commit=args.runtime_commit,
        oci_index=args.candidate_oci_index, config_digest=args.candidate_config_digest,
    )
    print("TASK11B_SOAK_VERIFY_OK")
    return 0


def _observe(args: argparse.Namespace) -> int:
    config = _load_json(args.config, 64 * 1024, "soak config", private=True)
    identities = _validate_config(config, args.observer_test_source)
    probe: LiveProbe | None = None
    previous_handlers: dict[int, Any] = {}

    def terminate(signum: int, _frame: Any) -> None:
        raise ObserverError(f"live soak received termination signal {signum}")

    managed_signals = [signal.SIGTERM]
    if hasattr(signal, "SIGHUP"): managed_signals.append(signal.SIGHUP)
    try:
        for signum in managed_signals:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, terminate)
        probe = LiveProbe(config, identities, _absolute(args.journal, "soak journal").parent)
        with JournalWriter(args.journal, config, args.observer_test_source) as writer:
            probe.start_streams(writer.header["started_utc"])
            previous_idle = False
            while True:
                scheduled = writer.header["started_monotonic_ns"] + writer.sample_index * ev.INTERVAL_NS
                delay_ns = scheduled - time.monotonic_ns()
                if delay_ns > 0: time.sleep(delay_ns / 1_000_000_000)
                elapsed = time.monotonic_ns() - writer.header["started_monotonic_ns"]
                final = elapsed >= ev.MIN_DURATION_NS and writer.workload_count > 0 and previous_idle
                health, events, final_watermark = probe.sample(final=final)
                if final and final_watermark is None: raise ObserverError("final live sample had no closed log watermark")
                if final:
                    for source_line, event, source_utc_ns, source_stream in events:
                        if hashlib.sha256(source_line).hexdigest() not in writer.event_hashes:
                            writer.append_runtime_event(source_line, event, source_utc_ns, source_stream, captured_monotonic_ns=final_watermark[0], captured_utc=final_watermark[1])
                    writer.append_health(health, captured_monotonic_ns=final_watermark[0], captured_utc=final_watermark[1])
                else:
                    writer.append_health(health)
                    for source_line, event, source_utc_ns, source_stream in events:
                        if hashlib.sha256(source_line).hexdigest() not in writer.event_hashes:
                            writer.append_runtime_event(source_line, event, source_utc_ns, source_stream)
                if final:
                    if health["active"] or health["chunk_uncommitted"] or writer.workload_count < 1: raise ObserverError("final live sample was not idle with a trusted event")
                    print("TASK11B_SOAK_RECORDING_COMPLETE"); return 0
                previous_idle = not health["active"] and not health["chunk_uncommitted"]
    except BaseException as original:
        try:
            if probe is not None: probe.stop_candidate_exact()
            else: _stop_bound_candidate(config["live"], identities["image"])
        except BaseException as cleanup: raise ObserverError("live soak failed and exact candidate cleanup failed") from cleanup
        raise original
    finally:
        if probe is not None: probe.close()
        for signum, previous in previous_handlers.items(): signal.signal(signum, previous)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__); commands = root.add_subparsers(dest="command", required=True)
    soak = commands.add_parser("soak"); modes = soak.add_subparsers(dest="mode", required=True)
    observe = modes.add_parser("observe"); observe.add_argument("--journal", type=Path, required=True); observe.add_argument("--config", type=Path, required=True); observe.add_argument("--observer-test-source", type=Path, required=True)
    finish = modes.add_parser("finalize"); finish.add_argument("--journal", type=Path, required=True); finish.add_argument("--record", type=Path, required=True); finish.add_argument("--finalization", type=Path, required=True)
    verify = commands.add_parser("verify-soak"); verify.add_argument("--soak-record", type=Path, required=True); verify.add_argument("--soak-journal", type=Path, required=True)
    release = commands.add_parser("verify-release")
    for option in ("evidence", "soak-record", "soak-journal", "candidate-identity-record", "gate-seal", "observer-test-source"): release.add_argument(f"--{option}", type=Path, required=True)
    release.add_argument("--binding-prefix", required=True); release.add_argument("--runtime-commit", required=True); release.add_argument("--candidate-oci-index", required=True); release.add_argument("--candidate-config-digest", required=True)
    return root


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command == "soak" and args.mode == "observe": return _observe(args)
    if args.command == "soak":
        finalization = _load_json(args.finalization, 64 * 1024, "soak finalization", private=True); finalize(args.journal, args.record, finalization); print("TASK11B_SOAK_FINALIZED"); return 0
    if args.command == "verify-soak":
        ev.verify_pair(_read(args.soak_record, ev.MAX_RECORD_BYTES, "soak record", private=True), _read(args.soak_journal, ev.MAX_JOURNAL_BYTES, "soak journal", private=True)); print("TASK11B_SOAK_VERIFY_OK"); return 0
    return verify_release(args)


def cli_entrypoint(argv: Iterable[str] | None = None) -> int:
    try: return main(argv)
    except SystemExit: raise
    except (ObserverError, ev.EvidenceError) as exc:
        print(f"TASK11B_SOAK_ABORT reason={exc.code}", file=sys.stderr); return 1
    except BaseException:
        print("TASK11B_SOAK_ABORT reason=internal_verifier_failure", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(cli_entrypoint())
