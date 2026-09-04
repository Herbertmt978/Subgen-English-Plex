"""Model loading, shared inference gating, and delayed cleanup algorithms.

All process state and runtime dependencies remain on the injected ``runtime``.
The pressure-release lock order is admission barrier, every inference permit,
then ``model_load_lock``.  A callback-triggered release is requested only after
the callback has unwound and its inference permit has been returned.
"""

from subgen_core import backend_release as _backend_release


class _ReleaseTransition:
    """One generation's shared release result for owners and joiners."""

    __slots__ = ("complete", "did_unload", "failure", "generation", "reason")

    def __init__(self, generation, reason=None):
        self.generation = generation
        self.reason = reason
        self.complete = False
        self.did_unload = False
        self.failure = None


class ModelLoadProfileUnhealthy(RuntimeError):
    """Fresh admission passed twice but the selected model still could not load."""


class ModelReleaseError(RuntimeError):
    """A coordinated model release failed and admission remains closed."""


class ModelRuntimeCancelled(RuntimeError):
    """Model work stopped because process shutdown cancelled the runtime."""


class ModelInferenceAllocationFailure(RuntimeError):
    """One inference allocation failed after admission and must be retried."""


class _ModelLoadAllocationFailure(Exception):
    """Private control flow for one admitted backend allocation failure."""

    __slots__ = ("attempt_count", "diagnostic")

    def __init__(self, attempt_count, diagnostic):
        super().__init__("admitted model-load allocation failed")
        self.attempt_count = attempt_count
        self.diagnostic = diagnostic


_LOAD_DEFERRED_CAPACITY = object()
_LOAD_DEFERRED_STALE_GENERATION = object()
_ANY_CLEANUP_TIMER = object()
_PRESSURE_RELEASE_TICKET_ATTR = "_subgen_pressure_release_ticket"
_MAX_GENERATION = (1 << 63) - 1


def _coordinated_runtime(runtime):
    """Return whether the root exposes the complete v0.5 coordinator state."""
    return all(
        hasattr(runtime, name)
        for name in (
            "model_runtime_condition",
            "model_admission_closed",
            "model_release_generation",
            "model_release_transition",
            "model_active_inferences",
            "model_inference_permit_count",
        )
    )


def _transition_active(transition):
    return transition is not None and not transition.complete


