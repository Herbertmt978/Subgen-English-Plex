"""Human-readable, best-effort progress and memory-plan formatting.

The transcription runtime owns when these helpers are called.  This module is
deliberately observational: it never changes model admission, pressure state,
chunk policy, or failure classification, and a failed diagnostic read must
never fail a subtitle job.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

from .resource_probes import UNBOUNDED_CGROUP_THRESHOLD

GIB = 1024**3


def execution_policy_lines(policy) -> tuple[str, ...]:
    activity = {
        "passive": "cautious use of spare capacity",
        "balanced": "balanced throughput and headroom",
        "max": "maximum throughput within safety limits",
    }[policy.activity]
    mode = (
        "other workloads take priority (recommended)"
        if policy.run_mode == "adaptive"
        else "keep working between files; still yield for memory pressure"
    )
    return (
        f"Subgen activity: {policy.activity} — {activity}",
        f"Run mode: {policy.run_mode} — {mode}",
        f"Chunk planning target: {policy.working_budget_percent}% of safe "
        "post-reserve capacity; model quality uses the full safe budget",
        "Optional priority signal: " + (
            "ignored in dedicated mode; RAM/VRAM safeguards remain active"
            "; application performance changes such as camera FPS drops will not pause Subgen"
            if policy.run_mode == "dedicated"
            else "enabled" if policy.priority_signal_enabled else "disabled"
        ),
    )


def cohort_model_selection_lines(selection) -> tuple[str, ...]:
    """Explain the aggregate owner's decisions without inventing memory usage."""
    lines = []
    meanings = {
        'insufficient_combined_host': 'combined system RAM cannot fit all selected workers',
        'insufficient_host': 'system RAM is insufficient',
        'insufficient_device': 'GPU memory is insufficient',
        'host_unavailable': 'system RAM observation is unavailable',
        'gpu_unavailable': 'GPU memory observation is unavailable or stale',
        'cgroup_unavailable': 'container memory enforcement is unavailable',
        'gpu_reserve_below_floor': 'GPU reserve is below its safety floor',
    }
    for model, admission in selection.assessments:
        lines.append(f'Model check: {safe_text(model, limit=64)}')
        lines.append(f'  Combined RAM: {format_gib(admission.required_host_bytes)} required; '
                     f'{format_gib(admission.available_host_bytes)} available after reserves')
        if (admission.available_host_bytes is not None
                and admission.required_host_bytes > admission.available_host_bytes):
            shortfall = math.ceil((admission.required_host_bytes-admission.available_host_bytes)/1024**2)
            lines.append(f'  RAM shortfall: {shortfall} MiB (rounded up; the GiB display is approximate)')
        for index, worker in enumerate(admission.workers, 1):
            basis = ('measured profile with margins'
                     if worker.requirement.provenance in ('envelope', 'native-profile')
                     else 'conservative estimate, not measured usage')
            lines.append(f'  Worker {index} memory requirement: {basis}')
            if worker.device_admission_bytes is not None:
                lines.append(f'  Worker {index} GPU memory: {format_gib(worker.requirement.required_device_bytes)} required; '
                             f'{format_gib(worker.device_admission_bytes)} available after reserve')
        for code in admission.reasons:
            worker, separator, reason = code.rpartition(':')
            reason = reason if separator else code
            label = meanings.get(reason, safe_text(reason, limit=80).replace('_', ' '))
            lines.append(f'  Waiting reason: {worker + ": " if separator else ""}{label}')
    if selection.selected_model is not None:
        reason = 'explicit model choice' if selection.explicit else 'highest common model that fits'
        lines.append(f'Selected model: {safe_text(selection.selected_model, limit=64)} on all selected GPUs ({reason})')
    elif selection.reason == 'explicit_unavailable':
        lines.append('Requested model is not provisioned for every selected GPU; no fallback model selected')
    elif selection.explicit:
        lines.append('Waiting for the requested model to fit; no fallback model selected')
    else:
        lines.append('Waiting: no common automatic model fits the current resource observations')
    return tuple(lines)


