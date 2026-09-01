import importlib
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import subgen

from language_code import LanguageCode

ROOT = Path(__file__).resolve().parents[1]


def _model_runtime():
    source_file = ROOT / "subgen_core" / "model_runtime.py"
    assert source_file.is_file(), "missing canonical model runtime owner"
    return importlib.import_module("subgen_core.model_runtime")


def _transcription_runtime():
    source_file = ROOT / "subgen_core" / "transcription.py"
    assert source_file.is_file(), "missing canonical transcription owner"
    return importlib.import_module("subgen_core.transcription")


def test_adaptive_runtime_facade_defaults_chunk_baseline_until_initialized():
    assert subgen.model_chunk_baseline_seconds is None


def test_adaptive_runtime_facade_forwards_release_and_recovery(monkeypatch):
    error = RuntimeError("inference allocation")
    cancelled = threading.Event()
    release = MagicMock(return_value=True)
    wait = MagicMock(return_value=True)
    monkeypatch.setattr(subgen, "model_runtime_cancel_event", cancelled)
    monkeypatch.setattr(
        subgen._model_runtime,
        "release_after_inference_failure",
        release,
    )
    monkeypatch.setattr(subgen._model_runtime, "wait_for_model_recovery", wait)

    assert subgen.release_after_inference_failure(error) is True
    assert subgen.wait_for_model_recovery() is True

    release.assert_called_once_with(subgen, error)
    wait.assert_called_once_with(subgen, cancelled)


def test_model_runtime_cancellation_check_raises_canonical_error(monkeypatch):
    cancelled = threading.Event()
    monkeypatch.setattr(subgen, "model_runtime_cancel_event", cancelled)

    assert subgen.check_model_runtime_cancelled() is None

    cancelled.set()
    with pytest.raises(
        _model_runtime().ModelRuntimeCancelled,
        match="cancelled",
    ):
        subgen.check_model_runtime_cancelled()


def test_append_line_is_safe_for_an_empty_result(monkeypatch):
    segment_factory = MagicMock()
    result = SimpleNamespace(segments=[])
    monkeypatch.setattr(subgen, "append", True)
    monkeypatch.setattr(subgen, "Segment", segment_factory)

    assert subgen.appendLine(result) is None

    assert result.segments == []
    segment_factory.assert_not_called()


class _RecordingContext:
    def __init__(self, events, name):
        self.events = events
        self.name = name
        self.held = False

    def __enter__(self):
        assert not self.held
        self.held = True
        self.events.append((f"{self.name}.enter",))
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.events.append((f"{self.name}.exit",))
        self.held = False


def _cleanup_runtime(*, clear=True, queue_idle=True, direct_tasks=0, os_name="nt"):
    events = []
    cleanup_lock = _RecordingContext(events, "cleanup_lock")
    direct_lock = _RecordingContext(events, "direct_lock")
    load_lock = _RecordingContext(events, "load_lock")

    def is_idle():
        assert cleanup_lock.held
        assert runtime.model_load_lock.held
        assert direct_lock.held
        events.append(("queue.is_idle",))
        return queue_idle

    unload_model = MagicMock()
    loaded_model = SimpleNamespace(model=SimpleNamespace(unload_model=unload_model))
    empty_cache = MagicMock()
    cuda_available = MagicMock(return_value=True)
    libc = SimpleNamespace(malloc_trim=MagicMock())
    find_library = MagicMock(return_value="libc.so")
    load_library = MagicMock(return_value=libc)
    collect = MagicMock()
    runtime = SimpleNamespace(
        model=loaded_model,
        model_cleanup_timer=object(),
        model_cleanup_lock=cleanup_lock,
        model_load_lock=load_lock,
        active_direct_tasks=direct_tasks,
        active_direct_tasks_lock=direct_lock,
        task_queue=SimpleNamespace(is_idle=MagicMock(side_effect=is_idle)),
        clear_vram_on_complete=clear,
        transcribe_device="CUDA",
        torch=SimpleNamespace(
            cuda=SimpleNamespace(
                is_available=cuda_available,
                empty_cache=empty_cache,
            )
        ),
        os=SimpleNamespace(name=os_name),
        gc=SimpleNamespace(collect=collect),
        ctypes=SimpleNamespace(
            util=SimpleNamespace(find_library=find_library),
            CDLL=load_library,
        ),
        logging=MagicMock(),
    )
    dependencies = SimpleNamespace(
        events=events,
        loaded_model=loaded_model,
        unload_model=unload_model,
        empty_cache=empty_cache,
        cuda_available=cuda_available,
        libc=libc,
        find_library=find_library,
        load_library=load_library,
        collect=collect,
    )
    return runtime, dependencies


