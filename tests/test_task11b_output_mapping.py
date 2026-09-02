import os
import posixpath
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from language_code import LanguageCode
from subgen_core import media, runtime_receipts, transcription


TOKEN = "1" * 64
PHASE_A = "a" * 64
PHASE_B = "b" * 64


class GateFilesystem:
    def __init__(self):
        self.items = {}
        self.resolved = {}
        self.path = SimpleNamespace(
            basename=posixpath.basename,
            dirname=posixpath.dirname,
            realpath=lambda path: self.resolved.get(path, path),
            splitext=posixpath.splitext,
        )

    def add_directory(self, path):
        self.items[path] = stat.S_IFDIR | 0o700

    def add_file(self, path):
        self.items[path] = stat.S_IFREG | 0o400

    def add_symlink(self, path, target):
        self.items[path] = stat.S_IFLNK | 0o777
        self.resolved[path] = target

    def lstat(self, path):
        try:
            mode = self.items[path]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc
        return SimpleNamespace(st_mode=mode)


class ChangedStagingIdentityOs:
    path = os.path

    def __getattr__(self, name):
        return getattr(os, name)

    def lstat(self, path):
        item = os.lstat(path)
        name = os.path.basename(path)
        if name.startswith(".movie.en.srt.") and ".tmp" in name:
            return SimpleNamespace(
                st_mode=item.st_mode,
                st_dev=item.st_dev,
                st_ino=item.st_ino + 1,
                st_nlink=item.st_nlink,
            )
        return item


def gate_config(tmp_path):
    return runtime_receipts.GateReceiptConfig(
        receipt_file=(tmp_path / "receipts.jsonl").resolve(),
        gate_token_sha256=TOKEN,
        phase_a_workload_sha256=PHASE_A,
        phase_b_workload_sha256=PHASE_B,
    )


def phase_filesystem(phase, relative="movie.mkv"):
    filesystem = GateFilesystem()
    input_root = f"/fixtures/phase-{phase}"
    output_root = f"/task11b-output/phase-{phase}"
    filesystem.add_directory(input_root)
    filesystem.add_directory(output_root)
    relative_parent = posixpath.dirname(relative)
    if relative_parent:
        filesystem.add_directory(posixpath.join(input_root, relative_parent))
        filesystem.add_directory(posixpath.join(output_root, relative_parent))
    filesystem.add_file(posixpath.join(input_root, relative))
    return filesystem


@pytest.mark.parametrize("phase", ("a", "b"))
def test_gate_maps_only_the_exact_phase_fixture_to_its_shadow_root(tmp_path, phase):
    config = gate_config(tmp_path)
    filesystem = phase_filesystem(phase, "show/movie.mkv")

    assert (
        config.map_output_media_path(
            f"/fixtures/phase-{phase}/show/movie.mkv",
            filesystem=filesystem,
        )
        == f"/task11b-output/phase-{phase}/show/movie.mkv"
    )


def test_disabled_gate_preserves_even_unusual_public_paths_without_filesystem_access():
    filesystem = GateFilesystem()

    assert (
        runtime_receipts.GateReceiptConfig().map_output_media_path(
            "relative/../public movie.mkv",
            filesystem=filesystem,
        )
        == "relative/../public movie.mkv"
    )


@pytest.mark.parametrize(
    "path",
    (
        "/fixtures/phase-a",
        "/fixtures/phase-a/../phase-b/movie.mkv",
        "/fixtures/phase-a//movie.mkv",
        "/fixtures/phase-c/movie.mkv",
        "/media/movie.mkv",
        "/fixtures/phase-a/movie\\name.mkv",
        "/fixtures/phase-a/movie\nname.mkv",
    ),
)
def test_gate_rejects_non_exact_or_ambiguous_input_paths(tmp_path, path):
    with pytest.raises(runtime_receipts.RuntimeReceiptError, match="Task 11B"):
        gate_config(tmp_path).map_output_media_path(
            path,
            filesystem=phase_filesystem("a"),
        )


