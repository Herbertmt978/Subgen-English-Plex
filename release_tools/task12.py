"""Task 12's fail-closed, restartable v0.5.0 publication state machine.

This module deliberately contains no credentials and performs no implicit
network or subprocess work.  A caller must provide an adapter whose public
writes have the exact semantics named by :class:`PublicationAdapter`.  In
particular, the immutable registry version operation is conditional-create;
there is no ordinary-overwrite method for ``v0.5.0`` in this contract.
"""

from __future__ import annotations

import hashlib
import gzip
import io
import json
import os
import re
import tarfile
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol, Sequence, TypeVar


FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TAG = "v0.5.0"
LOCK_REF = "refs/tags/subgen-task12-publication-lock-v0.5.0"
CANONICAL_REPOSITORY = "Herbertmt978/Subgen-English-Plex"
CANONICAL_IMAGE_REPOSITORY = "herbertmt978/subgen-english-plex"
GITHUB_API_ORIGIN = "https://api.github.com"
REGISTRY_API_ORIGIN = "https://ghcr.io"
CANONICAL_GIT_REMOTE_URL = "https://github.com/Herbertmt978/Subgen-English-Plex.git"
REGISTRY_CLIENT_CONTRACT = "release-tools-urllib-no-redirect/v1"
CREDENTIAL_TRANSPORT_CONTRACT = "bearer-header-and-git-extraheader/v1"
REGISTRY_PROBE_PREFIX = "task12-probe-"
OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
ANONYMOUS_PULL_CLIENT_CONTRACT = "release-tools-anonymous-clean-store/v1"
ANONYMOUS_DOCKER_CLIENT_CONTRACT = "classic-docker-engine-clean-pull/v1"
ANONYMOUS_DOCKER_HOST = "unix:///run/subgen-task12-anonymous/docker.sock"

_GZIP_LAYER_MEDIA_TYPES = {
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
}
_PLAIN_LAYER_MEDIA_TYPES = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.nondistributable.v1.tar",
    "application/vnd.docker.image.rootfs.diff.tar",
}


class PublicationBlocked(RuntimeError):
    """A safe, bounded Task 12 failure that must not be bypassed."""

    def __init__(self, code: str, message: str | None = None) -> None:
        if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{3,80}", code):
            code = "invalid_failure_code"
        self.code = code
        super().__init__(message or code)


def _block(code: str, message: str | None = None) -> None:
    raise PublicationBlocked(code, message)


def sha256_bytes(payload: bytes) -> str:
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    return hashlib.sha256(payload).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise PublicationBlocked("noncanonical_json") from exc
    return encoded + b"\n"


def _strict_json_object(payload: bytes, label: str) -> dict[str, Any]:
    if (
        not isinstance(payload, bytes)
        or not payload
        or payload.startswith(b"\xef\xbb\xbf")
    ):
        _block(f"{label}_encoding")
    if b"\r" in payload:
        _block(f"{label}_encoding")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _block(f"{label}_duplicate_key")
            result[key] = value
        return result

    try:
        text = payload.decode("utf-8", errors="strict")
        document = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: _block(f"{label}_nonfinite"),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationBlocked(f"{label}_invalid_json") from exc
    if not isinstance(document, dict):
        _block(f"{label}_not_object")
    return document


def _require_full_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or FULL_SHA_RE.fullmatch(value) is None:
        _block(f"invalid_{label}")
    return value


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        _block(f"invalid_{label}")
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _block(f"invalid_{label}")
    return value


def _strict_utf8_lf(payload: bytes, label: str) -> str:
    if not isinstance(payload, bytes) or payload.startswith(b"\xef\xbb\xbf"):
        _block(f"invalid_{label}_encoding")
    if b"\r" in payload:
        _block(f"invalid_{label}_line_endings")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise PublicationBlocked(f"invalid_{label}_encoding") from exc


def derive_layer_diff_id(payload: bytes, media_type: str) -> str:
    """Derive one OCI DiffID from the exact compressed/uncompressed layer bytes."""
    if media_type in _GZIP_LAYER_MEDIA_TYPES:
        try:
            uncompressed = gzip.decompress(payload)
        except (EOFError, OSError) as exc:
            raise PublicationBlocked("registry_layer_compression_invalid") from exc
    elif media_type in _PLAIN_LAYER_MEDIA_TYPES:
        uncompressed = payload
    else:
        _block("registry_layer_media_type_unsupported")
    try:
        with tarfile.open(fileobj=io.BytesIO(uncompressed), mode="r:") as archive:
            for _member in archive:
                pass
    except (OSError, tarfile.TarError) as exc:
        raise PublicationBlocked("registry_layer_tar_invalid") from exc
    return "sha256:" + sha256_bytes(uncompressed)


@dataclass(frozen=True)
class ImageIdentity:
    oci_index: str
    config_digest: str
    ordered_diff_ids: tuple[str, ...]
    revision_label: str

    def validate(self) -> None:
        _require_digest(self.oci_index, "oci_index")
        _require_digest(self.config_digest, "config_digest")
        if not self.ordered_diff_ids:
            _block("missing_ordered_diff_ids")
        for diff_id in self.ordered_diff_ids:
            _require_digest(diff_id, "diff_id")
        _require_full_sha(self.revision_label, "revision_label")


@dataclass(frozen=True)
class AnnotatedTag:
    object_sha: str
    target_commit: str
    tag: str
    message: str
    tagger_name: str
    tagger_email: str
    tagger_date: str

    def validate(self) -> None:
        _require_full_sha(self.object_sha, "annotated_tag_object")
        _require_full_sha(self.target_commit, "annotated_tag_target")
        if self.tag != TAG:
            _block("version_tag_not_exact")
        if (
            not self.message
            or "\r" in self.message
            or "\x00" in self.message
            or not self.tagger_name
            or "\n" in self.tagger_name
            or not self.tagger_email
            or "\n" in self.tagger_email
            or not self.tagger_date
            or "\n" in self.tagger_date
        ):
            _block("annotated_tag_metadata_invalid")


@dataclass(frozen=True)
class RegistryBlob:
    digest: str
    size: int
    media_type: str
    payload: bytes

    def validate(self) -> None:
        _require_digest(self.digest, "registry_blob_digest")
        if (
            isinstance(self.size, bool)
            or not isinstance(self.size, int)
            or self.size < 0
            or self.size != len(self.payload)
            or self.digest != "sha256:" + sha256_bytes(self.payload)
            or not self.media_type
            or "\r" in self.media_type
            or "\n" in self.media_type
        ):
            _block("registry_blob_descriptor_mismatch")

    def descriptor(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "size": self.size,
            "mediaType": self.media_type,
        }


@dataclass(frozen=True)
class RegistryManifest:
    digest: str
    size: int
    media_type: str
    payload: bytes

    def validate(self) -> None:
        _require_digest(self.digest, "registry_manifest_digest")
        if (
            isinstance(self.size, bool)
            or not isinstance(self.size, int)
            or self.size <= 0
            or self.size != len(self.payload)
            or self.digest != "sha256:" + sha256_bytes(self.payload)
            or not self.media_type
            or "\r" in self.media_type
            or "\n" in self.media_type
        ):
            _block("registry_manifest_descriptor_mismatch")
        _strict_json_object(self.payload, "registry_manifest")

    def descriptor(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "size": self.size,
            "mediaType": self.media_type,
        }


@dataclass(frozen=True)
class RegistryProbeObservation:
    """An authoritative read of one reserved Task 12 registry reference."""

    digest: str | None
    etag: str | None
    payload: bytes | None


@dataclass(frozen=True)
class RegistryProbeWrite:
    """The non-sensitive result of one live conditional probe request."""

    status: int
    digest: str | None


