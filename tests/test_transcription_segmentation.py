from __future__ import annotations

import errno
import json
import math
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import subgen

from subgen_core import model_runtime, resource_management, segmentation, transcription


class FakeLanguage:
    def __init__(self, code: str):
        self.code = code

    def to_iso_639_1(self) -> str:
        return self.code

    def to_name(self) -> str:
        return self.code

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakeLanguage) and self.code == other.code


class FakeLanguageCode:
    @staticmethod
    def from_string(value: object) -> FakeLanguage:
        if isinstance(value, FakeLanguage):
            return value
        return FakeLanguage(str(value or "und"))


class FakeWord:
    def __init__(
        self,
        word,
        start,
        end,
        probability=None,
        tokens=None,
        id=None,
        segment=None,
        **_ignored,
    ):
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability
        self.tokens = None if tokens is None else list(tokens)
        self.id = id
        self.segment = segment

    def to_dict(self):
        return {
            "word": self.word,
            "start": self.start,
            "end": self.end,
            "probability": self.probability,
            "tokens": None if self.tokens is None else self.tokens.copy(),
        }


class FakeSegment:
    def __init__(
        self,
        start=0.0,
        end=0.0,
        text="",
        seek=None,
        tokens=None,
        temperature=None,
        avg_logprob=None,
        compression_ratio=None,
        no_speech_prob=None,
        words=None,
        id=None,
        result=None,
        ignore_unused_args=False,
        **_ignored,
    ):
        del ignore_unused_args
        self._start = start
        self._end = end
        self._text = text
        self._tokens = [] if tokens is None else list(tokens)
        self.seek = seek
        self.temperature = temperature
        self.avg_logprob = avg_logprob
        self.compression_ratio = compression_ratio
        self.no_speech_prob = no_speech_prob
        self.words = (
            None
            if words is None
            else [
                item if isinstance(item, FakeWord) else FakeWord(**item, segment=self)
                for item in words
            ]
        )
        for word in self.words or ():
            word.segment = self
        self.id = id
        self.result = result

    @property
    def start(self):
        return self.words[0].start if self.words else self._start

    @property
    def end(self):
        return self.words[-1].end if self.words else self._end

    @property
    def text(self):
        return "".join(word.word for word in self.words) if self.words else self._text

    @property
    def tokens(self):
        if self.words and all(word.tokens is not None for word in self.words):
            return [token for word in self.words for token in word.tokens]
        return self._tokens

    def to_dict(self):
        payload = {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "seek": self.seek,
            "tokens": self.tokens.copy(),
            "temperature": self.temperature,
            "avg_logprob": self.avg_logprob,
            "compression_ratio": self.compression_ratio,
            "no_speech_prob": self.no_speech_prob,
        }
        if self.words is not None:
            payload["words"] = [word.to_dict() for word in self.words]
        return payload


class FakeWhisperResult:
    def __init__(
        self,
        payload=None,
        *,
        render_error=None,
        append_srt_extension=False,
    ):
        payload = payload or {}
        self.language = payload.get("language")
        self.segments = [
            item if isinstance(item, FakeSegment) else FakeSegment(**item)
            for item in payload.get("segments", ())
        ]
        self.render_error = render_error
        self.append_srt_extension = append_srt_extension
        self.render_calls = []
        self.reassign_ids()

    @property
    def text(self):
        return "".join(segment.text for segment in self.segments)

    def reassign_ids(self):
        for segment_id, segment in enumerate(self.segments):
            segment.id = segment_id
            segment.result = self
            for word_id, word in enumerate(segment.words or ()):
                word.id = word_id
                word.segment = segment

    def to_srt_vtt(self, filepath=None, word_level=False, vtt=False):
        self.render_calls.append((filepath, word_level, vtt))
        rendered = "\n".join(
            f"{index}\n{segment.start:.3f} --> {segment.end:.3f}\n{segment.text}\n"
            for index, segment in enumerate(self.segments, 1)
        )
        if filepath is not None:
            output_path = Path(filepath)
            if self.append_srt_extension and output_path.suffix.casefold() != ".srt":
                output_path = Path(f"{output_path}.srt")
            output_path.write_text(
                "PARTIAL" if self.render_error is not None else rendered,
                encoding="utf-8",
            )
            if self.render_error is not None:
                raise self.render_error
        return rendered


