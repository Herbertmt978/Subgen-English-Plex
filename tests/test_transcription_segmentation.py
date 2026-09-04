from __future__ import annotations

import errno
import hashlib
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

from subgen_core import (
    model_runtime,
    resource_management,
    runtime_events,
    runtime_receipts,
    segmentation,
    transcription,
)

PUBLICATION_MODES = [
    pytest.param(False, id="non-gate"),
    pytest.param(
        True,
        id="gate",
        marks=pytest.mark.skipif(
            os.name == "nt",
            reason="Task 11B gate publication is POSIX-only",
        ),
    ),
]


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


class FailingFsyncOs:
    """Delegate every OS operation except staged-file durability."""

    path = os.path

    def __init__(self, error):
        self.error = error

    def __getattr__(self, name):
        return getattr(os, name)

    def fsync(self, _descriptor):
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


class SupportedDirectorySyncOs:
    """Provide a file-backed directory-sync surrogate for Windows unit tests."""

    path = os.path
    O_DIRECTORY = 0x100000

    def __init__(self):
        self._directory_handles = {}

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, flags, *args, **kwargs):
        if Path(path).is_dir() and flags & self.O_DIRECTORY:
            proxy = Path(path) / ".directory-sync-test"
            descriptor = os.open(proxy, os.O_CREAT | os.O_RDWR, 0o600)
            self._directory_handles[descriptor] = proxy
            return descriptor
        return os.open(path, flags, *args, **kwargs)

    def close(self, descriptor):
        proxy = self._directory_handles.pop(descriptor, None)
        os.close(descriptor)
        if proxy is not None:
            proxy.unlink()


class NoStagedReadOs(SupportedDirectorySyncOs):
    """Fail if gate publication tries to seek or duplicate staged content."""

    def lseek(self, _descriptor, _offset, _whence):
        raise AssertionError("staged subtitle was sought for Python readback")

    def dup(self, _descriptor):
        raise AssertionError("staged subtitle descriptor was duplicated for readback")


class IncrementalLineSink:
    """Accept only an iterable of lines, never one accumulated LRC string."""

    def __init__(self):
        self.lines = []
        self.writelines_argument = None

    def __enter__(self):
        return self

    def __exit__(self, _error_type, _error, _traceback):
        return False

    def write(self, _payload):
        raise AssertionError("LRC output was accumulated into one write")

    def writelines(self, lines):
        self.writelines_argument = lines
        assert not isinstance(lines, (list, tuple, str))
        self.lines.extend(lines)


class RecordingReceiptCoordinator:
    """Record transcription-owned gate calls without opening a private journal."""

    gate_enabled = True

    def __init__(self, *, on_begin=None, on_complete=None, begin_error=None):
        self.events = []
        self.on_begin = on_begin
        self.on_complete = on_complete
        self.begin_error = begin_error
        self.token = object()

    def begin_workload_locked(
        self,
        workload_sha256,
        *,
        cursor_ms,
        runtime_state,
    ):
        self.events.append(("begin", workload_sha256, cursor_ms, runtime_state))
        if self.on_begin is not None:
            self.on_begin()
        if self.begin_error is not None:
            raise self.begin_error
        return self.token

    def record_chunk_locked(
        self,
        token,
        *,
        cursor_ms,
        chunk_uncommitted,
        runtime_state,
    ):
        assert token is self.token
        self.events.append(("chunk", cursor_ms, chunk_uncommitted, runtime_state))
        return True

    def abort_workload_locked(self, token, runtime_state):
        assert token is self.token
        self.events.append(("abort", runtime_state))

    def complete_workload_locked(
        self,
        token,
        *,
        terminal_cursor_ms,
        runtime_state,
    ):
        assert token is self.token
        if self.on_complete is not None:
            self.on_complete()
        self.events.append(("complete", terminal_cursor_ms, runtime_state))
        return 1


def attach_gate_receipts(runtime, coordinator):
    runtime.runtime_receipt_coordinator = coordinator
    runtime.model_runtime_condition = threading.RLock()
    if os.name == "nt":
        runtime.os = SupportedDirectorySyncOs()
    runtime.model = None
    runtime.model_admission_closed = False
    runtime.model_load_generation = 0
    runtime.model_unload_generation = 0
    runtime.cuda_oom_generation = 0
    runtime.media_failure_generation = 0
    runtime.resident_model_identity_sha256 = None


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


