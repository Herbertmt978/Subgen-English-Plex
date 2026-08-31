from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os

import pytest
import subgen_core.model_envelope_catalog as catalog_module

from subgen_core.model_envelope_catalog import (
    CATALOG_SCHEMA,
    IDENTITY_SCHEMA,
    ArtifactValidationError,
    ArtifactSecurityError,
    CatalogIntegrity,
    EnvelopeMeasurements,
    EnvelopeDisposition,
    EnvelopePolicy,
    EnvelopeResolution,
    ImageIdentity,
    ImageIdentityArtifact,
    ModelEnvelope,
    ModelEnvelopeCatalog,
    RESOLUTION_REASON_CODES,
    RuntimeIdentity,
    build_catalog,
    canonical_payload_bytes,
    decoder_options_sha256,
    find_exact_envelope,
    load_catalog,
    load_identity,
    normalize_model_revision,
    resolve_envelope,
    serialize_catalog,
    serialize_identity,
    write_catalog,
    write_identity,
)


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
HF_REVISION = "hf:" + "0" * 40
OWNER_UID = os.geteuid() if hasattr(os, "geteuid") else 0
POSIX_ARTIFACTS = pytest.mark.skipif(
    os.name == "nt", reason="owner-only filesystem provenance requires POSIX"
)


def test_shared_policy_normalizers_are_canonical_and_strict():
    left = {"beam_size": 5, "vad": True}
    right = {"vad": True, "beam_size": 5}

    assert decoder_options_sha256(left) == decoder_options_sha256(right)
    assert normalize_model_revision("0" * 40) == HF_REVISION
    assert normalize_model_revision(HF_REVISION) == HF_REVISION
    with pytest.raises(ArtifactValidationError, match="model_revision"):
        normalize_model_revision("main")
    with pytest.raises(ArtifactValidationError, match="decoder_options"):
        decoder_options_sha256({"invalid": {1, 2}})


def sample_identity():
    return ImageIdentity(config_digest=SHA_A, layer_diff_ids=(SHA_B, SHA_C))


def sample_runtime():
    return RuntimeIdentity(
        stable_ts_version="2.19.1",
        faster_whisper_version="1.2.0",
        ctranslate2_version="4.6.0",
        cuda_runtime_version="12.8",
        driver_version="570.133.20",
        device_name="NVIDIA GeForce RTX 3090",
        compute_capability="8.6",
        total_vram_bytes=24 * 1024**3,
    )


def sample_policy(model="large-v3"):
    return EnvelopePolicy(
        model=model,
        model_revision=HF_REVISION,
        compute_type="float16",
        task="translate",
        inference_concurrency=1,
        chunk_minutes=20,
        decoder_options_sha256=SHA_C,
    )


def sample_measurements():
    gib = 1024**3
    return EnvelopeMeasurements(
        runs=3,
        host_preload_used_bytes=2 * gib,
        host_peak_used_bytes=6 * gib,
        cgroup_preload_used_bytes=1 * gib,
        cgroup_peak_used_bytes=6 * gib,
        device_preload_used_bytes=6 * gib,
        device_peak_used_bytes=11 * gib,
        host_incremental_peak_bytes=4 * gib,
        cgroup_incremental_peak_bytes=5 * gib,
        device_incremental_peak_bytes=5 * gib,
        host_margin_bytes=512 * 1024**2,
        device_margin_bytes=2 * gib,
    )


def sample_envelope(model="large-v3"):
    return ModelEnvelope(
        image_identity=sample_identity(),
        runtime=sample_runtime(),
        policy=sample_policy(model),
        measurements=sample_measurements(),
    )


class PermissiveEqualDuck:
    def __init__(self, **attributes):
        self.__dict__.update(attributes)

    def __eq__(self, _other):
        return True


class PermissiveString(str):
    def __eq__(self, _other):
        return True


def write_owner_only(path, raw):
    path.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    path.chmod(0o600)
    return path


def parse_catalog_path(path):
    return catalog_module._parse_catalog_bytes(path.read_bytes())


def parse_identity_path(path):
    return catalog_module._parse_identity_bytes(path.read_bytes())


def catalog_document():
    return json.loads(
        serialize_catalog(
            build_catalog(catalog_version=1, entries=(sample_envelope(),))
        )
    )


