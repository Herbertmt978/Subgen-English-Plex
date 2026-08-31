"""Model loading, shared inference gating, and delayed cleanup algorithms.

All process state and runtime dependencies remain on the injected ``runtime``.
The pressure-release lock order is admission barrier, every inference permit,
then ``model_load_lock``.  A callback-triggered release is requested only after
the callback has unwound and its inference permit has been returned.
"""


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
            controller.admission_open = False
        if not runtime.model_admission_closed:
            runtime.model_admission_closed = True
            runtime.model_release_generation += 1
        generation = runtime.model_release_generation
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
            normal = getattr(controller, "NORMAL", "normal")
            if getattr(controller, "state", None) != normal or not getattr(
                controller, "admission_open", False
            ):
                return False
        if _transition_active(runtime.model_release_transition):
            return False
        runtime.model_admission_closed = False
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


def _bind_pressure_release_ticket(runtime, error, source_generation):
    """Return a fresh control signal carrying one immutable release epoch."""
    pressure_type = runtime._resource_management.MemoryPressureYield
    rebound = pressure_type(*error.args)
    controller = getattr(runtime, "model_pressure_controller", None)
    reason = getattr(controller, "recovery_reason", None) or "memory_pressure"
    setattr(
        rebound,
        _PRESSURE_RELEASE_TICKET_ATTR,
        (source_generation, reason),
    )
    error.__traceback__ = None
    error.__context__ = None
    error.__cause__ = None
    return rebound


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
    if runtime.cuda_device_index is None:
        return None, None, runtime.read_pressure_sample()

    first = runtime.read_pressure_sample()
    expected_device = first.gpu_device_id
    stabilized = None
    if expected_device:
        pending = [first]

        def sample_reader():
            if pending:
                return pending.pop()
            return runtime.read_pressure_sample()

        stabilized = resources.stabilize_gpu_capacity(
            sample_reader,
            expected_device=expected_device,
            clock=runtime.model_runtime_clock,
            sleep=runtime.model_runtime_sleep,
        )
    immediate = runtime.read_pressure_sample()
    if expected_device is None:
        expected_device = immediate.gpu_device_id
    return stabilized, expected_device, immediate


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
        stabilized, expected_gpu, immediate = _initial_gpu_capacity(
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
        controller = resources.PressureController(
            sample_reader=runtime.read_pressure_sample,
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
        )

        admitted_selection = bool(
            not bootstrap_reselection
            and decision.selected_model is not None
            and selected_requirement is not None
            and decision.admitted
        )
        if admitted_selection:
            controller.admission_open = True
        else:
            controller.enter_no_safe_model(controller_recovery_requirements)

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

            published_admission = admitted_selection and not release_during_selection
            if admitted_selection and release_during_selection:
                controller.mark_released(
                    getattr(transition, "reason", None) or "release_during_selection"
                )

            runtime.model_capacity_profile = capacity
            runtime.model_stabilized_gpu = stabilized
            runtime.model_decision = decision
            runtime.model_requirement = controller_requirement
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
        if source_generation is not None and (
            runtime.model_admission_closed
            or runtime.model_release_generation != source_generation
        ):
            return _LOAD_DEFERRED_STALE_GENERATION
        if runtime.model is not None:
            return True
        if not _fresh_model_admission(runtime):
            return _LOAD_DEFERRED_CAPACITY
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
            allocation_diagnostic = type(exc).__name__[:80]
        if allocation_diagnostic is not None:
            runtime.model_load_allocation_failures += 1
            close_model_admission(runtime)
            raise _ModelLoadAllocationFailure(
                runtime.model_load_allocation_failures,
                allocation_diagnostic,
            ) from None
        runtime.model = loaded
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
    delay = getattr(controller, "sample_interval_seconds", 5.0)
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
                else:
                    raise
        if pressure_error is not None:
            pressure_errors.clear()
            raise pressure_error.with_traceback(None) from None

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
    # Drop exception tracebacks, decoded audio, and backend-owned tensors before
    # asking the accelerator allocator to return its now-unused cache.
    runtime.gc.collect()

    device = getattr(runtime, "transcribe_device", "")
    if (
        isinstance(device, str)
        and device.casefold().startswith("cuda")
        and runtime.torch.cuda.is_available()
    ):
        synchronize = getattr(runtime.torch.cuda, "synchronize", None)
        if callable(synchronize):
            synchronize(getattr(runtime, "cuda_device_index", None))
        runtime.torch.cuda.empty_cache()
        if callable(synchronize):
            synchronize(getattr(runtime, "cuda_device_index", None))
        runtime.logging.debug("CUDA cache cleared.")

    if runtime.os.name != "nt":
        library_name = runtime.ctypes.util.find_library("c")
        if library_name:
            runtime.ctypes.CDLL(library_name).malloc_trim(0)


def _unload_model_under_lock(runtime):
    resident = runtime.model
    did_unload = False
    if resident is not None:
        backend = getattr(resident, "model", None)
        unload = getattr(backend, "unload_model", None)
        if not callable(unload):
            raise RuntimeError("Loaded model backend cannot be unloaded")
        unload()
        if getattr(backend, "model_is_loaded", False):
            raise RuntimeError("Loaded model backend did not confirm release")
        runtime.model = None
        did_unload = True
        runtime.logging.info("Model unloaded from memory")
    _release_accelerator_and_allocator_caches(runtime)
    return did_unload


def _release_model_once(runtime, reason=None, source_generation=None):
    """Release once per generation after closing and draining admission."""
    if not _coordinated_runtime(runtime):
        with runtime.model_load_lock:
            return _unload_model_under_lock(runtime)

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
                did_unload = _unload_model_under_lock(runtime)
                controller = getattr(runtime, "model_pressure_controller", None)
                if controller is not None:
                    controller.mark_released(reason)
    except BaseException as exc:
        release_failure = _release_failure_diagnostic(exc)
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
    ticket = getattr(error, _PRESSURE_RELEASE_TICKET_ATTR, None)
    if not isinstance(ticket, tuple) or len(ticket) != 2:
        raise ValueError("MemoryPressureYield is missing its release ticket")
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
    if controller.state != normal:
        controller.poll(model_resident=False)
        if controller.state == normal:
            reopen_model_admission(runtime)
    return False


def run_model_idle_observer(runtime):
    """Poll idle model pressure on the controller's five-second cadence."""
    stop = runtime.model_idle_observer_stop
    while True:
        controller = getattr(runtime, "model_pressure_controller", None)
        interval = getattr(controller, "sample_interval_seconds", 5.0)
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
        controller = getattr(runtime, "model_pressure_controller", None)
        if controller is not None:
            snapshot["controller_state"] = controller.state
            snapshot["recovery_reason"] = controller.recovery_reason
            snapshot["admission_open"] = bool(
                controller.admission_open
                and not getattr(runtime, "model_admission_closed", True)
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