def record_segment_journal_directories(monkeypatch):
    """Record where production orchestration places private chunk journals."""

    directories = []
    journal_type = transcription._segmented_result.SegmentJournal

    class RecordingSegmentJournal(journal_type):
        def __init__(self, *args, directory=None, **kwargs):
            directories.append(directory)
            super().__init__(*args, directory=directory, **kwargs)

    monkeypatch.setattr(
        transcription._segmented_result,
        "SegmentJournal",
        RecordingSegmentJournal,
    )
    return directories


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


def rendered_log_messages(mock):
    messages = []
    for call in mock.call_args_list:
        if not call.args:
            continue
        message, *arguments = call.args
        messages.append(str(message) % tuple(arguments) if arguments else str(message))
    return messages


def assert_fragments_in_order(messages, fragments):
    cursor = 0
    for fragment in fragments:
        cursor = next(
            index + 1
            for index, message in enumerate(messages[cursor:], cursor)
            if fragment in message
        )


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


def test_long_media_uses_bounded_selected_track_chunks_and_completes_once(
    tmp_path,
    monkeypatch,
):
    case = make_runtime(tmp_path, duration=1250)
    runtime = case.runtime
    journal_directories = record_segment_journal_directories(monkeypatch)
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
    assert journal_directories == [None]


def test_long_media_logs_human_readable_ram_chunk_join_and_success_sequence(tmp_path):
    case = make_runtime(tmp_path, duration=1250)
    runtime = case.runtime
    runtime.model_capacity_profile = resource_management.CapacityProfile(
        effective_bytes=10 * resource_management.GIB,
        host_total_bytes=12 * resource_management.GIB,
        cgroup_limit_bytes=10 * resource_management.GIB,
        source="cgroup_v2",
    )
    runtime.memory_pressure_reserve_gib = None
    runtime.model_decision = SimpleNamespace(
        automatic_ceiling="medium",
        selected_model="medium",
        requirement=SimpleNamespace(
            required_host_bytes=11 * resource_management.GIB // 2,
            provenance="fallback",
        ),
    )
    runtime.read_resource_pressure_sample = MagicMock(
        return_value=SimpleNamespace(
            host_available_bytes=10 * resource_management.GIB,
            cgroup_current_bytes=6 * resource_management.GIB,
            cgroup_limit_bytes=10 * resource_management.GIB,
        )
    )
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

    messages = rendered_log_messages(runtime.logging.info)
    ram_message = next(
        message for message in messages if message.startswith("RAM control")
    )
    for fragment in (
        "Memory available: 10.0 GiB",
        "Model suitable: medium",
        "Model using: medium — 5.5 GiB RAM requirement",
        "Available for subtitle chunks: 3.0 GiB working headroom",
    ):
        assert fragment in ram_message
    assert_fragments_in_order(
        messages,
        (
            "Starting file: episode.mkv",
            "File split into 3 planned chunks",
            "Chunk 1/3 started — 0% of file complete",
            "Chunk 1/3 finished — 48% of file complete",
            "Chunk 2/3 started — 48% of file complete",
            "Chunk 2/3 finished — 96% of file complete",
            "Chunk 3/3 started — 96% of file complete",
            "Chunk 3/3 finished — 100% of file complete",
            "Joining chunks 1–3",
            "Chunks joined",
            "File finished successfully: episode.mkv",
        ),
    )
    runtime.read_resource_pressure_sample.assert_called_once_with()
    assert not any(str(tmp_path) in message for message in messages)