@dataclass
class RegistryProbeReceipt:
    """Replayable, non-secret plan and result for one retained GHCR probe."""

    kind: str
    token: str
    reference: str
    stage: str = "planned"
    prior_etag: str | None = None
    winner_etag: str | None = None
    verification_sha256: str | None = None


@dataclass(frozen=True)
class ReleaseIntent:
    repository: str
    image_repository: str
    prior_main_commit: str
    runtime_commit: str
    sampler_commit: str
    release_commit: str
    annotated_tag: AnnotatedTag
    release_title: str
    release_notes: bytes
    release_notes_blob: str
    task11b_verifier_receipt: bytes
    task11b_verifier_receipt_sha256: str
    sealed_manifest: bytes
    image: ImageIdentity
    required_blobs: tuple[RegistryBlob, ...]
    required_manifests: tuple[RegistryManifest, ...]
    prior_latest_digest: str | None
    main_ref: str = "refs/heads/main"
    version_tag: str = TAG
    lock_ref: str = LOCK_REF

    def validate(self) -> None:
        if (
            REPOSITORY_RE.fullmatch(self.repository) is None
            or self.repository != CANONICAL_REPOSITORY
        ):
            _block("repository_invalid")
        if (
            REPOSITORY_RE.fullmatch(self.image_repository) is None
            or self.image_repository != CANONICAL_IMAGE_REPOSITORY
        ):
            _block("image_repository_invalid")
        commits = (
            _require_full_sha(self.prior_main_commit, "prior_main_commit"),
            _require_full_sha(self.runtime_commit, "runtime_commit"),
            _require_full_sha(self.sampler_commit, "sampler_commit"),
            _require_full_sha(self.release_commit, "release_commit"),
        )
        if len(set(commits[1:])) != 3:
            _block("release_commits_not_distinct")
        self.annotated_tag.validate()
        if self.annotated_tag.target_commit != self.release_commit:
            _block("annotated_tag_target_mismatch")
        if self.version_tag != TAG or self.annotated_tag.tag != self.version_tag:
            _block("version_tag_not_exact")
        if self.main_ref != "refs/heads/main" or self.lock_ref != LOCK_REF:
            _block("repository_ref_not_exact")
        if (
            not self.release_title
            or "\r" in self.release_title
            or "\n" in self.release_title
        ):
            _block("release_title_invalid")
        _strict_utf8_lf(self.release_notes, "release_notes")
        _require_full_sha(self.release_notes_blob, "release_notes_blob")
        receipt_sha = _require_sha256(
            self.task11b_verifier_receipt_sha256,
            "task11b_verifier_receipt_sha256",
        )
        if (
            not self.task11b_verifier_receipt
            or sha256_bytes(self.task11b_verifier_receipt) != receipt_sha
        ):
            _block("task11b_verifier_receipt_mismatch")
        self.image.validate()
        if self.image.revision_label != self.runtime_commit:
            _block("image_revision_mismatch")
        if not self.sealed_manifest or (
            "sha256:" + sha256_bytes(self.sealed_manifest) != self.image.oci_index
        ):
            _block("sealed_manifest_digest_mismatch")
        self._validate_registry_graph()
        if self.prior_latest_digest is not None:
            _require_digest(self.prior_latest_digest, "prior_latest_digest")

    def _validate_registry_graph(self) -> None:
        if not self.required_blobs or not self.required_manifests:
            _block("registry_graph_incomplete")
        blobs: dict[str, RegistryBlob] = {}
        for blob in self.required_blobs:
            blob.validate()
            if blob.digest in blobs:
                _block("registry_blob_descriptor_duplicate")
            blobs[blob.digest] = blob
        manifests: dict[str, RegistryManifest] = {}
        for manifest in self.required_manifests:
            manifest.validate()
            if manifest.digest in manifests:
                _block("registry_manifest_descriptor_duplicate")
            manifests[manifest.digest] = manifest

        index = _strict_json_object(self.sealed_manifest, "sealed_manifest")
        index_descriptors = index.get("manifests")
        if (
            index.get("schemaVersion") != 2
            or index.get("mediaType") != "application/vnd.oci.image.index.v1+json"
            or not isinstance(index_descriptors, list)
            or not index_descriptors
        ):
            _block("sealed_manifest_descriptors_missing")
        expected_manifest_descriptors = {
            canonical_json_bytes(manifest.descriptor())
            for manifest in manifests.values()
        }
        observed_manifest_descriptors: set[bytes] = set()
        index_descriptor_by_digest: dict[str, dict[str, Any]] = {}
        for descriptor in index_descriptors:
            if not isinstance(descriptor, dict):
                _block("sealed_manifest_descriptor_invalid")
            projected = {
                key: descriptor.get(key) for key in ("digest", "size", "mediaType")
            }
            observed_manifest_descriptors.add(canonical_json_bytes(projected))
            digest = descriptor.get("digest")
            if not isinstance(digest, str) or digest in index_descriptor_by_digest:
                _block("sealed_manifest_descriptor_invalid")
            index_descriptor_by_digest[digest] = descriptor
        if (
            len(observed_manifest_descriptors) != len(index_descriptors)
            or observed_manifest_descriptors != expected_manifest_descriptors
        ):
            _block("sealed_manifest_descriptor_mismatch")

        observed_blob_descriptors: set[bytes] = set()
        config_digests: set[str] = set()
        selected_layer_counts: list[int] = []
        selected_layer_diff_ids: list[tuple[str, ...]] = []
        selected_platforms: list[dict[str, Any]] = []
        for manifest in manifests.values():
            document = _strict_json_object(manifest.payload, "registry_manifest")
            config = document.get("config")
            layers = document.get("layers")
            if (
                document.get("schemaVersion") != 2
                or document.get("mediaType") != manifest.media_type
                or manifest.media_type != "application/vnd.oci.image.manifest.v1+json"
                or not isinstance(config, dict)
                or not isinstance(layers, list)
            ):
                _block("registry_manifest_graph_invalid")
            config_digests.add(config.get("digest"))
            if config.get("digest") == self.image.config_digest:
                if (
                    config.get("mediaType")
                    != "application/vnd.oci.image.config.v1+json"
                ):
                    _block("image_config_descriptor_invalid")
                selected_layer_counts.append(len(layers))
                platform = index_descriptor_by_digest[manifest.digest].get("platform")
                if not isinstance(platform, dict):
                    _block("image_platform_descriptor_invalid")
                selected_platforms.append(platform)
                derived: list[str] = []
                for layer in layers:
                    if not isinstance(layer, dict):
                        _block("registry_manifest_graph_invalid")
                    layer_blob = blobs.get(layer.get("digest"))
                    if layer_blob is None:
                        _block("registry_blob_descriptor_mismatch")
                    derived.append(
                        derive_layer_diff_id(layer_blob.payload, layer_blob.media_type)
                    )
                selected_layer_diff_ids.append(tuple(derived))
            for descriptor in (config, *layers):
                if not isinstance(descriptor, dict):
                    _block("registry_manifest_graph_invalid")
                projected = {
                    key: descriptor.get(key) for key in ("digest", "size", "mediaType")
                }
                observed_blob_descriptors.add(canonical_json_bytes(projected))
        expected_blob_descriptors = {
            canonical_json_bytes(blob.descriptor()) for blob in blobs.values()
        }
        if observed_blob_descriptors != expected_blob_descriptors:
            _block("registry_blob_descriptor_mismatch")
        if self.image.config_digest not in config_digests:
            _block("image_config_not_in_registry_graph")
        config_blob = blobs.get(self.image.config_digest)
        if (
            config_blob is None
            or config_blob.media_type != "application/vnd.oci.image.config.v1+json"
            or not selected_layer_counts
            or any(
                count != len(self.image.ordered_diff_ids)
                for count in selected_layer_counts
            )
            or any(
                diff_ids != self.image.ordered_diff_ids
                for diff_ids in selected_layer_diff_ids
            )
        ):
            _block("image_config_not_in_registry_graph")
        config_document = _strict_json_object(config_blob.payload, "image_config")
        rootfs = config_document.get("rootfs")
        runtime_config = config_document.get("config")
        labels = (
            runtime_config.get("Labels") if isinstance(runtime_config, dict) else None
        )
        if (
            not isinstance(rootfs, dict)
            or rootfs.get("type") != "layers"
            or rootfs.get("diff_ids") != list(self.image.ordered_diff_ids)
            or not isinstance(labels, dict)
            or labels.get("org.opencontainers.image.revision")
            != self.image.revision_label
            or config_document.get("os") != "linux"
            or config_document.get("architecture") != "amd64"
            or any(
                platform != {"architecture": "amd64", "os": "linux"}
                for platform in selected_platforms
            )
        ):
            _block("image_config_identity_mismatch")

    @property
    def release_notes_sha256(self) -> str:
        return sha256_bytes(self.release_notes)

    @property
    def sealed_manifest_sha256(self) -> str:
        return sha256_bytes(self.sealed_manifest)

    def binding_document(self) -> dict[str, Any]:
        return {
            "schema": "subgen.task12.publication-binding/v1",
            "repository": self.repository,
            "image_repository": self.image_repository,
            "main_ref": self.main_ref,
            "version_tag": self.version_tag,
            "lock_ref": self.lock_ref,
            "prior_main_commit": self.prior_main_commit,
            "runtime_commit": self.runtime_commit,
            "sampler_commit": self.sampler_commit,
            "release_commit": self.release_commit,
            "annotated_tag_object": self.annotated_tag.object_sha,
            "release_title": self.release_title,
            "release_notes_blob": self.release_notes_blob,
            "release_notes_sha256": self.release_notes_sha256,
            "task11b_verifier_receipt_sha256": (self.task11b_verifier_receipt_sha256),
            "sealed_manifest_sha256": self.sealed_manifest_sha256,
            "required_blobs": [blob.descriptor() for blob in self.required_blobs],
            "required_manifests": [
                manifest.descriptor() for manifest in self.required_manifests
            ],
            "oci_index": self.image.oci_index,
            "config_digest": self.image.config_digest,
            "ordered_diff_ids": list(self.image.ordered_diff_ids),
            "revision_label": self.image.revision_label,
            "prior_latest_digest": self.prior_latest_digest,
        }

    @property
    def binding_sha256(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.binding_document()))

    @property
    def intent_sha256(self) -> str:
        return self.binding_sha256


