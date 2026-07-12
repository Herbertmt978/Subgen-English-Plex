"""Focused contracts for the optional Plex and Jellyfin HTTP clients."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest

import subgen


ROOT = Path(__file__).resolve().parents[1]


class RequestFailure(Exception):
    """Request error exposed through the injected request client's API."""


class StubResponse:
    def __init__(self, *, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RequestFailure(f"HTTP {self.status_code}")


class StubRequests:
    exceptions = SimpleNamespace(RequestException=RequestFailure)

    def __init__(self, *, get=(), put=(), post=()):
        self._responses = {
            "get": list(get),
            "put": list(put),
            "post": list(post),
        }
        self.calls = []

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self._responses[method], f"unexpected {method.upper()} request to {url}"
        response = self._responses[method].pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        return self._request("get", url, **kwargs)

    def put(self, url, **kwargs):
        return self._request("put", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("post", url, **kwargs)


def _client_module(name):
    source_file = ROOT / "subgen_core" / "integrations" / f"{name}.py"
    assert source_file.is_file(), f"missing canonical {name} client: {source_file.relative_to(ROOT)}"
    importlib.invalidate_caches()
    return importlib.import_module(f"subgen_core.integrations.{name}")


def _plex_metadata(*, current="episode-1", season="season-1", show="show-1"):
    return (
        f'<MediaContainer><Video ratingKey="{current}" parentRatingKey="{season}" '
        f'grandparentRatingKey="{show}" /></MediaContainer>'
    ).encode()


def _plex_seasons(*seasons):
    directories = "".join(
        f'<Directory type="season" index="{index}" ratingKey="{rating_key}" />'
        for index, rating_key in seasons
    )
    return f"<MediaContainer>{directories}</MediaContainer>".encode()


def _plex_episodes(*episodes):
    videos = "".join(
        f'<Video ratingKey="{rating_key}" index="{index}" parentIndex="{season_index}" />'
        for rating_key, index, season_index in episodes
    )
    return f"<MediaContainer>{videos}</MediaContainer>".encode()


def _assert_auth_and_timeout(calls, header, timeout):
    assert calls
    for _method, _url, kwargs in calls:
        assert kwargs["headers"] == header
        assert kwargs["timeout"] == timeout


def test_plex_file_lookup_propagates_token_and_timeout():
    plex = _client_module("plex")
    response = StubResponse(
        content=b'<MediaContainer><Part file="/media/show.mkv" /></MediaContainer>'
    )
    request_client = StubRequests(get=[response])
    logger = MagicMock()

    result = plex.get_plex_file_name(
        "123",
        "http://plex.local:32400",
        "plex-token",
        timeout=4.5,
        request_client=request_client,
        logger=logger,
    )

    assert result == "/media/show.mkv"
    assert request_client.calls == [
        (
            "get",
            "http://plex.local:32400/library/metadata/123",
            {"headers": {"X-Plex-Token": "plex-token"}, "timeout": 4.5},
        )
    ]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "Error: 503"),
        (b"<MediaContainer />", "No Part element found in Plex XML response"),
    ],
)
def test_plex_file_lookup_preserves_error_handling(content, message):
    plex = _client_module("plex")
    status_code = 503 if not content else 200
    request_client = StubRequests(get=[StubResponse(status_code=status_code, content=content)])

    with pytest.raises(Exception, match=message):
        plex.get_plex_file_name(
            "123",
            "http://plex.local:32400",
            "plex-token",
            timeout=4.5,
            request_client=request_client,
            logger=MagicMock(),
        )


def test_plex_refresh_uses_authenticated_put_and_accepts_200():
    plex = _client_module("plex")
    request_client = StubRequests(put=[StubResponse(status_code=200)])
    logger = MagicMock()

    result = plex.refresh_plex_metadata(
        "123",
        "http://plex.local:32400",
        "plex-token",
        timeout=6,
        request_client=request_client,
        logger=logger,
    )

    assert result is None
    assert request_client.calls == [
        (
            "put",
            "http://plex.local:32400/library/metadata/123/refresh",
            {"headers": {"X-Plex-Token": "plex-token"}, "timeout": 6},
        )
    ]
    logger.info.assert_called_once_with("Metadata refresh initiated successfully.")