def test_multichunk_success_event_follows_atomic_publish_and_workload_completion(
    tmp_path,
    monkeypatch,
):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )
    coordinator = RecordingReceiptCoordinator()
    attach_gate_receipts(runtime, coordinator)
    opaque_id = "e" * 32
    monkeypatch.setattr(
        transcription._runtime_events,
        "new_workload_id",
        lambda: opaque_id,
    )
    original_emit = runtime_events.emit_multichunk_success

    def assert_complete_then_emit(runtime_arg, *, workload_id, chunks_total):
        assert runtime_arg is runtime
        assert case.output_path.is_file()
        assert case.output_path.read_text(encoding="utf-8").strip()
        assert coordinator.events[-1][0] == "complete"
        assert not any(
            "File finished successfully" in message
            for message in rendered_log_messages(runtime.logging.info)
        )
        return original_emit(
            runtime_arg,
            workload_id=workload_id,
            chunks_total=chunks_total,
        )

    monkeypatch.setattr(
        transcription._runtime_events,
        "emit_multichunk_success",
        assert_complete_then_emit,
    )

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_track_index=7,
    )

    messages = rendered_log_messages(runtime.logging.info)
    event_line = next(
        message for message in messages if message.startswith(runtime_events.SENTINEL)
    )
    document = json.loads(event_line.removeprefix(runtime_events.SENTINEL))
    assert document["workload_id"] == opaque_id
    assert document["chunks_total"] == 2
    assert document["atomic_publish"] == "succeeded"
    assert document["outcome"] == "success"
    assert set(document) == {
        "atomic_publish",
        "chunks_total",
        "event",
        "event_sequence",
        "monotonic_ns",
        "outcome",
        "schema",
        "workload_id",
    }
    assert str(case.media_path) not in event_line
    assert case.media_path.name not in event_line
    assert messages.index(event_line) < next(
        index
        for index, message in enumerate(messages)
        if message.startswith("File finished successfully")
    )


def test_one_chunk_success_does_not_emit_multichunk_event(tmp_path, monkeypatch):
    case = make_runtime(tmp_path, duration=300)
    runtime = case.runtime
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )
    emit = MagicMock()
    monkeypatch.setattr(
        transcription._runtime_events,
        "emit_multichunk_success",
        emit,
    )

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_track_index=7,
    )

    emit.assert_not_called()


def test_atomic_publish_failure_does_not_emit_multichunk_success(
    tmp_path,
    monkeypatch,
):
    failure = OSError("render failed")
    case = make_runtime(tmp_path, duration=601, render_error=failure)
    runtime = case.runtime
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )
    emit = MagicMock()
    monkeypatch.setattr(
        transcription._runtime_events,
        "emit_multichunk_success",
        emit,
    )

    with pytest.raises(OSError, match="render failed"):
        transcription.gen_subtitles(
            runtime,
            str(case.media_path),
            "translate",
            FakeLanguage("fr"),
            audio_track_index=7,
        )

    emit.assert_not_called()


def test_workload_completion_failure_does_not_emit_multichunk_success(
    tmp_path,
    monkeypatch,
):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )
    coordinator = RecordingReceiptCoordinator(
        on_complete=MagicMock(side_effect=RuntimeError("receipt completion failed"))
    )
    attach_gate_receipts(runtime, coordinator)
    emit = MagicMock()
    monkeypatch.setattr(
        transcription._runtime_events,
        "emit_multichunk_success",
        emit,
    )

    with pytest.raises(RuntimeError, match="receipt completion failed"):
        transcription.gen_subtitles(
            runtime,
            str(case.media_path),
            "translate",
            FakeLanguage("fr"),
            audio_track_index=7,
        )

    assert case.output_path.is_file()
    emit.assert_not_called()


def test_memory_pressure_log_names_same_cursor_and_smaller_retry_window(tmp_path):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    pressure = resource_management.MemoryPressureYield("shared pressure")
    runtime.transcribe_with_model.side_effect = (
        pressure,
        raw_chunk({"start": 0.0, "duration": 305.0}),
        raw_chunk({"start": 295.0, "duration": 306.0}),
        raw_chunk({"start": 595.0, "duration": 6.0}),
    )

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_track_index=7,
    )

    warnings = rendered_log_messages(runtime.logging.warning)
    messages = rendered_log_messages(runtime.logging.info)
    assert any(
        "Higher-priority memory pressure in chunk 1 at 00:00:00" in message
        and "MemoryPressureYield: shared pressure" in message
        for message in warnings
    )
    assert_fragments_in_order(
        messages,
        (
            "File split into 2 planned chunks",
            "Chunk 1/2 started — 0% of file complete",
            "Memory recovered; retrying chunk 1 from 00:00:00 with a "
            "5-minute window (previously 10 minutes)",
            "Adaptive chunk plan updated: 3 planned chunks",
            "Chunk 1/3 started — 0% of file complete",
            "File finished successfully: episode.mkv",
        ),
    )
    assert sum(message.startswith("RAM control") for message in messages) == 1


