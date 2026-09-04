"""Pure canonical evidence contract for the private Task 11B 72-hour soak."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

MIB = 1024**2
# Explicit MQTT health makes every five-second sample larger. Keep the journal
# bounded while preserving practical headroom for many legitimate workload events.
MAX_JOURNAL_BYTES = 96 * MIB
MAX_RECORD_BYTES = 2 * MIB
MIN_DURATION_NS = 72 * 60 * 60 * 1_000_000_000
INTERVAL_NS = 5_000_000_000
MAX_LAG_NS = 2_000_000_000
MIN_GAP_NS = 3_000_000_000
MAX_GAP_NS = 7_000_000_000
UTC_DRIFT_NS = 2_000_000_000
MAX_NON_NORMAL_NS = 300_000_000_000
BINDING_PREFIX = "Task-11B-Soak-Binding: "

HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
OCI = re.compile(r"^sha256:[0-9a-f]{64}$")
REVISION = re.compile(r"^hf:[0-9a-f]{40}$")
CONTAINER = re.compile(r"^[0-9a-f]{64}$")
UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
MODELS = {"tiny", "base", "small", "medium", "large-v3"}

IMAGE_KEYS = {"runtime_commit", "oci_index", "config_digest", "layer_diff_ids_sha256"}
MODEL_KEYS = {"selected_model", "model_revision", "model_identity_sha256", "catalog_sha256"}
CONFIG_KEYS = {"policy_sha256", "runtime_config_sha256", "frigate_config_sha256", "monitored_config_sha256"}
DEPLOYMENT_KEYS = {
    "host_boot_id_sha256", "docker_daemon_identity_sha256",
    "container_id_sha256", "frigate_container_id_sha256",
}
ARTIFACT_KEYS = {"gate_seal_sha256", "candidate_identity_record_sha256", "evidence_sha256", "observer_sha256", "observer_test_sha256"}
IDENTITIES_KEYS = {"image", "model", "configuration", "deployment", "artifacts"}
ROLLBACK_KEYS = {"ready", "target_version", "deletion_disabled", "repair_report_only", "record_sha256"}
MQTT_BINDING_KEYS = {
    "enabled", "semantic_config_sha256", "refresh_seconds", "library_label_policy",
}
MQTT_HEALTH_ENABLED_KEYS = {
    "enabled", "availability_retained", "availability_online",
    "discovery_exact_retained", "state_retained", "state_fresh",
    "state_parseable", "state_fields_valid",
}
MQTT_RECORD_KEYS = {
    *MQTT_BINDING_KEYS, "outcome", "observation_count", "healthy_all",
}

FAILURE_COUNTERS = (
    "candidate_restart",
    "frigate_restart",
    "cgroup_oom",
    "cgroup_oom_kill",
    "cgroup_oom_group_kill",
    "cgroup_max",
    "runtime_cuda_oom",
    "candidate_cuda_oom_log",
    "nvidia_xid",
    "candidate_health_breach",
    "frigate_health_breach",
    "identity_drift",
    "config_drift",
    "transcription_failure",
    "join_failure",
    "partial_output",
    "unresolved_yield",
    "media_deleted",
)
ALLOWED_COUNTERS = (
    "marker_created",
    "marker_skipped",
    "marker_handled",
    "cooperative_yield",
    "cooperative_recovery",
)
COUNTER_KEYS = set((*FAILURE_COUNTERS, *ALLOWED_COUNTERS))

HEADER_KEYS = {
    "schema", "record_index", "soak_id", "started_utc", "started_monotonic_ns",
    "interval_ns", "max_gap_ns", "deletion_enabled", "identities", "rollback_record_sha256",
    "mqtt_inventory",
}
SAMPLE_KEYS = {
    "schema", "record_index", "previous_record_sha256", "sample_index",
    "scheduled_monotonic_ns", "captured_monotonic_ns", "captured_utc", "identity_sha256",
    "candidate_running", "candidate_oom_killed", "frigate_healthy", "deletion_enabled",
    "active", "chunk_uncommitted", "completion_generation", "controller_phase", "counters",
    "mqtt_inventory",
}
WORKLOAD_KEYS = {
    "schema", "record_index", "previous_record_sha256", "captured_monotonic_ns",
    "captured_utc", "source_utc", "source_event_sha256", "event_sequence", "source_monotonic_ns",
    "workload_id_sha256", "chunks_total", "atomic_publish", "outcome", "source_stream",
}
END_KEYS = {
    "schema", "record_index", "previous_record_sha256", "ended_monotonic_ns",
    "ended_utc", "outcome", "rollback",
}
TIMING_KEYS = {
    "started_utc", "ended_utc", "started_monotonic_ns", "ended_monotonic_ns",
    "duration_ns", "interval_ns", "sample_count", "maximum_lag_ns", "maximum_gap_ns",
}
JOURNAL_KEYS = {"sha256", "byte_count", "record_count", "first_record_sha256", "last_record_sha256"}
TRANSCRIPTION_KEYS = {
    "completion_generation_start", "completion_generation_end", "completion_delta",
    "successful_completion_count", "long_multichunk_completion_count", "atomic_join_count",
}
MARKER_KEYS = {"created_count", "skipped_count", "handled_count", "deleted_count", "deletion_enabled_observed"}
HEALTH_KEYS = {"candidate_running_all", "candidate_oom_killed_any", "frigate_healthy_all", "failure_counter_deltas", "cooperative_yield_count", "cooperative_recovery_count"}
RECORD_KEYS = {"schema", "outcome", "soak_id", "identities", "timing", "journal", "transcription", "markers", "health", "mqtt_inventory", "rollback"}
BINDING_KEYS = {
    "schema", "soak_record_sha256", "soak_journal_sha256", "gate_seal_sha256",
    "candidate_identity_record_sha256", "runtime_commit", "candidate_oci_index",
    "candidate_config_digest", "layer_diff_ids_sha256", "model_envelope_catalog_sha256",
    "model_identity_sha256", "policy_sha256", "runtime_config_sha256",
    "frigate_config_sha256", "monitored_config_sha256", "soak_evidence_sha256", "soak_observer_sha256",
    "soak_observer_test_sha256", "started_utc", "ended_utc", "duration_ns",
    "outcome", "rollback_ready", "host_boot_id_sha256",
    "docker_daemon_identity_sha256", "candidate_container_id_sha256",
    "frigate_container_id_sha256", "mqtt_inventory_enabled",
    "mqtt_semantic_config_sha256",
}

GATE_KEYS = {
    "schema", "outcome", "runtime_commit", "candidate_oci_index", "candidate_config_digest",
    "container_id_sha256", "candidate_identity_record_sha256", "docker_daemon_identity_sha256",
    "layer_diff_ids_sha256", "sampler_sha256", "sampler_test_sha256", "observer_sha256",
    "observer_test_sha256", "producer_sha256", "policy_sha256",
    "model_envelope_catalog_sha256", "unloaded_gpu_envelope_sha256",
    "execution_boundary_manifest_sha256", "phase_a_seal_sha256", "phase_b_seal_sha256",
    "profiler_chain_sha256", "cleanup",
}
CANDIDATE_KEYS = {"schema", "candidate_identity", "docker_daemon_identity_sha256", "execution_boundary_manifest_sha256", "gate_token_sha256", "intended_command_sha256", "created_stopped"}
CANDIDATE_INNER_KEYS = {"container_id", "runtime_commit", "oci_index", "config_digest", "layer_diff_ids", "selected_model", "model_revision"}


class EvidenceError(RuntimeError):
    def __init__(self, message: str) -> None:
        self.code = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")[:96] or "evidence_error"
        super().__init__(self.code)


@dataclass(frozen=True)
class JournalView:
    header: dict[str, Any]
    samples: tuple[dict[str, Any], ...]
    workloads: tuple[dict[str, Any], ...]
    end: dict[str, Any] | None
    payload_sha256: str
    first_sha256: str
    last_sha256: str
    maximum_lag_ns: int
    maximum_gap_ns: int
    record_count: int


def canonical_line(value: Any) -> bytes:
    try:
        return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise EvidenceError("value was not canonicalizable") from exc


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def strict_line(payload: bytes, maximum: int = 64 * 1024) -> dict[str, Any]:
    if not isinstance(payload, bytes) or not payload or len(payload) > maximum:
        raise EvidenceError("canonical line size was invalid")
    try:
        value = json.loads(payload.decode("ascii"), object_pairs_hook=_unique, parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise EvidenceError("canonical line was malformed") from exc
    if not isinstance(value, dict) or canonical_line(value) != payload:
        raise EvidenceError("canonical line bytes disagreed")
    return value


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_sha(value: Any) -> str:
    return sha(canonical_line(value))


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise EvidenceError(f"{label} keys were invalid")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise EvidenceError(f"{label} integer was invalid")
    return value


def _hex(value: Any, label: str) -> str:
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        raise EvidenceError(f"{label} sha256 was invalid")
    return value


def _oci(value: Any, label: str) -> str:
    if not isinstance(value, str) or OCI.fullmatch(value) is None:
        raise EvidenceError(f"{label} digest was invalid")
    return value


def _utc(value: Any, label: str) -> int:
    if not isinstance(value, str) or UTC.fullmatch(value) is None:
        raise EvidenceError(f"{label} timestamp was invalid")
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise EvidenceError(f"{label} timestamp was invalid") from exc
    return int(parsed.timestamp() * 1_000_000_000)


def validate_identities(value: Any) -> dict[str, Any]:
    ids = _exact(value, IDENTITIES_KEYS, "identity")
    image = _exact(ids["image"], IMAGE_KEYS, "image identity")
    model = _exact(ids["model"], MODEL_KEYS, "model identity")
    config = _exact(ids["configuration"], CONFIG_KEYS, "configuration identity")
    deployment = _exact(ids["deployment"], DEPLOYMENT_KEYS, "deployment identity")
    artifacts = _exact(ids["artifacts"], ARTIFACT_KEYS, "artifact identity")
    if not isinstance(image["runtime_commit"], str) or HEX40.fullmatch(image["runtime_commit"]) is None:
        raise EvidenceError("runtime commit was invalid")
    _oci(image["oci_index"], "image index"); _oci(image["config_digest"], "image config")
    _hex(image["layer_diff_ids_sha256"], "image layers")
    if model["selected_model"] not in MODELS or not isinstance(model["model_revision"], str) or REVISION.fullmatch(model["model_revision"]) is None:
        raise EvidenceError("model selection was invalid")
    for item, prefix in ((model, "model"), (config, "configuration"), (deployment, "deployment"), (artifacts, "artifact")):
        for key, field in item.items():
            if key not in {"selected_model", "model_revision"}:
                _hex(field, f"{prefix} {key}")
    return ids


def validate_rollback(value: Any, expected_hash: str | None = None) -> dict[str, Any]:
    rollback = _exact(value, ROLLBACK_KEYS, "rollback")
    if rollback != {
        "ready": True, "target_version": "0.3.0", "deletion_disabled": True,
        "repair_report_only": True, "record_sha256": rollback.get("record_sha256"),
    }:
        raise EvidenceError("rollback readiness was invalid")
    _hex(rollback["record_sha256"], "rollback record")
    if expected_hash is not None and rollback["record_sha256"] != expected_hash:
        raise EvidenceError("rollback record changed")
    return rollback


def validate_mqtt_binding(value: Any) -> dict[str, Any]:
    binding = _exact(value, MQTT_BINDING_KEYS, "MQTT inventory binding")
    if type(binding["enabled"]) is not bool:
        raise EvidenceError("MQTT inventory enabled state was invalid")
    _hex(binding["semantic_config_sha256"], "MQTT semantic configuration")
    if binding["refresh_seconds"] != 60 or type(binding["refresh_seconds"]) is not int:
        raise EvidenceError("MQTT inventory refresh was not fixed at 60 seconds")
    if binding["library_label_policy"] not in {"generic", "custom"}:
        raise EvidenceError("MQTT inventory library label policy was invalid")
    if not binding["enabled"] and binding["library_label_policy"] != "generic":
        raise EvidenceError("disabled MQTT inventory had a custom label policy")
    return binding


def validate_mqtt_health(value: Any, binding: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise EvidenceError("MQTT inventory sample was invalid")
    if value["enabled"] != binding["enabled"]:
        raise EvidenceError("MQTT inventory enabled state drifted")
    if not binding["enabled"]:
        if value != {"enabled": False}:
            raise EvidenceError("disabled MQTT inventory sample was invalid")
        return value
    health = _exact(value, MQTT_HEALTH_ENABLED_KEYS, "MQTT inventory sample")
    if any(type(health[key]) is not bool for key in MQTT_HEALTH_ENABLED_KEYS):
        raise EvidenceError("MQTT inventory sample state was not boolean")
    if not all(health[key] for key in MQTT_HEALTH_ENABLED_KEYS):
        raise EvidenceError("MQTT inventory health proof failed")
    return health


def validate_header(value: Any) -> dict[str, Any]:
    header = _exact(value, HEADER_KEYS, "journal header")
    if header["schema"] != "subgen.task11b.soak-start/v1" or header["record_index"] != 0 or type(header["record_index"]) is not int:
        raise EvidenceError("journal header was invalid")
    if not isinstance(header["soak_id"], str) or HEX32.fullmatch(header["soak_id"]) is None:
        raise EvidenceError("soak id was invalid")
    _utc(header["started_utc"], "start"); _integer(header["started_monotonic_ns"], "start monotonic", 1)
    if header["interval_ns"] != INTERVAL_NS or type(header["interval_ns"]) is not int or header["max_gap_ns"] != MAX_GAP_NS or type(header["max_gap_ns"]) is not int or header["deletion_enabled"] is not False:
        raise EvidenceError("journal cadence or deletion policy was invalid")
    validate_identities(header["identities"]); _hex(header["rollback_record_sha256"], "rollback record")
    validate_mqtt_binding(header["mqtt_inventory"])
    return header


def validate_sample(value: Any, identity_sha256: str, mqtt_binding: dict[str, Any]) -> dict[str, Any]:
    sample = _exact(value, SAMPLE_KEYS, "sample")
    if sample["schema"] != "subgen.task11b.soak-sample/v1":
        raise EvidenceError("sample schema was invalid")
    for key in ("record_index", "sample_index", "scheduled_monotonic_ns", "captured_monotonic_ns", "completion_generation"):
        _integer(sample[key], f"sample {key}")
    _hex(sample["previous_record_sha256"], "sample previous record")
    _utc(sample["captured_utc"], "sample capture")
    if sample["identity_sha256"] != identity_sha256:
        raise EvidenceError("sample identity or configuration drifted")
    for key in ("candidate_running", "candidate_oom_killed", "frigate_healthy", "deletion_enabled", "active", "chunk_uncommitted"):
        if type(sample[key]) is not bool:
            raise EvidenceError("sample state was not boolean")
    if sample["controller_phase"] not in {"normal", "yielding", "recovering"}:
        raise EvidenceError("sample controller phase was invalid")
    counters = _exact(sample["counters"], COUNTER_KEYS, "sample counters")
    for key, item in counters.items(): _integer(item, f"counter {key}")
    validate_mqtt_health(sample["mqtt_inventory"], mqtt_binding)
    if not sample["candidate_running"] or sample["candidate_oom_killed"] or not sample["frigate_healthy"] or sample["deletion_enabled"]:
        raise EvidenceError("sample health or deletion policy failed")
    return sample


def validate_workload(value: Any) -> dict[str, Any]:
    event = _exact(value, WORKLOAD_KEYS, "workload event")
    if event["schema"] != "subgen.task11b.soak-workload/v1":
        raise EvidenceError("workload schema was invalid")
    _integer(event["record_index"], "workload record index", 1)
    _integer(event["captured_monotonic_ns"], "workload captured monotonic", 1)
    _integer(event["event_sequence"], "workload event sequence", 1)
    _integer(event["source_monotonic_ns"], "workload source monotonic")
    _integer(event["chunks_total"], "workload chunk total", 2)
    _utc(event["captured_utc"], "workload capture"); _utc(event["source_utc"], "workload source")
    for key in ("previous_record_sha256", "source_event_sha256", "workload_id_sha256"):
        _hex(event[key], f"workload {key}")
    if event["chunks_total"] <= 1 or event["atomic_publish"] != "succeeded" or event["outcome"] != "success":
        raise EvidenceError("workload did not prove atomic success")
    if event["source_stream"] != "stderr":
        raise EvidenceError("workload source provenance was invalid")
    return event


def validate_end(value: Any, rollback_hash: str) -> dict[str, Any]:
    end = _exact(value, END_KEYS, "journal end")
    if end["schema"] != "subgen.task11b.soak-end/v1" or end["outcome"] != "pass":
        raise EvidenceError("journal end was invalid")
    _integer(end["record_index"], "end index", 2); _integer(end["ended_monotonic_ns"], "end monotonic", 1)
    _hex(end["previous_record_sha256"], "end previous record"); _utc(end["ended_utc"], "end")
    validate_rollback(end["rollback"], rollback_hash)
    return end


def validate_journal(payload: bytes, *, require_end: bool = True, minimum_duration_ns: int | None = None) -> JournalView:
    minimum_duration_ns = MIN_DURATION_NS if minimum_duration_ns is None else minimum_duration_ns
    _integer(minimum_duration_ns, "minimum soak duration", 0 if not require_end else 1)
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_JOURNAL_BYTES or not payload.endswith(b"\n"):
        raise EvidenceError("journal size or termination was invalid")
    lines = payload.splitlines(keepends=True)
    if len(lines) < 2 or any(not line.endswith(b"\n") for line in lines):
        raise EvidenceError("journal was empty or partial")
    header = validate_header(strict_line(lines[0])); identity_hash = canonical_sha(header["identities"])
    start_utc = _utc(header["started_utc"], "start"); start_mono = header["started_monotonic_ns"]
    previous_hash = sha(lines[0]); record_index = 0; sample_index = 0
    samples: list[dict[str, Any]] = []; workloads: list[dict[str, Any]] = []; end = None
    last_sample_mono = start_mono; last_sample_utc = start_utc; max_lag = 0; max_gap = 0
    baseline_counters = None; prior_counters = None; event_hashes: set[str] = set(); prior_event_sequence = None
    non_normal_started = None
    for line_number, raw in enumerate(lines[1:], start=1):
        item = strict_line(raw); record_index += 1
        if item.get("record_index") != record_index or item.get("previous_record_sha256") != previous_hash:
            raise EvidenceError("journal index or hash chain was invalid")
        schema = item.get("schema")
        if schema == "subgen.task11b.soak-sample/v1":
            sample = validate_sample(item, identity_hash, header["mqtt_inventory"])
            if sample["sample_index"] != sample_index:
                raise EvidenceError("sample index was not contiguous")
            scheduled = start_mono + sample_index * INTERVAL_NS; captured = sample["captured_monotonic_ns"]
            lag = captured - scheduled; gap = captured - last_sample_mono
            if sample["scheduled_monotonic_ns"] != scheduled or lag < 0 or lag > MAX_LAG_NS or (sample_index and not MIN_GAP_NS <= gap <= MAX_GAP_NS):
                raise EvidenceError("sample schedule lag or gap was invalid")
            captured_utc = _utc(sample["captured_utc"], "sample")
            if captured_utc <= last_sample_utc and sample_index or abs((captured_utc - start_utc) - (captured - start_mono)) > UTC_DRIFT_NS:
                raise EvidenceError("sample UTC and monotonic clocks disagreed")
            counters = sample["counters"]
            if prior_counters is not None and any(counters[key] < prior_counters[key] for key in COUNTER_KEYS):
                raise EvidenceError("sample counter regressed")
            if baseline_counters is None: baseline_counters = dict(counters)
            elif any(counters[key] != baseline_counters[key] for key in FAILURE_COUNTERS):
                raise EvidenceError("failure counter changed")
            if sample["controller_phase"] == "normal":
                non_normal_started = None
            else:
                if non_normal_started is None: non_normal_started = captured
                if captured - non_normal_started > MAX_NON_NORMAL_NS:
                    raise EvidenceError("priority controller remained non-normal")
            prior_counters = dict(counters); samples.append(sample); sample_index += 1
            last_sample_mono = captured; last_sample_utc = captured_utc; max_lag = max(max_lag, lag); max_gap = max(max_gap, gap)
        elif schema == "subgen.task11b.soak-workload/v1":
            if not samples:
                raise EvidenceError("workload preceded the baseline sample")
            workload = validate_workload(item)
            if workload["source_event_sha256"] in event_hashes or workload["captured_monotonic_ns"] < last_sample_mono:
                raise EvidenceError("workload event was duplicated or stale")
            source_utc = _utc(workload["source_utc"], "workload source")
            capture_utc = _utc(workload["captured_utc"], "workload capture")
            if source_utc < start_utc or source_utc > capture_utc or abs((capture_utc - start_utc) - (workload["captured_monotonic_ns"] - start_mono)) > UTC_DRIFT_NS:
                raise EvidenceError("workload UTC and monotonic clocks disagreed")
            if prior_event_sequence is not None and workload["event_sequence"] <= prior_event_sequence:
                raise EvidenceError("workload event sequence did not advance")
            event_hashes.add(workload["source_event_sha256"]); prior_event_sequence = workload["event_sequence"]; workloads.append(workload)
        elif schema == "subgen.task11b.soak-end/v1":
            if end is not None or line_number != len(lines) - 1:
                raise EvidenceError("journal end was not unique and last")
            end = validate_end(item, header["rollback_record_sha256"])
        else:
            raise EvidenceError("journal line schema was unknown")
        previous_hash = sha(raw)
    if not samples or baseline_counters is None or prior_counters is None:
        raise EvidenceError("journal had no samples")
    if require_end and end is None: raise EvidenceError("journal was not finalized")
    if end is not None:
        duration = end["ended_monotonic_ns"] - start_mono
        if duration < minimum_duration_ns or end["ended_monotonic_ns"] - last_sample_mono > MAX_GAP_NS:
            raise EvidenceError("journal duration or final gap was invalid")
        end_utc = _utc(end["ended_utc"], "end")
        if abs((end_utc - start_utc) - duration) > UTC_DRIFT_NS:
            raise EvidenceError("end UTC and monotonic clocks disagreed")
        required_samples = minimum_duration_ns // INTERVAL_NS + 1
        if len(samples) < required_samples:
            raise EvidenceError("journal sample count was too small")
        if not workloads or samples[-1]["active"] or samples[-1]["chunk_uncommitted"] or samples[-1]["controller_phase"] != "normal":
            raise EvidenceError("journal did not prove an idle multi-chunk atomic completion")
    return JournalView(header, tuple(samples), tuple(workloads), end, sha(payload), sha(lines[0]), sha(lines[-1]), max_lag, max_gap, len(lines))


def derive_record(payload: bytes, *, minimum_duration_ns: int | None = None) -> dict[str, Any]:
    minimum_duration_ns = MIN_DURATION_NS if minimum_duration_ns is None else minimum_duration_ns
    view = validate_journal(payload, minimum_duration_ns=minimum_duration_ns); assert view.end is not None
    first, last = view.samples[0], view.samples[-1]; base, final = first["counters"], last["counters"]
    failure_deltas = {key: final[key] - base[key] for key in FAILURE_COUNTERS}
    mqtt_binding = view.header["mqtt_inventory"]
    mqtt_healthy = all(
        item["mqtt_inventory"] == {"enabled": False}
        if not mqtt_binding["enabled"]
        else all(item["mqtt_inventory"].values())
        for item in view.samples
    )
    return {
        "schema": "subgen.task11b.soak-record/v1", "outcome": "pass", "soak_id": view.header["soak_id"],
        "identities": view.header["identities"],
        "timing": {
            "started_utc": view.header["started_utc"], "ended_utc": view.end["ended_utc"],
            "started_monotonic_ns": view.header["started_monotonic_ns"], "ended_monotonic_ns": view.end["ended_monotonic_ns"],
            "duration_ns": view.end["ended_monotonic_ns"] - view.header["started_monotonic_ns"],
            "interval_ns": INTERVAL_NS, "sample_count": len(view.samples), "maximum_lag_ns": view.maximum_lag_ns, "maximum_gap_ns": view.maximum_gap_ns,
        },
        "journal": {"sha256": view.payload_sha256, "byte_count": len(payload), "record_count": view.record_count, "first_record_sha256": view.first_sha256, "last_record_sha256": view.last_sha256},
        "transcription": {
            "completion_generation_start": first["completion_generation"], "completion_generation_end": last["completion_generation"],
            "completion_delta": last["completion_generation"] - first["completion_generation"],
            "successful_completion_count": len(view.workloads), "long_multichunk_completion_count": len(view.workloads),
            "atomic_join_count": sum(item["atomic_publish"] == "succeeded" for item in view.workloads),
        },
        "markers": {"created_count": final["marker_created"] - base["marker_created"], "skipped_count": final["marker_skipped"] - base["marker_skipped"], "handled_count": final["marker_handled"] - base["marker_handled"], "deleted_count": final["media_deleted"] - base["media_deleted"], "deletion_enabled_observed": any(item["deletion_enabled"] for item in view.samples)},
        "health": {"candidate_running_all": all(item["candidate_running"] for item in view.samples), "candidate_oom_killed_any": any(item["candidate_oom_killed"] for item in view.samples), "frigate_healthy_all": all(item["frigate_healthy"] for item in view.samples), "failure_counter_deltas": failure_deltas, "cooperative_yield_count": final["cooperative_yield"] - base["cooperative_yield"], "cooperative_recovery_count": final["cooperative_recovery"] - base["cooperative_recovery"]},
        "mqtt_inventory": {
            **mqtt_binding,
            "outcome": "pass" if mqtt_binding["enabled"] else "disabled",
            "observation_count": len(view.samples) if mqtt_binding["enabled"] else 0,
            "healthy_all": mqtt_healthy,
        },
        "rollback": view.end["rollback"],
    }


def validate_record(value: Any, *, minimum_duration_ns: int | None = None) -> dict[str, Any]:
    minimum_duration_ns = MIN_DURATION_NS if minimum_duration_ns is None else minimum_duration_ns
    record = _exact(value, RECORD_KEYS, "soak record")
    if record["schema"] != "subgen.task11b.soak-record/v1" or record["outcome"] != "pass" or not isinstance(record["soak_id"], str) or HEX32.fullmatch(record["soak_id"]) is None:
        raise EvidenceError("soak record header was invalid")
    validate_identities(record["identities"]); validate_rollback(record["rollback"])
    timing = _exact(record["timing"], TIMING_KEYS, "record timing")
    for key in ("started_monotonic_ns", "ended_monotonic_ns", "duration_ns", "interval_ns", "sample_count", "maximum_lag_ns", "maximum_gap_ns"): _integer(timing[key], f"timing {key}")
    if timing["duration_ns"] < minimum_duration_ns or timing["interval_ns"] != INTERVAL_NS or timing["maximum_lag_ns"] > MAX_LAG_NS or timing["maximum_gap_ns"] > MAX_GAP_NS:
        raise EvidenceError("record timing was invalid")
    _utc(timing["started_utc"], "record start"); _utc(timing["ended_utc"], "record end")
    journal = _exact(record["journal"], JOURNAL_KEYS, "record journal")
    for key in ("sha256", "first_record_sha256", "last_record_sha256"): _hex(journal[key], f"journal {key}")
    for key in ("byte_count", "record_count"): _integer(journal[key], f"journal {key}", 1)
    trans = _exact(record["transcription"], TRANSCRIPTION_KEYS, "record transcription")
    for key in ("completion_generation_start", "completion_generation_end", "successful_completion_count", "long_multichunk_completion_count", "atomic_join_count"): _integer(trans[key], f"transcription {key}")
    if type(trans["completion_delta"]) is not int:
        raise EvidenceError("transcription completion telemetry was invalid")
    if trans["completion_delta"] != trans["completion_generation_end"] - trans["completion_generation_start"] or trans["successful_completion_count"] < 1 or trans["successful_completion_count"] != trans["long_multichunk_completion_count"] or trans["atomic_join_count"] != trans["long_multichunk_completion_count"]:
        raise EvidenceError("record transcription proof was invalid")
    markers = _exact(record["markers"], MARKER_KEYS, "record markers")
    if markers["deleted_count"] != 0 or markers["deletion_enabled_observed"] is not False: raise EvidenceError("record deletion proof failed")
    health = _exact(record["health"], HEALTH_KEYS, "record health")
    failures = _exact(health["failure_counter_deltas"], set(FAILURE_COUNTERS), "health failure deltas")
    if not health["candidate_running_all"] or health["candidate_oom_killed_any"] or not health["frigate_healthy_all"] or any(value != 0 or type(value) is not int for value in failures.values()):
        raise EvidenceError("record health proof failed")
    mqtt = _exact(record["mqtt_inventory"], MQTT_RECORD_KEYS, "record MQTT inventory")
    binding = validate_mqtt_binding({key: mqtt[key] for key in MQTT_BINDING_KEYS})
    _integer(mqtt["observation_count"], "MQTT observation count")
    if type(mqtt["healthy_all"]) is not bool or not mqtt["healthy_all"]:
        raise EvidenceError("record MQTT inventory health proof failed")
    expected_mqtt = (
        ("pass", timing["sample_count"])
        if binding["enabled"]
        else ("disabled", 0)
    )
    if (mqtt["outcome"], mqtt["observation_count"]) != expected_mqtt:
        raise EvidenceError("record MQTT inventory outcome was invalid")
    return record


def verify_pair(record_payload: bytes, journal_payload: bytes, *, minimum_duration_ns: int | None = None) -> dict[str, Any]:
    minimum_duration_ns = MIN_DURATION_NS if minimum_duration_ns is None else minimum_duration_ns
    expected = derive_record(journal_payload, minimum_duration_ns=minimum_duration_ns)
    record = validate_record(strict_line(record_payload, MAX_RECORD_BYTES), minimum_duration_ns=minimum_duration_ns)
    if record != expected or record_payload != canonical_line(expected): raise EvidenceError("record did not equal journal derivation")
    return record


def binding_projection(record: dict[str, Any], record_sha256: str) -> dict[str, Any]:
    ids, timing = record["identities"], record["timing"]
    return {
        "schema": "subgen.task11b.soak-binding/v1", "soak_record_sha256": record_sha256,
        "soak_journal_sha256": record["journal"]["sha256"], "gate_seal_sha256": ids["artifacts"]["gate_seal_sha256"],
        "candidate_identity_record_sha256": ids["artifacts"]["candidate_identity_record_sha256"], "runtime_commit": ids["image"]["runtime_commit"],
        "candidate_oci_index": ids["image"]["oci_index"], "candidate_config_digest": ids["image"]["config_digest"],
        "layer_diff_ids_sha256": ids["image"]["layer_diff_ids_sha256"], "model_envelope_catalog_sha256": ids["model"]["catalog_sha256"],
        "model_identity_sha256": ids["model"]["model_identity_sha256"], "policy_sha256": ids["configuration"]["policy_sha256"],
        "runtime_config_sha256": ids["configuration"]["runtime_config_sha256"], "frigate_config_sha256": ids["configuration"]["frigate_config_sha256"],
        "monitored_config_sha256": ids["configuration"]["monitored_config_sha256"], "soak_evidence_sha256": ids["artifacts"]["evidence_sha256"], "soak_observer_sha256": ids["artifacts"]["observer_sha256"],
        "soak_observer_test_sha256": ids["artifacts"]["observer_test_sha256"], "started_utc": timing["started_utc"], "ended_utc": timing["ended_utc"],
        "duration_ns": timing["duration_ns"], "outcome": "pass", "rollback_ready": record["rollback"]["ready"],
        "host_boot_id_sha256": ids["deployment"]["host_boot_id_sha256"],
        "docker_daemon_identity_sha256": ids["deployment"]["docker_daemon_identity_sha256"],
        "candidate_container_id_sha256": ids["deployment"]["container_id_sha256"],
        "frigate_container_id_sha256": ids["deployment"]["frigate_container_id_sha256"],
        "mqtt_inventory_enabled": record["mqtt_inventory"]["enabled"],
        "mqtt_semantic_config_sha256": record["mqtt_inventory"]["semantic_config_sha256"],
    }


def parse_binding(evidence: bytes, prefix: str) -> dict[str, Any]:
    if prefix != BINDING_PREFIX: raise EvidenceError("binding prefix was not exact")
    marker = prefix.encode("ascii"); matches = [line[len(marker):] + b"\n" for line in evidence.splitlines() if line.startswith(marker)]
    if len(matches) != 1: raise EvidenceError("binding line was missing or duplicated")
    binding = _exact(strict_line(matches[0]), BINDING_KEYS, "soak binding")
    if binding["schema"] != "subgen.task11b.soak-binding/v1" or binding["outcome"] != "pass" or binding["rollback_ready"] is not True: raise EvidenceError("binding header was invalid")
    for key in BINDING_KEYS - {"schema", "runtime_commit", "candidate_oci_index", "candidate_config_digest", "started_utc", "ended_utc", "duration_ns", "outcome", "rollback_ready", "mqtt_inventory_enabled"}:
        _hex(binding[key], f"binding {key}")
    if type(binding["mqtt_inventory_enabled"]) is not bool:
        raise EvidenceError("binding MQTT inventory enabled state was invalid")
    if not isinstance(binding["runtime_commit"], str) or HEX40.fullmatch(binding["runtime_commit"]) is None: raise EvidenceError("binding runtime commit was invalid")
    _oci(binding["candidate_oci_index"], "binding image index"); _oci(binding["candidate_config_digest"], "binding image config")
    started = _utc(binding["started_utc"], "binding start"); ended = _utc(binding["ended_utc"], "binding end")
    _integer(binding["duration_ns"], "binding duration", MIN_DURATION_NS)
    if abs((ended - started) - binding["duration_ns"]) > UTC_DRIFT_NS: raise EvidenceError("binding duration disagreed with UTC")
    return binding


def validate_gate(value: Any) -> dict[str, Any]:
    gate = _exact(value, GATE_KEYS, "gate seal")
    if gate["schema"] != "subgen.task11b.shared-gpu-gate/v4" or gate["outcome"] != "pass": raise EvidenceError("gate schema or outcome was invalid")
    cleanup = _exact(gate["cleanup"], {"verified_stopped", "candidate_pid_count", "execution_boundary_revalidated"}, "gate cleanup")
    if cleanup != {"verified_stopped": True, "candidate_pid_count": 0, "execution_boundary_revalidated": True}: raise EvidenceError("gate cleanup failed")
    if not isinstance(gate["runtime_commit"], str) or HEX40.fullmatch(gate["runtime_commit"]) is None: raise EvidenceError("gate runtime commit was invalid")
    _oci(gate["candidate_oci_index"], "gate image index"); _oci(gate["candidate_config_digest"], "gate image config")
    for key in GATE_KEYS - {"schema", "outcome", "runtime_commit", "candidate_oci_index", "candidate_config_digest", "cleanup"}: _hex(gate[key], f"gate {key}")
    return gate


def validate_candidate(value: Any) -> dict[str, Any]:
    candidate = _exact(value, CANDIDATE_KEYS, "candidate record"); inner = _exact(candidate["candidate_identity"], CANDIDATE_INNER_KEYS, "candidate identity")
    if candidate["schema"] != "subgen.task11b.candidate-identity/v2" or candidate["created_stopped"] is not True or not isinstance(inner["container_id"], str) or CONTAINER.fullmatch(inner["container_id"]) is None: raise EvidenceError("candidate record was invalid")
    if not isinstance(inner["runtime_commit"], str) or HEX40.fullmatch(inner["runtime_commit"]) is None or inner["selected_model"] not in MODELS or not isinstance(inner["model_revision"], str) or REVISION.fullmatch(inner["model_revision"]) is None: raise EvidenceError("candidate runtime or model was invalid")
    _oci(inner["oci_index"], "candidate index"); _oci(inner["config_digest"], "candidate config")
    if not isinstance(inner["layer_diff_ids"], list) or not inner["layer_diff_ids"] or any(not isinstance(item, str) or OCI.fullmatch(item) is None for item in inner["layer_diff_ids"]): raise EvidenceError("candidate layers were invalid")
    for key in CANDIDATE_KEYS - {"schema", "candidate_identity", "created_stopped"}: _hex(candidate[key], f"candidate {key}")
    return candidate


def verify_release_bindings(*, binding: dict[str, Any], record: dict[str, Any], record_sha256: str, gate: dict[str, Any], gate_sha256: str, candidate: dict[str, Any], candidate_sha256: str, evidence_sha256: str, observer_sha256: str, observer_test_sha256: str, runtime_commit: str, oci_index: str, config_digest: str) -> None:
    if binding != binding_projection(record, record_sha256): raise EvidenceError("committed binding did not match private evidence")
    ids, inner = record["identities"], candidate["candidate_identity"]
    layer_sha = canonical_sha(inner["layer_diff_ids"])
    if gate_sha256 != binding["gate_seal_sha256"] or candidate_sha256 != binding["candidate_identity_record_sha256"] or gate["candidate_identity_record_sha256"] != candidate_sha256: raise EvidenceError("gate or candidate bytes were not bound")
    if evidence_sha256 != binding["soak_evidence_sha256"] or observer_sha256 != binding["soak_observer_sha256"] or observer_test_sha256 != binding["soak_observer_test_sha256"]: raise EvidenceError("soak source identity changed")
    if any(item != runtime_commit for item in (gate["runtime_commit"], inner["runtime_commit"], ids["image"]["runtime_commit"])) or any(item != oci_index for item in (gate["candidate_oci_index"], inner["oci_index"], ids["image"]["oci_index"])) or any(item != config_digest for item in (gate["candidate_config_digest"], inner["config_digest"], ids["image"]["config_digest"])): raise EvidenceError("runtime image identity changed")
    if gate["layer_diff_ids_sha256"] != layer_sha or ids["image"]["layer_diff_ids_sha256"] != layer_sha: raise EvidenceError("ordered layer identity changed")
    if gate["policy_sha256"] != ids["configuration"]["policy_sha256"] or gate["model_envelope_catalog_sha256"] != ids["model"]["catalog_sha256"] or inner["selected_model"] != ids["model"]["selected_model"] or inner["model_revision"] != ids["model"]["model_revision"]: raise EvidenceError("model catalog or policy identity changed")
    candidate_container_sha256 = sha(inner["container_id"].encode("ascii"))
    if gate["container_id_sha256"] != candidate_container_sha256 or ids["deployment"]["container_id_sha256"] != candidate_container_sha256: raise EvidenceError("candidate container identity changed between gate and soak")
    if candidate["docker_daemon_identity_sha256"] != gate["docker_daemon_identity_sha256"] or gate["docker_daemon_identity_sha256"] != ids["deployment"]["docker_daemon_identity_sha256"]: raise EvidenceError("Docker daemon identity changed between gate and soak")
    if candidate["execution_boundary_manifest_sha256"] != gate["execution_boundary_manifest_sha256"]: raise EvidenceError("candidate execution boundary was not gate bound")
