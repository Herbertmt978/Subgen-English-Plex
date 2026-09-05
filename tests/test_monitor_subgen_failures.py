import io
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import monitor_subgen_failures as monitor_module
import subgen_ops_safety as safety_module
from monitor_subgen_failures import Monitor
from subgen_core.queueing import task_event_id
from subgen_ops_safety import file_identity, new_delete_token, supports_secure_unlink

requires_secure_unlink = pytest.mark.skipif(
    not supports_secure_unlink(),
    reason="requires Linux descriptor-relative unlink primitives",
)


def make_args(
    media_root: Path,
    state_dir: Path,
    *,
    auto_delete: bool = True,
    legacy_auto_delete: bool = False,
    min_failures: int = 3,
    auto_mark: bool = True,
    mark_min_failures: int | None = None,
):
    if mark_min_failures is None:
        mark_min_failures = min_failures
    return SimpleNamespace(
        container="subgen",
        media_root=str(media_root),
        state_dir=str(state_dir),
        auto_mark_failed_files=auto_mark,
        auto_mark_min_failures=mark_min_failures,
        auto_delete_invalid_media=auto_delete,
        auto_delete_failed_files=legacy_auto_delete,
        auto_delete_min_failures=min_failures,
        smtp_host="",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        smtp_from="",
        smtp_to="",
        smtp_use_tls=True,
        smtp_use_ssl=False,
        email_relay_url="",
        email_relay_admin_key="",
        email_relay_from_address="",
        email_english_mismatch_alerts=False,
        reconnect_delay_seconds=1,
        restart_cycle_alert_threshold=6,
        restart_cycle_alert_min_seconds=3600,
        restart_cycle_alert_require_memory=True,
    )


def make_monitor(
    tmp_path: Path,
    *,
    auto_delete: bool = True,
    legacy_auto_delete: bool = False,
    min_failures: int = 3,
    auto_mark: bool = True,
    mark_min_failures: int | None = None,
):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir(parents=True)
    return Monitor(
        make_args(
            media_root,
            state_dir,
            auto_delete=auto_delete,
            legacy_auto_delete=legacy_auto_delete,
            min_failures=min_failures,
            auto_mark=auto_mark,
            mark_min_failures=mark_min_failures,
        )
    )


def test_auto_delete_is_opt_in_by_default(monkeypatch):
    monkeypatch.delenv("AUTO_DELETE_INVALID_MEDIA", raising=False)
    monkeypatch.delenv("AUTO_DELETE_FAILED_FILES", raising=False)
    monkeypatch.setattr(sys, "argv", ["monitor_subgen_failures.py"])

    args = monitor_module.parse_args()
    assert args.auto_delete_invalid_media is False
    assert args.auto_delete_failed_files is False


def validation_event(
    target: Path,
    *,
    event: str = "media_validation_failed",
    failure_class: str = "invalid_media",
    validator_outcomes: dict | None = None,
    source_identity=None,
    validation_detail: str | None = None,
    task_id: str | None = None,
) -> dict:
    if validator_outcomes is None:
        validator_outcomes = {
            "ffprobe": "invalid_format",
            "pyav": "invalid_format",
        }
    if source_identity is None:
        source_identity = list(file_identity(target.stat()))
    if validation_detail is None:
        validation_detail = (
            "dual_parser_invalid"
            if failure_class == "invalid_media"
            else "validator_evidence_indeterminate"
        )
    payload = {
        "event": event,
        "task_type": "transcribe",
        "path": "/media/library/offender.mkv",
        "failure_class": failure_class,
        "source_identity": source_identity,
        "validator_outcomes": validator_outcomes,
        "validation_detail": validation_detail,
    }
    payload["task_id"] = task_id or task_event_id(payload)
    return payload


def emit_structured(monitor: Monitor, event: dict) -> None:
    monitor.process_log_line("SUBGEN_EVENT " + json.dumps(event))


def direct_delete_target(
    monitor: Monitor,
    source_path: Path,
    container_path: str,
) -> dict:
    identity = list(file_identity(source_path.stat()))
    target = {
        "record_kind": "processing_error",
        "host_path": str(source_path),
        "container_path": container_path,
        "first_seen_utc": "2026-01-01T00:00:00Z",
        "last_seen_utc": "2026-01-01T00:00:00Z",
        "count": 1,
        "invalid_media_count": 1,
        "delete_status": None,
        "deleted_utc": None,
        "delete_message": None,
        "failure_identity": identity,
        "failure_event": "media_validation_failed",
        "failure_class": "invalid_media",
        "source_identity": identity,
        "validator_outcomes": {
            "ffprobe": "invalid_format",
            "pyav": "invalid_format",
        },
        "validation_detail": "dual_parser_invalid",
    }
    assert monitor.persist_failure_marker(
        target,
        failure_kind="processing_error",
    )
    return target


def delete_proof(target: dict) -> dict:
    return {
        key: target[key]
        for key in (
            "failure_event",
            "failure_class",
            "source_identity",
            "validator_outcomes",
            "validation_detail",
        )
    }


def test_canonical_and_legacy_delete_switches_are_invalid_media_only(
    tmp_path,
):
    canonical = make_monitor(tmp_path / "canonical", auto_delete=True)
    assert canonical.auto_delete is True

    with pytest.warns(RuntimeWarning, match="invalid-media-only") as caught:
        legacy = make_monitor(
            tmp_path / "legacy",
            auto_delete=False,
            legacy_auto_delete=True,
        )

    assert legacy.auto_delete is True
    assert len(caught) == 1


def test_delete_policy_requires_automatic_marking(tmp_path):
    with pytest.raises(ValueError, match="automatic failure markers"):
        make_monitor(tmp_path, auto_delete=True, auto_mark=False)


@pytest.mark.skipif(
    monitor_module.fcntl is None,
    reason="requires advisory file locking",
)
def test_monitor_lifetime_lock_is_exclusive_and_releases(tmp_path):
    state_dir = tmp_path / "state"

    with monitor_module.monitor_process_lock(state_dir):
        with pytest.raises(RuntimeError, match="already owns"):
            with monitor_module.monitor_process_lock(state_dir):
                pass

    with monitor_module.monitor_process_lock(state_dir):
        assert (state_dir / "subgen_failure_monitor.lock").is_file()


@requires_secure_unlink
def test_only_current_dual_invalid_event_can_delete(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=1)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"invalid media")

    emit_structured(monitor, validation_event(target))

    assert not target.exists()
    item = next(iter(monitor.processing_errors.values()))
    assert item["failure_event"] == "media_validation_failed"
    assert item["failure_class"] == "invalid_media"
    assert item["validator_outcomes"] == {
        "ffprobe": "invalid_format",
        "pyav": "invalid_format",
    }
    assert item["delete_status"] == "deleted"