def test_exhausted_allocation_does_not_log_a_retry_that_never_starts(tmp_path):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    allocation = model_runtime.ModelInferenceAllocationFailure("decoder allocation")
    runtime.transcribe_with_model.side_effect = allocation

    with pytest.raises(
        model_runtime.ModelInferenceAllocationFailure,
        match="decoder allocation",
    ):
        transcription.gen_subtitles(
            runtime,
            str(case.media_path),
            "translate",
            FakeLanguage("fr"),
            audio_track_index=7,
        )

    messages = rendered_log_messages(runtime.logging.info)
    errors = rendered_log_messages(runtime.logging.error)
    retry_messages = [message for message in messages if "retrying chunk" in message]
    assert runtime.transcribe_with_model.call_count == 3
    assert runtime.release_after_inference_failure.call_count == 3
    assert len(retry_messages) == 2
    assert not any("File finished successfully" in message for message in messages)
    assert any(
        "File failed: episode.mkv — ModelInferenceAllocationFailure: "
        "decoder allocation" in message
        for message in errors
    )


def test_cold_start_publishes_baseline_before_first_segmented_inference(tmp_path):
    case = make_runtime(tmp_path, duration=1250)
    runtime = case.runtime
    runtime.model_chunk_baseline_seconds = None

    def publish_model_runtime():
        runtime.model_chunk_baseline_seconds = 600

    runtime.start_model.side_effect = publish_model_runtime
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

    runtime.start_model.assert_called_once_with()
    assert runtime.model_chunk_baseline_seconds == 600
    assert case.extract_calls == [
        (str(case.media_path), 0.0, 605.0, 7),
        (str(case.media_path), 595.0, 610.0, 7),
        (str(case.media_path), 1195.0, 55.0, 7),
    ]
    assert len(case.task_result.results) == 1
    assert case.task_result.errors == []


def test_cold_start_fails_closed_when_model_runtime_omits_baseline(tmp_path):
    case = make_runtime(tmp_path, duration=1250)
    runtime = case.runtime
    runtime.model_chunk_baseline_seconds = None

    with pytest.raises(
        RuntimeError,
        match="Model runtime did not publish a valid segmentation baseline",
    ):
        transcription.gen_subtitles(
            runtime,
            str(case.media_path),
            "translate",
            FakeLanguage("fr"),
            audio_track_index=7,
        )

    runtime.start_model.assert_called_once_with()
    runtime.transcribe_with_model.assert_not_called()
    runtime.extract_audio_segment_to_memory.assert_not_called()
    assert case.task_result.errors == [
        "Model runtime did not publish a valid segmentation baseline"
    ]


def test_gate_workload_binds_before_model_and_tracks_durable_chunk_completion(
    tmp_path,
):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )
    runtime.model_chunk_baseline_seconds = None

    def publish_model_runtime():
        runtime.model_chunk_baseline_seconds = 600

    runtime.start_model.side_effect = publish_model_runtime

    def assert_model_start_pending():
        runtime.start_model.assert_not_called()
        assert runtime.model_chunk_baseline_seconds is None

    coordinator = RecordingReceiptCoordinator(
        on_begin=assert_model_start_pending,
        on_complete=lambda: (
            (
                case.output_path.is_file()
                and case.output_path.read_text(encoding="utf-8").strip()
                and len(case.task_result.results) == 1
            )
            or pytest.fail("completion preceded durable subtitle publication")
        ),
    )
    attach_gate_receipts(runtime, coordinator)
    workload_identity = {
        "fixture_sha256": hashlib.sha256(b"media").hexdigest(),
        "task": "translate",
        "language": "fr",
        "cursor_start_ms": 0,
        "total_duration_ms": 601_000,
    }
    expected_workload = hashlib.sha256(
        runtime_receipts.canonical_json_line(workload_identity)
    ).hexdigest()

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_track_index=7,
    )

    assert coordinator.events[0][0:3] == ("begin", expected_workload, 0)
    assert [event[0:3] for event in coordinator.events[1:]] == [
        ("chunk", 0, True),
        ("chunk", 600_000, False),
        ("chunk", 600_000, True),
        ("chunk", 601_000, False),
        ("complete", 601_000, coordinator.events[-1][2]),
    ]
    assert all(event[-1] is not None for event in coordinator.events)
    runtime.start_model.assert_called_once_with()
    assert runtime.model_chunk_baseline_seconds == 600


