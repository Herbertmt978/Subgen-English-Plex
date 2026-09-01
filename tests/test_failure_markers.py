import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import monitor_subgen_failures as monitor_module
import subgen_failure_markers as markers
from language_code import LanguageCode
from monitor_subgen_failures import Monitor
from subgen_core import media
from subgen_failure_markers import (
    FailureMarkerReader,
    MarkerRegistryError,
    build_marker_entry,
    encode_marker_document,
    load_marker_document,
)
from subgen_ops_safety import file_identity, supports_secure_unlink

STAMP = "2026-08-30T12:00:00Z"
requires_secure_unlink = pytest.mark.skipif(
    not supports_secure_unlink(),
    reason="requires Linux descriptor-relative unlink primitives",
)


class QueueProbe:
    def __init__(self):
        self.items = []

    def is_active(self, _path):
        return False

    def put(self, item):
        self.items.append(item)


def write_registry(registry: Path, entries: list[dict], *, updated: str = STAMP) -> None:
    registry.write_text(
        encode_marker_document(entries, updated),
        encoding="utf-8",
    )


def entry_for(path: Path, container_path: str, *, kind: str = "processing_error") -> dict:
    return build_marker_entry(
        container_path,
        file_identity(path.lstat()),
        kind,
        1,
        STAMP,
    )


def create_symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")


def make_media_runtime(reader, *, skip_marked=True):
    queue = QueueProbe()
    audio_calls = []
    events = []
    audio_track = {
        "index": 0,
        "language": LanguageCode.ENGLISH,
        "default": True,
    }
    evidence = media.ValidatorEvidence(media.ValidatorOutcome.AUDIO_PRESENT)
    validation = media.MediaValidation(
        media.MediaOutcome.VALID_AUDIO,
        evidence,
        evidence,
        duration_seconds=60.0,
        audio_tracks=(audio_track,),
    )
    runtime = SimpleNamespace(
        task_queue=queue,
        logging=MagicMock(),
        os=os,
        skip_marked_failed_files=skip_marked,
        failure_marker_reader=reader,
        emit_subgen_event=lambda event, task, error=None, **_kwargs: events.append(
            (event, task, error)
        ),
        validate_media=lambda path: audio_calls.append(path) or validation,
        choose_transcribe_language=lambda _path, forced, audio_tracks=None: (
            forced or LanguageCode.ENGLISH
        ),
        select_audio_track=lambda tracks, _language: tracks[0],
        should_skip_file=lambda *_args, **_kwargs: False,
        should_whisper_detect_audio_language=False,
        force_detected_language_to=LanguageCode.NONE,
    )
    return runtime, queue, audio_calls, events


