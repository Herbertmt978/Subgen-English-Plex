from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SOURCE = Path(__file__).with_name("soak_runtime_observer.py")
SPEC = importlib.util.spec_from_file_location("task11b_soak_runtime_observer_tests", SOURCE)
assert SPEC is not None and SPEC.loader is not None
observer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = observer
SPEC.loader.exec_module(observer)
ev = observer.ev


class Clock:
    def __init__(self) -> None:
        self.start_ns = 1_000_000_000
        self.now_ns = self.start_ns
        self.start_utc = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc)

    def monotonic_ns(self) -> int:
        return self.now_ns

    def utc_now(self) -> str:
        elapsed = dt.timedelta(microseconds=(self.now_ns - self.start_ns) // 1000)
        return (self.start_utc + elapsed).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def advance(self, seconds: int) -> None:
        self.now_ns += seconds * 1_000_000_000


def _private(directory: Path) -> None:
    if os.name == "posix":
        directory.chmod(0o700)


def _candidate(runtime_commit: str, oci: str, config_digest: str, layers: list[str]) -> dict:
    return {
        "schema": "subgen.task11b.candidate-identity/v2",
        "candidate_identity": {
            "container_id": "1" * 64,
            "runtime_commit": runtime_commit,
            "oci_index": oci,
            "config_digest": config_digest,
            "layer_diff_ids": layers,
            "selected_model": "medium",
            "model_revision": "hf:" + "2" * 40,
        },
        "docker_daemon_identity_sha256": hashlib.sha256(b"daemon-id").hexdigest(),
        "execution_boundary_manifest_sha256": "4" * 64,
        "gate_token_sha256": "5" * 64,
        "intended_command_sha256": "6" * 64,
        "created_stopped": True,
    }


def _gate(runtime_commit: str, oci: str, config_digest: str, layers_sha: str, candidate_sha: str) -> dict:
    value = {key: "7" * 64 for key in ev.GATE_KEYS - {"schema", "outcome", "runtime_commit", "candidate_oci_index", "candidate_config_digest", "candidate_identity_record_sha256", "layer_diff_ids_sha256", "cleanup"}}
    value.update(
        schema="subgen.task11b.shared-gpu-gate/v4",
        outcome="pass",
        runtime_commit=runtime_commit,
        candidate_oci_index=oci,
        candidate_config_digest=config_digest,
        container_id_sha256=hashlib.sha256(("1" * 64).encode("ascii")).hexdigest(),
        candidate_identity_record_sha256=candidate_sha,
        layer_diff_ids_sha256=layers_sha,
        docker_daemon_identity_sha256=hashlib.sha256(b"daemon-id").hexdigest(),
        execution_boundary_manifest_sha256="4" * 64,
        cleanup={"verified_stopped": True, "candidate_pid_count": 0, "execution_boundary_revalidated": True},
    )
    return value


def _config(
    tmp_path: Path,
    gate_sha: str,
    candidate_sha: str,
    gate: dict,
    candidate: dict,
    *,
    mqtt_binding: dict | None = None,
) -> dict:
    mqtt_binding = mqtt_binding or observer._mqtt_settings_from_environment({}).binding
    live = {
        "schema": "subgen.task11b.soak-live/v1",
        "candidate_container_id": candidate["candidate_identity"]["container_id"],
        "frigate_container_id": "9" * 64,
        "docker_daemon_id": "daemon-id",
        "host_boot_id": "11111111-1111-4111-8111-111111111111",
        "candidate_runtime_config_sha256": "a" * 64,
        "frigate_runtime_config_sha256": "b" * 64,
        "model_catalog_path": str((tmp_path / "catalog.json").resolve()),
        "model_identity_path": str((tmp_path / "identity.json").resolve()),
        "frigate_config_path": str((tmp_path / "frigate.yml").resolve()),
        "priority_policy_path": str((tmp_path / "priority.json").resolve()),
        "systemd_boundary_sha256": "c" * 64,
        "host_memory_reserve_bytes": 2 * 1024**3,
        "gpu_uuid": "GPU-11111111-1111-4111-8111-111111111111",
        "gpu_total_bytes": 24_576 * 1024**2,
        "gpu_free_reserve_bytes": 2 * 1024**3,
        "mqtt_inventory": mqtt_binding,
    }
    return {
        "schema": "subgen.task11b.soak-config/v1",
        "image": {
            "runtime_commit": candidate["candidate_identity"]["runtime_commit"],
            "oci_index": candidate["candidate_identity"]["oci_index"],
            "config_digest": candidate["candidate_identity"]["config_digest"],
            "layer_diff_ids_sha256": gate["layer_diff_ids_sha256"],
        },
        "model": {
            "selected_model": "medium",
            "model_revision": "hf:" + "2" * 40,
            "model_identity_sha256": "d" * 64,
            "catalog_sha256": gate["model_envelope_catalog_sha256"],
        },
        "configuration": {
            "policy_sha256": gate["policy_sha256"],
            "runtime_config_sha256": live["candidate_runtime_config_sha256"],
            "frigate_config_sha256": "e" * 64,
            "monitored_config_sha256": hashlib.sha256(observer._canonical_bytes(live)).hexdigest(),
        },
        "deployment": {
            "host_boot_id_sha256": hashlib.sha256(live["host_boot_id"].encode("ascii")).hexdigest(),
            "docker_daemon_identity_sha256": hashlib.sha256(live["docker_daemon_id"].encode()).hexdigest(),
            "container_id_sha256": hashlib.sha256(live["candidate_container_id"].encode("ascii")).hexdigest(),
            "frigate_container_id_sha256": hashlib.sha256(live["frigate_container_id"].encode("ascii")).hexdigest(),
        },
        "gate_seal_sha256": gate_sha,
        "candidate_identity_record_sha256": candidate_sha,
        "rollback_record_sha256": "f" * 64,
        "live": live,
    }


def _health(config: dict, *, completion: int = 0, counters: dict[str, int] | None = None) -> dict:
    identities = observer._validate_config(config, Path(__file__))
    mqtt_enabled = config["live"]["mqtt_inventory"]["enabled"]
    mqtt_health = (
        {
            "enabled": True,
            "availability_retained": True,
            "availability_online": True,
            "discovery_exact_retained": True,
            "state_retained": True,
            "state_fresh": True,
            "state_parseable": True,
            "state_fields_valid": True,
        }
        if mqtt_enabled
        else {"enabled": False}
    )
    return {
        "schema": "subgen.task11b.soak-health/v1",
        "identity_sha256": ev.canonical_sha(identities),
        "candidate_running": True,
        "candidate_oom_killed": False,
        "frigate_healthy": True,
        "deletion_enabled": False,
        "active": False,
        "chunk_uncommitted": False,
        "completion_generation": completion,
        "controller_phase": "normal",
        "counters": counters or {key: 0 for key in ev.COUNTER_KEYS},
        "mqtt_inventory": mqtt_health,
    }


def _mqtt_environment() -> dict[str, str]:
    return {
        "MQTT_INVENTORY_ENABLED": "true",
        "MQTT_HOST": "broker.private.invalid",
        "MQTT_PORT": "1883",
        "MQTT_USERNAME": "sentinel-user",
        "MQTT_PASSWORD": "sentinel-password",
        "MQTT_CLIENT_ID": "subgen-inventory-private",
        "MQTT_TOPIC_PREFIX": "private/subgen",
        "MQTT_DISCOVERY_PREFIX": "homeassistant",
        "MQTT_INVENTORY_NODE_ID": "subgen_private",
        "MQTT_INVENTORY_LIBRARY_NAMES": "Private Movies|Private TV",
        "MQTT_INVENTORY_SCAN_TIMEOUT_SECONDS": "21600",
        "TRANSCRIBE_FOLDERS": "/private/media/movies|/private/media/tv",
        "PATH_MAPPING_FROM": "/private/media",
        "PATH_MAPPING_TO": "/media",
    }


def _mqtt_state_payload(
    *,
    items_left: int = 4,
    scan_percent: float = 50.0,
    label: str = "Private Movies",
) -> bytes:
    return observer._canonical_bytes(
        {
            "items_left": items_left,
            "scan_percent": scan_percent,
            "scan_complete": False,
            "scan_errors": 0,
            "libraries": {
                label: {"scanned": 5, "total": 10, "items_left": items_left},
            },
        }
    )[:-1]


def _mqtt_discovery_payload(settings: object, *, scan: bool) -> bytes:
    return observer._canonical_bytes(
        observer._mqtt_expected_discovery(settings, scan=scan)
    )[:-1]


def _seed_mqtt_observation(
    state: object,
    settings: object,
    *,
    retained_state_ns: int = 1,
    live_state_ns: int | None = 2,
) -> None:
    state.observe_suback()
    state.observe(
        settings.availability_topic,
        b"online",
        retained=True,
        now_ns=retained_state_ns,
    )
    state.observe(
        settings.items_discovery_topic,
        _mqtt_discovery_payload(settings, scan=False),
        retained=True,
        now_ns=retained_state_ns,
    )
    state.observe(
        settings.scan_discovery_topic,
        _mqtt_discovery_payload(settings, scan=True),
        retained=True,
        now_ns=retained_state_ns,
    )
    state.observe(
        settings.state_topic,
        _mqtt_state_payload(),
        retained=True,
        now_ns=retained_state_ns,
    )
    if live_state_ns is not None:
        state.observe(
            settings.state_topic,
            _mqtt_state_payload(),
            retained=False,
            now_ns=live_state_ns,
        )


def _runtime_line(*, sequence: int = 1) -> bytes:
    event = {
        "atomic_publish": "succeeded",
        "chunks_total": 6,
        "event": "multichunk_transcription_completed",
        "event_sequence": sequence,
        "monotonic_ns": 0,
        "outcome": "success",
        "schema": "subgen.runtime-event/v1",
        "workload_id": "a" * 32,
    }
    payload = json.dumps(event, sort_keys=True, separators=(",", ":")).encode("ascii")
    return b"2026-09-02T12:00:04.000000000Z 2026-09-02 12:00:04 INFO: SUBGEN_RUNTIME_EVENT " + payload + b"\n"


def _priority_signal(*, sequence: int, observed: int, source_observed: int, observation: str = "b" * 64) -> bytes:
    return observer._canonical_bytes(
        {
            "schema": 1,
            "boot_id_sha256": "1" * 64,
            "producer_epoch": "a" * 32,
            "sequence": sequence,
            "observed_monotonic_ns": observed,
            "source_generation": sequence,
            "source_observed_monotonic_ns": source_observed,
            "observation_id": observation,
            "policy_sha256": "2" * 64,
            "pressure": False,
            "clear_eligible": True,
            "reason_codes": [],
        }
    )


def _build_short_pair(
    tmp_path: Path,
    *,
    mqtt_binding: dict | None = None,
) -> tuple[dict, bytes, bytes, dict, bytes, dict, bytes]:
    _private(tmp_path)
    runtime_commit = "1" * 40
    oci = "sha256:" + "2" * 64
    config_digest = "sha256:" + "3" * 64
    layers = ["sha256:" + "4" * 64, "sha256:" + "5" * 64]
    candidate = _candidate(runtime_commit, oci, config_digest, layers)
    candidate_payload = ev.canonical_line(candidate)
    candidate_sha = hashlib.sha256(candidate_payload).hexdigest()
    gate = _gate(runtime_commit, oci, config_digest, ev.canonical_sha(layers), candidate_sha)
    gate_payload = ev.canonical_line(gate)
    gate_sha = hashlib.sha256(gate_payload).hexdigest()
    config = _config(
        tmp_path,
        gate_sha,
        candidate_sha,
        gate,
        candidate,
        mqtt_binding=mqtt_binding,
    )
    clock = Clock()
    journal_path = (tmp_path / "soak.journal").resolve()
    writer = observer.JournalWriter(journal_path, config, Path(__file__).resolve(), utc_now=clock.utc_now, monotonic_ns=clock.monotonic_ns)
    writer.append_health(_health(config, completion=9))
    clock.advance(5)
    writer.append_health(_health(config, completion=9))
    parsed = observer.parse_runtime_event(_runtime_line(), started_utc_ns=ev._utc("2026-09-02T12:00:00.000000Z", "test"))
    assert parsed is not None
    event, source_ns = parsed
    writer.append_runtime_event(_runtime_line(), event, source_ns, "stderr")
    clock.advance(5)
    writer.append_health(_health(config, completion=9))
    writer.close()
    record_path = (tmp_path / "soak.record").resolve()
    rollback = {"ready": True, "target_version": "0.3.0", "deletion_disabled": True, "repair_report_only": True, "record_sha256": "f" * 64}
    observer.finalize(
        journal_path,
        record_path,
        {"schema": "subgen.task11b.soak-finalization/v1", "outcome": "pass", "rollback": rollback},
        utc_now=clock.utc_now,
        monotonic_ns=clock.monotonic_ns,
        minimum_duration_ns=10_000_000_000,
    )
    return config, record_path.read_bytes(), journal_path.read_bytes(), gate, gate_payload, candidate, candidate_payload


def _rechain(documents: list[dict]) -> bytes:
    lines = [ev.canonical_line(documents[0])]
    previous = hashlib.sha256(lines[0]).hexdigest()
    for index, document in enumerate(documents[1:], start=1):
        document["record_index"] = index
        document["previous_record_sha256"] = previous
        raw = ev.canonical_line(document); lines.append(raw); previous = hashlib.sha256(raw).hexdigest()
    return b"".join(lines)


def _full_boundary_journal(config: dict) -> bytes:
    identities = observer._validate_config(config, Path(__file__).resolve())
    start_utc = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc)
    start_ns = 1_000_000_000
    header = {
        "schema": "subgen.task11b.soak-start/v1", "record_index": 0, "soak_id": "a" * 32,
        "started_utc": start_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "started_monotonic_ns": start_ns,
        "interval_ns": ev.INTERVAL_NS, "max_gap_ns": ev.MAX_GAP_NS, "deletion_enabled": False,
        "identities": identities, "rollback_record_sha256": config["rollback_record_sha256"],
        "mqtt_inventory": config["live"]["mqtt_inventory"],
    }
    documents = [header]
    identity_sha = ev.canonical_sha(identities); counters = {key: 0 for key in ev.COUNTER_KEYS}
    mqtt_health = _health(config)["mqtt_inventory"]
    required = ev.MIN_DURATION_NS // ev.INTERVAL_NS + 1
    for sample_index in range(required):
        captured_ns = start_ns + sample_index * ev.INTERVAL_NS
        captured_utc = (start_utc + dt.timedelta(seconds=sample_index * 5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        documents.append(
            {
                "schema": "subgen.task11b.soak-sample/v1", "record_index": 0, "previous_record_sha256": "0" * 64,
                "sample_index": sample_index, "scheduled_monotonic_ns": captured_ns, "captured_monotonic_ns": captured_ns,
                "captured_utc": captured_utc, "identity_sha256": identity_sha, "candidate_running": True,
                "candidate_oom_killed": False, "frigate_healthy": True, "deletion_enabled": False,
                "active": False, "chunk_uncommitted": False, "completion_generation": 0,
                "controller_phase": "normal", "counters": counters,
                "mqtt_inventory": mqtt_health,
            }
        )
        if sample_index == 0:
            documents.append(
                {
                    "schema": "subgen.task11b.soak-workload/v1", "record_index": 0, "previous_record_sha256": "0" * 64,
                    "captured_monotonic_ns": captured_ns, "captured_utc": captured_utc, "source_utc": captured_utc,
                    "source_event_sha256": "b" * 64, "event_sequence": 1, "source_monotonic_ns": 0,
                    "workload_id_sha256": "c" * 64, "chunks_total": 6, "atomic_publish": "succeeded", "outcome": "success", "source_stream": "stderr",
                }
            )
    end_ns = start_ns + ev.MIN_DURATION_NS
    documents.append(
        {
            "schema": "subgen.task11b.soak-end/v1", "record_index": 0, "previous_record_sha256": "0" * 64,
            "ended_monotonic_ns": end_ns, "ended_utc": (start_utc + dt.timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "outcome": "pass", "rollback": {"ready": True, "target_version": "0.3.0", "deletion_disabled": True, "repair_report_only": True, "record_sha256": config["rollback_record_sha256"]},
        }
    )
    return _rechain(documents)


def test_runtime_event_requires_exact_docker_and_logger_frames() -> None:
    parsed = observer.parse_runtime_event(_runtime_line(), started_utc_ns=0)
    assert parsed is not None and parsed[0]["chunks_total"] == 6 and parsed[0]["monotonic_ns"] == 0
    ordinary = b"2026-09-02T12:00:04.000000000Z 2026-09-02 12:00:04 INFO: filename SUBGEN_RUNTIME_EVENT notes.mkv\n"
    assert observer.parse_runtime_event(ordinary, started_utc_ns=0) is None
    with pytest.raises(observer.ObserverError):
        observer.parse_runtime_event(b"SUBGEN_RUNTIME_EVENT {}\n", started_utc_ns=0)
    with pytest.raises(observer.ObserverError):
        observer.parse_runtime_event(_runtime_line().replace(b" INFO: ", b" WARNING: "), started_utc_ns=0)
    with pytest.raises(observer.ObserverError):
        observer.parse_runtime_event(_runtime_line().replace(b'"outcome":"success"', b'"outcome":"failed"'), started_utc_ns=0)


def test_short_journal_uses_structured_event_not_completion_generation(tmp_path: Path) -> None:
    _config_value, record_payload, journal_payload, *_ = _build_short_pair(tmp_path)
    record = ev.verify_pair(record_payload, journal_payload, minimum_duration_ns=10_000_000_000)
    assert record["transcription"]["successful_completion_count"] == 1
    assert record["transcription"]["completion_delta"] == 0
    assert record["markers"]["deleted_count"] == 0


def test_writer_rejects_failure_at_baseline(tmp_path: Path) -> None:
    _private(tmp_path)
    runtime_commit = "1" * 40; oci = "sha256:" + "2" * 64; digest = "sha256:" + "3" * 64
    candidate = _candidate(runtime_commit, oci, digest, ["sha256:" + "4" * 64])
    candidate_payload = ev.canonical_line(candidate); candidate_sha = hashlib.sha256(candidate_payload).hexdigest()
    gate = _gate(runtime_commit, oci, digest, ev.canonical_sha(candidate["candidate_identity"]["layer_diff_ids"]), candidate_sha)
    gate_payload = ev.canonical_line(gate); config = _config(tmp_path, hashlib.sha256(gate_payload).hexdigest(), candidate_sha, gate, candidate)
    clock = Clock(); writer = observer.JournalWriter((tmp_path / "failed.journal").resolve(), config, Path(__file__).resolve(), utc_now=clock.utc_now, monotonic_ns=clock.monotonic_ns)
    counters = {key: 0 for key in ev.COUNTER_KEYS}; counters["cgroup_max"] = 1
    with pytest.raises(observer.ObserverError, match="failure_occurred_before_the_baseline_sample"):
        writer.append_health(_health(config, counters=counters))
    writer.close()


def test_release_verifier_cross_binds_private_soak_and_prints_exact_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config, record_payload, journal_payload, gate, gate_payload, candidate, candidate_payload = _build_short_pair(tmp_path)
    monkeypatch.setattr(ev, "MIN_DURATION_NS", 10_000_000_000)
    record = ev.verify_pair(record_payload, journal_payload)
    binding = ev.binding_projection(record, hashlib.sha256(record_payload).hexdigest())
    evidence_path = (tmp_path / "90-evidence.md").resolve(); evidence_path.write_bytes(ev.BINDING_PREFIX.encode("ascii") + ev.canonical_line(binding))
    record_path = (tmp_path / "private.record").resolve(); record_path.write_bytes(record_payload)
    journal_path = (tmp_path / "private.journal").resolve(); journal_path.write_bytes(journal_payload)
    if os.name == "posix": record_path.chmod(0o600); journal_path.chmod(0o600)
    gate_path = (tmp_path / "gate.json").resolve(); gate_path.write_bytes(gate_payload)
    candidate_path = (tmp_path / "candidate.json").resolve(); candidate_path.write_bytes(candidate_payload)
    args = argparse.Namespace(
        evidence=evidence_path,
        binding_prefix=ev.BINDING_PREFIX,
        soak_record=record_path,
        soak_journal=journal_path,
        candidate_identity_record=candidate_path,
        gate_seal=gate_path,
        observer_test_source=Path(__file__).resolve(),
        runtime_commit=config["image"]["runtime_commit"],
        candidate_oci_index=config["image"]["oci_index"],
        candidate_config_digest=config["image"]["config_digest"],
    )
    assert observer.verify_release(args) == 0
    assert capsys.readouterr().out == "TASK11B_SOAK_VERIFY_OK\n"


def test_release_binding_rejects_a_different_gate_container(tmp_path: Path) -> None:
    _config_value, record_payload, journal_payload, gate, _gate_payload, candidate, candidate_payload = _build_short_pair(tmp_path)
    record = ev.verify_pair(record_payload, journal_payload, minimum_duration_ns=10_000_000_000)
    bad_gate = json.loads(json.dumps(gate))
    bad_gate["container_id_sha256"] = "0" * 64
    bad_gate_sha = hashlib.sha256(ev.canonical_line(bad_gate)).hexdigest()
    bad_record = json.loads(json.dumps(record))
    bad_record["identities"]["artifacts"]["gate_seal_sha256"] = bad_gate_sha
    bad_record_payload = ev.canonical_line(bad_record)
    bad_record_sha = hashlib.sha256(bad_record_payload).hexdigest()
    artifacts = bad_record["identities"]["artifacts"]
    with pytest.raises(ev.EvidenceError, match="candidate_container_identity_changed_between_gate_and_soak"):
        ev.verify_release_bindings(
            binding=ev.binding_projection(bad_record, bad_record_sha),
            record=bad_record,
            record_sha256=bad_record_sha,
            gate=bad_gate,
            gate_sha256=bad_gate_sha,
            candidate=candidate,
            candidate_sha256=hashlib.sha256(candidate_payload).hexdigest(),
            evidence_sha256=artifacts["evidence_sha256"],
            observer_sha256=artifacts["observer_sha256"],
            observer_test_sha256=artifacts["observer_test_sha256"],
            runtime_commit=bad_record["identities"]["image"]["runtime_commit"],
            oci_index=bad_record["identities"]["image"]["oci_index"],
            config_digest=bad_record["identities"]["image"]["config_digest"],
        )


def test_release_binding_rejects_daemon_and_observer_artifact_mismatch(tmp_path: Path) -> None:
    _config_value, record_payload, journal_payload, gate, gate_payload, candidate, candidate_payload = _build_short_pair(tmp_path)
    record = ev.verify_pair(record_payload, journal_payload, minimum_duration_ns=10_000_000_000)
    record_sha = hashlib.sha256(record_payload).hexdigest(); candidate_sha = hashlib.sha256(candidate_payload).hexdigest(); gate_sha = hashlib.sha256(gate_payload).hexdigest()
    artifacts = record["identities"]["artifacts"]
    common = dict(
        binding=ev.binding_projection(record, record_sha), record=record, record_sha256=record_sha,
        gate=gate, gate_sha256=gate_sha, candidate=candidate, candidate_sha256=candidate_sha,
        evidence_sha256=artifacts["evidence_sha256"], observer_sha256=artifacts["observer_sha256"],
        observer_test_sha256=artifacts["observer_test_sha256"], runtime_commit=record["identities"]["image"]["runtime_commit"],
        oci_index=record["identities"]["image"]["oci_index"], config_digest=record["identities"]["image"]["config_digest"],
    )
    ev.verify_release_bindings(**common)
    bad_candidate = json.loads(json.dumps(candidate)); bad_candidate["docker_daemon_identity_sha256"] = "0" * 64
    with pytest.raises(ev.EvidenceError, match="docker_daemon_identity_changed_between_gate_and_soak"):
        ev.verify_release_bindings(**dict(common, candidate=bad_candidate))
    with pytest.raises(ev.EvidenceError, match="soak_source_identity_changed"):
        ev.verify_release_bindings(**dict(common, observer_sha256="0" * 64))


def test_binding_duration_allows_clock_precision_residue(tmp_path: Path) -> None:
    _config_value, record_payload, journal_payload, *_ = _build_short_pair(tmp_path)
    record = ev.verify_pair(record_payload, journal_payload, minimum_duration_ns=10_000_000_000)
    binding = ev.binding_projection(record, hashlib.sha256(record_payload).hexdigest())
    binding["ended_utc"] = "2026-09-05T12:00:00.000000Z"
    binding["duration_ns"] = ev.MIN_DURATION_NS + 123
    evidence = ev.BINDING_PREFIX.encode("ascii") + ev.canonical_line(binding)
    assert ev.parse_binding(evidence, ev.BINDING_PREFIX)["duration_ns"] == binding["duration_ns"]
    binding["duration_ns"] += ev.UTC_DRIFT_NS + 1
    with pytest.raises(ev.EvidenceError, match="binding_duration_disagreed_with_utc"):
        ev.parse_binding(ev.BINDING_PREFIX.encode("ascii") + ev.canonical_line(binding), ev.BINDING_PREFIX)


def test_shared_host_resource_probes_accept_only_the_bound_safe_envelope() -> None:
    meminfo = b"MemTotal:       25165824 kB\nMemAvailable:   8388608 kB\n"
    psi = b"some avg10=0.00 avg60=0.01 avg300=0.02 total=123\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=4\n"
    observer._validate_host_memory(meminfo, psi, 4 * 1024**3)
    observer._validate_nvidia(
        b"GPU-11111111-1111-4111-8111-111111111111, 24576, 18111\n",
        gpu_uuid="GPU-11111111-1111-4111-8111-111111111111",
        total_bytes=24_576 * 1024**2,
        free_reserve_bytes=2 * 1024**3,
    )
    observer._validate_ollama({"models": []})
    with pytest.raises(observer.ObserverError): observer._validate_host_memory(meminfo, psi, 9 * 1024**3)
    with pytest.raises(observer.ObserverError):
        observer._validate_nvidia(
            b"GPU-11111111-1111-4111-8111-111111111111, 24576, 1024\n",
            gpu_uuid="GPU-11111111-1111-4111-8111-111111111111",
            total_bytes=24_576 * 1024**2,
            free_reserve_bytes=2 * 1024**3,
        )
    with pytest.raises(observer.ObserverError): observer._validate_ollama({"models": [{"name": "qwen"}]})


def test_mqtt_semantic_binding_is_stable_and_excludes_private_values() -> None:
    environment = _mqtt_environment()
    settings = observer._mqtt_settings_from_environment(environment)
    binding = settings.binding
    assert binding == observer._mqtt_binding(binding)
    assert binding["enabled"] is True
    assert binding["refresh_seconds"] == 60
    assert binding["library_label_policy"] == "custom"
    assert repr(settings) == "_MqttSettings(<redacted>)"
    serialized = observer._canonical_bytes(binding)
    for private in (
        environment["MQTT_HOST"],
        environment["MQTT_USERNAME"],
        environment["MQTT_PASSWORD"],
        environment["MQTT_TOPIC_PREFIX"],
        environment["MQTT_INVENTORY_NODE_ID"],
        environment["MQTT_INVENTORY_LIBRARY_NAMES"],
        environment["TRANSCRIBE_FOLDERS"],
    ):
        assert private.encode("utf-8") not in serialized

    credentials_changed = dict(environment)
    credentials_changed["MQTT_USERNAME"] = "different-user"
    credentials_changed["MQTT_PASSWORD"] = "different-password"
    credentials_changed["MQTT_INVENTORY_LIBRARY_NAMES"] = "A|B"
    assert (
        observer._mqtt_settings_from_environment(credentials_changed).binding
        == binding
    )
    topic_changed = dict(environment)
    topic_changed["MQTT_TOPIC_PREFIX"] = "private/subgen-two"
    assert (
        observer._mqtt_settings_from_environment(topic_changed).binding
        != binding
    )
    disabled = observer._mqtt_settings_from_environment({})
    assert disabled.binding["enabled"] is False
    assert disabled.binding["library_label_policy"] == "generic"


def test_disabled_mqtt_never_opens_a_broker_connection() -> None:
    disabled = observer._mqtt_settings_from_environment({})
    called = False

    def socket_factory(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("disabled MQTT attempted network access")

    with pytest.raises(
        observer.ObserverError,
        match="disabled_mqtt_inventory_cannot_open_a_broker_probe",
    ):
        observer.MqttInventoryProbe(disabled, socket_factory=socket_factory)
    assert called is False


def test_enabled_mqtt_bootstrap_timeout_fails_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = observer._mqtt_settings_from_environment(_mqtt_environment())

    class SilentBroker:
        def __init__(self) -> None:
            self.response = bytearray(b"\x20\x02\x00\x00")

        def settimeout(self, _value: float) -> None:
            return None

        def sendall(self, _payload: bytes) -> None:
            return None

        def recv(self, size: int) -> bytes:
            if self.response:
                result = bytes(self.response[:size])
                del self.response[:size]
                return result
            raise observer.socket.timeout

        def shutdown(self, _how: int) -> None:
            return None

        def close(self) -> None:
            return None

    broker = SilentBroker()
    monkeypatch.setattr(observer, "MQTT_BOOTSTRAP_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(observer.ObserverError) as failure:
        observer.MqttInventoryProbe(
            settings,
            socket_factory=lambda *_args, **_kwargs: broker,
        )
    assert failure.value.code == "mqtt_inventory_bootstrap_was_incomplete"
    rendered = str(failure.value)
    assert "sentinel-user" not in rendered
    assert "sentinel-password" not in rendered
    assert "/private/media" not in rendered


def test_mqtt_retained_contract_requires_a_fresh_live_state() -> None:
    settings = observer._mqtt_settings_from_environment(_mqtt_environment())
    state = observer._MqttObservationState(settings)
    _seed_mqtt_observation(state, settings, live_state_ns=None)
    with pytest.raises(observer.ObserverError, match="mqtt_live_state_freshness_proof_was_missing"):
        state.health(10)
    state.observe(
        settings.state_topic,
        _mqtt_state_payload(items_left=0, scan_percent=50.0),
        retained=False,
        now_ns=20,
    )
    assert state.health(20 + observer.MQTT_MAX_STALE_NS)["state_fresh"] is True
    with pytest.raises(observer.ObserverError, match="mqtt_live_state_exceeded_its_freshness_bound"):
        state.health(21 + observer.MQTT_MAX_STALE_NS)

    missing = observer._MqttObservationState(settings)
    missing.observe_suback()
    missing.observe(settings.availability_topic, b"online", retained=True, now_ns=1)
    with pytest.raises(observer.ObserverError, match="mqtt_retained_discovery_was_missing"):
        missing.health(1)
    with pytest.raises(observer.ObserverError, match="mqtt_retained_availability_was_not_online"):
        state.observe(settings.availability_topic, b"offline", retained=True, now_ns=1)


@pytest.mark.parametrize(
    "payload",
    [
        _mqtt_state_payload(items_left=-1),
        _mqtt_state_payload(scan_percent=100.1),
        b'{"items_left":1,"items_left":2,"scan_percent":0.0,"scan_complete":false,"scan_errors":0,"libraries":{}}',
        b'{"items_left":1, "libraries":{},"scan_complete":false,"scan_errors":0,"scan_percent":0.0}',
        _mqtt_state_payload(label="/private/media"),
        _mqtt_state_payload(label="sentinel-password"),
        _mqtt_state_payload(label=" Private  Movies "),
        _mqtt_state_payload(label="Bad\x01Label"),
    ],
)
def test_mqtt_state_rejects_malformed_ranges_and_private_leakage(payload: bytes) -> None:
    settings = observer._mqtt_settings_from_environment(_mqtt_environment())
    with pytest.raises(observer.ObserverError) as failure:
        observer._validate_mqtt_state(payload, settings)
    rendered = str(failure.value)
    assert "sentinel-password" not in rendered
    assert "/private/media" not in rendered


def test_mqtt_state_accepts_the_producer_maximum_library_count_only() -> None:
    settings = observer._mqtt_settings_from_environment(_mqtt_environment())
    libraries = {
        f"Library {index}": {"scanned": 0, "total": 0, "items_left": 0}
        for index in range(1, 129)
    }
    libraries["Other"] = {"scanned": 0, "total": 0, "items_left": 0}

    def payload() -> bytes:
        return observer._canonical_bytes(
            {
                "items_left": 0,
                "scan_percent": 0.0,
                "scan_complete": False,
                "scan_errors": 0,
                "libraries": libraries,
            }
        )[:-1]

    observer._validate_mqtt_state(payload(), settings)
    libraries["Extra"] = {"scanned": 0, "total": 0, "items_left": 0}
    with pytest.raises(observer.ObserverError, match="mqtt_inventory_library_fields_were_invalid"):
        observer._validate_mqtt_state(payload(), settings)


def test_mqtt_discovery_and_publish_qos_are_exact() -> None:
    settings = observer._mqtt_settings_from_environment(_mqtt_environment())
    items = observer._mqtt_expected_discovery(settings, scan=False)
    scan = observer._mqtt_expected_discovery(settings, scan=True)
    assert items["object_id"] == "subgen_items_left"
    assert scan["object_id"] == "subgen_scan"
    assert items["state_topic"] == settings.state_topic
    assert scan["availability_topic"] == settings.availability_topic
    altered = dict(items)
    altered["object_id"] = "wrong"
    with pytest.raises(observer.ObserverError, match="mqtt_discovery_identity_or_topic_was_invalid"):
        observer._validate_mqtt_discovery(
            observer._canonical_bytes(altered)[:-1],
            settings,
            scan=False,
        )

    probe = object.__new__(observer.MqttInventoryProbe)
    probe.settings = settings
    probe.state = observer._MqttObservationState(settings)
    probe.monotonic_ns = lambda: 10
    probe.received_qos1 = {}
    acknowledgements: list[bytes] = []
    probe._send = acknowledgements.append
    topic = settings.availability_topic.encode("utf-8")
    publication = len(topic).to_bytes(2, "big") + topic + b"\x00\x07online"
    probe._publish(0x33, publication)
    assert acknowledgements == [observer._mqtt_frame(0x40, b"\x00\x07")]
    with pytest.raises(observer.ObserverError, match="mqtt_broker_publication_framing_was_invalid"):
        probe._publish(0x31, publication)
    probe._publish(0x3B, publication)
    assert acknowledgements == [
        observer._mqtt_frame(0x40, b"\x00\x07"),
        observer._mqtt_frame(0x40, b"\x00\x07"),
    ]
    unknown_duplicate = (
        len(topic).to_bytes(2, "big") + topic + b"\x00\x08online"
    )
    with pytest.raises(observer.ObserverError, match="mqtt_duplicate_publication_identity_was_invalid"):
        probe._publish(0x3B, unknown_duplicate)
    with pytest.raises(observer.ObserverError, match="mqtt_duplicate_publication_identity_was_invalid"):
        probe._publish(0x3A, publication)


def test_enabled_mqtt_soak_evidence_is_redacted_and_tamper_evident(tmp_path: Path) -> None:
    settings = observer._mqtt_settings_from_environment(_mqtt_environment())
    _config_value, record_payload, journal_payload, *_ = _build_short_pair(
        tmp_path,
        mqtt_binding=settings.binding,
    )
    record = ev.verify_pair(
        record_payload,
        journal_payload,
        minimum_duration_ns=10_000_000_000,
    )
    assert record["mqtt_inventory"] == {
        **settings.binding,
        "outcome": "pass",
        "observation_count": 3,
        "healthy_all": True,
    }
    binding = ev.binding_projection(record, hashlib.sha256(record_payload).hexdigest())
    assert binding["mqtt_inventory_enabled"] is True
    assert binding["mqtt_semantic_config_sha256"] == settings.binding["semantic_config_sha256"]
    combined = record_payload + journal_payload + ev.canonical_line(binding)
    for private in (
        "sentinel-user",
        "sentinel-password",
        "Private Movies",
        "Private TV",
        "/private/media",
        "broker.private.invalid",
    ):
        assert private.encode("utf-8") not in combined

    documents = [json.loads(line) for line in journal_payload.splitlines()]
    documents[1]["mqtt_inventory"]["state_fresh"] = False
    with pytest.raises(ev.EvidenceError, match="mqtt_inventory_health_proof_failed"):
        ev.validate_journal(
            _rechain(documents),
            minimum_duration_ns=10_000_000_000,
        )


def test_disabled_mqtt_does_not_fail_the_general_soak(tmp_path: Path) -> None:
    _config_value, record_payload, journal_payload, *_ = _build_short_pair(tmp_path)
    record = ev.verify_pair(
        record_payload,
        journal_payload,
        minimum_duration_ns=10_000_000_000,
    )
    assert record["mqtt_inventory"]["enabled"] is False
    assert record["mqtt_inventory"]["outcome"] == "disabled"
    assert record["mqtt_inventory"]["observation_count"] == 0


def test_priority_signal_tracker_requires_fresh_contiguous_immutable_publications() -> None:
    tracker = observer.PrioritySignalTracker()
    tracker.observe(_priority_signal(sequence=7, observed=95, source_observed=90), now_ns=100, boot_sha256="1" * 64, policy_sha256="2" * 64)
    tracker.observe(_priority_signal(sequence=8, observed=105, source_observed=100, observation="c" * 64), now_ns=110, boot_sha256="1" * 64, policy_sha256="2" * 64)
    with pytest.raises(observer.ObserverError):
        tracker.observe(_priority_signal(sequence=10, observed=115, source_observed=110), now_ns=120, boot_sha256="1" * 64, policy_sha256="2" * 64)
    stale = observer.PrioritySignalTracker()
    with pytest.raises(observer.ObserverError):
        stale.observe(_priority_signal(sequence=1, observed=1, source_observed=1), now_ns=40_000_000_002, boot_sha256="1" * 64, policy_sha256="2" * 64)


def test_bounded_docker_follower_keeps_stdout_and_stderr_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    class Process:
        def __init__(self, argv: list[str], **kwargs: object) -> None:
            calls.append((argv, kwargs))
            self.stdout = io.BytesIO(b"2026-09-02T12:00:03.000000000Z ordinary stdout\n")
            self.stderr = io.BytesIO(_runtime_line())

        def wait(self, timeout: float | None = None) -> int: return 0
        def poll(self) -> int: return 0
        def terminate(self) -> None: pass
        def kill(self) -> None: pass

    monkeypatch.setattr(observer.subprocess, "Popen", Process)
    follower = observer.StreamFollower(
        "candidate", "1" * 64, "2026-09-02T12:00:00.000000Z",
        until="2026-09-02T12:00:05.000000Z",
    )
    events, _evidence = follower.wait_complete()
    follower.close()
    assert len(events) == 1
    assert calls[0][1]["stdout"] is observer.subprocess.PIPE
    assert calls[0][1]["stderr"] is observer.subprocess.PIPE
    assert observer.subprocess.STDOUT not in calls[0][1].values()
    assert "--until" in calls[0][0]


def test_runtime_event_on_stdout_or_both_streams_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        def __init__(self, _argv: list[str], **_kwargs: object) -> None:
            self.stdout = io.BytesIO(_runtime_line())
            self.stderr = io.BytesIO(_runtime_line())

        def wait(self, timeout: float | None = None) -> int: return 0
        def poll(self) -> int: return 0
        def terminate(self) -> None: pass
        def kill(self) -> None: pass

    monkeypatch.setattr(observer.subprocess, "Popen", Process)
    follower = observer.StreamFollower(
        "candidate", "1" * 64, "2026-09-02T12:00:00.000000Z",
        until="2026-09-02T12:00:05.000000Z",
    )
    with pytest.raises(observer.ObserverError, match="runtime_event_did_not_originate_from_candidate_stderr"):
        follower.wait_complete()
    follower.close()


def test_kernel_watermark_replays_from_the_exact_soak_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Process:
        def __init__(self, argv: list[str], **_kwargs: object) -> None:
            calls.append(argv)
            event = {"MESSAGE": "ordinary kernel line", "__CURSOR": "s=cursor", "__REALTIME_TIMESTAMP": "1788350404000000"}
            self.stdout = io.BytesIO(json.dumps(event, separators=(",", ":")).encode("ascii") + b"\n")
            self.stderr = io.BytesIO()

        def wait(self, timeout: float | None = None) -> int: return 0
        def poll(self) -> int: return 0
        def terminate(self) -> None: pass
        def kill(self) -> None: pass

    monkeypatch.setattr(observer.subprocess, "Popen", Process)
    follower = observer.StreamFollower(
        "kernel", None, "2026-09-02T12:00:00.000000Z",
        until="2026-09-02T12:00:05.000000Z",
    )
    follower.wait_complete(); follower.close()
    assert "--since" in calls[0] and "--until" in calls[0]
    assert "--lines=0" not in calls[0]


def test_final_watermark_replays_the_post_close_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    actions: list[tuple] = []
    empty_evidence = {key: set() for key in ("candidate_cuda_oom_log", "transcription_failure", "join_failure", "partial_output", "media_deleted", "marker_skipped", "frigate_health_breach", "nvidia_xid")}

    class Follower:
        def __init__(self, kind: str, _container_id: str | None, since: str, *, until: str | None = None) -> None:
            self.kind = kind; self.since = since; self.until = until
            actions.append(("open", kind, since, until))

        def wait_complete(self, timeout: float = 5.0):
            actions.append(("wait", self.kind, self.since, self.until)); return [], empty_evidence

        def close(self) -> None: actions.append(("close", self.kind, self.since, self.until))
        def drain(self): actions.append(("drain", self.kind)); return [], empty_evidence

    class Continuous:
        def __init__(self, kind: str) -> None: self.kind = kind
        def close(self) -> None: actions.append(("continuous-close", self.kind))
        def drain(self): actions.append(("continuous-drain", self.kind)); return [], empty_evidence

    probe = object.__new__(observer.LiveProbe)
    probe.candidate_id = "1" * 64; probe.frigate_id = "2" * 64
    probe.replay_since_utc = "2026-09-02T12:00:00.000000Z"
    probe.followers = [Continuous("candidate"), Continuous("frigate"), Continuous("kernel")]
    probe.log_evidence = {key: set() for key in empty_evidence}; probe.counters = {key: 0 for key in ev.COUNTER_KEYS}
    monkeypatch.setattr(observer, "StreamFollower", Follower)
    monkeypatch.setattr(observer.time, "monotonic_ns", lambda: 123_000_000_000)
    monkeypatch.setattr(observer, "_utc_now", lambda: "2026-09-02T12:00:06.000000Z")
    _events, watermark = probe._watermark_streams("2026-09-02T12:00:05.000000Z", final=True)
    assert watermark == (123_000_000_000, "2026-09-02T12:00:06.000000Z")
    opened_windows = [(item[2], item[3]) for item in actions if item[0] == "open" and item[1] == "candidate"]
    assert opened_windows == [
        ("2026-09-02T12:00:00.000000Z", "2026-09-02T12:00:05.000000Z"),
        ("2026-09-02T12:00:05.000000Z", "2026-09-02T12:00:06.000000Z"),
    ]
    assert actions.index(("continuous-close", "candidate")) < actions.index(("open", "candidate", "2026-09-02T12:00:05.000000Z", "2026-09-02T12:00:06.000000Z"))


def test_finalize_rejects_without_modifying_the_unfinalized_journal(tmp_path: Path) -> None:
    _config_value, _record_payload, finalized, *_ = _build_short_pair(tmp_path)
    unfinalized = b"".join(finalized.splitlines(keepends=True)[:-1])
    journal = (tmp_path / "retryable.journal").resolve(); journal.write_bytes(unfinalized)
    if os.name == "posix": journal.chmod(0o600)
    finalization = {
        "schema": "subgen.task11b.soak-finalization/v1", "outcome": "pass",
        "rollback": {"ready": True, "target_version": "0.3.0", "deletion_disabled": True, "repair_report_only": True, "record_sha256": "f" * 64},
    }
    record = (tmp_path / "retryable.record").resolve()
    with pytest.raises(ev.EvidenceError, match="journal_duration_or_final_gap_was_invalid"):
        observer.finalize(
            journal, record, finalization,
            utc_now=lambda: "2026-09-02T12:00:11.000000Z",
            monotonic_ns=lambda: 12_000_000_000,
        )
    assert journal.read_bytes() == unfinalized
    assert not record.exists()
    result = observer.finalize(journal, record, finalization, minimum_duration_ns=10_000_000_000)
    assert result["outcome"] == "pass" and record.exists()


def test_finalize_record_target_failure_leaves_journal_retryable(tmp_path: Path) -> None:
    _config_value, _record_payload, finalized, *_ = _build_short_pair(tmp_path)
    unfinalized = b"".join(finalized.splitlines(keepends=True)[:-1])
    journal = (tmp_path / "record-failure.journal").resolve(); journal.write_bytes(unfinalized)
    if os.name == "posix": journal.chmod(0o600)
    record = (tmp_path / "already-there.record").resolve(); record.write_bytes(b"reserved")
    if os.name == "posix": record.chmod(0o600)
    finalization = {
        "schema": "subgen.task11b.soak-finalization/v1", "outcome": "pass",
        "rollback": {"ready": True, "target_version": "0.3.0", "deletion_disabled": True, "repair_report_only": True, "record_sha256": "f" * 64},
    }
    with pytest.raises(observer.ObserverError, match="soak_record_already_existed"):
        observer.finalize(journal, record, finalization, minimum_duration_ns=10_000_000_000)
    assert journal.read_bytes() == unfinalized and record.read_bytes() == b"reserved"


def test_finalize_create_failure_cannot_finalize_the_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_value, _record_payload, finalized, *_ = _build_short_pair(tmp_path)
    unfinalized = b"".join(finalized.splitlines(keepends=True)[:-1])
    journal = (tmp_path / "create-failure.journal").resolve(); journal.write_bytes(unfinalized)
    if os.name == "posix": journal.chmod(0o600)
    record = (tmp_path / "create-failure.record").resolve()
    finalization = {
        "schema": "subgen.task11b.soak-finalization/v1", "outcome": "pass",
        "rollback": {"ready": True, "target_version": "0.3.0", "deletion_disabled": True, "repair_report_only": True, "record_sha256": "f" * 64},
    }
    original_create = observer._create
    attempts = 0

    def fail_once(path: Path, payload: bytes, label: str) -> None:
        nonlocal attempts
        if label == "soak record" and attempts == 0:
            attempts += 1
            raise observer.ObserverError("injected record create failure")
        original_create(path, payload, label)

    monkeypatch.setattr(observer, "_create", fail_once)
    with pytest.raises(observer.ObserverError, match="injected_record_create_failure"):
        observer.finalize(journal, record, finalization, minimum_duration_ns=10_000_000_000)
    assert journal.read_bytes() == unfinalized and not record.exists()
    result = observer.finalize(journal, record, finalization, minimum_duration_ns=10_000_000_000)
    assert result == ev.verify_pair(record.read_bytes(), journal.read_bytes(), minimum_duration_ns=10_000_000_000)


def test_finalize_recovers_a_missing_record_after_the_end_was_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_value, expected_record, finalized, *_ = _build_short_pair(tmp_path)
    journal = (tmp_path / "post-append-failure.journal").resolve(); journal.write_bytes(finalized)
    if os.name == "posix": journal.chmod(0o600)
    record = (tmp_path / "post-append-failure.record").resolve()
    finalization = {
        "schema": "subgen.task11b.soak-finalization/v1", "outcome": "pass",
        "rollback": {"ready": True, "target_version": "0.3.0", "deletion_disabled": True, "repair_report_only": True, "record_sha256": "f" * 64},
    }
    original_create = observer._create
    failed = False

    def fail_once(path: Path, payload: bytes, label: str) -> None:
        nonlocal failed
        if label == "soak record" and not failed:
            failed = True
            raise observer.ObserverError("injected post append record create failure")
        original_create(path, payload, label)

    monkeypatch.setattr(observer, "_create", fail_once)
    with pytest.raises(observer.ObserverError, match="injected_post_append_record_create_failure"):
        observer.finalize(journal, record, finalization, minimum_duration_ns=10_000_000_000)
    assert journal.read_bytes() == finalized and not record.exists()
    result = observer.finalize(journal, record, finalization, minimum_duration_ns=10_000_000_000)
    assert record.read_bytes() == expected_record
    assert result == ev.verify_pair(record.read_bytes(), journal.read_bytes(), minimum_duration_ns=10_000_000_000)


def test_priority_phase_tracker_rejects_timeout_and_non_normal_final() -> None:
    tracker = observer.PriorityPhaseTracker("normal", 0)
    tracker.observe("yielding", 1)
    tracker.observe("recovering", ev.MAX_NON_NORMAL_NS)
    with pytest.raises(observer.ObserverError, match="priority_controller_remained_non_normal"):
        tracker.observe("recovering", ev.MAX_NON_NORMAL_NS + 2)
    final = observer.PriorityPhaseTracker("yielding", 0)
    with pytest.raises(observer.ObserverError, match="final_priority_controller_phase_was_not_normal"):
        final.observe("yielding", 1, final=True)


def test_cleanup_stops_exact_bound_id_despite_runtime_config_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate_id = "1" * 64; config_digest = "sha256:" + "2" * 64
    running = {"Id": candidate_id, "Image": config_digest, "RestartCount": 0, "State": {"Running": True, "OOMKilled": False, "Pid": 42}}
    stopped = {"Id": candidate_id, "Image": config_digest, "RestartCount": 0, "State": {"Running": False, "OOMKilled": False, "Pid": 0}}
    inspections = iter((running, stopped)); commands: list[list[str]] = []
    monkeypatch.setattr(observer, "_docker_json", lambda *_parts, **_kwargs: next(inspections))
    monkeypatch.setattr(observer, "_command", lambda argv, *_args, **_kwargs: commands.append(argv) or b"stopped\n")
    observer._stop_bound_candidate(
        {"candidate_container_id": candidate_id, "candidate_runtime_config_sha256": "0" * 64},
        {"config_digest": config_digest},
    )
    assert commands == [[observer.DOCKER_BINARY, "--host", observer.DOCKER_HOST, "stop", "--time", "120", candidate_id]]


def test_audit_tracker_retries_short_reads_without_skipping_deletion_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit_path = (tmp_path / "monitor.log").resolve(); audit_path.write_bytes(b"")
    monkeypatch.setattr(observer, "MONITOR_AUDIT_PATH", audit_path)
    tracker = observer.AuditTracker()
    audit_path.write_bytes(
        b"2026-09-02T12:00:00Z [FAILURE_MARKER_CREATED] private path discarded\n"
        b"2026-09-02T12:00:01Z [FILE_DELETED] private path discarded\n"
    )
    original_read = observer.os.read
    monkeypatch.setattr(observer.os, "read", lambda fd, amount: original_read(fd, min(amount, 1)))
    assert tracker.sample() == {"marker_created": 1, "marker_handled": 1, "media_deleted": 1}
    assert tracker.offset == audit_path.stat().st_size


def test_repair_units_require_report_only_effective_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = (tmp_path / "monitor.env").resolve()
    env_path.write_text(
        "AUTO_DELETE_INVALID_MEDIA=false\nAUTO_DELETE_FAILED_FILES=false\nAUTO_MARK_FAILED_FILES=true\nAUTO_MARK_MIN_FAILURES=1\nSUBGEN_REPAIR_ACTION=report\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(observer, "MONITOR_ENV_PATH", env_path)
    empty = {key: "" for key in observer.SYSTEMD_SHOW_FIELDS}
    repair = dict(empty, Id=observer.REPAIR_SERVICE, ActiveState="inactive", SubState="dead", MainPID="0", ExecStart=f"{{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 {observer.REPAIR_HELPER_PATH.as_posix()} ; ignore_errors=no ; }}", WorkingDirectory="/opt/subgen", EnvironmentFiles=f"{env_path.as_posix()} (ignore_errors=no)")
    timer = dict(empty, Id=observer.REPAIR_TIMER, ActiveState="active", SubState="waiting", MainPID="0", Unit=observer.REPAIR_SERVICE)
    states = {observer.REPAIR_SERVICE: repair, observer.REPAIR_TIMER: timer}
    observer._validate_repair_units(states)
    override = (tmp_path / "override.env").resolve(); override.write_text("SUBGEN_REPAIR_ACTION=delete\n", encoding="utf-8")
    repair["EnvironmentFiles"] += f" {override.as_posix()} (ignore_errors=no)"
    with pytest.raises(observer.ObserverError, match="repair_effective_environment_was_unsafe"):
        observer._validate_repair_units(states)
    repair["EnvironmentFiles"] = f"{env_path.as_posix()} (ignore_errors=no)"
    repair["ExecStart"] = f"{{ path=/bin/false ; argv[]=/usr/bin/python3 {observer.REPAIR_HELPER_PATH.as_posix()} ; ignore_errors=no ; }}"
    with pytest.raises(observer.ObserverError, match="repair_effective_command_was_unsafe"):
        observer._validate_repair_units(states)
    repair["ExecStart"] = f"{{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 {observer.REPAIR_HELPER_PATH.as_posix()} ; ignore_errors=no ; }}"
    for field in observer.SYSTEMD_EXEC_FIELDS:
        if field == "ExecStart": continue
        repair[field] = "{ path=/bin/false ; argv[]=/bin/false ; ignore_errors=no ; }"
        with pytest.raises(observer.ObserverError, match="repair_auxiliary_command_was_unsafe"):
            observer._validate_repair_units(states)
        repair[field] = ""
    timer["Unit"] = "unsafe-repair.service"
    with pytest.raises(observer.ObserverError, match="repair_timer_state_was_unsafe"):
        observer._validate_repair_units(states)
    timer["Unit"] = observer.REPAIR_SERVICE
    repair.update(ActiveState="active", SubState="running", MainPID="0")
    with pytest.raises(observer.ObserverError, match="repair_service_state_was_unsafe"):
        observer._validate_repair_units(states)
    repair.update(ActiveState="active", SubState="running", MainPID="123")
    safe_process = {
        "AUTO_DELETE_INVALID_MEDIA": "false", "AUTO_DELETE_FAILED_FILES": "false",
        "AUTO_MARK_FAILED_FILES": "true", "AUTO_MARK_MIN_FAILURES": "1", "SUBGEN_REPAIR_ACTION": "report",
    }
    monkeypatch.setattr(observer, "_process_environment", lambda _pid: dict(safe_process))
    observer._validate_repair_units(states)
    monkeypatch.setattr(observer, "_process_environment", lambda _pid: dict(safe_process, SUBGEN_REPAIR_ACTION="delete"))
    with pytest.raises(observer.ObserverError, match="running_repair_process_environment_was_unsafe"):
        observer._validate_repair_units(states)


def test_systemctl_show_captures_all_service_exec_surfaces_and_the_timer_target(monkeypatch: pytest.MonkeyPatch) -> None:
    queries: dict[str, tuple[str, ...]] = {}

    def show(argv: list[str], _label: str, **_kwargs: object) -> bytes:
        assert argv[:3] == [observer.SYSTEMCTL_BINARY, "show", "--all"]
        unit = argv[3]
        fields = tuple(item.removeprefix("--property=") for item in argv[4:])
        queries[unit] = fields
        values = {key: "" for key in fields}; values["Id"] = unit
        if unit == observer.REPAIR_TIMER: values["Unit"] = observer.REPAIR_SERVICE
        return ("\n".join(f"{key}={values[key]}" for key in fields) + "\n").encode("utf-8")

    monkeypatch.setattr(observer, "_command", show)
    service = observer._systemctl_show(observer.REPAIR_SERVICE)
    timer = observer._systemctl_show(observer.REPAIR_TIMER)
    assert set(service) == set(observer.SYSTEMD_SHOW_FIELDS) == set(timer)
    assert set(observer.SYSTEMD_EXEC_FIELDS) <= set(queries[observer.REPAIR_SERVICE])
    assert "Unit" not in queries[observer.REPAIR_SERVICE] and service["Unit"] == ""
    assert queries[observer.REPAIR_TIMER] == observer.SYSTEMD_BASE_FIELDS + ("Unit",)
    assert not (set(observer.SYSTEMD_SERVICE_STATE_FIELDS) & set(queries[observer.REPAIR_TIMER]))
    assert timer["Unit"] == observer.REPAIR_SERVICE


def test_journal_treats_completion_generation_as_telemetry_but_rejects_reorder_and_non_normal_final(tmp_path: Path) -> None:
    _config_value, _record_payload, journal, *_ = _build_short_pair(tmp_path)
    original = [ev.strict_line(line) for line in journal.splitlines(keepends=True)]
    completion_regression = json.loads(json.dumps(original))
    completion_regression[-2]["completion_generation"] = completion_regression[1]["completion_generation"] - 2
    regressed_payload = _rechain(completion_regression)
    ev.validate_journal(regressed_payload, minimum_duration_ns=10_000_000_000)
    assert ev.derive_record(regressed_payload, minimum_duration_ns=10_000_000_000)["transcription"]["completion_delta"] == -2
    reordered = json.loads(json.dumps(original))
    reordered[1], reordered[2] = reordered[2], reordered[1]
    with pytest.raises(ev.EvidenceError, match="sample_index_was_not_contiguous"):
        ev.validate_journal(_rechain(reordered), minimum_duration_ns=10_000_000_000)
    non_normal = json.loads(json.dumps(original))
    non_normal[-2]["controller_phase"] = "yielding"
    with pytest.raises(ev.EvidenceError, match="journal_did_not_prove_an_idle_multi_chunk_atomic_completion"):
        ev.validate_journal(_rechain(non_normal), minimum_duration_ns=10_000_000_000)


def test_runtime_event_sequence_is_the_sole_ordered_completion_authority(tmp_path: Path) -> None:
    _config_value, _record_payload, journal, *_ = _build_short_pair(tmp_path)
    original = [ev.strict_line(line) for line in journal.splitlines(keepends=True)]
    workload_index = next(index for index, item in enumerate(original) if item["schema"] == "subgen.task11b.soak-workload/v1")
    second = json.loads(json.dumps(original[workload_index])); second["event_sequence"] = 2
    second["source_event_sha256"] = "d" * 64; second["workload_id_sha256"] = "e" * 64
    two_events = original[: workload_index + 1] + [second] + original[workload_index + 1 :]
    assert len(ev.validate_journal(_rechain(json.loads(json.dumps(two_events))), minimum_duration_ns=10_000_000_000).workloads) == 2
    duplicate = json.loads(json.dumps(two_events)); duplicate[workload_index + 1]["event_sequence"] = 1
    with pytest.raises(ev.EvidenceError, match="workload_event_sequence_did_not_advance"):
        ev.validate_journal(_rechain(duplicate), minimum_duration_ns=10_000_000_000)
    reordered = json.loads(json.dumps(two_events)); reordered[workload_index]["event_sequence"] = 2; reordered[workload_index + 1]["event_sequence"] = 1
    with pytest.raises(ev.EvidenceError, match="workload_event_sequence_did_not_advance"):
        ev.validate_journal(_rechain(reordered), minimum_duration_ns=10_000_000_000)


def test_pair_verifier_rejects_truncation_hash_tamper_and_record_mismatch(tmp_path: Path) -> None:
    _config_value, record_payload, journal, *_ = _build_short_pair(tmp_path)
    with pytest.raises(ev.EvidenceError, match="journal_size_or_termination_was_invalid"):
        ev.verify_pair(record_payload, journal[:-1], minimum_duration_ns=10_000_000_000)
    documents = [ev.strict_line(line) for line in journal.splitlines(keepends=True)]
    documents[2]["previous_record_sha256"] = "0" * 64
    tampered_journal = b"".join(ev.canonical_line(item) for item in documents)
    with pytest.raises(ev.EvidenceError, match="journal_index_or_hash_chain_was_invalid"):
        ev.verify_pair(record_payload, tampered_journal, minimum_duration_ns=10_000_000_000)
    bad_record = ev.strict_line(record_payload); bad_record["markers"]["created_count"] += 1
    with pytest.raises(ev.EvidenceError, match="record_did_not_equal_journal_derivation"):
        ev.verify_pair(ev.canonical_line(bad_record), journal, minimum_duration_ns=10_000_000_000)
    prefix, suffix = b'{"padding":"', b'"}\n'
    oversized = prefix + b"x" * (ev.MAX_JOURNAL_BYTES + 1 - len(prefix) - len(suffix)) + suffix
    assert len(oversized) == ev.MAX_JOURNAL_BYTES + 1 and oversized.endswith(b"\n")
    assert len(json.loads(oversized)["padding"]) == len(oversized) - len(prefix) - len(suffix)
    with pytest.raises(ev.EvidenceError, match="journal_size_or_termination_was_invalid"):
        ev.validate_journal(oversized, minimum_duration_ns=10_000_000_000)


def test_otherwise_valid_journal_is_rejected_only_for_crossing_the_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_value, record_payload, journal, *_ = _build_short_pair(tmp_path)
    assert ev.verify_pair(record_payload, journal, minimum_duration_ns=10_000_000_000)
    monkeypatch.setattr(ev, "MAX_JOURNAL_BYTES", len(journal) - 1)
    with pytest.raises(ev.EvidenceError, match="journal_size_or_termination_was_invalid"):
        ev.validate_journal(journal, minimum_duration_ns=10_000_000_000)


def test_observe_constructor_failure_still_stops_bound_candidate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = {"live": {"candidate_container_id": "1" * 64}}
    identities = {"image": {"config_digest": "sha256:" + "2" * 64}}
    stopped: list[tuple[dict, dict]] = []
    handlers: dict[int, object] = {}
    monkeypatch.setattr(observer, "_load_json", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(observer, "_validate_config", lambda *_args, **_kwargs: identities)
    monkeypatch.setattr(observer, "LiveProbe", lambda *_args, **_kwargs: (_ for _ in ()).throw(observer.ObserverError("preflight failed")))
    monkeypatch.setattr(observer, "_stop_bound_candidate", lambda live, image: stopped.append((live, image)))
    monkeypatch.setattr(observer.signal, "getsignal", lambda signum: handlers.get(signum, "old"))
    monkeypatch.setattr(observer.signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    args = argparse.Namespace(config=tmp_path / "config", observer_test_source=Path(__file__), journal=(tmp_path / "soak.journal").resolve())
    with pytest.raises(observer.ObserverError, match="preflight_failed"):
        observer._observe(args)
    assert stopped == [(config["live"], identities["image"])]
    assert handlers and set(handlers.values()) == {"old"}


@pytest.mark.parametrize("signal_name", ("SIGTERM", "SIGHUP"))
def test_observe_delivered_signal_stops_candidate_and_restores_handlers(signal_name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if not hasattr(observer.signal, "SIGHUP"):
        monkeypatch.setattr(observer.signal, "SIGHUP", 4242, raising=False)
    delivered_signal = getattr(observer.signal, signal_name)
    config = {"live": {"candidate_container_id": "1" * 64}}
    identities = {"image": {"config_digest": "sha256:" + "2" * 64}}
    stopped: list[tuple[dict, dict]] = []
    installed: dict[int, object] = {}
    previous = {observer.signal.SIGTERM: "old-term", observer.signal.SIGHUP: "old-hup"}
    transitions: list[tuple[int, object]] = []
    monkeypatch.setattr(observer, "_load_json", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(observer, "_validate_config", lambda *_args, **_kwargs: identities)
    monkeypatch.setattr(observer.signal, "getsignal", lambda signum: previous[signum])

    def set_handler(signum: int, handler: object) -> None:
        transitions.append((signum, handler)); installed[signum] = handler

    monkeypatch.setattr(observer.signal, "signal", set_handler)

    class DeliverSignal:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            handler = installed[delivered_signal]
            assert callable(handler)
            handler(delivered_signal, None)

    monkeypatch.setattr(observer, "LiveProbe", DeliverSignal)
    monkeypatch.setattr(observer, "_stop_bound_candidate", lambda live, image: stopped.append((live, image)))
    args = argparse.Namespace(config=tmp_path / "config", observer_test_source=Path(__file__), journal=(tmp_path / "signal.journal").resolve())
    with pytest.raises(observer.ObserverError, match=f"live_soak_received_termination_signal_{delivered_signal}"):
        observer._observe(args)
    assert stopped == [(config["live"], identities["image"])]
    assert installed == previous
    for signum in previous:
        assert callable(next(handler for candidate, handler in transitions if candidate == signum))


@pytest.mark.skipif(os.name != "posix", reason="real POSIX signal delivery is unavailable on this host")
@pytest.mark.parametrize("signal_name", ("SIGTERM", "SIGHUP"))
def test_observe_real_posix_signal_delivery_restores_the_prior_handler(signal_name: str) -> None:
    script = textwrap.dedent(
        f"""
        import argparse
        import importlib.util
        import os
        import signal
        import sys
        from pathlib import Path

        source = Path({str(SOURCE.resolve())!r})
        spec = importlib.util.spec_from_file_location("task11b_real_signal_test", source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        delivered = getattr(signal, {signal_name!r})
        restored = []
        def prior_handler(signum, _frame):
            restored.append(signum)
        signal.signal(delivered, prior_handler)

        config = {{"live": {{"candidate_container_id": "1" * 64}}}}
        identities = {{"image": {{"config_digest": "sha256:" + "2" * 64}}}}
        stopped = []
        module._load_json = lambda *_args, **_kwargs: config
        module._validate_config = lambda *_args, **_kwargs: identities
        module._stop_bound_candidate = lambda live, image: stopped.append((live, image))

        class DeliverRealSignal:
            def __init__(self, *_args, **_kwargs):
                os.kill(os.getpid(), delivered)
                raise AssertionError("registered signal handler did not run")

        module.LiveProbe = DeliverRealSignal
        args = argparse.Namespace(
            config=Path("/tmp/task11b-signal-config"),
            observer_test_source=source.with_name("test_soak_runtime_observer.py"),
            journal=Path("/tmp/task11b-signal-journal"),
        )
        try:
            module._observe(args)
        except module.ObserverError as exc:
            assert exc.code == f"live_soak_received_termination_signal_{{delivered}}"
        else:
            raise AssertionError("real signal did not abort observation")
        assert stopped == [(config["live"], identities["image"])]
        assert signal.getsignal(delivered) is prior_handler
        assert restored == []
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_full_72_hour_sample_boundary_fits_and_validates(tmp_path: Path) -> None:
    runtime_commit = "1" * 40; oci = "sha256:" + "2" * 64; digest = "sha256:" + "3" * 64
    candidate = _candidate(runtime_commit, oci, digest, ["sha256:" + "4" * 64])
    candidate_payload = ev.canonical_line(candidate); candidate_sha = hashlib.sha256(candidate_payload).hexdigest()
    gate = _gate(runtime_commit, oci, digest, ev.canonical_sha(candidate["candidate_identity"]["layer_diff_ids"]), candidate_sha)
    gate_payload = ev.canonical_line(gate)
    mqtt_binding = observer._mqtt_settings_from_environment(_mqtt_environment()).binding
    config = _config(
        tmp_path,
        hashlib.sha256(gate_payload).hexdigest(),
        candidate_sha,
        gate,
        candidate,
        mqtt_binding=mqtt_binding,
    )
    journal = _full_boundary_journal(config)
    assert len(journal) <= ev.MAX_JOURNAL_BYTES
    assert ev.MAX_JOURNAL_BYTES - len(journal) >= 16 * ev.MIB
    view = ev.validate_journal(journal)
    assert len(view.samples) == 51_841
    assert len(view.workloads) == 1