def seal_catalog_document(value):
    payload = {
        "schema": value["schema"],
        "catalog_version": value["catalog_version"],
        "entries": value["entries"],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    value["integrity"]["canonical_payload_sha256"] = (
        "sha256:" + hashlib.sha256(encoded).hexdigest()
    )
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def test_catalog_and_identity_round_trip_are_canonical_and_immutable():
    identity = ImageIdentityArtifact(
        schema=IDENTITY_SCHEMA,
        image_identity=sample_identity(),
    )
    catalog = build_catalog(catalog_version=1, entries=(sample_envelope(),))

    payload = canonical_payload_bytes(catalog)
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert catalog.schema == CATALOG_SCHEMA
    assert catalog.integrity == CatalogIntegrity("sha256", expected)
    assert serialize_catalog(catalog) == serialize_catalog(catalog)
    assert serialize_identity(identity) == serialize_identity(identity)

    assert catalog_module._parse_catalog_bytes(serialize_catalog(catalog)) == catalog
    assert (
        catalog_module._parse_identity_bytes(serialize_identity(identity)) == identity
    )
    with pytest.raises(FrozenInstanceError):
        catalog.catalog_version = 2
    with pytest.raises(TypeError):
        catalog.entries[0] = sample_envelope("medium")


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_loader_rejects_duplicate_keys_and_nonfinite_numbers(tmp_path, constant):
    raw = serialize_catalog(
        build_catalog(catalog_version=1, entries=(sample_envelope(),))
    )
    duplicate = raw.replace(
        b'"catalog_version":1', b'"catalog_version":1,"catalog_version":1', 1
    )
    duplicate_path = write_owner_only(tmp_path / "duplicate.json", duplicate)
    with pytest.raises(ArtifactValidationError, match="duplicate_key"):
        parse_catalog_path(duplicate_path)

    nonfinite = raw.replace(b'"runs":3', f'"runs":{constant}'.encode("ascii"), 1)
    nonfinite_path = write_owner_only(tmp_path / f"{constant}.json", nonfinite)
    with pytest.raises(ArtifactValidationError, match="nonfinite_number"):
        parse_catalog_path(nonfinite_path)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("top", "unexpected"),
        ("entry", "hostname"),
        ("image_identity", "tag"),
        ("runtime", "device_uuid"),
        ("policy", "environment"),
        ("measurements", "media_path"),
        ("integrity", "credential"),
    ],
)
def test_catalog_rejects_unknown_fields_at_every_schema_level(tmp_path, section, field):
    value = catalog_document()
    entry = value["entries"][0]
    targets = {
        "top": value,
        "entry": entry,
        "image_identity": entry["image_identity"],
        "runtime": entry["runtime"],
        "policy": entry["policy"],
        "measurements": entry["measurements"],
        "integrity": value["integrity"],
    }
    targets[section][field] = "private"
    raw = seal_catalog_document(value)
    path = write_owner_only(tmp_path / f"unknown-{section}.json", raw)

    with pytest.raises(ArtifactValidationError, match="fields"):
        parse_catalog_path(path)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("top", "catalog_version"),
        ("entry", "measurements"),
        ("image_identity", "layer_diff_ids"),
        ("runtime", "driver_version"),
        ("policy", "model_revision"),
        ("measurements", "device_margin_bytes"),
        ("integrity", "algorithm"),
    ],
)
def test_catalog_rejects_missing_fields_at_every_schema_level(tmp_path, section, field):
    value = catalog_document()
    entry = value["entries"][0]
    targets = {
        "top": value,
        "entry": entry,
        "image_identity": entry["image_identity"],
        "runtime": entry["runtime"],
        "policy": entry["policy"],
        "measurements": entry["measurements"],
        "integrity": value["integrity"],
    }
    del targets[section][field]
    path = write_owner_only(
        tmp_path / f"missing-{section}.json", json.dumps(value).encode()
    )

    with pytest.raises(ArtifactValidationError, match="fields"):
        parse_catalog_path(path)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("top", "catalog_version"),
        ("runtime", "total_vram_bytes"),
        ("policy", "inference_concurrency"),
        ("policy", "chunk_minutes"),
        ("measurements", "runs"),
        ("measurements", "host_preload_used_bytes"),
        ("measurements", "host_peak_used_bytes"),
        ("measurements", "cgroup_preload_used_bytes"),
        ("measurements", "cgroup_peak_used_bytes"),
        ("measurements", "device_preload_used_bytes"),
        ("measurements", "device_peak_used_bytes"),
        ("measurements", "host_incremental_peak_bytes"),
        ("measurements", "cgroup_incremental_peak_bytes"),
        ("measurements", "device_incremental_peak_bytes"),
        ("measurements", "host_margin_bytes"),
        ("measurements", "device_margin_bytes"),
    ],
)
def test_catalog_rejects_bool_where_an_integer_is_required(tmp_path, section, field):
    value = catalog_document()
    entry = value["entries"][0]
    targets = {
        "top": value,
        "runtime": entry["runtime"],
        "policy": entry["policy"],
        "measurements": entry["measurements"],
    }
    targets[section][field] = True
    path = write_owner_only(
        tmp_path / f"bool-{section}-{field}.json", seal_catalog_document(value)
    )

    with pytest.raises(ArtifactValidationError, match="positive_integer"):
        parse_catalog_path(path)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda value: value.__setitem__("schema", "catalog/v2"), "schema"),
        (
            lambda value: value["entries"][0]["runtime"].__setitem__(
                "device_name", "GPU-\u00e9"
            ),
            "ascii_string",
        ),
        (
            lambda value: value["entries"][0]["image_identity"].__setitem__(
                "config_digest", "sha256:" + "A" * 64
            ),
            "digest",
        ),
        (
            lambda value: value["entries"][0]["image_identity"].__setitem__(
                "layer_diff_ids", []
            ),
            "layer_diff_ids",
        ),
        (
            lambda value: value["entries"][0]["policy"].__setitem__(
                "decoder_options_sha256", "c" * 64
            ),
            "digest",
        ),
        (
            lambda value: value["entries"][0]["measurements"].__setitem__(
                "host_margin_bytes", 0
            ),
            "positive_integer",
        ),
        (
            lambda value: value["entries"][0]["measurements"].__setitem__("runs", 2),
            "runs",
        ),
    ],
)
def test_catalog_rejects_invalid_values_even_with_resealed_integrity(
    tmp_path, mutate, reason
):
    value = catalog_document()
    mutate(value)
    path = write_owner_only(
        tmp_path / f"invalid-{reason}.json", seal_catalog_document(value)
    )

    with pytest.raises(ArtifactValidationError, match=reason):
        parse_catalog_path(path)


