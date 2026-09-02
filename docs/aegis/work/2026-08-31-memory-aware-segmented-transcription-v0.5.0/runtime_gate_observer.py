#!/usr/bin/env python3
"""Owner-operated composite workload observer for the Task 11B runtime gate.

This host-side tool is deliberately outside the Subgen image.  It imports the
frozen health sampler for immutable Docker binding, telemetry, log scanning,
evidence publication, and exact-ID cleanup.  It adds only the workload protocol
needed to prove the automatic runtime: a 31-minute atomic subtitle, a resident
short batch, the idle unload/recovery cycle, a post-unload reload, and retained
invalid/silent controls.

The fixture manifest contains disposable relative paths, but no path, token,
camera identifier, endpoint, or raw log line is written to evidence.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
import hmac
import http.client
import json
import math
import os
import posixpath
import re
import shlex
import stat
import struct
import sys
import threading
import time
import types
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable


# The adjacent sampler is intentionally not imported here.  The CLI reads and
# hashes its exact bytes independently, then executes only those verified bytes.
# Focused unit tests inject the adjacent module after importing this observer.
health: Any = None


FIXTURE_SCHEMA = "subgen.task11b.runtime-fixtures/v1"
OBSERVER_SCHEMA = "subgen.task11b.runtime-observer/v1"
MAX_FIXTURE_MANIFEST_BYTES = 32 * 1024
MAX_API_KEY_BYTES = 4 * 1024
MIB = 1024**2
GIB = 1024**3
MAX_MEDIA_BYTES = 16 * GIB
MAX_SUBTITLE_BYTES = 64 * MIB
MAX_HTTP_RESPONSE_BYTES = 64 * 1024
MAX_EVENT_LOG_BYTES = 512 * 1024
MAX_EVENT_IDENTITIES = 8192
MAX_RETAINED_EVENT_BYTES = 256 * 1024
MAX_OBSERVER_EVIDENCE_BYTES = 1536 * 1024
MAX_SAMPLER_SOURCE_BYTES = 4 * MIB
MAX_PRIORITY_SIGNAL_BYTES = 4 * 1024
MAX_RUNTIME_RECEIPT_BYTES = 4 * 1024
MAX_RUNTIME_RECEIPT_JOURNAL_BYTES = 8 * MIB
LONG_MINIMUM_SECONDS = 31 * 60
LONG_MAXIMUM_SECONDS = 32 * 60
SHORT_MAXIMUM_SECONDS = 10 * 60
MIN_RECOVERY_SPAN_SECONDS = 10.0
SRT_MINIMUM_TIMELINE_COVERAGE = 0.80
EXPECTED_FIXTURE_DIRECTORIES = {"long", "short", "reload", "invalid", "silent"}
SRT_TIMING_RE = re.compile(
    r"^(?P<sh>\d{2,}):(?P<sm>\d{2}):(?P<ss>\d{2}),(?P<sms>\d{3})"
    r" --> "
    r"(?P<eh>\d{2,}):(?P<em>\d{2}):(?P<es>\d{2}),(?P<ems>\d{3})$"
)
SUBGEN_EVENT_PREFIX = "SUBGEN_EVENT "
SILENT_EVENT_RE = re.compile(
    r"MEDIA_VALIDATION outcome=no_audio ffprobe=no_audio pyav=no_audio "
    r"path=(?P<path>/media/(?:long|short|reload|invalid|silent)/[^\s]+)$"
)
SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
DEFAULT_FRIGATE_STATS_URL = "http://127.0.0.1:5000/api/stats"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/ps"
DEFAULT_CANDIDATE_STATUS_URL = "http://127.0.0.1:19000/status"
LOWER_HEX_32_RE = re.compile(r"^[0-9a-f]{32}$")
LOWER_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
PRIORITY_SIGNAL_KEYS = {
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
MODEL_CATALOG_ENTRY_KEYS = {"image_identity", "runtime", "policy", "measurements"}
MODEL_CATALOG_RUNTIME_KEYS = {
    "stable_ts_version",
    "faster_whisper_version",
    "ctranslate2_version",
    "cuda_runtime_version",
    "driver_version",
    "device_name",
    "compute_capability",
    "total_vram_bytes",
}
MODEL_CATALOG_POLICY_KEYS = {
    "model",
    "model_revision",
    "compute_type",
    "task",
    "inference_concurrency",
    "chunk_minutes",
    "decoder_options_sha256",
}
MODEL_CATALOG_MEASUREMENT_KEYS = {
    "runs",
    "host_preload_used_bytes",
    "host_peak_used_bytes",
    "cgroup_preload_used_bytes",
    "cgroup_peak_used_bytes",
    "device_preload_used_bytes",
    "device_peak_used_bytes",
    "host_incremental_peak_bytes",
    "cgroup_incremental_peak_bytes",
    "device_incremental_peak_bytes",
    "host_margin_bytes",
    "device_margin_bytes",
}
PRIORITY_POLICY_KEYS = {
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
UNLOADED_ENVELOPE_KEYS = {
    "schema",
    "runtime_commit",
    "image",
    "gpu",
    "backend",
    "model_policy",
    "measurement",
}
EXECUTION_BOUNDARY_KEYS = {
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
SAMPLER_BINDING_KEYS = {
    "schema",
    "sampler_commit",
    "sampler_blob",
    "sampler_sha256",
    "test_blob",
    "test_sha256",
    "observer_blob",
    "observer_sha256",
    "observer_test_blob",
    "observer_test_sha256",
    "gate_seal_sha256",
    "producer_sha256",
    "policy_sha256",
    "unloaded_gpu_envelope_sha256",
    "model_envelope_catalog_sha256",
    "execution_boundary_manifest_sha256",
}
PHASE_A_EVENT_KINDS = (
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
PHASE_A_HOST_OBSERVATION_KEYS = {
    "monotonic_ns",
    "candidate_bytes",
    "output_count",
    "marker_count",
    "output_create_count",
    "marker_create_count",
    "threshold_masking_allowed",
}
PHASE_B_HOST_SAMPLE_KEYS = {
    "candidate_running",
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
PHASE_B_LIFECYCLE_STAGES = (
    "baseline",
    "reset_timestamp",
    "phase_b",
    "seal",
    "live_drain",
    "stop",
    "log_eof",
    "final_cgroup_drain",
    "final_kernel_drain",
    "final_receipt_drain",
    "cgroup_cleanup",
    "watcher_snapshot",
    "watcher_close",
    "final_gate_write",
)

GATE_CGROUP_NAME_PREFIX = "subgentask11b"
GATE_CGROUP_REQUIRED_CONTROLLERS = frozenset({"memory", "pids"})
GATE_CGROUP_MAX_FILE_BYTES = 64 * 1024


class ObserverBootstrapAbort(RuntimeError):
    """A fail-closed error available before the verified sampler is loaded."""

    def __init__(self, message: str) -> None:
        code = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")[:96]
        self.code = code or "observer_bootstrap_abort"
        super().__init__(self.code)


@dataclass(frozen=True)
class FixtureItem:
    role: str
    index: int
    media_relative: str
    subtitle_relative: str | None

    @property
    def container_media(self) -> str:
        return "/media/" + self.media_relative

    @property
    def container_directory(self) -> str:
        return "/media/" + self.media_relative.split("/", 1)[0]


@dataclass(frozen=True)
class FileSnapshot:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    links: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str
    subtitle_cue_count: int | None = None
    subtitle_first_start_ms: int | None = None
    subtitle_last_end_ms: int | None = None


@dataclass(frozen=True)
class FixtureSet:
    manifest_sha256: str
    media_root: Path
    runtime_uid: int
    runtime_gid: int
    long: FixtureItem
    short: tuple[FixtureItem, ...]
    reload: FixtureItem
    invalid: FixtureItem
    silent: FixtureItem

    @property
    def all_items(self) -> tuple[FixtureItem, ...]:
        return (self.long, *self.short, self.reload, self.invalid, self.silent)


@dataclass(frozen=True)
class RuntimeStatusObservation:
    sequence: int
    observed_monotonic: float
    state: str
    recovery_reason: str | None
    admission_open: bool


@dataclass(frozen=True)
class RuntimeRecoveryProof:
    recovering_sequence: int
    normal_sequence: int
    complete_health_polls: int
    elapsed_seconds: float


@dataclass(frozen=True)
class PriorityAssertion:
    """One inode-bound assertion observation and its host-defined T0."""

    document: dict[str, Any]
    payload: bytes
    attestation: dict[str, Any]
    t0_monotonic_ns: int


@dataclass(frozen=True)
class HealthBaselines:
    candidate_restart_count: int
    frigate_restart_count: int


@dataclass(frozen=True)
class PhaseFreshnessMark:
    event_sequence: int
    publication_sequence: int


_BOOTSTRAPPED_OBSERVER_SHA256: str | None = None
_BOOTSTRAPPED_SAMPLER_SHA256: str | None = None
_BOOTSTRAPPED_OBSERVER_PAYLOAD: bytes | None = None
_BOOTSTRAPPED_SAMPLER_PAYLOAD: bytes | None = None
_RELEASE_BINDING: dict[str, Any] | None = None


def _owner_id() -> int | None:
    getter = getattr(os, "geteuid", None)
    return getter() if callable(getter) else None


def _safe_code(message: str) -> BaseException:
    if health is not None and hasattr(health, "GateAbort"):
        return health.GateAbort(message)
    return ObserverBootstrapAbort(message)


def _canonical_ascii_json_line(value: Any, *, label: str) -> bytes:
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
        raise _safe_code(f"{label} was not canonicalizable") from exc


def _strict_json_line(payload: bytes, *, label: str, maximum: int) -> dict[str, Any]:
    if not payload or len(payload) > maximum or not payload.endswith(b"\n"):
        raise _safe_code(f"{label} was empty oversized or unterminated")
    try:
        document = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=(
                health._reject_duplicate_object
                if health is not None and hasattr(health, "_reject_duplicate_object")
                else _reject_duplicate_json_object
            ),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"nonfinite JSON constant {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _safe_code(f"{label} was malformed") from exc
    if not isinstance(document, dict):
        raise _safe_code(f"{label} was not an object")
    if _canonical_ascii_json_line(document, label=label) != payload:
        raise _safe_code(f"{label} bytes were not canonical")
    return document


def _strict_canonical_json_document(
    payload: bytes, *, label: str, maximum: int, trailing_newline: bool
) -> dict[str, Any]:
    if not payload or len(payload) > maximum:
        raise _safe_code(f"{label} was empty or oversized")
    try:
        document = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_reject_duplicate_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"nonfinite JSON constant {value}")
            ),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise _safe_code(f"{label} was malformed") from exc
    if not isinstance(document, dict):
        raise _safe_code(f"{label} was not an object")
    expected = _canonical_ascii_json_line(document, label=label)
    if not trailing_newline:
        expected = expected[:-1]
    if payload != expected:
        raise _safe_code(f"{label} bytes were not canonical")
    return document


def _reject_duplicate_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_sampler_binding(evidence_payload: bytes, prefix: str) -> dict[str, Any]:
    """Extract exactly one canonical privacy-safe binding from bounded evidence."""
    if (
        not isinstance(evidence_payload, bytes)
        or not evidence_payload
        or len(evidence_payload) > MAX_OBSERVER_EVIDENCE_BYTES
        or not isinstance(prefix, str)
        or not 1 <= len(prefix) <= 256
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in prefix)
    ):
        raise _safe_code("sampler binding evidence or prefix was invalid")
    marker = prefix.encode("ascii")
    matches = [
        line[len(marker) :]
        for line in evidence_payload.splitlines()
        if line.startswith(marker)
    ]
    if len(matches) != 1:
        raise _safe_code("sampler binding line was missing or duplicated")
    binding = _strict_json_line(
        matches[0] + b"\n",
        label="sampler binding",
        maximum=16 * 1024,
    )
    if (
        set(binding) != SAMPLER_BINDING_KEYS
        or binding.get("schema") != "subgen.task11b.sampler-binding/v1"
        or not isinstance(binding.get("sampler_commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", binding["sampler_commit"]) is None
    ):
        raise _safe_code("sampler binding schema was invalid")
    for key in ("sampler_blob", "test_blob", "observer_blob", "observer_test_blob"):
        if (
            not isinstance(binding.get(key), str)
            or re.fullmatch(r"[0-9a-f]{40}", binding[key]) is None
        ):
            raise _safe_code("sampler binding Git object identity was invalid")
    for key in SAMPLER_BINDING_KEYS - {
        "schema",
        "sampler_commit",
        "sampler_blob",
        "test_blob",
        "observer_blob",
        "observer_test_blob",
    }:
        if (
            not isinstance(binding.get(key), str)
            or LOWER_HEX_64_RE.fullmatch(binding[key]) is None
        ):
            raise _safe_code("sampler binding SHA-256 identity was invalid")
    return binding


def _exact_int(value: Any, *, minimum: int = 0, maximum: int = 2**63 - 1) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _exact_nullable_hex(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and LOWER_HEX_64_RE.fullmatch(value)
    )


def _exact_nullable_int(
    value: Any, *, minimum: int = 0, maximum: int = 2**63 - 1
) -> bool:
    return value is None or _exact_int(value, minimum=minimum, maximum=maximum)


def validate_priority_assertion(
    document: dict[str, Any],
    payload: bytes,
    *,
    expected_policy_sha256: str,
) -> dict[str, Any]:
    """Validate preserved observation N without exposing its opaque identity."""
    if (
        _strict_json_line(
            payload,
            label="priority assertion observation",
            maximum=MAX_PRIORITY_SIGNAL_BYTES,
        )
        != document
    ):
        raise _safe_code("priority assertion document disagreed with canonical bytes")
    integers = (
        document.get("sequence"),
        document.get("observed_monotonic_ns"),
        document.get("source_generation"),
        document.get("source_observed_monotonic_ns"),
    )
    reasons = document.get("reason_codes")
    if (
        set(document) != PRIORITY_SIGNAL_KEYS
        or document.get("schema") != 1
        or not all(_exact_int(value, minimum=1) for value in integers)
        or document["source_observed_monotonic_ns"] > document["observed_monotonic_ns"]
        or not isinstance(document.get("boot_id_sha256"), str)
        or not LOWER_HEX_64_RE.fullmatch(document["boot_id_sha256"])
        or not isinstance(document.get("producer_epoch"), str)
        or not LOWER_HEX_32_RE.fullmatch(document["producer_epoch"])
        or not isinstance(document.get("observation_id"), str)
        or not LOWER_HEX_64_RE.fullmatch(document["observation_id"])
        or document.get("policy_sha256") != expected_policy_sha256
        or type(document.get("pressure")) is not bool
        or type(document.get("clear_eligible")) is not bool
        or document["pressure"] is not True
        or document["clear_eligible"] is not False
        or not isinstance(reasons, list)
        or reasons != sorted(set(reasons))
        or not reasons
        or len(reasons) > 2
        or not set(reasons).issubset(
            {"higher_priority_busy", "higher_priority_degraded"}
        )
    ):
        raise _safe_code("priority assertion observation was not qualifying")
    return {
        "source_generation": document["source_generation"],
        "observed_monotonic_ns": document["observed_monotonic_ns"],
        "producer_epoch": document["producer_epoch"],
        "sequence": document["sequence"],
        "reason_codes": list(reasons),
        "observation_digest": hashlib.sha256(
            document["observation_id"].encode("ascii")
        ).hexdigest(),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }


def open_priority_assertion(
    path: Path,
    *,
    expected_policy_sha256: str,
    expected_boot_id_sha256: str,
    expected_producer_epoch: str,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> PriorityAssertion:
    """Open and validate final signal N, then define T0 as the next operation."""
    if (
        not path.is_absolute()
        or not isinstance(expected_policy_sha256, str)
        or not LOWER_HEX_64_RE.fullmatch(expected_policy_sha256)
        or not isinstance(expected_boot_id_sha256, str)
        or not LOWER_HEX_64_RE.fullmatch(expected_boot_id_sha256)
        or not isinstance(expected_producer_epoch, str)
        or not LOWER_HEX_32_RE.fullmatch(expected_producer_epoch)
    ):
        raise _safe_code("priority assertion binding was invalid")
    try:
        parent_lstat = path.parent.lstat()
        initial_path = path.lstat()
        resolved_parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise _safe_code("priority assertion path was unavailable") from exc
    owner = _owner_id()
    if (
        resolved_parent != path.parent.absolute()
        or stat.S_ISLNK(parent_lstat.st_mode)
        or not stat.S_ISDIR(parent_lstat.st_mode)
        or stat.S_ISLNK(initial_path.st_mode)
        or not stat.S_ISREG(initial_path.st_mode)
        or (owner is not None and parent_lstat.st_uid != owner)
        or (owner is not None and stat.S_IMODE(parent_lstat.st_mode) != 0o700)
        or (owner is not None and initial_path.st_uid != owner)
        or (owner is not None and stat.S_IMODE(initial_path.st_mode) != 0o600)
    ):
        raise _safe_code("priority assertion ownership mode or type was unsafe")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _safe_code("priority assertion could not be opened") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > MAX_PRIORITY_SIGNAL_BYTES
            or (before.st_dev, before.st_ino)
            != (initial_path.st_dev, initial_path.st_ino)
        ):
            raise _safe_code("priority assertion identity was unsafe")
        payload = _read_all_fd(
            descriptor,
            MAX_PRIORITY_SIGNAL_BYTES,
            label="priority assertion observation",
        )
        after = os.fstat(descriptor)
        identity_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(before, field) != getattr(after, field) for field in identity_fields
        ):
            raise _safe_code("priority assertion changed while read")
        document = _strict_json_line(
            payload,
            label="priority assertion observation",
            maximum=MAX_PRIORITY_SIGNAL_BYTES,
        )
        attestation = validate_priority_assertion(
            document,
            payload,
            expected_policy_sha256=expected_policy_sha256,
        )
        if (
            document["boot_id_sha256"] != expected_boot_id_sha256
            or document["producer_epoch"] != expected_producer_epoch
        ):
            raise _safe_code("priority assertion host or producer identity changed")
        final_path = path.lstat()
        if (
            stat.S_ISLNK(final_path.st_mode)
            or not stat.S_ISREG(final_path.st_mode)
            or (final_path.st_dev, final_path.st_ino) != (after.st_dev, after.st_ino)
            or final_path.st_size != after.st_size
        ):
            raise _safe_code("priority assertion final path was replaced")
        t0_monotonic_ns = monotonic_ns()
    finally:
        os.close(descriptor)
    if (
        not _exact_int(t0_monotonic_ns, minimum=1)
        or t0_monotonic_ns < document["observed_monotonic_ns"]
        or t0_monotonic_ns - document["observed_monotonic_ns"] > 10_000_000_000
        or t0_monotonic_ns - document["source_observed_monotonic_ns"] > 30_000_000_000
    ):
        raise _safe_code("priority assertion T0 was invalid or stale")
    return PriorityAssertion(
        document=document,
        payload=payload,
        attestation=attestation,
        t0_monotonic_ns=t0_monotonic_ns,
    )


def validate_runtime_receipt(
    document: dict[str, Any],
    payload: bytes,
    *,
    expected_runtime_epoch: str,
    expected_token_sha256: str,
) -> dict[str, Any]:
    """Validate one exact fsynced gate receipt before it enters evidence."""
    if (
        _strict_json_line(
            payload, label="runtime receipt", maximum=MAX_RUNTIME_RECEIPT_BYTES
        )
        != document
    ):
        raise _safe_code("runtime receipt disagreed with canonical bytes")
    priority = document.get("priority_state")
    controller = document.get("controller_phase")
    recovery = document.get("recovery_reason")
    source = document.get("source_generation")
    observation = document.get("observation_digest")
    policy = document.get("policy_sha256")
    heartbeat_age = document.get("heartbeat_age_ms")
    source_age = document.get("source_age_ms")
    workload = document.get("workload_sha256")
    model_resident = document.get("model_resident")
    active = document.get("active")
    chunk = document.get("chunk_uncommitted")
    if (
        set(document) != RUNTIME_RECEIPT_KEYS
        or document.get("schema") != "subgen.task11b.runtime-receipt/v1"
        or document.get("runtime_epoch") != expected_runtime_epoch
        or document.get("gate_token_sha256") != expected_token_sha256
        or not _exact_int(document.get("sequence"), minimum=1)
        or not _exact_int(document.get("observed_monotonic_ns"), minimum=1)
        or not _exact_nullable_hex(workload)
        or not _exact_nullable_int(source, minimum=1)
        or not _exact_nullable_hex(observation)
        or not _exact_nullable_hex(document.get("transition_observation_digest"))
        or not _exact_int(document.get("transition_sequence"))
        or not _exact_nullable_int(heartbeat_age, maximum=60000)
        or not _exact_nullable_int(source_age, maximum=60000)
        or not _exact_nullable_hex(policy)
        or priority not in {"clear", "neutral", "asserted", "unavailable"}
        or controller not in {"normal", "yielding", "recovering"}
        or recovery
        not in {None, "priority_pressure", "resource_pressure", "model_admission"}
        or (controller == "normal") != (recovery is None)
        or type(document.get("admission_open")) is not bool
        or not _exact_int(document.get("distinct_clear_count"), maximum=3)
        or type(model_resident) is not bool
        or not _exact_int(document.get("model_load_generation"))
        or not _exact_int(document.get("model_unload_generation"))
        or type(active) is not bool
        or type(chunk) is not bool
        or (not active and chunk)
        or not _exact_nullable_int(document.get("active_cursor_ms"))
        or (active != (document.get("active_cursor_ms") is not None))
        or not _exact_nullable_int(document.get("completed_cursor_ms"))
        or not _exact_int(document.get("completion_generation"))
        or not _exact_nullable_hex(document.get("model_identity_sha256"))
        or model_resident != (document.get("model_identity_sha256") is not None)
        or not _exact_int(document.get("cuda_oom_generation"))
        or not _exact_int(document.get("media_failure_generation"))
    ):
        raise _safe_code("runtime receipt schema types or state were invalid")
    last_accepted = (source, observation, policy, heartbeat_age, source_age)
    if (source is None and any(value is not None for value in last_accepted)) or (
        source is not None and any(value is None for value in last_accepted)
    ):
        raise _safe_code("runtime receipt last accepted priority fields disagreed")
    if workload is None and active:
        raise _safe_code("runtime receipt active workload lacked identity")
    return document


def _printable_ascii(value: Any, *, maximum: int = 256) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= maximum
        and all(0x20 <= ord(character) <= 0x7E for character in value)
    )


def validate_priority_policy(
    document: dict[str, Any],
    payload: bytes,
    *,
    expected_file_sha256: str,
) -> dict[str, Any]:
    """Independently validate the private producer policy without disclosing it."""
    if (
        _strict_json_line(payload, label="priority policy", maximum=32 * 1024)
        != document
        or hashlib.sha256(payload).hexdigest() != expected_file_sha256
        or set(document) != PRIORITY_POLICY_KEYS
    ):
        raise _safe_code("priority policy canonical identity or keys were invalid")
    identifier = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
    cameras = document.get("cameras")
    detectors = document.get("detectors")
    embeddings = document.get("required_embedding_speeds")
    pairs = document.get("conditional_embedding_pairs")
    if (
        document.get("schema") != 1
        or type(document.get("source_max_age_seconds")) is not int
        or document["source_max_age_seconds"] != 30
        or type(document.get("gpu_index")) is not int
        or not 0 <= document["gpu_index"] <= 31
        or type(document.get("detection_fps_limit")) is not float
        or document["detection_fps_limit"] != 80.0
        or not isinstance(document.get("frigate_version"), str)
        or re.fullmatch(r"[0-9A-Za-z._+-]{1,32}", document["frigate_version"]) is None
        or not isinstance(document.get("frigate_config_sha256"), str)
        or LOWER_HEX_64_RE.fullmatch(document["frigate_config_sha256"]) is None
        or not isinstance(document.get("gpu_uuid"), str)
        or re.fullmatch(
            r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            document["gpu_uuid"],
        )
        is None
        or not _printable_ascii(document.get("nvidia_driver_version"), maximum=32)
        or not isinstance(cameras, dict)
        or not 1 <= len(cameras) <= 128
        or any(
            identifier.fullmatch(key) is None
            or type(value) is not float
            or not math.isfinite(value)
            or not 0 < value <= 60.0
            for key, value in cameras.items()
        )
    ):
        raise _safe_code("priority policy scalar or camera schema was invalid")
    for values, minimum, maximum, label in (
        (detectors, 1, 32, "detectors"),
        (embeddings, 1, 64, "embedding identifiers"),
    ):
        if (
            not isinstance(values, list)
            or not minimum <= len(values) <= maximum
            or values != sorted(set(values))
            or any(
                not isinstance(value, str) or identifier.fullmatch(value) is None
                for value in values
            )
        ):
            raise _safe_code(f"priority policy {label} were invalid")
    if not isinstance(pairs, list) or not 0 <= len(pairs) <= 32:
        raise _safe_code("priority policy conditional pairs were invalid")
    canonical_pairs: list[list[str]] = []
    for pair in pairs:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or pair != sorted(set(pair))
            or any(
                not isinstance(value, str) or identifier.fullmatch(value) is None
                for value in pair
            )
        ):
            raise _safe_code("priority policy conditional pair was invalid")
        canonical_pairs.append(pair)
    if canonical_pairs != sorted(canonical_pairs) or len(
        {tuple(pair) for pair in canonical_pairs}
    ) != len(canonical_pairs):
        raise _safe_code("priority policy conditional pairs were not canonical")
    return document


def validate_unloaded_gpu_envelope(
    document: dict[str, Any],
    payload: bytes,
    *,
    expected_file_sha256: str,
    expected_policy_sha256: str,
    expected_runtime_commit: str,
    expected_oci_index: str,
    expected_config_digest: str,
    expected_layer_diff_ids: list[str],
) -> dict[str, Any]:
    """Validate the exact-image three-cycle unloaded GPU attribution envelope."""
    if (
        _strict_json_line(payload, label="unloaded GPU envelope", maximum=512 * 1024)
        != document
        or hashlib.sha256(payload).hexdigest() != expected_file_sha256
        or set(document) != UNLOADED_ENVELOPE_KEYS
        or document.get("schema") != "subgen.unloaded-gpu-envelope/v1"
        or document.get("runtime_commit") != expected_runtime_commit
    ):
        raise _safe_code("unloaded GPU envelope identity or schema was invalid")
    image = document.get("image")
    gpu = document.get("gpu")
    backend = document.get("backend")
    policy = document.get("model_policy")
    measurement = document.get("measurement")
    if (
        not isinstance(image, dict)
        or set(image) != {"oci_index", "config_digest", "layer_diff_ids"}
        or image.get("oci_index") != expected_oci_index
        or image.get("config_digest") != expected_config_digest
        or image.get("layer_diff_ids") != expected_layer_diff_ids
        or not isinstance(expected_layer_diff_ids, list)
        or not 1 <= len(expected_layer_diff_ids) <= 256
        or any(
            re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
            for value in expected_layer_diff_ids
        )
        or not isinstance(gpu, dict)
        or set(gpu) != {"uuid", "driver_version"}
        or not isinstance(gpu.get("uuid"), str)
        or re.fullmatch(
            r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            gpu["uuid"],
        )
        is None
        or not _printable_ascii(gpu.get("driver_version"), maximum=64)
        or not isinstance(backend, dict)
        or set(backend)
        != {
            "cuda_version",
            "ctranslate2_version",
            "stable_ts_version",
            "generator_sha256",
        }
        or any(
            not _printable_ascii(backend.get(key), maximum=64)
            for key in ("cuda_version", "ctranslate2_version", "stable_ts_version")
        )
        or not isinstance(backend.get("generator_sha256"), str)
        or LOWER_HEX_64_RE.fullmatch(backend["generator_sha256"]) is None
    ):
        raise _safe_code("unloaded GPU envelope image GPU or backend was invalid")
    policy_keys = {
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
    }
    if (
        not isinstance(policy, dict)
        or set(policy) != policy_keys
        or policy.get("selected_model")
        not in {"tiny", "base", "small", "medium", "large-v3"}
        or not isinstance(policy.get("model_revision"), str)
        or re.fullmatch(r"hf:[0-9a-f]{40}", policy["model_revision"]) is None
        or not _printable_ascii(policy.get("compute_type"), maximum=64)
        or policy.get("device") != "cuda"
        or not _exact_int(policy.get("device_index"), maximum=31)
        or policy.get("task") not in {"transcribe", "translate"}
        or not _printable_ascii(policy.get("language"), maximum=64)
        or policy.get("chunk_seconds") != 300
        or type(policy.get("chunk_seconds")) is not int
        or policy.get("overlap_seconds") != 5
        or type(policy.get("overlap_seconds")) is not int
        or not isinstance(policy.get("fixture_sha256"), str)
        or LOWER_HEX_64_RE.fullmatch(policy["fixture_sha256"]) is None
        or policy.get("priority_policy_sha256") != expected_policy_sha256
    ):
        raise _safe_code("unloaded GPU envelope model policy was invalid")
    measurement_keys = {
        "cycles",
        "cycle_count",
        "samples_per_cycle",
        "interval_seconds",
        "margin_bytes",
        "max_observed_candidate_bytes",
        "allowed_unloaded_bytes",
    }
    cycles = measurement.get("cycles") if isinstance(measurement, dict) else None
    if (
        not isinstance(measurement, dict)
        or set(measurement) != measurement_keys
        or measurement.get("cycle_count") != 3
        or type(measurement.get("cycle_count")) is not int
        or measurement.get("samples_per_cycle") != 10
        or type(measurement.get("samples_per_cycle")) is not int
        or measurement.get("interval_seconds") != 1
        or type(measurement.get("interval_seconds")) is not int
        or measurement.get("margin_bytes") != 134_217_728
        or type(measurement.get("margin_bytes")) is not int
        or not isinstance(cycles, list)
        or len(cycles) != 3
    ):
        raise _safe_code("unloaded GPU envelope measurement metadata was invalid")
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
    container_digests: set[str] = set()
    all_samples: list[int] = []
    for index, cycle in enumerate(cycles, start=1):
        if (
            not isinstance(cycle, dict)
            or set(cycle) != cycle_keys
            or cycle.get("cycle_index") != index
            or type(cycle.get("cycle_index")) is not int
            or cycle.get("load_generation_before") != 0
            or type(cycle.get("load_generation_before")) is not int
            or cycle.get("load_generation_after") != 1
            or type(cycle.get("load_generation_after")) is not int
            or cycle.get("unload_generation_before") != 0
            or type(cycle.get("unload_generation_before")) is not int
            or cycle.get("unload_generation_after") != 1
            or type(cycle.get("unload_generation_after")) is not int
            or cycle.get("inference_completed") is not True
            or not isinstance(cycle.get("container_id_sha256"), str)
            or LOWER_HEX_64_RE.fullmatch(cycle["container_id_sha256"]) is None
            or not isinstance(cycle.get("inference_result_sha256"), str)
            or LOWER_HEX_64_RE.fullmatch(cycle["inference_result_sha256"]) is None
            or not isinstance(cycle.get("candidate_bytes_samples"), list)
            or len(cycle["candidate_bytes_samples"]) != 10
            or any(not _exact_int(value) for value in cycle["candidate_bytes_samples"])
        ):
            raise _safe_code("unloaded GPU envelope cycle was invalid")
        container_digests.add(cycle["container_id_sha256"])
        all_samples.extend(cycle["candidate_bytes_samples"])
    maximum = max(all_samples)
    if (
        len(container_digests) != 3
        or measurement.get("max_observed_candidate_bytes") != maximum
        or type(measurement.get("max_observed_candidate_bytes")) is not int
        or measurement.get("allowed_unloaded_bytes") != maximum + 134_217_728
        or type(measurement.get("allowed_unloaded_bytes")) is not int
        or measurement["allowed_unloaded_bytes"] > 2**63 - 1
    ):
        raise _safe_code("unloaded GPU envelope allowed byte arithmetic was invalid")
    return document


def validate_execution_boundary_document(
    document: dict[str, Any],
    *,
    candidate_record: dict[str, Any],
    phase_a: dict[str, Any],
    phase_b: dict[str, Any],
    expected_catalog_sha256: str,
) -> dict[str, Any]:
    """Reconstruct and cross-bind the complete schema-3 security boundary."""
    if (
        not isinstance(document, dict)
        or set(document) != EXECUTION_BOUNDARY_KEYS
        or document.get("schema") != 4
        or document.get("model_envelope_catalog_sha256") != expected_catalog_sha256
        or not LOWER_HEX_64_RE.fullmatch(expected_catalog_sha256)
    ):
        raise _safe_code("execution boundary schema or catalog binding was invalid")
    fixture_hashes = (
        document.get("phase_a_fixture_record_sha256"),
        document.get("phase_b_fixture_record_sha256"),
    )
    if (
        any(
            not isinstance(value, str) or LOWER_HEX_64_RE.fullmatch(value) is None
            for value in fixture_hashes
        )
        or fixture_hashes[0] == fixture_hashes[1]
    ):
        raise _safe_code("execution boundary fixture record binding was invalid")
    for value_key, digest_key in (
        ("environment", "environment_sha256"),
        ("config", "config_sha256"),
        ("host_config", "host_config_sha256"),
        ("network_attachments", "network_attachments_sha256"),
    ):
        digest = document.get(digest_key)
        if (
            not isinstance(digest, str)
            or LOWER_HEX_64_RE.fullmatch(digest) is None
            or hashlib.sha256(
                _canonical_ascii_json_line(
                    document.get(value_key), label=f"boundary {value_key}"
                )[:-1]
            ).hexdigest()
            != digest
        ):
            raise _safe_code(f"execution boundary {value_key} preimage mismatch")
    identity = document.get("candidate_identity")
    record_identity = candidate_record.get("candidate_identity")
    if identity != record_identity:
        raise _safe_code("execution boundary candidate identity did not match record")
    if not isinstance(identity, dict) or set(identity) != {
        "container_id",
        "runtime_commit",
        "oci_index",
        "config_digest",
        "layer_diff_ids",
        "selected_model",
        "model_revision",
    }:
        raise _safe_code("execution boundary candidate identity was incomplete")
    try:
        daemon_identity_sha256 = health.docker_daemon_identity_sha256(
            document.get("docker_daemon_identity")
        )
    except health.GateAbort as exc:
        raise _safe_code(
            "execution boundary Docker daemon identity was invalid"
        ) from exc
    if candidate_record.get("docker_daemon_identity_sha256") != daemon_identity_sha256:
        raise _safe_code(
            "execution boundary Docker daemon identity did not match record"
        )

    environment = document.get("environment")
    if not isinstance(environment, list) or any(
        not isinstance(entry, str) or "=" not in entry or "\x00" in entry
        for entry in environment
    ):
        raise _safe_code("execution boundary environment was malformed")
    values: dict[str, str] = {}
    for entry in environment:
        key, value = entry.split("=", 1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None or key in values:
            raise _safe_code("execution boundary environment was duplicated")
        values[key] = value
    if environment != [f"{key}={values[key]}" for key in sorted(values)]:
        raise _safe_code("execution boundary environment was not canonical")
    gate_names = {
        "TASK11B_GATE_RECEIPT_FILE",
        "TASK11B_GATE_TOKEN_SHA256",
        "TASK11B_PHASE_A_WORKLOAD_SHA256",
        "TASK11B_PHASE_B_WORKLOAD_SHA256",
    }
    if {key for key in values if key.startswith("TASK11B_")} != gate_names:
        raise _safe_code("execution boundary gate environment was not exact")
    config = document.get("config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    ownership_labels = document.get("ownership_labels")
    ownership_label_keys = {
        "io.github.herbertmt978.subgen.task11b-gate",
        "io.github.herbertmt978.subgen.gate-token",
        "io.github.herbertmt978.subgen.gate-role",
        "io.github.herbertmt978.subgen.runtime-commit",
    }
    if (
        not isinstance(labels, dict)
        or not isinstance(ownership_labels, dict)
        or set(ownership_labels) != ownership_label_keys
        or any(labels.get(key) != value for key, value in ownership_labels.items())
        or ownership_labels.get("io.github.herbertmt978.subgen.task11b-gate") != "true"
        or ownership_labels.get("io.github.herbertmt978.subgen.gate-role")
        != "runtime-auto"
        or ownership_labels.get("io.github.herbertmt978.subgen.runtime-commit")
        != identity.get("runtime_commit")
    ):
        raise _safe_code("execution boundary labels were unavailable")
    token = ownership_labels.get("io.github.herbertmt978.subgen.gate-token")
    if not isinstance(token, str) or LOWER_HEX_32_RE.fullmatch(token) is None:
        raise _safe_code("execution boundary ownership token was invalid")
    token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
    expected_environment = {
        "TASK11B_GATE_RECEIPT_FILE": "/run/subgen-task11b/runtime-receipts.jsonl",
        "TASK11B_GATE_TOKEN_SHA256": token_sha256,
        "TASK11B_PHASE_A_WORKLOAD_SHA256": phase_a.get("workload_sha256"),
        "TASK11B_PHASE_B_WORKLOAD_SHA256": phase_b.get("workload_sha256"),
        "PRIORITY_PRESSURE_FILE": "/run/subgen-priority/pressure.json",
        "WHISPER_MODEL": "auto",
        "TRANSCRIBE_DEVICE": "cuda",
        "COMPUTE_TYPE": "float16",
        "CONCURRENT_TRANSCRIPTIONS": "1",
        "MODEL_ENVELOPE_CATALOG": "/opt/subgen/model-envelopes/catalog.json",
        "MODEL_ENVELOPE_IDENTITY": "/opt/subgen/model-envelopes/image-identity.json",
        "AUTO_DELETE_INVALID_MEDIA": "false",
        "AUTO_DELETE_FAILED_FILES": "false",
        "SUBTITLE_LANGUAGE_NAME": "en",
        "SHOW_IN_SUBNAME_SUBGEN": "false",
        "SHOW_IN_SUBNAME_MODEL": "false",
    }
    if any(values.get(key) != value for key, value in expected_environment.items()):
        raise _safe_code("execution boundary runtime environment was not exact")
    if (
        candidate_record.get("gate_token_sha256") != token_sha256
        or labels.get("io.github.herbertmt978.subgen.task11b-gate") != "true"
        or labels.get("io.github.herbertmt978.subgen.runtime-commit")
        != identity.get("runtime_commit")
        or config.get("Env") != environment
        or config.get("User") != document.get("user")
        or config.get("WorkingDir") != document.get("working_directory")
    ):
        raise _safe_code("execution boundary config or ownership binding was invalid")
    command_payload = json.dumps(
        [config.get("Entrypoint"), config.get("Cmd")],
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")
    command_sha256 = hashlib.sha256(command_payload).hexdigest()
    if (
        document.get("entrypoint_command_sha256") != command_sha256
        or candidate_record.get("intended_command_sha256") != command_sha256
    ):
        raise _safe_code("execution boundary command preimage did not match")

    host = document.get("host")
    host_config = document.get("host_config")
    if (
        not isinstance(host, dict)
        or not isinstance(host_config, dict)
        or not host
        or any(
            key not in host_config or host_config[key] != value
            for key, value in host.items()
        )
        or host.get("Privileged") is not False
        or host.get("ReadonlyRootfs") is not True
        or host.get("CapDrop") != ["ALL"]
        or host.get("NetworkMode") != "bridge"
    ):
        raise _safe_code("execution boundary host policy was unsafe or incomplete")
    mounts = document.get("mounts")
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
    if not isinstance(mounts, list) or [
        mount.get("destination") for mount in mounts
    ] != sorted(mount_policy):
        raise _safe_code("execution boundary mounts were incomplete or unordered")
    for mount in mounts:
        destination = mount.get("destination")
        writable = mount_policy.get(destination)
        if (
            set(mount)
            != {"type", "source", "destination", "mode", "read_write", "propagation"}
            or mount.get("type") != "bind"
            or not isinstance(mount.get("source"), str)
            or not mount["source"].startswith("/")
            or mount.get("read_write") is not writable
            or mount.get("mode") != ("rw" if writable else "ro")
            or mount.get("propagation") != "rprivate"
        ):
            raise _safe_code("execution boundary mount policy was unsafe")
    return document


def verify_release_cross_bindings(
    *,
    binding: dict[str, Any],
    final: dict[str, Any],
    phase_a: dict[str, Any],
    phase_b: dict[str, Any],
    candidate_record: dict[str, Any],
    boundary: dict[str, Any],
    catalog_attestation: dict[str, Any],
    hashes: dict[str, str],
    expected_runtime_commit: str,
    expected_oci_index: str,
    expected_config_digest: str,
) -> None:
    """Cross-bind all independently validated release documents and byte hashes."""
    required_hashes = {
        "final",
        "phase_a",
        "phase_b",
        "candidate",
        "boundary",
        "policy",
        "envelope",
        "catalog",
        "assertion",
        "phase_a_trace",
        "phase_b_trace",
        "output",
        "observer",
        "sampler",
        "sampler_test",
        "observer_test",
        "producer",
    }
    if set(hashes) != required_hashes or any(
        not isinstance(value, str) or LOWER_HEX_64_RE.fullmatch(value) is None
        for value in hashes.values()
    ):
        raise _safe_code("release artifact hash set was incomplete")
    if (
        binding.get("gate_seal_sha256") != hashes["final"]
        or binding.get("observer_sha256") != hashes["observer"]
        or binding.get("sampler_sha256") != hashes["sampler"]
        or binding.get("test_sha256") != hashes["sampler_test"]
        or binding.get("observer_test_sha256") != hashes["observer_test"]
        or binding.get("producer_sha256") != hashes["producer"]
        or binding.get("policy_sha256") != hashes["policy"]
        or binding.get("unloaded_gpu_envelope_sha256") != hashes["envelope"]
        or binding.get("model_envelope_catalog_sha256") != hashes["catalog"]
        or binding.get("execution_boundary_manifest_sha256") != hashes["boundary"]
    ):
        raise _safe_code("release binding did not match exact artifact bytes")
    if (
        final.get("runtime_commit") != expected_runtime_commit
        or final.get("candidate_oci_index") != expected_oci_index
        or final.get("candidate_config_digest") != expected_config_digest
        or final.get("phase_a_seal_sha256") != hashes["phase_a"]
        or final.get("phase_b_seal_sha256") != hashes["phase_b"]
        or final.get("candidate_identity_record_sha256") != hashes["candidate"]
        or final.get("execution_boundary_manifest_sha256") != hashes["boundary"]
        or final.get("policy_sha256") != hashes["policy"]
        or final.get("unloaded_gpu_envelope_sha256") != hashes["envelope"]
        or final.get("model_envelope_catalog_sha256") != hashes["catalog"]
        or final.get("observer_sha256") != hashes["observer"]
        or final.get("sampler_sha256") != hashes["sampler"]
        or final.get("sampler_test_sha256") != hashes["sampler_test"]
        or final.get("observer_test_sha256") != hashes["observer_test"]
        or final.get("producer_sha256") != hashes["producer"]
    ):
        raise _safe_code("final gate identity or subordinate hash was inconsistent")
    identity = candidate_record.get("candidate_identity")
    if not isinstance(identity, dict):
        raise _safe_code("candidate identity record was incomplete")
    try:
        daemon_identity_sha256 = health.docker_daemon_identity_sha256(
            boundary.get("docker_daemon_identity")
        )
    except health.GateAbort as exc:
        raise _safe_code("candidate Docker daemon identity was incomplete") from exc
    identity_sha256 = _canonical_document_sha256(identity, label="candidate identity")
    container_id = identity.get("container_id")
    layers = identity.get("layer_diff_ids")
    if (
        identity.get("runtime_commit") != expected_runtime_commit
        or identity.get("oci_index") != expected_oci_index
        or identity.get("config_digest") != expected_config_digest
        or not isinstance(container_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
        or not isinstance(layers, list)
        or not layers
        or final.get("container_id_sha256")
        != hashlib.sha256(container_id.encode("ascii")).hexdigest()
        or final.get("layer_diff_ids_sha256")
        != _canonical_document_sha256(layers, label="candidate layer diff IDs")
        or candidate_record.get("execution_boundary_manifest_sha256")
        != hashes["boundary"]
        or candidate_record.get("docker_daemon_identity_sha256")
        != daemon_identity_sha256
        or final.get("docker_daemon_identity_sha256") != daemon_identity_sha256
        or boundary.get("candidate_identity") != identity
        or phase_a.get("candidate_identity_sha256") != identity_sha256
        or phase_b.get("candidate_identity_sha256") != identity_sha256
        or phase_b.get("candidate_identity") != identity
    ):
        raise _safe_code("candidate identity did not cross-bind all release artifacts")
    if (
        catalog_attestation.get("catalog_sha256") != hashes["catalog"]
        or catalog_attestation.get("selected_model") != identity.get("selected_model")
        or catalog_attestation.get("model_revision") != identity.get("model_revision")
        or catalog_attestation.get("model_identity_sha256")
        != phase_b.get("model_identity_sha256")
    ):
        raise _safe_code("catalog or recomputed model identity did not match candidate")
    if (
        phase_a.get("execution_boundary_manifest_sha256") != hashes["boundary"]
        or phase_b.get("execution_boundary_manifest_sha256") != hashes["boundary"]
        or phase_a.get("policy_sha256") != hashes["policy"]
        or phase_b.get("policy_sha256") != hashes["policy"]
        or phase_a.get("unloaded_gpu_envelope_sha256") != hashes["envelope"]
        or phase_a.get("assertion_observation_sha256") != hashes["assertion"]
        or phase_a.get("gate_receipt_trace_sha256") != hashes["phase_a_trace"]
        or phase_b.get("gate_receipt_trace_sha256") != hashes["phase_b_trace"]
        or phase_a.get("final_output_sha256") != hashes["output"]
        or phase_b.get("phase_a_seal_sha256") != hashes["phase_a"]
        or phase_a.get("runtime_epoch") != phase_b.get("runtime_epoch")
        or phase_a.get("runtime_started_monotonic_ns")
        != phase_b.get("runtime_started_monotonic_ns")
        or phase_a.get("workload_sha256") == phase_b.get("workload_sha256")
    ):
        raise _safe_code("phase identity or private artifact hash was inconsistent")
    events = phase_a.get("events")
    if (
        not isinstance(events, list)
        or len(events) != 10
        or not (
            events[9].get("monotonic_ns")
            <= phase_a.get("sealed_monotonic_ns")
            <= phase_b.get("phase_a_durable_monotonic_ns")
            <= phase_b.get("reset_completed_monotonic_ns")
            < phase_b.get("started_monotonic_ns")
        )
    ):
        raise _safe_code("Phase A and Phase B durability boundary was invalid")


def _validate_catalog_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != MODEL_CATALOG_ENTRY_KEYS:
        raise _safe_code("model envelope catalog entry keys were invalid")
    image = entry.get("image_identity")
    runtime = entry.get("runtime")
    policy = entry.get("policy")
    measurements = entry.get("measurements")
    if (
        not isinstance(image, dict)
        or set(image) != {"config_digest", "layer_diff_ids"}
        or not isinstance(image.get("config_digest"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image["config_digest"])
        or not isinstance(image.get("layer_diff_ids"), list)
        or not 1 <= len(image["layer_diff_ids"]) <= 256
        or any(
            not isinstance(value, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
            for value in image["layer_diff_ids"]
        )
    ):
        raise _safe_code("model envelope image identity was invalid")
    if (
        not isinstance(runtime, dict)
        or set(runtime) != MODEL_CATALOG_RUNTIME_KEYS
        or any(
            not _printable_ascii(runtime.get(name))
            for name in MODEL_CATALOG_RUNTIME_KEYS - {"total_vram_bytes"}
        )
        or not _exact_int(runtime.get("total_vram_bytes"), minimum=1)
    ):
        raise _safe_code("model envelope runtime identity was invalid")
    if (
        not isinstance(policy, dict)
        or set(policy) != MODEL_CATALOG_POLICY_KEYS
        or policy.get("model") not in {"tiny", "base", "small", "medium", "large-v3"}
        or not isinstance(policy.get("model_revision"), str)
        or not re.fullmatch(r"hf:[0-9a-f]{40}", policy["model_revision"])
        or not _printable_ascii(policy.get("compute_type"))
        or policy.get("task") not in {"transcribe", "translate"}
        or not _exact_int(policy.get("inference_concurrency"), minimum=1)
        or not _exact_int(policy.get("chunk_minutes"), minimum=5, maximum=60)
        or not isinstance(policy.get("decoder_options_sha256"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", policy["decoder_options_sha256"])
    ):
        raise _safe_code("model envelope policy was invalid")
    if (
        not isinstance(measurements, dict)
        or set(measurements) != MODEL_CATALOG_MEASUREMENT_KEYS
        or any(not _exact_int(value, minimum=1) for value in measurements.values())
        or measurements["runs"] < 3
    ):
        raise _safe_code("model envelope measurements were invalid")
    for domain in ("host", "cgroup", "device"):
        preload = measurements[f"{domain}_preload_used_bytes"]
        peak = measurements[f"{domain}_peak_used_bytes"]
        incremental = measurements[f"{domain}_incremental_peak_bytes"]
        if peak < preload or not peak - preload <= incremental <= peak:
            raise _safe_code("model envelope measurement arithmetic was invalid")
    return entry


def validate_model_envelope_catalog(
    document: dict[str, Any],
    payload: bytes,
    *,
    expected_file_sha256: str,
    candidate_config_digest: str,
    candidate_layer_diff_ids: list[str],
    unloaded_envelope: dict[str, Any],
) -> dict[str, Any]:
    """Validate the complete catalog and independently derive model identity."""
    parsed = _strict_canonical_json_document(
        payload,
        label="model envelope catalog",
        maximum=4 * MIB,
        trailing_newline=False,
    )
    if parsed != document:
        raise _safe_code("model envelope catalog document disagreed with bytes")
    actual_file_sha256 = hashlib.sha256(payload).hexdigest()
    entries = document.get("entries")
    integrity = document.get("integrity")
    if (
        set(document) != {"schema", "catalog_version", "entries", "integrity"}
        or document.get("schema") != "subgen.model-envelope.catalog/v1"
        or not _exact_int(document.get("catalog_version"), minimum=1)
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= 256
        or not isinstance(integrity, dict)
        or set(integrity) != {"algorithm", "canonical_payload_sha256"}
        or integrity.get("algorithm") != "sha256"
        or not isinstance(integrity.get("canonical_payload_sha256"), str)
        or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", integrity["canonical_payload_sha256"]
        )
        or expected_file_sha256 != actual_file_sha256
    ):
        raise _safe_code("model envelope catalog schema or file identity was invalid")
    unsigned = {
        "schema": document["schema"],
        "catalog_version": document["catalog_version"],
        "entries": entries,
    }
    integrity_payload = _canonical_ascii_json_line(
        unsigned, label="model envelope catalog integrity"
    )[:-1]
    expected_integrity = "sha256:" + hashlib.sha256(integrity_payload).hexdigest()
    if integrity["canonical_payload_sha256"] != expected_integrity:
        raise _safe_code("model envelope catalog integrity mismatch")
    validated_entries = [_validate_catalog_entry(entry) for entry in entries]
    match_keys: set[bytes] = set()
    for entry in validated_entries:
        key = _canonical_ascii_json_line(
            {
                "image_identity": entry["image_identity"],
                "runtime": entry["runtime"],
                "policy": entry["policy"],
            },
            label="model envelope match key",
        )
        if key in match_keys:
            raise _safe_code("model envelope catalog match was not unique")
        match_keys.add(key)

    if not isinstance(unloaded_envelope, dict):
        raise _safe_code("unloaded GPU envelope was unavailable")
    backend = unloaded_envelope.get("backend")
    model_policy = unloaded_envelope.get("model_policy")
    if not isinstance(backend, dict) or not isinstance(model_policy, dict):
        raise _safe_code("unloaded GPU envelope identity was incomplete")
    matching: list[dict[str, Any]] = []
    for entry in validated_entries:
        image = entry["image_identity"]
        runtime = entry["runtime"]
        policy = entry["policy"]
        if (
            image["config_digest"] == candidate_config_digest
            and image["layer_diff_ids"] == candidate_layer_diff_ids
            and runtime["stable_ts_version"] == backend.get("stable_ts_version")
            and runtime["ctranslate2_version"] == backend.get("ctranslate2_version")
            and runtime["cuda_runtime_version"] == backend.get("cuda_version")
            and runtime["driver_version"]
            == unloaded_envelope.get("gpu", {}).get("driver_version")
            and policy["model"] == model_policy.get("selected_model")
            and policy["model_revision"] == model_policy.get("model_revision")
            and policy["compute_type"] == model_policy.get("compute_type")
            and policy["task"] == model_policy.get("task")
            and policy["inference_concurrency"] == 1
            and policy["chunk_minutes"] * 60 == model_policy.get("chunk_seconds")
        ):
            matching.append(entry)
    if len(matching) != 1:
        raise _safe_code("model envelope catalog identity was ambiguous or missing")
    match = matching[0]
    entry_sha256 = hashlib.sha256(
        _canonical_ascii_json_line(match, label="model envelope entry")
    ).hexdigest()
    policy_sha256 = hashlib.sha256(
        _canonical_ascii_json_line(match["policy"], label="model envelope policy")
    ).hexdigest()
    model_identity = {
        "catalog_entry_sha256": entry_sha256,
        "model_policy_sha256": policy_sha256,
        "model_revision": match["policy"]["model_revision"],
        "selected_model": match["policy"]["model"],
    }
    return {
        "catalog_sha256": actual_file_sha256,
        "catalog_payload_sha256": integrity["canonical_payload_sha256"],
        "entry_index": validated_entries.index(match),
        "catalog_entry_sha256": entry_sha256,
        "model_policy_sha256": policy_sha256,
        "model_identity_sha256": hashlib.sha256(
            _canonical_ascii_json_line(model_identity, label="model identity")
        ).hexdigest(),
        "selected_model": match["policy"]["model"],
        "model_revision": match["policy"]["model_revision"],
    }


class RuntimeReceiptJournal:
    """Inode-bound append-only receipt reader; poll cadence cannot lose records."""

    def __init__(
        self,
        path: Path,
        *,
        expected_runtime_epoch: str,
        expected_token_sha256: str,
    ) -> None:
        if (
            not path.is_absolute()
            or not LOWER_HEX_32_RE.fullmatch(expected_runtime_epoch)
            or not LOWER_HEX_64_RE.fullmatch(expected_token_sha256)
        ):
            raise _safe_code("runtime receipt journal binding was invalid")
        self.path = path
        self.expected_runtime_epoch = expected_runtime_epoch
        self.expected_token_sha256 = expected_token_sha256
        try:
            parent_lstat = path.parent.lstat()
            path_lstat = path.lstat()
        except OSError as exc:
            raise _safe_code("runtime receipt journal was unavailable") from exc
        owner = _owner_id()
        if (
            stat.S_ISLNK(parent_lstat.st_mode)
            or stat.S_ISLNK(path_lstat.st_mode)
            or not stat.S_ISREG(path_lstat.st_mode)
            or (owner is not None and parent_lstat.st_uid != owner)
            or (owner is not None and stat.S_IMODE(parent_lstat.st_mode) != 0o700)
            or (owner is not None and path_lstat.st_uid != owner)
            or (owner is not None and stat.S_IMODE(path_lstat.st_mode) != 0o600)
        ):
            raise _safe_code(
                "runtime receipt journal ownership mode or type was unsafe"
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            self._fd = os.open(path, flags)
        except OSError as exc:
            raise _safe_code("runtime receipt journal could not be opened") from exc
        item = os.fstat(self._fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or (item.st_dev, item.st_ino) != (path_lstat.st_dev, path_lstat.st_ino)
            or item.st_size > MAX_RUNTIME_RECEIPT_JOURNAL_BYTES
        ):
            os.close(self._fd)
            raise _safe_code("runtime receipt journal identity was unsafe")
        self._identity = (item.st_dev, item.st_ino)
        self._offset = 0
        self._consumed = b""
        self._buffer = b""
        self._receipt_hashes: set[str] = set()
        self.receipts: list[dict[str, Any]] = []

    def _read_prefix(self, length: int) -> bytes:
        if length == 0:
            return b""
        chunks: list[bytes] = []
        consumed = 0
        if hasattr(os, "pread"):
            while consumed < length:
                chunk = os.pread(self._fd, min(MIB, length - consumed), consumed)
                if not chunk:
                    break
                chunks.append(chunk)
                consumed += len(chunk)
            return b"".join(chunks)
        current = os.lseek(self._fd, 0, os.SEEK_CUR)
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            while consumed < length:
                chunk = os.read(self._fd, min(MIB, length - consumed))
                if not chunk:
                    break
                chunks.append(chunk)
                consumed += len(chunk)
        finally:
            os.lseek(self._fd, current, os.SEEK_SET)
        return b"".join(chunks)

    def _verify_identity(self) -> os.stat_result:
        try:
            current_path = self.path.lstat()
            current_fd = os.fstat(self._fd)
        except OSError as exc:
            raise _safe_code(
                "runtime receipt journal identity was unavailable"
            ) from exc
        if (
            stat.S_ISLNK(current_path.st_mode)
            or not stat.S_ISREG(current_path.st_mode)
            or (current_path.st_dev, current_path.st_ino) != self._identity
            or (current_fd.st_dev, current_fd.st_ino) != self._identity
        ):
            raise _safe_code("runtime receipt journal was replaced")
        if current_fd.st_size < self._offset:
            raise _safe_code("runtime receipt journal size regressed or was truncated")
        if current_fd.st_size > MAX_RUNTIME_RECEIPT_JOURNAL_BYTES:
            raise _safe_code("runtime receipt journal exceeded byte limit")
        if self._consumed and self._read_prefix(len(self._consumed)) != self._consumed:
            raise _safe_code("runtime receipt journal consumed prefix was mutated")
        return current_fd

    def read_available(self, *, final: bool = False) -> list[dict[str, Any]]:
        item = self._verify_identity()
        remaining = item.st_size - self._offset
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(self._fd, min(MIB, remaining))
            if not chunk:
                raise _safe_code("runtime receipt journal ended before its stated size")
            chunks.append(chunk)
            remaining -= len(chunk)
            self._offset += len(chunk)
        appended = b"".join(chunks)
        self._consumed += appended
        self._buffer += appended
        complete = self._buffer.split(b"\n")
        self._buffer = complete.pop()
        accepted: list[dict[str, Any]] = []
        for raw in complete:
            payload = raw + b"\n"
            document = _strict_json_line(
                payload, label="runtime receipt", maximum=MAX_RUNTIME_RECEIPT_BYTES
            )
            validate_runtime_receipt(
                document,
                payload,
                expected_runtime_epoch=self.expected_runtime_epoch,
                expected_token_sha256=self.expected_token_sha256,
            )
            expected_sequence = len(self.receipts) + 1
            if document["sequence"] != expected_sequence:
                raise _safe_code("runtime receipt sequence gap duplicate or regression")
            if (
                self.receipts
                and document["observed_monotonic_ns"]
                <= self.receipts[-1]["observed_monotonic_ns"]
            ):
                raise _safe_code("runtime receipt monotonic time did not increase")
            digest = hashlib.sha256(payload).hexdigest()
            if digest in self._receipt_hashes:
                raise _safe_code("runtime receipt canonical hash was duplicated")
            self._receipt_hashes.add(digest)
            retained = dict(document)
            retained["_receipt_sha256"] = digest
            self.receipts.append(retained)
            accepted.append(retained)
        if final and self._buffer:
            raise _safe_code("runtime receipt journal ended with a partial record")
        self._verify_identity()
        return accepted

    def close(self) -> None:
        descriptor = getattr(self, "_fd", None)
        if descriptor is not None:
            os.close(descriptor)
            self._fd = None


def _receipt_document(receipt: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical receipt, verifying optional journal-only metadata."""
    if not isinstance(receipt, dict):
        raise _safe_code("runtime receipt was not an object")
    extra = set(receipt) - RUNTIME_RECEIPT_KEYS
    if extra not in (set(), {"_receipt_sha256"}):
        raise _safe_code("runtime receipt carried unexpected observer metadata")
    document = {key: receipt[key] for key in RUNTIME_RECEIPT_KEYS if key in receipt}
    if set(document) != RUNTIME_RECEIPT_KEYS:
        raise _safe_code("runtime receipt keys were incomplete")
    payload = _canonical_ascii_json_line(document, label="runtime receipt")
    if (
        "_receipt_sha256" in receipt
        and receipt["_receipt_sha256"] != hashlib.sha256(payload).hexdigest()
    ):
        raise _safe_code("runtime receipt observer digest was inconsistent")
    return document