@pytest.mark.parametrize("ambiguous", ("input_root", "media", "output_root"))
def test_gate_rejects_symlink_ambiguity_on_either_side(tmp_path, ambiguous):
    filesystem = phase_filesystem("a")
    paths = {
        "input_root": "/fixtures/phase-a",
        "media": "/fixtures/phase-a/movie.mkv",
        "output_root": "/task11b-output/phase-a",
    }
    filesystem.add_symlink(paths[ambiguous], "/outside")

    with pytest.raises(runtime_receipts.RuntimeReceiptError, match="not one real"):
        gate_config(tmp_path).map_output_media_path(
            "/fixtures/phase-a/movie.mkv",
            filesystem=filesystem,
        )


def test_gate_subtitle_name_is_deterministic_under_the_phase_output_root(tmp_path):
    config = gate_config(tmp_path)
    filesystem = phase_filesystem("a")
    runtime = SimpleNamespace(
        os=filesystem,
        task11b_gate_config=config,
        show_in_subname_subgen=False,
        show_in_subname_model=False,
        whisper_model="large-v3",
        subtitle_language_name="en",
        transcribe_or_translate="transcribe",
        subtitle_language_naming_type="ISO_639_1",
    )
    runtime.define_subtitle_language_naming = lambda _language, _kind: "en"

    assert (
        media.name_subtitle(
            runtime,
            "/fixtures/phase-a/movie.mkv",
            LanguageCode.ENGLISH,
        )
        == "/task11b-output/phase-a/movie.en.srt"
    )


def test_gate_rejects_a_preexisting_final_artifact(tmp_path):
    filesystem = phase_filesystem("a")
    filesystem.add_file("/task11b-output/phase-a/movie.en.srt")

    with pytest.raises(runtime_receipts.RuntimeReceiptError, match="already existed"):
        gate_config(tmp_path).validate_output_artifact_path(
            "/task11b-output/phase-a/movie.en.srt",
            filesystem=filesystem,
        )


@pytest.mark.parametrize(
    "publisher",
    (transcription._publish_segmented_result, transcription._publish_legacy_result),
)
def test_segmented_and_opt_out_publication_use_the_same_mapped_atomic_target(
    monkeypatch,
    tmp_path,
    publisher,
):
    config = gate_config(tmp_path)
    filesystem = phase_filesystem("a")
    atomic_targets = []
    runtime = SimpleNamespace(
        os=filesystem,
        task11b_gate_config=config,
        show_in_subname_subgen=False,
        show_in_subname_model=False,
        whisper_model="large-v3",
        subtitle_language_name="en",
        transcribe_or_translate="transcribe",
        subtitle_language_naming_type="ISO_639_1",
        lrc_for_audio_files=False,
        task_results={},
        task_results_lock=MagicMock(),
        word_level_highlight=False,
        send_completion_webhook=MagicMock(),
    )
    runtime.define_subtitle_language_naming = lambda _language, _kind: "en"
    runtime.name_subtitle = lambda path, language: media.name_subtitle(
        runtime, path, language
    )
    monkeypatch.setattr(
        transcription,
        "_atomic_publish",
        lambda _runtime, path, _writer, **_options: (
            atomic_targets.append(path) or "subtitle"
        ),
    )

    publisher(
        runtime,
        SimpleNamespace(to_srt_vtt=MagicMock()),
        "/fixtures/phase-a/movie.mkv",
        "/task11b-output/phase-a/movie",
        "transcribe",
        LanguageCode.ENGLISH,
        False,
    )

    assert atomic_targets == ["/task11b-output/phase-a/movie.en.srt"]


@pytest.mark.skipif(os.name == "nt", reason="Task 11B gate publication is POSIX-only")
def test_atomic_publish_revalidates_gate_target_before_staging_and_replace(tmp_path):
    checks = []
    config = SimpleNamespace(
        enabled=True,
        validate_output_artifact_path=lambda path, filesystem: checks.append(path),
    )
    runtime = SimpleNamespace(os=os, task11b_gate_config=config, logging=MagicMock())
    output = tmp_path / "movie.en.srt"

    transcription._atomic_publish(
        runtime,
        str(output),
        lambda temporary: Path(temporary).write_text("subtitle", encoding="utf-8"),
    )

    assert output.read_text(encoding="utf-8") == "subtitle"
    assert checks == [str(output), str(output)]


