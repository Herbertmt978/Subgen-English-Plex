from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from release_tools import source_proof
from release_tools.task12 import (
    ImageIdentity,
    PublicationBlocked,
    canonical_json_bytes,
)


def _git(repo: Path, *arguments: str) -> bytes:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    if os.name == "nt":
        for key in ("COMSPEC", "SYSTEMROOT", "WINDIR"):
            value = os.environ.get(key)
            if value:
                environment[key] = value
    completed = subprocess.run(
        (source_proof._git_executable(), *arguments),
        cwd=repo,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
        timeout=30,
    )
    if completed.returncode != 0:
        pytest.fail(completed.stderr.decode("utf-8", errors="replace"))
    return completed.stdout


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Task 12 test")
    _git(repo, "config", "user.email", "task12@example.invalid")
    return repo


def _write_private(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    if os.name != "nt":
        path.chmod(0o600)


def test_release_blob_reads_disable_and_reject_replace_objects(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    payload = repo / "payload.txt"
    payload.write_text("original\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "--quiet", "-m", "original")
    original = _git(repo, "rev-parse", "HEAD").decode("ascii").strip()

    payload.write_text("replacement\n", encoding="utf-8", newline="\n")
    _git(repo, "commit", "--quiet", "-am", "replacement")
    replacement = _git(repo, "rev-parse", "HEAD").decode("ascii").strip()
    _git(repo, "switch", "--quiet", "--detach", original)
    _git(repo, "replace", original, replacement)

    assert _git(repo, "show", f"{original}:payload.txt") == b"replacement\n"
    assert source_proof._git(repo, "show", f"{original}:payload.txt") == b"original\n"
    with pytest.raises(PublicationBlocked, match="source_git_replace_refs_present"):
        source_proof._reject_git_indirection(repo)


def test_release_source_proof_rejects_grafts_file(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    payload = repo / "payload.txt"
    payload.write_text("original\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "--quiet", "-m", "original")
    git_dir = Path(
        _git(repo, "rev-parse", "--absolute-git-dir")
        .decode("utf-8", errors="strict")
        .strip()
    )
    grafts = git_dir / "info" / "grafts"
    grafts.parent.mkdir(parents=True, exist_ok=True)
    grafts.write_text("malicious graft\n", encoding="ascii", newline="\n")

    with pytest.raises(PublicationBlocked, match="source_git_grafts_present"):
        source_proof._reject_git_indirection(repo)


def test_release_source_proof_rejects_linked_worktree_common_grafts(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    payload = repo / "payload.txt"
    payload.write_text("original\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "--quiet", "-m", "original")
    worktree = tmp_path / "linked-worktree"
    _git(
        repo,
        "worktree",
        "add",
        "--quiet",
        "-b",
        "task12-linked-worktree",
        str(worktree),
    )
    common_dir = Path(
        _git(
            worktree,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
        .decode("utf-8", errors="strict")
        .strip()
    )
    grafts = common_dir / "info" / "grafts"
    grafts.parent.mkdir(parents=True, exist_ok=True)
    grafts.write_text("malicious graft\n", encoding="ascii", newline="\n")

    with pytest.raises(PublicationBlocked, match="source_git_grafts_present"):
        source_proof._reject_git_indirection(worktree)


def test_release_source_proof_rejects_shallow_repository(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    payload = repo / "payload.txt"
    payload.write_text("original\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "--quiet", "-m", "original")
    head = _git(repo, "rev-parse", "HEAD").decode("ascii").strip()
    common_dir = Path(
        _git(
            repo,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
        .decode("utf-8", errors="strict")
        .strip()
    )
    (common_dir / "shallow").write_text(head + "\n", encoding="ascii", newline="\n")

    with pytest.raises(PublicationBlocked, match="source_shallow_repository_forbidden"):
        source_proof._reject_git_indirection(repo)


def test_runtime_to_release_manifest_includes_publication_chain_and_matches_git(
    tmp_path: Path,
) -> None:
    expected_publication_suffix = (
        "M\trelease_tools/adapters.py\n"
        "M\trelease_tools/cli.py\n"
        "M\trelease_tools/source_proof.py\n"
        "M\ttests/test_task12_publisher.py\n"
        "M\ttests/test_task12_source_hardening.py\n"
    ).encode("utf-8")
    assert source_proof._RUNTIME_TO_RELEASE_STATUS.endswith(expected_publication_suffix)

    entries: list[tuple[str, str]] = []
    for line in source_proof._RUNTIME_TO_RELEASE_STATUS.decode("utf-8").splitlines():
        status, path = line.split("\t", 1)
        assert status in {"A", "M"}
        entries.append((status, path))
    assert [path for _status, path in entries] == sorted(
        path for _status, path in entries
    )

    repo = _repository(tmp_path)
    for status, relative_path in entries:
        if status != "M":
            continue
        target = repo / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("runtime\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "runtime")
    runtime = _git(repo, "rev-parse", "HEAD").decode("ascii").strip()

    for _status, relative_path in entries:
        target = repo / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("release\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "release")
    release = _git(repo, "rev-parse", "HEAD").decode("ascii").strip()

    assert (
        source_proof._git(
            repo,
            "diff",
            "--name-status",
            "--no-renames",
            runtime,
            release,
            "--",
        )
        == source_proof._RUNTIME_TO_RELEASE_STATUS
    )


def test_release_verifier_uses_one_captured_input_generation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runtime = "1" * 40
    sampler = "2" * 40
    release = "3" * 40
    image = ImageIdentity(
        oci_index="sha256:" + "a" * 64,
        config_digest="sha256:" + "b" * 64,
        ordered_diff_ids=("sha256:" + "c" * 64,),
        revision_label=runtime,
    )
    daemon_identity = {
        "schema": "subgen.task11b.docker-daemon/v1",
        "engine_id_sha256": "d" * 64,
        "host_boot_id_sha256": "e" * 64,
        "docker_host": "unix:///var/run/docker.sock",
        "os_type": "linux",
    }
    candidate_payload = canonical_json_bytes(
        {
            "schema": "subgen.task11b.candidate-identity/v2",
            "docker_daemon_identity_sha256": hashlib.sha256(
                canonical_json_bytes(daemon_identity)
            ).hexdigest(),
            "candidate_identity": {
                "container_id": "f" * 64,
                "runtime_commit": runtime,
                "oci_index": image.oci_index,
                "config_digest": image.config_digest,
                "layer_diff_ids": list(image.ordered_diff_ids),
            },
        }
    )
    boundary_payload = canonical_json_bytes(
        {
            "schema": "subgen.task11b.execution-boundary/v1",
            "docker_daemon_identity": daemon_identity,
        }
    )
    candidate_path = tmp_path / "candidate.json"
    boundary_path = tmp_path / "boundary.json"
    shared_path = tmp_path / "shared.json"
    _write_private(candidate_path, candidate_payload)
    _write_private(boundary_path, boundary_payload)
    _write_private(shared_path, b"{}\n")
    profiler_attempts: list[dict[str, str]] = []
    profiler_payloads: list[dict[str, bytes]] = []
    for attempt_index in range(2):
        attempt_paths: dict[str, str] = {}
        attempt_payloads: dict[str, bytes] = {}
        for key in source_proof._PROFILER_ATTEMPT_PATH_KEYS:
            path = tmp_path / f"profiler-{attempt_index}-{key}.bin"
            payload = f"attempt={attempt_index};field={key}\n".encode("ascii")
            _write_private(path, payload)
            attempt_paths[key] = str(path)
            attempt_payloads[key] = payload
        profiler_attempts.append(attempt_paths)
        profiler_payloads.append(attempt_payloads)

    release_payloads = {
        source_proof._EVIDENCE_PATH: b"evidence",
        source_proof._OBSERVER_PATH: b"observer",
        source_proof._OBSERVER_TEST_PATH: b"observer-test",
        source_proof._SAMPLER_PATH: b"sampler",
        source_proof._SAMPLER_TEST_PATH: b"sampler-test",
        source_proof._PRODUCER_PATH: b"producer",
    }

    def fake_git(_root: Path, *arguments: str, **_kwargs: object) -> bytes:
        assert arguments[0] == "show"
        return release_payloads[arguments[1].split(":", 1)[1]]

    verifier_inputs = {
        "binding_prefix": source_proof._RELEASE_BINDING_PREFIX,
        **{key: str(shared_path) for key in source_proof._VERIFIER_PATH_KEYS},
        "candidate_identity_record": str(candidate_path),
        "execution_boundary_manifest": str(boundary_path),
        source_proof._PROFILER_ATTEMPTS_KEY: profiler_attempts,
    }

    def fake_run(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        materialized_root = Path(str(kwargs["cwd"]))
        for key, option in source_proof._VERIFIER_PATH_KEYS.items():
            materialized_path = Path(arguments[arguments.index(option) + 1])
            assert materialized_path.parent == materialized_root
            assert materialized_path != Path(verifier_inputs[key])
        assert (
            Path(
                arguments[
                    arguments.index(
                        source_proof._VERIFIER_PATH_KEYS["candidate_identity_record"]
                    )
                    + 1
                ]
            ).read_bytes()
            == candidate_payload
        )
        assert (
            Path(
                arguments[
                    arguments.index(
                        source_proof._VERIFIER_PATH_KEYS["execution_boundary_manifest"]
                    )
                    + 1
                ]
            ).read_bytes()
            == boundary_payload
        )
        profiler_options = {
            option for _key, option, _maximum in source_proof._PROFILER_ATTEMPT_OPTIONS
        }
        forwarded_profiler_inputs = [
            (item, Path(arguments[index + 1]))
            for index, item in enumerate(arguments[:-1])
            if item in profiler_options
        ]
        expected_profiler_inputs = [
            (option, profiler_payloads[attempt_index][key])
            for attempt_index in range(len(profiler_payloads))
            for key, option, _maximum in source_proof._PROFILER_ATTEMPT_OPTIONS
        ]
        assert len(forwarded_profiler_inputs) == len(expected_profiler_inputs)
        for (observed_option, materialized_path), (
            expected_option,
            expected_payload,
        ) in zip(forwarded_profiler_inputs, expected_profiler_inputs, strict=True):
            assert observed_option == expected_option
            assert materialized_path.parent == materialized_root
            assert materialized_path.read_bytes() == expected_payload

        replacement_daemon = {**daemon_identity, "engine_id_sha256": "9" * 64}
        _write_private(
            candidate_path,
            canonical_json_bytes(
                {
                    "schema": "subgen.task11b.candidate-identity/v2",
                    "docker_daemon_identity_sha256": hashlib.sha256(
                        canonical_json_bytes(replacement_daemon)
                    ).hexdigest(),
                    "candidate_identity": {
                        "container_id": "8" * 64,
                        "runtime_commit": runtime,
                        "oci_index": "sha256:" + "7" * 64,
                        "config_digest": image.config_digest,
                        "layer_diff_ids": list(image.ordered_diff_ids),
                    },
                }
            ),
        )
        _write_private(
            boundary_path,
            canonical_json_bytes(
                {
                    "schema": "subgen.task11b.execution-boundary/v1",
                    "docker_daemon_identity": replacement_daemon,
                }
            ),
        )
        return SimpleNamespace(
            returncode=0,
            stdout=b"TASK11B_RELEASE_VERIFY_OK\n",
            stderr=b"",
        )

    monkeypatch.setattr(source_proof, "_git", fake_git)
    monkeypatch.setattr(source_proof.subprocess, "run", fake_run)

    observed = source_proof._run_release_verifier(
        tmp_path,
        runtime=runtime,
        sampler=sampler,
        release=release,
        expected_receipt=b"TASK11B_RELEASE_VERIFY_OK\n",
        image=image,
        verifier_inputs=verifier_inputs,
    )

    assert observed == (image, daemon_identity["engine_id_sha256"])


def _valid_verifier_inputs(tmp_path: Path) -> dict[str, Any]:
    shared_path = tmp_path / "shared.json"
    _write_private(shared_path, b"{}\n")
    attempts: list[dict[str, str]] = []
    for attempt_index in range(2):
        attempt: dict[str, str] = {}
        for key in source_proof._PROFILER_ATTEMPT_PATH_KEYS:
            path = tmp_path / f"attempt-{attempt_index}-{key}.bin"
            _write_private(path, f"{attempt_index}:{key}\n".encode("ascii"))
            attempt[key] = str(path)
        attempts.append(attempt)
    return {
        "binding_prefix": source_proof._RELEASE_BINDING_PREFIX,
        **{key: str(shared_path) for key in source_proof._VERIFIER_PATH_KEYS},
        source_proof._PROFILER_ATTEMPTS_KEY: attempts,
    }


def test_release_verifier_input_schema_preserves_ordered_profiler_triples(
    tmp_path: Path,
) -> None:
    verifier_inputs = _valid_verifier_inputs(tmp_path)

    normalized = source_proof._verifier_inputs(
        {"release_verifier_inputs": verifier_inputs}
    )

    assert (
        normalized[source_proof._PROFILER_ATTEMPTS_KEY]
        == verifier_inputs[source_proof._PROFILER_ATTEMPTS_KEY]
    )


@pytest.mark.parametrize("missing_key", source_proof._PROFILER_ATTEMPT_PATH_KEYS)
def test_release_verifier_input_schema_rejects_incomplete_profiler_triples(
    tmp_path: Path,
    missing_key: str,
) -> None:
    verifier_inputs = _valid_verifier_inputs(tmp_path)
    del verifier_inputs[source_proof._PROFILER_ATTEMPTS_KEY][0][missing_key]

    with pytest.raises(
        PublicationBlocked, match="source_release_verifier_inputs_invalid"
    ):
        source_proof._verifier_inputs({"release_verifier_inputs": verifier_inputs})


def test_release_verifier_input_schema_rejects_extra_profiler_triple_member(
    tmp_path: Path,
) -> None:
    verifier_inputs = _valid_verifier_inputs(tmp_path)
    verifier_inputs[source_proof._PROFILER_ATTEMPTS_KEY][0]["unexpected"] = str(
        tmp_path
    )

    with pytest.raises(
        PublicationBlocked, match="source_release_verifier_inputs_invalid"
    ):
        source_proof._verifier_inputs({"release_verifier_inputs": verifier_inputs})


def test_release_verifier_input_schema_rejects_duplicate_profiler_path(
    tmp_path: Path,
) -> None:
    verifier_inputs = _valid_verifier_inputs(tmp_path)
    attempts = verifier_inputs[source_proof._PROFILER_ATTEMPTS_KEY]
    attempts[1]["evidence"] = attempts[0]["evidence"]

    with pytest.raises(
        PublicationBlocked, match="source_release_verifier_inputs_invalid"
    ):
        source_proof._verifier_inputs({"release_verifier_inputs": verifier_inputs})


def test_release_verifier_input_schema_rejects_legacy_input_without_profiler_triples(
    tmp_path: Path,
) -> None:
    verifier_inputs = _valid_verifier_inputs(tmp_path)
    del verifier_inputs[source_proof._PROFILER_ATTEMPTS_KEY]

    with pytest.raises(
        PublicationBlocked, match="source_release_verifier_inputs_invalid"
    ):
        source_proof._verifier_inputs({"release_verifier_inputs": verifier_inputs})


def test_release_verifier_input_schema_rejects_mismatched_legacy_profiler_lists(
    tmp_path: Path,
) -> None:
    verifier_inputs = _valid_verifier_inputs(tmp_path)
    attempts = verifier_inputs.pop(source_proof._PROFILER_ATTEMPTS_KEY)
    verifier_inputs.update(
        {
            "profiler_evidence": [attempt["evidence"] for attempt in attempts],
            "profiler_evidence_seal": [attempts[0]["evidence_seal"]],
            "profiler_boundary_manifest": [
                attempt["boundary_manifest"] for attempt in attempts
            ],
        }
    )

    with pytest.raises(
        PublicationBlocked, match="source_release_verifier_inputs_invalid"
    ):
        source_proof._verifier_inputs({"release_verifier_inputs": verifier_inputs})
