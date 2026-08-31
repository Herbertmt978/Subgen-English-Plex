from __future__ import annotations

import json
import os
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
    def __init__(self, payload=None, *, render_error=None):
        payload = payload or {}
        self.language = payload.get("language")
        self.segments = [
            item if isinstance(item, FakeSegment) else FakeSegment(**item)
            for item in payload.get("segments", ())
        ]
        self.render_error = render_error
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
            Path(filepath).write_text(
                "PARTIAL" if self.render_error is not None else rendered,
                encoding="utf-8",
            )
            if self.render_error is not None:
                raise self.render_error
        return rendered


class ResultFactory:
    def __init__(self, render_error=None):
        self.render_error = render_error
        self.results = []

    def __call__(self, payload):
        result = FakeWhisperResult(payload, render_error=self.render_error)
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
    factory = ResultFactory(render_error=render_error)

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