def _cancelled(cancelled):
    if cancelled is None:
        return False
    is_set = getattr(cancelled, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if callable(cancelled):
        return bool(cancelled())
    return bool(cancelled)


def _notify_all(condition):
    notify = getattr(condition, "notify_all", None)
    if callable(notify):
        notify()


def _generation_value(runtime, name):
    value = getattr(runtime, name, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"Invalid runtime generation state: {name}")
    if not 0 <= value <= _MAX_GENERATION:
        raise RuntimeError(f"Runtime generation is out of range: {name}")
    return value


def _increment_generation_locked(runtime, name):
    value = _generation_value(runtime, name)
    if value == _MAX_GENERATION:
        raise RuntimeError(f"Runtime generation exhausted: {name}")
    value += 1
    setattr(runtime, name, value)
    return value


def _runtime_generation_snapshot_locked(runtime):
    """Read coarse process counters while the runtime condition is held."""
    return {
        "model_resident": getattr(runtime, "model", None) is not None,
        "model_load_generation": _generation_value(runtime, "model_load_generation"),
        "model_unload_generation": _generation_value(
            runtime, "model_unload_generation"
        ),
        "cuda_oom_generation": _generation_value(runtime, "cuda_oom_generation"),
        "media_failure_generation": _generation_value(
            runtime, "media_failure_generation"
        ),
    }


def _priority_receipt_state_locked(runtime, priority_snapshot=None):
    """Return gate-only priority state beneath the model-condition lock."""

    if priority_snapshot is None:
        controller = getattr(runtime, "model_pressure_controller", None)
        snapshot_reader = getattr(controller, "gate_priority_status_snapshot", None)
        if callable(snapshot_reader):
            priority_snapshot = snapshot_reader()
        else:
            configured = bool(
                getattr(
                    getattr(runtime, "priority_pressure_probe", None),
                    "configured",
                    False,
                )
            )
            priority_snapshot = {
                "configured": configured,
                "state": "unavailable" if configured else "disabled",
                "heartbeat_age_ms": None,
                "source_age_ms": None,
                "policy_sha256": None,
                "observation_digest": None,
                "transition_observation_digest": None,
                "transition_sequence": 0,
                "controller_phase": "recovering" if configured else "normal",
                "recovery_reason": "priority_pressure" if configured else None,
                "distinct_clear_count": 0,
                "source_generation": None,
                "admission_open": not configured,
            }
    return priority_snapshot


def runtime_receipt_state_locked(runtime, priority_snapshot=None):
    """Capture the exact private receipt state while the condition is held."""

    priority = _priority_receipt_state_locked(runtime, priority_snapshot)
    generations = _runtime_generation_snapshot_locked(runtime)
    return {
        "source_generation": priority.get("source_generation"),
        "observation_digest": priority.get("observation_digest"),
        "transition_observation_digest": priority.get("transition_observation_digest"),
        "transition_sequence": priority.get("transition_sequence", 0),
        "heartbeat_age_ms": priority.get("heartbeat_age_ms"),
        "source_age_ms": priority.get("source_age_ms"),
        "policy_sha256": priority.get("policy_sha256"),
        "priority_state": priority.get("state", "disabled"),
        "controller_phase": priority.get("controller_phase", "normal"),
        "recovery_reason": priority.get("recovery_reason"),
        "admission_open": bool(
            priority.get("admission_open", False)
            and not getattr(runtime, "model_admission_closed", True)
        ),
        "distinct_clear_count": priority.get("distinct_clear_count", 0),
        "model_resident": generations["model_resident"],
        "model_load_generation": generations["model_load_generation"],
        "model_unload_generation": generations["model_unload_generation"],
        "model_identity_sha256": getattr(
            runtime,
            "resident_model_identity_sha256",
            None,
        ),
        "cuda_oom_generation": generations["cuda_oom_generation"],
        "media_failure_generation": generations["media_failure_generation"],
    }


def _record_runtime_receipt_locked(runtime, priority_snapshot=None):
    coordinator = getattr(runtime, "runtime_receipt_coordinator", None)
    record = getattr(coordinator, "record_runtime_change_locked", None)
    if callable(record):
        try:
            record(runtime_receipt_state_locked(runtime, priority_snapshot))
        except BaseException:
            if bool(getattr(coordinator, "gate_enabled", False)):
                runtime.model_admission_closed = True
                controller = getattr(runtime, "model_pressure_controller", None)
                latch = getattr(
                    controller,
                    "latch_receipt_failure_without_publication",
                    None,
                )
                if callable(latch):
                    latch()
                elif controller is not None:
                    recovering = getattr(controller, "RECOVERING", "recovering")
                    yielding = getattr(controller, "YIELDING", "yielding")
                    if getattr(controller, "state", None) != yielding:
                        controller.state = recovering
                    controller.admission_open = False
                    controller.recovery_reason = "receipt_unavailable"
                status = getattr(runtime, "model_runtime_status", None)
                if isinstance(status, dict):
                    status = dict(status)
                    status["controller_state"] = getattr(
                        controller,
                        "state",
                        "recovering",
                    )
                    status["recovery_reason"] = "receipt_unavailable"
                    status["admission_open"] = False
                    runtime.model_runtime_status = status
                condition = getattr(runtime, "model_runtime_condition", None)
                if condition is not None:
                    _notify_all(condition)
            raise


def runtime_generation_snapshot(runtime):
    """Return one atomic, privacy-safe residency and generation snapshot."""
    condition = getattr(runtime, "model_runtime_condition", None)
    if condition is None:
        return _runtime_generation_snapshot_locked(runtime)
    with condition:
        return _runtime_generation_snapshot_locked(runtime)


def _increment_generation(runtime, name):
    condition = getattr(runtime, "model_runtime_condition", None)
    if condition is None:
        return _increment_generation_locked(runtime, name)
    with condition:
        value = _increment_generation_locked(runtime, name)
        _record_runtime_receipt_locked(runtime)
        _notify_all(condition)
        return value


def is_cuda_oom_failure(error):
    """Recognize explicit CUDA OOM signals without counting host ENOMEM."""
    seen = set()
    current = error
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        error_type = type(current)
        type_name = error_type.__name__.casefold()
        module_name = error_type.__module__.casefold()
        if type_name == "outofmemoryerror" and (
            "torch" in module_name or "cuda" in module_name
        ):
            return True

        try:
            message = str(current).strip().casefold()
        except Exception:
            message = ""
        if message.startswith("runtimeerror:"):
            message = message.removeprefix("runtimeerror:").lstrip()
        message = " ".join(message.split())
        explicit_prefixes = (
            "cuda out of memory",
            "cuda error: out of memory",
            "cuda runtime error: out of memory",
            "cuda failed with error out of memory",
            "cublas_status_alloc_failed",
            "cudnn_status_alloc_failed",
            "cuda_status_alloc_failed",
        )
        if message.startswith(explicit_prefixes):
            return True
        current = current.__cause__ or current.__context__
    return False


def _record_classified_cuda_oom(runtime, error):
    device = getattr(runtime, "transcribe_device", "")
    if not isinstance(device, str) or not device.casefold().startswith("cuda"):
        return False
    if not is_cuda_oom_failure(error):
        return False
    _increment_generation(runtime, "cuda_oom_generation")
    return True


def record_media_failure(runtime):
    """Count one accepted terminal media-processing failure."""
    return _increment_generation(runtime, "media_failure_generation")


def _runtime_cancellation(runtime):
    return getattr(runtime, "model_runtime_cancel_event", None)


def _raise_if_profile_unhealthy(runtime):
    if getattr(runtime, "model_profile_unhealthy", False):
        raise ModelLoadProfileUnhealthy(
            "Selected model memory profile is unhealthy after two admitted "
            "allocation failures; operator attention is required"
        ) from None


def _release_failure_diagnostic(error):
    try:
        message = str(error)[:240]
    except Exception:
        message = "message unavailable"
    return (type(error).__name__[:80], message)


def _raise_release_failure(failure):
    error_type, message = failure
    detail = f": {message}" if message else ""
    raise ModelReleaseError(f"Model release failed ({error_type}){detail}") from None


def close_model_admission(runtime):
    """Close admission and advance the epoch on the first close."""
    if not _coordinated_runtime(runtime):
        return None

    condition = runtime.model_runtime_condition
    with condition:
        controller = getattr(runtime, "model_pressure_controller", None)
        if controller is not None:
            close = getattr(controller, "close_admission", None)
            if callable(close):
                close()
            else:
                controller.admission_open = False
        if not runtime.model_admission_closed:
            runtime.model_admission_closed = True
            runtime.model_release_generation += 1
        generation = runtime.model_release_generation
        _record_runtime_receipt_locked(runtime)
        _notify_all(condition)
        return generation


def reopen_model_admission(runtime):
    """Reopen only after the current release and controller recovery finish."""
    if not _coordinated_runtime(runtime):
        return True

    condition = runtime.model_runtime_condition
    with condition:
        if getattr(runtime, "model_profile_unhealthy", False):
            return False
        controller = getattr(runtime, "model_pressure_controller", None)
        if controller is not None:
            open_if_normal = getattr(controller, "open_admission_if_normal", None)
            if callable(open_if_normal):
                open_if_normal()
            normal = getattr(controller, "NORMAL", "normal")
            if getattr(controller, "state", None) != normal or not getattr(
                controller, "admission_open", False
            ):
                return False
        if _transition_active(runtime.model_release_transition):
            return False
        runtime.model_admission_closed = False
        _record_runtime_receipt_locked(runtime)
        _notify_all(condition)
    return True


def wait_for_model_recovery(runtime, cancelled=None):
    """Wait through controller recovery, then atomically reopen admission."""
    while True:
        _raise_if_profile_unhealthy(runtime)
        if _cancelled(cancelled):
            return False
        controller = None
        if _coordinated_runtime(runtime):
            condition = runtime.model_runtime_condition
            with condition:
                transition = runtime.model_release_transition
                if transition is not None and transition.complete:
                    if transition.failure is not None:
                        _raise_release_failure(transition.failure)
                controller = getattr(runtime, "model_pressure_controller", None)
                yielding = getattr(controller, "YIELDING", "yielding")
                normal = getattr(controller, "NORMAL", "normal")
                state = getattr(controller, "state", None)
                blocked_normal = bool(
                    controller is not None
                    and state == normal
                    and not getattr(controller, "admission_open", False)
                )
                if (
                    _transition_active(transition)
                    or state == yielding
                    or blocked_normal
                ):
                    condition.wait(timeout=5.0)
                    continue
        else:
            controller = getattr(runtime, "model_pressure_controller", None)

        if controller is None:
            if reopen_model_admission(runtime):
                return True
        else:
            heartbeat = getattr(runtime, "model_recovery_heartbeat", None)
            if callable(heartbeat):
                recovered = controller.wait_for_recovery(
                    cancelled,
                    heartbeat=heartbeat,
                )
            else:
                recovered = controller.wait_for_recovery(cancelled)
            if not recovered:
                _raise_if_profile_unhealthy(runtime)
                return False
            if reopen_model_admission(runtime):
                return True
    return False


def wait_for_model_admission(runtime, cancelled=None):
    """Wait without holding a permit until the current admission epoch opens."""
    if not _coordinated_runtime(runtime):
        return not _cancelled(cancelled)

    condition = runtime.model_runtime_condition
    while True:
        _raise_if_profile_unhealthy(runtime)
        if _cancelled(cancelled):
            return False
        recover = False
        with condition:
            transition = runtime.model_release_transition
            if (
                transition is not None
                and transition.complete
                and transition.failure is not None
            ):
                _raise_release_failure(transition.failure)
            if not runtime.model_admission_closed and not _transition_active(
                transition
            ):
                return True
            if not _transition_active(transition):
                controller = getattr(runtime, "model_pressure_controller", None)
                if controller is not None:
                    normal = getattr(controller, "NORMAL", "normal")
                    yielding = getattr(controller, "YIELDING", "yielding")
                    state = getattr(controller, "state", None)
                    if state == yielding:
                        recover = False
                    elif state != normal:
                        recover = True
                    elif getattr(controller, "admission_open", False):
                        runtime.model_admission_closed = False
                        _record_runtime_receipt_locked(runtime)
                        _notify_all(condition)
                        return True
                elif runtime.model_admission_closed:
                    # A successful controller-free release normally reopens
                    # itself.  An externally closed barrier waits for its owner.
                    recover = False
            if not recover:
                condition.wait(timeout=5.0)
        if recover and not wait_for_model_recovery(runtime, cancelled):
            return False


def _acquire_inference_slot(runtime, cancelled=None):
    """Perform generation-safe admission before and after permit acquisition."""
    condition = runtime.model_runtime_condition
    while wait_for_model_admission(runtime, cancelled):
        with condition:
            if runtime.model_admission_closed or _transition_active(
                runtime.model_release_transition
            ):
                continue
            generation = runtime.model_release_generation

        if cancelled is None:
            acquired = runtime.model_inference_semaphore.acquire()
        else:
            timeout = getattr(runtime, "model_permit_wait_seconds", 1.0)
            acquired = runtime.model_inference_semaphore.acquire(timeout=timeout)
        if acquired is False:
            continue

        with condition:
            valid = (
                not _cancelled(cancelled)
                and not runtime.model_admission_closed
                and not _transition_active(runtime.model_release_transition)
                and runtime.model_release_generation == generation
            )
            if valid:
                runtime.model_active_inferences += 1
                return generation
        runtime.model_inference_semaphore.release()
    return None


def _release_inference_slot(runtime):
    try:
        runtime.model_inference_semaphore.release()
    finally:
        condition = runtime.model_runtime_condition
        with condition:
            runtime.model_active_inferences = max(
                0,
                runtime.model_active_inferences - 1,
            )
            _notify_all(condition)


def _compose_pressure_callback(runtime, transcribe_kwargs, pressure_errors):
    """Preserve the caller callback and append the controller pressure check."""
    controller = getattr(runtime, "model_pressure_controller", None)
    if not getattr(runtime, "memory_pressure_yield", False) or controller is None:
        return transcribe_kwargs

    original_callback = transcribe_kwargs.get("progress_callback")

    def progress_callback(*args, **kwargs):
        callback_result = None
        if original_callback is not None:
            callback_result = original_callback(*args, **kwargs)
        try:
            controller.check_or_raise()
        except Exception as exc:
            pressure_type = getattr(
                getattr(runtime, "_resource_management", None),
                "MemoryPressureYield",
                (),
            )
            if isinstance(exc, pressure_type):
                pressure_errors.append(exc)
                close_model_admission(runtime)
            raise
        if not getattr(controller, "admission_open", True):
            close_model_admission(runtime)
        return callback_result

    composed = dict(transcribe_kwargs)
    composed["progress_callback"] = progress_callback
    return composed


def check_segment_commit_allowed(runtime):
    """Recheck pressure at the final safe boundary before a chunk commit.

    The inference callback can finish between pressure samples.  This checkpoint
    therefore forces the optional priority input and the generic controller to
    refresh after staging but before any transcript bytes become committed.
    A rejected commit carries the same generation-safe release ticket as an
    inference callback yield.
    """

    controller = getattr(runtime, "model_pressure_controller", None)
    if not getattr(runtime, "memory_pressure_yield", False) or controller is None:
        return True

    pressure_type = runtime._resource_management.MemoryPressureYield
    source_generation = None
    if _coordinated_runtime(runtime):
        with runtime.model_runtime_condition:
            if runtime.model_admission_closed:
                # Bind to the current barrier, not a cached earlier release.
                # A new close can precede creation of its release transition;
                # the historical transition must not make this release a no-op.
                source_generation = runtime.model_release_generation - 1
            else:
                source_generation = runtime.model_release_generation

    try:
        state = controller.check_or_raise(force_priority=True)
    except Exception as exc:
        if not isinstance(exc, pressure_type):
            raise
        close_model_admission(runtime)
        rebound = _bind_pressure_release_ticket(runtime, exc, source_generation)
        raise rebound.with_traceback(None) from None

    normal = getattr(controller, "NORMAL", "normal")
    admitted = bool(
        state == normal
        and getattr(controller, "admission_open", True)
        and not getattr(runtime, "model_admission_closed", False)
    )
    if admitted:
        return True

    reason = getattr(controller, "recovery_reason", None) or "memory pressure"
    control = pressure_type(reason)
    close_model_admission(runtime)
    rebound = _bind_pressure_release_ticket(runtime, control, source_generation)
    raise rebound.with_traceback(None) from None


def _bind_release_ticket(
    rebound,
    original,
    *,
    source_generation,
    reason,
):
    """Attach one immutable release epoch and scrub the original traceback."""
    setattr(
        rebound,
        _PRESSURE_RELEASE_TICKET_ATTR,
        (source_generation, reason),
    )
    original.__traceback__ = None
    original.__context__ = None
    original.__cause__ = None
    return rebound


def _bind_pressure_release_ticket(runtime, error, source_generation):
    """Return a fresh pressure control signal carrying its release epoch."""
    pressure_type = runtime._resource_management.MemoryPressureYield
    controller = getattr(runtime, "model_pressure_controller", None)
    reason = getattr(controller, "recovery_reason", None) or "memory_pressure"
    return _bind_release_ticket(
        pressure_type(*error.args),
        error,
        source_generation=source_generation,
        reason=reason,
    )


def _bind_inference_allocation_ticket(error, source_generation):
    """Return a bounded allocation control error carrying its release epoch."""
    error_type, message = _release_failure_diagnostic(error)
    detail = f": {message}" if message else ""
    return _bind_release_ticket(
        ModelInferenceAllocationFailure(
            f"Inference allocation failed ({error_type}){detail}"
        ),
        error,
        source_generation=source_generation,
        reason="inference_allocation_failure",
    )


def _is_inference_allocation_failure(runtime, error):
    resources = getattr(runtime, "_resource_management", None)
    classifier = getattr(resources, "is_allocation_failure", None)
    return bool(callable(classifier) and classifier(error))


def _fresh_model_admission(runtime):
    controller = getattr(runtime, "model_pressure_controller", None)
    if controller is None:
        return True
    decision = controller.immediate_load_admission(
        runtime.model_requirement,
        sample_reader=runtime.read_pressure_sample,
    )
    if not decision.admitted:
        close_model_admission(runtime)
        return False
    return True


def _model_loader_kwargs(runtime):
    device = runtime.transcribe_device
    loader_kwargs = {
        "download_root": runtime.model_location,
        "device": device,
        "cpu_threads": runtime.whisper_threads,
        "num_workers": runtime.concurrent_transcriptions,
        "compute_type": runtime.compute_type,
    }

    device_index = getattr(runtime, "cuda_device_index", None)
    if (
        isinstance(device, str)
        and device.casefold().startswith("cuda")
        and device_index is not None
    ):
        loader_kwargs["device"] = "cuda"
        loader_kwargs["device_index"] = device_index

    revision = getattr(runtime, "whisper_model_revision_commit", None)
    if revision:
        loader_kwargs["revision"] = revision
    return loader_kwargs


def _artifact_reason(owner, phase, error):
    if isinstance(error, FileNotFoundError):
        return f"{phase}_missing"
    if isinstance(error, owner.ArtifactSecurityError):
        return f"{phase}_unsafe"
    if isinstance(error, owner.ArtifactValidationError):
        return f"{phase}_invalid"
    return f"{phase}_unreadable"


def _exact_envelope_resolutions(runtime, runtime_identity, chunk_minutes):
    """Use validated artifacts as revision hints, then require exact resolution."""
    owner = runtime._model_envelope_catalog
    expected_uid = runtime.model_envelope_expected_uid
    try:
        catalog = owner.load_catalog(
            runtime.model_envelope_catalog_path,
            expected_uid=expected_uid,
        )
    except (FileNotFoundError, OSError, owner.ArtifactValidationError) as exc:
        return (), None, _artifact_reason(owner, "catalog", exc), {}
    try:
        identity = owner.load_identity(
            runtime.model_envelope_identity_path,
            expected_uid=expected_uid,
        )
    except (FileNotFoundError, OSError, owner.ArtifactValidationError) as exc:
        return (), catalog, _artifact_reason(owner, "identity", exc), {}

    decoder_digest = runtime.decoder_options_sha256
    if runtime_identity is None or decoder_digest is None:
        return (), catalog, "runtime_policy_mismatch", {}

    requested = runtime.requested_whisper_model
    automatic = requested.casefold() == "auto"
    candidates = (
        ("large-v3", "medium", "small", "base", "tiny") if automatic else (requested,)
    )
    resolutions = []
    envelope_keys = {}
    last_reason = "runtime_policy_mismatch"
    for model in candidates:
        configured_revision = runtime.whisper_model_revision if not automatic else None
        if configured_revision is not None:
            revisions = (configured_revision,)
        else:
            revisions = tuple(
                dict.fromkeys(
                    entry.policy.model_revision
                    for entry in catalog.entries
                    if entry.image_identity == identity.image_identity
                    and entry.runtime == runtime_identity
                    and entry.policy.model == model
                    and entry.policy.compute_type == runtime.compute_type
                    and entry.policy.task == runtime.transcribe_or_translate
                    and entry.policy.inference_concurrency
                    == runtime.concurrent_transcriptions
                    and entry.policy.chunk_minutes == chunk_minutes
                    and entry.policy.decoder_options_sha256 == decoder_digest
                )
            )
        if len(revisions) != 1:
            continue
        try:
            policy = owner.EnvelopePolicy(
                model=model,
                model_revision=revisions[0],
                compute_type=runtime.compute_type,
                task=runtime.transcribe_or_translate,
                inference_concurrency=runtime.concurrent_transcriptions,
                chunk_minutes=chunk_minutes,
                decoder_options_sha256=decoder_digest,
            )
            resolution = owner.resolve_envelope(
                runtime.model_envelope_catalog_path,
                runtime.model_envelope_identity_path,
                runtime=runtime_identity,
                policy=policy,
                canonical_shared_cuda=runtime.canonical_shared_cuda,
                expected_image_identity=identity.image_identity,
                expected_uid=expected_uid,
            )
        except (FileNotFoundError, OSError, owner.ArtifactValidationError) as exc:
            last_reason = _artifact_reason(owner, "envelope", exc)
            continue
        if not resolution.matched:
            last_reason = resolution.reason_code
            continue
        index = catalog.entries.index(resolution.envelope)
        resolutions.append(resolution)
        envelope_keys[model] = {
            "catalog_payload_sha256": (catalog.integrity.canonical_payload_sha256),
            "entry_index": index,
        }
    return (
        tuple(resolutions),
        catalog,
        (None if resolutions else last_reason),
        envelope_keys,
    )


def _initial_gpu_capacity(runtime, resources):
    samples = []

    def read_sample():
        sample = runtime.read_pressure_sample()
        samples.append(sample)
        return sample

    if runtime.cuda_device_index is None:
        immediate = read_sample()
        return None, None, immediate, tuple(samples)

    first = read_sample()
    expected_device = first.gpu_device_id
    stabilized = None
    if expected_device:
        pending = [first]

        def sample_reader():
            if pending:
                return pending.pop()
            return read_sample()

        stabilized = resources.stabilize_gpu_capacity(
            sample_reader,
            expected_device=expected_device,
            clock=runtime.model_runtime_clock,
            sleep=runtime.model_runtime_sleep,
        )
    immediate = read_sample()
    if expected_device is None:
        expected_device = immediate.gpu_device_id
    return stabilized, expected_device, immediate, tuple(samples)


def _configured_priority_reader(runtime):
    """Return the one startup-owned reader only when its path is configured."""

    probe = getattr(runtime, "priority_pressure_probe", None)
    if not bool(getattr(probe, "configured", False)):
        return None
    reader = getattr(runtime, "priority_pressure_reader", None)
    if not callable(reader):
        raise RuntimeError("Configured priority pressure reader is unavailable")
    return reader


def _configured_priority_receipt_observer(runtime):
    coordinator = getattr(runtime, "runtime_receipt_coordinator", None)
    if not bool(getattr(coordinator, "gate_enabled", False)):
        return None

    def publish(priority_snapshot):
        _record_runtime_receipt_locked(runtime, priority_snapshot)

    return publish


def _selection_status(
    runtime,
    decision,
    capacity,
    stabilized,
    gpu_total,
    gpu_reserve,
    envelope_reason,
    envelope_keys,
    controller,
):
    requirement = decision.requirement
    exact = requirement is not None and requirement.envelope_resolution is not None
    if exact:
        disposition = "exact_match"
    elif runtime.canonical_shared_cuda and not decision.explicit:
        disposition = "fail_closed"
    else:
        disposition = "public_fallback"
    admission = decision.admission
    return {
        "controller_state": controller.state,
        "recovery_reason": controller.recovery_reason,
        "admission_open": bool(
            controller.admission_open and not runtime.model_admission_closed
        ),
        "capacity_source": capacity.source,
        "requested_model": runtime.requested_whisper_model,
        "envelope_key": envelope_keys.get(decision.selected_model) if exact else None,
        "envelope_disposition": disposition,
        "envelope_reason": None if exact else envelope_reason,
        "selected_model": decision.selected_model,
        "model_explicit": decision.explicit,
        "automatic_ceiling": decision.automatic_ceiling,
        "decision_reason": decision.reason,
        "decision_provenance": decision.provenance,
        "gpu_total_bytes": gpu_total,
        "gpu_stabilized_free_bytes": (
            stabilized.free_bytes if stabilized is not None else None
        ),
        "gpu_reserve_bytes": gpu_reserve,
        "gpu_allocatable_bytes": (
            admission.device_admission_bytes if admission is not None else None
        ),
    }


def initialize_model_runtime(runtime):
    """Select one fixed model and construct its pressure controller once."""
    with runtime.model_selection_lock:
        if runtime.model_runtime_initialized:
            return runtime.model_decision
        with runtime.model_runtime_condition:
            selection_generation = runtime.model_release_generation
            selection_transition = runtime.model_release_transition
            selection_transition_was_active = _transition_active(selection_transition)

        resources = runtime._resource_management
        capacity = resources.discover_capacity()
        chunk_seconds = resources.initial_chunk_seconds(
            capacity,
            runtime.segmentation_chunk_minutes,
        )
        chunk_minutes = chunk_seconds // 60
        stabilized, expected_gpu, immediate, bootstrap_samples = _initial_gpu_capacity(
            runtime,
            resources,
        )
        gpu_total = (
            stabilized.total_bytes
            if stabilized is not None
            else immediate.gpu_total_bytes
        )
        gpu_reserve = None
        if runtime.cuda_device_index is not None:
            gpu_reserve = resources.gpu_priority_reserve_bytes(
                gpu_total,
                explicit_reserve_gib=runtime.gpu_memory_reserve_gib,
                canonical_shared_cuda=runtime.canonical_shared_cuda,
            )
        host_reserve = resources.host_reserve_bytes(
            capacity,
            explicit_reserve_gib=runtime.memory_pressure_reserve_gib,
        )

        runtime_identity = None
        if stabilized is not None:
            try:
                runtime_identity = runtime.build_runtime_identity(
                    stabilized.total_bytes,
                    expected_gpu,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                runtime_identity = None
        resolutions, _catalog, envelope_reason, envelope_keys = (
            _exact_envelope_resolutions(
                runtime,
                runtime_identity,
                chunk_minutes,
            )
        )
        decision = resources.select_model(
            runtime.requested_whisper_model,
            capacity,
            device=runtime.transcribe_device,
            admission_sample=immediate,
            stabilized_gpu=stabilized,
            host_reserve=host_reserve,
            gpu_reserve_bytes=gpu_reserve,
            expected_gpu_device=expected_gpu,
            envelopes=resolutions,
            canonical_shared_cuda=runtime.canonical_shared_cuda,
            require_cgroup=capacity.cgroup_limit_bytes is not None,
            now=runtime.model_runtime_clock(),
        )
        selected_model_identity_sha256 = None
        if (
            decision.requirement is not None
            and decision.requirement.envelope_resolution is not None
            and decision.requirement.envelope_resolution.envelope is not None
        ):
            selected_model_identity_sha256 = (
                runtime._model_envelope_catalog.model_identity_sha256(
                    decision.requirement.envelope_resolution.envelope
                )
            )
        if decision.explicit and decision.reason == "no_load_budget":
            raise RuntimeError(
                "Explicit WHISPER_MODEL has no conservative model-load budget"
            )

        selected_requirement = decision.requirement
        bootstrap_reselection = bool(
            runtime.cuda_device_index is not None and expected_gpu is None
        )
        controller_requirement = None if bootstrap_reselection else selected_requirement
        controller_recovery_requirements = (
            () if bootstrap_reselection else decision.recovery_requirements
        )
        priority_receipt_observer = _configured_priority_receipt_observer(runtime)
        controller = resources.PressureController(
            sample_reader=getattr(
                runtime,
                "read_resource_pressure_sample",
                runtime.read_pressure_sample,
            ),
            reserve_bytes=host_reserve,
            gpu_reserve_bytes=None if bootstrap_reselection else gpu_reserve,
            expected_gpu_device=None if bootstrap_reselection else expected_gpu,
            canonical_shared_cuda=(
                runtime.canonical_shared_cuda and not bootstrap_reselection
            ),
            explicit_model_authority=(
                decision.explicit and controller_requirement is not None
            ),
            selected_requirement=controller_requirement,
            recovery_requirements=controller_recovery_requirements,
            require_cgroup=capacity.cgroup_limit_bytes is not None,
            clock=runtime.model_runtime_clock,
            sleep=runtime.model_runtime_sleep,
            priority_reader=_configured_priority_reader(runtime),
            priority_observer=priority_receipt_observer,
            priority_transition_lock=(
                runtime.model_runtime_condition
                if priority_receipt_observer is not None
                else None
            ),
        )

        observe = getattr(controller, "observe", None)
        if callable(observe):
            for bootstrap_sample in bootstrap_samples:
                observe(bootstrap_sample, model_resident=False)

        model_selection_admitted = bool(
            not bootstrap_reselection
            and decision.selected_model is not None
            and selected_requirement is not None
            and decision.admitted
        )
        if model_selection_admitted:
            open_if_normal = getattr(controller, "open_admission_if_normal", None)
            if callable(open_if_normal):
                controller_ready = open_if_normal()
            else:
                controller.admission_open = True
                controller_ready = True
        else:
            controller.enter_no_safe_model(controller_recovery_requirements)
            controller_ready = False

        with runtime.model_runtime_condition:
            transition = runtime.model_release_transition
            release_during_selection = (
                runtime.model_release_generation != selection_generation
                or runtime.model_release_transition is not selection_transition
                or selection_transition_was_active
            )
            while _transition_active(transition):
                if _cancelled(_runtime_cancellation(runtime)):
                    _raise_if_profile_unhealthy(runtime)
                    raise ModelRuntimeCancelled(
                        "Model selection recovery was cancelled"
                    )
                release_during_selection = True
                runtime.model_runtime_condition.wait(timeout=1.0)
                transition = runtime.model_release_transition
            if (
                transition is not None
                and transition.complete
                and transition.failure is not None
            ):
                _raise_release_failure(transition.failure)

            published_admission = bool(
                model_selection_admitted
                and controller_ready
                and not release_during_selection
            )
            if model_selection_admitted and release_during_selection:
                controller.mark_released(
                    getattr(transition, "reason", None) or "release_during_selection"
                )

            runtime.model_capacity_profile = capacity
            runtime.model_chunk_baseline_seconds = chunk_seconds
            runtime.model_stabilized_gpu = stabilized
            runtime.model_decision = decision
            runtime.model_requirement = controller_requirement
            runtime.selected_model_identity_sha256 = selected_model_identity_sha256
            runtime.model_pressure_controller = controller
            runtime.model_runtime_initialized = True
            if decision.selected_model is not None:
                runtime.whisper_model = decision.selected_model
                if selected_requirement.envelope_resolution is not None:
                    revision = selected_requirement.envelope_resolution.envelope.policy.model_revision
                    runtime.whisper_model_revision = revision
                    runtime.whisper_model_revision_commit = revision.removeprefix("hf:")

            runtime.model_admission_closed = not published_admission
            runtime.model_runtime_status = _selection_status(
                runtime,
                decision,
                capacity,
                stabilized,
                gpu_total,
                gpu_reserve,
                envelope_reason,
                envelope_keys,
                controller,
            )
            _record_runtime_receipt_locked(runtime)
            _notify_all(runtime.model_runtime_condition)
        if decision.warning:
            runtime.logging.warning("Model policy: %s", decision.warning)
        runtime.logging.info(
            "Model policy selected=%s provenance=%s capacity=%s "
            "gpu_total=%s gpu_free=%s gpu_reserve=%s",
            decision.selected_model,
            decision.provenance,
            capacity.source,
            runtime.model_runtime_status["gpu_total_bytes"],
            runtime.model_runtime_status["gpu_stabilized_free_bytes"],
            gpu_reserve,
        )
        return decision


def _load_model_once(runtime, source_generation=None):
    """Attempt one fresh, in-lock load; return false for capacity deferral."""
    with runtime.model_load_lock:
        coordinator = getattr(runtime, "runtime_receipt_coordinator", None)
        gate_enabled = bool(getattr(coordinator, "gate_enabled", False))
        if source_generation is not None and (
            runtime.model_admission_closed
            or runtime.model_release_generation != source_generation
        ):
            return _LOAD_DEFERRED_STALE_GENERATION
        if runtime.model is not None:
            if gate_enabled and getattr(runtime, "model_admission_closed", True):
                return _LOAD_DEFERRED_STALE_GENERATION
            return True
        if not _fresh_model_admission(runtime):
            return _LOAD_DEFERRED_CAPACITY
        selected_identity = getattr(runtime, "selected_model_identity_sha256", None)
        if gate_enabled and not selected_identity:
            raise RuntimeError(
                "Task 11B gate requires an exact selected ModelEnvelope identity"
            )
        runtime.logging.debug("Model was purged, need to re-create")
        allocation_diagnostic = None
        try:
            loaded = runtime.stable_whisper.load_faster_whisper(
                runtime.whisper_model,
                **_model_loader_kwargs(runtime),
            )
        except Exception as exc:
            resources = getattr(runtime, "_resource_management", None)
            classifier = getattr(resources, "is_allocation_failure", None)
            if not callable(classifier) or not classifier(exc):
                raise
            _record_classified_cuda_oom(runtime, exc)
            allocation_diagnostic = type(exc).__name__[:80]
        if allocation_diagnostic is not None:
            runtime.model_load_allocation_failures += 1
            close_model_admission(runtime)
            raise _ModelLoadAllocationFailure(
                runtime.model_load_allocation_failures,
                allocation_diagnostic,
            ) from None
        condition = getattr(runtime, "model_runtime_condition", None)
        if condition is None:
            runtime.model = loaded
            runtime.resident_model_identity_sha256 = selected_identity
            _increment_generation_locked(runtime, "model_load_generation")
        else:
            with condition:
                runtime.model = loaded
                runtime.resident_model_identity_sha256 = selected_identity
                _increment_generation_locked(runtime, "model_load_generation")
                try:
                    _record_runtime_receipt_locked(runtime)
                except BaseException:
                    try:
                        did_unload = _unload_resident_model_under_lock(runtime)
                    except BaseException:
                        did_unload = False
                    if did_unload:
                        _increment_generation_locked(
                            runtime,
                            "model_unload_generation",
                        )
                    _notify_all(condition)
                    raise
                _notify_all(condition)
        runtime.model_load_allocation_failures = 0
        return True


def _prepare_no_safe_reselection(runtime, controller):
    """Let one waiter re-run auto selection after the shared controller recovers."""
    with runtime.model_selection_lock:
        if (
            runtime.model_pressure_controller is controller
            and runtime.model_requirement is None
            and controller.state == getattr(controller, "NORMAL", "normal")
            and controller.admission_open
        ):
            runtime.model_runtime_initialized = False


def _wait_for_bootstrap_reselection(runtime, controller, cancelled):
    """Bound retries when no exact recovery requirement can yet be constructed."""
    delay = getattr(controller, "sample_interval_seconds", 1.0)
    runtime.logging.debug(
        "Model selection evidence unavailable; retrying in %.1f seconds",
        delay,
    )
    event_wait = getattr(cancelled, "wait", None)
    if callable(event_wait) and callable(getattr(cancelled, "is_set", None)):
        if event_wait(delay):
            return False
    else:
        runtime.model_runtime_sleep(delay)
    if _cancelled(cancelled):
        return False
    with runtime.model_selection_lock:
        if (
            runtime.model_pressure_controller is controller
            and runtime.model_requirement is None
        ):
            runtime.model_runtime_initialized = False
    return True


def _mark_model_profile_unhealthy(runtime):
    condition = runtime.model_runtime_condition
    with condition:
        runtime.model_profile_unhealthy = True
        runtime.model_profile_unhealthy_reason = "model_load_profile_unhealthy"
        runtime.model_admission_closed = True
        cancel = _runtime_cancellation(runtime)
        set_cancelled = getattr(cancel, "set", None)
        if callable(set_cancelled):
            set_cancelled()
        status = dict(getattr(runtime, "model_runtime_status", {}))
        status["recovery_reason"] = "model_load_profile_unhealthy"
        status["admission_open"] = False
        runtime.model_runtime_status = status
        _record_runtime_receipt_locked(runtime)
        _notify_all(condition)


def _handle_model_load_allocation_failure(
    runtime,
    failure,
    *,
    source_generation,
    cancelled,
):
    _release_model_once(
        runtime,
        reason="model_load_allocation_failure",
        source_generation=source_generation,
    )
    if failure.attempt_count >= 2:
        _mark_model_profile_unhealthy(runtime)
        _raise_if_profile_unhealthy(runtime)
    if not wait_for_model_recovery(runtime, cancelled):
        raise ModelRuntimeCancelled("Model load recovery was cancelled")


def transcribe_with_model(runtime, *args, **transcribe_kwargs):
    """Run one inference through a generation-safe, pressure-aware gate."""
    if not _coordinated_runtime(runtime):
        pressure_errors = []
        call_kwargs = _compose_pressure_callback(
            runtime,
            transcribe_kwargs,
            pressure_errors,
        )
        pressure_error = None
        allocation_error = None
        with runtime.model_inference_semaphore:
            try:
                return runtime.model.transcribe(*args, **call_kwargs)
            except Exception as exc:
                if pressure_errors and exc is pressure_errors[-1]:
                    pressure_error = _bind_pressure_release_ticket(
                        runtime,
                        exc,
                        None,
                    )
                elif _is_inference_allocation_failure(runtime, exc):
                    _record_classified_cuda_oom(runtime, exc)
                    close_model_admission(runtime)
                    allocation_error = _bind_inference_allocation_ticket(
                        exc,
                        None,
                    )
                else:
                    raise
        if pressure_error is not None:
            pressure_errors.clear()
            raise pressure_error.with_traceback(None) from None
        if allocation_error is not None:
            pressure_errors.clear()
            raise allocation_error.with_traceback(None) from None

    pressure_errors = []
    call_kwargs = _compose_pressure_callback(
        runtime,
        transcribe_kwargs,
        pressure_errors,
    )
    cancelled = _runtime_cancellation(runtime)
    while True:
        _raise_if_profile_unhealthy(runtime)
        generation = _acquire_inference_slot(runtime, cancelled)
        if generation is None:
            raise ModelRuntimeCancelled("Model inference admission was cancelled")

        pressure_error = None
        inference_allocation_error = None
        allocation_failure = None
        load_deferred = None
        try:
            load_result = _load_model_once(runtime, generation)
            if load_result is not True:
                load_deferred = load_result
            else:
                return runtime.model.transcribe(*args, **call_kwargs)
        except _ModelLoadAllocationFailure as exc:
            allocation_failure = exc
        except Exception as exc:
            if pressure_errors and exc is pressure_errors[-1]:
                pressure_error = _bind_pressure_release_ticket(
                    runtime,
                    exc,
                    generation,
                )
            elif _is_inference_allocation_failure(runtime, exc):
                _record_classified_cuda_oom(runtime, exc)
                close_model_admission(runtime)
                inference_allocation_error = _bind_inference_allocation_ticket(
                    exc,
                    generation,
                )
            else:
                raise
        finally:
            _release_inference_slot(runtime)

        if load_deferred is _LOAD_DEFERRED_STALE_GENERATION:
            if not wait_for_model_admission(runtime, cancelled):
                raise ModelRuntimeCancelled("Model load admission was cancelled")
            continue
        if load_deferred is _LOAD_DEFERRED_CAPACITY:
            if not wait_for_model_recovery(runtime, cancelled):
                raise ModelRuntimeCancelled("Model load recovery was cancelled")
            continue
        if allocation_failure is not None:
            _handle_model_load_allocation_failure(
                runtime,
                allocation_failure,
                source_generation=generation,
                cancelled=cancelled,
            )
            continue
        if pressure_error is not None:
            # The caller owns the inference payload.  Propagate only after the
            # permit is returned; that caller can then strip this traceback,
            # drop its audio/result references, and request canonical release.
            # Releasing here would run allocator cleanup while the payload is
            # still strongly reachable from both this frame and its caller.
            pressure_errors.clear()
            raise pressure_error.with_traceback(None) from None
        if inference_allocation_error is not None:
            pressure_errors.clear()
            raise inference_allocation_error.with_traceback(None) from None


def start_model(runtime):
    """Initialize, freshly admit, and load the configured model once."""
    initialize = getattr(runtime, "initialize_model_runtime", None)
    if not _coordinated_runtime(runtime):
        if callable(initialize):
            initialize()
        _load_model_once(runtime)
        return

    cancelled = _runtime_cancellation(runtime)
    while True:
        _raise_if_profile_unhealthy(runtime)
        if _cancelled(cancelled):
            raise ModelRuntimeCancelled("Model load admission was cancelled")
        if callable(initialize):
            initialize()
        _raise_if_profile_unhealthy(runtime)
        if _cancelled(cancelled):
            raise ModelRuntimeCancelled("Model load admission was cancelled")
        if getattr(runtime, "model_requirement", None) is None:
            controller = runtime.model_pressure_controller
            recovery_requirements = tuple(
                getattr(controller, "recovery_requirements", ())
            )
            if not recovery_requirements:
                if not _wait_for_bootstrap_reselection(
                    runtime,
                    controller,
                    cancelled,
                ):
                    _raise_if_profile_unhealthy(runtime)
                    raise ModelRuntimeCancelled(
                        "Model selection recovery was cancelled"
                    )
                continue
            if not wait_for_model_recovery(runtime, cancelled):
                raise ModelRuntimeCancelled("Model selection recovery was cancelled")
            _prepare_no_safe_reselection(runtime, controller)
            continue
        generation = _acquire_inference_slot(runtime, cancelled)
        if generation is None:
            raise ModelRuntimeCancelled("Model load admission was cancelled")
        allocation_failure = None
        try:
            load_result = _load_model_once(runtime, generation)
        except _ModelLoadAllocationFailure as exc:
            load_result = None
            allocation_failure = exc
        finally:
            _release_inference_slot(runtime)
        if allocation_failure is not None:
            _handle_model_load_allocation_failure(
                runtime,
                allocation_failure,
                source_generation=generation,
                cancelled=cancelled,
            )
            continue
        if load_result is True:
            return
        if load_result is _LOAD_DEFERRED_STALE_GENERATION:
            if not wait_for_model_admission(runtime, cancelled):
                raise ModelRuntimeCancelled("Model load admission was cancelled")
            continue
        if not wait_for_model_recovery(runtime, cancelled):
            raise ModelRuntimeCancelled("Model load recovery was cancelled")


def _release_accelerator_and_allocator_caches(runtime):
    _backend_release.release_allocator_caches(
        gc_module=runtime.gc,
        torch_module=runtime.torch,
        device=getattr(runtime, "transcribe_device", ""),
        cuda_device_index=getattr(runtime, "cuda_device_index", None),
        os_module=runtime.os,
        ctypes_module=runtime.ctypes,
        logger=runtime.logging,
    )


def _unload_resident_model_under_lock(runtime):
    """Unload the backend and clear resident state before fallible cache cleanup."""

    resident = runtime.model
    did_unload = False
    if resident is not None:
        _backend_release.unload_verified_backend(resident)
        runtime.model = None
        runtime.resident_model_identity_sha256 = None
        did_unload = True
    return did_unload


def _release_model_once(runtime, reason=None, source_generation=None):
    """Release once per generation after closing and draining admission."""
    if not _coordinated_runtime(runtime):
        with runtime.model_load_lock:
            did_unload = _unload_resident_model_under_lock(runtime)
            if did_unload:
                _increment_generation(runtime, "model_unload_generation")
                runtime.logging.info("Model unloaded from memory")
            _release_accelerator_and_allocator_caches(runtime)
            return did_unload

    condition = runtime.model_runtime_condition
    with condition:
        transition = runtime.model_release_transition
        if _transition_active(transition):
            owner = False
        elif (
            source_generation is not None
            and transition is not None
            and transition.complete
            and transition.generation > source_generation
        ):
            owner = False
        elif (
            source_generation is None
            and transition is not None
            and transition.complete
            and transition.failure is None
            and transition.generation == runtime.model_release_generation
            and runtime.model_admission_closed
        ):
            owner = False
        else:
            owner = True
            if not runtime.model_admission_closed:
                runtime.model_admission_closed = True
                runtime.model_release_generation += 1
            elif (
                transition is not None
                and transition.complete
                and runtime.model_release_generation <= transition.generation
            ):
                runtime.model_release_generation += 1
            transition = _ReleaseTransition(runtime.model_release_generation, reason)
            runtime.model_release_transition = transition
            _record_runtime_receipt_locked(runtime)
            _notify_all(condition)

    if not owner:
        with condition:
            while not transition.complete:
                condition.wait(timeout=5.0)
        if transition.failure is not None:
            _raise_release_failure(transition.failure)
        return transition.did_unload

    acquired_permits = 0
    release_failure = None
    release_aborted = False
    did_unload = False
    try:
        for _ in range(runtime.model_inference_permit_count):
            runtime.model_inference_semaphore.acquire()
            acquired_permits += 1
        with runtime.model_load_lock:
            if reason == "idle_cleanup":
                with runtime.active_direct_tasks_lock:
                    release_aborted = not (
                        runtime.task_queue.is_idle()
                        and runtime.active_direct_tasks == 0
                    )
            if not release_aborted:
                with condition:
                    did_unload = _unload_resident_model_under_lock(runtime)
                    if did_unload:
                        _increment_generation_locked(
                            runtime,
                            "model_unload_generation",
                        )
                    controller = getattr(runtime, "model_pressure_controller", None)
                    if controller is not None:
                        controller.mark_released(reason)
                    _record_runtime_receipt_locked(runtime)
                    if did_unload:
                        runtime.logging.info("Model unloaded from memory")
                    _release_accelerator_and_allocator_caches(runtime)
    except BaseException as exc:
        release_failure = _release_failure_diagnostic(exc)
        with condition:
            runtime.model_admission_closed = True
            controller = getattr(runtime, "model_pressure_controller", None)
            latch = getattr(
                controller,
                "latch_release_failure_without_publication",
                None,
            )
            model_resident = runtime.model is not None
            if callable(latch):
                latch(model_resident=model_resident)
            elif controller is not None:
                controller.state = getattr(
                    controller,
                    "YIELDING" if model_resident else "RECOVERING",
                    "yielding" if model_resident else "recovering",
                )
                controller.admission_open = False
                controller.recovery_reason = "model_release_failed"
            status = getattr(runtime, "model_runtime_status", None)
            if isinstance(status, dict):
                status = dict(status)
                status["controller_state"] = getattr(
                    controller,
                    "state",
                    "yielding",
                )
                status["recovery_reason"] = "model_release_failed"
                status["admission_open"] = False
                runtime.model_runtime_status = status
            _notify_all(condition)
    finally:
        for _ in range(acquired_permits):
            runtime.model_inference_semaphore.release()
        with condition:
            transition.did_unload = did_unload
            transition.failure = release_failure
            transition.complete = True
            if (
                release_failure is None
                and getattr(runtime, "model_pressure_controller", None) is None
            ):
                runtime.model_admission_closed = False
                _record_runtime_receipt_locked(runtime)
            _notify_all(condition)

    if release_failure is not None:
        try:
            runtime.gc.collect()
        except Exception:
            pass
        _raise_release_failure(release_failure)
    if release_aborted:
        reopen_model_admission(runtime)
        return False
    return did_unload


def release_model(runtime, reason=None):
    """Release through the public single-flight coordinator entry point."""
    return _release_model_once(runtime, reason=reason)


def release_after_pressure(runtime, error):
    """Release only the model generation bound to a propagated pressure yield."""
    pressure_type = runtime._resource_management.MemoryPressureYield
    if not isinstance(error, pressure_type):
        raise TypeError("Pressure release requires MemoryPressureYield")
    return release_after_inference_failure(runtime, error)


def release_after_inference_failure(runtime, error):
    """Release only the generation bound to a pressure/allocation control."""
    ticket = getattr(error, _PRESSURE_RELEASE_TICKET_ATTR, None)
    if not isinstance(ticket, tuple) or len(ticket) != 2:
        raise ValueError("Inference control error is missing its release ticket")
    source_generation, reason = ticket
    if not _coordinated_runtime(runtime):
        return release_model(runtime, reason=reason)
    return _release_model_once(
        runtime,
        reason=reason,
        source_generation=source_generation,
    )


def observe_idle_once(runtime):
    """Apply one resident-idle pressure observation without racing inference."""
    controller = getattr(runtime, "model_pressure_controller", None)
    if controller is None or not _coordinated_runtime(runtime):
        return False

    with runtime.model_runtime_condition:
        active = runtime.model_active_inferences
        transition_active = _transition_active(runtime.model_release_transition)
        resident = runtime.model is not None

    priority_configured = bool(getattr(controller, "priority_configured", False))
    if priority_configured:
        poll_priority = getattr(controller, "poll_priority", None)
        if callable(poll_priority):
            poll_priority(model_resident=resident)
        else:
            controller.poll(model_resident=resident)
        if not controller.admission_open:
            close_model_admission(runtime)
    if active or transition_active:
        return False

    if resident:
        should_release = controller.poll_idle_resident()
        if not controller.admission_open:
            close_model_admission(runtime)
        if should_release:
            release_model(
                runtime,
                reason=controller.recovery_reason or "memory_pressure",
            )
            return True
        return False

    normal = getattr(controller, "NORMAL", "normal")
    if priority_configured or controller.state != normal:
        controller.poll(model_resident=False)
    if not controller.admission_open:
        close_model_admission(runtime)
    elif controller.state == normal:
        reopen_model_admission(runtime)
    return False


def run_model_idle_observer(runtime):
    """Poll idle model pressure on the controller's required cadence."""
    stop = runtime.model_idle_observer_stop
    while True:
        controller = getattr(runtime, "model_pressure_controller", None)
        interval = getattr(controller, "poll_interval_seconds", None)
        if interval is None:
            interval = 1.0
        if stop.wait(interval):
            return
        try:
            observe_idle_once(runtime)
        except Exception as exc:
            close_model_admission(runtime)
            runtime.logging.error(
                "Model idle observer failed closed (%s)",
                type(exc).__name__,
            )


def _precontroller_priority_status(runtime, generations):
    """Return the exact fail-closed priority shape before controller startup."""

    configured = bool(
        getattr(getattr(runtime, "priority_pressure_probe", None), "configured", False)
    )
    return {
        "configured": configured,
        "state": "unavailable" if configured else "disabled",
        "heartbeat_age_ms": None,
        "source_age_ms": None,
        "policy_sha256": None,
        "observation_digest": None,
        "transition_observation_digest": None,
        "transition_sequence": 0,
        "controller_phase": "recovering" if configured else "normal",
        "recovery_reason": "priority_pressure" if configured else None,
        "distinct_clear_count": 0,
        "model_resident": generations["model_resident"],
        "model_load_generation": generations["model_load_generation"],
        "model_unload_generation": generations["model_unload_generation"],
    }


def runtime_status(runtime):
    """Return a bounded, JSON-safe snapshot without deployment identifiers."""
    allowed = (
        "controller_state",
        "recovery_reason",
        "admission_open",
        "capacity_source",
        "requested_model",
        "envelope_key",
        "envelope_disposition",
        "envelope_reason",
        "selected_model",
        "model_explicit",
        "automatic_ceiling",
        "decision_reason",
        "decision_provenance",
        "gpu_total_bytes",
        "gpu_stabilized_free_bytes",
        "gpu_reserve_bytes",
        "gpu_allocatable_bytes",
    )

    def snapshot_status():
        status = getattr(runtime, "model_runtime_status", {})
        snapshot = {name: status.get(name) for name in allowed}
        generations = _runtime_generation_snapshot_locked(runtime)
        snapshot["failure_counters"] = {
            "cuda_oom_generation": generations["cuda_oom_generation"],
            "media_failure_generation": generations["media_failure_generation"],
        }
        coordinator = getattr(runtime, "runtime_receipt_coordinator", None)
        if coordinator is not None:
            snapshot["workload"] = coordinator.workload_snapshot_locked()
            snapshot["runtime_identity"] = coordinator.runtime_identity_snapshot()
        controller = getattr(runtime, "model_pressure_controller", None)
        if controller is not None:
            runtime_snapshot = getattr(controller, "runtime_status_snapshot", None)
            if callable(runtime_snapshot):
                controller_status = runtime_snapshot(generations)
                snapshot["controller_state"] = controller_status["controller_state"]
                snapshot["recovery_reason"] = controller_status["recovery_reason"]
                snapshot["admission_open"] = bool(
                    controller_status["admission_open"]
                    and not getattr(runtime, "model_admission_closed", True)
                )
                snapshot["priority_pressure"] = controller_status["priority_pressure"]
            else:
                snapshot["controller_state"] = controller.state
                snapshot["recovery_reason"] = controller.recovery_reason
                snapshot["admission_open"] = bool(
                    controller.admission_open
                    and not getattr(runtime, "model_admission_closed", True)
                )
                priority_snapshot = getattr(
                    controller, "priority_status_snapshot", None
                )
                snapshot["priority_pressure"] = (
                    priority_snapshot(generations)
                    if callable(priority_snapshot)
                    else _precontroller_priority_status(runtime, generations)
                )
        else:
            snapshot["priority_pressure"] = _precontroller_priority_status(
                runtime,
                generations,
            )
        if getattr(runtime, "model_profile_unhealthy", False):
            snapshot["recovery_reason"] = "model_load_profile_unhealthy"
            snapshot["admission_open"] = False
        return snapshot

    condition = getattr(runtime, "model_runtime_condition", None)
    if condition is None:
        return snapshot_status()
    with condition:
        return snapshot_status()


def schedule_model_cleanup(runtime):
    """Replace the delayed cleanup timer and join its predecessor safely."""
    previous_timer = None
    with runtime.model_cleanup_lock:
        if runtime.model_cleanup_timer is not None:
            runtime.model_cleanup_timer.cancel()
            runtime.logging.debug("Cancelled previous model cleanup timer")
            previous_timer = runtime.model_cleanup_timer

        timer_holder = {}

        def scheduled_cleanup():
            return _perform_model_cleanup(
                runtime,
                expected_timer=timer_holder["timer"],
            )

        next_timer = runtime.Timer(
            runtime.model_cleanup_delay,
            scheduled_cleanup,
        )
        timer_holder["timer"] = next_timer
        runtime.model_cleanup_timer = next_timer
        next_timer.daemon = True
        next_timer.start()
        runtime.logging.debug(
            f"Model cleanup scheduled in {runtime.model_cleanup_delay} seconds"
        )

    if previous_timer is not None:
        previous_timer.join(timeout=1)


def _perform_legacy_model_cleanup(runtime, expected_timer=_ANY_CLEANUP_TIMER):
    """Unload an idle model and release accelerator and allocator caches."""
    with runtime.model_cleanup_lock:
        if (
            expected_timer is not _ANY_CLEANUP_TIMER
            and runtime.model_cleanup_timer is not expected_timer
        ):
            return False
        runtime.model_cleanup_timer = None
        runtime.logging.debug("Executing scheduled model cleanup")

        system_is_idle = False
        if runtime.clear_vram_on_complete:
            # A newly arriving task must pass start_model() under this same
            # lock. Rechecking activity after acquiring it prevents cleanup
            # from unloading the model between that task's load check and use.
            with runtime.model_load_lock:
                with runtime.active_direct_tasks_lock:
                    system_is_idle = (
                        runtime.task_queue.is_idle()
                        and runtime.active_direct_tasks == 0
                    )

                if system_is_idle:
                    runtime.logging.debug(
                        "Queue and direct tasks idle; clearing model from memory."
                    )
                    if runtime.model:
                        try:
                            runtime.model.model.unload_model()
                            del runtime.model
                            runtime.model = None
                            runtime.logging.info("Model unloaded from memory")
                        except Exception as exc:
                            runtime.logging.error(f"Error unloading model: {exc}")

                    if (
                        runtime.transcribe_device.lower() == "cuda"
                        and runtime.torch.cuda.is_available()
                    ):
                        try:
                            runtime.torch.cuda.empty_cache()
                            runtime.logging.debug("CUDA cache cleared.")
                        except Exception as exc:
                            runtime.logging.error(f"Error clearing CUDA cache: {exc}")

        if not runtime.clear_vram_on_complete or not system_is_idle:
            runtime.logging.debug(
                "Queue not idle or clear_vram disabled; skipping model cleanup"
            )

        if runtime.os.name != "nt":
            runtime.gc.collect()
            runtime.ctypes.CDLL(runtime.ctypes.util.find_library("c")).malloc_trim(0)

        return system_is_idle


def _perform_model_cleanup(runtime, expected_timer=_ANY_CLEANUP_TIMER):
    if not _coordinated_runtime(runtime):
        return _perform_legacy_model_cleanup(runtime, expected_timer)

    with runtime.model_cleanup_lock:
        if (
            expected_timer is not _ANY_CLEANUP_TIMER
            and runtime.model_cleanup_timer is not expected_timer
        ):
            return False
        runtime.logging.debug("Executing scheduled model cleanup")
        runtime.model_cleanup_timer = None
    if not runtime.clear_vram_on_complete:
        runtime.logging.debug(
            "Queue not idle or clear_vram disabled; skipping model cleanup"
        )
        return None

    with runtime.active_direct_tasks_lock:
        system_is_idle = (
            runtime.task_queue.is_idle() and runtime.active_direct_tasks == 0
        )
    if not system_is_idle:
        runtime.logging.debug(
            "Queue not idle or clear_vram disabled; skipping model cleanup"
        )
        return None

    runtime.logging.debug("Queue and direct tasks idle; clearing model from memory.")
    try:
        return release_model(runtime, reason="idle_cleanup")
    except Exception as exc:
        runtime.logging.error("Error unloading model: %s", exc)
        return False


def perform_model_cleanup(runtime):
    """Release an idle model through the single-flight coordinator."""
    return _perform_model_cleanup(runtime)


def delete_model(runtime):
    """Schedule cleanup only after queued and direct work have become idle."""
    if not runtime.clear_vram_on_complete:
        return

    with runtime.active_direct_tasks_lock:
        system_is_idle = (
            runtime.task_queue.is_idle() and runtime.active_direct_tasks == 0
        )

    if system_is_idle:
        runtime.schedule_model_cleanup()
    else:
        runtime.logging.debug(
            "Tasks still in queue or processing; skipping model cleanup scheduling."
        )