def test_catalog_rejects_integrity_mismatch_and_duplicate_matching_entries(tmp_path):
    bad_integrity = catalog_document()
    bad_integrity["integrity"]["canonical_payload_sha256"] = SHA_B
    bad_integrity_path = write_owner_only(
        tmp_path / "bad-integrity.json", json.dumps(bad_integrity).encode()
    )
    with pytest.raises(ArtifactValidationError, match="integrity_mismatch"):
        parse_catalog_path(bad_integrity_path)

    duplicate = catalog_document()
    duplicate["entries"].append(duplicate["entries"][0])
    duplicate_path = write_owner_only(
        tmp_path / "duplicate-entry.json", seal_catalog_document(duplicate)
    )
    with pytest.raises(ArtifactValidationError, match="duplicate_match"):
        parse_catalog_path(duplicate_path)


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda value: value.__setitem__("extra", "secret"), "fields"),
        (lambda value: value.__delitem__("schema"), "fields"),
        (lambda value: value.__setitem__("schema", "identity/v2"), "schema"),
        (
            lambda value: value["image_identity"].__setitem__(
                "config_digest", "sha256:" + "A" * 64
            ),
            "digest",
        ),
        (
            lambda value: value["image_identity"].__setitem__("layer_diff_ids", []),
            "layer_diff_ids",
        ),
    ],
)
def test_identity_loader_rejects_noncanonical_schema_and_values(
    tmp_path, mutate, reason
):
    value = json.loads(
        serialize_identity(ImageIdentityArtifact(IDENTITY_SCHEMA, sample_identity()))
    )
    mutate(value)
    path = write_owner_only(
        tmp_path / f"identity-{reason}.json", json.dumps(value).encode()
    )

    with pytest.raises(ArtifactValidationError, match=reason):
        parse_identity_path(path)


def test_build_catalog_rejects_invalid_version_and_duplicate_match():
    with pytest.raises(ArtifactValidationError, match="positive_integer"):
        build_catalog(catalog_version=True, entries=())
    with pytest.raises(ArtifactValidationError, match="duplicate_match"):
        build_catalog(catalog_version=1, entries=(sample_envelope(), sample_envelope()))


def test_build_catalog_consumes_at_most_max_plus_one_entries():
    consumed = 0

    def entries():
        nonlocal consumed
        for _ in range(1000):
            consumed += 1
            yield sample_envelope()

    with pytest.raises(ArtifactValidationError, match="entries_limit"):
        build_catalog(catalog_version=1, entries=entries())
    assert consumed == catalog_module.MAX_CATALOG_ENTRIES + 1


@pytest.mark.parametrize(
    "revision",
    [
        "main",
        "latest",
        "release/large-v3",
        "0123456789abcdef",
        "hf:" + "A" * 40,
        "hf:" + "a" * 39,
        "sha256:" + "a" * 64,
        "sha256:" + "a" * 63,
    ],
)
def test_policy_rejects_mutable_or_noncanonical_model_revisions(revision):
    with pytest.raises(ArtifactValidationError, match="model_revision"):
        replace(sample_policy(), model_revision=revision)


def test_resealed_catalog_rejects_mutable_model_revision(tmp_path):
    value = catalog_document()
    value["entries"][0]["policy"]["model_revision"] = "main"
    path = write_owner_only(
        tmp_path / "mutable-revision.json", seal_catalog_document(value)
    )

    with pytest.raises(ArtifactValidationError, match="model_revision"):
        parse_catalog_path(path)