class ResultFactory:
    def __init__(self, render_error=None, append_srt_extension=False):
        self.render_error = render_error
        self.append_srt_extension = append_srt_extension
        self.results = []

    def __call__(self, payload):
        result = FakeWhisperResult(
            payload,
            render_error=self.render_error,
            append_srt_extension=self.append_srt_extension,
        )
        self.results.append(result)
        return result


class RecordingTaskResult:
    def __init__(self):
        self.results = []
        self.errors = []

    def set_result(self, value):
        self.results.append(value)

    def set_error(self, value):
        self.errors.append(value)


class FailingReplaceOs:
    """Delegate every OS operation except the atomic publication boundary."""

    path = os.path

    def __init__(self, error):
        self.error = error

    def __getattr__(self, name):
        return getattr(os, name)

    def replace(self, _source, _destination):
        raise self.error


class AtomicOrderingOs:
    """Record the staged inode ordering while delegating real operations."""

    path = os.path

    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return getattr(os, name)

    def chmod(self, path, mode):
        self.events.append("chmod")
        return os.chmod(path, mode)

    def fsync(self, descriptor):
        self.events.append("fsync")
        return os.fsync(descriptor)

    def replace(self, source, destination):
        self.events.append("replace")
        return os.replace(source, destination)


class UnsupportedDirectorySyncOs:
    """Model a network filesystem that cannot fsync directories."""

    path = os.path
    O_DIRECTORY = 0x100000

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, flags, *args, **kwargs):
        if Path(path).is_dir() and flags & self.O_DIRECTORY:
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        return os.open(path, flags, *args, **kwargs)


def raw_chunk(audio, *, language="en"):
    if not isinstance(audio, dict):
        return FakeWhisperResult(
            {
                "language": language,
                "segments": [{"start": 0.5, "end": 1.0, "text": " whole"}],
            }
        )
    extract_start = audio["start"]
    local_start = 0.5 if extract_start == 0 else 5.5
    return FakeWhisperResult(
        {
            "language": language,
            "segments": [
                {
                    "start": local_start,
                    "end": local_start + 0.5,
                    "text": f" chunk-{extract_start:g}",
                    "words": None,
                }
            ],
        }
    )


def make_runtime(
    tmp_path,
    *,
    duration,
    extension=".mkv",
    segmentation_enabled=True,
    render_error=None,
    lrc_error=None,
    append_srt_extension=False,
):
    media_path = tmp_path / f"episode{extension}"
    media_path.write_bytes(b"media")
    output_path = (
        media_path.with_suffix(".lrc")
        if extension == ".mp3"
        else tmp_path / "episode.en.srt"
    )
    task_result = RecordingTaskResult()
    progress_calls = []
    completion_calls = []
    append_calls = []
    extract_calls = []
    factory = ResultFactory(
        render_error=render_error,
        append_srt_extension=append_srt_extension,
    )

    def progress(seek, total):
        progress_calls.append((seek, total))

    def extract_segment(path, start, chunk_duration, track_index=None):
        extract_calls.append((path, start, chunk_duration, track_index))
        return {"start": start, "duration": chunk_duration}

    def write_lrc(result, path):
        Path(path).write_text(
            "PARTIAL"
            if lrc_error is not None
            else "".join(
                f"[{segment.start:.2f}]{segment.text}\n" for segment in result.segments
            ),
            encoding="utf-8",
        )
        if lrc_error is not None:
            raise lrc_error

    default_tracks = [{"index": 7, "language": FakeLanguage("fr")}]

    def track_by_language(tracks, language):
        return next(
            (track for track in tracks if track.get("language") == language),
            None,
        )

    runtime = SimpleNamespace(
        os=os,
        json=json,
        logging=MagicMock(),
        start_model=MagicMock(),
        delete_model=MagicMock(),
        segmentation_enabled=segmentation_enabled,
        model_chunk_baseline_seconds=600,
        probe_media_duration=MagicMock(return_value=duration),
        handle_multiple_audio_tracks=MagicMock(return_value=None),
        get_audio_tracks=MagicMock(return_value=default_tracks),
        get_audio_track_by_language=MagicMock(side_effect=track_by_language),
        extract_audio_segment_to_memory=MagicMock(side_effect=extract_segment),
        transcribe_with_model=MagicMock(),
        release_after_inference_failure=MagicMock(),
        wait_for_model_recovery=MagicMock(return_value=True),
        check_model_runtime_cancelled=MagicMock(),
        ProgressHandler=MagicMock(return_value=progress),
        custom_regroup="custom-regroup",
        kwargs={"beam_size": 3},
        appendLine=MagicMock(side_effect=append_calls.append),
        LanguageCode=FakeLanguageCode,
        is_audio_file_extension=lambda suffix: suffix == ".mp3",
        lrc_for_audio_files=True,
        name_subtitle=MagicMock(return_value=str(output_path)),
        write_lrc=MagicMock(side_effect=write_lrc),
        word_level_highlight=True,
        send_completion_webhook=MagicMock(
            side_effect=lambda *args: completion_calls.append(args)
        ),
        task_results={str(media_path): task_result},
        task_results_lock=threading.Lock(),
        stable_whisper=SimpleNamespace(WhisperResult=factory),
        Segment=FakeSegment,
        model_pressure_controller=None,
        _segmentation=segmentation,
        _resource_management=resource_management,
        _model_runtime=model_runtime,
    )
    return SimpleNamespace(
        runtime=runtime,
        media_path=media_path,
        output_path=output_path,
        task_result=task_result,
        progress_calls=progress_calls,
        completion_calls=completion_calls,
        append_calls=append_calls,
        extract_calls=extract_calls,
        factory=factory,
    )


