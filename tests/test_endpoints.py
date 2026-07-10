"""
FastAPI endpoint tests.

gen_subtitles_queue() is mocked throughout — it calls has_audio() which touches
the file system, and should_skip_file() which opens video containers.
We test routing and parsing logic, not the transcription pipeline.
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import subgen
from subgen import app


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------
class TestStatus:
    def test_returns_version_key(self, client):
        resp = client.get("/status")
        assert resp.status_code == 200
        assert "version" in resp.json()

    def test_version_contains_subgen(self, client):
        resp = client.get("/status")
        assert "Subgen" in resp.json()["version"]


# ---------------------------------------------------------------------------
# GET on POST-only endpoints
# ---------------------------------------------------------------------------
class TestGetFallback:
    @pytest.mark.parametrize("path", ["/plex", "/jellyfin", "/emby", "/tautulli", "/asr", "/detect-language"])
    def test_get_returns_redirect_message(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 200
        # The response body should mention the GitHub URL
        body = str(resp.json())
        assert "github" in body.lower() or "GET" in body or "incorrect" in body.lower()


# ---------------------------------------------------------------------------
# /tautulli
# ---------------------------------------------------------------------------
class TestTautulli:
    def test_added_event_queues_file(self, client, monkeypatch):
        monkeypatch.setattr(subgen, "procaddedmedia", True)
        with patch.object(subgen, "gen_subtitles_queue") as mock_q:
            resp = client.post(
                "/tautulli",
                headers={"source": "Tautulli"},
                json={"event": "added", "file": "/media/show.mkv"},
            )
        assert resp.status_code == 200
        mock_q.assert_called_once()
        call_args = mock_q.call_args[0]
        assert "/media/show.mkv" in call_args[0]

    def test_played_event_queues_file(self, client, monkeypatch):
        monkeypatch.setattr(subgen, "procmediaonplay", True)
        with patch.object(subgen, "gen_subtitles_queue") as mock_q:
            resp = client.post(
                "/tautulli",
                headers={"source": "Tautulli"},
                json={"event": "played", "file": "/media/show.mkv"},
            )
        assert resp.status_code == 200
        mock_q.assert_called_once()

    def test_wrong_source_not_queued(self, client):
        with patch.object(subgen, "gen_subtitles_queue") as mock_q:
            resp = client.post(
                "/tautulli",
                headers={"source": "SomethingElse"},
                json={"event": "added", "file": "/media/show.mkv"},
            )
        assert resp.status_code == 200
        mock_q.assert_not_called()
        assert "message" in resp.json()

    def test_added_not_queued_when_procaddedmedia_false(self, client, monkeypatch):
        monkeypatch.setattr(subgen, "procaddedmedia", False)
        with patch.object(subgen, "gen_subtitles_queue") as mock_q:
            resp = client.post(
                "/tautulli",
                headers={"source": "Tautulli"},
                json={"event": "added", "file": "/media/show.mkv"},
            )
        mock_q.assert_not_called()


# ---------------------------------------------------------------------------
# /plex
# ---------------------------------------------------------------------------
class TestPlex:
    def _make_payload(self, event="library.new", rating_key="12345"):
        return {
            "event": event,
            "Metadata": {"ratingKey": rating_key},
        }

    def test_library_new_queues_file(self, client, monkeypatch):
        monkeypatch.setattr(subgen, "procaddedmedia", True)
        with (
            patch.object(subgen, "get_plex_file_name", return_value="/media/show.mkv"),
            patch.object(subgen, "gen_subtitles_queue") as mock_q,
        ):
            resp = client.post(
                "/plex",
                headers={"user-agent": "PlexMediaServer/1.0"},
                data={"payload": json.dumps(self._make_payload("library.new"))},
            )
        assert resp.status_code == 200
        mock_q.assert_called_once()

    def test_media_play_queues_file(self, client, monkeypatch):
        monkeypatch.setattr(subgen, "procmediaonplay", True)
        with (
            patch.object(subgen, "get_plex_file_name", return_value="/media/show.mkv"),
            patch.object(subgen, "gen_subtitles_queue") as mock_q,
        ):
            resp = client.post(
                "/plex",
                headers={"user-agent": "PlexMediaServer/1.0"},
                data={"payload": json.dumps(self._make_payload("media.play"))},
            )
        assert resp.status_code == 200
        mock_q.assert_called_once()

    def test_wrong_user_agent_rejected(self, client):
        with patch.object(subgen, "gen_subtitles_queue") as mock_q:
            resp = client.post(
                "/plex",
                headers={"user-agent": "curl/7.0"},
                data={"payload": json.dumps(self._make_payload())},
            )
        assert resp.status_code == 200
        mock_q.assert_not_called()
        assert "message" in resp.json()

    def test_plex_route_registered_once(self):
        """Regression: duplicate @app.post('/plex') silently dropped the first handler."""
        plex_post_routes = [
            r for r in app.routes
            if hasattr(r, "path") and r.path == "/plex"
            and hasattr(r, "methods") and "POST" in r.methods
        ]
        assert len(plex_post_routes) == 1, (
            f"Expected exactly 1 POST /plex route; found {len(plex_post_routes)}"
        )


# ---------------------------------------------------------------------------
# /jellyfin
# ---------------------------------------------------------------------------
class TestJellyfin:
    def test_item_added_queues_file(self, client, monkeypatch):
        monkeypatch.setattr(subgen, "procaddedmedia", True)
        with (
            patch.object(subgen, "get_jellyfin_file_name", return_value="/media/show.mkv"),
            patch.object(subgen, "gen_subtitles_queue") as mock_q,
        ):
            resp = client.post(
                "/jellyfin",
                headers={"user-agent": "Jellyfin-Server/10.0"},
                json={"NotificationType": "ItemAdded", "ItemId": "abc123"},
            )
        assert resp.status_code == 200
        mock_q.assert_called_once()

    def test_playback_start_queues_file(self, client, monkeypatch):
        monkeypatch.setattr(subgen, "procmediaonplay", True)
        with (
            patch.object(subgen, "get_jellyfin_file_name", return_value="/media/show.mkv"),
            patch.object(subgen, "gen_subtitles_queue") as mock_q,
        ):
            resp = client.post(
                "/jellyfin",
                headers={"user-agent": "Jellyfin-Server/10.0"},
                json={"NotificationType": "PlaybackStart", "ItemId": "abc123"},
            )
        assert resp.status_code == 200
        mock_q.assert_called_once()

    def test_wrong_user_agent_rejected(self, client):
        with patch.object(subgen, "gen_subtitles_queue") as mock_q:
            resp = client.post(
                "/jellyfin",
                headers={"user-agent": "curl/7.0"},
                json={"NotificationType": "ItemAdded", "ItemId": "abc123"},
            )
        assert resp.status_code == 200
        mock_q.assert_not_called()
        assert "message" in resp.json()


# ---------------------------------------------------------------------------
# /emby
# ---------------------------------------------------------------------------
class TestEmby:
    def test_library_new_queues_file(self, client, monkeypatch):
        monkeypatch.setattr(subgen, "procaddedmedia", True)
        data = {"Event": "library.new", "Item": {"Path": "/media/show.mkv"}}
        with patch.object(subgen, "gen_subtitles_queue") as mock_q:
            resp = client.post(
                "/emby",
                data={"data": json.dumps(data)},
            )
        assert resp.status_code == 200
        mock_q.assert_called_once()

    def test_notification_test_returns_success_without_queuing(self, client):
        data = {"Event": "system.notificationtest"}
        with patch.object(subgen, "gen_subtitles_queue") as mock_q:
            resp = client.post(
                "/emby",
                data={"data": json.dumps(data)},
            )
        assert resp.status_code == 200
        mock_q.assert_not_called()
        assert "message" in resp.json()


# ---------------------------------------------------------------------------
# /asr
# ---------------------------------------------------------------------------
class TestAsr:
    def test_empty_file_returns_error(self, client):
        resp = client.post(
            "/asr",
            files={"audio_file": ("test.wav", b"", "audio/wav")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") == "error"

    def test_video_file_prompt_changes_task_identity(self, client, monkeypatch):
        queued_tasks = []

        class CompletingQueue:
            def put(self, task):
                queued_tasks.append(task)
                task["result_container"].set_result("transcript")
                return True

            @staticmethod
            def is_active(_task_id):
                return False

        monkeypatch.setattr(subgen, "task_queue", CompletingQueue())
        monkeypatch.setattr(subgen, "use_path_mapping", True)
        monkeypatch.setattr(subgen, "path_mapping_from", "/plex-media")
        monkeypatch.setattr(subgen, "path_mapping_to", "/media")

        requests = (
            ("/plex-media/same-video.mkv", "British place names"),
            ("/plex-media/same-video.mkv", "Medical terminology"),
            ("/plex-media/other-video.mkv", "Medical terminology"),
        )
        for video_file, prompt in requests:
            response = client.post(
                "/asr",
                params={
                    "video_file": video_file,
                    "initial_prompt": prompt,
                    "output": "txt",
                },
                files={"audio_file": ("sample.wav", b"identical audio", "audio/wav")},
            )
            assert response.status_code == 200

        assert len({task["path"] for task in queued_tasks}) == len(requests)
        assert all(not task["path"].startswith("/media/") for task in queued_tasks)
        assert [task["video_file"] for task in queued_tasks] == [
            video_file.replace("/plex-media", "/media")
            for video_file, _prompt in requests
        ]


# ---------------------------------------------------------------------------
# OpenAI-compatible audio endpoints
# ---------------------------------------------------------------------------
class TestOpenAICompatibleAudio:
    @pytest.mark.parametrize("endpoint", ["transcriptions", "translations"])
    def test_prompt_changes_task_identity(self, client, monkeypatch, endpoint):
        queued_tasks = []

        class CompletingQueue:
            def put(self, task):
                queued_tasks.append(task)
                task["result_container"].set_result("transcript")
                return True

            @staticmethod
            def is_active(_task_id):
                return False

        monkeypatch.setattr(subgen, "task_queue", CompletingQueue())

        for prompt in ("British place names", "Medical terminology"):
            response = client.post(
                f"/v1/audio/{endpoint}",
                files={"file": ("sample.wav", b"identical audio", "audio/wav")},
                data={"prompt": prompt, "response_format": "text"},
            )
            assert response.status_code == 200

        assert queued_tasks[0]["path"] != queued_tasks[1]["path"]