def test_dual_invalid_summary_records_typed_evidence_before_deletion_is_enabled(
    tmp_path,
):
    monitor = make_monitor(
        tmp_path,
        auto_delete=False,
        min_failures=1,
    )
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"invalid media")

    emit_structured(monitor, validation_event(target))

    summary = monitor.summary_path.read_text(encoding="utf-8")
    assert target.read_bytes() == b"invalid media"
    assert "Auto delete invalid media: False" in summary
    assert (
        "\n".join(
            (
                "    count: 1",
                "    failure_event: media_validation_failed",
                "    failure_class: invalid_media",
                "    validator_outcomes:",
                "      ffprobe: invalid_format",
                "      pyav: invalid_format",
                "    validation_detail: dual_parser_invalid",
            )
        )
        in summary
    )


@pytest.mark.parametrize(
    ("failure_class", "validator_outcomes", "expected_record"),
    (
        (
            "probe_indeterminate",
            {"ffprobe": "indeterminate", "pyav": "invalid_format"},
            True,
        ),
        (
            "invalid_media",
            {"ffprobe": "invalid_format", "pyav": "indeterminate"},
            False,
        ),
        (
            "invalid_media",
            {"ffprobe": "audio_present", "pyav": "invalid_format"},
            False,
        ),
    ),
)
def test_non_dual_invalid_validation_evidence_is_retained(
    tmp_path,
    failure_class,
    validator_outcomes,
    expected_record,
):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=1)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")

    emit_structured(
        monitor,
        validation_event(
            target,
            failure_class=failure_class,
            validator_outcomes=validator_outcomes,
        ),
    )

    assert target.exists()
    if expected_record:
        item = next(iter(monitor.processing_errors.values()))
        assert item["failure_class"] == "probe_indeterminate"
        assert item["marker_status"] in {"created", "refreshed"}
        assert item.get("delete_status") != "deleted"
    else:
        assert monitor.processing_errors == {}
        assert "[MEDIA_VALIDATION_EVENT_BLOCKED]" in (
            monitor.events_path.read_text(encoding="utf-8")
        )


@pytest.mark.parametrize("event_name", ["worker_error", "file_error"])
def test_non_validation_error_cannot_smuggle_invalid_media_authority(
    tmp_path,
    event_name,
):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=1)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    event = validation_event(target, event=event_name)

    emit_structured(monitor, event)

    assert target.exists()
    item = next(iter(monitor.processing_errors.values()))
    assert item["failure_event"] == event_name
    assert item["failure_class"] != "invalid_media"
    assert item["delete_status"] != "deleted"


def test_structured_resource_exhaustion_is_marked_and_retained(tmp_path, monkeypatch):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=1)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    identity = list(file_identity(target.stat()))
    event = {
        "event": "worker_error",
        "task_id": "resource-task",
        "task_type": "transcribe",
        "path": "/media/library/offender.mkv",
        "failure_class": "resource_exhaustion",
        "source_identity": identity,
    }
    emit_structured(
        monitor,
        {
            **event,
            "event": "worker_start",
        },
    )
    delete_calls = []
    monkeypatch.setattr(
        monitor,
        "try_delete_path",
        lambda *args, **kwargs: delete_calls.append((args, kwargs)),
    )

    emit_structured(monitor, event)

    assert target.read_bytes() == b"media"
    item = next(iter(monitor.processing_errors.values()))
    assert item["failure_event"] == "worker_error"
    assert item["failure_class"] == "resource_exhaustion"
    assert item["invalid_media_count"] == 0
    assert item["marker_status"] in {"created", "refreshed"}
    assert item["delete_status"] != "deleted"
    assert delete_calls == []
    marker = monitor_module.load_marker_document(monitor.marker_registry_path)[
        "markers"
    ][0]
    assert marker["failure_kind"] == "processing_error"
    assert "failure_class: resource_exhaustion" in monitor.summary_path.read_text(
        encoding="utf-8"
    )


def test_validation_event_does_not_adopt_replacement_generation(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=1)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"generation-a")
    event = validation_event(target)
    target.unlink()
    target.write_bytes(b"generation-b-is-different")

    emit_structured(monitor, event)

    assert target.read_bytes() == b"generation-b-is-different"


def test_media_validation_failure_clears_matching_active_task(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=False, min_failures=1)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"invalid media")
    event = validation_event(target)
    start = {
        "event": "worker_start",
        "task_id": event["task_id"],
        "task_type": "transcribe",
        "path": event["path"],
        "source_identity": event["source_identity"],
    }

    emit_structured(monitor, start)
    emit_structured(monitor, event)

    assert event["task_id"] not in monitor.active_tasks
    assert (
        next(iter(monitor.processing_errors.values()))["failure_class"]
        == "invalid_media"
    )


def test_media_validation_identity_mismatch_does_not_clear_newer_active_task(
    tmp_path,
):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=1)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"generation-a")
    stale_event = validation_event(target)
    target.unlink()
    target.write_bytes(b"generation-b-is-different")
    start = {
        "event": "worker_start",
        "task_id": stale_event["task_id"],
        "task_type": "transcribe",
        "path": stale_event["path"],
        "source_identity": list(file_identity(target.stat())),
    }

    emit_structured(monitor, start)
    emit_structured(monitor, stale_event)

    assert stale_event["task_id"] in monitor.active_tasks
    assert monitor.processing_errors == {}
    assert target.read_bytes() == b"generation-b-is-different"
    assert "[STRUCTURED_TERMINAL_STALE]" in (
        monitor.events_path.read_text(encoding="utf-8")
    )


def test_unknown_structured_event_is_consumed_before_sigsegv_fallback(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=False)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    start = {
        "event": "worker_start",
        "task_id": "task",
        "task_type": "transcribe",
        "path": "/media/library/offender.mkv",
        "source_identity": list(file_identity(target.stat())),
    }
    emit_structured(monitor, start)

    monitor.process_log_line(
        "SUBGEN_EVENT "
        + json.dumps(
            {
                "event": "unknown_SIGSEGV_event",
                "task_id": "task",
                "path": "/media/library/offender.mkv",
            }
        )
    )

    assert monitor.crash_candidates == {}
    assert "task" in monitor.active_tasks