def test_gate_rejects_foreign_workload_before_model_admission(tmp_path):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    rejection = runtime_receipts.RuntimeReceiptError("foreign gate workload")
    coordinator = RecordingReceiptCoordinator(begin_error=rejection)
    attach_gate_receipts(runtime, coordinator)

    with pytest.raises(runtime_receipts.RuntimeReceiptError, match="foreign"):
        transcription.gen_subtitles(
            runtime,
            str(case.media_path),
            "translate",
            FakeLanguage("fr"),
            audio_track_index=7,
        )

    assert [event[0] for event in coordinator.events] == ["begin"]
    runtime.start_model.assert_not_called()
    runtime.transcribe_with_model.assert_not_called()
    runtime.delete_model.assert_called_once_with()


def test_gate_yield_unwinds_same_cursor_before_retry(tmp_path):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    pressure = resource_management.MemoryPressureYield("shared pressure")
    runtime.transcribe_with_model.side_effect = (
        pressure,
        raw_chunk({"start": 0.0, "duration": 605.0}),
        raw_chunk({"start": 295.0, "duration": 310.0}),
        raw_chunk({"start": 595.0, "duration": 6.0}),
    )
    coordinator = RecordingReceiptCoordinator()
    attach_gate_receipts(runtime, coordinator)

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_track_index=7,
    )

    chunk_events = [event[0:3] for event in coordinator.events if event[0] == "chunk"]
    assert chunk_events[:4] == [
        ("chunk", 0, True),
        ("chunk", 0, False),
        ("chunk", 0, True),
        ("chunk", 300_000, False),
    ]
    assert coordinator.events[-1][0:2] == ("complete", 601_000)


def test_gate_output_failure_aborts_without_completion(tmp_path):
    failure = OSError("render failed")
    case = make_runtime(tmp_path, duration=601, render_error=failure)
    runtime = case.runtime
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )
    coordinator = RecordingReceiptCoordinator()
    attach_gate_receipts(runtime, coordinator)

    with pytest.raises(OSError, match="render failed"):
        transcription.gen_subtitles(
            runtime,
            str(case.media_path),
            "translate",
            FakeLanguage("fr"),
            audio_track_index=7,
        )

    assert coordinator.events[-1][0] == "abort"
    assert not any(event[0] == "complete" for event in coordinator.events)
    info_messages = rendered_log_messages(runtime.logging.info)
    error_messages = rendered_log_messages(runtime.logging.error)
    assert any("Joining chunks 1–2" in message for message in info_messages)
    assert not any("Chunks joined" in message for message in info_messages)
    assert not any("File finished successfully" in message for message in info_messages)
    assert any(
        "File failed: episode.mkv — OSError: render failed" in message
        for message in error_messages
    )


def test_public_coordinator_reports_completion_without_private_identity(tmp_path):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    runtime.model_runtime_condition = threading.RLock()
    coordinator = runtime_receipts.RuntimeReceiptCoordinator(
        identity=runtime_receipts.RuntimeIdentity.create(),
        config=runtime_receipts.GateReceiptConfig(),
        condition=runtime.model_runtime_condition,
    )
    coordinator.initialize()
    runtime.runtime_receipt_coordinator = coordinator
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

    assert coordinator.workload_snapshot() == {
        "active": False,
        "chunk_uncommitted": False,
        "completion_generation": 1,
    }


