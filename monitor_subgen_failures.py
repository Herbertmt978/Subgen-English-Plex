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
from email.message import EmailMessage
from pathlib import Path, PurePosixPath

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
    validate_regular_file_beneath,
)


TRANSCRIBE_START_RE = re.compile(r"WORKER START : \[TRANSCRIBE\s*\] (?P<name>.+?) \| Jobs:")
TRANSCRIBE_FINISH_RE = re.compile(r"WORKER FINISH:\s*\[TRANSCRIBE\s*\] (?P<name>.+?) in ")
PROCESSING_ERROR_RE = re.compile(r"Error processing file (?P<path>/media/.+)$")
ENGLISH_MISMATCH_RE = re.compile(
    r"ENGLISH_AUDIO_MISMATCH \| (?P<path>.+?) \| detected=(?P<detected>[^|]+) \| audio=(?P<audio>.+)$"
)
MEDIA_PATH_ACTIVITY_RE = re.compile(
    r"(?:Detecting language of file: (?P<detect_path>/media/.+) \([^/]*starting at[^/]*\)|Extracting audio from: (?P<extract_path>/media/.+), start_time:)"
)
SUBGEN_EVENT_PREFIX = "SUBGEN_EVENT "


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
            or (
                sys.platform.startswith("linux")
                and file_stat.st_uid != os.geteuid()
            )
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
            or (
                sys.platform.startswith("linux")
                and file_stat.st_uid != os.geteuid()
            )
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
        self.auto_delete = args.auto_delete_failed_files
        self.auto_delete_min_failures = max(1, args.auto_delete_min_failures)
        if (
            self.auto_mark
            and self.auto_delete
            and self.auto_delete_min_failures > self.auto_mark_min_failures
        ):
            raise ValueError(
                "AUTO_DELETE_MIN_FAILURES cannot exceed "
                "AUTO_MARK_MIN_FAILURES while both features are enabled"
            )
        self.smtp_host = args.smtp_host
        self.smtp_port = args.smtp_port
        self.smtp_username = args.smtp_username
        self.smtp_password = args.smtp_password
        self.smtp_from = args.smtp_from
        self.smtp_to = [item.strip() for item in args.smtp_to.split(",") if item.strip()]
        self.smtp_use_tls = args.smtp_use_tls
        self.smtp_use_ssl = args.smtp_use_ssl
        self.email_relay_url = args.email_relay_url
        self.email_relay_admin_key = args.email_relay_admin_key
        self.email_relay_from_address = args.email_relay_from_address
        self.email_english_mismatch_alerts = args.email_english_mismatch_alerts
        self.reconnect_delay_seconds = args.reconnect_delay_seconds
        self.restart_cycle_alert_threshold = args.restart_cycle_alert_threshold
        self.restart_cycle_alert_min_seconds = args.restart_cycle_alert_min_seconds
        self.restart_cycle_alert_require_memory = args.restart_cycle_alert_require_memory
        self.summary_path = self.state_dir / "subgen_failed_files.txt"
        self.events_path = self.state_dir / "subgen_failed_events.log"
        self.state_path = self.state_dir / "subgen_failed_state.json"
        self.heartbeat_path = self.state_dir / "subgen_failure_monitor_heartbeat.txt"
        self.marker_registry_path = self.state_dir / Path(
            DEFAULT_MARKER_REGISTRY_PATH
        ).name
        self.processing_errors = {}
        self.crash_candidates = {}
        self.notifications = {}
        self.restart_cycles = {}
        self.last_transcribe_start = None
        self.recent_container_paths = {}
        self.active_tasks = {}

        self.load_state()

    def load_state(self) -> None:
        if not self.state_path.exists():
            return

        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(state, dict):
            return

        blocked_loads = []
        self.processing_errors = {}
        for index, item in enumerate(state.get("processing_errors", [])):
            if not isinstance(item, dict) or not item.get("host_path"):
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
        for index, item in enumerate(state.get("crash_candidates", [])):
            if not isinstance(item, dict) or not item.get("display_name"):
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
                key = str(
                    item.get("candidate_id") or f"legacy:{item['display_name']}"
                )
            item["candidate_id"] = key
            self.crash_candidates[key] = item
        self.notifications = {}
        for index, item in enumerate(state.get("notifications", [])):
            if not isinstance(item, dict) or not item.get("host_path"):
                continue
            key, blocked_message = self.loaded_path_key(
                item["host_path"], "notifications", index
            )
            if blocked_message:
                blocked_loads.append((item.get("host_path"), blocked_message))
            self.notifications[key] = item
        self.restart_cycles = {
            item["display_name"].lower(): item
            for item in state.get("restart_cycles", [])
            if isinstance(item, dict) and item.get("display_name")
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
    def loaded_path_key(host_path: object, collection: str, index: int) -> tuple[str, str | None]:
        try:
            return exact_path_key(host_path), None
        except (TypeError, ValueError, UnsafePathError) as exc:
            return (
                f"invalid:{collection}:{index}",
                f"Persisted path was quarantined: {exc}",
            )

    def save_state(self) -> None:
        state = {
            "updated_utc": utc_stamp(),
            "container_name": self.container,
            "media_root": str(self.media_root),
            "processing_errors": sorted(self.processing_errors.values(), key=lambda item: item["host_path"]),
            "crash_candidates": sorted(self.crash_candidates.values(), key=lambda item: item["display_name"]),
            "notifications": sorted(self.notifications.values(), key=lambda item: item["host_path"]),
            "restart_cycles": sorted(self.restart_cycles.values(), key=lambda item: item["display_name"]),
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
                target["failure_identity"] = self.current_failure_identity(
                    target["host_path"]
                )
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
            f"Auto delete failed files: {self.auto_delete}",
            f"Auto delete minimum failures: {self.auto_delete_min_failures}",
            "",
            "Processing errors:",
        ]

        if not self.processing_errors:
            lines.append("  none")
        else:
            for item in sorted(self.processing_errors.values(), key=lambda value: value["host_path"]):
                lines.append(f"  {item['host_path']}")
                lines.append(f"    container: {item['container_path']}")
                lines.append(f"    first_seen_utc: {item['first_seen_utc']}")
                lines.append(f"    last_seen_utc: {item['last_seen_utc']}")
                lines.append(f"    count: {item['count']}")
                self.append_marker_summary(lines, item)
                if item.get("delete_status"):
                    lines.append(f"    delete_status: {item['delete_status']}")
                    lines.append(f"    deleted_utc: {item.get('deleted_utc', '')}")
                    lines.append(f"    delete_message: {item.get('delete_message', '')}")

        lines.extend(["", "Crash candidates before SIGSEGV:"])
        if not self.crash_candidates:
            lines.append("  none")
        else:
            for item in sorted(self.crash_candidates.values(), key=lambda value: value["display_name"]):
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
                    lines.append(f"    delete_message: {item.get('delete_message', '')}")

        lines.extend(["", "Repeated transcribe/restart cycles:"])
        if not self.restart_cycles:
            lines.append("  none")
        else:
            for item in sorted(self.restart_cycles.values(), key=lambda value: value["display_name"]):
                lines.append(f"  {item['display_name']}")
                if item.get("host_path"):
                    lines.append(f"    host_path: {item['host_path']}")
                lines.append(f"    first_seen_utc: {item['first_seen_utc']}")
                lines.append(f"    last_seen_utc: {item['last_seen_utc']}")
                lines.append(f"    count: {item['count']}")
                lines.append(f"    alert_threshold: {self.restart_cycle_alert_threshold}")
                lines.append(f"    alert_min_seconds: {self.restart_cycle_alert_min_seconds}")
                lines.append(f"    alert_require_memory: {self.restart_cycle_alert_require_memory}")
                if item.get("alert_elapsed_seconds") is not None:
                    lines.append(f"    alert_elapsed_seconds: {item['alert_elapsed_seconds']}")
                if item.get("memory_evidence"):
                    lines.append("    memory_evidence:")
                    lines.extend(f"      - {entry}" for entry in item.get("memory_evidence", []))
                lines.append(f"    email_status: {item.get('email_status', 'not_sent')}")
                if item.get("email_message"):
                    lines.append(f"    email_message: {item['email_message']}")

        lines.extend(["", "English mismatch notifications:"])
        if not self.notifications:
            lines.append("  none")
        else:
            for item in sorted(self.notifications.values(), key=lambda value: value["host_path"]):
                lines.append(f"  {item['host_path']}")
                lines.append(f"    detected_language: {item['detected_language']}")
                lines.append(f"    english_audio: {item['english_audio']}")
                lines.append(f"    first_seen_utc: {item['first_seen_utc']}")
                lines.append(f"    last_seen_utc: {item['last_seen_utc']}")
                lines.append(f"    email_status: {item.get('email_status', 'not_sent')}")
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
            return True

        failure_count = int(target.get("count", 0) or 0)
        if failure_count < self.auto_mark_min_failures:
            target["marker_status"] = "waiting"
            target["marker_message"] = (
                f"Waiting for {self.auto_mark_min_failures} failures; "
                f"currently {failure_count}."
            )
            return True

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

    def marker_allows_delete(self, target: dict) -> bool:
        if not self.auto_delete or not self.auto_mark:
            return True
        failure_count = int(target.get("count", 0) or 0)
        if failure_count < self.auto_delete_min_failures:
            return True
        if target.get("marker_status") in {"created", "refreshed"}:
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
        """Capture an unfingerprinted generation without adopting its failure."""

        if (
            not self.auto_delete
            or int(target.get("count", 0) or 0) < self.auto_delete_min_failures
            or target.get("failure_identity")
        ):
            return True

        now = utc_stamp()
        try:
            captured_identity = validate_regular_file_beneath(
                self.media_root,
                host_path,
            )
        except CandidateUnavailableError:
            self.finish_delete_outcome(
                target,
                status="missing",
                message="Path not found while capturing file-generation evidence.",
                event_kind=missing_kind,
                event_message=f"{host_path} | missing generation evidence",
                timestamp=now,
            )
            return False
        except UnsafePathError as exc:
            self.finish_delete_outcome(
                target,
                status="blocked",
                message=str(exc),
                event_kind="FILE_DELETE_BLOCKED",
                event_message=f"{host_path} | {exc}",
                timestamp=now,
            )
            return False

        target["failure_identity"] = list(captured_identity)
        target["count"] = 0
        target["delete_status"] = "waiting"
        target["delete_message"] = (
            "Captured a new file-generation fingerprint; failure count reset."
        )
        self.save_state()
        return False

    def convert_container_path_to_host_path(self, container_path: str) -> str:
        if not container_path or not container_path.startswith("/media/"):
            raise ValueError(f"Unsupported media path: {container_path}")

        relative_path = container_path[len("/media/") :]
        posix_path = PurePosixPath(relative_path)
        if posix_path.is_absolute() or ".." in posix_path.parts or "\0" in relative_path:
            raise ValueError(f"Refusing unsafe media path: {container_path}")
        host_path = lexical_host_path(self.media_root.joinpath(*posix_path.parts))
        try:
            host_path.relative_to(self.media_root)
        except ValueError as exc:
            raise ValueError(f"Refusing path outside media root: {host_path}") from exc
        return str(host_path)

    def try_delete_path(self, host_path: str, target: dict, missing_kind: str, deleted_kind: str, failed_kind: str) -> None:
        if not self.auto_delete:
            return

        failure_count = int(target.get("count", 0) or 0)
        if failure_count < self.auto_delete_min_failures:
            target["delete_status"] = "waiting"
            target["delete_message"] = (
                f"Waiting for {self.auto_delete_min_failures} failures; currently {failure_count}."
            )
            return

        if not self.capture_delete_generation_if_needed(
            host_path,
            target,
            missing_kind=missing_kind,
        ):
            return

        now = utc_stamp()
        try:
            identity = validate_regular_file_beneath(
                self.media_root,
                host_path,
                expected_identity=target.get("failure_identity"),
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
        for collection in (self.processing_errors, self.crash_candidates):
            for target in collection.values():
                if target.get("delete_status") not in {"deleting", "delete_paused"}:
                    continue
                host_path = target.get("host_path")
                identity = target.get("delete_identity")
                deleted_kind = target.get("delete_event_kind") or "FILE_DELETED"
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
                if not target.get("delete_token"):
                    target["delete_token"] = new_delete_token()
                    self.save_state()
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

        if self.last_transcribe_start and self.last_transcribe_start["display_name"].lower() == key:
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

    def record_processing_error(self, container_path: str) -> None:
        self.remember_container_path(container_path)
        try:
            host_path = self.convert_container_path_to_host_path(container_path)
        except ValueError as exc:
            self.append_event("PROCESSING_ERROR_PATH_BLOCKED", str(exc))
            return
        key = exact_path_key(host_path)
        now = utc_stamp()
        current_identity = self.current_failure_identity(host_path)
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
                "host_path": host_path,
                "container_path": container_path,
                "first_seen_utc": now,
                "last_seen_utc": now,
                "count": 1,
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

        self.append_event("PROCESSING_ERROR", host_path)
        target = self.processing_errors[key]
        if self.auto_mark and not self.capture_delete_generation_if_needed(
            host_path,
            target,
            missing_kind="FILE_DELETE_SKIPPED",
        ):
            self.write_summary()
            return
        marker_ready = self.persist_failure_marker(
            target,
            failure_kind="processing_error",
        )
        if marker_ready and self.marker_allows_delete(target):
            self.try_delete_path(
                host_path,
                target,
                missing_kind="FILE_DELETE_SKIPPED",
                deleted_kind="FILE_DELETED",
                failed_kind="FILE_DELETE_FAILED",
            )
        self.write_summary()

    def record_crash_candidate(self, display_name: str, container_path: str | None = None) -> None:
        now = utc_stamp()

        resolved_host_path = None
        if container_path:
            try:
                resolved_host_path = self.convert_container_path_to_host_path(container_path)
            except ValueError as exc:
                self.append_event("CRASH_CANDIDATE_PATH_BLOCKED", f"{display_name} | {exc}")

        if resolved_host_path:
            key = exact_path_key(resolved_host_path)
        elif container_path:
            key = str(PurePosixPath(container_path))
        else:
            key = f"legacy:{display_name}"

        current_identity = (
            self.current_failure_identity(resolved_host_path)
            if resolved_host_path
            else None
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
            }
        else:
            self.crash_candidates[key]["last_seen_utc"] = now
            self.crash_candidates[key]["count"] += 1
            if current_identity is not None:
                self.crash_candidates[key]["failure_identity"] = current_identity

        self.append_event("CRASH_CANDIDATE", display_name)

        preferred_container_path = container_path or self.recent_container_paths.get(display_name.lower())
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
            resolved_host_path = self.resolve_crash_candidate_host_path(existing_host_path)

        if resolved_host_path:
            self.crash_candidates[key]["host_path"] = resolved_host_path
            self.crash_candidates[key]["container_path"] = container_path
            resolved_identity = self.current_failure_identity(resolved_host_path)
            if resolved_identity is not None:
                self.crash_candidates[key]["failure_identity"] = resolved_identity

        target = self.crash_candidates[key]
        if target["host_path"] and target.get("container_path"):
            if self.auto_mark and not self.capture_delete_generation_if_needed(
                target["host_path"],
                target,
                missing_kind="CRASH_FILE_DELETE_SKIPPED",
            ):
                self.write_summary()
                return
            marker_ready = self.persist_failure_marker(
                target,
                failure_kind="sigsegv",
            )
            if marker_ready and self.marker_allows_delete(target):
                self.try_delete_path(
                    target["host_path"],
                    target,
                    missing_kind="CRASH_FILE_DELETE_SKIPPED",
                    deleted_kind="CRASH_FILE_DELETED",
                    failed_kind="CRASH_FILE_DELETE_FAILED",
                )
        elif not target.get("container_path"):
            target["marker_status"] = "report_only"
            target["marker_message"] = (
                "No exact container path was available; marker and deletion skipped."
            )
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
                raise RuntimeError(f"Email relay returned HTTP {exc.code}: {response_body}") from exc

    def send_smtp_message(self, message: EmailMessage) -> None:
        context = ssl.create_default_context() if self.smtp_use_tls or self.smtp_use_ssl else None
        if self.smtp_use_ssl:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=30, context=context) as server:
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
        body_lines.extend([
            "",
            "This alert is held back until the loop has lasted long enough to avoid one-off warning spam.",
        ])
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
                    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
                        parts = line.split()
                        if len(parts) == 2:
                            try:
                                events[parts[0]] = int(parts[1])
                            except ValueError:
                                pass
                    if events.get("oom", 0) > 0 or events.get("oom_kill", 0) > 0:
                        evidence.append(f"cgroup memory.events oom={events.get('oom', 0)} oom_kill={events.get('oom_kill', 0)}")
                    elif events.get("max", 0) > 0:
                        evidence.append(f"cgroup memory.events max={events.get('max', 0)}")

                try:
                    peak_path = cgroup_path / "memory.peak"
                    max_path = cgroup_path / "memory.max"
                    peak = int(peak_path.read_text(encoding="utf-8").strip()) if peak_path.exists() else 0
                    raw_max = max_path.read_text(encoding="utf-8").strip() if max_path.exists() else ""
                    limit = 0 if raw_max in {"", "max"} else int(raw_max)
                    if peak and limit and peak >= limit * 0.95:
                        evidence.append(f"memory peak {peak / 1024 / 1024 / 1024:.2f} GiB near limit {limit / 1024 / 1024 / 1024:.2f} GiB")
                except Exception:
                    pass
                break

        since_epoch = utc_epoch(item.get("first_seen_utc"))
        if since_epoch is not None:
            since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(max(0, since_epoch - 300)))
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
                    if re.search(r"(oom|out of memory|killed process|memory cgroup|python3|ffprobe)", line, re.IGNORECASE)
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
            return False, f"waiting for {self.restart_cycle_alert_min_seconds}s sustained loop; currently {elapsed}s"

        evidence = self.collect_memory_pressure_evidence(item)
        item["memory_evidence"] = evidence
        if self.restart_cycle_alert_require_memory and not evidence:
            return False, "waiting for memory pressure evidence"

        return True, "sustained restart loop with memory pressure evidence"

    def record_restart_cycle(self, display_name: str, container_path: str | None = None) -> None:
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

        preferred_container_path = container_path or self.recent_container_paths.get(key)
        if preferred_container_path:
            self.remember_container_path(preferred_container_path)
            try:
                self.restart_cycles[key]["host_path"] = self.convert_container_path_to_host_path(preferred_container_path)
            except Exception as exc:
                self.append_event("RESTART_CYCLE_PATH_ERROR", f"{display_name} | {exc}")

        item = self.restart_cycles[key]
        self.append_event("RESTART_CYCLE", f"{display_name} | count={item['count']} | host_path={item.get('host_path') or 'unknown'}")

        alert_ready, alert_reason = self.restart_cycle_alert_ready(item)
        if alert_ready:
            email_status, email_message = self.send_restart_cycle_alert(item)
            item["email_status"] = email_status
            item["email_message"] = email_message
            item["email_utc"] = now
            self.append_event("RESTART_CYCLE_ALERT", f"{display_name} | count={item['count']} | email={email_status} | {email_message}")
        else:
            item["email_message"] = alert_reason
            if item.get("count", 0) >= self.restart_cycle_alert_threshold:
                self.append_event("RESTART_CYCLE_ALERT_WAIT", f"{display_name} | count={item['count']} | {alert_reason}")

        self.write_summary()

    def send_email_notification(self, host_path: str, detected_language: str, english_audio: str):
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

    def record_english_mismatch(self, container_path: str, detected_language: str, english_audio: str) -> None:
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
                email_status, email_message = self.send_email_notification(host_path, detected_language, english_audio)
            else:
                email_status, email_message = "skipped", "English mismatch email alerts disabled"
            self.notifications[key]["email_status"] = email_status
            self.notifications[key]["email_message"] = email_message
            self.append_event("ENGLISH_MISMATCH", f"{host_path} | detected={detected_language} | email={email_status}")

        self.write_summary()

    def process_structured_event(self, line: str) -> bool:
        if SUBGEN_EVENT_PREFIX not in line:
            return False

        try:
            payload = json.loads(line.split(SUBGEN_EVENT_PREFIX, 1)[1])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.append_event("SUBGEN_EVENT_INVALID", str(exc))
            return True

        event = str(payload.get("event", ""))
        task_id = str(payload.get("task_id") or "")
        task_type = str(payload.get("task_type") or "")
        container_path = str(payload.get("path") or "")
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
                )
            self.active_tasks[task_id] = {
                "task_id": task_id,
                "task_type": task_type,
                "container_path": container_path,
                "display_name": Path(container_path).name,
                "seen_utc": utc_stamp(),
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

        if event in {"worker_finish", "worker_error"}:
            self.active_tasks.pop(task_id, None)
            if (
                self.last_transcribe_start
                and self.last_transcribe_start["display_name"].lower()
                == Path(container_path).name.lower()
            ):
                self.last_transcribe_start = None
            if event == "worker_error" and container_path.startswith("/media/"):
                self.record_processing_error(container_path)
            else:
                self.append_event("STRUCTURED_FINISH", f"{task_type} | {container_path}")
            return True

        if event == "file_error":
            if container_path.startswith("/media/"):
                self.record_processing_error(container_path)
            else:
                self.append_event("FILE_ERROR_PATH_BLOCKED", container_path)
            return True

        return False

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
                    self.last_transcribe_start.get("container_path") or self.recent_container_paths.get(key),
                )
            self.last_transcribe_start = {"display_name": display_name, "seen_utc": utc_stamp()}
            if key in self.recent_container_paths:
                self.last_transcribe_start["container_path"] = self.recent_container_paths[key]
            self.append_event("TRANSCRIBE_START", display_name)
            return

        match = TRANSCRIBE_FINISH_RE.search(line)
        if match:
            display_name = match.group("name").strip()
            if self.last_transcribe_start and self.last_transcribe_start["display_name"].lower() == display_name.lower():
                self.last_transcribe_start = None
            self.append_event("TRANSCRIBE_FINISH", display_name)
            return

        match = MEDIA_PATH_ACTIVITY_RE.search(line)
        if match:
            path = match.groupdict().get("detect_path") or match.groupdict().get("extract_path")
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
                )
            elif self.last_transcribe_start:
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
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        assert process.stdout is not None
        for line in process.stdout:
            self.process_log_line(line.rstrip("\n"))

        return_code = process.wait()
        if return_code != 0:
            self.append_event("FOLLOW_EXIT", f"docker logs exited with status {return_code}")

    def run(self, since: str) -> None:
        self.write_summary()
        self.append_event("MONITOR_START", f"Watching container '{self.container}' (auto_delete_failed_files={self.auto_delete})")
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
    parser = argparse.ArgumentParser(description="Monitor Subgen logs and clean up failed media.")
    parser.add_argument("--container", default=os.getenv("SUBGEN_CONTAINER", "subgen"))
    parser.add_argument("--media-root", default=os.getenv("MEDIA_ROOT", "/srv/media"))
    parser.add_argument(
        "--state-dir",
        default=os.getenv("SUBGEN_STATE_DIR", "/opt/subgen/monitor"),
    )
    parser.add_argument(
        "--since",
        default=env_default("SUBGEN_LOG_SINCE", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10))),
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
        "--auto-delete-failed-files",
        action="store_true",
        default=env_bool("AUTO_DELETE_FAILED_FILES", False),
    )
    parser.add_argument(
        "--auto-delete-min-failures",
        type=int,
        default=int(os.getenv("AUTO_DELETE_MIN_FAILURES", "3")),
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
    parser.add_argument("--smtp-port", type=int, default=int(os.getenv("SMTP_PORT", "587")))
    parser.add_argument("--smtp-username", default=os.getenv("SMTP_USERNAME", ""))
    parser.add_argument("--smtp-password", default=os.getenv("SMTP_PASSWORD", ""))
    parser.add_argument("--smtp-from", default=os.getenv("SMTP_FROM", ""))
    parser.add_argument("--smtp-to", default=os.getenv("SMTP_TO", "alerts@example.com"))
    parser.add_argument("--smtp-use-tls", action="store_true", default=env_bool("SMTP_USE_TLS", True))
    parser.add_argument("--smtp-use-ssl", action="store_true", default=env_bool("SMTP_USE_SSL", False))
    parser.add_argument("--email-relay-url", default=os.getenv("EMAIL_RELAY_URL", ""))
    parser.add_argument("--email-relay-admin-key", default=os.getenv("EMAIL_RELAY_ADMIN_KEY", ""))
    parser.add_argument("--email-relay-from-address", default=os.getenv("EMAIL_RELAY_FROM_ADDRESS", ""))
    parser.add_argument("--email-english-mismatch-alerts", action="store_true", default=env_bool("EMAIL_ENGLISH_MISMATCH_ALERTS", False))
    return parser.parse_args()


def main():
    args = parse_args()
    monitor = Monitor(args)
    monitor.run(args.since)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
