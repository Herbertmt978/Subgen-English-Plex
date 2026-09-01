"""Focused regressions for the owner-operated Task 11B runtime observer."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest


WORK_DIR = Path(__file__).parent
sys.path.insert(0, str(WORK_DIR))
import gate_health_sampler as health  # noqa: E402
import runtime_gate_observer as observer  # noqa: E402

observer.health = health


def worker_event(
    event: str,
    path: str,
    *,
    source_identity: tuple[int, int, int, int, int] = (1, 2, 3, 4, 5),
) -> dict[str, object]:
    return {
        "event": event,
        "task_id": hashlib.sha256(f"transcribe:{path}".encode()).hexdigest()[:16],
        "task_type": "transcribe",
        "path": path,
        "source_identity": list(source_identity),
    }


def healthy_status() -> dict[str, object]:
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
            "gpu_total_bytes": 24 * health.GIB,
            "gpu_stabilized_free_bytes": 18 * health.GIB,
            "gpu_reserve_bytes": 8 * health.GIB,
            "gpu_allocatable_bytes": 10 * health.GIB,
        }
    }


def boundary(media_root: Path) -> health.BoundaryExpectation:
    document = {
        "schema": 3,
        "user": "1000:1000",
        "mounts": [
            {
                "source": str(media_root),
                "destination": "/media",
                "read_write": True,
                "mode": "rw",
            }
        ],
    }
    return health.BoundaryExpectation(document, "a" * 64, "b" * 64)


def fixture_document() -> dict[str, object]:
    return {
        "schema": observer.FIXTURE_SCHEMA,
        "long": {"media": "long/source.mkv", "subtitle": "long/source.en.srt"},
        "short_resident": [
            {"media": "short/one.mkv", "subtitle": "short/one.en.srt"},
            {"media": "short/two.mkv", "subtitle": "short/two.en.srt"},
        ],
        "reload": {
            "media": "reload/source.mkv",
            "subtitle": "reload/source.en.srt",
        },
        "invalid": {"media": "invalid/source.mkv"},
        "silent": {"media": "silent/source.mkv"},
    }


def create_fixture_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[observer.FixtureSet, Path]:
    monkeypatch.setattr(observer, "_owner_id", lambda: None)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    media = tmp_path / "media"
    media.mkdir(mode=0o700)
    for directory in sorted(observer.EXPECTED_FIXTURE_DIRECTORIES):
        (media / directory).mkdir(mode=0o700)
    document = fixture_document()
    manifest = private / "fixtures.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    manifest.chmod(0o600)
    for role, content in {
        "long/source.mkv": b"long-source",
        "short/one.mkv": b"short-one",
        "short/two.mkv": b"short-two",
        "reload/source.mkv": b"reload-source",
        "invalid/source.mkv": b"invalid-source",
        "silent/source.mkv": b"silent-source",
    }.items():
        target = media / role
        target.write_bytes(content)
        target.chmod(0o600)
    fixtures = observer.load_fixture_manifest(
        manifest.resolve(), boundary(media.resolve())
    )
    return fixtures, manifest


class FakeEvidenceWriter:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.seals: list[dict[str, object]] = []
        self.closed = False

    def write(self, record: dict[str, object]) -> None:
        self.records.append(record)

    def seal(self, **record: object) -> str:
        self.seals.append(record)
        self.closed = True
        return "f" * 64

    def close(self) -> None:
        self.closed = True


def test_runtime_status_accepts_only_healthy_or_idle_recovery() -> None:
    healthy = observer.validate_runtime_status(
        healthy_status(),
        expected_model="medium",
        expected_reserve_bytes=8 * health.GIB,
        observed_gpu_total_bytes=24 * health.GIB,
        allow_idle_recovery=False,
    )
    assert (healthy["controller_state"], healthy["admission_open"]) == (
        "normal",
        True,
    )

    recovering_payload = copy.deepcopy(healthy_status())
    recovering_resource = recovering_payload["resource_management"]
    assert isinstance(recovering_resource, dict)
    recovering_resource.update(
        {
            "controller_state": "recovering",
            "recovery_reason": "idle_cleanup",
            "admission_open": False,
        }
    )
    recovering = observer.validate_runtime_status(
        recovering_payload,
        expected_model="medium",
        expected_reserve_bytes=8 * health.GIB,
        observed_gpu_total_bytes=24 * health.GIB,
        allow_idle_recovery=True,
    )
    assert recovering["controller_state"] == "recovering"
    assert recovering["recovery_reason"] == "idle_cleanup"
    assert recovering["admission_open"] is False

    for state, reason, admission, allowed in (
        ("recovering", "idle_cleanup", False, False),
        ("recovering", "memory_pressure", False, True),
        ("yielding", "memory_pressure", False, True),
        ("normal", None, False, True),
    ):
        payload = copy.deepcopy(healthy_status())
        resource = payload["resource_management"]
        assert isinstance(resource, dict)
        resource.update(
            {
                "controller_state": state,
                "recovery_reason": reason,
                "admission_open": admission,
            }
        )
        with pytest.raises(health.GateAbort):
            observer.validate_runtime_status(
                payload,
                expected_model="medium",
                expected_reserve_bytes=8 * health.GIB,
                observed_gpu_total_bytes=24 * health.GIB,
                allow_idle_recovery=allowed,
            )


def test_runtime_chunk_policy_is_explicit_and_exact() -> None:
    item = {
        "Env": [
            "AUTO_DELETE_INVALID_MEDIA=false",
            "AUTO_DELETE_FAILED_FILES=false",
            "SKIP_STARTUP_SCAN=True",
            "MONITOR=False",
            "PROCESS_ADDED_MEDIA=False",
            "PROCESS_MEDIA_ON_PLAY=False",
            "SEGMENTATION_ENABLED=True",
            "SEGMENTATION_CHUNK_MINUTES=5",
        ]
    }
    assert observer.validate_runtime_chunk_policy(item, expected_chunk_minutes=5) == {
        "segmentation_enabled": True,
        "chunk_minutes": 5,
        "skip_startup_scan": True,
        "monitor": False,
        "process_added_media": False,
        "process_media_on_play": False,
    }

    for replacement in (
        "SEGMENTATION_CHUNK_MINUTES=auto",
        "SEGMENTATION_CHUNK_MINUTES=20",
        "SEGMENTATION_ENABLED=False",
    ):
        changed = copy.deepcopy(item)
        environment = changed["Env"]
        assert isinstance(environment, list)
        key = replacement.split("=", 1)[0] + "="
        environment[:] = [
            replacement if value.startswith(key) else value for value in environment
        ]
        with pytest.raises(health.GateAbort, match="segmentation|chunk_policy"):
            observer.validate_runtime_chunk_policy(changed, expected_chunk_minutes=5)

    for key, bad_value in (
        ("SKIP_STARTUP_SCAN", None),
        ("SKIP_STARTUP_SCAN", "False"),
        ("SKIP_STARTUP_SCAN", "true"),
        ("MONITOR", None),
        ("MONITOR", "True"),
        ("PROCESS_ADDED_MEDIA", None),
        ("PROCESS_ADDED_MEDIA", "True"),
        ("PROCESS_MEDIA_ON_PLAY", None),
        ("PROCESS_MEDIA_ON_PLAY", "True"),
    ):
        changed = copy.deepcopy(item)
        environment = changed["Env"]
        assert isinstance(environment, list)
        environment[:] = [
            value for value in environment if not value.startswith(key + "=")
        ]
        if bad_value is not None:
            environment.append(f"{key}={bad_value}")
        with pytest.raises(health.GateAbort, match="isolation|startup"):
            observer.validate_runtime_chunk_policy(changed, expected_chunk_minutes=5)

    for alias in ("PROCADDEDMEDIA=False", "PROCMEDIAONPLAY=False"):
        changed = copy.deepcopy(item)
        environment = changed["Env"]
        assert isinstance(environment, list)
        environment.append(alias)
        with pytest.raises(health.GateAbort, match="isolation"):
            observer.validate_runtime_chunk_policy(changed, expected_chunk_minutes=5)


def test_request_isolation_requires_matching_private_candidate_api_key() -> None:
    secret = "owner-only-request-key"
    item = {
        "Env": [
            "AUTO_DELETE_INVALID_MEDIA=false",
            "AUTO_DELETE_FAILED_FILES=false",
            f"SUBGEN_API_KEY={secret}",
        ]
    }
    observer.validate_request_isolation(item, secret)

    for changed in (
        {"Env": item["Env"][:-1]},
        {
            "Env": [
                "AUTO_DELETE_INVALID_MEDIA=false",
                "AUTO_DELETE_FAILED_FILES=false",
                "SUBGEN_API_KEY=different-owner-key",
            ]
        },
    ):
        with pytest.raises(health.GateAbort, match="request_isolation") as caught:
            observer.validate_request_isolation(changed, secret)
        assert secret not in str(caught.value)

    with pytest.raises(health.GateAbort, match="api_key_file"):
        observer._read_api_key(None)


def test_idle_recovery_proof_requires_three_complete_spaced_polls() -> None:
    tracker = observer.RuntimeCycleTracker()
    mark = tracker.mark()
    tracker.observe(
        {
            "controller_state": "recovering",
            "recovery_reason": "idle_cleanup",
            "admission_open": False,
        },
        100.0,
    )
    for moment in (105.0, 110.0):
        tracker.observe(
            {
                "controller_state": "recovering",
                "recovery_reason": "idle_cleanup",
                "admission_open": False,
            },
            moment,
        )
    assert tracker.idle_recovery_proof(mark) is None
    tracker.observe(
        {
            "controller_state": "normal",
            "recovery_reason": None,
            "admission_open": True,
        },
        115.0,
    )
    proof = tracker.idle_recovery_proof(mark)
    assert proof is not None
    assert proof.complete_health_polls == 3
    assert proof.elapsed_seconds == 15.0


def test_idle_recovery_rejects_reopening_before_three_health_polls() -> None:
    tracker = observer.RuntimeCycleTracker()
    mark = tracker.mark()
    tracker.observe(
        {
            "controller_state": "recovering",
            "recovery_reason": "idle_cleanup",
            "admission_open": False,
        },
        100.0,
    )
    tracker.observe(
        {
            "controller_state": "normal",
            "recovery_reason": None,
            "admission_open": True,
        },
        105.0,
    )
    with pytest.raises(health.GateAbort, match="three_complete"):
        tracker.idle_recovery_proof(mark)


def test_evidence_binds_separate_observer_and_sampler_without_private_values() -> None:
    raw = FakeEvidenceWriter()
    token = "task11b-private-token"
    host_path = "/var/lib/private/fixture"
    camera_name = "Private Camera"
    evidence = observer.LockedEvidence(raw, (token, host_path, camera_name))
    evidence.write(
        {
            "event": "observer_start",
            "observer_sha256": "1" * 64,
            "sampler_sha256": "2" * 64,
            "gate_token_sha256": "3" * 64,
            "camera_count": 15,
        }
    )
    evidence.seal(
        outcome="pass",
        sampler_sha256="2" * 64,
        image_config="sha256:" + "4" * 64,
        cleanup={"verified_stopped": True},
    )
    serialized = json.dumps(raw.records)
    assert "1" * 64 in serialized
    assert "2" * 64 in serialized
    assert token not in serialized
    assert host_path not in serialized
    assert camera_name not in serialized
    assert raw.seals[0]["sampler_sha256"] == "2" * 64

    with pytest.raises(health.GateAbort, match="privacy"):
        observer.LockedEvidence(FakeEvidenceWriter(), (token,)).write(
            {"event": "bad", "value": f"prefix-{token}"}
        )


def test_sampler_checksum_is_checked_before_sampler_bytes_execute() -> None:
    sentinel = object()

    def fake_read(path: Path, *, maximum: int, label: str):
        assert maximum == observer.MAX_SAMPLER_SOURCE_BYTES
        if path.name == "runtime_gate_observer.py":
            return b"observer", "1" * 64
        assert label == "frozen health sampler"
        return b"raise AssertionError('must not execute')", "2" * 64

    with (
        mock.patch.object(observer, "health", sentinel),
        mock.patch.object(
            observer, "_read_source_bytes_independently", side_effect=fake_read
        ),
        pytest.raises(observer.ObserverBootstrapAbort, match="sampler_checksum"),
    ):
        observer._bootstrap_verified_runtime(
            ["--observer-sha256", "1" * 64, "--sampler-sha256", "3" * 64]
        )
    assert observer.health is health


def test_failure_after_binding_stops_only_the_bound_candidate() -> None:
    args = SimpleNamespace(
        expected_docker_daemon_id="daemon",
        expected_host_boot_id="00000000-0000-4000-8000-000000000000",
        observer_sha256="1" * 64,
        sampler_sha256="2" * 64,
        expected_chunk_minutes=5,
    )
    binding = health.CandidateBinding(
        name="subgen-task11b-runtime-test",
        container_id="a" * 64,
        image_config="sha256:" + "b" * 64,
        runtime_commit="c" * 40,
        gate_role="runtime-auto",
        gate_token_digest="d" * 64,
        command_digest="e" * 64,
        boundary_digest="f" * 64,
    )
    stopped = {"verified_stopped": True}
    client = mock.Mock()
    client.inspect.return_value = {"Id": binding.container_id}
    with (
        mock.patch.object(
            observer,
            "_verified_runtime_identities",
            return_value=(args.observer_sha256, args.sampler_sha256),
        ),
        mock.patch.object(health, "DockerClient", return_value=client),
        mock.patch.object(health, "install_signal_handlers", return_value={}),
        mock.patch.object(
            health, "ensure_boundary_expectation", return_value=mock.Mock()
        ),
        mock.patch.object(health, "bind_candidate", return_value=binding),
        mock.patch.object(
            observer,
            "validate_runtime_chunk_policy",
            side_effect=health.GateAbort("policy failure"),
        ),
        mock.patch.object(health, "stop_bound_candidate", return_value=stopped) as stop,
        pytest.raises(health.GateAbort, match="policy_failure"),
    ):
        observer.run_observer(args)
    stop.assert_called_once()
    assert stop.call_args.args[1] is binding


def test_invalid_and_silent_events_attest_retention_without_paths_in_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures, _manifest = create_fixture_tree(tmp_path, monkeypatch)
    originals = observer.capture_original_snapshots(fixtures)
    scanner = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        (item.container_media for item in fixtures.all_items),
    )
    invalid_event = {
        "event": "media_validation_failed",
        "path": fixtures.invalid.container_media,
        "failure_class": "invalid_media",
        "validator_outcomes": {
            "ffprobe": "invalid_format",
            "pyav": "invalid_format",
        },
    }
    scanner.parse_lines(
        "INFO SUBGEN_EVENT "
        + json.dumps(invalid_event, separators=(",", ":"))
        + "\nINFO MEDIA_VALIDATION outcome=no_audio ffprobe=no_audio "
        f"pyav=no_audio path={fixtures.silent.container_media}\n"
    )
    assert scanner.matching_events(
        "media_validation_failed", [fixtures.invalid.container_media]
    ) == [invalid_event]
    assert fixtures.silent.container_media in scanner.silent_paths
    observer.attest_originals_unchanged(fixtures, originals)
    observer.assert_fixture_directory_exact(fixtures, "invalid", allow_outputs=False)
    observer.assert_fixture_directory_exact(fixtures, "silent", allow_outputs=False)

    mixed = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        [fixtures.silent.container_media],
    )
    mixed.parse_lines(
        "INFO MEDIA_VALIDATION outcome=no_audio ffprobe=timeout "
        f"pyav=no_audio path={fixtures.silent.container_media}\n"
    )
    assert mixed.silent_paths == set()


def test_incremental_event_overlap_does_not_duplicate_events_or_unloads() -> None:
    scanner = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        ["/media/short/one.mkv"],
    )
    line = (
        "2026-09-01T00:00:00Z INFO SUBGEN_EVENT "
        + json.dumps(worker_event("worker_finish", "/media/short/one.mkv"))
        + "\n2026-09-01T00:00:01Z INFO Model unloaded from memory\n"
    )
    scanner.parse_lines(line)
    scanner.parse_lines(line)
    assert len(scanner.events) == 1
    assert scanner.unload_count == 1


def test_phase_freshness_requires_new_worker_start_and_finish_after_mark() -> None:
    path = "/media/reload/source.mkv"
    item = observer.FixtureItem(
        role="reload",
        index=0,
        media_relative="reload/source.mkv",
        subtitle_relative="reload/source.en.srt",
    )
    scanner = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        [path],
    )
    scanner.parse_lines(
        "2026-09-01T00:00:00Z SUBGEN_EVENT "
        + json.dumps(worker_event("worker_start", path))
        + "\n2026-09-01T00:00:01Z SUBGEN_EVENT "
        + json.dumps(worker_event("worker_finish", path))
        + "\n"
    )
    mark = scanner.mark()
    assert not observer._worker_finished(scanner, (item,), after_mark=mark)

    scanner.parse_lines(
        "2026-09-01T00:01:00Z SUBGEN_EVENT "
        + json.dumps(worker_event("worker_start", path))
        + "\n2026-09-01T00:01:01Z SUBGEN_EVENT "
        + json.dumps(worker_event("worker_finish", path))
        + "\n"
    )
    assert observer._worker_finished(scanner, (item,), after_mark=mark)


def test_worker_lifecycle_rejects_a_changed_source_generation() -> None:
    path = "/media/reload/source.mkv"
    item = observer.FixtureItem(
        role="reload",
        index=0,
        media_relative="reload/source.mkv",
        subtitle_relative="reload/source.en.srt",
    )
    scanner = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        [path],
    )
    mark = scanner.mark()
    scanner.parse_lines(
        "2026-09-01T00:00:00Z SUBGEN_EVENT "
        + json.dumps(worker_event("worker_start", path))
        + "\n2026-09-01T00:00:01Z SUBGEN_EVENT "
        + json.dumps(
            worker_event("worker_finish", path, source_identity=(1, 2, 3, 4, 6))
        )
        + "\n"
    )
    with pytest.raises(health.GateAbort, match="identity"):
        observer._worker_finished(scanner, (item,), after_mark=mark)


def test_worker_event_outside_the_declared_workload_fails_closed() -> None:
    scanner = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        ["/media/short/one.mkv"],
    )
    with pytest.raises(health.GateAbort, match="allowlist"):
        scanner.parse_lines(
            "SUBGEN_EVENT "
            + json.dumps(worker_event("worker_start", "/media/short/two.mkv"))
        )


def test_event_retention_discards_unbounded_unneeded_fields() -> None:
    path = "/media/short/one.mkv"
    scanner = observer.RuntimeEventScanner(
        mock.Mock(),
        SimpleNamespace(container_id="a" * 64),
        0.0,
        [path],
    )
    scanner.parse_lines(
        "SUBGEN_EVENT "
        + json.dumps({**worker_event("worker_finish", path), "ignored": "x" * 100_000})
    )
    assert scanner.events == [worker_event("worker_finish", path)]
    assert scanner._retained_event_bytes < 256

    scanner.parse_lines(
        "SUBGEN_EVENT " + json.dumps(worker_event("worker_error", path))
    )
    with pytest.raises(health.GateAbort, match="worker_error"):
        scanner.assert_no_worker_errors([path])


def test_shutdown_drain_checks_structured_errors_only_after_final_scan() -> None:
    order: list[object] = []

    class Scanner:
        def scan(self, until_wall: float) -> None:
            assert until_wall > 0
            order.append("scan")

        def assert_no_worker_errors(self, paths) -> None:
            order.append(tuple(paths))

    item = observer.FixtureItem(
        role="invalid",
        index=0,
        media_relative="invalid/source.mkv",
        subtitle_relative=None,
    )
    observer.drain_workload_events_after_stop(Scanner(), (item,))
    assert order == ["scan", ("/media/invalid/source.mkv",)]


def test_fixture_manifest_rejects_duplicate_json_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "_owner_id", lambda: None)
    private = tmp_path / "private"
    private.mkdir()
    media = tmp_path / "media"
    media.mkdir()
    payload = json.dumps(fixture_document())
    payload = payload[:-1] + ',"schema":"subgen.task11b.runtime-fixtures/v1"}'
    manifest = private / "fixtures.json"
    manifest.write_text(payload, encoding="utf-8")
    with pytest.raises(health.GateAbort, match="duplicate"):
        observer.load_fixture_manifest(manifest.resolve(), boundary(media.resolve()))


def test_fixture_snapshot_rejects_symlink_and_oversize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures, _manifest = create_fixture_tree(tmp_path, monkeypatch)
    source = fixtures.media_root / fixtures.long.media_relative
    source.unlink()
    target = fixtures.media_root / "target.mkv"
    target.write_bytes(b"target")
    try:
        source.symlink_to(target)
    except OSError:
        source.write_bytes(b"placeholder")
        real_lstat = Path.lstat

        def lstat_with_fixture_symlink(path: Path):
            item = real_lstat(path)
            if path == source:
                return SimpleNamespace(st_mode=stat.S_IFLNK)
            return item

        with (
            mock.patch.object(Path, "lstat", lstat_with_fixture_symlink),
            pytest.raises(health.GateAbort, match="symlink"),
        ):
            observer.snapshot_fixture_file(fixtures, fixtures.long.media_relative)
    else:
        with pytest.raises(health.GateAbort, match="symlink"):
            observer.snapshot_fixture_file(fixtures, fixtures.long.media_relative)

    source.unlink()
    source.write_bytes(b"12345")
    with pytest.raises(health.GateAbort, match="byte limit|size"):
        observer.snapshot_fixture_file(
            fixtures, fixtures.long.media_relative, maximum_bytes=4
        )


def test_partial_or_undeclared_output_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures, _manifest = create_fixture_tree(tmp_path, monkeypatch)
    partial = fixtures.media_root / "long" / ".source.en.srt.abc.tmp.srt"
    partial.write_text("partial", encoding="utf-8")
    with pytest.raises(health.GateAbort, match="partial|undeclared"):
        observer.assert_fixture_directory_exact(fixtures, "long", allow_outputs=True)


def test_atomic_publication_ledger_requires_paired_staging_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures, _manifest = create_fixture_tree(tmp_path, monkeypatch)
    temporary = ".source.en.srt.abcdef12.tmp.srt"
    final = "source.en.srt"
    ledger = observer.AtomicPublicationLedger(fixtures)
    ledger.observe("long", temporary, ledger.IN_CREATE, 0)
    ledger.observe("long", temporary, 0x00000002, 0)
    ledger.observe("long", temporary, ledger.IN_CLOSE_WRITE, 0)
    ledger.observe("long", temporary, ledger.IN_MOVED_FROM, 73)
    ledger.observe("long", final, ledger.IN_MOVED_TO, 73)
    ledger.assert_published((fixtures.long,))
    stale_mark = ledger.mark()
    with pytest.raises(health.GateAbort, match="publication"):
        ledger.assert_published((fixtures.long,), after_mark=stale_mark)

    direct = observer.AtomicPublicationLedger(fixtures)
    with pytest.raises(health.GateAbort, match="non_atomically"):
        direct.observe("long", final, direct.IN_CREATE, 0)

    transient = observer.AtomicPublicationLedger(fixtures)
    with pytest.raises(health.GateAbort, match="undeclared"):
        transient.observe("long", ".unbound.partial", transient.IN_CREATE, 0)


def test_subtitle_attestation_requires_final_bounded_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures, _manifest = create_fixture_tree(tmp_path, monkeypatch)
    output = fixtures.media_root / (fixtures.long.subtitle_relative or "")
    output.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        encoding="utf-8",
    )
    output.chmod(0o600)
    result = observer.snapshot_fixture_file(
        fixtures,
        fixtures.long.subtitle_relative or "",
        maximum_bytes=observer.MAX_SUBTITLE_BYTES,
        expected_subtitle=True,
        expected_duration_seconds=1.0,
    )
    assert result.size > 0
    assert result.sha256 == health.sha256_file(output)

    output.write_text("not an srt", encoding="utf-8")
    with pytest.raises(health.GateAbort, match="subtitle"):
        observer.snapshot_fixture_file(
            fixtures,
            fixtures.long.subtitle_relative or "",
            maximum_bytes=observer.MAX_SUBTITLE_BYTES,
            expected_subtitle=True,
            expected_duration_seconds=1.0,
        )


def test_long_subtitle_attestation_rejects_truncation_and_accepts_full_timeline() -> (
    None
):
    truncated = b"1\n00:00:00,000 --> 00:00:01,000\nOnly one cue\n"
    with pytest.raises(health.GateAbort, match="cover"):
        observer.validate_srt_payload(
            truncated,
            expected_duration_seconds=31 * 60,
        )

    cues = [
        ("00:00:00,000", "00:00:10,000"),
        ("00:05:00,000", "00:05:10,000"),
        ("00:10:00,000", "00:10:10,000"),
        ("00:15:00,000", "00:15:10,000"),
        ("00:20:00,000", "00:20:10,000"),
        ("00:25:00,000", "00:25:10,000"),
        ("00:30:50,000", "00:31:00,000"),
    ]
    payload = "\n\n".join(
        f"{index}\n{start} --> {end}\nCue {index}"
        for index, (start, end) in enumerate(cues, start=1)
    ).encode("utf-8")
    assert observer.validate_srt_payload(
        payload,
        expected_duration_seconds=31 * 60,
    ) == (7, 0, 31 * 60 * 1000)


def test_wait_until_uses_hard_timeout_without_extra_attempt() -> None:
    class Clock:
        def __init__(self) -> None:
            self.value = 0.0
            self.attempts = 0

        def now(self) -> float:
            return self.value

        def sleep(self, delay: float) -> None:
            self.value += delay

        def predicate(self) -> bool:
            self.attempts += 1
            return False

    clock = Clock()
    with pytest.raises(health.GateAbort, match="timed_out"):
        observer.wait_until(
            clock.predicate,
            timeout=3.0,
            failure_message="fixture timed out",
            clock=clock.now,
            sleeper=clock.sleep,
        )
    assert clock.value == 3.0
    assert clock.attempts == 4


def test_post_batch_fails_closed_on_timeout_without_retrying() -> None:
    connection = mock.Mock()
    connection.request.side_effect = TimeoutError("bounded")
    with (
        mock.patch.object(
            observer.http.client, "HTTPConnection", return_value=connection
        ),
        pytest.raises(health.GateAbort, match="timed_out"),
    ):
        observer.post_batch(
            "/media/long", api_key="owner-only-request-key", timeout=1.0
        )
    connection.request.assert_called_once()
    connection.close.assert_called_once()


def test_main_refuses_to_run_without_systemd_cleanup_supervisor() -> None:
    args = SimpleNamespace(
        emit_systemd_run_script=None,
        supervisor_armed=False,
        expected_systemd_unit=None,
    )
    parser = mock.Mock()
    parser.parse_args.return_value = args
    with (
        mock.patch.object(observer, "_bootstrap_verified_runtime"),
        mock.patch.object(observer, "parser", return_value=parser),
        mock.patch.object(observer, "validate_args"),
        mock.patch.object(
            observer,
            "verify_systemd_supervisor",
            side_effect=health.GateAbort("supervisor missing"),
        ),
        mock.patch.object(observer, "run_observer") as run,
        pytest.raises(health.GateAbort, match="supervisor"),
    ):
        observer.main([])
    run.assert_not_called()


def test_supervisor_attestation_rejects_forged_environment_outside_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "private-gate-token"
    unit = "subgen-task11b-runtime-" + health.sha256_bytes(token.encode())[:16]
    args = SimpleNamespace(gate_token=token, expected_systemd_unit=unit)
    cleanup = ["/usr/bin/python3", "/private/gate_health_sampler.py"]
    digest = observer._cleanup_command_sha256(cleanup)
    monkeypatch.setenv("INVOCATION_ID", "a" * 32)
    monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid()))
    monkeypatch.setenv("TASK11B_CLEANUP_COMMAND_SHA256", digest)
    with (
        mock.patch.object(observer, "_cleanup_command", return_value=cleanup),
        mock.patch.object(observer.os, "open", return_value=71),
        mock.patch.object(observer.os, "close"),
        mock.patch.object(
            observer,
            "_read_all_fd",
            return_value=b"0::/user.slice/forged.service\n",
        ),
        mock.patch.object(health, "bounded_command") as systemctl,
        pytest.raises(health.GateAbort, match="outside_its_supervisor_cgroup"),
    ):
        observer.verify_systemd_supervisor(args)
    systemctl.assert_not_called()


def test_supervisor_attestation_binds_pid_unit_and_exact_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "private-gate-token"
    unit = "subgen-task11b-runtime-" + health.sha256_bytes(token.encode())[:16]
    args = SimpleNamespace(gate_token=token, expected_systemd_unit=unit)
    cleanup = [
        "/usr/bin/python3",
        "/private/gate_health_sampler.py",
        "--cleanup-only",
        "--systemd-stop-post",
    ]
    digest = observer._cleanup_command_sha256(cleanup)
    invocation = "a" * 32
    cgroup = f"/system.slice/{unit}.service"
    monkeypatch.setenv("INVOCATION_ID", invocation)
    monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid()))
    monkeypatch.setenv("TASK11B_CLEANUP_COMMAND_SHA256", digest)
    properties = "\n".join(
        (
            f"InvocationID={invocation}",
            f"ControlGroup={cgroup}",
            f"MainPID={os.getpid()}",
            "ActiveState=active",
            "ExecStopPost={ path=/usr/bin/python3 ; "
            f"argv[]={' '.join(cleanup)} ; ignore_errors=no ; "
            "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; "
            "status=0/0 ; }",
        )
    )
    with (
        mock.patch.object(observer, "_cleanup_command", return_value=cleanup),
        mock.patch.object(observer.os, "open", return_value=71),
        mock.patch.object(observer.os, "close"),
        mock.patch.object(
            observer,
            "_read_all_fd",
            return_value=f"0::{cgroup}\n".encode("ascii"),
        ),
        mock.patch.object(
            health,
            "bounded_command",
            return_value=SimpleNamespace(output=properties),
        ),
    ):
        observer.verify_systemd_supervisor(args)


@pytest.mark.parametrize(
    "exec_stop_post",
    (
        "{ path=/usr/bin/false ; argv[]=/usr/bin/python3 "
        "/private/gate_health_sampler.py --cleanup-only --systemd-stop-post ; "
        "ignore_errors=no ; }",
        "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 "
        "/private/gate_health_sampler.py --cleanup-only --systemd-stop-post ; "
        "ignore_errors=yes ; }",
        "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 "
        "/private/gate_health_sampler.py --cleanup-only --systemd-stop-post ; "
        "ignore_errors=no ; } { path=/usr/bin/true ; argv[]=/usr/bin/true ; "
        "ignore_errors=no ; }",
        "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 "
        "/private/gate_health_sampler.py --cleanup-only --systemd-stop-post "
        "--extra ; ignore_errors=no ; }",
    ),
)
def test_supervisor_attestation_rejects_inexact_cleanup_command(
    exec_stop_post: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "private-gate-token"
    unit = "subgen-task11b-runtime-" + health.sha256_bytes(token.encode())[:16]
    args = SimpleNamespace(gate_token=token, expected_systemd_unit=unit)
    cleanup = [
        "/usr/bin/python3",
        "/private/gate_health_sampler.py",
        "--cleanup-only",
        "--systemd-stop-post",
    ]
    digest = observer._cleanup_command_sha256(cleanup)
    invocation = "a" * 32
    cgroup = f"/system.slice/{unit}.service"
    monkeypatch.setenv("INVOCATION_ID", invocation)
    monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid()))
    monkeypatch.setenv("TASK11B_CLEANUP_COMMAND_SHA256", digest)
    properties = "\n".join(
        (
            f"InvocationID={invocation}",
            f"ControlGroup={cgroup}",
            f"MainPID={os.getpid()}",
            "ActiveState=active",
            f"ExecStopPost={exec_stop_post}",
        )
    )
    with (
        mock.patch.object(observer, "_cleanup_command", return_value=cleanup),
        mock.patch.object(observer.os, "open", return_value=71),
        mock.patch.object(observer.os, "close"),
        mock.patch.object(
            observer,
            "_read_all_fd",
            return_value=f"0::{cgroup}\n".encode("ascii"),
        ),
        mock.patch.object(
            health,
            "bounded_command",
            return_value=SimpleNamespace(output=properties),
        ),
        pytest.raises(health.GateAbort, match="cleanup"),
    ):
        observer.verify_systemd_supervisor(args)


def test_manifest_rejects_duplicate_fixture_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "_owner_id", lambda: None)
    private = tmp_path / "private"
    private.mkdir()
    media = tmp_path / "media"
    media.mkdir()
    document = fixture_document()
    short = document["short_resident"]
    assert isinstance(short, list)
    short[1] = copy.deepcopy(short[0])
    manifest = private / "fixtures.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(health.GateAbort, match="duplicate"):
        observer.load_fixture_manifest(manifest.resolve(), boundary(media.resolve()))


def test_manifest_parent_symlink_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "_owner_id", lambda: None)
    real = tmp_path / "real"
    real.mkdir()
    manifest = real / "fixtures.json"
    manifest.write_text(json.dumps(fixture_document()), encoding="utf-8")
    link = tmp_path / "linked"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        link.mkdir()
        linked_manifest = link / "fixtures.json"
        linked_manifest.write_text(
            manifest.read_text(encoding="utf-8"), encoding="utf-8"
        )
        real_lstat = Path.lstat

        def lstat_with_parent_symlink(path: Path):
            item = real_lstat(path)
            if path == link:
                return SimpleNamespace(st_mode=stat.S_IFLNK)
            return item

        with (
            mock.patch.object(Path, "lstat", lstat_with_parent_symlink),
            pytest.raises(health.GateAbort, match="symlink"),
        ):
            observer.load_fixture_manifest(linked_manifest, boundary(tmp_path))
    else:
        with pytest.raises(health.GateAbort, match="symlink"):
            observer.load_fixture_manifest(link / "fixtures.json", boundary(tmp_path))
