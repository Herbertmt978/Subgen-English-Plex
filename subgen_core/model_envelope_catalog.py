"""Immutable ModelEnvelope catalog and OCI identity artifact contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import errno
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Iterable, Mapping


CATALOG_SCHEMA = "subgen.model-envelope.catalog/v1"
IDENTITY_SCHEMA = "subgen.model-envelope.identity/v1"
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_CATALOG_ENTRIES = MAX_LAYER_DIFF_IDS = MAX_STRING_LENGTH = 256
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MODEL_REVISION_RE = re.compile(r"hf:[0-9a-f]{40}\Z")
_MODEL_REVISION_INPUT_RE = re.compile(r"(?:hf:)?([0-9a-f]{40})\Z")


class ArtifactValidationError(ValueError):
    """A bounded schema, canonicalization, or integrity failure."""


class ArtifactSecurityError(ArtifactValidationError):
    """An artifact path is not a safe owner-only regular file."""


@dataclass(frozen=True)
class ModelArtifactIdentity:
    """Exact converted weights; not an OCI envelope or admission permission.

    An artifact hash binds one backend's bytes. Cross-backend equivalence needs
    separately verified conversion provenance naming the same source checkpoint.
    Unknown provenance must stay unknown, even when both files say 'medium'.
    This experimental type does not alter the persisted CUDA catalog v1.
    """

    model: str
    backend_format: str
    precision: str
    weights_sha256: str
    size_bytes: int
    source_checkpoint_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.model) is not str or self.model not in {
            "tiny", "tiny.en", "base", "base.en", "small", "small.en",
            "medium", "medium.en", "large-v1", "large-v2", "large-v3",
        }:
            raise ArtifactValidationError("artifact_model")
        if type(self.backend_format) is not str or self.backend_format not in {"ggml", "ctranslate2"}:
            raise ArtifactValidationError("artifact_backend_format")
        _require_ascii_string(self.precision, "artifact_precision")
        _require_digest(self.weights_sha256, "weights_sha256")
        _require_positive_int(self.size_bytes, "weights_size_bytes")
        if self.size_bytes > 16 * 1024**3:
            raise ArtifactValidationError("weights_size_limit")
        if self.source_checkpoint_sha256 is not None:
            _require_digest(self.source_checkpoint_sha256, "source_checkpoint_sha256")


@dataclass(frozen=True)
class NativeArtifactIdentity:
    """One provisioned native binary; paths are supplied separately at launch."""

    component: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if type(self.component) is not str or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.component) is None:
            raise ArtifactValidationError("native_component")
        _require_digest(self.sha256, "native_sha256")
        _require_positive_int(self.size_bytes, "native_size_bytes")
        if self.size_bytes > 2 * 1024**3:
            raise ArtifactValidationError("native_size_limit")


def verify_native_artifact(path: str | Path, identity: NativeArtifactIdentity, *, check_cancelled=lambda: None) -> None:
    _revalidate(identity, NativeArtifactIdentity, "native_artifact")
    _verify_file_content(path, identity.sha256, identity.size_bytes, check_cancelled, "native")


@dataclass(frozen=True)
class ModelSupportFileIdentity:
    """A provisioned tokenizer/configuration file, not model weight identity."""

    name: str
    sha256: str
    size_bytes: int

    def __post_init__(self):
        if type(self.name) is not str or self.name not in {"config.json", "tokenizer.json", "preprocessor_config.json",
                             "vocabulary.json", "vocabulary.txt"}:
            raise ArtifactValidationError("model_support_filename")
        _require_digest(self.sha256, "model_support_sha256")
        _require_positive_int(self.size_bytes, "model_support_size_bytes")
        if self.size_bytes > 256 * 1024**2:
            raise ArtifactValidationError("model_support_size_limit")


def verify_ct2_model_directory(directory, weights, support_files, *, check_cancelled=lambda: None):
    """Verify local weights and every loader-relevant supporting artifact.

    Downloads are disabled by the worker. A tokenizer/config not provisioned
    with the weight conversion cannot silently change inference behavior.
    """
    root = Path(directory)
    _revalidate(weights, ModelArtifactIdentity, "model_artifact")
    if weights.backend_format != "ctranslate2" or not root.is_dir() or root.is_symlink():
        raise ArtifactValidationError("ct2_model_directory")
    if not isinstance(support_files, tuple) or not 3 <= len(support_files) <= 5:
        raise ArtifactValidationError("ct2_support_count")
    for identity in support_files:
        _revalidate(identity, ModelSupportFileIdentity, "model_support_file")
    names = {identity.name for identity in support_files}
    if len(names) != len(support_files) or not {"config.json", "tokenizer.json", "preprocessor_config.json"} <= names:
        raise ArtifactValidationError("ct2_support_incomplete")
    relevant = {"config.json", "tokenizer.json", "preprocessor_config.json", "vocabulary.json", "vocabulary.txt"}
    if {p.name for p in root.iterdir() if p.name in relevant} != names:
        raise ArtifactValidationError("ct2_support_unprovisioned")
    verify_model_artifact(root / "model.bin", weights, check_cancelled=check_cancelled)
    for identity in support_files:
        _verify_file_content(root / identity.name, identity.sha256, identity.size_bytes,
                             check_cancelled, "model_support")


def same_source_checkpoint(left: ModelArtifactIdentity, right: ModelArtifactIdentity) -> bool:
    """Compare provenance only, not precision equivalence or memory suitability."""
    _revalidate(left, ModelArtifactIdentity, "left_model_artifact")
    _revalidate(right, ModelArtifactIdentity, "right_model_artifact")
    return (left.source_checkpoint_sha256 is not None
            and left.source_checkpoint_sha256 == right.source_checkpoint_sha256
            and left.model == right.model)


def ggml_weight_ftype(identity: ModelArtifactIdentity) -> int:
    """Expected loaded weight format, not GPU arithmetic/activation precision.

    Values are ggml_ftype in pinned whisper.cpp 52a939a. whisper_model_ftype
    returns the normalized code after removing the quantization-version factor.
    Mostly-F16 retains some F32 tensors; it is not all-F16 inference.
    Recognizing a format does not certify its quality or hardware support.
    """
    _revalidate(identity, ModelArtifactIdentity, "model_artifact")
    formats = {"float32": 0, "float16": 1, "q4_0": 2, "q4_1": 3,
               "q8_0": 7, "q5_0": 8, "q5_1": 9}
    if identity.backend_format != "ggml" or identity.precision not in formats:
        raise ArtifactValidationError("unsupported_ggml_weight_format")
    return formats[identity.precision]


def validate_cohort_model_identity(artifacts: tuple[ModelArtifactIdentity, ...]) -> ModelArtifactIdentity:
    """Require one known checkpoint and weight precision across all workers.

    Identical formats additionally require identical artifact bytes. Different
    formats are allowed only with known source provenance and matching F16/F32
    weights; similarly named quantizers across engines are not assumed equal.
    This validates recorded provenance, not the provisioning process that
    produced it. The lifecycle owner must also verify each on-disk artifact.
    """
    if not isinstance(artifacts, tuple) or not 1 <= len(artifacts) <= 32:
        raise ArtifactValidationError("cohort_artifact_count")
    for artifact in artifacts:
        _revalidate(artifact, ModelArtifactIdentity, "cohort_model_artifact")
    first = artifacts[0]
    if first.source_checkpoint_sha256 is None:
        raise ArtifactValidationError("cohort_checkpoint_unknown")
    by_format = {}
    for artifact in artifacts:
        if not same_source_checkpoint(first, artifact):
            raise ArtifactValidationError("cohort_checkpoint_mismatch")
        if artifact.precision != first.precision:
            raise ArtifactValidationError("cohort_weight_precision_mismatch")
        if artifact.backend_format != first.backend_format and first.precision not in {"float16", "float32"}:
            raise ArtifactValidationError("cohort_cross_backend_quantization_unqualified")
        previous = by_format.setdefault(artifact.backend_format, artifact)
        if (previous.weights_sha256, previous.size_bytes) != (artifact.weights_sha256, artifact.size_bytes):
            raise ArtifactValidationError("cohort_same_format_weights_mismatch")
    return first


def verify_model_artifact(path: str | Path, identity: ModelArtifactIdentity, *, check_cancelled=lambda: None) -> None:
    """Bounded read-only byte verification, without inventing native OCI identity.

    Provisioning still owns immutable paths and verified conversion provenance.
    This is not loaded-module attestation or a Windows ACL/cgroup substitute.
    The lifecycle owner supplies cancellation/deadline checks between blocks.
    """
    _revalidate(identity, ModelArtifactIdentity, "model_artifact")
    _verify_file_content(path, identity.weights_sha256, identity.size_bytes, check_cancelled, "weights")


def _verify_file_content(path, expected_digest, expected_size, check_cancelled, label):
    if not callable(check_cancelled):
        raise TypeError("Artifact verification requires a cancellation check")
    model_path = Path(path)
    check_cancelled()
    try:
        before = model_path.lstat()
        if not stat.S_ISREG(before.st_mode) or model_path.is_symlink():
            raise ArtifactValidationError(f"{label}_not_regular_file")
        if before.st_size != expected_size:
            raise ArtifactValidationError(f"{label}_size_mismatch")
        digest = hashlib.sha256()
        with model_path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _same_file(before, opened):
                raise ArtifactValidationError(f"{label}_file_changed")
            remaining = expected_size
            while remaining:
                check_cancelled()
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    raise ArtifactValidationError(f"{label}_file_changed")
                digest.update(block)
                remaining -= len(block)
            if stream.read(1):
                raise ArtifactValidationError(f"{label}_file_changed")
            after = os.fstat(stream.fileno())
        rebound = model_path.lstat()
        for sample in (after, rebound):
            if (not _same_file(opened, sample) or sample.st_size != opened.st_size
                    or sample.st_mtime_ns != opened.st_mtime_ns):
                raise ArtifactValidationError(f"{label}_file_changed")
    except OSError:
        raise ArtifactValidationError(f"{label}_unreadable") from None
    check_cancelled()
    if "sha256:" + digest.hexdigest() != expected_digest:
        raise ArtifactValidationError(f"{label}_digest_mismatch")


def normalize_model_revision(value: str) -> str:
    """Normalize one immutable Hugging Face commit for policy matching."""

    if type(value) is not str:
        raise ArtifactValidationError("model_revision")
    match = _MODEL_REVISION_INPUT_RE.fullmatch(value.strip())
    if match is None:
        raise ArtifactValidationError("model_revision")
    return "hf:" + match.group(1)


def decoder_options_sha256(options: Mapping[str, object]) -> str:
    """Hash decoder options with the catalog's canonical JSON rules."""

    if not isinstance(options, Mapping) or any(type(key) is not str for key in options):
        raise ArtifactValidationError("decoder_options")
    try:
        payload = json.dumps(
            dict(options),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError("decoder_options") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class EnvelopeDisposition(str, Enum):
    EXACT_MATCH = "exact_match"
    PUBLIC_FALLBACK = "public_fallback"
    FAIL_CLOSED = "fail_closed"


RESOLUTION_REASON_CODES = frozenset(
    (
        "catalog_missing",
        "catalog_unreadable",
        "catalog_unsafe",
        "catalog_invalid",
        "identity_missing",
        "identity_unreadable",
        "identity_unsafe",
        "identity_invalid",
        "image_identity_mismatch",
        "identity_not_in_catalog",
        "runtime_policy_mismatch",
        "canonical_provenance_missing",
    )
)


@dataclass(frozen=True)
class ImageIdentity:
    config_digest: str
    layer_diff_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_digest(self.config_digest, "config_digest")
        if type(self.layer_diff_ids) is not tuple or not self.layer_diff_ids:
            raise ArtifactValidationError("layer_diff_ids")
        if len(self.layer_diff_ids) > MAX_LAYER_DIFF_IDS:
            raise ArtifactValidationError("layer_diff_ids_limit")
        for value in self.layer_diff_ids:
            _require_digest(value, "layer_diff_ids")


@dataclass(frozen=True)
class ImageIdentityArtifact:
    schema: str
    image_identity: ImageIdentity

    def __post_init__(self) -> None:
        if type(self.schema) is not str or self.schema != IDENTITY_SCHEMA:
            raise ArtifactValidationError("identity_schema")
        _revalidate(self.image_identity, ImageIdentity, "image_identity")


@dataclass(frozen=True)
class RuntimeIdentity:
    stable_ts_version: str
    faster_whisper_version: str
    ctranslate2_version: str
    cuda_runtime_version: str
    driver_version: str
    device_name: str
    compute_capability: str
    total_vram_bytes: int

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if name == "total_vram_bytes":
                continue
            _require_ascii_string(getattr(self, name), name)
        _require_positive_int(self.total_vram_bytes, "total_vram_bytes")


@dataclass(frozen=True)
class EnvelopePolicy:
    model: str
    model_revision: str
    compute_type: str
    task: str
    inference_concurrency: int
    chunk_minutes: int
    decoder_options_sha256: str

    def __post_init__(self) -> None:
        _require_ascii_string(self.model, "model")
        if self.model not in {"tiny", "base", "small", "medium", "large-v3"}:
            raise ArtifactValidationError("model")
        _require_model_revision(self.model_revision)
        _require_ascii_string(self.compute_type, "compute_type")
        _require_ascii_string(self.task, "task")
        if self.task not in {"transcribe", "translate"}:
            raise ArtifactValidationError("task")
        _require_positive_int(self.inference_concurrency, "inference_concurrency")
        _require_positive_int(self.chunk_minutes, "chunk_minutes")
        if not 5 <= self.chunk_minutes <= 60:
            raise ArtifactValidationError("chunk_minutes")
        _require_digest(self.decoder_options_sha256, "decoder_options_sha256")


@dataclass(frozen=True)
class EnvelopeMeasurements:
    runs: int
    host_preload_used_bytes: int
    host_peak_used_bytes: int
    cgroup_preload_used_bytes: int
    cgroup_peak_used_bytes: int
    device_preload_used_bytes: int
    device_peak_used_bytes: int
    host_incremental_peak_bytes: int
    cgroup_incremental_peak_bytes: int
    device_incremental_peak_bytes: int
    host_margin_bytes: int
    device_margin_bytes: int

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            _require_positive_int(getattr(self, name), name)
        if self.runs < 3:
            raise ArtifactValidationError("runs")
        for domain in ("host", "cgroup", "device"):
            preload = getattr(self, f"{domain}_preload_used_bytes")
            peak = getattr(self, f"{domain}_peak_used_bytes")
            incremental = getattr(self, f"{domain}_incremental_peak_bytes")
            if peak < preload or not peak - preload <= incremental <= peak:
                raise ArtifactValidationError(f"{domain}_measurements")


@dataclass(frozen=True)
class ModelEnvelope:
    image_identity: ImageIdentity
    runtime: RuntimeIdentity
    policy: EnvelopePolicy
    measurements: EnvelopeMeasurements

    def __post_init__(self) -> None:
        nested = (
            (self.image_identity, ImageIdentity, "image_identity"),
            (self.runtime, RuntimeIdentity, "runtime"),
            (self.policy, EnvelopePolicy, "policy"),
            (self.measurements, EnvelopeMeasurements, "measurements"),
        )
        for value, kind, name in nested:
            _revalidate(value, kind, name)


@dataclass(frozen=True)
class CatalogIntegrity:
    algorithm: str
    canonical_payload_sha256: str

    def __post_init__(self) -> None:
        if type(self.algorithm) is not str or self.algorithm != "sha256":
            raise ArtifactValidationError("integrity_algorithm")
        _require_digest(self.canonical_payload_sha256, "integrity_digest")


@dataclass(frozen=True)
class ModelEnvelopeCatalog:
    schema: str
    catalog_version: int
    entries: tuple[ModelEnvelope, ...]
    integrity: CatalogIntegrity

    def __post_init__(self) -> None:
        if type(self.schema) is not str or self.schema != CATALOG_SCHEMA:
            raise ArtifactValidationError("catalog_schema")
        _require_positive_int(self.catalog_version, "catalog_version")
        if type(self.entries) is not tuple:
            raise ArtifactValidationError("entries")
        if len(self.entries) > MAX_CATALOG_ENTRIES:
            raise ArtifactValidationError("entries_limit")
        for entry in self.entries:
            _revalidate(entry, ModelEnvelope, "entries")
        _revalidate(self.integrity, CatalogIntegrity, "integrity")
        _reject_duplicate_matches(self.entries)
        _verify_catalog_integrity(self)


@dataclass(frozen=True)
class EnvelopeResolution:
    envelope: ModelEnvelope | None
    disposition: EnvelopeDisposition
    reason_code: str | None

    def __post_init__(self) -> None:
        if type(self.disposition) is not EnvelopeDisposition:
            raise ArtifactValidationError("resolution")
        if self.disposition is EnvelopeDisposition.EXACT_MATCH:
            if self.reason_code is not None:
                raise ArtifactValidationError("resolution")
            _revalidate(self.envelope, ModelEnvelope, "resolution")
        elif (
            self.envelope is not None
            or type(self.reason_code) is not str
            or self.reason_code not in RESOLUTION_REASON_CODES
        ):
            raise ArtifactValidationError("resolution")

    @property
    def matched(self) -> bool:
        return self.disposition is EnvelopeDisposition.EXACT_MATCH

    @property
    def use_public_fallback(self) -> bool:
        return self.disposition is EnvelopeDisposition.PUBLIC_FALLBACK

    @property
    def fail_closed(self) -> bool:
        return self.disposition is EnvelopeDisposition.FAIL_CLOSED


def build_catalog(
    *, catalog_version: int, entries: Iterable[ModelEnvelope]
) -> ModelEnvelopeCatalog:
    _require_positive_int(catalog_version, "catalog_version")
    values = tuple(islice(entries, MAX_CATALOG_ENTRIES + 1))
    if len(values) > MAX_CATALOG_ENTRIES:
        raise ArtifactValidationError("entries_limit")
    for entry in values:
        _revalidate(entry, ModelEnvelope, "entries")
    _reject_duplicate_matches(values)
    digest = (
        "sha256:"
        + hashlib.sha256(_canonical_payload_bytes(catalog_version, values)).hexdigest()
    )
    return ModelEnvelopeCatalog(
        schema=CATALOG_SCHEMA,
        catalog_version=catalog_version,
        entries=values,
        integrity=CatalogIntegrity("sha256", digest),
    )


def find_exact_envelope(
    catalog: ModelEnvelopeCatalog,
    identity: ImageIdentityArtifact,
    runtime: RuntimeIdentity,
    policy: EnvelopePolicy,
) -> ModelEnvelope | None:
    for value, kind, name in (
        (catalog, ModelEnvelopeCatalog, "catalog"),
        (identity, ImageIdentityArtifact, "identity"),
        (runtime, RuntimeIdentity, "runtime"),
        (policy, EnvelopePolicy, "policy"),
    ):
        _revalidate(value, kind, name)
    target_key = (
        _image_identity_key(identity.image_identity),
        _runtime_key(runtime),
        _policy_key(policy),
    )
    for envelope in catalog.entries:
        if _envelope_match_key(envelope) == target_key:
            return envelope
    return None


def resolve_envelope(
    catalog_path: str | Path,
    identity_path: str | Path,
    *,
    runtime: RuntimeIdentity,
    policy: EnvelopePolicy,
    canonical_shared_cuda: bool,
    expected_image_identity: ImageIdentity | None = None,
    expected_uid: int | None = None,
) -> EnvelopeResolution:
    _revalidate(runtime, RuntimeIdentity, "runtime")
    _revalidate(policy, EnvelopePolicy, "policy")
    if type(canonical_shared_cuda) is not bool:
        raise ArtifactValidationError("canonical_shared_cuda_type")
    if expected_image_identity is not None:
        _revalidate(expected_image_identity, ImageIdentity, "expected_image_identity")
    expected_uid = _require_expected_uid(expected_uid)
    if canonical_shared_cuda and (
        expected_image_identity is None or expected_uid is None
    ):
        return _failure_resolution("canonical_provenance_missing", True)
    try:
        catalog = load_catalog(catalog_path, expected_uid=expected_uid)
    except FileNotFoundError:
        return _failure_resolution("catalog_missing", canonical_shared_cuda)
    except ArtifactSecurityError:
        return _failure_resolution("catalog_unsafe", canonical_shared_cuda)
    except ArtifactValidationError:
        return _failure_resolution("catalog_invalid", canonical_shared_cuda)
    except OSError:
        return _failure_resolution("catalog_unreadable", canonical_shared_cuda)

    try:
        identity = load_identity(identity_path, expected_uid=expected_uid)
    except FileNotFoundError:
        return _failure_resolution("identity_missing", canonical_shared_cuda)
    except ArtifactSecurityError:
        return _failure_resolution("identity_unsafe", canonical_shared_cuda)
    except ArtifactValidationError:
        return _failure_resolution("identity_invalid", canonical_shared_cuda)
    except OSError:
        return _failure_resolution("identity_unreadable", canonical_shared_cuda)

    identity_key = _image_identity_key(identity.image_identity)
    if expected_image_identity is not None and identity_key != _image_identity_key(
        expected_image_identity
    ):
        return _failure_resolution("image_identity_mismatch", canonical_shared_cuda)

    identity_entries = tuple(
        entry
        for entry in catalog.entries
        if _image_identity_key(entry.image_identity) == identity_key
    )
    if not identity_entries:
        return _failure_resolution("identity_not_in_catalog", canonical_shared_cuda)
    envelope = find_exact_envelope(catalog, identity, runtime, policy)
    if envelope is None:
        return _failure_resolution("runtime_policy_mismatch", canonical_shared_cuda)
    return EnvelopeResolution(envelope, EnvelopeDisposition.EXACT_MATCH, None)


def canonical_payload_bytes(catalog: ModelEnvelopeCatalog) -> bytes:
    _revalidate(catalog, ModelEnvelopeCatalog, "catalog")
    return _canonical_payload_bytes(catalog.catalog_version, catalog.entries)


def canonical_envelope_bytes(envelope: ModelEnvelope) -> bytes:
    """Serialize one exact catalog entry for an external identity proof."""

    _revalidate(envelope, ModelEnvelope, "envelope")
    return _json_bytes(_envelope_dict(envelope)) + b"\n"


def canonical_envelope_policy_bytes(policy: EnvelopePolicy) -> bytes:
    """Serialize one exact envelope policy for an external identity proof."""

    _revalidate(policy, EnvelopePolicy, "policy")
    return (
        _json_bytes(
            {
                "model": policy.model,
                "model_revision": policy.model_revision,
                "compute_type": policy.compute_type,
                "task": policy.task,
                "inference_concurrency": policy.inference_concurrency,
                "chunk_minutes": policy.chunk_minutes,
                "decoder_options_sha256": policy.decoder_options_sha256,
            }
        )
        + b"\n"
    )


def model_identity_sha256(envelope: ModelEnvelope) -> str:
    """Bind a resident backend to its exact immutable catalog entry and policy."""

    _revalidate(envelope, ModelEnvelope, "envelope")
    identity = {
        "catalog_entry_sha256": hashlib.sha256(
            canonical_envelope_bytes(envelope)
        ).hexdigest(),
        "model_policy_sha256": hashlib.sha256(
            canonical_envelope_policy_bytes(envelope.policy)
        ).hexdigest(),
        "model_revision": envelope.policy.model_revision,
        "selected_model": envelope.policy.model,
    }
    return hashlib.sha256(_json_bytes(identity) + b"\n").hexdigest()


def serialize_catalog(catalog: ModelEnvelopeCatalog) -> bytes:
    _revalidate(catalog, ModelEnvelopeCatalog, "catalog")
    return _json_bytes(
        {
            "schema": catalog.schema,
            "catalog_version": catalog.catalog_version,
            "entries": [_envelope_dict(entry) for entry in catalog.entries],
            "integrity": {
                "algorithm": catalog.integrity.algorithm,
                "canonical_payload_sha256": catalog.integrity.canonical_payload_sha256,
            },
        }
    )


def serialize_identity(identity: ImageIdentityArtifact) -> bytes:
    _revalidate(identity, ImageIdentityArtifact, "identity")
    return _json_bytes(
        {
            "schema": identity.schema,
            "image_identity": _image_identity_dict(identity.image_identity),
        }
    )


def load_catalog(
    path: str | Path, *, expected_uid: int | None = None
) -> ModelEnvelopeCatalog:
    return _parse_catalog_bytes(_read_artifact_bytes(path, expected_uid=expected_uid))


def _parse_catalog_bytes(raw: bytes) -> ModelEnvelopeCatalog:
    value = _decode_document(raw)
    _require_fields(
        value, {"schema", "catalog_version", "entries", "integrity"}, "catalog"
    )
    if value["schema"] != CATALOG_SCHEMA:
        raise ArtifactValidationError("catalog_schema")
    catalog_version = _require_positive_int(value["catalog_version"], "catalog_version")
    raw_entries = value["entries"]
    if not isinstance(raw_entries, list):
        raise ArtifactValidationError("entries")
    if len(raw_entries) > MAX_CATALOG_ENTRIES:
        raise ArtifactValidationError("entries_limit")
    entries = tuple(_envelope_from_dict(entry) for entry in raw_entries)
    integrity = _require_mapping(value["integrity"], "integrity")
    _require_fields(integrity, {"algorithm", "canonical_payload_sha256"}, "integrity")
    catalog = ModelEnvelopeCatalog(
        schema=CATALOG_SCHEMA,
        catalog_version=catalog_version,
        entries=entries,
        integrity=CatalogIntegrity(
            integrity["algorithm"], integrity["canonical_payload_sha256"]
        ),
    )
    _verify_catalog_integrity(catalog)
    return catalog


def load_identity(
    path: str | Path, *, expected_uid: int | None = None
) -> ImageIdentityArtifact:
    return _parse_identity_bytes(_read_artifact_bytes(path, expected_uid=expected_uid))


def _parse_identity_bytes(raw: bytes) -> ImageIdentityArtifact:
    value = _decode_document(raw)
    _require_fields(value, {"schema", "image_identity"}, "identity")
    if value["schema"] != IDENTITY_SCHEMA:
        raise ArtifactValidationError("identity_schema")
    return ImageIdentityArtifact(
        schema=IDENTITY_SCHEMA,
        image_identity=_image_identity_from_dict(value["image_identity"]),
    )


def write_catalog(
    path: str | Path,
    catalog: ModelEnvelopeCatalog,
    *,
    expected_uid: int | None = None,
) -> None:
    _atomic_write(path, serialize_catalog(catalog), expected_uid=expected_uid)


def write_identity(
    path: str | Path,
    identity: ImageIdentityArtifact,
    *,
    expected_uid: int | None = None,
) -> None:
    _atomic_write(path, serialize_identity(identity), expected_uid=expected_uid)


def _failure_resolution(
    reason_code: str, canonical_shared_cuda: bool
) -> EnvelopeResolution:
    disposition = (
        EnvelopeDisposition.FAIL_CLOSED
        if canonical_shared_cuda
        else EnvelopeDisposition.PUBLIC_FALLBACK
    )
    return EnvelopeResolution(None, disposition, reason_code)


def _read_artifact_bytes(path: str | Path, *, expected_uid: int | None = None) -> bytes:
    expected_uid = _require_expected_uid(expected_uid)
    _require_posix_artifact_security()
    artifact_path = Path(os.path.abspath(os.fspath(path)))
    parent_fd, owner_uid = _open_parent_directory(artifact_path, expected_uid)
    descriptor = -1
    try:
        before = _artifact_stat(artifact_path.name, parent_fd)
        _validate_artifact_stat(before, owner_uid)
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(artifact_path.name, flags, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ArtifactSecurityError("symlink") from exc
            raise
        opened = os.fstat(descriptor)
        _validate_artifact_stat(opened, owner_uid)
        after = _artifact_stat(artifact_path.name, parent_fd)
        if not _same_file(before, opened) or not _same_file(opened, after):
            raise ArtifactSecurityError("identity_changed")
        _verify_parent_binding(artifact_path, parent_fd, owner_uid)
        if opened.st_size > MAX_ARTIFACT_BYTES:
            raise ArtifactValidationError("artifact_size_limit")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            payload = handle.read(MAX_ARTIFACT_BYTES + 1)
            if len(payload) > MAX_ARTIFACT_BYTES:
                raise ArtifactValidationError("artifact_size_limit")
            return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _open_parent_directory(path: Path, expected_uid: int | None) -> tuple[int, int]:
    for ancestor in reversed(path.parents):
        metadata = ancestor.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ArtifactSecurityError("symlink_ancestor")
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode):
        raise ArtifactSecurityError("parent_not_directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path.parent, flags)
    try:
        opened = os.fstat(descriptor)
        owner_uid = opened.st_uid if expected_uid is None else expected_uid
        _validate_parent_stat(opened, owner_uid)
        after = path.parent.lstat()
        if not _same_file(parent, opened) or not _same_file(opened, after):
            raise ArtifactSecurityError("parent_identity_changed")
        return descriptor, owner_uid
    except Exception:
        os.close(descriptor)
        raise


def _validate_parent_stat(metadata: os.stat_result, expected_uid: int) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ArtifactSecurityError("parent_not_directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ArtifactSecurityError("parent_owner_only_mode")
    if metadata.st_uid != expected_uid:
        raise ArtifactSecurityError("owner_mismatch")


def _verify_parent_binding(path: Path, descriptor: int, expected_uid: int) -> None:
    opened = os.fstat(descriptor)
    _validate_parent_stat(opened, expected_uid)
    current = path.parent.lstat()
    if stat.S_ISLNK(current.st_mode) or not _same_file(opened, current):
        raise ArtifactSecurityError("parent_identity_changed")


def _artifact_stat(name: str, parent_fd: int) -> os.stat_result:
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(metadata.st_mode):
        raise ArtifactSecurityError("symlink")
    return metadata


def _validate_artifact_stat(metadata: os.stat_result, expected_uid: int | None) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ArtifactSecurityError("not_regular")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ArtifactSecurityError("owner_only_mode")
    if metadata.st_uid != expected_uid:
        raise ArtifactSecurityError("owner_mismatch")
    if metadata.st_nlink != 1:
        raise ArtifactSecurityError("link_count")


def _atomic_write(
    path: str | Path, payload: bytes, *, expected_uid: int | None = None
) -> None:
    expected_uid = _require_expected_uid(expected_uid)
    _require_posix_artifact_security()
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ArtifactValidationError("artifact_size_limit")
    target = Path(os.path.abspath(os.fspath(path)))
    parent_fd, owner_uid = _open_parent_directory(target, expected_uid)
    descriptor = -1
    temporary_name: str | None = None
    try:
        original = _validate_existing_target(target.name, parent_fd, owner_uid)
        descriptor, temporary_name = _create_temporary(target.name, parent_fd)
        os.fchmod(descriptor, 0o600)
        if os.fstat(descriptor).st_uid != owner_uid:
            os.fchown(descriptor, owner_uid, -1)
        temporary_stat = os.fstat(descriptor)
        _validate_artifact_stat(temporary_stat, owner_uid)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        current = _validate_existing_target(target.name, parent_fd, owner_uid)
        if (original is None) != (current is None) or (
            original is not None
            and current is not None
            and (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ArtifactSecurityError("identity_changed")
        staged = _validate_existing_target(temporary_name, parent_fd, owner_uid)
        if staged is None or not _same_file(temporary_stat, staged):
            raise ArtifactSecurityError("temporary_identity_changed")
        _verify_parent_binding(target, parent_fd, owner_uid)
        os.replace(
            temporary_name,
            target.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_name = None
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _validate_existing_target(
    name: str, parent_fd: int, expected_uid: int
) -> os.stat_result | None:
    try:
        metadata = _artifact_stat(name, parent_fd)
    except FileNotFoundError:
        return None
    _validate_artifact_stat(metadata, expected_uid)
    return metadata


def _create_temporary(name: str, parent_fd: int) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    for _ in range(10):
        temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
        try:
            return os.open(
                temporary_name, flags, 0o600, dir_fd=parent_fd
            ), temporary_name
        except FileExistsError:
            continue
    raise ArtifactSecurityError("temporary_name_exhausted")


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _require_posix_artifact_security() -> None:
    if os.name == "nt":
        raise ArtifactSecurityError("owner_only_unverifiable")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_payload_bytes(
    catalog_version: int, entries: tuple[ModelEnvelope, ...]
) -> bytes:
    return _json_bytes(
        {
            "schema": CATALOG_SCHEMA,
            "catalog_version": catalog_version,
            "entries": [_envelope_dict(entry) for entry in entries],
        }
    )


def _verify_catalog_integrity(catalog: ModelEnvelopeCatalog) -> None:
    payload = _canonical_payload_bytes(catalog.catalog_version, catalog.entries)
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    if catalog.integrity.canonical_payload_sha256 != expected:
        raise ArtifactValidationError("integrity_mismatch")


def _decode_document(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ArtifactValidationError("ascii_document") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except ArtifactValidationError:
        raise
    except json.JSONDecodeError as exc:
        raise ArtifactValidationError("json_syntax") from exc
    except (ValueError, RecursionError) as exc:
        raise ArtifactValidationError("json_limits") from exc
    return _require_mapping(value, "document")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ArtifactValidationError("duplicate_key")
        value[key] = item
    return value


def _reject_json_constant(_: str) -> object:
    raise ArtifactValidationError("nonfinite_number")


def _require_mapping(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ArtifactValidationError(f"{name}_object")
    return value


def _require_fields(
    value: Mapping[str, object], expected: Iterable[str], name: str
) -> None:
    if set(value) != set(expected):
        raise ArtifactValidationError(f"{name}_fields")


def _require_ascii_string(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_STRING_LENGTH
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value)
    ):
        suffix = (
            "limit"
            if type(value) is str and len(value) > MAX_STRING_LENGTH
            else "ascii_string"
        )
        raise ArtifactValidationError(f"{name}_{suffix}")
    return value


def _require_positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ArtifactValidationError(f"{name}_positive_integer")
    return value


def _require_exact_type(value: object, kind: type, name: str) -> None:
    if type(value) is not kind:
        raise ArtifactValidationError(f"{name}_type")


def _revalidate(value: object, kind: type, name: str) -> None:
    _require_exact_type(value, kind, name)
    kind.__post_init__(value)


def _require_expected_uid(value: object) -> int | None:
    if value is not None and (type(value) is not int or value < 0):
        raise ArtifactValidationError("expected_uid_type")
    return value


def _require_digest(value: object, name: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ArtifactValidationError(f"{name}_digest")
    return value


def _require_model_revision(value: object) -> str:
    if type(value) is not str or _MODEL_REVISION_RE.fullmatch(value) is None:
        raise ArtifactValidationError("model_revision")
    return value


def _reject_duplicate_matches(entries: tuple[ModelEnvelope, ...]) -> None:
    seen: set[tuple[object, ...]] = set()
    for entry in entries:
        key = _envelope_match_key(entry)
        if key in seen:
            raise ArtifactValidationError("duplicate_match")
        seen.add(key)


def _image_identity_dict(identity: ImageIdentity) -> dict[str, object]:
    return {
        "config_digest": identity.config_digest,
        "layer_diff_ids": list(identity.layer_diff_ids),
    }


def _image_identity_key(identity: ImageIdentity) -> tuple[str, tuple[str, ...]]:
    _revalidate(identity, ImageIdentity, "image_identity")
    return identity.config_digest, identity.layer_diff_ids


def _runtime_key(runtime: RuntimeIdentity) -> tuple[object, ...]:
    _revalidate(runtime, RuntimeIdentity, "runtime")
    return tuple(
        getattr(runtime, name) for name in RuntimeIdentity.__dataclass_fields__
    )


def _policy_key(policy: EnvelopePolicy) -> tuple[object, ...]:
    _revalidate(policy, EnvelopePolicy, "policy")
    return tuple(getattr(policy, name) for name in EnvelopePolicy.__dataclass_fields__)


def _envelope_match_key(envelope: ModelEnvelope) -> tuple[object, ...]:
    _revalidate(envelope, ModelEnvelope, "envelope")
    return (
        _image_identity_key(envelope.image_identity),
        _runtime_key(envelope.runtime),
        _policy_key(envelope.policy),
    )


def _image_identity_from_dict(value: object) -> ImageIdentity:
    fields = _require_mapping(value, "image_identity")
    _require_fields(fields, {"config_digest", "layer_diff_ids"}, "image_identity")
    layer_diff_ids = fields["layer_diff_ids"]
    if type(layer_diff_ids) is not list or not layer_diff_ids:
        raise ArtifactValidationError("layer_diff_ids")
    if len(layer_diff_ids) > MAX_LAYER_DIFF_IDS:
        raise ArtifactValidationError("layer_diff_ids_limit")
    return ImageIdentity(
        config_digest=_require_digest(fields["config_digest"], "config_digest"),
        layer_diff_ids=tuple(
            _require_digest(item, "layer_diff_ids") for item in layer_diff_ids
        ),
    )


def _envelope_dict(envelope: ModelEnvelope) -> dict[str, object]:
    runtime = envelope.runtime
    policy = envelope.policy
    measurements = envelope.measurements
    return {
        "image_identity": _image_identity_dict(envelope.image_identity),
        "runtime": {
            "stable_ts_version": runtime.stable_ts_version,
            "faster_whisper_version": runtime.faster_whisper_version,
            "ctranslate2_version": runtime.ctranslate2_version,
            "cuda_runtime_version": runtime.cuda_runtime_version,
            "driver_version": runtime.driver_version,
            "device_name": runtime.device_name,
            "compute_capability": runtime.compute_capability,
            "total_vram_bytes": runtime.total_vram_bytes,
        },
        "policy": {
            "model": policy.model,
            "model_revision": policy.model_revision,
            "compute_type": policy.compute_type,
            "task": policy.task,
            "inference_concurrency": policy.inference_concurrency,
            "chunk_minutes": policy.chunk_minutes,
            "decoder_options_sha256": policy.decoder_options_sha256,
        },
        "measurements": {
            name: getattr(measurements, name)
            for name in EnvelopeMeasurements.__dataclass_fields__
        },
    }


def _envelope_from_dict(value: object) -> ModelEnvelope:
    fields = _require_mapping(value, "entry")
    _require_fields(
        fields, {"image_identity", "runtime", "policy", "measurements"}, "entry"
    )
    runtime = _require_mapping(fields["runtime"], "runtime")
    policy = _require_mapping(fields["policy"], "policy")
    measurements = _require_mapping(fields["measurements"], "measurements")
    _require_fields(runtime, RuntimeIdentity.__dataclass_fields__, "runtime")
    _require_fields(policy, EnvelopePolicy.__dataclass_fields__, "policy")
    _require_fields(
        measurements, EnvelopeMeasurements.__dataclass_fields__, "measurements"
    )
    return ModelEnvelope(
        image_identity=_image_identity_from_dict(fields["image_identity"]),
        runtime=RuntimeIdentity(
            stable_ts_version=_require_ascii_string(
                runtime["stable_ts_version"], "stable_ts_version"
            ),
            faster_whisper_version=_require_ascii_string(
                runtime["faster_whisper_version"], "faster_whisper_version"
            ),
            ctranslate2_version=_require_ascii_string(
                runtime["ctranslate2_version"], "ctranslate2_version"
            ),
            cuda_runtime_version=_require_ascii_string(
                runtime["cuda_runtime_version"], "cuda_runtime_version"
            ),
            driver_version=_require_ascii_string(
                runtime["driver_version"], "driver_version"
            ),
            device_name=_require_ascii_string(runtime["device_name"], "device_name"),
            compute_capability=_require_ascii_string(
                runtime["compute_capability"], "compute_capability"
            ),
            total_vram_bytes=_require_positive_int(
                runtime["total_vram_bytes"], "total_vram_bytes"
            ),
        ),
        policy=EnvelopePolicy(
            model=_require_ascii_string(policy["model"], "model"),
            model_revision=_require_model_revision(policy["model_revision"]),
            compute_type=_require_ascii_string(policy["compute_type"], "compute_type"),
            task=_require_ascii_string(policy["task"], "task"),
            inference_concurrency=_require_positive_int(
                policy["inference_concurrency"], "inference_concurrency"
            ),
            chunk_minutes=_require_positive_int(
                policy["chunk_minutes"], "chunk_minutes"
            ),
            decoder_options_sha256=_require_digest(
                policy["decoder_options_sha256"], "decoder_options_sha256"
            ),
        ),
        measurements=EnvelopeMeasurements(
            **{
                name: _require_positive_int(measurements[name], name)
                for name in EnvelopeMeasurements.__dataclass_fields__
            }
        ),
    )
