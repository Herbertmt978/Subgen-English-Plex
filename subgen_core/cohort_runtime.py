"""Model-runtime-owned lifecycle for one file's explicitly selected workers.

Reuse the canonical aggregate reservation and artifact checks. Backend handles
must exist before their load can allocate, honor cancellation/deadlines, and
confirm release even after a partial load. Discovery, memory policy, chunk
scheduling and subtitle publication remain with their existing owners.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import threading
import time
from typing import Callable

from .execution_policy import ExecutionDevice
from .model_envelope_catalog import (
    ModelArtifactIdentity, validate_cohort_model_identity, verify_model_artifact,
)
from .resource_management import CohortReservation, MemoryPressureYield
from .resident_worker import WorkerCancelled, WorkerAllocationFailure


class CohortCancelled(RuntimeError):
    """The unfinished cohort operation was cancelled; do not publish its output."""


class CohortReleaseError(RuntimeError):
    """Release is unconfirmed; retain the reservation and refuse another load."""


class CohortCapacityDeferred(MemoryPressureYield):
    def __init__(self, decision):
        super().__init__("Selected workers do not fit the combined memory budget")
        self.decision = decision


@dataclass(frozen=True)
class CohortWorkerSpec:
    device: ExecutionDevice
    artifact: ModelArtifactIdentity
    artifact_path: Path
    make_worker: Callable


@dataclass(frozen=True)
class FileCohortPlan:
    """Cold per-file composition supplied by the device/resource policy owner.

    This does not discover hardware or authorize allocations. The provider must
    exclude competing legacy model operations before handing the plan to a file.
    """

    specs: tuple
    chunk_seconds: tuple
    reservation: CohortReservation
    decide_admission: Callable
    check_healthy: Callable
    wait_for_recovery: Callable
    scratch_directory: Path
    selection_reason: str
    load_timeout: float = 120
    chunk_timeout: float = 3600
    release_timeout: float = 30

    def __post_init__(self):
        # Canonical lifecycle validation is cold: no child, model or lease.
        CohortModelRuntime(self.specs, reservation=self.reservation,
            decide_admission=self.decide_admission, check_healthy=self.check_healthy)
        if (not isinstance(self.chunk_seconds, tuple) or len(self.chunk_seconds) != len(self.specs)
                or any(type(s) is not int or not 300 <= s <= 1800 for s in self.chunk_seconds)):
            raise ValueError('Cohort chunk budgets must be between five and thirty minutes')
        if not callable(self.wait_for_recovery):
            raise TypeError('Cohort recovery requires its resource-policy owner')
        directory = Path(self.scratch_directory)
        if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
            raise ValueError('Cohort scratch directory must be local, absolute and non-symlink')
        if (not isinstance(self.selection_reason, str) or not 1 <= len(self.selection_reason) <= 512
                or any(ord(c) < 32 for c in self.selection_reason)):
            raise ValueError('Cohort requires a printable model-selection reason')
        for seconds in (self.load_timeout, self.chunk_timeout, self.release_timeout):
            _deadline(seconds)


def _deadline(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Cohort operation timeout must be finite and positive")
    return time.monotonic() + seconds


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Cohort operation timed out")
    return remaining


class CohortModelRuntime:
    """One residency generation: exclusive load/release and per-device inference.

    A backend handle implements load(timeout, cancel), transcribe(audio,
    timeout, cancel, **options), release(timeout), and model_is_loaded. Its
    release returns exactly True only after native processes/allocations have
    been released. Factories themselves must not allocate model memory. After
    release, the owner constructs a new generation from the same fixed specs;
    cancellation is never cleared underneath a concurrent release request.
    """

    def __init__(self, specs, *, reservation, decide_admission, check_healthy):
        if not isinstance(specs, tuple) or not 1 <= len(specs) <= 32:
            raise ValueError("A cohort requires a bounded tuple of worker specifications")
        for spec in specs:
            if type(spec) is not CohortWorkerSpec or type(spec.device) is not ExecutionDevice:
                raise TypeError("Cohort workers require verified device specifications")
            spec.device.__post_init__()
            if not callable(spec.make_worker):
                raise TypeError("Cohort worker factory must be callable")
        if len({s.device.physical_uuid for s in specs}) != len(specs):
            raise ValueError("A physical device cannot have two cohort workers")
        if len({s.device.selector for s in specs}) != len(specs):
            raise ValueError("A backend selector cannot identify two cohort workers")
        validate_cohort_model_identity(tuple(s.artifact for s in specs))
        if type(reservation) is not CohortReservation or not callable(decide_admission) or not callable(check_healthy):
            raise TypeError("Cohort runtime requires canonical admission and health dependencies")
        self.specs = specs
        self._reservation = reservation
        self._decide = decide_admission
        self._healthy = check_healthy
        self._operation = threading.Lock()
        self._condition = threading.Condition()
        self._cancel = threading.Event()
        self._cancel_reason = None
        self._workers = {}
        self._active = set()
        self._token = None
        self._state = "cold"
        self._generation = 0

    @property
    def state(self):
        with self._condition:
            return self._state

    @property
    def cancellation_reason(self):
        with self._condition:
            return self._cancel_reason

    def _check(self, deadline):
        _remaining(deadline)
        if self._cancel.is_set():
            self._raise_cancelled()
        if self._healthy() is not True:
            self.cancel(reason="pressure")
            raise MemoryPressureYield("Worker cohort yielded to memory or workload pressure")

    def _raise_cancelled(self):
        if self._cancel_reason == "pressure":
            raise MemoryPressureYield("Worker cohort yielded to resource pressure")
        raise CohortCancelled("Worker cohort was cancelled")

    def load(self, *, timeout):
        deadline = _deadline(timeout)
        if not self._operation.acquire(blocking=False):
            raise RuntimeError("Worker cohort lifecycle is busy")
        try:
            with self._condition:
                if self._state != "cold":
                    raise RuntimeError("Worker cohort is not released")
                self._state = "loading"
                self._generation += 1
            try:
                for spec in self.specs:
                    verify_model_artifact(spec.artifact_path, spec.artifact,
                                          check_cancelled=lambda: self._check(deadline))
                self._check(deadline)
                self._token, decision = self._reservation.acquire(self._decide)
                if self._token is None:
                    raise CohortCapacityDeferred(decision)
                for spec in self.specs:
                    self._check(deadline)
                    worker = spec.make_worker(spec)
                    # Track the cold handle before load; failed construction/load
                    # must never strand a partially allocated model outside cleanup.
                    self._workers[spec.device.selector] = worker
                    if not all(callable(getattr(worker, name, None)) for name in ("load", "transcribe", "release")):
                        raise TypeError("Worker handle lacks lifecycle operations")
                    try:
                        worker.load(timeout=_remaining(deadline), cancel=self._cancel)
                    except WorkerAllocationFailure as error:
                        if error.phase != 'load':
                            raise RuntimeError('Worker allocation phase does not match model loading') from error
                        error.worker = spec.device.selector
                        raise
                    if getattr(worker, "model_is_loaded", None) is not True:
                        raise RuntimeError("Worker did not confirm model loading")
                    verify_model_artifact(spec.artifact_path, spec.artifact,
                                          check_cancelled=lambda: self._check(deadline))
                self._check(deadline)
                with self._condition:
                    if self._cancel.is_set():
                        self._raise_cancelled()
                    self._state = "ready"
                return decision
            except BaseException as error:
                self.cancel(reason="pressure" if isinstance(error, MemoryPressureYield) else "failure")
                # Cleanup has its own bounded budget after a load timeout.
                self._release_workers(time.monotonic() + timeout)
                if isinstance(error, (WorkerCancelled, CohortCancelled)):
                    self._raise_cancelled()
                raise
        finally:
            self._operation.release()

    def transcribe(self, selector, audio, *, timeout, **options):
        deadline = _deadline(timeout)
        with self._condition:
            if self._state != "ready" or self._cancel.is_set():
                self._raise_cancelled()
            if selector not in self._workers:
                raise ValueError("Chunk requested an unselected worker")
            if selector in self._active:
                raise RuntimeError("This worker already has an active chunk")
            worker = self._workers[selector]
            generation = self._generation
            self._active.add(selector)
        try:
            self._check(deadline)
            result = worker.transcribe(audio, timeout=_remaining(deadline), cancel=self._cancel, **options)
            self._check(deadline)
            with self._condition:
                if self._state != "ready" or generation != self._generation:
                    raise CohortCancelled("Late chunk result belongs to a draining cohort")
            return result
        except WorkerAllocationFailure as error:
            if error.phase != 'transcribe':
                self.cancel(reason="failure")
                raise RuntimeError('Worker allocation phase does not match inference') from error
            error.worker = selector
            self.cancel(reason="failure")
            raise
        except (WorkerCancelled, CohortCancelled):
            self.cancel()
            self._raise_cancelled()
        except MemoryPressureYield:
            self.cancel(reason="pressure")
            raise
        except BaseException:
            self.cancel(reason="failure")
            raise
        finally:
            with self._condition:
                self._active.remove(selector)
                self._condition.notify_all()

    def cancel(self, *, reason="stop"):
        """Preserve the first cause without waiting for a backend/lifecycle lock.

        Pressure is retry control flow, never evidence of a failed media file.
        A later release or sibling cancellation must not erase that cause.
        """
        if reason not in ("stop", "pressure", "failure"):
            raise ValueError("Unknown cohort cancellation reason")
        with self._condition:
            if self._cancel_reason is None:
                self._cancel_reason = reason
            self._cancel.set()
            if self._state == "ready":
                self._state = "draining"

    def _release_workers(self, deadline):
        failures = []
        for selector, worker in tuple(self._workers.items()):
            try:
                if worker.release(timeout=_remaining(deadline)) is not True or getattr(worker, "model_is_loaded", None) is not False:
                    raise RuntimeError("Release was not confirmed")
                del self._workers[selector]
            except BaseException as error:
                failures.append(f"{selector}:{type(error).__name__}")
        with self._condition:
            if failures:
                self._state = "blocked"
                raise CohortReleaseError("Worker release unconfirmed: " + ", ".join(failures))
            if self._token is not None:
                self._reservation.release(self._token, lambda: not self._workers and not self._active)
                self._token = None
            self._state = "released"
            self._condition.notify_all()

    def release(self, *, timeout):
        deadline = _deadline(timeout)
        self.cancel()
        if not self._operation.acquire(timeout=_remaining(deadline)):
            raise CohortReleaseError("Loading has not unwound; memory reservation retained")
        try:
            with self._condition:
                self._state = "draining"
                self._generation += 1
                while self._active:
                    try:
                        self._condition.wait(timeout=_remaining(deadline))
                    except TimeoutError as error:
                        self._state = "blocked"
                        raise CohortReleaseError("Active chunks have not unwound; reservation retained") from error
            self._release_workers(deadline)
        finally:
            self._operation.release()
