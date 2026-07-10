import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import subgen
from language_code import LanguageCode


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


def test_translation_naming_is_english_without_explicit_override(monkeypatch):
    monkeypatch.setattr(subgen, "subtitle_language_name", "")
    monkeypatch.setattr(subgen, "transcribe_or_translate", "translate")

    assert subgen.define_subtitle_language_naming(LanguageCode.FRENCH, "ISO_639_1") == "en"


def test_gen_subtitles_propagates_failure_to_worker(monkeypatch):
    fake_model = MagicMock()
    fake_model.transcribe.side_effect = RuntimeError("decoder failed")
    monkeypatch.setattr(subgen, "model", fake_model)
    monkeypatch.setattr(subgen, "start_model", lambda: None)
    monkeypatch.setattr(subgen, "delete_model", lambda: None)
    monkeypatch.setattr(subgen, "handle_multiple_audio_tracks", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="decoder failed"):
        subgen.gen_subtitles("/media/show/offender.mkv", "translate", LanguageCode.FRENCH)


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
    monkeypatch.setattr(subgen, "model", fake_model)
    monkeypatch.setattr(subgen, "start_model", lambda: None)
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


def test_unforced_english_metadata_still_queues_whisper_detection(monkeypatch):
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
    monkeypatch.setattr(subgen, "task_queue", fake_queue)
    monkeypatch.setattr(subgen, "has_audio", lambda _path: True)
    monkeypatch.setattr(subgen, "get_audio_tracks", lambda _path: tracks)
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