def assert_common_inference_options(call, language="fr"):
    assert call.kwargs["language"] == language
    assert call.kwargs["task"] == "translate"
    assert call.kwargs["verbose"] is None
    assert call.kwargs["regroup"] == "custom-regroup"
    assert call.kwargs["beam_size"] == 3
    assert callable(call.kwargs["progress_callback"])


def test_duration_probe_requires_one_finite_positive_ffprobe_result():
    run = MagicMock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout='{"format":{"duration":"123.5"}}',
        )
    )
    runtime = SimpleNamespace(subprocess=subprocess, json=json, math=math)
    runtime.subprocess = SimpleNamespace(
        run=run,
        TimeoutExpired=subprocess.TimeoutExpired,
    )

    assert transcription.probe_media_duration(runtime, "/media/episode.mkv") == 123.5

    command = run.call_args.args[0]
    assert command[-1] == "/media/episode.mkv"
    assert "format=duration" in command
    assert run.call_args.kwargs["timeout"] == 10


@pytest.mark.parametrize(
    "completed",
    [
        SimpleNamespace(returncode=1, stdout="{}"),
        SimpleNamespace(returncode=0, stdout="not-json"),
        SimpleNamespace(returncode=0, stdout='{"format":{"duration":"nan"}}'),
        SimpleNamespace(returncode=0, stdout='{"format":{"duration":"0"}}'),
    ],
)
def test_duration_probe_rejects_failed_or_unusable_results(completed):
    runtime = SimpleNamespace(
        subprocess=SimpleNamespace(
            run=MagicMock(return_value=completed),
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
        json=json,
        math=math,
    )

    with pytest.raises(transcription.MediaDurationError):
        transcription.probe_media_duration(runtime, "/media/episode.mkv")


def test_duration_probe_timeout_is_a_retained_processing_error():
    runtime = SimpleNamespace(
        subprocess=SimpleNamespace(
            run=MagicMock(side_effect=subprocess.TimeoutExpired("ffprobe", timeout=10)),
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
        json=json,
        math=math,
    )

    with pytest.raises(transcription.MediaDurationError, match="bounded probe"):
        transcription.probe_media_duration(runtime, "/media/episode.mkv")


def test_long_media_uses_bounded_selected_track_chunks_and_completes_once(tmp_path):
    case = make_runtime(tmp_path, duration=1250)
    runtime = case.runtime
    runtime.handle_multiple_audio_tracks.return_value = b"whole-track"
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_tracks=[{"index": 2}, {"index": 7}],
        audio_track_index=7,
    )

    assert case.extract_calls == [
        (str(case.media_path), 0.0, 605.0, 7),
        (str(case.media_path), 595.0, 610.0, 7),
        (str(case.media_path), 1195.0, 55.0, 7),
    ]
    runtime.handle_multiple_audio_tracks.assert_not_called()
    assert len(runtime.transcribe_with_model.call_args_list) == 3
    for call in runtime.transcribe_with_model.call_args_list:
        assert_common_inference_options(call)
    assert case.output_path.exists()
    assert "chunk-0" in case.output_path.read_text(encoding="utf-8")
    assert runtime.appendLine.call_count == 1
    runtime.send_completion_webhook.assert_called_once_with(
        str(case.media_path),
        str(case.output_path),
        FakeLanguage("en"),
        "translate",
    )
    assert len(case.task_result.results) == 1
    assert case.task_result.errors == []
    runtime.delete_model.assert_called_once_with()


@pytest.mark.parametrize(
    ("forced_language", "expected_index"),
    [
        (FakeLanguage("fr"), 7),
        (FakeLanguage("es"), 2),
    ],
)
def test_invalid_requested_track_preserves_language_then_first_fallback(
    tmp_path,
    forced_language,
    expected_index,
):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    tracks = [
        {"index": 2, "language": FakeLanguage("de")},
        {"index": 7, "language": FakeLanguage("fr")},
    ]
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        forced_language,
        audio_tracks=tracks,
        audio_track_index=99,
    )

    assert case.extract_calls
    assert {call[3] for call in case.extract_calls} == {expected_index}
    runtime.handle_multiple_audio_tracks.assert_not_called()