def model_selection_lines(decision) -> tuple[str, ...]:
    """Explain the owner's recorded decision; never recompute admission."""
    lines = []
    system = getattr(decision, "system_ceiling", None)
    device = getattr(decision, "device_ceiling", None)
    if system is not None:
        guide = f"Conservative model guide — system RAM: {safe_text(system, limit=64)}"
        if device is not None:
            guide += f"; GPU VRAM: {safe_text(device, limit=64)}"
        lines.append(guide + " (fallback estimates, not measured hardware limits)")
    for rejected in getattr(decision, "rejected_admissions", ()):
        reasons = []
        for reason in rejected.reasons:
            if reason == "insufficient_host":
                reasons.append(
                    f"RAM needs {format_gib(rejected.requirement.required_host_bytes)}; "
                    f"{format_gib(rejected.effective_host_admission_bytes)} available after reserves"
                )
            elif reason == "insufficient_device":
                reasons.append(
                    f"VRAM needs {format_gib(rejected.requirement.required_device_bytes)}; "
                    f"{format_gib(rejected.device_admission_bytes)} available after reserves"
                )
            else:
                reasons.append({
                    "host_unavailable": "system RAM readings unavailable",
                    "cgroup_unavailable": "required container memory readings unavailable",
                    "gpu_unavailable": "fresh readings for the selected GPU unavailable",
                    "sample_stale": "memory readings are too old",
                    "sample_time_unavailable": "memory reading time unavailable",
                    "decision_time_unavailable": "current measurement time unavailable",
                    "host_inconsistent": "system RAM readings are inconsistent",
                    "gpu_reserve_below_floor": "GPU reserve is below the safety minimum",
                }.get(reason, "admission evidence unavailable"))
        basis = (
            "measured profile with margins"
            if rejected.requirement.provenance == "envelope"
            else "conservative estimate"
        )
        lines.append(
            f"Model check: {safe_text(rejected.requirement.model, limit=64)} "
            f"not admitted — {'; '.join(reasons)} ({basis})"
        )
    selected = getattr(decision, "selected_model", None)
    explicit = bool(getattr(decision, "explicit", False))
    if selected is None:
        lines.append("Model selection paused: no model currently meets the memory policy")
    else:
        choice = "user-selected; never silently changed" if explicit else "automatic"
        outcome = "admitted" if getattr(decision, "admitted", False) else "waiting for safe capacity"
        basis = (
            "measured profile with margins"
            if getattr(decision, "provenance", None) == "envelope"
            else "conservative estimates"
        )
        lines.append(
            f"Model selected: {safe_text(selected, limit=64)} — {choice}; {outcome}; {basis}"
        )
        if not explicit and getattr(decision, "admitted", False):
            ceiling = getattr(decision, "automatic_ceiling", None)
            if getattr(decision, "provenance", None) == "fallback" and selected == ceiling:
                if system == ceiling and device is not None and device != ceiling:
                    lines.append(
                        "System RAM sets the conservative model ceiling; GPU VRAM "
                        "alone allows a larger model"
                    )
                elif device == ceiling and system is not None and system != ceiling:
                    lines.append(
                        "GPU VRAM sets the conservative model ceiling; system RAM "
                        "alone allows a larger model"
                    )
            lines.append(
                "Reason: highest-quality model admitted by the recorded RAM/VRAM "
                "policy; exact matching measurements can override the fallback guide"
            )
    return tuple(lines)


