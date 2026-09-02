"""Explicit, fail-closed command-line entrypoint for Task 12 publication."""

from __future__ import annotations

import argparse
import base64
import os
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from .adapters import (
    AdapterConfig,
    ProfilerAttemptInputs,
    ReleaseVerifierInputs,
    SubprocessCommandRunner,
    Task12HttpCommandAdapter,
    UrllibHttpClient,
)
from .journal import FileReceiptJournal
from .task12 import (
    AnnotatedTag,
    ImageIdentity,
    PublicationBlocked,
    RegistryBlob,
    RegistryManifest,
    ReleaseIntent,
    Task12Publisher,
    _strict_json_object,
    canonical_json_bytes,
)


_RELEASE_VERIFIER_PATH_KEYS = {
    "gate_seal",
    "phase_a_seal",
    "phase_a_output",
    "phase_b_seal",
    "assertion_observation",
    "phase_a_receipt_trace",
    "phase_b_receipt_trace",
    "candidate_identity_record",
    "execution_boundary_manifest",
    "priority_policy",
    "unloaded_gpu_envelope",
    "model_envelope_catalog",
}
_RELEASE_BINDING_PREFIX = "Task-11B-Sampler-Binding: "
_PROFILER_ATTEMPT_PATH_KEYS = {
    "evidence",
    "evidence_seal",
    "boundary_manifest",
}
_MAX_PROFILER_ATTEMPTS = 5