@pytest.mark.parametrize("domain", ["host", "cgroup", "device"])
@pytest.mark.parametrize(
    "violation",
    ["peak_below_preload", "incremental_below_delta", "incremental_above_peak"],
)
def test_resealed_catalog_rejects_inconsistent_measurement_bounds(
    tmp_path, domain, violation
):
    value = catalog_document()
    measurements = value["entries"][0]["measurements"]
    preload = f"{domain}_preload_used_bytes"
    peak = f"{domain}_peak_used_bytes"
    incremental = f"{domain}_incremental_peak_bytes"
    if violation == "peak_below_preload":
        measurements[peak] = measurements[preload] - 1
    elif violation == "incremental_below_delta":
        measurements[incremental] = measurements[peak] - measurements[preload] - 1
    else:
        measurements[incremental] = measurements[peak] + 1
    path = write_owner_only(
        tmp_path / f"bad-{domain}-{violation}.json", seal_catalog_document(value)
    )

    with pytest.raises(ArtifactValidationError, match=f"{domain}_measurements"):
        parse_catalog_path(path)


def test_catalog_constructor_and_match_reject_unverified_integrity():
    valid = build_catalog(catalog_version=1, entries=(sample_envelope(),))
    unrelated_integrity = build_catalog(catalog_version=1, entries=()).integrity
    with pytest.raises(ArtifactValidationError, match="integrity_mismatch"):
        ModelEnvelopeCatalog(
            schema=CATALOG_SCHEMA,
            catalog_version=1,
            entries=valid.entries,
            integrity=unrelated_integrity,
        )

    bypassed = object.__new__(ModelEnvelopeCatalog)
    object.__setattr__(bypassed, "schema", CATALOG_SCHEMA)
    object.__setattr__(bypassed, "catalog_version", 1)
    object.__setattr__(bypassed, "entries", valid.entries)
    object.__setattr__(bypassed, "integrity", unrelated_integrity)
    with pytest.raises(ArtifactValidationError, match="integrity_mismatch"):
        find_exact_envelope(
            bypassed,
            ImageIdentityArtifact(IDENTITY_SCHEMA, sample_identity()),
            sample_runtime(),
            sample_policy(),
        )


def write_valid_artifacts(tmp_path, entries=None):
    identity = ImageIdentityArtifact(IDENTITY_SCHEMA, sample_identity())
    catalog = build_catalog(
        catalog_version=1,
        entries=(sample_envelope(),) if entries is None else entries,
    )
    catalog_path = tmp_path / "catalog.json"
    identity_path = tmp_path / "image-identity.json"
    if os.name == "nt":
        write_owner_only(catalog_path, serialize_catalog(catalog))
        write_owner_only(identity_path, serialize_identity(identity))
    else:
        write_catalog(catalog_path, catalog)
        write_identity(identity_path, identity)
    return catalog, identity, catalog_path, identity_path


def test_exact_match_requires_identity_layer_order_runtime_and_policy():
    envelope = sample_envelope()
    catalog = build_catalog(catalog_version=1, entries=(envelope,))
    identity = ImageIdentityArtifact(IDENTITY_SCHEMA, sample_identity())

    assert (
        find_exact_envelope(catalog, identity, sample_runtime(), sample_policy())
        == envelope
    )
    reordered = replace(
        identity,
        image_identity=ImageIdentity(SHA_A, tuple(reversed((SHA_B, SHA_C)))),
    )
    assert (
        find_exact_envelope(catalog, reordered, sample_runtime(), sample_policy())
        is None
    )
    changed_runtime = replace(sample_runtime(), driver_version="570.133.21")
    assert (
        find_exact_envelope(catalog, identity, changed_runtime, sample_policy()) is None
    )
    changed_policy = replace(sample_policy(), compute_type="int8_float16")
    assert (
        find_exact_envelope(catalog, identity, sample_runtime(), changed_policy) is None
    )


@pytest.mark.parametrize("argument", ["catalog", "identity", "runtime", "policy"])
def test_exact_match_rejects_permissive_equality_ducks(argument):
    catalog = build_catalog(catalog_version=1, entries=(sample_envelope(),))
    values = {
        "catalog": catalog,
        "identity": ImageIdentityArtifact(IDENTITY_SCHEMA, sample_identity()),
        "runtime": sample_runtime(),
        "policy": sample_policy(),
    }
    if argument == "catalog":
        values[argument] = PermissiveEqualDuck(
            schema=catalog.schema,
            catalog_version=catalog.catalog_version,
            entries=catalog.entries,
            integrity=catalog.integrity,
        )
    elif argument == "identity":
        values[argument] = PermissiveEqualDuck(image_identity=sample_identity())
    else:
        values[argument] = PermissiveEqualDuck()

    with pytest.raises(ArtifactValidationError, match=f"{argument}_type"):
        find_exact_envelope(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("driver_version", PermissiveEqualDuck()),
        ("driver_version", PermissiveString("570.133.20")),
        ("total_vram_bytes", True),
    ],
)
def test_exact_match_revalidates_mutated_exact_runtime_primitives(field, value):
    catalog = build_catalog(catalog_version=1, entries=(sample_envelope(),))
    runtime = sample_runtime()
    object.__setattr__(runtime, field, value)
    with pytest.raises(ArtifactValidationError):
        find_exact_envelope(
            catalog,
            ImageIdentityArtifact(IDENTITY_SCHEMA, sample_identity()),
            runtime,
            sample_policy(),
        )