def pressure_reason(error) -> str:
    """Translate controller reason codes without making new pressure decisions."""
    labels = {
        "priority_pressure": "higher-priority application requested resources",
        "host_headroom": "system RAM reserve reached",
        "cgroup_headroom": "container RAM headroom is low",
        "critical_cgroup_headroom": "container RAM headroom is critically low",
        "gpu_headroom": "GPU memory reserve reached",
        "critical_gpu_headroom": "GPU memory headroom is critically low",
        "gpu_reserve_below_floor": "configured GPU reserve is below its safety floor",
        "psi_full": "other work is stalling while waiting for memory",
        "psi_some": "system memory contention detected",
        "gpu_telemetry_stale": "GPU memory readings are stale",
        "host_inconsistent": "system RAM readings are inconsistent",
    }
    reasons = str(error).split(",")
    translated = [labels[reason.strip()] for reason in reasons if reason.strip() in labels]
    return "; ".join(translated) if translated else "resource pressure detected"


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE = re.compile(r"\s+")
_MONITOR_PROTOCOL_REPLACEMENTS = (
    ("SUBGEN_EVENT ", "SUBGEN-EVENT "),
    ("WORKER START : [TRANSCRIBE", "WORKER-START [TRANSCRIBE"),
    ("WORKER FINISH:", "WORKER-FINISH:"),
    ("Error processing file /media/", "Error processing file [media]/"),
    ("ENGLISH_AUDIO_MISMATCH", "ENGLISH-AUDIO-MISMATCH"),
    ("Detecting language of file: /media/", "Detecting language of [media]/"),
    ("Extracting audio from: /media/", "Extracting audio from [media]/"),
    ("SIGSEGV", "SIG-SEGV"),
)


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """One non-authoritative, human-facing view of the active RAM plan."""

    host_available_bytes: int | None
    reserve_bytes: int | None
    cgroup_current_bytes: int | None
    cgroup_limit_bytes: int | None
    cgroup_floor_bytes: int | None
    working_headroom_bytes: int | None
    suitable_model: str | None
    selected_model: str | None
    model_host_requirement_bytes: int | None
    model_evidence: str | None


def _bytes_or_none(value: object, *, positive: bool = False) -> int | None:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _finite_cgroup_limit(value: object) -> int | None:
    parsed = _bytes_or_none(value, positive=True)
    if parsed is None or parsed >= UNBOUNDED_CGROUP_THRESHOLD:
        return None
    return parsed


def _empty_memory_snapshot() -> MemorySnapshot:
    return MemorySnapshot(
        host_available_bytes=None,
        reserve_bytes=None,
        cgroup_current_bytes=None,
        cgroup_limit_bytes=None,
        cgroup_floor_bytes=None,
        working_headroom_bytes=None,
        suitable_model=None,
        selected_model=None,
        model_host_requirement_bytes=None,
        model_evidence=None,
    )