def write_priority_assertion_observation(
    path: Path, assertion: PriorityAssertion
) -> Any:
    """Copy exact signal N to a create-once canonical owner-only artifact."""
    if not isinstance(assertion, PriorityAssertion):
        raise _safe_code("priority assertion observation was unavailable")

    def validator(document: dict[str, Any]) -> dict[str, Any]:
        payload = _canonical_ascii_json_line(
            document, label="priority assertion observation"
        )
        validate_priority_assertion(
            document,
            payload,
            expected_policy_sha256=document.get("policy_sha256", ""),
        )
        if payload != assertion.payload or document != assertion.document:
            raise _safe_code("priority assertion copy changed source bytes")
        return document

    artifact = health.write_canonical_artifact(
        path, assertion.document, validator=validator
    )
    if artifact.file_sha256 != assertion.attestation["payload_sha256"]:
        raise _safe_code("priority assertion copy digest changed")
    return artifact


def _validated_receipt_prefix(
    receipts: list[dict[str, Any]],
    *,
    expected_runtime_epoch: str,
    expected_token_sha256: str,
) -> list[dict[str, Any]]:
    if not isinstance(receipts, list) or not receipts:
        raise _safe_code("runtime receipt prefix was empty")
    validated: list[dict[str, Any]] = []
    prior_sequence = 0
    prior_monotonic_ns = 0
    digests: set[str] = set()
    for receipt in receipts:
        document = _receipt_document(receipt)
        payload = _canonical_ascii_json_line(document, label="runtime receipt")
        validate_runtime_receipt(
            document,
            payload,
            expected_runtime_epoch=expected_runtime_epoch,
            expected_token_sha256=expected_token_sha256,
        )
        if (
            document["sequence"] != prior_sequence + 1
            or document["observed_monotonic_ns"] <= prior_monotonic_ns
        ):
            raise _safe_code("runtime receipt prefix was gapped or reordered")
        digest = hashlib.sha256(payload).hexdigest()
        if digest in digests:
            raise _safe_code("runtime receipt prefix duplicated canonical bytes")
        digests.add(digest)
        validated.append(document)
        prior_sequence = document["sequence"]
        prior_monotonic_ns = document["observed_monotonic_ns"]
    return validated


def cross_bind_runtime_status_receipt(
    resource: dict[str, Any], receipt: dict[str, Any]
) -> dict[str, Any]:
    """Require the HTTP status and latest durable receipt to describe one state."""
    if not isinstance(resource, dict):
        raise _safe_code("runtime status was unavailable for receipt binding")
    receipt_document = _receipt_document(receipt)
    priority = resource.get("priority_pressure")
    workload = resource.get("workload")
    identity = resource.get("runtime_identity")
    failures = resource.get("failure_counters")
    if not all(
        isinstance(value, dict) for value in (priority, workload, identity, failures)
    ):
        raise _safe_code("runtime status receipt binding was incomplete")
    assert isinstance(priority, dict)
    assert isinstance(workload, dict)
    assert isinstance(identity, dict)
    assert isinstance(failures, dict)
    priority_bindings = {
        "state": "priority_state",
        "heartbeat_age_ms": "heartbeat_age_ms",
        "source_age_ms": "source_age_ms",
        "policy_sha256": "policy_sha256",
        "observation_digest": "observation_digest",
        "transition_observation_digest": "transition_observation_digest",
        "transition_sequence": "transition_sequence",
        "controller_phase": "controller_phase",
        "recovery_reason": "recovery_reason",
        "distinct_clear_count": "distinct_clear_count",
        "model_resident": "model_resident",
        "model_load_generation": "model_load_generation",
        "model_unload_generation": "model_unload_generation",
    }
    workload_bindings = {
        "active": "active",
        "chunk_uncommitted": "chunk_uncommitted",
        "completion_generation": "completion_generation",
    }
    if (
        any(
            priority.get(status_key) != receipt_document[receipt_key]
            for status_key, receipt_key in priority_bindings.items()
        )
        or resource.get("controller_state") != receipt_document["controller_phase"]
        or resource.get("recovery_reason") != receipt_document["recovery_reason"]
        or resource.get("admission_open") is not receipt_document["admission_open"]
        or any(
            workload.get(status_key) != receipt_document[receipt_key]
            for status_key, receipt_key in workload_bindings.items()
        )
        or identity.get("epoch") != receipt_document["runtime_epoch"]
        or failures.get("cuda_oom_generation")
        != receipt_document["cuda_oom_generation"]
        or failures.get("media_failure_generation")
        != receipt_document["media_failure_generation"]
    ):
        raise _safe_code("runtime status and durable receipt disagreed")
    return resource


class ArtifactCreationLedger:
    """Count final-path creations even when a later unlink hides the file."""

    def __init__(self) -> None:
        self.output_create_count = 0
        self.marker_create_count = 0

    def record(self, artifact: str) -> None:
        if artifact == "output":
            self.output_create_count += 1
        elif artifact == "marker":
            self.marker_create_count += 1
        else:
            raise _safe_code("artifact creation kind was invalid")
        if self.output_create_count > 1 or self.marker_create_count > 1:
            raise _safe_code("artifact final path was created more than once")

    def snapshot(self, *, output_exists: bool, marker_exists: bool) -> dict[str, int]:
        if type(output_exists) is not bool or type(marker_exists) is not bool:
            raise _safe_code("artifact point-in-time state was invalid")
        return {
            "output_count": int(output_exists),
            "marker_count": int(marker_exists),
            "output_create_count": self.output_create_count,
            "marker_create_count": self.marker_create_count,
        }