@pytest.mark.parametrize("nested", ["identity", "policy", "measurements", "integrity"])
def test_exact_match_revalidates_mutated_catalog_nested_instances(nested):
    catalog = build_catalog(catalog_version=1, entries=(sample_envelope(),))
    entry = catalog.entries[0]
    if nested == "identity":
        object.__setattr__(entry.image_identity, "config_digest", PermissiveEqualDuck())
    elif nested == "policy":
        object.__setattr__(entry.policy, "compute_type", PermissiveEqualDuck())
    elif nested == "measurements":
        object.__setattr__(entry.measurements, "runs", True)
    else:
        object.__setattr__(catalog.integrity, "algorithm", PermissiveEqualDuck())
    with pytest.raises(ArtifactValidationError):
        find_exact_envelope(
            catalog,
            ImageIdentityArtifact(IDENTITY_SCHEMA, sample_identity()),
            sample_runtime(),
            sample_policy(),
        )


@pytest.mark.parametrize("argument", ["runtime", "policy", "expected_image_identity"])
def test_resolution_rejects_permissive_equality_ducks(tmp_path, argument):
    _, _, catalog_path, identity_path = write_valid_artifacts(tmp_path)
    values = {
        "runtime": sample_runtime(),
        "policy": sample_policy(),
        "canonical_shared_cuda": True,
    }
    values[argument] = PermissiveEqualDuck()

    with pytest.raises(ArtifactValidationError, match=f"{argument}_type"):
        resolve_envelope(catalog_path, identity_path, **values)


@pytest.mark.parametrize("canonical_shared_cuda", [0, 1, None, "false"])
def test_resolution_rejects_ambiguous_canonical_control(
    tmp_path, canonical_shared_cuda
):
    _, _, catalog_path, identity_path = write_valid_artifacts(tmp_path)
    with pytest.raises(ArtifactValidationError, match="canonical_shared_cuda_type"):
        resolve_envelope(
            catalog_path,
            identity_path,
            runtime=sample_runtime(),
            policy=sample_policy(),
            canonical_shared_cuda=canonical_shared_cuda,
        )


@pytest.mark.parametrize("expected_uid", [True, -1, 1.5, "1000"])
@pytest.mark.parametrize(
    "operation", ["load_catalog", "load_identity", "write", "resolve"]
)
def test_public_artifact_operations_reject_ambiguous_expected_uid(
    tmp_path, operation, expected_uid
):
    catalog, identity, catalog_path, identity_path = write_valid_artifacts(tmp_path)
    with pytest.raises(ArtifactValidationError, match="expected_uid_type"):
        if operation == "load_catalog":
            load_catalog(catalog_path, expected_uid=expected_uid)
        elif operation == "load_identity":
            load_identity(identity_path, expected_uid=expected_uid)
        elif operation == "write":
            write_catalog(catalog_path, catalog, expected_uid=expected_uid)
        else:
            resolve_envelope(
                catalog_path,
                identity_path,
                runtime=sample_runtime(),
                policy=sample_policy(),
                canonical_shared_cuda=True,
                expected_uid=expected_uid,
            )


@pytest.mark.parametrize(
    ("expected_image_identity", "expected_uid"),
    [(None, None), (sample_identity(), None), (None, OWNER_UID)],
)
def test_canonical_resolution_requires_both_provenance_inputs(
    tmp_path, expected_image_identity, expected_uid
):
    result = resolve_envelope(
        tmp_path / "catalog.json",
        tmp_path / "identity.json",
        runtime=sample_runtime(),
        policy=sample_policy(),
        canonical_shared_cuda=True,
        expected_image_identity=expected_image_identity,
        expected_uid=expected_uid,
    )
    assert result == EnvelopeResolution(
        None,
        EnvelopeDisposition.FAIL_CLOSED,
        "canonical_provenance_missing",
    )


@pytest.mark.parametrize(
    ("envelope", "disposition", "reason"),
    [
        (None, "fail_closed", "catalog_unsafe"),
        (object(), EnvelopeDisposition.EXACT_MATCH, None),
        (None, EnvelopeDisposition.EXACT_MATCH, None),
        (object(), EnvelopeDisposition.FAIL_CLOSED, "catalog_unsafe"),
        (None, EnvelopeDisposition.PUBLIC_FALLBACK, None),
    ],
)
def test_resolution_state_rejects_invalid_exact_types_and_combinations(
    envelope, disposition, reason
):
    with pytest.raises(ArtifactValidationError, match="resolution"):
        EnvelopeResolution(envelope, disposition, reason)


def test_exact_resolution_revalidates_mutated_envelope():
    envelope = sample_envelope()
    object.__setattr__(envelope.policy, "model_revision", "main")
    with pytest.raises(ArtifactValidationError):
        EnvelopeResolution(envelope, EnvelopeDisposition.EXACT_MATCH, None)