def make_marker_monitor_args(
    media_root: Path,
    state_dir: Path,
    *,
    auto_delete: bool = True,
    min_failures: int = 1,
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
        auto_delete_failed_files=False,
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


def make_failure_monitor(
    tmp_path: Path,
    *,
    auto_delete: bool = True,
    min_failures: int = 1,
    auto_mark: bool = True,
    mark_min_failures: int | None = None,
):
    media_root = tmp_path / "media"
    media_root.mkdir()
    return Monitor(
        make_marker_monitor_args(
            media_root,
            tmp_path / "state",
            auto_delete=auto_delete,
            min_failures=min_failures,
            auto_mark=auto_mark,
            mark_min_failures=mark_min_failures,
        )
    )


def record_trusted_processing_error(
    monitor: Monitor,
    target: Path,
    container_path: str,
) -> None:
    monitor.record_processing_error(
        container_path,
        failure_event="worker_error",
        failure_class="inference_error",
        source_identity=list(file_identity(target.stat())),
    )


def record_invalid_media(
    monitor: Monitor,
    target: Path,
    container_path: str,
) -> None:
    monitor.record_processing_error(
        container_path,
        failure_event="media_validation_failed",
        failure_class="invalid_media",
        source_identity=list(file_identity(target.stat())),
        validator_outcomes={
            "ffprobe": "invalid_format",
            "pyav": "invalid_format",
        },
        validation_detail="dual_parser_invalid",
    )


def test_marker_document_round_trips_exact_case_sensitive_paths(tmp_path):
    registry = tmp_path / "markers.json"
    entries = [
        build_marker_entry(
            "/media/TV/Show/Episode.mkv",
            [1, 2, 3, 4, 5],
            "processing_error",
            1,
            STAMP,
        ),
        build_marker_entry(
            "/media/TV/show/Episode.mkv",
            [6, 7, 8, 9, 10],
            "sigsegv",
            2,
            STAMP,
        ),
    ]

    write_registry(registry, reversed(entries))
    loaded = load_marker_document(registry)

    assert [item["container_path"] for item in loaded["markers"]] == [
        "/media/TV/Show/Episode.mkv",
        "/media/TV/show/Episode.mkv",
    ]
    assert loaded["markers"][1]["failure_kind"] == "sigsegv"


def test_marker_document_rejects_duplicate_paths_and_invalid_identity(tmp_path):
    duplicate = build_marker_entry(
        "/media/TV/Show/Episode.mkv",
        [1, 2, 3, 4, 5],
        "processing_error",
        1,
        STAMP,
    )
    with pytest.raises(MarkerRegistryError, match="duplicate"):
        encode_marker_document([duplicate, duplicate], STAMP)

    registry = tmp_path / "markers.json"
    invalid = {
        "schema_version": 1,
        "updated_utc": STAMP,
        "markers": [{**duplicate, "file_identity": [1, 2, 3]}],
    }
    registry.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(MarkerRegistryError, match="five non-negative"):
        load_marker_document(registry)


def test_reader_matches_exact_generation(tmp_path):
    media_root = tmp_path / "media"
    media_file = media_root / "TV" / "Show" / "Episode.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"original generation")
    registry = tmp_path / "markers.json"
    write_registry(registry, [entry_for(media_file, "/media/TV/Show/Episode.mkv")])

    decision = FailureMarkerReader(registry, media_root=media_root).check(media_file)

    assert decision.status == "matched"
    assert decision.report is True


def test_reader_marks_replacement_generation_stale(tmp_path):
    media_root = tmp_path / "media"
    media_file = media_root / "Movies" / "Film.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"old")
    registry = tmp_path / "markers.json"
    write_registry(registry, [entry_for(media_file, "/media/Movies/Film.mkv")])
    reader = FailureMarkerReader(registry, media_root=media_root)
    assert reader.check(media_file).status == "matched"

    media_file.unlink()
    media_file.write_bytes(b"replacement generation with a different fingerprint")

    assert reader.check(media_file).status == "stale"


def test_reader_keeps_duplicate_basenames_independent(tmp_path):
    media_root = tmp_path / "media"
    first = media_root / "Show A" / "Episode.mkv"
    second = media_root / "Show B" / "Episode.mkv"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    registry = tmp_path / "markers.json"
    write_registry(registry, [entry_for(first, "/media/Show A/Episode.mkv")])
    reader = FailureMarkerReader(registry, media_root=media_root)

    assert reader.check(first).status == "matched"
    assert reader.check(second).status == "unmarked"


def test_reader_fails_open_for_missing_malformed_and_oversized_registry(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    media_file = media_root / "Film.mkv"
    media_file.write_bytes(b"media")
    registry = tmp_path / "markers.json"

    assert FailureMarkerReader(registry, media_root=media_root).check(media_file).status == "unmarked"

    registry.write_text("{not-json", encoding="utf-8")
    malformed = FailureMarkerReader(registry, media_root=media_root).check(media_file)
    assert malformed.status == "unavailable"
    assert malformed.report is True

    registry.write_bytes(b"x" * 65)
    oversized = FailureMarkerReader(
        registry,
        media_root=media_root,
        max_bytes=64,
    ).check(media_file)
    assert oversized.status == "unavailable"
    assert "byte limit" in oversized.detail


def test_reader_refuses_symlinked_registry_and_media_leaf(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    real_media = media_root / "real.mkv"
    real_media.write_bytes(b"media")
    media_link = media_root / "linked.mkv"
    create_symlink_or_skip(media_link, real_media)

    real_registry = tmp_path / "real-markers.json"
    write_registry(real_registry, [entry_for(real_media, "/media/real.mkv")])
    registry_link = tmp_path / "markers.json"
    create_symlink_or_skip(registry_link, real_registry)

    assert FailureMarkerReader(registry_link, media_root=media_root).check(real_media).status == "unavailable"
    assert FailureMarkerReader(real_registry, media_root=media_root).check(media_link).status == "unmarked"


def test_reader_reloads_only_after_registry_metadata_changes(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    media_root.mkdir()
    media_file = media_root / "Film.mkv"
    media_file.write_bytes(b"media")
    registry = tmp_path / "markers.json"
    write_registry(registry, [entry_for(media_file, "/media/Film.mkv")])
    calls = 0
    original_load = markers.load_marker_document

    def counted_load(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(markers, "load_marker_document", counted_load)
    reader = FailureMarkerReader(registry, media_root=media_root)
    assert reader.check(media_file).status == "matched"
    assert reader.check(media_file).status == "matched"
    assert calls == 1

    write_registry(registry, [])
    current = registry.stat()
    os.utime(registry, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))

    assert reader.check(media_file).status == "unmarked"
    assert calls == 2


def test_reader_retries_if_registry_is_replaced_during_reload(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    media_root.mkdir()
    media_file = media_root / "Film.mkv"
    media_file.write_bytes(b"media")
    registry = tmp_path / "markers.json"
    write_registry(registry, [entry_for(media_file, "/media/Film.mkv")])
    calls = 0
    original_load = markers.load_marker_document

    def replacing_load(*args, **kwargs):
        nonlocal calls
        document = original_load(*args, **kwargs)
        calls += 1
        if calls == 1:
            replacement = registry.with_suffix(".replacement")
            write_registry(replacement, [])
            os.replace(replacement, registry)
        return document

    monkeypatch.setattr(markers, "load_marker_document", replacing_load)

    decision = FailureMarkerReader(registry, media_root=media_root).check(media_file)

    assert decision.status == "unmarked"
    assert calls == 2


def test_reader_rate_limits_unchanged_failure_and_stale_reports(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    media_file = media_root / "Film.mkv"
    media_file.write_bytes(b"media")
    registry = tmp_path / "markers.json"
    registry.write_text("invalid", encoding="utf-8")
    reader = FailureMarkerReader(registry, media_root=media_root)

    assert reader.check(media_file).report is True
    assert reader.check(media_file).report is False

    stale_entry = build_marker_entry(
        "/media/Film.mkv",
        [0, 0, 0, 0, 0],
        "processing_error",
        1,
        STAMP,
    )
    write_registry(registry, [stale_entry])
    current = registry.stat()
    os.utime(registry, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))

    first = reader.check(media_file)
    second = reader.check(media_file)
    assert (first.status, first.report) == ("stale", True)
    assert (second.status, second.report) == ("stale", False)


def test_reader_rejects_candidates_outside_media_root(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")
    registry = tmp_path / "markers.json"
    write_registry(registry, [])

    decision = FailureMarkerReader(registry, media_root=media_root).check(outside)

    assert decision.status == "unmarked"
    assert decision.report is False


def test_public_marker_default_is_first_failure_and_delete_remains_off(
    monkeypatch,
):
    monkeypatch.delenv("AUTO_MARK_FAILED_FILES", raising=False)
    monkeypatch.delenv("AUTO_MARK_MIN_FAILURES", raising=False)
    monkeypatch.delenv("AUTO_DELETE_INVALID_MEDIA", raising=False)
    monkeypatch.delenv("AUTO_DELETE_FAILED_FILES", raising=False)
    monkeypatch.setattr(sys, "argv", ["monitor_subgen_failures.py"])

    args = monitor_module.parse_args()

    assert args.auto_mark_failed_files is True
    assert args.auto_mark_min_failures == 1
    assert args.auto_delete_invalid_media is False
    assert args.auto_delete_failed_files is False


def test_monitor_writes_first_failure_marker_without_deleting(tmp_path):
    monitor = make_failure_monitor(tmp_path, auto_delete=False)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")

    record_trusted_processing_error(
        monitor,
        target,
        "/media/library/offender.mkv",
    )

    document = load_marker_document(monitor.marker_registry_path)
    assert target.read_bytes() == b"media"
    assert len(document["markers"]) == 1
    marker = document["markers"][0]
    assert marker["container_path"] == "/media/library/offender.mkv"
    assert marker["file_identity"] == list(file_identity(target.stat()))
    assert marker["failure_kind"] == "processing_error"
    assert marker["failure_count"] == 1
    assert "[FAILURE_MARKER_CREATED]" in monitor.events_path.read_text(
        encoding="utf-8"
    )


def test_monitor_persists_marker_before_delete_invocation(tmp_path, monkeypatch):
    monitor = make_failure_monitor(tmp_path)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    order = []
    real_atomic_write = monitor_module.atomic_write_text

    def ordered_write(path, text):
        if Path(path) == monitor.marker_registry_path:
            order.append("marker")
        return real_atomic_write(path, text)

    def inspect_delete(*_args, **_kwargs):
        marker = load_marker_document(monitor.marker_registry_path)["markers"][0]
        assert marker["file_identity"] == list(file_identity(target.stat()))
        order.append("delete")

    monkeypatch.setattr(monitor_module, "atomic_write_text", ordered_write)
    monkeypatch.setattr(monitor, "try_delete_path", inspect_delete)

    record_invalid_media(monitor, target, "/media/library/offender.mkv")

    assert order[:2] == ["marker", "delete"]
    assert target.exists()


def test_monitor_marker_write_failure_blocks_delete_and_is_audited(
    tmp_path, monkeypatch
):
    monitor = make_failure_monitor(tmp_path)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    real_atomic_write = monitor_module.atomic_write_text
    delete_calls = []

    def fail_marker_write(path, text):
        if Path(path) == monitor.marker_registry_path:
            raise OSError("simulated marker write failure")
        return real_atomic_write(path, text)

    monkeypatch.setattr(monitor_module, "atomic_write_text", fail_marker_write)
    monkeypatch.setattr(
        monitor,
        "try_delete_path",
        lambda *_args, **_kwargs: delete_calls.append("delete"),
    )

    record_invalid_media(monitor, target, "/media/library/offender.mkv")

    item = next(iter(monitor.processing_errors.values()))
    assert target.read_bytes() == b"media"
    assert delete_calls == []
    assert item["marker_status"] == "write_failed"
    assert item["delete_status"] == "marker_blocked"
    assert "[FAILURE_MARKER_WRITE_FAILED]" in monitor.events_path.read_text(
        encoding="utf-8"
    )


def test_marker_gate_does_not_adopt_unfingerprinted_failure(
    tmp_path, monkeypatch
):
    monitor = make_failure_monitor(tmp_path)
    target = monitor.media_root / "offender.mkv"
    target.write_bytes(b"appeared after failure evidence")
    delete_calls = []

    monkeypatch.setattr(
        monitor,
        "try_delete_path",
        lambda *_args, **_kwargs: delete_calls.append("delete"),
    )

    monitor.record_processing_error("/media/offender.mkv")

    item = next(iter(monitor.processing_errors.values()))
    assert target.exists()
    assert item["count"] == 1
    assert item["failure_identity"] is None
    assert item["delete_status"] == "policy_blocked"
    assert delete_calls == []
    assert not monitor.marker_registry_path.exists()


def test_monitor_refuses_to_overwrite_invalid_marker_registry(tmp_path, monkeypatch):
    monitor = make_failure_monitor(tmp_path)
    target = monitor.media_root / "offender.mkv"
    target.write_bytes(b"media")
    monitor.marker_registry_path.write_text("invalid registry", encoding="utf-8")
    delete_calls = []
    monkeypatch.setattr(
        monitor,
        "try_delete_path",
        lambda *_args, **_kwargs: delete_calls.append("delete"),
    )

    record_invalid_media(monitor, target, "/media/offender.mkv")

    item = next(iter(monitor.processing_errors.values()))
    assert monitor.marker_registry_path.read_text(encoding="utf-8") == "invalid registry"
    assert delete_calls == []
    assert item["marker_status"] == "write_failed"
    assert item["delete_status"] == "marker_blocked"


def test_delete_waits_until_higher_marker_threshold_is_durable(tmp_path, monkeypatch):
    monitor = make_failure_monitor(
        tmp_path,
        min_failures=1,
        mark_min_failures=3,
    )
    target = monitor.media_root / "offender.mkv"
    target.write_bytes(b"media")
    delete_calls = []

    def inspect_delete(*_args, **_kwargs):
        assert load_marker_document(monitor.marker_registry_path)["markers"][0][
            "failure_count"
        ] == 3
        delete_calls.append("delete")

    monkeypatch.setattr(monitor, "try_delete_path", inspect_delete)

    for _ in range(2):
        record_invalid_media(monitor, target, "/media/offender.mkv")
        assert delete_calls == []
        assert next(iter(monitor.processing_errors.values()))[
            "delete_status"
        ] == "marker_blocked"
    record_invalid_media(monitor, target, "/media/offender.mkv")

    assert delete_calls == ["delete"]


def test_monitor_refreshes_same_generation_without_duplicate_entry(tmp_path):
    monitor = make_failure_monitor(tmp_path, auto_delete=False)
    target = monitor.media_root / "offender.mkv"
    target.write_bytes(b"media")

    record_trusted_processing_error(monitor, target, "/media/offender.mkv")
    original = load_marker_document(monitor.marker_registry_path)["markers"][0]
    record_trusted_processing_error(monitor, target, "/media/offender.mkv")

    document = load_marker_document(monitor.marker_registry_path)
    assert len(document["markers"]) == 1
    assert document["markers"][0]["created_utc"] == original["created_utc"]
    assert document["markers"][0]["failure_count"] == 2
    assert next(iter(monitor.processing_errors.values()))["marker_status"] == "refreshed"


def test_monitor_replaces_marker_entry_for_new_generation(tmp_path):
    monitor = make_failure_monitor(tmp_path, auto_delete=False)
    target = monitor.media_root / "offender.mkv"
    target.write_bytes(b"old")

    record_trusted_processing_error(monitor, target, "/media/offender.mkv")
    old_identity = load_marker_document(monitor.marker_registry_path)["markers"][0][
        "file_identity"
    ]
    target.unlink()
    target.write_bytes(b"replacement generation")
    record_trusted_processing_error(monitor, target, "/media/offender.mkv")

    document = load_marker_document(monitor.marker_registry_path)
    assert len(document["markers"]) == 1
    assert document["markers"][0]["file_identity"] != old_identity
    assert document["markers"][0]["file_identity"] == list(file_identity(target.stat()))
    assert document["markers"][0]["failure_count"] == 1
    assert next(iter(monitor.processing_errors.values()))["marker_status"] == "created"


def test_monitor_marks_exact_sigsegv_candidate_but_never_deletes(
    tmp_path,
    monkeypatch,
):
    monitor = make_failure_monitor(tmp_path)
    target = monitor.media_root / "show" / "episode.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    delete_calls = []

    def inspect_delete(*_args, **_kwargs):
        marker = load_marker_document(monitor.marker_registry_path)["markers"][0]
        assert marker["failure_kind"] == "sigsegv"
        assert marker["container_path"] == "/media/show/episode.mkv"
        delete_calls.append("delete")

    monkeypatch.setattr(monitor, "try_delete_path", inspect_delete)

    monitor.record_crash_candidate(
        target.name,
        container_path="/media/show/episode.mkv",
        source_identity=list(file_identity(target.stat())),
    )

    marker = load_marker_document(monitor.marker_registry_path)["markers"][0]
    assert marker["failure_kind"] == "sigsegv"
    assert marker["container_path"] == "/media/show/episode.mkv"
    assert delete_calls == []
    assert target.exists()


def test_monitor_keeps_legacy_basename_only_crash_candidate_report_only(
    tmp_path, monkeypatch
):
    monitor = make_failure_monitor(tmp_path)
    delete_calls = []
    monkeypatch.setattr(
        monitor,
        "try_delete_path",
        lambda *_args, **_kwargs: delete_calls.append("delete"),
    )

    monitor.record_crash_candidate("episode.mkv")

    item = next(iter(monitor.crash_candidates.values()))
    assert item["marker_status"] == "report_only"
    assert delete_calls == []
    assert not monitor.marker_registry_path.exists()


def test_delete_threshold_may_exceed_marker_threshold(tmp_path, monkeypatch):
    monitor = make_failure_monitor(
        tmp_path,
        auto_delete=True,
        min_failures=3,
        auto_mark=True,
        mark_min_failures=1,
    )
    target = monitor.media_root / "offender.mkv"
    target.write_bytes(b"media")
    monkeypatch.setattr(monitor, "try_delete_path", lambda *_args, **_kwargs: None)

    gates = []
    for _ in range(3):
        record_invalid_media(monitor, target, "/media/offender.mkv")
        item = next(iter(monitor.processing_errors.values()))
        gates.append(monitor.marker_allows_delete(item))

    assert gates == [False, False, True]
    marker = load_marker_document(monitor.marker_registry_path)["markers"][0]
    assert marker["failure_count"] == 3


def test_marker_disabled_blocks_invalid_media_delete_policy(tmp_path):
    with pytest.raises(ValueError, match="automatic failure markers"):
        make_failure_monitor(
            tmp_path,
            auto_delete=True,
            min_failures=1,
            auto_mark=False,
            mark_min_failures=1,
        )


def test_matching_marker_skips_before_media_validation(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    media_file = media_root / "Film.mkv"
    media_file.write_bytes(b"failed generation")
    registry = tmp_path / "markers.json"
    write_registry(registry, [entry_for(media_file, "/media/Film.mkv")])
    runtime, queue, audio_calls, events = make_media_runtime(
        FailureMarkerReader(registry, media_root=media_root)
    )

    media.gen_subtitles_queue(runtime, str(media_file), "transcribe")
    media.gen_subtitles_queue(runtime, str(media_file), "transcribe")

    assert audio_calls == []
    assert queue.items == []
    assert [event[0] for event in events] == ["failure_marker_skip"]


def test_replacement_identity_reaches_media_validation_and_queue(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    media_file = media_root / "Film.mkv"
    media_file.write_bytes(b"failed generation")
    registry = tmp_path / "markers.json"
    write_registry(registry, [entry_for(media_file, "/media/Film.mkv")])
    media_file.unlink()
    media_file.write_bytes(b"replacement generation with different identity")
    runtime, queue, audio_calls, events = make_media_runtime(
        FailureMarkerReader(registry, media_root=media_root)
    )

    media.gen_subtitles_queue(runtime, str(media_file), "transcribe")

    assert audio_calls == [str(media_file)]
    assert [item["path"] for item in queue.items] == [str(media_file)]
    assert [event[0] for event in events] == ["failure_marker_stale"]


def test_skip_marked_failed_files_false_ignores_matching_marker(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    media_file = media_root / "Film.mkv"
    media_file.write_bytes(b"media")

    class UnexpectedReader:
        def check(self, _path):
            raise AssertionError("disabled marker skipping must not read the registry")

    runtime, queue, audio_calls, events = make_media_runtime(
        UnexpectedReader(),
        skip_marked=False,
    )

    media.gen_subtitles_queue(runtime, str(media_file), "transcribe")

    assert audio_calls == [str(media_file)]
    assert [item["path"] for item in queue.items] == [str(media_file)]
    assert events == []


def test_malformed_registry_reaches_media_validation_with_one_warning(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    media_file = media_root / "Film.mkv"
    media_file.write_bytes(b"media")
    registry = tmp_path / "markers.json"
    registry.write_text("invalid registry", encoding="utf-8")
    runtime, queue, audio_calls, events = make_media_runtime(
        FailureMarkerReader(registry, media_root=media_root)
    )

    media.gen_subtitles_queue(runtime, str(media_file), "transcribe")
    media.gen_subtitles_queue(runtime, str(media_file), "transcribe")

    assert audio_calls == [str(media_file), str(media_file)]
    assert len(queue.items) == 2
    assert [event[0] for event in events] == ["failure_marker_read_failed"]