@pytest.mark.skipif(os.name == "nt", reason="secure gate journal requires POSIX")
def test_posix_gate_journal_captures_real_transcription_lifecycle(tmp_path):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )
    workload_identity = {
        "fixture_sha256": hashlib.sha256(b"media").hexdigest(),
        "task": "translate",
        "language": "fr",
        "cursor_start_ms": 0,
        "total_duration_ms": 601_000,
    }
    phase_a = hashlib.sha256(
        runtime_receipts.canonical_json_line(workload_identity)
    ).hexdigest()
    receipt_parent = tmp_path / "private-receipts"
    receipt_parent.mkdir(mode=0o700)
    receipt_parent.chmod(0o700)
    receipt_path = receipt_parent / "runtime.jsonl"
    attach_gate_receipts(runtime, None)
    runtime.priority_pressure_probe = SimpleNamespace(configured=True)
    coordinator = runtime_receipts.RuntimeReceiptCoordinator(
        identity=runtime_receipts.RuntimeIdentity.create(),
        config=runtime_receipts.GateReceiptConfig(
            receipt_file=receipt_path,
            gate_token_sha256="1" * 64,
            phase_a_workload_sha256=phase_a,
            phase_b_workload_sha256="2" * 64,
        ),
        condition=runtime.model_runtime_condition,
    )
    runtime.runtime_receipt_coordinator = coordinator
    with runtime.model_runtime_condition:
        coordinator.initialize_locked(
            model_runtime.runtime_receipt_state_locked(runtime)
        )

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_track_index=7,
    )
    coordinator.close()

    records = [
        json.loads(line)
        for line in receipt_path.read_text(encoding="ascii").splitlines()
    ]
    assert [record["sequence"] for record in records] == list(
        range(1, len(records) + 1)
    )
    assert records[0]["workload_sha256"] is None
    assert records[1]["workload_sha256"] == phase_a
    assert any(record["chunk_uncommitted"] is True for record in records)
    assert records[-1]["active"] is False
    assert records[-1]["completed_cursor_ms"] == 601_000
    assert records[-1]["completion_generation"] == 1


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
    monkeypatch,
):
    case = make_runtime(tmp_path, duration=500)
    runtime = case.runtime
    journal_directories = record_segment_journal_directories(monkeypatch)
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
    assert journal_directories == [None]
    warning_messages = [
        " ".join(str(value) for value in call.args)
        for call in runtime.logging.warning.call_args_list
    ]
    info_messages = [
        " ".join(str(value) for value in call.args)
        for call in runtime.logging.info.call_args_list
    ]
    expected_reason = (
        "Memory allocation failure"
        if isinstance(failure, model_runtime.ModelInferenceAllocationFailure)
        else "Higher-priority memory pressure"
    )
    assert any(
        expected_reason in message
        and "ended the one-chunk attempt" in message
        and str(failure) in message
        for message in warning_messages
    )
    assert any(
        "Retrying" in message
        and "through adaptive segmented processing" in message
        and expected_reason.lower() in message
        for message in info_messages
    )


def test_whole_file_retry_log_is_truthful_at_five_minute_floor(tmp_path):
    case = make_runtime(tmp_path, duration=250)
    runtime = case.runtime
    runtime.model_chunk_baseline_seconds = 300
    runtime.handle_multiple_audio_tracks.return_value = b"whole-track"
    pressure = resource_management.MemoryPressureYield("shared pressure")

    def transcribe(audio, **_kwargs):
        if audio == b"whole-track":
            raise pressure
        return raw_chunk(audio)

    runtime.transcribe_with_model.side_effect = transcribe

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_tracks=[{"index": 7}],
        audio_track_index=7,
    )

    info_messages = [
        " ".join(str(value) for value in call.args)
        for call in runtime.logging.info.call_args_list
    ]
    assert any(
        "Retrying" in message and "through adaptive segmented processing" in message
        for message in info_messages
    )
    assert not any("smaller adaptive chunks" in message for message in info_messages)
    assert len(case.task_result.results) == 1
    assert case.task_result.errors == []


def test_whole_file_external_pressure_recovery_separates_segment_allocation_failures(
    tmp_path,
):
    case = make_runtime(tmp_path, duration=250)
    runtime = case.runtime
    runtime.model_chunk_baseline_seconds = 300
    runtime.handle_multiple_audio_tracks.return_value = b"whole-track"
    controller = SimpleNamespace(external_pressure_recovery_generation=0)
    runtime.model_pressure_controller = controller
    allocation = model_runtime.ModelInferenceAllocationFailure("decoder allocation")
    attempts = 0

    def transcribe(audio, **_kwargs):
        nonlocal attempts
        attempts += 1
        if audio == b"whole-track" or attempts == 2:
            raise allocation
        return raw_chunk(audio)

    recovery_calls = 0

    def recover():
        nonlocal recovery_calls
        recovery_calls += 1
        if recovery_calls == 1:
            controller.external_pressure_recovery_generation += 1
        return True

    runtime.transcribe_with_model.side_effect = transcribe
    runtime.wait_for_model_recovery.side_effect = recover

    transcription.gen_subtitles(
        runtime,
        str(case.media_path),
        "translate",
        FakeLanguage("fr"),
        audio_tracks=[{"index": 7}],
        audio_track_index=7,
    )

    assert attempts == 3
    assert runtime.release_after_inference_failure.call_count == 2
    assert runtime.wait_for_model_recovery.call_count == 2
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


