"""Versioned exact-generation failure markers shared by Subgen and its monitor."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple

from subgen_ops_safety import FileIdentity, file_identity


MARKER_SCHEMA_VERSION = 1
DEFAULT_MARKER_REGISTRY_PATH = "/opt/subgen/monitor/subgen_failure_markers.json"
MAX_MARKER_REGISTRY_BYTES = 8 * 1024 * 1024
MAX_MARKER_ENTRIES = 10_000
SUPPORTED_FAILURE_KINDS = frozenset({"processing_error", "sigsegv"})

_ENTRY_KEYS = frozenset(
    {
        "container_path",
        "file_identity",
        "failure_kind",
        "failure_count",
        "created_utc",
        "updated_utc",
    }
)
_DOCUMENT_KEYS = frozenset({"schema_version", "updated_utc", "markers"})
_UTC_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class MarkerRegistryError(ValueError):
    """Raised when marker state does not satisfy the shared contract."""


class MarkerCheck(NamedTuple):
    """One fail-open marker decision for a candidate media generation."""

    status: Literal["matched", "stale", "unmarked", "unavailable"]
    detail: str
    report: bool


def canonical_container_path(value: str) -> str:
    """Return a canonical, case-preserving path strictly beneath ``/media``."""

    if not isinstance(value, str) or not value or "\0" in value:
        raise MarkerRegistryError("Marker container path must be non-empty text")
    path = PurePosixPath(value)
    if not path.is_absolute() or len(path.parts) < 3 or path.parts[:2] != ("/", "media"):
        raise MarkerRegistryError("Marker container path must be strictly beneath /media")
    if ".." in path.parts or str(path) != value:
        raise MarkerRegistryError("Marker container path must be canonical")
    return value


def normalize_file_identity(
    value: Mapping[str, int] | Sequence[int],
) -> FileIdentity:
    """Validate and normalize a five-field JSON file identity."""

    try:
        if isinstance(value, Mapping):
            fields = (
                value["device"],
                value["inode"],
                value["size"],
                value["mtime_ns"],
                value["ctime_ns"],
            )
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) != 5:
                raise ValueError
            fields = tuple(value)
        else:
            raise TypeError
        if any(isinstance(field, bool) or not isinstance(field, int) or field < 0 for field in fields):
            raise ValueError
        return FileIdentity(*fields)
    except (KeyError, TypeError, ValueError) as exc:
        raise MarkerRegistryError("Marker file identity must contain five non-negative integers") from exc


def _validated_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _UTC_STAMP_RE.fullmatch(value):
        raise MarkerRegistryError(f"Marker {field_name} must be a UTC timestamp")
    return value


def build_marker_entry(
    container_path: str,
    identity: Mapping[str, int] | Sequence[int],
    failure_kind: str,
    failure_count: int,
    timestamp: str,
    created_utc: str | None = None,
) -> dict:
    """Build one validated, JSON-serializable marker entry."""

    path = canonical_container_path(container_path)
    normalized_identity = normalize_file_identity(identity)
    if failure_kind not in SUPPORTED_FAILURE_KINDS:
        raise MarkerRegistryError("Marker failure kind is unsupported")
    if (
        isinstance(failure_count, bool)
        or not isinstance(failure_count, int)
        or failure_count < 1
    ):
        raise MarkerRegistryError("Marker failure count must be a positive integer")
    updated = _validated_timestamp(timestamp, "updated_utc")
    created = _validated_timestamp(created_utc or timestamp, "created_utc")
    return {
        "container_path": path,
        "file_identity": list(normalized_identity),
        "failure_kind": failure_kind,
        "failure_count": failure_count,
        "created_utc": created,
        "updated_utc": updated,
    }


def _validated_entry(value: object) -> dict:
    if not isinstance(value, Mapping) or frozenset(value) != _ENTRY_KEYS:
        raise MarkerRegistryError("Marker entry fields do not match schema version 1")
    return build_marker_entry(
        value["container_path"],
        value["file_identity"],
        value["failure_kind"],
        value["failure_count"],
        value["updated_utc"],
        value["created_utc"],
    )


def _validated_document(value: object) -> dict:
    if not isinstance(value, Mapping) or frozenset(value) != _DOCUMENT_KEYS:
        raise MarkerRegistryError("Marker document fields do not match schema version 1")
    schema_version = value["schema_version"]
    if isinstance(schema_version, bool) or schema_version != MARKER_SCHEMA_VERSION:
        raise MarkerRegistryError("Marker schema version is unsupported")
    updated_utc = _validated_timestamp(value["updated_utc"], "document updated_utc")
    markers = value["markers"]
    if not isinstance(markers, list) or len(markers) > MAX_MARKER_ENTRIES:
        raise MarkerRegistryError("Marker list is invalid or exceeds the entry limit")

    validated = []
    seen_paths = set()
    for marker in markers:
        entry = _validated_entry(marker)
        if entry["container_path"] in seen_paths:
            raise MarkerRegistryError("Marker document contains duplicate container paths")
        seen_paths.add(entry["container_path"])
        validated.append(entry)
    validated.sort(key=lambda entry: entry["container_path"])
    return {
        "schema_version": MARKER_SCHEMA_VERSION,
        "updated_utc": updated_utc,
        "markers": validated,
    }


def encode_marker_document(
    entries: Iterable[Mapping[str, object]],
    updated_utc: str,
) -> str:
    """Serialize a deterministic bounded marker document."""

    document = _validated_document(
        {
            "schema_version": MARKER_SCHEMA_VERSION,
            "updated_utc": updated_utc,
            "markers": list(entries),
        }
    )
    encoded = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if len(encoded.encode("utf-8")) > MAX_MARKER_REGISTRY_BYTES:
        raise MarkerRegistryError("Marker document exceeds the byte limit")
    return encoded


def _stat_signature(value: os.stat_result) -> tuple[int, ...]:
    signature = (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_mode),
        int(value.st_nlink),
    )
    if os.name == "nt":
        # NTFS ctime can be lazily normalized between path and descriptor
        # queries even when the file has not changed.
        return signature
    return signature + (int(value.st_ctime_ns),)


def _same_opened_snapshot(before: os.stat_result, opened: os.stat_result) -> bool:
    """Compare path and descriptor metadata without false Windows mismatches."""

    stable_before = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_size),
        int(before.st_mtime_ns),
        int(before.st_mode),
        int(before.st_nlink),
    )
    stable_opened = (
        int(opened.st_dev),
        int(opened.st_ino),
        int(opened.st_size),
        int(opened.st_mtime_ns),
        int(opened.st_mode),
        int(opened.st_nlink),
    )
    if stable_before != stable_opened:
        return False
    if os.name == "nt":
        return True
    return int(before.st_ctime_ns) == int(opened.st_ctime_ns)


def load_marker_document(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = MAX_MARKER_REGISTRY_BYTES,
) -> dict:
    """Securely read and validate one complete marker document."""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise MarkerRegistryError("Marker byte limit must be a positive integer")
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise MarkerRegistryError("Marker registry could not be inspected") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise MarkerRegistryError("Marker registry must be a single-link regular file")
    if before.st_size > max_bytes:
        raise MarkerRegistryError("Marker registry exceeds the byte limit")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MarkerRegistryError("Marker registry could not be opened safely") from exc

    try:
        opened = os.fstat(descriptor)
        if not _same_opened_snapshot(before, opened):
            raise MarkerRegistryError("Marker registry changed before it was opened")
        chunks = []
        bytes_read = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - bytes_read))
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)
            if bytes_read > max_bytes:
                raise MarkerRegistryError("Marker registry exceeds the byte limit")
        after = os.fstat(descriptor)
        if _stat_signature(after) != _stat_signature(opened):
            raise MarkerRegistryError("Marker registry changed while it was read")
    finally:
        os.close(descriptor)

    try:
        payload = b"".join(chunks).decode("utf-8")
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarkerRegistryError("Marker registry is not valid UTF-8 JSON") from exc
    return _validated_document(document)


class FailureMarkerReader:
    """Cache and match an exact generation while failing open on bad state."""

    def __init__(
        self,
        registry_path: str | os.PathLike[str] = DEFAULT_MARKER_REGISTRY_PATH,
        *,
        media_root: str | os.PathLike[str] = "/media",
        max_bytes: int = MAX_MARKER_REGISTRY_BYTES,
    ):
        self.registry_path = Path(registry_path)
        self.media_root = Path(os.path.abspath(os.path.normpath(os.fspath(media_root))))
        self.max_bytes = max_bytes
        self._cache_signature: tuple[object, ...] | None = None
        self._markers: dict[str, dict] = {}
        self._cache_error = ""
        self._reported: set[tuple[object, ...]] = set()
        self._lock = threading.Lock()

    def _current_registry_signature(self) -> tuple[object, ...]:
        try:
            return ("present",) + _stat_signature(os.lstat(self.registry_path))
        except FileNotFoundError:
            return ("missing",)
        except OSError as exc:
            return ("error", type(exc).__name__, getattr(exc, "errno", None))

    def _refresh(self) -> None:
        signature = self._current_registry_signature()
        if signature == self._cache_signature:
            return

        self._markers = {}
        self._cache_error = ""
        self._reported.clear()
        if signature[0] == "missing":
            self._cache_signature = signature
            return
        for attempt in range(2):
            try:
                document = load_marker_document(
                    self.registry_path,
                    max_bytes=self.max_bytes,
                )
            except FileNotFoundError:
                self._cache_signature = ("missing",)
                return
            except Exception as exc:
                self._cache_signature = signature
                self._cache_error = f"{type(exc).__name__}: {exc}"[:300]
                return

            after_signature = self._current_registry_signature()
            if after_signature == signature:
                self._markers = {
                    marker["container_path"]: marker for marker in document["markers"]
                }
                self._cache_signature = signature
                return
            signature = after_signature
            if signature[0] == "missing":
                self._cache_signature = signature
                return
            if attempt == 1:
                break

        self._cache_signature = signature
        self._cache_error = "MarkerRegistryError: marker registry changed while reloading"

    def _container_path(self, file_path: str | os.PathLike[str]) -> tuple[str, Path] | None:
        try:
            raw_path = os.fspath(file_path)
        except TypeError:
            return None
        if not isinstance(raw_path, str) or "\0" in raw_path or ".." in Path(raw_path).parts:
            return None
        candidate = Path(os.path.abspath(os.path.normpath(raw_path)))
        try:
            relative = candidate.relative_to(self.media_root)
        except ValueError:
            return None
        if not relative.parts:
            return None
        container_path = str(PurePosixPath("/media", *relative.parts))
        try:
            return canonical_container_path(container_path), candidate
        except MarkerRegistryError:
            return None

    def _reported_decision(
        self,
        status: Literal["matched", "stale", "unavailable"],
        detail: str,
        report_key: tuple[object, ...],
    ) -> MarkerCheck:
        report = report_key not in self._reported
        self._reported.add(report_key)
        return MarkerCheck(status, detail, report)

    def check(self, file_path: str | os.PathLike[str]) -> MarkerCheck:
        """Return whether ``file_path`` is the exact marked file generation."""

        with self._lock:
            candidate = self._container_path(file_path)
            if candidate is None:
                return MarkerCheck("unmarked", "candidate is outside the media root", False)
            container_path, candidate_path = candidate
            self._refresh()
            if self._cache_error:
                return self._reported_decision(
                    "unavailable",
                    self._cache_error,
                    ("unavailable", container_path, self._cache_error),
                )

            marker = self._markers.get(container_path)
            if marker is None:
                return MarkerCheck("unmarked", "no marker for this path", False)
            try:
                candidate_stat = os.lstat(candidate_path)
            except OSError:
                return MarkerCheck("unmarked", "candidate is unavailable", False)
            if not stat.S_ISREG(candidate_stat.st_mode):
                return MarkerCheck("unmarked", "candidate is not a regular file", False)

            current_identity = file_identity(candidate_stat)
            marked_identity = normalize_file_identity(marker["file_identity"])
            if current_identity == marked_identity:
                return self._reported_decision(
                    "matched",
                    "exact file generation is marked",
                    ("matched", container_path, tuple(current_identity)),
                )
            return self._reported_decision(
                "stale",
                "marker identity differs from the current file generation",
                ("stale", container_path, tuple(current_identity)),
            )


__all__ = [
    "DEFAULT_MARKER_REGISTRY_PATH",
    "FailureMarkerReader",
    "MARKER_SCHEMA_VERSION",
    "MAX_MARKER_ENTRIES",
    "MAX_MARKER_REGISTRY_BYTES",
    "MarkerCheck",
    "MarkerRegistryError",
    "SUPPORTED_FAILURE_KINDS",
    "build_marker_entry",
    "canonical_container_path",
    "encode_marker_document",
    "load_marker_document",
    "normalize_file_identity",
]
