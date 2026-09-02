"""Contracts for conservative, generation-bound media admission."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from enum import IntFlag
from itertools import product
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from language_code import LanguageCode
from subgen_core import media, resource_management, transcription
from subgen_core.media import (
    AudioTrack,
    MediaOutcome,
    MediaValidation,
    ValidatorEvidence,
    ValidatorOutcome,
    aggregate_validator_outcomes,
)
from subgen_ops_safety import FileIdentity

SOURCE_IDENTITY = FileIdentity(1, 2, 3, 4, 5)
REPLACEMENT_IDENTITY = FileIdentity(1, 8, 13, 21, 34)
TRACKS = (
    AudioTrack(
        index=7,
        codec="aac",
        channels=2,
        language=LanguageCode.SPANISH,
        title="Main",
        default=True,
        original=True,
    ),
)
TASK_TRACKS = tuple(track.as_task_dict() for track in TRACKS)


def _snapshot(identity: FileIdentity):
    return media._SourceSnapshot(identity=identity, mode=0, link_count=1)


def _evidence(
    outcome: ValidatorOutcome,
    *,
    duration_seconds: float | None = None,
    audio_tracks=(),
    detail_code: str | None = None,
) -> ValidatorEvidence:
    return ValidatorEvidence(
        outcome,
        duration_seconds=duration_seconds,
        audio_tracks=audio_tracks,
        detail_code=detail_code,
    )


def _validation(
    outcome: MediaOutcome,
    *,
    source_identity: FileIdentity | None = SOURCE_IDENTITY,
    duration_seconds: float | None = None,
    audio_tracks=(),
    ffprobe: ValidatorOutcome | None = None,
    pyav: ValidatorOutcome | None = None,
) -> MediaValidation:
    if ffprobe is None:
        ffprobe = {
            MediaOutcome.VALID_AUDIO: ValidatorOutcome.AUDIO_PRESENT,
            MediaOutcome.NO_AUDIO: ValidatorOutcome.NO_AUDIO,
            MediaOutcome.INVALID_MEDIA: ValidatorOutcome.INVALID_FORMAT,
            MediaOutcome.PROBE_INDETERMINATE: ValidatorOutcome.INDETERMINATE,
        }[outcome]
    if pyav is None:
        pyav = ffprobe
    return MediaValidation(
        outcome,
        _evidence(
            ffprobe,
            duration_seconds=duration_seconds,
            audio_tracks=audio_tracks,
        ),
        _evidence(pyav),
        source_identity=source_identity,
        duration_seconds=duration_seconds,
        audio_tracks=audio_tracks,
    )


def _expected_media_outcome(
    ffprobe: ValidatorOutcome,
    pyav: ValidatorOutcome,
) -> MediaOutcome:
    if ffprobe is pyav is ValidatorOutcome.INVALID_FORMAT:
        return MediaOutcome.INVALID_MEDIA
    if ValidatorOutcome.AUDIO_PRESENT in (ffprobe, pyav):
        return MediaOutcome.VALID_AUDIO
    if ValidatorOutcome.NO_AUDIO in (ffprobe, pyav):
        return MediaOutcome.NO_AUDIO
    return MediaOutcome.PROBE_INDETERMINATE


VALIDATOR_PAIRS = tuple(product(tuple(ValidatorOutcome), repeat=2))


@pytest.mark.parametrize(
    ("ffprobe", "pyav"),
    VALIDATOR_PAIRS,
    ids=lambda value: value.value,
)
def test_complete_ordered_validator_truth_table(ffprobe, pyav):
    assert aggregate_validator_outcomes(ffprobe, pyav) is _expected_media_outcome(
        ffprobe,
        pyav,
    )


def test_silent_container_wins_over_one_invalid_parser():
    assert (
        aggregate_validator_outcomes(
            ValidatorOutcome.NO_AUDIO,
            ValidatorOutcome.INVALID_FORMAT,
        )
        is MediaOutcome.NO_AUDIO
    )
    assert (
        aggregate_validator_outcomes(
            ValidatorOutcome.INVALID_FORMAT,
            ValidatorOutcome.NO_AUDIO,
        )
        is MediaOutcome.NO_AUDIO
    )


def test_validate_media_hands_off_ffprobe_duration_and_tracks(monkeypatch):
    calls = []
    source = _snapshot(SOURCE_IDENTITY)
    snapshots = iter((source, source, source))
    ffprobe = _evidence(
        ValidatorOutcome.AUDIO_PRESENT,
        duration_seconds=7_203.25,
        audio_tracks=TRACKS,
    )
    pyav = _evidence(ValidatorOutcome.AUDIO_PRESENT)

    def source_snapshot(_runtime, path):
        calls.append(("snapshot", path))
        return next(snapshots)

    def probe_ffprobe(_runtime, path):
        calls.append(("ffprobe", path))
        return ffprobe

    def probe_pyav(_runtime, path):
        calls.append(("pyav", path))
        return pyav

    monkeypatch.setattr(media, "_source_snapshot", source_snapshot)
    monkeypatch.setattr(media, "_probe_ffprobe", probe_ffprobe)
    monkeypatch.setattr(media, "_probe_pyav", probe_pyav)

    result = media.validate_media(SimpleNamespace(), "/media/show/episode.mkv")

    assert result.outcome is MediaOutcome.VALID_AUDIO
    assert result.ffprobe is ffprobe
    assert result.pyav is pyav
    assert result.source_identity == SOURCE_IDENTITY
    assert result.duration_seconds == 7_203.25
    assert result.audio_tracks == TRACKS
    assert calls == [
        ("snapshot", "/media/show/episode.mkv"),
        ("ffprobe", "/media/show/episode.mkv"),
        ("snapshot", "/media/show/episode.mkv"),
        ("pyav", "/media/show/episode.mkv"),
        ("snapshot", "/media/show/episode.mkv"),
    ]


def test_generation_change_between_validators_stops_and_forces_indeterminate(
    monkeypatch,
):
    snapshots = iter((_snapshot(SOURCE_IDENTITY), _snapshot(REPLACEMENT_IDENTITY)))
    pyav_calls = []
    monkeypatch.setattr(
        media,
        "_source_snapshot",
        lambda _runtime, _path: next(snapshots),
    )
    monkeypatch.setattr(
        media,
        "_probe_ffprobe",
        lambda _runtime, _path: _evidence(
            ValidatorOutcome.AUDIO_PRESENT,
            duration_seconds=100.0,
            audio_tracks=TRACKS,
        ),
    )
    monkeypatch.setattr(
        media,
        "_probe_pyav",
        lambda _runtime, _path: pyav_calls.append("called"),
    )

    result = media.validate_media(SimpleNamespace(), "/media/replaced.mkv")

    assert result.outcome is MediaOutcome.PROBE_INDETERMINATE
    assert result.source_identity == SOURCE_IDENTITY
    assert result.pyav.outcome is ValidatorOutcome.INDETERMINATE
    assert result.duration_seconds is None
    assert result.audio_tracks == ()
    assert pyav_calls == []


@pytest.mark.parametrize("final_snapshot", (REPLACEMENT_IDENTITY, None))
def test_generation_change_or_loss_after_pyav_overrides_dual_invalidity(
    monkeypatch,
    final_snapshot,
):
    source = _snapshot(SOURCE_IDENTITY)
    final = _snapshot(final_snapshot) if final_snapshot is not None else None
    snapshots = iter((source, source, final))
    monkeypatch.setattr(
        media,
        "_source_snapshot",
        lambda _runtime, _path: next(snapshots),
    )
    monkeypatch.setattr(
        media,
        "_probe_ffprobe",
        lambda _runtime, _path: _evidence(ValidatorOutcome.INVALID_FORMAT),
    )
    monkeypatch.setattr(
        media,
        "_probe_pyav",
        lambda _runtime, _path: _evidence(ValidatorOutcome.INVALID_FORMAT),
    )

    result = media.validate_media(SimpleNamespace(), "/media/replaced.mkv")

    assert result.outcome is MediaOutcome.PROBE_INDETERMINATE
    assert result.source_identity == SOURCE_IDENTITY
    assert result.detail_code


class _Queue:
    def __init__(self):
        self.items = []

    @staticmethod
    def is_active(_path):
        return False

    def put(self, task):
        self.items.append(task)


class _MarkerReader:
    def __init__(self, decision, calls=None):
        self.decision = decision
        self.calls = calls

    def check(self, path):
        if self.calls is not None:
            self.calls.append(("marker", path))
        return self.decision


def _queue_runtime(
    validation: MediaValidation,
    *,
    skip_marked=False,
    marker_decision=None,
    detect_language=False,
    calls=None,
):
    queue = _Queue()
    events = []
    validation_calls = []
    if marker_decision is None:
        marker_decision = SimpleNamespace(
            status="unmarked",
            report=False,
            detail="",
        )

    def validate(path):
        validation_calls.append(path)
        if calls is not None:
            calls.append(("validator", path))
        return validation

    def emit(event, task, error=None, **fields):
        events.append((event, dict(task), error, dict(fields)))

    def legacy_probe(_path):
        raise AssertionError("canonical queue must not repeat a legacy media probe")

    track = (
        validation.audio_tracks[0].as_task_dict() if validation.audio_tracks else None
    )
    runtime = SimpleNamespace(
        task_queue=queue,
        logging=MagicMock(),
        skip_marked_failed_files=skip_marked,
        failure_marker_reader=_MarkerReader(marker_decision, calls),
        validate_media=validate,
        has_audio=legacy_probe,
        get_audio_tracks=legacy_probe,
        choose_transcribe_language=lambda _path, _language, audio_tracks=None: (
            track["language"] if track else LanguageCode.NONE
        ),
        select_audio_track=lambda _tracks, _language: track,
        should_skip_file=lambda _path, _language, audio_langs=None: False,
        should_whisper_detect_audio_language=detect_language,
        force_detected_language_to=LanguageCode.NONE,
        emit_subgen_event=emit,
    )
    return runtime, queue, events, validation_calls


def test_matching_marker_prevents_both_validators():
    validation = _validation(
        MediaOutcome.VALID_AUDIO,
        duration_seconds=60.0,
        audio_tracks=TRACKS,
    )
    runtime, queue, events, validation_calls = _queue_runtime(
        validation,
        skip_marked=True,
        marker_decision=SimpleNamespace(
            status="matched",
            report=True,
            detail="exact generation",
        ),
    )

    media.gen_subtitles_queue(runtime, "/media/marked.mkv", "transcribe")

    assert validation_calls == []
    assert queue.items == []
    assert [event for event, *_rest in events] == ["failure_marker_skip"]


def test_marker_check_precedes_media_validation():
    calls = []
    validation = _validation(
        MediaOutcome.VALID_AUDIO,
        duration_seconds=60.0,
        audio_tracks=TRACKS,
    )
    runtime, queue, _events, _validation_calls = _queue_runtime(
        validation,
        skip_marked=True,
        calls=calls,
    )

    media.gen_subtitles_queue(runtime, "/media/unmarked.mkv", "transcribe")

    assert calls[:2] == [
        ("marker", "/media/unmarked.mkv"),
        ("validator", "/media/unmarked.mkv"),
    ]
    assert len(queue.items) == 1


def test_valid_media_queues_once_with_exact_validation_handoff():
    validation = _validation(
        MediaOutcome.VALID_AUDIO,
        duration_seconds=7_203.25,
        audio_tracks=TRACKS,
    )
    runtime, queue, events, validation_calls = _queue_runtime(validation)

    media.gen_subtitles_queue(
        runtime,
        "/media/movie.mkv",
        "translate",
        source="standalone",
    )

    assert validation_calls == ["/media/movie.mkv"]
    assert events == []
    assert len(queue.items) == 1
    task = queue.items[0]
    assert task["path"] == "/media/movie.mkv"
    assert task["transcribe_or_translate"] == "translate"
    assert task["audio_track_index"] == 7
    assert tuple(task["audio_tracks"]) == TASK_TRACKS
    assert task["media_duration"] == 7_203.25
    assert task["media_validation"] is validation
    assert task["source"] == "standalone"


def test_valid_detection_task_keeps_duration_tracks_and_validation():
    validation = _validation(
        MediaOutcome.VALID_AUDIO,
        duration_seconds=3_600.5,
        audio_tracks=TRACKS,
    )
    runtime, queue, events, _validation_calls = _queue_runtime(
        validation,
        detect_language=True,
    )

    media.gen_subtitles_queue(runtime, "/media/show.mkv", "transcribe")

    assert events == []
    assert len(queue.items) == 1
    task = queue.items[0]
    assert task["type"] == "detect_language"
    assert task["audio_track_index"] == 7
    assert tuple(task["audio_tracks"]) == TASK_TRACKS
    assert task["media_duration"] == 3_600.5
    assert task["media_validation"] is validation


def test_no_audio_is_retained_without_queue_or_failure_event():
    validation = _validation(MediaOutcome.NO_AUDIO)
    runtime, queue, events, validation_calls = _queue_runtime(validation)

    media.gen_subtitles_queue(runtime, "/media/silent.mkv", "transcribe")

    assert validation_calls == ["/media/silent.mkv"]
    assert queue.items == []
    assert events == []


@pytest.mark.parametrize(
    "outcome",
    (MediaOutcome.INVALID_MEDIA, MediaOutcome.PROBE_INDETERMINATE),
    ids=lambda outcome: outcome.value,
)
def test_terminal_validation_failure_emits_once_and_never_queues(outcome):
    validation = _validation(outcome)
    runtime, queue, events, validation_calls = _queue_runtime(validation)

    media.gen_subtitles_queue(runtime, "/media/bad.mkv", "transcribe")

    assert validation_calls == ["/media/bad.mkv"]
    assert queue.items == []
    assert len(events) == 1
    event, task, error, fields = events[0]
    assert event == "media_validation_failed"
    assert task["path"] == "/media/bad.mkv"
    assert task["type"] == "transcribe"
    assert error is None
    assert fields == {
        "failure_class": outcome.value,
        "source_identity": SOURCE_IDENTITY,
        "validator_outcomes": {
            "ffprobe": validation.ffprobe.outcome.value,
            "pyav": validation.pyav.outcome.value,
        },
        "validation_detail": validation.detail_code,
    }


def test_callers_cannot_override_generation_bound_queue_fields():
    validation = _validation(
        MediaOutcome.VALID_AUDIO,
        duration_seconds=120.0,
        audio_tracks=TRACKS,
    )
    replacement = _validation(MediaOutcome.INVALID_MEDIA)
    runtime, queue, events, _calls = _queue_runtime(validation)

    media.gen_subtitles_queue(
        runtime,
        "/media/canonical.mkv",
        "translate",
        path="/media/attacker.mkv",
        type="asr",
        media_validation=replacement,
        media_duration=None,
        audio_tracks=[],
        audio_track_index=99,
        source="plex",
    )

    assert events == []
    assert len(queue.items) == 1
    task = queue.items[0]
    assert task["path"] == "/media/canonical.mkv"
    assert task.get("type") is None
    assert task["media_validation"] is validation
    assert task["media_duration"] == 120.0
    assert tuple(task["audio_tracks"]) == TASK_TRACKS
    assert task["audio_track_index"] == 7
    assert task["source"] == "plex"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    (
        (MediaOutcome.VALID_AUDIO, True),
        (MediaOutcome.NO_AUDIO, False),
        (MediaOutcome.INVALID_MEDIA, False),
        (MediaOutcome.PROBE_INDETERMINATE, False),
    ),
    ids=lambda value: value.value if isinstance(value, MediaOutcome) else str(value),
)
def test_has_audio_is_a_pure_boolean_compatibility_facade(
    outcome,
    expected,
):
    calls = []
    validation = _validation(outcome)

    def validate(path):
        calls.append(path)
        return validation

    runtime = SimpleNamespace(
        validate_media=validate,
        emit_subgen_event=lambda *_args, **_kwargs: pytest.fail(
            "has_audio must not emit a second validation event"
        ),
    )

    assert media.has_audio(runtime, "/media/item.mkv") is expected
    assert calls == ["/media/item.mkv"]


def _process_runtime():
    return SimpleNamespace(
        json=json,
        os=os,
        subprocess=subprocess,
        sys=sys,
        threading=threading,
        time=time,
    )


def test_bounded_process_retains_only_completed_bounded_stdout():
    result = media._run_bounded_process(
        _process_runtime(),
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok')"],
        timeout_seconds=2,
        max_stdout_bytes=32,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert result.status == "completed"
    assert result.returncode == 0
    assert result.stdout == b"ok"


def test_bounded_process_kills_oversized_output_without_retaining_it():
    result = media._run_bounded_process(
        _process_runtime(),
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 4096)"],
        timeout_seconds=2,
        max_stdout_bytes=32,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert result.status == "overflow"
    assert result.stdout == b""


def test_bounded_process_kills_and_reaps_timeout():
    started = time.monotonic()
    result = media._run_bounded_process(
        _process_runtime(),
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_seconds=0.1,
        max_stdout_bytes=32,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert result.status == "timeout"
    assert result.stdout == b""
    assert time.monotonic() - started < 2


def test_bounded_process_timeout_is_not_held_open_by_descendant_stdout():
    child = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(2)']); "
        "time.sleep(5)"
    )
    started = time.monotonic()
    result = media._run_bounded_process(
        _process_runtime(),
        [sys.executable, "-c", child],
        timeout_seconds=0.1,
        max_stdout_bytes=32,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert result.status == "timeout"
    assert time.monotonic() - started < 1


def _ffprobe_result(payload, *, returncode=0):
    return media._BoundedProcessResult(
        "completed",
        returncode=returncode,
        stdout=json.dumps(payload).encode("utf-8"),
    )


def test_ffprobe_returns_bounded_normalized_audio_handoff(monkeypatch):
    payload = {
        "format": {"format_name": "matroska,webm", "duration": "7203.25"},
        "streams": [
            {
                "index": 7,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "tags": {"language": "spa", "title": "Main commentary"},
                "disposition": {"default": 1, "forced": 0, "original": 1},
            }
        ],
    }
    monkeypatch.setattr(
        media,
        "_run_bounded_process",
        lambda *_args, **_kwargs: _ffprobe_result(payload),
    )

    evidence = media._probe_ffprobe(_process_runtime(), "/media/show.mkv")

    assert evidence.outcome is ValidatorOutcome.AUDIO_PRESENT
    assert evidence.duration_seconds == 7203.25
    assert evidence.audio_tracks == (
        AudioTrack(
            index=7,
            codec="aac",
            channels=2,
            language=LanguageCode.SPANISH,
            title="Main commentary",
            default=True,
            original=True,
            commentary=True,
        ),
    )


def test_ffprobe_recognized_silent_container_is_no_audio(monkeypatch):
    monkeypatch.setattr(
        media,
        "_run_bounded_process",
        lambda *_args, **_kwargs: _ffprobe_result(
            {
                "format": {"format_name": "matroska", "duration": "60"},
                "streams": [],
            }
        ),
    )

    evidence = media._probe_ffprobe(_process_runtime(), "/media/silent.mkv")

    assert evidence.outcome is ValidatorOutcome.NO_AUDIO
    assert evidence.duration_seconds == 60


@pytest.mark.parametrize("codec_name", (None, "", "Unknown", "N/A"))
def test_ffprobe_does_not_claim_audio_without_a_known_codec(
    monkeypatch,
    codec_name,
):
    stream = {
        "index": 2,
        "codec_type": "audio",
        "channels": 2,
    }
    if codec_name is not None:
        stream["codec_name"] = codec_name
    monkeypatch.setattr(
        media,
        "_run_bounded_process",
        lambda *_args, **_kwargs: _ffprobe_result(
            {
                "format": {"format_name": "matroska", "duration": "60"},
                "streams": [stream],
            }
        ),
    )

    evidence = media._probe_ffprobe(_process_runtime(), "/media/ambiguous.mkv")

    assert evidence.outcome is ValidatorOutcome.INDETERMINATE


def test_ffprobe_uses_finite_audio_stream_duration_when_format_omits_it(
    monkeypatch,
):
    monkeypatch.setattr(
        media,
        "_run_bounded_process",
        lambda *_args, **_kwargs: _ffprobe_result(
            {
                "format": {"format_name": "matroska"},
                "streams": [
                    {
                        "index": 2,
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "channels": 2,
                        "duration": "42.5",
                    }
                ],
            }
        ),
    )

    evidence = media._probe_ffprobe(_process_runtime(), "/media/audio.mkv")

    assert evidence.outcome is ValidatorOutcome.AUDIO_PRESENT
    assert evidence.duration_seconds == 42.5


@pytest.mark.parametrize("stream_index", (None, True, -1, "7"))
def test_ffprobe_malformed_stream_index_is_indeterminate(
    monkeypatch,
    stream_index,
):
    stream = {
        "codec_type": "audio",
        "codec_name": "aac",
        "channels": 2,
    }
    if stream_index is not None:
        stream["index"] = stream_index
    monkeypatch.setattr(
        media,
        "_run_bounded_process",
        lambda *_args, **_kwargs: _ffprobe_result(
            {
                "format": {"format_name": "matroska", "duration": "60"},
                "streams": [stream],
            }
        ),
    )

    assert (
        media._probe_ffprobe(_process_runtime(), "/media/ambiguous.mkv").outcome
        is ValidatorOutcome.INDETERMINATE
    )


@pytest.mark.parametrize(
    ("payload", "returncode", "expected"),
    (
        (
            {"error": {"code": media.FFMPEG_INVALID_DATA}, "streams": []},
            1,
            ValidatorOutcome.INVALID_FORMAT,
        ),
        (
            {"error": {"code": -13}, "streams": []},
            1,
            ValidatorOutcome.INDETERMINATE,
        ),
        (
            {
                "error": {"code": media.FFMPEG_INVALID_DATA},
                "format": {"format_name": "mpeg"},
                "streams": [],
            },
            1,
            ValidatorOutcome.INDETERMINATE,
        ),
    ),
)
def test_ffprobe_only_exact_unrecognized_invalid_data_is_conclusive(
    monkeypatch,
    payload,
    returncode,
    expected,
):
    monkeypatch.setattr(
        media,
        "_run_bounded_process",
        lambda *_args, **_kwargs: _ffprobe_result(payload, returncode=returncode),
    )

    assert media._probe_ffprobe(_process_runtime(), "/media/input").outcome is expected


@pytest.mark.parametrize("status", ("timeout", "overflow", "spawn_error", "io_error"))
def test_ffprobe_process_failures_are_indeterminate(monkeypatch, status):
    monkeypatch.setattr(
        media,
        "_run_bounded_process",
        lambda *_args, **_kwargs: media._BoundedProcessResult(status),
    )

    evidence = media._probe_ffprobe(_process_runtime(), "/media/input")

    assert evidence.outcome is ValidatorOutcome.INDETERMINATE
    assert evidence.detail_code == f"ffprobe_{status}"


class _FakeCodec:
    name = "aac"
    channels = 2


class _Disposition(IntFlag):
    default = 0x0001
    original = 0x0004
    forced = 0x0040


class _FakeStream:
    def __init__(self):
        self.type = "audio"
        self.index = 4
        self.codec_context = _FakeCodec()
        self.metadata = {"language": "eng"}
        self.disposition = _Disposition(0)


class _FakeContainer:
    def __init__(self, frames, calls, streams=None):
        self.streams = [_FakeStream()] if streams is None else streams
        self.duration = 90_000_000
        self._frames = iter(frames)
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self._calls.append("closed")

    def decode(self, stream):
        self._calls.append(("decode", stream.index))
        return self._frames


def test_pyav_child_stops_after_first_decoded_frame():
    calls = []

    class Frames:
        def __init__(self):
            self.requests = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.requests += 1
            if self.requests > 1:
                raise AssertionError("validator requested more than the first frame")
            return object()

    frames = Frames()
    av_module = SimpleNamespace(
        open=lambda _path: _FakeContainer(frames, calls),
        time_base=1_000_000,
        error=SimpleNamespace(
            InvalidDataError=type("InvalidDataError", (Exception,), {})
        ),
    )

    payload = media._classify_with_pyav_module(av_module, "/media/movie.mkv")

    assert payload["outcome"] == ValidatorOutcome.AUDIO_PRESENT.value
    assert payload["duration_seconds"] == 90
    assert payload["audio_tracks"][0]["index"] == 4
    assert frames.requests == 1
    assert calls == [("decode", 4), "closed"]


def test_pyav_child_hands_off_all_bounded_audio_tracks_but_decodes_one():
    calls = []
    second_stream = SimpleNamespace(
        type="audio",
        index=9,
        codec_context=SimpleNamespace(name="ac3", channels=6),
        metadata={"language": "spa", "title": "Spanish"},
        disposition=(
            _Disposition.default | _Disposition.forced | _Disposition.original
        ),
    )
    av_module = SimpleNamespace(
        open=lambda _path: _FakeContainer(
            [object()],
            calls,
            streams=[_FakeStream(), second_stream],
        ),
        time_base=1_000_000,
        stream=SimpleNamespace(Disposition=_Disposition),
        error=SimpleNamespace(
            InvalidDataError=type("InvalidDataError", (Exception,), {})
        ),
    )

    payload = media._classify_with_pyav_module(av_module, "/media/movie.mkv")

    assert payload["outcome"] == ValidatorOutcome.AUDIO_PRESENT.value
    assert [track["index"] for track in payload["audio_tracks"]] == [4, 9]
    assert payload["audio_tracks"][1]["tags"] == {
        "language": "spa",
        "title": "Spanish",
    }
    assert payload["audio_tracks"][1]["disposition"] == {
        "default": True,
        "forced": True,
        "original": True,
    }
    assert calls == [("decode", 4), "closed"]


def test_pyav_only_multitrack_admission_selects_actual_default_stream():
    tracks = (
        AudioTrack(index=4, codec="aac", language=LanguageCode.NONE),
        AudioTrack(
            index=9,
            codec="ac3",
            language=LanguageCode.NONE,
            default=True,
            forced=True,
            original=True,
        ),
    )
    validation = _validation(
        MediaOutcome.VALID_AUDIO,
        duration_seconds=90.0,
        audio_tracks=tracks,
        ffprobe=ValidatorOutcome.INDETERMINATE,
        pyav=ValidatorOutcome.AUDIO_PRESENT,
    )
    runtime, queue, events, _calls = _queue_runtime(validation)
    runtime.select_audio_track = media.select_audio_track

    media.gen_subtitles_queue(runtime, "/media/multitrack.mkv", "transcribe")

    assert events == []
    assert len(queue.items) == 1
    task = queue.items[0]
    assert task["audio_track_index"] == 9
    selected = next(track for track in task["audio_tracks"] if track["index"] == 9)
    assert selected["default"] is True
    assert selected["forced"] is True
    assert selected["original"] is True


def test_pyav_child_recognizes_valid_silent_container():
    calls = []
    av_module = SimpleNamespace(
        open=lambda _path: _FakeContainer([], calls, streams=[]),
        time_base=1_000_000,
        error=SimpleNamespace(
            InvalidDataError=type("InvalidDataError", (Exception,), {})
        ),
    )

    payload = media._classify_with_pyav_module(av_module, "/media/silent.mkv")

    assert payload["outcome"] == ValidatorOutcome.NO_AUDIO.value
    assert calls == ["closed"]


def test_pyav_child_distinguishes_invalid_data_from_permission_failure():
    class InvalidDataError(Exception):
        pass

    av_module = SimpleNamespace(
        error=SimpleNamespace(InvalidDataError=InvalidDataError),
        open=MagicMock(side_effect=InvalidDataError()),
    )
    invalid = media._classify_with_pyav_module(av_module, "/media/bad.mkv")
    av_module.open.side_effect = PermissionError()
    permission = media._classify_with_pyav_module(av_module, "/media/private.mkv")

    assert invalid["outcome"] == ValidatorOutcome.INVALID_FORMAT.value
    assert permission["outcome"] == ValidatorOutcome.INDETERMINATE.value


def test_pyav_child_receives_absolute_path_when_parent_input_is_relative(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)
    captured = {}
    payload = media._pyav_payload(
        ValidatorOutcome.NO_AUDIO,
        detail_code="pyav_no_audio",
    )

    def run(_runtime, command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        return media._BoundedProcessResult(
            "completed",
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
        )

    monkeypatch.setattr(media, "_run_bounded_process", run)

    evidence = media._probe_pyav(_process_runtime(), os.path.join("media", "item.mkv"))

    assert evidence.outcome is ValidatorOutcome.NO_AUDIO
    assert captured["command"][-1] == os.path.abspath(os.path.join("media", "item.mkv"))
    assert os.path.isabs(captured["command"][-1])
    assert os.path.isabs(captured["cwd"])


@pytest.mark.parametrize(
    "owner", (transcription.gen_subtitles, transcription.detect_language_task)
)
def test_stale_admission_is_rejected_before_model_load(owner):
    start_model = MagicMock()
    runtime = SimpleNamespace(
        _media=media,
        is_media_validation_current=lambda _path, _validation: False,
        start_model=start_model,
    )
    validation = _validation(
        MediaOutcome.VALID_AUDIO,
        duration_seconds=60.0,
        audio_tracks=TRACKS,
    )

    with pytest.raises(media.MediaValidationStale):
        if owner is transcription.gen_subtitles:
            owner(
                runtime,
                "/media/replaced.mkv",
                "transcribe",
                LanguageCode.ENGLISH,
                media_validation=validation,
            )
        else:
            owner(
                runtime,
                "/media/replaced.mkv",
                {"media_validation": validation},
            )

    start_model.assert_not_called()


def _transcription_runtime_for_validation(validation):
    return SimpleNamespace(
        _media=media,
        _resource_management=resource_management,
        is_media_validation_current=lambda _path, candidate: candidate is validation,
        segmentation_enabled=True,
        model_chunk_baseline_seconds=30 * 60,
        os=os,
        is_audio_file_extension=lambda _extension: False,
        ProgressHandler=lambda _name: object(),
        start_model=MagicMock(),
        probe_media_duration=MagicMock(
            side_effect=AssertionError("duplicate duration probe")
        ),
        appendLine=MagicMock(),
        LanguageCode=LanguageCode,
        logging=MagicMock(),
        task_results_lock=threading.Lock(),
        task_results={},
        delete_model=MagicMock(),
    )


def test_missing_admission_duration_fails_before_model_load_or_duplicate_probe():
    validation = _validation(
        MediaOutcome.VALID_AUDIO,
        audio_tracks=TRACKS,
    )
    runtime = _transcription_runtime_for_validation(validation)

    with pytest.raises(transcription.MediaDurationError):
        transcription.gen_subtitles(
            runtime,
            "/media/movie.mkv",
            "transcribe",
            LanguageCode.ENGLISH,
            audio_tracks=list(TASK_TRACKS),
            audio_track_index=7,
            media_validation=validation,
        )

    runtime.start_model.assert_not_called()
    runtime.probe_media_duration.assert_not_called()


def test_detection_rejects_missing_duration_before_model_load():
    validation = _validation(
        MediaOutcome.VALID_AUDIO,
        audio_tracks=TRACKS,
    )
    start_model = MagicMock()
    runtime = SimpleNamespace(
        _media=media,
        is_media_validation_current=lambda _path, candidate: candidate is validation,
        segmentation_enabled=True,
        start_model=start_model,
    )

    with pytest.raises(transcription.MediaDurationError):
        transcription.detect_language_task(
            runtime,
            "/media/movie.mkv",
            {"media_validation": validation},
        )

    start_model.assert_not_called()


def test_detection_rejects_replacement_after_audio_extraction():
    validation = _validation(
        MediaOutcome.VALID_AUDIO,
        duration_seconds=60.0,
        audio_tracks=TRACKS,
    )
    current = iter((True, True, False))
    transcribe = MagicMock()
    runtime = SimpleNamespace(
        _media=media,
        is_media_validation_current=lambda _path, _candidate: next(current),
        segmentation_enabled=False,
        LanguageCode=LanguageCode,
        logging=MagicMock(),
        detect_language_length=30,
        detect_language_offset=0,
        start_model=MagicMock(),
        delete_model=MagicMock(),
        extract_audio_segment_to_memory=MagicMock(return_value=b"audio"),
        transcribe_with_model=transcribe,
    )

    with pytest.raises(media.MediaValidationStale):
        transcription.detect_language_task(
            runtime,
            "/media/movie.mkv",
            {"media_validation": validation, "audio_track_index": 7},
        )

    runtime.start_model.assert_called_once_with()
    runtime.extract_audio_segment_to_memory.assert_called_once()
    transcribe.assert_not_called()
    runtime.delete_model.assert_called_once_with()


def test_valid_admission_duration_reaches_whole_path_without_duplicate_probe(
    monkeypatch,
):
    validation = _validation(
        MediaOutcome.VALID_AUDIO,
        duration_seconds=60.0,
        audio_tracks=TRACKS,
    )
    runtime = _transcription_runtime_for_validation(validation)
    result = SimpleNamespace(language="en")
    whole = MagicMock(return_value=(result, None))
    publish = MagicMock()
    monkeypatch.setattr(transcription, "_whole_transcription_attempt", whole)
    monkeypatch.setattr(transcription, "_publish_segmented_result", publish)

    transcription.gen_subtitles(
        runtime,
        "/media/movie.mkv",
        "transcribe",
        LanguageCode.ENGLISH,
        audio_tracks=list(TASK_TRACKS),
        audio_track_index=7,
        media_validation=validation,
    )

    runtime.probe_media_duration.assert_not_called()
    runtime.start_model.assert_called_once_with()
    whole.assert_called_once()
    publish.assert_called_once()


def test_segmented_path_rejects_replacement_before_next_chunk(monkeypatch):
    validation = _validation(
        MediaOutcome.VALID_AUDIO,
        duration_seconds=3600.0,
        audio_tracks=TRACKS,
    )
    current = iter((True, True, True, True, False))
    extracted = MagicMock(return_value=b"chunk")
    inferred = MagicMock(return_value=SimpleNamespace())
    runtime = SimpleNamespace(
        is_media_validation_current=lambda _path, _candidate: next(current),
        _media=media,
        get_audio_tracks=MagicMock(),
        get_audio_track_by_language=MagicMock(),
        logging=MagicMock(),
        os=os,
        extract_audio_segment_to_memory=extracted,
        transcribe_with_model=inferred,
        custom_regroup=False,
        kwargs={},
        stable_whisper=SimpleNamespace(WhisperResult=lambda *_args, **_kwargs: None),
        Segment=object,
        release_after_inference_failure=MagicMock(),
        check_model_runtime_cancelled=MagicMock(),
        wait_for_model_recovery=MagicMock(),
        model_pressure_controller=None,
        model_admission_closed=False,
        _model_runtime=SimpleNamespace(),
        _resource_management=resource_management,
    )
    window = SimpleNamespace(
        extract_start=0,
        extract_duration=300,
        ordinal=0,
    )

    def run_segmented_transcription(**kwargs):
        audio = kwargs["extract_chunk"](window)
        kwargs["transcribe_chunk"](audio, window, object())
        kwargs["extract_chunk"](window)
        raise AssertionError("stale generation should stop before second extraction")

    monkeypatch.setattr(
        transcription._segmentation,
        "run_segmented_transcription",
        run_segmented_transcription,
    )

    with pytest.raises(media.MediaValidationStale):
        transcription._segmented_transcription(
            runtime,
            "/media/movie.mkv",
            "transcribe",
            LanguageCode.ENGLISH,
            list(TASK_TRACKS),
            7,
            3600.0,
            resource_management.AdaptiveChunkState(30 * 60),
            object(),
            validation,
        )

    extracted.assert_called_once()
    inferred.assert_called_once()


def test_replacement_immediately_before_publication_discards_result(monkeypatch):
    validation = _validation(
        MediaOutcome.VALID_AUDIO,
        duration_seconds=60.0,
        audio_tracks=TRACKS,
    )
    runtime = _transcription_runtime_for_validation(validation)
    current = iter((True, True, True, False))
    runtime.is_media_validation_current = lambda _path, _candidate: next(current)
    result = SimpleNamespace(language="en")
    monkeypatch.setattr(
        transcription,
        "_whole_transcription_attempt",
        MagicMock(return_value=(result, None)),
    )
    publish = MagicMock()
    monkeypatch.setattr(transcription, "_publish_segmented_result", publish)

    with pytest.raises(media.MediaValidationStale):
        transcription.gen_subtitles(
            runtime,
            "/media/replaced.mkv",
            "transcribe",
            LanguageCode.ENGLISH,
            audio_tracks=list(TASK_TRACKS),
            audio_track_index=7,
            media_validation=validation,
        )

    runtime.appendLine.assert_called_once_with(result)
    publish.assert_not_called()