@pytest.mark.parametrize("gate_enabled", PUBLICATION_MODES)
def test_nonwaiting_srt_publication_never_reads_staged_payload(
    tmp_path,
    gate_enabled,
):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    runtime.task_results = {}
    runtime.os = NoStagedReadOs()
    runtime.open = MagicMock(
        side_effect=AssertionError("staged subtitle was opened for Python readback")
    )
    runtime.task11b_gate_config = SimpleNamespace(
        enabled=gate_enabled,
        validate_output_artifact_path=lambda _path, filesystem: None,
    )
    result = FakeWhisperResult(
        {
            "language": "en",
            "segments": [{"start": 0.5, "end": 1.0, "text": " exact"}],
        }
    )

    transcription._publish_segmented_result(
        runtime,
        result,
        str(case.media_path),
        str(case.media_path.with_suffix("")),
        "translate",
        FakeLanguage("en"),
        False,
    )

    assert case.output_path.read_text(encoding="utf-8") == (
        "1\n0.500 --> 1.000\n exact\n"
    )
    runtime.open.assert_not_called()
    assert case.task_result.results == []
    runtime.send_completion_webhook.assert_called_once()


@pytest.mark.parametrize("gate_enabled", PUBLICATION_MODES)
def test_waiting_srt_task_receives_exact_published_payload(tmp_path, gate_enabled):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    runtime.os = SupportedDirectorySyncOs()
    runtime.open = MagicMock(wraps=open)
    runtime.task11b_gate_config = SimpleNamespace(
        enabled=gate_enabled,
        validate_output_artifact_path=lambda _path, filesystem: None,
    )
    result = FakeWhisperResult(
        {
            "language": "en",
            "segments": [{"start": 0.5, "end": 1.0, "text": " exact"}],
        }
    )
    expected = "1\n0.500 --> 1.000\n exact\n"

    transcription._publish_segmented_result(
        runtime,
        result,
        str(case.media_path),
        str(case.media_path.with_suffix("")),
        "translate",
        FakeLanguage("en"),
        False,
    )

    published_payload = case.output_path.read_bytes().decode("utf-8")
    assert published_payload.replace("\r\n", "\n") == expected
    assert case.task_result.results == [published_payload]
    assert case.task_result.errors == []
    runtime.send_completion_webhook.assert_called_once()


def test_lrc_writer_streams_an_iterator_of_lines_without_rendering_one_string():
    sink = IncrementalLineSink()
    runtime = SimpleNamespace(open=MagicMock(return_value=sink))
    result = FakeWhisperResult(
        {
            "segments": [
                {"start": 65.25, "end": 66.0, "text": " line\nbreak"},
                {"start": 125.5, "end": 126.0, "text": " second"},
            ]
        }
    )

    transcription.write_lrc(runtime, result, "episode.lrc")

    runtime.open.assert_called_once_with("episode.lrc", "w")
    assert sink.writelines_argument is not None
    assert sink.lines == [
        "[01:05.25] linebreak\n",
        "[02:05.50] second\n",
    ]


@pytest.mark.parametrize("gate_enabled", PUBLICATION_MODES)
def test_waiting_lrc_task_keeps_srt_return_quirk_without_staged_readback(
    tmp_path,
    gate_enabled,
):
    case = make_runtime(tmp_path, duration=601, extension=".mp3")
    runtime = case.runtime
    runtime.os = NoStagedReadOs()
    runtime.task11b_gate_config = SimpleNamespace(
        enabled=gate_enabled,
        validate_output_artifact_path=lambda _path, filesystem: None,
    )

    def write_only_open(path, mode, *args, **kwargs):
        if "r" in mode:
            raise AssertionError("staged LRC was opened for Python readback")
        return open(path, mode, *args, **kwargs)

    runtime.open = write_only_open
    runtime.write_lrc = MagicMock(
        side_effect=lambda result, path: transcription.write_lrc(
            runtime,
            result,
            path,
        )
    )
    result = FakeWhisperResult(
        {
            "language": "en",
            "segments": [{"start": 65.25, "end": 66.0, "text": " lyric"}],
        }
    )

    transcription._publish_segmented_result(
        runtime,
        result,
        str(case.media_path),
        str(case.media_path.with_suffix("")),
        "translate",
        FakeLanguage("en"),
        True,
    )

    assert case.output_path.read_text(encoding="utf-8") == "[01:05.25] lyric\n"
    assert case.task_result.results == ["1\n65.250 --> 66.000\n lyric\n"]
    assert result.render_calls == [(None, True, False)]
    runtime.send_completion_webhook.assert_called_once()


