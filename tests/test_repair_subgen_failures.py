import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import repair_subgen_failures as repair_module
from repair_subgen_failures import Repairer
from subgen_ops_safety import file_identity, new_delete_token, supports_secure_unlink

requires_secure_unlink = pytest.mark.skipif(
    not supports_secure_unlink(),
    reason="requires Linux descriptor-relative unlink primitives",
)


def make_args(
    media_root: Path,
    state_dir: Path,
    *,
    min_crash_count: int = 3,
    action: str = "report",
    event_log_max_bytes: int = 5 * 1024 * 1024,
):
    return SimpleNamespace(
        container="subgen",
        media_root=str(media_root),
        state_dir=str(state_dir),
        lookback="7d",
        min_crash_count=min_crash_count,
        model="large-v3",
        language="en",
        action=action,
        event_log_max_bytes=event_log_max_bytes,
    )


def write_candidates(repairer: Repairer, candidates: list[dict]):
    repairer.monitor_state_path.write_text(
        json.dumps({"crash_candidates": candidates}),
        encoding="utf-8",
    )


def run_candidate_once(args, candidate: dict) -> Repairer:
    repairer = Repairer(args, log_lines=[])
    write_candidates(repairer, [candidate])
    assert repairer.run() == 0
    return repairer


def create_symlink_or_skip(link_path: Path, target_path: Path) -> None:
    try:
        link_path.symlink_to(target_path)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")


def valid_repair_state_bytes() -> bytes:
    return json.dumps(
        {
            "version": 2,
            "pending_events": [],
            "repairs": {
                "sentinel": {
                    "display_name": "sentinel.mkv",
                    "status": "blocked_recovery",
                }
            },
        }
    ).encode("utf-8")


def test_repair_defaults_to_report_only(monkeypatch):
    monkeypatch.delenv("SUBGEN_REPAIR_ACTION", raising=False)
    monkeypatch.setattr(sys, "argv", ["repair_subgen_failures.py"])

    assert repair_module.parse_args().action == "report"


def test_delete_action_is_accepted_but_effectively_report_only(
    tmp_path, monkeypatch
):
    media_root = tmp_path / "media"
    target = media_root / "show" / "offender.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    candidate = {
        "candidate_id": "legacy-delete-request",
        "display_name": target.name,
        "host_path": str(target),
        "count": 3,
        "failure_identity": list(file_identity(target.stat())),
    }
    unlink_calls = []

    def forbid_secure_unlink(*args, **kwargs):
        unlink_calls.append((args, kwargs))
        pytest.fail("repair must never call secure_unlink_regular_beneath")

    def validate_candidate(root, path, *, expected_identity=None):
        assert root == media_root.resolve()
        assert path == target
        assert expected_identity == candidate["failure_identity"]

    monkeypatch.setattr(
        repair_module,
        "secure_unlink_regular_beneath",
        forbid_secure_unlink,
        raising=False,
    )
    monkeypatch.setattr(
        repair_module,
        "validate_regular_file_beneath",
        validate_candidate,
    )

    with pytest.warns(RuntimeWarning, match="report-only") as caught:
        repairer = run_candidate_once(
            make_args(media_root, tmp_path / "state", action="delete"),
            candidate,
        )

    assert len(caught) == 1
    assert repairer.requested_action == "delete"
    assert repairer.action == "report"
    assert target.read_bytes() == b"media"
    assert repairer.repair_state["legacy-delete-request"]["status"] == "eligible"
    assert unlink_calls == []


