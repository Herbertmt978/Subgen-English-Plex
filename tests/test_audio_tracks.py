"""
Regression tests for handle_multiple_audio_tracks().

Fix 1 addressed: UnboundLocalError when language=None and len(audio_tracks) > 1.
Before the fix, `audio_track` was never initialized when `language is None`,
causing `if audio_track is None:` to raise UnboundLocalError.
"""
import sys
import os
import importlib
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import MagicMock, patch
import subgen
from subgen import handle_multiple_audio_tracks
from language_code import LanguageCode

FAKE_TRACK_ENG = {"index": 0, "codec": "aac", "language": LanguageCode.ENGLISH, "default": True}
FAKE_TRACK_FRA = {"index": 1, "codec": "ac3", "language": LanguageCode.FRENCH, "default": False}
FAKE_BYTES = b"fake audio data"


class TestSingleAudioTrack:
    def test_single_track_returns_none(self):
        """Single-track file: no extraction needed, return None."""
        with patch.object(subgen, "get_audio_tracks", return_value=[FAKE_TRACK_ENG]):
            result = handle_multiple_audio_tracks("/fake/movie.mkv")
        assert result is None


class TestMultipleAudioTracks:
    def test_language_none_uses_first_track_no_error(self):
        """
        Regression: language=None must NOT raise UnboundLocalError.
        Should fall back to the first audio track.
        """
        with (
            patch.object(subgen, "get_audio_tracks", return_value=[FAKE_TRACK_ENG, FAKE_TRACK_FRA]),
            patch.object(subgen, "extract_audio_track_to_memory", return_value=FAKE_BYTES) as mock_extract,
        ):
            result = handle_multiple_audio_tracks("/fake/movie.mkv", language=None)

        # Must not raise; must return bytes from the first track (index 0)
        assert result == FAKE_BYTES
        mock_extract.assert_called_once_with("/fake/movie.mkv", FAKE_TRACK_ENG["index"])

    def test_language_match_selects_correct_track(self):
        """Matching language selects the right track."""
        with (
            patch.object(subgen, "get_audio_tracks", return_value=[FAKE_TRACK_ENG, FAKE_TRACK_FRA]),
            patch.object(subgen, "extract_audio_track_to_memory", return_value=FAKE_BYTES) as mock_extract,
        ):
            result = handle_multiple_audio_tracks("/fake/movie.mkv", language=LanguageCode.FRENCH)

        assert result == FAKE_BYTES
        mock_extract.assert_called_once_with("/fake/movie.mkv", FAKE_TRACK_FRA["index"])

    def test_explicit_track_index_wins_over_language(self):
        """The track selected during queueing is reused for transcription."""
        with (
            patch.object(subgen, "get_audio_tracks", return_value=[FAKE_TRACK_ENG, FAKE_TRACK_FRA]),
            patch.object(subgen, "extract_audio_track_to_memory", return_value=FAKE_BYTES) as mock_extract,
        ):
            result = handle_multiple_audio_tracks(
                "/fake/movie.mkv",
                language=LanguageCode.FRENCH,
                audio_track_index=FAKE_TRACK_ENG["index"],
            )

        assert result == FAKE_BYTES
        mock_extract.assert_called_once_with("/fake/movie.mkv", FAKE_TRACK_ENG["index"])

    def test_no_language_match_falls_back_to_first_track(self):
        """When no track matches the requested language, fall back to first track."""
        with (
            patch.object(subgen, "get_audio_tracks", return_value=[FAKE_TRACK_ENG, FAKE_TRACK_FRA]),
            patch.object(subgen, "extract_audio_track_to_memory", return_value=FAKE_BYTES) as mock_extract,
        ):
            result = handle_multiple_audio_tracks("/fake/movie.mkv", language=LanguageCode.GERMAN)

        assert result == FAKE_BYTES
        mock_extract.assert_called_once_with("/fake/movie.mkv", FAKE_TRACK_ENG["index"])

    def test_extraction_failure_returns_none(self):
        """If extraction returns None (ffmpeg error), propagate None."""
        with (
            patch.object(subgen, "get_audio_tracks", return_value=[FAKE_TRACK_ENG, FAKE_TRACK_FRA]),
            patch.object(subgen, "extract_audio_track_to_memory", return_value=None),
        ):
            result = handle_multiple_audio_tracks("/fake/movie.mkv", language=None)

        assert result is None


def test_canonical_queue_policy_uses_runtime_callbacks():
    media = importlib.import_module("subgen_core.media")
    track = {"index": 7, "language": LanguageCode.SPANISH, "default": True}
    queued = []

    class Queue:
        @staticmethod
        def is_active(_path):
            return False

        @staticmethod
        def put(task):
            queued.append(task)

    runtime = SimpleNamespace(
        task_queue=Queue(),
        logging=MagicMock(),
        skip_marked_failed_files=False,
        has_audio=lambda _path: True,
        get_audio_tracks=lambda _path: [track],
        choose_transcribe_language=lambda _path, _language, audio_tracks=None: LanguageCode.SPANISH,
        select_audio_track=lambda _tracks, _language: track,
        should_skip_file=lambda _path, _language, audio_langs=None: False,
        should_whisper_detect_audio_language=False,
        force_detected_language_to=LanguageCode.NONE,
    )

    media.gen_subtitles_queue(
        runtime,
        "/media/movie.mkv",
        "translate",
        source="standalone",
    )

    assert queued == [
        {
            "path": "/media/movie.mkv",
            "transcribe_or_translate": "translate",
            "force_language": LanguageCode.SPANISH,
            "audio_track_index": 7,
            "audio_tracks": [track],
            "source": "standalone",
        }
    ]