@pytest.mark.parametrize("duration", [599, 600])
def test_short_and_exact_boundary_media_keep_legacy_whole_file_path(tmp_path, duration):
    case = make_runtime(tmp_path, duration=duration)
    runtime = case.runtime
    whole = FakeWhisperResult(
        {
            "language": "en",
            "segments": [{"start": 1, "end": 2, "text": " whole"}],
        }
    )
    runtime.handle_multiple_audio_tracks.return_value = b"selected-whole-track"
    runtime.transcribe_with_model.return_value = whole

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_tracks=[{"index": 2}, {"index": 7}],
        audio_track_index=7,
    )

    runtime.handle_multiple_audio_tracks.assert_called_once_with(
        str(case.media_path),
        FakeLanguage("fr"),
        audio_tracks=[{"index": 2}, {"index": 7}],
        audio_track_index=7,
    )
    runtime.extract_audio_segment_to_memory.assert_not_called()
    assert runtime.transcribe_with_model.call_args.args == (b"selected-whole-track",)
    assert_common_inference_options(runtime.transcribe_with_model.call_args)
    assert len(case.task_result.results) == 1


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            resource_management.MemoryPressureYield("shared pressure"),
            id="pressure",
        ),
        pytest.param(
            model_runtime.ModelInferenceAllocationFailure("decoder allocation"),
            id="allocation",
        ),
    ],
)
def test_whole_file_resource_failure_releases_then_falls_back_to_segments(
    tmp_path,
    failure,
):
    case = make_runtime(tmp_path, duration=500)
    runtime = case.runtime
    runtime.handle_multiple_audio_tracks.return_value = b"whole-track"

    def transcribe(audio, **_kwargs):
        if audio == b"whole-track":
            raise failure
        return raw_chunk(audio)

    runtime.transcribe_with_model.side_effect = transcribe

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_tracks=[{"index": 2}, {"index": 7}],
        audio_track_index=7,
    )

    runtime.handle_multiple_audio_tracks.assert_called_once()
    runtime.release_after_inference_failure.assert_called_once_with(failure)
    runtime.wait_for_model_recovery.assert_called_once_with()
    assert [call[1:3] for call in case.extract_calls] == [
        (0.0, 305.0),
        (295.0, 205.0),
    ]
    assert all(call[3] == 7 for call in case.extract_calls)
    assert len(case.task_result.results) == 1
    assert case.task_result.errors == []


def test_segmentation_opt_out_retries_whole_file_without_segment_extraction(tmp_path):
    case = make_runtime(tmp_path, duration=1200, segmentation_enabled=False)
    runtime = case.runtime
    payloads = iter((b"whole-attempt-1", b"whole-attempt-2"))
    runtime.handle_multiple_audio_tracks.side_effect = lambda *_args, **_kwargs: next(
        payloads
    )
    pressure = resource_management.MemoryPressureYield("shared pressure")
    successful = FakeWhisperResult(
        {
            "language": "en",
            "segments": [{"start": 1, "end": 2, "text": " whole"}],
        }
    )

    def transcribe(audio, **_kwargs):
        if audio == b"whole-attempt-1":
            raise pressure
        assert audio == b"whole-attempt-2"
        return successful

    runtime.transcribe_with_model.side_effect = transcribe

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_tracks=[{"index": 2}, {"index": 7}],
        audio_track_index=7,
    )

    assert runtime.handle_multiple_audio_tracks.call_count == 2
    runtime.extract_audio_segment_to_memory.assert_not_called()
    runtime.release_after_inference_failure.assert_called_once_with(pressure)
    runtime.wait_for_model_recovery.assert_called_once_with()
    assert runtime.logging.warning.call_count == 1
    warning = " ".join(str(value) for value in runtime.logging.warning.call_args.args)
    assert "segment" in warning.lower()
    assert len(case.task_result.results) == 1


