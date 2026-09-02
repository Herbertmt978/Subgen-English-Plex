"""Isolated, package-owned source proof for the Task 12 publisher."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_tools.task12 import (  # noqa: E402
    CANONICAL_GIT_REMOTE_URL,
    CANONICAL_REPOSITORY,
    ImageIdentity,
    PublicationBlocked,
    _strict_json_object,
    canonical_json_bytes,
)


_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_NOTES_PATH = "docs/RELEASE_NOTES_0.5.0.md"
_EVIDENCE_PATH = (
    "docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "90-evidence.md"
)
_OBSERVER_PATH = (
    "docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "runtime_gate_observer.py"
)
_OBSERVER_TEST_PATH = (
    "docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "test_runtime_gate_observer.py"
)
_SAMPLER_PATH = (
    "docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "gate_health_sampler.py"
)
_SAMPLER_TEST_PATH = (
    "docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "test_gate_health_sampler.py"
)
_PRODUCER_PATH = "monitor_frigate_priority.py"
_SOURCE_PROOF_PATH = "release_tools/source_proof.py"
_SOURCE_PROOF_REQUEST_SCHEMA = "subgen.task12.source-proof-request/v2"
_RELEASE_BINDING_PREFIX = "Task-11B-Sampler-Binding: "
_VERIFIER_PATH_KEYS = {
    "gate_seal": "--gate-seal",
    "phase_a_seal": "--phase-a-seal",
    "phase_a_output": "--phase-a-output",
    "phase_b_seal": "--phase-b-seal",
    "assertion_observation": "--assertion-observation",
    "phase_a_receipt_trace": "--phase-a-receipt-trace",
    "phase_b_receipt_trace": "--phase-b-receipt-trace",
    "candidate_identity_record": "--candidate-identity-record",
    "execution_boundary_manifest": "--execution-boundary-manifest",
    "priority_policy": "--priority-policy",
    "unloaded_gpu_envelope": "--unloaded-gpu-envelope",
    "model_envelope_catalog": "--model-envelope-catalog",
}
_MIB = 1024 * 1024
_VERIFIER_INPUT_LIMITS = {
    "gate_seal": 2 * _MIB,
    "phase_a_seal": 2 * _MIB,
    "phase_a_output": 64 * _MIB,
    "phase_b_seal": 4 * _MIB,
    "assertion_observation": 4 * 1024,
    "phase_a_receipt_trace": 8 * _MIB,
    "phase_b_receipt_trace": 8 * _MIB,
    "candidate_identity_record": 2 * _MIB,
    "execution_boundary_manifest": 4 * _MIB,
    "priority_policy": 32 * 1024,
    "unloaded_gpu_envelope": 512 * 1024,
    "model_envelope_catalog": 4 * _MIB,
}
_PROFILER_ATTEMPTS_KEY = "profiler_attempts"
_PROFILER_ATTEMPT_OPTIONS = (
    ("evidence", "--profiler-evidence", 16 * _MIB),
    ("evidence_seal", "--profiler-evidence-seal", 512 * 1024),
    ("boundary_manifest", "--profiler-boundary-manifest", 4 * _MIB),
)
_PROFILER_ATTEMPT_PATH_KEYS = tuple(
    key for key, _option, _maximum in _PROFILER_ATTEMPT_OPTIONS
)
_MAX_PROFILER_ATTEMPTS = 5
_VERIFIER_INPUT_AGGREGATE_MAX_BYTES = 128 * _MIB
_RUNTIME_TO_RELEASE_STATUS = (
    "M\tdocs/aegis/INDEX.md\n"
    "M\tdocs/aegis/adr/0002-memory-aware-segmented-transcription.md\n"
    "A\tdocs/aegis/baseline/2026-09-01-v0.5.0-release-baseline.md\n"
    "M\tdocs/aegis/plans/2026-08-31-memory-aware-segmented-transcription-v0.5.0.md\n"
    "M\tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "20-checkpoint.md\n"
    "M\tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "90-evidence.md\n"
    "M\tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "drift-check-draft.json\n"
    "M\tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "gate_health_sampler.py\n"
    "M\tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "resume-state-hint.json\n"
    "M\tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "runtime_gate_observer.py\n"
    "M\tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "test_gate_health_sampler.py\n"
    "M\tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "test_runtime_gate_observer.py\n"
    "M\tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/"
    "todo-checkpoint-draft.json\n"
    "M\trelease_tools/adapters.py\n"
    "M\trelease_tools/cli.py\n"
    "M\trelease_tools/source_proof.py\n"
    "M\ttests/test_task12_publisher.py\n"
    "M\ttests/test_task12_source_hardening.py\n"
).encode("utf-8")


def _block(code: str) -> None:
    raise PublicationBlocked(code)


def _git_executable() -> str:
    """Return a fixed Git executable for the release host, with a test-host fallback."""
    if os.name != "nt":
        candidate = Path("/usr/bin/git")
    else:
        discovered = shutil.which("git")
        if discovered is None:
            _block("source_git_executable_unavailable")
        candidate = Path(discovered)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PublicationBlocked("source_git_executable_unavailable") from exc
    if not resolved.is_absolute() or not resolved.is_file():
        _block("source_git_executable_unavailable")
    return str(resolved)


def _git_environment() -> dict[str, str]:
    """Return a minimal, replacement-disabled environment for local Git reads."""
    return {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PAGER": "cat",
        "GIT_EXTERNAL_DIFF": "",
        "LC_ALL": "C",
    }


def _git(root: Path, *arguments: str, allow_status_one: bool = False) -> bytes:
    try:
        completed = subprocess.run(
            (
                _git_executable(),
                "--no-replace-objects",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "core.fsmonitor=false",
                *arguments,
            ),
            cwd=root,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PublicationBlocked("source_git_command_failed") from exc
    if completed.returncode == 1 and allow_status_one:
        return b""
    if completed.returncode != 0 or completed.stderr:
        _block("source_git_command_failed")
    return completed.stdout


def _reject_git_indirection(root: Path) -> None:
    """Reject repository state that can rewrite commit ancestry or object lookup."""
    directories: list[Path] = []
    for arguments in (
        ("rev-parse", "--absolute-git-dir"),
        ("rev-parse", "--path-format=absolute", "--git-common-dir"),
    ):
        raw_directory = _git(root, *arguments)
        try:
            decoded = raw_directory.decode("utf-8", errors="strict")
            if not decoded.endswith("\n") or "\n" in decoded[:-1]:
                _block("source_git_directory_invalid")
            directory = Path(decoded.rstrip("\n"))
            resolved_directory = directory.resolve(strict=True)
        except (OSError, UnicodeError) as exc:
            raise PublicationBlocked("source_git_directory_invalid") from exc
        if (
            not directory.is_absolute()
            or resolved_directory != directory
            or not directory.is_dir()
        ):
            _block("source_git_directory_invalid")
        if directory not in directories:
            directories.append(directory)
    for directory in directories:
        grafts = directory / "info" / "grafts"
        if grafts.exists() or grafts.is_symlink():
            _block("source_git_grafts_present")
    if _git(root, "for-each-ref", "--format=%(refname)", "refs/replace"):
        _block("source_git_replace_refs_present")
    if _git(root, "rev-parse", "--is-shallow-repository") != b"false\n":
        _block("source_shallow_repository_forbidden")


def _read_owner_only(
    path: Path,
    label: str,
    *,
    maximum: int,
    require_owner_only: bool = True,
) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PublicationBlocked(f"source_{label}_unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
            or (
                require_owner_only
                and os.name != "nt"
                and stat.S_IMODE(before.st_mode) & 0o077
            )
            or before.st_size > maximum
        ):
            _block(f"source_{label}_unsafe")
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        try:
            path_after = path.lstat()
        except OSError as exc:
            raise PublicationBlocked(f"source_{label}_changed") from exc

        def identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
            if os.name == "nt":
                return (item.st_dev, item.st_ino, item.st_size, 0, 0)
            return (
                item.st_dev,
                item.st_ino,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )

        if (
            len(payload) > maximum
            or len(payload) != before.st_size
            or identity(before) != identity(after)
            or identity(after) != identity(path_after)
        ):
            _block(f"source_{label}_changed")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _write_exact(root: Path, name: str, payload: bytes) -> Path:
    target = root / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise PublicationBlocked("source_materialization_failed") from exc
    if (
        _read_owner_only(target, "materialized_blob", maximum=max(len(payload), 1))
        != payload
    ):
        _block("source_materialization_failed")
    return target


def _normalize_verifier_inputs(raw: Any) -> dict[str, Any]:
    expected = set(_VERIFIER_PATH_KEYS) | {
        "binding_prefix",
        _PROFILER_ATTEMPTS_KEY,
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != expected
        or raw.get("binding_prefix") != _RELEASE_BINDING_PREFIX
    ):
        _block("source_release_verifier_inputs_invalid")

    result: dict[str, Any] = {"binding_prefix": _RELEASE_BINDING_PREFIX}
    for key in _VERIFIER_PATH_KEYS:
        value = raw.get(key)
        if not isinstance(value, str):
            _block("source_release_verifier_inputs_invalid")
        path = Path(value)
        try:
            if not path.is_absolute() or path.resolve(strict=True) != path:
                _block("source_release_verifier_inputs_invalid")
        except OSError as exc:
            raise PublicationBlocked("source_release_verifier_inputs_invalid") from exc
        result[key] = str(path)

    raw_attempts = raw.get(_PROFILER_ATTEMPTS_KEY)
    if (
        not isinstance(raw_attempts, list)
        or not raw_attempts
        or len(raw_attempts) > _MAX_PROFILER_ATTEMPTS
    ):
        _block("source_release_verifier_inputs_invalid")
    attempts: list[dict[str, str]] = []
    seen_profiler_paths: set[Path] = set()
    for raw_attempt in raw_attempts:
        if not isinstance(raw_attempt, dict) or set(raw_attempt) != set(
            _PROFILER_ATTEMPT_PATH_KEYS
        ):
            _block("source_release_verifier_inputs_invalid")
        attempt: dict[str, str] = {}
        for key in _PROFILER_ATTEMPT_PATH_KEYS:
            value = raw_attempt.get(key)
            if not isinstance(value, str):
                _block("source_release_verifier_inputs_invalid")
            path = Path(value)
            try:
                if not path.is_absolute() or path.resolve(strict=True) != path:
                    _block("source_release_verifier_inputs_invalid")
            except OSError as exc:
                raise PublicationBlocked(
                    "source_release_verifier_inputs_invalid"
                ) from exc
            if path in seen_profiler_paths:
                _block("source_release_verifier_inputs_invalid")
            seen_profiler_paths.add(path)
            attempt[key] = str(path)
        attempts.append(attempt)
    result[_PROFILER_ATTEMPTS_KEY] = attempts
    return result


def _verifier_inputs(document: dict[str, Any]) -> dict[str, Any]:
    return _normalize_verifier_inputs(document.get("release_verifier_inputs"))


def _capture_verifier_inputs(
    verifier_inputs: dict[str, Any],
) -> tuple[dict[str, bytes], list[dict[str, bytes]]]:
    """Capture each distinct external verifier file once, then bind every input."""
    verifier_inputs = _normalize_verifier_inputs(verifier_inputs)
    path_keys: dict[Path, list[str]] = {}
    path_limits: dict[Path, list[int]] = {}
    for key in _VERIFIER_PATH_KEYS:
        path = Path(verifier_inputs[key])
        path_keys.setdefault(path, []).append(key)
        path_limits.setdefault(path, []).append(_VERIFIER_INPUT_LIMITS[key])
    profiler_attempts = verifier_inputs[_PROFILER_ATTEMPTS_KEY]
    for index, attempt in enumerate(profiler_attempts):
        for key, _option, maximum in _PROFILER_ATTEMPT_OPTIONS:
            path = Path(attempt[key])
            logical_key = f"profiler_attempt_{index}_{key}"
            path_keys.setdefault(path, []).append(logical_key)
            path_limits.setdefault(path, []).append(maximum)

    captured_by_path: dict[Path, bytes] = {}
    for path, keys in path_keys.items():
        captured_by_path[path] = _read_owner_only(
            path,
            f"release_verifier_input_{keys[0]}",
            maximum=min(path_limits[path]),
        )

    captured = {
        key: captured_by_path[Path(verifier_inputs[key])] for key in _VERIFIER_PATH_KEYS
    }
    captured_profiler_attempts = [
        {
            key: captured_by_path[Path(attempt[key])]
            for key in _PROFILER_ATTEMPT_PATH_KEYS
        }
        for attempt in profiler_attempts
    ]
    if (
        sum(len(payload) for payload in captured.values())
        + sum(
            len(payload)
            for attempt in captured_profiler_attempts
            for payload in attempt.values()
        )
        > _VERIFIER_INPUT_AGGREGATE_MAX_BYTES
    ):
        _block("source_release_verifier_inputs_too_large")
    return captured, captured_profiler_attempts


def _derive_candidate_identity(
    captured_inputs: dict[str, bytes], image: ImageIdentity
) -> tuple[ImageIdentity, str]:
    candidate = _strict_json_object(
        captured_inputs["candidate_identity_record"],
        "candidate_identity_record",
    )
    if candidate.get("schema") != "subgen.task11b.candidate-identity/v2":
        _block("source_candidate_identity_invalid")
    identity = candidate.get("candidate_identity")
    if not isinstance(identity, dict):
        _block("source_candidate_identity_invalid")
    layer_diff_ids = identity.get("layer_diff_ids")
    if not isinstance(layer_diff_ids, list):
        _block("source_candidate_identity_invalid")
    observed = ImageIdentity(
        oci_index=identity.get("oci_index"),
        config_digest=identity.get("config_digest"),
        ordered_diff_ids=tuple(layer_diff_ids),
        revision_label=identity.get("runtime_commit"),
    )
    observed.validate()
    if observed != image:
        _block("source_candidate_identity_mismatch")

    boundary = _strict_json_object(
        captured_inputs["execution_boundary_manifest"],
        "execution_boundary_manifest",
    )
    daemon_identity = boundary.get("docker_daemon_identity")
    if not isinstance(daemon_identity, dict) or set(daemon_identity) != {
        "schema",
        "engine_id_sha256",
        "host_boot_id_sha256",
        "docker_host",
        "os_type",
    }:
        _block("source_candidate_docker_identity_invalid")
    engine_id_sha256 = daemon_identity.get("engine_id_sha256")
    host_boot_id_sha256 = daemon_identity.get("host_boot_id_sha256")
    if (
        daemon_identity.get("schema") != "subgen.task11b.docker-daemon/v1"
        or daemon_identity.get("docker_host") != "unix:///var/run/docker.sock"
        or daemon_identity.get("os_type") != "linux"
        or not isinstance(engine_id_sha256, str)
        or SHA256_RE.fullmatch(engine_id_sha256) is None
        or not isinstance(host_boot_id_sha256, str)
        or SHA256_RE.fullmatch(host_boot_id_sha256) is None
        or candidate.get("docker_daemon_identity_sha256")
        != hashlib.sha256(canonical_json_bytes(daemon_identity)).hexdigest()
    ):
        _block("source_candidate_docker_identity_invalid")
    return observed, engine_id_sha256


def _run_release_verifier(
    root: Path,
    *,
    runtime: str,
    sampler: str,
    release: str,
    expected_receipt: bytes,
    image: ImageIdentity,
    verifier_inputs: dict[str, Any],
) -> tuple[ImageIdentity, str]:
    release_payloads = {
        "90-evidence.md": _git(root, "show", f"{release}:{_EVIDENCE_PATH}"),
        "runtime_gate_observer.py": _git(root, "show", f"{release}:{_OBSERVER_PATH}"),
        "test_runtime_gate_observer.py": _git(
            root, "show", f"{release}:{_OBSERVER_TEST_PATH}"
        ),
        "gate_health_sampler.py": _git(root, "show", f"{release}:{_SAMPLER_PATH}"),
        "test_gate_health_sampler.py": _git(
            root, "show", f"{release}:{_SAMPLER_TEST_PATH}"
        ),
        "monitor_frigate_priority.py": _git(
            root, "show", f"{runtime}:{_PRODUCER_PATH}"
        ),
    }
    for path, name in (
        (_OBSERVER_PATH, "runtime_gate_observer.py"),
        (_OBSERVER_TEST_PATH, "test_runtime_gate_observer.py"),
        (_SAMPLER_PATH, "gate_health_sampler.py"),
        (_SAMPLER_TEST_PATH, "test_gate_health_sampler.py"),
    ):
        if _git(root, "show", f"{sampler}:{path}") != release_payloads[name]:
            _block("source_sampler_release_blob_mismatch")
    if (
        _git(root, "show", f"{release}:{_PRODUCER_PATH}")
        != release_payloads["monitor_frigate_priority.py"]
    ):
        _block("source_runtime_release_producer_mismatch")

    verifier_inputs = _normalize_verifier_inputs(verifier_inputs)
    captured_inputs, captured_profiler_attempts = _capture_verifier_inputs(
        verifier_inputs
    )
    observed, engine_id_sha256 = _derive_candidate_identity(captured_inputs, image)

    materialized = Path(tempfile.mkdtemp(prefix="subgen-v050-release-verifier-"))
    cleanup_failed = False
    try:
        os.chmod(materialized, 0o700)
        if any(materialized.iterdir()):
            _block("source_materialization_not_empty")
        files = {
            name: _write_exact(materialized, name, payload)
            for name, payload in release_payloads.items()
        }
        input_files = {
            key: _write_exact(
                materialized,
                f"verifier-input-{key}.bin",
                captured_inputs[key],
            )
            for key in _VERIFIER_PATH_KEYS
        }
        profiler_input_files = [
            {
                key: _write_exact(
                    materialized,
                    f"verifier-input-profiler-{index:03d}-{key}.bin",
                    attempt[key],
                )
                for key in _PROFILER_ATTEMPT_PATH_KEYS
            }
            for index, attempt in enumerate(captured_profiler_attempts)
        ]
        arguments = [
            sys.executable,
            "-I",
            str(files["runtime_gate_observer.py"]),
            "verify-release",
            "--evidence",
            str(files["90-evidence.md"]),
            "--binding-prefix",
            verifier_inputs["binding_prefix"],
        ]
        for key, option in _VERIFIER_PATH_KEYS.items():
            arguments.extend((option, str(input_files[key])))
        for attempt in profiler_input_files:
            for key, option, _maximum in _PROFILER_ATTEMPT_OPTIONS:
                arguments.extend((option, str(attempt[key])))
        arguments.extend(
            (
                "--producer-source",
                str(files["monitor_frigate_priority.py"]),
                "--sampler-source",
                str(files["gate_health_sampler.py"]),
                "--sampler-test-source",
                str(files["test_gate_health_sampler.py"]),
                "--observer-test-source",
                str(files["test_runtime_gate_observer.py"]),
                "--runtime-commit",
                runtime,
                "--candidate-oci-index",
                image.oci_index,
                "--candidate-config-digest",
                image.config_digest,
            )
        )
        try:
            completed = subprocess.run(
                arguments,
                cwd=materialized,
                env={
                    "PATH": os.defpath,
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                check=False,
                timeout=900,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PublicationBlocked("source_release_verifier_failed") from exc
        if (
            completed.returncode != 0
            or completed.stderr
            or completed.stdout != expected_receipt
            or expected_receipt != b"TASK11B_RELEASE_VERIFY_OK\n"
        ):
            _block("source_release_verifier_failed")
    finally:
        try:
            shutil.rmtree(materialized)
        except OSError:
            cleanup_failed = True
    if cleanup_failed or materialized.exists():
        _block("source_materialization_cleanup_failed")
    return observed, engine_id_sha256


def _require_sha(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or _FULL_SHA.fullmatch(value) is None:
        _block("source_proof_request_invalid")
    return value


def _workflow_is_manual_only(payload: bytes) -> bool:
    try:
        lines = payload.decode("utf-8", errors="strict").splitlines()
    except UnicodeError:
        return False
    declarations: list[set[str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or line[:1].isspace():
            continue
        if not (line.startswith("on:") or line.startswith("'on':")):
            continue
        inline = line.split(":", 1)[1].strip()
        if inline:
            if inline not in {"workflow_dispatch", "[workflow_dispatch]"}:
                return False
            declarations.append({"workflow_dispatch"})
            continue
        block: list[tuple[int, str]] = []
        for candidate in lines[index + 1 :]:
            if candidate and not candidate[:1].isspace():
                break
            stripped_candidate = candidate.strip()
            if not stripped_candidate or stripped_candidate.startswith("#"):
                continue
            indentation = len(candidate) - len(candidate.lstrip())
            block.append((indentation, stripped_candidate))
        if not block:
            return False
        trigger_indent = min(indentation for indentation, _value in block)
        triggers: list[str] = []
        for indentation, candidate in block:
            if indentation != trigger_indent:
                continue
            if candidate.startswith("-"):
                triggers.append(candidate[1:].strip().rstrip(":"))
            elif ":" in candidate:
                triggers.append(candidate.split(":", 1)[0].strip(" '\""))
        if not triggers:
            return False
        declarations.append(set(triggers))
    return len(declarations) == 1 and declarations[0] == {"workflow_dispatch"}


def build_source_proof(raw: bytes) -> bytes:
    document = _strict_json_object(raw, "source_proof_request")
    expected = {
        "schema",
        "binding_sha256",
        "repository_root",
        "repository",
        "prior_main_commit",
        "runtime_commit",
        "sampler_commit",
        "release_commit",
        "annotated_tag_object",
        "annotated_tag_target",
        "annotated_tag_name",
        "annotated_tag_message",
        "annotated_tagger_name",
        "annotated_tagger_email",
        "annotated_tagger_date",
        "release_notes_blob",
        "release_notes_base64",
        "task11b_verifier_receipt_sha256",
        "task11b_verifier_receipt_base64",
        "release_verifier_inputs",
        "image",
    }
    if (
        set(document) != expected
        or document["schema"] != _SOURCE_PROOF_REQUEST_SCHEMA
        or canonical_json_bytes(document) != raw
        or document["repository"] != CANONICAL_REPOSITORY
    ):
        _block("source_proof_request_invalid")
    root_value = document["repository_root"]
    if not isinstance(root_value, str):
        _block("source_repository_root_invalid")
    root = Path(root_value)
    try:
        if not root.is_absolute() or root.resolve(strict=True) != root:
            _block("source_repository_root_invalid")
    except OSError as exc:
        raise PublicationBlocked("source_repository_root_invalid") from exc

    prior = _require_sha(document, "prior_main_commit")
    runtime = _require_sha(document, "runtime_commit")
    sampler = _require_sha(document, "sampler_commit")
    release = _require_sha(document, "release_commit")
    tag_object = _require_sha(document, "annotated_tag_object")
    tag_target = _require_sha(document, "annotated_tag_target")
    notes_blob = _require_sha(document, "release_notes_blob")
    if tag_target != release:
        _block("source_annotated_tag_mismatch")

    _reject_git_indirection(root)
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        _block("source_worktree_not_clean")
    if _git(root, "rev-parse", "--verify", "HEAD^{commit}") != (release + "\n").encode(
        "ascii"
    ):
        _block("source_head_not_release_commit")
    top_level = Path(
        _git(root, "rev-parse", "--show-toplevel")
        .decode("utf-8", errors="strict")
        .rstrip("\n")
    ).resolve(strict=True)
    if top_level != root:
        _block("source_repository_root_invalid")
    if _git(root, "show", f"{release}:{_SOURCE_PROOF_PATH}") != _read_owner_only(
        Path(__file__).resolve(strict=True),
        "source_proof",
        maximum=2 * 1024 * 1024,
        require_owner_only=False,
    ):
        _block("source_proof_not_release_blob")
    remote = _git(root, "remote", "get-url", "--push", "origin")
    if remote != (CANONICAL_GIT_REMOTE_URL + "\n").encode("ascii"):
        _block("source_git_remote_not_exact")
    for commit in (prior, runtime, sampler, release):
        _git(root, "cat-file", "-e", f"{commit}^{{commit}}")
    for ancestor, descendant in (
        (prior, runtime),
        (runtime, sampler),
        (sampler, release),
    ):
        try:
            completed = subprocess.run(
                (
                    _git_executable(),
                    "--no-replace-objects",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-c",
                    "core.fsmonitor=false",
                    "merge-base",
                    "--is-ancestor",
                    ancestor,
                    descendant,
                ),
                cwd=root,
                env=_git_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PublicationBlocked("source_git_command_failed") from exc
        if completed.returncode != 0 or completed.stdout or completed.stderr:
            _block("source_commit_ancestry_invalid")
    if (
        _git(
            root,
            "diff",
            "--name-status",
            "--no-renames",
            runtime,
            release,
            "--",
        )
        != _RUNTIME_TO_RELEASE_STATUS
    ):
        _block("source_runtime_release_status_manifest_mismatch")

    if _git(root, "cat-file", "-t", tag_object) != b"tag\n":
        _block("source_annotated_tag_mismatch")
    tag_payload = _git(root, "cat-file", "-p", tag_object)
    try:
        tag_headers, tag_message = tag_payload.split(b"\n\n", 1)
        header_lines = tag_headers.decode("utf-8", errors="strict").splitlines()
    except (ValueError, UnicodeError) as exc:
        raise PublicationBlocked("source_annotated_tag_mismatch") from exc
    headers = {
        key: value for key, value in (line.split(" ", 1) for line in header_lines)
    }
    expected_message = document["annotated_tag_message"]
    tagger_name = document["annotated_tagger_name"]
    tagger_email = document["annotated_tagger_email"]
    tagger_date = document["annotated_tagger_date"]
    if not all(
        isinstance(value, str) for value in (tagger_name, tagger_email, tagger_date)
    ):
        _block("source_annotated_tag_mismatch")
    try:
        parsed_date = datetime.fromisoformat(tagger_date.replace("Z", "+00:00"))
        offset = parsed_date.strftime("%z")
        expected_tagger = (
            f"{tagger_name} <{tagger_email}> {int(parsed_date.timestamp())} {offset}"
        )
    except (OverflowError, ValueError) as exc:
        raise PublicationBlocked("source_annotated_tag_mismatch") from exc
    if (
        headers.get("object") != release
        or headers.get("type") != "commit"
        or headers.get("tag") != document["annotated_tag_name"]
        or headers.get("tagger") != expected_tagger
        or not isinstance(expected_message, str)
        or tag_message != expected_message.encode("utf-8")
    ):
        _block("source_annotated_tag_mismatch")

    observed_blob = _git(root, "rev-parse", f"{release}:{_RELEASE_NOTES_PATH}")
    if observed_blob != (notes_blob + "\n").encode("ascii"):
        _block("source_release_notes_blob_mismatch")
    encoded_notes = document["release_notes_base64"]
    if not isinstance(encoded_notes, str):
        _block("source_release_notes_blob_mismatch")
    try:
        expected_notes = base64.b64decode(encoded_notes, validate=True)
    except ValueError as exc:
        raise PublicationBlocked("source_release_notes_blob_mismatch") from exc
    if _git(root, "show", f"{release}:{_RELEASE_NOTES_PATH}") != expected_notes:
        _block("source_release_notes_blob_mismatch")

    workflows = (
        _git(
            root,
            "ls-tree",
            "-r",
            "--name-only",
            release,
            "--",
            ".github/workflows",
        )
        .decode("utf-8", errors="strict")
        .splitlines()
    )
    if any(
        not _workflow_is_manual_only(_git(root, "show", f"{release}:{workflow}"))
        for workflow in workflows
    ):
        _block("source_hosted_workflow_trigger_present")

    image = document["image"]
    if not isinstance(image, dict) or set(image) != {
        "oci_index",
        "config_digest",
        "ordered_diff_ids",
        "revision_label",
    }:
        _block("source_image_identity_invalid")
    diff_ids = image["ordered_diff_ids"]
    if not isinstance(diff_ids, list):
        _block("source_image_identity_invalid")
    identity = ImageIdentity(
        oci_index=image["oci_index"],
        config_digest=image["config_digest"],
        ordered_diff_ids=tuple(diff_ids),
        revision_label=image["revision_label"],
    )
    identity.validate()
    receipt_base64 = document["task11b_verifier_receipt_base64"]
    receipt_sha256 = document["task11b_verifier_receipt_sha256"]
    if not isinstance(receipt_base64, str) or not isinstance(receipt_sha256, str):
        _block("source_release_verifier_receipt_invalid")
    try:
        expected_receipt = base64.b64decode(receipt_base64, validate=True)
    except ValueError as exc:
        raise PublicationBlocked("source_release_verifier_receipt_invalid") from exc
    if hashlib.sha256(expected_receipt).hexdigest() != receipt_sha256:
        _block("source_release_verifier_receipt_invalid")
    observed_identity, candidate_docker_engine_id_sha256 = _run_release_verifier(
        root,
        runtime=runtime,
        sampler=sampler,
        release=release,
        expected_receipt=expected_receipt,
        image=identity,
        verifier_inputs=_verifier_inputs(document),
    )
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        _block("source_worktree_changed_during_proof")
    proof = {
        "schema": "subgen.task12.source-proof/v1",
        "binding_sha256": document["binding_sha256"],
        "clean_worktree": True,
        "workflows_manual_only": True,
        "runtime_commit": runtime,
        "sampler_commit": sampler,
        "release_commit": release,
        "runtime_is_ancestor_of_sampler": True,
        "sampler_is_ancestor_of_release": True,
        "annotated_tag_object": tag_object,
        "annotated_tag_target": release,
        "release_notes_blob": notes_blob,
        "release_notes_base64": encoded_notes,
        "task11b_verifier_receipt_sha256": document["task11b_verifier_receipt_sha256"],
        "candidate_docker_engine_id_sha256": candidate_docker_engine_id_sha256,
        "image": {
            "oci_index": observed_identity.oci_index,
            "config_digest": observed_identity.config_digest,
            "ordered_diff_ids": list(observed_identity.ordered_diff_ids),
            "revision_label": observed_identity.revision_label,
        },
        "git_remote_url": CANONICAL_GIT_REMOTE_URL,
    }
    return canonical_json_bytes(proof)


def main() -> int:
    try:
        sys.stdout.buffer.write(build_source_proof(sys.stdin.buffer.read()))
        return 0
    except PublicationBlocked as exc:
        print(exc.code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
