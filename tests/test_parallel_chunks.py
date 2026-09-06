"""Deterministic asynchronous event simulations, not multi-GPU benchmarks."""

from dataclasses import replace

import pytest

from subgen_core.segmentation import ParallelChunkCoordinator, SegmentationError, StagedChunk
from subgen_core.segmented_result import PendingChunkStore, SegmentJournalError


def output(assignment):
    window = assignment.window
    offset = window.core_start - window.extract_start
    return {"language": "en", "segments": [
        {"start": offset + 0.1, "end": offset + 0.9, "text": f"section {window.core_start}"},
    ]}


@pytest.fixture
def setup_scheduler(tmp_path):
    stores = []
    schedulers = []

    def create(count=2, duration=100):
        store = PendingChunkStore(directory=tmp_path, maximum_entries=2 * count)
        stores.append(store)
        persisted = []
        scheduler = ParallelChunkCoordinator(
            media_duration=duration, workers=tuple(f"gpu:{i}" for i in range(count)),
            store_result=store.store, read_result=store.read, discard_result=store.discard,
            persist_chunk=lambda w, result, state: persisted.append((w, result, state)),
        )
        schedulers.append(scheduler)
        return scheduler, persisted, store

    yield create
    for scheduler in schedulers:
        scheduler.close()
    for store in stores:
        store.close()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("count", [2, 3, 4, 5, 32])
def test_reverse_completion_commits_in_source_order(setup_scheduler, count):
    scheduler, persisted, _ = setup_scheduler(count, 10 * count)
    assigned = [scheduler.claim(w, core_seconds=10) for w in scheduler.workers]
    for assignment in reversed(assigned):
        scheduler.completed(assignment, output(assignment))
        scheduler.commit_ready(check_before_commit=lambda: True)
        if assignment is not assigned[0]:
            assert persisted == []
    assert scheduler.state.complete
    assert [w.ordinal for w, _, _ in persisted] == list(range(count))
    assert [w.core_start for w, _, _ in persisted] == list(range(0, 10 * count, 10))


def test_fast_worker_gets_more_work_but_lookahead_is_bounded(setup_scheduler):
    scheduler, persisted, _ = setup_scheduler()
    slow = scheduler.claim("gpu:0", core_seconds=10)
    for start in (10, 20, 30):
        fast = scheduler.claim("gpu:1", core_seconds=10)
        assert fast.window.core_start == start
        scheduler.completed(fast, output(fast))
    assert scheduler.pending_count == scheduler.maximum_pending == 4
    assert scheduler.claim("gpu:1", core_seconds=10) is None
    scheduler.completed(slow, output(slow))
    scheduler.commit_ready(check_before_commit=lambda: True)
    assert len(persisted) == 4
    assert scheduler.claim("gpu:1", core_seconds=10).window.core_start == 40


def test_pressure_retry_shrinks_without_overlapping_other_workers(setup_scheduler):
    scheduler, persisted, _ = setup_scheduler(duration=30)
    first = scheduler.claim("gpu:0", core_seconds=20)
    second = scheduler.claim("gpu:1", core_seconds=10)
    scheduler.yielded(first)
    scheduler.completed(second, output(second))
    for start in (0, 5, 10, 15):
        retry = scheduler.claim("gpu:0", core_seconds=5)
        assert (retry.window.core_start, retry.window.core_end) == (start, start + 5)
        scheduler.completed(retry, output(retry))
        scheduler.commit_ready(check_before_commit=lambda: True)
    assert scheduler.state.complete
    assert [(w.core_start, w.core_end) for w, _, _ in persisted] == [
        (0, 5), (5, 10), (10, 15), (15, 20), (20, 30),
    ]
    assert [w.ordinal for w, _, _ in persisted] == list(range(5))


def test_late_duplicate_and_forged_completions_cannot_steal_retry(setup_scheduler):
    scheduler, _, _ = setup_scheduler()
    first = scheduler.claim("gpu:0", core_seconds=20)
    scheduler.yielded(first)
    retry = scheduler.claim("gpu:0", core_seconds=5)
    for stale in (first, replace(retry)):
        with pytest.raises(SegmentationError, match="Late, duplicate"):
            scheduler.completed(stale, output(stale))
    scheduler.completed(retry, output(retry))
    with pytest.raises(SegmentationError, match="Late, duplicate"):
        scheduler.completed(retry, output(retry))


def test_pressure_closes_dispatch_and_commit_without_losing_finished_output(setup_scheduler):
    scheduler, persisted, _ = setup_scheduler()
    first = scheduler.claim("gpu:0", core_seconds=10)
    scheduler.completed(first, output(first))
    scheduler.pause_dispatch()
    assert scheduler.claim("gpu:1", core_seconds=10) is None
    assert scheduler.commit_ready(check_before_commit=lambda: False) == ()
    assert persisted == []
    scheduler.pause_dispatch(False)
    scheduler.commit_ready(check_before_commit=lambda: True)
    assert scheduler.state.cursor == 10


def test_failed_persistence_does_not_advance_or_discard_chunk(setup_scheduler):
    scheduler, persisted, _ = setup_scheduler()
    first = scheduler.claim("gpu:0", core_seconds=10)
    scheduler.completed(first, output(first))
    persist = scheduler._persist

    def fail(*args):
        raise OSError("disk full")

    scheduler._persist = fail
    with pytest.raises(OSError, match="disk full"):
        scheduler.commit_ready(check_before_commit=lambda: True)
    assert scheduler.state.cursor == 0 and scheduler.pending_count == 1
    scheduler._persist = persist
    scheduler.commit_ready(check_before_commit=lambda: True)
    assert len(persisted) == 1


def test_pending_store_enforces_record_count_size_and_cleanup(tmp_path):
    store = PendingChunkStore(directory=tmp_path, maximum_entries=1, maximum_record_bytes=64)
    with pytest.raises(SegmentJournalError, match="allowance"):
        store.store(StagedChunk("en", ({"text": "x" * 100},)))
    item = StagedChunk("en", ())
    handle = store.store(item)
    assert store.read(handle) == item
    with pytest.raises(SegmentJournalError, match="full"):
        store.store(item)
    store.discard(handle)
    with pytest.raises(SegmentJournalError, match="unavailable"):
        store.read(handle)
    store.close()
    with pytest.raises(SegmentJournalError, match="closed"):
        store.store(item)
    assert list(tmp_path.iterdir()) == []
