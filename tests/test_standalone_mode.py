"""Contracts for using Subgen without Plex, Jellyfin, Emby, or an *Arr stack."""

import importlib
import importlib.util
import logging
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from subgen_failure_markers import (
    DEFAULT_MARKER_REGISTRY_PATH,
    FailureMarkerReader,
)

ROOT = Path(__file__).resolve().parents[1]
MUTATED_LOGGER_NAMES = (
    "multipart",
    "urllib3",
    "watchfiles",
    "asyncio",
    "httpcore",
    "httpx",
    "huggingface_hub",
)


@pytest.fixture()
def runtime(monkeypatch):
    """Load an isolated facade instance without starting its daemon workers."""
    yield _load_isolated_runtime(monkeypatch, "_subgen_standalone_contract")


def _disable_integrations(monkeypatch, runtime):
    monkeypatch.setattr(runtime, "plexserver", "")
    monkeypatch.setattr(runtime, "plextoken", "")
    monkeypatch.setattr(runtime, "jellyfinserver", "")
    monkeypatch.setattr(runtime, "jellyfintoken", "")
    monkeypatch.setattr(runtime, "webhook_url_completed", "")

    def unexpected_request(*args, **kwargs):
        raise AssertionError("standalone scanning must not call a media server")

    for method in ("get", "put", "post"):
        monkeypatch.setattr(runtime.requests, method, unexpected_request)


def _configure_scan(monkeypatch, runtime, *, monitor=False, skip_startup=False):
    monkeypatch.setattr(runtime, "monitor", monitor)
    monkeypatch.setattr(runtime, "skip_startup_scan", skip_startup)
    monkeypatch.setattr(runtime, "use_path_mapping", False)
    monkeypatch.setattr(runtime, "transcribe_or_translate", "translate")


def _install_real_queue_pipeline(monkeypatch, runtime):
    queue = runtime.DeduplicatedQueue()
    spanish_track = {
        "index": 0,
        "language": runtime.LanguageCode.SPANISH,
        "default": True,
    }
    evidence = runtime._media.ValidatorEvidence(
        runtime._media.ValidatorOutcome.AUDIO_PRESENT
    )
    validation = runtime._media.MediaValidation(
        runtime._media.MediaOutcome.VALID_AUDIO,
        evidence,
        evidence,
        duration_seconds=60.0,
        audio_tracks=(spanish_track,),
    )
    monkeypatch.setattr(runtime, "task_queue", queue)
    monkeypatch.setattr(runtime, "validate_media", lambda path: validation)
    monkeypatch.setattr(runtime, "preferred_audio_languages", [])
    monkeypatch.setattr(runtime, "force_detected_language_to", runtime.LanguageCode.NONE)
    monkeypatch.setattr(runtime, "should_whisper_detect_audio_language", False)
    monkeypatch.setattr(runtime, "skip_if_target_subtitle_exists", False)
    monkeypatch.setattr(runtime, "skip_if_internal_sub_language", runtime.LanguageCode.NONE)
    monkeypatch.setattr(runtime, "skip_subtitle_languages", [])
    monkeypatch.setattr(runtime, "skip_if_external_sub_exists", False)
    monkeypatch.setattr(runtime, "skip_unknown_language", False)
    monkeypatch.setattr(runtime, "skip_if_no_audio_language_but_subtitles_exist", False)
    monkeypatch.setattr(runtime, "limit_to_preferred_audio_languages", False)
    monkeypatch.setattr(runtime, "skip_audio_languages", [])
    monkeypatch.setattr(runtime, "subtitle_language_name", "")
    return queue


def _load_isolated_runtime(monkeypatch, module_name):
    root_logger = logging.getLogger()
    root_level = root_logger.level
    root_handlers = list(root_logger.handlers)
    root_handler_ids = {id(handler) for handler in root_handlers}
    handler_filters = [
        (handler, list(handler.filters))
        for handler in root_handlers
    ]
    named_logger_levels = {
        name: logging.getLogger(name).level
        for name in MUTATED_LOGGER_NAMES
    }

    spec = importlib.util.spec_from_file_location(module_name, ROOT / "subgen_override.py")
    assert spec is not None and spec.loader is not None
    runtime = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, runtime)
    try:
        with patch.object(threading.Thread, "start", return_value=None):
            spec.loader.exec_module(runtime)
    finally:
        for handler in list(root_logger.handlers):
            if id(handler) not in root_handler_ids:
                root_logger.removeHandler(handler)
                handler.close()
        root_logger.handlers[:] = root_handlers
        root_logger.setLevel(root_level)
        for handler, filters in handler_filters:
            handler.filters[:] = filters
        for name, level in named_logger_levels.items():
            logging.getLogger(name).setLevel(level)

    return runtime


