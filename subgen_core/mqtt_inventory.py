"""Optional MQTT inventory reporting and a startup-scan admission barrier.

The MQTT surface is deliberately diagnostic-only: broker failures are contained
inside this module and can never become transcription failures.  Full media paths
are used only as one-way hashes for in-process accounting and are never published.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
import posixpath
import re
import threading
import time
from typing import Callable, Mapping, Optional, Sequence


DEFAULT_REFRESH_SECONDS = 60.0
DEFAULT_SCAN_TIMEOUT_SECONDS = 6 * 60 * 60.0
DEFAULT_SCAN_EVENT_DRAIN_SECONDS = 30.0
MAX_LABEL_LENGTH = 80
_TOPIC_RE = re.compile(r"[A-Za-z0-9_.\-/]+\Z")
_NODE_RE = re.compile(r"[a-z0-9_\-]+\Z")


def _environment_bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("MQTT_INVENTORY_ENABLED must be a boolean")


def _safe_label(value: object, *, fallback: str = "Library") -> str:
    try:
        text = str(value)
    except Exception:
        text = fallback
    cleaned = "".join(
        " " if ord(character) < 32 or ord(character) == 127 else character
        for character in text
    )
    cleaned = " ".join(cleaned.split()).strip()
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > MAX_LABEL_LENGTH:
        cleaned = f"{cleaned[: MAX_LABEL_LENGTH - 1]}…"
    return cleaned


def _unique_library_label(base: str, used: set[str]) -> str:
    candidate = _safe_label(base)
    suffix_number = 2
    while candidate in used:
        suffix = f" ({suffix_number})"
        available = max(1, MAX_LABEL_LENGTH - len(suffix))
        candidate = _safe_label(f"{base[:available]}{suffix}")
        suffix_number += 1
    used.add(candidate)
    return candidate


def _normalized_path(path: object) -> Optional[str]:
    try:
        text = str(path).strip().replace("\\", "/")
    except Exception:
        return None
    if not text or "\x00" in text:
        return None
    normalized = posixpath.normpath(text)
    return normalized if normalized not in {"", "."} else None


def _path_key(path: object) -> Optional[bytes]:
    normalized = _normalized_path(path)
    if normalized is None:
        return None
    return sha256(normalized.encode("utf-8", errors="surrogatepass")).digest()


def _safe_topic(value: object, *, name: str) -> str:
    text = str(value).strip().strip("/")
    if (
        not text
        or len(text) > 256
        or not text.isascii()
        or _TOPIC_RE.fullmatch(text) is None
        or "//" in text
        or "+" in text
        or "#" in text
    ):
        raise ValueError(f"{name} is not a safe MQTT topic prefix")
    return text


def _safe_node_id(value: object) -> str:
    text = str(value).strip().casefold()
    if not text or len(text) > 64 or _NODE_RE.fullmatch(text) is None:
        raise ValueError("MQTT_INVENTORY_NODE_ID is invalid")
    return text


@dataclass(frozen=True, slots=True)
class MqttInventoryConfig:
    """Validated optional MQTT configuration; secrets are excluded from repr."""

    enabled: bool = False
    host: str = ""
    port: int = 1883
    username: Optional[str] = field(default=None, repr=False)
    password: Optional[str] = field(default=None, repr=False)
    client_id: str = "subgen-inventory"
    topic_prefix: str = "subgen"
    discovery_prefix: str = "homeassistant"
    node_id: str = "subgen_inventory"
    library_names: tuple[str, ...] = ()
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS
    scan_timeout_seconds: float = DEFAULT_SCAN_TIMEOUT_SECONDS

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "MqttInventoryConfig":
        enabled = _environment_bool(environment.get("MQTT_INVENTORY_ENABLED"))
        if not enabled:
            return cls()

        host = str(environment.get("MQTT_HOST", "")).strip()
        if not host or len(host) > 253 or any(ord(char) < 33 for char in host):
            raise ValueError("MQTT_HOST is required when inventory reporting is enabled")
        try:
            port = int(environment.get("MQTT_PORT", "1883"))
        except (TypeError, ValueError) as exc:
            raise ValueError("MQTT_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("MQTT_PORT must be between 1 and 65535")

        try:
            scan_timeout_seconds = float(
                environment.get(
                    "MQTT_INVENTORY_SCAN_TIMEOUT_SECONDS",
                    str(DEFAULT_SCAN_TIMEOUT_SECONDS),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "MQTT_INVENTORY_SCAN_TIMEOUT_SECONDS must be a number"
            ) from exc
        if (
            not math.isfinite(scan_timeout_seconds)
            or not 60.0 <= scan_timeout_seconds <= 24 * 60 * 60.0
        ):
            raise ValueError(
                "MQTT_INVENTORY_SCAN_TIMEOUT_SECONDS must be between 60 and 86400"
            )

        username = environment.get("MQTT_USERNAME") or None
        password = environment.get("MQTT_PASSWORD") or None
        if password is not None and username is None:
            raise ValueError("MQTT_PASSWORD requires MQTT_USERNAME")
        if username is not None and len(username) > 256:
            raise ValueError("MQTT_USERNAME is too long")
        if password is not None and len(password) > 1024:
            raise ValueError("MQTT_PASSWORD is too long")

        raw_library_names = str(
            environment.get("MQTT_INVENTORY_LIBRARY_NAMES", "")
        )
        if len(raw_library_names) > 4096:
            raise ValueError("MQTT_INVENTORY_LIBRARY_NAMES is too long")
        if raw_library_names:
            parts = raw_library_names.split("|")
            if len(parts) > 128:
                raise ValueError("MQTT_INVENTORY_LIBRARY_NAMES has too many entries")
            library_names = tuple(
                _safe_label(part, fallback=f"Library {index + 1}")
                for index, part in enumerate(parts)
            )
        else:
            library_names = ()

        client_id = str(
            environment.get("MQTT_CLIENT_ID", "subgen-inventory")
        ).strip()
        if (
            not client_id
            or len(client_id) > 64
            or not client_id.isascii()
            or any(not (char.isalnum() or char in "_-") for char in client_id)
        ):
            raise ValueError("MQTT_CLIENT_ID is invalid")

        return cls(
            enabled=True,
            host=host,
            port=port,
            username=username,
            password=password,
            client_id=client_id,
            topic_prefix=_safe_topic(
                environment.get("MQTT_TOPIC_PREFIX", "subgen"),
                name="MQTT_TOPIC_PREFIX",
            ),
            discovery_prefix=_safe_topic(
                environment.get("MQTT_DISCOVERY_PREFIX", "homeassistant"),
                name="MQTT_DISCOVERY_PREFIX",
            ),
            node_id=_safe_node_id(
                environment.get("MQTT_INVENTORY_NODE_ID", "subgen_inventory")
            ),
            library_names=library_names,
            scan_timeout_seconds=scan_timeout_seconds,
        )


def load_mqtt_inventory_config(environment, logger) -> MqttInventoryConfig:
    """Return a disabled config on any optional-feature configuration failure."""

    try:
        return MqttInventoryConfig.from_environment(environment)
    except Exception as exc:
        try:
            logger.warning(
                "MQTT inventory disabled because its configuration is invalid (%s); "
                "transcription will continue.",
                type(exc).__name__,
            )
        except Exception:
            pass
        return MqttInventoryConfig()


@dataclass(frozen=True, slots=True)
class LibraryInventory:
    name: str
    scanned: int
    total: int
    items_left: int


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    items_left: int
    scan_percent: float
    scan_complete: bool
    scan_errors: int
    libraries: tuple[LibraryInventory, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "items_left": self.items_left,
            "scan_percent": self.scan_percent,
            "scan_complete": self.scan_complete,
            "scan_errors": self.scan_errors,
            "libraries": {
                library.name: {
                    "scanned": library.scanned,
                    "total": library.total,
                    "items_left": library.items_left,
                }
                for library in self.libraries
            },
        }


@dataclass(slots=True)
class _MutableLibrary:
    name: str
    total: int = 0
    scanned: int = 0
    items_left: int = 0


@dataclass(frozen=True, slots=True)
class _PendingItem:
    label: str
    counted_at_enqueue: bool
    generation: Optional[int]


class InventoryCoordinator:
    """Thread-safe inventory state plus a fail-open startup-scan barrier."""

    def __init__(self, config: MqttInventoryConfig, logger, *, publisher=None):
        self.config = config
        self.logger = logger
        self._lock = threading.RLock()
        self._publication_lock = threading.Lock()
        self._scan_ingress_condition = threading.Condition(self._lock)
        self._scan_ready = threading.Event()
        self._scan_ready.set()
        self._scan_cancelled = threading.Event()
        self._scan_watchdog: Optional[threading.Timer] = None
        self._scan_generation = 0
        self._scan_ingress_open = False
        self._scan_ingress_inflight: dict[int, int] = {}
        self._scan_layout_signature: tuple[object, ...] = ()
        self._libraries: dict[str, _MutableLibrary] = {}
        self._roots: list[tuple[str, str]] = []
        self._pending_items: dict[bytes, _PendingItem] = {}
        self._unbound_pending_paths: dict[bytes, str] = {}
        self._scan_unvisited_items: dict[str, set[bytes]] = {}
        self._runtime_items_during_scan: dict[bytes, str] = {}
        self._post_cutoff_runtime_items: set[bytes] = set()
        self._post_cutoff_removed_items: set[bytes] = set()
        self._scan_seen_items: set[bytes] = set()
        self._counted_items: dict[bytes, str] = {}
        self._scan_complete = True
        self._scan_finished = True
        self._scan_errors = 0
        self._publisher = publisher or InventoryPublisher(config, logger)

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def start(self) -> None:
        try:
            self._update_publisher(self.snapshot(), urgent=True)
            self._publisher.start()
        except Exception as exc:
            self._warn_nonblocking("start", exc)

    def stop(self) -> None:
        with self._lock:
            self._cancel_watchdog_locked()
            if not self._scan_finished:
                self._scan_cancelled.set()
                self._scan_ingress_open = False
                self._scan_complete = False
                self._scan_finished = True
                self._scan_ready.set()
                self._scan_ingress_condition.notify_all()
        try:
            self._publisher.stop()
        except Exception as exc:
            self._warn_nonblocking("stop", exc)

    def arm_scan(self) -> None:
        timer = None
        with self._lock:
            self._cancel_watchdog_locked()
            self._scan_generation += 1
            self._scan_ingress_open = True
            self._scan_ingress_inflight.setdefault(self._scan_generation, 0)
            self._scan_layout_signature = ()
            self._libraries = {}
            self._roots = []
            self._pending_items = {}
            self._unbound_pending_paths = {}
            self._scan_unvisited_items = {}
            self._runtime_items_during_scan = {}
            self._post_cutoff_runtime_items = set()
            self._post_cutoff_removed_items = set()
            self._scan_seen_items = set()
            self._counted_items = {}
            self._scan_cancelled.clear()
            self._scan_complete = False
            self._scan_finished = False
            self._scan_errors = 0
            self._scan_ready.clear()
            snapshot = self._snapshot_locked()
            if self.config.enabled:
                timer = threading.Timer(
                    self.config.scan_timeout_seconds,
                    self._expire_scan,
                    args=(self._scan_generation,),
                )
                timer.daemon = True
                self._scan_watchdog = timer
        self._update_publisher(snapshot, urgent=True)
        if timer is not None:
            timer.start()

    @property
    def scan_cancelled(self) -> bool:
        return self._scan_cancelled.is_set()

    @property
    def scan_generation(self) -> int:
        with self._lock:
            return self._scan_generation

    def begin_scan(
        self,
        paths: Sequence[object],
        *,
        mapped_paths: Sequence[object] = (),
        library_names: Sequence[object] = (),
    ) -> tuple[str, ...]:
        with self._lock:
            already_armed = not self._scan_finished and not self._scan_ready.is_set()
        if not already_armed and not self._scan_cancelled.is_set():
            self.arm_scan()

        raw_paths = list(paths)
        mapped = list(mapped_paths)[: len(raw_paths)]
        names = list(library_names)[: len(raw_paths)]
        labels: list[str] = []
        used_labels: set[str] = set()
        roots: list[tuple[str, str]] = []
        libraries: dict[str, _MutableLibrary] = {}

        for index, path in enumerate(raw_paths):
            base = names[index] if index < len(names) else f"Library {index + 1}"
            label = _unique_library_label(_safe_label(base), used_labels)
            labels.append(label)
            libraries[label] = _MutableLibrary(label)
            aliases = [path]
            if index < len(mapped):
                aliases.append(mapped[index])
            for alias in aliases:
                normalized = _normalized_path(alias)
                if normalized is not None:
                    roots.append((normalized.rstrip("/"), label))

        sorted_roots = tuple(
            sorted(roots, key=lambda item: len(item[0]), reverse=True)
        )
        layout_signature = (tuple(labels), sorted_roots)
        with self._lock:
            if self._scan_layout_signature != layout_signature:
                self._libraries = libraries
                self._roots = list(sorted_roots)
                self._scan_layout_signature = layout_signature
                for key, pending in tuple(self._pending_items.items()):
                    path_hint = self._unbound_pending_paths.pop(key, None)
                    label = (
                        self._label_for_normalized_path_locked(path_hint)
                        if path_hint is not None
                        else pending.label
                    )
                    if label is None or label not in self._libraries:
                        label = "Other"
                        self._libraries.setdefault(label, _MutableLibrary(label))
                    if label != pending.label:
                        self._pending_items[key] = _PendingItem(
                            label=label,
                            counted_at_enqueue=pending.counted_at_enqueue,
                            generation=pending.generation,
                        )
                    self._libraries[label].items_left += 1
                    if key in self._runtime_items_during_scan:
                        self._runtime_items_during_scan[key] = label
            snapshot = self._snapshot_locked()
        self._update_publisher(snapshot, urgent=True)
        return tuple(labels)

    def set_library_total(
        self, label: str, total: int, *, generation: Optional[int] = None
    ) -> None:
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            return
        with self._lock:
            if not self._generation_matches_locked(generation):
                return
            if self._scan_cancelled.is_set():
                return
            library = self._libraries.get(label)
            if library is None:
                return
            library.total = max(total, library.scanned)
            snapshot = self._snapshot_locked()
        self._update_publisher(snapshot)

    def record_counted_item(
        self, label: str, path: object, *, generation: Optional[int] = None
    ) -> None:
        """Remember a first-pass candidate without retaining or publishing its path."""

        key = _path_key(path)
        if key is None:
            return
        with self._lock:
            if not self._generation_matches_locked(generation):
                return
            if self._scan_cancelled.is_set():
                return
            if label not in self._libraries:
                return
            self._post_cutoff_removed_items.discard(key)
            self._scan_unvisited_items.setdefault(label, set()).add(key)
            self._counted_items[key] = label

    def record_scanned(
        self,
        label: str,
        count: int = 1,
        *,
        generation: Optional[int] = None,
    ) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return
        with self._lock:
            if not self._generation_matches_locked(generation):
                return
            if self._scan_cancelled.is_set():
                return
            library = self._libraries.get(label)
            if library is None:
                return
            library.scanned += count
            library.total = max(library.total, library.scanned)
            snapshot = self._snapshot_locked()
        self._update_publisher(snapshot)

    def record_scanned_item(
        self, label: str, path: object, *, generation: Optional[int] = None
    ) -> None:
        """Record one visited candidate and bind it to the first-pass identity set."""

        key = _path_key(path)
        with self._lock:
            if not self._generation_matches_locked(generation):
                return
            if self._scan_cancelled.is_set():
                return
            library = self._libraries.get(label)
            if library is None:
                return
            if key is not None:
                self._scan_unvisited_items.setdefault(label, set()).discard(key)
                self._runtime_items_during_scan.pop(key, None)
                self._post_cutoff_runtime_items.discard(key)
                self._post_cutoff_removed_items.discard(key)
                self._scan_seen_items.add(key)
                self._counted_items[key] = label
            library.scanned += 1
            library.total = max(library.total, library.scanned)
            snapshot = self._snapshot_locked()
        self._update_publisher(snapshot)

    def record_scan_error(self, *, generation: Optional[int] = None) -> None:
        with self._lock:
            if not self._generation_matches_locked(generation):
                return
            if self._scan_cancelled.is_set():
                return
            self._scan_errors += 1
            snapshot = self._snapshot_locked()
        self._update_publisher(snapshot)

    def mark_item_queued(
        self,
        path: object,
        *,
        source: str = "startup_scan",
        generation: Optional[int] = None,
    ) -> bool:
        if not self.config.enabled:
            return False
        key = _path_key(path)
        if key is None:
            return False
        with self._lock:
            if not self._generation_matches_locked(generation):
                return False
            if key in self._pending_items:
                return False
            self._post_cutoff_removed_items.discard(key)
            label = self._label_for_path_locked(path)
            if label is None:
                label = "Other"
                self._libraries.setdefault(label, _MutableLibrary(label))
                if not self._scan_finished:
                    normalized_path = _normalized_path(path)
                    if normalized_path is not None:
                        self._unbound_pending_paths[key] = normalized_path
            counted_at_enqueue = self._scan_finished and key not in self._counted_items
            self._pending_items[key] = _PendingItem(
                label=label,
                counted_at_enqueue=counted_at_enqueue,
                generation=generation,
            )
            library = self._libraries[label]
            library.items_left += 1
            if counted_at_enqueue:
                library.total += 1
                library.scanned += 1
                self._counted_items[key] = label
            elif source != "startup_scan":
                self._runtime_items_during_scan[key] = label
                if generation is None and not self._scan_ingress_open:
                    self._post_cutoff_runtime_items.add(key)
            snapshot = self._snapshot_locked()
        self._update_publisher(snapshot)
        return True

    def mark_item_observed(
        self, path: object, *, generation: Optional[int] = None
    ) -> bool:
        """Count one stable supported watcher arrival independently of backlog work."""

        if not self.config.enabled:
            return False
        key = _path_key(path)
        if key is None:
            return False
        with self._lock:
            if not self._generation_matches_locked(generation):
                return False
            if key in self._counted_items or key in self._runtime_items_during_scan:
                return False
            label = self._label_for_path_locked(path)
            if label is None:
                label = "Other"
                self._libraries.setdefault(label, _MutableLibrary(label))
            self._post_cutoff_removed_items.discard(key)
            library = self._libraries[label]
            if self._scan_finished:
                self._counted_items[key] = label
                library.total += 1
                library.scanned += 1
            else:
                self._runtime_items_during_scan[key] = label
                if generation is None and not self._scan_ingress_open:
                    self._post_cutoff_runtime_items.add(key)
            snapshot = self._snapshot_locked()
        self._update_publisher(snapshot)
        return True

    def mark_item_completed(
        self, path: object, *, generation: Optional[int] = None
    ) -> bool:
        key = _path_key(path)
        if key is None:
            return False
        with self._lock:
            if not self._generation_matches_locked(generation):
                return False
            pending = self._pending_items.pop(key, None)
            if pending is None:
                return False
            self._unbound_pending_paths.pop(key, None)
            self._runtime_items_during_scan.pop(key, None)
            self._post_cutoff_runtime_items.discard(key)
            library = self._libraries.get(pending.label)
            if library is not None:
                library.items_left = max(0, library.items_left - 1)
            snapshot = self._snapshot_locked()
        self._update_publisher(snapshot, urgent=True)
        return True

    def mark_item_removed(
        self, path: object, *, generation: Optional[int] = None
    ) -> bool:
        """Remove a deleted or moved-away media identity from live inventory."""

        key = _path_key(path)
        if key is None:
            return False
        with self._lock:
            if not self._generation_matches_locked(generation):
                return False
            pending = self._pending_items.pop(key, None)
            counted_label = self._counted_items.pop(key, None)
            was_scanned = self._scan_finished or key in self._scan_seen_items
            label = (
                pending.label
                if pending is not None
                else counted_label or self._label_for_path_locked(path)
            )
            self._unbound_pending_paths.pop(key, None)
            self._runtime_items_during_scan.pop(key, None)
            self._post_cutoff_runtime_items.discard(key)
            self._scan_seen_items.discard(key)
            if (
                generation is None
                and not self._scan_finished
                and not self._scan_ingress_open
                and (pending is not None or counted_label is not None)
            ):
                self._post_cutoff_removed_items.add(key)
            for unvisited in self._scan_unvisited_items.values():
                unvisited.discard(key)
            library = self._libraries.get(label) if label is not None else None
            if library is not None:
                if pending is not None:
                    library.items_left = max(0, library.items_left - 1)
                if counted_label is not None:
                    library.total = max(0, library.total - 1)
                    if was_scanned:
                        library.scanned = max(0, library.scanned - 1)
                    library.scanned = min(library.scanned, library.total)
            changed = pending is not None or counted_label is not None
            snapshot = self._snapshot_locked()
        if changed:
            self._update_publisher(snapshot, urgent=True)
        return changed

    def mark_item_moved(
        self,
        source_path: object,
        destination_path: object,
        *,
        generation: Optional[int] = None,
    ) -> bool:
        """Transfer a counted media identity independently of queue admission."""

        source_key = _path_key(source_path)
        destination_key = _path_key(destination_path)
        if source_key is None or destination_key is None:
            return False
        if source_key == destination_key:
            return False
        with self._lock:
            if not self._generation_matches_locked(generation):
                return False
            pending = self._pending_items.pop(source_key, None)
            counted_label = self._counted_items.pop(source_key, None)
            source_label = (
                pending.label
                if pending is not None
                else counted_label or self._label_for_path_locked(source_path)
            )
            destination_label = self._label_for_path_locked(destination_path)
            was_scanned = self._scan_finished or source_key in self._scan_seen_items
            was_unvisited = False
            for unvisited in self._scan_unvisited_items.values():
                if source_key in unvisited:
                    was_unvisited = True
                unvisited.discard(source_key)

            self._unbound_pending_paths.pop(source_key, None)
            self._runtime_items_during_scan.pop(source_key, None)
            self._post_cutoff_runtime_items.discard(source_key)
            self._scan_seen_items.discard(source_key)
            if (
                generation is None
                and not self._scan_finished
                and not self._scan_ingress_open
                and (pending is not None or counted_label is not None)
            ):
                self._post_cutoff_removed_items.add(source_key)

            source_library = (
                self._libraries.get(source_label) if source_label is not None else None
            )
            if source_library is not None and pending is not None:
                source_library.items_left = max(0, source_library.items_left - 1)

            destination_already_counted = destination_key in self._counted_items
            transfer_count = (
                counted_label is not None
                and destination_label is not None
                and not destination_already_counted
            )
            if counted_label is not None and (
                not transfer_count or counted_label != destination_label
            ):
                counted_library = self._libraries.get(counted_label)
                if counted_library is not None:
                    counted_library.total = max(0, counted_library.total - 1)
                    if was_scanned:
                        counted_library.scanned = max(0, counted_library.scanned - 1)
                    counted_library.scanned = min(
                        counted_library.scanned,
                        counted_library.total,
                    )

            if transfer_count:
                destination_library = self._libraries.get(destination_label)
                if destination_library is not None:
                    if counted_label != destination_label:
                        destination_library.total += 1
                        if was_scanned:
                            destination_library.scanned += 1
                    self._counted_items[destination_key] = destination_label
                    self._post_cutoff_removed_items.discard(destination_key)
                    if was_scanned:
                        self._scan_seen_items.add(destination_key)
                    elif was_unvisited and not self._scan_finished:
                        self._scan_unvisited_items.setdefault(
                            destination_label, set()
                        ).add(destination_key)
                    if not self._scan_finished:
                        self._runtime_items_during_scan[
                            destination_key
                        ] = destination_label
                        if generation is None and not self._scan_ingress_open:
                            self._post_cutoff_runtime_items.add(destination_key)

            changed = pending is not None or counted_label is not None
            snapshot = self._snapshot_locked()
        if changed:
            self._update_publisher(snapshot, urgent=True)
        return changed

    def cancel_item_queue(
        self, path: object, *, generation: Optional[int] = None
    ) -> bool:
        """Roll back an inventory reservation when no queue accepted the item."""

        key = _path_key(path)
        if key is None:
            return False
        with self._lock:
            if not self._generation_matches_locked(generation):
                return False
            pending = self._pending_items.pop(key, None)
            if pending is None:
                return False
            self._unbound_pending_paths.pop(key, None)
            self._runtime_items_during_scan.pop(key, None)
            self._post_cutoff_runtime_items.discard(key)
            library = self._libraries.get(pending.label)
            if library is not None:
                library.items_left = max(0, library.items_left - 1)
                if pending.counted_at_enqueue:
                    library.total = max(0, library.total - 1)
                    library.scanned = max(0, library.scanned - 1)
                    self._counted_items.pop(key, None)
            snapshot = self._snapshot_locked()
        self._update_publisher(snapshot, urgent=True)
        return True

    def finish_scan(
        self,
        *,
        successful: bool = True,
        generation: Optional[int] = None,
    ) -> None:
        drain_timed_out = False
        with self._scan_ingress_condition:
            if not self._generation_matches_locked(generation):
                return
            self._scan_ingress_open = False
            generation = self._scan_generation
            if successful and not self._scan_cancelled.is_set():
                drain_timeout = min(
                    DEFAULT_SCAN_EVENT_DRAIN_SECONDS,
                    max(0.0, self.config.scan_timeout_seconds),
                )
                self._scan_ingress_condition.wait_for(
                    lambda: self._scan_ingress_inflight.get(generation, 0) == 0
                    or self._scan_cancelled.is_set(),
                    timeout=drain_timeout,
                )
                drain_timed_out = (
                    self._scan_ingress_inflight.get(generation, 0) != 0
                    and not self._scan_cancelled.is_set()
                )
                if drain_timed_out:
                    successful = False
                    self._scan_errors += 1
            self._cancel_watchdog_locked()
            self._reconcile_runtime_additions_locked(
                drop_unvisited=bool(successful)
                and not self._scan_cancelled.is_set()
            )
            self._scan_complete = bool(successful) and not self._scan_cancelled.is_set()
            self._scan_finished = True
            snapshot = self._snapshot_locked()
            self._scan_ready.set()
            self._scan_ingress_condition.notify_all()
        self._update_publisher(snapshot, urgent=True)
        if drain_timed_out:
            try:
                self.logger.warning(
                    "Startup inventory could not drain file events admitted "
                    "before its cutoff within %.0f seconds; queued work was "
                    "released and the scan was marked incomplete.",
                    drain_timeout,
                )
            except Exception:
                pass

    def _expire_scan(self, generation: Optional[int] = None) -> None:
        with self._scan_ingress_condition:
            if not self._generation_matches_locked(generation):
                return
            if self._scan_finished:
                return
            self._scan_watchdog = None
            self._scan_cancelled.set()
            self._scan_ingress_open = False
            self._reconcile_runtime_additions_locked(drop_unvisited=False)
            self._scan_complete = False
            self._scan_finished = True
            self._scan_errors += 1
            self._scan_ready.set()
            self._scan_ingress_condition.notify_all()
            snapshot = self._snapshot_locked()
        self._update_publisher(snapshot, urgent=True)
        try:
            self.logger.warning(
                "Startup inventory exceeded its %.0f-second safety timeout; "
                "queued transcription work has been released and the scan was "
                "marked incomplete.",
                self.config.scan_timeout_seconds,
            )
        except Exception:
            pass

    def _cancel_watchdog_locked(self) -> None:
        timer = self._scan_watchdog
        self._scan_watchdog = None
        if timer is not None:
            timer.cancel()

    def wait_until_scanned(self, timeout: Optional[float] = None) -> bool:
        try:
            with self._lock:
                generation = self._scan_generation
            effective_timeout = timeout
            enforce_watchdog_fallback = effective_timeout is None and self.config.enabled
            if enforce_watchdog_fallback:
                effective_timeout = self.config.scan_timeout_seconds + 5.0
            ready = self._scan_ready.wait(effective_timeout)
            if not ready and enforce_watchdog_fallback:
                self._expire_scan(generation)
                return True
            return ready
        except Exception:
            return True

    def acquire_scan_event(self) -> Optional[int]:
        """Admit one Watchdog callback into the current scan cutoff."""

        with self._scan_ingress_condition:
            if self._scan_finished or not self._scan_ingress_open:
                return None
            generation = self._scan_generation
            self._scan_ingress_inflight[generation] = (
                self._scan_ingress_inflight.get(generation, 0) + 1
            )
            return generation

    def release_scan_event(self, generation: Optional[int]) -> None:
        if generation is None:
            return
        with self._scan_ingress_condition:
            current = self._scan_ingress_inflight.get(generation, 0)
            if current <= 1:
                self._scan_ingress_inflight.pop(generation, None)
            else:
                self._scan_ingress_inflight[generation] = current - 1
            self._scan_ingress_condition.notify_all()

    def close_scan_event_cutoff(self, *, generation: Optional[int] = None) -> None:
        """Define the scan cutoff before the final filesystem reconciliation."""

        with self._scan_ingress_condition:
            if not self._generation_matches_locked(generation):
                return
            self._scan_ingress_open = False

    def wait_for_scan_events(self, *, generation: int) -> bool:
        """Wait conditionally for callbacks admitted before the scan cutoff."""

        with self._scan_ingress_condition:
            if not self._generation_matches_locked(generation):
                return False
            drain_timeout = min(
                DEFAULT_SCAN_EVENT_DRAIN_SECONDS,
                max(0.0, self.config.scan_timeout_seconds),
            )
            self._scan_ingress_condition.wait_for(
                lambda: self._scan_ingress_inflight.get(generation, 0) == 0
                or self._scan_cancelled.is_set()
                or not self._generation_matches_locked(generation),
                timeout=drain_timeout,
            )
            return (
                self._generation_matches_locked(generation)
                and not self._scan_cancelled.is_set()
                and self._scan_ingress_inflight.get(generation, 0) == 0
            )

    def reconcile_final_library(
        self,
        label: str,
        paths: Sequence[object],
        *,
        generation: int,
    ) -> None:
        """Commit the exact supported-file set observed at the scan cutoff."""

        keys = {key for path in paths if (key := _path_key(path)) is not None}
        with self._lock:
            if not self._generation_matches_locked(generation):
                return
            keys.difference_update(self._post_cutoff_removed_items)
            library = self._libraries.get(label)
            if library is None:
                return
            for key, counted_label in tuple(self._counted_items.items()):
                if counted_label == label and key not in keys:
                    self._counted_items.pop(key, None)
            for key in keys:
                self._counted_items[key] = label
            for key, pending in tuple(self._pending_items.items()):
                if (
                    pending.label == label
                    and key not in keys
                    and key not in self._post_cutoff_runtime_items
                ):
                    self._pending_items.pop(key, None)
                    self._runtime_items_during_scan.pop(key, None)
                    self._post_cutoff_runtime_items.discard(key)
                    self._unbound_pending_paths.pop(key, None)
            for key, runtime_label in tuple(self._runtime_items_during_scan.items()):
                if runtime_label == label and (
                    key in keys or key not in self._post_cutoff_runtime_items
                ):
                    self._runtime_items_during_scan.pop(key, None)
                    self._post_cutoff_runtime_items.discard(key)
            library.total = len(keys)
            library.scanned = len(keys)
            library.items_left = sum(
                pending.label == label for pending in self._pending_items.values()
            )
            self._scan_unvisited_items.setdefault(label, set()).clear()
            self._scan_seen_items.update(keys)
            snapshot = self._snapshot_locked()
        self._update_publisher(snapshot)

    def needs_scan_reconciliation(
        self, path: object, *, generation: Optional[int] = None
    ) -> bool:
        key = _path_key(path)
        if key is None:
            return False
        with self._lock:
            return (
                self._generation_matches_locked(generation)
                and not self._scan_cancelled.is_set()
                and key not in self._scan_seen_items
                and key not in self._pending_items
            )

    def _generation_matches_locked(self, generation: Optional[int]) -> bool:
        return generation is None or generation == self._scan_generation

    def is_scan_generation_active(self, generation: object) -> bool:
        if isinstance(generation, bool) or not isinstance(generation, int):
            return False
        with self._lock:
            return (
                generation == self._scan_generation
                and not self._scan_finished
                and not self._scan_cancelled.is_set()
            )

    def is_scan_generation_current(self, generation: object) -> bool:
        """Reject queued work superseded by a newer inventory generation."""

        if isinstance(generation, bool) or not isinstance(generation, int):
            return False
        with self._lock:
            return generation == self._scan_generation

    def _reconcile_runtime_additions_locked(self, *, drop_unvisited: bool) -> None:
        for key, label in self._runtime_items_during_scan.items():
            library = self._libraries.get(label)
            if library is None:
                continue
            unvisited = self._scan_unvisited_items.get(label)
            if key in self._counted_items:
                if unvisited is not None:
                    unvisited.discard(key)
                continue
            if unvisited is not None and key in unvisited:
                unvisited.discard(key)
                library.scanned += 1
                library.total = max(library.total, library.scanned)
            else:
                library.total += 1
                library.scanned += 1
            self._counted_items[key] = label
        self._runtime_items_during_scan.clear()
        self._post_cutoff_runtime_items.clear()
        self._post_cutoff_removed_items.clear()
        if drop_unvisited:
            for label, unvisited in self._scan_unvisited_items.items():
                library = self._libraries.get(label)
                if library is None:
                    continue
                library.total = max(
                    library.scanned,
                    library.total - len(unvisited),
                )
        self._scan_unvisited_items.clear()
        self._scan_seen_items.clear()
        self._unbound_pending_paths.clear()

    def snapshot(self) -> InventorySnapshot:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> InventorySnapshot:
        library_snapshots = []
        for library in self._libraries.values():
            reported_total = max(
                library.total,
                library.scanned,
                library.items_left,
            )
            library_snapshots.append(
                LibraryInventory(
                    name=library.name,
                    scanned=min(library.scanned, reported_total),
                    total=reported_total,
                    items_left=library.items_left,
                )
            )
        libraries = tuple(library_snapshots)
        total = sum(library.total for library in libraries)
        scanned = sum(min(library.scanned, library.total) for library in libraries)
        if self._scan_complete:
            percent = 100.0
        elif total <= 0:
            percent = 0.0
        else:
            percent = round(min(100.0, scanned * 100.0 / total), 1)
        return InventorySnapshot(
            items_left=len(self._pending_items),
            scan_percent=percent,
            scan_complete=self._scan_complete,
            scan_errors=self._scan_errors,
            libraries=libraries,
        )

    def _label_for_path_locked(self, path: object) -> Optional[str]:
        normalized = _normalized_path(path)
        return self._label_for_normalized_path_locked(normalized)

    def _label_for_normalized_path_locked(
        self, normalized: Optional[str]
    ) -> Optional[str]:
        if normalized is None:
            return None
        for root, label in self._roots:
            if normalized == root or normalized.startswith(f"{root}/"):
                return label
        return None

    def _update_publisher(
        self, _snapshot: InventorySnapshot, *, urgent: bool = False
    ) -> None:
        if not self.config.enabled:
            return
        with self._publication_lock:
            with self._lock:
                snapshot = self._snapshot_locked()
            try:
                self._publisher.update(snapshot, urgent=urgent)
            except Exception as exc:
                self._warn_nonblocking("update", exc)

    def _warn_nonblocking(self, operation: str, error: Exception) -> None:
        try:
            self.logger.warning(
                "MQTT inventory %s failed (%s); transcription will continue.",
                operation,
                type(error).__name__,
            )
        except Exception:
            pass


def discovery_messages(
    config: MqttInventoryConfig,
) -> tuple[tuple[str, str], ...]:
    """Return retained Home Assistant discovery topics and JSON payloads."""

    state_topic = f"{config.topic_prefix}/inventory/state"
    availability_topic = f"{config.topic_prefix}/availability"
    device = {
        "identifiers": [config.node_id],
        "name": "Subgen",
        "manufacturer": "Subgen",
        "model": "Subtitle inventory",
    }
    common = {
        "state_topic": state_topic,
        "json_attributes_topic": state_topic,
        "availability_topic": availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device,
        "entity_category": "diagnostic",
    }
    entities = (
        (
            "items_left",
            {
                **common,
                "name": "Items Left",
                "unique_id": f"{config.node_id}_items_left",
                "object_id": "subgen_items_left",
                "value_template": "{{ value_json.items_left }}",
                "icon": "mdi:subtitles-outline",
            },
        ),
        (
            "scan_percent",
            {
                **common,
                "name": "Scan %",
                "unique_id": f"{config.node_id}_scan_percent",
                "object_id": "subgen_scan",
                "value_template": "{{ value_json.scan_percent }}",
                "unit_of_measurement": "%",
                "icon": "mdi:progress-check",
            },
        ),
    )
    return tuple(
        (
            f"{config.discovery_prefix}/sensor/{config.node_id}/{key}/config",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )
        for key, payload in entities
    )


def state_message(snapshot: InventorySnapshot) -> str:
    return json.dumps(snapshot.as_payload(), sort_keys=True, separators=(",", ":"))


def _default_client_factory(config: MqttInventoryConfig):
    import paho.mqtt.client as mqtt  # Imported only when the feature is enabled.

    return mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=config.client_id,
        protocol=mqtt.MQTTv311,
    )


class InventoryPublisher:
    """Best-effort retained MQTT publisher with HA discovery and LWT."""

    def __init__(
        self,
        config: MqttInventoryConfig,
        logger,
        *,
        client_factory: Callable[[MqttInventoryConfig], object] = _default_client_factory,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.logger = logger
        self._client_factory = client_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._latest: Optional[InventorySnapshot] = None
        self._urgent = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._last_publish = -math.inf
        self._needs_discovery = False

    def start(self) -> None:
        if not self.config.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="subgen-mqtt-inventory",
            daemon=True,
        )
        self._thread.start()

    def update(self, snapshot: InventorySnapshot, *, urgent: bool = False) -> None:
        if not self.config.enabled:
            return
        with self._lock:
            self._latest = snapshot
            self._urgent = self._urgent or urgent
        self._wake.set()

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        self._wake.set()
        thread.join(timeout=5.0)
        if thread.is_alive():
            try:
                self.logger.warning(
                    "MQTT inventory publisher did not stop within five seconds; "
                    "its thread remains owned and will not be duplicated."
                )
            except Exception:
                pass
            return
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect_and_run()
            except Exception as exc:
                self._warn(exc)
            if not self._stop.is_set():
                self._stop.wait(min(self.config.refresh_seconds, 60.0))

    def _connect_and_run(self) -> None:
        self._connected.clear()
        client = self._client_factory(self.config)
        self._client = client
        try:
            if self.config.username is not None:
                client.username_pw_set(self.config.username, self.config.password)
            client.will_set(
                f"{self.config.topic_prefix}/availability",
                payload="offline",
                qos=1,
                retain=True,
            )
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.connect(self.config.host, self.config.port, keepalive=60)
            client.loop_start()
            while not self._stop.is_set():
                now = self._clock()
                if not self._connected.is_set() or not math.isfinite(
                    self._last_publish
                ):
                    timeout = self.config.refresh_seconds
                else:
                    timeout = max(
                        0.0,
                        min(
                            self.config.refresh_seconds,
                            self._last_publish
                            + self.config.refresh_seconds
                            - now,
                        ),
                    )
                self._wake.wait(timeout)
                self._wake.clear()
                with self._lock:
                    urgent = self._urgent
                    self._urgent = False
                    latest = self._latest
                    needs_discovery = self._needs_discovery and latest is not None
                    if needs_discovery:
                        self._needs_discovery = False
                now = self._clock()
                due = now - self._last_publish >= self.config.refresh_seconds
                if self._connected.is_set() and latest is not None and (
                    needs_discovery or urgent or due
                ):
                    try:
                        if needs_discovery:
                            self._publish_discovery(client)
                        self._publish_snapshot(client, latest)
                    except Exception:
                        if needs_discovery:
                            with self._lock:
                                self._needs_discovery = True
                        raise
        finally:
            was_connected = self._connected.is_set()
            self._connected.clear()
            offline_confirmed = not was_connected
            if was_connected:
                try:
                    self._publish_message(
                        client,
                        f"{self.config.topic_prefix}/availability",
                        "offline",
                        retain=True,
                    )
                    offline_confirmed = True
                except Exception:
                    offline_confirmed = False
            if offline_confirmed:
                try:
                    disconnect_result = client.disconnect()
                except Exception:
                    self._abort_connection(client)
                else:
                    if disconnect_result not in (None, 0):
                        self._abort_connection(client)
                    else:
                        try:
                            client.loop_stop()
                        except Exception:
                            self._abort_connection(client)
            else:
                self._abort_connection(client)
            self._client = None

    def _on_connect(self, _client, _userdata, _flags, reason_code, _properties=None):
        if reason_code != 0:
            self._connected.clear()
            return
        with self._lock:
            self._needs_discovery = True
            self._urgent = True
        self._connected.set()
        self._wake.set()

    def _on_disconnect(self, _client, _userdata, *_args):
        self._connected.clear()

    def _publish_discovery(self, client) -> None:
        for topic, payload in discovery_messages(self.config):
            self._publish_message(client, topic, payload, retain=True)
        self._publish_message(
            client,
            f"{self.config.topic_prefix}/availability",
            "online",
            retain=True,
        )

    def _publish_snapshot(self, client, snapshot: InventorySnapshot) -> None:
        self._publish_message(
            client,
            f"{self.config.topic_prefix}/inventory/state",
            state_message(snapshot),
            retain=True,
        )
        self._last_publish = self._clock()

    @staticmethod
    def _publish_message(client, topic: str, payload: str, *, retain: bool) -> None:
        result = client.publish(
            topic,
            payload=payload,
            qos=1,
            retain=retain,
        )
        result_code = getattr(result, "rc", 0)
        if result_code not in (None, 0):
            raise RuntimeError("MQTT publish was rejected")
        wait_for_publish = getattr(result, "wait_for_publish", None)
        if callable(wait_for_publish):
            try:
                wait_for_publish(timeout=5.0)
            except TypeError:
                wait_for_publish()
        is_published = getattr(result, "is_published", None)
        if callable(is_published) and not is_published():
            raise TimeoutError("MQTT publish was not confirmed")

    @staticmethod
    def _abort_connection(client) -> None:
        """Close without MQTT DISCONNECT so the broker can publish the LWT."""

        try:
            client.loop_stop()
        except Exception:
            pass
        try:
            get_socket = getattr(client, "socket", None)
            mqtt_socket = get_socket() if callable(get_socket) else None
            if mqtt_socket is not None:
                mqtt_socket.close()
                return
        except Exception:
            pass
        try:
            close_socket = getattr(client, "_sock_close", None)
            if callable(close_socket):
                close_socket()
        except Exception:
            pass

    def _warn(self, error: Exception) -> None:
        try:
            self.logger.warning(
                "MQTT inventory connection or publish failed (%s); "
                "transcription will continue.",
                type(error).__name__,
            )
        except Exception:
            pass


__all__ = [
    "DEFAULT_REFRESH_SECONDS",
    "InventoryCoordinator",
    "InventoryPublisher",
    "InventorySnapshot",
    "LibraryInventory",
    "MqttInventoryConfig",
    "discovery_messages",
    "load_mqtt_inventory_config",
    "state_message",
]
