import json
import sys
from pathlib import Path
from types import SimpleNamespace

import repair_subgen_failures as repair_module
from repair_subgen_failures import Repairer


def make_args(
    media_root: Path,
    state_dir: Path,
    *,
    min_crash_count: int = 3,
    action: str = "delete",
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
    )


def write_candidates(repairer: Repairer, candidates: list[dict]):
    repairer.monitor_state_path.write_text(
        json.dumps({"crash_candidates": candidates}),
        encoding="utf-8",
    )


def test_repair_defaults_to_report_only(monkeypatch):
    monkeypatch.delenv("SUBGEN_REPAIR_ACTION", raising=False)
    monkeypatch.setattr(sys, "argv", ["repair_subgen_failures.py"])

    assert repair_module.parse_args().action == "report"


def test_repair_deletes_exact_repeated_offender_without_fake_subtitle(tmp_path):
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
        }],
    )

    assert repairer.run() == 0

    assert not target.exists()
    assert not list(target.parent.glob("*.srt"))
    result = next(iter(repairer.repair_state.values()))
    assert result["status"] == "deleted"


def test_repair_removes_legacy_empty_skip_marker_with_offender(tmp_path):
    media_root = tmp_path / "media"
    state_dir = tmp_path / "state"
    target = media_root / "show" / "offender.mkv"
    marker = media_root / "show" / "offender.subgen.large-v3-turbo.en.srt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    marker.touch()
    repairer = Repairer(make_args(media_root, state_dir), log_lines=[])
    write_candidates(
        repairer,
        [{"display_name": target.name, "host_path": str(target), "count": 3}],
    )

    repairer.run()

    assert not target.exists()
    assert not marker.exists()


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
