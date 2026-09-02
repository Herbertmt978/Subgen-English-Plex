from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("unloaded_gpu_envelope.py")
SPEC = importlib.util.spec_from_file_location("unloaded_gpu_envelope", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
envelope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(envelope)


def cycle(index: int, *, sample: int = 0) -> dict[str, object]:
    return {
        "cycle_index": index,
        "container_id_sha256": str(index) * 64,
        "load_generation_before": 0,
        "load_generation_after": 1,
        "inference_completed": True,
        "inference_result_sha256": "e" * 64,
        "unload_generation_before": 0,
        "unload_generation_after": 1,
        "candidate_bytes_samples": [sample] * 10,
    }


def draft() -> dict[str, object]:
    return {
        "schema": envelope.SCHEMA,
        "runtime_commit": "f" * 40,
        "image": {
            "oci_index": "sha256:" + "1" * 64,
            "config_digest": "sha256:" + "a" * 64,
            "layer_diff_ids": ["sha256:" + "b" * 64],
        },
        "gpu": {
            "uuid": "GPU-00000000-0000-4000-8000-000000000000",
            "driver_version": "580.0",
        },
        "backend": {
            "cuda_version": "12.8",
            "ctranslate2_version": "4.6.0",
            "stable_ts_version": "2.19.1",
        },
        "model_policy": {
            "selected_model": "medium",
            "model_revision": "hf:" + "c" * 40,
            "compute_type": "float16",
            "device": "cuda",
            "device_index": 0,
            "task": "translate",
            "language": "en",
            "chunk_seconds": 300,
            "overlap_seconds": 5,
            "fixture_sha256": "3" * 64,
            "priority_policy_sha256": "4" * 64,
        },
        "measurement": {
            "cycles": [cycle(1, sample=1), cycle(2, sample=3), cycle(3, sample=2)]
        },
    }


def built() -> dict[str, object]:
    return envelope.build_envelope(draft(), generator_sha256="2" * 64)


def test_build_derives_fixed_measurement_and_generator_identity() -> None:
    result = built()
    measurement = result["measurement"]
    assert measurement == {
        "cycles": draft()["measurement"]["cycles"],
        "cycle_count": 3,
        "samples_per_cycle": 10,
        "interval_seconds": 1,
        "margin_bytes": 134_217_728,
        "max_observed_candidate_bytes": 3,
        "allowed_unloaded_bytes": 134_217_731,
    }
    assert result["backend"]["generator_sha256"] == "2" * 64
    assert (
        envelope.validate_envelope(result, expected_generator_sha256="2" * 64) is result
    )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda item: item.update(extra=True), "keys"),
        (lambda item: item["image"].update(extra=True), "keys"),
        (lambda item: item["gpu"].update(uuid="GPU-UPPER"), "uuid"),
        (lambda item: item["model_policy"].update(device_index=True), "integer"),
        (lambda item: item["model_policy"].update(selected_model="turbo"), "model"),
        (
            lambda item: item["measurement"]["cycles"][0].update(inference_completed=1),
            "inference",
        ),
        (
            lambda item: item["measurement"]["cycles"][0].update(
                load_generation_before=1
            ),
            "transition",
        ),
        (
            lambda item: item["measurement"]["cycles"][1].update(cycle_index=1),
            "ordering",
        ),
        (
            lambda item: item["measurement"]["cycles"][1].update(
                container_id_sha256="1" * 64
            ),
            "distinct",
        ),
        (
            lambda item: item["measurement"]["cycles"][0].update(
                candidate_bytes_samples=[0] * 9
            ),
            "sample",
        ),
        (
            lambda item: item["measurement"].update(max_observed_candidate_bytes=99),
            "derived",
        ),
        (lambda item: item["measurement"].update(allowed_unloaded_bytes=99), "derived"),
    ],
)
def test_validation_fails_closed_on_schema_type_and_arithmetic_drift(
    mutate, match: str
) -> None:
    item = built()
    mutate(item)
    with pytest.raises(envelope.EnvelopeError, match=match):
        envelope.validate_envelope(item)


def test_build_rejects_integer_overflow() -> None:
    item = draft()
    item["measurement"]["cycles"][0]["candidate_bytes_samples"][0] = envelope.MAX_INT
    with pytest.raises(envelope.EnvelopeError, match="overflow"):
        envelope.build_envelope(item, generator_sha256="2" * 64)


def test_canonical_parser_rejects_duplicates_noncanonical_and_nonfinite() -> None:
    with pytest.raises(envelope.EnvelopeError, match="duplicate"):
        envelope.parse_json(b'{"a":1,"a":2}\n', require_canonical=False)
    with pytest.raises(envelope.EnvelopeError, match="canonical"):
        envelope.parse_json(b'{ "a": 1 }\n', require_canonical=True)
    with pytest.raises(envelope.EnvelopeError, match="finite"):
        envelope.parse_json(b'{"a":NaN}\n', require_canonical=False)
    payload = envelope.canonical_json_line(built())
    assert payload.endswith(b"\n") and not payload.endswith(b"\n\n")
    assert envelope.parse_json(payload, require_canonical=True) == built()