def _read_owner_only(path: Path, label: str) -> bytes:
    if os.name == "nt":
        raise PublicationBlocked("owner_only_windows_acl_unverified")
    if not path.is_absolute():
        raise PublicationBlocked(f"{label}_path_not_absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PublicationBlocked(f"{label}_file_unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o077
        ):
            raise PublicationBlocked(f"{label}_file_not_owner_only")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        try:
            path_after = path.lstat()
        except OSError as exc:
            raise PublicationBlocked(f"{label}_file_changed") from exc
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        identity_path = (
            path_after.st_dev,
            path_after.st_ino,
            path_after.st_size,
            path_after.st_mtime_ns,
            path_after.st_ctime_ns,
        )
        if identity_before != identity_after or identity_after != identity_path:
            raise PublicationBlocked(f"{label}_file_changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _decode_base64(value: Any, label: str) -> bytes:
    if not isinstance(value, str):
        raise PublicationBlocked(f"{label}_base64_invalid")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise PublicationBlocked(f"{label}_base64_invalid") from exc


def _decode_blob(document: Any) -> RegistryBlob:
    if not isinstance(document, dict) or set(document) != {
        "digest",
        "size",
        "media_type",
        "payload_base64",
    }:
        raise PublicationBlocked("intent_blob_schema")
    return RegistryBlob(
        digest=document["digest"],
        size=document["size"],
        media_type=document["media_type"],
        payload=_decode_base64(document["payload_base64"], "intent_blob"),
    )


def _decode_manifest(document: Any) -> RegistryManifest:
    if not isinstance(document, dict) or set(document) != {
        "digest",
        "size",
        "media_type",
        "payload_base64",
    }:
        raise PublicationBlocked("intent_manifest_schema")
    return RegistryManifest(
        digest=document["digest"],
        size=document["size"],
        media_type=document["media_type"],
        payload=_decode_base64(document["payload_base64"], "intent_manifest"),
    )


def decode_intent(raw: bytes) -> ReleaseIntent:
    document = _strict_json_object(raw, "publication_intent")
    expected = {
        "schema",
        "repository",
        "image_repository",
        "prior_main_commit",
        "runtime_commit",
        "sampler_commit",
        "release_commit",
        "annotated_tag",
        "release_title",
        "release_notes_base64",
        "release_notes_blob",
        "task11b_verifier_receipt_base64",
        "task11b_verifier_receipt_sha256",
        "sealed_manifest_base64",
        "image",
        "required_blobs",
        "required_manifests",
        "prior_latest_digest",
        "main_ref",
        "version_tag",
        "lock_ref",
    }
    if (
        set(document) != expected
        or document["schema"] != "subgen.task12.publication-intent/v2"
        or canonical_json_bytes(document) != raw
    ):
        raise PublicationBlocked("publication_intent_schema")
    annotated = document["annotated_tag"]
    image = document["image"]
    blobs = document["required_blobs"]
    manifests = document["required_manifests"]
    if (
        not isinstance(annotated, dict)
        or set(annotated)
        != {
            "object_sha",
            "target_commit",
            "tag",
            "message",
            "tagger_name",
            "tagger_email",
            "tagger_date",
        }
        or not isinstance(image, dict)
        or set(image)
        != {"oci_index", "config_digest", "ordered_diff_ids", "revision_label"}
        or not isinstance(image["ordered_diff_ids"], list)
        or not isinstance(blobs, list)
        or not isinstance(manifests, list)
    ):
        raise PublicationBlocked("publication_intent_schema")
    intent = ReleaseIntent(
        repository=document["repository"],
        image_repository=document["image_repository"],
        prior_main_commit=document["prior_main_commit"],
        runtime_commit=document["runtime_commit"],
        sampler_commit=document["sampler_commit"],
        release_commit=document["release_commit"],
        annotated_tag=AnnotatedTag(**annotated),
        release_title=document["release_title"],
        release_notes=_decode_base64(document["release_notes_base64"], "release_notes"),
        release_notes_blob=document["release_notes_blob"],
        task11b_verifier_receipt=_decode_base64(
            document["task11b_verifier_receipt_base64"], "task11b_receipt"
        ),
        task11b_verifier_receipt_sha256=document["task11b_verifier_receipt_sha256"],
        sealed_manifest=_decode_base64(
            document["sealed_manifest_base64"], "sealed_manifest"
        ),
        image=ImageIdentity(
            oci_index=image["oci_index"],
            config_digest=image["config_digest"],
            ordered_diff_ids=tuple(image["ordered_diff_ids"]),
            revision_label=image["revision_label"],
        ),
        required_blobs=tuple(_decode_blob(item) for item in blobs),
        required_manifests=tuple(_decode_manifest(item) for item in manifests),
        prior_latest_digest=document["prior_latest_digest"],
        main_ref=document["main_ref"],
        version_tag=document["version_tag"],
        lock_ref=document["lock_ref"],
    )
    intent.validate()
    return intent


def _string_mapping(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise PublicationBlocked(f"{label}_invalid")
    return dict(value)


def _release_verifier_path(value: Any) -> Path:
    if not isinstance(value, str):
        raise PublicationBlocked("release_verifier_inputs_invalid")
    raw_path = Path(value)
    try:
        resolved_path = raw_path.resolve(strict=True)
    except OSError as exc:
        raise PublicationBlocked("release_verifier_inputs_invalid") from exc
    if (
        not raw_path.is_absolute()
        or resolved_path != raw_path
        or not raw_path.is_file()
    ):
        raise PublicationBlocked("release_verifier_inputs_invalid")
    return resolved_path


def _release_verifier_inputs(value: Any) -> ReleaseVerifierInputs:
    expected = _RELEASE_VERIFIER_PATH_KEYS | {
        "binding_prefix",
        "profiler_attempts",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("binding_prefix") != _RELEASE_BINDING_PREFIX
    ):
        raise PublicationBlocked("release_verifier_inputs_invalid")
    paths = {
        key: _release_verifier_path(value.get(key))
        for key in _RELEASE_VERIFIER_PATH_KEYS
    }
    raw_attempts = value.get("profiler_attempts")
    if (
        not isinstance(raw_attempts, list)
        or not raw_attempts
        or len(raw_attempts) > _MAX_PROFILER_ATTEMPTS
    ):
        raise PublicationBlocked("release_verifier_inputs_invalid")
    attempts: list[ProfilerAttemptInputs] = []
    seen_paths: set[Path] = set()
    for raw_attempt in raw_attempts:
        if (
            not isinstance(raw_attempt, dict)
            or set(raw_attempt) != _PROFILER_ATTEMPT_PATH_KEYS
        ):
            raise PublicationBlocked("release_verifier_inputs_invalid")
        evidence = _release_verifier_path(raw_attempt.get("evidence"))
        evidence_seal = _release_verifier_path(raw_attempt.get("evidence_seal"))
        boundary_manifest = _release_verifier_path(raw_attempt.get("boundary_manifest"))
        attempt_paths = {evidence, evidence_seal, boundary_manifest}
        if len(attempt_paths) != 3 or seen_paths.intersection(attempt_paths):
            raise PublicationBlocked("release_verifier_inputs_invalid")
        seen_paths.update(attempt_paths)
        attempts.append(
            ProfilerAttemptInputs(
                evidence=evidence,
                evidence_seal=evidence_seal,
                boundary_manifest=boundary_manifest,
            )
        )
    return ReleaseVerifierInputs(
        binding_prefix=_RELEASE_BINDING_PREFIX,
        gate_seal=paths["gate_seal"],
        phase_a_seal=paths["phase_a_seal"],
        phase_a_output=paths["phase_a_output"],
        phase_b_seal=paths["phase_b_seal"],
        assertion_observation=paths["assertion_observation"],
        phase_a_receipt_trace=paths["phase_a_receipt_trace"],
        phase_b_receipt_trace=paths["phase_b_receipt_trace"],
        candidate_identity_record=paths["candidate_identity_record"],
        execution_boundary_manifest=paths["execution_boundary_manifest"],
        priority_policy=paths["priority_policy"],
        unloaded_gpu_envelope=paths["unloaded_gpu_envelope"],
        model_envelope_catalog=paths["model_envelope_catalog"],
        profiler_attempts=tuple(attempts),
    )


def decode_config(raw: bytes, environment: Mapping[str, str]) -> AdapterConfig:
    document = _strict_json_object(raw, "publisher_config")
    expected = {
        "schema",
        "repository_root",
        "lock_tagger",
        "release_verifier_inputs",
    }
    if (
        set(document) != expected
        or document["schema"] != "subgen.task12.publisher-config/v3"
        or canonical_json_bytes(document) != raw
    ):
        raise PublicationBlocked("publisher_config_schema")
    repository_root = document["repository_root"]
    if not isinstance(repository_root, str) or not Path(repository_root).is_absolute():
        raise PublicationBlocked("repository_root_not_exact")
    try:
        resolved_root = Path(repository_root).resolve(strict=True)
    except OSError as exc:
        raise PublicationBlocked("repository_root_not_exact") from exc
    if resolved_root != Path(repository_root) or not resolved_root.is_dir():
        raise PublicationBlocked("repository_root_not_exact")
    github_token = environment.get("SUBGEN_TASK12_GITHUB_TOKEN")
    registry_token = environment.get("SUBGEN_TASK12_REGISTRY_TOKEN")
    if not github_token or "\r" in github_token or "\n" in github_token:
        raise PublicationBlocked("github_token_missing")
    if not registry_token or "\r" in registry_token or "\n" in registry_token:
        raise PublicationBlocked("registry_token_missing")
    git_environment = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": ("http.https://github.com/.extraheader"),
        "GIT_CONFIG_VALUE_1": f"Authorization: Bearer {github_token}",
        "GIT_CONFIG_KEY_2": "core.hooksPath",
        "GIT_CONFIG_VALUE_2": os.devnull,
    }
    lock_tagger = _string_mapping(document["lock_tagger"], "lock_tagger")
    if set(lock_tagger) != {"name", "email", "date"} or any(
        not value or "\r" in value or "\n" in value for value in lock_tagger.values()
    ):
        raise PublicationBlocked("lock_tagger_invalid")
    verifier_inputs = _release_verifier_inputs(document["release_verifier_inputs"])
    return AdapterConfig(
        repository_root=resolved_root,
        git_environment=git_environment,
        github_headers={"Authorization": f"Bearer {github_token}"},
        registry_headers={"Authorization": f"Bearer {registry_token}"},
        lock_tagger=lock_tagger,
        release_verifier_inputs=verifier_inputs,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m release_tools")
    subparsers = parser.add_subparsers(dest="verb", required=True)
    publish = subparsers.add_parser("publish")
    publish.add_argument("--intent", type=Path, required=True)
    publish.add_argument("--config", type=Path, required=True)
    publish.add_argument("--state-dir", type=Path, required=True)
    publish.add_argument("--recover", action="store_true")
    publish.add_argument("--validate-only", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    try:
        args = build_parser().parse_args(argv)
        env = environment if environment is not None else os.environ
        intent = decode_intent(_read_owner_only(args.intent, "intent"))
        config = decode_config(_read_owner_only(args.config, "config"), env)
        if args.validate_only:
            print(
                "Task 12 inputs validated; no command or request was issued.",
                file=output,
            )
            return 0
        journal = FileReceiptJournal(args.state_dir)
        with journal.exclusive():
            recovery = journal.load_latest()
            if args.recover and recovery is None:
                raise PublicationBlocked("recovery_receipt_missing")
            if not args.recover and recovery is not None:
                raise PublicationBlocked("existing_receipt_requires_recover")
            adapter = Task12HttpCommandAdapter(
                SubprocessCommandRunner(), UrllibHttpClient(), config
            )
            Task12Publisher(adapter, journal).publish(
                intent,
                recovery=recovery if args.recover else None,
            )
        print("Task 12 publication completed.", file=output)
        return 0
    except PublicationBlocked as exc:
        print(f"Task 12 blocked: {exc.code}", file=errors)
        return 2
