"""Standalone startup scanning and Watchdog file event handling."""

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

    def create_subtitle(self, event):
        if not event.is_directory:
            file_path = event.src_path
            if self.runtime._is_in_skipped_dir(file_path):
                self.runtime.logging.info(
                    f"Skipping (skip marker present): {file_path}"
                )
                return
            mapped_path = self.runtime.path_mapping(file_path)
            self.runtime.logging.info(f"File: {mapped_path} was added")
            self.runtime.gen_subtitles_queue(
                mapped_path,
                self.runtime.transcribe_or_translate,
            )

    def handle_event(self, event):
        """Wait for file stability before processing."""
        if self.runtime.is_file_stable(event.src_path):
            self.create_subtitle(event)

    def on_created(self, event):
        self.runtime.time.sleep(5)
        self.handle_event(event)

    def on_modified(self, event):
        self.handle_event(event)


def transcribe_existing(
    runtime,
    transcribe_folders,
    forceLanguage: LanguageCode = LanguageCode.NONE,
):
    """Queue existing files and optionally monitor configured directories."""
    paths = transcribe_folders.split("|")
    if runtime.skip_startup_scan:
        runtime.logging.info(
            "SKIP_STARTUP_SCAN is enabled — skipping existing file scan."
        )
    else:
        runtime.logging.info(
            "Starting to search folders to see if we need to create subtitles."
        )
        runtime.logging.debug("The folders are:")
        for path in paths:
            runtime.logging.debug(path)
            for root, dirs, files in runtime.os.walk(path):
                if runtime.SKIP_MARKER in files:
                    runtime.logging.info(
                        f"Skipping (skip marker present): {root}"
                    )
                    dirs.clear()
                    continue
                for file in files:
                    file_path = runtime.os.path.join(root, file)
                    runtime.gen_subtitles_queue(
                        runtime.path_mapping(file_path),
                        runtime.transcribe_or_translate,
                        forceLanguage,
                    )
            if runtime.os.path.isfile(path):
                runtime.gen_subtitles_queue(
                    runtime.path_mapping(path),
                    runtime.transcribe_or_translate,
                    forceLanguage,
                )

    if runtime.monitor:
        observer = runtime.Observer()
        for path in paths:
            if runtime.os.path.isdir(path):
                handler = runtime.NewFileHandler()
                observer.schedule(handler, path, recursive=True)
        observer.start()
        runtime.logging.info(
            "Finished searching and queueing files for transcription. "
            "Now watching for new files."
        )
