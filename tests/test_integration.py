"""
Integration tests: full request → queue flow (Whisper still mocked).

These tests verify that a webhook POST actually results in a task appearing
in the task_queue, and that path mapping is applied end-to-end.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import subgen
from fastapi.testclient import TestClient
from subgen import DeduplicatedQueue, app, task_queue

from language_code import LanguageCode


def _valid_media_validation():
    media = subgen._media
    track = {
        "index": 0,
        "codec": "aac",
        "language": LanguageCode.NONE,
        "default": True,
    }
    evidence = media.ValidatorEvidence(media.ValidatorOutcome.AUDIO_PRESENT)
    return media.MediaValidation(
        media.MediaOutcome.VALID_AUDIO,
        evidence,
        evidence,
        duration_seconds=60.0,
        audio_tracks=(track,),
    )


def _record_accepted_enqueues(monkeypatch, task_queue):
    """Capture accepted queue submissions even if a worker consumes them."""

    accepted_tasks = []
    original_put = task_queue.put

    def record_put(item, block=True, timeout=None):
        accepted = original_put(item, block=block, timeout=timeout)
        if accepted:
            accepted_tasks.append(dict(item))
        return accepted

    monkeypatch.setattr(task_queue, "put", record_put)
    return accepted_tasks


@pytest.fixture(autouse=True)
def reset_queue(monkeypatch):
    """Give each test a clean queue so tasks from one test don't affect another."""
    fresh = DeduplicatedQueue()
    monkeypatch.setattr(subgen, "task_queue", fresh)
    yield fresh


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


class TestTautulliQueuesTask:
    def test_webhook_results_in_queued_task(self, client, monkeypatch, reset_queue):
        accepted_tasks = _record_accepted_enqueues(monkeypatch, reset_queue)
        monkeypatch.setattr(subgen, "procaddedmedia", True)
        monkeypatch.setattr(
            subgen, "validate_media", lambda path: _valid_media_validation()
        )
        monkeypatch.setattr(
            subgen, "should_skip_file", lambda path, lang, audio_langs=None: False
        )
        monkeypatch.setattr(
            subgen,
            "choose_transcribe_language",
            lambda path, lang, audio_tracks=None: lang,
        )
        monkeypatch.setattr(subgen, "should_whisper_detect_audio_language", False)

        client.post(
            "/tautulli",
            headers={"source": "Tautulli"},
            json={"event": "added", "file": "/media/show.mkv"},
        )

        assert [task["path"] for task in accepted_tasks] == ["/media/show.mkv"]


class TestPathMappingApplied:
    def test_container_path_is_remapped(self, client, monkeypatch, reset_queue):
        """Container path /tv → host path /Volumes/TV must be applied before queuing."""
        accepted_tasks = _record_accepted_enqueues(monkeypatch, reset_queue)
        monkeypatch.setattr(subgen, "procaddedmedia", True)
        monkeypatch.setattr(subgen, "use_path_mapping", True)
        monkeypatch.setattr(subgen, "path_mapping_from", "/tv")
        monkeypatch.setattr(subgen, "path_mapping_to", "/Volumes/TV")
        monkeypatch.setattr(
            subgen, "validate_media", lambda path: _valid_media_validation()
        )
        monkeypatch.setattr(
            subgen, "should_skip_file", lambda path, lang, audio_langs=None: False
        )
        monkeypatch.setattr(
            subgen,
            "choose_transcribe_language",
            lambda path, lang, audio_tracks=None: lang,
        )
        monkeypatch.setattr(subgen, "should_whisper_detect_audio_language", False)

        client.post(
            "/tautulli",
            headers={"source": "Tautulli"},
            json={"event": "added", "file": "/tv/show.mkv"},
        )

        assert [task["path"] for task in accepted_tasks] == ["/Volumes/TV/show.mkv"]


class TestEmbyQueuesTask:
    def test_emby_library_new_adds_to_queue(self, client, monkeypatch, reset_queue):
        accepted_tasks = _record_accepted_enqueues(monkeypatch, reset_queue)
        monkeypatch.setattr(subgen, "procaddedmedia", True)
        monkeypatch.setattr(
            subgen, "validate_media", lambda path: _valid_media_validation()
        )
        monkeypatch.setattr(
            subgen, "should_skip_file", lambda path, lang, audio_langs=None: False
        )
        monkeypatch.setattr(
            subgen,
            "choose_transcribe_language",
            lambda path, lang, audio_tracks=None: lang,
        )
        monkeypatch.setattr(subgen, "should_whisper_detect_audio_language", False)

        data = {"Event": "library.new", "Item": {"Path": "/media/movie.mkv"}}
        client.post("/emby", data={"data": json.dumps(data)})

        assert [task["path"] for task in accepted_tasks] == ["/media/movie.mkv"]