class ExactArtifactWatcher:
    """Continuously watch exactly one final subtitle and one marker path."""

    EVENT_HEADER = struct.Struct("iIII")
    IN_MOVED_FROM = 0x00000040
    IN_MOVED_TO = 0x00000080
    IN_CREATE = 0x00000100
    IN_DELETE = 0x00000200
    IN_DELETE_SELF = 0x00000400
    IN_MOVE_SELF = 0x00000800
    IN_UNMOUNT = 0x00002000
    IN_Q_OVERFLOW = 0x00004000
    IN_IGNORED = 0x00008000
    WATCH_MASK = (
        IN_MOVED_FROM
        | IN_MOVED_TO
        | IN_CREATE
        | IN_DELETE
        | IN_DELETE_SELF
        | IN_MOVE_SELF
        | IN_UNMOUNT
    )
    INVALID_MASK = (
        IN_DELETE_SELF | IN_MOVE_SELF | IN_UNMOUNT | IN_Q_OVERFLOW | IN_IGNORED
    )

    def __init__(self, output: Path, marker: Path) -> None:
        if sys.platform != "linux":
            raise _safe_code("exact artifact watcher requires Linux inotify")
        self.output = self._validate_target(Path(output), "output")
        self.marker = self._validate_target(Path(marker), "marker")
        if self.output == self.marker:
            raise _safe_code("output and marker paths were not distinct")
        self.ledger = ArtifactCreationLedger()
        self._closed = False
        library = ctypes.CDLL(None, use_errno=True)
        try:
            initialize = library.inotify_init1
            add_watch = library.inotify_add_watch
        except AttributeError as exc:
            raise _safe_code("exact artifact watcher was unavailable") from exc
        initialize.argtypes = [ctypes.c_int]
        initialize.restype = ctypes.c_int
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int
        descriptor = initialize(os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        if descriptor < 0:
            raise _safe_code("exact artifact watcher could not be initialized")
        self._descriptor = descriptor
        self._watches: dict[int, dict[str, str]] = {}
        try:
            targets: dict[Path, dict[str, str]] = {}
            for artifact, target in (("output", self.output), ("marker", self.marker)):
                targets.setdefault(target.parent, {})[target.name] = artifact
            for parent, names in targets.items():
                before = parent.lstat()
                watch = add_watch(
                    descriptor,
                    os.fsencode(parent),
                    ctypes.c_uint32(self.WATCH_MASK),
                )
                after = parent.lstat()
                if (
                    watch < 0
                    or stat.S_ISLNK(after.st_mode)
                    or not stat.S_ISDIR(after.st_mode)
                    or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or watch in self._watches
                ):
                    raise _safe_code("exact artifact watch binding failed")
                self._watches[watch] = names
        except BaseException:
            os.close(descriptor)
            self._closed = True
            raise

    @staticmethod
    def _validate_target(path: Path, label: str) -> Path:
        if not path.is_absolute():
            raise _safe_code(f"fixture {label} path was not absolute")
        try:
            parent = path.parent.resolve(strict=True)
        except OSError as exc:
            raise _safe_code(f"fixture {label} parent was unavailable") from exc
        if parent != path.parent.absolute() or not parent.is_dir():
            raise _safe_code(f"fixture {label} parent was not one real directory")
        if path.exists() or path.is_symlink():
            raise _safe_code(f"fixture {label} already existed")
        return path

    @staticmethod
    def _exists_regular(path: Path, label: str) -> bool:
        try:
            item = path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _safe_code(f"fixture {label} state was unavailable") from exc
        if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
            raise _safe_code(f"fixture {label} was not a regular file")
        return True

    def _drain(self) -> None:
        if self._closed:
            raise _safe_code("exact artifact watcher was already closed")
        while True:
            try:
                payload = os.read(self._descriptor, MAX_EVENT_LOG_BYTES)
            except BlockingIOError:
                return
            except OSError as exc:
                raise _safe_code("exact artifact event read failed") from exc
            if not payload:
                raise _safe_code("exact artifact event stream closed")
            offset = 0
            while offset < len(payload):
                if len(payload) - offset < self.EVENT_HEADER.size:
                    raise _safe_code("exact artifact event was truncated")
                watch, mask, _cookie, name_length = self.EVENT_HEADER.unpack_from(
                    payload, offset
                )
                offset += self.EVENT_HEADER.size
                if name_length > 4096 or offset + name_length > len(payload):
                    raise _safe_code("exact artifact event length was invalid")
                raw_name = payload[offset : offset + name_length]
                offset += name_length
                if mask & self.INVALID_MASK:
                    raise _safe_code("exact artifact watch lost continuity")
                names = self._watches.get(watch)
                if names is None:
                    raise _safe_code("exact artifact watch identity changed")
                try:
                    name = raw_name.rstrip(b"\x00").decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise _safe_code("exact artifact filename was not UTF-8") from exc
                artifact = names.get(name)
                if artifact is not None and mask & (self.IN_CREATE | self.IN_MOVED_TO):
                    self.ledger.record(artifact)

    def snapshot(self) -> dict[str, int]:
        self._drain()
        return self.ledger.snapshot(
            output_exists=self._exists_regular(self.output, "output"),
            marker_exists=self._exists_regular(self.marker, "marker"),
        )

    def close(self) -> None:
        if not self._closed:
            os.close(self._descriptor)
            self._closed = True


class PhaseBLifecycleOrder:
    """Fail closed unless the real Phase-B lifecycle advances exactly once."""

    def __init__(self) -> None:
        self._index = 0

    def _require_next(self, stage: str) -> None:
        if (
            not isinstance(stage, str)
            or self._index >= len(PHASE_B_LIFECYCLE_STAGES)
            or stage != PHASE_B_LIFECYCLE_STAGES[self._index]
        ):
            raise _safe_code("Phase B lifecycle order changed")

    def checkpoint(self, stage: str) -> None:
        self._require_next(stage)
        self._index += 1

    def perform(self, stage: str, operation: Callable[[], Any]) -> Any:
        if not callable(operation):
            raise _safe_code("Phase B lifecycle operation was unavailable")
        self._require_next(stage)
        result = operation()
        self._index += 1
        return result

    def require_complete(self) -> None:
        if self._index != len(PHASE_B_LIFECYCLE_STAGES):
            raise _safe_code("Phase B lifecycle ended before final gate publication")


class GateOwnedCgroupParent:
    """Own one stable cgroup-v2 parent until stopped evidence is drained."""

    def __init__(
        self,
        *,
        driver: str,
        root: Path,
        path: Path,
        host_config_parent: str,
        name: str,
        path_identity: tuple[int, int],
        path_owner_uid: int,
        subtree_controllers: frozenset[str],
        slice_unit: str | None = None,
        keeper_unit: str | None = None,
    ) -> None:
        self.driver = driver
        self.root = root
        self.path = path
        self.host_config_parent = host_config_parent
        self.name = name
        self._path_identity = path_identity
        self._path_owner_uid = path_owner_uid
        self._subtree_controllers = subtree_controllers
        self.slice_unit = slice_unit
        self.keeper_unit = keeper_unit
        self._candidate_path: Path | None = None
        self._candidate_identity: tuple[int, int] | None = None
        self._created = True
        self._cleaned = False

    @staticmethod
    def _read_text(path: Path, *, label: str) -> str:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise _safe_code(f"{label} was unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise _safe_code(f"{label} was not a regular cgroup file")
            payload = _read_all_fd(descriptor, GATE_CGROUP_MAX_FILE_BYTES, label=label)
        finally:
            os.close(descriptor)
        try:
            return payload.decode("ascii")
        except UnicodeDecodeError as exc:
            raise _safe_code(f"{label} was not ASCII") from exc

    @staticmethod
    def _write_text(path: Path, payload: bytes, *, label: str) -> None:
        if not payload or len(payload) > 4096 or not payload.endswith(b"\n"):
            raise _safe_code(f"{label} payload was invalid")
        flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags)
            written = os.write(descriptor, payload)
        except OSError as exc:
            raise _safe_code(f"{label} failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if written != len(payload):
            raise _safe_code(f"{label} was incomplete")

    @staticmethod
    def _path_present(path: Path, *, label: str) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _safe_code(f"{label} could not be revalidated") from exc
        return True

    @classmethod
    def _require_path_removed(cls, path: Path, *, label: str) -> None:
        deadline = time.monotonic() + 5.0
        while cls._path_present(path, label=label) and time.monotonic() < deadline:
            time.sleep(0.05)
        if cls._path_present(path, label=label):
            raise _safe_code(f"{label} survived cleanup")

    @classmethod
    def _controller_set(cls, path: Path, *, label: str) -> frozenset[str]:
        raw = cls._read_text(path, label=label)
        values = raw.split()
        if len(values) != len(set(values)) or any(
            re.fullmatch(r"[a-z][a-z0-9_]*", value) is None for value in values
        ):
            raise _safe_code(f"{label} was malformed")
        return frozenset(values)

    @classmethod
    def _event_values(cls, path: Path) -> dict[str, int]:
        return health.parse_key_value_lines(
            cls._read_text(path / "cgroup.events", label="gate parent cgroup events"),
            "gate parent cgroup events",
            required_keys={"populated", "frozen"},
        )

    @classmethod
    def _memory_values(cls, path: Path) -> dict[str, int]:
        return health.parse_key_value_lines(
            cls._read_text(path / "memory.events", label="gate parent memory events"),
            "gate parent memory events",
            required_keys=health.REQUIRED_MEMORY_EVENTS,
        )

    @classmethod
    def _pid_set(cls, path: Path, *, label: str) -> set[int]:
        raw = cls._read_text(path / "cgroup.procs", label=label)
        result: set[int] = set()
        for line in raw.splitlines():
            if re.fullmatch(r"[1-9][0-9]*", line) is None:
                raise _safe_code(f"{label} was malformed")
            value = int(line)
            if value > 2**63 - 1 or value in result:
                raise _safe_code(f"{label} was invalid")
            result.add(value)
        return result

    @staticmethod
    def _parse_systemd_properties(
        output: str, *, expected_keys: frozenset[str], label: str
    ) -> dict[str, str]:
        if not isinstance(output, str) or len(output.encode("utf-8")) > 64 * 1024:
            raise _safe_code(f"{label} was malformed")
        properties: dict[str, str] = {}
        for line in output.splitlines():
            if "=" not in line:
                raise _safe_code(f"{label} was malformed")
            key, value = line.split("=", 1)
            if key not in expected_keys or key in properties:
                raise _safe_code(f"{label} was malformed")
            properties[key] = value
        if frozenset(properties) != expected_keys:
            raise _safe_code(f"{label} was incomplete")
        return properties

    @classmethod
    def _systemd_show(
        cls, unit: str, properties: tuple[str, ...], *, label: str
    ) -> dict[str, str]:
        result = health.bounded_command(
            [
                "/usr/bin/systemctl",
                "show",
                unit,
                "--no-pager",
                *(f"--property={name}" for name in properties),
            ],
            label=label,
            timeout=10,
            max_bytes=64 * 1024,
        )
        return cls._parse_systemd_properties(
            result.output, expected_keys=frozenset(properties), label=label
        )

    @classmethod
    def _validate_root(cls, root: Path) -> Path:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise _safe_code("gate cgroup parent requires Linux root supervision")
        try:
            resolved = root.resolve(strict=True)
            metadata = root.lstat()
        except OSError as exc:
            raise _safe_code("cgroup-v2 root was unavailable") from exc
        if (
            resolved != root.absolute()
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
        ):
            raise _safe_code("cgroup-v2 root identity was unsafe")
        controllers = cls._controller_set(
            root / "cgroup.controllers", label="cgroup-v2 root controllers"
        )
        delegated = cls._controller_set(
            root / "cgroup.subtree_control",
            label="cgroup-v2 root subtree controllers",
        )
        if not GATE_CGROUP_REQUIRED_CONTROLLERS.issubset(controllers) or not (
            GATE_CGROUP_REQUIRED_CONTROLLERS.issubset(delegated)
        ):
            raise _safe_code("cgroup-v2 root did not delegate required controllers")
        return resolved

    @classmethod
    def _create_cgroupfs_parent(cls, path: Path) -> None:
        try:
            os.mkdir(path, 0o755)
        except FileExistsError as exc:
            raise _safe_code("gate cgroup parent already existed") from exc
        except OSError as exc:
            raise _safe_code("gate cgroup parent could not be created") from exc

    @classmethod
    def _delegate_cgroupfs_controllers(cls, path: Path) -> None:
        payload = (
            " ".join(
                f"+{name}" for name in sorted(GATE_CGROUP_REQUIRED_CONTROLLERS)
            ).encode("ascii")
            + b"\n"
        )
        cls._write_text(
            path / "cgroup.subtree_control",
            payload,
            label="gate cgroup controller delegation",
        )

    @classmethod
    def _create_systemd_parent(cls, *, slice_unit: str, keeper_unit: str) -> None:
        result = health.bounded_command(
            [
                "/usr/bin/systemd-run",
                f"--unit={keeper_unit}",
                f"--slice={slice_unit}",
                "--collect",
                "--service-type=oneshot",
                "--property=RemainAfterExit=yes",
                "--property=User=root",
                "--property=Group=root",
                "--property=UMask=0077",
                "--property=NoNewPrivileges=yes",
                "--property=PrivateTmp=yes",
                "--property=MemoryAccounting=yes",
                "--property=TasksAccounting=yes",
                "--quiet",
                "--",
                "/usr/bin/true",
            ],
            label="gate cgroup systemd keeper creation",
            timeout=20,
            max_bytes=64 * 1024,
        )
        if result.output.strip():
            raise _safe_code("gate cgroup systemd keeper emitted output")

    @staticmethod
    def _binding_for_driver(
        driver: str, gate_token_digest: str
    ) -> tuple[str, str, str | None, str | None]:
        if (
            driver not in {"cgroupfs", "systemd"}
            or LOWER_HEX_64_RE.fullmatch(gate_token_digest) is None
        ):
            raise _safe_code("Docker cgroup driver binding was invalid")
        name = GATE_CGROUP_NAME_PREFIX + gate_token_digest
        if driver == "systemd":
            slice_unit = f"{name}.slice"
            return name, slice_unit, slice_unit, f"{name}keeper.service"
        return name, f"/{name}", None, None

    @classmethod
    def create(
        cls,
        client: Any,
        candidate_item: dict[str, Any],
        binding: Any,
        *,
        cgroup_root: Path | None = None,
    ) -> GateOwnedCgroupParent:
        """Create one token-bound parent before the stopped candidate starts."""
        if not isinstance(candidate_item, dict) or not LOWER_HEX_64_RE.fullmatch(
            str(getattr(binding, "gate_token_digest", ""))
        ):
            raise _safe_code("gate cgroup candidate identity was invalid")
        client.verify_local_daemon()
        driver_result = client.command(
            "info",
            "--format",
            "{{.CgroupDriver}}",
            label="Docker cgroup driver",
            max_bytes=1024,
        )
        driver = driver_result.output.strip()
        if driver not in {"cgroupfs", "systemd"}:
            raise _safe_code("Docker cgroup driver was unsupported")
        root = cls._validate_root(
            Path(health.CGROUP_ROOT if cgroup_root is None else cgroup_root)
        )
        name, host_config_parent, slice_unit, keeper_unit = cls._binding_for_driver(
            driver, binding.gate_token_digest
        )
        host = candidate_item.get("HostConfigFull")
        if not isinstance(host, dict) or host.get("CgroupParent") != host_config_parent:
            raise _safe_code("candidate cgroup parent binding was not exact")
        path = root / (slice_unit if slice_unit is not None else name)
        if cls._path_present(path, label="gate cgroup parent"):
            raise _safe_code("gate cgroup parent already existed")

        created = False
        try:
            if driver == "systemd":
                assert slice_unit is not None and keeper_unit is not None
                cls._create_systemd_parent(
                    slice_unit=slice_unit, keeper_unit=keeper_unit
                )
                created = True
                deadline = time.monotonic() + 5.0
                while (
                    not cls._path_present(path, label="gate cgroup systemd parent")
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                if not cls._path_present(path, label="gate cgroup systemd parent"):
                    raise _safe_code("gate cgroup systemd slice was not created")
            else:
                cls._create_cgroupfs_parent(path)
                created = True
                cls._delegate_cgroupfs_controllers(path)
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_dev != root.lstat().st_dev
            ):
                raise _safe_code("gate cgroup parent identity was unsafe")
            parent_controllers = cls._controller_set(
                path / "cgroup.controllers", label="gate cgroup parent controllers"
            )
            subtree_controllers = cls._controller_set(
                path / "cgroup.subtree_control",
                label="gate cgroup parent subtree controllers",
            )
            if (
                not GATE_CGROUP_REQUIRED_CONTROLLERS.issubset(parent_controllers)
                or (
                    driver == "cgroupfs"
                    and subtree_controllers != GATE_CGROUP_REQUIRED_CONTROLLERS
                )
                or not GATE_CGROUP_REQUIRED_CONTROLLERS.issubset(subtree_controllers)
            ):
                raise _safe_code("gate cgroup parent controller delegation was invalid")
            if (
                cls._read_text(
                    path / "cgroup.type", label="gate cgroup parent type"
                ).strip()
                != "domain"
            ):
                raise _safe_code("gate cgroup parent was not a domain")
            if cls._pid_set(path, label="gate cgroup parent process set"):
                raise _safe_code("gate cgroup parent contained direct processes")
            events = cls._event_values(path)
            cls._memory_values(path)
            if events != {"populated": 0, "frozen": 0}:
                raise _safe_code("gate cgroup parent was not initially empty")
            instance = cls(
                driver=driver,
                root=root,
                path=path,
                host_config_parent=host_config_parent,
                name=name,
                path_identity=(metadata.st_dev, metadata.st_ino),
                path_owner_uid=metadata.st_uid,
                subtree_controllers=subtree_controllers,
                slice_unit=slice_unit,
                keeper_unit=keeper_unit,
            )
            if driver == "systemd":
                instance._verify_systemd_owner()
            return instance
        except BaseException:
            if created:
                cls._cleanup_failed_creation(
                    driver=driver,
                    path=path,
                    slice_unit=slice_unit,
                    keeper_unit=keeper_unit,
                )
            raise

    @classmethod
    def _cleanup_failed_creation(
        cls,
        *,
        driver: str,
        path: Path,
        slice_unit: str | None,
        keeper_unit: str | None,
    ) -> None:
        if driver == "systemd":
            if (
                not slice_unit
                or not keeper_unit
                or path.name != slice_unit
                or not slice_unit.startswith(GATE_CGROUP_NAME_PREFIX)
                or not keeper_unit.startswith(GATE_CGROUP_NAME_PREFIX)
            ):
                raise _safe_code("failed gate cgroup systemd identity was unavailable")
            result = health.bounded_command(
                ["/usr/bin/systemctl", "stop", slice_unit, "--no-pager"],
                label="failed gate cgroup systemd cleanup",
                timeout=20,
                max_bytes=64 * 1024,
                allow_failure=True,
            )
            if result.returncode == 0 and result.output.strip():
                raise _safe_code("failed gate cgroup systemd cleanup emitted output")
            cls._require_path_removed(path, label="failed gate cgroup systemd parent")
            return

        if driver != "cgroupfs":
            raise _safe_code("failed gate cgroup driver identity was invalid")
        if not cls._path_present(path, label="failed gate cgroup parent"):
            return
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise _safe_code(
                "failed gate cgroup parent identity was unavailable"
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
        ):
            raise _safe_code("failed gate cgroup parent identity was unsafe")
        if cls._pid_set(path, label="failed gate cgroup parent process set"):
            raise _safe_code("failed gate cgroup parent remained directly populated")
        if cls._event_values(path) != {"populated": 0, "frozen": 0}:
            raise _safe_code("failed gate cgroup parent remained populated")
        try:
            children = list(path.iterdir())
        except OSError as exc:
            raise _safe_code(
                "failed gate cgroup parent children were unavailable"
            ) from exc
        for child in children:
            try:
                child_metadata = child.lstat()
            except OSError as exc:
                raise _safe_code("failed gate cgroup parent child changed") from exc
            if stat.S_ISLNK(child_metadata.st_mode) or stat.S_ISDIR(
                child_metadata.st_mode
            ):
                raise _safe_code("failed gate cgroup parent retained a child")
        controllers = cls._controller_set(
            path / "cgroup.subtree_control",
            label="failed gate cgroup parent subtree controllers",
        )
        if not controllers.issubset(GATE_CGROUP_REQUIRED_CONTROLLERS):
            raise _safe_code("failed gate cgroup controller identity changed")
        if controllers:
            payload = (
                " ".join(f"-{name}" for name in sorted(controllers)).encode("ascii")
                + b"\n"
            )
            cls._write_text(
                path / "cgroup.subtree_control",
                payload,
                label="failed gate cgroup controller cleanup",
            )
            if cls._controller_set(
                path / "cgroup.subtree_control",
                label="failed gate cgroup parent subtree controllers",
            ):
                raise _safe_code("failed gate cgroup controllers remained delegated")
        try:
            os.rmdir(path)
        except OSError as exc:
            raise _safe_code("failed gate cgroup parent removal failed") from exc
        cls._require_path_removed(path, label="failed gate cgroup parent")

    def _verify_systemd_owner(self) -> None:
        if self.driver != "systemd" or not self.slice_unit or not self.keeper_unit:
            raise _safe_code("gate cgroup systemd owner identity was unavailable")
        slice_properties = self._systemd_show(
            self.slice_unit,
            ("LoadState", "ActiveState", "ControlGroup"),
            label="gate cgroup systemd slice inspection",
        )
        keeper_properties = self._systemd_show(
            self.keeper_unit,
            (
                "LoadState",
                "ActiveState",
                "SubState",
                "ControlGroup",
                "MainPID",
                "MemoryAccounting",
                "Result",
                "Slice",
                "TasksAccounting",
            ),
            label="gate cgroup systemd keeper inspection",
        )
        expected_slice_path = "/" + self.slice_unit
        expected_keeper_path = f"{expected_slice_path}/{self.keeper_unit}"
        if slice_properties != {
            "LoadState": "loaded",
            "ActiveState": "active",
            "ControlGroup": expected_slice_path,
        } or keeper_properties != {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "exited",
            "ControlGroup": expected_keeper_path,
            "MainPID": "0",
            "MemoryAccounting": "yes",
            "Result": "success",
            "Slice": self.slice_unit,
            "TasksAccounting": "yes",
        }:
            raise _safe_code("gate cgroup systemd owner identity was not exact")

    def _assert_identity(self) -> None:
        if self._cleaned:
            raise _safe_code("gate cgroup parent was already cleaned")
        try:
            current = self.path.lstat()
        except OSError as exc:
            raise _safe_code("gate cgroup parent disappeared") from exc
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != self._path_identity
            or current.st_uid != self._path_owner_uid
        ):
            raise _safe_code("gate cgroup parent identity changed")
        controllers = self._controller_set(
            self.path / "cgroup.subtree_control",
            label="gate cgroup parent subtree controllers",
        )
        if controllers != self._subtree_controllers:
            raise _safe_code("gate cgroup parent controller delegation changed")
        if self.driver == "systemd":
            self._verify_systemd_owner()

    def _direct_child_directories(self) -> set[str]:
        result: set[str] = set()
        try:
            items = list(self.path.iterdir())
        except OSError as exc:
            raise _safe_code("gate cgroup parent children were unavailable") from exc
        for item in items:
            try:
                metadata = item.lstat()
            except OSError as exc:
                raise _safe_code("gate cgroup parent child changed") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise _safe_code("gate cgroup parent contained a symlink")
            if stat.S_ISDIR(metadata.st_mode):
                result.add(item.name)
        return result

    def _allowed_keeper_directory(self) -> str | None:
        if self.driver != "systemd" or not self.keeper_unit:
            return None
        keeper_path = self.path / self.keeper_unit
        if not keeper_path.exists():
            return None
        if self._pid_set(keeper_path, label="gate cgroup keeper process set"):
            raise _safe_code("gate cgroup keeper contained a live process")
        events = self._event_values(keeper_path)
        if events != {"populated": 0, "frozen": 0}:
            raise _safe_code("gate cgroup keeper was populated")
        return self.keeper_unit

    def bind_live(self, candidate_probe: Any) -> None:
        """Prove the candidate is the sole populated child of this parent."""
        resolver = getattr(candidate_probe, "_candidate_pid_and_cgroup", None)
        pid_set_reader = getattr(candidate_probe, "_pid_set", None)
        if not callable(resolver) or not callable(pid_set_reader):
            raise _safe_code("candidate cgroup placement probe was unavailable")
        pid, candidate_path = resolver()
        candidate_path = Path(candidate_path)
        self._assert_identity()
        try:
            candidate_metadata = candidate_path.lstat()
        except OSError as exc:
            raise _safe_code("candidate cgroup child was unavailable") from exc
        if (
            candidate_path.parent != self.path
            or stat.S_ISLNK(candidate_metadata.st_mode)
            or not stat.S_ISDIR(candidate_metadata.st_mode)
            or candidate_metadata.st_dev != self._path_identity[0]
        ):
            raise _safe_code("candidate was outside the gate-owned cgroup parent")
        pids = pid_set_reader(expected_pid=pid, cgroup=candidate_path)
        if pid not in pids:
            raise _safe_code("candidate PID was outside its exact cgroup child")
        if self._pid_set(self.path, label="gate cgroup parent process set"):
            raise _safe_code("gate cgroup parent contained direct processes")
        keeper = self._allowed_keeper_directory()
        expected = {candidate_path.name}
        if keeper is not None:
            expected.add(keeper)
        if self._direct_child_directories() != expected:
            raise _safe_code("gate cgroup parent contained an unexpected child")
        if self._event_values(self.path) != {"populated": 1, "frozen": 0}:
            raise _safe_code("gate cgroup parent was not exactly populated")
        self._candidate_path = candidate_path
        self._candidate_identity = (
            candidate_metadata.st_dev,
            candidate_metadata.st_ino,
        )

    def assert_live_placement(self, candidate_probe: Any) -> tuple[int, Path]:
        if self._candidate_path is None or self._candidate_identity is None:
            raise _safe_code("gate cgroup parent was not bound to the candidate")
        resolver = getattr(candidate_probe, "_candidate_pid_and_cgroup", None)
        if not callable(resolver):
            raise _safe_code("candidate cgroup placement probe was unavailable")
        pid, candidate_path = resolver()
        candidate_path = Path(candidate_path)
        self._assert_identity()
        try:
            metadata = candidate_path.lstat()
        except OSError as exc:
            raise _safe_code("candidate cgroup child disappeared while live") from exc
        if (
            candidate_path != self._candidate_path
            or (metadata.st_dev, metadata.st_ino) != self._candidate_identity
            or self._pid_set(self.path, label="gate cgroup parent process set")
            or self._event_values(self.path) != {"populated": 1, "frozen": 0}
        ):
            raise _safe_code("candidate gate cgroup placement changed")
        keeper = self._allowed_keeper_directory()
        expected = {candidate_path.name}
        if keeper is not None:
            expected.add(keeper)
        if self._direct_child_directories() != expected:
            raise _safe_code("gate cgroup parent child set changed")
        return pid, candidate_path

    def memory_events(self, candidate_probe: Any) -> Any:
        before_pid, _before_path = self.assert_live_placement(candidate_probe)
        values = self._memory_values(self.path)
        after_pid, _after_path = self.assert_live_placement(candidate_probe)
        if after_pid != before_pid:
            raise _safe_code("candidate PID changed during gate parent observation")
        return health.CgroupSnapshot(
            container_pid=before_pid,
            cgroup_path_sha256=hashlib.sha256(
                str(self.path).encode("utf-8")
            ).hexdigest(),
            oom=values["oom"],
            oom_kill=values["oom_kill"],
            oom_group_kill=values["oom_group_kill"],
        )

    def pinned_source(self, candidate_probe: Any) -> tuple[int, Path]:
        pid, _candidate_path = self.assert_live_placement(candidate_probe)
        return pid, self.path

    def cleanup(self) -> None:
        """Remove only the empty gate-owned parent after final evidence."""
        if self._cleaned:
            return
        self._assert_identity()
        if self._pid_set(self.path, label="gate cgroup parent process set"):
            raise _safe_code("gate cgroup parent remained directly populated")
        if self._event_values(self.path) != {"populated": 0, "frozen": 0}:
            raise _safe_code("gate cgroup parent remained populated at cleanup")
        deadline = time.monotonic() + 5.0
        while True:
            keeper = self._allowed_keeper_directory()
            expected = {keeper} if keeper is not None else set()
            children = self._direct_child_directories()
            if children == expected:
                break
            candidate_name = (
                self._candidate_path.name if self._candidate_path is not None else None
            )
            if candidate_name is None or children - expected != {candidate_name}:
                raise _safe_code("gate cgroup parent retained an unexpected child")
            if time.monotonic() >= deadline:
                raise _safe_code("gate cgroup parent retained a candidate child")
            time.sleep(0.05)
            self._assert_identity()
            if self._event_values(self.path) != {"populated": 0, "frozen": 0}:
                raise _safe_code("gate cgroup parent repopulated during cleanup")
        if self.driver == "systemd":
            assert self.slice_unit is not None and self.keeper_unit is not None
            result = health.bounded_command(
                ["/usr/bin/systemctl", "stop", self.slice_unit, "--no-pager"],
                label="gate cgroup systemd cleanup",
                timeout=20,
                max_bytes=64 * 1024,
            )
            if result.output.strip():
                raise _safe_code("gate cgroup systemd cleanup emitted output")
            self._require_path_removed(self.path, label="gate cgroup systemd parent")
        else:
            self._write_text(
                self.path / "cgroup.subtree_control",
                b"-memory -pids\n",
                label="gate cgroup controller cleanup",
            )
            if self._controller_set(
                self.path / "cgroup.subtree_control",
                label="gate cgroup parent subtree controllers",
            ):
                raise _safe_code("gate cgroup controllers remained delegated")
            try:
                os.rmdir(self.path)
            except OSError as exc:
                raise _safe_code("gate cgroup parent removal failed") from exc
            self._require_path_removed(self.path, label="gate cgroup parent")
        self._cleaned = True


class GateOwnedCgroupProbe:
    """Expose the stable parent for memory and the candidate child for GPU PIDs."""

    def __init__(self, parent: GateOwnedCgroupParent, candidate_probe: Any) -> None:
        self.parent = parent
        self.candidate_probe = candidate_probe

    def memory_events(self) -> Any:
        return self.parent.memory_events(self.candidate_probe)

    def attributed_gpu_bytes(self, gpu_uuid: str) -> Any:
        self.parent.assert_live_placement(self.candidate_probe)
        result = self.candidate_probe.attributed_gpu_bytes(gpu_uuid)
        self.parent.assert_live_placement(self.candidate_probe)
        return result

    def pinned_source(self) -> tuple[int, Path]:
        return self.parent.pinned_source(self.candidate_probe)


@dataclass(frozen=True)
class PinnedCgroupSnapshot:
    """One descriptor-bound view of hierarchical cgroup-v2 failure state."""

    container_pid: int
    cgroup_path_sha256: str
    oom: int
    oom_kill: int
    oom_group_kill: int
    descendants_populated: int
    frozen: int


class PinnedCgroupEvidence:
    """Keep exact memory/cgroup event files readable across candidate stop."""

    _MAX_FILE_BYTES = 64 * 1024
    _CGROUP_EVENT_KEYS = {"populated", "frozen"}

    def __init__(
        self,
        *,
        container_pid: int,
        path: Path,
        path_identity: tuple[int, int],
        directory_descriptor: int,
        directory_identity: tuple[int, int, int],
        memory_descriptor: int,
        memory_identity: tuple[int, int, int],
        events_descriptor: int,
        events_identity: tuple[int, int, int],
    ) -> None:
        self.container_pid = container_pid
        self.path = path
        self.cgroup_path_sha256 = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        self._path_identity = path_identity
        self._directory_descriptor = directory_descriptor
        self._directory_identity = directory_identity
        self._memory_descriptor = memory_descriptor
        self._memory_identity = memory_identity
        self._events_descriptor = events_descriptor
        self._events_identity = events_identity
        self._closed = False

    @staticmethod
    def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
        return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))

    @classmethod
    def capture(
        cls, cgroup_probe: Any, *, baseline: GateFailureBaseline
    ) -> PinnedCgroupEvidence:
        """Pin the live candidate hierarchy and prove it matches the baseline."""
        if sys.platform != "linux" or not isinstance(baseline, GateFailureBaseline):
            raise _safe_code("pinned cgroup evidence requires a Linux gate baseline")
        resolver = getattr(cgroup_probe, "pinned_source", None)
        if not callable(resolver):
            raise _safe_code("gate-owned cgroup resolver was unavailable")
        before_pid, raw_path = resolver()
        path = Path(raw_path)
        if (
            not _exact_int(before_pid, minimum=1)
            or before_pid != baseline.cgroup_pid
            or not path.is_absolute()
            or hashlib.sha256(str(path).encode("utf-8")).hexdigest()
            != baseline.cgroup_path_sha256
        ):
            raise _safe_code("pinned cgroup source did not match the Phase B baseline")
        try:
            initial = path.lstat()
        except OSError as exc:
            raise _safe_code(
                "candidate cgroup path was unavailable for pinning"
            ) from exc
        if stat.S_ISLNK(initial.st_mode) or not stat.S_ISDIR(initial.st_mode):
            raise _safe_code("candidate cgroup path was unsafe for pinning")
        descriptors: list[int] = []
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        file_flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            directory_descriptor = os.open(path, directory_flags)
            descriptors.append(directory_descriptor)
            directory_metadata = os.fstat(directory_descriptor)
            if not stat.S_ISDIR(directory_metadata.st_mode) or (
                directory_metadata.st_dev,
                directory_metadata.st_ino,
            ) != (initial.st_dev, initial.st_ino):
                raise _safe_code(
                    "candidate cgroup directory identity changed while pinned"
                )
            memory_descriptor = os.open(
                "memory.events", file_flags, dir_fd=directory_descriptor
            )
            descriptors.append(memory_descriptor)
            events_descriptor = os.open(
                "cgroup.events", file_flags, dir_fd=directory_descriptor
            )
            descriptors.append(events_descriptor)
            memory_metadata = os.fstat(memory_descriptor)
            events_metadata = os.fstat(events_descriptor)
            if not stat.S_ISREG(memory_metadata.st_mode) or not stat.S_ISREG(
                events_metadata.st_mode
            ):
                raise _safe_code("candidate cgroup evidence files were not regular")
            after_pid, after_path = resolver()
            if (after_pid, Path(after_path)) != (before_pid, path):
                raise _safe_code(
                    "candidate PID or gate cgroup changed while evidence was pinned"
                )
            evidence = cls(
                container_pid=before_pid,
                path=path,
                path_identity=(initial.st_dev, initial.st_ino),
                directory_descriptor=directory_descriptor,
                directory_identity=cls._identity(directory_metadata),
                memory_descriptor=memory_descriptor,
                memory_identity=cls._identity(memory_metadata),
                events_descriptor=events_descriptor,
                events_identity=cls._identity(events_metadata),
            )
            live = evidence.snapshot(expected_population=1)
            if (
                live.oom != baseline.cgroup_oom
                or live.oom_kill != baseline.cgroup_oom_kill
                or live.oom_group_kill != baseline.cgroup_oom_group_kill
            ):
                raise _safe_code(
                    "cgroup failure counters changed while evidence was pinned"
                )
            descriptors.clear()
            return evidence
        except BaseException:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    @classmethod
    def _read_descriptor(
        cls,
        descriptor: int,
        expected_identity: tuple[int, int, int],
        *,
        label: str,
    ) -> str:
        try:
            before = os.fstat(descriptor)
            if cls._identity(before) != expected_identity:
                raise _safe_code(f"{label} descriptor identity changed")
            if hasattr(os, "pread"):
                payload = bytearray()
                offset = 0
                while len(payload) <= cls._MAX_FILE_BYTES:
                    chunk = os.pread(
                        descriptor,
                        min(4096, cls._MAX_FILE_BYTES + 1 - len(payload)),
                        offset,
                    )
                    if not chunk:
                        break
                    payload.extend(chunk)
                    offset += len(chunk)
            else:
                os.lseek(descriptor, 0, os.SEEK_SET)
                payload = bytearray(
                    _read_all_fd(descriptor, cls._MAX_FILE_BYTES, label=label)
                )
            after = os.fstat(descriptor)
        except OSError as exc:
            raise _safe_code(f"{label} descriptor became unavailable") from exc
        if cls._identity(after) != expected_identity:
            raise _safe_code(f"{label} descriptor changed while read")
        if len(payload) > cls._MAX_FILE_BYTES:
            raise _safe_code(f"{label} exceeded its byte limit")
        try:
            return bytes(payload).decode("ascii")
        except UnicodeDecodeError as exc:
            raise _safe_code(f"{label} was not ASCII") from exc

    def _assert_identity(self) -> None:
        if self._closed:
            raise _safe_code("pinned cgroup evidence was already closed")
        try:
            directory = os.fstat(self._directory_descriptor)
        except OSError as exc:
            raise _safe_code("pinned cgroup directory became unavailable") from exc
        if self._identity(directory) != self._directory_identity:
            raise _safe_code("pinned cgroup directory identity changed")
        try:
            current = self.path.lstat()
        except OSError as exc:
            raise _safe_code("pinned gate cgroup path disappeared") from exc
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != self._path_identity
        ):
            raise _safe_code("pinned cgroup path was changed or reused")

    def snapshot(self, *, expected_population: int) -> PinnedCgroupSnapshot:
        if expected_population not in (0, 1):
            raise _safe_code("pinned cgroup population expectation was invalid")
        self._assert_identity()
        memory_values = health.parse_key_value_lines(
            self._read_descriptor(
                self._memory_descriptor,
                self._memory_identity,
                label="pinned cgroup memory events",
            ),
            "pinned cgroup memory events",
            required_keys=health.REQUIRED_MEMORY_EVENTS,
        )
        event_values = health.parse_key_value_lines(
            self._read_descriptor(
                self._events_descriptor,
                self._events_identity,
                label="pinned cgroup descendant events",
            ),
            "pinned cgroup descendant events",
            required_keys=self._CGROUP_EVENT_KEYS,
        )
        if (
            event_values["populated"] != expected_population
            or event_values["frozen"] != 0
        ):
            raise _safe_code("pinned cgroup descendant population was not exact")
        return PinnedCgroupSnapshot(
            container_pid=self.container_pid,
            cgroup_path_sha256=self.cgroup_path_sha256,
            oom=memory_values["oom"],
            oom_kill=memory_values["oom_kill"],
            oom_group_kill=memory_values["oom_group_kill"],
            descendants_populated=event_values["populated"],
            frozen=event_values["frozen"],
        )

    def close(self) -> None:
        if self._closed:
            return
        failure: OSError | None = None
        for descriptor in (
            self._events_descriptor,
            self._memory_descriptor,
            self._directory_descriptor,
        ):
            try:
                os.close(descriptor)
            except OSError as exc:
                if failure is None:
                    failure = exc
        self._closed = True
        if failure is not None:
            raise _safe_code("pinned cgroup evidence cleanup failed") from failure


@dataclass(frozen=True)
class GateFailureBaseline:
    candidate_restart_count: int
    candidate_oom_killed: bool
    frigate_restart_count: int
    cgroup_pid: int
    cgroup_path_sha256: str
    cgroup_oom: int
    cgroup_oom_kill: int
    cgroup_oom_group_kill: int
    runtime_cuda_oom_generation: int
    runtime_media_failure_generation: int
    candidate_log_byte_cursor: int
    candidate_cuda_oom_log_matches: int
    candidate_log_source_sha256: str
    kernel_cursor_sha256: str
    nvidia_xid_log_matches: int


def capture_gate_failure_baseline(
    *,
    client: Any,
    candidate: Any,
    frigate: Any,
    args: argparse.Namespace,
    cgroup_probe: Any,
    candidate_log: Any,
    kernel_journal: Any,
    receipt: dict[str, Any],
) -> GateFailureBaseline:
    """Capture independent process-lifetime failure sources at a phase boundary."""
    state = health.candidate_state(client, candidate, args)
    observed = health.observed_state(client, frigate)
    cgroup = cgroup_probe.memory_events()
    log = candidate_log.snapshot()
    kernel = kernel_journal.snapshot()
    receipt_document = _receipt_document(receipt)
    if (
        state["running"] is not True
        or state["status"] != "running"
        or state["oom_killed"] is not False
        or observed["running"] is not True
        or observed["health"] != "healthy"
        or log.continuous is not True
        or kernel.continuous is not True
    ):
        raise _safe_code("gate failure baseline was not healthy and continuous")
    return GateFailureBaseline(
        candidate_restart_count=state["restart_count"],
        candidate_oom_killed=state["oom_killed"],
        frigate_restart_count=observed["restart_count"],
        cgroup_pid=cgroup.container_pid,
        cgroup_path_sha256=cgroup.cgroup_path_sha256,
        cgroup_oom=cgroup.oom,
        cgroup_oom_kill=cgroup.oom_kill,
        cgroup_oom_group_kill=cgroup.oom_group_kill,
        runtime_cuda_oom_generation=receipt_document["cuda_oom_generation"],
        runtime_media_failure_generation=receipt_document["media_failure_generation"],
        candidate_log_byte_cursor=log.byte_cursor,
        candidate_cuda_oom_log_matches=log.cuda_oom_matches,
        candidate_log_source_sha256=log.source_container_id_sha256,
        kernel_cursor_sha256=kernel.cursor_sha256,
        nvidia_xid_log_matches=kernel.xid_matches,
    )


def _nonnegative_delta(current: int, baseline: int, label: str) -> int:
    if not _exact_int(current) or not _exact_int(baseline) or current < baseline:
        raise _safe_code(f"{label} counter regressed")
    return current - baseline