def test_nvidia_parser_attributes_only_exact_candidate_pids_and_gpu() -> None:
    gpu = draft()["gpu"]["uuid"]
    other = "GPU-11111111-1111-4111-8111-111111111111"
    output = f"101, {gpu}, 2\n202, {gpu}, 3\n303, {other}, 99\n"
    assert (
        envelope.parse_nvidia_compute_apps(
            output, candidate_pids={101, 202}, expected_gpu_uuid=gpu
        )
        == 5 * 1024 * 1024
    )
    assert (
        envelope.parse_nvidia_compute_apps(
            f"303, {other}, 99\n", candidate_pids={101}, expected_gpu_uuid=gpu
        )
        == 0
    )


def test_exact_container_and_inference_preimages_are_bound() -> None:
    full_id = "a" * 64
    srt = "1\n00:00:00,000 --> 00:00:01,000\nhello\n".encode()
    assert (
        envelope.container_id_sha256(full_id)
        == hashlib.sha256(full_id.encode("ascii")).hexdigest()
    )
    assert envelope.inference_result_sha256(srt) == hashlib.sha256(srt).hexdigest()
    for malformed in (
        b"\xef\xbb\xbf" + srt,
        srt.replace(b"\n", b"\r\n"),
        srt + b"\n",
        b"\xff\n",
    ):
        with pytest.raises(envelope.EnvelopeError):
            envelope.inference_result_sha256(malformed)


@pytest.mark.parametrize(
    "output",
    [
        "101, GPU-00000000-0000-4000-8000-000000000000, N/A\n",
        "101, GPU-00000000-0000-4000-8000-000000000000, 1, extra\n",
        "101, GPU-00000000-0000-4000-8000-000000000000, 1\n101, GPU-00000000-0000-4000-8000-000000000000, 1\n",
        "101, GPU-11111111-1111-4111-8111-111111111111, 1\n",
        "\n",
    ],
)
def test_nvidia_parser_rejects_ambiguous_rows(output: str) -> None:
    with pytest.raises(envelope.EnvelopeError):
        envelope.parse_nvidia_compute_apps(
            output,
            candidate_pids={101},
            expected_gpu_uuid=draft()["gpu"]["uuid"],
        )


def test_stable_sample_binds_exact_command_and_rejects_pid_churn() -> None:
    calls: list[tuple[str, ...]] = []
    snapshots = iter(({101}, {101}))

    def query(command) -> str:
        calls.append(tuple(command))
        return f"101, {draft()['gpu']['uuid']}, 7\n"

    result = envelope.stable_candidate_sample(
        resolve_candidate_pids=lambda: next(snapshots),
        run_query=query,
        expected_gpu_uuid=draft()["gpu"]["uuid"],
    )
    assert result == 7 * 1024 * 1024
    assert calls == [envelope.NVIDIA_QUERY]

    snapshots = iter(({101}, {102}))
    with pytest.raises(envelope.EnvelopeError, match="changed"):
        envelope.stable_candidate_sample(
            resolve_candidate_pids=lambda: next(snapshots),
            run_query=lambda _command: "",
            expected_gpu_uuid=draft()["gpu"]["uuid"],
        )


def test_publish_is_create_once_and_preserves_canonical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if hasattr(os, "geteuid"):
        tmp_path.chmod(0o700)
    payload = envelope.canonical_json_line(built())
    output = tmp_path / "envelope.json"
    digest = envelope.publish_create_once(output.absolute(), payload)
    assert output.read_bytes() == payload
    assert digest == hashlib.sha256(payload).hexdigest()
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(envelope.EnvelopeError, match="existed"):
        envelope.publish_create_once(output.absolute(), payload)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and symlink semantics")
def test_publish_rejects_nonprivate_and_symlinked_parent(tmp_path: Path) -> None:
    payload = envelope.canonical_json_line(built())
    tmp_path.chmod(0o755)
    with pytest.raises(envelope.EnvelopeError, match="owner"):
        envelope.publish_create_once((tmp_path / "bad.json").absolute(), payload)
    tmp_path.chmod(0o700)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(envelope.EnvelopeError, match="unsafe"):
        envelope.publish_create_once((alias / "bad.json").absolute(), payload)


def test_cli_generates_then_validates_with_self_binding(tmp_path: Path) -> None:
    if hasattr(os, "geteuid"):
        tmp_path.chmod(0o700)
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(draft()), encoding="ascii")
    output = tmp_path / "envelope.json"
    generated = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--draft",
            str(draft_path),
            "--output",
            str(output.absolute()),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert generated.returncode == 0, generated.stderr
    artifact_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    assert f"sha256={artifact_sha}" in generated.stdout
    validated = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--validate",
            str(output),
            "--expected-sha256",
            artifact_sha,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stderr


def test_cli_rejects_generator_mismatch(tmp_path: Path) -> None:
    item = built()
    path = tmp_path / "wrong.json"
    path.write_bytes(envelope.canonical_json_line(item))
    completed = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--validate", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "generator_checksum_binding_changed" in completed.stderr