@pytest.mark.parametrize("intent_status", ["deleting", "delete_paused"])
def test_repair_policy_blocks_persisted_delete_intent_without_unlinking(
    tmp_path, monkeypatch, intent_status
):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "offender.mkv"
    target.parent.mkdir(parents=True)
    state_dir.mkdir(mode=0o700)
    target.write_bytes(b"media")
    identity = list(file_identity(target.stat()))
    token = new_delete_token()
    evidence = {
        "event_type": "legacy_failure",
        "source_identity": identity,
    }
    unlink_calls = []

    def forbid_secure_unlink(*args, **kwargs):
        unlink_calls.append((args, kwargs))
        pytest.fail("repair recovery must never call secure_unlink_regular_beneath")

    monkeypatch.setattr(
        repair_module,
        "secure_unlink_regular_beneath",
        forbid_secure_unlink,
        raising=False,
    )
    state_path = state_dir / "subgen_repair_state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "updated_utc": "2026-09-01T00:00:00Z",
                "repairs": {
                    "legacy": {
                        "display_name": target.name,
                        "status": intent_status,
                        "host_path": str(target),
                        "delete_identity": identity,
                        "delete_token": token,
                        "delete_event_kind": "DELETED",
                        "delete_intent_utc": "2026-08-31T23:59:00Z",
                        "evidence": evidence,
                    }
                },
                "pending_events": [],
            }
        ),
        encoding="utf-8",
    )

    repairer = Repairer(make_args(media_root, state_dir, action="report"), log_lines=[])
    assert repairer.run() == 0

    result = repairer.repair_state["legacy"]
    persisted_result = json.loads(state_path.read_text(encoding="utf-8"))["repairs"][
        "legacy"
    ]
    assert result["status"] == "blocked_recovery"
    assert result["delete_identity"] == identity
    assert result["delete_token"] == token
    assert result["delete_event_kind"] == "DELETED"
    assert result["delete_intent_utc"] == "2026-08-31T23:59:00Z"
    assert result["evidence"] == evidence
    assert persisted_result["status"] == "blocked_recovery"
    assert persisted_result["delete_identity"] == identity
    assert persisted_result["delete_token"] == token
    assert persisted_result["evidence"] == evidence
    assert target.read_bytes() == b"media"
    assert unlink_calls == []


def test_repair_event_log_limit_defaults_to_five_mib_and_accepts_cli_override(monkeypatch):
    monkeypatch.delenv("SUBGEN_REPAIR_EVENT_LOG_MAX_BYTES", raising=False)
    monkeypatch.setattr(sys, "argv", ["repair_subgen_failures.py"])

    assert repair_module.parse_args().event_log_max_bytes == 5 * 1024 * 1024

    monkeypatch.setenv("SUBGEN_REPAIR_EVENT_LOG_MAX_BYTES", "4096")
    assert repair_module.parse_args().event_log_max_bytes == 4096

    monkeypatch.setattr(
        sys,
        "argv",
        ["repair_subgen_failures.py", "--event-log-max-bytes", "8192"],
    )

    assert repair_module.parse_args().event_log_max_bytes == 8192


def test_repair_rejects_event_log_limit_below_safe_omission_record(tmp_path):
    with pytest.raises(ValueError, match="at least 256"):
        Repairer(
            make_args(
                tmp_path / "media",
                tmp_path / "state",
                event_log_max_bytes=0,
            ),
            log_lines=[],
        )