def test_terminal_event_with_different_path_does_not_clear_active_task(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=1)
    source = monitor.media_root / "show-a" / "episode.mkv"
    alias = monitor.media_root / "show-b" / "episode.mkv"
    source.parent.mkdir()
    alias.parent.mkdir()
    source.write_bytes(b"media")
    os.link(source, alias)
    identity = list(file_identity(source.stat()))
    start = {
        "event": "worker_start",
        "task_id": "task",
        "task_type": "transcribe",
        "path": "/media/show-a/episode.mkv",
        "source_identity": identity,
    }
    terminal = {
        "event": "worker_error",
        "task_id": "task",
        "task_type": "transcribe",
        "path": "/media/show-b/episode.mkv",
        "source_identity": identity,
    }

    emit_structured(monitor, start)
    emit_structured(monitor, terminal)

    assert "task" in monitor.active_tasks
    assert monitor.processing_errors == {}
    assert source.read_bytes() == b"media"
    assert alias.read_bytes() == b"media"
    assert "[STRUCTURED_TERMINAL_STALE]" in (
        monitor.events_path.read_text(encoding="utf-8")
    )


def test_structured_event_with_duplicate_keys_is_consumed_and_blocked(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=1)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    encoded = json.dumps(validation_event(target))
    encoded = encoded.replace(
        '"event": "media_validation_failed"',
        '"event": "unknown_SIGSEGV_event", "event": "media_validation_failed"',
        1,
    )

    monitor.process_log_line("SUBGEN_EVENT " + encoded)

    assert target.read_bytes() == b"media"
    assert monitor.processing_errors == {}
    assert "[SUBGEN_EVENT_INVALID]" in monitor.events_path.read_text(encoding="utf-8")


def test_embedded_structured_frame_cannot_fall_through_to_sigsegv(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=False)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    start = {
        "event": "worker_start",
        "task_id": "task",
        "task_type": "transcribe",
        "path": "/media/library/offender.mkv",
        "source_identity": list(file_identity(target.stat())),
    }
    emit_structured(monitor, start)

    monitor.process_log_line(
        "prefix SUBGEN_EVENT " + json.dumps({"event": "unknown", "detail": "SIGSEGV"})
    )

    assert monitor.crash_candidates == {}
    assert "task" in monitor.active_tasks
    assert "[SUBGEN_EVENT_INVALID]" in monitor.events_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "malformation",
    [
        "extra_field",
        "wrong_task_id",
        "noncanonical_path",
        "control_character",
        "unsupported_outcome",
    ],
)
def test_malformed_media_validation_event_is_consumed_and_retained(
    tmp_path,
    malformation,
):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=1)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    event = validation_event(target)
    if malformation == "extra_field":
        event["unexpected"] = "value"
    elif malformation == "wrong_task_id":
        event["task_id"] = "wrong-task"
    elif malformation == "noncanonical_path":
        event["path"] = "/media/library/../offender.mkv"
        event["task_id"] = task_event_id({"type": "transcribe", "path": event["path"]})
    elif malformation == "control_character":
        event["path"] = "/media/library/offender\n.mkv"
        event["task_id"] = task_event_id({"type": "transcribe", "path": event["path"]})
    else:
        event["validator_outcomes"]["pyav"] = "not_a_validator_outcome"

    emit_structured(monitor, event)

    assert target.read_bytes() == b"media"
    assert monitor.processing_errors == {}
    assert "[MEDIA_VALIDATION_EVENT_BLOCKED]" in (
        monitor.events_path.read_text(encoding="utf-8")
    )


def test_bounded_log_reader_discards_oversized_record_and_continues():
    oversized = b"x" * (monitor_module.MAX_LOG_RECORD_BYTES + 100) + b"\n"
    stream = io.BytesIO(oversized + b"next record\n")

    assert list(monitor_module.iter_bounded_log_records(stream)) == [
        None,
        "next record",
    ]


def test_structured_event_size_limit_is_measured_in_bytes(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=False)
    line = "SUBGEN_EVENT " + json.dumps(
        {
            "event": "unknown",
            "task_id": "task",
            "task_type": "transcribe",
            "path": "/media/" + ("é" * 9000),
        },
        ensure_ascii=False,
    )
    assert len(line) < monitor_module.MAX_STRUCTURED_EVENT_BYTES
    assert len(line.encode("utf-8")) > monitor_module.MAX_STRUCTURED_EVENT_BYTES

    monitor.process_log_line(line)

    assert "event exceeded size limit" in monitor.events_path.read_text(
        encoding="utf-8"
    )


def test_generic_processing_error_is_retained_above_threshold(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=3)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")

    monitor.record_processing_error("/media/library/offender.mkv")
    assert target.exists()
    monitor.record_processing_error("/media/library/offender.mkv")
    assert target.exists()
    monitor.record_processing_error("/media/library/offender.mkv")

    assert target.read_bytes() == b"media"
    item = next(iter(monitor.processing_errors.values()))
    assert item["count"] == 3
    assert item["invalid_media_count"] == 0
    assert item["delete_status"] == "policy_blocked"


def test_generic_processing_error_summary_keeps_typed_fields_readable(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=False, min_failures=1)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")

    monitor.record_processing_error("/media/library/offender.mkv")

    summary = monitor.summary_path.read_text(encoding="utf-8")
    assert (
        "\n".join(
            (
                "    count: 1",
                "    failure_event: legacy_processing_error",
                "    failure_class: processing_error",
            )
        )
        in summary
    )
    assert "    validator_outcomes:" not in summary
    assert "    validation_detail:" not in summary


def test_monitor_never_recursively_deletes_a_directory(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=1)
    target = monitor.media_root / "library" / "season"
    target.mkdir(parents=True)
    (target / "episode.mkv").write_bytes(b"media")

    monitor.record_processing_error("/media/library/season")

    assert target.exists()
    item = next(iter(monitor.processing_errors.values()))
    assert item["delete_status"] == "policy_blocked"


def test_monitor_revalidates_media_root_before_delete(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=1)
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")
    target = direct_delete_target(monitor, outside, "/media/outside.mkv")

    monitor.try_delete_path(outside, target, "MISSING", "DELETED", "FAILED")

    assert outside.exists()
    assert target["delete_status"] == "policy_blocked"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("record_kind", "crash_candidate"),
        ("failure_event", "worker_error"),
        ("failure_class", "inference_error"),
        ("validation_detail", "validator_evidence_indeterminate"),
        (
            "validator_outcomes",
            {"ffprobe": "invalid_format", "pyav": "indeterminate"},
        ),
        ("source_identity", [1, 2, 3, 4, 5]),
        ("failure_identity", [5, 4, 3, 2, 1]),
    ],
)
def test_direct_delete_gate_rejects_each_noncanonical_proof(
    tmp_path,
    field,
    value,
):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=1)
    source = monitor.media_root / "offender.mkv"
    source.write_bytes(b"media")
    target = direct_delete_target(monitor, source, "/media/offender.mkv")
    target[field] = value

    monitor.try_delete_path(source, target, "MISSING", "DELETED", "FAILED")

    assert source.read_bytes() == b"media"
    assert target["delete_status"] == "policy_blocked"


