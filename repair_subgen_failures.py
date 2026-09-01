#!/usr/bin/env python3
import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import warnings
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path, PurePosixPath

from subgen_ops_safety import (
    CandidateUnavailableError,
    UnsafePathError,
    exact_path_key,
    lexical_host_path,
    prepare_private_state_directory,
    validate_regular_file_beneath,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows test environment
    fcntl = None


MEDIA_PATH_ACTIVITY_RE = re.compile(
    r"(?:Detecting language of file: (?P<detect_path>/media/.+) \([^/]*starting at[^/]*\)|Extracting audio from: (?P<extract_path>/media/.+), start_time:)"
)
DEFAULT_REPAIR_EVENT_LOG_MAX_BYTES = 5 * 1024 * 1024
MIN_REPAIR_EVENT_LOG_MAX_BYTES = 256
REPAIR_STATE_VERSION = 2
MAX_PENDING_EVENTS = 128
MAX_PENDING_EVENT_KIND_CHARS = 64
MAX_PENDING_EVENT_MESSAGE_CHARS = 4096
MAX_REPAIR_STATE_BYTES = 4 * 1024 * 1024
MAX_MONITOR_STATE_BYTES = 4 * 1024 * 1024


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def is_private_regular_file(file_stat: os.stat_result) -> bool:
    return (
        stat.S_ISREG(file_stat.st_mode)
        and file_stat.st_nlink == 1
        and (
            not sys.platform.startswith("linux")
            or file_stat.st_uid == os.geteuid()
        )
    )


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


def read_private_text(path: Path, *, maximum_bytes: int) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        path_stat = os.lstat(path)
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or not os.path.samestat(path_stat, file_stat)
            or not is_private_regular_file(file_stat)
            or file_stat.st_size > maximum_bytes
        ):
            raise UnsafePathError(
                "Repair state must be a bounded service-owned regular file with "
                "one link"
            )
        payload = os.read(descriptor, maximum_bytes + 1)
        if len(payload) > maximum_bytes:
            raise UnsafePathError("Repair state exceeds the size limit")
        return payload.decode("utf-8", errors="strict")
    finally:
        os.close(descriptor)