def _logging_state():
    root_logger = logging.getLogger()
    return (
        root_logger.level,
        tuple(root_logger.handlers),
        tuple((handler, tuple(handler.filters)) for handler in root_logger.handlers),
        tuple(
            (name, logging.getLogger(name).level)
            for name in MUTATED_LOGGER_NAMES
        ),
    )


def test_isolated_runtime_load_restores_logging_state(monkeypatch):
    root_logger = logging.getLogger()
    existing_filters = {
        handler: list(handler.filters)
        for handler in root_logger.handlers
    }
    sentinel_handler = logging.NullHandler()
    sentinel_filter = logging.Filter("standalone-loader-sentinel")
    sentinel_handler.addFilter(sentinel_filter)
    monkeypatch.setattr(
        root_logger,
        "handlers",
        [*root_logger.handlers, sentinel_handler],
    )
    monkeypatch.setattr(root_logger, "level", logging.ERROR)
    for name in MUTATED_LOGGER_NAMES:
        monkeypatch.setattr(logging.getLogger(name), "level", logging.CRITICAL)

    before = _logging_state()
    try:
        _load_isolated_runtime(monkeypatch, "_subgen_logging_state_contract")
        assert _logging_state() == before
    finally:
        for handler, filters in existing_filters.items():
            handler.filters[:] = filters
        sentinel_handler.filters[:] = [sentinel_filter]
        sentinel_handler.close()


