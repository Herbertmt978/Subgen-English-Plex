#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


MEDIA_PATH_ACTIVITY_RE = re.compile(
    r"(?:Detecting language of file: (?P<detect_path>/media/.+) \([^/]*starting at[^/]*\)|Extracting audio from: (?P<extract_path>/media/.+), start_time:)"
)


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Repairer:
    def __init__(self, args, log_lines=None):
        self.container = args.container
        self.media_root = Path(args.media_root).resolve()
        self.state_dir = Path(args.state_dir).resolve()
        self.lookback = args.lookback
        self.min_crash_count = args.min_crash_count
        self.model = args.model
        self.language = args.language
        self.action = args.action
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.monitor_state_path = self.state_dir / "subgen_failed_state.json"
        self.repair_state_path = self.state_dir / "subgen_repair_state.json"
        self.events_path = self.state_dir / "subgen_repair_events.log"
        self.repair_state = self.load_repair_state()
        self.log_lines = log_lines if log_lines is not None else self.load_recent_logs()
        self.logged_paths = self.collect_logged_paths(self.log_lines)

    def load_repair_state(self) -> dict:
        if not self.repair_state_path.exists():
            return {}

        try:
            raw_state = json.loads(self.repair_state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

        return self.extract_repair_entries(raw_state)

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
            "updated_utc": utc_stamp(),
            "container_name": self.container,
            "repairs": self.repair_state,
        }
        self.repair_state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def append_event(self, kind: str, message: str) -> None:
        line = f"{utc_stamp()} [{kind}] {message}\n"
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def load_monitor_state(self) -> list[dict]:
        if not self.monitor_state_path.exists():
            return []

        try:
            state = json.loads(self.monitor_state_path.read_text(encoding="utf-8"))
        except Exception:
            return []

        crash_candidates = state.get("crash_candidates", [])
        if not isinstance(crash_candidates, list):
            return []
        return crash_candidates

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
        host_path = (self.media_root / relative_path).resolve()
        media_root_text = str(self.media_root)
        host_text = str(host_path)
        if host_text != media_root_text and not host_text.startswith(media_root_text + os.sep):
            raise ValueError(f"Refusing path outside media root: {host_path}")
        return host_text

    def resolve_host_path(self, candidate: dict) -> tuple[str | None, str]:
        container_path = candidate.get("container_path")
        if container_path:
            try:
                return self.convert_container_path_to_host_path(container_path), "monitor_container_path"
            except ValueError:
                pass

        state_host_path = candidate.get("host_path")
        if state_host_path:
            return str(Path(state_host_path).resolve()), "monitor_state"

        display_name = candidate["display_name"].lower()
        logged_container_path = self.logged_paths.get(display_name)
        if logged_container_path:
            return self.convert_container_path_to_host_path(logged_container_path), "unique_recent_log"
        return None, "not_found"

    def record_result(self, candidate: dict, status: str, detail: str, host_path: str | None = None, skip_path: Path | None = None) -> None:
        key = str(
            candidate.get("candidate_id")
            or candidate.get("host_path")
            or candidate.get("container_path")
            or candidate["display_name"]
        ).lower()
        self.repair_state[key] = {
            "display_name": candidate["display_name"],
            "updated_utc": utc_stamp(),
            "status": status,
            "detail": detail,
            "crash_count": candidate.get("count", 0),
            "host_path": host_path,
            "skip_path": str(skip_path) if skip_path else None,
        }

    def remove_legacy_empty_markers(self, candidate: dict, media_path: Path) -> None:
        marker_paths = {
            media_path.with_name(f"{media_path.stem}.subgen.{model}.{self.language}.srt")
            for model in {self.model, "large-v3-turbo"}
        }
        for previous in self.repair_state.values():
            if previous.get("display_name") == candidate.get("display_name") and previous.get("skip_path"):
                marker_paths.add(Path(previous["skip_path"]))

        for marker_path in marker_paths:
            try:
                if marker_path.is_symlink():
                    continue
                resolved_marker = marker_path.resolve()
                resolved_marker.relative_to(self.media_root)
                if marker_path.is_file() and marker_path.stat().st_size == 0:
                    marker_path.unlink()
                    self.append_event("LEGACY_EMPTY_MARKER_REMOVED", str(resolved_marker))
            except (OSError, ValueError):
                continue

    def repair_candidate(self, candidate: dict) -> None:
        crash_count = int(candidate.get("count", 0) or 0)
        if crash_count < self.min_crash_count:
            self.record_result(
                candidate,
                status="below_threshold",
                detail=f"crash_count={crash_count} threshold={self.min_crash_count}",
            )
            return

        if candidate.get("delete_status") == "deleted":
            detail = candidate.get("delete_message") or "Removed by monitor."
            self.record_result(
                candidate,
                status="deleted_by_monitor",
                detail=detail,
                host_path=candidate.get("host_path"),
            )
            self.append_event("DELETED_BY_MONITOR", f"{candidate['display_name']} | {candidate.get('host_path')}")
            return

        host_path, source = self.resolve_host_path(candidate)
        if not host_path:
            self.record_result(candidate, status="unresolved", detail=source)
            self.append_event("UNRESOLVED", f"{candidate['display_name']} | {source}")
            return

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
                self.record_result(candidate, status="missing", detail=source, host_path=str(resolved_path))
                self.append_event("MISSING", f"{resolved_path} | source={source}")
                return
            if not path_obj.is_file():
                raise ValueError("Refusing to delete anything except a regular file")

            if self.action == "report":
                self.record_result(
                    candidate,
                    status="eligible",
                    detail=f"{source}; deletion disabled",
                    host_path=str(resolved_path),
                )
                self.append_event(
                    "ELIGIBLE",
                    f"{resolved_path} | source={source} | crashes={crash_count}",
                )
                return

            self.remove_legacy_empty_markers(candidate, path_obj)
            path_obj.unlink()
            self.record_result(candidate, status="deleted", detail=source, host_path=str(resolved_path))
            self.append_event("DELETED", f"{resolved_path} | source={source} | crashes={crash_count}")
        except ValueError as exc:
            self.record_result(candidate, status="blocked", detail=str(exc), host_path=host_path)
            self.append_event("BLOCKED", f"{host_path} | {exc}")
        except Exception as exc:
            self.record_result(candidate, status="failed", detail=str(exc), host_path=host_path)
            self.append_event("FAILED", f"{host_path} | {exc}")

    def run(self) -> int:
        candidates = self.load_monitor_state()
        if not candidates:
            self.append_event("NOOP", "No crash candidates found.")
            self.save_repair_state()
            return 0

        for candidate in sorted(candidates, key=lambda item: int(item.get("count", 0) or 0), reverse=True):
            display_name = candidate.get("display_name")
            if not display_name:
                continue
            self.repair_candidate(candidate)

        self.save_repair_state()
        return 0


def parse_args():
    parser = argparse.ArgumentParser(
        description="Report or remove exact media files that repeatedly crash Subgen."
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