@dataclass(frozen=True)
class ActionsBaseline:
    run_ids: tuple[int, ...]
    sha256: str

    @classmethod
    def create(cls, run_ids: Sequence[int]) -> ActionsBaseline:
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in run_ids
        ):
            _block("actions_run_id_invalid")
        normalized = tuple(sorted(run_ids))
        if len(normalized) != len(set(normalized)):
            _block("actions_run_id_duplicate")
        payload = canonical_json_bytes(
            {"schema": "subgen.task12.actions-baseline/v1", "run_ids": list(normalized)}
        )
        return cls(run_ids=normalized, sha256=sha256_bytes(payload))

    def validate(self) -> None:
        rebuilt = self.create(self.run_ids)
        if rebuilt != self:
            _block("actions_baseline_hash_mismatch")


@dataclass(frozen=True)
class LocalSourceProof:
    clean_worktree: bool
    workflows_manual_only: bool
    runtime_commit: str
    sampler_commit: str
    release_commit: str
    runtime_is_ancestor_of_sampler: bool
    sampler_is_ancestor_of_release: bool
    annotated_tag_object: str
    annotated_tag_target: str
    release_notes_blob: str
    release_notes: bytes
    task11b_verifier_receipt_sha256: str
    candidate_docker_engine_id_sha256: str
    image: ImageIdentity
    git_remote_url: str

    def verify(self, intent: ReleaseIntent) -> None:
        if not self.clean_worktree:
            _block("release_worktree_not_clean")
        if not self.workflows_manual_only:
            _block("hosted_workflow_trigger_present")
        if (
            self.runtime_commit != intent.runtime_commit
            or self.sampler_commit != intent.sampler_commit
            or self.release_commit != intent.release_commit
            or self.runtime_is_ancestor_of_sampler is not True
            or self.sampler_is_ancestor_of_release is not True
            or self.annotated_tag_object != intent.annotated_tag.object_sha
            or self.annotated_tag_target != intent.release_commit
            or self.release_notes_blob != intent.release_notes_blob
            or self.release_notes != intent.release_notes
            or self.task11b_verifier_receipt_sha256
            != intent.task11b_verifier_receipt_sha256
            or SHA256_RE.fullmatch(self.candidate_docker_engine_id_sha256) is None
            or self.image != intent.image
            or self.git_remote_url != CANONICAL_GIT_REMOTE_URL
        ):
            _block("local_source_proof_mismatch")


@dataclass(frozen=True)
class ReleaseView:
    tag: str
    title: str
    draft: bool
    prerelease: bool
    body: bytes

    def is_exact(self, intent: ReleaseIntent) -> bool:
        return (
            self.tag == intent.version_tag
            and self.title == intent.release_title
            and self.draft is False
            and self.prerelease is False
            and self.body == intent.release_notes
        )


@dataclass(frozen=True)
class LockObservation:
    object_sha: str
    document_sha256: str


@dataclass(frozen=True)
class PublicState:
    main_commit: str
    version_tag_object: str | None
    release: ReleaseView | None
    version_digest: str | None
    latest_digest: str | None
    lock: LockObservation | None


@dataclass
class PublicationCheckpoint:
    intent_sha256: str
    run_token: str
    actions_baseline: ActionsBaseline
    lock_document_sha256: str
    phase: str = "prepared"
    lock_object_sha: str | None = None
    lock_acquired: bool = False
    lock_removed: bool = False
    completed_writes: list[str] = field(default_factory=list)
    anonymous_smoke_sha256: str | None = None
    version_create_probe: RegistryProbeReceipt | None = None
    latest_cas_probe: RegistryProbeReceipt | None = None
    latest_write_attempted: bool = False
    blob_upload_urls: dict[str, str] = field(default_factory=dict)
    failure_code: str | None = None
    receipt_sequence: int = 0
    previous_receipt_sha256: str | None = None

    def snapshot(self) -> dict[str, Any]:
        document = asdict(self)
        document["schema"] = "subgen.task12.publication-checkpoint/v3"
        return document


class PublicationReceiptSink(Protocol):
    def append(self, checkpoint: PublicationCheckpoint) -> None: ...


