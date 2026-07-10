#!/usr/bin/env python3
import argparse
import calendar
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path


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
        self.state_dir = Path(args.state_dir).resolve()
        self.auto_delete = args.auto_delete_failed_files
        self.auto_delete_min_failures = max(1, args.auto_delete_min_failures)
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
        self.processing_errors = {}
        self.crash_candidates = {}
        self.notifications = {}
        self.restart_cycles = {}
        self.last_transcribe_start = None
        self.recent_container_paths = {}
        self.active_tasks = {}

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.load_state()

    def load_state(self) -> None:
        if not self.state_path.exists():
            return

        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return

        self.processing_errors = {
            item["host_path"].lower(): item
            for item in state.get("processing_errors", [])
            if item.get("host_path")
        }
        self.crash_candidates = {}
        for item in state.get("crash_candidates", []):
            if not item.get("display_name"):
                continue
            key = (
                item.get("candidate_id")
                or item.get("host_path")
                or item.get("container_path")
                or f"legacy:{item['display_name']}"
            ).lower()
            item["candidate_id"] = key
            self.crash_candidates[key] = item
        self.notifications = {
            item["host_path"].lower(): item
            for item in state.get("notifications", [])
            if item.get("host_path")
        }
        self.restart_cycles = {
            item["display_name"].lower(): item
            for item in state.get("restart_cycles", [])
            if item.get("display_name")
        }

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
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def append_event(self, kind: str, message: str) -> None:
        line = f"{utc_stamp()} [{kind}] {message}\n"
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        self.heartbeat_path.write_text(f"{utc_stamp()} {kind}\n", encoding="utf-8")

    def write_summary(self) -> None:
        lines = [
            f"Updated UTC: {utc_stamp()}",
            f"Container: {self.container}",
            f"Media root: {self.media_root}",
            "",
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
        self.summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.save_state()

    def convert_container_path_to_host_path(self, container_path: str) -> str:
        if not container_path or not container_path.startswith("/media/"):
            raise ValueError(f"Unsupported media path: {container_path}")

        relative_path = container_path[len("/media/") :]
        host_path = (self.media_root / relative_path).resolve()
        try:
            host_path.relative_to(self.media_root)
        except ValueError:
            raise ValueError(f"Refusing path outside media root: {host_path}")
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

        now = utc_stamp()
        path_obj = Path(host_path)
        try:
            if path_obj.is_symlink():
                raise ValueError("Refusing to delete a symbolic link")

            resolved_path = path_obj.resolve()
            try:
                resolved_path.relative_to(self.media_root)
            except ValueError as exc:
                raise ValueError(f"Refusing path outside media root: {resolved_path}") from exc

            if not path_obj.exists():
                target["delete_status"] = "missing"
                target["delete_message"] = "Path not found at delete time."
                target["deleted_utc"] = now
                self.append_event(missing_kind, f"{host_path} | missing")
                return

            if not path_obj.is_file():
                raise ValueError("Refusing to delete anything except a regular file")

            path_obj.unlink()

            target["delete_status"] = "deleted"
            target["delete_message"] = "Removed by monitor."
            target["deleted_utc"] = now
            self.append_event(deleted_kind, host_path)
        except (ValueError, IsADirectoryError) as exc:
            target["delete_status"] = "blocked"
            target["delete_message"] = str(exc)
            target["deleted_utc"] = now
            self.append_event("FILE_DELETE_BLOCKED", f"{host_path} | {exc}")
        except Exception as exc:
            target["delete_status"] = "failed"
            target["delete_message"] = str(exc)
            target["deleted_utc"] = now
            self.append_event(failed_kind, f"{host_path} | {exc}")

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
        if previous_host_path and Path(previous_host_path).exists():
            return str(Path(previous_host_path).resolve())
        return None

    def record_processing_error(self, container_path: str) -> None:
        self.remember_container_path(container_path)
        try:
            host_path = self.convert_container_path_to_host_path(container_path)
        except ValueError as exc:
            self.append_event("PROCESSING_ERROR_PATH_BLOCKED", str(exc))
            return
        key = host_path.lower()
        now = utc_stamp()

        if key not in self.processing_errors:
            self.processing_errors[key] = {
                "host_path": host_path,
                "container_path": container_path,
                "first_seen_utc": now,
                "last_seen_utc": now,
                "count": 1,
                "delete_status": None,
                "deleted_utc": None,
                "delete_message": None,
            }
        else:
            self.processing_errors[key]["last_seen_utc"] = now
            self.processing_errors[key]["count"] += 1

        self.append_event("PROCESSING_ERROR", host_path)
        self.try_delete_path(
            host_path,
            self.processing_errors[key],
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

        key = (
            resolved_host_path
            or container_path
            or f"legacy:{display_name}"
        ).lower()

        if key not in self.crash_candidates:
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
            }
        else:
            self.crash_candidates[key]["last_seen_utc"] = now
            self.crash_candidates[key]["count"] += 1

        self.append_event("CRASH_CANDIDATE", display_name)

        preferred_container_path = container_path or self.recent_container_paths.get(display_name.lower())
        existing_host_path = self.crash_candidates[key]["host_path"]
        if preferred_container_path and not resolved_host_path:
            self.remember_container_path(preferred_container_path)
            resolved_host_path = self.convert_container_path_to_host_path(preferred_container_path)
        elif not existing_host_path or not Path(existing_host_path).exists():
            resolved_host_path = self.resolve_crash_candidate_host_path(existing_host_path)

        if resolved_host_path:
            self.crash_candidates[key]["host_path"] = resolved_host_path
            self.crash_candidates[key]["container_path"] = container_path

        if self.crash_candidates[key]["host_path"]:
            self.try_delete_path(
                self.crash_candidates[key]["host_path"],
                self.crash_candidates[key],
                missing_kind="CRASH_FILE_DELETE_SKIPPED",
                deleted_kind="CRASH_FILE_DELETED",
                failed_kind="CRASH_FILE_DELETE_FAILED",
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
        key = host_path.lower()
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
            self.heartbeat_path.write_text(f"{utc_stamp()} reconnect after follow exit\n", encoding="utf-8")


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
