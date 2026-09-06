"""File-scoped asynchronous orchestration of the canonical chunk coordinator.

No library queue or memory policy lives here. The composition root supplies
verified cohorts, adaptive budgets, cancellable extraction and recovery checks.
Only this thread mutates scheduling/journal state; progress callbacks may run
on inference threads and must not mutate those owners.
"""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, CancelledError
import math
import time

from .cohort_runtime import CohortModelRuntime, CohortCancelled, CohortReleaseError
from .media import MediaProcessReleaseUnconfirmed
from .resource_management import AdaptiveChunkState, MemoryPressureYield
from .resident_worker import WorkerAllocationFailure
from .segmentation import ParallelChunkCoordinator, _external_pressure_recovered


class CohortLoadError(RuntimeError):
    """Backend/model setup failed before media inference; not a bad media file."""


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('Parallel operation deadlines must be finite and positive')
    return float(value)


def _without_tracebacks(error):
    # An exception chain from inference can retain its audio/model locals even
    # after the future unwinds. Keep diagnostic types/causes, not their frames.
    seen, pending = set(), [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        current.__traceback__ = None
        pending.extend((current.__cause__, current.__context__))
    return error


def _terminal_errors(errors):
    return [e for e in errors if not isinstance(e,
        (MemoryPressureYield, CohortCancelled, CancelledError, WorkerAllocationFailure))]


def run_parallel_segmented_transcription(*, media_duration, adaptive_by_worker,
        cohort_factory, extract_chunk, transcription_options,
        store_result, read_result, discard_result, persist_chunk, finalize_assembly,
        check_cancelled, check_healthy, wait_for_recovery, on_event=lambda *a, **k: None,
        load_timeout=120, chunk_timeout=3600, release_timeout=30, transform_result=None):
    """Run one fixed-model file, retaining staged/committed work across pressure.

    extract_chunk(window, timeout_seconds=..., check_cancelled=...) must honor
    both bounds; use transcription.extract_local_audio_chunk in production.
    The cohort factory's owner must retain the active cohort until its release
    is confirmed, including if a broken backend fails to unwind by the deadline.
    """
    load_timeout, chunk_timeout, release_timeout = map(_seconds, (load_timeout, chunk_timeout, release_timeout))
    if not isinstance(adaptive_by_worker, dict) or not 1 <= len(adaptive_by_worker) <= 32:
        raise ValueError('Parallel transcription requires bounded worker budgets')
    if any(type(value) is not AdaptiveChunkState for value in adaptive_by_worker.values()):
        raise TypeError('Use the canonical adaptive chunk budget')
    if not isinstance(transcription_options, dict) or 'progress_callback' in transcription_options:
        raise ValueError('Parallel transcription options cannot replace progress ownership')
    if not all(callable(v) for v in (cohort_factory, extract_chunk, check_cancelled, check_healthy,
                                    wait_for_recovery, finalize_assembly, on_event)):
        raise TypeError('Parallel runtime dependencies must be callable')
    if transform_result is not None and not callable(transform_result):
        raise TypeError('Chunk result transformation must be callable')
    options = dict(transcription_options)
    workers = tuple(adaptive_by_worker)
    coordinator = ParallelChunkCoordinator(media_duration=media_duration, workers=workers,
        store_result=store_result, read_result=read_result, discard_result=discard_result,
        persist_chunk=persist_chunk)
    pool = ThreadPoolExecutor(max_workers=len(workers), thread_name_prefix='subgen-chunk')
    active, staged_owners, rates = {}, {}, {}
    cohort = None
    identity = None
    generation = 0
    unconfirmed = False
    loading = None
    load_failures = {worker: 0 for worker in workers}

    def runtime_check():
        check_cancelled()
        if check_healthy() is not True:
            if cohort is not None:
                cohort.cancel(reason='pressure')
            raise MemoryPressureYield('Parallel transcription yielded to resource pressure')
        return True

    def stage(future, assignment, started):
        result, finished = future.result()
        coordinator.completed(assignment, result)
        staged_owners[assignment.window.core_start] = (assignment.worker, generation)
        # Harvest latency (including another worker's serial extraction) is not
        # this worker's processing time and must not distort its next budget.
        elapsed = max(.001, finished - started)
        rate = (assignment.window.core_end - assignment.window.core_start) / elapsed
        rates[assignment.worker] = .5 * rates.get(assignment.worker, rate) + .5 * rate

    def drain(reason, *, release_cohort=True):
        nonlocal unconfirmed, loading
        if cohort is not None:
            cohort.cancel(reason=reason)
        for future in active:
            future.cancel()
        operations = tuple(active) + (() if loading is None else (loading,))
        if loading is not None:
            loading.cancel()
        _done, pending = wait(operations, timeout=release_timeout)
        if pending:
            unconfirmed = True
            error = CohortReleaseError('Parallel inference or loading has not unwound; cohort reservation retained')
            error.cohort = cohort
            error.pending_futures = tuple(pending)
            raise error
        failures = []
        if loading is not None:
            try:
                loading.result()
            except BaseException as error:
                failures.append(_without_tracebacks(error))
            loading = None
        for future, (assignment, started) in tuple(active.items()):
            try:
                stage(future, assignment, started)
            except BaseException as error:
                coordinator.yielded(assignment)
                failures.append(_without_tracebacks(error))
            del active[future]
        if cohort is not None and release_cohort:
            cohort.release(timeout=release_timeout)
        return failures

    def infer(selected_cohort, assignment, audio, deadline):
        def progress(seek, total):
            on_event('progress', worker=assignment.worker, window=assignment.window,
                     seek=seek, total=total)
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Chunk deadline expired before inference')
        try:
            result = selected_cohort.transcribe(assignment.worker, audio,
                timeout=remaining, progress_callback=progress, **options)
            if transform_result is not None:
                check_cancelled()
                try:
                    result = transform_result(result)
                except MemoryError as error:
                    failure = WorkerAllocationFailure('transcribe')
                    failure.worker = assignment.worker
                    raise failure from error
                check_cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError('Chunk deadline expired during result processing')
        except WorkerAllocationFailure as error:
            error.attempted_seconds = assignment.window.core_end-assignment.window.core_start
            raise
        return result, time.monotonic()

    try:
        while True:
            control = None
            errors = []
            try:
                runtime_check()
                if coordinator.state.complete:
                    break
                if cohort is None:
                    candidate = cohort_factory()
                    if type(candidate) is not CohortModelRuntime:
                        raise TypeError('Use the canonical cohort lifecycle')
                    cohort = candidate
                    selected = tuple((spec.device, spec.artifact) for spec in cohort.specs)
                    if tuple(spec.device.selector for spec in cohort.specs) != workers:
                        raise ValueError('Cohort devices changed from the selected worker list')
                    if identity is None:
                        identity = selected
                    elif identity != selected:
                        raise ValueError('Device or model identity changed during this file')
                    # Loading must not block the thread which watches global
                    # pressure/user stop. The lifecycle lock still serializes
                    # the cold loads; this future adds no second model owner.
                    loading = pool.submit(cohort.load, timeout=load_timeout)
                    while not loading.done():
                        runtime_check()
                        wait((loading,), timeout=.05)
                    try:
                        admission = loading.result()
                    except (MemoryPressureYield, CohortCancelled, CohortReleaseError, WorkerAllocationFailure):
                        raise
                    except Exception as error:
                        raise CohortLoadError('Selected GPU model or runtime could not load') from error
                    loading = None
                    load_failures = {worker: 0 for worker in workers}
                    generation += 1
                    coordinator.pause_dispatch(False)
                    on_event('loaded', workers=workers, generation=generation, admission=admission)
                # Harvest every ready result before assigning another bounded window.
                done, _pending = wait(tuple(active), timeout=.05, return_when=FIRST_COMPLETED)
                for future in done:
                    assignment, started = active.pop(future)
                    try:
                        stage(future, assignment, started)
                    except BaseException as error:
                        coordinator.yielded(assignment)
                        errors.append(_without_tracebacks(error))
                if errors:
                    allocations = [e for e in errors if isinstance(e, WorkerAllocationFailure)]
                    raise (_terminal_errors(errors) or allocations or errors)[0]
                # One commit at a time lets cancellation/pressure interrupt the
                # next commit without losing accounting for the preceding one.
                while committed := coordinator.commit_ready(check_before_commit=runtime_check, maximum_commits=1):
                    window = committed[0]
                    owner, completed_generation = staged_owners.pop(window.core_start)
                    if completed_generation == generation:
                        adaptive_by_worker[owner].record_success(healthy=True)
                    on_event('committed', worker=owner, window=window, state=coordinator.state)
                if coordinator.state.complete:
                    continue
                busy = {assignment.worker for assignment, _started in active.values()}
                for worker in workers:
                    if worker in busy:
                        continue
                    runtime_check()
                    adaptive = adaptive_by_worker[worker]
                    seconds = adaptive.current_seconds
                    if worker in rates:
                        seconds = max(adaptive.minimum_seconds,
                                      math.floor(seconds * rates[worker] / max(rates.values())))
                    assignment = coordinator.claim(worker, core_seconds=seconds)
                    if assignment is None:
                        continue
                    started = time.monotonic()
                    deadline = started + chunk_timeout
                    try:
                        on_event('started', worker=worker, window=assignment.window)
                        audio = extract_chunk(assignment.window, timeout_seconds=chunk_timeout,
                                              check_cancelled=runtime_check)
                        runtime_check()
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Chunk deadline expired during extraction')
                        future = pool.submit(infer, cohort, assignment, audio, deadline)
                        active[future] = (assignment, started)
                    except BaseException:
                        coordinator.yielded(assignment)
                        raise
                    finally:
                        audio = None
                if not active and not coordinator.state.complete:
                    runtime_check()
                    raise RuntimeError('Parallel scheduler cannot make forward progress')
            except (MemoryPressureYield, CohortCancelled, WorkerAllocationFailure) as error:
                control = _without_tracebacks(error)
                reason = 'pressure' if isinstance(error, MemoryPressureYield) or (cohort is not None and cohort.cancellation_reason == 'pressure') else 'stop'
                coordinator.pause_dispatch(True)
                failures = errors + [control] + drain(reason)
                terminal = _terminal_errors(failures)
                if terminal:
                    raise terminal[0]
                check_cancelled()
                allocations = {e.worker: e for e in failures if isinstance(e, WorkerAllocationFailure)}
                if not allocations and reason != 'pressure':
                    raise control
                cohort = None
                exhausted = False
                if allocations:
                    control = next(iter(allocations.values()))
                    for worker, failure in allocations.items():
                        if worker not in adaptive_by_worker:
                            raise RuntimeError('Allocation failure lacks a selected worker identity')
                        if failure.phase == 'load':
                            load_failures[worker] += 1
                            exhausted |= load_failures[worker] >= 2
                        else:
                            exhausted |= adaptive_by_worker[worker].record_allocation_failure(
                                attempted_seconds=failure.attempted_seconds)
                else:
                    for adaptive in adaptive_by_worker.values():
                        adaptive.record_pressure_yield()
                on_event('yielded', error=control, state=coordinator.state)
                recovery_window = wait_for_recovery(control)
                check_cancelled()
                if allocations and _external_pressure_recovered(recovery_window):
                    for worker in allocations:
                        adaptive_by_worker[worker].record_external_pressure_recovery()
                        load_failures[worker] = 0
                    exhausted = False
                if exhausted:
                    raise control
        if cohort is not None:
            cohort.release(timeout=release_timeout)
            cohort = None
        result = finalize_assembly(coordinator.state)
        check_cancelled()
        return result
    except MediaProcessReleaseUnconfirmed as error:
        # An extractor which has not exited still owns memory. Do not permit
        # another file/cohort to spend that reservation while cleanup is unknown.
        unconfirmed = True
        try:
            drain('stop', release_cohort=False)
        except CohortReleaseError as failure:
            failure.process = error.process
            raise
        failure = CohortReleaseError('Audio extraction has not exited; cohort reservation retained')
        failure.cohort = cohort
        failure.process = error.process
        raise failure from error
    finally:
        try:
            if not unconfirmed:
                drain('stop')
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            coordinator.close()
