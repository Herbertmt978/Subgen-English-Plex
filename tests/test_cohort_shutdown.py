"""Shutdown uses existing file ownership; no model/backend is allocated."""
import threading
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from subgen_core import model_runtime


def runtime():
    active = SimpleNamespace(state='ready', cancel=MagicMock())
    active.release = MagicMock(side_effect=lambda **kw:setattr(active, 'state', 'released'))
    return SimpleNamespace(cohort_plan_provider=lambda:None,
        active_file_cohort=active, model_runtime_cancel_event=threading.Event(),
        model_runtime_condition=threading.Condition(), cohort_file_token=None)


def test_legacy_lifespan_is_unchanged():
    r = SimpleNamespace()
    model_runtime.validate_cohort_startup(r)
    assert model_runtime.shutdown_cohort_runtime(r) is False


@pytest.mark.parametrize('failure', [True, False])
def test_shutdown_also_drains_provider_discovery(failure):
    r = runtime()
    r.cohort_plan_provider = SimpleNamespace(release=MagicMock(
        side_effect=RuntimeError('discovery cleanup') if failure else None))
    if failure:
        with pytest.raises(model_runtime.ModelReleaseError, match='unconfirmed'):
            model_runtime.shutdown_cohort_runtime(r)
        assert r.cohort_cleanup_error is not None
    else:
        assert model_runtime.shutdown_cohort_runtime(r)
    r.cohort_plan_provider.release.assert_called_once()


def test_idle_handles_are_released_and_shutdown_is_repeatable():
    r = runtime()
    active = r.active_file_cohort
    assert model_runtime.shutdown_cohort_runtime(r)
    assert r.model_runtime_cancel_event.is_set()
    active.cancel.assert_called_once_with(reason='stop')
    active.release.assert_called_once()
    assert r.active_file_cohort is None
    assert model_runtime.shutdown_cohort_runtime(r)
    model_runtime.validate_cohort_startup(r)


def test_shutdown_waits_for_file_cleanup_before_idle_release():
    r = runtime()
    token = r.cohort_file_token = object()
    active = r.active_file_cohort
    stopped = threading.Event()
    def finish_file():
        assert r.model_runtime_cancel_event.wait(2)
        assert active.release.call_count == 0
        model_runtime.release_cohort_file(r, token)
        stopped.set()
    worker = threading.Thread(target=finish_file)
    worker.start()
    try:
        assert model_runtime.shutdown_cohort_runtime(r, timeout=2)
        assert stopped.wait(2)
        active.release.assert_called_once()
    finally:
        worker.join(2)
    assert not worker.is_alive()


def test_unfinished_file_retains_token_and_handles_and_blocks_restart():
    r = runtime()
    token = r.cohort_file_token = object()
    active = r.active_file_cohort
    with pytest.raises(model_runtime.ModelReleaseError, match='ownership retained'):
        model_runtime.shutdown_cohort_runtime(r, timeout=0.01)
    assert r.cohort_file_token is token and r.active_file_cohort is active
    active.release.assert_not_called()
    with pytest.raises(model_runtime.ModelReleaseError, match='startup is blocked'):
        model_runtime.validate_cohort_startup(r)


@pytest.mark.parametrize('failure', [True, False])
def test_unconfirmed_release_keeps_handle_and_blocks_restart(failure):
    r = runtime()
    active = r.active_file_cohort
    active.release = MagicMock(side_effect=OSError('test') if failure else None)
    with pytest.raises(model_runtime.ModelReleaseError):
        model_runtime.shutdown_cohort_runtime(r)
    assert r.active_file_cohort is active
    with pytest.raises(model_runtime.ModelReleaseError):
        model_runtime.validate_cohort_startup(r)


@pytest.mark.parametrize('timeout', [0, -1, True, float('nan'), float('inf'), '30'])
def test_invalid_timeout_does_not_change_runtime(timeout):
    r = runtime()
    with pytest.raises(ValueError):
        model_runtime.shutdown_cohort_runtime(r, timeout=timeout)
    assert not r.model_runtime_cancel_event.is_set()


@pytest.mark.parametrize('failure', [False, True])
def test_application_lifespan_drains_cohort_and_always_stops_inventory(monkeypatch, failure):
    import subgen
    inventory = MagicMock()
    monkeypatch.setattr(subgen, 'inventory_coordinator', inventory)
    monkeypatch.setattr(subgen, 'memory_pressure_yield', False)
    monkeypatch.setattr(subgen, 'transcribe_folders', '')
    monkeypatch.setattr(subgen, 'model_idle_observer_thread', None)
    monkeypatch.setattr(subgen, 'model_runtime_cancel_event', threading.Event())
    monkeypatch.setattr(subgen, 'cohort_plan_provider', lambda:None, raising=False)
    for name in ('active_file_cohort', 'cohort_file_token', 'cohort_cleanup_error'):
        monkeypatch.setattr(subgen, name, None, raising=False)
    calls = []
    def drain(r):
        assert r.model_runtime_cancel_event.is_set()
        calls.append(r)
        if failure:
            raise model_runtime.ModelReleaseError('test cleanup failure')
    monkeypatch.setattr(model_runtime, 'shutdown_cohort_runtime', drain)
    async def lifecycle():
        async with subgen.lifespan(subgen.app):
            assert not subgen.model_runtime_cancel_event.is_set()
    if failure:
        with pytest.raises(model_runtime.ModelReleaseError):
            asyncio.run(lifecycle())
    else:
        asyncio.run(lifecycle())
    assert len(calls) == 1
    inventory.stop.assert_called_once()


def test_lifespan_does_not_clear_stop_after_failed_cleanup(monkeypatch):
    import subgen
    stop = threading.Event()
    stop.set()
    inventory = MagicMock()
    monkeypatch.setattr(subgen, 'inventory_coordinator', inventory)
    monkeypatch.setattr(subgen, 'model_runtime_cancel_event', stop)
    monkeypatch.setattr(subgen, 'cohort_plan_provider', lambda:None, raising=False)
    monkeypatch.setattr(subgen, 'cohort_cleanup_error', RuntimeError('test'), raising=False)
    async def lifecycle():
        async with subgen.lifespan(subgen.app):
            pytest.fail('incomplete cleanup must block startup')
    with pytest.raises(model_runtime.ModelReleaseError):
        asyncio.run(lifecycle())
    assert stop.is_set()
    inventory.start.assert_not_called()