def test_delete_gate_reloads_and_rejects_corrupt_marker_registry(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=1)
    source = monitor.media_root / "offender.mkv"
    source.write_bytes(b"media")
    target = direct_delete_target(monitor, source, "/media/offender.mkv")
    monitor.marker_registry_path.write_text("not-json", encoding="utf-8")

    monitor.try_delete_path(source, target, "MISSING", "DELETED", "FAILED")

    assert source.read_bytes() == b"media"
    assert target["marker_status"] == "verification_failed"
    assert target["delete_status"] == "marker_blocked"


def test_delete_gate_rejects_marker_below_delete_threshold(tmp_path):
    monitor = make_monitor(
        tmp_path,
        auto_delete=True,
        min_failures=3,
        mark_min_failures=1,
    )
    source = monitor.media_root / "offender.mkv"
    source.write_bytes(b"media")
    target = direct_delete_target(monitor, source, "/media/offender.mkv")
    target["count"] = 3
    target["invalid_media_count"] = 3

    monitor.try_delete_path(source, target, "MISSING", "DELETED", "FAILED")

    assert source.read_bytes() == b"media"
    assert target["delete_status"] == "marker_blocked"


def test_direct_delete_gate_binds_container_path_to_exact_host_path(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=1)
    source = monitor.media_root / "library" / "offender.mkv"
    alias = monitor.media_root / "library" / "alias.mkv"
    source.parent.mkdir()
    source.write_bytes(b"media")
    os.link(source, alias)
    target = direct_delete_target(
        monitor,
        source,
        "/media/library/offender.mkv",
    )

    monitor.try_delete_path(alias, target, "MISSING", "DELETED", "FAILED")

    assert source.read_bytes() == b"media"
    assert alias.read_bytes() == b"media"
    assert target["delete_status"] == "policy_blocked"


def create_symlink_or_skip(link_path: Path, target_path: Path) -> None:
    try:
        link_path.symlink_to(target_path)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")


@requires_secure_unlink
def test_monitor_event_log_refuses_symlink_target(tmp_path):
    monitor = make_monitor(tmp_path)
    outside = tmp_path / "outside.log"
    outside.write_text("keep\n", encoding="utf-8")
    create_symlink_or_skip(monitor.events_path, outside)

    with pytest.raises(OSError):
        monitor.append_event("TEST", "must not follow")

    assert outside.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize("candidate_kind", ["processing", "crash"])
def test_monitor_refuses_symlink_candidate_without_deleting_target(
    tmp_path, candidate_kind
):
    monitor = make_monitor(tmp_path, min_failures=1)
    target = monitor.media_root / "library" / "target.mkv"
    link = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    create_symlink_or_skip(link, target)

    if candidate_kind == "processing":
        monitor.record_processing_error("/media/library/offender.mkv")
        item = next(iter(monitor.processing_errors.values()))
    else:
        monitor.record_crash_candidate(
            link.name,
            container_path="/media/library/offender.mkv",
        )
        item = next(iter(monitor.crash_candidates.values()))

    assert link.is_symlink()
    assert target.read_bytes() == b"media"
    assert item["delete_status"] == "policy_blocked"


def test_monitor_blocks_parent_symlink_that_resolves_outside_media_root(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=1)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "offender.mkv"
    target.write_bytes(b"outside")
    create_symlink_or_skip(monitor.media_root / "linked", outside)

    monitor.record_processing_error("/media/linked/offender.mkv")

    assert target.read_bytes() == b"outside"
    item = next(iter(monitor.processing_errors.values()))
    assert item["delete_status"] == "policy_blocked"


def test_monitor_rejects_lexical_parent_traversal_from_container_path(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=1)
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")

    monitor.record_processing_error("/media/../outside.mkv")

    assert outside.read_bytes() == b"outside"
    assert monitor.processing_errors == {}


def test_monitor_keeps_running_when_crash_candidate_uses_parent_traversal(
    tmp_path,
):
    monitor = make_monitor(tmp_path, min_failures=1)
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")

    monitor.record_crash_candidate(
        outside.name,
        container_path="/media/../outside.mkv",
    )

    assert outside.read_bytes() == b"outside"
    item = next(iter(monitor.crash_candidates.values()))
    assert item["host_path"] is None
    assert item["delete_status"] == "policy_blocked"


def test_monitor_blocks_lexical_parent_traversal_in_host_path(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=1)
    (monitor.media_root / "library").mkdir()
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")
    lexical_escape = monitor.media_root / "library" / ".." / ".." / outside.name
    target = direct_delete_target(
        monitor,
        outside,
        "/media/library/outside.mkv",
    )

    monitor.try_delete_path(
        str(lexical_escape),
        target,
        "MISSING",
        "DELETED",
        "FAILED",
    )

    assert outside.read_bytes() == b"outside"
    assert target["delete_status"] == "policy_blocked"


def test_monitor_rejects_parent_traversal_even_when_it_stays_inside_media_root(
    tmp_path,
):
    monitor = make_monitor(tmp_path, min_failures=1)
    target_path = monitor.media_root / "offender.mkv"
    target_path.write_bytes(b"media")
    lexical_traversal = f"{monitor.media_root}/unused/../{target_path.name}"
    target = direct_delete_target(
        monitor,
        target_path,
        "/media/offender.mkv",
    )

    monitor.try_delete_path(
        lexical_traversal,
        target,
        "MISSING",
        "DELETED",
        "FAILED",
    )

    assert target_path.read_bytes() == b"media"
    assert target["delete_status"] == "policy_blocked"


@requires_secure_unlink
def test_monitor_delete_result_survives_audit_append_failure(
    tmp_path, monkeypatch, capsys
):
    monitor = make_monitor(tmp_path, min_failures=1)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    real_append = monitor.append_event

    def fail_delete_audit(kind, message):
        if kind == "FILE_DELETED":
            raise OSError("simulated audit failure")
        return real_append(kind, message)

    monkeypatch.setattr(monitor, "append_event", fail_delete_audit)

    emit_structured(monitor, validation_event(target))

    assert not target.exists()
    item = next(iter(monitor.processing_errors.values()))
    assert item["delete_status"] == "deleted"
    persisted = json.loads(monitor.state_path.read_text(encoding="utf-8"))
    assert persisted["processing_errors"][0]["delete_status"] == "deleted"
    assert "offender.mkv" not in capsys.readouterr().err