def safe_text(value: object, *, limit: int = 240) -> str:
    """Return one bounded log-safe line without terminal control characters."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 4:
        raise ValueError("safe text limit must be an integer of at least four")
    text = _CONTROL_CHARACTERS.sub(" ", str(value))
    text = _WHITESPACE.sub(" ", text).strip()
    for sentinel, replacement in _MONITOR_PROTOCOL_REPLACEMENTS:
        text = text.replace(sentinel, replacement)
    if not text:
        return "unknown"
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def safe_path(value: object, *, limit: int = 160) -> str:
    """Return only a bounded basename so routine logs do not expose media roots."""

    normalized = str(value).replace("\\", "/").rstrip("/")
    basename = os.path.basename(normalized) if normalized else "unknown"
    return safe_text(basename, limit=limit)


def format_error(error: BaseException) -> str:
    """Return a bounded exception class and message for an operator log."""

    return safe_text(f"{type(error).__name__}: {error}", limit=240)


def format_gib(value: int | None) -> str:
    """Format a byte count in binary GiB, retaining an explicit unknown state."""

    parsed = _bytes_or_none(value)
    return "unavailable" if parsed is None else f"{parsed / GIB:.1f} GiB"


def format_duration(seconds: object) -> str:
    """Format seconds as HH:MM:SS for file and chunk progress."""

    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "unknown"
    value = float(seconds)
    if not math.isfinite(value) or value < 0:
        return "unknown"
    rounded = int(value)
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def planned_chunk_count(
    media_duration: object,
    cursor: object,
    chunk_seconds: object,
    completed_chunks: object = 0,
) -> int:
    """Project a current adaptive plan without claiming it is immutable."""

    numbers = (media_duration, cursor, chunk_seconds)
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in numbers
    ):
        raise ValueError("chunk plan values must be finite numbers")
    duration, position, working = (float(value) for value in numbers)
    if not all(math.isfinite(value) for value in (duration, position, working)):
        raise ValueError("chunk plan values must be finite numbers")
    if duration <= 0 or working <= 0 or not 0 <= position <= duration:
        raise ValueError("chunk plan values are outside their valid ranges")
    if (
        isinstance(completed_chunks, bool)
        or not isinstance(completed_chunks, int)
        or completed_chunks < 0
    ):
        raise ValueError("completed chunk count must be a non-negative integer")
    remaining = max(0.0, duration - position)
    return completed_chunks + (math.ceil(remaining / working) if remaining else 0)


def progress_percent(cursor: object, media_duration: object) -> int:
    """Return a bounded whole-file percentage for a committed source cursor."""

    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in (cursor, media_duration)
    ):
        return 0
    position, duration = float(cursor), float(media_duration)
    if not math.isfinite(position) or not math.isfinite(duration) or duration <= 0:
        return 0
    return min(100, max(0, round(position / duration * 100)))


def build_memory_snapshot(sample, capacity, decision, reserve_bytes) -> MemorySnapshot:
    """Build a truthful post-load snapshot without double-counting the model.

    ``memory.current`` and host ``MemAvailable`` already include a resident
    model.  Working headroom therefore subtracts only the configured host
    reserve and finite-cgroup floor from the fresh sample; the model requirement
    is displayed as evidence and is not subtracted a second time.
    """

    reserve = _bytes_or_none(reserve_bytes)
    host_available = _bytes_or_none(getattr(sample, "host_available_bytes", None))
    cgroup_current = _bytes_or_none(getattr(sample, "cgroup_current_bytes", None))
    capacity_limit = _finite_cgroup_limit(getattr(capacity, "cgroup_limit_bytes", None))
    cgroup_unbounded = getattr(capacity, "cgroup_unbounded", None) is True
    cgroup_limit_unknown = False
    if sample is not None:
        sampled_limit = _bytes_or_none(
            getattr(sample, "cgroup_limit_bytes", None),
            positive=True,
        )
        cgroup_limit = _finite_cgroup_limit(sampled_limit)
        explicitly_unbounded = bool(
            sampled_limit is not None and sampled_limit >= UNBOUNDED_CGROUP_THRESHOLD
        )
        if explicitly_unbounded:
            cgroup_unbounded = True
        # A normalized ``None`` can mean unbounded or a failed read.  If startup
        # did not explicitly establish an unbounded cgroup, do not claim host-
        # only headroom from that ambiguous fresh sample.
        cgroup_limit_unknown = bool(
            cgroup_limit is None
            and not explicitly_unbounded
            and (capacity_limit is not None or not cgroup_unbounded)
        )
    else:
        cgroup_limit = capacity_limit

    host_headroom = None
    if host_available is not None and reserve is not None:
        host_headroom = max(0, host_available - reserve)

    cgroup_floor = None
    cgroup_headroom = None
    if cgroup_limit is not None:
        cgroup_floor = max(512 * 1024**2, cgroup_limit // 10)
        if cgroup_current is not None:
            cgroup_headroom = max(0, cgroup_limit - cgroup_current - cgroup_floor)

    requirement = getattr(decision, "requirement", None)
    model_requirement = _bytes_or_none(
        getattr(requirement, "required_host_bytes", None)
    )
    provenance = getattr(requirement, "provenance", None)
    if provenance == "envelope":
        evidence = "measured envelope plus safety margin"
    elif provenance == "fallback":
        evidence = "conservative estimate plus safety margin"
    else:
        evidence = None
    automatic_ceiling = getattr(decision, "automatic_ceiling", None)
    selected_model = getattr(decision, "selected_model", None)
    working_headroom = None
    if host_headroom is not None:
        if cgroup_limit is None and cgroup_unbounded and not cgroup_limit_unknown:
            working_headroom = host_headroom
        elif cgroup_headroom is not None:
            working_headroom = min(host_headroom, cgroup_headroom)

    return MemorySnapshot(
        host_available_bytes=host_available,
        reserve_bytes=reserve,
        cgroup_current_bytes=cgroup_current,
        cgroup_limit_bytes=cgroup_limit,
        cgroup_floor_bytes=cgroup_floor,
        working_headroom_bytes=working_headroom,
        suitable_model=(
            safe_text(automatic_ceiling, limit=64)
            if automatic_ceiling is not None
            else None
        ),
        selected_model=(
            safe_text(selected_model, limit=64) if selected_model is not None else None
        ),
        model_host_requirement_bytes=model_requirement,
        model_evidence=evidence,
    )


def snapshot_runtime_memory(runtime) -> MemorySnapshot:
    """Read one best-effort post-load sample; diagnostics never fail a job."""

    try:
        capacity = getattr(runtime, "model_capacity_profile", None)
    except Exception:  # noqa: BLE001 - optional diagnostics cannot fail a job
        capacity = None
    try:
        decision = getattr(runtime, "model_decision", None)
    except Exception:  # noqa: BLE001 - optional diagnostics cannot fail a job
        decision = None
    reserve = None
    try:
        resources = getattr(runtime, "_resource_management", None)
        reserve_owner = getattr(resources, "host_reserve_bytes", None)
        if callable(reserve_owner):
            reserve = reserve_owner(
                capacity,
                explicit_reserve_gib=getattr(
                    runtime,
                    "memory_pressure_reserve_gib",
                    None,
                ),
            )
    except Exception:  # noqa: BLE001 - optional diagnostics cannot fail a job
        reserve = None

    sample = None
    try:
        reader = getattr(runtime, "read_resource_pressure_sample", None)
        if callable(reader):
            sample = reader()
    except Exception:  # noqa: BLE001 - optional diagnostics cannot fail a job
        sample = None

    try:
        return build_memory_snapshot(sample, capacity, decision, reserve)
    except Exception:  # noqa: BLE001 - optional diagnostics cannot fail a job
        return _empty_memory_snapshot()


def format_memory_lines(snapshot: MemorySnapshot) -> tuple[str, ...]:
    """Render the operator-facing RAM plan with explicit evidence semantics."""

    if not isinstance(snapshot, MemorySnapshot):
        raise TypeError("snapshot must be MemorySnapshot")
    used_limit = (
        f"{format_gib(snapshot.cgroup_current_bytes)} / "
        f"{format_gib(snapshot.cgroup_limit_bytes)}"
    )
    selected = snapshot.selected_model or "unavailable"
    model_requirement = format_gib(snapshot.model_host_requirement_bytes)
    evidence = snapshot.model_evidence or "evidence unavailable"
    return (
        f"Memory available: {format_gib(snapshot.host_available_bytes)}",
        (
            "Memory reserved for system/priority tasks: "
            f"{format_gib(snapshot.reserve_bytes)}"
        ),
        f"Subgen memory in use / limit: {used_limit}",
        f"Model suitable: {snapshot.suitable_model or 'unavailable'}",
        (
            f"Model using: {selected} — {model_requirement} RAM requirement "
            f"({evidence}; not live RSS)"
        ),
        (
            "Available for subtitle chunks: "
            f"{format_gib(snapshot.working_headroom_bytes)} working headroom"
        ),
    )


__all__ = [
    "MemorySnapshot",
    "build_memory_snapshot",
    "format_duration",
    "format_error",
    "format_gib",
    "format_memory_lines",
    "planned_chunk_count",
    "progress_percent",
    "safe_path",
    "safe_text",
    "snapshot_runtime_memory",
]
