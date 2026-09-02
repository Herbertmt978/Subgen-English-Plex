#!/usr/bin/env python3
import argparse
import calendar
import json
import os
import re
import smtplib
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import warnings
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path, PurePosixPath

from subgen_core.queueing import task_event_id
from subgen_failure_markers import (
    DEFAULT_MARKER_REGISTRY_PATH,
    build_marker_entry,
    encode_marker_document,
    load_marker_document,
    normalize_file_identity,
)
from subgen_ops_safety import (
    CandidateUnavailableError,
    DeleteRecoveryRequiredError,
    UnsafePathError,
    exact_path_key,
    file_identity,
    lexical_host_path,
    new_delete_token,
    prepare_private_state_directory,
    secure_unlink_regular_beneath,
    supports_secure_unlink,
    validate_regular_file_beneath,
)

TRANSCRIBE_START_RE = re.compile(
    r"WORKER START : \[TRANSCRIBE\s*\] (?P<name>.+?) \| Jobs:"
)
TRANSCRIBE_FINISH_RE = re.compile(
    r"WORKER FINISH:\s*\[TRANSCRIBE\s*\] (?P<name>.+?) in "
)
PROCESSING_ERROR_RE = re.compile(r"Error processing file (?P<path>/media/.+)$")
ENGLISH_MISMATCH_RE = re.compile(
    r"ENGLISH_AUDIO_MISMATCH \| (?P<path>.+?) \| detected=(?P<detected>[^|]+) \| audio=(?P<audio>.+)$"
)
MEDIA_PATH_ACTIVITY_RE = re.compile(
    r"(?:Detecting language of file: (?P<detect_path>/media/.+) \([^/]*starting at[^/]*\)|Extracting audio from: (?P<extract_path>/media/.+), start_time:)"
)
SUBGEN_EVENT_PREFIX = "SUBGEN_EVENT "
STRUCTURED_EVENT_FRAME_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "
    r"(?:DEBUG|INFO|WARNING|ERROR|CRITICAL): )?"
    r"SUBGEN_EVENT (?P<payload>\{.*\})\r?\n?$"
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows test environment
    fcntl = None
MONITOR_STATE_VERSION = 2
MAX_STRUCTURED_EVENT_BYTES = 16 * 1024
MAX_LOG_RECORD_BYTES = 64 * 1024
MAX_MONITOR_STATE_BYTES = 4 * 1024 * 1024
MAX_EVENT_PATH_CHARS = 4096
MAX_TASK_ID_CHARS = 128
MAX_TASK_TYPE_CHARS = 32
VALIDATOR_OUTCOMES = {
    "audio_present",
    "no_audio",
    "invalid_format",
    "indeterminate",
}
INVALID_MEDIA_VALIDATOR_PROOF = {
    "ffprobe": "invalid_format",
    "pyav": "invalid_format",
}


def reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate structured-event key: {key}")
        result[key] = value
    return result


def bounded_event_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"Invalid structured-event {field}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"Invalid structured-event {field}")
    return value


def normalized_event_identity(value: object) -> list[int] | None:
    try:
        return list(normalize_file_identity(value))
    except (TypeError, ValueError):
        return None


def canonical_media_event_path(value: object) -> str:
    path = bounded_event_text(
        value,
        field="path",
        maximum=MAX_EVENT_PATH_CHARS,
    )
    posix_path = PurePosixPath(path)
    if (
        not path.startswith("/media/")
        or ".." in posix_path.parts
        or str(posix_path) != path
    ):
        raise ValueError("Invalid structured-event media path")
    return path


def normalized_validator_outcomes(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict) or set(value) != {"ffprobe", "pyav"}:
        return None
    if any(
        not isinstance(outcome, str) or outcome not in VALIDATOR_OUTCOMES
        for outcome in value.values()
    ):
        return None
    return {"ffprobe": value["ffprobe"], "pyav": value["pyav"]}


def canonical_invalid_media_proof(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    source_identity = normalized_event_identity(value.get("source_identity"))
    outcomes = normalized_validator_outcomes(value.get("validator_outcomes"))
    if (
        value.get("failure_event") != "media_validation_failed"
        or value.get("failure_class") != "invalid_media"
        or outcomes != INVALID_MEDIA_VALIDATOR_PROOF
        or value.get("validation_detail") != "dual_parser_invalid"
        or source_identity is None
    ):
        return None
    return {
        "failure_event": "media_validation_failed",
        "failure_class": "invalid_media",
        "source_identity": source_identity,
        "validator_outcomes": dict(INVALID_MEDIA_VALIDATOR_PROOF),
        "validation_detail": "dual_parser_invalid",
    }


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def atomic_write_text(path: Path, text: str) -> None:
    """Replace a state file only after its complete contents reach disk."""
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        if sys.platform.startswith("linux"):
            directory_descriptor = os.open(
                path.parent,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def append_private_text(path: Path, text: str) -> None:
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
            or (sys.platform.startswith("linux") and file_stat.st_uid != os.geteuid())
        ):
            raise UnsafePathError(
                "Private monitor log must be a service-owned regular file with one link"
            )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_private_text(path: Path, text: str) -> None:
    flags = os.O_CREAT | os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
            or (sys.platform.startswith("linux") and file_stat.st_uid != os.geteuid())
        ):
            raise UnsafePathError(
                "Private monitor output must be a service-owned regular file with one link"
            )
        os.fchmod(descriptor, 0o600)
        os.ftruncate(descriptor, 0)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_private_text(path: Path, *, maximum_bytes: int) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
            or file_stat.st_size > maximum_bytes
            or (sys.platform.startswith("linux") and file_stat.st_uid != os.geteuid())
        ):
            raise UnsafePathError(
                "Private monitor state must be a bounded service-owned regular file "
                "with one link"
            )
        payload = os.read(descriptor, maximum_bytes + 1)
        if len(payload) > maximum_bytes:
            raise UnsafePathError("Private monitor state exceeds the size limit")
        return payload.decode("utf-8", errors="strict")
    finally:
        os.close(descriptor)


@contextmanager
def monitor_process_lock(state_dir: str | os.PathLike[str]):
    """Hold one private lifetime lock before monitor state is loaded."""

    directory = prepare_private_state_directory(state_dir)
    lock_path = directory / "subgen_failure_monitor.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    locked = False
    try:
        lock_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_nlink != 1
            or (sys.platform.startswith("linux") and lock_stat.st_uid != os.geteuid())
        ):
            raise UnsafePathError(
                "Monitor lock must be a service-owned regular file with one link"
            )
        os.fchmod(descriptor, 0o600)
        if fcntl is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "Another Subgen failure monitor already owns this state directory"
                ) from exc
            locked = True
        elif sys.platform.startswith("linux"):
            raise RuntimeError("Linux monitor singleton locking is unavailable")
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def iter_bounded_log_records(stream):
    """Yield decoded log records without retaining an unbounded input line."""

    while True:
        raw_record = stream.readline(MAX_LOG_RECORD_BYTES + 1)
        if not raw_record:
            return
        if len(raw_record) > MAX_LOG_RECORD_BYTES:
            while raw_record and not raw_record.endswith(b"\n"):
                raw_record = stream.readline(MAX_LOG_RECORD_BYTES + 1)
            yield None
            continue
        yield raw_record.rstrip(b"\r\n").decode("utf-8", errors="replace")