@POSIX_ARTIFACTS
def test_resolution_returns_exact_match_without_invoking_writers(tmp_path, monkeypatch):
    envelope = sample_envelope()
    _, _, catalog_path, identity_path = write_valid_artifacts(tmp_path, (envelope,))

    def writer_must_not_run(*_args, **_kwargs):
        raise AssertionError("ordinary runtime invoked an artifact writer")

    monkeypatch.setattr(catalog_module, "write_catalog", writer_must_not_run)
    monkeypatch.setattr(catalog_module, "write_identity", writer_must_not_run)
    result = resolve_envelope(
        catalog_path,
        identity_path,
        runtime=sample_runtime(),
        policy=sample_policy(),
        canonical_shared_cuda=True,
        expected_image_identity=sample_identity(),
        expected_uid=OWNER_UID,
    )

    assert result == EnvelopeResolution(
        envelope=envelope,
        disposition=EnvelopeDisposition.EXACT_MATCH,
        reason_code=None,
    )
    assert result.matched
    assert not result.use_public_fallback
    assert not result.fail_closed


@pytest.mark.parametrize(
    "parser",
    [
        catalog_module._parse_catalog_bytes,
        catalog_module._parse_identity_bytes,
    ],
    ids=("catalog", "identity"),
)
@pytest.mark.parametrize(
    ("malformed", "error"),
    [
        (b"{broken", "json_syntax"),
        (b"9" * 5000, "json_limits"),
        (
            b"[" * 2000 + b"0" + b"]" * 2000,
            "json_limits|document_object",
        ),
    ],
    ids=("syntax", "oversized-integer", "excessive-nesting"),
)
def test_pure_parsers_reject_platform_independent_malformed_input(
    parser, malformed, error
):
    with pytest.raises(ArtifactValidationError, match=error):
        parser(malformed)


@POSIX_ARTIFACTS
@pytest.mark.parametrize(
    ("canonical_shared_cuda", "expected"),
    [
        (False, EnvelopeDisposition.PUBLIC_FALLBACK),
        (True, EnvelopeDisposition.FAIL_CLOSED),
    ],
)
def test_nonmatching_runtime_has_bounded_public_or_fail_closed_result(
    tmp_path, canonical_shared_cuda, expected
):
    _, _, catalog_path, identity_path = write_valid_artifacts(tmp_path)
    result = resolve_envelope(
        catalog_path,
        identity_path,
        runtime=replace(sample_runtime(), ctranslate2_version="4.7.0"),
        policy=sample_policy(),
        canonical_shared_cuda=canonical_shared_cuda,
        expected_image_identity=sample_identity(),
        expected_uid=OWNER_UID,
    )

    assert result.envelope is None
    assert result.disposition is expected
    assert result.reason_code == "runtime_policy_mismatch"
    assert result.reason_code in RESOLUTION_REASON_CODES
    assert len(result.reason_code) <= 32
    assert result.use_public_fallback is (not canonical_shared_cuda)
    assert result.fail_closed is canonical_shared_cuda


@POSIX_ARTIFACTS
@pytest.mark.parametrize(
    ("missing", "canonical_shared_cuda", "reason"),
    [
        ("catalog", False, "catalog_missing"),
        ("catalog", True, "catalog_missing"),
        ("identity", False, "identity_missing"),
        ("identity", True, "identity_missing"),
    ],
)
def test_missing_artifact_result_is_bounded_and_does_not_expose_path(
    tmp_path, missing, canonical_shared_cuda, reason
):
    _, _, catalog_path, identity_path = write_valid_artifacts(tmp_path)
    if missing == "catalog":
        catalog_path.unlink()
    else:
        identity_path.unlink()

    result = resolve_envelope(
        catalog_path,
        identity_path,
        runtime=sample_runtime(),
        policy=sample_policy(),
        canonical_shared_cuda=canonical_shared_cuda,
        expected_image_identity=sample_identity(),
        expected_uid=OWNER_UID,
    )

    assert result.reason_code == reason
    assert str(tmp_path) not in result.reason_code
    assert result.disposition is (
        EnvelopeDisposition.FAIL_CLOSED
        if canonical_shared_cuda
        else EnvelopeDisposition.PUBLIC_FALLBACK
    )


@POSIX_ARTIFACTS
def test_expected_image_identity_mismatch_fails_before_catalog_match(tmp_path):
    _, _, catalog_path, identity_path = write_valid_artifacts(tmp_path)
    other_image = ImageIdentity("sha256:" + "d" * 64, (SHA_B, SHA_C))

    result = resolve_envelope(
        catalog_path,
        identity_path,
        runtime=sample_runtime(),
        policy=sample_policy(),
        canonical_shared_cuda=True,
        expected_image_identity=other_image,
        expected_uid=OWNER_UID,
    )

    assert result.envelope is None
    assert result.reason_code == "image_identity_mismatch"
    assert result.fail_closed


