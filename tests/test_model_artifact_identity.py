from dataclasses import replace
import hashlib

import pytest

from subgen_core.model_envelope_catalog import (
    ArtifactValidationError, ModelArtifactIdentity, same_source_checkpoint,
    verify_model_artifact, ggml_weight_ftype, NativeArtifactIdentity, verify_native_artifact,
    validate_cohort_model_identity,
)


def identity(data=b"weights", **kwargs):
    return ModelArtifactIdentity(model="base.en", backend_format="ggml",
        precision="float16", weights_sha256="sha256:" + hashlib.sha256(data).hexdigest(),
        size_bytes=len(data), **kwargs)


def test_exact_model_bytes_pass_without_claiming_source_provenance(tmp_path):
    path = tmp_path / "weights.bin"
    path.write_bytes(b"weights")
    item = identity()
    verify_model_artifact(path, item)
    assert not same_source_checkpoint(item, item)


def test_cross_backend_checkpoint_match_requires_known_same_source_and_model():
    source = "sha256:" + "a" * 64
    left = identity(source_checkpoint_sha256=source)
    right = replace(left, backend_format="ctranslate2", weights_sha256="sha256:" + "b" * 64)
    assert same_source_checkpoint(left, right)
    assert not same_source_checkpoint(left, replace(right, model="base"))
    assert not same_source_checkpoint(left, replace(right, source_checkpoint_sha256="sha256:" + "c" * 64))
    # This function only compares checkpoints; precision suitability is separate.
    assert same_source_checkpoint(left, replace(right, precision="int8"))


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 32])
def test_cohort_requires_same_checkpoint_and_weight_precision(count):
    item = identity(source_checkpoint_sha256="sha256:" + "a" * 64)
    assert validate_cohort_model_identity((item,) * count) == item


def test_cohort_accepts_known_float16_conversion_across_backends():
    item = identity(source_checkpoint_sha256="sha256:" + "a" * 64)
    converted = replace(item, backend_format="ctranslate2", weights_sha256="sha256:" + "b" * 64)
    assert validate_cohort_model_identity((item, converted)) == item


@pytest.mark.parametrize("changes,reason", [
    ({"source_checkpoint_sha256": None}, "checkpoint_mismatch"),
    ({"model": "base"}, "checkpoint_mismatch"),
    ({"precision": "int8"}, "weight_precision_mismatch"),
    ({"weights_sha256": "sha256:" + "b" * 64}, "same_format_weights_mismatch"),
])
def test_cohort_refuses_silent_model_or_conversion_differences(changes, reason):
    item = identity(source_checkpoint_sha256="sha256:" + "a" * 64)
    with pytest.raises(ArtifactValidationError, match=reason):
        validate_cohort_model_identity((item, replace(item, **changes)))


def test_cohort_does_not_equate_unverified_or_different_backend_quantizers():
    with pytest.raises(ArtifactValidationError, match="checkpoint_unknown"):
        validate_cohort_model_identity((identity(), identity()))
    item = replace(identity(source_checkpoint_sha256="sha256:" + "a" * 64), precision="q8_0")
    with pytest.raises(ArtifactValidationError, match="cross_backend_quantization_unqualified"):
        validate_cohort_model_identity((item, replace(item, backend_format="ctranslate2")))


@pytest.mark.parametrize("precision,code", [
    ("float32", 0), ("float16", 1), ("q4_0", 2), ("q4_1", 3),
    ("q8_0", 7), ("q5_0", 8), ("q5_1", 9),
])
def test_expected_native_weight_format_uses_pinned_ggml_codes(precision, code):
    assert ggml_weight_ftype(replace(identity(), precision=precision)) == code


@pytest.mark.parametrize("changes", [{"precision": "int8"}, {"precision": "q2_new"},
                                      {"backend_format": "ctranslate2"}])
def test_unknown_or_other_backend_precision_is_not_guessed(changes):
    with pytest.raises(ArtifactValidationError, match="unsupported_ggml_weight_format"):
        ggml_weight_ftype(replace(identity(), **changes))


@pytest.mark.parametrize("changes", [
    {"model": "medium2"}, {"model": []}, {"backend_format": "cuda"},
    {"precision": ""}, {"precision": "float16\n"}, {"weights_sha256": "main"},
    {"size_bytes": 0}, {"size_bytes": True}, {"size_bytes": 16 * 1024**3 + 1},
    {"source_checkpoint_sha256": "unknown"},
])
def test_invalid_identity_refused(changes):
    with pytest.raises(ArtifactValidationError):
        replace(identity(), **changes)


def test_wrong_bytes_size_and_missing_file_are_sanitized(tmp_path):
    path = tmp_path / "private-model.bin"
    path.write_bytes(b"changes")
    with pytest.raises(ArtifactValidationError, match="digest_mismatch"):
        verify_model_artifact(path, identity())
    path.write_bytes(b"short")
    with pytest.raises(ArtifactValidationError, match="size_mismatch"):
        verify_model_artifact(path, identity())
    with pytest.raises(ArtifactValidationError) as error:
        verify_model_artifact(tmp_path / "missing-private-path", identity())
    assert "private" not in str(error.value)


def test_directory_is_not_weights(tmp_path):
    with pytest.raises(ArtifactValidationError, match="not_regular"):
        verify_model_artifact(tmp_path, identity())


def test_cancellation_exception_identity_preserved(tmp_path):
    path = tmp_path / "weights.bin"
    path.write_bytes(b"weights")
    cancellation = RuntimeError("owner cancellation")
    def check():
        raise cancellation
    with pytest.raises(RuntimeError) as error:
        verify_model_artifact(path, identity(), check_cancelled=check)
    assert error.value is cancellation


def test_file_growth_during_streaming_is_rejected(tmp_path):
    data = b"x" * (2 * 1024**2)
    path = tmp_path / "weights.bin"
    path.write_bytes(data)
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls == 3:
            with path.open("ab") as stream:
                stream.write(b"changed")
    with pytest.raises(ArtifactValidationError, match="file_changed"):
        verify_model_artifact(path, identity(data), check_cancelled=check)


def test_native_binary_verification_reuses_exact_byte_owner(tmp_path):
    path = tmp_path / "worker.bin"
    path.write_bytes(b"worker")
    item = NativeArtifactIdentity("worker", "sha256:" + hashlib.sha256(b"worker").hexdigest(), 6)
    verify_native_artifact(path, item)
    path.write_bytes(b"WRONG!")
    with pytest.raises(ArtifactValidationError, match="native_digest_mismatch"):
        verify_native_artifact(path, item)


@pytest.mark.parametrize("changes", [{"component": "../worker"}, {"component": ""},
    {"component": True}, {"sha256": "unknown"}, {"size_bytes": True},
    {"size_bytes": 2 * 1024**3 + 1}])
def test_invalid_native_artifact_identity(changes):
    with pytest.raises(ArtifactValidationError):
        replace(NativeArtifactIdentity("worker", "sha256:" + "a" * 64, 1), **changes)