def test_plex_refresh_rejects_non_200_status():
    plex = _client_module("plex")
    request_client = StubRequests(put=[StubResponse(status_code=202)])

    with pytest.raises(Exception, match="Error refreshing metadata: 202"):
        plex.refresh_plex_metadata(
            "123",
            "http://plex.local:32400",
            "plex-token",
            timeout=6,
            request_client=request_client,
            logger=MagicMock(),
        )


def test_plex_next_episode_finds_next_item_in_current_season():
    plex = _client_module("plex")
    request_client = StubRequests(
        get=[
            StubResponse(content=_plex_metadata()),
            StubResponse(content=_plex_seasons((1, "season-1"), (2, "season-2"))),
            StubResponse(content=_plex_episodes(("episode-1", 1, 1), ("episode-2", 2, 1))),
        ]
    )

    result = plex.get_next_plex_episode(
        "episode-1",
        "http://plex.local:32400",
        "plex-token",
        stay_in_season=True,
        timeout=7,
        request_client=request_client,
        logger=MagicMock(),
    )

    assert result == "episode-2"
    _assert_auth_and_timeout(request_client.calls, {"X-Plex-Token": "plex-token"}, 7)


def test_plex_next_episode_crosses_to_first_item_in_next_season():
    plex = _client_module("plex")
    request_client = StubRequests(
        get=[
            StubResponse(content=_plex_metadata(current="episode-2")),
            StubResponse(content=_plex_seasons((1, "season-1"), (2, "season-2"))),
            StubResponse(content=_plex_episodes(("episode-1", 1, 1), ("episode-2", 2, 1))),
            StubResponse(content=_plex_episodes(("episode-3", 1, 2), ("episode-4", 2, 2))),
        ]
    )

    result = plex.get_next_plex_episode(
        "episode-2",
        "http://plex.local:32400",
        "plex-token",
        stay_in_season=False,
        timeout=7,
        request_client=request_client,
        logger=MagicMock(),
    )

    assert result == "episode-3"
    assert request_client.calls[-1][1] == "http://plex.local:32400/library/metadata/season-2/children"


def test_plex_next_episode_stops_at_season_boundary_when_requested():
    plex = _client_module("plex")
    request_client = StubRequests(
        get=[
            StubResponse(content=_plex_metadata(current="episode-2")),
            StubResponse(content=_plex_seasons((1, "season-1"), (2, "season-2"))),
            StubResponse(content=_plex_episodes(("episode-1", 1, 1), ("episode-2", 2, 1))),
        ]
    )

    result = plex.get_next_plex_episode(
        "episode-2",
        "http://plex.local:32400",
        "plex-token",
        stay_in_season=True,
        timeout=7,
        request_client=request_client,
        logger=MagicMock(),
    )

    assert result is None
    assert len(request_client.calls) == 3


def test_plex_next_episode_returns_none_on_request_failure():
    plex = _client_module("plex")
    request_client = StubRequests(get=[RequestFailure("offline")])
    logger = MagicMock()

    result = plex.get_next_plex_episode(
        "episode-1",
        "http://plex.local:32400",
        "plex-token",
        stay_in_season=False,
        timeout=7,
        request_client=request_client,
        logger=logger,
    )

    assert result is None
    logger.error.assert_called_once_with("Error fetching data from Plex: offline")


def test_plex_traversal_fallback_uses_canonical_file_lookup(monkeypatch):
    plex = _client_module("plex")
    request_client = StubRequests(
        get=[
            StubResponse(content=_plex_metadata()),
            StubResponse(content=_plex_seasons((1, "season-1"))),
            StubResponse(content=_plex_episodes(("episode-1", 1, 1), ("episode-3", 3, 1), ("episode-4", 4, 1))),
        ]
    )
    lookup = MagicMock(return_value="/media/episode-1.mkv")
    monkeypatch.setattr(plex, "get_plex_file_name", lookup)

    result = plex.get_next_plex_episode(
        "episode-1",
        "http://plex.local:32400",
        "plex-token",
        stay_in_season=True,
        timeout=7,
        request_client=request_client,
        logger=MagicMock(),
    )

    assert result is None
    lookup.assert_called_once_with(
        "episode-1",
        "http://plex.local:32400",
        "plex-token",
        timeout=7,
        request_client=request_client,
        logger=ANY,
    )