def test_new_process_suppresses_an_unchanged_candidate_event(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "offender.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    args = make_args(media_root, state_dir, action="report")
    candidate = {
        "candidate_id": "stable-offender",
        "display_name": target.name,
        "host_path": str(target),
        "count": 3,
        "failure_identity": list(file_identity(target.stat())),
    }

    first = run_candidate_once(args, candidate)
    first_events = first.events_path.read_text(encoding="utf-8").splitlines()
    second = run_candidate_once(args, candidate)

    assert len(first_events) == 1
    assert second.events_path.read_text(encoding="utf-8").splitlines() == first_events


@requires_secure_unlink
def test_run_lock_reloads_state_created_after_process_construction(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "offender.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    args = make_args(media_root, state_dir, action="report")
    candidate = {
        "candidate_id": "preconstructed-offender",
        "display_name": target.name,
        "host_path": str(target),
        "count": 3,
        "failure_identity": list(file_identity(target.stat())),
    }
    first = Repairer(args, log_lines=[])
    second = Repairer(args, log_lines=[])
    write_candidates(first, [candidate])

    assert first.run() == 0
    assert second.run() == 0

    events = second.events_path.read_text(encoding="utf-8").splitlines()
    assert sum("[ELIGIBLE]" in event for event in events) == 1


def test_display_name_change_does_not_duplicate_the_same_semantic_result(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "offender.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    args = make_args(media_root, state_dir, action="report")
    candidate = {
        "candidate_id": "stable-offender",
        "display_name": target.name,
        "host_path": str(target),
        "count": 3,
    }
    first = run_candidate_once(args, candidate)
    first_events = first.events_path.read_text(encoding="utf-8").splitlines()

    candidate["display_name"] = "presentation-only-name.mkv"
    second = run_candidate_once(args, candidate)

    assert second.events_path.read_text(encoding="utf-8").splitlines() == first_events
    assert second.repair_state["stable-offender"]["display_name"] == "presentation-only-name.mkv"


def test_legacy_skip_path_metadata_is_preserved_without_duplicate_event(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "offender.mkv"
    legacy_marker = media_root / "show" / "legacy-empty.srt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    args = make_args(media_root, state_dir, action="report")
    candidate = {
        "candidate_id": "stable-offender",
        "display_name": target.name,
        "host_path": str(target),
        "count": 3,
    }
    first = run_candidate_once(args, candidate)
    first_events = first.events_path.read_text(encoding="utf-8").splitlines()
    persisted = json.loads(first.repair_state_path.read_text(encoding="utf-8"))
    persisted["repairs"]["stable-offender"]["skip_path"] = str(legacy_marker)
    first.repair_state_path.write_text(json.dumps(persisted), encoding="utf-8")

    second = run_candidate_once(args, candidate)

    assert second.events_path.read_text(encoding="utf-8").splitlines() == first_events
    assert second.repair_state["stable-offender"]["skip_path"] == str(legacy_marker)


def test_new_process_logs_one_event_when_candidate_semantics_change(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    first_target = media_root / "show" / "first.mkv"
    second_target = media_root / "show" / "second.mkv"
    first_target.parent.mkdir(parents=True)
    first_target.write_bytes(b"first")
    second_target.write_bytes(b"second")
    args = make_args(media_root, state_dir, action="report")
    candidate = {
        "candidate_id": "stable-offender",
        "display_name": first_target.name,
        "host_path": str(first_target),
        "count": 3,
    }

    repairer = run_candidate_once(args, candidate)
    assert len(repairer.events_path.read_text(encoding="utf-8").splitlines()) == 1

    candidate["count"] = 4
    repairer = run_candidate_once(args, candidate)
    assert len(repairer.events_path.read_text(encoding="utf-8").splitlines()) == 2

    candidate["display_name"] = second_target.name
    candidate["host_path"] = str(second_target)
    repairer = run_candidate_once(args, candidate)
    assert len(repairer.events_path.read_text(encoding="utf-8").splitlines()) == 3

    candidate["delete_status"] = "deleted"
    candidate["delete_message"] = "Removed by the monitor."
    repairer = run_candidate_once(args, candidate)
    assert len(repairer.events_path.read_text(encoding="utf-8").splitlines()) == 4

    repairer = run_candidate_once(args, candidate)
    assert len(repairer.events_path.read_text(encoding="utf-8").splitlines()) == 4

    candidate["delete_message"] = "Monitor confirmed exact-path removal."
    repairer = run_candidate_once(args, candidate)
    assert len(repairer.events_path.read_text(encoding="utf-8").splitlines()) == 5


def test_event_log_rotates_to_one_backup_before_crossing_limit(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    repairer = Repairer(
        make_args(media_root, state_dir, event_log_max_bytes=256),
        log_lines=[],
    )
    prior_log = "x" * 240
    repairer.events_path.write_text(prior_log, encoding="utf-8")
    rotated_path = repairer.events_path.with_name(f"{repairer.events_path.name}.1")
    rotated_path.write_text("older rotation", encoding="utf-8")

    repairer.append_event("TEST", "rotated")

    assert rotated_path.read_text(encoding="utf-8") == prior_log
    assert "[TEST] rotated" in repairer.events_path.read_text(encoding="utf-8")
    assert repairer.events_path.stat().st_size <= 256
    assert not repairer.events_path.with_name(f"{repairer.events_path.name}.2").exists()


def test_event_log_refuses_to_follow_a_primary_symlink(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    media_file = media_root / "movie.mkv"
    media_file.write_bytes(b"media")
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    create_symlink_or_skip(repairer.events_path, media_file)

    repairer.append_event("TEST", "must not reach media")

    assert repairer.events_path.is_symlink()
    assert media_file.read_bytes() == b"media"


def test_event_log_refuses_primary_hardlink(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    media_file = media_root / "movie.mkv"
    media_file.write_bytes(b"media")
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    try:
        os.link(media_file, repairer.events_path)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")

    delivered, reason = repairer.append_event("TEST", "must not write media")

    assert delivered is False
    assert reason == "event_log_not_regular"
    assert media_file.read_bytes() == b"media"


def test_event_log_refuses_to_replace_a_rotation_symlink(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    media_file = media_root / "movie.mkv"
    media_file.write_bytes(b"media")
    repairer = Repairer(
        make_args(media_root, state_dir, event_log_max_bytes=256),
        log_lines=[],
    )
    prior_log = "x" * 240
    repairer.events_path.write_text(prior_log, encoding="utf-8")
    rotated_path = repairer.events_path.with_name(f"{repairer.events_path.name}.1")
    create_symlink_or_skip(rotated_path, media_file)

    repairer.append_event("TEST", "must not replace the link")

    assert repairer.events_path.read_text(encoding="utf-8") == prior_log
    assert rotated_path.is_symlink()
    assert media_file.read_bytes() == b"media"


def test_event_larger_than_limit_is_not_written(tmp_path):
    repairer = Repairer(
        make_args(tmp_path / "media", tmp_path / "state", event_log_max_bytes=256),
        log_lines=[],
    )

    repairer.append_event("TEST", "x" * 1000)

    assert not repairer.events_path.exists()


@requires_secure_unlink
def test_failed_event_is_persisted_and_retried_by_the_next_process(
    tmp_path, monkeypatch, capsys
):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "offender.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    args = make_args(media_root, state_dir, action="report")
    candidate = {
        "candidate_id": "pending-offender",
        "display_name": target.name,
        "host_path": str(target),
        "count": 3,
    }
    first = Repairer(args, log_lines=[])
    write_candidates(first, [candidate])
    monkeypatch.setattr(first, "append_event", lambda *_args: (False, "simulated"))

    assert first.run() == 0

    persisted = json.loads(first.repair_state_path.read_text(encoding="utf-8"))
    assert len(persisted["pending_events"]) == 1
    assert persisted["pending_events"][0]["kind"] == "ELIGIBLE"
    assert "offender.mkv" not in capsys.readouterr().err

    second = run_candidate_once(args, candidate)
    persisted = json.loads(second.repair_state_path.read_text(encoding="utf-8"))
    events = second.events_path.read_text(encoding="utf-8").splitlines()

    assert persisted["pending_events"] == []
    assert len(events) == 1
    assert "[ELIGIBLE]" in events[0]


@requires_secure_unlink
def test_eligible_event_survives_log_failure_without_reprocessing_unchanged_candidate(
    tmp_path, monkeypatch
):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "offender.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    args = make_args(media_root, state_dir)
    candidate = {
        "candidate_id": "deleted-offender",
        "display_name": target.name,
        "host_path": str(target),
        "count": 3,
        "failure_identity": list(file_identity(target.stat())),
    }
    first = Repairer(args, log_lines=[])
    write_candidates(first, [candidate])
    monkeypatch.setattr(first, "append_event", lambda *_args: (False, "simulated"))

    assert first.run() == 0
    assert target.read_bytes() == b"media"
    persisted = json.loads(first.repair_state_path.read_text(encoding="utf-8"))
    assert persisted["pending_events"][0]["kind"] == "ELIGIBLE"

    second = run_candidate_once(args, candidate)
    events = second.events_path.read_text(encoding="utf-8").splitlines()

    assert "[ELIGIBLE]" in events[0]
    assert not any("[MISSING]" in event for event in events[1:])
    assert target.read_bytes() == b"media"
    assert json.loads(second.repair_state_path.read_text(encoding="utf-8"))[
        "pending_events"
    ] == []


def test_atomic_repair_state_write_keeps_previous_file_on_replace_failure(
    tmp_path, monkeypatch
):
    repairer = Repairer(
        make_args(tmp_path / "media", tmp_path / "state"),
        log_lines=[],
    )
    previous = '{"repairs": {"sentinel": {"display_name": "kept"}}}\n'
    repairer.repair_state_path.write_text(previous, encoding="utf-8")
    repairer.repair_state = {
        "replacement": {"display_name": "new", "status": "eligible"}
    }

    def fail_replace(*_args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(repair_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        repairer.save_repair_state()

    assert repairer.repair_state_path.read_text(encoding="utf-8") == previous
    assert list(repairer.state_dir.glob(".subgen_repair_state.json.*.tmp")) == []


def test_malformed_repair_state_is_never_overwritten(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    state_path = state_dir / "subgen_repair_state.json"
    original = b"not-json"
    state_path.write_bytes(original)

    with pytest.warns(RuntimeWarning, match="will not be overwritten"):
        repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    with pytest.warns(RuntimeWarning, match="will not be overwritten"):
        assert repairer.run() == 1

    assert state_path.read_bytes() == original


@pytest.mark.parametrize(
    "original",
    [
        b'{"repairs":[]}',
        b'{"repairs":null}',
        b'{"pending_events":{}}',
        b'{"pending_events":null}',
        b'{"version":999,"repairs":{}}',
        b'{"version":true,"repairs":{}}',
        b'{"version":2.0,"repairs":{}}',
        b'{"repairs":{"intent":{"status":"deleting"}}}',
    ],
)
def test_schema_invalid_repair_state_is_never_overwritten(tmp_path, original):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    state_path = state_dir / "subgen_repair_state.json"
    state_path.write_bytes(original)

    with pytest.warns(RuntimeWarning, match="will not be overwritten"):
        repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    with pytest.warns(RuntimeWarning, match="will not be overwritten"):
        assert repairer.run() == 1

    assert state_path.read_bytes() == original


def test_oversized_repair_state_is_never_overwritten(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    state_path = state_dir / "subgen_repair_state.json"
    original = valid_repair_state_bytes()
    monkeypatch.setattr(repair_module, "MAX_REPAIR_STATE_BYTES", len(original) - 1)
    state_path.write_bytes(original)

    with pytest.warns(RuntimeWarning, match="will not be overwritten"):
        repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    with pytest.warns(RuntimeWarning, match="will not be overwritten"):
        assert repairer.run() == 1

    assert state_path.read_bytes() == original


def test_repair_state_symlink_is_not_followed_or_overwritten(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    media_file = media_root / "movie.mkv"
    original = valid_repair_state_bytes()
    media_file.write_bytes(original)
    state_path = state_dir / "subgen_repair_state.json"
    create_symlink_or_skip(state_path, media_file)

    with pytest.warns(RuntimeWarning, match="will not be overwritten"):
        repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    with pytest.warns(RuntimeWarning, match="will not be overwritten"):
        assert repairer.run() == 1

    assert state_path.is_symlink()
    assert media_file.read_bytes() == original


def test_private_state_reader_rejects_symlink_without_o_nofollow(
    tmp_path, monkeypatch
):
    target = tmp_path / "target.json"
    link = tmp_path / "state.json"
    target.write_bytes(valid_repair_state_bytes())
    create_symlink_or_skip(link, target)
    monkeypatch.setattr(repair_module.os, "O_NOFOLLOW", 0, raising=False)

    with pytest.raises(repair_module.UnsafePathError):
        repair_module.read_private_text(link, maximum_bytes=4096)


def test_repair_state_hardlink_is_not_overwritten(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    media_file = media_root / "movie.mkv"
    original = valid_repair_state_bytes()
    media_file.write_bytes(original)
    state_path = state_dir / "subgen_repair_state.json"
    try:
        os.link(media_file, state_path)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")

    with pytest.warns(RuntimeWarning, match="will not be overwritten"):
        repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    with pytest.warns(RuntimeWarning, match="will not be overwritten"):
        assert repairer.run() == 1

    assert state_path.samefile(media_file)
    assert media_file.read_bytes() == original


def test_repair_leaves_media_and_intent_when_policy_block_cannot_persist(
    tmp_path, monkeypatch
):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "offender.mkv"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    target.write_bytes(b"media")
    identity = list(file_identity(target.stat()))
    token = new_delete_token()
    state_path = state_dir / "subgen_repair_state.json"
    state_path.write_text(
        json.dumps(
            {
                "repairs": {
                    "intent": {
                        "display_name": target.name,
                        "status": "deleting",
                        "host_path": str(target),
                        "delete_identity": identity,
                        "delete_token": token,
                        "evidence": {"source": "legacy-repair"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])

    def fail_save():
        raise OSError("simulated policy-block write failure")

    monkeypatch.setattr(repairer, "save_repair_state", fail_save)

    with pytest.raises(OSError, match="policy-block write failure"):
        repairer.run()

    assert target.read_bytes() == b"media"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["repairs"]["intent"]
    assert persisted["status"] == "deleting"
    assert persisted["delete_identity"] == identity
    assert persisted["delete_token"] == token


@pytest.mark.parametrize("intent_status", ["deleting", "delete_paused"])
def test_repair_blocks_each_legacy_delete_intent_once(tmp_path, intent_status):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "offender.mkv"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    target.write_bytes(b"media")
    state_path = state_dir / "subgen_repair_state.json"
    state_path.write_text(
        json.dumps(
            {
                "repairs": {
                    "intent-recovery": {
                        "display_name": target.name,
                        "status": intent_status,
                        "host_path": str(target),
                        "delete_identity": list(file_identity(target.stat())),
                        "delete_token": new_delete_token(),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    args = make_args(media_root, state_dir)

    first = Repairer(args, log_lines=[])
    assert first.run() == 0
    second = Repairer(args, log_lines=[])
    assert second.run() == 0

    events = second.events_path.read_text(encoding="utf-8").splitlines()
    assert sum("[DELETE_RECOVERY_BLOCKED]" in event for event in events) == 1
    assert second.repair_state["intent-recovery"]["status"] == "blocked_recovery"
    assert target.read_bytes() == b"media"


@pytest.mark.parametrize("action", ["report", "delete"])
def test_repair_recovery_is_policy_blocked_for_every_requested_action(
    tmp_path, action
):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    state_dir.mkdir(mode=0o700)
    target = media_root / "offender.mkv"
    target.write_bytes(b"media")
    identity = list(file_identity(target.stat()))
    token = new_delete_token()
    state_path = state_dir / "subgen_repair_state.json"
    state_path.write_text(
        json.dumps(
            {
                "repairs": {
                    "paused-intent": {
                        "display_name": target.name,
                        "status": "deleting",
                        "host_path": str(target),
                        "delete_identity": identity,
                        "delete_token": token,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    if action == "delete":
        with pytest.warns(RuntimeWarning, match="report-only"):
            repairer = Repairer(
                make_args(media_root, state_dir, action=action),
                log_lines=[],
            )
    else:
        repairer = Repairer(
            make_args(media_root, state_dir, action=action),
            log_lines=[],
        )

    assert repairer.run() == 0

    assert target.read_bytes() == b"media"
    result = repairer.repair_state["paused-intent"]
    assert result["status"] == "blocked_recovery"
    assert result["delete_identity"] == identity
    assert result["delete_token"] == token


@requires_secure_unlink
def test_repair_does_not_delete_replacement_for_stale_monitor_evidence(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "offender.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old offender")
    candidate = {
        "candidate_id": "stale-evidence",
        "display_name": target.name,
        "host_path": str(target),
        "count": 3,
        "first_seen_utc": "2026-01-01T00:00:00Z",
        "last_seen_utc": "2026-01-01T00:00:00Z",
        "failure_identity": list(file_identity(target.stat())),
    }
    args = make_args(media_root, state_dir)
    first = Repairer(args, log_lines=[])
    write_candidates(first, [candidate])
    assert first.run() == 0
    assert target.read_bytes() == b"old offender"
    assert first.repair_state["stale-evidence"]["status"] == "eligible"

    target.unlink()
    target.write_bytes(b"fixed replacement")
    second = Repairer(args, log_lines=[])
    assert second.run() == 0

    assert target.read_bytes() == b"fixed replacement"
    assert second.repair_state["stale-evidence"]["status"] == "blocked"


def test_repair_does_not_overwrite_recovery_owned_intent_with_candidate_pass(
    tmp_path,
):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    target = media_root / "offender.mkv"
    target.write_bytes(b"replacement")
    token = new_delete_token()
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    repairer.repair_state_path.write_text(
        json.dumps(
            {
                "repairs": {
                    "recovery-owned": {
                        "display_name": target.name,
                        "status": "blocked_recovery",
                        "host_path": str(target),
                        "delete_identity": [1, 2, 3, 4, 5],
                        "delete_token": token,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    write_candidates(
        repairer,
        [
            {
                "candidate_id": "recovery-owned",
                "display_name": target.name,
                "host_path": str(target),
                "count": 4,
                "failure_identity": list(file_identity(target.stat())),
            }
        ],
    )

    assert repairer.run() == 0

    retained = repairer.repair_state["recovery-owned"]
    assert retained["status"] == "blocked_recovery"
    assert retained["delete_token"] == token


def test_repair_blocks_and_audits_incomplete_persisted_delete_intent(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    repairer.repair_state_path.write_text(
        json.dumps(
            {
                "repairs": {
                    "incomplete-intent": {
                        "display_name": "offender.mkv",
                        "status": "deleting",
                        "host_path": str(media_root / "offender.mkv"),
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert repairer.run() == 0

    assert repairer.repair_state["incomplete-intent"]["status"] == "blocked_recovery"
    assert "[DELETE_RECOVERY_BLOCKED]" in repairer.events_path.read_text(
        encoding="utf-8"
    )


def test_oversized_pending_event_does_not_starve_later_deliverable_event(tmp_path):
    repairer = Repairer(
        make_args(tmp_path / "media", tmp_path / "state", event_log_max_bytes=256),
        log_lines=[],
    )
    repairer.pending_events = [
        {
            "kind": "OVERSIZED",
            "message": "x" * 1000,
            "signature": "oversized",
            "queued_utc": "2000-01-01T00:00:00Z",
        },
        {
            "kind": "SMALL",
            "message": "deliver me",
            "signature": "small",
            "queued_utc": "2001-01-01T00:00:00Z",
        },
    ]
    repairer.save_repair_state()

    repairer.flush_pending_events()

    events = repairer.events_path.read_text(encoding="utf-8").splitlines()
    assert any(event.startswith("2001-01-01T00:00:00Z [SMALL]") for event in events)


def test_transient_pending_head_failure_preserves_fifo_order(tmp_path, monkeypatch):
    repairer = Repairer(
        make_args(tmp_path / "media", tmp_path / "state"),
        log_lines=[],
    )
    repairer.pending_events = [
        {
            "kind": "FIRST",
            "message": "one",
            "signature": "first",
            "queued_utc": "2000-01-01T00:00:00Z",
        },
        {
            "kind": "SECOND",
            "message": "two",
            "signature": "second",
            "queued_utc": "2001-01-01T00:00:00Z",
        },
    ]
    attempts = []

    def fail_head(kind, message, event_utc=None):
        attempts.append((kind, message, event_utc))
        return False, "event_log_lock_failed"

    monkeypatch.setattr(repairer, "append_event", fail_head)

    repairer.flush_pending_events()

    assert [attempt[0] for attempt in attempts] == ["FIRST"]
    assert [event["kind"] for event in repairer.pending_events] == ["FIRST", "SECOND"]


def test_flat_legacy_repair_state_still_loads(tmp_path):
    repairer = Repairer(
        make_args(tmp_path / "media", tmp_path / "state"),
        log_lines=[],
    )
    repairer.repair_state_path.write_text(
        json.dumps(
            {
                "legacy-key": {
                    "display_name": "legacy.mkv",
                    "status": "eligible",
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = Repairer(
        make_args(tmp_path / "media", tmp_path / "state"),
        log_lines=[],
    )

    assert loaded.repair_state["legacy-key"]["display_name"] == "legacy.mkv"


@pytest.mark.parametrize("path_source", ["host", "container"])
def test_repair_refuses_symlink_candidate_without_deleting_its_target(
    tmp_path, path_source
):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "target.mkv"
    link = media_root / "show" / "offender.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    create_symlink_or_skip(link, target)
    candidate = {
        "candidate_id": f"{path_source}-symlink",
        "display_name": link.name,
        "count": 3,
    }
    if path_source == "host":
        candidate["host_path"] = str(link)
    else:
        candidate["container_path"] = "/media/show/offender.mkv"
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    write_candidates(repairer, [candidate])

    assert repairer.run() == 0

    assert link.is_symlink()
    assert target.read_bytes() == b"media"
    assert repairer.repair_state[f"{path_source}-symlink"]["status"] == "blocked"


def test_repair_blocks_container_path_through_parent_symlink_outside_media_root(
    tmp_path,
):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "offender.mkv"
    target.write_bytes(b"outside")
    media_root.mkdir()
    create_symlink_or_skip(media_root / "linked", outside)
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    write_candidates(
        repairer,
        [
            {
                "candidate_id": "parent-symlink",
                "display_name": target.name,
                "container_path": "/media/linked/offender.mkv",
                "count": 3,
            }
        ],
    )

    assert repairer.run() == 0

    assert target.read_bytes() == b"outside"
    assert repairer.repair_state["parent-symlink"]["status"] == "blocked"


def test_repair_rejects_lexical_parent_traversal_from_container_path(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    write_candidates(
        repairer,
        [
            {
                "candidate_id": "parent-traversal",
                "display_name": outside.name,
                "container_path": "/media/../outside.mkv",
                "count": 3,
            }
        ],
    )

    assert repairer.run() == 0

    assert outside.read_bytes() == b"outside"
    assert repairer.repair_state["parent-traversal"]["status"] == "unresolved"


def test_repair_blocks_lexical_parent_traversal_in_persisted_host_path(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    (media_root / "library").mkdir(parents=True)
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")
    lexical_escape = media_root / "library" / ".." / ".." / outside.name
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    write_candidates(
        repairer,
        [
            {
                "candidate_id": "host-parent-traversal",
                "display_name": outside.name,
                "host_path": str(lexical_escape),
                "count": 3,
            }
        ],
    )

    assert repairer.run() == 0

    assert outside.read_bytes() == b"outside"
    assert repairer.repair_state["host-parent-traversal"]["status"] == "blocked"


def test_repair_rejects_parent_traversal_even_when_it_stays_inside_media_root(
    tmp_path,
):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    target = media_root / "offender.mkv"
    target.write_bytes(b"media")
    lexical_traversal = f"{media_root}/unused/../{target.name}"
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    write_candidates(
        repairer,
        [
            {
                "candidate_id": "host-in-root-traversal",
                "display_name": target.name,
                "host_path": lexical_traversal,
                "count": 3,
            }
        ],
    )

    assert repairer.run() == 0

    assert target.read_bytes() == b"media"
    assert repairer.repair_state["host-in-root-traversal"]["status"] == "blocked"


@requires_secure_unlink
def test_repair_reports_exact_repeated_offender_without_fake_subtitle(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "offender.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    write_candidates(
        repairer,
        [{
            "candidate_id": str(target).lower(),
            "display_name": target.name,
            "container_path": "/media/show/offender.mkv",
            "host_path": str(target),
            "count": 3,
            "delete_status": None,
            "failure_identity": list(file_identity(target.stat())),
        }],
    )

    assert repairer.run() == 0

    assert target.read_bytes() == b"media"
    assert not list(target.parent.glob("*.srt"))
    result = next(iter(repairer.repair_state.values()))
    assert result["status"] == "eligible"


@requires_secure_unlink
@pytest.mark.parametrize("action", ["report", "delete"])
def test_repair_retains_legacy_empty_skip_marker_with_offender(tmp_path, action):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "offender.mkv"
    marker = media_root / "show" / "offender.subgen.large-v3-turbo.en.srt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    marker.touch()
    args = make_args(media_root, state_dir, action=action)
    if action == "delete":
        with pytest.warns(RuntimeWarning, match="report-only"):
            repairer = Repairer(args, log_lines=[])
    else:
        repairer = Repairer(args, log_lines=[])
    write_candidates(
        repairer,
        [{
            "display_name": target.name,
            "host_path": str(target),
            "count": 3,
            "failure_identity": list(file_identity(target.stat())),
        }],
    )

    repairer.run()

    assert target.read_bytes() == b"media"
    assert marker.exists()
    assert marker.stat().st_size == 0
    result = next(iter(repairer.repair_state.values()))
    assert result["status"] == "eligible"


def test_repair_keeps_candidate_below_threshold(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "offender.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    write_candidates(
        repairer,
        [{"display_name": target.name, "host_path": str(target), "count": 2}],
    )

    repairer.run()

    assert target.exists()
    assert not list(target.parent.glob("*.srt"))


@requires_secure_unlink
def test_report_action_never_deletes_eligible_candidate(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "offender.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    repairer = Repairer(make_args(media_root, state_dir, action="report"), log_lines=[])
    write_candidates(
        repairer,
        [{"display_name": target.name, "host_path": str(target), "count": 3}],
    )

    repairer.run()

    assert target.exists()
    result = next(iter(repairer.repair_state.values()))
    assert result["status"] == "eligible"


def test_repair_blocks_state_path_outside_media_root(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    media_root.mkdir()
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    write_candidates(
        repairer,
        [{"display_name": outside.name, "host_path": str(outside), "count": 3}],
    )

    repairer.run()

    assert outside.exists()
    assert not (tmp_path / "outside.subgen.large-v3.en.srt").exists()
    result = next(iter(repairer.repair_state.values()))
    assert result["status"] == "blocked"


def test_repair_never_deletes_a_directory(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show"
    target.mkdir(parents=True)
    (target / "episode.mkv").write_bytes(b"media")
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    write_candidates(
        repairer,
        [{"display_name": target.name, "host_path": str(target), "count": 3}],
    )

    repairer.run()

    assert target.exists()
    result = next(iter(repairer.repair_state.values()))
    assert result["status"] == "blocked"