class Repairer:
    def __init__(self, args, log_lines=None):
        self.container = args.container
        self.media_root = Path(args.media_root).resolve()
        self.state_dir = prepare_private_state_directory(args.state_dir)
        self.lookback = args.lookback
        self.min_crash_count = args.min_crash_count
        self.model = args.model
        self.language = args.language
        self.requested_action = args.action
        self.action = "report"
        if self.requested_action == "delete":
            warnings.warn(
                "SUBGEN_REPAIR_ACTION=delete is report-only in v0.5; repair no "
                "longer removes media or legacy subtitle markers.",
                RuntimeWarning,
                stacklevel=2,
            )
        self.event_log_max_bytes = int(
            getattr(args, "event_log_max_bytes", DEFAULT_REPAIR_EVENT_LOG_MAX_BYTES)
        )
        if self.event_log_max_bytes < MIN_REPAIR_EVENT_LOG_MAX_BYTES:
            raise ValueError(
                "event_log_max_bytes must be at least "
                f"{MIN_REPAIR_EVENT_LOG_MAX_BYTES} bytes"
            )
        self.monitor_state_path = self.state_dir / "subgen_failed_state.json"
        self.repair_state_path = self.state_dir / "subgen_repair_state.json"
        self.events_path = self.state_dir / "subgen_repair_events.log"
        self.events_lock_path = self.state_dir / "subgen_repair_events.lock"
        self.run_lock_path = self.state_dir / "subgen_repair_run.lock"
        self.pending_events = []
        self.repair_state_load_safe = True
        self.repair_state = self.load_repair_state()
        self.log_lines = log_lines if log_lines is not None else self.load_recent_logs()
        self.logged_paths = self.collect_logged_paths(self.log_lines)

    def load_repair_state(self) -> dict:
        self.repair_state_load_safe = True
        if not os.path.lexists(self.repair_state_path):
            return {}

        try:
            raw_state = json.loads(
                read_private_text(
                    self.repair_state_path,
                    maximum_bytes=MAX_REPAIR_STATE_BYTES,
                )
            )
            if not isinstance(raw_state, dict):
                raise TypeError("Repair state must be an object")
            self.validate_repair_state_schema(raw_state)
        except Exception as exc:
            self.repair_state_load_safe = False
            warnings.warn(
                "Repair state could not be validated and will not be overwritten "
                f"({type(exc).__name__}).",
                RuntimeWarning,
                stacklevel=2,
            )
            return {}

        self.pending_events = self.extract_pending_events(raw_state)
        return self.extract_repair_entries(raw_state)

    @staticmethod
    def validate_repair_state_schema(raw_state: dict) -> None:
        if "version" in raw_state and (
            type(raw_state["version"]) is not int
            or raw_state["version"] != REPAIR_STATE_VERSION
        ):
            raise ValueError("Repair state version is unsupported")

        if "repairs" in raw_state:
            repairs = raw_state["repairs"]
            if not isinstance(repairs, dict):
                raise ValueError("Repair state repairs collection must be an object")
            for key, value in repairs.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or not isinstance(value, dict)
                    or not isinstance(value.get("display_name"), str)
                    or not value["display_name"]
                ):
                    raise ValueError("Repair state contains an invalid repair entry")

        if "pending_events" in raw_state:
            pending = raw_state["pending_events"]
            if not isinstance(pending, list) or len(pending) > MAX_PENDING_EVENTS:
                raise ValueError("Repair state pending events collection is invalid")
            for event in pending:
                if not isinstance(event, dict):
                    raise TypeError("Repair state contains an invalid pending event")
                kind = event.get("kind")
                message = event.get("message")
                if (
                    not isinstance(kind, str)
                    or not kind
                    or len(kind) > MAX_PENDING_EVENT_KIND_CHARS
                    or not isinstance(message, str)
                    or len(message) > MAX_PENDING_EVENT_MESSAGE_CHARS
                ):
                    raise ValueError("Repair state contains an invalid pending event")

        metadata_fields = {
            "version",
            "updated_utc",
            "container_name",
            "pending_events",
            "repairs",
        }
        for key, value in raw_state.items():
            if key in metadata_fields:
                continue
            if (
                not isinstance(key, str)
                or not key
                or not isinstance(value, dict)
                or not isinstance(value.get("display_name"), str)
                or not value["display_name"]
            ):
                raise ValueError("Repair state contains invalid legacy data")

    def extract_pending_events(self, raw_state: object) -> list[dict]:
        if not isinstance(raw_state, dict):
            return []
        pending = raw_state.get("pending_events")
        if not isinstance(pending, list):
            return []

        validated = []
        for event in pending[-MAX_PENDING_EVENTS:]:
            if not isinstance(event, dict):
                continue
            kind = event.get("kind")
            message = event.get("message")
            if not isinstance(kind, str) or not kind or len(kind) > MAX_PENDING_EVENT_KIND_CHARS:
                continue
            if not isinstance(message, str) or len(message) > MAX_PENDING_EVENT_MESSAGE_CHARS:
                continue
            validated.append(
                {
                    "kind": kind,
                    "message": message,
                    "signature": self.event_signature(kind, message),
                    "queued_utc": event.get("queued_utc") or utc_stamp(),
                }
            )
        return validated

    def extract_repair_entries(self, raw_state: object) -> dict:
        if not isinstance(raw_state, dict):
            return {}

        repairs = {}
        nested_repairs = raw_state.get("repairs")
        if isinstance(nested_repairs, dict):
            repairs.update(self.extract_repair_entries(nested_repairs))

        for key, value in raw_state.items():
            if key == "repairs":
                continue
            if isinstance(value, dict) and value.get("display_name"):
                repairs[key] = value

        return repairs

    def save_repair_state(self) -> None:
        payload = {
            "version": REPAIR_STATE_VERSION,
            "updated_utc": utc_stamp(),
            "container_name": self.container,
            "pending_events": self.pending_events,
            "repairs": self.repair_state,
        }
        atomic_write_text(
            self.repair_state_path,
            json.dumps(payload, indent=2) + "\n",
        )

    def append_event(
        self,
        kind: str,
        message: str,
        event_utc: str | None = None,
    ) -> tuple[bool, str]:
        timestamp = event_utc if self.valid_utc_stamp(event_utc) else utc_stamp()
        line = f"{timestamp} [{kind}] {message}\n"
        encoded_line = line.encode("utf-8")
        if self.event_log_max_bytes <= 0:
            return False, "event_log_limit_disabled"
        if len(encoded_line) > self.event_log_max_bytes:
            return False, "event_exceeds_log_limit"
        try:
            with self.event_log_lock():
                return self._append_event_locked(encoded_line)
        except OSError:
            return False, "event_log_lock_failed"

    def _append_event_locked(self, encoded_line: bytes) -> tuple[bool, str]:
        prepared, reason = self._prepare_event_log(len(encoded_line))
        if not prepared:
            return False, reason

        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(self.events_path, flags, 0o600)
        except OSError:
            return False, "event_log_open_failed"

        try:
            if not is_private_regular_file(os.fstat(file_descriptor)):
                return False, "event_log_not_regular"
            try:
                os.fchmod(file_descriptor, 0o600)
            except (AttributeError, OSError):
                return False, "event_log_permissions_failed"
            with os.fdopen(file_descriptor, "ab", closefd=False) as handle:
                handle.write(encoded_line)
                handle.flush()
                os.fsync(handle.fileno())
            return True, "written"
        except OSError:
            return False, "event_log_write_failed"
        finally:
            os.close(file_descriptor)

    @contextmanager
    def event_log_lock(self):
        if fcntl is None:
            yield
            return

        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.events_lock_path, flags, 0o600)
        try:
            if not is_private_regular_file(os.fstat(descriptor)):
                raise OSError("event log lock is not a regular file")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @contextmanager
    def repair_run_lock(self):
        if fcntl is None:
            yield
            return

        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.run_lock_path, flags, 0o600)
        try:
            if not is_private_regular_file(os.fstat(descriptor)):
                raise OSError("repair run lock is not a regular file")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _prepare_event_log(self, append_size: int) -> tuple[bool, str]:
        try:
            current = os.lstat(self.events_path)
        except FileNotFoundError:
            return True, "new_event_log"
        except OSError:
            return False, "event_log_stat_failed"

        if not is_private_regular_file(current):
            return False, "event_log_not_regular"
        if current.st_size + append_size <= self.event_log_max_bytes:
            return True, "within_limit"

        rotated_path = self.events_path.with_name(f"{self.events_path.name}.1")
        try:
            rotated = os.lstat(rotated_path)
        except FileNotFoundError:
            pass
        except OSError:
            return False, "event_log_backup_stat_failed"
        else:
            if not is_private_regular_file(rotated):
                return False, "event_log_backup_not_regular"

        try:
            os.replace(self.events_path, rotated_path)
        except OSError:
            return False, "event_log_rotation_failed"
        return True, "rotated"

    @staticmethod
    def event_signature(kind: str, message: str) -> str:
        return sha256(f"{kind}\0{message}".encode("utf-8")).hexdigest()

    @staticmethod
    def valid_utc_stamp(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return False
        return True

    def defer_event(self, kind: str, message: str, reason: str) -> None:
        bounded_kind = kind[:MAX_PENDING_EVENT_KIND_CHARS]
        bounded_message = message[:MAX_PENDING_EVENT_MESSAGE_CHARS]
        signature = self.event_signature(bounded_kind, bounded_message)
        if not any(event.get("signature") == signature for event in self.pending_events):
            self.pending_events.append(
                {
                    "kind": bounded_kind,
                    "message": bounded_message,
                    "signature": signature,
                    "queued_utc": utc_stamp(),
                }
            )
            if len(self.pending_events) > MAX_PENDING_EVENTS:
                self.pending_events = self.pending_events[-MAX_PENDING_EVENTS:]
                print(
                    "WARNING: repair pending-event queue reached its limit; "
                    "the oldest entry was dropped.",
                    file=sys.stderr,
                )
        print(
            f"WARNING: repair event delivery deferred ({reason}).",
            file=sys.stderr,
        )
        self.save_repair_state()

    def deliver_event(self, kind: str, message: str) -> bool:
        delivered, reason = self.append_event(kind, message)
        if delivered:
            return True
        self.defer_event(kind, message, reason)
        return False

    def flush_pending_events(self) -> None:
        if not self.pending_events:
            return

        remaining = []
        for index, event in enumerate(self.pending_events):
            delivered, reason = self.append_event(
                event["kind"],
                event["message"],
                event.get("queued_utc"),
            )
            if delivered:
                continue

            if reason == "event_exceeds_log_limit":
                summary = (
                    f"original_signature={event['signature']} "
                    "message omitted because it exceeded the configured limit"
                )
                delivered, reason = self.append_event(
                    "EVENT_OMITTED",
                    summary,
                    event.get("queued_utc"),
                )
                if delivered:
                    continue

            remaining.extend(self.pending_events[index:])
            print(
                f"WARNING: pending repair event delivery deferred ({reason}).",
                file=sys.stderr,
            )
            break
        self.pending_events = remaining
        self.save_repair_state()

    def load_monitor_state(self) -> list[dict]:
        if not os.path.lexists(self.monitor_state_path):
            return []

        try:
            state = json.loads(
                read_private_text(
                    self.monitor_state_path,
                    maximum_bytes=MAX_MONITOR_STATE_BYTES,
                )
            )
        except Exception:
            return []

        crash_candidates = state.get("crash_candidates", [])
        if not isinstance(crash_candidates, list):
            return []
        return [candidate for candidate in crash_candidates if isinstance(candidate, dict)]

    def load_recent_logs(self) -> list[str]:
        command = ["docker", "logs", self.container, "--since", self.lookback]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        log_text = ""
        if result.stdout:
            log_text += result.stdout
        if result.stderr:
            if log_text:
                log_text += "\n"
            log_text += result.stderr
        return [line for line in log_text.splitlines() if line.strip()]

    def collect_logged_paths(self, log_lines: list[str]) -> dict[str, str]:
        candidates = {}
        for line in log_lines:
            match = MEDIA_PATH_ACTIVITY_RE.search(line)
            if not match:
                continue
            container_path = (match.groupdict().get("detect_path") or match.groupdict().get("extract_path")).strip()
            candidates.setdefault(Path(container_path).name.lower(), set()).add(container_path)
        return {
            display_name: next(iter(paths))
            for display_name, paths in candidates.items()
            if len(paths) == 1
        }

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
        except ValueError:
            raise ValueError(f"Refusing path outside media root: {host_path}")
        return str(host_path)

    def resolve_host_path(self, candidate: dict) -> tuple[str | None, str]:
        container_path = candidate.get("container_path")
        if container_path:
            try:
                return self.convert_container_path_to_host_path(container_path), "monitor_container_path"
            except ValueError:
                pass

        state_host_path = candidate.get("host_path")
        if state_host_path:
            return os.fspath(state_host_path), "monitor_state"

        display_name = candidate["display_name"].lower()
        logged_container_path = self.logged_paths.get(display_name)
        if logged_container_path:
            return self.convert_container_path_to_host_path(logged_container_path), "unique_recent_log"
        return None, "not_found"

    def result_key(self, candidate: dict) -> str:
        candidate_id = candidate.get("candidate_id")
        if candidate_id and not any(separator in str(candidate_id) for separator in ("/", "\\")):
            return str(candidate_id)
        if candidate.get("host_path"):
            try:
                return exact_path_key(candidate["host_path"])
            except (TypeError, ValueError, UnsafePathError):
                raw_identity = repr(candidate.get("host_path"))
                return f"unsafe-host:{sha256(raw_identity.encode('utf-8')).hexdigest()}"
        if candidate.get("container_path"):
            return str(PurePosixPath(candidate["container_path"]))
        return str(candidate_id or candidate["display_name"])

    def candidate_evidence_signature(self, candidate: dict) -> str:
        evidence = {
            "candidate_id": candidate.get("candidate_id"),
            "container_path": candidate.get("container_path"),
            "host_path": candidate.get("host_path"),
            "count": int(candidate.get("count", 0) or 0),
            "first_seen_utc": candidate.get("first_seen_utc"),
            "last_seen_utc": candidate.get("last_seen_utc"),
            "failure_identity": candidate.get("failure_identity"),
            "delete_status": candidate.get("delete_status"),
        }
        encoded = json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def record_result(self, candidate: dict, status: str, detail: str, host_path: str | None = None, skip_path: Path | None = None) -> bool:
        key = self.result_key(candidate)
        semantic_result = {
            "status": status,
            "detail": detail,
            "crash_count": int(candidate.get("count", 0) or 0),
            "host_path": str(host_path) if host_path is not None else None,
            "evidence_signature": self.candidate_evidence_signature(candidate),
        }
        previous = self.repair_state.get(key)
        if isinstance(previous, dict) and all(
            previous.get(field) == value for field, value in semantic_result.items()
        ):
            previous["display_name"] = candidate["display_name"]
            if skip_path is not None:
                previous["skip_path"] = str(skip_path)
            return False

        previous_skip_path = previous.get("skip_path") if isinstance(previous, dict) else None
        result = {
            "display_name": candidate["display_name"],
            **semantic_result,
            "skip_path": str(skip_path) if skip_path else previous_skip_path,
        }
        result["updated_utc"] = utc_stamp()
        self.repair_state[key] = result
        return True

    def repair_candidate(self, candidate: dict) -> None:
        key = self.result_key(candidate)
        evidence_signature = self.candidate_evidence_signature(candidate)
        previous = self.repair_state.get(key)
        if isinstance(previous, dict) and previous.get("status") in {
            "blocked_recovery",
            "deleted",
            "deleted_recovered",
            "failed_recovery",
        }:
            previous_signature = previous.get("evidence_signature")
            if previous_signature in {None, evidence_signature}:
                if previous_signature is None:
                    previous["evidence_signature"] = evidence_signature
                    previous["updated_utc"] = utc_stamp()
                    self.save_repair_state()
                return

        crash_count = int(candidate.get("count", 0) or 0)
        if crash_count < self.min_crash_count:
            self.record_result(
                candidate,
                status="below_threshold",
                detail=f"crash_count={crash_count} threshold={self.min_crash_count}",
            )
            return

        if candidate.get("delete_status") in {"deleted", "deleted_recovered"}:
            detail = candidate.get("delete_message") or "Removed by monitor."
            changed = self.record_result(
                candidate,
                status="deleted_by_monitor",
                detail=detail,
                host_path=candidate.get("host_path"),
            )
            if changed:
                self.deliver_event("DELETED_BY_MONITOR", f"{candidate['display_name']} | {candidate.get('host_path')}")
            return

        host_path, source = self.resolve_host_path(candidate)
        if not host_path:
            changed = self.record_result(candidate, status="unresolved", detail=source)
            if changed:
                self.deliver_event("UNRESOLVED", f"{candidate['display_name']} | {source}")
            return

        try:
            path_obj = lexical_host_path(host_path)
            validate_regular_file_beneath(
                self.media_root,
                path_obj,
                expected_identity=candidate.get("failure_identity"),
            )
        except CandidateUnavailableError:
            changed = self.record_result(
                candidate,
                status="missing",
                detail=source,
                host_path=host_path,
            )
            if changed:
                self.deliver_event("MISSING", f"{host_path} | source={source}")
            return
        except UnsafePathError as exc:
            changed = self.record_result(candidate, status="blocked", detail=str(exc), host_path=host_path)
            if changed:
                self.deliver_event("BLOCKED", f"{host_path} | {exc}")
            return
        except Exception as exc:
            changed = self.record_result(candidate, status="failed", detail=str(exc), host_path=host_path)
            if changed:
                self.deliver_event("FAILED", f"{host_path} | {exc}")
            return

        changed = self.record_result(
            candidate,
            status="eligible",
            detail=f"{source}; repair is report-only",
            host_path=str(path_obj),
        )
        if changed:
            self.deliver_event(
                "ELIGIBLE",
                f"{path_obj} | source={source} | crashes={crash_count}",
            )

    def recover_delete_intents(self) -> set[str]:
        blocked_events = []
        recovered_keys = set()
        changed = False
        for key, result in self.repair_state.items():
            status = result.get("status")
            if status in {"blocked_recovery", "failed_recovery"}:
                recovered_keys.add(key)
                continue
            if status not in {"deleting", "delete_paused"}:
                continue
            recovered_keys.add(key)
            changed = True
            host_path = result.get("host_path")
            result["status"] = "blocked_recovery"
            result["detail"] = (
                "Legacy repair delete intent is policy-blocked in v0.5; only the "
                "live monitor may delete typed invalid media."
            )
            result["updated_utc"] = utc_stamp()
            blocked_events.append(
                (
                    "DELETE_RECOVERY_BLOCKED",
                    f"{host_path or '<missing>'} | repair deletion retired",
                )
            )

        if not changed:
            return recovered_keys
        self.save_repair_state()
        for event_kind, event_message in blocked_events:
            self.deliver_event(event_kind, event_message)
        return recovered_keys

    def run(self) -> int:
        with self.repair_run_lock():
            return self.run_locked()

    def run_locked(self) -> int:
        # A second timer invocation may have constructed this object before it
        # acquired the process lock. Reload only after the lock is held so it
        # cannot overwrite a completed run with stale in-memory state.
        self.pending_events = []
        self.repair_state = self.load_repair_state()
        if not self.repair_state_load_safe:
            print(
                "ERROR: repair state is unsafe or malformed; refusing to replace it.",
                file=sys.stderr,
            )
            return 1
        recovered_keys = self.recover_delete_intents()
        self.flush_pending_events()
        candidates = self.load_monitor_state()
        if not candidates:
            self.deliver_event("NOOP", "No crash candidates found.")
            self.save_repair_state()
            return 0

        for candidate in sorted(candidates, key=lambda item: int(item.get("count", 0) or 0), reverse=True):
            display_name = candidate.get("display_name")
            if not display_name:
                continue
            if self.result_key(candidate) in recovered_keys:
                continue
            self.repair_candidate(candidate)

        self.save_repair_state()
        return 0


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Report exact media files associated with repeated Subgen failures; "
            "repair-side deletion is retired in v0.5."
        )
    )
    parser.add_argument("--container", default=os.getenv("SUBGEN_CONTAINER", "subgen"))
    parser.add_argument("--media-root", default=os.getenv("MEDIA_ROOT", "/srv/media"))
    parser.add_argument(
        "--state-dir",
        default=os.getenv("SUBGEN_STATE_DIR", "/opt/subgen/monitor"),
    )
    parser.add_argument(
        "--lookback",
        default=os.getenv("SUBGEN_REPAIR_LOOKBACK", "7d"),
    )
    parser.add_argument(
        "--min-crash-count",
        type=int,
        default=int(os.getenv("SUBGEN_REPAIR_MIN_CRASH_COUNT", "3")),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("SUBGEN_REPAIR_MODEL", "large-v3"),
    )
    parser.add_argument(
        "--language",
        default=os.getenv("SUBGEN_REPAIR_LANGUAGE", "en"),
    )
    parser.add_argument(
        "--action",
        choices=("report", "delete"),
        default=os.getenv("SUBGEN_REPAIR_ACTION", "report"),
        help=(
            "Compatibility option; 'delete' is deprecated and behaves as "
            "report-only in v0.5."
        ),
    )
    parser.add_argument(
        "--event-log-max-bytes",
        type=int,
        default=int(
            os.getenv(
                "SUBGEN_REPAIR_EVENT_LOG_MAX_BYTES",
                str(DEFAULT_REPAIR_EVENT_LOG_MAX_BYTES),
            )
        ),
        help=(
            "Rotate the repair event log before it exceeds this many bytes "
            f"(minimum {MIN_REPAIR_EVENT_LOG_MAX_BYTES})."
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()
    repairer = Repairer(args)
    return repairer.run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
