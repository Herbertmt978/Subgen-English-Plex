"""Secure, policy-free consumption of a host-owned priority signal.

This leaf validates one coarse publication and its sequence.  Admission, yield,
recovery, and model-lifecycle policy remain in ``resource_management``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import stat
import threading
import time
from typing import Callable, Literal, Optional


MAX_SIGNAL_BYTES = 4096
MAX_GENERATION = (1 << 63) - 1
HEARTBEAT_MAX_AGE_NS = 10_000_000_000
SOURCE_MAX_AGE_NS = 30_000_000_000
MAX_TRACKED_PRODUCER_EPOCHS = 4096
_HEX_32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_BOOT_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_REASONS = {
    "higher_priority_busy",
    "higher_priority_degraded",
    "higher_priority_unavailable",
    "policy_drift",
}
_KEYS = {
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

PriorityState = Literal["disabled", "clear", "neutral", "asserted", "unavailable"]


@dataclass(frozen=True)
class PriorityObservation:
    """One immutable, privacy-safe priority-signal observation."""

    state: PriorityState
    configured: bool = True
    heartbeat_age_ms: Optional[int] = None
    source_age_ms: Optional[int] = None
    policy_sha256: Optional[str] = None
    observation_digest: Optional[str] = None
    producer_epoch: Optional[str] = None
    sequence: Optional[int] = None
    observed_monotonic_ns: Optional[int] = None
    source_generation: Optional[int] = None
    source_observed_monotonic_ns: Optional[int] = None
    reason_codes: tuple[str, ...] = ()
    accepted: bool = False
    new_publication: bool = False
    producer_epoch_changed: bool = False
    sequence_gap: bool = False


@dataclass(frozen=True)
class PrioritySignalSnapshot:
    """Injected file-boundary facts used by the parser and Windows tests."""

    raw: bytes
    parent_uid: int
    parent_mode: int
    file_uid: int
    file_mode: int
    parent_is_directory: bool = True
    file_is_regular: bool = True
    stable_inode: bool = True


class _DuplicateKey(ValueError):
    pass


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not 1 <= value <= MAX_GENERATION:
        raise ValueError(f"{name} is out of range")
    return value


def _default_uid() -> int:
    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        raise OSError("effective uid is unavailable")
    return int(getter())


def _default_boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii")


def canonical_boot_id_sha256(raw: str) -> str:
    """Hash one canonical Linux boot ID without retaining the private value."""

    if not isinstance(raw, str):
        raise ValueError("Host boot ID is unavailable")
    value = raw[:-1] if raw.endswith("\n") else raw
    if raw not in {value, value + "\n"} or _BOOT_ID.fullmatch(value) is None:
        raise ValueError("Host boot ID is noncanonical")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def encode_priority_publication(
    *,
    boot_id_sha256: str,
    producer_epoch: str,
    sequence: int,
    observed_monotonic_ns: int,
    source_generation: int,
    source_observed_monotonic_ns: int,
    observation_id: str,
    policy_sha256: str,
    pressure: bool,
    clear_eligible: bool,
    reason_codes: tuple[str, ...] | list[str],
) -> bytes:
    """Encode the one canonical producer/consumer signal contract."""

    for name, value, pattern in (
        ("boot_id_sha256", boot_id_sha256, _HEX_64),
        ("producer_epoch", producer_epoch, _HEX_32),
        ("observation_id", observation_id, _HEX_64),
        ("policy_sha256", policy_sha256, _HEX_64),
    ):
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise ValueError(f"{name} is invalid")
    sequence = _positive_int(sequence, "sequence")
    observed_monotonic_ns = _positive_int(
        observed_monotonic_ns, "observed_monotonic_ns"
    )
    source_generation = _positive_int(source_generation, "source_generation")
    source_observed_monotonic_ns = _positive_int(
        source_observed_monotonic_ns,
        "source_observed_monotonic_ns",
    )
    if source_observed_monotonic_ns > observed_monotonic_ns:
        raise ValueError("Priority observation times are inconsistent")
    if type(pressure) is not bool or type(clear_eligible) is not bool:
        raise ValueError("Priority state flags must be booleans")
    if not isinstance(reason_codes, (tuple, list)):
        raise ValueError("Priority reason codes are invalid")
    reasons = list(reason_codes)
    if (
        any(not isinstance(item, str) for item in reasons)
        or reasons != sorted(set(reasons))
        or len(reasons) > 4
        or any(item not in _REASONS for item in reasons)
    ):
        raise ValueError("Priority reason codes are invalid")
    if pressure:
        if clear_eligible or not reasons:
            raise ValueError("Asserted priority signal is inconsistent")
    elif clear_eligible:
        if reasons:
            raise ValueError("Clear priority signal is inconsistent")
    elif reasons:
        raise ValueError("Neutral priority signal is inconsistent")
    value = {
        "schema": 1,
        "boot_id_sha256": boot_id_sha256,
        "producer_epoch": producer_epoch,
        "sequence": sequence,
        "observed_monotonic_ns": observed_monotonic_ns,
        "source_generation": source_generation,
        "source_observed_monotonic_ns": source_observed_monotonic_ns,
        "observation_id": observation_id,
        "policy_sha256": policy_sha256,
        "pressure": pressure,
        "clear_eligible": clear_eligible,
        "reason_codes": reasons,
    }
    encoded = (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )
    if len(encoded) > MAX_SIGNAL_BYTES:
        raise ValueError("Priority signal size is invalid")
    return encoded


def _default_snapshot(path: str) -> Optional[PrioritySignalSnapshot]:
    parent, leaf = os.path.split(path)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_flags |= getattr(os, "O_NONBLOCK", 0)
    parent_fd = None
    file_fd = None
    try:
        parent_fd = os.open(parent, directory_flags)
        parent_before = os.fstat(parent_fd)
        entry_before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(entry_before.st_mode):
            parent_after = os.fstat(parent_fd)
            return PrioritySignalSnapshot(
                raw=b"",
                parent_uid=parent_before.st_uid,
                parent_mode=stat.S_IMODE(parent_before.st_mode),
                file_uid=entry_before.st_uid,
                file_mode=stat.S_IMODE(entry_before.st_mode),
                parent_is_directory=stat.S_ISDIR(parent_before.st_mode),
                file_is_regular=False,
                stable_inode=(
                    (parent_before.st_dev, parent_before.st_ino)
                    == (parent_after.st_dev, parent_after.st_ino)
                ),
            )
        file_fd = os.open(leaf, file_flags, dir_fd=parent_fd)
        file_before = os.fstat(file_fd)
        entry_after_open = os.stat(
            leaf, dir_fd=parent_fd, follow_symlinks=False
        )
        chunks = []
        remaining = MAX_SIGNAL_BYTES + 1
        while remaining:
            chunk = os.read(file_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        file_after = os.fstat(file_fd)
        entry_after_read = os.stat(
            leaf, dir_fd=parent_fd, follow_symlinks=False
        )
        parent_after = os.fstat(parent_fd)
        def entry_identity(value):
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        stable = (
            entry_identity(entry_before)
            == entry_identity(file_before)
            == entry_identity(entry_after_open)
            == entry_identity(file_after)
            == entry_identity(entry_after_read)
            and (parent_before.st_dev, parent_before.st_ino)
            == (parent_after.st_dev, parent_after.st_ino)
        )
        return PrioritySignalSnapshot(
            raw=b"".join(chunks),
            parent_uid=parent_before.st_uid,
            parent_mode=stat.S_IMODE(parent_before.st_mode),
            file_uid=file_before.st_uid,
            file_mode=stat.S_IMODE(file_before.st_mode),
            parent_is_directory=stat.S_ISDIR(parent_before.st_mode),
            file_is_regular=stat.S_ISREG(file_before.st_mode),
            stable_inode=stable,
        )
    except (OSError, NotImplementedError, ValueError):
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)


class PriorityPressureReader:
    """Read and sequence-check one configured signal without applying policy."""

    def __init__(
        self,
        signal_path: Optional[str],
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        uid_reader: Callable[[], int] = _default_uid,
        boot_id_reader: Callable[[], str] = _default_boot_id,
        snapshot_reader: Callable[[str], Optional[PrioritySignalSnapshot]] = (
            _default_snapshot
        ),
    ) -> None:
        if signal_path is not None and not isinstance(signal_path, str):
            raise TypeError("Priority signal path must be a string or None")
        if signal_path == "":
            signal_path = None
        if signal_path is not None:
            posix_absolute = signal_path.startswith("/")
            if not posix_absolute and not os.path.isabs(signal_path):
                raise ValueError("Priority signal path must be absolute")
            normalized = (
                posixpath.normpath(signal_path)
                if posix_absolute
                else os.path.normpath(signal_path)
            )
            if normalized != signal_path or not os.path.basename(signal_path):
                raise ValueError("Priority signal path must be canonical")
        self.signal_path = signal_path
        self._clock_ns = clock_ns
        self._uid_reader = uid_reader
        self._boot_id_reader = boot_id_reader
        self._snapshot_reader = snapshot_reader
        self._lock = threading.RLock()
        self._seen_epoch: Optional[str] = None
        self._accepted_epochs: set[str] = set()
        self._epoch_history_saturated = False
        self._seen_sequence: Optional[int] = None
        self._seen_source_generation: Optional[int] = None
        self._seen_source_observed_ns: Optional[int] = None
        self._seen_payload_sha256: Optional[str] = None
        self._last_accepted: Optional[PriorityObservation] = None
        self._unavailable_latched = False

    @property
    def configured(self) -> bool:
        return self.signal_path is not None

    @staticmethod
    def _bounded_age_ms(now_ns: int, observed_ns: Optional[int]) -> Optional[int]:
        if observed_ns is None:
            return None
        return min(60_000, max(0, now_ns - observed_ns) // 1_000_000)

    def _unavailable(self, now_ns: Optional[int] = None) -> PriorityObservation:
        self._unavailable_latched = True
        if self._last_accepted is None:
            return PriorityObservation(state="unavailable", configured=True)
        if now_ns is None:
            try:
                now_ns = self._current_time()
            except (TypeError, ValueError):
                now_ns = None
        return replace(
            self._last_accepted,
            state="unavailable",
            heartbeat_age_ms=(
                self._last_accepted.heartbeat_age_ms
                if now_ns is None
                else self._bounded_age_ms(
                    now_ns,
                    self._last_accepted.observed_monotonic_ns,
                )
            ),
            source_age_ms=(
                self._last_accepted.source_age_ms
                if now_ns is None
                else self._bounded_age_ms(
                    now_ns,
                    self._last_accepted.source_observed_monotonic_ns,
                )
            ),
            accepted=False,
            new_publication=False,
            producer_epoch_changed=False,
            sequence_gap=False,
            reason_codes=(),
        )

    def _current_time(self) -> int:
        value = self._clock_ns()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Priority clock must return a non-negative integer")
        return value

    def _expected_boot_hash(self) -> str:
        return canonical_boot_id_sha256(self._boot_id_reader())

    @staticmethod
    def _decode(raw: bytes) -> dict:
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_SIGNAL_BYTES:
            raise ValueError("Priority signal size is invalid")
        if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
            raise ValueError("Priority signal must have one trailing newline")
        text = raw.decode("ascii", "strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
        if not isinstance(value, dict) or set(value) != _KEYS:
            raise ValueError("Priority signal keys are invalid")
        canonical = (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
        if canonical != raw:
            raise ValueError("Priority signal bytes are noncanonical")
        return value

    def _validate_snapshot(self, snapshot: PrioritySignalSnapshot, uid: int) -> bytes:
        if not isinstance(snapshot, PrioritySignalSnapshot):
            raise TypeError("snapshot_reader must return PrioritySignalSnapshot or None")
        if (
            not snapshot.parent_is_directory
            or not snapshot.file_is_regular
            or not snapshot.stable_inode
            or snapshot.parent_uid != uid
            or snapshot.file_uid != uid
            or snapshot.parent_mode != 0o700
            or snapshot.file_mode != 0o600
        ):
            raise ValueError("Priority signal ownership or mode is unsafe")
        return snapshot.raw

    def _validate_value(self, value: dict, now_ns: int, boot_hash: str) -> dict:
        if value["schema"] != 1 or isinstance(value["schema"], bool):
            raise ValueError("Priority signal schema is invalid")
        for name in (
            "boot_id_sha256",
            "observation_id",
            "policy_sha256",
        ):
            if not isinstance(value[name], str) or _HEX_64.fullmatch(value[name]) is None:
                raise ValueError(f"{name} is invalid")
        if value["boot_id_sha256"] != boot_hash:
            raise ValueError("Priority signal belongs to another host boot")
        epoch = value["producer_epoch"]
        if not isinstance(epoch, str) or _HEX_32.fullmatch(epoch) is None:
            raise ValueError("producer_epoch is invalid")
        sequence = _positive_int(value["sequence"], "sequence")
        observed_ns = _positive_int(
            value["observed_monotonic_ns"], "observed_monotonic_ns"
        )
        source_generation = _positive_int(
            value["source_generation"], "source_generation"
        )
        source_observed_ns = _positive_int(
            value["source_observed_monotonic_ns"],
            "source_observed_monotonic_ns",
        )
        if not source_observed_ns <= observed_ns <= now_ns:
            raise ValueError("Priority observation times are inconsistent")
        heartbeat_age = now_ns - observed_ns
        source_age = now_ns - source_observed_ns
        if heartbeat_age > HEARTBEAT_MAX_AGE_NS or source_age > SOURCE_MAX_AGE_NS:
            raise ValueError("Priority observation is stale")
        pressure = value["pressure"]
        clear_eligible = value["clear_eligible"]
        if type(pressure) is not bool or type(clear_eligible) is not bool:
            raise ValueError("Priority state flags must be booleans")
        reasons = value["reason_codes"]
        if (
            not isinstance(reasons, list)
            or any(not isinstance(item, str) for item in reasons)
            or reasons != sorted(set(reasons))
            or len(reasons) > 4
            or any(item not in _REASONS for item in reasons)
        ):
            raise ValueError("Priority reason codes are invalid")
        if pressure:
            if clear_eligible or not reasons:
                raise ValueError("Asserted priority signal is inconsistent")
            state = "asserted"
        elif clear_eligible:
            if reasons:
                raise ValueError("Clear priority signal is inconsistent")
            state = "clear"
        else:
            if reasons:
                raise ValueError("Neutral priority signal is inconsistent")
            state = "neutral"
        return {
            "state": state,
            "epoch": epoch,
            "sequence": sequence,
            "observed_ns": observed_ns,
            "source_generation": source_generation,
            "source_observed_ns": source_observed_ns,
            "heartbeat_age_ms": min(60_000, heartbeat_age // 1_000_000),
            "source_age_ms": min(60_000, source_age // 1_000_000),
            "policy_sha256": value["policy_sha256"],
            "observation_digest": hashlib.sha256(
                value["observation_id"].encode("ascii")
            ).hexdigest(),
            "reason_codes": tuple(reasons),
        }

    def _read_locked(self) -> PriorityObservation:
        if not self.configured:
            return PriorityObservation(state="disabled", configured=False)
        if self._epoch_history_saturated:
            return self._unavailable()
        now_ns = None
        try:
            now_ns = self._current_time()
            uid = self._uid_reader()
            if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
                raise ValueError("Effective uid is invalid")
            snapshot = self._snapshot_reader(self.signal_path)
            if snapshot is None:
                return self._unavailable(now_ns)
            raw = self._validate_snapshot(snapshot, uid)
            value = self._decode(raw)
            parsed = self._validate_value(value, now_ns, self._expected_boot_hash())
        except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return self._unavailable(now_ns)

        first_epoch = self._seen_epoch is None
        epoch_changed = parsed["epoch"] != self._seen_epoch
        payload_sha = hashlib.sha256(raw).hexdigest()
        if epoch_changed:
            if not first_epoch and parsed["sequence"] != 1:
                return self._unavailable(now_ns)
            if parsed["epoch"] in self._accepted_epochs:
                return self._unavailable(now_ns)
            if len(self._accepted_epochs) >= MAX_TRACKED_PRODUCER_EPOCHS:
                self._epoch_history_saturated = True
                return self._unavailable(now_ns)
            # A newly started consumer may meet an already-running producer.
            # Checkpoint that valid sequence fail-closed; only its exact next
            # publication may begin ordinary recovery. Later epoch changes
            # still require sequence one so replay protection stays exact.
            gap = first_epoch and parsed["sequence"] != 1
        else:
            if self._seen_sequence is None:
                return self._unavailable(now_ns)
            if parsed["sequence"] == self._seen_sequence:
                if payload_sha != self._seen_payload_sha256:
                    return self._unavailable(now_ns)
                if self._last_accepted is None:
                    return self._unavailable(now_ns)
                if self._unavailable_latched:
                    return self._unavailable(now_ns)
                return replace(
                    self._last_accepted,
                    heartbeat_age_ms=parsed["heartbeat_age_ms"],
                    source_age_ms=parsed["source_age_ms"],
                    new_publication=False,
                    producer_epoch_changed=False,
                )
            if parsed["sequence"] < self._seen_sequence:
                return self._unavailable(now_ns)
            gap = parsed["sequence"] != self._seen_sequence + 1
            if parsed["source_generation"] < self._seen_source_generation:
                return self._unavailable(now_ns)
            if parsed["source_generation"] == self._seen_source_generation:
                if parsed["source_observed_ns"] != self._seen_source_observed_ns:
                    return self._unavailable(now_ns)
            elif parsed["source_observed_ns"] <= self._seen_source_observed_ns:
                return self._unavailable(now_ns)

        self._seen_epoch = parsed["epoch"]
        self._accepted_epochs.add(parsed["epoch"])
        self._seen_sequence = parsed["sequence"]
        self._seen_source_generation = parsed["source_generation"]
        self._seen_source_observed_ns = parsed["source_observed_ns"]
        self._seen_payload_sha256 = payload_sha
        if gap:
            # The publication is not accepted as health evidence, but its
            # validated source generation is the new replay checkpoint. Carry
            # only that barrier so an exact-next heartbeat of the same source
            # generation cannot count toward recovery.
            return replace(
                self._unavailable(now_ns),
                source_generation=parsed["source_generation"],
                sequence_gap=True,
            )

        observation = PriorityObservation(
            state=parsed["state"],
            configured=True,
            heartbeat_age_ms=parsed["heartbeat_age_ms"],
            source_age_ms=parsed["source_age_ms"],
            policy_sha256=parsed["policy_sha256"],
            observation_digest=parsed["observation_digest"],
            producer_epoch=parsed["epoch"],
            sequence=parsed["sequence"],
            observed_monotonic_ns=parsed["observed_ns"],
            source_generation=parsed["source_generation"],
            source_observed_monotonic_ns=parsed["source_observed_ns"],
            reason_codes=parsed["reason_codes"],
            accepted=True,
            new_publication=True,
            producer_epoch_changed=epoch_changed,
        )
        self._last_accepted = observation
        self._unavailable_latched = False
        return observation

    def read(self) -> PriorityObservation:
        with self._lock:
            return self._read_locked()

    __call__ = read


__all__ = [
    "PriorityObservation",
    "PriorityPressureReader",
    "PrioritySignalSnapshot",
    "PriorityState",
    "canonical_boot_id_sha256",
    "encode_priority_publication",
]