def env_default(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def utc_epoch(value: str) -> int | None:
    try:
        return calendar.timegm(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return None


class Monitor:
    def __init__(self, args):
        self.container = args.container
        self.media_root = Path(args.media_root).resolve()
        self.state_dir = prepare_private_state_directory(args.state_dir)
        self.auto_mark = args.auto_mark_failed_files
        self.auto_mark_min_failures = max(1, args.auto_mark_min_failures)
        self.legacy_auto_delete = bool(getattr(args, "auto_delete_failed_files", False))
        self.auto_delete = bool(
            getattr(args, "auto_delete_invalid_media", False) or self.legacy_auto_delete
        )
        self.auto_delete_min_failures = max(1, args.auto_delete_min_failures)
        if self.auto_delete and not self.auto_mark:
            raise ValueError(
                "Invalid-media deletion requires automatic failure markers."
            )
        self.smtp_host = args.smtp_host
        self.smtp_port = args.smtp_port
        self.smtp_username = args.smtp_username
        self.smtp_password = args.smtp_password
        self.smtp_from = args.smtp_from
        self.smtp_to = [
            item.strip() for item in args.smtp_to.split(",") if item.strip()
        ]
        self.smtp_use_tls = args.smtp_use_tls
        self.smtp_use_ssl = args.smtp_use_ssl
        self.email_relay_url = args.email_relay_url
        self.email_relay_admin_key = args.email_relay_admin_key
        self.email_relay_from_address = args.email_relay_from_address
        self.email_english_mismatch_alerts = args.email_english_mismatch_alerts
        self.reconnect_delay_seconds = args.reconnect_delay_seconds
        self.restart_cycle_alert_threshold = args.restart_cycle_alert_threshold
        self.restart_cycle_alert_min_seconds = args.restart_cycle_alert_min_seconds
        self.restart_cycle_alert_require_memory = (
            args.restart_cycle_alert_require_memory
        )
        self.summary_path = self.state_dir / "subgen_failed_files.txt"
        self.events_path = self.state_dir / "subgen_failed_events.log"
        self.state_path = self.state_dir / "subgen_failed_state.json"
        self.heartbeat_path = self.state_dir / "subgen_failure_monitor_heartbeat.txt"
        self.marker_registry_path = (
            self.state_dir / Path(DEFAULT_MARKER_REGISTRY_PATH).name
        )
        self.processing_errors = {}
        self.crash_candidates = {}
        self.notifications = {}
        self.restart_cycles = {}
        self.last_transcribe_start = None
        self.recent_container_paths = {}
        self.active_tasks = {}
        self.state_recovery_safe = True
        self.state_context_current = True

        self.load_state()
        if self.legacy_auto_delete:
            warnings.warn(
                "AUTO_DELETE_FAILED_FILES now enables invalid-media-only deletion; "
                "generic errors and crashes are always retained.",
                RuntimeWarning,
                stacklevel=2,
            )

    def load_state(self) -> None:
        if not os.path.lexists(self.state_path):
            return

        try:
            state = json.loads(
                read_private_text(
                    self.state_path,
                    maximum_bytes=MAX_MONITOR_STATE_BYTES,
                )
            )
        except Exception as exc:
            self.state_recovery_safe = False
            warnings.warn(
                "Monitor state could not be validated; deletion is disabled for "
                f"this process ({type(exc).__name__}).",
                RuntimeWarning,
                stacklevel=2,
            )
            return
        if not isinstance(state, dict):
            self.state_recovery_safe = False
            self.state_context_current = False
            warnings.warn(
                "Monitor state has an invalid schema; deletion is disabled for "
                "this process.",
                RuntimeWarning,
                stacklevel=2,
            )
            return

        try:
            persisted_media_root = exact_path_key(state.get("media_root"))
        except (TypeError, ValueError, UnsafePathError):
            persisted_media_root = None
        self.state_context_current = bool(
            state.get("version") == MONITOR_STATE_VERSION
            and state.get("container_name") == self.container
            and persisted_media_root == exact_path_key(self.media_root)
        )

        collection_names = (
            "processing_errors",
            "crash_candidates",
            "notifications",
            "restart_cycles",
        )
        collections = {}
        schema_invalid = False
        for collection_name in collection_names:
            value = state.get(collection_name, [])
            if not isinstance(value, list):
                schema_invalid = True
                value = []
            if any(not isinstance(item, dict) for item in value):
                schema_invalid = True
            collections[collection_name] = [
                item for item in value if isinstance(item, dict)
            ]
        if schema_invalid:
            self.state_recovery_safe = False
            warnings.warn(
                "Monitor state has malformed collections; deletion is disabled "
                "for this process.",
                RuntimeWarning,
                stacklevel=2,
            )

        blocked_loads = []
        self.processing_errors = {}
        for index, item in enumerate(collections["processing_errors"]):
            if not item.get("host_path"):
                continue
            key, blocked_message = self.loaded_path_key(
                item["host_path"], "processing_errors", index
            )
            if blocked_message:
                item["delete_status"] = "blocked_recovery"
                item["delete_message"] = blocked_message
                blocked_loads.append((item.get("host_path"), blocked_message))
            self.processing_errors[key] = item
        self.crash_candidates = {}
        for index, item in enumerate(collections["crash_candidates"]):
            if not item.get("display_name"):
                continue
            if item.get("host_path"):
                key, blocked_message = self.loaded_path_key(
                    item["host_path"], "crash_candidates", index
                )
                if blocked_message:
                    item["delete_status"] = "blocked_recovery"
                    item["delete_message"] = blocked_message
                    blocked_loads.append((item.get("host_path"), blocked_message))
            elif item.get("container_path"):
                try:
                    key = str(PurePosixPath(item["container_path"]))
                except (TypeError, ValueError) as exc:
                    key = f"invalid:crash_candidates:{index}"
                    item["delete_status"] = "blocked_recovery"
                    item["delete_message"] = (
                        f"Persisted container path was quarantined: {exc}"
                    )
                    blocked_loads.append(
                        (item.get("container_path"), item["delete_message"])
                    )
            else:
                key = str(item.get("candidate_id") or f"legacy:{item['display_name']}")
            item["candidate_id"] = key
            self.crash_candidates[key] = item
        self.notifications = {}
        for index, item in enumerate(collections["notifications"]):
            if not item.get("host_path"):
                continue
            key, blocked_message = self.loaded_path_key(
                item["host_path"], "notifications", index
            )
            if blocked_message:
                blocked_loads.append((item.get("host_path"), blocked_message))
            self.notifications[key] = item
        self.restart_cycles = {
            item["display_name"].lower(): item
            for item in collections["restart_cycles"]
            if item.get("display_name")
        }
        reset_evidence_paths = self.migrate_failure_identities()
        self.recover_delete_intents()
        if blocked_loads or reset_evidence_paths:
            self.save_state()
        for host_path, message in blocked_loads:
            self.append_delete_audit(
                "FILE_DELETE_RECOVERY_BLOCKED",
                f"{host_path!s} | {message}",
            )
        for host_path in reset_evidence_paths:
            self.append_delete_audit(
                "FAILURE_EVIDENCE_RESET",
                f"{host_path} | legacy path-only failure count reset",
            )

    @staticmethod
    def loaded_path_key(
        host_path: object, collection: str, index: int
    ) -> tuple[str, str | None]:
        try:
            return exact_path_key(host_path), None
        except (TypeError, ValueError, UnsafePathError) as exc:
            return (
                f"invalid:{collection}:{index}",
                f"Persisted path was quarantined: {exc}",
            )

    def save_state(self) -> None:
        state = {
            "version": MONITOR_STATE_VERSION,
            "updated_utc": utc_stamp(),
            "container_name": self.container,
            "media_root": str(self.media_root),
            "processing_errors": sorted(
                self.processing_errors.values(), key=lambda item: item["host_path"]
            ),
            "crash_candidates": sorted(
                self.crash_candidates.values(), key=lambda item: item["display_name"]
            ),
            "notifications": sorted(
                self.notifications.values(), key=lambda item: item["host_path"]
            ),
            "restart_cycles": sorted(
                self.restart_cycles.values(), key=lambda item: item["display_name"]
            ),
        }
        atomic_write_text(self.state_path, json.dumps(state, indent=2) + "\n")

    def current_failure_identity(self, host_path: str) -> list[int] | None:
        try:
            path_stat = lexical_host_path(host_path).lstat()
        except (OSError, TypeError, ValueError, UnsafePathError):
            return None
        if not stat.S_ISREG(path_stat.st_mode):
            return None
        return list(file_identity(path_stat))

    def validated_event_generation(
        self,
        host_path: str,
        event_identity: list[int],
    ) -> list[int] | None:
        if supports_secure_unlink():
            try:
                return list(
                    validate_regular_file_beneath(
                        self.media_root,
                        host_path,
                        expected_identity=event_identity,
                    )
                )
            except (CandidateUnavailableError, UnsafePathError):
                return None
        current_identity = self.current_failure_identity(host_path)
        if current_identity == event_identity:
            return list(event_identity)
        return None

    def migrate_failure_identities(self) -> list[str]:
        reset_paths = []
        for collection in (self.processing_errors, self.crash_candidates):
            for target in collection.values():
                if target.get("failure_identity") or not target.get("host_path"):
                    continue
                delete_identity = target.get("delete_identity")
                delete_status = target.get("delete_status")
                if isinstance(delete_identity, list) and len(delete_identity) == 5:
                    target["failure_identity"] = delete_identity
                    continue
                if delete_status in {
                    "blocked_recovery",
                    "delete_paused",
                    "deleting",
                    "failed_recovery",
                }:
                    target["delete_status"] = "blocked_recovery"
                    target["delete_message"] = (
                        target.get("delete_message")
                        or "Legacy delete intent lacked a complete file identity."
                    )
                    continue
                if delete_status in {"deleted", "deleted_recovered"}:
                    continue
                target["failure_identity"] = None
                target["count"] = 0
                target["delete_status"] = None
                target["deleted_utc"] = None
                target["delete_message"] = (
                    "Legacy path-only failure evidence was reset during upgrade."
                )
                target.pop("delete_identity", None)
                target.pop("delete_intent_utc", None)
                target.pop("delete_token", None)
                target.pop("delete_event_kind", None)
                target["failure_evidence_reset_utc"] = utc_stamp()
                reset_paths.append(str(target["host_path"]))
        return reset_paths

    def append_event(self, kind: str, message: str) -> None:
        line = f"{utc_stamp()} [{kind}] {message}\n"
        append_private_text(self.events_path, line)
        write_private_text(self.heartbeat_path, f"{utc_stamp()} {kind}\n")

    def write_summary(self) -> None:
        lines = [
            f"Updated UTC: {utc_stamp()}",
            f"Container: {self.container}",
            f"Media root: {self.media_root}",
            "",
            f"Auto mark failed files: {self.auto_mark}",
            f"Auto mark minimum failures: {self.auto_mark_min_failures}",
            f"Failure marker registry: {self.marker_registry_path}",
            f"Auto delete invalid media: {self.auto_delete}",
            f"Invalid-media delete minimum failures: {self.auto_delete_min_failures}",
            "",
            "Processing errors:",
        ]

        if not self.processing_errors:
            lines.append("  none")
        else:
            for item in sorted(
                self.processing_errors.values(), key=lambda value: value["host_path"]
            ):
                lines.append(f"  {item['host_path']}")
                lines.append(f"    container: {item['container_path']}")
                lines.append(f"    first_seen_utc: {item['first_seen_utc']}")
                lines.append(f"    last_seen_utc: {item['last_seen_utc']}")
                lines.append(f"    count: {item['count']}")
                self.append_processing_evidence(lines, item)
                self.append_marker_summary(lines, item)
                if item.get("delete_status"):
                    lines.append(f"    delete_status: {item['delete_status']}")
                    lines.append(f"    deleted_utc: {item.get('deleted_utc', '')}")
                    lines.append(
                        f"    delete_message: {item.get('delete_message', '')}"
                    )

        lines.extend(["", "Crash candidates before SIGSEGV:"])
        if not self.crash_candidates:
            lines.append("  none")
        else:
            for item in sorted(
                self.crash_candidates.values(), key=lambda value: value["display_name"]
            ):
                lines.append(f"  {item['display_name']}")
                if item.get("host_path"):
                    lines.append(f"    host_path: {item['host_path']}")
                lines.append(f"    first_seen_utc: {item['first_seen_utc']}")
                lines.append(f"    last_seen_utc: {item['last_seen_utc']}")
                lines.append(f"    count: {item['count']}")
                self.append_marker_summary(lines, item)
                if item.get("delete_status"):
                    lines.append(f"    delete_status: {item['delete_status']}")
                    lines.append(f"    deleted_utc: {item.get('deleted_utc', '')}")
                    lines.append(
                        f"    delete_message: {item.get('delete_message', '')}"
                    )

        lines.extend(["", "Repeated transcribe/restart cycles:"])
        if not self.restart_cycles:
            lines.append("  none")
        else:
            for item in sorted(
                self.restart_cycles.values(), key=lambda value: value["display_name"]
            ):
                lines.append(f"  {item['display_name']}")
                if item.get("host_path"):
                    lines.append(f"    host_path: {item['host_path']}")
                lines.append(f"    first_seen_utc: {item['first_seen_utc']}")
                lines.append(f"    last_seen_utc: {item['last_seen_utc']}")
                lines.append(f"    count: {item['count']}")
                lines.append(
                    f"    alert_threshold: {self.restart_cycle_alert_threshold}"
                )
                lines.append(
                    f"    alert_min_seconds: {self.restart_cycle_alert_min_seconds}"
                )
                lines.append(
                    f"    alert_require_memory: {self.restart_cycle_alert_require_memory}"
                )
                if item.get("alert_elapsed_seconds") is not None:
                    lines.append(
                        f"    alert_elapsed_seconds: {item['alert_elapsed_seconds']}"
                    )
                if item.get("memory_evidence"):
                    lines.append("    memory_evidence:")
                    lines.extend(
                        f"      - {entry}" for entry in item.get("memory_evidence", [])
                    )
                lines.append(
                    f"    email_status: {item.get('email_status', 'not_sent')}"
                )
                if item.get("email_message"):
                    lines.append(f"    email_message: {item['email_message']}")

        lines.extend(["", "English mismatch notifications:"])
        if not self.notifications:
            lines.append("  none")
        else:
            for item in sorted(
                self.notifications.values(), key=lambda value: value["host_path"]
            ):
                lines.append(f"  {item['host_path']}")
                lines.append(f"    detected_language: {item['detected_language']}")
                lines.append(f"    english_audio: {item['english_audio']}")
                lines.append(f"    first_seen_utc: {item['first_seen_utc']}")
                lines.append(f"    last_seen_utc: {item['last_seen_utc']}")
                lines.append(
                    f"    email_status: {item.get('email_status', 'not_sent')}"
                )
                if item.get("email_message"):
                    lines.append(f"    email_message: {item['email_message']}")

        lines.extend(
            [
                "",
                "Notes:",
                "  Processing errors are exact file paths reported by Subgen logs.",
                "  Crash candidates are the last TRANSCRIBE jobs seen before a SIGSEGV.",
                "  Repeated transcribe/restart cycles are the same TRANSCRIBE job starting again before a matching finish line.",
                "  English mismatch notifications are emitted when Whisper detects non-English audio but file metadata still shows an English audio track.",
            ]
        )
        atomic_write_text(self.summary_path, "\n".join(lines) + "\n")
        self.save_state()

    @staticmethod
    def append_processing_evidence(lines: list[str], item: dict) -> None:
        for field in ("failure_event", "failure_class"):
            try:
                value = bounded_event_text(
                    item.get(field),
                    field=field,
                    maximum=64,
                )
            except ValueError:
                continue
            lines.append(f"    {field}: {value}")

        validator_outcomes = normalized_validator_outcomes(
            item.get("validator_outcomes")
        )
        if validator_outcomes is not None:
            lines.append("    validator_outcomes:")
            for validator in ("ffprobe", "pyav"):
                lines.append(f"      {validator}: {validator_outcomes[validator]}")

        try:
            validation_detail = bounded_event_text(
                item.get("validation_detail"),
                field="validation_detail",
                maximum=64,
            )
        except ValueError:
            return
        lines.append(f"    validation_detail: {validation_detail}")

    @staticmethod
    def append_marker_summary(lines: list[str], item: dict) -> None:
        if not item.get("marker_status"):
            return
        lines.append(f"    marker_status: {item['marker_status']}")
        lines.append(f"    marker_utc: {item.get('marker_utc') or ''}")
        lines.append(f"    marker_message: {item.get('marker_message') or ''}")
        if item.get("failure_identity"):
            lines.append(
                "    marker_generation_scope: exact container path and "
                "five-field file identity"
            )

    def append_marker_audit(self, event_kind: str, event_message: str) -> None:
        try:
            self.append_event(event_kind, event_message)
        except Exception as exc:
            print(
                "WARNING: monitor failure-marker audit could not be written "
                f"({type(exc).__name__}).",
                file=sys.stderr,
            )

    def persist_failure_marker(
        self,
        target: dict,
        *,
        failure_kind: str,
    ) -> bool:
        """Persist one exact-generation marker and return its deletion gate."""

        if not self.auto_mark:
            target["marker_status"] = "disabled"
            target["marker_message"] = "Automatic failure markers are disabled."
            return False

        failure_count = int(target.get("count", 0) or 0)
        if failure_count < self.auto_mark_min_failures:
            target["marker_status"] = "waiting"
            target["marker_message"] = (
                f"Waiting for {self.auto_mark_min_failures} failures; "
                f"currently {failure_count}."
            )
            if self.auto_delete and failure_count >= self.auto_delete_min_failures:
                target["delete_status"] = "marker_blocked"
                target["delete_message"] = (
                    "Deletion is waiting for a durable exact-generation marker."
                )
            return False

        now = utc_stamp()
        container_path = target.get("container_path")
        failure_identity = target.get("failure_identity")
        try:
            normalized_identity = normalize_file_identity(failure_identity)
            try:
                document = load_marker_document(self.marker_registry_path)
            except FileNotFoundError:
                if os.path.lexists(self.marker_registry_path):
                    raise
                document = {"markers": []}

            existing = next(
                (
                    marker
                    for marker in document["markers"]
                    if marker["container_path"] == container_path
                ),
                None,
            )
            same_generation = bool(
                existing
                and normalize_file_identity(existing["file_identity"])
                == normalized_identity
            )
            created_utc = existing["created_utc"] if same_generation else now
            marker = build_marker_entry(
                container_path,
                normalized_identity,
                failure_kind,
                failure_count,
                now,
                created_utc,
            )
            entries = [
                item
                for item in document["markers"]
                if item["container_path"] != container_path
            ]
            entries.append(marker)
            encoded = encode_marker_document(entries, now)
            atomic_write_text(self.marker_registry_path, encoded)
        except Exception as exc:
            target["marker_status"] = "write_failed"
            target["marker_utc"] = now
            target["marker_message"] = (
                f"Failure marker was not persisted ({type(exc).__name__})."
            )
            if self.auto_delete and failure_count >= self.auto_delete_min_failures:
                if failure_identity is None:
                    target["delete_status"] = "blocked"
                    target["delete_message"] = (
                        "Deletion requires an exact regular-file generation identity."
                    )
                else:
                    target["delete_status"] = "marker_blocked"
                    target["delete_message"] = (
                        "Deletion is waiting for a durable exact-generation marker."
                    )
            self.append_marker_audit(
                "FAILURE_MARKER_WRITE_FAILED",
                f"{container_path!s} | {type(exc).__name__}",
            )
            return False

        target["marker_status"] = "refreshed" if same_generation else "created"
        target["marker_utc"] = now
        target["marker_message"] = "Exact file-generation marker persisted."
        self.append_marker_audit(
            "FAILURE_MARKER_REFRESHED" if same_generation else "FAILURE_MARKER_CREATED",
            f"{container_path} | failure_kind={failure_kind} | count={failure_count}",
        )
        return True

    @staticmethod
    def invalid_media_delete_proof(target: dict) -> dict | None:
        if target.get("record_kind") != "processing_error":
            return None
        proof = canonical_invalid_media_proof(target)
        if proof is None:
            return None
        failure_identity = normalized_event_identity(target.get("failure_identity"))
        if failure_identity != proof["source_identity"]:
            return None
        return proof

    def invalid_media_path_binding(
        self,
        target: dict,
        host_path: str | os.PathLike[str] | None = None,
    ) -> bool:
        try:
            container_path = canonical_media_event_path(target.get("container_path"))
            expected_host_path = self.convert_container_path_to_host_path(
                container_path
            )
            actual_host_path = host_path or target.get("host_path")
            return exact_path_key(expected_host_path) == exact_path_key(
                actual_host_path
            )
        except (TypeError, ValueError, UnsafePathError):
            return False

    def durable_marker_matches(self, target: dict, proof: dict) -> bool:
        try:
            document = load_marker_document(self.marker_registry_path)
        except Exception as exc:
            target["marker_status"] = "verification_failed"
            target["marker_message"] = (
                f"Exact marker could not be verified ({type(exc).__name__})."
            )
            return False

        return any(
            marker.get("container_path") == target.get("container_path")
            and marker.get("failure_kind") == "processing_error"
            and normalized_event_identity(marker.get("file_identity"))
            == proof["source_identity"]
            and isinstance(marker.get("failure_count"), int)
            and not isinstance(marker.get("failure_count"), bool)
            and marker["failure_count"] >= self.auto_delete_min_failures
            for marker in document.get("markers", [])
        )

    def marker_allows_delete(self, target: dict) -> bool:
        if not self.auto_delete:
            return False
        if not self.auto_mark or not self.state_recovery_safe:
            target["delete_status"] = "policy_blocked"
            target["delete_message"] = (
                "Invalid-media deletion requires validated state and automatic "
                "failure markers."
            )
            return False
        failure_count = int(target.get("invalid_media_count", 0) or 0)
        if failure_count < self.auto_delete_min_failures:
            target["delete_status"] = "waiting"
            target["delete_message"] = (
                f"Waiting for {self.auto_delete_min_failures} invalid-media "
                f"failures; currently {failure_count}."
            )
            return False
        proof = self.invalid_media_delete_proof(target)
        if proof is None:
            target["delete_status"] = "policy_blocked"
            target["delete_message"] = (
                "Deletion requires a dedicated dual-validator invalid-media event."
            )
            return False
        if target.get("marker_status") in {
            "created",
            "refreshed",
        } and self.durable_marker_matches(target, proof):
            return True
        target["delete_status"] = "marker_blocked"
        target["delete_message"] = (
            "Deletion is waiting for a durable exact-generation marker."
        )
        return False

    def capture_delete_generation_if_needed(
        self,
        host_path: str,
        target: dict,
        *,
        missing_kind: str,
    ) -> bool:
        """Reject legacy attempts to adopt whatever generation is present now."""

        target["delete_status"] = "policy_blocked"
        target["delete_message"] = (
            "Deletion requires the source identity from a dedicated invalid-media "
            "event; the current path was not adopted."
        )
        return False

    def convert_container_path_to_host_path(self, container_path: str) -> str:
        if not container_path or not container_path.startswith("/media/"):
            raise ValueError(f"Unsupported media path: {container_path}")

        relative_path = container_path[len("/media/") :]
        posix_path = PurePosixPath(relative_path)
        if (
            posix_path.is_absolute()
            or ".." in posix_path.parts
            or "\0" in relative_path
        ):
            raise ValueError(f"Refusing unsafe media path: {container_path}")
        host_path = lexical_host_path(self.media_root.joinpath(*posix_path.parts))
        try:
            host_path.relative_to(self.media_root)
        except ValueError as exc:
            raise ValueError(f"Refusing path outside media root: {host_path}") from exc
        return str(host_path)

    def try_delete_path(
        self,
        host_path: str,
        target: dict,
        missing_kind: str,
        deleted_kind: str,
        failed_kind: str,
    ) -> None:
        if not self.auto_delete:
            return

        if not self.invalid_media_path_binding(target, host_path):
            target["delete_status"] = "policy_blocked"
            target["delete_message"] = (
                "Deletion requires an exact canonical container-to-host path binding."
            )
            return

        failure_count = int(target.get("invalid_media_count", 0) or 0)
        if failure_count < self.auto_delete_min_failures:
            target["delete_status"] = "waiting"
            target["delete_message"] = (
                f"Waiting for {self.auto_delete_min_failures} invalid-media "
                f"failures; currently {failure_count}."
            )
            return

        proof = self.invalid_media_delete_proof(target)
        if proof is None:
            target["delete_status"] = "policy_blocked"
            target["delete_message"] = (
                "Deletion requires a dedicated dual-validator invalid-media event."
            )
            return
        if not self.marker_allows_delete(target):
            return

        now = utc_stamp()
        try:
            identity = validate_regular_file_beneath(
                self.media_root,
                host_path,
                expected_identity=proof["source_identity"],
            )
        except CandidateUnavailableError:
            self.finish_delete_outcome(
                target,
                status="missing",
                message="Path not found at delete time.",
                event_kind=missing_kind,
                event_message=f"{host_path} | missing",
                timestamp=now,
            )
            return
        except UnsafePathError as exc:
            self.finish_delete_outcome(
                target,
                status="blocked",
                message=str(exc),
                event_kind="FILE_DELETE_BLOCKED",
                event_message=f"{host_path} | {exc}",
                timestamp=now,
            )
            return
        except Exception as exc:
            self.finish_delete_outcome(
                target,
                status="failed",
                message=str(exc),
                event_kind=failed_kind,
                event_message=f"{host_path} | {exc}",
                timestamp=now,
            )
            return

        target["delete_status"] = "deleting"
        target["delete_message"] = "Durable delete intent recorded."
        target["delete_intent_utc"] = now
        target["delete_identity"] = list(identity)
        target["failure_identity"] = list(identity)
        target["delete_proof"] = proof
        target["delete_token"] = new_delete_token()
        target["delete_event_kind"] = deleted_kind
        self.save_state()

        try:
            secure_unlink_regular_beneath(
                self.media_root,
                host_path,
                expected_identity=identity,
                operation_token=target["delete_token"],
            )
            status = "deleted"
            message = "Removed by monitor."
            event_kind = deleted_kind
            event_message = str(lexical_host_path(host_path))
        except CandidateUnavailableError:
            status = "deleted_recovered"
            message = "Path disappeared after durable delete intent."
            event_kind = f"{deleted_kind}_RECOVERED"
            event_message = f"{host_path} | missing after intent"
        except DeleteRecoveryRequiredError as exc:
            status = "deleting"
            message = f"Delete recovery required: {exc}"
            event_kind = "FILE_DELETE_RECOVERY_DEFERRED"
            event_message = f"{host_path} | {exc}"
        except UnsafePathError as exc:
            status = "blocked"
            message = str(exc)
            event_kind = "FILE_DELETE_BLOCKED"
            event_message = f"{host_path} | {exc}"
        except Exception as exc:
            status = "failed_recovery"
            message = f"Delete recovery required after unexpected failure: {exc}"
            event_kind = failed_kind
            event_message = f"{host_path} | recovery required: {exc}"

        self.finish_delete_outcome(
            target,
            status=status,
            message=message,
            event_kind=event_kind,
            event_message=event_message,
            timestamp=utc_stamp(),
        )

    def finish_delete_outcome(
        self,
        target: dict,
        *,
        status: str,
        message: str,
        event_kind: str,
        event_message: str,
        timestamp: str,
    ) -> None:
        target["delete_status"] = status
        target["delete_message"] = message
        target["deleted_utc"] = timestamp
        if status not in {
            "blocked_recovery",
            "delete_paused",
            "deleting",
            "failed_recovery",
        }:
            target.pop("delete_identity", None)
            target.pop("delete_intent_utc", None)
            target.pop("delete_token", None)
            target.pop("delete_event_kind", None)
            target.pop("delete_proof", None)
        self.save_state()
        self.append_delete_audit(event_kind, event_message)

    def append_delete_audit(self, event_kind: str, event_message: str) -> None:
        try:
            self.append_event(event_kind, event_message)
        except Exception as exc:
            print(
                "WARNING: monitor deletion audit could not be written "
                f"({type(exc).__name__}).",
                file=sys.stderr,
            )

    def recover_delete_intents(self) -> None:
        recovered_events = []
        blocked_events = []
        for collection_name, collection in (
            ("processing_errors", self.processing_errors),
            ("crash_candidates", self.crash_candidates),
        ):
            for target in collection.values():
                if target.get("delete_status") not in {"deleting", "delete_paused"}:
                    continue
                host_path = target.get("host_path")
                identity = target.get("delete_identity")
                deleted_kind = target.get("delete_event_kind") or "FILE_DELETED"
                proof = canonical_invalid_media_proof(target.get("delete_proof"))
                current_target_proof = self.invalid_media_delete_proof(target)
                normalized_delete_identity = normalized_event_identity(identity)
                if (
                    collection_name != "processing_errors"
                    or proof is None
                    or current_target_proof != proof
                    or normalized_delete_identity != proof["source_identity"]
                    or not target.get("delete_token")
                    or not self.invalid_media_path_binding(target, host_path)
                ):
                    target["delete_status"] = "blocked_recovery"
                    target["delete_message"] = (
                        "Legacy or incomplete delete intent is policy-blocked; only "
                        "typed invalid-media intents may resume."
                    )
                    blocked_events.append(
                        (
                            "FILE_DELETE_RECOVERY_BLOCKED",
                            f"{host_path or '<missing>'} | invalid-media proof absent",
                        )
                    )
                    continue
                if (
                    not self.auto_mark
                    or not self.state_recovery_safe
                    or not self.state_context_current
                    or not self.durable_marker_matches(target, proof)
                ):
                    target["delete_status"] = "blocked_recovery"
                    target["delete_message"] = (
                        "Delete intent is blocked because its exact durable marker "
                        "or validated monitor state is unavailable."
                    )
                    blocked_events.append(
                        (
                            "FILE_DELETE_RECOVERY_BLOCKED",
                            f"{host_path or '<missing>'} | marker/state proof unavailable",
                        )
                    )
                    continue
                if not self.auto_delete:
                    if target.get("delete_status") != "delete_paused":
                        target["delete_status"] = "delete_paused"
                        target["delete_message"] = (
                            "Durable delete recovery paused because automatic deletion is disabled."
                        )
                        blocked_events.append(
                            (
                                "FILE_DELETE_RECOVERY_PAUSED",
                                f"{host_path or '<missing>'} | automatic deletion disabled",
                            )
                        )
                    continue
                target["delete_status"] = "deleting"
                if not host_path or not identity:
                    target["delete_status"] = "blocked_recovery"
                    target["delete_message"] = "Delete intent lacked path identity."
                    blocked_events.append(
                        (
                            "FILE_DELETE_RECOVERY_BLOCKED",
                            f"{host_path or '<missing>'} | delete intent lacked path identity",
                        )
                    )
                    continue
                try:
                    secure_unlink_regular_beneath(
                        self.media_root,
                        host_path,
                        expected_identity=identity,
                        operation_token=target["delete_token"],
                    )
                    detail = "Resumed durable delete intent."
                except CandidateUnavailableError:
                    detail = "Confirmed missing after durable delete intent."
                except DeleteRecoveryRequiredError as exc:
                    target["delete_status"] = "deleting"
                    target["delete_message"] = f"Delete recovery deferred: {exc}"
                    blocked_events.append(
                        (
                            "FILE_DELETE_RECOVERY_DEFERRED",
                            f"{host_path} | {exc}",
                        )
                    )
                    continue
                except UnsafePathError as exc:
                    target["delete_status"] = "blocked_recovery"
                    target["delete_message"] = str(exc)
                    blocked_events.append(
                        (
                            "FILE_DELETE_RECOVERY_BLOCKED",
                            f"{host_path} | {exc}",
                        )
                    )
                    continue
                except Exception as exc:
                    target["delete_status"] = "blocked_recovery"
                    target["delete_message"] = f"Recovery failed closed: {exc}"
                    blocked_events.append(
                        (
                            "FILE_DELETE_RECOVERY_BLOCKED",
                            f"{host_path} | recovery failed closed: {exc}",
                        )
                    )
                    continue

                target["delete_status"] = "deleted_recovered"
                target["delete_message"] = detail
                target["deleted_utc"] = utc_stamp()
                target.pop("delete_identity", None)
                target.pop("delete_intent_utc", None)
                target.pop("delete_token", None)
                target.pop("delete_event_kind", None)
                target.pop("delete_proof", None)
                recovered_events.append(
                    (f"{deleted_kind}_RECOVERED", f"{host_path} | recovered")
                )

        if not recovered_events and not blocked_events:
            return
        self.save_state()
        for event_kind, event_message in recovered_events + blocked_events:
            self.append_delete_audit(event_kind, event_message)

    def remember_container_path(self, container_path: str) -> None:
        if not container_path or not container_path.startswith("/media/"):
            return

        display_name = Path(container_path).name
        key = display_name.lower()
        previous_path = self.recent_container_paths.get(key)
        if previous_path and previous_path != container_path:
            # Basenames are not unique across a media library. Mark ambiguous
            # legacy log data unusable instead of guessing which file to delete.
            self.recent_container_paths[key] = None
        elif key not in self.recent_container_paths:
            self.recent_container_paths[key] = container_path

        if (
            self.last_transcribe_start
            and self.last_transcribe_start["display_name"].lower() == key
        ):
            self.last_transcribe_start["container_path"] = container_path

    def resolve_crash_candidate_host_path(self, previous_host_path: str | None = None):
        if previous_host_path:
            try:
                candidate_path = lexical_host_path(previous_host_path)
            except ValueError:
                return None
            if candidate_path.exists() or candidate_path.is_symlink():
                return str(candidate_path)
        return None

    def record_processing_error(
        self,
        container_path: str,
        *,
        failure_event: str = "legacy_processing_error",
        failure_class: str = "processing_error",
        source_identity=None,
        validator_outcomes: dict | None = None,
        validation_detail: str | None = None,
    ) -> None:
        self.remember_container_path(container_path)
        try:
            host_path = self.convert_container_path_to_host_path(container_path)
        except ValueError as exc:
            self.append_event("PROCESSING_ERROR_PATH_BLOCKED", str(exc))
            return
        key = exact_path_key(host_path)
        now = utc_stamp()
        event_identity = normalized_event_identity(source_identity)
        current_identity = None
        if event_identity is not None:
            current_identity = self.validated_event_generation(
                host_path,
                event_identity,
            )
            if current_identity is None:
                self.append_event(
                    "PROCESSING_ERROR_STALE",
                    f"{host_path} | source generation unavailable",
                )
                return
        terminal_statuses = {"deleted", "deleted_recovered"}
        recovery_owned_statuses = {
            "blocked_recovery",
            "delete_paused",
            "deleting",
            "failed_recovery",
        }
        reset_generation = False
        if key in self.processing_errors:
            previous = self.processing_errors[key]
            if previous.get("delete_status") in recovery_owned_statuses:
                self.append_event(
                    "PROCESSING_ERROR_RECOVERY_OWNED",
                    f"{host_path} | durable delete intent retained",
                )
                self.write_summary()
                return
            if previous.get("delete_status") in terminal_statuses:
                if current_identity is None:
                    self.append_event(
                        "PROCESSING_ERROR_STALE",
                        f"{host_path} | ignored after terminal deletion",
                    )
                    self.write_summary()
                    return
                reset_generation = True
            elif current_identity is not None and (
                previous.get("failure_identity") != current_identity
            ):
                reset_generation = True

        if key not in self.processing_errors or reset_generation:
            self.processing_errors[key] = {
                "record_kind": "processing_error",
                "host_path": host_path,
                "container_path": container_path,
                "first_seen_utc": now,
                "last_seen_utc": now,
                "count": 1,
                "invalid_media_count": 0,
                "delete_status": None,
                "deleted_utc": None,
                "delete_message": None,
                "failure_identity": current_identity,
            }
        else:
            self.processing_errors[key]["last_seen_utc"] = now
            self.processing_errors[key]["count"] += 1
            if current_identity is not None:
                self.processing_errors[key]["failure_identity"] = current_identity

        target = self.processing_errors[key]
        target["record_kind"] = "processing_error"
        target["failure_event"] = failure_event
        target["failure_class"] = failure_class
        target["source_identity"] = event_identity
        target["validator_outcomes"] = (
            dict(validator_outcomes) if isinstance(validator_outcomes, dict) else None
        )
        target["validation_detail"] = validation_detail
        if canonical_invalid_media_proof(target) is not None:
            target["invalid_media_count"] = (
                int(target.get("invalid_media_count", 0) or 0) + 1
            )

        self.append_event("PROCESSING_ERROR", host_path)
        if current_identity is None:
            target["marker_status"] = "report_only"
            target["marker_message"] = (
                "No admitted source identity was available; marker and deletion "
                "were skipped."
            )
            target["delete_status"] = "policy_blocked"
            target["delete_message"] = (
                "Untyped processing evidence cannot authorize deletion."
            )
            self.write_summary()
            return

        marker_ready = self.persist_failure_marker(
            target,
            failure_kind="processing_error",
        )
        if marker_ready and self.invalid_media_delete_proof(target) is not None:
            self.try_delete_path(
                host_path,
                target,
                missing_kind="FILE_DELETE_SKIPPED",
                deleted_kind="FILE_DELETED",
                failed_kind="FILE_DELETE_FAILED",
            )
        self.write_summary()

    def record_media_validation_failure(self, payload: dict) -> None:
        required_fields = {
            "event",
            "task_id",
            "task_type",
            "path",
            "failure_class",
            "source_identity",
            "validator_outcomes",
            "validation_detail",
        }
        try:
            if set(payload) != required_fields:
                raise ValueError("Unexpected media-validation event fields")
            container_path = canonical_media_event_path(payload["path"])
            task_type = bounded_event_text(
                payload["task_type"],
                field="task_type",
                maximum=MAX_TASK_TYPE_CHARS,
            )
            task_id = bounded_event_text(
                payload["task_id"],
                field="task_id",
                maximum=MAX_TASK_ID_CHARS,
            )
            if task_type != "transcribe" or task_id != task_event_id(
                {"type": task_type, "path": container_path}
            ):
                raise ValueError("Media-validation event task identity is invalid")
            failure_class = bounded_event_text(
                payload["failure_class"],
                field="failure_class",
                maximum=64,
            )
            source_identity = normalized_event_identity(payload["source_identity"])
            validator_outcomes = normalized_validator_outcomes(
                payload["validator_outcomes"]
            )
            validation_detail = bounded_event_text(
                payload["validation_detail"],
                field="validation_detail",
                maximum=64,
            )
            if source_identity is None or validator_outcomes is None:
                raise ValueError("Media-validation event evidence is invalid")
            candidate_proof = {
                "failure_event": "media_validation_failed",
                "failure_class": failure_class,
                "source_identity": source_identity,
                "validator_outcomes": validator_outcomes,
                "validation_detail": validation_detail,
            }
            if failure_class == "invalid_media":
                if canonical_invalid_media_proof(candidate_proof) is None:
                    raise ValueError("Invalid-media event lacks dual-parser proof")
            elif failure_class == "probe_indeterminate":
                if validation_detail != "validator_evidence_indeterminate":
                    raise ValueError("Indeterminate validation detail is invalid")
            else:
                raise ValueError("Unsupported media-validation failure class")
        except (KeyError, TypeError, ValueError) as exc:
            self.append_event(
                "MEDIA_VALIDATION_EVENT_BLOCKED",
                type(exc).__name__,
            )
            return

        previous = self.active_tasks.get(task_id)
        if previous and (
            previous.get("task_type") != task_type
            or previous.get("container_path") != container_path
            or (
                previous.get("source_identity") is not None
                and previous.get("source_identity") != source_identity
            )
        ):
            self.append_event(
                "STRUCTURED_TERMINAL_STALE",
                f"{task_type} | {container_path}",
            )
            return
        previous = self.active_tasks.pop(task_id, None)
        if (
            previous
            and self.last_transcribe_start
            and self.last_transcribe_start["display_name"].lower()
            == previous["display_name"].lower()
        ):
            another_matching_task = any(
                active["display_name"].lower() == previous["display_name"].lower()
                for active in self.active_tasks.values()
            )
            if not another_matching_task:
                self.last_transcribe_start = None

        self.record_processing_error(
            container_path,
            failure_event="media_validation_failed",
            failure_class=failure_class,
            source_identity=source_identity,
            validator_outcomes=validator_outcomes,
            validation_detail=validation_detail,
        )

    def record_crash_candidate(
        self,
        display_name: str,
        container_path: str | None = None,
        *,
        source_identity=None,
    ) -> None:
        now = utc_stamp()

        resolved_host_path = None
        if container_path:
            try:
                resolved_host_path = self.convert_container_path_to_host_path(
                    container_path
                )
            except ValueError as exc:
                self.append_event(
                    "CRASH_CANDIDATE_PATH_BLOCKED", f"{display_name} | {exc}"
                )

        if resolved_host_path:
            key = exact_path_key(resolved_host_path)
        elif container_path:
            key = str(PurePosixPath(container_path))
        else:
            key = f"legacy:{display_name}"

        event_identity = normalized_event_identity(source_identity)
        current_identity = None
        if resolved_host_path and event_identity is not None:
            current_identity = self.validated_event_generation(
                resolved_host_path,
                event_identity,
            )
        terminal_statuses = {"deleted", "deleted_recovered"}
        recovery_owned_statuses = {
            "blocked_recovery",
            "delete_paused",
            "deleting",
            "failed_recovery",
        }
        reset_generation = False
        if key in self.crash_candidates:
            previous = self.crash_candidates[key]
            if previous.get("delete_status") in recovery_owned_statuses:
                self.append_event(
                    "CRASH_CANDIDATE_RECOVERY_OWNED",
                    f"{display_name} | durable delete intent retained",
                )
                self.write_summary()
                return
            if previous.get("delete_status") in terminal_statuses:
                if current_identity is None:
                    self.append_event(
                        "CRASH_CANDIDATE_STALE",
                        f"{display_name} | ignored after terminal deletion",
                    )
                    self.write_summary()
                    return
                reset_generation = True
            elif current_identity is not None and (
                previous.get("failure_identity") != current_identity
            ):
                reset_generation = True

        if key not in self.crash_candidates or reset_generation:
            self.crash_candidates[key] = {
                "record_kind": "crash_candidate",
                "candidate_id": key,
                "display_name": display_name,
                "container_path": container_path,
                "host_path": resolved_host_path,
                "first_seen_utc": now,
                "last_seen_utc": now,
                "count": 1,
                "delete_status": None,
                "deleted_utc": None,
                "delete_message": None,
                "failure_identity": current_identity,
                "failure_event": "sigsegv",
                "failure_class": "sigsegv",
                "source_identity": event_identity,
                "validator_outcomes": None,
                "validation_detail": None,
            }
        else:
            self.crash_candidates[key]["last_seen_utc"] = now
            self.crash_candidates[key]["count"] += 1
            if current_identity is not None:
                self.crash_candidates[key]["failure_identity"] = current_identity

        self.append_event("CRASH_CANDIDATE", display_name)

        preferred_container_path = container_path or self.recent_container_paths.get(
            display_name.lower()
        )
        existing_host_path = self.crash_candidates[key]["host_path"]
        if preferred_container_path and not resolved_host_path:
            self.remember_container_path(preferred_container_path)
            try:
                resolved_host_path = self.convert_container_path_to_host_path(
                    preferred_container_path
                )
            except ValueError as exc:
                self.append_event(
                    "CRASH_CANDIDATE_PATH_BLOCKED",
                    f"{display_name} | {exc}",
                )
        elif not existing_host_path or not Path(existing_host_path).exists():
            resolved_host_path = self.resolve_crash_candidate_host_path(
                existing_host_path
            )

        if resolved_host_path:
            self.crash_candidates[key]["host_path"] = resolved_host_path
            self.crash_candidates[key]["container_path"] = container_path
            if event_identity is not None:
                resolved_identity = self.validated_event_generation(
                    resolved_host_path,
                    event_identity,
                )
                if resolved_identity is not None:
                    self.crash_candidates[key]["failure_identity"] = resolved_identity

        target = self.crash_candidates[key]
        target["record_kind"] = "crash_candidate"
        target["failure_event"] = "sigsegv"
        target["failure_class"] = "sigsegv"
        target["source_identity"] = event_identity
        target["validator_outcomes"] = None
        target["validation_detail"] = None
        if (
            target["host_path"]
            and target.get("container_path")
            and target.get("failure_identity") == event_identity
        ):
            self.persist_failure_marker(
                target,
                failure_kind="sigsegv",
            )
            target["delete_status"] = "policy_blocked"
            target["delete_message"] = "Native crashes are always retained."
        else:
            target["marker_status"] = "report_only"
            target["marker_message"] = (
                "No unchanged admitted source identity was available; marker and "
                "deletion were skipped."
            )
            target["delete_status"] = "policy_blocked"
            target["delete_message"] = "Native crashes are always retained."
        self.write_summary()

    def send_email_message(self, message: EmailMessage) -> None:
        if self.email_relay_url:
            self.send_relay_message(message)
            return
        self.send_smtp_message(message)

    def send_relay_message(self, message: EmailMessage) -> None:
        if not self.email_relay_admin_key:
            raise RuntimeError("EMAIL_RELAY_ADMIN_KEY is not configured")

        subject = str(message["Subject"] or "Subgen alert")
        body = message.get_content()
        from_address = self.email_relay_from_address or self.smtp_from or ""

        for recipient in self.smtp_to:
            payload = {
                "to": recipient,
                "subject": subject,
                "text": body,
                "fromAddress": from_address,
            }
            request = urllib.request.Request(
                self.email_relay_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Admin-Key": self.email_relay_admin_key,
                    "X-Admin-Name": "Subgen Monitor",
                    "X-Admin-Email": from_address,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    response.read()
            except urllib.error.HTTPError as exc:
                response_body = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(
                    f"Email relay returned HTTP {exc.code}: {response_body}"
                ) from exc

    def send_smtp_message(self, message: EmailMessage) -> None:
        context = (
            ssl.create_default_context()
            if self.smtp_use_tls or self.smtp_use_ssl
            else None
        )
        if self.smtp_use_ssl:
            with smtplib.SMTP_SSL(
                self.smtp_host, self.smtp_port, timeout=30, context=context
            ) as server:
                if self.smtp_username:
                    server.login(self.smtp_username, self.smtp_password)
                server.send_message(message)
            return

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
            if self.smtp_use_tls:
                server.starttls(context=context)
            if self.smtp_username:
                server.login(self.smtp_username, self.smtp_password)
            server.send_message(message)

    def send_restart_cycle_alert(self, item: dict):
        if (not self.smtp_host and not self.email_relay_url) or not self.smtp_to:
            return "skipped", "Email delivery not configured"

        message = EmailMessage()
        message["Subject"] = f"Subgen restart cycle on {os.uname().nodename}"
        message["From"] = self.smtp_from or self.smtp_username or "subgen@localhost"
        message["To"] = ", ".join(self.smtp_to)
        memory_evidence = item.get("memory_evidence") or []
        body_lines = [
            "Subgen has been in a sustained restart loop with memory-pressure evidence.",
            "",
            f"File: {item.get('display_name')}",
            f"Host path: {item.get('host_path') or 'unknown'}",
            f"Cycle count: {item.get('count')}",
            f"Alert threshold: {self.restart_cycle_alert_threshold}",
            f"Minimum duration seconds: {self.restart_cycle_alert_min_seconds}",
            f"Observed duration seconds: {item.get('alert_elapsed_seconds', 'unknown')}",
            f"First seen UTC: {item.get('first_seen_utc')}",
            f"Last seen UTC: {item.get('last_seen_utc')}",
            f"Container: {self.container}",
            "",
            "Memory evidence:",
        ]
        if memory_evidence:
            body_lines.extend(f"- {line}" for line in memory_evidence)
        else:
            body_lines.append("- none recorded")
        body_lines.extend(
            [
                "",
                "This alert is held back until the loop has lasted long enough to avoid one-off warning spam.",
            ]
        )
        message.set_content("\n".join(body_lines))

        try:
            self.send_email_message(message)
            return "sent", "Delivered successfully"
        except Exception as exc:
            return "failed", str(exc)

    def restart_cycle_elapsed_seconds(self, item: dict) -> int:
        first = utc_epoch(item.get("first_seen_utc"))
        last = utc_epoch(item.get("last_seen_utc")) or int(time.time())
        if first is None:
            return 0
        return max(0, last - first)

    def collect_memory_pressure_evidence(self, item: dict) -> list[str]:
        evidence = []
        container_id = ""
        try:
            completed = subprocess.run(
                ["docker", "inspect", "-f", "{{.Id}}", self.container],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            container_id = completed.stdout.strip()
        except Exception:
            pass

        if container_id:
            cgroup_paths = [
                Path(f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope"),
                Path(f"/sys/fs/cgroup/docker/{container_id}"),
            ]
            for cgroup_path in cgroup_paths:
                if not cgroup_path.exists():
                    continue

                events_path = cgroup_path / "memory.events"
                if events_path.exists():
                    events = {}
                    for line in events_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines():
                        parts = line.split()
                        if len(parts) == 2:
                            try:
                                events[parts[0]] = int(parts[1])
                            except ValueError:
                                pass
                    if events.get("oom", 0) > 0 or events.get("oom_kill", 0) > 0:
                        evidence.append(
                            f"cgroup memory.events oom={events.get('oom', 0)} oom_kill={events.get('oom_kill', 0)}"
                        )
                    elif events.get("max", 0) > 0:
                        evidence.append(
                            f"cgroup memory.events max={events.get('max', 0)}"
                        )

                try:
                    peak_path = cgroup_path / "memory.peak"
                    max_path = cgroup_path / "memory.max"
                    peak = (
                        int(peak_path.read_text(encoding="utf-8").strip())
                        if peak_path.exists()
                        else 0
                    )
                    raw_max = (
                        max_path.read_text(encoding="utf-8").strip()
                        if max_path.exists()
                        else ""
                    )
                    limit = 0 if raw_max in {"", "max"} else int(raw_max)
                    if peak and limit and peak >= limit * 0.95:
                        evidence.append(
                            f"memory peak {peak / 1024 / 1024 / 1024:.2f} GiB near limit {limit / 1024 / 1024 / 1024:.2f} GiB"
                        )
                except Exception:
                    pass
                break

        since_epoch = utc_epoch(item.get("first_seen_utc"))
        if since_epoch is not None:
            since = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(max(0, since_epoch - 300))
            )
            try:
                completed = subprocess.run(
                    ["journalctl", "-k", "--since", since, "--no-pager"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=10,
                    check=False,
                )
                lines = [
                    line.strip()
                    for line in completed.stdout.splitlines()
                    if re.search(
                        r"(oom|out of memory|killed process|memory cgroup|python3|ffprobe)",
                        line,
                        re.IGNORECASE,
                    )
                ]
                if lines:
                    evidence.append("kernel memory log: " + lines[-1][-300:])
            except Exception:
                pass

        return evidence

    def restart_cycle_alert_ready(self, item: dict) -> tuple[bool, str]:
        if item.get("email_status") == "sent":
            return False, "already sent"
        if item.get("count", 0) < self.restart_cycle_alert_threshold:
            return False, f"waiting for {self.restart_cycle_alert_threshold} cycles"

        elapsed = self.restart_cycle_elapsed_seconds(item)
        item["alert_elapsed_seconds"] = elapsed
        if elapsed < self.restart_cycle_alert_min_seconds:
            return (
                False,
                f"waiting for {self.restart_cycle_alert_min_seconds}s sustained loop; currently {elapsed}s",
            )

        evidence = self.collect_memory_pressure_evidence(item)
        item["memory_evidence"] = evidence
        if self.restart_cycle_alert_require_memory and not evidence:
            return False, "waiting for memory pressure evidence"

        return True, "sustained restart loop with memory pressure evidence"

    def record_restart_cycle(
        self, display_name: str, container_path: str | None = None
    ) -> None:
        key = display_name.lower()
        now = utc_stamp()

        if key not in self.restart_cycles:
            self.restart_cycles[key] = {
                "display_name": display_name,
                "host_path": None,
                "first_seen_utc": now,
                "last_seen_utc": now,
                "count": 1,
                "email_status": None,
                "email_message": None,
                "email_utc": None,
            }
        else:
            self.restart_cycles[key]["last_seen_utc"] = now
            self.restart_cycles[key]["count"] += 1

        preferred_container_path = container_path or self.recent_container_paths.get(
            key
        )
        if preferred_container_path:
            self.remember_container_path(preferred_container_path)
            try:
                self.restart_cycles[key]["host_path"] = (
                    self.convert_container_path_to_host_path(preferred_container_path)
                )
            except Exception as exc:
                self.append_event("RESTART_CYCLE_PATH_ERROR", f"{display_name} | {exc}")

        item = self.restart_cycles[key]
        self.append_event(
            "RESTART_CYCLE",
            f"{display_name} | count={item['count']} | host_path={item.get('host_path') or 'unknown'}",
        )

        alert_ready, alert_reason = self.restart_cycle_alert_ready(item)
        if alert_ready:
            email_status, email_message = self.send_restart_cycle_alert(item)
            item["email_status"] = email_status
            item["email_message"] = email_message
            item["email_utc"] = now
            self.append_event(
                "RESTART_CYCLE_ALERT",
                f"{display_name} | count={item['count']} | email={email_status} | {email_message}",
            )
        else:
            item["email_message"] = alert_reason
            if item.get("count", 0) >= self.restart_cycle_alert_threshold:
                self.append_event(
                    "RESTART_CYCLE_ALERT_WAIT",
                    f"{display_name} | count={item['count']} | {alert_reason}",
                )

        self.write_summary()

    def send_email_notification(
        self, host_path: str, detected_language: str, english_audio: str
    ):
        if (not self.smtp_host and not self.email_relay_url) or not self.smtp_to:
            return "skipped", "Email delivery not configured"

        message = EmailMessage()
        message["Subject"] = f"Subgen English mismatch on {os.uname().nodename}"
        message["From"] = self.smtp_from or self.smtp_username or "subgen@localhost"
        message["To"] = ", ".join(self.smtp_to)
        message.set_content(
            "\n".join(
                [
                    "Subgen detected a non-English language on a file that still looks English based on its audio metadata.",
                    "",
                    f"File: {host_path}",
                    f"Detected language: {detected_language}",
                    f"English audio tracks: {english_audio}",
                    f"Timestamp (UTC): {utc_stamp()}",
                ]
            )
        )

        try:
            self.send_email_message(message)
            return "sent", "Delivered successfully"
        except Exception as exc:
            return "failed", str(exc)

    def record_english_mismatch(
        self, container_path: str, detected_language: str, english_audio: str
    ) -> None:
        self.remember_container_path(container_path)
        host_path = self.convert_container_path_to_host_path(container_path)
        key = exact_path_key(host_path)
        now = utc_stamp()

        if key not in self.notifications:
            self.notifications[key] = {
                "host_path": host_path,
                "detected_language": detected_language,
                "english_audio": english_audio,
                "first_seen_utc": now,
                "last_seen_utc": now,
                "email_status": None,
                "email_message": None,
            }
        else:
            self.notifications[key]["last_seen_utc"] = now
            self.notifications[key]["detected_language"] = detected_language
            self.notifications[key]["english_audio"] = english_audio

        if self.notifications[key].get("email_status") != "sent":
            if self.email_english_mismatch_alerts:
                email_status, email_message = self.send_email_notification(
                    host_path, detected_language, english_audio
                )
            else:
                email_status, email_message = (
                    "skipped",
                    "English mismatch email alerts disabled",
                )
            self.notifications[key]["email_status"] = email_status
            self.notifications[key]["email_message"] = email_message
            self.append_event(
                "ENGLISH_MISMATCH",
                f"{host_path} | detected={detected_language} | email={email_status}",
            )

        self.write_summary()

    def process_structured_event(self, line: str) -> bool:
        if SUBGEN_EVENT_PREFIX not in line:
            return False

        if len(line.encode("utf-8")) > MAX_STRUCTURED_EVENT_BYTES:
            self.append_event("SUBGEN_EVENT_INVALID", "event exceeded size limit")
            return True
        frame = STRUCTURED_EVENT_FRAME_RE.fullmatch(line)
        if frame is None:
            self.append_event("SUBGEN_EVENT_INVALID", "invalid event framing")
            return True
        try:
            payload = json.loads(
                frame.group("payload"),
                object_pairs_hook=reject_duplicate_json_keys,
            )
            if not isinstance(payload, dict):
                raise ValueError("Structured event must be an object")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.append_event("SUBGEN_EVENT_INVALID", type(exc).__name__)
            return True

        try:
            event = bounded_event_text(
                payload.get("event"),
                field="event",
                maximum=64,
            )
        except ValueError as exc:
            self.append_event("SUBGEN_EVENT_INVALID", type(exc).__name__)
            return True

        if event == "media_validation_failed":
            self.record_media_validation_failure(payload)
            return True

        try:
            task_id = bounded_event_text(
                payload.get("task_id"),
                field="task_id",
                maximum=MAX_TASK_ID_CHARS,
            )
            task_type = bounded_event_text(
                payload.get("task_type"),
                field="task_type",
                maximum=MAX_TASK_TYPE_CHARS,
            )
            container_path = bounded_event_text(
                payload.get("path", "unknown"),
                field="path",
                maximum=MAX_EVENT_PATH_CHARS,
            )
        except ValueError as exc:
            self.append_event("SUBGEN_EVENT_INVALID", type(exc).__name__)
            return True

        source_identity = normalized_event_identity(payload.get("source_identity"))
        if "source_identity" in payload and source_identity is None:
            self.append_event("SUBGEN_EVENT_INVALID", "invalid source identity")
            return True
        if not task_id:
            task_id = f"{task_type}:{container_path}"

        if event == "worker_start":
            previous = self.active_tasks.get(task_id)
            if previous and task_type == "transcribe":
                self.record_restart_cycle(
                    Path(container_path).name,
                    container_path if container_path.startswith("/media/") else None,
                )
                self.record_crash_candidate(
                    previous["display_name"],
                    previous.get("container_path"),
                    source_identity=previous.get("source_identity"),
                )
            self.active_tasks[task_id] = {
                "task_id": task_id,
                "task_type": task_type,
                "container_path": container_path,
                "display_name": Path(container_path).name,
                "seen_utc": utc_stamp(),
                "source_identity": source_identity,
            }
            self.remember_container_path(container_path)
            self.append_event("STRUCTURED_START", f"{task_type} | {container_path}")
            return True

        if event == "runtime_error":
            previous = self.active_tasks.pop(task_id, None)
            if previous and self.last_transcribe_start:
                display_name = previous["display_name"].lower()
                another_matching_task = any(
                    active["display_name"].lower() == display_name
                    for active in self.active_tasks.values()
                )
                if (
                    not another_matching_task
                    and self.last_transcribe_start["display_name"].lower()
                    == display_name
                ):
                    self.last_transcribe_start = None
            error_code = str(payload.get("error_code") or "runtime_error")[:120]
            self.append_event("MODEL_RUNTIME_ERROR", error_code)
            return True

        if event in {"worker_finish", "worker_error", "media_validation_stale"}:
            previous = self.active_tasks.get(task_id)
            if previous and (
                previous.get("task_type") != task_type
                or previous.get("container_path") != container_path
                or (
                    previous.get("source_identity") is not None
                    and source_identity != previous.get("source_identity")
                )
            ):
                self.append_event(
                    "STRUCTURED_TERMINAL_STALE",
                    f"{task_type} | {container_path}",
                )
                return True
            previous = self.active_tasks.pop(task_id, None)
            if (
                self.last_transcribe_start
                and self.last_transcribe_start["display_name"].lower()
                == Path(container_path).name.lower()
            ):
                self.last_transcribe_start = None
            if event == "media_validation_stale":
                self.append_event(
                    "MEDIA_VALIDATION_STALE",
                    f"{task_type} | {container_path}",
                )
            elif event == "worker_error" and container_path.startswith("/media/"):
                failure_class = (
                    "resource_exhaustion"
                    if payload.get("failure_class") == "resource_exhaustion"
                    else "inference_error"
                )
                admitted_identity = (
                    previous.get("source_identity") if previous else source_identity
                )
                self.record_processing_error(
                    container_path,
                    failure_event="worker_error",
                    failure_class=failure_class,
                    source_identity=admitted_identity,
                )
            else:
                self.append_event(
                    "STRUCTURED_FINISH", f"{task_type} | {container_path}"
                )
            return True

        if event == "file_error":
            if container_path.startswith("/media/"):
                self.record_processing_error(
                    container_path,
                    failure_event="file_error",
                    failure_class="processing_error",
                    source_identity=source_identity,
                )
            else:
                self.append_event("FILE_ERROR_PATH_BLOCKED", container_path)
            return True

        self.append_event("SUBGEN_EVENT_UNKNOWN", event)
        return True

    def process_log_line(self, line: str) -> None:
        if not line:
            return

        if self.process_structured_event(line):
            return

        match = TRANSCRIBE_START_RE.search(line)
        if match:
            display_name = match.group("name").strip()
            key = display_name.lower()
            tracked_by_structured_event = any(
                item.get("display_name", "").lower() == key
                for item in self.active_tasks.values()
            )
            if (
                self.last_transcribe_start
                and self.last_transcribe_start["display_name"].lower() == key
                and not tracked_by_structured_event
            ):
                self.record_restart_cycle(
                    display_name,
                    self.last_transcribe_start.get("container_path")
                    or self.recent_container_paths.get(key),
                )
            self.last_transcribe_start = {
                "display_name": display_name,
                "seen_utc": utc_stamp(),
            }
            if key in self.recent_container_paths:
                self.last_transcribe_start["container_path"] = (
                    self.recent_container_paths[key]
                )
            self.append_event("TRANSCRIBE_START", display_name)
            return

        match = TRANSCRIBE_FINISH_RE.search(line)
        if match:
            display_name = match.group("name").strip()
            if (
                self.last_transcribe_start
                and self.last_transcribe_start["display_name"].lower()
                == display_name.lower()
            ):
                self.last_transcribe_start = None
            self.append_event("TRANSCRIBE_FINISH", display_name)
            return

        match = MEDIA_PATH_ACTIVITY_RE.search(line)
        if match:
            path = match.groupdict().get("detect_path") or match.groupdict().get(
                "extract_path"
            )
            self.remember_container_path(path.strip())
            return

        match = PROCESSING_ERROR_RE.search(line)
        if match:
            self.record_processing_error(match.group("path").strip())
            return

        match = ENGLISH_MISMATCH_RE.search(line)
        if match:
            self.record_english_mismatch(
                match.group("path").strip(),
                match.group("detected").strip(),
                match.group("audio").strip(),
            )
            return

        if "SIGSEGV" in line:
            if len(self.active_tasks) == 1:
                active_task = next(iter(self.active_tasks.values()))
                self.record_crash_candidate(
                    active_task["display_name"],
                    active_task.get("container_path"),
                    source_identity=active_task.get("source_identity"),
                )
            elif not self.active_tasks and self.last_transcribe_start:
                self.record_crash_candidate(
                    self.last_transcribe_start["display_name"],
                    self.last_transcribe_start.get("container_path"),
                )
            else:
                self.append_event(
                    "SIGSEGV",
                    f"Crash seen without one exact active task (active={len(self.active_tasks)})",
                )
            self.active_tasks.clear()
            self.last_transcribe_start = None

    def follow_logs(self, since: str) -> None:
        command = [
            "docker",
            "logs",
            "--follow",
            "--since",
            since,
            self.container,
        ]
        self.append_event("FOLLOW", " ".join(command))
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
        )

        assert process.stdout is not None
        for line in iter_bounded_log_records(process.stdout):
            if line is None:
                self.append_event(
                    "LOG_RECORD_DROPPED",
                    f"record exceeded {MAX_LOG_RECORD_BYTES} bytes",
                )
                continue
            self.process_log_line(line)

        return_code = process.wait()
        if return_code != 0:
            self.append_event(
                "FOLLOW_EXIT", f"docker logs exited with status {return_code}"
            )

    def run(self, since: str) -> None:
        self.write_summary()
        self.append_event(
            "MONITOR_START",
            f"Watching container '{self.container}' "
            f"(auto_delete_invalid_media={self.auto_delete})",
        )
        cursor = since

        while True:
            try:
                self.follow_logs(cursor)
            except Exception as exc:
                self.append_event("MONITOR_ERROR", str(exc))

            time.sleep(self.reconnect_delay_seconds)
            cursor = utc_stamp()
            write_private_text(
                self.heartbeat_path,
                f"{utc_stamp()} reconnect after follow exit\n",
            )


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Monitor Subgen logs, preserve failure evidence, and optionally delete "
            "only media proven invalid by both validators."
        )
    )
    parser.add_argument("--container", default=os.getenv("SUBGEN_CONTAINER", "subgen"))
    parser.add_argument("--media-root", default=os.getenv("MEDIA_ROOT", "/srv/media"))
    parser.add_argument(
        "--state-dir",
        default=os.getenv("SUBGEN_STATE_DIR", "/opt/subgen/monitor"),
    )
    parser.add_argument(
        "--since",
        default=env_default(
            "SUBGEN_LOG_SINCE",
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10)),
        ),
    )
    parser.add_argument(
        "--reconnect-delay-seconds",
        type=int,
        default=int(os.getenv("SUBGEN_RECONNECT_DELAY_SECONDS", "5")),
    )
    parser.add_argument(
        "--auto-mark-failed-files",
        action="store_true",
        default=env_bool("AUTO_MARK_FAILED_FILES", True),
    )
    parser.add_argument(
        "--auto-mark-min-failures",
        type=int,
        default=int(os.getenv("AUTO_MARK_MIN_FAILURES", "1")),
    )
    parser.add_argument(
        "--auto-delete-invalid-media",
        action="store_true",
        default=env_bool("AUTO_DELETE_INVALID_MEDIA", False),
    )
    parser.add_argument(
        "--auto-delete-failed-files",
        action="store_true",
        default=env_bool("AUTO_DELETE_FAILED_FILES", False),
    )
    parser.add_argument(
        "--auto-delete-min-failures",
        type=int,
        default=int(os.getenv("AUTO_DELETE_MIN_FAILURES", "1")),
    )
    parser.add_argument(
        "--restart-cycle-alert-threshold",
        type=int,
        default=int(os.getenv("SUBGEN_RESTART_CYCLE_ALERT_THRESHOLD", "6")),
    )
    parser.add_argument(
        "--restart-cycle-alert-min-seconds",
        type=int,
        default=int(os.getenv("SUBGEN_RESTART_CYCLE_ALERT_MIN_SECONDS", "3600")),
    )
    parser.add_argument(
        "--restart-cycle-alert-require-memory",
        action="store_true",
        default=env_bool("SUBGEN_RESTART_CYCLE_ALERT_REQUIRE_MEMORY", True),
    )
    parser.add_argument("--smtp-host", default=os.getenv("SMTP_HOST", ""))
    parser.add_argument(
        "--smtp-port", type=int, default=int(os.getenv("SMTP_PORT", "587"))
    )
    parser.add_argument("--smtp-username", default=os.getenv("SMTP_USERNAME", ""))
    parser.add_argument("--smtp-password", default=os.getenv("SMTP_PASSWORD", ""))
    parser.add_argument("--smtp-from", default=os.getenv("SMTP_FROM", ""))
    parser.add_argument("--smtp-to", default=os.getenv("SMTP_TO", "alerts@example.com"))
    parser.add_argument(
        "--smtp-use-tls", action="store_true", default=env_bool("SMTP_USE_TLS", True)
    )
    parser.add_argument(
        "--smtp-use-ssl", action="store_true", default=env_bool("SMTP_USE_SSL", False)
    )
    parser.add_argument("--email-relay-url", default=os.getenv("EMAIL_RELAY_URL", ""))
    parser.add_argument(
        "--email-relay-admin-key", default=os.getenv("EMAIL_RELAY_ADMIN_KEY", "")
    )
    parser.add_argument(
        "--email-relay-from-address", default=os.getenv("EMAIL_RELAY_FROM_ADDRESS", "")
    )
    parser.add_argument(
        "--email-english-mismatch-alerts",
        action="store_true",
        default=env_bool("EMAIL_ENGLISH_MISMATCH_ALERTS", False),
    )
    return parser.parse_args()


def main():
    args = parse_args()
    with monitor_process_lock(args.state_dir):
        monitor = Monitor(args)
        monitor.run(args.since)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
