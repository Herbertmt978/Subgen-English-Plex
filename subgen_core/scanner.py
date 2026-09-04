"""Standalone startup scanning and Watchdog file event handling."""

import threading

from watchdog.events import FileSystemEventHandler

from language_code import LanguageCode


SKIP_MARKER = ".subgen_skip"


def is_file_stable(runtime, file_path, wait_time=2, check_intervals=3):
    """Return whether a file's size is stable across successive checks."""
    if not runtime.os.path.exists(file_path):
        return False

    previous_size = -1
    for _ in range(check_intervals):
        try:
            current_size = runtime.os.path.getsize(file_path)
        except OSError:
            return False
        if current_size == previous_size:
            return True
        previous_size = current_size
        runtime.time.sleep(wait_time)
    return False


def _is_in_skipped_dir(runtime, file_path: str) -> bool:
    """Return whether an ancestor directory contains a skip marker."""
    check = runtime.os.path.dirname(runtime.os.path.abspath(file_path))
    while True:
        if runtime.os.path.exists(runtime.os.path.join(check, runtime.SKIP_MARKER)):
            return True
        parent = runtime.os.path.dirname(check)
        if parent == check:
            return False
        check = parent


class NewFileHandler(FileSystemEventHandler):
    """Watchdog handler that queues newly created or modified media files."""

    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime
        self._dispatch_context = threading.local()

    def dispatch(self, event):
        """Bind one Watchdog callback to the active inventory cutoff."""

        coordinator = getattr(self.runtime, "inventory_coordinator", None)
        acquire = None
        release = None
        if coordinator is not None and getattr(coordinator, "enabled", False):
            acquire = getattr(coordinator, "acquire_scan_event", None)
            release = getattr(coordinator, "release_scan_event", None)
        generation = acquire() if callable(acquire) else None
        previous_generation = getattr(
            self._dispatch_context, "inventory_generation", None
        )
        self._dispatch_context.inventory_generation = generation
        try:
            on_any_event = getattr(self, "on_any_event", None)
            if callable(on_any_event):
                on_any_event(event)
            callback = getattr(self, f"on_{event.event_type}")
            return callback(event)
        finally:
            self._dispatch_context.inventory_generation = previous_generation
            if callable(release):
                release(generation)

    def create_subtitle(self, event, *, file_path=None):
        if not event.is_directory:
            file_path = file_path or event.src_path
            if self.runtime._is_in_skipped_dir(file_path):
                self.runtime.logging.info(
                    f"Skipping (skip marker present): {file_path}"
                )
                return
            mapped_path = self.runtime.path_mapping(file_path)
            self.runtime.logging.info(f"File: {mapped_path} was added")
            inventory_kwargs = {}
            generation = getattr(
                self._dispatch_context,
                "inventory_generation",
                None,
            )
            if generation is not None:
                inventory_kwargs["_inventory_generation"] = generation
            if _is_supported_media(self.runtime, file_path):
                self.observe_inventory_item(mapped_path, generation=generation)
            self.runtime.gen_subtitles_queue(
                mapped_path,
                self.runtime.transcribe_or_translate,
                **inventory_kwargs,
            )

    def handle_event(self, event, *, file_path=None):
        """Wait for file stability before processing."""
        file_path = file_path or event.src_path
        if self.runtime.is_file_stable(file_path):
            self.create_subtitle(event, file_path=file_path)

    def remove_inventory_item(self, file_path):
        coordinator = getattr(self.runtime, "inventory_coordinator", None)
        if coordinator is None or not getattr(coordinator, "enabled", False):
            return
        callback = getattr(self.runtime, "inventory_item_removed", None)
        if not callable(callback):
            return
        try:
            callback_kwargs = {}
            generation = getattr(
                self._dispatch_context,
                "inventory_generation",
                None,
            )
            if generation is not None:
                callback_kwargs["generation"] = generation
            callback(
                self.runtime.path_mapping(file_path),
                **callback_kwargs,
            )
        except Exception as exc:
            self.runtime.logging.warning(
                "MQTT inventory removal accounting failed (%s); "
                "file monitoring will continue.",
                type(exc).__name__,
            )

    def observe_inventory_item(self, mapped_path, *, generation=None):
        coordinator = getattr(self.runtime, "inventory_coordinator", None)
        if coordinator is None or not getattr(coordinator, "enabled", False):
            return
        callback = getattr(self.runtime, "inventory_item_observed", None)
        if not callable(callback):
            return
        try:
            callback_kwargs = {}
            if generation is not None:
                callback_kwargs["generation"] = generation
            callback(mapped_path, **callback_kwargs)
        except Exception as exc:
            self.runtime.logging.warning(
                "MQTT inventory arrival accounting failed (%s); "
                "file monitoring will continue.",
                type(exc).__name__,
            )

    def move_inventory_item(self, source_path, destination_path):
        callback = getattr(self.runtime, "inventory_item_moved", None)
        if not callable(callback):
            return False
        try:
            callback_kwargs = {}
            generation = getattr(
                self._dispatch_context,
                "inventory_generation",
                None,
            )
            if generation is not None:
                callback_kwargs["generation"] = generation
            moved = callback(
                self.runtime.path_mapping(source_path),
                self.runtime.path_mapping(destination_path),
                **callback_kwargs,
            )
        except Exception as exc:
            self.runtime.logging.warning(
                "MQTT inventory move accounting failed (%s); "
                "file monitoring will continue.",
                type(exc).__name__,
            )
            return True
        return bool(moved)

    def on_created(self, event):
        self.runtime.time.sleep(5)
        self.handle_event(event)

    def on_modified(self, event):
        self.handle_event(event)

    def on_moved(self, event):
        coordinator = getattr(self.runtime, "inventory_coordinator", None)
        if (
            not event.is_directory
            and coordinator is not None
            and getattr(coordinator, "enabled", False)
        ):
            moved = False
            destination_is_inventory_media = _is_supported_media(
                self.runtime, event.dest_path
            ) and not self.runtime._is_in_skipped_dir(event.dest_path)
            if destination_is_inventory_media:
                moved = self.move_inventory_item(event.src_path, event.dest_path)
            if not moved:
                self.remove_inventory_item(event.src_path)
            self.handle_event(event, file_path=event.dest_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self.remove_inventory_item(event.src_path)


def _configured_paths(transcribe_folders, *, normalize=False):
    paths = transcribe_folders.split("|")
    if not normalize:
        return paths
    return [path.strip() for path in paths if path.strip()]


def _raise_walk_error(error):
    raise error


def _iter_library_files(
    runtime,
    path,
    *,
    log_skips,
    strict_errors=False,
    supported_only=False,
):
    if runtime.os.path.isfile(path):
        if not supported_only or _is_supported_media(runtime, path):
            yield path
        return
    if strict_errors:
        if not runtime.os.path.isdir(path):
            raise OSError("configured library is unavailable")
        walk = runtime.os.walk(path, onerror=_raise_walk_error)
    else:
        walk = runtime.os.walk(path)
    for root, dirs, files in walk:
        if runtime.SKIP_MARKER in files:
            if log_skips:
                runtime.logging.info(f"Skipping (skip marker present): {root}")
            dirs.clear()
            continue
        for file_name in files:
            file_path = runtime.os.path.join(root, file_name)
            if not supported_only or _is_supported_media(runtime, file_path):
                yield file_path


def _is_supported_media(runtime, file_path):
    has_video = getattr(runtime, "has_video_extension", None)
    has_audio = getattr(runtime, "has_audio_extension", None)
    if not callable(has_video) or not callable(has_audio):
        return True
    basename = getattr(runtime.os.path, "basename", None)
    file_name = (
        basename(file_path)
        if callable(basename)
        else str(file_path).replace("\\", "/").rsplit("/", 1)[-1]
    )
    try:
        return bool(has_video(file_name) or has_audio(file_name))
    except Exception:
        return True


def _mapped_paths(runtime, paths):
    return [runtime.path_mapping(path) for path in paths]


def _inventory_library_names(runtime, paths, inventory):
    names = []
    direct_file_number = 0
    configured_names = tuple(
        getattr(getattr(inventory, "config", None), "library_names", ()) or ()
    )
    for index, path in enumerate(paths):
        if index < len(configured_names):
            names.append(configured_names[index])
        elif _is_supported_media(runtime, path):
            direct_file_number += 1
            names.append(f"Direct file {direct_file_number}")
        else:
            names.append(f"Library {index + 1}")
    return names


def prepare_inventory_scan(runtime, transcribe_folders):
    """Install the scan layout before requests or Watchdog events can arrive."""

    inventory = getattr(runtime, "inventory_coordinator", None)
    if inventory is None or not getattr(inventory, "enabled", False):
        return ()
    paths = _configured_paths(transcribe_folders, normalize=True)
    return inventory.begin_scan(
        paths,
        mapped_paths=_mapped_paths(runtime, paths),
        library_names=_inventory_library_names(runtime, paths, inventory),
    )


def _queue_existing(
    runtime,
    paths,
    force_language,
    *,
    inventory=None,
    after_inventory_begin=None,
    after_inventory_scan=None,
):
    if inventory is None:
        return _scan_existing(runtime, paths, force_language, (), inventory=None)

    if inventory.wait_until_scanned(0) and not inventory.scan_cancelled:
        inventory.arm_scan()
    scan_failed = True
    primary_scan_failed = True
    generation = None
    labels = ()
    direct_file_labels = set()
    try:
        labels = inventory.begin_scan(
            paths,
            mapped_paths=_mapped_paths(runtime, paths),
            library_names=_inventory_library_names(runtime, paths, inventory),
        )
        generation = inventory.scan_generation
        runtime.logging.info(
            "Subgen inventory: scanning %d configured libraries before decoding.",
            len(paths),
        )
        observer_failed = bool(
            after_inventory_begin(generation)
            if callable(after_inventory_begin)
            else False
        )
        if inventory.scan_cancelled:
            scan_failed = True
        else:
            existing_scan_failed = _scan_existing(
                runtime,
                paths,
                force_language,
                labels,
                inventory=inventory,
                generation=generation,
                direct_file_labels=direct_file_labels,
            )
            primary_scan_failed = existing_scan_failed
            scan_failed = observer_failed or existing_scan_failed
    except Exception as exc:
        inventory.record_scan_error(generation=generation)
        runtime.logging.warning(
            "Startup inventory failed before every library was scanned (%s); "
            "transcription will continue.",
            type(exc).__name__,
        )
    finally:
        if callable(after_inventory_scan):
            try:
                scan_failed = (
                    bool(after_inventory_scan(generation))
                    or scan_failed
                )
            except Exception as exc:
                scan_failed = True
                inventory.record_scan_error(generation=generation)
                runtime.logging.warning(
                    "Temporary library monitoring could not stop cleanly (%s); "
                    "the inventory was marked incomplete.",
                    type(exc).__name__,
                )
        if (
            generation is not None
            and not inventory.scan_cancelled
            and not primary_scan_failed
        ):
            try:
                scan_failed = (
                    _reconcile_inventory_cutoff(
                        runtime,
                        paths,
                        force_language,
                        labels,
                        inventory,
                        generation=generation,
                        direct_file_labels=direct_file_labels,
                    )
                    or scan_failed
                )
            except Exception as exc:
                scan_failed = True
                inventory.record_scan_error(generation=generation)
                runtime.logging.warning(
                    "Startup inventory could not complete its final "
                    "reconciliation (%s); the inventory was marked incomplete.",
                    type(exc).__name__,
                )
        inventory.finish_scan(
            successful=not scan_failed,
            generation=generation,
        )
        snapshot = inventory.snapshot()
        runtime.logging.info(
            "Subgen inventory finished: %d items need subtitles; "
            "scan complete=%s, errors=%d.",
            snapshot.items_left,
            snapshot.scan_complete,
            snapshot.scan_errors,
        )


def _inspect_inventory_candidate(
    runtime,
    file_path,
    force_language,
    *,
    inventory,
    label,
    generation=None,
    mapped_path=None,
):
    failed = False
    identity_path = mapped_path
    try:
        if mapped_path is None:
            mapped_path = runtime.path_mapping(file_path)
        identity_path = mapped_path
        inventory_kwargs = (
            {"_inventory_source": "startup_scan"}
            if inventory is not None
            else {}
        )
        if inventory is not None and generation is not None:
            inventory_kwargs["_inventory_generation"] = generation
        runtime.gen_subtitles_queue(
            mapped_path,
            runtime.transcribe_or_translate,
            force_language,
            **inventory_kwargs,
        )
    except Exception as exc:
        if inventory is None or not inventory.enabled:
            raise
        failed = True
        inventory.record_scan_error(generation=generation)
        runtime.logging.warning(
            "Startup inventory could not inspect one file (%s); "
            "the scan will continue.",
            type(exc).__name__,
        )
    finally:
        if inventory is not None and label is not None and identity_path is not None:
            inventory.record_scanned_item(
                label,
                identity_path,
                generation=generation,
            )
    return failed


def _reconcile_inventory_cutoff(
    runtime,
    paths,
    force_language,
    labels,
    inventory,
    *,
    generation,
    direct_file_labels=(),
):
    """Inspect supported files not covered by the primary processing pass."""

    inventory.close_scan_event_cutoff(generation=generation)
    if not inventory.wait_for_scan_events(generation=generation):
        inventory.record_scan_error(generation=generation)
        runtime.logging.warning(
            "Startup inventory could not drain file events admitted before "
            "its cutoff; the scan was marked incomplete."
        )
        return True
    runtime.logging.info(
        "Subgen inventory: reconciling files added while the startup scan ran."
    )
    scan_failed = False
    for label, path in zip(labels, paths):
        if inventory.scan_cancelled:
            return True
        try:
            final_paths = []
            for file_path in _iter_library_files(
                runtime,
                path,
                log_skips=False,
                strict_errors=True,
                supported_only=True,
            ):
                if inventory.scan_cancelled:
                    return True
                mapped_path = runtime.path_mapping(file_path)
                final_paths.append(mapped_path)
                if not inventory.needs_scan_reconciliation(
                    mapped_path,
                    generation=generation,
                ):
                    continue
                scan_failed = (
                    _inspect_inventory_candidate(
                        runtime,
                        file_path,
                        force_language,
                        inventory=inventory,
                        label=label,
                        generation=generation,
                        mapped_path=mapped_path,
                    )
                    or scan_failed
                )
            inventory.reconcile_final_library(
                label,
                final_paths,
                generation=generation,
            )
        except Exception as exc:
            scan_failed = True
            if label in direct_file_labels:
                try:
                    direct_file_missing = not runtime.os.path.isfile(path)
                except Exception:
                    direct_file_missing = False
                if direct_file_missing:
                    inventory.reconcile_final_library(
                        label,
                        [],
                        generation=generation,
                    )
            inventory.record_scan_error(generation=generation)
            runtime.logging.warning(
                "Startup inventory could not reconcile library %s (%s); "
                "the scan will continue.",
                label,
                type(exc).__name__,
            )
    return scan_failed


def _scan_existing(
    runtime,
    paths,
    force_language,
    labels,
    *,
    inventory,
    generation=None,
    direct_file_labels=None,
):
    scan_failed = False
    if direct_file_labels is None:
        direct_file_labels = set()
    if inventory is not None:
        for label, path in zip(labels, paths):
            if inventory.scan_cancelled:
                return True
            try:
                if runtime.os.path.isfile(path):
                    direct_file_labels.add(label)
            except Exception:
                pass
            try:
                total = 0
                for counted_path in _iter_library_files(
                    runtime,
                    path,
                    log_skips=False,
                    strict_errors=True,
                    supported_only=True,
                ):
                    if inventory.scan_cancelled:
                        return True
                    mapped_counted_path = runtime.path_mapping(counted_path)
                    inventory.record_counted_item(
                        label,
                        mapped_counted_path,
                        generation=generation,
                    )
                    total += 1
            except Exception as exc:
                total = 0
                scan_failed = True
                inventory.record_scan_error(generation=generation)
                runtime.logging.warning(
                    "Startup inventory could not count library %s (%s); "
                    "the scan will continue.",
                    label,
                    type(exc).__name__,
                )
            inventory.set_library_total(
                label,
                total,
                generation=generation,
            )
            runtime.logging.info(
                "Subgen inventory: %s has %d supported media items to inspect.",
                label,
                total,
            )

    runtime.logging.info(
        "Starting to search folders to see if we need to create subtitles."
    )
    runtime.logging.debug("The folders are:")
    for index, path in enumerate(paths):
        if inventory is not None and inventory.scan_cancelled:
            return True
        label = labels[index] if index < len(labels) else None
        runtime.logging.debug(path)
        try:
            files = _iter_library_files(
                runtime,
                path,
                log_skips=True,
                strict_errors=inventory is not None,
                supported_only=inventory is not None,
            )
            for file_path in files:
                if inventory is not None and inventory.scan_cancelled:
                    return True
                scan_failed = (
                    _inspect_inventory_candidate(
                        runtime,
                        file_path,
                        force_language,
                        inventory=inventory,
                        label=label,
                        generation=generation,
                    )
                    or scan_failed
                )
        except Exception:
            scan_failed = True
            if inventory is None:
                raise
            inventory.record_scan_error(generation=generation)
            runtime.logging.warning(
                "Startup inventory could not finish library %s; "
                "transcription will continue.",
                label or "Library",
            )

    return scan_failed


def _start_monitoring(runtime, paths, *, during_inventory=False, retain=True):
    observer = runtime.Observer()
    try:
        for path in paths:
            if runtime.os.path.isdir(path):
                handler = runtime.NewFileHandler()
                observer.schedule(handler, path, recursive=True)
        observer.start()
    except Exception:
        try:
            observer.stop()
        except Exception:
            pass
        try:
            observer.join(timeout=5.0)
        except Exception:
            pass
        raise
    if retain:
        try:
            runtime._subgen_file_observer = observer
        except Exception:
            pass
    if during_inventory:
        runtime.logging.info(
            "Watching configured libraries for new files while the startup "
            "inventory runs."
        )
    else:
        runtime.logging.info(
            "Finished searching and queueing files for transcription. "
            "Now watching for new files."
        )
    return observer


def queue_existing(
    runtime,
    transcribe_folders,
    forceLanguage: LanguageCode = LanguageCode.NONE,
):
    """Queue existing files without applying startup or watcher policy."""
    return _queue_existing(
        runtime,
        _configured_paths(transcribe_folders),
        forceLanguage,
    )


def transcribe_existing(
    runtime,
    transcribe_folders,
    forceLanguage: LanguageCode = LanguageCode.NONE,
):
    """Apply startup scan policy and optionally monitor configured directories."""
    configured_inventory = getattr(runtime, "inventory_coordinator", None)
    force_inventory_scan = bool(
        configured_inventory is not None
        and getattr(configured_inventory, "enabled", False)
    )
    inventory = configured_inventory if force_inventory_scan else None
    paths = _configured_paths(
        transcribe_folders,
        normalize=force_inventory_scan,
    )
    monitor_started = False
    temporary_observer = None
    if runtime.skip_startup_scan and not force_inventory_scan:
        runtime.logging.info(
            "SKIP_STARTUP_SCAN is enabled — skipping existing file scan."
        )
    else:
        if runtime.skip_startup_scan and force_inventory_scan:
            runtime.logging.info(
                "MQTT inventory is enabled — performing the required full startup scan."
            )
        if inventory is None:
            queue_existing(runtime, transcribe_folders, forceLanguage)
        else:
            def start_monitor_before_scan(_generation):
                nonlocal monitor_started, temporary_observer
                try:
                    observer = _start_monitoring(
                        runtime,
                        paths,
                        during_inventory=True,
                        retain=runtime.monitor,
                    )
                    if runtime.monitor:
                        monitor_started = True
                    else:
                        temporary_observer = observer
                    return False
                except Exception as exc:
                    inventory.record_scan_error(generation=_generation)
                    runtime.logging.warning(
                        "Library monitoring could not start before inventory (%s); "
                        "the startup scan will continue.",
                        type(exc).__name__,
                    )
                    return True

            def stop_temporary_monitor_after_scan(_generation):
                nonlocal temporary_observer
                observer = temporary_observer
                temporary_observer = None
                if observer is None:
                    return False
                try:
                    observer.stop()
                    observer.join(timeout=5.0)
                    is_alive = getattr(observer, "is_alive", None)
                    if callable(is_alive) and is_alive():
                        raise RuntimeError("temporary observer did not stop")
                    return False
                except Exception as exc:
                    inventory.record_scan_error(generation=_generation)
                    runtime.logging.warning(
                        "Temporary library monitoring could not stop cleanly (%s); "
                        "the inventory was marked incomplete.",
                        type(exc).__name__,
                    )
                    return True

            _queue_existing(
                runtime,
                paths,
                forceLanguage,
                inventory=inventory,
                after_inventory_begin=start_monitor_before_scan,
                after_inventory_scan=stop_temporary_monitor_after_scan,
            )

    if runtime.monitor and not monitor_started:
        if inventory is None:
            _start_monitoring(runtime, paths)
        else:
            try:
                _start_monitoring(runtime, paths)
            except Exception as exc:
                inventory.record_scan_error()
                runtime.logging.warning(
                    "Library monitoring could not start after inventory (%s); "
                    "transcription will continue.",
                    type(exc).__name__,
                )
