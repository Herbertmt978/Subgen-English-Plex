#!/usr/bin/env python3
"""Publish a coarse owner-only priority signal for a shared Frigate GPU host.

This host service is the only Frigate/Ollama-specific policy owner.  It never
starts, stops, or configures either service and never handles media failures.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import http.client
import json
import logging
import math
import os
import posixpath
import re
import secrets
import selectors
import signal
import socket
import stat
import subprocess
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Optional

from subgen_core.priority_pressure import (
    MAX_GENERATION,
    canonical_boot_id_sha256,
    encode_priority_publication,
)


LOGGER = logging.getLogger("subgen.priority_monitor")

DEFAULT_SIGNAL_FILE = "/run/subgen-priority/pressure.json"
DEFAULT_FRIGATE_ORIGIN = "http://127.0.0.1:5000"
DEFAULT_OLLAMA_ORIGIN = "http://127.0.0.1:11434"
POLL_INTERVAL_SECONDS = 5.0
CONNECT_TIMEOUT_SECONDS = 1.0
READ_TIMEOUT_SECONDS = 2.0
TOTAL_TIMEOUT_SECONDS = 3.0
NVIDIA_TIMEOUT_SECONDS = 2.0
MAX_FRIGATE_BODY_BYTES = 2 * 1024 * 1024
MAX_OLLAMA_BODY_BYTES = 256 * 1024
MAX_NVIDIA_OUTPUT_BYTES = 64 * 1024
MAX_POLICY_BYTES = 32 * 1024

_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_VERSION = re.compile(r"[0-9A-Za-z._+-]{1,32}\Z")
_GPU_UUID = re.compile(
    r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_NVIDIA_MODE = re.compile(r"[A-Za-z][ -~]{0,31}\Z")
_ORIGIN = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})\Z")
_POLICY_KEYS = {
    "schema",
    "frigate_version",
    "detection_fps_limit",
    "source_max_age_seconds",
    "cameras",
    "detectors",
    "required_embedding_speeds",
    "conditional_embedding_pairs",
    "frigate_config_sha256",
    "gpu_uuid",
    "nvidia_driver_version",
    "gpu_index",
}


class PriorityMonitorError(RuntimeError):
    """One privacy-safe producer failure."""


class FatalBoundaryError(PriorityMonitorError):
    """A local security or durability boundary cannot safely continue."""


class PolicyDrift(PriorityMonitorError):
    """A policy-bound identity or topology changed."""


class SourceUnavailable(PriorityMonitorError):
    """Required source telemetry was unavailable or malformed."""


class _DuplicateKey(ValueError):
    pass


def _reject_duplicate_pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _strict_json(raw: bytes, *, maximum: int) -> Any:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= maximum:
        raise ValueError("JSON body size is invalid")
    return json.loads(
        raw.decode("utf-8", "strict"),
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_constant,
    )


def _canonical_json(value: Any) -> bytes:
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


def _canonical_absolute_path(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be an absolute canonical path")
    if not value.startswith("/") or posixpath.normpath(value) != value:
        raise ValueError(f"{label} must be an absolute canonical path")
    parent, leaf = posixpath.split(value)
    if not parent or not leaf or leaf in {".", ".."}:
        raise ValueError(f"{label} must name a file")
    return value


@dataclass(frozen=True)
class LoopbackOrigin:
    port: int


def parse_loopback_origin(value: str) -> LoopbackOrigin:
    if not isinstance(value, str):
        raise ValueError("origin must be a literal loopback HTTP origin")
    match = _ORIGIN.fullmatch(value)
    if match is None:
        raise ValueError("origin must be a literal loopback HTTP origin")
    port = int(match.group(1), 10)
    if not 1 <= port <= 65535:
        raise ValueError("origin port is out of range")
    return LoopbackOrigin(port=port)


@dataclass(frozen=True)
class ProducerConfig:
    signal_file: str
    policy_file: str
    frigate_config_file: str
    expected_policy_sha256: str
    frigate_origin: LoopbackOrigin
    ollama_origin: LoopbackOrigin

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "ProducerConfig":
        signal_file = _canonical_absolute_path(
            environment.get("FRIGATE_PRIORITY_SIGNAL_FILE", DEFAULT_SIGNAL_FILE),
            "FRIGATE_PRIORITY_SIGNAL_FILE",
        )
        policy_file = _canonical_absolute_path(
            environment.get("FRIGATE_PRIORITY_POLICY_FILE", ""),
            "FRIGATE_PRIORITY_POLICY_FILE",
        )
        frigate_config_file = _canonical_absolute_path(
            environment.get("FRIGATE_CONFIG_FILE", ""),
            "FRIGATE_CONFIG_FILE",
        )
        expected_policy_sha256 = environment.get("FRIGATE_PRIORITY_POLICY_SHA256", "")
        if _HEX_64.fullmatch(expected_policy_sha256) is None:
            raise ValueError("FRIGATE_PRIORITY_POLICY_SHA256 is invalid")
        return cls(
            signal_file=signal_file,
            policy_file=policy_file,
            frigate_config_file=frigate_config_file,
            expected_policy_sha256=expected_policy_sha256,
            frigate_origin=parse_loopback_origin(
                environment.get("FRIGATE_PRIORITY_ORIGIN", DEFAULT_FRIGATE_ORIGIN)
            ),
            ollama_origin=parse_loopback_origin(
                environment.get("OLLAMA_PRIORITY_ORIGIN", DEFAULT_OLLAMA_ORIGIN)
            ),
        )


@dataclass(frozen=True)
class PriorityPolicy:
    frigate_version: str
    detection_fps_limit: float
    source_max_age_seconds: int
    cameras: tuple[tuple[str, float], ...]
    detectors: tuple[str, ...]
    required_embedding_speeds: tuple[str, ...]
    conditional_embedding_pairs: tuple[tuple[str, str], ...]
    frigate_config_sha256: str
    gpu_uuid: str
    nvidia_driver_version: str
    gpu_index: int
    sha256: str

    @property
    def camera_map(self) -> dict[str, float]:
        return dict(self.cameras)


def _strict_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _strict_float(value: Any, label: str, *, maximum: float) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError(f"{label} must be a JSON float")
    if not 0.0 < value <= maximum:
        raise ValueError(f"{label} is out of range")
    return value


def parse_priority_policy(raw: bytes, expected_sha256: str) -> PriorityPolicy:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_POLICY_BYTES:
        raise ValueError("priority policy size is invalid")
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise ValueError("priority policy newline is invalid")
    value = _strict_json(raw, maximum=MAX_POLICY_BYTES)
    if not isinstance(value, dict) or set(value) != _POLICY_KEYS:
        raise ValueError("priority policy keys are invalid")
    if _canonical_json(value) != raw:
        raise ValueError("priority policy bytes are noncanonical")
    actual_sha = hashlib.sha256(raw).hexdigest()
    if _HEX_64.fullmatch(expected_sha256) is None or actual_sha != expected_sha256:
        raise ValueError("priority policy hash is invalid")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise ValueError("priority policy schema is invalid")
    if (
        type(value["source_max_age_seconds"]) is not int
        or value["source_max_age_seconds"] != 30
    ):
        raise ValueError("priority policy source age is invalid")
    if type(value["gpu_index"]) is not int or not 0 <= value["gpu_index"] <= 31:
        raise ValueError("priority policy GPU index is invalid")
    version = value["frigate_version"]
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ValueError("priority policy version is invalid")
    detection_limit = value["detection_fps_limit"]
    if type(detection_limit) is not float or detection_limit != 80.0:
        raise ValueError("priority policy detection limit is invalid")
    cameras = value["cameras"]
    if not isinstance(cameras, dict) or not 1 <= len(cameras) <= 128:
        raise ValueError("priority policy cameras are invalid")
    parsed_cameras: list[tuple[str, float]] = []
    for name, expected_fps in cameras.items():
        parsed_cameras.append(
            (
                _strict_identifier(name, "camera identifier"),
                _strict_float(expected_fps, "camera expected FPS", maximum=60.0),
            )
        )
    if parsed_cameras != sorted(parsed_cameras):
        raise ValueError("priority policy cameras are not sorted")

    def identifier_array(name: str, minimum: int, maximum: int) -> tuple[str, ...]:
        items = value[name]
        if not isinstance(items, list) or not minimum <= len(items) <= maximum:
            raise ValueError(f"priority policy {name} is invalid")
        parsed = tuple(_strict_identifier(item, name) for item in items)
        if list(parsed) != sorted(set(parsed)):
            raise ValueError(f"priority policy {name} is not sorted and unique")
        return parsed

    detectors = identifier_array("detectors", 1, 32)
    required_embeddings = identifier_array("required_embedding_speeds", 1, 64)
    pairs_value = value["conditional_embedding_pairs"]
    if not isinstance(pairs_value, list) or len(pairs_value) > 32:
        raise ValueError("priority policy conditional pairs are invalid")
    pairs: list[tuple[str, str]] = []
    for item in pairs_value:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("priority policy conditional pair is invalid")
        pair = (
            _strict_identifier(item[0], "conditional identifier"),
            _strict_identifier(item[1], "conditional identifier"),
        )
        if pair[0] == pair[1] or list(pair) != sorted(pair):
            raise ValueError("priority policy conditional pair is invalid")
        pairs.append(pair)
    if pairs != sorted(set(pairs)):
        raise ValueError("priority policy conditional pairs are not sorted and unique")
    config_sha = value["frigate_config_sha256"]
    if not isinstance(config_sha, str) or _HEX_64.fullmatch(config_sha) is None:
        raise ValueError("priority policy config hash is invalid")
    gpu_uuid = value["gpu_uuid"]
    if not isinstance(gpu_uuid, str) or _GPU_UUID.fullmatch(gpu_uuid) is None:
        raise ValueError("priority policy GPU UUID is invalid")
    driver = value["nvidia_driver_version"]
    if (
        not isinstance(driver, str)
        or not 1 <= len(driver) <= 32
        or any(ord(character) < 32 or ord(character) > 126 for character in driver)
    ):
        raise ValueError("priority policy NVIDIA driver is invalid")
    return PriorityPolicy(
        frigate_version=version,
        detection_fps_limit=detection_limit,
        source_max_age_seconds=value["source_max_age_seconds"],
        cameras=tuple(parsed_cameras),
        detectors=detectors,
        required_embedding_speeds=required_embeddings,
        conditional_embedding_pairs=tuple(pairs),
        frigate_config_sha256=config_sha,
        gpu_uuid=gpu_uuid,
        nvidia_driver_version=driver,
        gpu_index=value["gpu_index"],
        sha256=actual_sha,
    )


def _open_directory_chain(path: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW")):
        raise FatalBoundaryError("required no-follow directory support is unavailable")
    current = os.open("/", flags)
    try:
        for component in path.split("/")[1:]:
            if not component:
                continue
            following = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _private_file_stat(info: os.stat_result, uid: int) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == uid
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
    )


def _read_private_policy_file(path: str, uid: int) -> tuple[bytes, tuple[int, int]]:
    parent, leaf = posixpath.split(path)
    parent_fd: Optional[int] = None
    file_fd: Optional[int] = None
    try:
        parent_fd = _open_directory_chain(parent)
        parent_before = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_before.st_mode)
            or parent_before.st_uid != uid
            or stat.S_IMODE(parent_before.st_mode) != 0o700
        ):
            raise PolicyDrift("priority policy boundary is unsafe")
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        file_fd = os.open(leaf, flags, dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if not _private_file_stat(before, uid) or before.st_size > MAX_POLICY_BYTES:
            raise PolicyDrift("priority policy file is unsafe")
        payload = bytearray()
        while len(payload) <= MAX_POLICY_BYTES:
            chunk = os.read(file_fd, min(65536, MAX_POLICY_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(file_fd)
        path_after = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        parent_after = os.fstat(parent_fd)
        if (
            len(payload) > MAX_POLICY_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (before.st_dev, before.st_ino) != (path_after.st_dev, path_after.st_ino)
            or (parent_before.st_dev, parent_before.st_ino)
            != (parent_after.st_dev, parent_after.st_ino)
        ):
            raise PolicyDrift("priority policy changed while reading")
        return bytes(payload), (before.st_dev, before.st_ino)
    except (OSError, ValueError) as exc:
        if isinstance(exc, PolicyDrift):
            raise
        raise PolicyDrift("priority policy could not be read") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def hash_exact_config_file(path: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    file_fd: Optional[int] = None
    try:
        file_fd = os.open(path, flags)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink < 1:
            raise PolicyDrift("Frigate config boundary is unsafe")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(file_fd)
        path_after = os.stat(path, follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or (before.st_dev, before.st_ino) != (path_after.st_dev, path_after.st_ino):
            raise PolicyDrift("Frigate config changed while hashing")
        return digest.hexdigest()
    except (OSError, ValueError) as exc:
        if isinstance(exc, PolicyDrift):
            raise
        raise PolicyDrift("Frigate config could not be hashed") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)


class PolicyStore:
    def __init__(self, config: ProducerConfig, *, uid: int) -> None:
        self._config = config
        self._uid = uid
        self._accepted_identity: Optional[tuple[int, int]] = None

    def load(self) -> PriorityPolicy:
        raw, identity = _read_private_policy_file(self._config.policy_file, self._uid)
        if self._accepted_identity is not None and identity != self._accepted_identity:
            raise PolicyDrift("priority policy replacement was refused")
        try:
            policy = parse_priority_policy(raw, self._config.expected_policy_sha256)
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise PolicyDrift("priority policy is invalid") from exc
        if (
            hash_exact_config_file(self._config.frigate_config_file)
            != policy.frigate_config_sha256
        ):
            raise PolicyDrift("Frigate config hash drifted")
        if self._accepted_identity is None:
            self._accepted_identity = identity
        return policy


class SignalDirectory:
    """Pinned owner-only signal directory and durable atomic publisher."""

    def __init__(self, path: str, *, uid: int) -> None:
        self._path = _canonical_absolute_path(path, "FRIGATE_PRIORITY_SIGNAL_FILE")
        self._parent, self._leaf = posixpath.split(self._path)
        self._uid = uid
        self._fd: Optional[int] = None
        self._identity: Optional[tuple[int, int]] = None

    def _validate_parent(self) -> os.stat_result:
        if self._fd is None:
            raise FatalBoundaryError("signal directory is not prepared")
        info = os.fstat(self._fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != self._uid
            or stat.S_IMODE(info.st_mode) != 0o700
            or self._identity != (info.st_dev, info.st_ino)
        ):
            raise FatalBoundaryError("signal directory boundary changed")
        return info

    def _target_stat(self) -> Optional[os.stat_result]:
        if self._fd is None:
            raise FatalBoundaryError("signal directory is not prepared")
        try:
            return os.stat(self._leaf, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def _validate_target(self, info: os.stat_result) -> None:
        if not _private_file_stat(info, self._uid):
            raise FatalBoundaryError("signal target boundary is unsafe")

    def prepare(self) -> None:
        if self._fd is not None:
            raise FatalBoundaryError("signal directory was prepared twice")
        try:
            self._fd = _open_directory_chain(self._parent)
            info = os.fstat(self._fd)
            self._identity = (info.st_dev, info.st_ino)
            self._validate_parent()
            existing = self._target_stat()
            if existing is not None:
                self._validate_target(existing)
                os.unlink(self._leaf, dir_fd=self._fd)
                os.fsync(self._fd)
        except OSError as exc:
            self.close()
            raise FatalBoundaryError("signal directory preparation failed") from exc
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _write_all(file_fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise OSError("short signal write")
            view = view[written:]

    def publish(self, payload: bytes) -> None:
        self._validate_parent()
        existing = self._target_stat()
        if existing is not None:
            self._validate_target(existing)
        if self._fd is None:
            raise FatalBoundaryError("signal directory is not prepared")
        temporary = f".{self._leaf}.{secrets.token_hex(16)}.tmp"
        file_fd: Optional[int] = None
        replaced = False
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            file_fd = os.open(temporary, flags, 0o600, dir_fd=self._fd)
            created = os.fstat(file_fd)
            if not _private_file_stat(created, self._uid):
                raise FatalBoundaryError("signal temporary boundary is unsafe")
            self._write_all(file_fd, payload)
            os.fsync(file_fd)
            os.close(file_fd)
            file_fd = None
            self._validate_parent()
            os.replace(
                temporary,
                self._leaf,
                src_dir_fd=self._fd,
                dst_dir_fd=self._fd,
            )
            replaced = True
            os.fsync(self._fd)
        except OSError as exc:
            raise FatalBoundaryError("signal publication failed") from exc
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if not replaced and self._fd is not None:
                try:
                    os.unlink(temporary, dir_fd=self._fd)
                except OSError:
                    pass

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self._identity = None


class BoundedHttpClient:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        connection_factory: Callable[..., http.client.HTTPConnection] = (
            http.client.HTTPConnection
        ),
    ) -> None:
        self._monotonic = monotonic
        self._connection_factory = connection_factory

    def _socket_timeout(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise SourceUnavailable("HTTP total deadline expired")
        return min(READ_TIMEOUT_SECONDS, remaining)

    @staticmethod
    def _abort_socket(
        active_socket: socket.socket,
        expired: threading.Event,
    ) -> None:
        expired.set()
        try:
            active_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _check_deadline(
        self,
        deadline: float,
        expired: threading.Event,
    ) -> None:
        if expired.is_set() or self._monotonic() >= deadline:
            raise SourceUnavailable("HTTP total deadline expired")

    def get(
        self,
        origin: LoopbackOrigin,
        path: str,
        *,
        maximum: int,
        deadline: Optional[float] = None,
    ) -> tuple[str, bytes]:
        if path not in {"/api/stats", "/api/version", "/api/ps"}:
            raise ValueError("HTTP path is not fixed")
        if deadline is None:
            deadline = self._monotonic() + TOTAL_TIMEOUT_SECONDS
        connect_timeout = min(
            CONNECT_TIMEOUT_SECONDS,
            self._socket_timeout(deadline),
        )
        connection = self._connection_factory(
            "127.0.0.1", origin.port, timeout=connect_timeout
        )
        expired = threading.Event()
        deadline_abort: Optional[threading.Timer] = None
        try:
            connection.connect()
            if connection.sock is None:
                raise SourceUnavailable("HTTP socket unavailable")
            self._check_deadline(deadline, expired)
            deadline_abort = threading.Timer(
                max(0.0, deadline - self._monotonic()),
                self._abort_socket,
                args=(connection.sock, expired),
            )
            deadline_abort.daemon = True
            deadline_abort.start()
            connection.sock.settimeout(self._socket_timeout(deadline))
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": "application/json, text/plain;q=0.9",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "User-Agent": "subgen-priority-monitor/0.5",
                },
            )
            self._check_deadline(deadline, expired)
            connection.sock.settimeout(self._socket_timeout(deadline))
            response = connection.getresponse()
            self._check_deadline(deadline, expired)
            if response.status != 200:
                raise SourceUnavailable("HTTP status was not 200")
            content_encodings = response.headers.get_all("Content-Encoding", [])
            if content_encodings and [
                item.strip().lower() for item in content_encodings
            ] != ["identity"]:
                raise SourceUnavailable("HTTP content encoding is unsupported")
            lengths = response.headers.get_all("Content-Length", [])
            transfers = response.headers.get_all("Transfer-Encoding", [])
            if len(lengths) > 1 or len(transfers) > 1 or (lengths and transfers):
                raise SourceUnavailable("HTTP framing is ambiguous")
            if lengths:
                try:
                    declared = int(lengths[0], 10)
                except ValueError as exc:
                    raise SourceUnavailable("HTTP content length is invalid") from exc
                if declared < 0 or declared > maximum:
                    raise SourceUnavailable("HTTP body exceeded its byte limit")
            if transfers and transfers[0].strip().lower() != "chunked":
                raise SourceUnavailable("HTTP transfer encoding is unsupported")
            payload = bytearray()
            while True:
                connection.sock.settimeout(self._socket_timeout(deadline))
                chunk = response.read(min(65536, maximum + 1 - len(payload)))
                if not chunk:
                    self._check_deadline(deadline, expired)
                    break
                payload.extend(chunk)
                if len(payload) > maximum:
                    raise SourceUnavailable("HTTP body exceeded its byte limit")
                self._check_deadline(deadline, expired)
            if lengths and len(payload) != declared:
                raise SourceUnavailable("HTTP body length was incomplete")
            content_type = response.headers.get("Content-Type", "")
            return content_type.lower(), bytes(payload)
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise SourceUnavailable("HTTP probe failed") from exc
        finally:
            if deadline_abort is not None:
                deadline_abort.cancel()
                deadline_abort.join()
            connection.close()

    def get_json(
        self,
        origin: LoopbackOrigin,
        path: str,
        *,
        maximum: int,
        deadline: Optional[float] = None,
    ) -> Any:
        content_type, raw = self.get(
            origin,
            path,
            maximum=maximum,
            deadline=deadline,
        )
        media_type = content_type.split(";", 1)[0].strip()
        if media_type != "application/json":
            raise SourceUnavailable("HTTP JSON content type is invalid")
        try:
            return _strict_json(raw, maximum=maximum)
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise SourceUnavailable("HTTP JSON body is invalid") from exc

    def get_version(
        self,
        origin: LoopbackOrigin,
        *,
        deadline: Optional[float] = None,
    ) -> str:
        content_type, raw = self.get(
            origin,
            "/api/version",
            maximum=MAX_FRIGATE_BODY_BYTES,
            deadline=deadline,
        )
        media_type = content_type.split(";", 1)[0].strip()
        if media_type != "text/plain":
            raise SourceUnavailable("Frigate version content type is invalid")
        try:
            version = raw.decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise SourceUnavailable("Frigate version body is invalid") from exc
        if _VERSION.fullmatch(version) is None:
            raise SourceUnavailable("Frigate version body is invalid")
        return version


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
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _read_boot_id() -> str:
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
        return handle.read()


def _bounded_command(argv: list[str]) -> bytes:
    if os.name != "posix":
        raise SourceUnavailable("NVIDIA probing requires POSIX pipe support")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            shell=False,
        )
    except OSError as exc:
        raise SourceUnavailable("NVIDIA command is unavailable") from exc
    if process.stdout is None:
        _kill_process(process)
        raise SourceUnavailable("NVIDIA command pipe is unavailable")
    selector = selectors.DefaultSelector()
    payload = bytearray()
    deadline = time.monotonic() + NVIDIA_TIMEOUT_SECONDS
    eof = False
    try:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        while not eof or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SourceUnavailable("NVIDIA command timed out")
            events = selector.select(min(0.1, remaining))
            for key, _mask in events:
                available = MAX_NVIDIA_OUTPUT_BYTES + 1 - len(payload)
                if available <= 0:
                    raise SourceUnavailable("NVIDIA output exceeded its byte limit")
                chunk = os.read(key.fd, min(65536, available))
                if not chunk:
                    eof = True
                    selector.unregister(process.stdout)
                    break
                payload.extend(chunk)
                if len(payload) > MAX_NVIDIA_OUTPUT_BYTES:
                    raise SourceUnavailable("NVIDIA output exceeded its byte limit")
            if process.poll() is not None and not events and not eof:
                available = MAX_NVIDIA_OUTPUT_BYTES + 1 - len(payload)
                chunk = os.read(process.stdout.fileno(), min(65536, available))
                if chunk:
                    payload.extend(chunk)
                else:
                    eof = True
        if process.wait(timeout=1) != 0:
            raise SourceUnavailable("NVIDIA command failed")
        return bytes(payload)
    except BaseException:
        _kill_process(process)
        raise
    finally:
        selector.close()
        process.stdout.close()


@dataclass(frozen=True)
class NvidiaObservation:
    index: int
    uuid: str
    driver_version: str
    compute_mode: str


def parse_nvidia_output(raw: bytes, policy: PriorityPolicy) -> NvidiaObservation:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_NVIDIA_OUTPUT_BYTES:
        raise SourceUnavailable("NVIDIA output size is invalid")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SourceUnavailable("NVIDIA output encoding is invalid") from exc
    rows = text.splitlines()
    if len(rows) != 1 or not rows[0]:
        raise SourceUnavailable("NVIDIA output row count is invalid")
    fields = [field.strip() for field in rows[0].split(",")]
    if len(fields) != 4 or not fields[0].isdigit():
        raise SourceUnavailable("NVIDIA output fields are invalid")
    index = int(fields[0], 10)
    uuid, driver, mode = fields[1:]
    if (
        index != policy.gpu_index
        or uuid != policy.gpu_uuid
        or driver != policy.nvidia_driver_version
    ):
        raise PolicyDrift("NVIDIA identity drifted")
    if _NVIDIA_MODE.fullmatch(mode) is None or "," in mode:
        raise SourceUnavailable("NVIDIA compute mode is invalid")
    return NvidiaObservation(index, uuid, driver, mode)


def probe_nvidia(policy: PriorityPolicy) -> NvidiaObservation:
    argv = [
        "nvidia-smi",
        f"--id={policy.gpu_index}",
        "--query-gpu=index,uuid,driver_version,compute_mode",
        "--format=csv,noheader,nounits",
    ]
    return parse_nvidia_output(_bounded_command(argv), policy)


def _json_number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceUnavailable(f"{label} is not numeric")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise SourceUnavailable(f"{label} is out of range") from exc
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        raise SourceUnavailable(f"{label} is out of range")
    return number


def _positive_generation(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_GENERATION:
        raise SourceUnavailable("Frigate generation is invalid")
    return value


@dataclass(frozen=True)
class NormalizedSource:
    generation: int
    detection_fps: float
    cameras: tuple[tuple[str, float, float], ...]
    detectors: tuple[tuple[str, float], ...]
    embeddings: tuple[tuple[str, Optional[float]], ...]
    frigate_version: str
    config_sha256: str
    policy_sha256: str
    nvidia: NvidiaObservation

    def ratios(self, policy: PriorityPolicy) -> tuple[float, ...]:
        expected = policy.camera_map
        return tuple(
            process_fps / expected[name] for name, process_fps, _ in self.cameras
        )

    @property
    def has_skips(self) -> bool:
        return any(skipped_fps > 0.0 for _, _, skipped_fps in self.cameras)

    @property
    def has_stalled_worker(self) -> bool:
        return any(speed == 0.0 for _, speed in self.detectors) or any(
            value == 0.0 for _, value in self.embeddings if value is not None
        )


def normalize_frigate_source(
    stats: Any,
    version: str,
    nvidia: NvidiaObservation,
    policy: PriorityPolicy,
) -> NormalizedSource:
    if version != policy.frigate_version:
        raise PolicyDrift("Frigate version drifted")
    if not isinstance(stats, dict):
        raise SourceUnavailable("Frigate stats root is invalid")
    service = stats.get("service")
    if not isinstance(service, dict):
        raise SourceUnavailable("Frigate service telemetry is invalid")
    generation = _positive_generation(service.get("last_updated"))
    detection_fps = _json_number(stats.get("detection_fps"), "detection FPS")
    cameras = stats.get("cameras")
    if not isinstance(cameras, dict) or set(cameras) != set(policy.camera_map):
        raise PolicyDrift("Frigate camera topology drifted")
    normalized_cameras: list[tuple[str, float, float]] = []
    for name, _expected in policy.cameras:
        item = cameras.get(name)
        if not isinstance(item, dict):
            raise SourceUnavailable("Frigate camera telemetry is invalid")
        normalized_cameras.append(
            (
                name,
                _json_number(item.get("process_fps"), "camera process FPS"),
                _json_number(item.get("skipped_fps"), "camera skipped FPS"),
            )
        )
    detectors = stats.get("detectors")
    if not isinstance(detectors, dict) or set(detectors) != set(policy.detectors):
        raise PolicyDrift("Frigate detector topology drifted")
    normalized_detectors: list[tuple[str, float]] = []
    for name in policy.detectors:
        item = detectors.get(name)
        if not isinstance(item, dict):
            raise SourceUnavailable("Frigate detector telemetry is invalid")
        normalized_detectors.append(
            (name, _json_number(item.get("inference_speed"), "detector speed"))
        )
    embeddings = stats.get("embeddings")
    if not isinstance(embeddings, dict):
        raise SourceUnavailable("Frigate embedding telemetry is invalid")
    normalized_embeddings: list[tuple[str, Optional[float]]] = []
    for name in policy.required_embedding_speeds:
        if name not in embeddings:
            raise SourceUnavailable("required embedding telemetry is missing")
        normalized_embeddings.append(
            (name, _json_number(embeddings[name], "embedding speed"))
        )
    for first, second in policy.conditional_embedding_pairs:
        first_present = first in embeddings
        second_present = second in embeddings
        if first_present != second_present:
            raise SourceUnavailable("conditional embedding telemetry is incomplete")
        if not first_present:
            normalized_embeddings.extend(((first, None), (second, None)))
        else:
            normalized_embeddings.extend(
                (
                    (first, _json_number(embeddings[first], "embedding metric")),
                    (second, _json_number(embeddings[second], "embedding metric")),
                )
            )
    return NormalizedSource(
        generation=generation,
        detection_fps=detection_fps,
        cameras=tuple(normalized_cameras),
        detectors=tuple(normalized_detectors),
        embeddings=tuple(normalized_embeddings),
        frigate_version=version,
        config_sha256=policy.frigate_config_sha256,
        policy_sha256=policy.sha256,
        nvidia=nvidia,
    )


def classify_ollama(value: Any) -> bool:
    if not isinstance(value, dict):
        raise SourceUnavailable("Ollama response root is invalid")
    models = value.get("models")
    if not isinstance(models, list) or len(models) > 128:
        raise SourceUnavailable("Ollama model list is invalid")
    if any(not isinstance(item, dict) for item in models):
        raise SourceUnavailable("Ollama model entry is invalid")
    return bool(models)


@dataclass(frozen=True)
class SourceDecision:
    pressure: bool
    clear_eligible: bool
    reason_codes: tuple[str, ...]

    @classmethod
    def asserted(cls, reasons: Iterable[str]) -> "SourceDecision":
        values = tuple(sorted(set(reasons)))
        if not values:
            raise ValueError("asserted decision needs one reason")
        return cls(True, False, values)

    @classmethod
    def clear(cls) -> "SourceDecision":
        return cls(False, True, ())

    @classmethod
    def neutral(cls) -> "SourceDecision":
        return cls(False, False, ())

    def union(self, reasons: Iterable[str]) -> "SourceDecision":
        values = tuple(sorted(set(self.reason_codes).union(reasons)))
        return SourceDecision.asserted(values) if values else self


class FrigatePriorityEvaluator:
    """Own distinct-generation streaks, never controller recovery hysteresis."""

    def __init__(self) -> None:
        self._source: Optional[NormalizedSource] = None
        self._source_observed_ns: Optional[int] = None
        self._cached_base_decision: Optional[SourceDecision] = None
        self._high_streak = 0
        self._low_streak = 0

    @property
    def has_source(self) -> bool:
        return self._source is not None

    @property
    def source_generation(self) -> int:
        if self._source is None:
            raise SourceUnavailable("no valid source generation exists")
        return self._source.generation

    @property
    def source_observed_ns(self) -> int:
        if self._source_observed_ns is None:
            raise SourceUnavailable("no valid source timestamp exists")
        return self._source_observed_ns

    def reset_streaks(self) -> None:
        self._high_streak = 0
        self._low_streak = 0

    def failure(self, reasons: Iterable[str]) -> Optional[SourceDecision]:
        self.reset_streaks()
        if not self.has_source:
            return None
        cached = self._cached_base_decision or SourceDecision.neutral()
        return cached.union(reasons)

    @staticmethod
    def _immediate_decision(source: NormalizedSource) -> SourceDecision:
        if (
            source.has_skips
            or source.has_stalled_worker
            or source.nvidia.compute_mode != "Default"
        ):
            return SourceDecision.asserted(("higher_priority_degraded",))
        return SourceDecision.neutral()

    def _base_decision(
        self, source: NormalizedSource, policy: PriorityPolicy
    ) -> SourceDecision:
        reasons: set[str] = set(self._immediate_decision(source).reason_codes)
        ratios = source.ratios(policy)
        if source.detection_fps >= policy.detection_fps_limit:
            self._high_streak += 1
        else:
            self._high_streak = 0
        if any(ratio < 0.95 for ratio in ratios):
            self._low_streak += 1
        elif all(ratio >= 0.95 for ratio in ratios):
            self._low_streak = 0
        if self._high_streak >= 2:
            reasons.add("higher_priority_busy")
        if self._low_streak >= 2:
            reasons.add("higher_priority_degraded")
        if reasons:
            return SourceDecision.asserted(reasons)
        if source.detection_fps < policy.detection_fps_limit and all(
            ratio >= 0.98 for ratio in ratios
        ):
            return SourceDecision.clear()
        return SourceDecision.neutral()

    def observe(
        self,
        source: NormalizedSource,
        policy: PriorityPolicy,
        *,
        observed_ns: int,
        ollama_busy: bool,
        ollama_unavailable: bool = False,
    ) -> SourceDecision:
        ollama_reasons = set()
        if ollama_busy:
            ollama_reasons.add("higher_priority_busy")
        if ollama_unavailable:
            ollama_reasons.add("higher_priority_unavailable")
        if self._source is not None:
            if source.generation < self._source.generation:
                result = self.failure(
                    ollama_reasons.union(("higher_priority_unavailable",))
                )
                assert result is not None
                return result
            if source.generation == self._source.generation:
                if source != self._source:
                    result = self.failure(
                        ollama_reasons.union(("higher_priority_unavailable",))
                    )
                    assert result is not None
                    return result
                if ollama_unavailable:
                    result = self.failure(ollama_reasons)
                    assert result is not None
                    return result
                if self._cached_base_decision is None:
                    raise SourceUnavailable("cached source decision is unavailable")
                return self._cached_base_decision.union(ollama_reasons)
        self._source = source
        self._source_observed_ns = observed_ns
        if ollama_unavailable:
            self.reset_streaks()
            self._cached_base_decision = self._immediate_decision(source)
            return self._cached_base_decision.union(ollama_reasons)
        self._cached_base_decision = self._base_decision(source, policy)
        return self._cached_base_decision.union(ollama_reasons)


class FrigatePriorityMonitor:
    def __init__(
        self,
        config: ProducerConfig,
        *,
        uid: Optional[int] = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        monotonic: Callable[[], float] = time.monotonic,
        sleep_event: Optional[threading.Event] = None,
        http_client: Optional[BoundedHttpClient] = None,
        nvidia_probe: Callable[[PriorityPolicy], NvidiaObservation] = probe_nvidia,
        boot_id_reader: Callable[[], str] = _read_boot_id,
        token_hex: Callable[[int], str] = secrets.token_hex,
        policy_store: Optional[PolicyStore] = None,
        signal_directory: Optional[SignalDirectory] = None,
    ) -> None:
        if uid is None:
            getter = getattr(os, "geteuid", None)
            if not callable(getter):
                raise FatalBoundaryError("effective uid is unavailable")
            uid = int(getter())
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            raise FatalBoundaryError("effective uid is invalid")
        self.config = config
        self._uid = uid
        self._clock_ns = clock_ns
        self._monotonic = monotonic
        self._stop = sleep_event or threading.Event()
        self._http = http_client or BoundedHttpClient(monotonic=monotonic)
        self._nvidia_probe = nvidia_probe
        self._boot_id_reader = boot_id_reader
        self._token_hex = token_hex
        self._policy_store = policy_store or PolicyStore(config, uid=uid)
        self._signals = signal_directory or SignalDirectory(config.signal_file, uid=uid)
        self._evaluator = FrigatePriorityEvaluator()
        self._boot_sha: Optional[str] = None
        self._epoch: Optional[str] = None
        self._sequence = 0
        self._started = False

    def start(self) -> None:
        if self._started:
            raise FatalBoundaryError("priority monitor was started twice")
        self._signals.prepare()
        try:
            self._boot_sha = canonical_boot_id_sha256(self._boot_id_reader())
            self._epoch = self._token_hex(16)
            if not re.fullmatch(r"[0-9a-f]{32}", self._epoch):
                raise FatalBoundaryError("producer epoch source is invalid")
            self._started = True
        except OSError as exc:
            self._signals.close()
            raise FatalBoundaryError("host boot identity is unavailable") from exc
        except BaseException:
            self._signals.close()
            raise

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self._signals.close()
        self._started = False

    def _publish(self, decision: SourceDecision) -> None:
        if not self._started or self._boot_sha is None or self._epoch is None:
            raise FatalBoundaryError("priority monitor is not started")
        observed_ns = self._clock_ns()
        if type(observed_ns) is not int or not 1 <= observed_ns <= MAX_GENERATION:
            raise FatalBoundaryError("monotonic clock is invalid")
        next_sequence = self._sequence + 1
        observation_id = self._token_hex(32)
        payload = encode_priority_publication(
            boot_id_sha256=self._boot_sha,
            producer_epoch=self._epoch,
            sequence=next_sequence,
            observed_monotonic_ns=observed_ns,
            source_generation=self._evaluator.source_generation,
            source_observed_monotonic_ns=self._evaluator.source_observed_ns,
            observation_id=observation_id,
            policy_sha256=self.config.expected_policy_sha256,
            pressure=decision.pressure,
            clear_eligible=decision.clear_eligible,
            reason_codes=decision.reason_codes,
        )
        self._signals.publish(payload)
        self._sequence = next_sequence

    def poll_once(self) -> bool:
        if not self._started:
            raise FatalBoundaryError("priority monitor is not started")
        try:
            policy = self._policy_store.load()
        except PolicyDrift:
            decision = self._evaluator.failure(("policy_drift",))
            if decision is None:
                return False
            self._publish(decision)
            return True

        stats: Any = None
        version: Any = None
        ollama: Any = None
        nvidia: Any = None
        probe_errors: list[Optional[BaseException]] = [None, None, None, None]
        # Submit together even when one result fails; no source can serially hold
        # the heartbeat beyond the ten-second consumer freshness boundary.
        http_deadline = self._monotonic() + TOTAL_TIMEOUT_SECONDS
        with ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="priority-probe"
        ) as pool:
            futures = (
                pool.submit(
                    self._http.get_json,
                    self.config.frigate_origin,
                    "/api/stats",
                    maximum=MAX_FRIGATE_BODY_BYTES,
                    deadline=http_deadline,
                ),
                pool.submit(
                    self._http.get_version,
                    self.config.frigate_origin,
                    deadline=http_deadline,
                ),
                pool.submit(
                    self._http.get_json,
                    self.config.ollama_origin,
                    "/api/ps",
                    maximum=MAX_OLLAMA_BODY_BYTES,
                    deadline=http_deadline,
                ),
                pool.submit(self._nvidia_probe, policy),
            )
            results: list[Any] = []
            for index, future in enumerate(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    probe_errors[index] = exc
                    results.append(None)
            stats, version, ollama, nvidia = results

        ollama_busy = False
        ollama_unavailable = probe_errors[2] is not None
        if not ollama_unavailable:
            try:
                ollama_busy = classify_ollama(ollama)
            except SourceUnavailable:
                ollama_unavailable = True

        source_reasons: set[str] = set()
        for error in (probe_errors[0], probe_errors[1], probe_errors[3]):
            if error is None:
                continue
            if isinstance(error, PolicyDrift):
                source_reasons.add("policy_drift")
            else:
                source_reasons.add("higher_priority_unavailable")
        source = None
        if not source_reasons:
            try:
                source = normalize_frigate_source(stats, version, nvidia, policy)
            except PolicyDrift:
                source_reasons.add("policy_drift")
            except SourceUnavailable:
                source_reasons.add("higher_priority_unavailable")
        if source is None:
            if ollama_busy:
                source_reasons.add("higher_priority_busy")
            if ollama_unavailable:
                source_reasons.add("higher_priority_unavailable")
            decision = self._evaluator.failure(source_reasons)
            if decision is None:
                return False
            self._publish(decision)
            return True
        decision = self._evaluator.observe(
            source,
            policy,
            observed_ns=self._clock_ns(),
            ollama_busy=ollama_busy,
            ollama_unavailable=ollama_unavailable,
        )
        self._publish(decision)
        return True

    def run(self) -> None:
        self.start()
        LOGGER.info("priority monitor started")
        next_poll = self._monotonic()
        try:
            while not self._stop.is_set():
                self.poll_once()
                next_poll += POLL_INTERVAL_SECONDS
                delay = max(0.0, next_poll - self._monotonic())
                self._stop.wait(delay)
                if self._monotonic() - next_poll > POLL_INTERVAL_SECONDS:
                    next_poll = self._monotonic()
        finally:
            self.close()
            LOGGER.info("priority monitor stopped")


def _install_signal_handlers(monitor: FrigatePriorityMonitor) -> None:
    def stop_handler(_signum: int, _frame: Any) -> None:
        monitor.stop()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = ProducerConfig.from_environment(os.environ)
        monitor = FrigatePriorityMonitor(config)
        _install_signal_handlers(monitor)
        monitor.run()
        return 0
    except (FatalBoundaryError, PolicyDrift, SourceUnavailable, ValueError):
        LOGGER.error("priority monitor stopped at a fail-closed boundary")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BoundedHttpClient",
    "FatalBoundaryError",
    "FrigatePriorityEvaluator",
    "FrigatePriorityMonitor",
    "LoopbackOrigin",
    "NvidiaObservation",
    "NormalizedSource",
    "PolicyDrift",
    "PriorityPolicy",
    "ProducerConfig",
    "SignalDirectory",
    "SourceDecision",
    "SourceUnavailable",
    "classify_ollama",
    "hash_exact_config_file",
    "main",
    "normalize_frigate_source",
    "parse_loopback_origin",
    "parse_nvidia_output",
    "parse_priority_policy",
    "probe_nvidia",
]
