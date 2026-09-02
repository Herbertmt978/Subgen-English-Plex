"""Privacy-safe runtime state and the normally-disabled Task 11B journal.

The public coordinator exists in every process so the status endpoint can expose
only coarse workload state and a process-local identity.  The append-only
receipt journal is enabled only when all four owner-operated Task 11B settings
are supplied.  Callers that already hold ``model_runtime_condition`` use the
``*_locked`` methods; the convenience methods acquire the condition passed to
the constructor (or a private lock when no condition was supplied).

Receipt state is written and fsynced before the corresponding coordinator-owned
workload mutation becomes visible.  Callers must likewise invoke
``record_runtime_change_locked`` while retaining their controller/model locks
and before releasing the changed runtime state to further work.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import posixpath
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional


RECEIPT_SCHEMA = "subgen.task11b.runtime-receipt/v1"
MAX_RECEIPT_BYTES = 4 * 1024
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_GENERATION = (1 << 63) - 1

GATE_RECEIPT_FILE = "TASK11B_GATE_RECEIPT_FILE"
GATE_TOKEN_SHA256 = "TASK11B_GATE_TOKEN_SHA256"
PHASE_A_WORKLOAD_SHA256 = "TASK11B_PHASE_A_WORKLOAD_SHA256"
PHASE_B_WORKLOAD_SHA256 = "TASK11B_PHASE_B_WORKLOAD_SHA256"
GATE_ENVIRONMENT_KEYS = (
    GATE_RECEIPT_FILE,
    GATE_TOKEN_SHA256,
    PHASE_A_WORKLOAD_SHA256,
    PHASE_B_WORKLOAD_SHA256,
)

GATE_PHASE_PATHS = (
    ("/fixtures/phase-a", "/task11b-output/phase-a"),
    ("/fixtures/phase-b", "/task11b-output/phase-b"),
)

RUNTIME_STATE_KEYS = frozenset(
    {
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
        "model_identity_sha256",
        "cuda_oom_generation",
        "media_failure_generation",
    }
)

RECEIPT_KEYS = frozenset(
    {
        "schema",
        "runtime_epoch",
        "gate_token_sha256",
        "sequence",
        "observed_monotonic_ns",
        "workload_sha256",
        *RUNTIME_STATE_KEYS,
        "active",
        "chunk_uncommitted",
        "active_cursor_ms",
        "completed_cursor_ms",
        "completion_generation",
    }
)

_LOWER_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_LOWER_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_PRIORITY_STATES = frozenset({"clear", "neutral", "asserted", "unavailable"})
_CONTROLLER_PHASES = frozenset({"normal", "yielding", "recovering"})
_RECOVERY_REASONS = frozenset(
    {None, "priority_pressure", "resource_pressure", "model_admission"}
)


class RuntimeReceiptError(RuntimeError):
    """The gate receipt contract could not be preserved safely."""


class WorkloadToken:
    """Opaque process-local handle used to update one admitted workload."""

    __slots__ = ("_owner", "_serial")

    def __init__(self, owner: object, serial: int) -> None:
        self._owner = owner
        self._serial = serial


def _is_int(value: object, *, minimum: int = 0, maximum: int = MAX_GENERATION) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and minimum <= value <= maximum
    )


def _require_int(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_GENERATION,
) -> int:
    if not _is_int(value, minimum=minimum, maximum=maximum):
        raise RuntimeReceiptError(f"{name} is invalid")
    return value


def _require_lower_hex(value: object, name: str, *, length: int = 64) -> str:
    pattern = _LOWER_HEX_32 if length == 32 else _LOWER_HEX_64
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise RuntimeReceiptError(f"{name} must be lowercase {length}-hex")
    return value


def _require_normalized_gate_path(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or posixpath.normpath(value) != value
    ):
        raise RuntimeReceiptError(f"Task 11B {label} was not a normalized POSIX path")
    return value


def _require_real_gate_path(
    filesystem: object,
    path: str,
    label: str,
    *,
    directory: bool,
) -> None:
    try:
        item = filesystem.lstat(path)
        resolved = filesystem.path.realpath(path)
    except (AttributeError, OSError) as exc:
        raise RuntimeReceiptError(f"Task 11B {label} was unavailable") from exc
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        stat.S_ISLNK(item.st_mode)
        or not expected_type(item.st_mode)
        or resolved != path
    ):
        raise RuntimeReceiptError(
            f"Task 11B {label} was not one real {'directory' if directory else 'file'}"
        )


def canonical_json_line(document: Mapping[str, object]) -> bytes:
    """Return canonical ASCII JSON with exactly one trailing newline."""
    try:
        text = json.dumps(
            dict(document),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        payload = (text + "\n").encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeReceiptError("receipt state was not canonical JSON") from exc
    return payload


@dataclass(frozen=True)
class RuntimeIdentity:
    """One non-persistent process identity for public release evidence."""

    epoch: str
    started_monotonic_ns: int

    def __post_init__(self) -> None:
        _require_lower_hex(self.epoch, "runtime epoch", length=32)
        _require_int(
            self.started_monotonic_ns,
            "runtime start time",
            minimum=1,
        )

    @classmethod
    def create(
        cls,
        *,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> "RuntimeIdentity":
        raw = random_bytes(16)
        if not isinstance(raw, bytes) or len(raw) != 16:
            raise RuntimeReceiptError("runtime identity entropy was invalid")
        started = monotonic_ns()
        _require_int(started, "runtime start time", minimum=1)
        return cls(epoch=raw.hex(), started_monotonic_ns=started)

    def snapshot(self) -> dict[str, object]:
        return {
            "epoch": self.epoch,
            "started_monotonic_ns": self.started_monotonic_ns,
        }


@dataclass(frozen=True)
class GateReceiptConfig:
    """Validated all-or-none owner-operated gate configuration."""

    receipt_file: Optional[Path] = None
    gate_token_sha256: Optional[str] = None
    phase_a_workload_sha256: Optional[str] = None
    phase_b_workload_sha256: Optional[str] = None

    def __post_init__(self) -> None:
        values = (
            self.receipt_file,
            self.gate_token_sha256,
            self.phase_a_workload_sha256,
            self.phase_b_workload_sha256,
        )
        populated = tuple(value is not None for value in values)
        if not any(populated):
            return
        if not all(populated):
            raise RuntimeReceiptError(
                "Task 11B gate configuration must be entirely enabled or disabled"
            )
        if not isinstance(self.receipt_file, Path):
            raise RuntimeReceiptError("Task 11B receipt path must be a Path")
        if not self.receipt_file.is_absolute() or not self.receipt_file.name:
            raise RuntimeReceiptError(
                "Task 11B receipt path must be an absolute file path"
            )
        _require_lower_hex(self.gate_token_sha256, GATE_TOKEN_SHA256)
        phase_a = _require_lower_hex(
            self.phase_a_workload_sha256, PHASE_A_WORKLOAD_SHA256
        )
        phase_b = _require_lower_hex(
            self.phase_b_workload_sha256, PHASE_B_WORKLOAD_SHA256
        )
        if phase_a == phase_b:
            raise RuntimeReceiptError("Task 11B phase workload hashes must be distinct")

    @property
    def enabled(self) -> bool:
        return self.receipt_file is not None

    def map_output_media_path(self, file_path: str, *, filesystem: object = os) -> str:
        """Map one gate fixture to its fixed writable shadow path.

        Public runtimes return the input byte-for-byte.  The Task 11B runtime
        accepts only the two frozen read-only fixture roots and validates both
        sides of the mapping before publication can begin.
        """

        if not self.enabled:
            return file_path

        source = _require_normalized_gate_path(file_path, "fixture path")
        selected: Optional[tuple[str, str]] = None
        for input_root, output_root in GATE_PHASE_PATHS:
            if source.startswith(input_root + "/"):
                selected = (input_root, output_root)
                break
        if selected is None:
            raise RuntimeReceiptError(
                "Task 11B fixture path was outside the exact phase roots"
            )

        input_root, output_root = selected
        relative = source[len(input_root) + 1 :]
        if not relative:
            raise RuntimeReceiptError("Task 11B fixture path did not name a file")
        mapped = _require_normalized_gate_path(
            posixpath.join(output_root, relative),
            "mapped media path",
        )
        mapped_parent = posixpath.dirname(mapped)

        _require_real_gate_path(
            filesystem,
            input_root,
            "fixture root",
            directory=True,
        )
        _require_real_gate_path(
            filesystem,
            source,
            "fixture media",
            directory=False,
        )
        _require_real_gate_path(
            filesystem,
            output_root,
            "output root",
            directory=True,
        )
        _require_real_gate_path(
            filesystem,
            mapped_parent,
            "output parent",
            directory=True,
        )
        return mapped

    def validate_output_artifact_path(
        self,
        artifact_path: str,
        *,
        filesystem: object = os,
    ) -> str:
        """Require one fresh final gate artifact under a fixed shadow root."""

        if not self.enabled:
            return artifact_path

        target = _require_normalized_gate_path(artifact_path, "artifact path")
        selected_root = next(
            (
                output_root
                for _input_root, output_root in GATE_PHASE_PATHS
                if target.startswith(output_root + "/")
            ),
            None,
        )
        if selected_root is None:
            raise RuntimeReceiptError(
                "Task 11B artifact path was outside the exact output roots"
            )

        _require_real_gate_path(
            filesystem,
            selected_root,
            "output root",
            directory=True,
        )
        _require_real_gate_path(
            filesystem,
            posixpath.dirname(target),
            "artifact parent",
            directory=True,
        )
        try:
            filesystem.lstat(target)
        except FileNotFoundError:
            return target
        except (AttributeError, OSError) as exc:
            raise RuntimeReceiptError(
                "Task 11B artifact path could not be checked"
            ) from exc
        raise RuntimeReceiptError("Task 11B artifact path already existed")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        concurrent_transcriptions: object,
        expected_gate_token_sha256: Optional[str] = None,
    ) -> "GateReceiptConfig":
        values: dict[str, str] = {}
        for key in GATE_ENVIRONMENT_KEYS:
            value = environment.get(key, "")
            if not isinstance(value, str):
                raise RuntimeReceiptError(f"{key} must be a string")
            values[key] = value

        populated = [bool(values[key]) for key in GATE_ENVIRONMENT_KEYS]
        if not any(populated):
            return cls()
        if not all(populated):
            raise RuntimeReceiptError(
                "Task 11B gate configuration must be entirely enabled or disabled"
            )

        token = _require_lower_hex(values[GATE_TOKEN_SHA256], GATE_TOKEN_SHA256)
        phase_a = _require_lower_hex(
            values[PHASE_A_WORKLOAD_SHA256], PHASE_A_WORKLOAD_SHA256
        )
        phase_b = _require_lower_hex(
            values[PHASE_B_WORKLOAD_SHA256], PHASE_B_WORKLOAD_SHA256
        )
        if phase_a == phase_b:
            raise RuntimeReceiptError("Task 11B phase workload hashes must be distinct")

        if expected_gate_token_sha256 is not None:
            expected = _require_lower_hex(
                expected_gate_token_sha256,
                "expected gate token digest",
            )
            if token != expected:
                raise RuntimeReceiptError(
                    "Task 11B gate token did not match the execution boundary"
                )

        if isinstance(concurrent_transcriptions, bool):
            concurrency = None
        elif isinstance(concurrent_transcriptions, int):
            concurrency = concurrent_transcriptions
        elif isinstance(concurrent_transcriptions, str) and re.fullmatch(
            r"[0-9]+", concurrent_transcriptions
        ):
            concurrency = int(concurrent_transcriptions)
        else:
            concurrency = None
        if concurrency != 1:
            raise RuntimeReceiptError(
                "Task 11B gate requires CONCURRENT_TRANSCRIPTIONS=1"
            )

        path = Path(values[GATE_RECEIPT_FILE])
        if not path.is_absolute() or not path.name:
            raise RuntimeReceiptError(
                "Task 11B receipt path must be an absolute file path"
            )
        return cls(
            receipt_file=path,
            gate_token_sha256=token,
            phase_a_workload_sha256=phase_a,
            phase_b_workload_sha256=phase_b,
        )


class _SecureJournal:
    """One securely-created, inode-bound append-only journal writer."""

    def __init__(self, path: Path) -> None:
        if os.name != "posix" or not hasattr(os, "geteuid"):
            raise RuntimeReceiptError(
                "Task 11B receipt journal requires POSIX ownership semantics"
            )
        if not path.is_absolute() or not path.name:
            raise RuntimeReceiptError("Task 11B receipt path was invalid")
        owner = os.geteuid()
        try:
            parent = path.parent.lstat()
        except OSError as exc:
            raise RuntimeReceiptError(
                "Task 11B receipt parent was unavailable"
            ) from exc
        if (
            stat.S_ISLNK(parent.st_mode)
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != owner
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise RuntimeReceiptError(
                "Task 11B receipt parent ownership, mode, or type was unsafe"
            )
        try:
            path.lstat()
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise RuntimeReceiptError(
                    "Task 11B receipt path could not be checked"
                ) from exc
        else:
            raise RuntimeReceiptError(
                "Task 11B receipt path already existed at process start"
            )

        required_flags = ("O_APPEND", "O_CREAT", "O_EXCL", "O_NOFOLLOW")
        if any(not hasattr(os, name) for name in required_flags):
            raise RuntimeReceiptError("secure append-only open flags were unavailable")
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
        )
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise RuntimeReceiptError(
                "Task 11B receipt journal could not be created"
            ) from exc
        self._fd = fd
        self._path = path
        self._owner = owner
        self._closed = False
        try:
            item = os.fstat(fd)
            current = path.lstat()
            identity = (item.st_dev, item.st_ino)
            if (
                not stat.S_ISREG(item.st_mode)
                or item.st_nlink != 1
                or item.st_uid != owner
                or stat.S_IMODE(item.st_mode) != 0o600
                or (current.st_dev, current.st_ino) != identity
                or stat.S_ISLNK(current.st_mode)
                or not stat.S_ISREG(current.st_mode)
            ):
                raise RuntimeReceiptError(
                    "Task 11B receipt journal ownership, mode, or identity was unsafe"
                )
            self._identity = identity
            self._size = item.st_size
            if self._size != 0:
                raise RuntimeReceiptError("Task 11B receipt journal was not empty")
        except BaseException:
            os.close(fd)
            self._closed = True
            raise

    def _verify_identity(self) -> None:
        if self._closed:
            raise RuntimeReceiptError("Task 11B receipt journal was closed")
        try:
            item = os.fstat(self._fd)
            current = self._path.lstat()
        except OSError as exc:
            raise RuntimeReceiptError(
                "Task 11B receipt journal identity was unavailable"
            ) from exc
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_uid != self._owner
            or stat.S_IMODE(item.st_mode) != 0o600
            or (item.st_dev, item.st_ino) != self._identity
            or (current.st_dev, current.st_ino) != self._identity
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or item.st_size != self._size
        ):
            raise RuntimeReceiptError(
                "Task 11B receipt journal was replaced, mutated, or unsafe"
            )

    def append(self, payload: bytes) -> None:
        if not isinstance(payload, bytes) or not payload.endswith(b"\n"):
            raise RuntimeReceiptError("Task 11B receipt bytes were invalid")
        if len(payload) > MAX_RECEIPT_BYTES:
            raise RuntimeReceiptError("Task 11B receipt exceeded 4 KiB")
        self._verify_identity()
        if self._size + len(payload) > MAX_JOURNAL_BYTES:
            raise RuntimeReceiptError("Task 11B receipt journal exceeded 8 MiB")
        try:
            written = os.write(self._fd, payload)
            if written != len(payload):
                raise RuntimeReceiptError(
                    "Task 11B receipt journal accepted a partial write"
                )
            os.fsync(self._fd)
        except OSError as exc:
            raise RuntimeReceiptError(
                "Task 11B receipt journal could not be durably appended"
            ) from exc
        self._size += len(payload)

    def close(self) -> None:
        if not self._closed:
            os.close(self._fd)
            self._closed = True


def _validate_runtime_state(state: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(state, Mapping) or set(state) != RUNTIME_STATE_KEYS:
        raise RuntimeReceiptError("Task 11B runtime state keys were invalid")
    result = dict(state)

    source = result["source_generation"]
    observation = result["observation_digest"]
    policy = result["policy_sha256"]
    heartbeat = result["heartbeat_age_ms"]
    source_age = result["source_age_ms"]
    last_accepted = (source, observation, policy, heartbeat, source_age)
    if source is None:
        if any(value is not None for value in last_accepted):
            raise RuntimeReceiptError("last-accepted priority fields disagreed")
    else:
        if any(value is None for value in last_accepted):
            raise RuntimeReceiptError("last-accepted priority fields disagreed")
        _require_int(source, "source_generation", minimum=1)
        _require_lower_hex(observation, "observation_digest")
        _require_lower_hex(policy, "policy_sha256")
        _require_int(heartbeat, "heartbeat_age_ms", maximum=60_000)
        _require_int(source_age, "source_age_ms", maximum=60_000)

    transition_digest = result["transition_observation_digest"]
    if transition_digest is not None:
        _require_lower_hex(
            transition_digest,
            "transition_observation_digest",
        )
    transition_sequence = _require_int(
        result["transition_sequence"],
        "transition_sequence",
    )
    if transition_digest is not None and transition_sequence == 0:
        raise RuntimeReceiptError("transition digest lacked a transition sequence")

    if result["priority_state"] not in _PRIORITY_STATES:
        raise RuntimeReceiptError("priority_state was invalid")
    controller = result["controller_phase"]
    recovery = result["recovery_reason"]
    if controller not in _CONTROLLER_PHASES or recovery not in _RECOVERY_REASONS:
        raise RuntimeReceiptError("controller phase or recovery reason was invalid")
    if (controller == "normal") != (recovery is None):
        raise RuntimeReceiptError("controller phase and recovery reason disagreed")
    if type(result["admission_open"]) is not bool:
        raise RuntimeReceiptError("admission_open must be a boolean")
    _require_int(
        result["distinct_clear_count"],
        "distinct_clear_count",
        maximum=3,
    )

    resident = result["model_resident"]
    identity = result["model_identity_sha256"]
    if type(resident) is not bool:
        raise RuntimeReceiptError("model_resident must be a boolean")
    if identity is not None:
        _require_lower_hex(identity, "model_identity_sha256")
    if resident != (identity is not None):
        raise RuntimeReceiptError("model residency and model identity disagreed")
    for name in (
        "model_load_generation",
        "model_unload_generation",
        "cuda_oom_generation",
        "media_failure_generation",
    ):
        _require_int(result[name], name)
    return result


class RuntimeReceiptCoordinator:
    """Own public workload state and an optional Task 11B receipt journal."""

    def __init__(
        self,
        *,
        identity: RuntimeIdentity,
        config: GateReceiptConfig,
        condition: Optional[object] = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not isinstance(identity, RuntimeIdentity):
            raise TypeError("identity must be RuntimeIdentity")
        if not isinstance(config, GateReceiptConfig):
            raise TypeError("config must be GateReceiptConfig")
        self._identity = identity
        self._config = config
        self._condition = condition if condition is not None else threading.RLock()
        self._monotonic_ns = monotonic_ns
        self._journal = _SecureJournal(config.receipt_file) if config.enabled else None
        self._initialized = False
        self._failed = False
        self._closed = False
        self._sequence = 0
        self._last_observed_monotonic_ns = 0
        self._runtime_state: Optional[dict[str, object]] = None

        self._workload_sha256: Optional[str] = None
        self._active = False
        self._chunk_uncommitted = False
        self._active_cursor_ms: Optional[int] = None
        self._completed_cursor_ms: Optional[int] = None
        self._completion_generation = 0
        self._gate_phase = 0
        self._gate_aborted = False
        self._token_owner = object()
        self._next_token_serial = 0
        self._gate_workload_token: Optional[WorkloadToken] = None
        self._ordinary_workloads: dict[WorkloadToken, tuple[int, bool]] = {}

    @property
    def gate_enabled(self) -> bool:
        return self._config.enabled

    def runtime_identity_snapshot(self) -> dict[str, object]:
        return self._identity.snapshot()

    def workload_snapshot_locked(self) -> dict[str, object]:
        """Return exactly the privacy-safe public workload fields."""
        if self._config.enabled:
            active = self._active
            chunk_uncommitted = self._chunk_uncommitted if active else False
        else:
            active = bool(self._ordinary_workloads)
            chunk_uncommitted = any(
                chunk for _cursor, chunk in self._ordinary_workloads.values()
            )
        return {
            "active": active,
            "chunk_uncommitted": chunk_uncommitted if active else False,
            "completion_generation": self._completion_generation,
        }

    def workload_snapshot(self) -> dict[str, object]:
        with self._condition:
            return self.workload_snapshot_locked()

    def _require_open_locked(self) -> None:
        if self._closed:
            raise RuntimeReceiptError("runtime receipt coordinator was closed")
        if self._failed:
            raise RuntimeReceiptError("runtime receipt coordinator failed closed")

    def _next_time_locked(self) -> int:
        observed = self._monotonic_ns()
        _require_int(observed, "receipt monotonic time", minimum=1)
        if observed <= self._last_observed_monotonic_ns:
            observed = self._last_observed_monotonic_ns + 1
        if observed > MAX_GENERATION:
            raise RuntimeReceiptError("receipt monotonic time was exhausted")
        return observed

    def _validate_runtime_transition_locked(
        self,
        previous: Optional[Mapping[str, object]],
        current: Mapping[str, object],
    ) -> None:
        if previous is None:
            return
        for name in (
            "model_load_generation",
            "model_unload_generation",
            "cuda_oom_generation",
            "media_failure_generation",
        ):
            before = previous[name]
            after = current[name]
            if after < before or after > before + 1:
                raise RuntimeReceiptError(f"{name} transition was not lossless")

        was_resident = previous["model_resident"]
        is_resident = current["model_resident"]
        load_delta = (
            current["model_load_generation"] - previous["model_load_generation"]
        )
        unload_delta = (
            current["model_unload_generation"] - previous["model_unload_generation"]
        )
        if was_resident == is_resident:
            if load_delta or unload_delta:
                raise RuntimeReceiptError(
                    "model generation changed without a residency transition"
                )
            if is_resident and (
                current["model_identity_sha256"] != previous["model_identity_sha256"]
            ):
                raise RuntimeReceiptError(
                    "resident model identity changed without unload and reload"
                )
        elif is_resident:
            if load_delta != 1 or unload_delta != 0:
                raise RuntimeReceiptError("model load transition was invalid")
        elif unload_delta != 1 or load_delta != 0:
            raise RuntimeReceiptError("model unload transition was invalid")

    def _publish_locked(
        self,
        runtime_state: Optional[Mapping[str, object]],
        *,
        workload_sha256: Optional[str],
        active: bool,
        chunk_uncommitted: bool,
        active_cursor_ms: Optional[int],
        completed_cursor_ms: Optional[int],
        completion_generation: int,
    ) -> None:
        self._require_open_locked()
        if not self._config.enabled:
            return
        if runtime_state is None:
            raise RuntimeReceiptError("enabled Task 11B gate requires runtime state")
        current = _validate_runtime_state(runtime_state)
        self._validate_runtime_transition_locked(self._runtime_state, current)
        if self._sequence == MAX_GENERATION:
            raise RuntimeReceiptError("Task 11B receipt sequence was exhausted")
        sequence = self._sequence + 1
        observed = self._next_time_locked()
        record = {
            "schema": RECEIPT_SCHEMA,
            "runtime_epoch": self._identity.epoch,
            "gate_token_sha256": self._config.gate_token_sha256,
            "sequence": sequence,
            "observed_monotonic_ns": observed,
            "workload_sha256": workload_sha256,
            **current,
            "active": active,
            "chunk_uncommitted": chunk_uncommitted,
            "active_cursor_ms": active_cursor_ms,
            "completed_cursor_ms": completed_cursor_ms,
            "completion_generation": completion_generation,
        }
        if set(record) != RECEIPT_KEYS:
            raise RuntimeReceiptError("Task 11B receipt keys were invalid")
        payload = canonical_json_line(record)
        if len(payload) > MAX_RECEIPT_BYTES:
            raise RuntimeReceiptError("Task 11B receipt exceeded 4 KiB")
        try:
            self._journal.append(payload)
        except BaseException:
            self._failed = True
            raise
        self._sequence = sequence
        self._last_observed_monotonic_ns = observed
        self._runtime_state = current

    def initialize_locked(
        self, runtime_state: Optional[Mapping[str, object]] = None
    ) -> None:
        """Publish sequence one before any gate workload or model activity."""
        self._require_open_locked()
        if self._initialized:
            raise RuntimeReceiptError(
                "runtime receipt coordinator was already initialized"
            )
        self._publish_locked(
            runtime_state,
            workload_sha256=None,
            active=False,
            chunk_uncommitted=False,
            active_cursor_ms=None,
            completed_cursor_ms=None,
            completion_generation=self._completion_generation,
        )
        self._initialized = True

    def initialize(self, runtime_state: Optional[Mapping[str, object]] = None) -> None:
        with self._condition:
            self.initialize_locked(runtime_state)

    def _require_initialized_locked(self) -> None:
        self._require_open_locked()
        if self._config.enabled and not self._initialized:
            raise RuntimeReceiptError("Task 11B receipt journal was not initialized")

    def record_runtime_change_locked(
        self, runtime_state: Optional[Mapping[str, object]] = None
    ) -> None:
        """Durably retain one accepted priority/model/failure transition."""
        self._require_initialized_locked()
        self._publish_locked(
            runtime_state,
            workload_sha256=self._workload_sha256,
            active=self._active,
            chunk_uncommitted=self._chunk_uncommitted,
            active_cursor_ms=self._active_cursor_ms,
            completed_cursor_ms=self._completed_cursor_ms,
            completion_generation=self._completion_generation,
        )

    def record_runtime_change(
        self, runtime_state: Optional[Mapping[str, object]] = None
    ) -> None:
        with self._condition:
            self.record_runtime_change_locked(runtime_state)

    def _expected_gate_workload_locked(self) -> Optional[str]:
        if not self._config.enabled:
            return None
        if self._gate_phase == 0:
            return self._config.phase_a_workload_sha256
        if self._gate_phase == 1:
            return self._config.phase_b_workload_sha256
        return None

    def _new_token_locked(self) -> WorkloadToken:
        if self._next_token_serial == MAX_GENERATION:
            raise RuntimeReceiptError("workload token sequence was exhausted")
        self._next_token_serial += 1
        return WorkloadToken(self._token_owner, self._next_token_serial)

    def _require_token_locked(self, token: object) -> WorkloadToken:
        if (
            not isinstance(token, WorkloadToken)
            or token._owner is not self._token_owner
        ):
            raise RuntimeReceiptError("workload token was foreign or invalid")
        return token

    def bind_workload_locked(
        self,
        workload_sha256: Optional[str],
        *,
        cursor_ms: int = 0,
        runtime_state: Optional[Mapping[str, object]] = None,
    ) -> WorkloadToken:
        """Bind the next workload before model admission and publish it."""
        self._require_initialized_locked()
        cursor = _require_int(cursor_ms, "active_cursor_ms")
        if self._config.enabled and self._active:
            raise RuntimeReceiptError("concurrent runtime workloads are forbidden")
        if self._config.enabled:
            if self._gate_aborted:
                raise RuntimeReceiptError("Task 11B gate workload previously aborted")
            expected = self._expected_gate_workload_locked()
            if expected is None or workload_sha256 != expected:
                raise RuntimeReceiptError(
                    "Task 11B workload was foreign, repeated, or out of order"
                )
            _require_lower_hex(workload_sha256, "workload_sha256")
        elif workload_sha256 is not None:
            _require_lower_hex(workload_sha256, "workload_sha256")

        token = self._new_token_locked()
        if not self._config.enabled:
            self._ordinary_workloads[token] = (cursor, False)
            return token

        self._publish_locked(
            runtime_state,
            workload_sha256=workload_sha256,
            active=True,
            chunk_uncommitted=False,
            active_cursor_ms=cursor,
            completed_cursor_ms=None,
            completion_generation=self._completion_generation,
        )
        self._workload_sha256 = workload_sha256
        self._active = True
        self._chunk_uncommitted = False
        self._active_cursor_ms = cursor
        self._completed_cursor_ms = None
        self._gate_workload_token = token
        return token

    def bind_workload(
        self,
        workload_sha256: Optional[str],
        *,
        cursor_ms: int = 0,
        runtime_state: Optional[Mapping[str, object]] = None,
    ) -> WorkloadToken:
        with self._condition:
            return self.bind_workload_locked(
                workload_sha256,
                cursor_ms=cursor_ms,
                runtime_state=runtime_state,
            )

    begin_workload_locked = bind_workload_locked
    begin_workload = bind_workload

    def record_chunk_locked(
        self,
        token: WorkloadToken,
        *,
        cursor_ms: int,
        chunk_uncommitted: bool,
        runtime_state: Optional[Mapping[str, object]] = None,
    ) -> bool:
        """Publish cursor/chunk state; false returns mean there was no change."""
        self._require_initialized_locked()
        token = self._require_token_locked(token)
        if not self._config.enabled:
            try:
                old_cursor, old_chunk = self._ordinary_workloads[token]
            except KeyError as exc:
                raise RuntimeReceiptError(
                    "workload token was no longer active"
                ) from exc
            cursor = _require_int(cursor_ms, "active_cursor_ms")
            if cursor < old_cursor:
                raise RuntimeReceiptError("active cursor cannot regress")
            if type(chunk_uncommitted) is not bool:
                raise RuntimeReceiptError("chunk_uncommitted must be a boolean")
            if cursor == old_cursor and chunk_uncommitted == old_chunk:
                return False
            self._ordinary_workloads[token] = (cursor, chunk_uncommitted)
            return True
        if token is not self._gate_workload_token:
            raise RuntimeReceiptError("workload token was no longer active")
        if not self._active or self._active_cursor_ms is None:
            raise RuntimeReceiptError("no active workload accepted chunk state")
        cursor = _require_int(cursor_ms, "active_cursor_ms")
        if cursor < self._active_cursor_ms:
            raise RuntimeReceiptError("active cursor cannot regress")
        if type(chunk_uncommitted) is not bool:
            raise RuntimeReceiptError("chunk_uncommitted must be a boolean")
        if (
            cursor == self._active_cursor_ms
            and chunk_uncommitted == self._chunk_uncommitted
        ):
            return False
        self._publish_locked(
            runtime_state,
            workload_sha256=self._workload_sha256,
            active=True,
            chunk_uncommitted=chunk_uncommitted,
            active_cursor_ms=cursor,
            completed_cursor_ms=None,
            completion_generation=self._completion_generation,
        )
        self._active_cursor_ms = cursor
        self._chunk_uncommitted = chunk_uncommitted
        return True

    def record_chunk(
        self,
        token: WorkloadToken,
        *,
        cursor_ms: int,
        chunk_uncommitted: bool,
        runtime_state: Optional[Mapping[str, object]] = None,
    ) -> bool:
        with self._condition:
            return self.record_chunk_locked(
                token,
                cursor_ms=cursor_ms,
                chunk_uncommitted=chunk_uncommitted,
                runtime_state=runtime_state,
            )

    def abort_workload_locked(
        self,
        token: WorkloadToken,
        runtime_state: Optional[Mapping[str, object]] = None,
    ) -> None:
        """Publish cancellation/failure without advancing durable completion."""
        self._require_initialized_locked()
        token = self._require_token_locked(token)
        if not self._config.enabled:
            try:
                del self._ordinary_workloads[token]
            except KeyError as exc:
                raise RuntimeReceiptError(
                    "workload token was no longer active"
                ) from exc
            return
        if token is not self._gate_workload_token:
            raise RuntimeReceiptError("workload token was no longer active")
        if not self._active:
            raise RuntimeReceiptError("no active workload could be aborted")
        self._publish_locked(
            runtime_state,
            workload_sha256=self._workload_sha256,
            active=False,
            chunk_uncommitted=False,
            active_cursor_ms=None,
            completed_cursor_ms=None,
            completion_generation=self._completion_generation,
        )
        self._active = False
        self._chunk_uncommitted = False
        self._active_cursor_ms = None
        self._completed_cursor_ms = None
        self._gate_workload_token = None
        if self._config.enabled:
            self._gate_aborted = True

    def abort_workload(
        self,
        token: WorkloadToken,
        runtime_state: Optional[Mapping[str, object]] = None,
    ) -> None:
        with self._condition:
            self.abort_workload_locked(token, runtime_state)

    def complete_workload_locked(
        self,
        token: WorkloadToken,
        *,
        terminal_cursor_ms: int,
        runtime_state: Optional[Mapping[str, object]] = None,
    ) -> int:
        """Publish one completion only after the final subtitle is durable."""
        self._require_initialized_locked()
        token = self._require_token_locked(token)
        if not self._config.enabled:
            try:
                cursor, _chunk = self._ordinary_workloads[token]
            except KeyError as exc:
                raise RuntimeReceiptError(
                    "workload token was no longer active"
                ) from exc
            terminal = _require_int(terminal_cursor_ms, "completed_cursor_ms")
            if terminal < cursor:
                raise RuntimeReceiptError(
                    "completed cursor cannot precede active cursor"
                )
            if self._completion_generation == MAX_GENERATION:
                raise RuntimeReceiptError("completion generation was exhausted")
            del self._ordinary_workloads[token]
            self._completion_generation += 1
            return self._completion_generation
        if token is not self._gate_workload_token:
            raise RuntimeReceiptError("workload token was no longer active")
        if not self._active or self._active_cursor_ms is None:
            raise RuntimeReceiptError("no active workload could complete")
        terminal = _require_int(terminal_cursor_ms, "completed_cursor_ms")
        if terminal < self._active_cursor_ms:
            raise RuntimeReceiptError("completed cursor cannot precede active cursor")
        if self._completion_generation == MAX_GENERATION:
            raise RuntimeReceiptError("completion generation was exhausted")
        generation = self._completion_generation + 1
        self._publish_locked(
            runtime_state,
            workload_sha256=self._workload_sha256,
            active=False,
            chunk_uncommitted=False,
            active_cursor_ms=None,
            completed_cursor_ms=terminal,
            completion_generation=generation,
        )
        self._active = False
        self._chunk_uncommitted = False
        self._active_cursor_ms = None
        self._completed_cursor_ms = terminal
        self._completion_generation = generation
        self._gate_workload_token = None
        if self._config.enabled:
            self._gate_phase += 1
        return generation

    def complete_workload(
        self,
        token: WorkloadToken,
        *,
        terminal_cursor_ms: int,
        runtime_state: Optional[Mapping[str, object]] = None,
    ) -> int:
        with self._condition:
            return self.complete_workload_locked(
                token,
                terminal_cursor_ms=terminal_cursor_ms,
                runtime_state=runtime_state,
            )

    def close_locked(self) -> None:
        if not self._closed:
            if self._journal is not None:
                self._journal.close()
            self._closed = True

    def close(self) -> None:
        with self._condition:
            self.close_locked()