def capture_gate_failure_deltas(
    *,
    baseline: GateFailureBaseline,
    client: Any,
    candidate: Any,
    frigate: Any,
    args: argparse.Namespace,
    cgroup_probe: Any,
    candidate_log: Any,
    kernel_journal: Any,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Re-read every independent failure source and return exact phase deltas."""
    state = health.candidate_state(client, candidate, args)
    observed = health.observed_state(client, frigate)
    cgroup = cgroup_probe.memory_events()
    log = candidate_log.snapshot()
    kernel = kernel_journal.snapshot()
    receipt_document = _receipt_document(receipt)
    if (
        state["running"] is not True
        or state["status"] != "running"
        or state["oom_killed"] is not False
        or observed["running"] is not True
        or observed["health"] != "healthy"
        or log.continuous is not True
        or kernel.continuous is not True
        or log.source_container_id_sha256 != baseline.candidate_log_source_sha256
        or log.byte_cursor < baseline.candidate_log_byte_cursor
        or cgroup.container_pid != baseline.cgroup_pid
        or cgroup.cgroup_path_sha256 != baseline.cgroup_path_sha256
    ):
        raise _safe_code("gate failure source identity or continuity changed")
    return {
        "candidate_restart_delta": _nonnegative_delta(
            state["restart_count"],
            baseline.candidate_restart_count,
            "candidate restart",
        ),
        "candidate_oom_killed": state["oom_killed"],
        "frigate_restart_delta": _nonnegative_delta(
            observed["restart_count"], baseline.frigate_restart_count, "Frigate restart"
        ),
        "cgroup_oom_delta": _nonnegative_delta(
            cgroup.oom, baseline.cgroup_oom, "cgroup oom"
        ),
        "cgroup_oom_kill_delta": _nonnegative_delta(
            cgroup.oom_kill, baseline.cgroup_oom_kill, "cgroup oom_kill"
        ),
        "cgroup_oom_group_kill_delta": _nonnegative_delta(
            cgroup.oom_group_kill,
            baseline.cgroup_oom_group_kill,
            "cgroup oom_group_kill",
        ),
        "runtime_cuda_oom_generation_delta": _nonnegative_delta(
            receipt_document["cuda_oom_generation"],
            baseline.runtime_cuda_oom_generation,
            "runtime CUDA OOM generation",
        ),
        "runtime_media_failure_generation_delta": _nonnegative_delta(
            receipt_document["media_failure_generation"],
            baseline.runtime_media_failure_generation,
            "runtime media failure generation",
        ),
        "candidate_cuda_oom_log_match_delta": _nonnegative_delta(
            log.cuda_oom_matches,
            baseline.candidate_cuda_oom_log_matches,
            "candidate CUDA OOM log match",
        ),
        "nvidia_xid_log_match_delta": _nonnegative_delta(
            kernel.xid_matches,
            baseline.nvidia_xid_log_matches,
            "NVIDIA Xid log match",
        ),
    }


def capture_phase_b_host_sample(
    *,
    baseline: GateFailureBaseline,
    client: Any,
    candidate: Any,
    frigate: Any,
    args: argparse.Namespace,
    cgroup_probe: Any,
    candidate_log: Any,
    kernel_journal: Any,
    receipt: dict[str, Any],
    camera_expectations: dict[str, float],
    camera_low_since: dict[str, float | None],
) -> dict[str, Any]:
    """Capture one complete live sample with no source standing in for another."""
    now_monotonic = time.monotonic()
    now_wall = time.time()
    receipt_document = _receipt_document(receipt)
    deltas = capture_gate_failure_deltas(
        baseline=baseline,
        client=client,
        candidate=candidate,
        frigate=frigate,
        args=args,
        cgroup_probe=cgroup_probe,
        candidate_log=candidate_log,
        kernel_journal=kernel_journal,
        receipt=receipt_document,
    )
    if any(
        deltas[key] != 0
        for key in (
            "candidate_restart_delta",
            "frigate_restart_delta",
            "cgroup_oom_delta",
            "cgroup_oom_kill_delta",
            "cgroup_oom_group_kill_delta",
            "runtime_cuda_oom_generation_delta",
            "runtime_media_failure_generation_delta",
            "candidate_cuda_oom_log_match_delta",
            "nvidia_xid_log_match_delta",
        )
    ):
        raise _safe_code("gate failure counter increased")
    host_available = health.read_mem_available_bytes()
    if host_available < args.host_reserve_bytes:
        raise _safe_code("host memory reserve breached")
    health.read_host_pressure()
    memory = health.candidate_memory(client, candidate)
    health.validate_candidate_memory_snapshot(
        memory, expected_memory_bytes=args.expected_memory_bytes
    )
    gpu = health.gpu_telemetry()
    status = validate_runtime_status(
        health.fetch_json(args.candidate_status_url, endpoint="candidate"),
        expected_model=args.expected_model,
        expected_reserve_bytes=args.gpu_free_floor_bytes,
        observed_gpu_total_bytes=gpu["total_mib"] * MIB,
        expected_priority_state=receipt_document["priority_state"],
        expected_policy_sha256=receipt_document["policy_sha256"],
        expected_controller_phase=receipt_document["controller_phase"],
        expected_recovery_reason=receipt_document["recovery_reason"],
        expected_admission_open=receipt_document["admission_open"],
        expected_model_resident=receipt_document["model_resident"],
        require_gate_runtime=receipt_document["active"],
    )
    cross_bind_runtime_status_receipt(status, receipt_document)
    stats = health.fetch_json(args.frigate_stats_url, endpoint="frigate")
    metrics = health.validate_frigate_stats(
        stats,
        camera_expectations,
        camera_low_since,
        now_monotonic,
        now_wall,
    )
    detection_fps = health.finite_number(
        stats.get("detection_fps"), "Frigate total detection FPS"
    )
    ollama = health.fetch_json(args.ollama_url, endpoint="ollama")
    models = ollama.get("models")
    if not isinstance(models, list):
        raise _safe_code("Ollama model telemetry was malformed")
    sample = {
        "candidate_running": True,
        "detection_fps": detection_fps,
        "camera_min_process_ratio": metrics["camera_min_process_ratio"],
        "camera_max_skipped_fps": metrics["camera_max_skipped_fps"],
        "camera_low_ratio_elapsed_ms": int(
            round(metrics["camera_longest_low_seconds"] * 1000)
        ),
        "detector_count": metrics["detector_count"],
        "detector_stalled_count": 0,
        "embedding_metric_count": metrics["embedding_metric_count"],
        "embedding_invalid_count": 0,
        **deltas,
        "ollama_loaded": bool(models),
    }
    return _validate_phase_b_host_sample(sample)


def _validate_phase_a_host_observation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != PHASE_A_HOST_OBSERVATION_KEYS:
        raise _safe_code("Phase A host observation keys were invalid")
    for key in PHASE_A_HOST_OBSERVATION_KEYS - {"threshold_masking_allowed"}:
        if not _exact_int(value[key]):
            raise _safe_code(f"Phase A host observation {key} was invalid")
    if type(value["threshold_masking_allowed"]) is not bool:
        raise _safe_code("Phase A threshold masking flag was not a boolean")
    return value


class PhaseAEventCollector:
    """Fail-closed ten-event Phase-A state machine over the receipt journal."""

    def __init__(
        self,
        *,
        runtime_epoch: str,
        runtime_started_monotonic_ns: int,
        gate_token_sha256: str,
        workload_sha256: str,
        workload_identity: dict[str, Any],
        policy_sha256: str,
        assertion: PriorityAssertion,
        model_identity_sha256: str,
        allowed_unloaded_bytes: int,
    ) -> None:
        hashes = (
            gate_token_sha256,
            workload_sha256,
            policy_sha256,
            model_identity_sha256,
        )
        if (
            not LOWER_HEX_32_RE.fullmatch(runtime_epoch)
            or any(LOWER_HEX_64_RE.fullmatch(value) is None for value in hashes)
            or not _exact_int(runtime_started_monotonic_ns, minimum=1)
            or not _exact_int(allowed_unloaded_bytes)
            or not isinstance(workload_identity, dict)
            or set(workload_identity)
            != {
                "fixture_sha256",
                "task",
                "language",
                "cursor_start_ms",
                "total_duration_ms",
            }
            or _canonical_document_sha256(
                workload_identity, label="Phase A workload identity"
            )
            != workload_sha256
            or not isinstance(assertion, PriorityAssertion)
            or assertion.document.get("policy_sha256") != policy_sha256
        ):
            raise _safe_code("Phase A event collector binding was invalid")
        cursor = workload_identity.get("cursor_start_ms")
        duration = workload_identity.get("total_duration_ms")
        if (
            not _exact_int(cursor)
            or not _exact_int(duration, minimum=1)
            or duration <= cursor
        ):
            raise _safe_code("Phase A workload cursor was invalid")
        self.runtime_epoch = runtime_epoch
        self.runtime_started_monotonic_ns = runtime_started_monotonic_ns
        self.gate_token_sha256 = gate_token_sha256
        self.workload_sha256 = workload_sha256
        self.workload_identity = dict(workload_identity)
        self.policy_sha256 = policy_sha256
        self.assertion = assertion
        self.model_identity_sha256 = model_identity_sha256
        self.allowed_unloaded_bytes = allowed_unloaded_bytes
        self._events: list[dict[str, Any]] = []

    @property
    def events(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self._events]

    def _validate_event_equations(self, event: dict[str, Any]) -> None:
        index = event["event_index"]
        t0 = self.assertion.t0_monotonic_ns
        if index == 0:
            if not (
                self.runtime_started_monotonic_ns
                < event["monotonic_ns"]
                < self.assertion.attestation["observed_monotonic_ns"]
                <= t0
            ):
                raise _safe_code("Phase A pre-assertion timing was invalid")
            if not (
                event["priority_state"] in {"clear", "neutral"}
                and event["controller_phase"] == "normal"
                and event["recovery_reason"] is None
                and event["admission_open"] is True
                and (event["priority_state"], event["distinct_clear_count"])
                in {("clear", 3), ("neutral", 0)}
            ):
                raise _safe_code("Phase A pre-assertion state was invalid")
        elif event["monotonic_ns"] <= self._events[-1]["monotonic_ns"]:
            raise _safe_code("Phase A event time did not strictly increase")

        if index in (1, 2) and event["monotonic_ns"] > t0 + 15_000_000_000:
            raise _safe_code("Phase A yield deadline was exceeded")
        if index == 3 and event["monotonic_ns"] > t0 + 30_000_000_000:
            raise _safe_code("Phase A runtime unload deadline was exceeded")
        if index == 4 and event["monotonic_ns"] > t0 + 45_000_000_000:
            raise _safe_code("Phase A host unloaded-envelope deadline was exceeded")

        asserted = index in range(1, 5)
        expected_clear_counts = {5: 1, 6: 2, 7: 3, 8: 3, 9: 3}
        if asserted and not (
            event["priority_state"] == "asserted"
            and event["distinct_clear_count"] == 0
            and event["recovery_reason"] == "priority_pressure"
            and event["admission_open"] is False
            and (
                (index == 1 and event["controller_phase"] in {"yielding", "recovering"})
                or (index > 1 and event["controller_phase"] == "recovering")
            )
        ):
            raise _safe_code("Phase A asserted state sequence was invalid")
        if index in expected_clear_counts and not (
            event["priority_state"] == "clear"
            and event["distinct_clear_count"] == expected_clear_counts[index]
            and (
                (
                    index in (5, 6)
                    and event["controller_phase"] == "recovering"
                    and event["recovery_reason"] == "priority_pressure"
                    and event["admission_open"] is False
                )
                or (
                    index >= 7
                    and event["controller_phase"] == "normal"
                    and event["recovery_reason"] is None
                    and event["admission_open"] is True
                )
            )
        ):
            raise _safe_code("Phase A clear recovery sequence was invalid")

        expected_resident = index in {0, 1, 2, 8, 9}
        expected_active = index != 9
        expected_chunk = index in {0, 1}
        expected_mask = index in {4, 5, 6, 7}
        if (
            event["model_resident"] is not expected_resident
            or event["workload_active"] is not expected_active
            or event["chunk_uncommitted"] is not expected_chunk
            or event["threshold_masking_allowed"] is not expected_mask
            or event["model_identity_sha256"]
            != (self.model_identity_sha256 if expected_resident else None)
        ):
            raise _safe_code("Phase A model workload or masking state was invalid")

        cursor = self.workload_identity["cursor_start_ms"]
        duration = self.workload_identity["total_duration_ms"]
        if event["cursor_ms"] != (None if index == 9 else cursor) or event[
            "last_completed_cursor_ms"
        ] != (duration if index == 9 else None):
            raise _safe_code("Phase A workload cursor equation was invalid")
        if index < 9:
            if any(
                event[key] != 0
                for key in (
                    "output_count",
                    "marker_count",
                    "output_create_count",
                    "marker_create_count",
                )
            ):
                raise _safe_code("Phase A output or marker appeared before completion")
        elif (
            event["output_count"],
            event["marker_count"],
            event["output_create_count"],
            event["marker_create_count"],
        ) != (1, 0, 1, 0):
            raise _safe_code("Phase A final output creation equation was invalid")

        if index == 0:
            return
        baseline = self._events[0]
        expected_load = baseline["model_load_generation"] + (index >= 8)
        expected_unload = baseline["model_unload_generation"] + (index >= 3)
        expected_completion = baseline["completion_generation"] + (index == 9)
        if (
            event["model_load_generation"] != expected_load
            or event["model_unload_generation"] != expected_unload
            or event["completion_generation"] != expected_completion
            or event["cuda_oom_generation"] != baseline["cuda_oom_generation"]
            or event["media_failure_generation"] != baseline["media_failure_generation"]
            or event["source_generation"] < self._events[-1]["source_generation"]
        ):
            raise _safe_code("Phase A generation equation was invalid")
        if index == 1 and not (
            event["source_generation"]
            == self.assertion.attestation["source_generation"]
            and event["observation_digest"]
            == self.assertion.attestation["observation_digest"]
            and event["transition_observation_digest"]
            == self.assertion.attestation["observation_digest"]
            and event["transition_sequence"] == baseline["transition_sequence"] + 1
        ):
            raise _safe_code("Phase A assertion receipt binding was invalid")
        if index in (2, 3, 4) and not (
            event["transition_sequence"] == self._events[1]["transition_sequence"]
            and event["transition_observation_digest"]
            == self.assertion.attestation["observation_digest"]
        ):
            raise _safe_code("Phase A asserted transition changed")
        if index == 5 and not (
            event["source_generation"] > self._events[1]["source_generation"]
            and event["transition_sequence"]
            == self._events[1]["transition_sequence"] + 1
            and event["transition_observation_digest"] == event["observation_digest"]
        ):
            raise _safe_code("Phase A first clear transition was invalid")
        if index in (6, 7):
            if not (
                event["source_generation"]
                > self._events[index - 1]["source_generation"]
                and event["observation_digest"]
                not in {item["observation_digest"] for item in self._events[5:index]}
            ):
                raise _safe_code("Phase A clear observations were not distinct")
        if index >= 6 and not (
            event["transition_sequence"] == self._events[5]["transition_sequence"]
            and event["transition_observation_digest"]
            == self._events[5]["transition_observation_digest"]
        ):
            raise _safe_code("Phase A recovery transition changed")
        if index == 4 and event["candidate_bytes"] > self.allowed_unloaded_bytes:
            raise _safe_code("Phase A candidate bytes exceeded unloaded envelope")

    def record_event(
        self,
        kind: str,
        *,
        receipts: list[dict[str, Any]],
        host_observation: dict[str, Any],
    ) -> dict[str, Any]:
        index = len(self._events)
        if index >= len(PHASE_A_EVENT_KINDS) or kind != PHASE_A_EVENT_KINDS[index]:
            raise _safe_code("Phase A event order was invalid")
        host = _validate_phase_a_host_observation(host_observation)
        validated = _validated_receipt_prefix(
            receipts,
            expected_runtime_epoch=self.runtime_epoch,
            expected_token_sha256=self.gate_token_sha256,
        )
        receipt, digest = _receipt_at(
            validated, host["monotonic_ns"], label=f"Phase A event {index}"
        )
        if index != 4 and host["monotonic_ns"] != receipt["observed_monotonic_ns"]:
            raise _safe_code("Phase A event did not use its receipt publication time")
        if receipt["workload_sha256"] != self.workload_sha256:
            raise _safe_code("Phase A event receipt workload changed")
        event = {
            "event_index": index,
            "kind": kind,
            "monotonic_ns": host["monotonic_ns"],
            "runtime_epoch": receipt["runtime_epoch"],
            "runtime_started_monotonic_ns": self.runtime_started_monotonic_ns,
            "gate_receipt_sha256": digest,
            "output_count": host["output_count"],
            "marker_count": host["marker_count"],
            "output_create_count": host["output_create_count"],
            "marker_create_count": host["marker_create_count"],
            "threshold_masking_allowed": host["threshold_masking_allowed"],
            "candidate_bytes": host["candidate_bytes"],
        }
        for event_key, receipt_key in _EVENT_RECEIPT_BINDINGS.items():
            event[event_key] = receipt[receipt_key]
        if event["policy_sha256"] != self.policy_sha256:
            raise _safe_code("Phase A receipt policy changed")
        self._validate_event_equations(event)
        self._events.append(event)
        return dict(event)

    def require_complete(self) -> list[dict[str, Any]]:
        if len(self._events) != len(PHASE_A_EVENT_KINDS):
            raise _safe_code("Phase A event sequence was incomplete")
        return self.events


class ProtectedPhaseASampleCollector:
    """Retain only cadence/failure proof for the unmasked Phase-A interval."""

    def __init__(self) -> None:
        self._sample_times: list[int] = []
        self._blind_interval_count = 0
        self._threshold_failure_count = 0

    def record(
        self,
        *,
        captured_monotonic_ns: int,
        telemetry_valid: bool,
        threshold_failed: bool,
    ) -> None:
        if (
            not _exact_int(captured_monotonic_ns, minimum=1)
            or type(telemetry_valid) is not bool
            or type(threshold_failed) is not bool
        ):
            raise _safe_code("protected Phase A sample was malformed")
        if not telemetry_valid:
            self._blind_interval_count += 1
            raise _safe_code("protected Phase A telemetry became blind")
        if threshold_failed:
            self._threshold_failure_count += 1
            raise _safe_code("protected Phase A health threshold failed")
        if self._sample_times:
            interval = captured_monotonic_ns - self._sample_times[-1]
            if interval <= 0 or interval > 2_000_000_000:
                self._blind_interval_count += 1
                raise _safe_code("protected Phase A sampling cadence became blind")
        self._sample_times.append(captured_monotonic_ns)

    def proof(
        self, *, t0_monotonic_ns: int, gpu_proof_monotonic_ns: int
    ) -> dict[str, int]:
        if self._blind_interval_count or self._threshold_failure_count:
            raise _safe_code("protected Phase A sampling had a failure")
        cadence = health.validate_protected_sample_cadence(
            self._sample_times,
            t0_monotonic_ns=t0_monotonic_ns,
            gpu_proof_monotonic_ns=gpu_proof_monotonic_ns,
        )
        return {
            **cadence,
            "protected_threshold_failure_count": self._threshold_failure_count,
        }


_EVENT_RECEIPT_BINDINGS = {
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


def _canonical_document_sha256(document: Any, *, label: str) -> str:
    return hashlib.sha256(_canonical_ascii_json_line(document, label=label)).hexdigest()


def _receipt_at(
    receipts: list[dict[str, Any]], observed_monotonic_ns: int, *, label: str
) -> tuple[dict[str, Any], str]:
    normalized = [_receipt_document(receipt) for receipt in receipts]
    matching = [
        (index, receipt)
        for index, receipt in enumerate(normalized)
        if receipt["observed_monotonic_ns"] <= observed_monotonic_ns
    ]
    if not matching:
        raise _safe_code(f"{label} preceded the receipt journal")
    index, receipt = matching[-1]
    if index + 1 < len(normalized) and not (
        observed_monotonic_ns < normalized[index + 1]["observed_monotonic_ns"]
    ):
        raise _safe_code(f"{label} receipt selection was ambiguous")
    digest = _canonical_document_sha256(receipt, label=f"{label} receipt")
    return receipt, digest


def verify_phase_a_receipt_bindings(
    phase_a: dict[str, Any],
    trace: dict[str, Any],
    *,
    assertion_attestation: dict[str, Any],
    expected_model_identity_sha256: str,
) -> None:
    """Cross-bind the ten host events to the lossless runtime receipt trace."""
    if (
        not isinstance(phase_a, dict)
        or not isinstance(trace, dict)
        or not isinstance(assertion_attestation, dict)
        or not LOWER_HEX_64_RE.fullmatch(expected_model_identity_sha256)
        or phase_a.get("gate_receipt_trace_sha256")
        != _canonical_document_sha256(trace, label="Phase A receipt trace")
        or trace.get("runtime_epoch") != phase_a.get("runtime_epoch")
        or trace.get("workload_sha256") != phase_a.get("workload_sha256")
    ):
        raise _safe_code("Phase A receipt trace binding was invalid")
    receipts = trace.get("receipts")
    events = phase_a.get("events")
    if not isinstance(receipts, list) or not receipts or not isinstance(events, list):
        raise _safe_code("Phase A receipt trace or events were unavailable")
    if len(events) != 10:
        raise _safe_code("Phase A event count was invalid")
    token_sha256 = trace.get("gate_token_sha256")
    runtime_epoch = trace.get("runtime_epoch")
    if (
        not isinstance(token_sha256, str)
        or not LOWER_HEX_64_RE.fullmatch(token_sha256)
        or not isinstance(runtime_epoch, str)
        or not LOWER_HEX_32_RE.fullmatch(runtime_epoch)
    ):
        raise _safe_code("Phase A receipt trace identity was invalid")
    receipts = _validated_receipt_prefix(
        receipts,
        expected_runtime_epoch=runtime_epoch,
        expected_token_sha256=token_sha256,
    )
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise _safe_code("Phase A event was not an object")
        receipt, digest = _receipt_at(
            receipts, event.get("monotonic_ns"), label=f"Phase A event {index}"
        )
        if event.get("gate_receipt_sha256") != digest:
            raise _safe_code("Phase A event receipt hash did not bind latest state")
        if index != 4 and event.get("monotonic_ns") != receipt.get(
            "observed_monotonic_ns"
        ):
            raise _safe_code("Phase A event time did not bind its receipt")
        for event_key, receipt_key in _EVENT_RECEIPT_BINDINGS.items():
            if event.get(event_key) != receipt.get(receipt_key):
                raise _safe_code(f"Phase A event receipt field {event_key} disagreed")
    final_receipt_hash = _canonical_document_sha256(
        receipts[-1], label="Phase A final receipt"
    )
    if events[-1].get("gate_receipt_sha256") != final_receipt_hash:
        raise _safe_code("Phase A receipt trace continued beyond completion")

    assertion_event = events[1]
    if (
        assertion_event.get("source_generation")
        != assertion_attestation.get("source_generation")
        or assertion_event.get("observation_digest")
        != assertion_attestation.get("observation_digest")
        or assertion_event.get("transition_observation_digest")
        != assertion_attestation.get("observation_digest")
        or phase_a.get("assertion_observation_digest")
        != assertion_attestation.get("observation_digest")
        or phase_a.get("assertion_observed_monotonic_ns")
        != assertion_attestation.get("observed_monotonic_ns")
        or phase_a.get("assertion_reason_codes")
        != assertion_attestation.get("reason_codes")
    ):
        raise _safe_code("Phase A assertion observation did not bind event one")
    clear_digests = [event.get("observation_digest") for event in events[5:8]]
    if len(set(clear_digests)) != 3:
        raise _safe_code("Phase A clear observations were not distinct")
    for index in (0, 1, 2, 8, 9):
        if events[index].get("model_identity_sha256") != (
            expected_model_identity_sha256
        ):
            raise _safe_code("Phase A resident model identity did not match catalog")
    if any(
        events[index].get("model_identity_sha256") is not None for index in range(3, 8)
    ):
        raise _safe_code("Phase A unloaded event retained a model identity")

    first_event_time = events[0].get("monotonic_ns")
    if not _exact_int(first_event_time, minimum=1):
        raise _safe_code("Phase A pre-assertion event time was invalid")
    initial = receipts[0]
    admitted = False
    stage_boundaries = (
        (events[1]["monotonic_ns"], 0),
        (events[2]["monotonic_ns"], 1),
        (events[3]["monotonic_ns"], 2),
        (events[5]["monotonic_ns"], 3),
        (events[6]["monotonic_ns"], 5),
        (events[7]["monotonic_ns"], 6),
        (events[8]["monotonic_ns"], 7),
        (events[9]["monotonic_ns"], 8),
        (2**63 - 1, 9),
    )
    continuous_keys = {
        "transition_observation_digest": "transition_observation_digest",
        "transition_sequence": "transition_sequence",
        "priority_state": "priority_state",
        "controller_phase": "controller_phase",
        "recovery_reason": "recovery_reason",
        "admission_open": "admission_open",
        "distinct_clear_count": "distinct_clear_count",
        "model_resident": "model_resident",
        "model_load_generation": "model_load_generation",
        "model_unload_generation": "model_unload_generation",
        "model_identity_sha256": "model_identity_sha256",
        "active": "workload_active",
        "chunk_uncommitted": "chunk_uncommitted",
        "active_cursor_ms": "cursor_ms",
        "completed_cursor_ms": "last_completed_cursor_ms",
        "completion_generation": "completion_generation",
        "cuda_oom_generation": "cuda_oom_generation",
        "media_failure_generation": "media_failure_generation",
    }
    for receipt in receipts:
        receipt_time = receipt["observed_monotonic_ns"]
        if receipt_time < first_event_time:
            if (
                receipt["workload_sha256"] is not None
                or receipt["active"] is not False
                or receipt["chunk_uncommitted"] is not False
                or receipt["active_cursor_ms"] is not None
                or receipt["completed_cursor_ms"] is not None
                or any(
                    receipt[key] != initial[key]
                    for key in (
                        "completion_generation",
                        "cuda_oom_generation",
                        "media_failure_generation",
                    )
                )
            ):
                raise _safe_code("Phase A pre-admission receipt was not idle")
            continue
        if not admitted:
            admitted = True
            if not (
                receipt["workload_sha256"] == phase_a.get("workload_sha256")
                and receipt["active"] is True
                and receipt["chunk_uncommitted"] is True
            ):
                raise _safe_code("Phase A first workload receipt was not admission")
        if receipt["workload_sha256"] != phase_a.get("workload_sha256"):
            raise _safe_code("Phase A receipt workload changed after admission")
        stage = next(
            stage_index
            for boundary_time, stage_index in stage_boundaries
            if receipt_time < boundary_time
        )
        expected_event = events[stage]
        if any(
            receipt[receipt_key] != expected_event[event_key]
            for receipt_key, event_key in continuous_keys.items()
        ):
            raise _safe_code("Phase A receipt hid a runtime transition between events")
    if not admitted:
        raise _safe_code("Phase A receipt trace never admitted its workload")


_SAMPLE_RECEIPT_BINDINGS = {
    key: value
    for key, value in _EVENT_RECEIPT_BINDINGS.items()
    if key not in {"chunk_uncommitted", "cursor_ms", "last_completed_cursor_ms"}
}


def _validate_phase_b_host_sample(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != PHASE_B_HOST_SAMPLE_KEYS:
        raise _safe_code("Phase B host sample keys were invalid")
    boolean_keys = {"candidate_running", "candidate_oom_killed", "ollama_loaded"}
    number_keys = {
        "detection_fps",
        "camera_min_process_ratio",
        "camera_max_skipped_fps",
    }
    for key in boolean_keys:
        if type(value[key]) is not bool:
            raise _safe_code(f"Phase B host sample {key} was not a boolean")
    for key in number_keys:
        if (
            isinstance(value[key], bool)
            or not isinstance(value[key], (int, float))
            or not math.isfinite(float(value[key]))
        ):
            raise _safe_code(f"Phase B host sample {key} was not finite")
    for key in PHASE_B_HOST_SAMPLE_KEYS - boolean_keys - number_keys:
        if not _exact_int(value[key]):
            raise _safe_code(f"Phase B host sample {key} was not an integer")
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
    if (
        any(value[key] != 0 for key in zero_keys)
        or value["candidate_running"] is not True
        or value["candidate_oom_killed"] is not False
        or value["ollama_loaded"] is not False
        or not float(value["detection_fps"]) < 80
        or not float(value["camera_min_process_ratio"]) >= 0.98
        or float(value["camera_max_skipped_fps"]) != 0
        or value["detector_count"] <= 0
        or value["embedding_metric_count"] <= 0
    ):
        raise _safe_code("Phase B host sample failed an exact health equation")
    return value


class PhaseBSampleCollector:
    """Collect exactly t=0,5,...,900 without catch-up or status blind spots."""

    def __init__(
        self,
        *,
        started_monotonic_ns: int,
        runtime_epoch: str,
        runtime_started_monotonic_ns: int,
        gate_token_sha256: str,
        workload_sha256: str,
        policy_sha256: str,
        producer_epoch: str,
        candidate_identity_sha256: str,
        model_identity_sha256: str,
    ) -> None:
        hashes = (
            gate_token_sha256,
            workload_sha256,
            policy_sha256,
            candidate_identity_sha256,
            model_identity_sha256,
        )
        if (
            not _exact_int(started_monotonic_ns, minimum=1)
            or not _exact_int(runtime_started_monotonic_ns, minimum=1)
            or not LOWER_HEX_32_RE.fullmatch(runtime_epoch)
            or not LOWER_HEX_32_RE.fullmatch(producer_epoch)
            or any(LOWER_HEX_64_RE.fullmatch(value) is None for value in hashes)
        ):
            raise _safe_code("Phase B sample collector binding was invalid")
        self.started_monotonic_ns = started_monotonic_ns
        self.runtime_epoch = runtime_epoch
        self.runtime_started_monotonic_ns = runtime_started_monotonic_ns
        self.gate_token_sha256 = gate_token_sha256
        self.workload_sha256 = workload_sha256
        self.policy_sha256 = policy_sha256
        self.producer_epoch = producer_epoch
        self.candidate_identity_sha256 = candidate_identity_sha256
        self.model_identity_sha256 = model_identity_sha256
        self._samples: list[dict[str, Any]] = []

    @property
    def samples(self) -> list[dict[str, Any]]:
        return [dict(sample) for sample in self._samples]

    def capture_sample(
        self,
        *,
        captured_monotonic_ns: int,
        receipts: list[dict[str, Any]],
        host_sample: dict[str, Any],
    ) -> dict[str, Any]:
        index = len(self._samples)
        if index > 180 or not _exact_int(captured_monotonic_ns, minimum=1):
            raise _safe_code("Phase B sample index or capture time was invalid")
        scheduled = self.started_monotonic_ns + index * 5_000_000_000
        if not scheduled <= captured_monotonic_ns <= scheduled + 2_000_000_000:
            raise _safe_code("Phase B sample missed its exact schedule")
        if self._samples and (
            captured_monotonic_ns - self._samples[-1]["captured_monotonic_ns"]
            < 3_000_000_000
        ):
            raise _safe_code("Phase B sample attempted catch-up")
        host = _validate_phase_b_host_sample(host_sample)
        validated = _validated_receipt_prefix(
            receipts,
            expected_runtime_epoch=self.runtime_epoch,
            expected_token_sha256=self.gate_token_sha256,
        )
        receipt, digest = _receipt_at(
            validated, captured_monotonic_ns, label=f"Phase B sample {index}"
        )
        if not (
            receipt["workload_sha256"] == self.workload_sha256
            and receipt["active"] is True
            and receipt["priority_state"] == "clear"
            and receipt["controller_phase"] == "normal"
            and receipt["recovery_reason"] is None
            and receipt["admission_open"] is True
            and receipt["distinct_clear_count"] == 3
            and receipt["model_resident"] is True
            and receipt["model_identity_sha256"] == self.model_identity_sha256
            and receipt["policy_sha256"] == self.policy_sha256
        ):
            raise _safe_code("Phase B receipt was not continuously clear and active")
        sample = {
            "sample_index": index,
            "scheduled_offset_seconds": index * 5,
            "captured_monotonic_ns": captured_monotonic_ns,
            "producer_epoch": self.producer_epoch,
            "runtime_epoch": self.runtime_epoch,
            "runtime_started_monotonic_ns": self.runtime_started_monotonic_ns,
            "candidate_identity_sha256": self.candidate_identity_sha256,
            "gate_receipt_sha256": digest,
            **host,
        }
        for sample_key, receipt_key in _SAMPLE_RECEIPT_BINDINGS.items():
            sample[sample_key] = receipt[receipt_key]
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
        if self._samples and any(
            sample[key] != self._samples[0][key] for key in stable_keys
        ):
            raise _safe_code("Phase B runtime generation changed")
        if (
            self._samples
            and sample["source_generation"] < self._samples[-1]["source_generation"]
        ):
            raise _safe_code("Phase B source generation regressed")
        self._samples.append(sample)
        return dict(sample)

    def require_complete(
        self, *, ended_monotonic_ns: int, receipts: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if (
            len(self._samples) != 181
            or not _exact_int(ended_monotonic_ns, minimum=1)
            or ended_monotonic_ns < self._samples[-1]["captured_monotonic_ns"]
            or ended_monotonic_ns - self.started_monotonic_ns < 900_000_000_000
        ):
            raise _safe_code("Phase B did not complete all 181 scheduled samples")
        validated = _validated_receipt_prefix(
            receipts,
            expected_runtime_epoch=self.runtime_epoch,
            expected_token_sha256=self.gate_token_sha256,
        )
        post_end = [
            index
            for index, receipt in enumerate(validated)
            if receipt["observed_monotonic_ns"] > ended_monotonic_ns
        ]
        if len(post_end) != 1 or post_end[0] != len(validated) - 1:
            raise _safe_code("Phase B lacked its first post-end receipt sentinel")
        return self.samples


def run_phase_b_schedule(
    collector: PhaseBSampleCollector,
    journal: RuntimeReceiptJournal,
    *,
    capture_host_sample: Callable[[int], dict[str, Any]],
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Execute the exact Phase-B schedule; lateness aborts instead of catching up."""
    if collector.samples:
        raise _safe_code("Phase B scheduler requires a fresh collector")
    for index in range(181):
        scheduled = collector.started_monotonic_ns + index * 5_000_000_000
        now = monotonic_ns()
        if now > scheduled + 2_000_000_000:
            raise _safe_code("Phase B scheduler was already too late")
        if now < scheduled:
            sleeper((scheduled - now) / 1_000_000_000)
        journal.read_available()
        host = capture_host_sample(index)
        journal.read_available()
        captured = monotonic_ns()
        collector.capture_sample(
            captured_monotonic_ns=captured,
            receipts=journal.receipts,
            host_sample=host,
        )
    return collector.samples


def build_phase_a_receipt_trace_document(
    *,
    receipts: list[dict[str, Any]],
    runtime_epoch: str,
    gate_token_sha256: str,
    workload_sha256: str,
    completion_event: dict[str, Any],
) -> dict[str, Any]:
    """Freeze the complete journal prefix ending at Phase-A completion."""
    validated = _validated_receipt_prefix(
        receipts,
        expected_runtime_epoch=runtime_epoch,
        expected_token_sha256=gate_token_sha256,
    )
    if not isinstance(completion_event, dict):
        raise _safe_code("Phase A completion event was unavailable")
    final_digest = _canonical_document_sha256(
        validated[-1], label="Phase A completion receipt"
    )
    if (
        completion_event.get("kind") != "completed"
        or completion_event.get("event_index") != 9
        or completion_event.get("gate_receipt_sha256") != final_digest
        or completion_event.get("monotonic_ns")
        != validated[-1]["observed_monotonic_ns"]
    ):
        raise _safe_code("Phase A trace did not end at completion")
    document = {
        "schema": "subgen.task11b.runtime-receipt-trace/v1",
        "runtime_epoch": runtime_epoch,
        "gate_token_sha256": gate_token_sha256,
        "workload_sha256": workload_sha256,
        "receipts": validated,
    }
    return health.validate_runtime_receipt_trace_document(document)


def build_phase_b_receipt_trace_document(
    *,
    all_receipts: list[dict[str, Any]],
    runtime_epoch: str,
    gate_token_sha256: str,
    phase_a_trace_sha256: str,
    phase_a_last_sequence: int,
    workload_sha256: str,
    ended_monotonic_ns: int,
) -> dict[str, Any]:
    """Freeze the exact post-A journal range through the first post-end record."""
    validated = _validated_receipt_prefix(
        all_receipts,
        expected_runtime_epoch=runtime_epoch,
        expected_token_sha256=gate_token_sha256,
    )
    if (
        not LOWER_HEX_64_RE.fullmatch(phase_a_trace_sha256)
        or not LOWER_HEX_64_RE.fullmatch(workload_sha256)
        or not _exact_int(phase_a_last_sequence, minimum=1)
        or not _exact_int(ended_monotonic_ns, minimum=1)
        or phase_a_last_sequence >= validated[-1]["sequence"]
    ):
        raise _safe_code("Phase B receipt trace binding was invalid")
    try:
        start = next(
            index
            for index, receipt in enumerate(validated)
            if receipt["sequence"] == phase_a_last_sequence + 1
        )
    except StopIteration as exc:
        raise _safe_code("Phase B receipt trace did not continue Phase A") from exc
    post_end = next(
        (
            index
            for index in range(start, len(validated))
            if validated[index]["observed_monotonic_ns"] > ended_monotonic_ns
        ),
        None,
    )
    if post_end is None:
        raise _safe_code("Phase B receipt trace lacked a post-end sentinel")
    phase_receipts = validated[start : post_end + 1]
    document = {
        "schema": "subgen.task11b.phase-b-runtime-receipt-trace/v1",
        "runtime_epoch": runtime_epoch,
        "gate_token_sha256": gate_token_sha256,
        "phase_a_trace_sha256": phase_a_trace_sha256,
        "phase_a_last_sequence": phase_a_last_sequence,
        "workload_sha256": workload_sha256,
        "receipts": phase_receipts,
    }
    return health.validate_phase_b_receipt_trace_document(document)


def write_phase_a_artifacts(
    *,
    trace_path: Path,
    seal_path: Path,
    trace_document: dict[str, Any],
    phase_document: dict[str, Any],
) -> tuple[Any, Any]:
    """Create Phase-A trace first and cross-bind its exact bytes into the seal."""
    trace = health.write_runtime_receipt_trace_document(trace_path, trace_document)
    phase = dict(phase_document)
    phase["gate_receipt_trace_sha256"] = trace.file_sha256
    phase_artifact = health.write_phase_a_document(seal_path, phase)
    return trace, phase_artifact


def write_phase_b_artifacts(
    *,
    trace_path: Path,
    seal_path: Path,
    trace_document: dict[str, Any],
    phase_document: dict[str, Any],
) -> tuple[Any, Any]:
    """Create Phase-B trace first and cross-bind its exact bytes into the seal."""
    trace = health.write_phase_b_receipt_trace_document(trace_path, trace_document)
    phase = dict(phase_document)
    phase["gate_receipt_trace_sha256"] = trace.file_sha256
    phase_artifact = health.write_phase_b_document(seal_path, phase)
    return trace, phase_artifact


def verify_phase_b_receipt_bindings(
    phase_a: dict[str, Any],
    phase_b: dict[str, Any],
    trace: dict[str, Any],
    *,
    expected_phase_a_sha256: str,
    phase_a_trace: dict[str, Any] | None = None,
) -> None:
    """Prove every Phase-B capture and intervening transition from the journal."""
    if (
        not isinstance(phase_a, dict)
        or not isinstance(phase_b, dict)
        or not isinstance(trace, dict)
        or not LOWER_HEX_64_RE.fullmatch(expected_phase_a_sha256)
        or phase_b.get("phase_a_seal_sha256") != expected_phase_a_sha256
        or phase_b.get("gate_receipt_trace_sha256")
        != _canonical_document_sha256(trace, label="Phase B receipt trace")
        or trace.get("phase_a_trace_sha256") != phase_a.get("gate_receipt_trace_sha256")
        or trace.get("runtime_epoch") != phase_b.get("runtime_epoch")
        or phase_a.get("runtime_epoch") != phase_b.get("runtime_epoch")
        or phase_a.get("runtime_started_monotonic_ns")
        != phase_b.get("runtime_started_monotonic_ns")
        or trace.get("workload_sha256") != phase_b.get("workload_sha256")
    ):
        raise _safe_code("Phase B receipt trace binding was invalid")
    receipts = trace.get("receipts")
    samples = phase_b.get("samples")
    if not isinstance(receipts, list) or not receipts or not isinstance(samples, list):
        raise _safe_code("Phase B receipt trace or samples were unavailable")
    if phase_a_trace is not None:
        phase_a_receipts = phase_a_trace.get("receipts")
        if (
            not isinstance(phase_a_receipts, list)
            or not phase_a_receipts
            or trace.get("phase_a_last_sequence")
            != phase_a_receipts[-1].get("sequence")
            or trace.get("phase_a_trace_sha256")
            != _canonical_document_sha256(phase_a_trace, label="Phase A receipt trace")
        ):
            raise _safe_code("Phase B trace did not continue Phase A exactly")
    ended = phase_b.get("ended_monotonic_ns")
    started = phase_b.get("started_monotonic_ns")
    if not _exact_int(started, minimum=1) or not _exact_int(ended, minimum=1):
        raise _safe_code("Phase B interval was invalid")
    post_end = [
        index
        for index, receipt in enumerate(receipts)
        if receipt.get("observed_monotonic_ns", -1) > ended
    ]
    if len(post_end) != 1 or post_end[0] != len(receipts) - 1:
        raise _safe_code("Phase B trace lacked its first post-end sentinel")
    sentinel_index = post_end[0]
    if (
        sentinel_index == 0
        or receipts[sentinel_index - 1].get("observed_monotonic_ns", 2**63) > ended
    ):
        raise _safe_code("Phase B post-end sentinel was not the first one")

    workload = phase_b.get("workload_sha256")
    admissions = [
        index
        for index, receipt in enumerate(receipts[:sentinel_index])
        if receipt.get("workload_sha256") == workload and receipt.get("active") is True
    ]
    if not admissions:
        raise _safe_code("Phase B receipt trace lacked workload admission")
    admission_index = admissions[0]
    admission = receipts[admission_index]
    if not (
        phase_b.get("reset_completed_monotonic_ns")
        < admission.get("observed_monotonic_ns", 0)
        <= started
    ):
        raise _safe_code("Phase B workload admission time was invalid")
    phase_a_duration = phase_a.get("workload_identity", {}).get("total_duration_ms")
    phase_a_workload = phase_a.get("workload_sha256")
    for receipt in receipts[:admission_index]:
        if (
            receipt.get("workload_sha256") != phase_a_workload
            or receipt.get("active") is not False
            or receipt.get("chunk_uncommitted") is not False
            or receipt.get("active_cursor_ms") is not None
            or receipt.get("completed_cursor_ms") != phase_a_duration
        ):
            raise _safe_code("Phase B pre-admission receipt was not Phase-A idle")

    stable_keys = (
        "transition_observation_digest",
        "transition_sequence",
        "model_load_generation",
        "model_unload_generation",
        "completion_generation",
        "cuda_oom_generation",
        "media_failure_generation",
        "model_identity_sha256",
    )
    final_a = phase_a.get("events", [])[-1]
    for key in stable_keys:
        if admission.get(key) != final_a.get(key):
            raise _safe_code("Phase B admission did not continue Phase A runtime state")
    expected_model_identity = phase_b.get("model_identity_sha256")
    for receipt in receipts[admission_index:sentinel_index]:
        if (
            receipt.get("workload_sha256") != workload
            or receipt.get("active") is not True
            or receipt.get("priority_state") != "clear"
            or receipt.get("controller_phase") != "normal"
            or receipt.get("recovery_reason") is not None
            or receipt.get("admission_open") is not True
            or receipt.get("distinct_clear_count") != 3
            or receipt.get("model_resident") is not True
            or receipt.get("model_identity_sha256") != expected_model_identity
            or any(receipt.get(key) != admission.get(key) for key in stable_keys)
        ):
            raise _safe_code("Phase B receipt state was not continuously stable")

    if len(samples) != 181:
        raise _safe_code("Phase B sample count was invalid")
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise _safe_code("Phase B sample was not an object")
        receipt, digest = _receipt_at(
            receipts,
            sample.get("captured_monotonic_ns"),
            label=f"Phase B sample {index}",
        )
        if sample.get("gate_receipt_sha256") != digest:
            raise _safe_code("Phase B sample receipt hash did not bind latest state")
        for sample_key, receipt_key in _SAMPLE_RECEIPT_BINDINGS.items():
            if sample.get(sample_key) != receipt.get(receipt_key):
                raise _safe_code(f"Phase B sample receipt field {sample_key} disagreed")


def _read_source_bytes_independently(
    path: Path, *, maximum: int, label: str
) -> tuple[bytes, str]:
    """Read one exact regular file without invoking sampler-owned helpers."""
    if not path.is_absolute():
        raise ObserverBootstrapAbort(f"{label} path was not absolute")
    try:
        parent = path.parent.resolve(strict=True)
        parent_lstat = path.parent.lstat()
        path_lstat = path.lstat()
    except OSError as exc:
        raise ObserverBootstrapAbort(f"{label} was unavailable") from exc
    if (
        parent != path.parent.absolute()
        or stat.S_ISLNK(parent_lstat.st_mode)
        or stat.S_ISLNK(path_lstat.st_mode)
        or not stat.S_ISREG(path_lstat.st_mode)
    ):
        raise ObserverBootstrapAbort(f"{label} path was unsafe")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ObserverBootstrapAbort(f"{label} could not be opened") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum
        ):
            raise ObserverBootstrapAbort(f"{label} size or type was unsafe")
        remaining = maximum + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(MIB, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(payload) > maximum
            or len(payload) != before.st_size
            or any(
                getattr(after, field) != getattr(before, field)
                for field in identity_fields
            )
        ):
            raise ObserverBootstrapAbort(f"{label} changed while it was read")
        return payload, hashlib.sha256(payload).hexdigest()
    finally:
        os.close(descriptor)