@POSIX_ARTIFACTS
def test_loaders_reject_non_regular_files(tmp_path):
    with pytest.raises(ArtifactSecurityError, match="not_regular"):
        load_catalog(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows provenance policy")
def test_windows_filesystem_artifacts_are_unsafe_and_resolution_is_bounded(tmp_path):
    catalog = build_catalog(catalog_version=1, entries=(sample_envelope(),))
    identity = ImageIdentityArtifact(IDENTITY_SCHEMA, sample_identity())
    catalog_path = write_owner_only(
        tmp_path / "catalog.json", serialize_catalog(catalog)
    )
    identity_path = write_owner_only(
        tmp_path / "identity.json", serialize_identity(identity)
    )

    with pytest.raises(ArtifactSecurityError, match="owner_only_unverifiable"):
        load_catalog(catalog_path)
    with pytest.raises(ArtifactSecurityError, match="owner_only_unverifiable"):
        write_catalog(catalog_path, catalog)

    public = resolve_envelope(
        catalog_path,
        identity_path,
        runtime=sample_runtime(),
        policy=sample_policy(),
        canonical_shared_cuda=False,
    )
    canonical = resolve_envelope(
        catalog_path,
        identity_path,
        runtime=sample_runtime(),
        policy=sample_policy(),
        canonical_shared_cuda=True,
        expected_image_identity=sample_identity(),
        expected_uid=0,
    )
    assert (public.disposition, public.reason_code) == (
        EnvelopeDisposition.PUBLIC_FALLBACK,
        "catalog_unsafe",
    )
    assert (canonical.disposition, canonical.reason_code) == (
        EnvelopeDisposition.FAIL_CLOSED,
        "catalog_unsafe",
    )


@POSIX_ARTIFACTS
def test_writers_replace_atomically_leave_no_temp_and_use_owner_only_mode(
    tmp_path, monkeypatch
):
    catalog, identity, catalog_path, identity_path = write_valid_artifacts(tmp_path)
    catalog_path.write_text("old catalog", encoding="ascii")
    identity_path.write_text("old identity", encoding="ascii")
    replacements = []
    real_replace = os.replace

    def observe_replace(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        assert isinstance(src_dir_fd, int) and src_dir_fd == dst_dir_fd
        source_path = tmp_path / source
        destination_path = tmp_path / destination
        replacements.append((source_path, destination_path))
        assert source_path.parent == destination_path.parent == tmp_path
        assert source_path.name.endswith(".tmp")
        if os.name != "nt":
            assert source_path.stat().st_mode & 0o777 == 0o600
        return real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "replace", observe_replace)
    write_catalog(catalog_path, catalog)
    write_identity(identity_path, identity)

    assert [destination for _, destination in replacements] == [
        catalog_path,
        identity_path,
    ]
    assert load_catalog(catalog_path) == catalog
    assert load_identity(identity_path) == identity
    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]
    if os.name != "nt":
        assert catalog_path.stat().st_mode & 0o777 == 0o600
        assert identity_path.stat().st_mode & 0o777 == 0o600


@POSIX_ARTIFACTS
@pytest.mark.parametrize(
    ("artifact", "reason"),
    [("catalog", "catalog_invalid"), ("identity", "identity_invalid")],
)
@pytest.mark.parametrize(
    "malformed",
    [
        b"{broken",
        b"9" * 5000,
        b"[" * 2000 + b"0" + b"]" * 2000,
    ],
    ids=("syntax", "oversized-integer", "excessive-nesting"),
)
def test_malformed_parser_input_returns_bounded_failure(
    tmp_path, artifact, reason, malformed
):
    _, _, catalog_path, identity_path = write_valid_artifacts(tmp_path)
    target = catalog_path if artifact == "catalog" else identity_path
    write_owner_only(target, malformed)

    result = resolve_envelope(
        catalog_path,
        identity_path,
        runtime=sample_runtime(),
        policy=sample_policy(),
        canonical_shared_cuda=True,
        expected_image_identity=sample_identity(),
        expected_uid=OWNER_UID,
    )

    assert result.reason_code == reason
    assert result.disposition is EnvelopeDisposition.FAIL_CLOSED


@POSIX_ARTIFACTS
def test_artifact_size_is_bounded_before_json_decode(tmp_path):
    _, _, catalog_path, identity_path = write_valid_artifacts(tmp_path)
    write_owner_only(
        catalog_path,
        b" " * (catalog_module.MAX_ARTIFACT_BYTES + 1),
    )

    result = resolve_envelope(
        catalog_path,
        identity_path,
        runtime=sample_runtime(),
        policy=sample_policy(),
        canonical_shared_cuda=True,
        expected_image_identity=sample_identity(),
        expected_uid=OWNER_UID,
    )

    assert result.reason_code == "catalog_invalid"
    assert result.fail_closed