def test_jellyfin_admin_selects_first_administrator():
    jellyfin = _client_module("jellyfin")
    users = [
        {"Id": "viewer", "Policy": {"IsAdministrator": False}},
        {"Id": "admin-1", "Policy": {"IsAdministrator": True}},
        {"Id": "admin-2", "Policy": {"IsAdministrator": True}},
    ]

    assert jellyfin.get_jellyfin_admin(users) == "admin-1"


def test_jellyfin_admin_rejects_user_list_without_administrator():
    jellyfin = _client_module("jellyfin")

    with pytest.raises(Exception, match="Unable to find administrator user in Jellyfin"):
        jellyfin.get_jellyfin_admin([{"Id": "viewer", "Policy": {"IsAdministrator": False}}])


def test_jellyfin_file_lookup_propagates_auth_and_timeout():
    jellyfin = _client_module("jellyfin")
    request_client = StubRequests(
        get=[
            StubResponse(content=json.dumps([{"Id": "admin", "Policy": {"IsAdministrator": True}}]).encode()),
            StubResponse(content=json.dumps({"Path": "/media/movie.mkv"}).encode()),
        ]
    )

    result = jellyfin.get_jellyfin_file_name(
        "item-1",
        "http://jellyfin.local:8096",
        "jellyfin-token",
        timeout=8.5,
        request_client=request_client,
        logger=MagicMock(),
    )

    assert result == "/media/movie.mkv"
    assert [call[1] for call in request_client.calls] == [
        "http://jellyfin.local:8096/Users",
        "http://jellyfin.local:8096/Users/admin/Items/item-1",
    ]
    _assert_auth_and_timeout(
        request_client.calls,
        {"Authorization": "MediaBrowser Token=jellyfin-token"},
        8.5,
    )


def test_jellyfin_file_lookup_rejects_item_error_status():
    jellyfin = _client_module("jellyfin")
    request_client = StubRequests(
        get=[
            StubResponse(content=json.dumps([{"Id": "admin", "Policy": {"IsAdministrator": True}}]).encode()),
            StubResponse(status_code=404),
        ]
    )

    with pytest.raises(Exception, match="Error: 404"):
        jellyfin.get_jellyfin_file_name(
            "missing",
            "http://jellyfin.local:8096",
            "jellyfin-token",
            timeout=8.5,
            request_client=request_client,
            logger=MagicMock(),
        )


def test_jellyfin_refresh_uses_authenticated_post_and_accepts_204():
    jellyfin = _client_module("jellyfin")
    request_client = StubRequests(post=[StubResponse(status_code=204)])
    logger = MagicMock()

    result = jellyfin.refresh_jellyfin_metadata(
        "item-1",
        "http://jellyfin.local:8096",
        "jellyfin-token",
        timeout=9,
        request_client=request_client,
        logger=logger,
    )

    assert result is None
    assert request_client.calls == [
        (
            "post",
            "http://jellyfin.local:8096/Items/item-1/Refresh?MetadataRefreshMode=FullRefresh",
            {"headers": {"Authorization": "MediaBrowser Token=jellyfin-token"}, "timeout": 9},
        )
    ]
    logger.info.assert_called_once_with("Metadata refresh queued successfully.")


def test_jellyfin_refresh_rejects_non_204_status():
    jellyfin = _client_module("jellyfin")
    request_client = StubRequests(post=[StubResponse(status_code=200)])

    with pytest.raises(Exception, match="Error refreshing metadata: 200"):
        jellyfin.refresh_jellyfin_metadata(
            "item-1",
            "http://jellyfin.local:8096",
            "jellyfin-token",
            timeout=9,
            request_client=request_client,
            logger=MagicMock(),
        )


def test_facade_next_episode_wrapper_passes_current_runtime_dependencies(monkeypatch):
    plex = _client_module("plex")
    delegate = MagicMock(return_value="episode-2")
    monkeypatch.setattr(plex, "get_next_plex_episode", delegate)
    monkeypatch.setattr(subgen, "plexserver", "http://plex.facade:32400")
    monkeypatch.setattr(subgen, "plextoken", "facade-token")
    monkeypatch.setattr(subgen, "http_timeout", 12.5)

    result = subgen.get_next_plex_episode("episode-1", stay_in_season=True)

    assert result == "episode-2"
    delegate.assert_called_once_with(
        "episode-1",
        "http://plex.facade:32400",
        "facade-token",
        stay_in_season=True,
        timeout=12.5,
        request_client=subgen.requests,
        logger=subgen.logging,
    )