def _bootstrap_verified_runtime(argv: list[str]) -> None:
    """Verify sampler bytes independently before executing the frozen module."""
    global health
    global _BOOTSTRAPPED_OBSERVER_SHA256
    global _BOOTSTRAPPED_SAMPLER_SHA256
    global _BOOTSTRAPPED_OBSERVER_PAYLOAD
    global _BOOTSTRAPPED_SAMPLER_PAYLOAD

    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--sampler-sha256")
    bootstrap.add_argument("--observer-sha256")
    preliminary, _unknown = bootstrap.parse_known_args(argv)
    if not isinstance(preliminary.sampler_sha256, str) or not SHA256_RE.fullmatch(
        preliminary.sampler_sha256
    ):
        raise ObserverBootstrapAbort("sampler checksum must be SHA256")
    if not isinstance(preliminary.observer_sha256, str) or not SHA256_RE.fullmatch(
        preliminary.observer_sha256
    ):
        raise ObserverBootstrapAbort("observer checksum must be SHA256")

    observer_path = Path(__file__).resolve(strict=True)
    observer_payload, observer_digest = _read_source_bytes_independently(
        observer_path,
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="runtime observer",
    )
    if observer_digest != preliminary.observer_sha256.lower():
        raise ObserverBootstrapAbort("runtime observer checksum mismatch")

    sampler_path = observer_path.with_name("gate_health_sampler.py")
    sampler_payload, sampler_digest = _read_source_bytes_independently(
        sampler_path,
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="frozen health sampler",
    )
    if sampler_digest != preliminary.sampler_sha256.lower():
        raise ObserverBootstrapAbort("frozen health sampler checksum mismatch")

    module_name = "_task11b_verified_gate_health_sampler"
    module = types.ModuleType(module_name)
    module.__file__ = str(sampler_path)
    module.__package__ = ""
    module.__loader__ = None
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        code = compile(sampler_payload, str(sampler_path), "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except BaseException:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    health = module
    _BOOTSTRAPPED_OBSERVER_SHA256 = observer_digest
    _BOOTSTRAPPED_SAMPLER_SHA256 = sampler_digest
    _BOOTSTRAPPED_OBSERVER_PAYLOAD = observer_payload
    _BOOTSTRAPPED_SAMPLER_PAYLOAD = sampler_payload


def _bootstrap_release_runtime(args: argparse.Namespace) -> dict[str, Any]:
    """Load only a sampler whose bytes match the committed release binding."""
    global health
    global _BOOTSTRAPPED_OBSERVER_SHA256
    global _BOOTSTRAPPED_SAMPLER_SHA256
    global _BOOTSTRAPPED_OBSERVER_PAYLOAD
    global _BOOTSTRAPPED_SAMPLER_PAYLOAD
    global _RELEASE_BINDING

    evidence_payload, _evidence_sha256 = _read_source_bytes_independently(
        args.evidence,
        maximum=MAX_OBSERVER_EVIDENCE_BYTES,
        label="release evidence",
    )
    binding = parse_sampler_binding(evidence_payload, args.binding_prefix)
    observer_path = Path(__file__).resolve(strict=True)
    sampler_path = args.sampler_source.resolve(strict=True)
    if sampler_path != observer_path.with_name("gate_health_sampler.py"):
        raise ObserverBootstrapAbort("release sampler was not adjacent to observer")
    sampler_test_path = args.sampler_test_source.resolve(strict=True)
    observer_test_path = args.observer_test_source.resolve(strict=True)
    if sampler_test_path != observer_path.with_name(
        "test_gate_health_sampler.py"
    ) or observer_test_path != observer_path.with_name("test_runtime_gate_observer.py"):
        raise ObserverBootstrapAbort("release test sources were not adjacent and exact")
    observer_payload, observer_sha256 = _read_source_bytes_independently(
        observer_path,
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="release observer",
    )
    sampler_payload, sampler_sha256 = _read_source_bytes_independently(
        sampler_path,
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="release sampler",
    )
    _sampler_test_payload, sampler_test_sha256 = _read_source_bytes_independently(
        sampler_test_path,
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="release sampler test",
    )
    _observer_test_payload, observer_test_sha256 = _read_source_bytes_independently(
        observer_test_path,
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="release observer test",
    )
    _producer_payload, producer_sha256 = _read_source_bytes_independently(
        args.producer_source,
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="priority producer",
    )
    if (
        observer_sha256 != binding["observer_sha256"]
        or sampler_sha256 != binding["sampler_sha256"]
        or sampler_test_sha256 != binding["test_sha256"]
        or observer_test_sha256 != binding["observer_test_sha256"]
        or producer_sha256 != binding["producer_sha256"]
    ):
        raise ObserverBootstrapAbort("release program source identity mismatch")
    module_name = "_task11b_release_gate_health_sampler"
    module = types.ModuleType(module_name)
    module.__file__ = str(sampler_path)
    module.__package__ = ""
    module.__loader__ = None
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        code = compile(sampler_payload, str(sampler_path), "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except BaseException:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    health = module
    _BOOTSTRAPPED_OBSERVER_SHA256 = observer_sha256
    _BOOTSTRAPPED_SAMPLER_SHA256 = sampler_sha256
    _BOOTSTRAPPED_OBSERVER_PAYLOAD = observer_payload
    _BOOTSTRAPPED_SAMPLER_PAYLOAD = sampler_payload
    _RELEASE_BINDING = binding
    return binding


def _verified_runtime_identities(args: argparse.Namespace) -> tuple[str, str]:
    observer_digest = _BOOTSTRAPPED_OBSERVER_SHA256
    sampler_digest = _BOOTSTRAPPED_SAMPLER_SHA256
    if (
        observer_digest is None
        or sampler_digest is None
        or observer_digest != args.observer_sha256.lower()
        or sampler_digest != args.sampler_sha256.lower()
    ):
        raise _safe_code("runtime observer was not checksum bootstrapped")
    return observer_digest, sampler_digest


def _verify_adjacent_frozen_sampler() -> None:
    expected = Path(__file__).with_name("gate_health_sampler.py").resolve(strict=True)
    actual = Path(health.__file__).resolve(strict=True)
    if actual != expected:
        raise _safe_code("runtime observer imported a non-adjacent health sampler")


def _read_all_fd(fd: int, maximum: int, *, label: str) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > maximum:
        raise _safe_code(f"{label} exceeded byte limit")
    return payload


def _require_private_file(path: Path, *, maximum: int, label: str) -> bytes:
    if not path.is_absolute():
        raise _safe_code(f"{label} path must be absolute")
    try:
        path_lstat = path.lstat()
        parent = path.parent.resolve(strict=True)
        parent_lstat = path.parent.lstat()
    except OSError as exc:
        raise _safe_code(f"{label} parent was unavailable") from exc
    if (
        parent != path.parent.absolute()
        or stat.S_ISLNK(parent_lstat.st_mode)
        or stat.S_ISLNK(path_lstat.st_mode)
    ):
        raise _safe_code(f"{label} parent used a symlink")
    owner = _owner_id()
    if owner is not None and (
        parent_lstat.st_uid != owner or parent_lstat.st_mode & 0o077
    ):
        raise _safe_code(f"{label} parent was not owner only")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _safe_code(f"{label} could not be opened") from exc
    try:
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or (owner is not None and (item.st_uid != owner or item.st_mode & 0o077))
            or item.st_nlink != 1
            or item.st_size > maximum
        ):
            raise _safe_code(f"{label} was not a private bounded regular file")
        return _read_all_fd(fd, maximum, label=label)
    finally:
        os.close(fd)


def _parse_runtime_identity(boundary: health.BoundaryExpectation) -> tuple[int, int]:
    user = boundary.document.get("user")
    if not isinstance(user, str) or not re.fullmatch(r"[1-9]\d*:[1-9]\d*", user):
        raise _safe_code("runtime fixture owner identity was unavailable")
    uid_text, gid_text = user.split(":", 1)
    uid, gid = int(uid_text), int(gid_text)
    if (uid, gid) != (1000, 1000):
        raise _safe_code("runtime fixture owner identity changed")
    return uid, gid


def _media_mount_source(boundary: health.BoundaryExpectation) -> Path:
    mounts = boundary.document.get("mounts")
    if not isinstance(mounts, list):
        raise _safe_code("runtime media mount was unavailable")
    matches = [
        item
        for item in mounts
        if isinstance(item, dict)
        and item.get("destination") == "/media"
        and item.get("read_write") is True
        and item.get("mode") == "rw"
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("source"), str):
        raise _safe_code("runtime media mount was not exact")
    return Path(matches[0]["source"])


def _validate_relative_path(value: Any, *, directory: str, subtitle: bool) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise _safe_code("fixture relative path was malformed")
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != directory:
        raise _safe_code("fixture relative path left its dedicated directory")
    if any(part in {"", ".", ".."} or len(part) > 255 for part in path.parts):
        raise _safe_code("fixture relative path was unsafe")
    normalized = posixpath.normpath(value)
    if normalized != value:
        raise _safe_code("fixture relative path was not normalized")
    if subtitle:
        if path.suffix.casefold() != ".srt":
            raise _safe_code("fixture subtitle path was not SRT")
    elif path.suffix.casefold() not in {".mkv", ".mp4", ".mov", ".m4v", ".webm"}:
        raise _safe_code("fixture media extension was not allowlisted")
    return value


def _fixture_item(raw: Any, *, role: str, directory: str, index: int) -> FixtureItem:
    expected = {"media"} if role in {"invalid", "silent"} else {"media", "subtitle"}
    if not isinstance(raw, dict) or set(raw) != expected:
        raise _safe_code("fixture item shape was not exact")
    media = _validate_relative_path(
        raw.get("media"), directory=directory, subtitle=False
    )
    subtitle_value = raw.get("subtitle")
    subtitle = (
        _validate_relative_path(subtitle_value, directory=directory, subtitle=True)
        if subtitle_value is not None
        else None
    )
    if subtitle == media:
        raise _safe_code("fixture media and subtitle paths collided")
    return FixtureItem(role, index, media, subtitle)


def load_fixture_manifest(
    path: Path,
    boundary: health.BoundaryExpectation,
) -> FixtureSet:
    payload = _require_private_file(
        path, maximum=MAX_FIXTURE_MANIFEST_BYTES, label="fixture manifest"
    )
    document = health.strict_json_object(
        payload, label="fixture manifest", max_bytes=MAX_FIXTURE_MANIFEST_BYTES
    )
    expected_keys = {"schema", "long", "short_resident", "reload", "invalid", "silent"}
    if set(document) != expected_keys or document.get("schema") != FIXTURE_SCHEMA:
        raise _safe_code("fixture manifest schema or keys were not exact")
    short_raw = document.get("short_resident")
    if not isinstance(short_raw, list) or not 2 <= len(short_raw) <= 8:
        raise _safe_code("resident short batch size was outside boundary")
    long_item = _fixture_item(document["long"], role="long", directory="long", index=0)
    short_items = tuple(
        _fixture_item(item, role="short", directory="short", index=index)
        for index, item in enumerate(short_raw)
    )
    reload_item = _fixture_item(
        document["reload"], role="reload", directory="reload", index=0
    )
    invalid_item = _fixture_item(
        document["invalid"], role="invalid", directory="invalid", index=0
    )
    silent_item = _fixture_item(
        document["silent"], role="silent", directory="silent", index=0
    )
    all_items = (long_item, *short_items, reload_item, invalid_item, silent_item)
    all_paths: list[str] = []
    for item in all_items:
        all_paths.append(item.media_relative)
        if item.subtitle_relative is not None:
            all_paths.append(item.subtitle_relative)
    if len(all_paths) != len(set(all_paths)):
        raise _safe_code("fixture manifest contained duplicate paths")
    media_root = _media_mount_source(boundary)
    uid, gid = _parse_runtime_identity(boundary)
    return FixtureSet(
        manifest_sha256=health.sha256_bytes(payload),
        media_root=media_root,
        runtime_uid=uid,
        runtime_gid=gid,
        long=long_item,
        short=short_items,
        reload=reload_item,
        invalid=invalid_item,
        silent=silent_item,
    )


def _open_fixture_directory(
    root: Path,
    directory: str,
    *,
    expected_uid: int,
    expected_gid: int,
) -> int | Path:
    if directory not in EXPECTED_FIXTURE_DIRECTORIES:
        raise _safe_code("fixture directory role was invalid")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        root_lstat = root.lstat()
        child_lstat = (root / directory).lstat()
    except OSError as exc:
        raise _safe_code("fixture directory was unavailable") from exc
    if stat.S_ISLNK(root_lstat.st_mode) or stat.S_ISLNK(child_lstat.st_mode):
        raise _safe_code("fixture directory used a symlink")
    owner_enforced = _owner_id() is not None
    if (
        not stat.S_ISDIR(child_lstat.st_mode)
        or (
            owner_enforced
            and (child_lstat.st_uid, child_lstat.st_gid) != (expected_uid, expected_gid)
        )
        or (owner_enforced and child_lstat.st_mode & 0o077)
    ):
        raise _safe_code("fixture directory ownership or mode was unsafe")
    if os.open not in os.supports_dir_fd:
        # The observer is Linux-only, but keeping the pure validation surface
        # portable lets its security regressions run on the local Windows test
        # workstation. Linux always takes the openat/O_NOFOLLOW branch below.
        return root / directory
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise _safe_code("fixture media root could not be opened") from exc
    try:
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode) or (
            root_stat.st_dev,
            root_stat.st_ino,
        ) != (root_lstat.st_dev, root_lstat.st_ino):
            raise _safe_code("fixture media root was not a directory")
        child_fd = os.open(directory, flags, dir_fd=root_fd)
    except BaseException:
        os.close(root_fd)
        raise
    os.close(root_fd)
    item = os.fstat(child_fd)
    if (
        not stat.S_ISDIR(item.st_mode)
        or (item.st_dev, item.st_ino) != (child_lstat.st_dev, child_lstat.st_ino)
        or item.st_dev != root_stat.st_dev
        or (
            owner_enforced
            and (item.st_uid, item.st_gid) != (expected_uid, expected_gid)
        )
        or (owner_enforced and item.st_mode & 0o077)
    ):
        os.close(child_fd)
        raise _safe_code("fixture directory ownership or mode was unsafe")
    return child_fd


def _srt_timestamp_milliseconds(match: re.Match[str], prefix: str) -> int:
    hour = int(match.group(prefix + "h"))
    minute = int(match.group(prefix + "m"))
    second = int(match.group(prefix + "s"))
    millisecond = int(match.group(prefix + "ms"))
    if minute >= 60 or second >= 60:
        raise _safe_code("subtitle output contained an invalid timestamp")
    return (((hour * 60) + minute) * 60 + second) * 1000 + millisecond