def test_gate_atomic_publish_does_not_replace_target_created_during_race(tmp_path):
    output = tmp_path / "movie.en.srt"
    checks = []

    def validate(path, filesystem):
        checks.append(path)
        if len(checks) == 2:
            output.write_text("racer", encoding="utf-8")

    runtime = SimpleNamespace(
        os=os,
        task11b_gate_config=SimpleNamespace(
            enabled=True,
            validate_output_artifact_path=validate,
        ),
        logging=MagicMock(),
    )

    with pytest.raises(runtime_receipts.RuntimeReceiptError, match="no-replace"):
        transcription._atomic_publish(
            runtime,
            str(output),
            lambda temporary: Path(temporary).write_text(
                "subtitle",
                encoding="utf-8",
            ),
        )

    assert output.read_text(encoding="utf-8") == "racer"
    assert checks == [str(output), str(output)]


def test_gate_atomic_publish_rejects_changed_staging_inode(tmp_path):
    output = tmp_path / "movie.en.srt"
    runtime = SimpleNamespace(
        os=ChangedStagingIdentityOs(),
        task11b_gate_config=SimpleNamespace(
            enabled=True,
            validate_output_artifact_path=lambda _path, filesystem: None,
        ),
        logging=MagicMock(),
    )

    with pytest.raises(runtime_receipts.RuntimeReceiptError, match="staging inode"):
        transcription._atomic_publish(
            runtime,
            str(output),
            lambda temporary: Path(temporary).write_text(
                "subtitle",
                encoding="utf-8",
            ),
        )

    assert not output.exists()


def test_public_atomic_publish_retains_replace_existing_semantics(tmp_path):
    output = tmp_path / "movie.en.srt"
    output.write_text("old", encoding="utf-8")
    runtime = SimpleNamespace(
        os=os,
        task11b_gate_config=SimpleNamespace(
            enabled=False,
            validate_output_artifact_path=lambda _path, filesystem: None,
        ),
        logging=MagicMock(),
    )

    transcription._atomic_publish(
        runtime,
        str(output),
        lambda temporary: Path(temporary).write_text("new", encoding="utf-8"),
    )

    assert output.read_text(encoding="utf-8") == "new"


def test_queue_keeps_the_exact_read_only_fixture_input_path():
    queued = []
    track = media.AudioTrack(index=1, language=LanguageCode.ENGLISH, default=True)
    evidence = media.ValidatorEvidence(
        outcome=media.ValidatorOutcome.AUDIO_PRESENT,
        duration_seconds=60.0,
        audio_tracks=(track,),
    )
    validation = media.MediaValidation(
        outcome=media.MediaOutcome.VALID_AUDIO,
        ffprobe=evidence,
        pyav=evidence,
        duration_seconds=60.0,
        audio_tracks=(track,),
    )
    runtime = SimpleNamespace(
        task_queue=SimpleNamespace(is_active=lambda _path: False, put=queued.append),
        logging=MagicMock(),
        os=SimpleNamespace(path=SimpleNamespace(basename=posixpath.basename)),
        skip_marked_failed_files=False,
        validate_media=lambda _path: validation,
        choose_transcribe_language=lambda _path, language, audio_tracks: language,
        select_audio_track=media.select_audio_track,
        should_skip_file=lambda *_args, **_kwargs: False,
        should_whisper_detect_audio_language=False,
        force_detected_language_to=None,
    )
    source = "/fixtures/phase-a/movie.mkv"

    media.gen_subtitles_queue(
        runtime,
        source,
        "transcribe",
        LanguageCode.ENGLISH,
    )

    assert len(queued) == 1
    assert queued[0]["path"] == source