@pytest.mark.parametrize("extension", [".mkv", ".mp3"])
def test_segmented_srt_and_lrc_replace_existing_output_atomically(tmp_path, extension):
    case = make_runtime(tmp_path, duration=601, extension=extension)
    runtime = case.runtime
    case.output_path.write_text("OLD", encoding="utf-8")
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_track_index=7,
    )

    assert len(case.extract_calls) == 2
    assert case.output_path.read_text(encoding="utf-8") != "OLD"
    assert {path.name for path in tmp_path.iterdir()} == {
        case.media_path.name,
        case.output_path.name,
    }
    runtime.send_completion_webhook.assert_called_once()
    assert len(case.task_result.results) == 1
    assert case.task_result.errors == []


def test_segmented_srt_staging_path_prevents_stable_ts_extension_sidecar(tmp_path):
    case = make_runtime(
        tmp_path,
        duration=601,
        append_srt_extension=True,
    )
    runtime = case.runtime
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_track_index=7,
    )

    assert case.output_path.read_text(encoding="utf-8").strip()
    assert {path.name for path in tmp_path.iterdir()} == {
        case.media_path.name,
        case.output_path.name,
    }
    staged_path = case.factory.results[-1].render_calls[0][0]
    assert staged_path.endswith(".tmp.srt")


def test_segmented_publish_persists_mode_before_replace(tmp_path):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    ordering_os = AtomicOrderingOs()
    runtime.os = ordering_os
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_track_index=7,
    )

    assert ordering_os.events.index("chmod") < ordering_os.events.index("fsync")
    assert ordering_os.events.index("fsync") < ordering_os.events.index("replace")


def test_unsupported_directory_fsync_does_not_turn_commit_into_failure(tmp_path):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    runtime.os = UnsupportedDirectorySyncOs()
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_track_index=7,
    )

    assert case.output_path.read_text(encoding="utf-8").strip()
    runtime.send_completion_webhook.assert_called_once()
    assert len(case.task_result.results) == 1
    assert case.task_result.errors == []
    assert (
        "directory sync unavailable"
        in str(runtime.logging.warning.call_args.args[0]).lower()
    )


@pytest.mark.parametrize("extension", [".mkv", ".mp3"])
def test_segmented_render_failure_preserves_old_file_and_cleans_temporary(
    tmp_path,
    extension,
):
    failure = OSError("render failed")
    case = make_runtime(
        tmp_path,
        duration=601,
        extension=extension,
        render_error=failure if extension == ".mkv" else None,
        lrc_error=failure if extension == ".mp3" else None,
    )
    runtime = case.runtime
    case.output_path.write_text("OLD", encoding="utf-8")
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )

    with pytest.raises(OSError, match="render failed"):
        transcription.gen_subtitles(
            runtime,
            str(case.media_path),
            "translate",
            FakeLanguage("fr"),
            audio_track_index=7,
        )

    assert case.output_path.read_text(encoding="utf-8") == "OLD"
    assert {path.name for path in tmp_path.iterdir()} == {
        case.media_path.name,
        case.output_path.name,
    }
    runtime.send_completion_webhook.assert_not_called()
    assert case.task_result.results == []
    assert case.task_result.errors == ["render failed"]
    runtime.delete_model.assert_called_once_with()


@pytest.mark.parametrize("extension", [".mkv", ".mp3"])
def test_segmented_output_failure_preserves_old_file_and_cleans_temporary(
    tmp_path,
    extension,
):
    failure = OSError("render failed")
    case = make_runtime(
        tmp_path,
        duration=601,
        extension=extension,
    )
    runtime = case.runtime
    runtime.os = FailingReplaceOs(failure)
    case.output_path.write_text("OLD", encoding="utf-8")
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )

    with pytest.raises(OSError, match="render failed"):
        transcription.gen_subtitles(
            runtime,
            str(case.media_path),
            "translate",
            FakeLanguage("fr"),
            audio_track_index=7,
        )

    assert case.output_path.read_text(encoding="utf-8") == "OLD"
    assert {path.name for path in tmp_path.iterdir()} == {
        case.media_path.name,
        case.output_path.name,
    }
    runtime.send_completion_webhook.assert_not_called()
    assert case.task_result.results == []
    assert case.task_result.errors == ["render failed"]
    runtime.delete_model.assert_called_once_with()