@pytest.mark.parametrize("limit_kind", ["entries", "layers", "string"])
def test_catalog_rejects_values_above_explicit_schema_limits(tmp_path, limit_kind):
    value = catalog_document()
    entry = value["entries"][0]
    if limit_kind == "entries":
        value["entries"] = [entry] * (catalog_module.MAX_CATALOG_ENTRIES + 1)
    elif limit_kind == "layers":
        entry["image_identity"]["layer_diff_ids"] = [SHA_B] * (
            catalog_module.MAX_LAYER_DIFF_IDS + 1
        )
    else:
        entry["runtime"]["device_name"] = "G" * (catalog_module.MAX_STRING_LENGTH + 1)
    path = write_owner_only(
        tmp_path / f"over-limit-{limit_kind}.json",
        seal_catalog_document(value),
    )

    with pytest.raises(ArtifactValidationError, match="limit"):
        parse_catalog_path(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership/link policy")
def test_posix_loader_requires_single_link_expected_uid_and_0700_parent(tmp_path):
    catalog, _, catalog_path, _ = write_valid_artifacts(tmp_path)
    hardlink = tmp_path / "catalog-hardlink.json"
    os.link(catalog_path, hardlink)
    with pytest.raises(ArtifactSecurityError, match="link_count"):
        load_catalog(catalog_path)
    hardlink.unlink()

    catalog_path.chmod(0o640)
    with pytest.raises(ArtifactSecurityError, match="owner_only_mode"):
        load_catalog(catalog_path)
    catalog_path.chmod(0o600)

    with pytest.raises(ArtifactSecurityError, match="owner_mismatch"):
        load_catalog(catalog_path, expected_uid=os.geteuid() + 1)
    with pytest.raises(ArtifactSecurityError, match="owner_mismatch"):
        write_catalog(
            tmp_path / "new-catalog.json",
            catalog,
            expected_uid=os.geteuid() + 1,
        )

    tmp_path.chmod(0o750)
    with pytest.raises(ArtifactSecurityError, match="parent_owner_only_mode"):
        load_catalog(catalog_path)
    with pytest.raises(ArtifactSecurityError, match="parent_owner_only_mode"):
        write_catalog(tmp_path / "new-catalog.json", catalog)


@POSIX_ARTIFACTS
def test_posix_loader_opens_artifact_nonblocking_nofollow_relative_to_parent(
    tmp_path, monkeypatch
):
    _, _, catalog_path, _ = write_valid_artifacts(tmp_path)
    real_open = os.open
    observed = []

    def observe_open(path, flags, mode=0o777, *, dir_fd=None):
        observed.append((path, flags, dir_fd))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", observe_open)
    load_catalog(catalog_path)
    artifact_calls = [
        call
        for call in observed
        if call[0] == catalog_path.name and call[2] is not None
    ]
    assert len(artifact_calls) == 1
    _, flags, parent_fd = artifact_calls[0]
    assert isinstance(parent_fd, int)
    assert flags & os.O_NONBLOCK
    assert flags & os.O_NOFOLLOW


@POSIX_ARTIFACTS
def test_posix_loader_rejects_regular_file_replacement_race(tmp_path, monkeypatch):
    _, _, catalog_path, _ = write_valid_artifacts(tmp_path)
    replacement = write_owner_only(
        tmp_path / "replacement.json", catalog_path.read_bytes()
    )
    real_open = os.open
    swapped = False

    def race_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and path == catalog_path.name and dir_fd is not None:
            os.replace(replacement, catalog_path)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", race_open)
    with pytest.raises(ArtifactSecurityError, match="identity_changed"):
        load_catalog(catalog_path)
    assert swapped


@POSIX_ARTIFACTS
def test_posix_loader_fifo_replacement_race_cannot_block(tmp_path, monkeypatch):
    _, _, catalog_path, _ = write_valid_artifacts(tmp_path)
    real_open = os.open
    swapped = False

    def race_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and path == catalog_path.name and dir_fd is not None:
            catalog_path.unlink()
            os.mkfifo(catalog_path, 0o600)
            swapped = True
            assert flags & os.O_NONBLOCK
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", race_open)
    with pytest.raises(ArtifactSecurityError, match="not_regular"):
        load_catalog(catalog_path)
    assert swapped


@POSIX_ARTIFACTS
def test_load_and_write_reject_symlink_ancestor_and_final_when_supported(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    catalog = build_catalog(catalog_version=1, entries=(sample_envelope(),))
    real_catalog = real_parent / "catalog.json"
    write_catalog(real_catalog, catalog)
    with pytest.raises(ArtifactSecurityError, match="symlink"):
        load_catalog(linked_parent / "catalog.json")
    with pytest.raises(ArtifactSecurityError, match="symlink"):
        write_catalog(linked_parent / "new-catalog.json", catalog)

    final_link = real_parent / "catalog-link.json"
    final_link.symlink_to(real_catalog)
    with pytest.raises(ArtifactSecurityError, match="symlink"):
        load_catalog(final_link)
    with pytest.raises(ArtifactSecurityError, match="symlink"):
        write_catalog(final_link, catalog)


def test_schema_contains_no_private_or_credential_fields():
    raw = serialize_catalog(
        build_catalog(catalog_version=1, entries=(sample_envelope(),))
    )
    forbidden = (
        b"credential token password hostname device_uuid media_path environment".split()
    )

    assert all(value not in raw.lower() for value in forbidden)