def test_structured_event_reports_real_video_path_for_synthetic_task(caplog):
    caplog.set_level(logging.INFO)

    subgen.emit_subgen_event(
        "worker_start",
        {
            "path": "asr-deadbeef",
            "video_file": "/media/show/episode.mkv",
            "type": "asr",
        },
    )

    message = next(
        record.message
        for record in caplog.records
        if record.message.startswith("SUBGEN_EVENT ")
    )
    payload = json.loads(message.split("SUBGEN_EVENT ", 1)[1])
    assert payload["path"] == "/media/show/episode.mkv"


def test_structured_validation_event_allowlists_class_and_identity(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(subgen, "media_failure_generation", 0)
    caplog.set_level(logging.INFO)

    subgen.emit_subgen_event(
        "media_validation_failed",
        {"path": "/media/show/bad.mkv", "type": "transcribe"},
        failure_class="invalid_media",
        source_identity=(1, 2, 3, 4, 5),
        validator_outcomes={
            "ffprobe": "invalid_format",
            "pyav": "invalid_format",
        },
        validation_detail="dual_parser_invalid",
    )

    message = next(
        record.message
        for record in caplog.records
        if record.message.startswith("SUBGEN_EVENT ")
    )
    payload = json.loads(message.split("SUBGEN_EVENT ", 1)[1])
    assert payload["failure_class"] == "invalid_media"
    assert payload["source_identity"] == [1, 2, 3, 4, 5]
    assert payload["validator_outcomes"] == {
        "ffprobe": "invalid_format",
        "pyav": "invalid_format",
    }
    assert payload["validation_detail"] == "dual_parser_invalid"
    assert "error" not in payload
    assert subgen.media_failure_generation == 1


@pytest.mark.parametrize(
    ("failure_class", "source_identity"),
    (
        ("guessed_from_log_text", (1, 2, 3, 4, 5)),
        ("invalid_media", (1, 2, 3, 4)),
        ("invalid_media", (1, 2, 3, 4, -1)),
    ),
)
def test_structured_validation_event_rejects_untrusted_class_or_identity(
    failure_class,
    source_identity,
):
    with pytest.raises(ValueError):
        subgen.emit_subgen_event(
            "media_validation_failed",
            {"path": "/media/show/bad.mkv", "type": "transcribe"},
            failure_class=failure_class,
            source_identity=source_identity,
            validator_outcomes={
                "ffprobe": "invalid_format",
                "pyav": "invalid_format",
            },
            validation_detail="dual_parser_invalid",
        )


def test_transcription_worker_dispatches_tasks_and_cleans_up_after_mark_done(monkeypatch):
    class StopWorker(BaseException):
        pass

    tasks = [
        ({"path": "detect-demo", "type": "detect_language", "audio_content": b"audio"}, "detect"),
        ({"path": "asr-demo", "type": "asr"}, "asr"),
        (
            {
                "path": "/media/show/episode.mkv",
                "type": "transcribe",
                "transcribe_or_translate": "translate",
                "force_language": LanguageCode.FRENCH,
                "audio_tracks": [{"index": 4}],
                "audio_track_index": 4,
            },
            "transcribe",
        ),
    ]

    for task, expected_dispatch in tasks:
        events = []

        class OneTaskQueue:
            consumed = False

            def get(self, **_kwargs):
                if not self.consumed:
                    self.consumed = True
                    return task
                raise StopWorker

            @staticmethod
            def get_processing_tasks():
                return []

            @staticmethod
            def get_queued_tasks():
                return []

            @staticmethod
            def task_done():
                events.append(("task_done",))

            @staticmethod
            def mark_done(completed_task):
                events.append(("mark_done", completed_task))

        monkeypatch.setattr(subgen, "task_queue", OneTaskQueue())
        monkeypatch.setattr(subgen, "emit_subgen_event", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            subgen,
            "detect_language_from_upload",
            lambda dispatched_task: events.append(("detect", dispatched_task)),
        )
        monkeypatch.setattr(
            subgen,
            "asr_task_worker",
            lambda dispatched_task: events.append(("asr", dispatched_task)),
        )
        monkeypatch.setattr(
            subgen,
            "gen_subtitles",
            lambda *args, **kwargs: events.append(("transcribe", args, kwargs)),
        )
        monkeypatch.setattr(
            subgen,
            "cleanup_task_result",
            lambda task_id: events.append(("cleanup_result", task_id)),
        )
        monkeypatch.setattr(subgen, "delete_model", lambda: events.append(("delete_model",)))

        with pytest.raises(StopWorker):
            subgen.transcription_worker()

        assert events[0][0] == expected_dispatch
        assert events[-4:] == [
            ("task_done",),
            ("mark_done", task),
            ("cleanup_result", str(task["path"])),
            ("delete_model",),
        ]


@pytest.mark.parametrize(
    ("error", "terminal_event", "expected_media_failures"),
    (
        (
            subgen._media.MediaValidationStale("replacement"),
            "media_validation_stale",
            0,
        ),
        (RuntimeError("unclassified failure"), "worker_error", 0),
    ),
)
def test_worker_terminal_event_preserves_admitted_identity_without_double_event(
    monkeypatch,
    error,
    terminal_event,
    expected_media_failures,
):
    class StopWorker(BaseException):
        pass

    source_identity = (1, 2, 3, 4, 5)
    task = {
        "path": "/media/show/episode.mkv",
        "type": "transcribe",
        "transcribe_or_translate": "translate",
        "force_language": LanguageCode.FRENCH,
        "media_validation": SimpleNamespace(source_identity=source_identity),
    }

    class OneTaskQueue:
        consumed = False

        def get(self, **_kwargs):
            if not self.consumed:
                self.consumed = True
                return task
            raise StopWorker

        @staticmethod
        def get_processing_tasks():
            return []

        @staticmethod
        def get_queued_tasks():
            return []

        @staticmethod
        def task_done():
            return None

        @staticmethod
        def mark_done(_task):
            return None

    emitted = []
    monkeypatch.setattr(subgen, "media_failure_generation", 0)
    monkeypatch.setattr(subgen, "task_queue", OneTaskQueue())
    monkeypatch.setattr(
        subgen,
        "emit_subgen_event",
        lambda event, _task, error=None, **fields: emitted.append(
            (event, error, fields)
        ),
    )
    monkeypatch.setattr(
        subgen,
        "gen_subtitles",
        MagicMock(side_effect=error),
    )
    monkeypatch.setattr(subgen, "cleanup_task_result", lambda _task_id: None)
    monkeypatch.setattr(subgen, "delete_model", lambda: None)

    with pytest.raises(StopWorker):
        subgen.transcription_worker()

    assert [event for event, _error, _fields in emitted] == [
        "worker_start",
        terminal_event,
    ]
    assert all(
        fields["source_identity"] == source_identity
        for _event, _error, fields in emitted
    )
    assert subgen.media_failure_generation == expected_media_failures


@pytest.mark.parametrize(
    ("error_name", "error_code"),
    [
        ("ModelLoadProfileUnhealthy", "model_load_profile_unhealthy"),
        ("ModelReleaseError", "model_release_failed"),
        ("ModelRuntimeCancelled", "model_runtime_cancelled"),
        ("MemoryPressureYield", "memory_pressure_yield"),
    ],
)
def test_worker_reports_model_runtime_failure_without_media_attribution(
    monkeypatch,
    caplog,
    error_name,
    error_code,
):
    class StopWorker(BaseException):
        pass

    task = {
        "path": "/media/show/episode.mkv",
        "type": "transcribe",
        "transcribe_or_translate": "translate",
        "force_language": LanguageCode.FRENCH,
    }

    class OneTaskQueue:
        consumed = False

        def get(self, **_kwargs):
            if not self.consumed:
                self.consumed = True
                return task
            raise StopWorker

        @staticmethod
        def get_processing_tasks():
            return []

        @staticmethod
        def get_queued_tasks():
            return []

        @staticmethod
        def task_done():
            return None

        @staticmethod
        def mark_done(_task):
            return None

    error_owner = (
        subgen._resource_management
        if error_name == "MemoryPressureYield"
        else _model_runtime()
    )
    runtime_error = getattr(error_owner, error_name)("model runtime unavailable")
    monkeypatch.setattr(subgen, "media_failure_generation", 0)
    monkeypatch.setattr(subgen, "task_queue", OneTaskQueue())
    monkeypatch.setattr(
        subgen,
        "gen_subtitles",
        MagicMock(side_effect=runtime_error),
    )
    monkeypatch.setattr(subgen, "cleanup_task_result", lambda _task_id: None)
    monkeypatch.setattr(subgen, "delete_model", lambda: None)
    caplog.set_level(logging.INFO)

    with pytest.raises(StopWorker):
        subgen.transcription_worker()

    payloads = [
        json.loads(record.message.split("SUBGEN_EVENT ", 1)[1])
        for record in caplog.records
        if record.message.startswith("SUBGEN_EVENT ")
    ]
    runtime_errors = [
        payload for payload in payloads if payload["event"] == "runtime_error"
    ]
    assert len(runtime_errors) == 1
    assert runtime_errors[0]["scope"] == "model_runtime"
    assert runtime_errors[0]["error_code"] == error_code
    assert "path" not in runtime_errors[0]
    assert all(payload["event"] != "worker_error" for payload in payloads)
    assert subgen.media_failure_generation == 0


def test_translation_naming_is_english_without_explicit_override(monkeypatch):
    monkeypatch.setattr(subgen, "subtitle_language_name", "")
    monkeypatch.setattr(subgen, "transcribe_or_translate", "translate")

    assert subgen.define_subtitle_language_naming(LanguageCode.FRENCH, "ISO_639_1") == "en"


def test_gen_subtitles_propagates_failure_to_worker(monkeypatch):
    path = "/media/show/offender.mkv"
    fake_model = MagicMock()
    fake_model.transcribe.side_effect = RuntimeError("decoder failed")
    result_container = subgen.TaskResult()
    monkeypatch.setattr(subgen, "start_model", lambda: None)
    monkeypatch.setattr(subgen, "transcribe_with_model", fake_model.transcribe)
    monkeypatch.setattr(subgen, "delete_model", lambda: None)
    monkeypatch.setattr(subgen, "segmentation_enabled", False)
    monkeypatch.setattr(subgen, "handle_multiple_audio_tracks", lambda *args, **kwargs: None)
    monkeypatch.setattr(subgen, "task_results", {path: result_container})

    with pytest.raises(RuntimeError, match="decoder failed"):
        subgen.gen_subtitles(path, "translate", LanguageCode.FRENCH)

    assert result_container.error == "decoder failed"
    assert result_container.done.is_set()


def test_plex_requests_use_bounded_timeout(monkeypatch):
    response = SimpleNamespace(status_code=503, content=b"")
    request = MagicMock(return_value=response)
    monkeypatch.setattr(subgen.requests, "get", request)

    with pytest.raises(Exception, match="503"):
        subgen.get_plex_file_name("123", "http://plex.local:32400", "token")

    assert request.call_args.kwargs["timeout"] == subgen.http_timeout


def test_legacy_asr_output_option_is_honoured(monkeypatch):
    result = MagicMock()
    result.text = " translated text "
    result.language = "en"
    result.segments = []
    result.to_srt_vtt.return_value = "SRT"
    fake_model = MagicMock()
    fake_model.transcribe.return_value = result
    container = subgen.TaskResult()
    monkeypatch.setattr(subgen, "start_model", lambda: None)
    monkeypatch.setattr(subgen, "transcribe_with_model", fake_model.transcribe)
    monkeypatch.setattr(subgen, "delete_model", lambda: None)

    subgen.asr_task_worker({
        "path": "asr-demo",
        "type": "asr",
        "task": "translate",
        "language": None,
        "video_file": None,
        "initial_prompt": None,
        "audio_content": b"audio",
        "encode": True,
        "output": "json",
        "word_timestamps": False,
        "result_container": container,
    })

    assert container.error is None
    assert json.loads(container.result) == {"text": "translated text"}


@pytest.mark.parametrize(
    "output_format",
    ["text", "txt", "srt", "vtt", "tsv", "verbose_json"],
)
def test_asr_response_formats_forward_initial_prompt(monkeypatch, output_format):
    word = SimpleNamespace(word=" hello", start=0.1, end=0.3)
    segment = SimpleNamespace(start=0.1, end=0.4, text=" Hello ", words=[word])
    result = SimpleNamespace(
        text=" Hello ",
        language="en",
        segments=[segment],
        to_srt_vtt=MagicMock(
            side_effect=lambda filepath=None, word_level=False, vtt=False: (
                "VTT" if vtt else "SRT"
            )
        ),
    )
    transcribe = MagicMock(return_value=result)
    container = subgen.TaskResult()
    monkeypatch.setattr(subgen, "start_model", lambda: None)
    monkeypatch.setattr(subgen, "delete_model", lambda: None)
    monkeypatch.setattr(subgen, "transcribe_with_model", transcribe)
    monkeypatch.setattr(subgen, "appendLine", lambda _result: None)
    monkeypatch.setattr(subgen, "custom_regroup", "default")
    monkeypatch.setattr(subgen, "kwargs", {})

    subgen.asr_task_worker({
        "path": "asr-formats",
        "type": "asr",
        "task": "translate",
        "language": None,
        "video_file": None,
        "initial_prompt": "British place names",
        "audio_content": b"audio",
        "encode": True,
        "output_format": output_format,
        "word_timestamps": True,
        "result_container": container,
    })

    assert container.error is None
    assert transcribe.call_args.kwargs["initial_prompt"] == "British place names"
    if output_format in {"text", "txt"}:
        assert container.result == "Hello"
    elif output_format == "srt":
        assert container.result == "SRT"
    elif output_format == "vtt":
        assert container.result == "VTT"
    elif output_format == "tsv":
        assert container.result == "start\tend\ttext\n0.100\t0.400\tHello"
    else:
        payload = json.loads(container.result)
        assert payload["task"] == "translate"
        assert payload["language"] == "en"
        assert payload["duration"] == 0.4
        assert payload["text"] == "Hello"
        assert payload["segments"][0]["words"] == [
            {"word": " hello", "start": 0.1, "end": 0.3}
        ]


def test_timestamp_offset_mutates_words_and_wordless_segments_once():
    transcription = _transcription_runtime()
    word = SimpleNamespace(start=1.0, end=1.5)
    word_segment = SimpleNamespace(words=[word])
    wordless_segment = SimpleNamespace(
        words=[],
        _default_start=2.0,
        _default_end=3.0,
    )
    runtime = SimpleNamespace(logging=MagicMock())
    result = SimpleNamespace(segments=[word_segment, wordless_segment])

    transcription.apply_timestamp_offset(runtime, result, 0.75)

    assert (word.start, word.end) == (1.75, 2.25)
    assert (wordless_segment._default_start, wordless_segment._default_end) == (
        2.75,
        3.75,
    )
    runtime.logging.info.assert_called_once_with(
        "Applied +0.750s timestamp offset to 2 segments"
    )


def test_write_lrc_uses_facade_open_override(monkeypatch, tmp_path):
    output_path = tmp_path / "intercepted.lrc"
    output_file = MagicMock()
    output_file.__enter__.return_value = output_file
    opener = MagicMock(return_value=output_file)
    result = SimpleNamespace(
        segments=[SimpleNamespace(start=61.25, text="hello\nworld")]
    )
    monkeypatch.setattr(subgen, "open", opener, raising=False)

    subgen.write_lrc(result, str(output_path))

    opener.assert_called_once_with(str(output_path), "w")
    output_file.write.assert_called_once_with("[01:01.25]helloworld\n")
    assert not output_path.exists()


def test_completion_webhook_preserves_payload_and_timeout(monkeypatch, tmp_path):
    source = tmp_path / "episode.mkv"
    subtitle = tmp_path / "episode.en.srt"
    response = SimpleNamespace(status_code=204, raise_for_status=MagicMock())
    post = MagicMock(return_value=response)
    monkeypatch.setattr(subgen, "webhook_url_completed", "https://hooks.example/subgen")
    monkeypatch.setattr(subgen.requests, "post", post)

    subgen.send_completion_webhook(
        str(source),
        str(subtitle),
        LanguageCode.ENGLISH,
        "translate",
    )

    post.assert_called_once_with(
        "https://hooks.example/subgen",
        json={
            "event": "translated",
            "file": str(source.resolve()),
            "subtitle": str(subtitle.resolve()),
            "language": "en",
        },
        timeout=10,
    )
    response.raise_for_status.assert_called_once_with()


def test_translation_writes_english_named_subtitle(monkeypatch, tmp_path):
    media_file = tmp_path / "episode.mkv"
    result = SimpleNamespace(
        language="fr",
        segments=[],
        text="bonjour",
        to_srt_vtt=MagicMock(return_value="SRT"),
    )
    monkeypatch.setattr(subgen, "start_model", lambda: None)
    monkeypatch.setattr(subgen, "delete_model", lambda: None)
    monkeypatch.setattr(subgen, "segmentation_enabled", False)
    monkeypatch.setattr(subgen, "is_audio_file_extension", lambda _extension: False)
    monkeypatch.setattr(subgen, "handle_multiple_audio_tracks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(subgen, "ProgressHandler", lambda _name: None)
    monkeypatch.setattr(subgen, "transcribe_with_model", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(subgen, "appendLine", lambda _result: None)
    monkeypatch.setattr(subgen, "send_completion_webhook", lambda *_args: None)
    monkeypatch.setattr(subgen, "custom_regroup", "default")
    monkeypatch.setattr(subgen, "kwargs", {})
    monkeypatch.setattr(subgen, "transcribe_or_translate", "translate")
    monkeypatch.setattr(subgen, "subtitle_language_name", "")
    monkeypatch.setattr(subgen, "subtitle_language_naming_type", "ISO_639_1")
    monkeypatch.setattr(subgen, "show_in_subname_subgen", False)
    monkeypatch.setattr(subgen, "show_in_subname_model", False)
    monkeypatch.setattr(subgen, "task_results", {})

    subgen.gen_subtitles(str(media_file), "translate", LanguageCode.FRENCH)

    expected_path = str(media_file.with_suffix("")) + ".en.srt"
    result.to_srt_vtt.assert_called_once_with(
        expected_path,
        word_level=subgen.word_level_highlight,
    )


def test_unforced_english_metadata_still_queues_whisper_detection(monkeypatch):
    media = importlib.import_module("subgen_core.media")
    queued = []
    fake_queue = MagicMock()
    fake_queue.is_active.return_value = False
    fake_queue.put.side_effect = queued.append
    tracks = [{
        "index": 2,
        "language": LanguageCode.ENGLISH,
        "default": True,
        "codec": "aac",
    }]
    evidence = media.ValidatorEvidence(media.ValidatorOutcome.AUDIO_PRESENT)
    validation = media.MediaValidation(
        media.MediaOutcome.VALID_AUDIO,
        evidence,
        evidence,
        duration_seconds=321.0,
        audio_tracks=tuple(tracks),
    )
    monkeypatch.setattr(subgen, "task_queue", fake_queue)
    monkeypatch.setattr(subgen, "validate_media", lambda _path: validation)
    monkeypatch.setattr(subgen, "should_skip_file", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(subgen, "should_whisper_detect_audio_language", True)
    monkeypatch.setattr(subgen, "force_detected_language_to", LanguageCode.NONE)

    subgen.gen_subtitles_queue("/media/show/episode.mkv", "translate")

    assert queued == [{
        "path": "/media/show/episode.mkv",
        "type": "detect_language",
        "audio_tracks": tracks,
        "selected_audio_language": LanguageCode.ENGLISH,
        "audio_track_index": 2,
        "media_validation": validation,
        "media_duration": 321.0,
    }]


def test_detection_uses_selected_track_and_reports_bad_english_metadata(monkeypatch, caplog):
    extracted = MagicMock(return_value=b"audio")
    result = SimpleNamespace(language="es")
    monkeypatch.setattr(subgen, "extract_audio_segment_to_memory", extracted)
    monkeypatch.setattr(subgen, "transcribe_with_model", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(subgen, "start_model", lambda: None)
    monkeypatch.setattr(subgen, "delete_model", lambda: None)
    monkeypatch.setattr(subgen, "notify_on_english_audio_mismatch", True)
    original = {
        "audio_tracks": [{
            "index": 2,
            "language": LanguageCode.ENGLISH,
            "default": True,
            "codec": "aac",
            "title": "English",
        }],
        "selected_audio_language": LanguageCode.ENGLISH,
        "audio_track_index": 2,
    }

    task = subgen.detect_language_task("/media/show/episode.mkv", original)

    assert extracted.call_args.kwargs["track_index"] == 2
    assert task["force_language"] == LanguageCode.SPANISH
    assert task["audio_track_index"] == 2
    assert "ENGLISH_AUDIO_MISMATCH | /media/show/episode.mkv | detected=Spanish" in caplog.text


def test_transcribe_with_model_enters_shared_gate_for_every_inference():
    model_runtime = _model_runtime()
    events = []
    gate = _RecordingContext(events, "inference_gate")
    results = [object(), object()]

    def transcribe(*args, **kwargs):
        events.append(("model.transcribe", args, kwargs))
        return results.pop(0)

    runtime = SimpleNamespace(
        model_inference_semaphore=gate,
        model=SimpleNamespace(transcribe=transcribe),
    )

    first = model_runtime.transcribe_with_model(
        runtime,
        "first-audio",
        task="translate",
    )
    second = model_runtime.transcribe_with_model(
        runtime,
        "second-audio",
        verbose=False,
    )

    assert first is not second
    assert events == [
        ("inference_gate.enter",),
        ("model.transcribe", ("first-audio",), {"task": "translate"}),
        ("inference_gate.exit",),
        ("inference_gate.enter",),
        ("model.transcribe", ("second-audio",), {"verbose": False}),
        ("inference_gate.exit",),
    ]


def test_transcribe_with_model_releases_gate_and_propagates_inference_error():
    model_runtime = _model_runtime()
    events = []
    gate = _RecordingContext(events, "inference_gate")
    failure = RuntimeError("decoder failed")

    def transcribe(*args, **kwargs):
        events.append(("model.transcribe", args, kwargs))
        raise failure

    runtime = SimpleNamespace(
        model_inference_semaphore=gate,
        model=SimpleNamespace(transcribe=transcribe),
    )

    with pytest.raises(RuntimeError, match="decoder failed") as raised:
        model_runtime.transcribe_with_model(runtime, "audio")

    assert raised.value is failure
    assert events == [
        ("inference_gate.enter",),
        ("model.transcribe", ("audio",), {}),
        ("inference_gate.exit",),
    ]


def test_start_model_loads_once_with_current_runtime_settings():
    model_runtime = _model_runtime()
    events = []
    load_lock = _RecordingContext(events, "load_lock")
    loaded_model = object()
    loader = MagicMock(return_value=loaded_model)
    runtime = SimpleNamespace(
        model=None,
        model_load_lock=load_lock,
        stable_whisper=SimpleNamespace(load_faster_whisper=loader),
        whisper_model="large-v3",
        model_location="/models",
        transcribe_device="cuda",
        whisper_threads=7,
        concurrent_transcriptions=3,
        compute_type="float16",
        logging=MagicMock(),
    )

    model_runtime.start_model(runtime)
    model_runtime.start_model(runtime)

    assert runtime.model is loaded_model
    loader.assert_called_once_with(
        "large-v3",
        download_root="/models",
        device="cuda",
        cpu_threads=7,
        num_workers=3,
        compute_type="float16",
    )
    assert events == [
        ("load_lock.enter",),
        ("load_lock.exit",),
        ("load_lock.enter",),
        ("load_lock.exit",),
    ]


def test_start_model_checks_existing_model_inside_load_lock():
    model_runtime = _model_runtime()
    already_loaded = object()
    loader = MagicMock()

    class RacingLoadLock:
        def __init__(self):
            self.runtime = None

        def __enter__(self):
            self.runtime.model = already_loaded

        def __exit__(self, exc_type, exc_value, traceback):
            return None

    lock = RacingLoadLock()
    runtime = SimpleNamespace(
        model=None,
        model_load_lock=lock,
        stable_whisper=SimpleNamespace(load_faster_whisper=loader),
        logging=MagicMock(),
    )
    lock.runtime = runtime

    model_runtime.start_model(runtime)

    assert runtime.model is already_loaded
    loader.assert_not_called()


def test_schedule_model_cleanup_replaces_timer_and_joins_outside_lock():
    model_runtime = _model_runtime()
    events = []
    cleanup_lock = _RecordingContext(events, "cleanup_lock")

    class PreviousTimer:
        def cancel(self):
            events.append(("previous.cancel", cleanup_lock.held))

        def join(self, *, timeout):
            events.append(("previous.join", cleanup_lock.held, timeout))

    class NewTimer:
        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.daemon = False

        def start(self):
            events.append(("new.start", cleanup_lock.held, self.daemon))

    created = []

    def timer_factory(delay, callback):
        events.append(("new.create", cleanup_lock.held, delay, callback))
        timer = NewTimer(delay, callback)
        created.append(timer)
        return timer

    runtime = SimpleNamespace(
        model_cleanup_timer=PreviousTimer(),
        model_cleanup_lock=cleanup_lock,
        model_cleanup_delay=45,
        Timer=timer_factory,
        logging=MagicMock(),
    )

    model_runtime.schedule_model_cleanup(runtime)

    assert runtime.model_cleanup_timer is created[0]
    assert callable(created[0].callback)
    assert events[0] == ("cleanup_lock.enter",)
    assert events[1] == ("previous.cancel", True)
    assert events[2][:3] == ("new.create", True, 45)
    assert events[2][3] is created[0].callback
    assert events[3:] == [
        ("new.start", True, True),
        ("cleanup_lock.exit",),
        ("previous.join", False, 1),
    ]


def test_stale_cleanup_callback_cannot_clear_replacement_timer():
    model_runtime = _model_runtime()

    class Timer:
        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.daemon = False
            self.cancelled = False

        def start(self):
            return None

        def cancel(self):
            self.cancelled = True

        def join(self, *, timeout):
            assert timeout == 1

    created = []

    def timer_factory(delay, callback):
        timer = Timer(delay, callback)
        created.append(timer)
        return timer

    runtime = SimpleNamespace(
        model_cleanup_timer=None,
        model_cleanup_lock=threading.Lock(),
        model_cleanup_delay=30,
        Timer=timer_factory,
        logging=MagicMock(),
    )

    model_runtime.schedule_model_cleanup(runtime)
    first = created[0]
    model_runtime.schedule_model_cleanup(runtime)
    second = created[1]

    assert first.cancelled is True
    assert runtime.model_cleanup_timer is second
    assert first.callback() is False
    assert runtime.model_cleanup_timer is second


@pytest.mark.parametrize(
    ("clear_vram", "queue_idle", "direct_tasks", "should_schedule"),
    [
        (False, True, 0, False),
        (True, False, 0, False),
        (True, True, 1, False),
        (True, True, 0, True),
    ],
)
def test_delete_model_schedules_only_when_all_work_is_idle(
    clear_vram,
    queue_idle,
    direct_tasks,
    should_schedule,
):
    model_runtime = _model_runtime()
    events = []
    direct_lock = _RecordingContext(events, "direct_lock")

    def is_idle():
        assert direct_lock.held
        events.append(("queue.is_idle",))
        return queue_idle

    def schedule_cleanup():
        assert not direct_lock.held
        events.append(("schedule_cleanup",))

    queue_idle_mock = MagicMock(side_effect=is_idle)
    schedule_mock = MagicMock(side_effect=schedule_cleanup)
    runtime = SimpleNamespace(
        clear_vram_on_complete=clear_vram,
        active_direct_tasks=direct_tasks,
        active_direct_tasks_lock=direct_lock,
        task_queue=SimpleNamespace(is_idle=queue_idle_mock),
        schedule_model_cleanup=schedule_mock,
        logging=MagicMock(),
    )

    model_runtime.delete_model(runtime)

    assert schedule_mock.call_count == int(should_schedule)
    if clear_vram:
        queue_idle_mock.assert_called_once_with()
    else:
        queue_idle_mock.assert_not_called()
        assert events == []


@pytest.mark.parametrize(
    ("clear_vram", "queue_idle", "direct_tasks"),
    [
        (False, True, 0),
        (True, False, 0),
        (True, True, 1),
    ],
)
def test_perform_model_cleanup_skips_model_while_work_is_active_or_disabled(
    clear_vram,
    queue_idle,
    direct_tasks,
):
    model_runtime = _model_runtime()
    runtime, dependencies = _cleanup_runtime(
        clear=clear_vram,
        queue_idle=queue_idle,
        direct_tasks=direct_tasks,
    )

    model_runtime.perform_model_cleanup(runtime)

    assert runtime.model is dependencies.loaded_model
    dependencies.unload_model.assert_not_called()
    dependencies.cuda_available.assert_not_called()
    dependencies.empty_cache.assert_not_called()
    dependencies.collect.assert_not_called()
    dependencies.load_library.assert_not_called()
    assert runtime.model_cleanup_timer is None


def test_perform_model_cleanup_unloads_idle_model_and_releases_posix_memory():
    model_runtime = _model_runtime()
    runtime, dependencies = _cleanup_runtime(os_name="posix")

    model_runtime.perform_model_cleanup(runtime)

    dependencies.unload_model.assert_called_once_with()
    assert runtime.model is None
    dependencies.cuda_available.assert_called_once_with()
    dependencies.empty_cache.assert_called_once_with()
    dependencies.collect.assert_called_once_with()
    dependencies.find_library.assert_called_once_with("c")
    dependencies.load_library.assert_called_once_with("libc.so")
    dependencies.libc.malloc_trim.assert_called_once_with(0)
    assert runtime.model_cleanup_timer is None


def test_perform_model_cleanup_rechecks_idleness_after_acquiring_load_lock():
    model_runtime = _model_runtime()
    runtime, dependencies = _cleanup_runtime()
    events = dependencies.events

    class ArrivingTaskLoadLock(_RecordingContext):
        def __enter__(self):
            result = super().__enter__()
            runtime.active_direct_tasks = 1
            events.append(("direct_task.arrived",))
            return result

    runtime.model_load_lock = ArrivingTaskLoadLock(events, "load_lock")

    model_runtime.perform_model_cleanup(runtime)

    assert runtime.model is dependencies.loaded_model
    dependencies.unload_model.assert_not_called()
    assert ("direct_task.arrived",) in events
    assert ("queue.is_idle",) in events


def test_perform_model_cleanup_logs_unload_and_cuda_errors_then_clears_timer():
    model_runtime = _model_runtime()
    runtime, dependencies = _cleanup_runtime()
    unload_error = RuntimeError("unload failed")
    cache_error = RuntimeError("cache failed")
    dependencies.unload_model.side_effect = unload_error
    dependencies.empty_cache.side_effect = cache_error

    model_runtime.perform_model_cleanup(runtime)

    assert runtime.model is dependencies.loaded_model
    dependencies.unload_model.assert_called_once_with()
    dependencies.empty_cache.assert_called_once_with()
    assert runtime.model_cleanup_timer is None
    error_messages = [call.args[0] for call in runtime.logging.error.call_args_list]
    assert error_messages == [
        "Error unloading model: unload failed",
        "Error clearing CUDA cache: cache failed",
    ]
    dependencies.collect.assert_not_called()
    dependencies.load_library.assert_not_called()
