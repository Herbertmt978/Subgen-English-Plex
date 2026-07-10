import json
import sys
from pathlib import Path
from types import SimpleNamespace

import monitor_subgen_failures as monitor_module
from monitor_subgen_failures import Monitor


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
    item = monitor.processing_errors[str(target).lower()]
    assert item["delete_status"] == "blocked"


def test_monitor_revalidates_media_root_before_delete(tmp_path):
    monitor = make_monitor(tmp_path, min_failures=1)
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")
    target = {"count": 1}

    monitor.try_delete_path(outside, target, "MISSING", "DELETED", "FAILED")

    assert outside.exists()
    assert target["delete_status"] == "blocked"


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