def validate_srt_payload(
    payload: bytes, *, expected_duration_seconds: float
) -> tuple[int, int, int]:
    """Validate every cue and require representative whole-media coverage."""
    if (
        isinstance(expected_duration_seconds, bool)
        or not isinstance(expected_duration_seconds, (int, float))
        or expected_duration_seconds <= 0
        or expected_duration_seconds > LONG_MAXIMUM_SECONDS
    ):
        raise _safe_code("subtitle duration boundary was invalid")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _safe_code("subtitle output was not UTF-8") from exc
    if "\x00" in text or "\r" in text.replace("\r\n", ""):
        raise _safe_code("subtitle output contained invalid control data")
    normalized = text.replace("\r\n", "\n").strip("\n")
    blocks = re.split(r"\n{2,}", normalized) if normalized else []
    if not blocks:
        raise _safe_code("subtitle output contained no cues")

    first_start: int | None = None
    last_start = -1
    last_end = -1
    for expected_index, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) < 3 or lines[0] != str(expected_index):
            raise _safe_code("subtitle cue sequence was malformed")
        timing = SRT_TIMING_RE.fullmatch(lines[1])
        if timing is None:
            raise _safe_code("subtitle cue timing was malformed")
        start_ms = _srt_timestamp_milliseconds(timing, "s")
        end_ms = _srt_timestamp_milliseconds(timing, "e")
        if start_ms < last_start or end_ms <= start_ms:
            raise _safe_code("subtitle cue timeline was malformed")
        if not any(line.strip() for line in lines[2:]) or any(
            any(ord(character) < 32 and character != "\t" for character in line)
            for line in lines[2:]
        ):
            raise _safe_code("subtitle cue text was malformed")
        if first_start is None:
            first_start = start_ms
        last_start = start_ms
        last_end = end_ms

    assert first_start is not None
    expected_ms = int(expected_duration_seconds * 1000)
    minimum_cues = max(1, (expected_ms + 300_000 - 1) // 300_000)
    permitted_overrun = max(5_000, expected_ms // 50)
    if (
        len(blocks) < minimum_cues
        or last_end > expected_ms + permitted_overrun
        or last_end - first_start < int(expected_ms * SRT_MINIMUM_TIMELINE_COVERAGE)
    ):
        raise _safe_code("subtitle output did not cover the bounded media timeline")
    return len(blocks), first_start, last_end


def snapshot_fixture_file(
    fixtures: FixtureSet,
    relative_path: str,
    *,
    maximum_bytes: int = MAX_MEDIA_BYTES,
    expected_subtitle: bool = False,
    expected_duration_seconds: float | None = None,
) -> FileSnapshot:
    directory, name = relative_path.split("/", 1)
    directory_fd = _open_fixture_directory(
        fixtures.media_root,
        directory,
        expected_uid=fixtures.runtime_uid,
        expected_gid=fixtures.runtime_gid,
    )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        try:
            if isinstance(directory_fd, int):
                path_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            else:
                path_stat = (directory_fd / name).lstat()
        except OSError as exc:
            raise _safe_code("fixture file could not be inspected") from exc
        if stat.S_ISLNK(path_stat.st_mode):
            raise _safe_code("fixture file used a symlink")
        try:
            if isinstance(directory_fd, int):
                fd = os.open(name, flags, dir_fd=directory_fd)
            else:
                fd = os.open(directory_fd / name, flags)
        except OSError as exc:
            raise _safe_code("fixture file could not be opened") from exc
        try:
            item = os.fstat(fd)
            owner_enforced = _owner_id() is not None
            if (
                not stat.S_ISREG(item.st_mode)
                or (item.st_dev, item.st_ino) != (path_stat.st_dev, path_stat.st_ino)
                or item.st_nlink != 1
                or item.st_size <= 0
                or item.st_size > maximum_bytes
                or (
                    owner_enforced
                    and (item.st_uid, item.st_gid)
                    != (fixtures.runtime_uid, fixtures.runtime_gid)
                )
                or (owner_enforced and item.st_mode & 0o022)
            ):
                raise _safe_code("fixture file identity size or mode was unsafe")
            digest = hashlib.sha256()
            subtitle_chunks: list[bytes] | None = [] if expected_subtitle else None
            read_bytes = 0
            while True:
                chunk = os.read(fd, min(1024 * 1024, maximum_bytes + 1 - read_bytes))
                if not chunk:
                    break
                read_bytes += len(chunk)
                if read_bytes > maximum_bytes:
                    raise _safe_code(
                        "subtitle output exceeded byte limit"
                        if expected_subtitle
                        else "fixture media exceeded byte limit"
                    )
                digest.update(chunk)
                if subtitle_chunks is not None:
                    subtitle_chunks.append(chunk)
            after = os.fstat(fd)
            identity_fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_gid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if read_bytes != item.st_size or any(
                getattr(after, field) != getattr(item, field)
                for field in identity_fields
            ):
                raise _safe_code("fixture file changed while it was hashed")
            subtitle_metrics: tuple[int, int, int] | None = None
            if expected_subtitle:
                subtitle_payload = b"".join(subtitle_chunks or ())
                if (
                    len(subtitle_payload) > MAX_SUBTITLE_BYTES
                    or expected_duration_seconds is None
                ):
                    raise _safe_code("subtitle output was malformed or unbounded")
                subtitle_metrics = validate_srt_payload(
                    subtitle_payload,
                    expected_duration_seconds=expected_duration_seconds,
                )
            return FileSnapshot(
                device=item.st_dev,
                inode=item.st_ino,
                mode=stat.S_IMODE(item.st_mode),
                uid=item.st_uid,
                gid=item.st_gid,
                links=item.st_nlink,
                size=item.st_size,
                mtime_ns=item.st_mtime_ns,
                ctime_ns=item.st_ctime_ns,
                sha256=digest.hexdigest(),
                subtitle_cue_count=(subtitle_metrics or (None, None, None))[0],
                subtitle_first_start_ms=(subtitle_metrics or (None, None, None))[1],
                subtitle_last_end_ms=(subtitle_metrics or (None, None, None))[2],
            )
        finally:
            os.close(fd)
    finally:
        if isinstance(directory_fd, int):
            os.close(directory_fd)


def _directory_names(fixtures: FixtureSet, directory: str) -> set[str]:
    fd = _open_fixture_directory(
        fixtures.media_root,
        directory,
        expected_uid=fixtures.runtime_uid,
        expected_gid=fixtures.runtime_gid,
    )
    try:
        names = os.listdir(fd)
    finally:
        if isinstance(fd, int):
            os.close(fd)
    if any(
        not isinstance(name, str) or not name or "/" in name or "\\" in name
        for name in names
    ):
        raise _safe_code("fixture directory contained a malformed entry")
    return set(names)


def assert_fixture_directory_exact(
    fixtures: FixtureSet,
    directory: str,
    *,
    allow_outputs: bool,
) -> None:
    items = [
        item
        for item in fixtures.all_items
        if item.media_relative.split("/", 1)[0] == directory
    ]
    allowed = {item.media_relative.split("/", 1)[1] for item in items}
    if allow_outputs:
        allowed.update(
            item.subtitle_relative.split("/", 1)[1]
            for item in items
            if item.subtitle_relative is not None
        )
    observed = _directory_names(fixtures, directory)
    if observed != allowed:
        raise _safe_code("fixture directory contained partial or undeclared files")


def assert_outputs_absent(fixtures: FixtureSet) -> None:
    for directory in sorted(EXPECTED_FIXTURE_DIRECTORIES):
        assert_fixture_directory_exact(fixtures, directory, allow_outputs=False)


class AtomicPublicationLedger:
    """Fail-closed state machine fed by a continuous kernel event stream."""

    MUTATING_MASK = 0x00000002 | 0x00000004 | 0x00000008 | 0x00000040
    MUTATING_MASK |= 0x00000080 | 0x00000100 | 0x00000200
    IN_CLOSE_WRITE = 0x00000008
    IN_MOVED_FROM = 0x00000040
    IN_MOVED_TO = 0x00000080
    IN_CREATE = 0x00000100
    IN_DELETE = 0x00000200
    IN_Q_OVERFLOW = 0x00004000
    IN_IGNORED = 0x00008000
    DIRECTORY_INVALIDATED_MASK = 0x00000400 | 0x00000800
    EVENT_INVALIDATED_MASK = DIRECTORY_INVALIDATED_MASK | IN_IGNORED
    IN_ISDIR = 0x40000000

    def __init__(self, fixtures: FixtureSet) -> None:
        self.media_names: dict[str, set[str]] = {
            directory: set() for directory in EXPECTED_FIXTURE_DIRECTORIES
        }
        self.output_names: dict[str, set[str]] = {
            directory: set() for directory in EXPECTED_FIXTURE_DIRECTORIES
        }
        self.temp_patterns: dict[str, list[tuple[re.Pattern[str], str]]] = {
            directory: [] for directory in EXPECTED_FIXTURE_DIRECTORIES
        }
        self.expected_publications: set[str] = set()
        for item in fixtures.all_items:
            directory, media_name = item.media_relative.split("/", 1)
            self.media_names[directory].add(media_name)
            if item.subtitle_relative is None:
                continue
            output_directory, output_name = item.subtitle_relative.split("/", 1)
            if output_directory != directory:
                raise _safe_code("fixture output directory binding changed")
            self.output_names[directory].add(output_name)
            self.expected_publications.add(item.subtitle_relative)
            suffix = PurePosixPath(output_name).suffix
            pattern = re.compile(
                r"^\."
                + re.escape(output_name)
                + r"\.[A-Za-z0-9_-]{6,64}\.tmp"
                + re.escape(suffix)
                + r"$"
            )
            self.temp_patterns[directory].append((pattern, item.subtitle_relative))
        self.active_temporaries: dict[str, str] = {}
        self.rename_cookies: dict[int, str] = {}
        self.published: set[str] = set()
        self.publication_events: list[str] = []

    def _temporary_target(self, directory: str, name: str) -> str | None:
        for pattern, target in self.temp_patterns[directory]:
            if pattern.fullmatch(name):
                return target
        return None

    def observe(self, directory: str, name: str, mask: int, cookie: int) -> None:
        if directory not in EXPECTED_FIXTURE_DIRECTORIES:
            raise _safe_code("atomic watcher directory identity was invalid")
        if mask & self.IN_Q_OVERFLOW:
            raise _safe_code("atomic watcher kernel queue overflowed")
        if mask & self.EVENT_INVALIDATED_MASK or mask & self.IN_ISDIR:
            raise _safe_code("atomic watcher directory boundary changed")
        if not name or len(name) > 255 or "/" in name or "\\" in name:
            raise _safe_code("atomic watcher filename was malformed")
        if not mask & self.MUTATING_MASK:
            return
        relative = f"{directory}/{name}"
        if name in self.media_names[directory]:
            raise _safe_code("original fixture emitted a mutation event")
        if name in self.output_names[directory]:
            if mask != self.IN_MOVED_TO or cookie <= 0:
                raise _safe_code("subtitle final was exposed non-atomically")
            expected = self.rename_cookies.pop(cookie, None)
            if expected != relative or relative in self.published:
                raise _safe_code("subtitle atomic rename pairing was invalid")
            self.published.add(relative)
            self.publication_events.append(relative)
            return
        target = self._temporary_target(directory, name)
        if target is None:
            raise _safe_code("fixture directory emitted an undeclared mutation")
        if mask & self.IN_CREATE:
            if relative in self.active_temporaries:
                raise _safe_code("subtitle staging identity was duplicated")
            self.active_temporaries[relative] = target
        unsupported = mask & ~(
            self.IN_CREATE
            | self.IN_CLOSE_WRITE
            | self.IN_MOVED_FROM
            | self.IN_DELETE
            | 0x00000002
            | 0x00000004
        )
        if unsupported:
            raise _safe_code("subtitle staging event was unexpected")
        if mask & self.IN_MOVED_FROM:
            if cookie <= 0 or self.active_temporaries.pop(relative, None) != target:
                raise _safe_code("subtitle staging rename was unpaired")
            if cookie in self.rename_cookies:
                raise _safe_code("subtitle rename cookie was duplicated")
            self.rename_cookies[cookie] = target
        if mask & self.IN_DELETE:
            self.active_temporaries.pop(relative, None)

    def mark(self) -> int:
        return len(self.publication_events)

    def assert_published(
        self, items: Iterable[FixtureItem], *, after_mark: int = 0
    ) -> None:
        if (
            isinstance(after_mark, bool)
            or not isinstance(after_mark, int)
            or not 0 <= after_mark <= len(self.publication_events)
        ):
            raise _safe_code("atomic publication freshness mark was invalid")
        expected = {
            item.subtitle_relative
            for item in items
            if item.subtitle_relative is not None
        }
        fresh = set(self.publication_events[after_mark:])
        if not expected or not expected.issubset(fresh):
            raise _safe_code("subtitle atomic publication was not observed")

    def assert_complete(self) -> None:
        if self.published != self.expected_publications:
            raise _safe_code("atomic publication set was incomplete")
        if self.active_temporaries or self.rename_cookies:
            raise _safe_code("atomic publication state was left incomplete")


class AtomicOutputWatcher:
    """Linux inotify queue; events remain continuous between bounded drains."""

    EVENT_HEADER = struct.Struct("iIII")
    WATCH_MASK = AtomicPublicationLedger.MUTATING_MASK
    WATCH_MASK |= AtomicPublicationLedger.DIRECTORY_INVALIDATED_MASK

    def __init__(self, fixtures: FixtureSet) -> None:
        if sys.platform != "linux":
            raise _safe_code("atomic output watcher requires Linux inotify")
        self.fixtures = fixtures
        self.ledger = AtomicPublicationLedger(fixtures)
        self._lock = threading.Lock()
        self._closed = False
        library = ctypes.CDLL(None, use_errno=True)
        try:
            initialize = library.inotify_init1
            add_watch = library.inotify_add_watch
        except AttributeError as exc:
            raise _safe_code("atomic output watcher was unavailable") from exc
        initialize.argtypes = [ctypes.c_int]
        initialize.restype = ctypes.c_int
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int
        descriptor = initialize(os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        if descriptor < 0:
            raise _safe_code("atomic output watcher could not be initialized")
        self._descriptor = descriptor
        self._watch_directories: dict[int, str] = {}
        try:
            for directory in sorted(EXPECTED_FIXTURE_DIRECTORIES):
                path = fixtures.media_root / directory
                before = path.lstat()
                watch = add_watch(
                    descriptor,
                    os.fsencode(path),
                    ctypes.c_uint32(self.WATCH_MASK),
                )
                after = path.lstat()
                if (
                    watch < 0
                    or stat.S_ISLNK(after.st_mode)
                    or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or watch in self._watch_directories
                ):
                    raise _safe_code("atomic output watch binding failed")
                self._watch_directories[watch] = directory
        except BaseException:
            os.close(descriptor)
            self._closed = True
            raise

    def _drain_locked(self) -> None:
        if self._closed:
            raise _safe_code("atomic output watcher was already closed")
        while True:
            try:
                payload = os.read(self._descriptor, MAX_EVENT_LOG_BYTES)
            except BlockingIOError:
                return
            except OSError as exc:
                raise _safe_code("atomic output event read failed") from exc
            if not payload:
                raise _safe_code("atomic output event stream closed")
            offset = 0
            while offset < len(payload):
                if len(payload) - offset < self.EVENT_HEADER.size:
                    raise _safe_code("atomic output event was truncated")
                watch, mask, cookie, name_length = self.EVENT_HEADER.unpack_from(
                    payload, offset
                )
                offset += self.EVENT_HEADER.size
                if name_length > 4096 or offset + name_length > len(payload):
                    raise _safe_code("atomic output event length was invalid")
                raw_name = payload[offset : offset + name_length]
                offset += name_length
                try:
                    name = raw_name.rstrip(b"\x00").decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise _safe_code("atomic output filename was not UTF-8") from exc
                if mask & AtomicPublicationLedger.IN_Q_OVERFLOW:
                    self.ledger.observe("long", "overflow", mask, cookie)
                    continue
                directory = self._watch_directories.get(watch)
                if directory is None:
                    raise _safe_code("atomic output watch identity changed")
                self.ledger.observe(directory, name, mask, cookie)

    def drain(self) -> None:
        with self._lock:
            self._drain_locked()

    def assert_published(
        self, items: Iterable[FixtureItem], *, after_mark: int = 0
    ) -> None:
        with self._lock:
            self._drain_locked()
            self.ledger.assert_published(items, after_mark=after_mark)

    def mark(self) -> int:
        with self._lock:
            self._drain_locked()
            return self.ledger.mark()

    def assert_complete(self) -> None:
        with self._lock:
            self._drain_locked()
            self.ledger.assert_complete()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                os.close(self._descriptor)
                self._closed = True


def capture_original_snapshots(fixtures: FixtureSet) -> dict[str, FileSnapshot]:
    return {
        item.media_relative: snapshot_fixture_file(fixtures, item.media_relative)
        for item in fixtures.all_items
    }


def attest_originals_unchanged(
    fixtures: FixtureSet,
    originals: dict[str, FileSnapshot],
) -> None:
    if set(originals) != {item.media_relative for item in fixtures.all_items}:
        raise _safe_code("original fixture snapshot set changed")
    for relative, before in originals.items():
        if snapshot_fixture_file(fixtures, relative) != before:
            raise _safe_code("original fixture generation or content changed")


def _probe_duration(path: Path) -> float:
    result = health.bounded_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        label="fixture duration probe",
        timeout=30,
        max_bytes=64 * 1024,
    )
    document = health.strict_json_object(
        result.output.encode("utf-8"), label="fixture duration", max_bytes=64 * 1024
    )
    format_item = document.get("format")
    if not isinstance(format_item, dict) or set(format_item) != {"duration"}:
        raise _safe_code("fixture duration response was malformed")
    return health.finite_number(
        format_item.get("duration"), "fixture duration", positive=True
    )


def validate_fixture_durations(fixtures: FixtureSet) -> dict[str, Any]:
    long_duration = _probe_duration(fixtures.media_root / fixtures.long.media_relative)
    if not LONG_MINIMUM_SECONDS <= long_duration <= LONG_MAXIMUM_SECONDS:
        raise _safe_code("long fixture was not a bounded 31 minute workload")
    short_durations = [
        _probe_duration(fixtures.media_root / item.media_relative)
        for item in fixtures.short
    ]
    reload_duration = _probe_duration(
        fixtures.media_root / fixtures.reload.media_relative
    )
    if any(
        value > SHORT_MAXIMUM_SECONDS for value in (*short_durations, reload_duration)
    ):
        raise _safe_code("short or reload fixture exceeded duration boundary")
    return {
        "long_seconds": round(long_duration, 3),
        "short_count": len(short_durations),
        "short_max_seconds": round(max(short_durations), 3),
        "reload_seconds": round(reload_duration, 3),
    }


def _read_api_key(path: Path | None) -> str:
    if path is None:
        raise _safe_code("API key file was required for request isolation")
    payload = _require_private_file(path, maximum=MAX_API_KEY_BYTES, label="API key")
    try:
        value = payload.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise _safe_code("API key was not ASCII") from exc
    if not 16 <= len(value) <= 512 or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        raise _safe_code("API key format was invalid")
    return value


def validate_request_isolation(item: dict[str, Any], api_key: str) -> None:
    """Bind the private observer credential to the exact candidate API gate."""
    if (
        not isinstance(api_key, str)
        or not 16 <= len(api_key) <= 512
        or any(ord(character) < 33 or ord(character) > 126 for character in api_key)
    ):
        raise _safe_code("runtime request isolation credential was invalid")
    _normalized, environment = health._normalized_environment(item)
    configured = environment.get("SUBGEN_API_KEY")
    if (
        not isinstance(configured, str)
        or not 16 <= len(configured) <= 512
        or any(ord(character) < 33 or ord(character) > 126 for character in configured)
        or not hmac.compare_digest(configured, api_key)
    ):
        raise _safe_code("runtime request isolation binding was not exact")


def post_batch(directory: str, *, api_key: str, timeout: float) -> None:
    if not re.fullmatch(
        r"(?:/media/(?:long|short|reload|invalid|silent)|"
        r"/fixtures/phase-(?:a|b)/[A-Za-z0-9_.-]+)",
        directory,
    ):
        raise _safe_code("batch directory left the disposable fixture allowlist")
    if (
        not isinstance(api_key, str)
        or not 16 <= len(api_key) <= 512
        or any(ord(character) < 33 or ord(character) > 126 for character in api_key)
    ):
        raise _safe_code("batch request isolation credential was invalid")
    query = urllib.parse.urlencode({"directory": directory, "forceLanguage": "en"})
    request_path = "/batch?" + query
    headers = {"Content-Length": "0", "X-Subgen-Api-Key": api_key}
    connection = http.client.HTTPConnection("127.0.0.1", 19000, timeout=timeout)
    try:
        connection.request("POST", request_path, body=b"", headers=headers)
        response = connection.getresponse()
        body = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
        if len(body) > MAX_HTTP_RESPONSE_BYTES:
            raise _safe_code("batch response exceeded byte limit")
        if response.status != 200:
            raise _safe_code("batch request did not return exact success")
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise _safe_code("batch request was unavailable or timed out") from exc
    finally:
        connection.close()


class RuntimeEventScanner:
    """Bounded incremental parser that retains only expected event identities."""

    def __init__(
        self,
        client: health.DockerClient,
        binding: health.CandidateBinding,
        started_wall: float,
        expected_paths: Iterable[str],
    ) -> None:
        self.client = client
        self.binding = binding
        self.cursor_wall = started_wall
        self.expected_paths = frozenset(expected_paths)
        if not self.expected_paths or any(
            not isinstance(path, str)
            or len(path) > 1024
            or not re.fullmatch(
                r"/media/(?:long|short|reload|invalid|silent)/[^\s/]+", path
            )
            for path in self.expected_paths
        ):
            raise _safe_code("workload event path allowlist was malformed")
        self._line_digests: set[str] = set()
        self._retained_event_bytes = 0
        self._observation_sequence = 0
        self.events: list[dict[str, Any]] = []
        self._event_sequences: list[int] = []
        self.silent_paths: set[str] = set()
        self._silent_events: list[tuple[int, str]] = []
        self.unload_count = 0

    def _retain_machine_event(self, event: dict[str, Any]) -> None:
        path = event.get("path")
        name = event.get("event")
        if not isinstance(path, str) or not isinstance(name, str):
            raise _safe_code("candidate machine event identity was incomplete")
        retained_names = {
            "worker_start",
            "worker_finish",
            "worker_error",
            "media_validation_failed",
        }
        if name in retained_names and path not in self.expected_paths:
            raise _safe_code("candidate workload event left the path allowlist")
        if path not in self.expected_paths:
            return
        if name in {"worker_start", "worker_finish", "worker_error"}:
            task_id = event.get("task_id")
            task_type = event.get("task_type")
            source_identity = event.get("source_identity")
            expected_task_id = hashlib.sha256(
                f"transcribe:{path}".encode("utf-8")
            ).hexdigest()[:16]
            if (
                task_id != expected_task_id
                or task_type != "transcribe"
                or not isinstance(source_identity, list)
                or len(source_identity) != 5
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in source_identity
                )
            ):
                raise _safe_code("candidate worker event identity was malformed")
            retained = {
                "event": name,
                "task_id": task_id,
                "task_type": task_type,
                "path": path,
                "source_identity": list(source_identity),
            }
        elif name == "media_validation_failed":
            failure_class = event.get("failure_class")
            outcomes = event.get("validator_outcomes")
            if (
                not isinstance(failure_class, str)
                or len(failure_class) > 64
                or not isinstance(outcomes, dict)
                or set(outcomes) != {"ffprobe", "pyav"}
                or any(
                    not isinstance(value, str) or len(value) > 64
                    for value in outcomes.values()
                )
            ):
                raise _safe_code("candidate validation event was malformed")
            retained = {
                "event": name,
                "path": path,
                "failure_class": failure_class,
                "validator_outcomes": {
                    "ffprobe": outcomes["ffprobe"],
                    "pyav": outcomes["pyav"],
                },
            }
        else:
            return
        encoded = json.dumps(retained, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        if (
            len(self.events) >= MAX_EVENT_IDENTITIES
            or self._retained_event_bytes + len(encoded) > MAX_RETAINED_EVENT_BYTES
        ):
            raise _safe_code("retained workload event budget was exceeded")
        self._retained_event_bytes += len(encoded)
        self._observation_sequence += 1
        self.events.append(retained)
        self._event_sequences.append(self._observation_sequence)

    def parse_lines(self, payload: str) -> None:
        for line in payload.splitlines():
            if len(line.encode("utf-8", errors="replace")) > MAX_EVENT_LOG_BYTES:
                raise _safe_code("candidate workload event line exceeded byte limit")
            digest = health.sha256_bytes(line.encode("utf-8", errors="replace"))
            if digest in self._line_digests:
                continue
            self._line_digests.add(digest)
            if len(self._line_digests) > MAX_EVENT_IDENTITIES:
                raise _safe_code("workload event identity bound was exceeded")
            if "Model unloaded from memory" in line:
                self.unload_count += 1
            if SUBGEN_EVENT_PREFIX in line:
                raw = line.split(SUBGEN_EVENT_PREFIX, 1)[1]
                try:
                    event = json.loads(
                        raw, object_pairs_hook=health._reject_duplicate_object
                    )
                except (json.JSONDecodeError, health.GateAbort) as exc:
                    raise _safe_code("candidate machine event was malformed") from exc
                if not isinstance(event, dict):
                    raise _safe_code("candidate machine event was not an object")
                self._retain_machine_event(event)
            silent = SILENT_EVENT_RE.search(line)
            if silent and silent.group("path") in self.expected_paths:
                path = silent.group("path")
                self._observation_sequence += 1
                self.silent_paths.add(path)
                self._silent_events.append((self._observation_sequence, path))

    def mark(self) -> int:
        return self._observation_sequence

    def _validate_mark(self, after_mark: int) -> None:
        if (
            isinstance(after_mark, bool)
            or not isinstance(after_mark, int)
            or not 0 <= after_mark <= self._observation_sequence
        ):
            raise _safe_code("workload event freshness mark was invalid")

    def events_after(self, after_mark: int) -> list[dict[str, Any]]:
        self._validate_mark(after_mark)
        return [
            event
            for sequence, event in zip(self._event_sequences, self.events, strict=True)
            if sequence > after_mark
        ]

    def silent_after(self, after_mark: int, expected_path: str) -> bool:
        self._validate_mark(after_mark)
        if expected_path not in self.expected_paths:
            raise _safe_code("silent event path left the workload allowlist")
        return any(
            sequence > after_mark and path == expected_path
            for sequence, path in self._silent_events
        )

    def scan(self, until_wall: float | None = None) -> None:
        end = time.time() if until_wall is None else until_wall
        since = max(0.0, self.cursor_wall - health.LOG_OVERLAP_SECONDS)
        if end < since:
            raise _safe_code("workload event clock moved backwards")
        result = self.client.command(
            "logs",
            "--timestamps",
            "--since",
            f"{since:.6f}",
            "--until",
            f"{end:.6f}",
            self.binding.container_id,
            label="candidate workload event logs",
            timeout=10,
            max_bytes=MAX_EVENT_LOG_BYTES,
        )
        self.parse_lines(result.output)
        self.cursor_wall = end

    def matching_events(
        self,
        event_name: str,
        expected_paths: Iterable[str],
        *,
        after_mark: int = 0,
    ) -> list[dict[str, Any]]:
        paths = set(expected_paths)
        return [
            event
            for event in self.events_after(after_mark)
            if event.get("event") == event_name and event.get("path") in paths
        ]

    def assert_no_worker_errors(
        self, expected_paths: Iterable[str], *, after_mark: int = 0
    ) -> None:
        if self.matching_events("worker_error", expected_paths, after_mark=after_mark):
            raise _safe_code("candidate workload emitted a worker error")


class RuntimeCycleTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._observations: list[RuntimeStatusObservation] = []

    def mark(self) -> int:
        with self._lock:
            return len(self._observations)

    def observe(self, resource: dict[str, Any], now_monotonic: float) -> None:
        state = resource.get("controller_state")
        reason = resource.get("recovery_reason")
        admission = resource.get("admission_open")
        if not isinstance(state, str) or not isinstance(admission, bool):
            raise _safe_code("runtime state observation was malformed")
        with self._lock:
            self._observations.append(
                RuntimeStatusObservation(
                    sequence=len(self._observations) + 1,
                    observed_monotonic=now_monotonic,
                    state=state,
                    recovery_reason=reason,
                    admission_open=admission,
                )
            )

    def idle_recovery_proof(self, mark: int) -> RuntimeRecoveryProof | None:
        with self._lock:
            observations = list(self._observations[mark:])
        first_index = next(
            (
                index
                for index, item in enumerate(observations)
                if item.state == "recovering"
                and item.recovery_reason == "idle_cleanup"
                and item.admission_open is False
            ),
            None,
        )
        if first_index is None:
            return None
        recovery = observations[first_index]
        for final in observations[first_index + 1 :]:
            if (
                final.state == "normal"
                and final.recovery_reason is None
                and final.admission_open
            ):
                relevant = [
                    item
                    for item in observations[first_index + 1 :]
                    if item.sequence <= final.sequence
                ]
                elapsed = final.observed_monotonic - recovery.observed_monotonic
                if len(relevant) >= 3 and elapsed >= MIN_RECOVERY_SPAN_SECONDS:
                    return RuntimeRecoveryProof(
                        recovering_sequence=recovery.sequence,
                        normal_sequence=final.sequence,
                        complete_health_polls=len(relevant),
                        elapsed_seconds=elapsed,
                    )
                raise _safe_code(
                    "runtime reopened before three complete recovery health polls"
                )
        return None

    def latest_is_healthy(self) -> bool:
        with self._lock:
            if not self._observations:
                return False
            latest = self._observations[-1]
        return (
            latest.state == "normal"
            and latest.recovery_reason is None
            and latest.admission_open is True
        )


def validate_runtime_status(
    payload: dict[str, Any],
    *,
    expected_model: str,
    expected_reserve_bytes: int,
    observed_gpu_total_bytes: int,
    expected_priority_state: str = "clear",
    expected_policy_sha256: str | None = None,
    expected_controller_phase: str = "normal",
    expected_recovery_reason: str | None = None,
    expected_admission_open: bool = True,
    expected_model_resident: bool | None = True,
    require_gate_runtime: bool = True,
) -> dict[str, Any]:
    """Validate one atomic status without rewriting away causal transitions."""
    return health.validate_candidate_status(
        payload,
        expected_model=expected_model,
        expected_reserve_bytes=expected_reserve_bytes,
        observed_gpu_total_bytes=observed_gpu_total_bytes,
        expected_priority_state=expected_priority_state,
        expected_policy_sha256=expected_policy_sha256,
        expected_controller_phase=expected_controller_phase,
        expected_recovery_reason=expected_recovery_reason,
        expected_admission_open=expected_admission_open,
        expected_model_resident=expected_model_resident,
        require_gate_runtime=require_gate_runtime,
    )


def validate_runtime_chunk_policy(
    item: dict[str, Any], *, expected_chunk_minutes: int
) -> dict[str, Any]:
    """Bind the explicit gate chunk policy from the exact candidate config."""
    if (
        isinstance(expected_chunk_minutes, bool)
        or not isinstance(expected_chunk_minutes, int)
        or not 5 <= expected_chunk_minutes <= 30
    ):
        raise _safe_code("expected runtime chunk policy was outside boundary")
    _normalized, environment = health._normalized_environment(item)
    if environment.get("SEGMENTATION_ENABLED", "").casefold() != "true":
        raise _safe_code("runtime segmentation was not explicitly enabled")
    if environment.get("SEGMENTATION_CHUNK_MINUTES") != str(expected_chunk_minutes):
        raise _safe_code("runtime chunk policy did not match gate expectation")
    isolation = {
        "SKIP_STARTUP_SCAN": "True",
        "MONITOR": "False",
        "PROCESS_ADDED_MEDIA": "False",
        "PROCESS_MEDIA_ON_PLAY": "False",
    }
    if any(environment.get(key) != expected for key, expected in isolation.items()):
        raise _safe_code("runtime startup and event isolation controls were not exact")
    if {"PROCADDEDMEDIA", "PROCMEDIAONPLAY"}.intersection(environment):
        raise _safe_code("runtime event isolation aliases were present")
    return {
        "segmentation_enabled": True,
        "chunk_minutes": expected_chunk_minutes,
        "skip_startup_scan": True,
        "monitor": False,
        "process_added_media": False,
        "process_media_on_play": False,
    }


def _contains_forbidden(value: Any, forbidden: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(secret and secret in value for secret in forbidden)
    if isinstance(value, dict):
        return any(_contains_forbidden(item, forbidden) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden(item, forbidden) for item in value)
    return False


class LockedEvidence:
    """Serialize main/health-thread writes and reject privacy-boundary leaks."""

    def __init__(self, writer: health.EvidenceWriter, forbidden: Iterable[str]) -> None:
        self.writer = writer
        self.forbidden = tuple(item for item in forbidden if item)
        self.lock = threading.Lock()
        self.approximate_bytes = 0

    @property
    def closed(self) -> bool:
        return self.writer.closed

    def write(self, record: dict[str, Any]) -> None:
        if _contains_forbidden(record, self.forbidden):
            raise _safe_code("observer evidence crossed the privacy boundary")
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        with self.lock:
            if self.approximate_bytes + len(encoded) + 1 > MAX_OBSERVER_EVIDENCE_BYTES:
                raise _safe_code("observer evidence exceeded byte budget")
            self.writer.write(record)
            self.approximate_bytes += len(encoded) + 1

    def seal(
        self,
        *,
        outcome: str,
        sampler_sha256: str,
        image_config: str,
        cleanup: dict[str, Any],
    ) -> str:
        with self.lock:
            return self.writer.seal(
                outcome=outcome,
                sampler_sha256=sampler_sha256,
                image_config=image_config,
                cleanup=cleanup,
            )

    def close(self) -> None:
        with self.lock:
            self.writer.close()


class HealthMonitor:
    def __init__(
        self,
        *,
        args: argparse.Namespace,
        evidence: LockedEvidence,
        client: health.DockerClient,
        candidate: health.CandidateBinding,
        frigate: health.ObservedBinding,
        expectations: dict[str, float],
        baselines: HealthBaselines,
        logs: health.IncrementalLogScanner,
        tracker: RuntimeCycleTracker,
        output_watcher: AtomicOutputWatcher,
        started_monotonic: float,
    ) -> None:
        self.args = args
        self.evidence = evidence
        self.client = client
        self.candidate = candidate
        self.frigate = frigate
        self.expectations = expectations
        self.baselines = baselines
        self.logs = logs
        self.tracker = tracker
        self.output_watcher = output_watcher
        self.started_monotonic = started_monotonic
        self.low_since = {name: None for name in expectations}
        self._allow_idle_recovery = False
        self._phase_lock = threading.Lock()
        self._stop = threading.Event()
        self._duration_reached = threading.Event()
        self._failure = threading.Event()
        self._failure_exception: BaseException | None = None
        self._thread: threading.Thread | None = None
        self.sample_count = 0
        self.last_elapsed = 0.0

    def set_phase(self, *, allow_idle_recovery: bool) -> None:
        with self._phase_lock:
            self._allow_idle_recovery = allow_idle_recovery

    def _phase_allows_recovery(self) -> bool:
        with self._phase_lock:
            return self._allow_idle_recovery

    def start(self) -> None:
        if self._thread is not None:
            raise _safe_code("health monitor was already started")
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="subgen-task11b-runtime-health",
        )
        self._thread.start()

    def _state_and_memory(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        health.verify_bound_docker_daemon(self.client, self.args)
        candidate = health.candidate_state(self.client, self.candidate, self.args)
        frigate = health.observed_state(self.client, self.frigate)
        if candidate["running"] is not True or candidate["status"] != "running":
            raise _safe_code("candidate stopped during composite workload")
        if candidate["oom_killed"] is not False:
            raise _safe_code("candidate was OOM killed during composite workload")
        if candidate["restart_count"] != self.baselines.candidate_restart_count:
            raise _safe_code("candidate restart count increased")
        if frigate["running"] is not True or frigate["health"] != "healthy":
            raise _safe_code("Frigate lost healthy state")
        if frigate["restart_count"] != self.baselines.frigate_restart_count:
            raise _safe_code("Frigate restart count increased")
        memory = health.candidate_memory(self.client, self.candidate)
        health.validate_candidate_memory_snapshot(
            memory, expected_memory_bytes=self.args.expected_memory_bytes
        )
        return candidate, frigate, memory

    def collect(self, *, final: bool = False) -> None:
        self.output_watcher.drain()
        now_monotonic = time.monotonic()
        now_wall = time.time()
        candidate, frigate, memory = self._state_and_memory()
        host_available = health.read_mem_available_bytes()
        if host_available < self.args.host_reserve_bytes:
            raise _safe_code("host memory reserve breached")
        health.read_host_pressure()
        gpu = health.gpu_telemetry()
        if gpu["free_mib"] * health.MIB < self.args.gpu_free_floor_bytes:
            raise _safe_code("GPU priority reserve breached")
        ollama = health.fetch_json(self.args.ollama_url, endpoint="ollama")
        models = ollama.get("models")
        if not isinstance(models, list) or models:
            raise _safe_code("Ollama model became loaded")
        stats = health.fetch_json(self.args.frigate_stats_url, endpoint="frigate")
        frigate_metrics = health.validate_frigate_stats(
            stats,
            self.expectations,
            self.low_since,
            now_monotonic,
            now_wall,
        )
        resource = validate_runtime_status(
            health.fetch_json(self.args.candidate_status_url, endpoint="candidate"),
            expected_model=self.args.expected_model,
            expected_reserve_bytes=self.args.gpu_free_floor_bytes,
            observed_gpu_total_bytes=gpu["total_mib"] * health.MIB,
            allow_idle_recovery=self._phase_allows_recovery(),
        )
        self.tracker.observe(resource, now_monotonic)
        log_end = time.time()
        self.logs.scan(log_end)
        elapsed = now_monotonic - self.started_monotonic
        self.sample_count += 1
        self.last_elapsed = elapsed
        self.evidence.write(
            {
                "event": "health_final" if final else "health_sample",
                "timestamp": health.utc_now(),
                "sample": self.sample_count,
                "elapsed_seconds": round(elapsed, 3),
                "candidate_status": candidate["status"],
                "candidate_restart_count": candidate["restart_count"],
                "candidate_oom_killed": candidate["oom_killed"],
                "memory_current_bytes": memory["memory.current"],
                "memory_peak_bytes": memory["memory.peak"],
                "memory_events": memory["events"],
                "runtime_state": resource["controller_state"],
                "runtime_recovery_reason": resource["recovery_reason"],
                "runtime_admission_open": resource["admission_open"],
                "host_mem_available_bytes": host_available,
                "gpu_total_mib": gpu["total_mib"],
                "gpu_used_mib": gpu["used_mib"],
                "gpu_free_mib": gpu["free_mib"],
                "frigate_restart_count": frigate["restart_count"],
                "camera_min_process_ratio": frigate_metrics["camera_min_process_ratio"],
                "camera_max_skipped_fps": frigate_metrics["camera_max_skipped_fps"],
                "camera_longest_low_seconds": frigate_metrics[
                    "camera_longest_low_seconds"
                ],
                "detector_count": frigate_metrics["detector_count"],
                "embedding_metric_count": frigate_metrics["embedding_metric_count"],
                "ollama_loaded_models": 0,
                "psi_policy": "parsed_observation_only",
            }
        )
        if elapsed >= self.args.duration_seconds:
            self._duration_reached.set()

    def _run(self) -> None:
        scheduled = time.monotonic()
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now < scheduled:
                    if self._stop.wait(scheduled - now):
                        break
                    now = time.monotonic()
                if now - scheduled > health.MAX_SAMPLE_LAG_SECONDS:
                    raise _safe_code("runtime observer health cadence lag exceeded")
                self.collect()
                scheduled += self.args.interval_seconds
                if time.monotonic() > scheduled:
                    raise _safe_code("runtime observer health work exceeded cadence")
        except BaseException as exc:
            self._failure_exception = exc
            self._failure.set()

    def raise_if_failed(self) -> None:
        if self._failure.is_set():
            failure = self._failure_exception or _safe_code("health monitor failed")
            if isinstance(failure, health.GateAbort):
                raise failure
            raise _safe_code(f"health monitor {type(failure).__name__}") from failure

    def wait_for_duration(self) -> None:
        while not self._duration_reached.wait(1.0):
            self.raise_if_failed()
        self.raise_if_failed()

    def stop_and_join(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.args.interval_seconds + 20)
            if self._thread.is_alive():
                raise _safe_code("health monitor did not stop")
        self.raise_if_failed()


def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float,
    failure_message: str,
    heartbeat: Callable[[], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Any:
    deadline = clock() + timeout
    while True:
        if heartbeat is not None:
            heartbeat()
        value = predicate()
        if value:
            return value
        now = clock()
        if now >= deadline:
            raise _safe_code(failure_message)
        sleeper(min(1.0, deadline - now))


def _worker_finished(
    scanner: RuntimeEventScanner,
    items: Iterable[FixtureItem],
    *,
    after_mark: int,
) -> bool:
    paths = {item.container_media for item in items}
    scanner.assert_no_worker_errors(paths)
    fresh = scanner.events_after(after_mark)
    if any(event.get("path") not in paths for event in fresh):
        raise _safe_code("workload phase observed an unrelated fixture event")
    started: dict[str, tuple[str, str, tuple[int, ...]]] = {}
    finished: set[str] = set()
    for event in fresh:
        path = event.get("path")
        name = event.get("event")
        identity = (
            event.get("task_id"),
            event.get("task_type"),
            tuple(event.get("source_identity", ())),
        )
        if name == "worker_start":
            if path in started or path in finished:
                raise _safe_code("workload phase emitted a duplicate worker start")
            started[path] = identity
        elif name == "worker_finish":
            if path not in started or path in finished:
                raise _safe_code("workload phase worker finish was stale or unpaired")
            if started[path] != identity:
                raise _safe_code("workload phase worker identity changed")
            finished.add(path)
    return set(started) == paths and finished == paths


def prepare_workload_phase(
    *,
    fixtures: FixtureSet,
    items: tuple[FixtureItem, ...],
    scanner: RuntimeEventScanner,
    output_watcher: AtomicOutputWatcher,
) -> PhaseFreshnessMark:
    if not items:
        raise _safe_code("workload phase fixture set was empty")
    directories = {item.media_relative.split("/", 1)[0] for item in items}
    if len(directories) != 1:
        raise _safe_code("workload phase crossed fixture directories")
    scanner.scan()
    event_sequence = scanner.mark()
    publication_sequence = output_watcher.mark()
    assert_fixture_directory_exact(
        fixtures,
        next(iter(directories)),
        allow_outputs=False,
    )
    return PhaseFreshnessMark(event_sequence, publication_sequence)


def _outputs_exist(fixtures: FixtureSet, items: Iterable[FixtureItem]) -> bool:
    item_list = tuple(items)
    directories = {item.media_relative.split("/", 1)[0] for item in item_list}
    for directory in directories:
        declared_media = {
            item.media_relative.split("/", 1)[1]
            for item in item_list
            if item.media_relative.startswith(directory + "/")
        }
        declared_outputs = {
            item.subtitle_relative.split("/", 1)[1]
            for item in item_list
            if item.subtitle_relative is not None
            and item.subtitle_relative.startswith(directory + "/")
        }
        if not _directory_names(fixtures, directory).issubset(
            declared_media | declared_outputs
        ):
            raise _safe_code("fixture directory contained a partial output")
    for item in item_list:
        assert item.subtitle_relative is not None
        directory, name = item.subtitle_relative.split("/", 1)
        if name not in _directory_names(fixtures, directory):
            return False
    return True


def wait_for_transcription_outputs(
    *,
    fixtures: FixtureSet,
    items: tuple[FixtureItem, ...],
    scanner: RuntimeEventScanner,
    monitor: HealthMonitor,
    output_watcher: AtomicOutputWatcher,
    phase_mark: PhaseFreshnessMark,
    timeout: float,
) -> tuple[FileSnapshot, ...]:
    def completed() -> bool:
        output_watcher.drain()
        scanner.scan()
        return _worker_finished(
            scanner, items, after_mark=phase_mark.event_sequence
        ) and _outputs_exist(fixtures, items)

    wait_until(
        completed,
        timeout=timeout,
        failure_message="transcription workload timed out",
        heartbeat=monitor.raise_if_failed,
    )
    output_watcher.assert_published(items, after_mark=phase_mark.publication_sequence)
    outputs = tuple(
        snapshot_fixture_file(
            fixtures,
            item.subtitle_relative or "",
            maximum_bytes=MAX_SUBTITLE_BYTES,
            expected_subtitle=True,
            expected_duration_seconds=_probe_duration(
                fixtures.media_root / item.media_relative
            ),
        )
        for item in items
    )
    directory = items[0].media_relative.split("/", 1)[0]
    assert_fixture_directory_exact(fixtures, directory, allow_outputs=True)
    return outputs


def _output_evidence(role: str, outputs: tuple[FileSnapshot, ...]) -> dict[str, Any]:
    return {
        "event": "workload_output",
        "timestamp": health.utc_now(),
        "role": role,
        "count": len(outputs),
        "outputs": [
            {
                "index": index,
                "bytes": item.size,
                "sha256": item.sha256,
                "cue_count": item.subtitle_cue_count,
                "first_start_ms": item.subtitle_first_start_ms,
                "last_end_ms": item.subtitle_last_end_ms,
            }
            for index, item in enumerate(outputs)
        ],
        "atomic_final_only": True,
        "partials_present": False,
        "atomic_publications_observed": len(outputs),
    }


def _run_composite_workload(
    *,
    args: argparse.Namespace,
    fixtures: FixtureSet,
    originals: dict[str, FileSnapshot],
    api_key: str,
    scanner: RuntimeEventScanner,
    monitor: HealthMonitor,
    tracker: RuntimeCycleTracker,
    output_watcher: AtomicOutputWatcher,
    evidence: LockedEvidence,
) -> None:
    all_workload_paths = [item.container_media for item in fixtures.all_items]
    monitor.set_phase(allow_idle_recovery=False)
    evidence.write(
        {"event": "workload_phase", "timestamp": health.utc_now(), "phase": "long"}
    )
    long_mark = prepare_workload_phase(
        fixtures=fixtures,
        items=(fixtures.long,),
        scanner=scanner,
        output_watcher=output_watcher,
    )
    post_batch(
        fixtures.long.container_directory,
        api_key=api_key,
        timeout=args.http_timeout_seconds,
    )
    long_output = wait_for_transcription_outputs(
        fixtures=fixtures,
        items=(fixtures.long,),
        scanner=scanner,
        monitor=monitor,
        output_watcher=output_watcher,
        phase_mark=long_mark,
        timeout=args.long_timeout_seconds,
    )
    evidence.write(_output_evidence("long_31m", long_output))
    attest_originals_unchanged(fixtures, originals)

    scanner.scan()
    unload_before_short = scanner.unload_count
    if unload_before_short != 0:
        raise _safe_code("model unloaded before resident short batch")
    evidence.write(
        {
            "event": "workload_phase",
            "timestamp": health.utc_now(),
            "phase": "resident_short_batch",
        }
    )
    short_mark = prepare_workload_phase(
        fixtures=fixtures,
        items=fixtures.short,
        scanner=scanner,
        output_watcher=output_watcher,
    )
    post_batch(
        fixtures.short[0].container_directory,
        api_key=api_key,
        timeout=args.http_timeout_seconds,
    )
    short_outputs = wait_for_transcription_outputs(
        fixtures=fixtures,
        items=fixtures.short,
        scanner=scanner,
        monitor=monitor,
        output_watcher=output_watcher,
        phase_mark=short_mark,
        timeout=args.short_timeout_seconds,
    )
    scanner.scan()
    if scanner.unload_count != unload_before_short:
        raise _safe_code("model unloaded during resident short batch")
    evidence.write(_output_evidence("resident_short_batch", short_outputs))
    attest_originals_unchanged(fixtures, originals)

    recovery_mark = tracker.mark()
    monitor.set_phase(allow_idle_recovery=True)
    evidence.write(
        {
            "event": "workload_phase",
            "timestamp": health.utc_now(),
            "phase": "idle_unload_recovery",
        }
    )

    def recovery_complete() -> RuntimeRecoveryProof | None:
        scanner.scan()
        if scanner.unload_count <= unload_before_short:
            return None
        return tracker.idle_recovery_proof(recovery_mark)

    proof = wait_until(
        recovery_complete,
        timeout=args.recovery_timeout_seconds,
        failure_message="idle unload recovery cycle timed out",
        heartbeat=monitor.raise_if_failed,
    )
    assert isinstance(proof, RuntimeRecoveryProof)
    evidence.write(
        {
            "event": "idle_recovery_proof",
            "timestamp": health.utc_now(),
            "recovering_sequence": proof.recovering_sequence,
            "normal_sequence": proof.normal_sequence,
            "complete_health_polls": proof.complete_health_polls,
            "elapsed_seconds": round(proof.elapsed_seconds, 3),
            "recovery_reason": "idle_cleanup",
            "admission_reopened": True,
        }
    )

    scanner.scan()
    unload_before_reload = scanner.unload_count
    evidence.write(
        {
            "event": "workload_phase",
            "timestamp": health.utc_now(),
            "phase": "post_unload_reload",
        }
    )
    reload_mark = prepare_workload_phase(
        fixtures=fixtures,
        items=(fixtures.reload,),
        scanner=scanner,
        output_watcher=output_watcher,
    )
    post_batch(
        fixtures.reload.container_directory,
        api_key=api_key,
        timeout=args.http_timeout_seconds,
    )
    reload_output = wait_for_transcription_outputs(
        fixtures=fixtures,
        items=(fixtures.reload,),
        scanner=scanner,
        monitor=monitor,
        output_watcher=output_watcher,
        phase_mark=reload_mark,
        timeout=args.reload_timeout_seconds,
    )
    scanner.scan()
    if scanner.unload_count != unload_before_reload:
        raise _safe_code("model unloaded before reload workload completed")
    evidence.write(_output_evidence("post_unload_reload", reload_output))
    attest_originals_unchanged(fixtures, originals)

    monitor.set_phase(allow_idle_recovery=True)
    evidence.write(
        {
            "event": "workload_phase",
            "timestamp": health.utc_now(),
            "phase": "retention_controls",
        }
    )
    invalid_mark = prepare_workload_phase(
        fixtures=fixtures,
        items=(fixtures.invalid,),
        scanner=scanner,
        output_watcher=output_watcher,
    )
    post_batch(
        fixtures.invalid.container_directory,
        api_key=api_key,
        timeout=args.http_timeout_seconds,
    )

    def invalid_observed() -> bool:
        scanner.scan()
        scanner.assert_no_worker_errors(all_workload_paths)
        if any(
            event.get("path") != fixtures.invalid.container_media
            for event in scanner.events_after(invalid_mark.event_sequence)
        ):
            raise _safe_code("invalid control observed an unrelated fixture event")
        matches = scanner.matching_events(
            "media_validation_failed",
            [fixtures.invalid.container_media],
            after_mark=invalid_mark.event_sequence,
        )
        return any(
            event.get("failure_class") == "invalid_media"
            and event.get("validator_outcomes")
            == {"ffprobe": "invalid_format", "pyav": "invalid_format"}
            for event in matches
        )

    wait_until(
        invalid_observed,
        timeout=args.retention_timeout_seconds,
        failure_message="dual-invalid control was not observed",
        heartbeat=monitor.raise_if_failed,
    )
    if (
        snapshot_fixture_file(fixtures, fixtures.invalid.media_relative)
        != originals[fixtures.invalid.media_relative]
    ):
        raise _safe_code("dual-invalid control was deleted or changed")
    assert_fixture_directory_exact(fixtures, "invalid", allow_outputs=False)
    evidence.write(
        {
            "event": "retention_proof",
            "timestamp": health.utc_now(),
            "role": "dual_invalid",
            "retained": True,
            "source_sha256": originals[fixtures.invalid.media_relative].sha256,
            "subtitle_created": False,
            "deletion_controls": "off",
        }
    )

    silent_mark = prepare_workload_phase(
        fixtures=fixtures,
        items=(fixtures.silent,),
        scanner=scanner,
        output_watcher=output_watcher,
    )
    post_batch(
        fixtures.silent.container_directory,
        api_key=api_key,
        timeout=args.http_timeout_seconds,
    )

    def silent_observed() -> bool:
        scanner.scan()
        scanner.assert_no_worker_errors(all_workload_paths)
        if any(
            event.get("path") != fixtures.silent.container_media
            for event in scanner.events_after(silent_mark.event_sequence)
        ):
            raise _safe_code("silent control observed an unrelated fixture event")
        return scanner.silent_after(
            silent_mark.event_sequence,
            fixtures.silent.container_media,
        )

    wait_until(
        silent_observed,
        timeout=args.retention_timeout_seconds,
        failure_message="silent media control was not observed",
        heartbeat=monitor.raise_if_failed,
    )
    if (
        snapshot_fixture_file(fixtures, fixtures.silent.media_relative)
        != originals[fixtures.silent.media_relative]
    ):
        raise _safe_code("silent media control was deleted or changed")
    assert_fixture_directory_exact(fixtures, "silent", allow_outputs=False)
    evidence.write(
        {
            "event": "retention_proof",
            "timestamp": health.utc_now(),
            "role": "silent_media",
            "retained": True,
            "source_sha256": originals[fixtures.silent.media_relative].sha256,
            "subtitle_created": False,
            "deletion_controls": "off",
        }
    )
    attest_originals_unchanged(fixtures, originals)
    scanner.scan()
    scanner.assert_no_worker_errors(all_workload_paths)
    for directory in sorted(EXPECTED_FIXTURE_DIRECTORIES):
        allow_outputs = directory in {"long", "short", "reload"}
        assert_fixture_directory_exact(fixtures, directory, allow_outputs=allow_outputs)
    output_watcher.assert_complete()


def _stop_and_validate(
    *,
    args: argparse.Namespace,
    client: health.DockerClient,
    candidate: health.CandidateBinding,
    frigate: health.ObservedBinding,
    logs: health.IncrementalLogScanner,
    baselines: HealthBaselines,
) -> dict[str, Any]:
    outcome = health.stop_bound_candidate(client, candidate, args)
    item = health.candidate_state(client, candidate, args)
    frigate_state = health.observed_state(client, frigate)
    if item["running"] is not False or item["status"] not in {"exited", "dead"}:
        raise _safe_code("candidate stop completion state was invalid")
    if (
        item["oom_killed"] is not False
        or item["restart_count"] != baselines.candidate_restart_count
    ):
        raise _safe_code("candidate health changed during cleanup")
    if (
        frigate_state["running"] is not True
        or frigate_state["health"] != "healthy"
        or frigate_state["restart_count"] != baselines.frigate_restart_count
    ):
        raise _safe_code("Frigate changed during candidate cleanup")
    end_wall = time.time()
    logs.scan(end_wall)
    outcome["completion"] = {
        "candidate": item,
        "frigate": frigate_state,
        "logs_drained_through_wall": round(end_wall, 6),
    }
    return outcome


def drain_workload_events_after_stop(
    scanner: RuntimeEventScanner, items: Iterable[FixtureItem]
) -> None:
    """Drain structured events only after Docker has confirmed process exit."""
    scanner.scan(time.time())
    scanner.assert_no_worker_errors(item.container_media for item in items)


def _retired_pre_amendment_observer(args: argparse.Namespace) -> int:
    """Retained only as dead source context; the old composite gate cannot pass."""
    raise _safe_code("pre amendment runtime observer contract was retired")
    observer_sha256, sampler_sha256 = _verified_runtime_identities(args)
    started_wall = time.time()
    client = health.DockerClient(
        args.expected_docker_daemon_id, args.expected_host_boot_id
    )
    candidate: health.CandidateBinding | None = None
    frigate: health.ObservedBinding | None = None
    evidence: LockedEvidence | None = None
    monitor: HealthMonitor | None = None
    output_watcher: AtomicOutputWatcher | None = None
    logs: health.IncrementalLogScanner | None = None
    baselines: HealthBaselines | None = None
    cleanup: dict[str, Any] | None = None
    prior_handlers: dict[int, Any] = {}
    passed = False
    failure: BaseException | None = None
    try:
        prior_handlers = health.install_signal_handlers()
        _verify_adjacent_frozen_sampler()
        boundary = health.ensure_boundary_expectation(args)
        candidate = health.bind_candidate(client, args)
        candidate_item = client.inspect(candidate.container_id)
        if (
            not isinstance(candidate_item, dict)
            or candidate_item.get("Id") != candidate.container_id
        ):
            raise _safe_code("runtime chunk policy candidate identity changed")
        chunk_policy = validate_runtime_chunk_policy(
            candidate_item,
            expected_chunk_minutes=args.expected_chunk_minutes,
        )
        fixtures = load_fixture_manifest(args.fixture_manifest, boundary)
        api_key = _read_api_key(args.api_key_file)
        validate_request_isolation(candidate_item, api_key)
        assert_outputs_absent(fixtures)
        originals = capture_original_snapshots(fixtures)
        durations = validate_fixture_durations(fixtures)
        output_watcher = AtomicOutputWatcher(fixtures)
        camera_expectations = health.load_camera_expectations(args.camera_expectations)
        daemon_digest, boot_digest = health.verify_bound_docker_daemon(client, args)
        frigate = health.bind_observed_container(client, args.frigate_container)
        frigate_before = health.observed_state(client, frigate)
        if (
            frigate_before["running"] is not True
            or frigate_before["health"] != "healthy"
        ):
            raise _safe_code("Frigate was unhealthy before runtime workload")
        writer = health.EvidenceWriter.open(args.output, candidate.gate_token_digest)
        evidence = LockedEvidence(
            writer,
            (
                args.gate_token,
                str(fixtures.media_root),
                str(args.fixture_manifest),
                str(args.api_key_file) if args.api_key_file else "",
                api_key or "",
                *camera_expectations.keys(),
            ),
        )
        logs = health.IncrementalLogScanner(client, candidate, frigate, started_wall)
        workload_logs = RuntimeEventScanner(
            client,
            candidate,
            started_wall,
            (item.container_media for item in fixtures.all_items),
        )
        evidence.write(
            {
                "event": "observer_start",
                "timestamp": health.utc_now(),
                "schema": OBSERVER_SCHEMA,
                "observer_sha256": observer_sha256,
                "sampler_sha256": sampler_sha256,
                "fixture_manifest_sha256": fixtures.manifest_sha256,
                "candidate_container_id_sha256": health.sha256_bytes(
                    candidate.container_id.encode("ascii")
                ),
                "candidate_image_config": candidate.image_config,
                "candidate_command_sha256": candidate.command_digest,
                "candidate_boundary_sha256": candidate.boundary_digest,
                "boundary_manifest_sha256": boundary.file_sha256,
                "runtime_commit": candidate.runtime_commit,
                "gate_role": candidate.gate_role,
                "gate_token_sha256": candidate.gate_token_digest,
                "docker_daemon_id_sha256": daemon_digest,
                "host_boot_id_sha256": boot_digest,
                "expected_memory_bytes": args.expected_memory_bytes,
                "gpu_free_floor_bytes": args.gpu_free_floor_bytes,
                "host_reserve_bytes": args.host_reserve_bytes,
                "minimum_observation_seconds": args.duration_seconds,
                "interval_seconds": args.interval_seconds,
                "camera_count": len(camera_expectations),
                "fixture_durations": durations,
                "runtime_chunk_policy": chunk_policy,
                "deletion_controls": "explicitly_off_and_boundary_bound",
            }
        )
        health.start_bound_candidate(client, candidate, args)
        initial_candidate = health.wait_for_running(
            client,
            candidate,
            args,
            time.monotonic() + args.start_timeout_seconds,
        )
        initial_resource = health.wait_for_runtime_ready(
            args, time.monotonic() + args.start_timeout_seconds
        )
        initial_memory = health.candidate_memory(client, candidate)
        health.validate_candidate_memory_snapshot(
            initial_memory, expected_memory_bytes=args.expected_memory_bytes
        )
        frigate_initial = health.observed_state(client, frigate)
        if (
            frigate_initial["running"] is not True
            or frigate_initial["health"] != "healthy"
        ):
            raise _safe_code("Frigate was unhealthy at runtime start")
        baselines = HealthBaselines(
            candidate_restart_count=initial_candidate["restart_count"],
            frigate_restart_count=frigate_initial["restart_count"],
        )
        tracker = RuntimeCycleTracker()
        observation_started = time.monotonic()
        monitor = HealthMonitor(
            args=args,
            evidence=evidence,
            client=client,
            candidate=candidate,
            frigate=frigate,
            expectations=camera_expectations,
            baselines=baselines,
            logs=logs,
            tracker=tracker,
            output_watcher=output_watcher,
            started_monotonic=observation_started,
        )
        evidence.write(
            {
                "event": "candidate_ready",
                "timestamp": health.utc_now(),
                "candidate_state": initial_candidate,
                "candidate_resource": initial_resource,
                "candidate_memory": initial_memory,
                "frigate_state": frigate_initial,
            }
        )
        monitor.start()
        _run_composite_workload(
            args=args,
            fixtures=fixtures,
            originals=originals,
            api_key=api_key,
            scanner=workload_logs,
            monitor=monitor,
            tracker=tracker,
            output_watcher=output_watcher,
            evidence=evidence,
        )
        monitor.wait_for_duration()
        attest_originals_unchanged(fixtures, originals)
        wait_until(
            tracker.latest_is_healthy,
            timeout=args.recovery_timeout_seconds,
            failure_message="runtime did not finish in normal open state",
            heartbeat=monitor.raise_if_failed,
        )
        monitor.set_phase(allow_idle_recovery=False)
        monitor.stop_and_join()
        monitor.collect(final=True)
        workload_logs.scan()
        workload_logs.assert_no_worker_errors(
            item.container_media for item in fixtures.all_items
        )
        cleanup = _stop_and_validate(
            args=args,
            client=client,
            candidate=candidate,
            frigate=frigate,
            logs=logs,
            baselines=baselines,
        )
        drain_workload_events_after_stop(workload_logs, fixtures.all_items)
        output_watcher.assert_complete()
        output_watcher.close()
        output_watcher = None
        evidence.write(
            {
                "event": "observer_pass",
                "timestamp": health.utc_now(),
                "continuous_seconds": round(monitor.last_elapsed, 3),
                "health_samples": monitor.sample_count,
                "observer_sha256": observer_sha256,
                "sampler_sha256": sampler_sha256,
                "cleanup": cleanup,
            }
        )
        evidence.seal(
            outcome="pass",
            sampler_sha256=sampler_sha256,
            image_config=candidate.image_config,
            cleanup=cleanup,
        )
        passed = True
    except BaseException as exc:
        failure = exc
    finally:
        if monitor is not None and not passed:
            try:
                monitor.stop_and_join()
            except BaseException as monitor_exc:
                if failure is None:
                    failure = monitor_exc
        if not passed and candidate is not None:
            try:
                cleanup = health.stop_bound_candidate(client, candidate, args)
            except BaseException as cleanup_exc:
                cleanup = {
                    "verified_stopped": False,
                    "error": health.safe_reason(cleanup_exc),
                }
                failure = _safe_code(
                    f"{health.safe_reason(failure or _safe_code('observer failure'))} cleanup unverified"
                )
        if output_watcher is not None:
            try:
                output_watcher.close()
            except BaseException as watcher_exc:
                if failure is None:
                    failure = watcher_exc
        if not passed and evidence is not None and not evidence.closed:
            try:
                evidence.write(
                    {
                        "event": "observer_abort",
                        "timestamp": health.utc_now(),
                        "reason": health.safe_reason(
                            failure or _safe_code("observer failure")
                        ),
                        "elapsed_wall_seconds": round(time.time() - started_wall, 3),
                        "observer_sha256": observer_sha256,
                        "sampler_sha256": sampler_sha256,
                        "cleanup": cleanup,
                    }
                )
                evidence.seal(
                    outcome="abort",
                    sampler_sha256=sampler_sha256,
                    image_config=candidate.image_config if candidate else "unbound",
                    cleanup=cleanup or {"verified_stopped": False},
                )
            except BaseException:
                pass
        if evidence is not None:
            evidence.close()
        if prior_handlers:
            health.restore_signal_handlers(prior_handlers)
    if not passed:
        if isinstance(failure, health.GateAbort):
            raise failure
        raise _safe_code(
            health.safe_reason(failure or _safe_code("observer failure"))
        ) from failure
    assert candidate is not None and monitor is not None
    print(
        "TASK11B_RUNTIME_WORKLOAD_PASS "
        f"container_id_sha256={health.sha256_bytes(candidate.container_id.encode('ascii'))} "
        f"seconds={round(monitor.last_elapsed, 3)} samples={monitor.sample_count}"
    )
    return 0


def _exact_boundary_mount(
    boundary: Any, destination: str, *, read_write: bool
) -> dict[str, Any]:
    """Select one already-validated mount without accepting a caller path."""
    mounts = boundary.document.get("mounts")
    if not isinstance(mounts, list):
        raise _safe_code("execution boundary mounts were unavailable")
    matching = [
        mount
        for mount in mounts
        if isinstance(mount, dict) and mount.get("destination") == destination
    ]
    if (
        len(matching) != 1
        or matching[0].get("read_write") is not read_write
        or matching[0].get("mode") != ("rw" if read_write else "ro")
        or matching[0].get("type") != "bind"
        or matching[0].get("propagation") != "rprivate"
    ):
        raise _safe_code("execution boundary mount binding was not exact")
    return dict(matching[0])


def _load_amended_gate_inputs(
    args: argparse.Namespace,
    boundary: Any,
) -> dict[str, Any]:
    """Load every private immutable input before the candidate is started."""
    identity = boundary.document.get("candidate_identity")
    if not isinstance(identity, dict):
        raise _safe_code("candidate identity was unavailable from boundary")
    policy_payload = _require_private_file(
        args.priority_policy, maximum=32 * 1024, label="priority policy"
    )
    policy_document = _strict_json_line(
        policy_payload, label="priority policy", maximum=32 * 1024
    )
    policy = validate_priority_policy(
        policy_document,
        policy_payload,
        expected_file_sha256=args.priority_policy_sha256,
    )
    envelope_payload = _require_private_file(
        args.unloaded_gpu_envelope,
        maximum=512 * 1024,
        label="unloaded GPU envelope",
    )
    envelope_document = _strict_json_line(
        envelope_payload,
        label="unloaded GPU envelope",
        maximum=512 * 1024,
    )
    envelope = validate_unloaded_gpu_envelope(
        envelope_document,
        envelope_payload,
        expected_file_sha256=args.unloaded_gpu_envelope_sha256,
        expected_policy_sha256=args.priority_policy_sha256,
        expected_runtime_commit=args.runtime_commit,
        expected_oci_index=args.candidate_oci_index,
        expected_config_digest=args.candidate_config_digest,
        expected_layer_diff_ids=identity.get("layer_diff_ids"),
    )
    catalog_payload = _require_private_file(
        args.model_envelope_catalog,
        maximum=4 * MIB,
        label="model envelope catalog",
    )
    catalog_document = _strict_canonical_json_document(
        catalog_payload,
        label="model envelope catalog",
        maximum=4 * MIB,
        trailing_newline=False,
    )
    catalog = validate_model_envelope_catalog(
        catalog_document,
        catalog_payload,
        expected_file_sha256=args.model_envelope_catalog_sha256,
        candidate_config_digest=identity.get("config_digest"),
        candidate_layer_diff_ids=identity.get("layer_diff_ids"),
        unloaded_envelope=envelope,
    )
    fixtures: dict[str, Any] = {}
    for phase, fixture_path, expected_hash in (
        ("a", args.phase_a_fixture_record, args.phase_a_fixture_record_sha256),
        ("b", args.phase_b_fixture_record, args.phase_b_fixture_record_sha256),
    ):
        record = health.load_fixture_record(fixture_path, expected_hash)
        fixture = health.revalidate_fixture_record(
            record,
            _exact_boundary_mount(
                boundary, f"/fixtures/phase-{phase}", read_write=False
            ),
            _exact_boundary_mount(
                boundary, f"/task11b-output/phase-{phase}", read_write=True
            ),
        )
        if fixture.record_sha256 != expected_hash:
            raise _safe_code("fixture record digest changed during binding")
        fixtures[phase] = fixture
    if fixtures["a"].workload_sha256 == fixtures["b"].workload_sha256:
        raise _safe_code("Phase A and Phase B fixture identities were not distinct")
    return {
        "identity": dict(identity),
        "policy": policy,
        "envelope": envelope,
        "catalog": catalog,
        "fixtures": fixtures,
    }


def _wait_for_initial_runtime(
    args: argparse.Namespace, *, deadline: float
) -> dict[str, Any]:
    """Wait only for an initialized atomic runtime, not for a workload."""
    while time.monotonic() < deadline:
        try:
            gpu = health.gpu_telemetry()
            status = validate_runtime_status(
                health.fetch_json(args.candidate_status_url, endpoint="candidate"),
                expected_model=args.expected_model,
                expected_reserve_bytes=args.gpu_free_floor_bytes,
                observed_gpu_total_bytes=gpu["total_mib"] * MIB,
                expected_priority_state=None,
                expected_policy_sha256=args.priority_policy_sha256,
                expected_controller_phase="normal",
                expected_recovery_reason=None,
                expected_admission_open=True,
                expected_model_resident=None,
                require_gate_runtime=False,
            )
            if status["priority_pressure"]["state"] not in {"clear", "neutral"}:
                raise _safe_code("runtime initialized under priority pressure")
            return status
        except (health.TelemetryUnavailable, health.CandidateNotReady):
            time.sleep(0.25)
    raise _safe_code("candidate runtime did not initialize before deadline")


def _open_runtime_journal(
    path: Path,
    *,
    runtime_epoch: str,
    gate_token_sha256: str,
    deadline: float,
) -> RuntimeReceiptJournal:
    while time.monotonic() < deadline:
        try:
            journal = RuntimeReceiptJournal(
                path,
                expected_runtime_epoch=runtime_epoch,
                expected_token_sha256=gate_token_sha256,
            )
            journal.read_available()
            if journal.receipts and journal.receipts[0]["sequence"] == 1:
                return journal
            journal.close()
        except FileNotFoundError:
            pass
        except health.GateAbort as exc:
            if exc.code not in {
                "runtime_receipt_journal_was_unavailable",
                "runtime_receipt_journal_could_not_be_opened",
            }:
                raise
        time.sleep(0.25)
    raise _safe_code("runtime receipt journal did not publish sequence one")


def _wait_for_receipt(
    journal: RuntimeReceiptJournal,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    deadline_ns: int,
    label: str,
    after_sequence: int = 0,
    heartbeat: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Consume every record and return the first later record matching predicate."""
    last_heartbeat_ns = 0
    while True:
        journal.read_available()
        for receipt in journal.receipts:
            if receipt["sequence"] > after_sequence and predicate(receipt):
                return receipt
        now = time.monotonic_ns()
        if now >= deadline_ns:
            raise _safe_code(f"{label} receipt deadline was exceeded")
        if (
            heartbeat is not None
            and journal.receipts
            and (not last_heartbeat_ns or now - last_heartbeat_ns >= 1_000_000_000)
        ):
            heartbeat(journal.receipts[-1])
            last_heartbeat_ns = time.monotonic_ns()
        time.sleep(min(0.25, (deadline_ns - now) / 1_000_000_000))


def _validate_clear_signal(
    args: argparse.Namespace, *, minimum_source_generation: int
) -> dict[str, Any]:
    payload = _require_private_file(
        args.priority_signal,
        maximum=MAX_PRIORITY_SIGNAL_BYTES,
        label="Phase B priority clear signal",
    )
    document = _strict_json_line(
        payload,
        label="Phase B priority clear signal",
        maximum=MAX_PRIORITY_SIGNAL_BYTES,
    )
    if (
        set(document) != PRIORITY_SIGNAL_KEYS
        or document.get("schema") != 1
        or document.get("boot_id_sha256") != args.priority_boot_id_sha256
        or document.get("producer_epoch") != args.priority_producer_epoch
        or document.get("policy_sha256") != args.priority_policy_sha256
        or document.get("pressure") is not False
        or document.get("clear_eligible") is not True
        or document.get("reason_codes") != []
        or not _exact_int(document.get("sequence"), minimum=1)
        or not _exact_int(
            document.get("source_generation"), minimum=minimum_source_generation + 1
        )
        or not _exact_int(document.get("observed_monotonic_ns"), minimum=1)
        or not _exact_int(document.get("source_observed_monotonic_ns"), minimum=1)
        or not isinstance(document.get("observation_id"), str)
        or LOWER_HEX_64_RE.fullmatch(document["observation_id"]) is None
    ):
        raise _safe_code("Phase B priority clear signal was not exact and fresh")
    now = time.monotonic_ns()
    if (
        document["source_observed_monotonic_ns"] > document["observed_monotonic_ns"]
        or document["observed_monotonic_ns"] > now
        or now - document["observed_monotonic_ns"] > 10_000_000_000
        or now - document["source_observed_monotonic_ns"] > 30_000_000_000
    ):
        raise _safe_code("Phase B priority clear signal was stale")
    return document


def _phase_a_host_observation(
    *,
    fixture: Any,
    watcher: ExactArtifactWatcher,
    cgroup_probe: Any,
    gpu_uuid: str,
    receipt: dict[str, Any],
    threshold_masking_allowed: bool,
    attribution_time: bool = False,
) -> dict[str, Any]:
    attribution = cgroup_probe.attributed_gpu_bytes(gpu_uuid)
    state = watcher.snapshot()
    return {
        "monotonic_ns": (
            attribution.validated_monotonic_ns
            if attribution_time
            else receipt["observed_monotonic_ns"]
        ),
        "candidate_bytes": attribution.candidate_bytes,
        **state,
        "threshold_masking_allowed": threshold_masking_allowed,
    }


def _require_zero_phase_failures(deltas: dict[str, Any]) -> None:
    if deltas.get("candidate_oom_killed") is not False or any(
        value != 0
        for key, value in deltas.items()
        if key not in {"candidate_oom_killed"}
    ):
        raise _safe_code("gate failure counter increased")


def _capture_phase_b_reset_boundary(
    *,
    client: Any,
    candidate: Any,
    frigate: Any,
    args: argparse.Namespace,
    cgroup_probe: Any,
    candidate_log: Any,
    kernel_journal: Any,
    receipt: dict[str, Any],
    lifecycle: PhaseBLifecycleOrder,
) -> tuple[GateFailureBaseline, PinnedCgroupEvidence, int]:
    """Finish every Phase-B baseline read before publishing the reset time."""
    baseline = capture_gate_failure_baseline(
        client=client,
        candidate=candidate,
        frigate=frigate,
        args=args,
        cgroup_probe=cgroup_probe,
        candidate_log=candidate_log,
        kernel_journal=kernel_journal,
        receipt=receipt,
    )
    cgroup_evidence = PinnedCgroupEvidence.capture(cgroup_probe, baseline=baseline)
    try:
        lifecycle.checkpoint("baseline")
        reset_completed_ns = lifecycle.perform("reset_timestamp", time.monotonic_ns)
    except BaseException:
        cgroup_evidence.close()
        raise
    return baseline, cgroup_evidence, reset_completed_ns


def _validate_live_phase_b_failure_boundary(
    *,
    baseline: GateFailureBaseline,
    client: Any,
    candidate: Any,
    frigate: Any,
    args: argparse.Namespace,
    cgroup_probe: Any,
    candidate_log: Any,
    kernel_journal: Any,
    receipt: dict[str, Any],
    gpu_uuid: str,
) -> None:
    """Drain every live failure source after the durable Phase-B seal."""
    deltas = capture_gate_failure_deltas(
        baseline=baseline,
        client=client,
        candidate=candidate,
        frigate=frigate,
        args=args,
        cgroup_probe=cgroup_probe,
        candidate_log=candidate_log,
        kernel_journal=kernel_journal,
        receipt=receipt,
    )
    _require_zero_phase_failures(deltas)
    attribution = cgroup_probe.attributed_gpu_bytes(gpu_uuid)
    expected_gpu_digest = hashlib.sha256(gpu_uuid.encode("ascii")).hexdigest()
    if (
        not _exact_int(attribution.candidate_bytes)
        or not _exact_int(attribution.validated_monotonic_ns, minimum=1)
        or not isinstance(attribution.pid_set_sha256, str)
        or LOWER_HEX_64_RE.fullmatch(attribution.pid_set_sha256) is None
        or attribution.gpu_uuid_sha256 != expected_gpu_digest
    ):
        raise _safe_code("Phase B final candidate GPU attribution was invalid")
    gpu = health.gpu_telemetry()
    if gpu["free_mib"] * MIB < args.gpu_free_floor_bytes:
        raise _safe_code("Phase B final GPU priority reserve was breached")


def _validate_stopped_candidate_log_boundary(
    *,
    baseline: GateFailureBaseline,
    client: Any,
    frigate: Any,
    stopped_item: dict[str, Any],
    candidate_log_snapshot: Any,
) -> None:
    """Validate the stopped Docker/Frigate state and the closed candidate log."""
    state = stopped_item.get("State")
    restart_count = stopped_item.get("RestartCount")
    if (
        not isinstance(state, dict)
        or state.get("Running") is not False
        or state.get("Pid") != 0
        or state.get("OOMKilled") is not False
        or not _exact_int(restart_count)
        or restart_count != baseline.candidate_restart_count
    ):
        raise _safe_code("Phase B stopped candidate failure state changed")
    observed = health.observed_state(client, frigate)
    if (
        observed["running"] is not True
        or observed["health"] != "healthy"
        or observed["restart_count"] != baseline.frigate_restart_count
    ):
        raise _safe_code("Phase B stopped-boundary Frigate state changed")
    if (
        candidate_log_snapshot.continuous is not True
        or candidate_log_snapshot.source_container_id_sha256
        != baseline.candidate_log_source_sha256
        or candidate_log_snapshot.byte_cursor < baseline.candidate_log_byte_cursor
        or _nonnegative_delta(
            candidate_log_snapshot.cuda_oom_matches,
            baseline.candidate_cuda_oom_log_matches,
            "candidate CUDA OOM log match",
        )
        != 0
    ):
        raise _safe_code("Phase B final candidate log drain was not clean")


def _validate_stopped_cgroup_boundary(
    *,
    baseline: GateFailureBaseline,
    cgroup_evidence: PinnedCgroupEvidence,
) -> None:
    """Read the exact live-pinned hierarchy after candidate stop."""
    if not isinstance(cgroup_evidence, PinnedCgroupEvidence):
        raise _safe_code("Phase B pinned cgroup evidence was unavailable")
    cgroup = cgroup_evidence.snapshot(expected_population=0)
    if (
        cgroup.container_pid != baseline.cgroup_pid
        or cgroup.cgroup_path_sha256 != baseline.cgroup_path_sha256
        or _nonnegative_delta(cgroup.oom, baseline.cgroup_oom, "cgroup oom") != 0
        or _nonnegative_delta(
            cgroup.oom_kill, baseline.cgroup_oom_kill, "cgroup oom_kill"
        )
        != 0
        or _nonnegative_delta(
            cgroup.oom_group_kill,
            baseline.cgroup_oom_group_kill,
            "cgroup oom_group_kill",
        )
        != 0
        or cgroup.descendants_populated != 0
        or cgroup.frozen != 0
    ):
        raise _safe_code("Phase B final pinned cgroup drain was not clean")


def _validate_stopped_kernel_boundary(
    *, baseline: GateFailureBaseline, kernel_journal: Any
) -> None:
    """Drain the kernel cursor after stop and candidate-log EOF."""
    kernel = kernel_journal.snapshot()
    if (
        kernel.continuous is not True
        or _nonnegative_delta(
            kernel.xid_matches,
            baseline.nvidia_xid_log_matches,
            "NVIDIA Xid log match",
        )
        != 0
    ):
        raise _safe_code("Phase B final kernel journal drain was not clean")


def _validate_stopped_receipt_boundary(
    *, baseline: GateFailureBaseline, journal: RuntimeReceiptJournal
) -> None:
    """Drain the durable runtime journal to EOF after candidate stop."""
    journal.read_available(final=True)
    if not journal.receipts:
        raise _safe_code("Phase B final runtime receipt drain was empty")
    receipt = _receipt_document(journal.receipts[-1])
    if (
        _nonnegative_delta(
            receipt["cuda_oom_generation"],
            baseline.runtime_cuda_oom_generation,
            "runtime CUDA OOM generation",
        )
        != 0
        or _nonnegative_delta(
            receipt["media_failure_generation"],
            baseline.runtime_media_failure_generation,
            "runtime media failure generation",
        )
        != 0
    ):
        raise _safe_code("Phase B final runtime failure generation increased")


def _validate_stopped_phase_b_failure_boundary(
    *,
    baseline: GateFailureBaseline,
    client: Any,
    frigate: Any,
    stopped_item: dict[str, Any],
    candidate_log_snapshot: Any,
    cgroup_evidence: PinnedCgroupEvidence,
    kernel_journal: Any,
    journal: RuntimeReceiptJournal,
) -> None:
    """Compatibility wrapper for the complete stopped-boundary validation."""
    _validate_stopped_candidate_log_boundary(
        baseline=baseline,
        client=client,
        frigate=frigate,
        stopped_item=stopped_item,
        candidate_log_snapshot=candidate_log_snapshot,
    )
    _validate_stopped_cgroup_boundary(
        baseline=baseline, cgroup_evidence=cgroup_evidence
    )
    _validate_stopped_kernel_boundary(baseline=baseline, kernel_journal=kernel_journal)
    _validate_stopped_receipt_boundary(baseline=baseline, journal=journal)


def _read_private_runtime_output(path: Path, *, owner_uid: int = 1000) -> bytes:
    """Read the exact created subtitle while allowing the non-root runtime owner."""
    if not path.is_absolute():
        raise _safe_code("Phase A subtitle path was not absolute")
    try:
        initial = path.lstat()
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise _safe_code("Phase A subtitle was unavailable") from exc
    if (
        parent != path.parent.absolute()
        or stat.S_ISLNK(initial.st_mode)
        or not stat.S_ISREG(initial.st_mode)
        or initial.st_uid != owner_uid
        or initial.st_nlink != 1
        or not 0 < initial.st_size <= MAX_SUBTITLE_BYTES
    ):
        raise _safe_code("Phase A subtitle identity was unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        payload = _read_all_fd(descriptor, MAX_SUBTITLE_BYTES, label="Phase A subtitle")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, key) != getattr(after, key) for key in fields) or (
        before.st_dev,
        before.st_ino,
    ) != (initial.st_dev, initial.st_ino):
        raise _safe_code("Phase A subtitle changed while read")
    return payload


def _record_phase_a_event(
    collector: PhaseAEventCollector,
    kind: str,
    *,
    journal: RuntimeReceiptJournal,
    receipt: dict[str, Any],
    fixture: Any,
    watcher: ExactArtifactWatcher,
    cgroup_probe: Any,
    gpu_uuid: str,
    threshold_masking_allowed: bool,
    attribution_time: bool = False,
) -> dict[str, Any]:
    del fixture
    return collector.record_event(
        kind,
        receipts=journal.receipts,
        host_observation=_phase_a_host_observation(
            fixture=None,
            watcher=watcher,
            cgroup_probe=cgroup_probe,
            gpu_uuid=gpu_uuid,
            receipt=receipt,
            threshold_masking_allowed=threshold_masking_allowed,
            attribution_time=attribution_time,
        ),
    )


def _wait_for_assertion(
    args: argparse.Namespace,
    *,
    journal: RuntimeReceiptJournal,
    after_sequence: int,
    deadline_ns: int,
    heartbeat: Callable[[dict[str, Any]], None],
) -> PriorityAssertion:
    expected_not_ready = "priority_assertion_observation_was_not_qualifying"
    while time.monotonic_ns() < deadline_ns:
        journal.read_available()
        if any(
            receipt["sequence"] > after_sequence
            and receipt["priority_state"] == "asserted"
            for receipt in journal.receipts
        ):
            raise _safe_code("runtime consumed assertion before host defined T0")
        try:
            return open_priority_assertion(
                args.priority_signal,
                expected_policy_sha256=args.priority_policy_sha256,
                expected_boot_id_sha256=args.priority_boot_id_sha256,
                expected_producer_epoch=args.priority_producer_epoch,
            )
        except health.GateAbort as exc:
            if exc.code != expected_not_ready:
                raise
        heartbeat(journal.receipts[-1])
        time.sleep(0.25)
    raise _safe_code("qualifying priority assertion was not observed")


def run_observer(args: argparse.Namespace) -> int:
    """Run only the amended two-phase, receipt-bound Task 11B gate."""
    observer_sha256, sampler_sha256 = _verified_runtime_identities(args)
    client = health.DockerClient(
        args.expected_docker_daemon_id, args.expected_host_boot_id
    )
    candidate: Any = None
    journal: RuntimeReceiptJournal | None = None
    candidate_log: Any = None
    phase_a_watcher: ExactArtifactWatcher | None = None
    phase_b_watcher: ExactArtifactWatcher | None = None
    phase_b_cgroup_evidence: PinnedCgroupEvidence | None = None
    gate_cgroup_parent: GateOwnedCgroupParent | None = None
    prior_handlers: dict[int, Any] = {}
    passed = False
    failure: BaseException | None = None
    try:
        prior_handlers = health.install_signal_handlers()
        _verify_adjacent_frozen_sampler()
        boundary = health.ensure_boundary_expectation(args)
        candidate = health.bind_candidate(client, args)
        candidate_item = client.inspect(candidate.container_id)
        if not isinstance(candidate_item, dict):
            raise _safe_code("candidate Docker item was unavailable")
        validate_runtime_chunk_policy(
            candidate_item, expected_chunk_minutes=args.expected_chunk_minutes
        )
        api_key = _read_api_key(args.api_key_file)
        validate_request_isolation(candidate_item, api_key)
        gate = _load_amended_gate_inputs(args, boundary)
        identity = gate["identity"]
        policy = gate["policy"]
        envelope = gate["envelope"]
        catalog = gate["catalog"]
        phase_a_fixture = gate["fixtures"]["a"]
        phase_b_fixture = gate["fixtures"]["b"]
        candidate_identity_sha256 = _canonical_document_sha256(
            identity, label="candidate identity"
        )
        docker_daemon_identity_sha256 = health.docker_daemon_identity_sha256(
            boundary.document.get("docker_daemon_identity")
        )
        candidate_record = health.write_candidate_identity_document(
            args.candidate_identity_record,
            {
                "schema": "subgen.task11b.candidate-identity/v2",
                "candidate_identity": identity,
                "docker_daemon_identity_sha256": docker_daemon_identity_sha256,
                "execution_boundary_manifest_sha256": boundary.file_sha256,
                "gate_token_sha256": candidate.gate_token_digest,
                "intended_command_sha256": candidate.command_digest,
                "created_stopped": True,
            },
        )
        phase_a_watcher = ExactArtifactWatcher(
            phase_a_fixture.host_output, phase_a_fixture.host_marker
        )
        phase_b_watcher = ExactArtifactWatcher(
            phase_b_fixture.host_output, phase_b_fixture.host_marker
        )
        frigate = health.bind_observed_container(client, args.frigate_container)
        camera_expectations = health.load_camera_expectations(args.camera_expectations)
        kernel_journal = health.KernelJournalCursor.open_at_tail()
        kernel_initial = kernel_journal.snapshot()
        if kernel_initial.xid_matches != 0 or kernel_initial.continuous is not True:
            raise _safe_code("kernel journal baseline was not clean")
        gate_cgroup_parent = GateOwnedCgroupParent.create(
            client, candidate_item, candidate
        )
        health.start_bound_candidate(client, candidate, args)
        health.wait_for_running(
            client,
            candidate,
            args,
            time.monotonic() + args.start_timeout_seconds,
        )
        candidate_log = health.ContinuousCandidateLog.open(client, candidate)
        initial_status = _wait_for_initial_runtime(
            args, deadline=time.monotonic() + args.start_timeout_seconds
        )
        runtime_identity = initial_status["runtime_identity"]
        runtime_epoch = runtime_identity["epoch"]
        runtime_started_ns = runtime_identity["started_monotonic_ns"]
        journal = _open_runtime_journal(
            args.runtime_receipt_journal,
            runtime_epoch=runtime_epoch,
            gate_token_sha256=candidate.gate_token_digest,
            deadline=time.monotonic() + args.start_timeout_seconds,
        )
        cross_bind_runtime_status_receipt(initial_status, journal.receipts[-1])
        candidate_cgroup_probe = health.CandidateCgroupProbe(client, candidate, args)
        gate_cgroup_parent.bind_live(candidate_cgroup_probe)
        cgroup_probe = GateOwnedCgroupProbe(gate_cgroup_parent, candidate_cgroup_probe)
        baseline = capture_gate_failure_baseline(
            client=client,
            candidate=candidate,
            frigate=frigate,
            args=args,
            cgroup_probe=cgroup_probe,
            candidate_log=candidate_log,
            kernel_journal=kernel_journal,
            receipt=journal.receipts[-1],
        )
        if baseline.candidate_cuda_oom_log_matches != 0:
            raise _safe_code("candidate log baseline contained CUDA OOM")
        post_batch(
            phase_a_fixture.container_media,
            api_key=api_key,
            timeout=args.http_timeout_seconds,
        )
        phase_a_deadline = time.monotonic_ns() + int(
            args.long_timeout_seconds * 1_000_000_000
        )
        event0_receipt = _wait_for_receipt(
            journal,
            lambda receipt: (
                receipt["workload_sha256"] == phase_a_fixture.workload_sha256
                and receipt["active"] is True
                and receipt["chunk_uncommitted"] is True
                and receipt["model_resident"] is True
                and receipt["priority_state"] in {"clear", "neutral"}
                and receipt["controller_phase"] == "normal"
            ),
            deadline_ns=phase_a_deadline,
            label="Phase A admission",
        )
        protected = ProtectedPhaseASampleCollector()
        low_since = {name: None for name in camera_expectations}

        def protected_sample(receipt: dict[str, Any]) -> None:
            capture_phase_b_host_sample(
                baseline=baseline,
                client=client,
                candidate=candidate,
                frigate=frigate,
                args=args,
                cgroup_probe=cgroup_probe,
                candidate_log=candidate_log,
                kernel_journal=kernel_journal,
                receipt=receipt,
                camera_expectations=camera_expectations,
                camera_low_since=low_since,
            )
            protected.record(
                captured_monotonic_ns=time.monotonic_ns(),
                telemetry_valid=True,
                threshold_failed=False,
            )

        protected_sample(event0_receipt)
        assertion = _wait_for_assertion(
            args,
            journal=journal,
            after_sequence=event0_receipt["sequence"],
            deadline_ns=phase_a_deadline,
            heartbeat=protected_sample,
        )
        assertion_artifact = write_priority_assertion_observation(
            args.assertion_observation, assertion
        )
        collector = PhaseAEventCollector(
            runtime_epoch=runtime_epoch,
            runtime_started_monotonic_ns=runtime_started_ns,
            gate_token_sha256=candidate.gate_token_digest,
            workload_sha256=phase_a_fixture.workload_sha256,
            workload_identity=phase_a_fixture.workload_identity,
            policy_sha256=args.priority_policy_sha256,
            assertion=assertion,
            model_identity_sha256=catalog["model_identity_sha256"],
            allowed_unloaded_bytes=envelope["measurement"]["allowed_unloaded_bytes"],
        )
        _record_phase_a_event(
            collector,
            "pre_assertion",
            journal=journal,
            receipt=event0_receipt,
            fixture=phase_a_fixture,
            watcher=phase_a_watcher,
            cgroup_probe=cgroup_probe,
            gpu_uuid=policy["gpu_uuid"],
            threshold_masking_allowed=False,
        )
        predicates: list[tuple[str, Callable[[dict[str, Any]], bool], bool]] = [
            (
                "assertion_consumed",
                lambda r: (
                    r["priority_state"] == "asserted"
                    and r["active"]
                    and r["chunk_uncommitted"]
                    and r["model_resident"]
                    and r["controller_phase"] in {"yielding", "recovering"}
                ),
                False,
            ),
            (
                "yielded",
                lambda r: (
                    r["priority_state"] == "asserted"
                    and r["active"]
                    and not r["chunk_uncommitted"]
                    and r["model_resident"]
                    and r["controller_phase"] == "recovering"
                ),
                False,
            ),
            (
                "unloaded",
                lambda r: (
                    r["priority_state"] == "asserted"
                    and r["active"]
                    and not r["chunk_uncommitted"]
                    and not r["model_resident"]
                    and r["controller_phase"] == "recovering"
                ),
                False,
            ),
        ]
        prior_sequence = event0_receipt["sequence"]
        phase_receipts: dict[str, dict[str, Any]] = {}
        for kind, predicate, masking in predicates:
            receipt = _wait_for_receipt(
                journal,
                predicate,
                deadline_ns=(
                    assertion.t0_monotonic_ns
                    + (30 if kind == "unloaded" else 15) * 1_000_000_000
                ),
                label=f"Phase A {kind}",
                after_sequence=prior_sequence,
                heartbeat=protected_sample,
            )
            protected_sample(receipt)
            phase_receipts[kind] = receipt
            _record_phase_a_event(
                collector,
                kind,
                journal=journal,
                receipt=receipt,
                fixture=phase_a_fixture,
                watcher=phase_a_watcher,
                cgroup_probe=cgroup_probe,
                gpu_uuid=policy["gpu_uuid"],
                threshold_masking_allowed=masking,
            )
            prior_sequence = receipt["sequence"]
        unloaded_receipt = phase_receipts["unloaded"]
        event4 = _record_phase_a_event(
            collector,
            "unloaded_gpu",
            journal=journal,
            receipt=unloaded_receipt,
            fixture=phase_a_fixture,
            watcher=phase_a_watcher,
            cgroup_probe=cgroup_probe,
            gpu_uuid=policy["gpu_uuid"],
            threshold_masking_allowed=True,
            attribution_time=True,
        )
        if event4["monotonic_ns"] > assertion.t0_monotonic_ns + 45_000_000_000:
            raise _safe_code("Phase A host unloaded envelope deadline was exceeded")
        protected_sample(journal.receipts[-1])
        protected_proof = protected.proof(
            t0_monotonic_ns=assertion.t0_monotonic_ns,
            gpu_proof_monotonic_ns=event4["monotonic_ns"],
        )
        recovery_predicates: list[
            tuple[str, Callable[[dict[str, Any]], bool], bool]
        ] = [
            (
                "clear_1",
                lambda r: (
                    r["priority_state"] == "clear"
                    and r["distinct_clear_count"] == 1
                    and not r["model_resident"]
                ),
                True,
            ),
            (
                "clear_2",
                lambda r: (
                    r["priority_state"] == "clear"
                    and r["distinct_clear_count"] == 2
                    and not r["model_resident"]
                ),
                True,
            ),
            (
                "clear_3",
                lambda r: (
                    r["priority_state"] == "clear"
                    and r["distinct_clear_count"] == 3
                    and not r["model_resident"]
                ),
                True,
            ),
            (
                "reloaded",
                lambda r: (
                    r["priority_state"] == "clear"
                    and r["distinct_clear_count"] == 3
                    and r["model_resident"]
                    and r["active"]
                ),
                False,
            ),
            (
                "completed",
                lambda r: (
                    not r["active"]
                    and r["completed_cursor_ms"] == phase_a_fixture.duration_ms
                    and r["model_resident"]
                ),
                False,
            ),
        ]
        prior_sequence = unloaded_receipt["sequence"]
        final_receipt: dict[str, Any] = unloaded_receipt
        for kind, predicate, masking in recovery_predicates:
            final_receipt = _wait_for_receipt(
                journal,
                lambda r, inner=predicate: (
                    r["workload_sha256"] == phase_a_fixture.workload_sha256 and inner(r)
                ),
                deadline_ns=phase_a_deadline,
                label=f"Phase A {kind}",
                after_sequence=prior_sequence,
            )
            _record_phase_a_event(
                collector,
                kind,
                journal=journal,
                receipt=final_receipt,
                fixture=phase_a_fixture,
                watcher=phase_a_watcher,
                cgroup_probe=cgroup_probe,
                gpu_uuid=policy["gpu_uuid"],
                threshold_masking_allowed=masking,
            )
            prior_sequence = final_receipt["sequence"]
        events = collector.require_complete()
        failure_deltas = capture_gate_failure_deltas(
            baseline=baseline,
            client=client,
            candidate=candidate,
            frigate=frigate,
            args=args,
            cgroup_probe=cgroup_probe,
            candidate_log=candidate_log,
            kernel_journal=kernel_journal,
            receipt=final_receipt,
        )
        _require_zero_phase_failures(failure_deltas)
        phase_a_output = _read_private_runtime_output(phase_a_fixture.host_output)
        validate_srt_payload(
            phase_a_output,
            expected_duration_seconds=phase_a_fixture.duration_ms / 1000,
        )
        phase_a_receipts = journal.receipts[: final_receipt["sequence"]]
        phase_a_trace_document = build_phase_a_receipt_trace_document(
            receipts=phase_a_receipts,
            runtime_epoch=runtime_epoch,
            gate_token_sha256=candidate.gate_token_digest,
            workload_sha256=phase_a_fixture.workload_sha256,
            completion_event=events[-1],
        )
        phase_a_document = {
            "schema": "subgen.task11b.phase-a/v1",
            "outcome": "pass",
            "policy_sha256": args.priority_policy_sha256,
            "unloaded_gpu_envelope_sha256": args.unloaded_gpu_envelope_sha256,
            "workload_sha256": phase_a_fixture.workload_sha256,
            "workload_identity": phase_a_fixture.workload_identity,
            "candidate_identity_sha256": candidate_identity_sha256,
            "execution_boundary_manifest_sha256": boundary.file_sha256,
            "gate_receipt_trace_sha256": "0" * 64,
            "runtime_epoch": runtime_epoch,
            "runtime_started_monotonic_ns": runtime_started_ns,
            "assertion_reason_codes": assertion.attestation["reason_codes"],
            "assertion_observation_digest": assertion.attestation["observation_digest"],
            "assertion_observation_sha256": assertion_artifact.file_sha256,
            "assertion_observed_monotonic_ns": assertion.attestation[
                "observed_monotonic_ns"
            ],
            "t0_monotonic_ns": assertion.t0_monotonic_ns,
            "sealed_monotonic_ns": time.monotonic_ns(),
            "allowed_unloaded_bytes": envelope["measurement"]["allowed_unloaded_bytes"],
            "events": events,
            "final_output_sha256": hashlib.sha256(phase_a_output).hexdigest(),
            **protected_proof,
            "candidate_restart_delta": failure_deltas["candidate_restart_delta"],
            "candidate_oom_killed": failure_deltas["candidate_oom_killed"],
            "cgroup_oom_delta": failure_deltas["cgroup_oom_delta"],
            "cgroup_oom_kill_delta": failure_deltas["cgroup_oom_kill_delta"],
            "cgroup_oom_group_kill_delta": failure_deltas[
                "cgroup_oom_group_kill_delta"
            ],
            "runtime_cuda_oom_generation_delta": failure_deltas[
                "runtime_cuda_oom_generation_delta"
            ],
            "runtime_media_failure_generation_delta": failure_deltas[
                "runtime_media_failure_generation_delta"
            ],
            "candidate_cuda_oom_log_match_delta": failure_deltas[
                "candidate_cuda_oom_log_match_delta"
            ],
            "nvidia_xid_log_match_delta": failure_deltas["nvidia_xid_log_match_delta"],
        }
        phase_a_trace, phase_a_artifact = write_phase_a_artifacts(
            trace_path=args.phase_a_receipt_trace,
            seal_path=args.phase_a_seal,
            trace_document=phase_a_trace_document,
            phase_document=phase_a_document,
        )
        phase_a_durable_ns = time.monotonic_ns()
        verify_phase_a_receipt_bindings(
            phase_a_artifact.document,
            phase_a_trace.document,
            assertion_attestation=assertion.attestation,
            expected_model_identity_sha256=catalog["model_identity_sha256"],
        )
        phase_a_watcher.close()
        phase_a_watcher = None

        idle_receipt = _wait_for_receipt(
            journal,
            lambda r: (
                r["workload_sha256"] == phase_a_fixture.workload_sha256
                and not r["active"]
                and not r["chunk_uncommitted"]
                and r["completed_cursor_ms"] == phase_a_fixture.duration_ms
            ),
            deadline_ns=time.monotonic_ns() + 30_000_000_000,
            label="Phase B pre-admission idle",
            after_sequence=final_receipt["sequence"],
        )
        clear_signal = _validate_clear_signal(
            args, minimum_source_generation=events[7]["source_generation"] - 1
        )
        clear_observation_digest = hashlib.sha256(
            clear_signal["observation_id"].encode("ascii")
        ).hexdigest()
        reset_receipt = _wait_for_receipt(
            journal,
            lambda r: (
                r["workload_sha256"] == phase_a_fixture.workload_sha256
                and not r["active"]
                and r["priority_state"] == "clear"
                and r["controller_phase"] == "normal"
                and r["distinct_clear_count"] == 3
                and r["source_generation"] == clear_signal["source_generation"]
                and r["observation_digest"] == clear_observation_digest
            ),
            deadline_ns=time.monotonic_ns() + 15_000_000_000,
            label="Phase B exact-clear reset",
            after_sequence=idle_receipt["sequence"],
        )
        phase_b_lifecycle = PhaseBLifecycleOrder()
        (
            baseline_b,
            phase_b_cgroup_evidence,
            reset_completed_ns,
        ) = _capture_phase_b_reset_boundary(
            client=client,
            candidate=candidate,
            frigate=frigate,
            args=args,
            cgroup_probe=cgroup_probe,
            candidate_log=candidate_log,
            kernel_journal=kernel_journal,
            receipt=reset_receipt,
            lifecycle=phase_b_lifecycle,
        )
        post_batch(
            phase_b_fixture.container_media,
            api_key=api_key,
            timeout=args.http_timeout_seconds,
        )
        admission_b = _wait_for_receipt(
            journal,
            lambda r: (
                r["workload_sha256"] == phase_b_fixture.workload_sha256
                and r["active"]
                and r["priority_state"] == "clear"
                and r["controller_phase"] == "normal"
                and r["model_resident"]
            ),
            deadline_ns=time.monotonic_ns()
            + int(args.short_timeout_seconds * 1_000_000_000),
            label="Phase B workload admission",
            after_sequence=reset_receipt["sequence"],
        )
        started_b = max(time.monotonic_ns(), admission_b["observed_monotonic_ns"])
        phase_b_collector = PhaseBSampleCollector(
            started_monotonic_ns=started_b,
            runtime_epoch=runtime_epoch,
            runtime_started_monotonic_ns=runtime_started_ns,
            gate_token_sha256=candidate.gate_token_digest,
            workload_sha256=phase_b_fixture.workload_sha256,
            policy_sha256=args.priority_policy_sha256,
            producer_epoch=args.priority_producer_epoch,
            candidate_identity_sha256=candidate_identity_sha256,
            model_identity_sha256=catalog["model_identity_sha256"],
        )
        low_since_b = {name: None for name in camera_expectations}

        def capture_b(_index: int) -> dict[str, Any]:
            journal.read_available()
            latest = journal.receipts[-1]
            result = capture_phase_b_host_sample(
                baseline=baseline_b,
                client=client,
                candidate=candidate,
                frigate=frigate,
                args=args,
                cgroup_probe=cgroup_probe,
                candidate_log=candidate_log,
                kernel_journal=kernel_journal,
                receipt=latest,
                camera_expectations=camera_expectations,
                camera_low_since=low_since_b,
            )
            if phase_b_watcher.snapshot() != {
                "output_count": 0,
                "marker_count": 0,
                "output_create_count": 0,
                "marker_create_count": 0,
            }:
                raise _safe_code("Phase B output or marker appeared during observation")
            return result

        samples = run_phase_b_schedule(
            phase_b_collector, journal, capture_host_sample=capture_b
        )
        ended_b = time.monotonic_ns()
        sentinel = _wait_for_receipt(
            journal,
            lambda r: r["observed_monotonic_ns"] > ended_b,
            deadline_ns=ended_b + 15_000_000_000,
            label="Phase B post-end sentinel",
            after_sequence=admission_b["sequence"],
        )
        sentinel_prefix = journal.receipts[: sentinel["sequence"]]
        phase_b_collector.require_complete(
            ended_monotonic_ns=ended_b, receipts=sentinel_prefix
        )
        phase_b_trace_document = build_phase_b_receipt_trace_document(
            all_receipts=sentinel_prefix,
            runtime_epoch=runtime_epoch,
            gate_token_sha256=candidate.gate_token_digest,
            phase_a_trace_sha256=phase_a_trace.file_sha256,
            phase_a_last_sequence=phase_a_receipts[-1]["sequence"],
            workload_sha256=phase_b_fixture.workload_sha256,
            ended_monotonic_ns=ended_b,
        )
        phase_b_lifecycle.checkpoint("phase_b")
        phase_b_document = {
            "schema": "subgen.task11b.phase-b/v1",
            "outcome": "pass",
            "started_monotonic_ns": started_b,
            "ended_monotonic_ns": ended_b,
            "phase_a_seal_sha256": phase_a_artifact.file_sha256,
            "phase_a_durable_monotonic_ns": phase_a_durable_ns,
            "reset_completed_monotonic_ns": reset_completed_ns,
            "runtime_epoch": runtime_epoch,
            "runtime_started_monotonic_ns": runtime_started_ns,
            "sample_interval_seconds": 5,
            "policy_sha256": args.priority_policy_sha256,
            "producer_epoch_digest": hashlib.sha256(
                args.priority_producer_epoch.encode("ascii")
            ).hexdigest(),
            "producer_epoch": args.priority_producer_epoch,
            "candidate_identity_sha256": candidate_identity_sha256,
            "candidate_identity": identity,
            "execution_boundary_manifest_sha256": boundary.file_sha256,
            "workload_sha256": phase_b_fixture.workload_sha256,
            "workload_identity": phase_b_fixture.workload_identity,
            "gate_receipt_trace_sha256": "0" * 64,
            "model_identity_sha256": catalog["model_identity_sha256"],
            "samples": samples,
        }
        phase_b_trace, phase_b_artifact = write_phase_b_artifacts(
            trace_path=args.phase_b_receipt_trace,
            seal_path=args.phase_b_seal,
            trace_document=phase_b_trace_document,
            phase_document=phase_b_document,
        )
        verify_phase_b_receipt_bindings(
            phase_a_artifact.document,
            phase_b_artifact.document,
            phase_b_trace.document,
            expected_phase_a_sha256=phase_a_artifact.file_sha256,
            phase_a_trace=phase_a_trace.document,
        )
        phase_b_lifecycle.checkpoint("seal")
        journal.read_available()
        _validate_live_phase_b_failure_boundary(
            baseline=baseline_b,
            client=client,
            candidate=candidate,
            frigate=frigate,
            args=args,
            cgroup_probe=cgroup_probe,
            candidate_log=candidate_log,
            kernel_journal=kernel_journal,
            receipt=journal.receipts[-1],
            gpu_uuid=policy["gpu_uuid"],
        )
        phase_b_lifecycle.checkpoint("live_drain")
        if phase_b_watcher.snapshot() != {
            "output_count": 0,
            "marker_count": 0,
            "output_create_count": 0,
            "marker_create_count": 0,
        }:
            raise _safe_code("Phase B output or marker appeared before cleanup")
        cleanup_outcome = health.stop_bound_candidate(client, candidate, args)
        stopped_item = client.inspect(candidate.container_id)
        if not isinstance(stopped_item, dict):
            raise _safe_code("candidate disappeared before cleanup proof")
        health.verify_candidate_item(
            stopped_item,
            candidate,
            args,
            require_name=False,
            filesystem_check=True,
        )
        state = stopped_item.get("State")
        if (
            cleanup_outcome.get("verified_stopped") is not True
            or not isinstance(state, dict)
            or state.get("Running") is not False
            or state.get("Pid") != 0
        ):
            raise _safe_code("candidate cleanup proof was incomplete")
        phase_b_lifecycle.checkpoint("stop")
        final_candidate_log = candidate_log.close_after_stop()
        candidate_log = None
        phase_b_lifecycle.checkpoint("log_eof")
        _validate_stopped_candidate_log_boundary(
            baseline=baseline_b,
            client=client,
            frigate=frigate,
            stopped_item=stopped_item,
            candidate_log_snapshot=final_candidate_log,
        )
        phase_b_lifecycle.perform(
            "final_cgroup_drain",
            lambda: _validate_stopped_cgroup_boundary(
                baseline=baseline_b,
                cgroup_evidence=phase_b_cgroup_evidence,
            ),
        )
        phase_b_lifecycle.perform(
            "final_kernel_drain",
            lambda: _validate_stopped_kernel_boundary(
                baseline=baseline_b, kernel_journal=kernel_journal
            ),
        )
        phase_b_lifecycle.perform(
            "final_receipt_drain",
            lambda: _validate_stopped_receipt_boundary(
                baseline=baseline_b, journal=journal
            ),
        )
        phase_b_cgroup_evidence.close()
        phase_b_cgroup_evidence = None
        phase_b_lifecycle.perform("cgroup_cleanup", gate_cgroup_parent.cleanup)
        gate_cgroup_parent = None
        final_watcher_state = phase_b_lifecycle.perform(
            "watcher_snapshot", phase_b_watcher.snapshot
        )
        if final_watcher_state != {
            "output_count": 0,
            "marker_count": 0,
            "output_create_count": 0,
            "marker_create_count": 0,
        }:
            raise _safe_code("Phase B output or marker appeared during cleanup")
        phase_b_lifecycle.perform("watcher_close", phase_b_watcher.close)
        phase_b_watcher = None
        final = {
            "schema": "subgen.task11b.shared-gpu-gate/v3",
            "outcome": "pass",
            "runtime_commit": args.runtime_commit,
            "candidate_oci_index": args.candidate_oci_index,
            "candidate_config_digest": args.candidate_config_digest,
            "container_id_sha256": hashlib.sha256(
                candidate.container_id.encode("ascii")
            ).hexdigest(),
            "candidate_identity_record_sha256": candidate_record.file_sha256,
            "docker_daemon_identity_sha256": docker_daemon_identity_sha256,
            "layer_diff_ids_sha256": _canonical_document_sha256(
                identity["layer_diff_ids"], label="candidate layer diff IDs"
            ),
            "sampler_sha256": sampler_sha256,
            "sampler_test_sha256": args.sampler_test_sha256,
            "observer_sha256": observer_sha256,
            "observer_test_sha256": args.observer_test_sha256,
            "producer_sha256": args.producer_sha256,
            "policy_sha256": args.priority_policy_sha256,
            "model_envelope_catalog_sha256": args.model_envelope_catalog_sha256,
            "unloaded_gpu_envelope_sha256": args.unloaded_gpu_envelope_sha256,
            "execution_boundary_manifest_sha256": boundary.file_sha256,
            "phase_a_seal_sha256": phase_a_artifact.file_sha256,
            "phase_b_seal_sha256": phase_b_artifact.file_sha256,
            "cleanup": {
                "verified_stopped": True,
                "candidate_pid_count": 0,
                "execution_boundary_revalidated": True,
            },
        }
        phase_b_lifecycle.perform(
            "final_gate_write",
            lambda: health.write_final_gate_document(args.output, final),
        )
        phase_b_lifecycle.require_complete()
        passed = True
    except BaseException as exc:
        failure = exc
    finally:
        if not passed and candidate is not None:
            try:
                health.stop_bound_candidate(client, candidate, args)
            except BaseException:
                failure = _safe_code(
                    "runtime observer failed and cleanup was unverified"
                )
        if candidate_log is not None:
            try:
                item = client.inspect(candidate.container_id, missing_ok=True)
                if item is not None and item.get("State", {}).get("Running") is False:
                    candidate_log.close_after_stop()
            except BaseException:
                pass
        if phase_b_cgroup_evidence is not None:
            try:
                phase_b_cgroup_evidence.close()
            except BaseException as cgroup_cleanup_error:
                if failure is None:
                    failure = cgroup_cleanup_error
        if gate_cgroup_parent is not None:
            try:
                gate_cgroup_parent.cleanup()
            except BaseException as cgroup_parent_cleanup_error:
                failure = _safe_code(
                    "runtime observer failed and gate cgroup cleanup was unverified"
                )
                failure.__cause__ = cgroup_parent_cleanup_error
        for watcher in (phase_a_watcher, phase_b_watcher):
            if watcher is not None:
                try:
                    watcher.close()
                except BaseException:
                    pass
        if journal is not None:
            journal.close()
        if prior_handlers:
            health.restore_signal_handlers(prior_handlers)
    if not passed:
        if isinstance(failure, health.GateAbort):
            raise failure
        raise _safe_code("runtime observer failed closed") from failure
    print(
        "TASK11B_RUNTIME_WORKLOAD_PASS "
        f"container_id_sha256={hashlib.sha256(candidate.container_id.encode('ascii')).hexdigest()} "
        "phase_a_events=10 phase_b_samples=181"
    )
    return 0


def _create_verified_runtime_bundle(script_path: Path) -> tuple[Path, Path]:
    observer_payload = _BOOTSTRAPPED_OBSERVER_PAYLOAD
    sampler_payload = _BOOTSTRAPPED_SAMPLER_PAYLOAD
    if observer_payload is None or sampler_payload is None:
        raise _safe_code("verified runtime source payloads were unavailable")
    bundle = script_path.with_name(script_path.name + ".runtime")
    if any(character.isspace() for character in str(bundle)):
        raise _safe_code("supervisor artifact path contained whitespace")
    try:
        parent = bundle.parent.resolve(strict=True)
        parent_stat = parent.stat()
    except OSError as exc:
        raise _safe_code("supervisor artifact parent was unavailable") from exc
    if (
        parent != bundle.parent.absolute()
        or parent_stat.st_uid != os.geteuid()
        or parent_stat.st_mode & 0o077
    ):
        raise _safe_code("supervisor artifact parent was not owner only")
    try:
        os.mkdir(bundle, 0o700)
    except OSError as exc:
        raise _safe_code("supervisor runtime bundle already existed or failed") from exc
    observer_snapshot = bundle / "runtime_gate_observer.py"
    sampler_snapshot = bundle / "gate_health_sampler.py"
    try:
        item = bundle.lstat()
        if (
            stat.S_ISLNK(item.st_mode)
            or not stat.S_ISDIR(item.st_mode)
            or item.st_uid != os.geteuid()
            or stat.S_IMODE(item.st_mode) != 0o700
        ):
            raise _safe_code("supervisor runtime bundle was unsafe")
        health._write_private_create_only(observer_snapshot, observer_payload, 0o600)
        health._write_private_create_only(sampler_snapshot, sampler_payload, 0o600)
        if (
            hashlib.sha256(observer_payload).hexdigest()
            != _BOOTSTRAPPED_OBSERVER_SHA256
            or hashlib.sha256(sampler_payload).hexdigest()
            != _BOOTSTRAPPED_SAMPLER_SHA256
        ):
            raise _safe_code("supervisor runtime bundle identity changed")
        return observer_snapshot, sampler_snapshot
    except BaseException:
        for created in (observer_snapshot, sampler_snapshot):
            try:
                created.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            bundle.rmdir()
        except OSError:
            pass
        raise


def _remove_verified_runtime_bundle(observer_snapshot: Path) -> None:
    bundle = observer_snapshot.parent
    expected = {
        bundle / "runtime_gate_observer.py",
        bundle / "gate_health_sampler.py",
    }
    if (
        observer_snapshot.name != "runtime_gate_observer.py"
        or not bundle.name.endswith(".runtime")
        or observer_snapshot not in expected
    ):
        raise _safe_code("supervisor runtime bundle cleanup target was invalid")
    for target in expected:
        target.unlink(missing_ok=True)
    bundle.rmdir()


def _cleanup_command(args: argparse.Namespace, sampler_path: Path) -> list[str]:
    command = [
        str(Path(sys.executable).resolve()),
        str(sampler_path.resolve(strict=True)),
        *health._gate_cli_arguments(args),
        "--cleanup-only",
        "--systemd-stop-post",
    ]
    if any(not re.fullmatch(r"[A-Za-z0-9_./:=@+,%~-]+", part) for part in command):
        raise _safe_code("cleanup supervisor command contained unsafe characters")
    return command


def _cleanup_command_sha256(command: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(command, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _parse_exec_stop_post(value: str) -> dict[str, str]:
    """Parse one bounded systemd ExecStopPost command, never a command list."""
    if (
        not isinstance(value, str)
        or not value.startswith("{ ")
        or not value.endswith(" }")
        or value.count("{") != 1
        or value.count("}") != 1
        or len(value) > 64 * 1024
        or any(ord(character) < 32 or ord(character) > 126 for character in value)
    ):
        raise _safe_code("runtime supervisor cleanup command was malformed")
    body = value[2:-2]
    if body.endswith(" ;"):
        body = body[:-2]
    if not body:
        raise _safe_code("runtime supervisor cleanup command was empty")
    allowed = {
        "path",
        "argv[]",
        "ignore_errors",
        "start_time",
        "stop_time",
        "pid",
        "code",
        "status",
    }
    fields: dict[str, str] = {}
    for field in body.split(" ; "):
        if "=" not in field:
            raise _safe_code("runtime supervisor cleanup command was malformed")
        key, item = field.split("=", 1)
        if key not in allowed or key in fields or not item:
            raise _safe_code("runtime supervisor cleanup command was malformed")
        fields[key] = item
    if not {"path", "argv[]", "ignore_errors"}.issubset(fields):
        raise _safe_code("runtime supervisor cleanup command was incomplete")
    return fields


def _verify_exec_stop_post(value: str, cleanup: list[str]) -> None:
    fields = _parse_exec_stop_post(value)
    if (
        not cleanup
        or fields["path"] != cleanup[0]
        or fields["argv[]"] != " ".join(cleanup)
        or fields["ignore_errors"] != "no"
    ):
        raise _safe_code("runtime supervisor cleanup binding was not exact")


def verify_systemd_supervisor(args: argparse.Namespace) -> None:
    """Prove this PID is the generated unit with the exact cleanup command."""
    unit = args.expected_systemd_unit
    expected_unit = (
        "subgen-task11b-runtime-"
        + hashlib.sha256(args.gate_token.encode("utf-8")).hexdigest()[:16]
    )
    if unit != expected_unit:
        raise _safe_code("runtime supervisor unit identity was invalid")
    invocation_id = os.environ.get("INVOCATION_ID", "")
    systemd_exec_pid = os.environ.get("SYSTEMD_EXEC_PID", "")
    cleanup_path = Path(__file__).with_name("gate_health_sampler.py")
    cleanup = _cleanup_command(args, cleanup_path)
    cleanup_digest = _cleanup_command_sha256(cleanup)
    if (
        not re.fullmatch(r"[0-9a-f]{32}", invocation_id)
        or systemd_exec_pid != str(os.getpid())
        or os.environ.get("TASK11B_CLEANUP_COMMAND_SHA256") != cleanup_digest
    ):
        raise _safe_code("runtime supervisor process environment was invalid")

    try:
        descriptor = os.open(
            "/proc/self/cgroup",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise _safe_code("runtime supervisor cgroup was unavailable") from exc
    try:
        cgroup_payload = _read_all_fd(
            descriptor, 8 * 1024, label="runtime supervisor cgroup"
        )
    finally:
        os.close(descriptor)
    try:
        cgroup_lines = cgroup_payload.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise _safe_code("runtime supervisor cgroup was malformed") from exc
    expected_cgroup = f"/system.slice/{unit}.service"
    if cgroup_lines != [f"0::{expected_cgroup}"]:
        raise _safe_code("runtime process was outside its supervisor cgroup")

    result = health.bounded_command(
        [
            "/usr/bin/systemctl",
            "show",
            f"{unit}.service",
            "--no-pager",
            "--property=InvocationID",
            "--property=ControlGroup",
            "--property=MainPID",
            "--property=ActiveState",
            "--property=ExecStopPost",
        ],
        label="runtime supervisor inspection",
        timeout=10,
        max_bytes=64 * 1024,
    )
    properties: dict[str, str] = {}
    for line in result.output.splitlines():
        if "=" not in line:
            raise _safe_code("runtime supervisor inspection was malformed")
        key, value = line.split("=", 1)
        if key in properties:
            raise _safe_code("runtime supervisor inspection was duplicated")
        properties[key] = value
    if set(properties) != {
        "InvocationID",
        "ControlGroup",
        "MainPID",
        "ActiveState",
        "ExecStopPost",
    }:
        raise _safe_code("runtime supervisor inspection was incomplete")
    if (
        properties["InvocationID"] != invocation_id
        or properties["ControlGroup"] != expected_cgroup
        or properties["MainPID"] != str(os.getpid())
        or properties["ActiveState"] != "active"
    ):
        raise _safe_code("runtime supervisor cleanup binding was not exact")
    _verify_exec_stop_post(properties["ExecStopPost"], cleanup)


def _observer_cli_arguments(
    args: argparse.Namespace, *, supervisor_armed: bool = False
) -> list[str]:
    pairs: list[tuple[str, Any]] = [
        ("--duration-seconds", args.duration_seconds),
        ("--interval-seconds", args.interval_seconds),
        ("--start-timeout-seconds", args.start_timeout_seconds),
        ("--long-timeout-seconds", args.long_timeout_seconds),
        ("--short-timeout-seconds", args.short_timeout_seconds),
        ("--recovery-timeout-seconds", args.recovery_timeout_seconds),
        ("--reload-timeout-seconds", args.reload_timeout_seconds),
        ("--retention-timeout-seconds", args.retention_timeout_seconds),
        ("--http-timeout-seconds", args.http_timeout_seconds),
        ("--expected-memory-bytes", args.expected_memory_bytes),
        ("--expected-chunk-minutes", args.expected_chunk_minutes),
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
        ("--observer-sha256", args.observer_sha256),
        ("--expected-docker-daemon-id", args.expected_docker_daemon_id),
        ("--expected-host-boot-id", args.expected_host_boot_id),
        ("--boundary-manifest", args.boundary_manifest),
        ("--boundary-manifest-sha256", args.boundary_manifest_sha256),
        ("--disposable-root", args.disposable_root),
        ("--priority-signal", args.priority_signal),
        ("--priority-policy", args.priority_policy),
        ("--priority-policy-sha256", args.priority_policy_sha256),
        ("--priority-producer-epoch", args.priority_producer_epoch),
        ("--priority-boot-id-sha256", args.priority_boot_id_sha256),
        ("--runtime-receipt-journal", args.runtime_receipt_journal),
        ("--model-envelope-catalog", args.model_envelope_catalog),
        ("--unloaded-gpu-envelope", args.unloaded_gpu_envelope),
        ("--unloaded-gpu-envelope-sha256", args.unloaded_gpu_envelope_sha256),
        ("--phase-a-fixture-record", args.phase_a_fixture_record),
        ("--phase-b-fixture-record", args.phase_b_fixture_record),
        ("--candidate-identity-record", args.candidate_identity_record),
        ("--assertion-observation", args.assertion_observation),
        ("--phase-a-receipt-trace", args.phase_a_receipt_trace),
        ("--phase-a-seal", args.phase_a_seal),
        ("--phase-b-receipt-trace", args.phase_b_receipt_trace),
        ("--phase-b-seal", args.phase_b_seal),
        ("--sampler-test-sha256", args.sampler_test_sha256),
        ("--observer-test-sha256", args.observer_test_sha256),
        ("--producer-sha256", args.producer_sha256),
    ]
    if args.expected_systemd_unit is not None:
        pairs.append(("--expected-systemd-unit", args.expected_systemd_unit))
    if args.api_key_file is not None:
        pairs.append(("--api-key-file", args.api_key_file))
    result = [str(args.container), str(args.output)]
    for option, value in pairs:
        result.extend((option, str(value)))
    for diff_id in args.candidate_layer_diff_ids:
        result.extend(("--candidate-layer-diff-id", diff_id))
    if supervisor_armed:
        result.append("--supervisor-armed")
    return result


def emit_systemd_run_script(args: argparse.Namespace) -> int:
    """Create a wrapper whose ExecStopPost is the frozen sampler cleanup."""
    _verified_runtime_identities(args)
    _verify_adjacent_frozen_sampler()
    health.ensure_boundary_expectation(args)
    client = health.DockerClient(
        args.expected_docker_daemon_id, args.expected_host_boot_id
    )
    binding = health.bind_candidate(client, args)
    candidate_item = client.inspect(binding.container_id)
    if (
        not isinstance(candidate_item, dict)
        or candidate_item.get("Id") != binding.container_id
    ):
        raise _safe_code("runtime chunk policy candidate identity changed")
    validate_runtime_chunk_policy(
        candidate_item,
        expected_chunk_minutes=args.expected_chunk_minutes,
    )
    validate_request_isolation(candidate_item, _read_api_key(args.api_key_file))
    unit = f"subgen-task11b-runtime-{binding.gate_token_digest[:16]}"
    observer_snapshot, sampler_snapshot = _create_verified_runtime_bundle(
        args.emit_systemd_run_script
    )
    try:
        worker_args = copy.copy(args)
        worker_args.expected_systemd_unit = unit
        worker = [
            str(Path(sys.executable).resolve()),
            str(observer_snapshot),
            *_observer_cli_arguments(worker_args, supervisor_armed=True),
        ]
        cleanup = _cleanup_command(args, sampler_snapshot)
        cleanup_digest = _cleanup_command_sha256(cleanup)
        cleanup_property = "ExecStopPost=" + " ".join(
            health._systemd_quote(part) for part in cleanup
        )
        runtime_max = int(
            args.start_timeout_seconds
            + args.long_timeout_seconds
            + args.short_timeout_seconds
            + args.recovery_timeout_seconds
            + args.reload_timeout_seconds
            + 2 * args.retention_timeout_seconds
            + args.duration_seconds
            + 600
        )
        command = [
            "/usr/bin/systemd-run",
            f"--unit={unit}",
            "--collect",
            "--wait",
            "--service-type=exec",
            f"--setenv=TASK11B_CLEANUP_COMMAND_SHA256={cleanup_digest}",
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
        script = ("#!/bin/sh\nset -eu\nexec " + shlex.join(command) + "\n").encode(
            "utf-8"
        )
        health._write_private_create_only(args.emit_systemd_run_script, script, 0o700)
    except BaseException:
        _remove_verified_runtime_bundle(observer_snapshot)
        raise
    print(
        "TASK11B_RUNTIME_SUPERVISOR_READY "
        f"unit={unit} script_sha256={health.sha256_bytes(script)}"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("container", nargs="?")
    result.add_argument("output", nargs="?", type=Path)
    result.add_argument("--duration-seconds", type=int, default=900)
    result.add_argument("--interval-seconds", type=int, default=5)
    result.add_argument("--start-timeout-seconds", type=int, default=120)
    result.add_argument("--long-timeout-seconds", type=int, default=7200)
    result.add_argument("--short-timeout-seconds", type=int, default=1800)
    result.add_argument("--recovery-timeout-seconds", type=int, default=300)
    result.add_argument("--reload-timeout-seconds", type=int, default=1800)
    result.add_argument("--retention-timeout-seconds", type=int, default=120)
    result.add_argument("--http-timeout-seconds", type=float, default=10.0)
    result.add_argument("--expected-memory-bytes", type=int)
    result.add_argument("--expected-chunk-minutes", type=int)
    result.add_argument("--gpu-free-floor-bytes", type=int, default=8 * GIB)
    result.add_argument("--host-reserve-bytes", type=int, default=4 * GIB)
    result.add_argument("--frigate-container", default="frigate")
    result.add_argument("--frigate-stats-url", default=DEFAULT_FRIGATE_STATS_URL)
    result.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    result.add_argument("--candidate-status-url", default=DEFAULT_CANDIDATE_STATUS_URL)
    result.add_argument("--candidate-mode", choices=("runtime",), default="runtime")
    result.add_argument("--expected-model")
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
    result.add_argument("--observer-sha256")
    result.add_argument("--expected-docker-daemon-id")
    result.add_argument("--expected-host-boot-id")
    result.add_argument("--boundary-manifest", type=Path)
    result.add_argument("--boundary-manifest-sha256")
    result.add_argument("--disposable-root")
    result.add_argument("--api-key-file", type=Path)
    result.add_argument("--priority-signal", type=Path)
    result.add_argument("--priority-policy", type=Path)
    result.add_argument("--priority-policy-sha256")
    result.add_argument("--priority-producer-epoch")
    result.add_argument("--priority-boot-id-sha256")
    result.add_argument("--runtime-receipt-journal", type=Path)
    result.add_argument("--model-envelope-catalog", type=Path)
    result.add_argument("--unloaded-gpu-envelope", type=Path)
    result.add_argument("--unloaded-gpu-envelope-sha256")
    result.add_argument("--phase-a-fixture-record", type=Path)
    result.add_argument("--phase-b-fixture-record", type=Path)
    result.add_argument("--candidate-identity-record", type=Path)
    result.add_argument("--assertion-observation", type=Path)
    result.add_argument("--phase-a-receipt-trace", type=Path)
    result.add_argument("--phase-a-seal", type=Path)
    result.add_argument("--phase-b-receipt-trace", type=Path)
    result.add_argument("--phase-b-seal", type=Path)
    result.add_argument("--sampler-test-sha256")
    result.add_argument("--observer-test-sha256")
    result.add_argument("--producer-sha256")
    result.add_argument("--emit-systemd-run-script", type=Path)
    result.add_argument("--expected-systemd-unit", help=argparse.SUPPRESS)
    result.add_argument(
        "--supervisor-armed", action="store_true", help=argparse.SUPPRESS
    )
    return result


def release_parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="runtime_gate_observer.py verify-release",
        description="Strict offline verifier for the amended Task 11B release gate.",
    )
    result.add_argument("--evidence", type=Path, required=True)
    result.add_argument("--binding-prefix", required=True)
    result.add_argument("--gate-seal", type=Path, required=True)
    result.add_argument("--phase-a-seal", type=Path, required=True)
    result.add_argument("--phase-a-output", type=Path, required=True)
    result.add_argument("--phase-b-seal", type=Path, required=True)
    result.add_argument("--assertion-observation", type=Path, required=True)
    result.add_argument("--phase-a-receipt-trace", type=Path, required=True)
    result.add_argument("--phase-b-receipt-trace", type=Path, required=True)
    result.add_argument("--candidate-identity-record", type=Path, required=True)
    result.add_argument("--execution-boundary-manifest", type=Path, required=True)
    result.add_argument("--priority-policy", type=Path, required=True)
    result.add_argument("--unloaded-gpu-envelope", type=Path, required=True)
    result.add_argument("--model-envelope-catalog", type=Path, required=True)
    result.add_argument("--producer-source", type=Path, required=True)
    result.add_argument("--sampler-source", type=Path, required=True)
    result.add_argument("--sampler-test-source", type=Path, required=True)
    result.add_argument("--observer-test-source", type=Path, required=True)
    result.add_argument("--runtime-commit", required=True)
    result.add_argument("--candidate-oci-index", required=True)
    result.add_argument("--candidate-config-digest", required=True)
    return result


def verify_release(args: argparse.Namespace) -> int:
    """Read, independently validate, and cross-bind every amended gate artifact."""
    binding = _RELEASE_BINDING
    if binding is None:
        evidence_payload, _digest = _read_source_bytes_independently(
            args.evidence,
            maximum=MAX_OBSERVER_EVIDENCE_BYTES,
            label="release evidence",
        )
        binding = parse_sampler_binding(evidence_payload, args.binding_prefix)
    observer_payload, observer_sha256 = _read_source_bytes_independently(
        Path(__file__).resolve(strict=True),
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="release observer",
    )
    del observer_payload
    _sampler_payload, sampler_sha256 = _read_source_bytes_independently(
        args.sampler_source,
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="release sampler",
    )
    _sampler_test_payload, sampler_test_sha256 = _read_source_bytes_independently(
        args.sampler_test_source,
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="release sampler test",
    )
    _observer_test_payload, observer_test_sha256 = _read_source_bytes_independently(
        args.observer_test_source,
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="release observer test",
    )
    _producer_payload, producer_sha256 = _read_source_bytes_independently(
        args.producer_source,
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="priority producer",
    )
    if (
        observer_sha256 != binding["observer_sha256"]
        or sampler_sha256 != binding["sampler_sha256"]
        or sampler_test_sha256 != binding["test_sha256"]
        or observer_test_sha256 != binding["observer_test_sha256"]
        or producer_sha256 != binding["producer_sha256"]
    ):
        raise _safe_code("release source bytes changed after bootstrap")

    load = health.load_canonical_artifact
    final = load(
        args.gate_seal,
        validator=health.validate_final_gate_document,
        expected_sha256=binding["gate_seal_sha256"],
    )
    candidate_record = load(
        args.candidate_identity_record,
        validator=health.validate_candidate_identity_document,
        expected_sha256=final["candidate_identity_record_sha256"],
    )
    phase_a = load(
        args.phase_a_seal,
        validator=health.validate_phase_a_document,
        expected_sha256=final["phase_a_seal_sha256"],
        max_bytes=2 * MIB,
    )
    phase_b = load(
        args.phase_b_seal,
        validator=health.validate_phase_b_document,
        expected_sha256=final["phase_b_seal_sha256"],
        max_bytes=4 * MIB,
    )
    phase_a_trace = load(
        args.phase_a_receipt_trace,
        validator=health.validate_runtime_receipt_trace_document,
        expected_sha256=phase_a["gate_receipt_trace_sha256"],
        max_bytes=MAX_RUNTIME_RECEIPT_JOURNAL_BYTES,
    )
    phase_b_trace = load(
        args.phase_b_receipt_trace,
        validator=health.validate_phase_b_receipt_trace_document,
        expected_sha256=phase_b["gate_receipt_trace_sha256"],
        max_bytes=MAX_RUNTIME_RECEIPT_JOURNAL_BYTES,
    )

    policy_sha256 = binding["policy_sha256"]

    def policy_validator(document: dict[str, Any]) -> dict[str, Any]:
        payload = _canonical_ascii_json_line(document, label="priority policy")
        return validate_priority_policy(
            document,
            payload,
            expected_file_sha256=policy_sha256,
        )

    policy = load(
        args.priority_policy,
        validator=policy_validator,
        expected_sha256=policy_sha256,
        max_bytes=32 * 1024,
    )
    identity = candidate_record["candidate_identity"]
    envelope_sha256 = binding["unloaded_gpu_envelope_sha256"]

    def envelope_validator(document: dict[str, Any]) -> dict[str, Any]:
        payload = _canonical_ascii_json_line(document, label="unloaded GPU envelope")
        return validate_unloaded_gpu_envelope(
            document,
            payload,
            expected_file_sha256=envelope_sha256,
            expected_policy_sha256=policy_sha256,
            expected_runtime_commit=args.runtime_commit,
            expected_oci_index=args.candidate_oci_index,
            expected_config_digest=args.candidate_config_digest,
            expected_layer_diff_ids=identity["layer_diff_ids"],
        )

    unloaded = load(
        args.unloaded_gpu_envelope,
        validator=envelope_validator,
        expected_sha256=envelope_sha256,
        max_bytes=512 * 1024,
    )

    catalog_payload = _require_private_file(
        args.model_envelope_catalog,
        maximum=4 * MIB,
        label="model envelope catalog",
    )
    catalog_document = _strict_canonical_json_document(
        catalog_payload,
        label="model envelope catalog",
        maximum=4 * MIB,
        trailing_newline=False,
    )
    catalog_attestation = validate_model_envelope_catalog(
        catalog_document,
        catalog_payload,
        expected_file_sha256=binding["model_envelope_catalog_sha256"],
        candidate_config_digest=identity["config_digest"],
        candidate_layer_diff_ids=identity["layer_diff_ids"],
        unloaded_envelope=unloaded,
    )

    assertion_payload = _require_private_file(
        args.assertion_observation,
        maximum=MAX_PRIORITY_SIGNAL_BYTES,
        label="priority assertion observation",
    )
    assertion_document = _strict_json_line(
        assertion_payload,
        label="priority assertion observation",
        maximum=MAX_PRIORITY_SIGNAL_BYTES,
    )
    assertion_attestation = validate_priority_assertion(
        assertion_document,
        assertion_payload,
        expected_policy_sha256=policy_sha256,
    )
    if (
        hashlib.sha256(assertion_payload).hexdigest()
        != phase_a["assertion_observation_sha256"]
    ):
        raise _safe_code("priority assertion bytes did not match Phase A")

    boundary = load(
        args.execution_boundary_manifest,
        validator=lambda document: validate_execution_boundary_document(
            document,
            candidate_record=candidate_record,
            phase_a=phase_a,
            phase_b=phase_b,
            expected_catalog_sha256=binding["model_envelope_catalog_sha256"],
        ),
        expected_sha256=binding["execution_boundary_manifest_sha256"],
        max_bytes=4 * MIB,
    )
    output_payload = _require_private_file(
        args.phase_a_output,
        maximum=MAX_SUBTITLE_BYTES,
        label="Phase A final subtitle",
    )
    output_sha256 = hashlib.sha256(output_payload).hexdigest()
    if output_sha256 != phase_a["final_output_sha256"]:
        raise _safe_code("Phase A final subtitle hash did not match")
    validate_srt_payload(
        output_payload,
        expected_duration_seconds=(
            phase_a["workload_identity"]["total_duration_ms"] / 1000
        ),
    )

    if (
        policy["gpu_uuid"] != unloaded["gpu"]["uuid"]
        or policy["nvidia_driver_version"] != unloaded["gpu"]["driver_version"]
        or policy["gpu_index"] != unloaded["model_policy"]["device_index"]
        or phase_a["allowed_unloaded_bytes"]
        != unloaded["measurement"]["allowed_unloaded_bytes"]
        or assertion_document["producer_epoch"] != phase_b["producer_epoch"]
        or phase_a_trace["gate_token_sha256"] != candidate_record["gate_token_sha256"]
        or phase_b_trace["gate_token_sha256"] != candidate_record["gate_token_sha256"]
    ):
        raise _safe_code("release policy device epoch or gate-token binding failed")
    verify_phase_a_receipt_bindings(
        phase_a,
        phase_a_trace,
        assertion_attestation=assertion_attestation,
        expected_model_identity_sha256=catalog_attestation["model_identity_sha256"],
    )
    verify_phase_b_receipt_bindings(
        phase_a,
        phase_b,
        phase_b_trace,
        expected_phase_a_sha256=final["phase_a_seal_sha256"],
        phase_a_trace=phase_a_trace,
    )
    hashes = {
        "final": binding["gate_seal_sha256"],
        "phase_a": final["phase_a_seal_sha256"],
        "phase_b": final["phase_b_seal_sha256"],
        "candidate": final["candidate_identity_record_sha256"],
        "boundary": binding["execution_boundary_manifest_sha256"],
        "policy": policy_sha256,
        "envelope": envelope_sha256,
        "catalog": binding["model_envelope_catalog_sha256"],
        "assertion": phase_a["assertion_observation_sha256"],
        "phase_a_trace": phase_a["gate_receipt_trace_sha256"],
        "phase_b_trace": phase_b["gate_receipt_trace_sha256"],
        "output": output_sha256,
        "observer": observer_sha256,
        "sampler": sampler_sha256,
        "sampler_test": sampler_test_sha256,
        "observer_test": observer_test_sha256,
        "producer": producer_sha256,
    }
    verify_release_cross_bindings(
        binding=binding,
        final=final,
        phase_a=phase_a,
        phase_b=phase_b,
        candidate_record=candidate_record,
        boundary=boundary,
        catalog_attestation=catalog_attestation,
        hashes=hashes,
        expected_runtime_commit=args.runtime_commit,
        expected_oci_index=args.candidate_oci_index,
        expected_config_digest=args.candidate_config_digest,
    )
    print("TASK11B_RELEASE_VERIFY_OK")
    return 0


def validate_args(args: argparse.Namespace) -> None:
    # Supply the sampler-only fields its exact runtime validator expects.
    args.expected_profiler_returncode = None
    args.leave_running_on_pass = False
    args.cleanup_only = False
    args.systemd_stop_post = False
    args.emit_boundary_manifest = None
    health.validate_args(args)
    required_paths = (
        args.api_key_file,
        args.priority_signal,
        args.priority_policy,
        args.runtime_receipt_journal,
        args.model_envelope_catalog,
        args.unloaded_gpu_envelope,
        args.phase_a_fixture_record,
        args.phase_b_fixture_record,
        args.candidate_identity_record,
        args.assertion_observation,
        args.phase_a_receipt_trace,
        args.phase_a_seal,
        args.phase_b_receipt_trace,
        args.phase_b_seal,
    )
    required_hashes = (
        args.priority_policy_sha256,
        args.priority_boot_id_sha256,
        args.unloaded_gpu_envelope_sha256,
        args.sampler_test_sha256,
        args.observer_test_sha256,
        args.producer_sha256,
    )
    if (
        args.observer_sha256 is None
        or any(path is None for path in required_paths)
        or any(value is None for value in required_hashes)
    ):
        raise _safe_code("missing runtime observer arguments")
    if not SHA256_RE.fullmatch(args.observer_sha256):
        raise _safe_code("observer checksum must be SHA256")
    if any(not path.is_absolute() for path in required_paths):
        raise _safe_code("runtime observer paths must be absolute")
    if any(
        not isinstance(value, str) or LOWER_HEX_64_RE.fullmatch(value) is None
        for value in required_hashes
    ):
        raise _safe_code("runtime observer identity checksum was invalid")
    if (
        not isinstance(args.priority_producer_epoch, str)
        or LOWER_HEX_32_RE.fullmatch(args.priority_producer_epoch) is None
        or args.gate_role != "runtime-auto"
        or args.duration_seconds != 900
        or args.interval_seconds != 5
    ):
        raise _safe_code("runtime observer phase identity or cadence was invalid")
    if (
        isinstance(args.expected_chunk_minutes, bool)
        or not isinstance(args.expected_chunk_minutes, int)
        or not 5 <= args.expected_chunk_minutes <= 30
    ):
        raise _safe_code("expected runtime chunk policy was outside boundary")
    bounds = (
        (args.long_timeout_seconds, 600, 10800, "long timeout"),
        (args.short_timeout_seconds, 60, 3600, "short timeout"),
        (args.recovery_timeout_seconds, 30, 900, "recovery timeout"),
        (args.reload_timeout_seconds, 60, 3600, "reload timeout"),
        (args.retention_timeout_seconds, 30, 600, "retention timeout"),
    )
    for value, minimum, maximum, label in bounds:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise _safe_code(f"{label} was outside safety boundary")
    if not 1.0 <= args.http_timeout_seconds <= 30.0:
        raise _safe_code("HTTP timeout was outside safety boundary")
    if (
        args.emit_systemd_run_script is not None
        and not args.emit_systemd_run_script.is_absolute()
    ):
        raise _safe_code("supervisor script path must be absolute")
    if args.emit_systemd_run_script is not None:
        if args.expected_systemd_unit is not None or args.supervisor_armed:
            raise _safe_code("supervisor generation arguments were inconsistent")
    elif (
        not args.supervisor_armed
        or not isinstance(args.expected_systemd_unit, str)
        or not re.fullmatch(
            r"subgen-task11b-runtime-[0-9a-f]{16}", args.expected_systemd_unit
        )
    ):
        raise _safe_code("runtime observer requires its frozen cleanup supervisor")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["verify-release"]:
        release_args = release_parser().parse_args(arguments[1:])
        _bootstrap_release_runtime(release_args)
        return verify_release(release_args)
    _bootstrap_verified_runtime(arguments)
    args = parser().parse_args(arguments)
    validate_args(args)
    if args.emit_systemd_run_script is not None:
        return emit_systemd_run_script(args)
    verify_systemd_supervisor(args)
    return run_observer(args)


def cli_entrypoint(argv: list[str] | None = None) -> int:
    """Run the CLI without allowing private verifier state into a traceback."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    release_mode = arguments[:1] == ["verify-release"]
    prefix = "TASK11B_RELEASE_VERIFY" if release_mode else "TASK11B_RUNTIME_WORKLOAD"
    try:
        return main(arguments)
    except Exception as exc:
        gate_abort = health is not None and isinstance(exc, health.GateAbort)
        if isinstance(exc, ObserverBootstrapAbort) or gate_abort:
            reason = exc.code
        else:
            reason = "internal_verifier_failure"
        print(f"{prefix}_ABORT reason={reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli_entrypoint())
