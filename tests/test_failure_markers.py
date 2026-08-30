import json
import os
from pathlib import Path

import pytest

import subgen_failure_markers as markers
from subgen_failure_markers import (
    FailureMarkerReader,
    MarkerRegistryError,
    build_marker_entry,
    encode_marker_document,
    load_marker_document,
)
from subgen_ops_safety import file_identity


STAMP = "2026-08-30T12:00:00Z"


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