def test_atomic_monitor_state_write_keeps_previous_file_on_replace_failure(
    tmp_path, monkeypatch
):
    monitor = make_monitor(tmp_path)
    previous = '{"processing_errors": [], "sentinel": "kept"}\n'
    monitor.state_path.write_text(previous, encoding="utf-8")

    def fail_replace(*_args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(monitor_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        monitor.save_state()

    assert monitor.state_path.read_text(encoding="utf-8") == previous
    assert list(monitor.state_dir.glob(".subgen_failed_state.json.*.tmp")) == []


def test_malformed_monitor_state_disables_live_deletion_for_process(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    (state_dir / "subgen_failed_state.json").write_text(
        "not-json",
        encoding="utf-8",
    )

    with pytest.warns(RuntimeWarning, match="deletion is disabled"):
        monitor = Monitor(
            make_args(media_root, state_dir, auto_delete=True, min_failures=1)
        )
    target = media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"invalid media")

    emit_structured(monitor, validation_event(target))

    assert monitor.state_recovery_safe is False
    assert target.read_bytes() == b"invalid media"
    item = next(iter(monitor.processing_errors.values()))
    assert item["delete_status"] == "policy_blocked"


def test_malformed_monitor_state_collections_fail_closed(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    (state_dir / "subgen_failed_state.json").write_text(
        json.dumps(
            {
                "version": monitor_module.MONITOR_STATE_VERSION,
                "container_name": "subgen",
                "media_root": str(media_root.resolve()),
                "processing_errors": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.warns(RuntimeWarning, match="malformed collections"):
        monitor = Monitor(make_args(media_root, state_dir))

    assert monitor.state_recovery_safe is False


def test_oversized_monitor_state_fails_closed(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    monkeypatch.setattr(monitor_module, "MAX_MONITOR_STATE_BYTES", 32)
    (state_dir / "subgen_failed_state.json").write_text(
        "x" * 33,
        encoding="utf-8",
    )

    with pytest.warns(RuntimeWarning, match="deletion is disabled"):
        monitor = Monitor(make_args(media_root, state_dir))

    assert monitor.state_recovery_safe is False


def test_monitor_state_symlink_is_not_followed_or_overwritten(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    media_file = media_root / "movie.mkv"
    media_file.write_bytes(b"media")
    state_path = state_dir / "subgen_failed_state.json"
    create_symlink_or_skip(state_path, media_file)

    with pytest.warns(RuntimeWarning, match="deletion is disabled"):
        monitor = Monitor(make_args(media_root, state_dir))

    assert monitor.state_recovery_safe is False
    assert state_path.is_symlink()
    assert media_file.read_bytes() == b"media"


@pytest.mark.skipif(
    os.path.normcase("A") == os.path.normcase("a"),
    reason="requires a case-sensitive filesystem",
)
def test_case_distinct_generic_processing_paths_remain_independent_and_retained(
    tmp_path,
):
    monitor = make_monitor(tmp_path, min_failures=3)
    upper = monitor.media_root / "Show" / "Episode.mkv"
    lower = monitor.media_root / "show" / "episode.mkv"
    upper.parent.mkdir()
    lower.parent.mkdir()
    upper.write_bytes(b"upper")
    lower.write_bytes(b"lower")

    monitor.record_processing_error("/media/Show/Episode.mkv")
    monitor.record_processing_error("/media/Show/Episode.mkv")
    monitor.record_processing_error("/media/show/episode.mkv")

    assert upper.exists()
    assert lower.exists()
    assert sorted(item["count"] for item in monitor.processing_errors.values()) == [
        1,
        2,
    ]

    monitor.record_processing_error("/media/Show/Episode.mkv")

    assert upper.read_bytes() == b"upper"
    assert lower.read_bytes() == b"lower"
    assert sorted(item["count"] for item in monitor.processing_errors.values()) == [
        1,
        3,
    ]
    assert all(
        item["delete_status"] == "policy_blocked"
        for item in monitor.processing_errors.values()
    )


@requires_secure_unlink
def test_monitor_aborts_before_unlink_when_delete_intent_cannot_persist(
    tmp_path, monkeypatch
):
    monitor = make_monitor(tmp_path, min_failures=1)
    target_path = monitor.media_root / "offender.mkv"
    target_path.write_bytes(b"media")
    target = direct_delete_target(
        monitor,
        target_path,
        "/media/offender.mkv",
    )
    monitor.processing_errors[str(target_path)] = target
    monitor.save_state()

    def fail_save():
        raise OSError("simulated intent write failure")

    monkeypatch.setattr(monitor, "save_state", fail_save)

    with pytest.raises(OSError, match="intent write failure"):
        monitor.try_delete_path(
            str(target_path),
            target,
            "MISSING",
            "DELETED",
            "FAILED",
        )

    assert target_path.read_bytes() == b"media"
    persisted = json.loads(monitor.state_path.read_text(encoding="utf-8"))
    assert persisted["processing_errors"][0]["delete_status"] is None


@requires_secure_unlink
def test_monitor_recovers_persisted_delete_intent_after_final_save_failure(
    tmp_path, monkeypatch
):
    args = make_args(tmp_path / "media", tmp_path / "state", min_failures=1)
    monitor = Monitor(args)
    monitor.media_root.mkdir()
    target_path = monitor.media_root / "offender.mkv"
    target_path.write_bytes(b"media")
    target = direct_delete_target(
        monitor,
        target_path,
        "/media/offender.mkv",
    )
    monitor.processing_errors[str(target_path)] = target
    real_save = monitor.save_state
    save_calls = 0

    def fail_final_save():
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            return real_save()
        raise OSError("simulated final write failure")

    monkeypatch.setattr(monitor, "save_state", fail_final_save)

    with pytest.raises(OSError, match="final write failure"):
        monitor.try_delete_path(
            str(target_path),
            target,
            "MISSING",
            "DELETED",
            "FAILED",
        )

    assert not target_path.exists()
    persisted = json.loads(monitor.state_path.read_text(encoding="utf-8"))
    assert persisted["processing_errors"][0]["delete_status"] == "deleting"

    recovered = Monitor(args)
    recovered_item = next(iter(recovered.processing_errors.values()))
    assert recovered_item["delete_status"] == "deleted_recovered"


@requires_secure_unlink
def test_monitor_blocks_legacy_untyped_delete_intent(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    target = media_root / "offender.mkv"
    target.write_bytes(b"media")
    identity = list(file_identity(target.stat()))
    (state_dir / "subgen_failed_state.json").write_text(
        json.dumps(
            {
                "processing_errors": [
                    {
                        "host_path": str(target),
                        "container_path": "/media/offender.mkv",
                        "first_seen_utc": "2026-01-01T00:00:00Z",
                        "last_seen_utc": "2026-01-01T00:00:00Z",
                        "count": 3,
                        "delete_status": "deleting",
                        "delete_identity": identity,
                        "failure_identity": identity,
                        "delete_token": new_delete_token(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monitor = Monitor(make_args(media_root, state_dir, auto_delete=False))

    assert target.read_bytes() == b"media"
    item = next(iter(monitor.processing_errors.values()))
    assert item["delete_status"] == "blocked_recovery"
    assert item["delete_identity"] == identity


@requires_secure_unlink
def test_monitor_pauses_valid_typed_recovery_when_auto_delete_is_disabled(
    tmp_path,
):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    setup = Monitor(make_args(media_root, state_dir, auto_delete=True, min_failures=1))
    target_path = media_root / "offender.mkv"
    target_path.write_bytes(b"media")
    target = direct_delete_target(
        setup,
        target_path,
        "/media/offender.mkv",
    )
    identity = list(file_identity(target_path.stat()))
    token = new_delete_token()
    target.update(
        {
            "delete_status": "deleting",
            "delete_identity": identity,
            "delete_token": token,
            "delete_event_kind": "FILE_DELETED",
            "delete_proof": delete_proof(target),
        }
    )
    setup.processing_errors[str(target_path)] = target
    setup.save_state()

    monitor = Monitor(
        make_args(media_root, state_dir, auto_delete=False, min_failures=1)
    )

    assert target_path.read_bytes() == b"media"
    item = next(iter(monitor.processing_errors.values()))
    assert item["delete_status"] == "delete_paused"
    assert item["delete_identity"] == identity
    assert item["delete_token"] == token


def test_monitor_blocks_typed_recovery_from_different_deployment_context(
    tmp_path,
):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    args = make_args(media_root, state_dir, auto_delete=True, min_failures=1)
    setup = Monitor(args)
    target_path = media_root / "offender.mkv"
    target_path.write_bytes(b"media")
    target = direct_delete_target(
        setup,
        target_path,
        "/media/offender.mkv",
    )
    identity = list(file_identity(target_path.stat()))
    target.update(
        {
            "delete_status": "deleting",
            "delete_identity": identity,
            "delete_token": new_delete_token(),
            "delete_event_kind": "FILE_DELETED",
            "delete_proof": delete_proof(target),
        }
    )
    setup.processing_errors[str(target_path)] = target
    setup.save_state()
    persisted = json.loads(setup.state_path.read_text(encoding="utf-8"))
    persisted["container_name"] = "different-subgen-container"
    setup.state_path.write_text(json.dumps(persisted), encoding="utf-8")

    recovered = Monitor(args)

    assert target_path.read_bytes() == b"media"
    item = next(iter(recovered.processing_errors.values()))
    assert recovered.state_context_current is False
    assert item["delete_status"] == "blocked_recovery"
    assert item["delete_identity"] == identity


def test_monitor_blocks_recovery_when_host_path_is_hardlink_alias(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    args = make_args(media_root, state_dir, auto_delete=True, min_failures=1)
    setup = Monitor(args)
    source = media_root / "offender.mkv"
    alias = media_root / "alias.mkv"
    source.write_bytes(b"media")
    os.link(source, alias)
    target = direct_delete_target(setup, source, "/media/offender.mkv")
    identity = list(file_identity(source.stat()))
    target.update(
        {
            "host_path": str(alias),
            "delete_status": "deleting",
            "delete_identity": identity,
            "delete_token": new_delete_token(),
            "delete_event_kind": "FILE_DELETED",
            "delete_proof": delete_proof(target),
        }
    )
    setup.processing_errors[str(alias)] = target
    setup.save_state()

    recovered = Monitor(args)

    assert source.read_bytes() == b"media"
    assert alias.read_bytes() == b"media"
    item = next(iter(recovered.processing_errors.values()))
    assert item["delete_status"] == "blocked_recovery"


def test_monitor_blocks_typed_recovery_when_durable_marker_disappears(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    args = make_args(media_root, state_dir, auto_delete=True, min_failures=1)
    setup = Monitor(args)
    target_path = media_root / "offender.mkv"
    target_path.write_bytes(b"media")
    target = direct_delete_target(
        setup,
        target_path,
        "/media/offender.mkv",
    )
    identity = list(file_identity(target_path.stat()))
    token = new_delete_token()
    target.update(
        {
            "delete_status": "deleting",
            "delete_identity": identity,
            "delete_token": token,
            "delete_event_kind": "FILE_DELETED",
            "delete_proof": delete_proof(target),
        }
    )
    setup.processing_errors[str(target_path)] = target
    setup.save_state()
    setup.marker_registry_path.unlink()

    recovered = Monitor(args)

    assert target_path.read_bytes() == b"media"
    item = next(iter(recovered.processing_errors.values()))
    assert item["delete_status"] == "blocked_recovery"
    assert item["delete_identity"] == identity
    assert item["delete_token"] == token


@requires_secure_unlink
def test_replacement_at_deleted_monitor_path_starts_a_new_failure_generation(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=3)
    target = monitor.media_root / "show" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"old offender")
    for _ in range(3):
        event = validation_event(target)
        event["path"] = "/media/show/offender.mkv"
        event["task_id"] = task_event_id({"type": "transcribe", "path": event["path"]})
        emit_structured(monitor, event)
    assert not target.exists()

    target.write_bytes(b"fixed replacement with a different fingerprint")
    event = validation_event(target)
    event["path"] = "/media/show/offender.mkv"
    event["task_id"] = task_event_id({"type": "transcribe", "path": event["path"]})
    emit_structured(monitor, event)

    assert target.read_bytes().startswith(b"fixed replacement")
    item = next(iter(monitor.processing_errors.values()))
    assert item["count"] == 1
    assert item["invalid_media_count"] == 1
    assert item["marker_status"] == "waiting"
    assert item["delete_status"] != "deleted"


def test_monitor_never_adopts_unfingerprinted_file_at_delete_threshold(
    tmp_path, monkeypatch
):
    monitor = make_monitor(tmp_path, min_failures=1)
    target = monitor.media_root / "offender.mkv"
    target.write_bytes(b"appeared after failure evidence")
    real_current_identity = monitor.current_failure_identity
    monkeypatch.setattr(monitor, "current_failure_identity", lambda _path: None)

    monitor.record_processing_error("/media/offender.mkv")

    assert target.exists()
    item = next(iter(monitor.processing_errors.values()))
    assert item["count"] == 1
    assert item["failure_identity"] is None
    assert item["delete_status"] == "policy_blocked"

    monkeypatch.setattr(monitor, "current_failure_identity", real_current_identity)
    monitor.record_processing_error("/media/offender.mkv")
    assert target.read_bytes() == b"appeared after failure evidence"
    assert next(iter(monitor.processing_errors.values()))["count"] == 2


@requires_secure_unlink
def test_monitor_aborts_delete_when_intent_directory_fsync_fails(tmp_path, monkeypatch):
    monitor = make_monitor(tmp_path, min_failures=1)
    target = monitor.media_root / "offender.mkv"
    target.write_bytes(b"media")
    delete_target = direct_delete_target(
        monitor,
        target,
        "/media/offender.mkv",
    )
    monitor.processing_errors[str(target)] = delete_target
    real_fsync = monitor_module.os.fsync

    def fail_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("simulated directory fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(monitor_module.os, "fsync", fail_directory_fsync)

    with pytest.raises(OSError, match="directory fsync failure"):
        monitor.try_delete_path(
            str(target),
            delete_target,
            "MISSING",
            "DELETED",
            "FAILED",
        )

    assert target.read_bytes() == b"media"


@requires_secure_unlink
def test_monitor_retains_token_and_recovers_post_quarantine_sync_failure(
    tmp_path, monkeypatch
):
    args = make_args(tmp_path / "media", tmp_path / "state", min_failures=1)
    monitor = Monitor(args)
    monitor.media_root.mkdir()
    target = monitor.media_root / "offender.mkv"
    target.write_bytes(b"media")
    delete_target = direct_delete_target(
        monitor,
        target,
        "/media/offender.mkv",
    )
    monitor.processing_errors[str(target)] = delete_target
    real_unlink = safety_module.os.unlink
    real_fsync = safety_module.os.fsync
    candidate_unlinked = False
    failure_injected = False

    def track_candidate_unlink(path, *, dir_fd=None):
        nonlocal candidate_unlinked
        result = real_unlink(path, dir_fd=dir_fd)
        if path == "candidate":
            candidate_unlinked = True
        return result

    def fail_post_unlink_sync(descriptor):
        nonlocal failure_injected
        if (
            candidate_unlinked
            and not failure_injected
            and stat.S_ISDIR(os.fstat(descriptor).st_mode)
        ):
            failure_injected = True
            raise OSError("simulated post-unlink sync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(safety_module.os, "unlink", track_candidate_unlink)
    monkeypatch.setattr(safety_module.os, "fsync", fail_post_unlink_sync)

    monitor.try_delete_path(
        str(target),
        delete_target,
        "MISSING",
        "DELETED",
        "FAILED",
    )

    item = next(iter(monitor.processing_errors.values()))
    assert item["delete_status"] == "deleting"
    assert item["delete_token"]
    assert item["delete_identity"]
    assert failure_injected is True

    monkeypatch.setattr(safety_module.os, "unlink", real_unlink)
    monkeypatch.setattr(safety_module.os, "fsync", real_fsync)
    recovered = Monitor(args)
    recovered_item = next(iter(recovered.processing_errors.values()))
    assert recovered_item["delete_status"] == "deleted_recovered"


@requires_secure_unlink
def test_monitor_does_not_overwrite_recovery_owned_intent_with_new_log_event(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=3)
    target = monitor.media_root / "offender.mkv"
    target.write_bytes(b"media")
    identity = list(file_identity(target.stat()))
    token = new_delete_token()
    item = {
        "host_path": str(target),
        "container_path": "/media/offender.mkv",
        "first_seen_utc": "2026-01-01T00:00:00Z",
        "last_seen_utc": "2026-01-01T00:00:00Z",
        "count": 3,
        "failure_identity": identity,
        "delete_status": "blocked_recovery",
        "delete_identity": identity,
        "delete_token": token,
    }
    monitor.processing_errors[str(target)] = item
    monitor.save_state()

    monitor.record_processing_error("/media/offender.mkv")

    retained = next(iter(monitor.processing_errors.values()))
    assert retained["delete_status"] == "blocked_recovery"
    assert retained["delete_token"] == token
    assert retained["count"] == 3


def test_monitor_quarantines_and_audits_unsafe_persisted_delete_intent(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    unsafe_path = f"{media_root}/unused/../offender.mkv"
    state_path = state_dir / "subgen_failed_state.json"
    state_path.write_text(
        json.dumps(
            {
                "processing_errors": [
                    {
                        "host_path": unsafe_path,
                        "container_path": "/media/offender.mkv",
                        "first_seen_utc": "2026-01-01T00:00:00Z",
                        "last_seen_utc": "2026-01-01T00:00:00Z",
                        "count": 3,
                        "delete_status": "deleting",
                        "delete_identity": [1, 2],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monitor = Monitor(make_args(media_root, state_dir))

    item = next(iter(monitor.processing_errors.values()))
    assert item["delete_status"] == "blocked_recovery"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["processing_errors"][0]["delete_status"] == "blocked_recovery"
    assert "[FILE_DELETE_RECOVERY_BLOCKED]" in monitor.events_path.read_text(
        encoding="utf-8"
    )


def test_monitor_resets_legacy_path_only_failure_count_before_new_evidence(
    tmp_path,
):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    target = media_root / "offender.mkv"
    target.write_bytes(b"replacement that must get fresh evidence")
    state_path = state_dir / "subgen_failed_state.json"
    state_path.write_text(
        json.dumps(
            {
                "processing_errors": [
                    {
                        "host_path": str(target),
                        "container_path": "/media/offender.mkv",
                        "first_seen_utc": "2025-01-01T00:00:00Z",
                        "last_seen_utc": "2025-01-01T00:00:00Z",
                        "count": 2,
                        "delete_status": "waiting",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monitor = Monitor(make_args(media_root, state_dir, min_failures=3))

    item = next(iter(monitor.processing_errors.values()))
    assert item["count"] == 0
    assert item["failure_identity"] is None
    monitor.record_processing_error("/media/offender.mkv")
    assert target.exists()
    item = next(iter(monitor.processing_errors.values()))
    assert item["count"] == 1
    assert item["failure_identity"] is None
    assert item["delete_status"] == "policy_blocked"


def test_structured_worker_events_keep_duplicate_basenames_separate(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=False)
    first = monitor.media_root / "show-a" / "Episode 1.mkv"
    second = monitor.media_root / "show-b" / "Episode 1.mkv"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    for task_id, container_path in (
        ("task-a", "/media/show-a/Episode 1.mkv"),
        ("task-b", "/media/show-b/Episode 1.mkv"),
    ):
        event = {
            "event": "worker_start",
            "task_id": task_id,
            "task_type": "transcribe",
            "path": container_path,
        }
        monitor.process_log_line("SUBGEN_EVENT " + json.dumps(event))
        monitor.process_log_line("process died with SIGSEGV")

    assert len(monitor.crash_candidates) == 2
    assert {item["host_path"] for item in monitor.crash_candidates.values()} == {
        str(first.resolve()),
        str(second.resolve()),
    }


def test_finished_structured_task_is_not_blamed_for_later_sigsegv(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=False)
    target = monitor.media_root / "show" / "episode.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    task = {
        "task_id": "task-1",
        "task_type": "transcribe",
        "path": "/media/show/episode.mkv",
    }

    monitor.process_log_line(
        "SUBGEN_EVENT " + json.dumps({"event": "worker_start", **task})
    )
    monitor.process_log_line(
        "SUBGEN_EVENT " + json.dumps({"event": "worker_finish", **task})
    )
    monitor.process_log_line("unrelated process died with SIGSEGV")

    assert monitor.crash_candidates == {}


def test_sigsegv_with_multiple_active_tasks_is_not_attributed(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=1)
    targets = []
    for index in (1, 2):
        target = monitor.media_root / f"show-{index}" / "episode.mkv"
        target.parent.mkdir()
        target.write_bytes(f"media-{index}".encode())
        targets.append(target)
        emit_structured(
            monitor,
            {
                "event": "worker_start",
                "task_id": f"task-{index}",
                "task_type": "transcribe",
                "path": f"/media/show-{index}/episode.mkv",
                "source_identity": list(file_identity(target.stat())),
            },
        )

    monitor.process_log_line("native process exited with SIGSEGV")

    assert monitor.crash_candidates == {}
    assert monitor.active_tasks == {}
    assert [target.read_bytes() for target in targets] == [b"media-1", b"media-2"]
    assert "active=2" in monitor.events_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "error_code",
    [
        "model_load_profile_unhealthy",
        "model_release_failed",
        "model_runtime_cancelled",
        "memory_pressure_yield",
    ],
)
def test_model_runtime_error_is_never_attributed_to_media(tmp_path, error_code):
    monitor = make_monitor(
        tmp_path,
        auto_delete=True,
        min_failures=1,
        auto_mark=True,
        mark_min_failures=1,
    )
    target = monitor.media_root / "show" / "episode.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    start = {
        "event": "worker_start",
        "task_id": "runtime-task",
        "task_type": "transcribe",
        "path": "/media/show/episode.mkv",
    }
    unrelated = {
        "display_name": "other.mkv",
        "container_path": "/media/other/other.mkv",
        "seen_utc": "2026-08-31T00:00:00Z",
    }

    monitor.process_log_line("SUBGEN_EVENT " + json.dumps(start))
    monitor.last_transcribe_start = unrelated.copy()
    monitor.process_log_line(
        "SUBGEN_EVENT "
        + json.dumps(
            {
                "event": "runtime_error",
                "task_id": "runtime-task",
                "task_type": "transcribe",
                "scope": "model_runtime",
                "error_code": error_code,
            }
        )
    )

    assert "runtime-task" not in monitor.active_tasks
    assert monitor.last_transcribe_start == unrelated
    assert monitor.processing_errors == {}
    assert monitor.crash_candidates == {}
    assert target.exists()
    assert f"[MODEL_RUNTIME_ERROR] {error_code}" in (
        monitor.events_path.read_text(encoding="utf-8")
    )


def test_human_progress_logs_never_create_failure_evidence(tmp_path):
    monitor = make_monitor(
        tmp_path,
        auto_delete=True,
        min_failures=1,
        auto_mark=True,
        mark_min_failures=1,
    )
    target = monitor.media_root / "show" / "episode.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    task = {
        "event": "worker_start",
        "task_id": "progress-task",
        "task_type": "transcribe",
        "path": "/media/show/episode.mkv",
        "source_identity": list(file_identity(target.stat())),
    }
    emit_structured(monitor, task)

    for line in (
        "Starting file: episode.mkv",
        "Memory available: 10.0 GiB",
        "Memory reserved for system/priority tasks: 2.0 GiB",
        "Subgen memory in use / limit: 6.0 GiB / 10.0 GiB",
        "Model suitable: medium",
        "Model using: medium - 5.5 GiB RAM requirement",
        "Available for subtitle chunks: 3.0 GiB working headroom",
        "File split into 3 planned chunks: episode.mkv",
        "Chunk 1/3 started — 0% of file complete (00:00:00 to 00:05:00)",
        "Higher-priority memory pressure; releasing the uncommitted chunk",
        "Memory recovered; retrying chunk 1 with a 5-minute window",
        "Chunk 1/3 finished — 33% of file complete",
        "Joining chunks 1–3",
        "Chunks joined",
        "File finished successfully: episode.mkv",
    ):
        monitor.process_log_line(line)

    assert "progress-task" in monitor.active_tasks
    assert monitor.processing_errors == {}
    assert monitor.crash_candidates == {}
    assert target.read_bytes() == b"media"

    emit_structured(monitor, {**task, "event": "worker_finish"})

    assert monitor.active_tasks == {}
    assert monitor.processing_errors == {}
    assert monitor.crash_candidates == {}
    assert target.read_bytes() == b"media"


def test_repeated_unfinished_structured_task_marks_and_retains_exact_file(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=3)
    target = monitor.media_root / "show" / "stuck.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    event = {
        "event": "worker_start",
        "task_id": "same-task",
        "task_type": "transcribe",
        "path": "/media/show/stuck.mkv",
        "source_identity": list(file_identity(target.stat())),
    }

    for _ in range(3):
        monitor.process_log_line("SUBGEN_EVENT " + json.dumps(event))
        assert target.exists()

    monitor.process_log_line("SUBGEN_EVENT " + json.dumps(event))

    assert target.read_bytes() == b"media"
    candidate = next(iter(monitor.crash_candidates.values()))
    assert candidate["count"] == 3
    assert candidate["marker_status"] in {"created", "refreshed"}
    assert candidate["delete_status"] == "policy_blocked"