@pytest.mark.parametrize("failure_phase", ["write", "fsync", "replace"])
def test_streamed_srt_publish_failure_preserves_old_output_without_completion(
    tmp_path,
    failure_phase,
):
    failure = OSError(f"{failure_phase} failed")
    case = make_runtime(
        tmp_path,
        duration=601,
        render_error=failure if failure_phase == "write" else None,
    )
    runtime = case.runtime
    if failure_phase == "fsync":
        runtime.os = FailingFsyncOs(failure)
    elif failure_phase == "replace":
        runtime.os = FailingReplaceOs(failure)
    case.output_path.write_text("OLD", encoding="utf-8")
    result = FakeWhisperResult(
        {
            "language": "en",
            "segments": [{"start": 0.5, "end": 1.0, "text": " new"}],
        },
        render_error=failure if failure_phase == "write" else None,
    )

    with pytest.raises(OSError, match=f"{failure_phase} failed"):
        transcription._publish_segmented_result(
            runtime,
            result,
            str(case.media_path),
            str(case.media_path.with_suffix("")),
            "translate",
            FakeLanguage("en"),
            False,
        )

    assert case.output_path.read_text(encoding="utf-8") == "OLD"
    assert {path.name for path in tmp_path.iterdir()} == {
        case.media_path.name,
        case.output_path.name,
    }
    runtime.send_completion_webhook.assert_not_called()
    assert case.task_result.results == []
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
    assert all(
        call[0] is None
        for bounded_result in case.factory.results
        for call in bounded_result.render_calls
    )


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


def test_gate_directory_fsync_failure_aborts_without_completion(tmp_path):
    case = make_runtime(tmp_path, duration=601)
    runtime = case.runtime
    coordinator = RecordingReceiptCoordinator()
    attach_gate_receipts(runtime, coordinator)
    runtime.os = UnsupportedDirectorySyncOs()
    runtime.transcribe_with_model.side_effect = lambda audio, **_kwargs: raw_chunk(
        audio
    )

    with pytest.raises(OSError, match="directory fsync unsupported"):
        transcription.gen_subtitles(
            runtime,
            str(case.media_path),
            "translate",
            FakeLanguage("fr"),
            audio_track_index=7,
        )

    assert coordinator.events[-1][0] == "abort"
    assert not any(event[0] == "complete" for event in coordinator.events)
    runtime.send_completion_webhook.assert_not_called()
    assert case.task_result.results == []


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
    assert len(runtime.appendLine.call_args.args[0].segments) == 0
    runtime.send_completion_webhook.assert_called_once()
    assert len(case.task_result.results) == 1


def test_append_line_accepts_an_empty_result(monkeypatch):
    monkeypatch.setattr(subgen, "append", True)
    segment_factory = MagicMock()
    monkeypatch.setattr(subgen, "Segment", segment_factory)

    subgen.appendLine(SimpleNamespace(segments=[]))

    segment_factory.assert_not_called()


def test_append_line_places_credit_after_long_final_segment(monkeypatch):
    monkeypatch.setattr(subgen, "append", True)
    segment_factory = MagicMock(return_value=object())
    monkeypatch.setattr(subgen, "Segment", segment_factory)
    segments = [SimpleNamespace(start=10.0, end=20.0, id=3)]

    subgen.appendLine(SimpleNamespace(segments=segments))

    assert segments[-1] is segment_factory.return_value
    assert segment_factory.call_args.kwargs["start"] == 25.0
    assert segment_factory.call_args.kwargs["end"] == 35.0


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