def test_empty_segmented_result_completes_without_append_failure(tmp_path):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    runtime.transcribe_with_model.side_effect = lambda _audio, **_kwargs: (
        FakeWhisperResult({"language": "en", "segments": []})
    )

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_track_index=7,
    )

    runtime.appendLine.assert_called_once()
    assert runtime.appendLine.call_args.args[0].segments == []
    runtime.send_completion_webhook.assert_called_once()
    assert len(case.task_result.results) == 1


def test_append_line_accepts_an_empty_result(monkeypatch):
    monkeypatch.setattr(subgen, "append", True)
    segment_factory = MagicMock()
    monkeypatch.setattr(subgen, "Segment", segment_factory)

    subgen.appendLine(SimpleNamespace(segments=[]))

    segment_factory.assert_not_called()


def test_uploaded_asr_bytes_never_enter_local_segmentation(tmp_path):
    case = make_runtime(tmp_path, duration=3600)
    runtime = case.runtime
    result_container = RecordingTaskResult()
    result = FakeWhisperResult(
        {
            "language": "en",
            "segments": [{"start": 0, "end": 1, "text": " upload"}],
        }
    )
    runtime.transcribe_with_model.return_value = result

    transcription.asr_task_worker(
        runtime,
        {
            "path": "upload-task",
            "task": "translate",
            "language": None,
            "video_file": None,
            "initial_prompt": None,
            "audio_content": b"uploaded-audio",
            "encode": True,
            "output": "json",
            "result_container": result_container,
        },
    )

    runtime.probe_media_duration.assert_not_called()
    runtime.extract_audio_segment_to_memory.assert_not_called()
    assert runtime.transcribe_with_model.call_args.kwargs["audio"] == b"uploaded-audio"
    assert result_container.results == ['{"text": "upload"}']
    assert result_container.errors == []


def test_uploaded_asr_pressure_releases_and_retries_without_segmentation(tmp_path):
    case = make_runtime(tmp_path, duration=3600)
    runtime = case.runtime
    result_container = RecordingTaskResult()
    pressure = resource_management.MemoryPressureYield("shared pressure")
    result = FakeWhisperResult(
        {
            "language": "en",
            "segments": [{"start": 0, "end": 1, "text": " upload"}],
        }
    )
    runtime.transcribe_with_model.side_effect = (pressure, result)

    transcription.asr_task_worker(
        runtime,
        {
            "path": "upload-pressure",
            "task": "translate",
            "language": None,
            "video_file": None,
            "initial_prompt": None,
            "audio_content": b"uploaded-audio",
            "encode": True,
            "output": "json",
            "result_container": result_container,
        },
    )

    assert runtime.transcribe_with_model.call_count == 2
    runtime.release_after_inference_failure.assert_called_once_with(pressure)
    runtime.wait_for_model_recovery.assert_called_once_with()
    runtime.probe_media_duration.assert_not_called()
    runtime.extract_audio_segment_to_memory.assert_not_called()
    assert result_container.results == ['{"text": "upload"}']
    assert result_container.errors == []


def test_uploaded_asr_allocation_releases_then_surfaces_without_segmentation(
    tmp_path,
):
    case = make_runtime(tmp_path, duration=3600)
    runtime = case.runtime
    result_container = RecordingTaskResult()
    allocation = model_runtime.ModelInferenceAllocationFailure("decoder allocation")
    runtime.transcribe_with_model.side_effect = allocation

    transcription.asr_task_worker(
        runtime,
        {
            "path": "upload-allocation",
            "task": "translate",
            "language": None,
            "video_file": None,
            "initial_prompt": None,
            "audio_content": b"uploaded-audio",
            "encode": True,
            "output": "json",
            "result_container": result_container,
        },
    )

    runtime.release_after_inference_failure.assert_called_once_with(allocation)
    runtime.wait_for_model_recovery.assert_called_once_with()
    runtime.probe_media_duration.assert_not_called()
    runtime.extract_audio_segment_to_memory.assert_not_called()
    assert result_container.results == []
    assert result_container.errors == ["decoder allocation"]
