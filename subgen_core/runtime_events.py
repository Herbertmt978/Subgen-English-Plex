"""Privacy-safe structured events for owner-operated runtime observation."""

from __future__ import annotations

import json
import secrets
import threading
import time


SENTINEL = "SUBGEN_RUNTIME_EVENT "
SCHEMA = "subgen.runtime-event/v1"
MULTICHUNK_COMPLETED = "multichunk_transcription_completed"

_event_lock = threading.Lock()
_event_sequence = 0


def new_workload_id() -> str:
    """Return an opaque identifier that cannot disclose the source media."""

    return secrets.token_hex(16)


def _canonical_line(document: dict[str, object]) -> str:
    return SENTINEL + json.dumps(
        document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def multichunk_success_line(
    *,
    workload_id: str,
    chunks_total: int,
    event_sequence: int,
    monotonic_ns: int,
) -> str:
    """Build one canonical success event after durable subtitle publication."""

    if (
        not isinstance(workload_id, str)
        or len(workload_id) != 32
        or any(character not in "0123456789abcdef" for character in workload_id)
    ):
        raise ValueError("workload_id must be an opaque 128-bit lowercase hex token")
    if (
        isinstance(chunks_total, bool)
        or not isinstance(chunks_total, int)
        or chunks_total <= 1
    ):
        raise ValueError("chunks_total must describe a multi-chunk workload")
    if (
        isinstance(event_sequence, bool)
        or not isinstance(event_sequence, int)
        or event_sequence <= 0
    ):
        raise ValueError("event_sequence must be a positive integer")
    if (
        isinstance(monotonic_ns, bool)
        or not isinstance(monotonic_ns, int)
        or monotonic_ns < 0
    ):
        raise ValueError("monotonic_ns must be a nonnegative integer")

    return _canonical_line(
        {
            "atomic_publish": "succeeded",
            "chunks_total": chunks_total,
            "event": MULTICHUNK_COMPLETED,
            "event_sequence": event_sequence,
            "monotonic_ns": monotonic_ns,
            "outcome": "success",
            "schema": SCHEMA,
            "workload_id": workload_id,
        }
    )


def emit_multichunk_success(runtime, *, workload_id: str, chunks_total: int) -> str:
    """Log one machine-readable receipt for a completed multi-chunk workload."""

    global _event_sequence
    with _event_lock:
        _event_sequence += 1
        line = multichunk_success_line(
            workload_id=workload_id,
            chunks_total=chunks_total,
            event_sequence=_event_sequence,
            monotonic_ns=time.monotonic_ns(),
        )
        runtime.logging.info("%s", line)
    return line
