"""Model loading, shared inference gating, and delayed cleanup algorithms."""


def transcribe_with_model(runtime, *args, **transcribe_kwargs):
    """Run one model inference through the root-owned concurrency gate."""
    with runtime.model_inference_semaphore:
        return runtime.model.transcribe(*args, **transcribe_kwargs)


def start_model(runtime):
    """Load the configured model once while holding the root-owned load lock."""
    with runtime.model_load_lock:
        if runtime.model is None:
            runtime.logging.debug("Model was purged, need to re-create")
            runtime.model = runtime.stable_whisper.load_faster_whisper(
                runtime.whisper_model,
                download_root=runtime.model_location,
                device=runtime.transcribe_device,
                cpu_threads=runtime.whisper_threads,
                num_workers=runtime.concurrent_transcriptions,
                compute_type=runtime.compute_type,
            )


def schedule_model_cleanup(runtime):
    """Replace the delayed cleanup timer and join its predecessor safely."""
    previous_timer = None
    with runtime.model_cleanup_lock:
        if runtime.model_cleanup_timer is not None:
            runtime.model_cleanup_timer.cancel()
            runtime.logging.debug("Cancelled previous model cleanup timer")
            previous_timer = runtime.model_cleanup_timer

        runtime.model_cleanup_timer = runtime.Timer(
            runtime.model_cleanup_delay,
            runtime.perform_model_cleanup,
        )
        runtime.model_cleanup_timer.daemon = True
        runtime.model_cleanup_timer.start()
        runtime.logging.debug(
            f"Model cleanup scheduled in {runtime.model_cleanup_delay} seconds"
        )

    if previous_timer is not None:
        previous_timer.join(timeout=1)


def perform_model_cleanup(runtime):
    """Unload an idle model and release accelerator and allocator caches."""
    with runtime.model_cleanup_lock:
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
            runtime.ctypes.CDLL(
                runtime.ctypes.util.find_library("c")
            ).malloc_trim(0)

        runtime.model_cleanup_timer = None


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