class PublicationAdapter(Protocol):
    def verify_local_sources(self, intent: ReleaseIntent) -> LocalSourceProof: ...

    def fetch_all_actions_run_ids(self, intent: ReleaseIntent) -> Sequence[int]: ...

    def read_public_state(
        self, intent: ReleaseIntent, lock_document_sha256: str
    ) -> PublicState: ...

    def create_lock_object(
        self, intent: ReleaseIntent, lock_document: bytes
    ) -> str: ...

    def create_lock_ref(self, intent: ReleaseIntent, object_sha: str) -> None: ...

    def assert_lock(
        self,
        intent: ReleaseIntent,
        object_sha: str,
        lock_document_sha256: str,
    ) -> None: ...

    def advance_main(self, intent: ReleaseIntent) -> None: ...

    def create_version_tag_object(self, intent: ReleaseIntent) -> str: ...

    def create_version_tag_ref(self, intent: ReleaseIntent) -> None: ...

    def registry_blob_present(
        self, intent: ReleaseIntent, blob: RegistryBlob
    ) -> bool: ...

    def start_registry_blob_upload(
        self, intent: ReleaseIntent, blob: RegistryBlob
    ) -> str: ...

    def finish_registry_blob_upload(
        self, intent: ReleaseIntent, blob: RegistryBlob, upload_url: str
    ) -> None: ...

    def registry_manifest_present(
        self, intent: ReleaseIntent, manifest: RegistryManifest
    ) -> bool: ...

    def put_registry_manifest(
        self, intent: ReleaseIntent, manifest: RegistryManifest
    ) -> None: ...

    def read_registry_probe(
        self, intent: ReleaseIntent, reference: str
    ) -> RegistryProbeObservation: ...

    def put_registry_probe(
        self,
        intent: ReleaseIntent,
        operation: str,
        reference: str,
        manifest: RegistryManifest,
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> RegistryProbeWrite: ...

    def conditional_create_version(self, intent: ReleaseIntent) -> None: ...

    def anonymous_pull_smoke(
        self,
        intent: ReleaseIntent,
        candidate_docker_engine_id_sha256: str,
    ) -> ImageIdentity: ...

    def create_release(self, intent: ReleaseIntent) -> None: ...

    def update_latest(
        self,
        intent: ReleaseIntent,
        expected_prior_digest: str | None,
    ) -> None: ...

    def remove_lock_exact(self, intent: ReleaseIntent, object_sha: str) -> None: ...


T = TypeVar("T")


class Task12Publisher:
    """Execute or recover one immutable Task 12 publication intent."""

    def __init__(
        self,
        adapter: PublicationAdapter,
        receipt_sink: PublicationReceiptSink,
        *,
        token_factory: Callable[[], str] | None = None,
        probe_token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.adapter = adapter
        self.receipt_sink = receipt_sink
        self.token_factory = token_factory or (lambda: os.urandom(32).hex())
        self.probe_token_factory = probe_token_factory or (lambda: os.urandom(32).hex())

    def _record(self, checkpoint: PublicationCheckpoint) -> None:
        self.receipt_sink.append(checkpoint)

    def _actions_snapshot(self, intent: ReleaseIntent) -> ActionsBaseline:
        return ActionsBaseline.create(self.adapter.fetch_all_actions_run_ids(intent))

    def _assert_actions_unchanged(
        self, intent: ReleaseIntent, baseline: ActionsBaseline
    ) -> None:
        observed = self._actions_snapshot(intent)
        if observed != baseline:
            _block("hosted_actions_run_set_changed")

    def _public_write(
        self,
        checkpoint: PublicationCheckpoint,
        intent: ReleaseIntent,
        operation: str,
        write: Callable[[], T],
    ) -> T:
        checkpoint.phase = f"{operation}_pending"
        checkpoint.failure_code = None
        self._record(checkpoint)
        try:
            self._assert_actions_unchanged(intent, checkpoint.actions_baseline)
            if operation not in {"lock_object_create", "lock_ref_create"}:
                self._require_lock(checkpoint, intent)
        except BaseException as exc:
            checkpoint.phase = f"{operation}_blocked_before_write"
            checkpoint.failure_code = (
                exc.code
                if isinstance(exc, PublicationBlocked)
                else f"{operation}_precondition_failed"
            )
            self._record(checkpoint)
            raise
        result: T | None = None
        write_error: BaseException | None = None
        try:
            result = write()
        except BaseException as exc:
            write_error = exc
        actions_error: BaseException | None = None
        try:
            self._assert_actions_unchanged(intent, checkpoint.actions_baseline)
        except BaseException as exc:
            actions_error = exc
        if actions_error is not None:
            checkpoint.phase = f"{operation}_ambiguous"
            checkpoint.failure_code = "hosted_actions_run_set_changed"
            self._record(checkpoint)
            raise PublicationBlocked(
                "hosted_actions_run_set_changed"
            ) from actions_error
        if write_error is not None:
            checkpoint.phase = f"{operation}_ambiguous"
            checkpoint.failure_code = f"{operation}_response_ambiguous"
            self._record(checkpoint)
            raise PublicationBlocked(checkpoint.failure_code) from write_error
        checkpoint.completed_writes.append(operation)
        checkpoint.phase = f"{operation}_written"
        self._record(checkpoint)
        return result  # type: ignore[return-value]

    @staticmethod
    def _lock_document(
        intent: ReleaseIntent,
        baseline: ActionsBaseline,
        run_token: str,
    ) -> bytes:
        if (
            not isinstance(run_token, str)
            or re.fullmatch(r"[0-9a-f]{64}", run_token) is None
        ):
            _block("publication_run_token_invalid")
        return canonical_json_bytes(
            {
                "schema": "subgen.task12.publication-lock/v1",
                "intent_sha256": intent.intent_sha256,
                "release_commit": intent.release_commit,
                "candidate_oci_index": intent.image.oci_index,
                "release_notes_sha256": intent.release_notes_sha256,
                "actions_baseline_sha256": baseline.sha256,
                "run_token_sha256": sha256_bytes(run_token.encode("ascii")),
            }
        )

    @staticmethod
    def _validate_checkpoint(
        checkpoint: PublicationCheckpoint,
        intent: ReleaseIntent,
    ) -> None:
        if (
            checkpoint.intent_sha256 != intent.intent_sha256
            or re.fullmatch(r"[0-9a-f]{64}", checkpoint.run_token) is None
            or not isinstance(checkpoint.actions_baseline, ActionsBaseline)
            or (
                checkpoint.anonymous_smoke_sha256 is not None
                and SHA256_RE.fullmatch(checkpoint.anonymous_smoke_sha256) is None
            )
            or (
                checkpoint.version_create_probe is not None
                and not isinstance(
                    checkpoint.version_create_probe,
                    RegistryProbeReceipt,
                )
            )
            or (
                checkpoint.latest_cas_probe is not None
                and not isinstance(checkpoint.latest_cas_probe, RegistryProbeReceipt)
            )
            or not isinstance(checkpoint.latest_write_attempted, bool)
            or not isinstance(checkpoint.blob_upload_urls, dict)
            or isinstance(checkpoint.receipt_sequence, bool)
            or not isinstance(checkpoint.receipt_sequence, int)
            or checkpoint.receipt_sequence < 0
            or (
                checkpoint.previous_receipt_sha256 is not None
                and (
                    not isinstance(checkpoint.previous_receipt_sha256, str)
                    or SHA256_RE.fullmatch(checkpoint.previous_receipt_sha256) is None
                )
            )
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in checkpoint.blob_upload_urls.items()
            )
        ):
            _block("recovery_checkpoint_identity_mismatch")
        checkpoint.actions_baseline.validate()
        expected_lock = Task12Publisher._lock_document(
            intent,
            checkpoint.actions_baseline,
            checkpoint.run_token,
        )
        if sha256_bytes(expected_lock) != checkpoint.lock_document_sha256:
            _block("recovery_checkpoint_lock_mismatch")
        if checkpoint.version_create_probe is not None:
            Task12Publisher._validate_probe_receipt(
                checkpoint.version_create_probe,
                intent,
                kind="create",
            )
        if checkpoint.latest_cas_probe is not None:
            Task12Publisher._validate_probe_receipt(
                checkpoint.latest_cas_probe,
                intent,
                kind="cas",
            )

    @staticmethod
    def _validate_public_components(
        state: PublicState,
        intent: ReleaseIntent,
    ) -> None:
        if state.main_commit not in {intent.prior_main_commit, intent.release_commit}:
            _block("remote_main_diverged")
        if state.version_tag_object not in {None, intent.annotated_tag.object_sha}:
            _block("remote_version_tag_foreign")
        if state.release is not None and not state.release.is_exact(intent):
            _block("github_release_foreign")
        if state.version_digest not in {None, intent.image.oci_index}:
            _block("registry_version_foreign")
        if state.latest_digest not in {
            intent.prior_latest_digest,
            intent.image.oci_index,
        }:
            _block("registry_latest_foreign")

    def _read_state(
        self,
        intent: ReleaseIntent,
        checkpoint: PublicationCheckpoint,
    ) -> PublicState:
        state = self.adapter.read_public_state(
            intent,
            checkpoint.lock_document_sha256,
        )
        self._validate_public_components(state, intent)
        return state

    def _require_lock(
        self,
        checkpoint: PublicationCheckpoint,
        intent: ReleaseIntent,
    ) -> None:
        if not checkpoint.lock_acquired or checkpoint.lock_object_sha is None:
            _block("publication_lock_not_held")
        self.adapter.assert_lock(
            intent,
            checkpoint.lock_object_sha,
            checkpoint.lock_document_sha256,
        )

    @staticmethod
    def _probe_materials(
        intent: ReleaseIntent,
        kind: str,
        labels: Sequence[str],
        *,
        token: str,
    ) -> tuple[str, str, tuple[RegistryManifest, ...]]:
        if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
            _block("registry_probe_token_invalid")
        if kind not in {"create", "cas"} or not labels:
            _block("registry_probe_plan_invalid")
        binding = canonical_json_bytes(
            {
                "schema": "subgen.task12.registry-probe-binding/v1",
                "intent_sha256": intent.intent_sha256,
                "kind": kind,
                "token_sha256": sha256_bytes(token.encode("ascii")),
            }
        )
        binding_sha256 = sha256_bytes(binding)
        reference = f"{REGISTRY_PROBE_PREFIX}{kind}-{binding_sha256[:40]}"
        if reference in {intent.version_tag, "latest"} or len(reference) > 128:
            _block("registry_probe_reference_invalid")
        source = _strict_json_object(intent.sealed_manifest, "sealed_manifest")
        existing_annotations = source.get("annotations", {})
        if not isinstance(existing_annotations, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in existing_annotations.items()
        ):
            _block("registry_probe_annotations_invalid")
        manifests: list[RegistryManifest] = []
        reserved_digests = {
            intent.image.oci_index,
            *(item.digest for item in intent.required_manifests),
        }
        for label in labels:
            if (
                not isinstance(label, str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", label) is None
            ):
                _block("registry_probe_label_invalid")
            document = dict(source)
            annotations = dict(existing_annotations)
            annotations["io.github.herbertmt978.subgen.task12.probe"] = sha256_bytes(
                canonical_json_bytes(
                    {
                        "binding_sha256": binding_sha256,
                        "kind": kind,
                        "label": label,
                    }
                )
            )
            document["annotations"] = annotations
            payload = canonical_json_bytes(document)
            manifest = RegistryManifest(
                digest="sha256:" + sha256_bytes(payload),
                size=len(payload),
                media_type=OCI_INDEX_MEDIA_TYPE,
                payload=payload,
            )
            manifest.validate()
            if manifest.digest in reserved_digests:
                _block("registry_probe_manifest_collision")
            reserved_digests.add(manifest.digest)
            manifests.append(manifest)
        return reference, binding_sha256, tuple(manifests)

    @staticmethod
    def _probe_verification_sha256(
        intent: ReleaseIntent,
        receipt: RegistryProbeReceipt,
        manifests: Sequence[RegistryManifest],
    ) -> str:
        labels = (
            ("winner", "rejected")
            if receipt.kind == "create"
            else (
                "prior",
                "winner",
                "rejected",
            )
        )
        if len(manifests) != len(labels):
            _block("registry_probe_plan_invalid")
        document: dict[str, Any] = {
            "schema": "subgen.task12.registry-probe-verification/v3",
            "intent_sha256": intent.intent_sha256,
            "kind": receipt.kind,
            "reference": receipt.reference,
            "token_sha256": sha256_bytes(receipt.token.encode("ascii")),
            "manifests": {
                label: manifest.digest
                for label, manifest in zip(labels, manifests, strict=True)
            },
            "statuses": [201, 412] if receipt.kind == "create" else [201, 201, 412],
            "retained_for_audit": True,
        }
        if receipt.kind == "cas":
            if (
                not isinstance(receipt.prior_etag, str)
                or re.fullmatch(r'"[\x21\x23-\x7e]*"', receipt.prior_etag) is None
            ):
                _block("registry_probe_prior_etag_invalid")
            document["prior_etag_sha256"] = sha256_bytes(
                receipt.prior_etag.encode("ascii")
            )
        elif receipt.prior_etag is not None:
            _block("registry_probe_prior_etag_invalid")
        if (
            not isinstance(receipt.winner_etag, str)
            or re.fullmatch(r'"[\x21\x23-\x7e]*"', receipt.winner_etag) is None
        ):
            _block("registry_probe_winner_etag_invalid")
        document["winner_etag_sha256"] = sha256_bytes(
            receipt.winner_etag.encode("ascii")
        )
        return sha256_bytes(canonical_json_bytes(document))

    @staticmethod
    def _validate_probe_receipt(
        receipt: RegistryProbeReceipt,
        intent: ReleaseIntent,
        *,
        kind: str,
    ) -> tuple[RegistryManifest, ...]:
        labels = (
            ("winner", "rejected")
            if kind == "create"
            else (
                "prior",
                "winner",
                "rejected",
            )
        )
        allowed_stages = (
            {"planned", "seed_armed", "reject_armed", "verified"}
            if kind == "create"
            else {"planned", "seed_armed", "cas_armed", "stale_armed", "verified"}
        )
        winner_etag_required = (
            kind == "create" and receipt.stage in {"reject_armed", "verified"}
        ) or (kind == "cas" and receipt.stage in {"stale_armed", "verified"})
        if (
            receipt.kind != kind
            or re.fullmatch(r"[0-9a-f]{64}", receipt.token) is None
            or not isinstance(receipt.reference, str)
            or receipt.stage not in allowed_stages
            or (
                receipt.verification_sha256 is not None
                and SHA256_RE.fullmatch(receipt.verification_sha256) is None
            )
            or (
                receipt.winner_etag is not None
                and (
                    not isinstance(receipt.winner_etag, str)
                    or re.fullmatch(r'"[\x21\x23-\x7e]*"', receipt.winner_etag) is None
                )
            )
        ):
            _block("registry_probe_receipt_invalid")
        reference, _binding_sha256, manifests = Task12Publisher._probe_materials(
            intent,
            kind,
            labels,
            token=receipt.token,
        )
        if receipt.reference != reference:
            _block("registry_probe_receipt_invalid")
        if kind == "create" and receipt.prior_etag is not None:
            _block("registry_probe_receipt_invalid")
        if (
            kind == "cas"
            and receipt.prior_etag is not None
            and (
                not isinstance(receipt.prior_etag, str)
                or re.fullmatch(r'"[\x21\x23-\x7e]*"', receipt.prior_etag) is None
            )
        ):
            _block("registry_probe_receipt_invalid")
        if kind == "cas" and (
            (receipt.stage in {"cas_armed", "stale_armed", "verified"})
            != (receipt.prior_etag is not None)
        ):
            _block("registry_probe_receipt_invalid")
        if (receipt.stage == "verified") != (receipt.verification_sha256 is not None):
            _block("registry_probe_receipt_invalid")
        if winner_etag_required != (receipt.winner_etag is not None):
            _block("registry_probe_receipt_invalid")
        if receipt.stage == "verified" and receipt.verification_sha256 != (
            Task12Publisher._probe_verification_sha256(
                intent,
                receipt,
                manifests,
            )
        ):
            _block("registry_probe_receipt_invalid")
        return manifests

    def _prepare_probe_receipt(
        self,
        checkpoint: PublicationCheckpoint,
        intent: ReleaseIntent,
        *,
        kind: str,
    ) -> tuple[RegistryProbeReceipt, tuple[RegistryManifest, ...]]:
        attribute = "version_create_probe" if kind == "create" else "latest_cas_probe"
        receipt = getattr(checkpoint, attribute)
        if receipt is None:
            token = self.probe_token_factory()
            labels = (
                ("winner", "rejected")
                if kind == "create"
                else (
                    "prior",
                    "winner",
                    "rejected",
                )
            )
            reference, _binding_sha256, manifests = self._probe_materials(
                intent,
                kind,
                labels,
                token=token,
            )
            receipt = RegistryProbeReceipt(
                kind=kind,
                token=token,
                reference=reference,
            )
            setattr(checkpoint, attribute, receipt)
            checkpoint.phase = f"registry_{kind}_probe_planned"
            self._record(checkpoint)
            return receipt, manifests
        if not isinstance(receipt, RegistryProbeReceipt):
            _block("registry_probe_receipt_invalid")
        return receipt, self._validate_probe_receipt(
            receipt,
            intent,
            kind=kind,
        )

    @staticmethod
    def _assert_probe_absent(observation: RegistryProbeObservation) -> None:
        if observation != RegistryProbeObservation(None, None, None):
            _block("registry_probe_reference_not_absent")

    @staticmethod
    def _assert_probe_exact(
        observation: RegistryProbeObservation,
        manifest: RegistryManifest,
        *,
        require_etag: bool,
    ) -> None:
        if (
            observation.digest != manifest.digest
            or observation.payload != manifest.payload
            or (
                require_etag
                and (
                    not isinstance(observation.etag, str)
                    or re.fullmatch(r'"[\x21\x23-\x7e]*"', observation.etag) is None
                )
            )
        ):
            _block("registry_probe_postcondition_failed")

    @staticmethod
    def _assert_probe_write(
        result: RegistryProbeWrite,
        expected_status: int,
        manifest: RegistryManifest,
    ) -> None:
        if (
            result.status != expected_status
            or (expected_status == 201 and result.digest != manifest.digest)
            or (expected_status == 412 and result.digest is not None)
        ):
            _block("registry_probe_response_invalid")

    @staticmethod
    def _classify_probe_observation(
        observation: RegistryProbeObservation,
        candidates: Sequence[tuple[str, RegistryManifest]],
    ) -> str | None:
        if observation == RegistryProbeObservation(None, None, None):
            return None
        if (
            not isinstance(observation.etag, str)
            or re.fullmatch(r'"[\x21\x23-\x7e]*"', observation.etag) is None
        ):
            _block("registry_probe_postcondition_failed")
        for label, manifest in candidates:
            if (
                observation.digest == manifest.digest
                and observation.payload == manifest.payload
            ):
                return label
        _block("registry_probe_retained_reference_foreign")

    def _verify_retained_probe(
        self,
        checkpoint: PublicationCheckpoint,
        intent: ReleaseIntent,
        *,
        kind: str,
    ) -> None:
        receipt = (
            checkpoint.version_create_probe
            if kind == "create"
            else checkpoint.latest_cas_probe
        )
        if not isinstance(receipt, RegistryProbeReceipt):
            _block(f"registry_{kind}_probe_evidence_missing")
        manifests = self._validate_probe_receipt(receipt, intent, kind=kind)
        if receipt.verification_sha256 is None:
            _block(f"registry_{kind}_probe_evidence_missing")
        winner = manifests[0] if kind == "create" else manifests[1]
        observation = self.adapter.read_registry_probe(intent, receipt.reference)
        self._assert_probe_exact(observation, winner, require_etag=True)
        if observation.etag != receipt.winner_etag:
            _block("registry_probe_winner_etag_changed")
        if kind == "cas" and observation.etag == receipt.prior_etag:
            _block("registry_probe_etag_not_changed")

    def _verify_completion_evidence(
        self,
        checkpoint: PublicationCheckpoint,
        intent: ReleaseIntent,
    ) -> None:
        expected_smoke_sha256 = sha256_bytes(canonical_json_bytes(asdict(intent.image)))
        if checkpoint.anonymous_smoke_sha256 != expected_smoke_sha256:
            _block("anonymous_pull_smoke_evidence_missing")
        self._verify_retained_probe(checkpoint, intent, kind="create")
        self._verify_retained_probe(checkpoint, intent, kind="cas")

    def _run_version_create_probe(
        self,
        checkpoint: PublicationCheckpoint,
        intent: ReleaseIntent,
    ) -> None:
        receipt, manifests = self._prepare_probe_receipt(
            checkpoint,
            intent,
            kind="create",
        )
        winner, rejected = manifests
        if receipt.stage == "verified":
            self._verify_retained_probe(checkpoint, intent, kind="create")
            return
        observation = self.adapter.read_registry_probe(intent, receipt.reference)
        observed = self._classify_probe_observation(
            observation,
            (("winner", winner), ("rejected", rejected)),
        )
        if receipt.stage == "planned":
            if observed is not None:
                _block("registry_create_probe_unowned_progress")
            receipt.stage = "seed_armed"
            checkpoint.phase = "registry_version_probe_seed_armed"
            self._record(checkpoint)
        if receipt.stage == "seed_armed" and observed is None:
            self._require_lock(checkpoint, intent)
            created = self._public_write(
                checkpoint,
                intent,
                "registry_version_probe_seed",
                lambda: self.adapter.put_registry_probe(
                    intent,
                    "registry_version_probe_seed",
                    receipt.reference,
                    winner,
                    if_none_match=True,
                ),
            )
            self._assert_probe_write(created, 201, winner)
            observation = self.adapter.read_registry_probe(
                intent,
                receipt.reference,
            )
            observed = self._classify_probe_observation(
                observation,
                (("winner", winner), ("rejected", rejected)),
            )
        if receipt.stage == "seed_armed":
            if observed != "winner":
                _block("registry_create_probe_retained_state_invalid")
            self._assert_probe_exact(observation, winner, require_etag=True)
            receipt.winner_etag = observation.etag
            receipt.stage = "reject_armed"
            checkpoint.phase = "registry_version_probe_reject_armed"
            self._record(checkpoint)
        if receipt.stage != "reject_armed" or observed != "winner":
            _block("registry_create_probe_retained_state_invalid")
        winner_observation = self.adapter.read_registry_probe(
            intent,
            receipt.reference,
        )
        self._assert_probe_exact(winner_observation, winner, require_etag=True)
        if winner_observation.etag != receipt.winner_etag:
            _block("registry_probe_winner_etag_changed")
        self._require_lock(checkpoint, intent)
        rejected_result = self._public_write(
            checkpoint,
            intent,
            "registry_version_probe_reject",
            lambda: self.adapter.put_registry_probe(
                intent,
                "registry_version_probe_reject",
                receipt.reference,
                rejected,
                if_none_match=True,
            ),
        )
        self._assert_probe_write(rejected_result, 412, rejected)
        retained_winner = self.adapter.read_registry_probe(
            intent,
            receipt.reference,
        )
        self._assert_probe_exact(retained_winner, winner, require_etag=True)
        if retained_winner.etag != receipt.winner_etag:
            _block("registry_probe_winner_etag_changed")
        receipt.stage = "verified"
        receipt.verification_sha256 = self._probe_verification_sha256(
            intent,
            receipt,
            manifests,
        )
        checkpoint.phase = "registry_version_probe_verified"
        self._record(checkpoint)

    def _run_latest_cas_probe(
        self,
        checkpoint: PublicationCheckpoint,
        intent: ReleaseIntent,
    ) -> None:
        receipt, manifests = self._prepare_probe_receipt(
            checkpoint,
            intent,
            kind="cas",
        )
        prior, winner, rejected = manifests
        if receipt.stage == "verified":
            self._verify_retained_probe(checkpoint, intent, kind="cas")
            return
        observation = self.adapter.read_registry_probe(intent, receipt.reference)
        observed = self._classify_probe_observation(
            observation,
            (("prior", prior), ("winner", winner), ("rejected", rejected)),
        )
        if receipt.stage == "planned":
            if observed is not None:
                _block("registry_cas_probe_unowned_progress")
            receipt.stage = "seed_armed"
            checkpoint.phase = "registry_latest_probe_seed_armed"
            self._record(checkpoint)
        if receipt.stage == "seed_armed" and observed is None:
            self._require_lock(checkpoint, intent)
            seeded = self._public_write(
                checkpoint,
                intent,
                "registry_latest_probe_seed",
                lambda: self.adapter.put_registry_probe(
                    intent,
                    "registry_latest_probe_seed",
                    receipt.reference,
                    prior,
                    if_none_match=True,
                ),
            )
            self._assert_probe_write(seeded, 201, prior)
            observation = self.adapter.read_registry_probe(
                intent,
                receipt.reference,
            )
            observed = self._classify_probe_observation(
                observation,
                (("prior", prior), ("winner", winner), ("rejected", rejected)),
            )
        if receipt.stage == "seed_armed":
            if observed != "prior":
                _block("registry_cas_probe_retained_state_invalid")
            prior_observation = self.adapter.read_registry_probe(
                intent,
                receipt.reference,
            )
            self._assert_probe_exact(prior_observation, prior, require_etag=True)
            observed_prior_etag = prior_observation.etag or ""
            if receipt.prior_etag is not None:
                _block("registry_probe_prior_etag_already_set")
            receipt.prior_etag = observed_prior_etag
            receipt.stage = "cas_armed"
            checkpoint.phase = "registry_latest_probe_cas_armed"
            self._record(checkpoint)
        if receipt.stage == "cas_armed":
            if observed == "prior":
                prior_observation = self.adapter.read_registry_probe(
                    intent,
                    receipt.reference,
                )
                self._assert_probe_exact(
                    prior_observation,
                    prior,
                    require_etag=True,
                )
                if receipt.prior_etag != prior_observation.etag:
                    _block("registry_probe_prior_etag_changed")
                self._require_lock(checkpoint, intent)
                won = self._public_write(
                    checkpoint,
                    intent,
                    "registry_latest_probe_cas",
                    lambda: self.adapter.put_registry_probe(
                        intent,
                        "registry_latest_probe_cas",
                        receipt.reference,
                        winner,
                        if_match=receipt.prior_etag,
                    ),
                )
                self._assert_probe_write(won, 201, winner)
                observation = self.adapter.read_registry_probe(
                    intent,
                    receipt.reference,
                )
                observed = self._classify_probe_observation(
                    observation,
                    (
                        ("prior", prior),
                        ("winner", winner),
                        ("rejected", rejected),
                    ),
                )
            if observed != "winner":
                _block("registry_cas_probe_retained_state_invalid")
            self._assert_probe_exact(observation, winner, require_etag=True)
            if observation.etag == receipt.prior_etag:
                _block("registry_probe_etag_not_changed")
            receipt.winner_etag = observation.etag
            receipt.stage = "stale_armed"
            checkpoint.phase = "registry_latest_probe_stale_armed"
            self._record(checkpoint)
        if receipt.stage != "stale_armed" or observed != "winner":
            _block("registry_cas_probe_retained_state_invalid")
        if receipt.prior_etag is None:
            _block("registry_probe_prior_etag_missing")
        winner_observation = self.adapter.read_registry_probe(
            intent,
            receipt.reference,
        )
        self._assert_probe_exact(winner_observation, winner, require_etag=True)
        if winner_observation.etag == receipt.prior_etag:
            _block("registry_probe_etag_not_changed")
        if winner_observation.etag != receipt.winner_etag:
            _block("registry_probe_winner_etag_changed")
        self._require_lock(checkpoint, intent)
        rejected_result = self._public_write(
            checkpoint,
            intent,
            "registry_latest_probe_stale",
            lambda: self.adapter.put_registry_probe(
                intent,
                "registry_latest_probe_stale",
                receipt.reference,
                rejected,
                if_match=receipt.prior_etag,
            ),
        )
        self._assert_probe_write(rejected_result, 412, rejected)
        retained_winner = self.adapter.read_registry_probe(
            intent,
            receipt.reference,
        )
        self._assert_probe_exact(retained_winner, winner, require_etag=True)
        if retained_winner.etag != receipt.winner_etag:
            _block("registry_probe_winner_etag_changed")
        receipt.stage = "verified"
        receipt.verification_sha256 = self._probe_verification_sha256(
            intent,
            receipt,
            manifests,
        )
        checkpoint.phase = "registry_latest_probe_verified"
        self._record(checkpoint)

    def publish(
        self,
        intent: ReleaseIntent,
        *,
        recovery: PublicationCheckpoint | None = None,
    ) -> PublicationCheckpoint:
        """Run or recover the sole ordered Task 12 publication transaction."""

        intent.validate()
        source_proof = self.adapter.verify_local_sources(intent)
        source_proof.verify(intent)

        if recovery is None:
            first = self._actions_snapshot(intent)
            second = self._actions_snapshot(intent)
            if first != second:
                _block("actions_baseline_unstable")
            run_token = self.token_factory()
            lock_document = self._lock_document(intent, first, run_token)
            checkpoint = PublicationCheckpoint(
                intent_sha256=intent.intent_sha256,
                run_token=run_token,
                actions_baseline=first,
                lock_document_sha256=sha256_bytes(lock_document),
            )
            state = self._read_state(intent, checkpoint)
            if state.lock is not None:
                _block("publication_lock_preexisting")
            if (
                state.main_commit != intent.prior_main_commit
                or state.version_tag_object is not None
                or state.release is not None
                or state.version_digest is not None
                or state.latest_digest != intent.prior_latest_digest
            ):
                _block("unowned_partial_publication")
            self._record(checkpoint)
        else:
            checkpoint = recovery
            self._validate_checkpoint(checkpoint, intent)
            self._assert_actions_unchanged(intent, checkpoint.actions_baseline)
            lock_document = self._lock_document(
                intent,
                checkpoint.actions_baseline,
                checkpoint.run_token,
            )
            state = self._read_state(intent, checkpoint)
            if checkpoint.lock_removed:
                if state.lock is not None:
                    _block("removed_lock_reappeared")
                if self._is_final_state(state, intent):
                    self._verify_completion_evidence(checkpoint, intent)
                    checkpoint.phase = "complete"
                    checkpoint.failure_code = None
                    self._record(checkpoint)
                    return checkpoint
                _block("recovery_lock_missing_before_completion")
            if state.lock is None and checkpoint.phase in {
                "lock_ref_remove_pending",
                "lock_ref_remove_ambiguous",
                "lock_ref_remove_written",
            }:
                if self._is_final_state(state, intent):
                    self._verify_completion_evidence(checkpoint, intent)
                    checkpoint.lock_removed = True
                    checkpoint.phase = "complete"
                    checkpoint.failure_code = None
                    self._record(checkpoint)
                    return checkpoint
            if checkpoint.lock_acquired:
                if checkpoint.lock_object_sha is None or state.lock != LockObservation(
                    checkpoint.lock_object_sha,
                    checkpoint.lock_document_sha256,
                ):
                    _block("publication_lock_missing_or_replaced")
            elif state.lock is not None:
                expected_pending_lock = (
                    checkpoint.lock_object_sha is not None
                    and checkpoint.phase
                    in {
                        "lock_ref_create_pending",
                        "lock_ref_create_ambiguous",
                        "lock_ref_create_written",
                    }
                    and state.lock
                    == LockObservation(
                        checkpoint.lock_object_sha,
                        checkpoint.lock_document_sha256,
                    )
                )
                if not expected_pending_lock:
                    _block("publication_lock_replaced_during_acquisition")
                checkpoint.lock_acquired = True
                checkpoint.phase = "lock_ref_create_reconciled"
                checkpoint.failure_code = None
                self._record(checkpoint)

        if not checkpoint.lock_acquired:
            if checkpoint.lock_object_sha is None:
                object_sha = self._public_write(
                    checkpoint,
                    intent,
                    "lock_object_create",
                    lambda: self.adapter.create_lock_object(intent, lock_document),
                )
                checkpoint.lock_object_sha = _require_full_sha(
                    object_sha,
                    "lock_object",
                )
                self._record(checkpoint)
            state = self._read_state(intent, checkpoint)
            if state.lock is not None:
                _block("publication_lock_replaced_during_acquisition")
            self._public_write(
                checkpoint,
                intent,
                "lock_ref_create",
                lambda: self.adapter.create_lock_ref(
                    intent,
                    checkpoint.lock_object_sha or "",
                ),
            )
            checkpoint.lock_acquired = True
            self._require_lock(checkpoint, intent)
            self._record(checkpoint)

        state = self._read_state(intent, checkpoint)
        self._require_lock(checkpoint, intent)
        if state.main_commit == intent.prior_main_commit:
            self._public_write(
                checkpoint,
                intent,
                "main_fast_forward",
                lambda: self.adapter.advance_main(intent),
            )
            state = self._read_state(intent, checkpoint)
        if state.main_commit != intent.release_commit:
            _block("remote_main_not_release_commit")

        self._require_lock(checkpoint, intent)
        if state.version_tag_object is None:
            tag_object = self._public_write(
                checkpoint,
                intent,
                "version_tag_object_create",
                lambda: self.adapter.create_version_tag_object(intent),
            )
            if tag_object != intent.annotated_tag.object_sha:
                _block("annotated_tag_object_response_mismatch")
            self._public_write(
                checkpoint,
                intent,
                "version_tag_ref_create",
                lambda: self.adapter.create_version_tag_ref(intent),
            )
            state = self._read_state(intent, checkpoint)
        if state.version_tag_object != intent.annotated_tag.object_sha:
            _block("remote_version_tag_not_exact")

        for index, blob in enumerate(intent.required_blobs):
            self._require_lock(checkpoint, intent)
            if self.adapter.registry_blob_present(intent, blob):
                continue
            upload_url = checkpoint.blob_upload_urls.get(blob.digest)
            if upload_url is None:
                upload_url = self._public_write(
                    checkpoint,
                    intent,
                    f"registry_blob_{index:03d}_upload_start",
                    lambda blob=blob: self.adapter.start_registry_blob_upload(
                        intent, blob
                    ),
                )
                if not isinstance(upload_url, str) or not upload_url:
                    _block("registry_blob_upload_location_invalid")
                checkpoint.blob_upload_urls[blob.digest] = upload_url
                self._record(checkpoint)
            self._public_write(
                checkpoint,
                intent,
                f"registry_blob_{index:03d}_upload_finish",
                lambda blob=blob, upload_url=upload_url: (
                    self.adapter.finish_registry_blob_upload(intent, blob, upload_url)
                ),
            )
            if not self.adapter.registry_blob_present(intent, blob):
                _block("registry_blob_upload_not_exact")

        for index, manifest in enumerate(intent.required_manifests):
            self._require_lock(checkpoint, intent)
            if self.adapter.registry_manifest_present(intent, manifest):
                continue
            self._public_write(
                checkpoint,
                intent,
                f"registry_manifest_{index:03d}_put",
                lambda manifest=manifest: self.adapter.put_registry_manifest(
                    intent, manifest
                ),
            )
            if not self.adapter.registry_manifest_present(intent, manifest):
                _block("registry_manifest_upload_not_exact")

        self._require_lock(checkpoint, intent)
        if state.version_digest is None:
            self._run_version_create_probe(checkpoint, intent)
            self._require_lock(checkpoint, intent)
            self._public_write(
                checkpoint,
                intent,
                "registry_version_conditional_create",
                lambda: self.adapter.conditional_create_version(intent),
            )
            state = self._read_state(intent, checkpoint)
        if state.version_digest != intent.image.oci_index:
            _block("registry_version_not_exact")
        self._verify_retained_probe(checkpoint, intent, kind="create")

        self._require_lock(checkpoint, intent)
        smoke_identity = self.adapter.anonymous_pull_smoke(
            intent,
            source_proof.candidate_docker_engine_id_sha256,
        )
        smoke_identity.validate()
        if smoke_identity != intent.image:
            _block("anonymous_pull_smoke_identity_mismatch")
        checkpoint.anonymous_smoke_sha256 = sha256_bytes(
            canonical_json_bytes(asdict(smoke_identity))
        )
        checkpoint.phase = "anonymous_pull_smoke_verified"
        self._record(checkpoint)

        self._require_lock(checkpoint, intent)
        state = self._read_state(intent, checkpoint)
        if state.release is None:
            self._public_write(
                checkpoint,
                intent,
                "github_release_create",
                lambda: self.adapter.create_release(intent),
            )
            state = self._read_state(intent, checkpoint)
        if state.release is None or not state.release.is_exact(intent):
            _block("github_release_not_exact")

        self._require_lock(checkpoint, intent)
        if (
            checkpoint.latest_write_attempted
            and state.latest_digest == intent.prior_latest_digest
        ):
            self._verify_completion_evidence(checkpoint, intent)
            checkpoint.phase = "registry_latest_outcome_unresolved"
            checkpoint.failure_code = "registry_latest_outcome_unresolved"
            self._record(checkpoint)
            _block("registry_latest_outcome_unresolved")
        if state.latest_digest == intent.prior_latest_digest:
            state = self._read_state(intent, checkpoint)
            if state.latest_digest != intent.prior_latest_digest:
                _block("registry_latest_changed_before_compare_and_set")
            self._run_latest_cas_probe(checkpoint, intent)
            self._require_lock(checkpoint, intent)
            checkpoint.latest_write_attempted = True
            checkpoint.phase = "registry_latest_update_armed"
            self._record(checkpoint)
            self._public_write(
                checkpoint,
                intent,
                "registry_latest_update",
                lambda: self.adapter.update_latest(
                    intent,
                    intent.prior_latest_digest,
                ),
            )
            state = self._read_state(intent, checkpoint)
        if state.latest_digest != intent.image.oci_index:
            _block("registry_latest_not_exact")

        self._verify_completion_evidence(checkpoint, intent)

        self._require_lock(checkpoint, intent)
        self._public_write(
            checkpoint,
            intent,
            "lock_ref_remove",
            lambda: self.adapter.remove_lock_exact(
                intent,
                checkpoint.lock_object_sha or "",
            ),
        )
        state = self._read_state(intent, checkpoint)
        if state.lock is not None:
            _block("publication_lock_removal_unverified")
        checkpoint.lock_removed = True
        checkpoint.phase = "complete"
        checkpoint.failure_code = None
        self._record(checkpoint)
        if not self._is_final_state(state, intent):
            _block("final_publication_state_not_exact")
        return checkpoint

    @staticmethod
    def _is_final_state(state: PublicState, intent: ReleaseIntent) -> bool:
        return (
            state.main_commit == intent.release_commit
            and state.version_tag_object == intent.annotated_tag.object_sha
            and state.release is not None
            and state.release.is_exact(intent)
            and state.version_digest == intent.image.oci_index
            and state.latest_digest == intent.image.oci_index
            and state.lock is None
        )