def test_unset_media_server_environment_defaults_to_blank(monkeypatch):
    for name in (
        "PLEX_SERVER",
        "PLEXSERVER",
        "PLEX_TOKEN",
        "PLEXTOKEN",
        "JELLYFIN_SERVER",
        "JELLYFINSERVER",
        "JELLYFIN_TOKEN",
        "JELLYFINTOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    runtime = _load_isolated_runtime(monkeypatch, "_subgen_unset_media_server_contract")

    assert (
        runtime.plexserver,
        runtime.plextoken,
        runtime.jellyfinserver,
        runtime.jellyfintoken,
    ) == ("", "", "", "")


def test_failure_marker_runtime_defaults_are_enabled(monkeypatch):
    monkeypatch.delenv("SKIP_MARKED_FAILED_FILES", raising=False)
    monkeypatch.delenv("SUBGEN_FAILURE_MARKER_PATH", raising=False)

    runtime = _load_isolated_runtime(monkeypatch, "_subgen_failure_marker_contract")

    assert runtime.skip_marked_failed_files is True
    assert runtime.failure_marker_registry_path == DEFAULT_MARKER_REGISTRY_PATH
    assert isinstance(runtime.failure_marker_reader, FailureMarkerReader)


def test_blank_media_server_environment_queues_standalone_translation(monkeypatch, tmp_path):
    for name in (
        "PLEX_SERVER",
        "PLEX_TOKEN",
        "JELLYFIN_SERVER",
        "JELLYFIN_TOKEN",
    ):
        monkeypatch.setenv(name, "")
    for name, value in {
        "PLEXSERVER": "http://legacy-plex.invalid:32400",
        "PLEXTOKEN": "legacy-plex-token",
        "JELLYFINSERVER": "http://legacy-jellyfin.invalid:8096",
        "JELLYFINTOKEN": "legacy-jellyfin-token",
    }.items():
        monkeypatch.setenv(name, value)

    runtime = _load_isolated_runtime(monkeypatch, "_subgen_blank_media_server_contract")

    assert (
        runtime.plexserver,
        runtime.plextoken,
        runtime.jellyfinserver,
        runtime.jellyfintoken,
    ) == ("", "", "", "")

    media_file = tmp_path / "standalone-foreign-language-film.mkv"
    media_file.touch()
    _configure_scan(monkeypatch, runtime)
    queue = _install_real_queue_pipeline(monkeypatch, runtime)

    def unexpected_request(*args, **kwargs):
        raise AssertionError("blank media-server settings must not make HTTP requests")

    for method in ("get", "put", "post"):
        monkeypatch.setattr(runtime.requests, method, unexpected_request)
    monkeypatch.setattr(runtime.requests.sessions.Session, "request", unexpected_request)

    runtime.transcribe_existing(str(media_file))

    task = queue.get(block=False)
    assert task["path"] == str(media_file)
    assert task["transcribe_or_translate"] == "translate"
    queue.mark_done(task)


def test_direct_file_path_queues_translation_without_integrations(monkeypatch, tmp_path, runtime):
    media_file = tmp_path / "foreign-language-film.mkv"
    media_file.touch()

    _disable_integrations(monkeypatch, runtime)
    _configure_scan(monkeypatch, runtime)
    queue = _install_real_queue_pipeline(monkeypatch, runtime)

    runtime.transcribe_existing(str(media_file))

    assert queue.get_queued_tasks() == [str(media_file)]
    task = queue.get(block=False)
    assert task["path"] == str(media_file)
    assert task["transcribe_or_translate"] == "translate"
    assert task["force_language"] == runtime.LanguageCode.SPANISH
    assert task["audio_track_index"] == 0
    queue.mark_done(task)


def test_startup_folder_scan_queues_media_without_integrations(monkeypatch, tmp_path, runtime):
    media_file = tmp_path / "episode.mkv"
    media_file.touch()

    _disable_integrations(monkeypatch, runtime)
    _configure_scan(monkeypatch, runtime)
    queue = _install_real_queue_pipeline(monkeypatch, runtime)

    runtime.transcribe_existing(str(tmp_path))

    assert queue.get_queued_tasks() == [str(media_file)]
    task = queue.get(block=False)
    assert task["path"] == str(media_file)
    assert task["transcribe_or_translate"] == "translate"
    assert task["force_language"] == runtime.LanguageCode.SPANISH
    queue.mark_done(task)


def test_monitor_schedules_directories_but_not_direct_files(monkeypatch, tmp_path, runtime):
    watched_directory = tmp_path / "library"
    watched_directory.mkdir()
    direct_file = tmp_path / "single.mkv"
    direct_file.touch()
    observer = MagicMock()

    _disable_integrations(monkeypatch, runtime)
    _configure_scan(monkeypatch, runtime, monitor=True, skip_startup=True)
    monkeypatch.setattr(runtime, "Observer", lambda: observer)

    runtime.transcribe_existing(f"{watched_directory}|{direct_file}")

    observer.schedule.assert_called_once()
    _, scheduled_path = observer.schedule.call_args.args[:2]
    assert scheduled_path == str(watched_directory)
    assert observer.schedule.call_args.kwargs == {"recursive": True}
    observer.start.assert_called_once_with()


def test_canonical_queue_existing_has_no_startup_or_monitor_dependency(tmp_path):
    scanner = importlib.import_module("subgen_core.scanner")
    library = tmp_path / "library"
    library.mkdir()
    nested_file = library / "episode.mkv"
    nested_file.touch()
    direct_file = tmp_path / "movie.mkv"
    direct_file.touch()
    queued = []
    runtime = SimpleNamespace(
        os=os,
        SKIP_MARKER=scanner.SKIP_MARKER,
        logging=MagicMock(),
        path_mapping=lambda path: f"mapped:{path}",
        transcribe_or_translate="translate",
        gen_subtitles_queue=lambda *args: queued.append(args),
    )

    scanner.queue_existing(
        runtime,
        f"{library}|{direct_file}",
        forceLanguage=scanner.LanguageCode.FRENCH,
    )

    assert queued == [
        (f"mapped:{nested_file}", "translate", scanner.LanguageCode.FRENCH),
        (f"mapped:{direct_file}", "translate", scanner.LanguageCode.FRENCH),
    ]


def test_facade_file_stability_uses_rebound_os_and_time(monkeypatch, runtime):
    sizes = iter((1024, 1024))
    sleeps = []
    fake_path = SimpleNamespace(
        exists=lambda _path: True,
        getsize=lambda _path: next(sizes),
    )
    monkeypatch.setattr(runtime, "os", SimpleNamespace(path=fake_path))
    monkeypatch.setattr(
        runtime,
        "time",
        SimpleNamespace(sleep=lambda delay: sleeps.append(delay)),
    )

    assert runtime.is_file_stable("/virtual/movie.mkv", wait_time=0.25) is True
    assert sleeps == [0.25]


def test_facade_startup_scan_uses_rebound_os(monkeypatch, runtime):
    queued = []
    fake_path = SimpleNamespace(
        join=lambda root, name: f"{root}/{name}",
        isfile=lambda _path: False,
        isdir=lambda _path: False,
    )
    fake_os = SimpleNamespace(
        path=fake_path,
        walk=lambda _path: [("/virtual/library", [], ["episode.mkv"])],
    )
    _configure_scan(monkeypatch, runtime)
    monkeypatch.setattr(runtime, "os", fake_os)
    monkeypatch.setattr(
        runtime,
        "gen_subtitles_queue",
        lambda *args: queued.append(args),
    )

    runtime.transcribe_existing("/virtual/library")

    assert queued == [
        (
            "/virtual/library/episode.mkv",
            "translate",
            runtime.LanguageCode.NONE,
        )
    ]


def test_canonical_scanner_direct_file_uses_runtime_callbacks(tmp_path):
    scanner = importlib.import_module("subgen_core.scanner")
    media_file = tmp_path / "direct.mkv"
    media_file.touch()
    queued = []
    runtime = SimpleNamespace(
        os=os,
        SKIP_MARKER=scanner.SKIP_MARKER,
        skip_startup_scan=False,
        logging=MagicMock(),
        path_mapping=lambda path: f"mapped:{path}",
        transcribe_or_translate="translate",
        gen_subtitles_queue=lambda *args: queued.append(args),
        has_audio=lambda _path: (_ for _ in ()).throw(
            AssertionError("scanner must delegate media probing to the queue boundary")
        ),
        monitor=False,
    )

    scanner.transcribe_existing(runtime, str(media_file))

    assert queued == [
        (f"mapped:{media_file}", "translate", scanner.LanguageCode.NONE),
    ]


def test_canonical_handler_queues_through_runtime(tmp_path):
    scanner = importlib.import_module("subgen_core.scanner")
    media_file = tmp_path / "new.mkv"
    media_file.touch()
    queued = []
    runtime = SimpleNamespace(
        logging=MagicMock(),
        _is_in_skipped_dir=lambda _path: False,
        has_audio=lambda _path: (_ for _ in ()).throw(
            AssertionError("scanner must delegate media probing to the queue boundary")
        ),
        path_mapping=lambda path: f"mapped:{path}",
        gen_subtitles_queue=lambda *args: queued.append(args),
        transcribe_or_translate="translate",
    )
    handler = scanner.NewFileHandler(runtime)

    handler.create_subtitle(SimpleNamespace(is_directory=False, src_path=str(media_file)))

    assert queued == [(f"mapped:{media_file}", "translate")]


def test_public_route_inventory_remains_compatible(runtime):
    expected = {
        ("GET", "/"),
        ("GET", "/status"),
        ("GET", "/plex"),
        ("GET", "/webhook"),
        ("GET", "/jellyfin"),
        ("GET", "/asr"),
        ("GET", "/emby"),
        ("GET", "/detect-language"),
        ("GET", "/tautulli"),
        ("POST", "/tautulli"),
        ("POST", "/plex"),
        ("POST", "/jellyfin"),
        ("POST", "/emby"),
        ("POST", "/batch"),
        ("POST", "/asr"),
        ("POST", "/detect-language"),
        ("POST", "/v1/audio/transcriptions"),
        ("POST", "/v1/audio/translations"),
    }
    framework_paths = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    actual = [
        (method, route.path)
        for route in runtime.app.routes
        for method in getattr(route, "methods", set())
        if route.path not in framework_paths
    ]

    assert len(actual) == len(set(actual)), f"duplicate public route registrations: {actual}"
    assert set(actual) == expected
