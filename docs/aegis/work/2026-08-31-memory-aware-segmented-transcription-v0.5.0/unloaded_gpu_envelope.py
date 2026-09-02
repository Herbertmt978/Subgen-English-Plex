#!/usr/bin/env python3
"""Build and validate the owner-only Task 11B unloaded-GPU envelope.

The tool is intentionally local and offline.  A privileged cycle driver owns
container creation, inference, canonical unload, and cgroup PID discovery.  It
passes the resulting identity and cycle observations to this tool as a draft.
This tool is the fail-closed schema, attribution, arithmetic, canonicalization,
and create-once publication boundary.

Draft JSON has the final top-level shape, except ``backend`` omits
``generator_sha256`` and ``measurement`` contains only ``cycles``.  The tool
binds its own source digest and derives all fixed measurement fields.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA = "subgen.unloaded-gpu-envelope/v1"
CYCLE_COUNT = 3
SAMPLES_PER_CYCLE = 10
INTERVAL_SECONDS = 1
MARGIN_BYTES = 128 * 1024 * 1024
MAX_INT = 2**63 - 1
MAX_DOCUMENT_BYTES = 1024 * 1024
NVIDIA_QUERY = (
    "nvidia-smi",
    "--query-compute-apps=pid,gpu_uuid,used_memory",
    "--format=csv,noheader,nounits",
)

LOWER_HEX_40_RE = re.compile(r"^[0-9a-f]{40}$")
LOWER_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^hf:[0-9a-f]{40}$")
GPU_UUID_RE = re.compile(
    r"^GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
PRINTABLE_ASCII_RE = re.compile(r"^[\x20-\x7e]{1,64}$")

TOP_KEYS = {
    "schema",
    "runtime_commit",
    "image",
    "gpu",
    "backend",
    "model_policy",
    "measurement",
}
IMAGE_KEYS = {"oci_index", "config_digest", "layer_diff_ids"}
GPU_KEYS = {"uuid", "driver_version"}
BACKEND_KEYS = {
    "cuda_version",
    "ctranslate2_version",
    "stable_ts_version",
    "generator_sha256",
}
DRAFT_BACKEND_KEYS = BACKEND_KEYS - {"generator_sha256"}
MODEL_POLICY_KEYS = {
    "selected_model",
    "model_revision",
    "compute_type",
    "device",
    "device_index",
    "task",
    "language",
    "chunk_seconds",
    "overlap_seconds",
    "fixture_sha256",
    "priority_policy_sha256",
}
MEASUREMENT_KEYS = {
    "cycles",
    "cycle_count",
    "samples_per_cycle",
    "interval_seconds",
    "margin_bytes",
    "max_observed_candidate_bytes",
    "allowed_unloaded_bytes",
}
CYCLE_KEYS = {
    "cycle_index",
    "container_id_sha256",
    "load_generation_before",
    "load_generation_after",
    "inference_completed",
    "inference_result_sha256",
    "unload_generation_before",
    "unload_generation_after",
    "candidate_bytes_samples",
}
MODELS = {"tiny", "base", "small", "medium", "large-v3"}
TASKS = {"transcribe", "translate"}


class EnvelopeError(RuntimeError):
    """A bounded, fail-closed envelope error safe to print to an operator."""

    def __init__(self, message: str) -> None:
        code = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")[:96]
        self.code = code or "unloaded_gpu_envelope_error"
        super().__init__(self.code)


def _reject_constant(_value: str) -> None:
    raise EnvelopeError("non finite JSON number")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EnvelopeError("duplicate JSON key")
        result[key] = value
    return result


def canonical_json_line(value: Any) -> bytes:
    """Return canonical ASCII JSON with exactly one trailing LF."""

    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise EnvelopeError("document was not canonicalizable") from exc


def parse_json(payload: bytes, *, require_canonical: bool) -> dict[str, Any]:
    if (
        not isinstance(payload, bytes)
        or not payload
        or len(payload) > MAX_DOCUMENT_BYTES
    ):
        raise EnvelopeError("document size was invalid")
    try:
        text = payload.decode("ascii")
        document = json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeError("document was not strict ASCII JSON") from exc
    if not isinstance(document, dict):
        raise EnvelopeError("document root was not an object")
    if require_canonical and canonical_json_line(document) != payload:
        raise EnvelopeError("document bytes were not canonical")
    return document


def _exact_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise EnvelopeError(f"{label} keys were not exact")
    return value


def _bounded_int(
    value: Any, label: str, *, minimum: int = 0, maximum: int = MAX_INT
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise EnvelopeError(f"{label} was outside its integer boundary")
    return value


def _match(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise EnvelopeError(f"{label} was malformed")
    return value


def _ascii(value: Any, label: str) -> str:
    return _match(value, PRINTABLE_ASCII_RE, label)


def _validate_cycle(value: Any, expected_index: int) -> dict[str, Any]:
    cycle = _exact_object(value, CYCLE_KEYS, "cycle")
    if (
        _bounded_int(cycle["cycle_index"], "cycle index", minimum=1, maximum=3)
        != expected_index
    ):
        raise EnvelopeError("cycle ordering was invalid")
    _match(cycle["container_id_sha256"], LOWER_HEX_64_RE, "container ID checksum")
    _match(cycle["inference_result_sha256"], LOWER_HEX_64_RE, "inference checksum")
    if cycle["inference_completed"] is not True:
        raise EnvelopeError("inference was not completed")
    for prefix in ("load", "unload"):
        before = _bounded_int(
            cycle[f"{prefix}_generation_before"], f"{prefix} generation before"
        )
        after = _bounded_int(
            cycle[f"{prefix}_generation_after"], f"{prefix} generation after"
        )
        if before != 0 or after != 1:
            raise EnvelopeError(f"{prefix} generation transition was not clean")
    samples = cycle["candidate_bytes_samples"]
    if not isinstance(samples, list) or len(samples) != SAMPLES_PER_CYCLE:
        raise EnvelopeError("candidate sample count was not exact")
    for sample in samples:
        _bounded_int(sample, "candidate sample")
    return cycle


def _validate_identity(document: dict[str, Any], *, draft: bool) -> None:
    _match(document["runtime_commit"], LOWER_HEX_40_RE, "runtime commit")

    image = _exact_object(document["image"], IMAGE_KEYS, "image")
    _match(image["oci_index"], DIGEST_RE, "OCI index")
    _match(image["config_digest"], DIGEST_RE, "config digest")
    layers = image["layer_diff_ids"]
    if not isinstance(layers, list) or not 1 <= len(layers) <= 256:
        raise EnvelopeError("layer diff ID count was invalid")
    for layer in layers:
        _match(layer, DIGEST_RE, "layer diff ID")

    gpu = _exact_object(document["gpu"], GPU_KEYS, "GPU")
    _match(gpu["uuid"], GPU_UUID_RE, "GPU UUID")
    _ascii(gpu["driver_version"], "driver version")

    backend = _exact_object(
        document["backend"], DRAFT_BACKEND_KEYS if draft else BACKEND_KEYS, "backend"
    )
    for key in ("cuda_version", "ctranslate2_version", "stable_ts_version"):
        _ascii(backend[key], key)
    if not draft:
        _match(backend["generator_sha256"], LOWER_HEX_64_RE, "generator checksum")

    policy = _exact_object(document["model_policy"], MODEL_POLICY_KEYS, "model policy")
    if policy["selected_model"] not in MODELS:
        raise EnvelopeError("selected model was invalid")
    _match(policy["model_revision"], REVISION_RE, "model revision")
    _ascii(policy["compute_type"], "compute type")
    if policy["device"] != "cuda":
        raise EnvelopeError("device was not CUDA")
    _bounded_int(policy["device_index"], "device index", maximum=31)
    if policy["task"] not in TASKS:
        raise EnvelopeError("task was invalid")
    _ascii(policy["language"], "language")
    if _bounded_int(policy["chunk_seconds"], "chunk seconds") != 300:
        raise EnvelopeError("chunk seconds were not fixed")
    if _bounded_int(policy["overlap_seconds"], "overlap seconds") != 5:
        raise EnvelopeError("overlap seconds were not fixed")
    _match(policy["fixture_sha256"], LOWER_HEX_64_RE, "fixture checksum")
    _match(
        policy["priority_policy_sha256"], LOWER_HEX_64_RE, "priority policy checksum"
    )


def _validated_cycles(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != CYCLE_COUNT:
        raise EnvelopeError("cycle count was not exact")
    cycles = [_validate_cycle(cycle, index) for index, cycle in enumerate(value, 1)]
    container_ids = [cycle["container_id_sha256"] for cycle in cycles]
    if len(set(container_ids)) != CYCLE_COUNT:
        raise EnvelopeError("container identities were not distinct")
    return cycles


def build_envelope(
    draft: Mapping[str, Any], *, generator_sha256: str
) -> dict[str, Any]:
    """Validate a cycle draft and derive the immutable v1 artifact."""

    if not isinstance(draft, Mapping):
        raise EnvelopeError("draft root was not an object")
    document = dict(draft)
    _exact_object(document, TOP_KEYS, "draft")
    _validate_identity(document, draft=True)
    measurement = _exact_object(
        document["measurement"], {"cycles"}, "draft measurement"
    )
    cycles = _validated_cycles(measurement["cycles"])
    _match(generator_sha256, LOWER_HEX_64_RE, "generator checksum")
    maximum = max(
        sample for cycle in cycles for sample in cycle["candidate_bytes_samples"]
    )
    if maximum > MAX_INT - MARGIN_BYTES:
        raise EnvelopeError("allowed unloaded bytes overflowed")

    backend = dict(document["backend"])
    backend["generator_sha256"] = generator_sha256
    document["backend"] = backend
    document["measurement"] = {
        "cycles": cycles,
        "cycle_count": CYCLE_COUNT,
        "samples_per_cycle": SAMPLES_PER_CYCLE,
        "interval_seconds": INTERVAL_SECONDS,
        "margin_bytes": MARGIN_BYTES,
        "max_observed_candidate_bytes": maximum,
        "allowed_unloaded_bytes": maximum + MARGIN_BYTES,
    }
    validate_envelope(document)
    return document


def validate_envelope(
    value: Any, *, expected_generator_sha256: str | None = None
) -> dict[str, Any]:
    document = _exact_object(value, TOP_KEYS, "envelope")
    if document["schema"] != SCHEMA:
        raise EnvelopeError("envelope schema was invalid")
    _validate_identity(document, draft=False)
    if expected_generator_sha256 is not None:
        _match(
            expected_generator_sha256, LOWER_HEX_64_RE, "expected generator checksum"
        )
        if document["backend"]["generator_sha256"] != expected_generator_sha256:
            raise EnvelopeError("generator checksum binding changed")

    measurement = _exact_object(
        document["measurement"], MEASUREMENT_KEYS, "measurement"
    )
    cycles = _validated_cycles(measurement["cycles"])
    fixed = {
        "cycle_count": CYCLE_COUNT,
        "samples_per_cycle": SAMPLES_PER_CYCLE,
        "interval_seconds": INTERVAL_SECONDS,
        "margin_bytes": MARGIN_BYTES,
    }
    for key, expected in fixed.items():
        if _bounded_int(measurement[key], key) != expected:
            raise EnvelopeError(f"{key} was not fixed")
    maximum = max(
        sample for cycle in cycles for sample in cycle["candidate_bytes_samples"]
    )
    if maximum > MAX_INT - MARGIN_BYTES:
        raise EnvelopeError("allowed unloaded bytes overflowed")
    if (
        _bounded_int(
            measurement["max_observed_candidate_bytes"], "maximum observed bytes"
        )
        != maximum
    ):
        raise EnvelopeError("maximum observed bytes were not derived")
    if (
        _bounded_int(measurement["allowed_unloaded_bytes"], "allowed unloaded bytes")
        != maximum + MARGIN_BYTES
    ):
        raise EnvelopeError("allowed unloaded bytes were not derived")
    return document


def parse_nvidia_compute_apps(
    stdout: str,
    *,
    candidate_pids: Iterable[int],
    expected_gpu_uuid: str,
) -> int:
    """Return candidate-attributed bytes from one exact NVIDIA query.

    A valid result with no candidate row is zero.  Any malformed row, duplicate,
    candidate row on another GPU, unknown unit, or overflow fails closed.
    """

    _match(expected_gpu_uuid, GPU_UUID_RE, "GPU UUID")
    pids = set(candidate_pids)
    if not pids or any(
        isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 for pid in pids
    ):
        raise EnvelopeError("candidate PID set was invalid")
    if not isinstance(stdout, str) or "\x00" in stdout or "\r" in stdout:
        raise EnvelopeError("NVIDIA query output was malformed")
    seen: set[tuple[int, str]] = set()
    total = 0
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            raise EnvelopeError("NVIDIA query contained an empty row")
        fields = [field.strip() for field in raw_line.split(",")]
        if len(fields) != 3 or not fields[0].isdigit() or not fields[2].isdigit():
            raise EnvelopeError("NVIDIA query row was malformed")
        pid = int(fields[0], 10)
        gpu_uuid = fields[1]
        used_mib = int(fields[2], 10)
        if pid <= 0 or GPU_UUID_RE.fullmatch(gpu_uuid) is None:
            raise EnvelopeError("NVIDIA query row identity was malformed")
        row = (pid, gpu_uuid)
        if row in seen:
            raise EnvelopeError("NVIDIA query contained a duplicate row")
        seen.add(row)
        if pid not in pids:
            continue
        if gpu_uuid != expected_gpu_uuid:
            raise EnvelopeError("candidate PID appeared on another GPU")
        if used_mib > MAX_INT // (1024 * 1024):
            raise EnvelopeError("NVIDIA candidate bytes overflowed")
        amount = used_mib * 1024 * 1024
        if total > MAX_INT - amount:
            raise EnvelopeError("NVIDIA candidate total overflowed")
        total += amount
    return total


def container_id_sha256(full_container_id: str) -> str:
    """Bind the exact canonical full Docker ID, without a newline."""

    _match(full_container_id, LOWER_HEX_64_RE, "full container ID")
    return hashlib.sha256(full_container_id.encode("ascii")).hexdigest()


def inference_result_sha256(payload: bytes) -> str:
    """Bind the exact closed/fsynced canonical disposable SRT payload."""

    if (
        not isinstance(payload, bytes)
        or not payload
        or payload.startswith(b"\xef\xbb\xbf")
        or b"\r" in payload
        or not payload.endswith(b"\n")
        or payload.endswith(b"\n\n")
    ):
        raise EnvelopeError("inference result bytes were not canonical SRT")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvelopeError("inference result was not UTF-8") from exc
    return hashlib.sha256(payload).hexdigest()


def stable_candidate_sample(
    *,
    resolve_candidate_pids: Callable[[], Iterable[int]],
    run_query: Callable[[Sequence[str]], str],
    expected_gpu_uuid: str,
) -> int:
    """Resolve candidate PIDs before and after the exact query and require stability."""

    before = frozenset(resolve_candidate_pids())
    if not before:
        raise EnvelopeError("candidate PID set was unresolved")
    stdout = run_query(NVIDIA_QUERY)
    after = frozenset(resolve_candidate_pids())
    if before != after:
        raise EnvelopeError("candidate PID set changed during NVIDIA query")
    return parse_nvidia_compute_apps(
        stdout,
        candidate_pids=before,
        expected_gpu_uuid=expected_gpu_uuid,
    )


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        count = os.write(fd, view[offset:])
        if count <= 0:
            raise EnvelopeError("artifact write stalled")
        offset += count


def publish_create_once(path: Path, payload: bytes) -> str:
    """Publish canonical bytes create-once, privately, and durably."""

    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise EnvelopeError("output path was not absolute")
    try:
        parent_lstat = path.parent.lstat()
        resolved_parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise EnvelopeError("output parent was unavailable") from exc
    if (
        resolved_parent != path.parent.absolute()
        or stat.S_ISLNK(parent_lstat.st_mode)
        or not stat.S_ISDIR(parent_lstat.st_mode)
    ):
        raise EnvelopeError("output parent type was unsafe")
    owner = os.geteuid() if hasattr(os, "geteuid") else None
    if owner is not None and (
        parent_lstat.st_uid != owner or stat.S_IMODE(parent_lstat.st_mode) != 0o700
    ):
        raise EnvelopeError("output parent was not owner-only")
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise EnvelopeError("output path could not be inspected") from exc
    else:
        raise EnvelopeError("output already existed")

    suffix = hashlib.sha256(payload).hexdigest()[:16]
    partial = path.parent / f".{path.name}.{os.getpid()}.{suffix}.partial"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd: int | None = None
    linked = False
    try:
        fd = os.open(partial, flags, 0o600)
        _write_all(fd, payload)
        os.fsync(fd)
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_size != len(payload)
        ):
            raise EnvelopeError("partial artifact metadata was unsafe")
        if owner is not None and (
            item.st_uid != owner or stat.S_IMODE(item.st_mode) != 0o600
        ):
            raise EnvelopeError("partial artifact was not owner-only")
        os.close(fd)
        fd = None
        os.link(partial, path, follow_symlinks=False)
        linked = True
        partial.unlink()
        if os.name != "nt":
            directory_fd = os.open(
                path.parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        final = path.lstat()
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or final.st_size != len(payload)
        ):
            raise EnvelopeError("published artifact metadata was unsafe")
        if owner is not None and (
            final.st_uid != owner or stat.S_IMODE(final.st_mode) != 0o600
        ):
            raise EnvelopeError("published artifact was not owner-only")
    except FileExistsError as exc:
        raise EnvelopeError("output already existed") from exc
    except OSError as exc:
        raise EnvelopeError("artifact publication failed") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if not linked:
            try:
                partial.unlink()
            except FileNotFoundError:
                pass
    return hashlib.sha256(payload).hexdigest()


def source_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _read_bounded(path: Path) -> bytes:
    try:
        item = path.lstat()
    except OSError as exc:
        raise EnvelopeError("input was unavailable") from exc
    if (
        stat.S_ISLNK(item.st_mode)
        or not stat.S_ISREG(item.st_mode)
        or item.st_size <= 0
        or item.st_size > MAX_DOCUMENT_BYTES
    ):
        raise EnvelopeError("input was not a bounded regular file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EnvelopeError("input could not be opened") from exc
    try:
        opened = os.fstat(fd)
        identity = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
        if os.name != "nt":
            identity += ("st_ctime_ns",)
        if not stat.S_ISREG(opened.st_mode) or any(
            getattr(opened, key) != getattr(item, key) for key in identity
        ):
            raise EnvelopeError("input identity changed before read")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, MAX_DOCUMENT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_DOCUMENT_BYTES:
                raise EnvelopeError("input exceeded its byte boundary")
        after = os.fstat(fd)
        if total != opened.st_size or any(
            getattr(after, key) != getattr(opened, key) for key in identity
        ):
            raise EnvelopeError("input changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--draft", type=Path, help="strict JSON cycle draft to seal")
    mode.add_argument("--validate", type=Path, help="canonical envelope to validate")
    result.add_argument("--output", type=Path, help="absolute create-once output path")
    result.add_argument("--expected-sha256", help="optional exact artifact SHA-256")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    own_digest = source_sha256()
    if args.draft is not None:
        if args.output is None:
            raise EnvelopeError("draft mode required output")
        draft = parse_json(_read_bounded(args.draft), require_canonical=False)
        envelope = build_envelope(draft, generator_sha256=own_digest)
        payload = canonical_json_line(envelope)
        digest = publish_create_once(args.output, payload)
    else:
        if args.output is not None:
            raise EnvelopeError("validate mode did not accept output")
        payload = _read_bounded(args.validate)
        envelope = parse_json(payload, require_canonical=True)
        validate_envelope(envelope, expected_generator_sha256=own_digest)
        digest = hashlib.sha256(payload).hexdigest()
    if args.expected_sha256 is not None:
        _match(args.expected_sha256, LOWER_HEX_64_RE, "expected artifact checksum")
        if digest != args.expected_sha256:
            raise EnvelopeError("artifact checksum changed")
    print(f"UNLOADED_GPU_ENVELOPE_OK sha256={digest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EnvelopeError as exc:
        print(f"UNLOADED_GPU_ENVELOPE_FAIL code={exc.code}", file=sys.stderr)
        raise SystemExit(2) from None
