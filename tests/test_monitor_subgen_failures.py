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
from subgen_ops_safety import file_identity, new_delete_token, supports_secure_unlink


requires_secure_unlink = pytest.mark.skipif(
    not supports_secure_unlink(),
    reason="requires Linux descriptor-relative unlink primitives",
)


def make_args(media_root: Path, state_dir: Path, *, auto_delete: bool = True, min_failures: int = 3):
    return SimpleNamespace(
        container="subgen",
        media_root=str(media_root),
        state_dir=str(state_dir),
        auto_delete_failed_files=auto_delete,
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


def make_monitor(tmp_path: Path, *, auto_delete: bool = True, min_failures: int = 3):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    return Monitor(make_args(media_root, state_dir, auto_delete=auto_delete, min_failures=min_failures))


def test_auto_delete_is_opt_in_by_default(monkeypatch):
    monkeypatch.delenv("AUTO_DELETE_FAILED_FILES", raising=False)
    monkeypatch.setattr(sys, "argv", ["monitor_subgen_failures.py"])

    assert monitor_module.parse_args().auto_delete_failed_files is False


@requires_secure_unlink
def test_processing_error_deletes_only_after_threshold(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=3)
    target = monitor.media_root / "library" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")

    monitor.record_processing_error("/media/library/offender.mkv")
    assert target.exists()
    monitor.record_processing_error("/media/library/offender.mkv")
    assert target.exists()
    monitor.record_processing_error("/media/library/offender.mkv")

    assert not target.exists()
    item = monitor.processing_errors[str(target).lower()]
    assert item["count"] == 3
    assert item["delete_status"] == "deleted"


def test_monitor_never_recursively_deletes_a_directory(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=1)
    target = monitor.media_root / "library" / "season"
    target.mkdir(parents=True)
    (target / "episode.mkv").write_bytes(b"media")

    monitor.record_processing_error("/media/library/season")

    assert target.exists()
    item = next(iter(monitor.processing_errors.values()))
    assert item["delete_status"] == "blocked"


def test_monitor_revalidates_media_root_before_delete(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=1)
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")
    target = {"count": 1}

    monitor.try_delete_path(outside, target, "MISSING", "DELETED", "FAILED")

    assert outside.exists()
    assert target["delete_status"] == "blocked"


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
    assert item["delete_status"] == "blocked"


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
    assert item["delete_status"] == "blocked"


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
    assert item["delete_status"] is None


def test_monitor_blocks_lexical_parent_traversal_in_host_path(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=1)
    (monitor.media_root / "library").mkdir()
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")
    lexical_escape = monitor.media_root / "library" / ".." / ".." / outside.name
    target = {"count": 1}

    monitor.try_delete_path(
        str(lexical_escape),
        target,
        "MISSING",
        "DELETED",
        "FAILED",
    )

    assert outside.read_bytes() == b"outside"
    assert target["delete_status"] == "blocked"


def test_monitor_rejects_parent_traversal_even_when_it_stays_inside_media_root(
    tmp_path,
):
    monitor = make_monitor(tmp_path, min_failures=1)
    target_path = monitor.media_root / "offender.mkv"
    target_path.write_bytes(b"media")
    lexical_traversal = f"{monitor.media_root}/unused/../{target_path.name}"
    target = {"count": 1}

    monitor.try_delete_path(
        lexical_traversal,
        target,
        "MISSING",
        "DELETED",
        "FAILED",
    )

    assert target_path.read_bytes() == b"media"
    assert target["delete_status"] == "blocked"


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

    monitor.record_processing_error("/media/library/offender.mkv")

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


@pytest.mark.skipif(
    os.path.normcase("A") == os.path.normcase("a"),
    reason="requires a case-sensitive filesystem",
)
@requires_secure_unlink
def test_case_distinct_processing_paths_keep_independent_thresholds(tmp_path):
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
    assert sorted(item["count"] for item in monitor.processing_errors.values()) == [1, 2]

    monitor.record_processing_error("/media/Show/Episode.mkv")

    assert not upper.exists()
    assert lower.read_bytes() == b"lower"


@requires_secure_unlink
def test_monitor_aborts_before_unlink_when_delete_intent_cannot_persist(
    tmp_path, monkeypatch
):
    monitor = make_monitor(tmp_path, min_failures=1)
    target_path = monitor.media_root / "offender.mkv"
    target_path.write_bytes(b"media")
    target = {
        "host_path": str(target_path),
        "container_path": "/media/offender.mkv",
        "first_seen_utc": "2026-01-01T00:00:00Z",
        "last_seen_utc": "2026-01-01T00:00:00Z",
        "count": 1,
        "delete_status": None,
        "deleted_utc": None,
        "delete_message": None,
        "failure_identity": list(file_identity(target_path.stat())),
    }
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
    target = {
        "host_path": str(target_path),
        "container_path": "/media/offender.mkv",
        "first_seen_utc": "2026-01-01T00:00:00Z",
        "last_seen_utc": "2026-01-01T00:00:00Z",
        "count": 1,
        "delete_status": None,
        "deleted_utc": None,
        "delete_message": None,
        "failure_identity": list(file_identity(target_path.stat())),
    }
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
def test_monitor_recovery_honours_disabled_auto_delete(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir()
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
    assert item["delete_status"] == "delete_paused"
    assert item["delete_identity"] == identity


@requires_secure_unlink
def test_replacement_at_deleted_monitor_path_starts_a_new_failure_generation(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=3)
    target = monitor.media_root / "show" / "offender.mkv"
    target.parent.mkdir()
    target.write_bytes(b"old offender")
    for _ in range(3):
        monitor.record_processing_error("/media/show/offender.mkv")
    assert not target.exists()

    target.write_bytes(b"fixed replacement with a different fingerprint")
    monitor.record_processing_error("/media/show/offender.mkv")

    assert target.read_bytes().startswith(b"fixed replacement")
    item = next(iter(monitor.processing_errors.values()))
    assert item["count"] == 1
    assert item["delete_status"] == "waiting"


@requires_secure_unlink
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
    assert item["count"] == 0
    assert item["failure_identity"] == list(file_identity(target.stat()))

    monkeypatch.setattr(monitor, "current_failure_identity", real_current_identity)
    monitor.record_processing_error("/media/offender.mkv")
    assert not target.exists()


@requires_secure_unlink
def test_monitor_aborts_delete_when_intent_directory_fsync_fails(
    tmp_path, monkeypatch
):
    monitor = make_monitor(tmp_path, min_failures=1)
    target = monitor.media_root / "offender.mkv"
    target.write_bytes(b"media")
    real_fsync = monitor_module.os.fsync

    def fail_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("simulated directory fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(monitor_module.os, "fsync", fail_directory_fsync)

    with pytest.raises(OSError, match="directory fsync failure"):
        monitor.record_processing_error("/media/offender.mkv")

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

    monitor.record_processing_error("/media/offender.mkv")

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
    state_dir.mkdir()
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


@requires_secure_unlink
def test_monitor_resets_legacy_path_only_failure_count_before_deletion(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir()
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
    monitor.record_processing_error("/media/offender.mkv")
    assert target.exists()
    assert next(iter(monitor.processing_errors.values()))["count"] == 1


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

    monitor.process_log_line("SUBGEN_EVENT " + json.dumps({"event": "worker_start", **task}))
    monitor.process_log_line("SUBGEN_EVENT " + json.dumps({"event": "worker_finish", **task}))
    monitor.process_log_line("unrelated process died with SIGSEGV")

    assert monitor.crash_candidates == {}


@requires_secure_unlink
def test_repeated_unfinished_structured_task_eventually_removes_exact_file(tmp_path):
    monitor = make_monitor(tmp_path, auto_delete=True, min_failures=3)
    target = monitor.media_root / "show" / "stuck.mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    event = {
        "event": "worker_start",
        "task_id": "same-task",
        "task_type": "transcribe",
        "path": "/media/show/stuck.mkv",
    }

    for _ in range(3):
        monitor.process_log_line("SUBGEN_EVENT " + json.dumps(event))
        assert target.exists()

    monitor.process_log_line("SUBGEN_EVENT " + json.dumps(event))

    assert not target.exists()
    candidate = next(iter(monitor.crash_candidates.values()))
    assert candidate["count"] == 3
    assert candidate["delete_status"] == "deleted"
