#!/usr/bin/env python3
"""Owner-operated composite workload observer for the Task 11B runtime gate.

This host-side tool is deliberately outside the Subgen image.  It imports the
frozen health sampler for immutable Docker binding, telemetry, log scanning,
evidence publication, and exact-ID cleanup.  It adds only the workload protocol
needed to prove the automatic runtime: a 31-minute atomic subtitle, a resident
short batch, the idle unload/recovery cycle, a post-unload reload, and retained
invalid/silent controls.

The fixture manifest contains disposable relative paths, but no path, token,
camera identifier, endpoint, or raw log line is written to evidence.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
import hmac
import http.client
import json
import os
import posixpath
import re
import shlex
import stat
import struct
import sys
import threading
import time
import types
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable


# The adjacent sampler is intentionally not imported here.  The CLI reads and
# hashes its exact bytes independently, then executes only those verified bytes.
# Focused unit tests inject the adjacent module after importing this observer.
health: Any = None


FIXTURE_SCHEMA = "subgen.task11b.runtime-fixtures/v1"
OBSERVER_SCHEMA = "subgen.task11b.runtime-observer/v1"
MAX_FIXTURE_MANIFEST_BYTES = 32 * 1024
MAX_API_KEY_BYTES = 4 * 1024
MIB = 1024**2
GIB = 1024**3
MAX_MEDIA_BYTES = 16 * GIB
MAX_SUBTITLE_BYTES = 64 * MIB
MAX_HTTP_RESPONSE_BYTES = 64 * 1024
MAX_EVENT_LOG_BYTES = 512 * 1024
MAX_EVENT_IDENTITIES = 8192
MAX_RETAINED_EVENT_BYTES = 256 * 1024
MAX_OBSERVER_EVIDENCE_BYTES = 1536 * 1024
MAX_SAMPLER_SOURCE_BYTES = 4 * MIB
LONG_MINIMUM_SECONDS = 31 * 60
LONG_MAXIMUM_SECONDS = 32 * 60
SHORT_MAXIMUM_SECONDS = 10 * 60
MIN_RECOVERY_SPAN_SECONDS = 10.0
SRT_MINIMUM_TIMELINE_COVERAGE = 0.80
EXPECTED_FIXTURE_DIRECTORIES = {"long", "short", "reload", "invalid", "silent"}
SRT_TIMING_RE = re.compile(
    r"^(?P<sh>\d{2,}):(?P<sm>\d{2}):(?P<ss>\d{2}),(?P<sms>\d{3})"
    r" --> "
    r"(?P<eh>\d{2,}):(?P<em>\d{2}):(?P<es>\d{2}),(?P<ems>\d{3})$"
)
SUBGEN_EVENT_PREFIX = "SUBGEN_EVENT "
SILENT_EVENT_RE = re.compile(
    r"MEDIA_VALIDATION outcome=no_audio ffprobe=no_audio pyav=no_audio "
    r"path=(?P<path>/media/(?:long|short|reload|invalid|silent)/[^\s]+)$"
)
SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
DEFAULT_FRIGATE_STATS_URL = "http://127.0.0.1:5000/api/stats"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/ps"
DEFAULT_CANDIDATE_STATUS_URL = "http://127.0.0.1:19000/status"


class ObserverBootstrapAbort(RuntimeError):
    """A fail-closed error available before the verified sampler is loaded."""

    def __init__(self, message: str) -> None:
        code = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")[:96]
        self.code = code or "observer_bootstrap_abort"
        super().__init__(self.code)


@dataclass(frozen=True)
class FixtureItem:
    role: str
    index: int
    media_relative: str
    subtitle_relative: str | None

    @property
    def container_media(self) -> str:
        return "/media/" + self.media_relative

    @property
    def container_directory(self) -> str:
        return "/media/" + self.media_relative.split("/", 1)[0]


@dataclass(frozen=True)
class FileSnapshot:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    links: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str
    subtitle_cue_count: int | None = None
    subtitle_first_start_ms: int | None = None
    subtitle_last_end_ms: int | None = None


@dataclass(frozen=True)
class FixtureSet:
    manifest_sha256: str
    media_root: Path
    runtime_uid: int
    runtime_gid: int
    long: FixtureItem
    short: tuple[FixtureItem, ...]
    reload: FixtureItem
    invalid: FixtureItem
    silent: FixtureItem

    @property
    def all_items(self) -> tuple[FixtureItem, ...]:
        return (self.long, *self.short, self.reload, self.invalid, self.silent)


@dataclass(frozen=True)
class RuntimeStatusObservation:
    sequence: int
    observed_monotonic: float
    state: str
    recovery_reason: str | None
    admission_open: bool


@dataclass(frozen=True)
class RuntimeRecoveryProof:
    recovering_sequence: int
    normal_sequence: int
    complete_health_polls: int
    elapsed_seconds: float


@dataclass(frozen=True)
class HealthBaselines:
    candidate_restart_count: int
    frigate_restart_count: int


@dataclass(frozen=True)
class PhaseFreshnessMark:
    event_sequence: int
    publication_sequence: int


_BOOTSTRAPPED_OBSERVER_SHA256: str | None = None
_BOOTSTRAPPED_SAMPLER_SHA256: str | None = None
_BOOTSTRAPPED_OBSERVER_PAYLOAD: bytes | None = None
_BOOTSTRAPPED_SAMPLER_PAYLOAD: bytes | None = None


def _owner_id() -> int | None:
    getter = getattr(os, "geteuid", None)
    return getter() if callable(getter) else None


def _safe_code(message: str) -> BaseException:
    if health is not None and hasattr(health, "GateAbort"):
        return health.GateAbort(message)
    return ObserverBootstrapAbort(message)


def _read_source_bytes_independently(
    path: Path, *, maximum: int, label: str
) -> tuple[bytes, str]:
    """Read one exact regular file without invoking sampler-owned helpers."""
    if not path.is_absolute():
        raise ObserverBootstrapAbort(f"{label} path was not absolute")
    try:
        parent = path.parent.resolve(strict=True)
        parent_lstat = path.parent.lstat()
        path_lstat = path.lstat()
    except OSError as exc:
        raise ObserverBootstrapAbort(f"{label} was unavailable") from exc
    if (
        parent != path.parent.absolute()
        or stat.S_ISLNK(parent_lstat.st_mode)
        or stat.S_ISLNK(path_lstat.st_mode)
        or not stat.S_ISREG(path_lstat.st_mode)
    ):
        raise ObserverBootstrapAbort(f"{label} path was unsafe")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ObserverBootstrapAbort(f"{label} could not be opened") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum
        ):
            raise ObserverBootstrapAbort(f"{label} size or type was unsafe")
        remaining = maximum + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(MIB, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(payload) > maximum
            or len(payload) != before.st_size
            or any(
                getattr(after, field) != getattr(before, field)
                for field in identity_fields
            )
        ):
            raise ObserverBootstrapAbort(f"{label} changed while it was read")
        return payload, hashlib.sha256(payload).hexdigest()
    finally:
        os.close(descriptor)


def _bootstrap_verified_runtime(argv: list[str]) -> None:
    """Verify sampler bytes independently before executing the frozen module."""
    global health
    global _BOOTSTRAPPED_OBSERVER_SHA256
    global _BOOTSTRAPPED_SAMPLER_SHA256
    global _BOOTSTRAPPED_OBSERVER_PAYLOAD
    global _BOOTSTRAPPED_SAMPLER_PAYLOAD

    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--sampler-sha256")
    bootstrap.add_argument("--observer-sha256")
    preliminary, _unknown = bootstrap.parse_known_args(argv)
    if not isinstance(preliminary.sampler_sha256, str) or not SHA256_RE.fullmatch(
        preliminary.sampler_sha256
    ):
        raise ObserverBootstrapAbort("sampler checksum must be SHA256")
    if not isinstance(preliminary.observer_sha256, str) or not SHA256_RE.fullmatch(
        preliminary.observer_sha256
    ):
        raise ObserverBootstrapAbort("observer checksum must be SHA256")

    observer_path = Path(__file__).resolve(strict=True)
    observer_payload, observer_digest = _read_source_bytes_independently(
        observer_path,
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="runtime observer",
    )
    if observer_digest != preliminary.observer_sha256.lower():
        raise ObserverBootstrapAbort("runtime observer checksum mismatch")

    sampler_path = observer_path.with_name("gate_health_sampler.py")
    sampler_payload, sampler_digest = _read_source_bytes_independently(
        sampler_path,
        maximum=MAX_SAMPLER_SOURCE_BYTES,
        label="frozen health sampler",
    )
    if sampler_digest != preliminary.sampler_sha256.lower():
        raise ObserverBootstrapAbort("frozen health sampler checksum mismatch")

    module_name = "_task11b_verified_gate_health_sampler"
    module = types.ModuleType(module_name)
    module.__file__ = str(sampler_path)
    module.__package__ = ""
    module.__loader__ = None
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        code = compile(sampler_payload, str(sampler_path), "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except BaseException:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    health = module
    _BOOTSTRAPPED_OBSERVER_SHA256 = observer_digest
    _BOOTSTRAPPED_SAMPLER_SHA256 = sampler_digest
    _BOOTSTRAPPED_OBSERVER_PAYLOAD = observer_payload
    _BOOTSTRAPPED_SAMPLER_PAYLOAD = sampler_payload


def _verified_runtime_identities(args: argparse.Namespace) -> tuple[str, str]:
    observer_digest = _BOOTSTRAPPED_OBSERVER_SHA256
    sampler_digest = _BOOTSTRAPPED_SAMPLER_SHA256
    if (
        observer_digest is None
        or sampler_digest is None
        or observer_digest != args.observer_sha256.lower()
        or sampler_digest != args.sampler_sha256.lower()
    ):
        raise _safe_code("runtime observer was not checksum bootstrapped")
    return observer_digest, sampler_digest


def _verify_adjacent_frozen_sampler() -> None:
    expected = Path(__file__).with_name("gate_health_sampler.py").resolve(strict=True)
    actual = Path(health.__file__).resolve(strict=True)
    if actual != expected:
        raise _safe_code("runtime observer imported a non-adjacent health sampler")


def _read_all_fd(fd: int, maximum: int, *, label: str) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > maximum:
        raise _safe_code(f"{label} exceeded byte limit")
    return payload


def _require_private_file(path: Path, *, maximum: int, label: str) -> bytes:
    if not path.is_absolute():
        raise _safe_code(f"{label} path must be absolute")
    try:
        path_lstat = path.lstat()
        parent = path.parent.resolve(strict=True)
        parent_lstat = path.parent.lstat()
    except OSError as exc:
        raise _safe_code(f"{label} parent was unavailable") from exc
    if (
        parent != path.parent.absolute()
        or stat.S_ISLNK(parent_lstat.st_mode)
        or stat.S_ISLNK(path_lstat.st_mode)
    ):
        raise _safe_code(f"{label} parent used a symlink")
    owner = _owner_id()
    if owner is not None and (
        parent_lstat.st_uid != owner or parent_lstat.st_mode & 0o077
    ):
        raise _safe_code(f"{label} parent was not owner only")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _safe_code(f"{label} could not be opened") from exc
    try:
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or (owner is not None and (item.st_uid != owner or item.st_mode & 0o077))
            or item.st_nlink != 1
            or item.st_size > maximum
        ):
            raise _safe_code(f"{label} was not a private bounded regular file")
        return _read_all_fd(fd, maximum, label=label)
    finally:
        os.close(fd)


def _parse_runtime_identity(boundary: health.BoundaryExpectation) -> tuple[int, int]:
    user = boundary.document.get("user")
    if not isinstance(user, str) or not re.fullmatch(r"[1-9]\d*:[1-9]\d*", user):
        raise _safe_code("runtime fixture owner identity was unavailable")
    uid_text, gid_text = user.split(":", 1)
    uid, gid = int(uid_text), int(gid_text)
    if (uid, gid) != (1000, 1000):
        raise _safe_code("runtime fixture owner identity changed")
    return uid, gid


def _media_mount_source(boundary: health.BoundaryExpectation) -> Path:
    mounts = boundary.document.get("mounts")
    if not isinstance(mounts, list):
        raise _safe_code("runtime media mount was unavailable")
    matches = [
        item
        for item in mounts
        if isinstance(item, dict)
        and item.get("destination") == "/media"
        and item.get("read_write") is True
        and item.get("mode") == "rw"
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("source"), str):
        raise _safe_code("runtime media mount was not exact")
    return Path(matches[0]["source"])


def _validate_relative_path(value: Any, *, directory: str, subtitle: bool) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise _safe_code("fixture relative path was malformed")
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != directory:
        raise _safe_code("fixture relative path left its dedicated directory")
    if any(part in {"", ".", ".."} or len(part) > 255 for part in path.parts):
        raise _safe_code("fixture relative path was unsafe")
    normalized = posixpath.normpath(value)
    if normalized != value:
        raise _safe_code("fixture relative path was not normalized")
    if subtitle:
        if path.suffix.casefold() != ".srt":
            raise _safe_code("fixture subtitle path was not SRT")
    elif path.suffix.casefold() not in {".mkv", ".mp4", ".mov", ".m4v", ".webm"}:
        raise _safe_code("fixture media extension was not allowlisted")
    return value


def _fixture_item(raw: Any, *, role: str, directory: str, index: int) -> FixtureItem:
    expected = {"media"} if role in {"invalid", "silent"} else {"media", "subtitle"}
    if not isinstance(raw, dict) or set(raw) != expected:
        raise _safe_code("fixture item shape was not exact")
    media = _validate_relative_path(
        raw.get("media"), directory=directory, subtitle=False
    )
    subtitle_value = raw.get("subtitle")
    subtitle = (
        _validate_relative_path(subtitle_value, directory=directory, subtitle=True)
        if subtitle_value is not None
        else None
    )
    if subtitle == media:
        raise _safe_code("fixture media and subtitle paths collided")
    return FixtureItem(role, index, media, subtitle)


def load_fixture_manifest(
    path: Path,
    boundary: health.BoundaryExpectation,
) -> FixtureSet:
    payload = _require_private_file(
        path, maximum=MAX_FIXTURE_MANIFEST_BYTES, label="fixture manifest"
    )
    document = health.strict_json_object(
        payload, label="fixture manifest", max_bytes=MAX_FIXTURE_MANIFEST_BYTES
    )
    expected_keys = {"schema", "long", "short_resident", "reload", "invalid", "silent"}
    if set(document) != expected_keys or document.get("schema") != FIXTURE_SCHEMA:
        raise _safe_code("fixture manifest schema or keys were not exact")
    short_raw = document.get("short_resident")
    if not isinstance(short_raw, list) or not 2 <= len(short_raw) <= 8:
        raise _safe_code("resident short batch size was outside boundary")
    long_item = _fixture_item(document["long"], role="long", directory="long", index=0)
    short_items = tuple(
        _fixture_item(item, role="short", directory="short", index=index)
        for index, item in enumerate(short_raw)
    )
    reload_item = _fixture_item(
        document["reload"], role="reload", directory="reload", index=0
    )
    invalid_item = _fixture_item(
        document["invalid"], role="invalid", directory="invalid", index=0
    )
    silent_item = _fixture_item(
        document["silent"], role="silent", directory="silent", index=0
    )
    all_items = (long_item, *short_items, reload_item, invalid_item, silent_item)
    all_paths: list[str] = []
    for item in all_items:
        all_paths.append(item.media_relative)
        if item.subtitle_relative is not None:
            all_paths.append(item.subtitle_relative)
    if len(all_paths) != len(set(all_paths)):
        raise _safe_code("fixture manifest contained duplicate paths")
    media_root = _media_mount_source(boundary)
    uid, gid = _parse_runtime_identity(boundary)
    return FixtureSet(
        manifest_sha256=health.sha256_bytes(payload),
        media_root=media_root,
        runtime_uid=uid,
        runtime_gid=gid,
        long=long_item,
        short=short_items,
        reload=reload_item,
        invalid=invalid_item,
        silent=silent_item,
    )


def _open_fixture_directory(
    root: Path,
    directory: str,
    *,
    expected_uid: int,
    expected_gid: int,
) -> int | Path:
    if directory not in EXPECTED_FIXTURE_DIRECTORIES:
        raise _safe_code("fixture directory role was invalid")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        root_lstat = root.lstat()
        child_lstat = (root / directory).lstat()
    except OSError as exc:
        raise _safe_code("fixture directory was unavailable") from exc
    if stat.S_ISLNK(root_lstat.st_mode) or stat.S_ISLNK(child_lstat.st_mode):
        raise _safe_code("fixture directory used a symlink")
    owner_enforced = _owner_id() is not None
    if (
        not stat.S_ISDIR(child_lstat.st_mode)
        or (
            owner_enforced
            and (child_lstat.st_uid, child_lstat.st_gid) != (expected_uid, expected_gid)
        )
        or (owner_enforced and child_lstat.st_mode & 0o077)
    ):
        raise _safe_code("fixture directory ownership or mode was unsafe")
    if os.open not in os.supports_dir_fd:
        # The observer is Linux-only, but keeping the pure validation surface
        # portable lets its security regressions run on the local Windows test
        # workstation. Linux always takes the openat/O_NOFOLLOW branch below.
        return root / directory
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise _safe_code("fixture media root could not be opened") from exc
    try:
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode) or (
            root_stat.st_dev,
            root_stat.st_ino,
        ) != (root_lstat.st_dev, root_lstat.st_ino):
            raise _safe_code("fixture media root was not a directory")
        child_fd = os.open(directory, flags, dir_fd=root_fd)
    except BaseException:
        os.close(root_fd)
        raise
    os.close(root_fd)
    item = os.fstat(child_fd)
    if (
        not stat.S_ISDIR(item.st_mode)
        or (item.st_dev, item.st_ino) != (child_lstat.st_dev, child_lstat.st_ino)
        or item.st_dev != root_stat.st_dev
        or (
            owner_enforced
            and (item.st_uid, item.st_gid) != (expected_uid, expected_gid)
        )
        or (owner_enforced and item.st_mode & 0o077)
    ):
        os.close(child_fd)
        raise _safe_code("fixture directory ownership or mode was unsafe")
    return child_fd


def _srt_timestamp_milliseconds(match: re.Match[str], prefix: str) -> int:
    hour = int(match.group(prefix + "h"))
    minute = int(match.group(prefix + "m"))
    second = int(match.group(prefix + "s"))
    millisecond = int(match.group(prefix + "ms"))
    if minute >= 60 or second >= 60:
        raise _safe_code("subtitle output contained an invalid timestamp")
    return (((hour * 60) + minute) * 60 + second) * 1000 + millisecond


def validate_srt_payload(
    payload: bytes, *, expected_duration_seconds: float
) -> tuple[int, int, int]:
    """Validate every cue and require representative whole-media coverage."""
    if (
        isinstance(expected_duration_seconds, bool)
        or not isinstance(expected_duration_seconds, (int, float))
        or expected_duration_seconds <= 0
        or expected_duration_seconds > LONG_MAXIMUM_SECONDS
    ):
        raise _safe_code("subtitle duration boundary was invalid")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _safe_code("subtitle output was not UTF-8") from exc
    if "\x00" in text or "\r" in text.replace("\r\n", ""):
        raise _safe_code("subtitle output contained invalid control data")
    normalized = text.replace("\r\n", "\n").strip("\n")
    blocks = re.split(r"\n{2,}", normalized) if normalized else []
    if not blocks:
        raise _safe_code("subtitle output contained no cues")

    first_start: int | None = None
    last_start = -1
    last_end = -1
    for expected_index, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) < 3 or lines[0] != str(expected_index):
            raise _safe_code("subtitle cue sequence was malformed")
        timing = SRT_TIMING_RE.fullmatch(lines[1])
        if timing is None:
            raise _safe_code("subtitle cue timing was malformed")
        start_ms = _srt_timestamp_milliseconds(timing, "s")
        end_ms = _srt_timestamp_milliseconds(timing, "e")
        if start_ms < last_start or end_ms <= start_ms:
            raise _safe_code("subtitle cue timeline was malformed")
        if not any(line.strip() for line in lines[2:]) or any(
            any(ord(character) < 32 and character != "\t" for character in line)
            for line in lines[2:]
        ):
            raise _safe_code("subtitle cue text was malformed")
        if first_start is None:
            first_start = start_ms
        last_start = start_ms
        last_end = end_ms

    assert first_start is not None
    expected_ms = int(expected_duration_seconds * 1000)
    minimum_cues = max(1, (expected_ms + 300_000 - 1) // 300_000)
    permitted_overrun = max(5_000, expected_ms // 50)
    if (
        len(blocks) < minimum_cues
        or last_end > expected_ms + permitted_overrun
        or last_end - first_start < int(expected_ms * SRT_MINIMUM_TIMELINE_COVERAGE)
    ):
        raise _safe_code("subtitle output did not cover the bounded media timeline")
    return len(blocks), first_start, last_end


def snapshot_fixture_file(
    fixtures: FixtureSet,
    relative_path: str,
    *,
    maximum_bytes: int = MAX_MEDIA_BYTES,
    expected_subtitle: bool = False,
    expected_duration_seconds: float | None = None,
) -> FileSnapshot:
    directory, name = relative_path.split("/", 1)
    directory_fd = _open_fixture_directory(
        fixtures.media_root,
        directory,
        expected_uid=fixtures.runtime_uid,
        expected_gid=fixtures.runtime_gid,
    )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        try:
            if isinstance(directory_fd, int):
                path_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            else:
                path_stat = (directory_fd / name).lstat()
        except OSError as exc:
            raise _safe_code("fixture file could not be inspected") from exc
        if stat.S_ISLNK(path_stat.st_mode):
            raise _safe_code("fixture file used a symlink")
        try:
            if isinstance(directory_fd, int):
                fd = os.open(name, flags, dir_fd=directory_fd)
            else:
                fd = os.open(directory_fd / name, flags)
        except OSError as exc:
            raise _safe_code("fixture file could not be opened") from exc
        try:
            item = os.fstat(fd)
            owner_enforced = _owner_id() is not None
            if (
                not stat.S_ISREG(item.st_mode)
                or (item.st_dev, item.st_ino) != (path_stat.st_dev, path_stat.st_ino)
                or item.st_nlink != 1
                or item.st_size <= 0
                or item.st_size > maximum_bytes
                or (
                    owner_enforced
                    and (item.st_uid, item.st_gid)
                    != (fixtures.runtime_uid, fixtures.runtime_gid)
                )
                or (owner_enforced and item.st_mode & 0o022)
            ):
                raise _safe_code("fixture file identity size or mode was unsafe")
            digest = hashlib.sha256()
            subtitle_chunks: list[bytes] | None = [] if expected_subtitle else None
            read_bytes = 0
            while True:
                chunk = os.read(fd, min(1024 * 1024, maximum_bytes + 1 - read_bytes))
                if not chunk:
                    break
                read_bytes += len(chunk)
                if read_bytes > maximum_bytes:
                    raise _safe_code(
                        "subtitle output exceeded byte limit"
                        if expected_subtitle
                        else "fixture media exceeded byte limit"
                    )
                digest.update(chunk)
                if subtitle_chunks is not None:
                    subtitle_chunks.append(chunk)
            after = os.fstat(fd)
            identity_fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_gid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if read_bytes != item.st_size or any(
                getattr(after, field) != getattr(item, field)
                for field in identity_fields
            ):
                raise _safe_code("fixture file changed while it was hashed")
            subtitle_metrics: tuple[int, int, int] | None = None
            if expected_subtitle:
                subtitle_payload = b"".join(subtitle_chunks or ())
                if (
                    len(subtitle_payload) > MAX_SUBTITLE_BYTES
                    or expected_duration_seconds is None
                ):
                    raise _safe_code("subtitle output was malformed or unbounded")
                subtitle_metrics = validate_srt_payload(
                    subtitle_payload,
                    expected_duration_seconds=expected_duration_seconds,
                )
            return FileSnapshot(
                device=item.st_dev,
                inode=item.st_ino,
                mode=stat.S_IMODE(item.st_mode),
                uid=item.st_uid,
                gid=item.st_gid,
                links=item.st_nlink,
                size=item.st_size,
                mtime_ns=item.st_mtime_ns,
                ctime_ns=item.st_ctime_ns,
                sha256=digest.hexdigest(),
                subtitle_cue_count=(subtitle_metrics or (None, None, None))[0],
                subtitle_first_start_ms=(subtitle_metrics or (None, None, None))[1],
                subtitle_last_end_ms=(subtitle_metrics or (None, None, None))[2],
            )
        finally:
            os.close(fd)
    finally:
        if isinstance(directory_fd, int):
            os.close(directory_fd)


def _directory_names(fixtures: FixtureSet, directory: str) -> set[str]:
    fd = _open_fixture_directory(
        fixtures.media_root,
        directory,
        expected_uid=fixtures.runtime_uid,
        expected_gid=fixtures.runtime_gid,
    )
    try:
        names = os.listdir(fd)
    finally:
        if isinstance(fd, int):
            os.close(fd)
    if any(
        not isinstance(name, str) or not name or "/" in name or "\\" in name
        for name in names
    ):
        raise _safe_code("fixture directory contained a malformed entry")
    return set(names)


def assert_fixture_directory_exact(
    fixtures: FixtureSet,
    directory: str,
    *,
    allow_outputs: bool,
) -> None:
    items = [
        item
        for item in fixtures.all_items
        if item.media_relative.split("/", 1)[0] == directory
    ]
    allowed = {item.media_relative.split("/", 1)[1] for item in items}
    if allow_outputs:
        allowed.update(
            item.subtitle_relative.split("/", 1)[1]
            for item in items
            if item.subtitle_relative is not None
        )
    observed = _directory_names(fixtures, directory)
    if observed != allowed:
        raise _safe_code("fixture directory contained partial or undeclared files")


def assert_outputs_absent(fixtures: FixtureSet) -> None:
    for directory in sorted(EXPECTED_FIXTURE_DIRECTORIES):
        assert_fixture_directory_exact(fixtures, directory, allow_outputs=False)


class AtomicPublicationLedger:
    """Fail-closed state machine fed by a continuous kernel event stream."""

    MUTATING_MASK = 0x00000002 | 0x00000004 | 0x00000008 | 0x00000040
    MUTATING_MASK |= 0x00000080 | 0x00000100 | 0x00000200
    IN_CLOSE_WRITE = 0x00000008
    IN_MOVED_FROM = 0x00000040
    IN_MOVED_TO = 0x00000080
    IN_CREATE = 0x00000100
    IN_DELETE = 0x00000200
    IN_Q_OVERFLOW = 0x00004000
    IN_IGNORED = 0x00008000
    DIRECTORY_INVALIDATED_MASK = 0x00000400 | 0x00000800
    EVENT_INVALIDATED_MASK = DIRECTORY_INVALIDATED_MASK | IN_IGNORED
    IN_ISDIR = 0x40000000

    def __init__(self, fixtures: FixtureSet) -> None:
        self.media_names: dict[str, set[str]] = {
            directory: set() for directory in EXPECTED_FIXTURE_DIRECTORIES
        }
        self.output_names: dict[str, set[str]] = {
            directory: set() for directory in EXPECTED_FIXTURE_DIRECTORIES
        }
        self.temp_patterns: dict[str, list[tuple[re.Pattern[str], str]]] = {
            directory: [] for directory in EXPECTED_FIXTURE_DIRECTORIES
        }
        self.expected_publications: set[str] = set()
        for item in fixtures.all_items:
            directory, media_name = item.media_relative.split("/", 1)
            self.media_names[directory].add(media_name)
            if item.subtitle_relative is None:
                continue
            output_directory, output_name = item.subtitle_relative.split("/", 1)
            if output_directory != directory:
                raise _safe_code("fixture output directory binding changed")
            self.output_names[directory].add(output_name)
            self.expected_publications.add(item.subtitle_relative)
            suffix = PurePosixPath(output_name).suffix
            pattern = re.compile(
                r"^\."
                + re.escape(output_name)
                + r"\.[A-Za-z0-9_-]{6,64}\.tmp"
                + re.escape(suffix)
                + r"$"
            )
            self.temp_patterns[directory].append((pattern, item.subtitle_relative))
        self.active_temporaries: dict[str, str] = {}
        self.rename_cookies: dict[int, str] = {}
        self.published: set[str] = set()
        self.publication_events: list[str] = []

    def _temporary_target(self, directory: str, name: str) -> str | None:
        for pattern, target in self.temp_patterns[directory]:
            if pattern.fullmatch(name):
                return target
        return None

    def observe(self, directory: str, name: str, mask: int, cookie: int) -> None:
        if directory not in EXPECTED_FIXTURE_DIRECTORIES:
            raise _safe_code("atomic watcher directory identity was invalid")
        if mask & self.IN_Q_OVERFLOW:
            raise _safe_code("atomic watcher kernel queue overflowed")
        if mask & self.EVENT_INVALIDATED_MASK or mask & self.IN_ISDIR:
            raise _safe_code("atomic watcher directory boundary changed")
        if not name or len(name) > 255 or "/" in name or "\\" in name:
            raise _safe_code("atomic watcher filename was malformed")
        if not mask & self.MUTATING_MASK:
            return
        relative = f"{directory}/{name}"
        if name in self.media_names[directory]:
            raise _safe_code("original fixture emitted a mutation event")
        if name in self.output_names[directory]:
            if mask != self.IN_MOVED_TO or cookie <= 0:
                raise _safe_code("subtitle final was exposed non-atomically")
            expected = self.rename_cookies.pop(cookie, None)
            if expected != relative or relative in self.published:
                raise _safe_code("subtitle atomic rename pairing was invalid")
            self.published.add(relative)
            self.publication_events.append(relative)
            return
        target = self._temporary_target(directory, name)
        if target is None:
            raise _safe_code("fixture directory emitted an undeclared mutation")
        if mask & self.IN_CREATE:
            if relative in self.active_temporaries:
                raise _safe_code("subtitle staging identity was duplicated")
            self.active_temporaries[relative] = target
        unsupported = mask & ~(
            self.IN_CREATE
            | self.IN_CLOSE_WRITE
            | self.IN_MOVED_FROM
            | self.IN_DELETE
            | 0x00000002
            | 0x00000004
        )
        if unsupported:
            raise _safe_code("subtitle staging event was unexpected")
        if mask & self.IN_MOVED_FROM:
            if cookie <= 0 or self.active_temporaries.pop(relative, None) != target:
                raise _safe_code("subtitle staging rename was unpaired")
            if cookie in self.rename_cookies:
                raise _safe_code("subtitle rename cookie was duplicated")
            self.rename_cookies[cookie] = target
        if mask & self.IN_DELETE:
            self.active_temporaries.pop(relative, None)

    def mark(self) -> int:
        return len(self.publication_events)

    def assert_published(
        self, items: Iterable[FixtureItem], *, after_mark: int = 0
    ) -> None:
        if (
            isinstance(after_mark, bool)
            or not isinstance(after_mark, int)
            or not 0 <= after_mark <= len(self.publication_events)
        ):
            raise _safe_code("atomic publication freshness mark was invalid")
        expected = {
            item.subtitle_relative
            for item in items
            if item.subtitle_relative is not None
        }
        fresh = set(self.publication_events[after_mark:])
        if not expected or not expected.issubset(fresh):
            raise _safe_code("subtitle atomic publication was not observed")

    def assert_complete(self) -> None:
        if self.published != self.expected_publications:
            raise _safe_code("atomic publication set was incomplete")
        if self.active_temporaries or self.rename_cookies:
            raise _safe_code("atomic publication state was left incomplete")


class AtomicOutputWatcher:
    """Linux inotify queue; events remain continuous between bounded drains."""

    EVENT_HEADER = struct.Struct("iIII")
    WATCH_MASK = AtomicPublicationLedger.MUTATING_MASK
    WATCH_MASK |= AtomicPublicationLedger.DIRECTORY_INVALIDATED_MASK

    def __init__(self, fixtures: FixtureSet) -> None:
        if sys.platform != "linux":
            raise _safe_code("atomic output watcher requires Linux inotify")
        self.fixtures = fixtures
        self.ledger = AtomicPublicationLedger(fixtures)
        self._lock = threading.Lock()
        self._closed = False
        library = ctypes.CDLL(None, use_errno=True)
        try:
            initialize = library.inotify_init1
            add_watch = library.inotify_add_watch
        except AttributeError as exc:
            raise _safe_code("atomic output watcher was unavailable") from exc
        initialize.argtypes = [ctypes.c_int]
        initialize.restype = ctypes.c_int
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int
        descriptor = initialize(os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        if descriptor < 0:
            raise _safe_code("atomic output watcher could not be initialized")
        self._descriptor = descriptor
        self._watch_directories: dict[int, str] = {}
        try:
            for directory in sorted(EXPECTED_FIXTURE_DIRECTORIES):
                path = fixtures.media_root / directory
                before = path.lstat()
                watch = add_watch(
                    descriptor,
                    os.fsencode(path),
                    ctypes.c_uint32(self.WATCH_MASK),
                )
                after = path.lstat()
                if (
                    watch < 0
                    or stat.S_ISLNK(after.st_mode)
                    or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or watch in self._watch_directories
                ):
                    raise _safe_code("atomic output watch binding failed")
                self._watch_directories[watch] = directory
        except BaseException:
            os.close(descriptor)
            self._closed = True
            raise

    def _drain_locked(self) -> None:
        if self._closed:
            raise _safe_code("atomic output watcher was already closed")
        while True:
            try:
                payload = os.read(self._descriptor, MAX_EVENT_LOG_BYTES)
            except BlockingIOError:
                return
            except OSError as exc:
                raise _safe_code("atomic output event read failed") from exc
            if not payload:
                raise _safe_code("atomic output event stream closed")
            offset = 0
            while offset < len(payload):
                if len(payload) - offset < self.EVENT_HEADER.size:
                    raise _safe_code("atomic output event was truncated")
                watch, mask, cookie, name_length = self.EVENT_HEADER.unpack_from(
                    payload, offset
                )
                offset += self.EVENT_HEADER.size
                if name_length > 4096 or offset + name_length > len(payload):
                    raise _safe_code("atomic output event length was invalid")
                raw_name = payload[offset : offset + name_length]
                offset += name_length
                try:
                    name = raw_name.rstrip(b"\x00").decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise _safe_code("atomic output filename was not UTF-8") from exc
                if mask & AtomicPublicationLedger.IN_Q_OVERFLOW:
                    self.ledger.observe("long", "overflow", mask, cookie)
                    continue
                directory = self._watch_directories.get(watch)
                if directory is None:
                    raise _safe_code("atomic output watch identity changed")
                self.ledger.observe(directory, name, mask, cookie)

    def drain(self) -> None:
        with self._lock:
            self._drain_locked()

    def assert_published(
        self, items: Iterable[FixtureItem], *, after_mark: int = 0
    ) -> None:
        with self._lock:
            self._drain_locked()
            self.ledger.assert_published(items, after_mark=after_mark)

    def mark(self) -> int:
        with self._lock:
            self._drain_locked()
            return self.ledger.mark()

    def assert_complete(self) -> None:
        with self._lock:
            self._drain_locked()
            self.ledger.assert_complete()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                os.close(self._descriptor)
                self._closed = True


def capture_original_snapshots(fixtures: FixtureSet) -> dict[str, FileSnapshot]:
    return {
        item.media_relative: snapshot_fixture_file(fixtures, item.media_relative)
        for item in fixtures.all_items
    }


def attest_originals_unchanged(
    fixtures: FixtureSet,
    originals: dict[str, FileSnapshot],
) -> None:
    if set(originals) != {item.media_relative for item in fixtures.all_items}:
        raise _safe_code("original fixture snapshot set changed")
    for relative, before in originals.items():
        if snapshot_fixture_file(fixtures, relative) != before:
            raise _safe_code("original fixture generation or content changed")


def _probe_duration(path: Path) -> float:
    result = health.bounded_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        label="fixture duration probe",
        timeout=30,
        max_bytes=64 * 1024,
    )
    document = health.strict_json_object(
        result.output.encode("utf-8"), label="fixture duration", max_bytes=64 * 1024
    )
    format_item = document.get("format")
    if not isinstance(format_item, dict) or set(format_item) != {"duration"}:
        raise _safe_code("fixture duration response was malformed")
    return health.finite_number(
        format_item.get("duration"), "fixture duration", positive=True
    )


def validate_fixture_durations(fixtures: FixtureSet) -> dict[str, Any]:
    long_duration = _probe_duration(fixtures.media_root / fixtures.long.media_relative)
    if not LONG_MINIMUM_SECONDS <= long_duration <= LONG_MAXIMUM_SECONDS:
        raise _safe_code("long fixture was not a bounded 31 minute workload")
    short_durations = [
        _probe_duration(fixtures.media_root / item.media_relative)
        for item in fixtures.short
    ]
    reload_duration = _probe_duration(
        fixtures.media_root / fixtures.reload.media_relative
    )
    if any(
        value > SHORT_MAXIMUM_SECONDS for value in (*short_durations, reload_duration)
    ):
        raise _safe_code("short or reload fixture exceeded duration boundary")
    return {
        "long_seconds": round(long_duration, 3),
        "short_count": len(short_durations),
        "short_max_seconds": round(max(short_durations), 3),
        "reload_seconds": round(reload_duration, 3),
    }


def _read_api_key(path: Path | None) -> str:
    if path is None:
        raise _safe_code("API key file was required for request isolation")
    payload = _require_private_file(path, maximum=MAX_API_KEY_BYTES, label="API key")
    try:
        value = payload.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise _safe_code("API key was not ASCII") from exc
    if not 16 <= len(value) <= 512 or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        raise _safe_code("API key format was invalid")
    return value


def validate_request_isolation(item: dict[str, Any], api_key: str) -> None:
    """Bind the private observer credential to the exact candidate API gate."""
    if (
        not isinstance(api_key, str)
        or not 16 <= len(api_key) <= 512
        or any(ord(character) < 33 or ord(character) > 126 for character in api_key)
    ):
        raise _safe_code("runtime request isolation credential was invalid")
    _normalized, environment = health._normalized_environment(item)
    configured = environment.get("SUBGEN_API_KEY")
    if (
        not isinstance(configured, str)
        or not 16 <= len(configured) <= 512
        or any(ord(character) < 33 or ord(character) > 126 for character in configured)
        or not hmac.compare_digest(configured, api_key)
    ):
        raise _safe_code("runtime request isolation binding was not exact")


def post_batch(directory: str, *, api_key: str, timeout: float) -> None:
    if not re.fullmatch(r"/media/(?:long|short|reload|invalid|silent)", directory):
        raise _safe_code("batch directory left the disposable fixture allowlist")
    if (
        not isinstance(api_key, str)
        or not 16 <= len(api_key) <= 512
        or any(ord(character) < 33 or ord(character) > 126 for character in api_key)
    ):
        raise _safe_code("batch request isolation credential was invalid")
    query = urllib.parse.urlencode({"directory": directory, "forceLanguage": "en"})
    request_path = "/batch?" + query
    headers = {"Content-Length": "0", "X-Subgen-Api-Key": api_key}
    connection = http.client.HTTPConnection("127.0.0.1", 19000, timeout=timeout)
    try:
        connection.request("POST", request_path, body=b"", headers=headers)
        response = connection.getresponse()
        body = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
        if len(body) > MAX_HTTP_RESPONSE_BYTES:
            raise _safe_code("batch response exceeded byte limit")
        if response.status != 200:
            raise _safe_code("batch request did not return exact success")
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise _safe_code("batch request was unavailable or timed out") from exc
    finally:
        connection.close()


class RuntimeEventScanner:
    """Bounded incremental parser that retains only expected event identities."""

    def __init__(
        self,
        client: health.DockerClient,
        binding: health.CandidateBinding,
        started_wall: float,
        expected_paths: Iterable[str],
    ) -> None:
        self.client = client
        self.binding = binding
        self.cursor_wall = started_wall
        self.expected_paths = frozenset(expected_paths)
        if not self.expected_paths or any(
            not isinstance(path, str)
            or len(path) > 1024
            or not re.fullmatch(
                r"/media/(?:long|short|reload|invalid|silent)/[^\s/]+", path
            )
            for path in self.expected_paths
        ):
            raise _safe_code("workload event path allowlist was malformed")
        self._line_digests: set[str] = set()
        self._retained_event_bytes = 0
        self._observation_sequence = 0
        self.events: list[dict[str, Any]] = []
        self._event_sequences: list[int] = []
        self.silent_paths: set[str] = set()
        self._silent_events: list[tuple[int, str]] = []
        self.unload_count = 0

    def _retain_machine_event(self, event: dict[str, Any]) -> None:
        path = event.get("path")
        name = event.get("event")
        if not isinstance(path, str) or not isinstance(name, str):
            raise _safe_code("candidate machine event identity was incomplete")
        retained_names = {
            "worker_start",
            "worker_finish",
            "worker_error",
            "media_validation_failed",
        }
        if name in retained_names and path not in self.expected_paths:
            raise _safe_code("candidate workload event left the path allowlist")
        if path not in self.expected_paths:
            return
        if name in {"worker_start", "worker_finish", "worker_error"}:
            task_id = event.get("task_id")
            task_type = event.get("task_type")
            source_identity = event.get("source_identity")
            expected_task_id = hashlib.sha256(
                f"transcribe:{path}".encode("utf-8")
            ).hexdigest()[:16]
            if (
                task_id != expected_task_id
                or task_type != "transcribe"
                or not isinstance(source_identity, list)
                or len(source_identity) != 5
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in source_identity
                )
            ):
                raise _safe_code("candidate worker event identity was malformed")
            retained = {
                "event": name,
                "task_id": task_id,
                "task_type": task_type,
                "path": path,
                "source_identity": list(source_identity),
            }
        elif name == "media_validation_failed":
            failure_class = event.get("failure_class")
            outcomes = event.get("validator_outcomes")
            if (
                not isinstance(failure_class, str)
                or len(failure_class) > 64
                or not isinstance(outcomes, dict)
                or set(outcomes) != {"ffprobe", "pyav"}
                or any(
                    not isinstance(value, str) or len(value) > 64
                    for value in outcomes.values()
                )
            ):
                raise _safe_code("candidate validation event was malformed")
            retained = {
                "event": name,
                "path": path,
                "failure_class": failure_class,
                "validator_outcomes": {
                    "ffprobe": outcomes["ffprobe"],
                    "pyav": outcomes["pyav"],
                },
            }
        else:
            return
        encoded = json.dumps(retained, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        if (
            len(self.events) >= MAX_EVENT_IDENTITIES
            or self._retained_event_bytes + len(encoded) > MAX_RETAINED_EVENT_BYTES
        ):
            raise _safe_code("retained workload event budget was exceeded")
        self._retained_event_bytes += len(encoded)
        self._observation_sequence += 1
        self.events.append(retained)
        self._event_sequences.append(self._observation_sequence)

    def parse_lines(self, payload: str) -> None:
        for line in payload.splitlines():
            if len(line.encode("utf-8", errors="replace")) > MAX_EVENT_LOG_BYTES:
                raise _safe_code("candidate workload event line exceeded byte limit")
            digest = health.sha256_bytes(line.encode("utf-8", errors="replace"))
            if digest in self._line_digests:
                continue
            self._line_digests.add(digest)
            if len(self._line_digests) > MAX_EVENT_IDENTITIES:
                raise _safe_code("workload event identity bound was exceeded")
            if "Model unloaded from memory" in line:
                self.unload_count += 1
            if SUBGEN_EVENT_PREFIX in line:
                raw = line.split(SUBGEN_EVENT_PREFIX, 1)[1]
                try:
                    event = json.loads(
                        raw, object_pairs_hook=health._reject_duplicate_object
                    )
                except (json.JSONDecodeError, health.GateAbort) as exc:
                    raise _safe_code("candidate machine event was malformed") from exc
                if not isinstance(event, dict):
                    raise _safe_code("candidate machine event was not an object")
                self._retain_machine_event(event)
            silent = SILENT_EVENT_RE.search(line)
            if silent and silent.group("path") in self.expected_paths:
                path = silent.group("path")
                self._observation_sequence += 1
                self.silent_paths.add(path)
                self._silent_events.append((self._observation_sequence, path))

    def mark(self) -> int:
        return self._observation_sequence

    def _validate_mark(self, after_mark: int) -> None:
        if (
            isinstance(after_mark, bool)
            or not isinstance(after_mark, int)
            or not 0 <= after_mark <= self._observation_sequence
        ):
            raise _safe_code("workload event freshness mark was invalid")

    def events_after(self, after_mark: int) -> list[dict[str, Any]]:
        self._validate_mark(after_mark)
        return [
            event
            for sequence, event in zip(self._event_sequences, self.events, strict=True)
            if sequence > after_mark
        ]

    def silent_after(self, after_mark: int, expected_path: str) -> bool:
        self._validate_mark(after_mark)
        if expected_path not in self.expected_paths:
            raise _safe_code("silent event path left the workload allowlist")
        return any(
            sequence > after_mark and path == expected_path
            for sequence, path in self._silent_events
        )

    def scan(self, until_wall: float | None = None) -> None:
        end = time.time() if until_wall is None else until_wall
        since = max(0.0, self.cursor_wall - health.LOG_OVERLAP_SECONDS)
        if end < since:
            raise _safe_code("workload event clock moved backwards")
        result = self.client.command(
            "logs",
            "--timestamps",
            "--since",
            f"{since:.6f}",
            "--until",
            f"{end:.6f}",
            self.binding.container_id,
            label="candidate workload event logs",
            timeout=10,
            max_bytes=MAX_EVENT_LOG_BYTES,
        )
        self.parse_lines(result.output)
        self.cursor_wall = end

    def matching_events(
        self,
        event_name: str,
        expected_paths: Iterable[str],
        *,
        after_mark: int = 0,
    ) -> list[dict[str, Any]]:
        paths = set(expected_paths)
        return [
            event
            for event in self.events_after(after_mark)
            if event.get("event") == event_name and event.get("path") in paths
        ]

    def assert_no_worker_errors(
        self, expected_paths: Iterable[str], *, after_mark: int = 0
    ) -> None:
        if self.matching_events("worker_error", expected_paths, after_mark=after_mark):
            raise _safe_code("candidate workload emitted a worker error")


class RuntimeCycleTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._observations: list[RuntimeStatusObservation] = []

    def mark(self) -> int:
        with self._lock:
            return len(self._observations)

    def observe(self, resource: dict[str, Any], now_monotonic: float) -> None:
        state = resource.get("controller_state")
        reason = resource.get("recovery_reason")
        admission = resource.get("admission_open")
        if not isinstance(state, str) or not isinstance(admission, bool):
            raise _safe_code("runtime state observation was malformed")
        with self._lock:
            self._observations.append(
                RuntimeStatusObservation(
                    sequence=len(self._observations) + 1,
                    observed_monotonic=now_monotonic,
                    state=state,
                    recovery_reason=reason,
                    admission_open=admission,
                )
            )

    def idle_recovery_proof(self, mark: int) -> RuntimeRecoveryProof | None:
        with self._lock:
            observations = list(self._observations[mark:])
        first_index = next(
            (
                index
                for index, item in enumerate(observations)
                if item.state == "recovering"
                and item.recovery_reason == "idle_cleanup"
                and item.admission_open is False
            ),
            None,
        )
        if first_index is None:
            return None
        recovery = observations[first_index]
        for final in observations[first_index + 1 :]:
            if (
                final.state == "normal"
                and final.recovery_reason is None
                and final.admission_open
            ):
                relevant = [
                    item
                    for item in observations[first_index + 1 :]
                    if item.sequence <= final.sequence
                ]
                elapsed = final.observed_monotonic - recovery.observed_monotonic
                if len(relevant) >= 3 and elapsed >= MIN_RECOVERY_SPAN_SECONDS:
                    return RuntimeRecoveryProof(
                        recovering_sequence=recovery.sequence,
                        normal_sequence=final.sequence,
                        complete_health_polls=len(relevant),
                        elapsed_seconds=elapsed,
                    )
                raise _safe_code(
                    "runtime reopened before three complete recovery health polls"
                )
        return None

    def latest_is_healthy(self) -> bool:
        with self._lock:
            if not self._observations:
                return False
            latest = self._observations[-1]
        return (
            latest.state == "normal"
            and latest.recovery_reason is None
            and latest.admission_open is True
        )


def validate_runtime_status(
    payload: dict[str, Any],
    *,
    expected_model: str,
    expected_reserve_bytes: int,
    observed_gpu_total_bytes: int,
    allow_idle_recovery: bool,
) -> dict[str, Any]:
    resource = payload.get("resource_management")
    if not isinstance(resource, dict):
        raise health.CandidateNotReady("candidate resource status not initialized")
    state = resource.get("controller_state")
    reason = resource.get("recovery_reason")
    admission = resource.get("admission_open")
    dynamic = (state, reason, admission)
    healthy = ("normal", None, True)
    recovering = ("recovering", "idle_cleanup", False)
    if dynamic != healthy and (not allow_idle_recovery or dynamic != recovering):
        raise _safe_code("candidate runtime state left the allowed workload phase")
    normalized = copy.deepcopy(payload)
    normalized_resource = normalized.get("resource_management")
    assert isinstance(normalized_resource, dict)
    normalized_resource.update(
        {"controller_state": "normal", "recovery_reason": None, "admission_open": True}
    )
    checked = health.validate_candidate_status(
        normalized,
        expected_model=expected_model,
        expected_reserve_bytes=expected_reserve_bytes,
        observed_gpu_total_bytes=observed_gpu_total_bytes,
    )
    checked.update(
        {
            "controller_state": state,
            "recovery_reason": reason,
            "admission_open": admission,
        }
    )
    return checked


def validate_runtime_chunk_policy(
    item: dict[str, Any], *, expected_chunk_minutes: int
) -> dict[str, Any]:
    """Bind the explicit gate chunk policy from the exact candidate config."""
    if (
        isinstance(expected_chunk_minutes, bool)
        or not isinstance(expected_chunk_minutes, int)
        or not 5 <= expected_chunk_minutes <= 30
    ):
        raise _safe_code("expected runtime chunk policy was outside boundary")
    _normalized, environment = health._normalized_environment(item)
    if environment.get("SEGMENTATION_ENABLED", "").casefold() != "true":
        raise _safe_code("runtime segmentation was not explicitly enabled")
    if environment.get("SEGMENTATION_CHUNK_MINUTES") != str(expected_chunk_minutes):
        raise _safe_code("runtime chunk policy did not match gate expectation")
    isolation = {
        "SKIP_STARTUP_SCAN": "True",
        "MONITOR": "False",
        "PROCESS_ADDED_MEDIA": "False",
        "PROCESS_MEDIA_ON_PLAY": "False",
    }
    if any(environment.get(key) != expected for key, expected in isolation.items()):
        raise _safe_code("runtime startup and event isolation controls were not exact")
    if {"PROCADDEDMEDIA", "PROCMEDIAONPLAY"}.intersection(environment):
        raise _safe_code("runtime event isolation aliases were present")
    return {
        "segmentation_enabled": True,
        "chunk_minutes": expected_chunk_minutes,
        "skip_startup_scan": True,
        "monitor": False,
        "process_added_media": False,
        "process_media_on_play": False,
    }


def _contains_forbidden(value: Any, forbidden: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(secret and secret in value for secret in forbidden)
    if isinstance(value, dict):
        return any(_contains_forbidden(item, forbidden) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden(item, forbidden) for item in value)
    return False


class LockedEvidence:
    """Serialize main/health-thread writes and reject privacy-boundary leaks."""

    def __init__(self, writer: health.EvidenceWriter, forbidden: Iterable[str]) -> None:
        self.writer = writer
        self.forbidden = tuple(item for item in forbidden if item)
        self.lock = threading.Lock()
        self.approximate_bytes = 0

    @property
    def closed(self) -> bool:
        return self.writer.closed

    def write(self, record: dict[str, Any]) -> None:
        if _contains_forbidden(record, self.forbidden):
            raise _safe_code("observer evidence crossed the privacy boundary")
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        with self.lock:
            if self.approximate_bytes + len(encoded) + 1 > MAX_OBSERVER_EVIDENCE_BYTES:
                raise _safe_code("observer evidence exceeded byte budget")
            self.writer.write(record)
            self.approximate_bytes += len(encoded) + 1

    def seal(
        self,
        *,
        outcome: str,
        sampler_sha256: str,
        image_config: str,
        cleanup: dict[str, Any],
    ) -> str:
        with self.lock:
            return self.writer.seal(
                outcome=outcome,
                sampler_sha256=sampler_sha256,
                image_config=image_config,
                cleanup=cleanup,
            )

    def close(self) -> None:
        with self.lock:
            self.writer.close()


class HealthMonitor:
    def __init__(
        self,
        *,
        args: argparse.Namespace,
        evidence: LockedEvidence,
        client: health.DockerClient,
        candidate: health.CandidateBinding,
        frigate: health.ObservedBinding,
        expectations: dict[str, float],
        baselines: HealthBaselines,
        logs: health.IncrementalLogScanner,
        tracker: RuntimeCycleTracker,
        output_watcher: AtomicOutputWatcher,
        started_monotonic: float,
    ) -> None:
        self.args = args
        self.evidence = evidence
        self.client = client
        self.candidate = candidate
        self.frigate = frigate
        self.expectations = expectations
        self.baselines = baselines
        self.logs = logs
        self.tracker = tracker
        self.output_watcher = output_watcher
        self.started_monotonic = started_monotonic
        self.low_since = {name: None for name in expectations}
        self._allow_idle_recovery = False
        self._phase_lock = threading.Lock()
        self._stop = threading.Event()
        self._duration_reached = threading.Event()
        self._failure = threading.Event()
        self._failure_exception: BaseException | None = None
        self._thread: threading.Thread | None = None
        self.sample_count = 0
        self.last_elapsed = 0.0

    def set_phase(self, *, allow_idle_recovery: bool) -> None:
        with self._phase_lock:
            self._allow_idle_recovery = allow_idle_recovery

    def _phase_allows_recovery(self) -> bool:
        with self._phase_lock:
            return self._allow_idle_recovery

    def start(self) -> None:
        if self._thread is not None:
            raise _safe_code("health monitor was already started")
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="subgen-task11b-runtime-health",
        )
        self._thread.start()

    def _state_and_memory(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        self.client.verify_local_daemon()
        candidate = health.candidate_state(self.client, self.candidate, self.args)
        frigate = health.observed_state(self.client, self.frigate)
        if candidate["running"] is not True or candidate["status"] != "running":
            raise _safe_code("candidate stopped during composite workload")
        if candidate["oom_killed"] is not False:
            raise _safe_code("candidate was OOM killed during composite workload")
        if candidate["restart_count"] != self.baselines.candidate_restart_count:
            raise _safe_code("candidate restart count increased")
        if frigate["running"] is not True or frigate["health"] != "healthy":
            raise _safe_code("Frigate lost healthy state")
        if frigate["restart_count"] != self.baselines.frigate_restart_count:
            raise _safe_code("Frigate restart count increased")
        memory = health.candidate_memory(self.client, self.candidate)
        health.validate_candidate_memory_snapshot(
            memory, expected_memory_bytes=self.args.expected_memory_bytes
        )
        return candidate, frigate, memory

    def collect(self, *, final: bool = False) -> None:
        self.output_watcher.drain()
        now_monotonic = time.monotonic()
        now_wall = time.time()
        candidate, frigate, memory = self._state_and_memory()
        host_available = health.read_mem_available_bytes()
        if host_available < self.args.host_reserve_bytes:
            raise _safe_code("host memory reserve breached")
        health.read_host_pressure()
        gpu = health.gpu_telemetry()
        if gpu["free_mib"] * health.MIB < self.args.gpu_free_floor_bytes:
            raise _safe_code("GPU priority reserve breached")
        ollama = health.fetch_json(self.args.ollama_url, endpoint="ollama")
        models = ollama.get("models")
        if not isinstance(models, list) or models:
            raise _safe_code("Ollama model became loaded")
        stats = health.fetch_json(self.args.frigate_stats_url, endpoint="frigate")
        frigate_metrics = health.validate_frigate_stats(
            stats,
            self.expectations,
            self.low_since,
            now_monotonic,
            now_wall,
        )
        resource = validate_runtime_status(
            health.fetch_json(self.args.candidate_status_url, endpoint="candidate"),
            expected_model=self.args.expected_model,
            expected_reserve_bytes=self.args.gpu_free_floor_bytes,
            observed_gpu_total_bytes=gpu["total_mib"] * health.MIB,
            allow_idle_recovery=self._phase_allows_recovery(),
        )
        self.tracker.observe(resource, now_monotonic)
        log_end = time.time()
        self.logs.scan(log_end)
        elapsed = now_monotonic - self.started_monotonic
        self.sample_count += 1
        self.last_elapsed = elapsed
        self.evidence.write(
            {
                "event": "health_final" if final else "health_sample",
                "timestamp": health.utc_now(),
                "sample": self.sample_count,
                "elapsed_seconds": round(elapsed, 3),
                "candidate_status": candidate["status"],
                "candidate_restart_count": candidate["restart_count"],
                "candidate_oom_killed": candidate["oom_killed"],
                "memory_current_bytes": memory["memory.current"],
                "memory_peak_bytes": memory["memory.peak"],
                "memory_events": memory["events"],
                "runtime_state": resource["controller_state"],
                "runtime_recovery_reason": resource["recovery_reason"],
                "runtime_admission_open": resource["admission_open"],
                "host_mem_available_bytes": host_available,
                "gpu_total_mib": gpu["total_mib"],
                "gpu_used_mib": gpu["used_mib"],
                "gpu_free_mib": gpu["free_mib"],
                "frigate_restart_count": frigate["restart_count"],
                "camera_min_process_ratio": frigate_metrics["camera_min_process_ratio"],
                "camera_max_skipped_fps": frigate_metrics["camera_max_skipped_fps"],
                "camera_longest_low_seconds": frigate_metrics[
                    "camera_longest_low_seconds"
                ],
                "detector_count": frigate_metrics["detector_count"],
                "embedding_metric_count": frigate_metrics["embedding_metric_count"],
                "ollama_loaded_models": 0,
                "psi_policy": "parsed_observation_only",
            }
        )
        if elapsed >= self.args.duration_seconds:
            self._duration_reached.set()

    def _run(self) -> None:
        scheduled = time.monotonic()
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now < scheduled:
                    if self._stop.wait(scheduled - now):
                        break
                    now = time.monotonic()
                if now - scheduled > health.MAX_SAMPLE_LAG_SECONDS:
                    raise _safe_code("runtime observer health cadence lag exceeded")
                self.collect()
                scheduled += self.args.interval_seconds
                if time.monotonic() > scheduled:
                    raise _safe_code("runtime observer health work exceeded cadence")
        except BaseException as exc:
            self._failure_exception = exc
            self._failure.set()

    def raise_if_failed(self) -> None:
        if self._failure.is_set():
            failure = self._failure_exception or _safe_code("health monitor failed")
            if isinstance(failure, health.GateAbort):
                raise failure
            raise _safe_code(f"health monitor {type(failure).__name__}") from failure

    def wait_for_duration(self) -> None:
        while not self._duration_reached.wait(1.0):
            self.raise_if_failed()
        self.raise_if_failed()

    def stop_and_join(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.args.interval_seconds + 20)
            if self._thread.is_alive():
                raise _safe_code("health monitor did not stop")
        self.raise_if_failed()


def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float,
    failure_message: str,
    heartbeat: Callable[[], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Any:
    deadline = clock() + timeout
    while True:
        if heartbeat is not None:
            heartbeat()
        value = predicate()
        if value:
            return value
        now = clock()
        if now >= deadline:
            raise _safe_code(failure_message)
        sleeper(min(1.0, deadline - now))


def _worker_finished(
    scanner: RuntimeEventScanner,
    items: Iterable[FixtureItem],
    *,
    after_mark: int,
) -> bool:
    paths = {item.container_media for item in items}
    scanner.assert_no_worker_errors(paths)
    fresh = scanner.events_after(after_mark)
    if any(event.get("path") not in paths for event in fresh):
        raise _safe_code("workload phase observed an unrelated fixture event")
    started: dict[str, tuple[str, str, tuple[int, ...]]] = {}
    finished: set[str] = set()
    for event in fresh:
        path = event.get("path")
        name = event.get("event")
        identity = (
            event.get("task_id"),
            event.get("task_type"),
            tuple(event.get("source_identity", ())),
        )
        if name == "worker_start":
            if path in started or path in finished:
                raise _safe_code("workload phase emitted a duplicate worker start")
            started[path] = identity
        elif name == "worker_finish":
            if path not in started or path in finished:
                raise _safe_code("workload phase worker finish was stale or unpaired")
            if started[path] != identity:
                raise _safe_code("workload phase worker identity changed")
            finished.add(path)
    return set(started) == paths and finished == paths


def prepare_workload_phase(
    *,
    fixtures: FixtureSet,
    items: tuple[FixtureItem, ...],
    scanner: RuntimeEventScanner,
    output_watcher: AtomicOutputWatcher,
) -> PhaseFreshnessMark:
    if not items:
        raise _safe_code("workload phase fixture set was empty")
    directories = {item.media_relative.split("/", 1)[0] for item in items}
    if len(directories) != 1:
        raise _safe_code("workload phase crossed fixture directories")
    scanner.scan()
    event_sequence = scanner.mark()
    publication_sequence = output_watcher.mark()
    assert_fixture_directory_exact(
        fixtures,
        next(iter(directories)),
        allow_outputs=False,
    )
    return PhaseFreshnessMark(event_sequence, publication_sequence)


def _outputs_exist(fixtures: FixtureSet, items: Iterable[FixtureItem]) -> bool:
    item_list = tuple(items)
    directories = {item.media_relative.split("/", 1)[0] for item in item_list}
    for directory in directories:
        declared_media = {
            item.media_relative.split("/", 1)[1]
            for item in item_list
            if item.media_relative.startswith(directory + "/")
        }
        declared_outputs = {
            item.subtitle_relative.split("/", 1)[1]
            for item in item_list
            if item.subtitle_relative is not None
            and item.subtitle_relative.startswith(directory + "/")
        }
        if not _directory_names(fixtures, directory).issubset(
            declared_media | declared_outputs
        ):
            raise _safe_code("fixture directory contained a partial output")
    for item in item_list:
        assert item.subtitle_relative is not None
        directory, name = item.subtitle_relative.split("/", 1)
        if name not in _directory_names(fixtures, directory):
            return False
    return True


def wait_for_transcription_outputs(
    *,
    fixtures: FixtureSet,
    items: tuple[FixtureItem, ...],
    scanner: RuntimeEventScanner,
    monitor: HealthMonitor,
    output_watcher: AtomicOutputWatcher,
    phase_mark: PhaseFreshnessMark,
    timeout: float,
) -> tuple[FileSnapshot, ...]:
    def completed() -> bool:
        output_watcher.drain()
        scanner.scan()
        return _worker_finished(
            scanner, items, after_mark=phase_mark.event_sequence
        ) and _outputs_exist(fixtures, items)

    wait_until(
        completed,
        timeout=timeout,
        failure_message="transcription workload timed out",
        heartbeat=monitor.raise_if_failed,
    )
    output_watcher.assert_published(items, after_mark=phase_mark.publication_sequence)
    outputs = tuple(
        snapshot_fixture_file(
            fixtures,
            item.subtitle_relative or "",
            maximum_bytes=MAX_SUBTITLE_BYTES,
            expected_subtitle=True,
            expected_duration_seconds=_probe_duration(
                fixtures.media_root / item.media_relative
            ),
        )
        for item in items
    )
    directory = items[0].media_relative.split("/", 1)[0]
    assert_fixture_directory_exact(fixtures, directory, allow_outputs=True)
    return outputs


def _output_evidence(role: str, outputs: tuple[FileSnapshot, ...]) -> dict[str, Any]:
    return {
        "event": "workload_output",
        "timestamp": health.utc_now(),
        "role": role,
        "count": len(outputs),
        "outputs": [
            {
                "index": index,
                "bytes": item.size,
                "sha256": item.sha256,
                "cue_count": item.subtitle_cue_count,
                "first_start_ms": item.subtitle_first_start_ms,
                "last_end_ms": item.subtitle_last_end_ms,
            }
            for index, item in enumerate(outputs)
        ],
        "atomic_final_only": True,
        "partials_present": False,
        "atomic_publications_observed": len(outputs),
    }


def _run_composite_workload(
    *,
    args: argparse.Namespace,
    fixtures: FixtureSet,
    originals: dict[str, FileSnapshot],
    api_key: str,
    scanner: RuntimeEventScanner,
    monitor: HealthMonitor,
    tracker: RuntimeCycleTracker,
    output_watcher: AtomicOutputWatcher,
    evidence: LockedEvidence,
) -> None:
    all_workload_paths = [item.container_media for item in fixtures.all_items]
    monitor.set_phase(allow_idle_recovery=False)
    evidence.write(
        {"event": "workload_phase", "timestamp": health.utc_now(), "phase": "long"}
    )
    long_mark = prepare_workload_phase(
        fixtures=fixtures,
        items=(fixtures.long,),
        scanner=scanner,
        output_watcher=output_watcher,
    )
    post_batch(
        fixtures.long.container_directory,
        api_key=api_key,
        timeout=args.http_timeout_seconds,
    )
    long_output = wait_for_transcription_outputs(
        fixtures=fixtures,
        items=(fixtures.long,),
        scanner=scanner,
        monitor=monitor,
        output_watcher=output_watcher,
        phase_mark=long_mark,
        timeout=args.long_timeout_seconds,
    )
    evidence.write(_output_evidence("long_31m", long_output))
    attest_originals_unchanged(fixtures, originals)

    scanner.scan()
    unload_before_short = scanner.unload_count
    if unload_before_short != 0:
        raise _safe_code("model unloaded before resident short batch")
    evidence.write(
        {
            "event": "workload_phase",
            "timestamp": health.utc_now(),
            "phase": "resident_short_batch",
        }
    )
    short_mark = prepare_workload_phase(
        fixtures=fixtures,
        items=fixtures.short,
        scanner=scanner,
        output_watcher=output_watcher,
    )
    post_batch(
        fixtures.short[0].container_directory,
        api_key=api_key,
        timeout=args.http_timeout_seconds,
    )
    short_outputs = wait_for_transcription_outputs(
        fixtures=fixtures,
        items=fixtures.short,
        scanner=scanner,
        monitor=monitor,
        output_watcher=output_watcher,
        phase_mark=short_mark,
        timeout=args.short_timeout_seconds,
    )
    scanner.scan()
    if scanner.unload_count != unload_before_short:
        raise _safe_code("model unloaded during resident short batch")
    evidence.write(_output_evidence("resident_short_batch", short_outputs))
    attest_originals_unchanged(fixtures, originals)

    recovery_mark = tracker.mark()
    monitor.set_phase(allow_idle_recovery=True)
    evidence.write(
        {
            "event": "workload_phase",
            "timestamp": health.utc_now(),
            "phase": "idle_unload_recovery",
        }
    )

    def recovery_complete() -> RuntimeRecoveryProof | None:
        scanner.scan()
        if scanner.unload_count <= unload_before_short:
            return None
        return tracker.idle_recovery_proof(recovery_mark)

    proof = wait_until(
        recovery_complete,
        timeout=args.recovery_timeout_seconds,
        failure_message="idle unload recovery cycle timed out",
        heartbeat=monitor.raise_if_failed,
    )
    assert isinstance(proof, RuntimeRecoveryProof)
    evidence.write(
        {
            "event": "idle_recovery_proof",
            "timestamp": health.utc_now(),
            "recovering_sequence": proof.recovering_sequence,
            "normal_sequence": proof.normal_sequence,
            "complete_health_polls": proof.complete_health_polls,
            "elapsed_seconds": round(proof.elapsed_seconds, 3),
            "recovery_reason": "idle_cleanup",
            "admission_reopened": True,
        }
    )

    scanner.scan()
    unload_before_reload = scanner.unload_count
    evidence.write(
        {
            "event": "workload_phase",
            "timestamp": health.utc_now(),
            "phase": "post_unload_reload",
        }
    )
    reload_mark = prepare_workload_phase(
        fixtures=fixtures,
        items=(fixtures.reload,),
        scanner=scanner,
        output_watcher=output_watcher,
    )
    post_batch(
        fixtures.reload.container_directory,
        api_key=api_key,
        timeout=args.http_timeout_seconds,
    )
    reload_output = wait_for_transcription_outputs(
        fixtures=fixtures,
        items=(fixtures.reload,),
        scanner=scanner,
        monitor=monitor,
        output_watcher=output_watcher,
        phase_mark=reload_mark,
        timeout=args.reload_timeout_seconds,
    )
    scanner.scan()
    if scanner.unload_count != unload_before_reload:
        raise _safe_code("model unloaded before reload workload completed")
    evidence.write(_output_evidence("post_unload_reload", reload_output))
    attest_originals_unchanged(fixtures, originals)

    monitor.set_phase(allow_idle_recovery=True)
    evidence.write(
        {
            "event": "workload_phase",
            "timestamp": health.utc_now(),
            "phase": "retention_controls",
        }
    )
    invalid_mark = prepare_workload_phase(
        fixtures=fixtures,
        items=(fixtures.invalid,),
        scanner=scanner,
        output_watcher=output_watcher,
    )
    post_batch(
        fixtures.invalid.container_directory,
        api_key=api_key,
        timeout=args.http_timeout_seconds,
    )

    def invalid_observed() -> bool:
        scanner.scan()
        scanner.assert_no_worker_errors(all_workload_paths)
        if any(
            event.get("path") != fixtures.invalid.container_media
            for event in scanner.events_after(invalid_mark.event_sequence)
        ):
            raise _safe_code("invalid control observed an unrelated fixture event")
        matches = scanner.matching_events(
            "media_validation_failed",
            [fixtures.invalid.container_media],
            after_mark=invalid_mark.event_sequence,
        )
        return any(
            event.get("failure_class") == "invalid_media"
            and event.get("validator_outcomes")
            == {"ffprobe": "invalid_format", "pyav": "invalid_format"}
            for event in matches
        )

    wait_until(
        invalid_observed,
        timeout=args.retention_timeout_seconds,
        failure_message="dual-invalid control was not observed",
        heartbeat=monitor.raise_if_failed,
    )
    if (
        snapshot_fixture_file(fixtures, fixtures.invalid.media_relative)
        != originals[fixtures.invalid.media_relative]
    ):
        raise _safe_code("dual-invalid control was deleted or changed")
    assert_fixture_directory_exact(fixtures, "invalid", allow_outputs=False)
    evidence.write(
        {
            "event": "retention_proof",
            "timestamp": health.utc_now(),
            "role": "dual_invalid",
            "retained": True,
            "source_sha256": originals[fixtures.invalid.media_relative].sha256,
            "subtitle_created": False,
            "deletion_controls": "off",
        }
    )

    silent_mark = prepare_workload_phase(
        fixtures=fixtures,
        items=(fixtures.silent,),
        scanner=scanner,
        output_watcher=output_watcher,
    )
    post_batch(
        fixtures.silent.container_directory,
        api_key=api_key,
        timeout=args.http_timeout_seconds,
    )

    def silent_observed() -> bool:
        scanner.scan()
        scanner.assert_no_worker_errors(all_workload_paths)
        if any(
            event.get("path") != fixtures.silent.container_media
            for event in scanner.events_after(silent_mark.event_sequence)
        ):
            raise _safe_code("silent control observed an unrelated fixture event")
        return scanner.silent_after(
            silent_mark.event_sequence,
            fixtures.silent.container_media,
        )

    wait_until(
        silent_observed,
        timeout=args.retention_timeout_seconds,
        failure_message="silent media control was not observed",
        heartbeat=monitor.raise_if_failed,
    )
    if (
        snapshot_fixture_file(fixtures, fixtures.silent.media_relative)
        != originals[fixtures.silent.media_relative]
    ):
        raise _safe_code("silent media control was deleted or changed")
    assert_fixture_directory_exact(fixtures, "silent", allow_outputs=False)
    evidence.write(
        {
            "event": "retention_proof",
            "timestamp": health.utc_now(),
            "role": "silent_media",
            "retained": True,
            "source_sha256": originals[fixtures.silent.media_relative].sha256,
            "subtitle_created": False,
            "deletion_controls": "off",
        }
    )
    attest_originals_unchanged(fixtures, originals)
    scanner.scan()
    scanner.assert_no_worker_errors(all_workload_paths)
    for directory in sorted(EXPECTED_FIXTURE_DIRECTORIES):
        allow_outputs = directory in {"long", "short", "reload"}
        assert_fixture_directory_exact(fixtures, directory, allow_outputs=allow_outputs)
    output_watcher.assert_complete()


def _stop_and_validate(
    *,
    args: argparse.Namespace,
    client: health.DockerClient,
    candidate: health.CandidateBinding,
    frigate: health.ObservedBinding,
    logs: health.IncrementalLogScanner,
    baselines: HealthBaselines,
) -> dict[str, Any]:
    outcome = health.stop_bound_candidate(client, candidate, args)
    item = health.candidate_state(client, candidate, args)
    frigate_state = health.observed_state(client, frigate)
    if item["running"] is not False or item["status"] not in {"exited", "dead"}:
        raise _safe_code("candidate stop completion state was invalid")
    if (
        item["oom_killed"] is not False
        or item["restart_count"] != baselines.candidate_restart_count
    ):
        raise _safe_code("candidate health changed during cleanup")
    if (
        frigate_state["running"] is not True
        or frigate_state["health"] != "healthy"
        or frigate_state["restart_count"] != baselines.frigate_restart_count
    ):
        raise _safe_code("Frigate changed during candidate cleanup")
    end_wall = time.time()
    logs.scan(end_wall)
    outcome["completion"] = {
        "candidate": item,
        "frigate": frigate_state,
        "logs_drained_through_wall": round(end_wall, 6),
    }
    return outcome


def drain_workload_events_after_stop(
    scanner: RuntimeEventScanner, items: Iterable[FixtureItem]
) -> None:
    """Drain structured events only after Docker has confirmed process exit."""
    scanner.scan(time.time())
    scanner.assert_no_worker_errors(item.container_media for item in items)


def run_observer(args: argparse.Namespace) -> int:
    observer_sha256, sampler_sha256 = _verified_runtime_identities(args)
    started_wall = time.time()
    client = health.DockerClient(
        args.expected_docker_daemon_id, args.expected_host_boot_id
    )
    candidate: health.CandidateBinding | None = None
    frigate: health.ObservedBinding | None = None
    evidence: LockedEvidence | None = None
    monitor: HealthMonitor | None = None
    output_watcher: AtomicOutputWatcher | None = None
    logs: health.IncrementalLogScanner | None = None
    baselines: HealthBaselines | None = None
    cleanup: dict[str, Any] | None = None
    prior_handlers: dict[int, Any] = {}
    passed = False
    failure: BaseException | None = None
    try:
        prior_handlers = health.install_signal_handlers()
        _verify_adjacent_frozen_sampler()
        boundary = health.ensure_boundary_expectation(args)
        candidate = health.bind_candidate(client, args)
        candidate_item = client.inspect(candidate.container_id)
        if (
            not isinstance(candidate_item, dict)
            or candidate_item.get("Id") != candidate.container_id
        ):
            raise _safe_code("runtime chunk policy candidate identity changed")
        chunk_policy = validate_runtime_chunk_policy(
            candidate_item,
            expected_chunk_minutes=args.expected_chunk_minutes,
        )
        fixtures = load_fixture_manifest(args.fixture_manifest, boundary)
        api_key = _read_api_key(args.api_key_file)
        validate_request_isolation(candidate_item, api_key)
        assert_outputs_absent(fixtures)
        originals = capture_original_snapshots(fixtures)
        durations = validate_fixture_durations(fixtures)
        output_watcher = AtomicOutputWatcher(fixtures)
        camera_expectations = health.load_camera_expectations(args.camera_expectations)
        daemon_digest, boot_digest = client.verify_local_daemon()
        frigate = health.bind_observed_container(client, args.frigate_container)
        frigate_before = health.observed_state(client, frigate)
        if (
            frigate_before["running"] is not True
            or frigate_before["health"] != "healthy"
        ):
            raise _safe_code("Frigate was unhealthy before runtime workload")
        writer = health.EvidenceWriter.open(args.output, candidate.gate_token_digest)
        evidence = LockedEvidence(
            writer,
            (
                args.gate_token,
                str(fixtures.media_root),
                str(args.fixture_manifest),
                str(args.api_key_file) if args.api_key_file else "",
                api_key or "",
                *camera_expectations.keys(),
            ),
        )
        logs = health.IncrementalLogScanner(client, candidate, frigate, started_wall)
        workload_logs = RuntimeEventScanner(
            client,
            candidate,
            started_wall,
            (item.container_media for item in fixtures.all_items),
        )
        evidence.write(
            {
                "event": "observer_start",
                "timestamp": health.utc_now(),
                "schema": OBSERVER_SCHEMA,
                "observer_sha256": observer_sha256,
                "sampler_sha256": sampler_sha256,
                "fixture_manifest_sha256": fixtures.manifest_sha256,
                "candidate_container_id_sha256": health.sha256_bytes(
                    candidate.container_id.encode("ascii")
                ),
                "candidate_image_config": candidate.image_config,
                "candidate_command_sha256": candidate.command_digest,
                "candidate_boundary_sha256": candidate.boundary_digest,
                "boundary_manifest_sha256": boundary.file_sha256,
                "runtime_commit": candidate.runtime_commit,
                "gate_role": candidate.gate_role,
                "gate_token_sha256": candidate.gate_token_digest,
                "docker_daemon_id_sha256": daemon_digest,
                "host_boot_id_sha256": boot_digest,
                "expected_memory_bytes": args.expected_memory_bytes,
                "gpu_free_floor_bytes": args.gpu_free_floor_bytes,
                "host_reserve_bytes": args.host_reserve_bytes,
                "minimum_observation_seconds": args.duration_seconds,
                "interval_seconds": args.interval_seconds,
                "camera_count": len(camera_expectations),
                "fixture_durations": durations,
                "runtime_chunk_policy": chunk_policy,
                "deletion_controls": "explicitly_off_and_boundary_bound",
            }
        )
        health.start_bound_candidate(client, candidate, args)
        initial_candidate = health.wait_for_running(
            client,
            candidate,
            args,
            time.monotonic() + args.start_timeout_seconds,
        )
        initial_resource = health.wait_for_runtime_ready(
            args, time.monotonic() + args.start_timeout_seconds
        )
        initial_memory = health.candidate_memory(client, candidate)
        health.validate_candidate_memory_snapshot(
            initial_memory, expected_memory_bytes=args.expected_memory_bytes
        )
        frigate_initial = health.observed_state(client, frigate)
        if (
            frigate_initial["running"] is not True
            or frigate_initial["health"] != "healthy"
        ):
            raise _safe_code("Frigate was unhealthy at runtime start")
        baselines = HealthBaselines(
            candidate_restart_count=initial_candidate["restart_count"],
            frigate_restart_count=frigate_initial["restart_count"],
        )
        tracker = RuntimeCycleTracker()
        observation_started = time.monotonic()
        monitor = HealthMonitor(
            args=args,
            evidence=evidence,
            client=client,
            candidate=candidate,
            frigate=frigate,
            expectations=camera_expectations,
            baselines=baselines,
            logs=logs,
            tracker=tracker,
            output_watcher=output_watcher,
            started_monotonic=observation_started,
        )
        evidence.write(
            {
                "event": "candidate_ready",
                "timestamp": health.utc_now(),
                "candidate_state": initial_candidate,
                "candidate_resource": initial_resource,
                "candidate_memory": initial_memory,
                "frigate_state": frigate_initial,
            }
        )
        monitor.start()
        _run_composite_workload(
            args=args,
            fixtures=fixtures,
            originals=originals,
            api_key=api_key,
            scanner=workload_logs,
            monitor=monitor,
            tracker=tracker,
            output_watcher=output_watcher,
            evidence=evidence,
        )
        monitor.wait_for_duration()
        attest_originals_unchanged(fixtures, originals)
        wait_until(
            tracker.latest_is_healthy,
            timeout=args.recovery_timeout_seconds,
            failure_message="runtime did not finish in normal open state",
            heartbeat=monitor.raise_if_failed,
        )
        monitor.set_phase(allow_idle_recovery=False)
        monitor.stop_and_join()
        monitor.collect(final=True)
        workload_logs.scan()
        workload_logs.assert_no_worker_errors(
            item.container_media for item in fixtures.all_items
        )
        cleanup = _stop_and_validate(
            args=args,
            client=client,
            candidate=candidate,
            frigate=frigate,
            logs=logs,
            baselines=baselines,
        )
        drain_workload_events_after_stop(workload_logs, fixtures.all_items)
        output_watcher.assert_complete()
        output_watcher.close()
        output_watcher = None
        evidence.write(
            {
                "event": "observer_pass",
                "timestamp": health.utc_now(),
                "continuous_seconds": round(monitor.last_elapsed, 3),
                "health_samples": monitor.sample_count,
                "observer_sha256": observer_sha256,
                "sampler_sha256": sampler_sha256,
                "cleanup": cleanup,
            }
        )
        evidence.seal(
            outcome="pass",
            sampler_sha256=sampler_sha256,
            image_config=candidate.image_config,
            cleanup=cleanup,
        )
        passed = True
    except BaseException as exc:
        failure = exc
    finally:
        if monitor is not None and not passed:
            try:
                monitor.stop_and_join()
            except BaseException as monitor_exc:
                if failure is None:
                    failure = monitor_exc
        if not passed and candidate is not None:
            try:
                cleanup = health.stop_bound_candidate(client, candidate, args)
            except BaseException as cleanup_exc:
                cleanup = {
                    "verified_stopped": False,
                    "error": health.safe_reason(cleanup_exc),
                }
                failure = _safe_code(
                    f"{health.safe_reason(failure or _safe_code('observer failure'))} cleanup unverified"
                )
        if output_watcher is not None:
            try:
                output_watcher.close()
            except BaseException as watcher_exc:
                if failure is None:
                    failure = watcher_exc
        if not passed and evidence is not None and not evidence.closed:
            try:
                evidence.write(
                    {
                        "event": "observer_abort",
                        "timestamp": health.utc_now(),
                        "reason": health.safe_reason(
                            failure or _safe_code("observer failure")
                        ),
                        "elapsed_wall_seconds": round(time.time() - started_wall, 3),
                        "observer_sha256": observer_sha256,
                        "sampler_sha256": sampler_sha256,
                        "cleanup": cleanup,
                    }
                )
                evidence.seal(
                    outcome="abort",
                    sampler_sha256=sampler_sha256,
                    image_config=candidate.image_config if candidate else "unbound",
                    cleanup=cleanup or {"verified_stopped": False},
                )
            except BaseException:
                pass
        if evidence is not None:
            evidence.close()
        if prior_handlers:
            health.restore_signal_handlers(prior_handlers)
    if not passed:
        if isinstance(failure, health.GateAbort):
            raise failure
        raise _safe_code(
            health.safe_reason(failure or _safe_code("observer failure"))
        ) from failure
    assert candidate is not None and monitor is not None
    print(
        "TASK11B_RUNTIME_WORKLOAD_PASS "
        f"container_id_sha256={health.sha256_bytes(candidate.container_id.encode('ascii'))} "
        f"seconds={round(monitor.last_elapsed, 3)} samples={monitor.sample_count}"
    )
    return 0


def _create_verified_runtime_bundle(script_path: Path) -> tuple[Path, Path]:
    observer_payload = _BOOTSTRAPPED_OBSERVER_PAYLOAD
    sampler_payload = _BOOTSTRAPPED_SAMPLER_PAYLOAD
    if observer_payload is None or sampler_payload is None:
        raise _safe_code("verified runtime source payloads were unavailable")
    bundle = script_path.with_name(script_path.name + ".runtime")
    if any(character.isspace() for character in str(bundle)):
        raise _safe_code("supervisor artifact path contained whitespace")
    try:
        parent = bundle.parent.resolve(strict=True)
        parent_stat = parent.stat()
    except OSError as exc:
        raise _safe_code("supervisor artifact parent was unavailable") from exc
    if (
        parent != bundle.parent.absolute()
        or parent_stat.st_uid != os.geteuid()
        or parent_stat.st_mode & 0o077
    ):
        raise _safe_code("supervisor artifact parent was not owner only")
    try:
        os.mkdir(bundle, 0o700)
    except OSError as exc:
        raise _safe_code("supervisor runtime bundle already existed or failed") from exc
    observer_snapshot = bundle / "runtime_gate_observer.py"
    sampler_snapshot = bundle / "gate_health_sampler.py"
    try:
        item = bundle.lstat()
        if (
            stat.S_ISLNK(item.st_mode)
            or not stat.S_ISDIR(item.st_mode)
            or item.st_uid != os.geteuid()
            or stat.S_IMODE(item.st_mode) != 0o700
        ):
            raise _safe_code("supervisor runtime bundle was unsafe")
        health._write_private_create_only(observer_snapshot, observer_payload, 0o600)
        health._write_private_create_only(sampler_snapshot, sampler_payload, 0o600)
        if (
            hashlib.sha256(observer_payload).hexdigest()
            != _BOOTSTRAPPED_OBSERVER_SHA256
            or hashlib.sha256(sampler_payload).hexdigest()
            != _BOOTSTRAPPED_SAMPLER_SHA256
        ):
            raise _safe_code("supervisor runtime bundle identity changed")
        return observer_snapshot, sampler_snapshot
    except BaseException:
        for created in (observer_snapshot, sampler_snapshot):
            try:
                created.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            bundle.rmdir()
        except OSError:
            pass
        raise


def _remove_verified_runtime_bundle(observer_snapshot: Path) -> None:
    bundle = observer_snapshot.parent
    expected = {
        bundle / "runtime_gate_observer.py",
        bundle / "gate_health_sampler.py",
    }
    if (
        observer_snapshot.name != "runtime_gate_observer.py"
        or not bundle.name.endswith(".runtime")
        or observer_snapshot not in expected
    ):
        raise _safe_code("supervisor runtime bundle cleanup target was invalid")
    for target in expected:
        target.unlink(missing_ok=True)
    bundle.rmdir()


def _cleanup_command(args: argparse.Namespace, sampler_path: Path) -> list[str]:
    command = [
        str(Path(sys.executable).resolve()),
        str(sampler_path.resolve(strict=True)),
        *health._gate_cli_arguments(args),
        "--cleanup-only",
        "--systemd-stop-post",
    ]
    if any(not re.fullmatch(r"[A-Za-z0-9_./:=@+,%~-]+", part) for part in command):
        raise _safe_code("cleanup supervisor command contained unsafe characters")
    return command


def _cleanup_command_sha256(command: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(command, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _parse_exec_stop_post(value: str) -> dict[str, str]:
    """Parse one bounded systemd ExecStopPost command, never a command list."""
    if (
        not isinstance(value, str)
        or not value.startswith("{ ")
        or not value.endswith(" }")
        or value.count("{") != 1
        or value.count("}") != 1
        or len(value) > 64 * 1024
        or any(ord(character) < 32 or ord(character) > 126 for character in value)
    ):
        raise _safe_code("runtime supervisor cleanup command was malformed")
    body = value[2:-2]
    if body.endswith(" ;"):
        body = body[:-2]
    if not body:
        raise _safe_code("runtime supervisor cleanup command was empty")
    allowed = {
        "path",
        "argv[]",
        "ignore_errors",
        "start_time",
        "stop_time",
        "pid",
        "code",
        "status",
    }
    fields: dict[str, str] = {}
    for field in body.split(" ; "):
        if "=" not in field:
            raise _safe_code("runtime supervisor cleanup command was malformed")
        key, item = field.split("=", 1)
        if key not in allowed or key in fields or not item:
            raise _safe_code("runtime supervisor cleanup command was malformed")
        fields[key] = item
    if not {"path", "argv[]", "ignore_errors"}.issubset(fields):
        raise _safe_code("runtime supervisor cleanup command was incomplete")
    return fields


def _verify_exec_stop_post(value: str, cleanup: list[str]) -> None:
    fields = _parse_exec_stop_post(value)
    if (
        not cleanup
        or fields["path"] != cleanup[0]
        or fields["argv[]"] != " ".join(cleanup)
        or fields["ignore_errors"] != "no"
    ):
        raise _safe_code("runtime supervisor cleanup binding was not exact")


def verify_systemd_supervisor(args: argparse.Namespace) -> None:
    """Prove this PID is the generated unit with the exact cleanup command."""
    unit = args.expected_systemd_unit
    expected_unit = (
        "subgen-task11b-runtime-"
        + hashlib.sha256(args.gate_token.encode("utf-8")).hexdigest()[:16]
    )
    if unit != expected_unit:
        raise _safe_code("runtime supervisor unit identity was invalid")
    invocation_id = os.environ.get("INVOCATION_ID", "")
    systemd_exec_pid = os.environ.get("SYSTEMD_EXEC_PID", "")
    cleanup_path = Path(__file__).with_name("gate_health_sampler.py")
    cleanup = _cleanup_command(args, cleanup_path)
    cleanup_digest = _cleanup_command_sha256(cleanup)
    if (
        not re.fullmatch(r"[0-9a-f]{32}", invocation_id)
        or systemd_exec_pid != str(os.getpid())
        or os.environ.get("TASK11B_CLEANUP_COMMAND_SHA256") != cleanup_digest
    ):
        raise _safe_code("runtime supervisor process environment was invalid")

    try:
        descriptor = os.open(
            "/proc/self/cgroup",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise _safe_code("runtime supervisor cgroup was unavailable") from exc
    try:
        cgroup_payload = _read_all_fd(
            descriptor, 8 * 1024, label="runtime supervisor cgroup"
        )
    finally:
        os.close(descriptor)
    try:
        cgroup_lines = cgroup_payload.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise _safe_code("runtime supervisor cgroup was malformed") from exc
    expected_cgroup = f"/system.slice/{unit}.service"
    if cgroup_lines != [f"0::{expected_cgroup}"]:
        raise _safe_code("runtime process was outside its supervisor cgroup")

    result = health.bounded_command(
        [
            "/usr/bin/systemctl",
            "show",
            f"{unit}.service",
            "--no-pager",
            "--property=InvocationID",
            "--property=ControlGroup",
            "--property=MainPID",
            "--property=ActiveState",
            "--property=ExecStopPost",
        ],
        label="runtime supervisor inspection",
        timeout=10,
        max_bytes=64 * 1024,
    )
    properties: dict[str, str] = {}
    for line in result.output.splitlines():
        if "=" not in line:
            raise _safe_code("runtime supervisor inspection was malformed")
        key, value = line.split("=", 1)
        if key in properties:
            raise _safe_code("runtime supervisor inspection was duplicated")
        properties[key] = value
    if set(properties) != {
        "InvocationID",
        "ControlGroup",
        "MainPID",
        "ActiveState",
        "ExecStopPost",
    }:
        raise _safe_code("runtime supervisor inspection was incomplete")
    if (
        properties["InvocationID"] != invocation_id
        or properties["ControlGroup"] != expected_cgroup
        or properties["MainPID"] != str(os.getpid())
        or properties["ActiveState"] != "active"
    ):
        raise _safe_code("runtime supervisor cleanup binding was not exact")
    _verify_exec_stop_post(properties["ExecStopPost"], cleanup)


def _observer_cli_arguments(
    args: argparse.Namespace, *, supervisor_armed: bool = False
) -> list[str]:
    pairs: list[tuple[str, Any]] = [
        ("--duration-seconds", args.duration_seconds),
        ("--interval-seconds", args.interval_seconds),
        ("--start-timeout-seconds", args.start_timeout_seconds),
        ("--long-timeout-seconds", args.long_timeout_seconds),
        ("--short-timeout-seconds", args.short_timeout_seconds),
        ("--recovery-timeout-seconds", args.recovery_timeout_seconds),
        ("--reload-timeout-seconds", args.reload_timeout_seconds),
        ("--retention-timeout-seconds", args.retention_timeout_seconds),
        ("--http-timeout-seconds", args.http_timeout_seconds),
        ("--expected-memory-bytes", args.expected_memory_bytes),
        ("--expected-chunk-minutes", args.expected_chunk_minutes),
        ("--gpu-free-floor-bytes", args.gpu_free_floor_bytes),
        ("--host-reserve-bytes", args.host_reserve_bytes),
        ("--frigate-container", args.frigate_container),
        ("--frigate-stats-url", args.frigate_stats_url),
        ("--ollama-url", args.ollama_url),
        ("--candidate-status-url", args.candidate_status_url),
        ("--candidate-mode", args.candidate_mode),
        ("--expected-model", args.expected_model),
        ("--expected-container-id", args.expected_container_id),
        ("--expected-image-config", args.expected_image_config),
        ("--expected-command-sha256", args.expected_command_sha256),
        ("--runtime-commit", args.runtime_commit),
        ("--gate-token", args.gate_token),
        ("--gate-role", args.gate_role),
        ("--camera-expectations", args.camera_expectations),
        ("--sampler-sha256", args.sampler_sha256),
        ("--observer-sha256", args.observer_sha256),
        ("--expected-docker-daemon-id", args.expected_docker_daemon_id),
        ("--expected-host-boot-id", args.expected_host_boot_id),
        ("--boundary-manifest", args.boundary_manifest),
        ("--boundary-manifest-sha256", args.boundary_manifest_sha256),
        ("--disposable-root", args.disposable_root),
        ("--fixture-manifest", args.fixture_manifest),
    ]
    if args.expected_systemd_unit is not None:
        pairs.append(("--expected-systemd-unit", args.expected_systemd_unit))
    if args.api_key_file is not None:
        pairs.append(("--api-key-file", args.api_key_file))
    result = [str(args.container), str(args.output)]
    for option, value in pairs:
        result.extend((option, str(value)))
    if supervisor_armed:
        result.append("--supervisor-armed")
    return result


def emit_systemd_run_script(args: argparse.Namespace) -> int:
    """Create a wrapper whose ExecStopPost is the frozen sampler cleanup."""
    _verified_runtime_identities(args)
    _verify_adjacent_frozen_sampler()
    health.ensure_boundary_expectation(args)
    client = health.DockerClient(
        args.expected_docker_daemon_id, args.expected_host_boot_id
    )
    binding = health.bind_candidate(client, args)
    candidate_item = client.inspect(binding.container_id)
    if (
        not isinstance(candidate_item, dict)
        or candidate_item.get("Id") != binding.container_id
    ):
        raise _safe_code("runtime chunk policy candidate identity changed")
    validate_runtime_chunk_policy(
        candidate_item,
        expected_chunk_minutes=args.expected_chunk_minutes,
    )
    validate_request_isolation(candidate_item, _read_api_key(args.api_key_file))
    unit = f"subgen-task11b-runtime-{binding.gate_token_digest[:16]}"
    observer_snapshot, sampler_snapshot = _create_verified_runtime_bundle(
        args.emit_systemd_run_script
    )
    try:
        worker_args = copy.copy(args)
        worker_args.expected_systemd_unit = unit
        worker = [
            str(Path(sys.executable).resolve()),
            str(observer_snapshot),
            *_observer_cli_arguments(worker_args, supervisor_armed=True),
        ]
        cleanup = _cleanup_command(args, sampler_snapshot)
        cleanup_digest = _cleanup_command_sha256(cleanup)
        cleanup_property = "ExecStopPost=" + " ".join(
            health._systemd_quote(part) for part in cleanup
        )
        runtime_max = int(
            args.start_timeout_seconds
            + args.long_timeout_seconds
            + args.short_timeout_seconds
            + args.recovery_timeout_seconds
            + args.reload_timeout_seconds
            + 2 * args.retention_timeout_seconds
            + args.duration_seconds
            + 600
        )
        command = [
            "/usr/bin/systemd-run",
            f"--unit={unit}",
            "--collect",
            "--wait",
            "--service-type=exec",
            f"--setenv=TASK11B_CLEANUP_COMMAND_SHA256={cleanup_digest}",
            "--property=User=root",
            "--property=Group=root",
            "--property=UMask=0077",
            "--property=WorkingDirectory=/",
            "--property=StandardInput=null",
            "--property=NoNewPrivileges=yes",
            "--property=Restart=no",
            "--property=KillMode=mixed",
            "--property=SendSIGKILL=yes",
            "--property=TimeoutStopSec=300s",
            f"--property=RuntimeMaxSec={runtime_max}s",
            f"--property={cleanup_property}",
            "--",
            *worker,
        ]
        script = ("#!/bin/sh\nset -eu\nexec " + shlex.join(command) + "\n").encode(
            "utf-8"
        )
        health._write_private_create_only(args.emit_systemd_run_script, script, 0o700)
    except BaseException:
        _remove_verified_runtime_bundle(observer_snapshot)
        raise
    print(
        "TASK11B_RUNTIME_SUPERVISOR_READY "
        f"unit={unit} script_sha256={health.sha256_bytes(script)}"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("container", nargs="?")
    result.add_argument("output", nargs="?", type=Path)
    result.add_argument("--duration-seconds", type=int, default=900)
    result.add_argument("--interval-seconds", type=int, default=5)
    result.add_argument("--start-timeout-seconds", type=int, default=120)
    result.add_argument("--long-timeout-seconds", type=int, default=7200)
    result.add_argument("--short-timeout-seconds", type=int, default=1800)
    result.add_argument("--recovery-timeout-seconds", type=int, default=300)
    result.add_argument("--reload-timeout-seconds", type=int, default=1800)
    result.add_argument("--retention-timeout-seconds", type=int, default=120)
    result.add_argument("--http-timeout-seconds", type=float, default=10.0)
    result.add_argument("--expected-memory-bytes", type=int)
    result.add_argument("--expected-chunk-minutes", type=int)
    result.add_argument("--gpu-free-floor-bytes", type=int, default=8 * GIB)
    result.add_argument("--host-reserve-bytes", type=int, default=4 * GIB)
    result.add_argument("--frigate-container", default="frigate")
    result.add_argument("--frigate-stats-url", default=DEFAULT_FRIGATE_STATS_URL)
    result.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    result.add_argument("--candidate-status-url", default=DEFAULT_CANDIDATE_STATUS_URL)
    result.add_argument("--candidate-mode", choices=("runtime",), default="runtime")
    result.add_argument("--expected-model")
    result.add_argument("--expected-container-id")
    result.add_argument("--expected-image-config")
    result.add_argument("--expected-command-sha256")
    result.add_argument("--runtime-commit")
    result.add_argument("--gate-token")
    result.add_argument("--gate-role")
    result.add_argument("--camera-expectations", type=Path)
    result.add_argument("--sampler-sha256")
    result.add_argument("--observer-sha256")
    result.add_argument("--expected-docker-daemon-id")
    result.add_argument("--expected-host-boot-id")
    result.add_argument("--boundary-manifest", type=Path)
    result.add_argument("--boundary-manifest-sha256")
    result.add_argument("--disposable-root")
    result.add_argument("--fixture-manifest", type=Path)
    result.add_argument("--api-key-file", type=Path)
    result.add_argument("--emit-systemd-run-script", type=Path)
    result.add_argument("--expected-systemd-unit", help=argparse.SUPPRESS)
    result.add_argument(
        "--supervisor-armed", action="store_true", help=argparse.SUPPRESS
    )
    return result


def validate_args(args: argparse.Namespace) -> None:
    # Supply the sampler-only fields its exact runtime validator expects.
    args.expected_profiler_returncode = None
    args.leave_running_on_pass = False
    args.cleanup_only = False
    args.systemd_stop_post = False
    args.emit_boundary_manifest = None
    health.validate_args(args)
    if (
        args.fixture_manifest is None
        or args.api_key_file is None
        or args.observer_sha256 is None
    ):
        raise _safe_code("missing runtime observer arguments")
    if not SHA256_RE.fullmatch(args.observer_sha256):
        raise _safe_code("observer checksum must be SHA256")
    if (
        isinstance(args.expected_chunk_minutes, bool)
        or not isinstance(args.expected_chunk_minutes, int)
        or not 5 <= args.expected_chunk_minutes <= 30
    ):
        raise _safe_code("expected runtime chunk policy was outside boundary")
    bounds = (
        (args.long_timeout_seconds, 600, 10800, "long timeout"),
        (args.short_timeout_seconds, 60, 3600, "short timeout"),
        (args.recovery_timeout_seconds, 30, 900, "recovery timeout"),
        (args.reload_timeout_seconds, 60, 3600, "reload timeout"),
        (args.retention_timeout_seconds, 30, 600, "retention timeout"),
    )
    for value, minimum, maximum, label in bounds:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise _safe_code(f"{label} was outside safety boundary")
    if not 1.0 <= args.http_timeout_seconds <= 30.0:
        raise _safe_code("HTTP timeout was outside safety boundary")
    if (
        args.emit_systemd_run_script is not None
        and not args.emit_systemd_run_script.is_absolute()
    ):
        raise _safe_code("supervisor script path must be absolute")
    if args.emit_systemd_run_script is not None:
        if args.expected_systemd_unit is not None or args.supervisor_armed:
            raise _safe_code("supervisor generation arguments were inconsistent")
    elif (
        not args.supervisor_armed
        or not isinstance(args.expected_systemd_unit, str)
        or not re.fullmatch(
            r"subgen-task11b-runtime-[0-9a-f]{16}", args.expected_systemd_unit
        )
    ):
        raise _safe_code("runtime observer requires its frozen cleanup supervisor")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    _bootstrap_verified_runtime(arguments)
    args = parser().parse_args(arguments)
    validate_args(args)
    if args.emit_systemd_run_script is not None:
        return emit_systemd_run_script(args)
    verify_systemd_supervisor(args)
    return run_observer(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException as exc:
        gate_abort = health is not None and isinstance(exc, health.GateAbort)
        if isinstance(exc, ObserverBootstrapAbort) or gate_abort:
            print(f"TASK11B_RUNTIME_WORKLOAD_ABORT reason={exc.code}", file=sys.stderr)
            raise SystemExit(1) from exc
        raise
