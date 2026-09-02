from __future__ import annotations

import copy
import io
import base64
import gzip
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest

from release_tools.adapters import (
    AdapterConfig,
    CommandResult,
    HttpResponse,
    ProfilerAttemptInputs,
    ReleaseVerifierInputs,
    SubprocessCommandRunner,
    Task12HttpCommandAdapter,
    UrllibHttpClient,
)
from release_tools import cli
from release_tools import anonymous_smoke
from release_tools import journal as journal_module
from release_tools import source_proof
from release_tools.journal import FileReceiptJournal
from release_tools.task12 import (
    ANONYMOUS_DOCKER_CLIENT_CONTRACT,
    ANONYMOUS_DOCKER_HOST,
    AnnotatedTag,
    CANONICAL_GIT_REMOTE_URL,
    CREDENTIAL_TRANSPORT_CONTRACT,
    ImageIdentity,
    LocalSourceProof,
    LockObservation,
    PublicationBlocked,
    PublicationCheckpoint,
    PublicState,
    RegistryBlob,
    RegistryManifest,
    RegistryProbeObservation,
    RegistryProbeWrite,
    REGISTRY_API_ORIGIN,
    REGISTRY_CLIENT_CONTRACT,
    ReleaseIntent,
    ReleaseView,
    Task12Publisher,
    canonical_json_bytes,
    derive_layer_diff_id,
    sha256_bytes,
)


PRIOR_MAIN = "1" * 40
RUNTIME = "2" * 40
SAMPLER = "3" * 40
RELEASE = "4" * 40
TAG_OBJECT = "5" * 40
LOCK_OBJECT = "9" * 40
PRIOR_LATEST = "sha256:" + "a" * 64
REGISTRY_TOKEN = "registry-secret-value"
ANONYMOUS_DAEMON_ID = "anonymous-daemon-001"
CANDIDATE_DAEMON_ID = "candidate-daemon-001"
ANONYMOUS_ENGINE_ID_SHA256 = sha256_bytes(ANONYMOUS_DAEMON_ID.encode("utf-8"))
CANDIDATE_ENGINE_ID_SHA256 = sha256_bytes(CANDIDATE_DAEMON_ID.encode("utf-8"))


def _intent_with_capability() -> tuple[ReleaseIntent, bytes]:
    notes = b"# Subgen v0.5.0\n\nExact release notes.\n"
    layer_buffer = io.BytesIO()
    with tarfile.open(fileobj=layer_buffer, mode="w") as layer_archive:
        content = b"task12-test-layer"
        member = tarfile.TarInfo("task12-test-layer.txt")
        member.size = len(content)
        layer_archive.addfile(member, io.BytesIO(content))
    uncompressed_layer = layer_buffer.getvalue()
    layer_payload = gzip.compress(uncompressed_layer, mtime=0)
    layer_diff_id = "sha256:" + sha256_bytes(uncompressed_layer)
    config_payload = canonical_json_bytes(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [layer_diff_id]},
            "config": {"Labels": {"org.opencontainers.image.revision": RUNTIME}},
        }
    )
    config_blob = RegistryBlob(
        digest="sha256:" + sha256_bytes(config_payload),
        size=len(config_payload),
        media_type="application/vnd.oci.image.config.v1+json",
        payload=config_payload,
    )
    layer_blob = RegistryBlob(
        digest="sha256:" + sha256_bytes(layer_payload),
        size=len(layer_payload),
        media_type="application/vnd.oci.image.layer.v1.tar+gzip",
        payload=layer_payload,
    )
    platform_payload = canonical_json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": config_blob.descriptor(),
            "layers": [layer_blob.descriptor()],
        }
    )
    platform_manifest = RegistryManifest(
        digest="sha256:" + sha256_bytes(platform_payload),
        size=len(platform_payload),
        media_type="application/vnd.oci.image.manifest.v1+json",
        payload=platform_payload,
    )
    platform_descriptor = platform_manifest.descriptor()
    platform_descriptor["platform"] = {"architecture": "amd64", "os": "linux"}
    manifest = canonical_json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [platform_descriptor],
        }
    )
    provisional = ReleaseIntent(
        repository="Herbertmt978/Subgen-English-Plex",
        image_repository="herbertmt978/subgen-english-plex",
        prior_main_commit=PRIOR_MAIN,
        runtime_commit=RUNTIME,
        sampler_commit=SAMPLER,
        release_commit=RELEASE,
        annotated_tag=AnnotatedTag(
            object_sha=TAG_OBJECT,
            target_commit=RELEASE,
            tag="v0.5.0",
            message="Release v0.5.0\n",
            tagger_name="Release Owner",
            tagger_email="release@example.invalid",
            tagger_date="2026-09-01T12:00:00Z",
        ),
        release_title="Subgen v0.5.0",
        release_notes=notes,
        release_notes_blob="6" * 40,
        task11b_verifier_receipt=b"TASK11B_RELEASE_VERIFY_OK\n",
        task11b_verifier_receipt_sha256=sha256_bytes(b"TASK11B_RELEASE_VERIFY_OK\n"),
        sealed_manifest=manifest,
        image=ImageIdentity(
            oci_index="sha256:" + sha256_bytes(manifest),
            config_digest=config_blob.digest,
            ordered_diff_ids=(layer_diff_id,),
            revision_label=RUNTIME,
        ),
        required_blobs=(config_blob, layer_blob),
        required_manifests=(platform_manifest,),
        prior_latest_digest=PRIOR_LATEST,
    )
    return provisional, b""


def _source_proof(intent: ReleaseIntent) -> LocalSourceProof:
    return LocalSourceProof(
        clean_worktree=True,
        workflows_manual_only=True,
        runtime_commit=intent.runtime_commit,
        sampler_commit=intent.sampler_commit,
        release_commit=intent.release_commit,
        runtime_is_ancestor_of_sampler=True,
        sampler_is_ancestor_of_release=True,
        annotated_tag_object=intent.annotated_tag.object_sha,
        annotated_tag_target=intent.release_commit,
        release_notes_blob=intent.release_notes_blob,
        release_notes=intent.release_notes,
        task11b_verifier_receipt_sha256=intent.task11b_verifier_receipt_sha256,
        candidate_docker_engine_id_sha256=CANDIDATE_ENGINE_ID_SHA256,
        image=intent.image,
        git_remote_url=CANONICAL_GIT_REMOTE_URL,
    )


def _bound_adapter_config() -> AdapterConfig:
    return AdapterConfig()


def _release_verifier_inputs() -> ReleaseVerifierInputs:
    root = Path(__file__).resolve().parents[1]
    shared = Path(__file__).resolve()
    profiler_paths = (
        (
            root / "release_tools" / "cli.py",
            root / "release_tools" / "adapters.py",
            root / "release_tools" / "source_proof.py",
        ),
        (
            root / "release_tools" / "task12.py",
            root / "release_tools" / "journal.py",
            root / "release_tools" / "anonymous_smoke.py",
        ),
    )
    return ReleaseVerifierInputs(
        binding_prefix="Task-11B-Sampler-Binding: ",
        gate_seal=shared,
        phase_a_seal=shared,
        phase_a_output=shared,
        phase_b_seal=shared,
        assertion_observation=shared,
        phase_a_receipt_trace=shared,
        phase_b_receipt_trace=shared,
        candidate_identity_record=shared,
        execution_boundary_manifest=shared,
        priority_policy=shared,
        unloaded_gpu_envelope=shared,
        model_envelope_catalog=shared,
        profiler_attempts=tuple(
            ProfilerAttemptInputs(
                evidence=evidence.resolve(),
                evidence_seal=seal.resolve(),
                boundary_manifest=boundary.resolve(),
            )
            for evidence, seal, boundary in profiler_paths
        ),
    )


class MemorySink:
    def __init__(self) -> None:
        self.receipts: list[PublicationCheckpoint] = []

    def append(self, checkpoint: PublicationCheckpoint) -> None:
        self.receipts.append(copy.deepcopy(checkpoint))

    @property
    def latest(self) -> PublicationCheckpoint:
        return copy.deepcopy(self.receipts[-1])


MUTATIONS = (
    "lock_object_create",
    "lock_ref_create",
    "main_fast_forward",
    "version_tag_object_create",
    "version_tag_ref_create",
    "registry_blob_000_upload_start",
    "registry_blob_000_upload_finish",
    "registry_blob_001_upload_start",
    "registry_blob_001_upload_finish",
    "registry_manifest_000_put",
    "registry_version_probe_seed",
    "registry_version_probe_reject",
    "registry_version_conditional_create",
    "github_release_create",
    "registry_latest_probe_seed",
    "registry_latest_probe_cas",
    "registry_latest_probe_stale",
    "registry_latest_update",
    "lock_ref_remove",
)


class FakeAdapter:
    def __init__(self, intent: ReleaseIntent) -> None:
        self.intent = intent
        self.actions = list(range(1, 206))
        self.state = PublicState(
            main_commit=intent.prior_main_commit,
            version_tag_object=None,
            release=None,
            version_digest=None,
            latest_digest=intent.prior_latest_digest,
            lock=None,
        )
        self.events: list[str] = []
        self.blobs: set[str] = set()
        self.manifests: set[str] = set()
        self.probes: dict[str, tuple[RegistryManifest, str]] = {}
        self.fail_before: str | None = None
        self.fail_after: str | None = None
        self.change_actions_after: str | None = None

    def _mutate(self, operation: str, mutation: Any = None) -> None:
        self.events.append(operation)
        if self.fail_before == operation:
            self.fail_before = None
            raise OSError("simulated request failure before mutation")
        if mutation is not None:
            mutation()
        if self.change_actions_after == operation:
            self.actions.append(999_999)
        if self.fail_after == operation:
            self.fail_after = None
            raise OSError("simulated lost response")

    def _set_state(self, **changes: Any) -> None:
        self.state = replace(self.state, **changes)

    def verify_local_sources(self, intent: ReleaseIntent) -> LocalSourceProof:
        return _source_proof(intent)

    def fetch_all_actions_run_ids(self, intent: ReleaseIntent) -> Sequence[int]:
        self.events.append("actions")
        return tuple(self.actions)

    def read_public_state(
        self, intent: ReleaseIntent, lock_document_sha256: str
    ) -> PublicState:
        self.events.append("read_state")
        return copy.deepcopy(self.state)

    def create_lock_object(self, intent: ReleaseIntent, lock_document: bytes) -> str:
        self._mutate("lock_object_create")
        return LOCK_OBJECT

    def create_lock_ref(self, intent: ReleaseIntent, object_sha: str) -> None:
        self._mutate(
            "lock_ref_create",
            lambda: self._set_state(
                lock=LockObservation(object_sha, self._checkpoint_lock_hash)
            ),
        )

    _checkpoint_lock_hash = ""

    def assert_lock(
        self,
        intent: ReleaseIntent,
        object_sha: str,
        lock_document_sha256: str,
    ) -> None:
        self._checkpoint_lock_hash = lock_document_sha256
        if self.state.lock == LockObservation(object_sha, ""):
            self._set_state(lock=LockObservation(object_sha, lock_document_sha256))
        if self.state.lock != LockObservation(object_sha, lock_document_sha256):
            raise PublicationBlocked("publication_lock_missing_or_replaced")

    def advance_main(self, intent: ReleaseIntent) -> None:
        self._mutate(
            "main_fast_forward",
            lambda: self._set_state(main_commit=intent.release_commit),
        )

    def create_version_tag_object(self, intent: ReleaseIntent) -> str:
        self._mutate("version_tag_object_create")
        return intent.annotated_tag.object_sha

    def create_version_tag_ref(self, intent: ReleaseIntent) -> None:
        self._mutate(
            "version_tag_ref_create",
            lambda: self._set_state(version_tag_object=intent.annotated_tag.object_sha),
        )

    def registry_blob_present(self, intent: ReleaseIntent, blob: RegistryBlob) -> bool:
        return blob.digest in self.blobs

    def start_registry_blob_upload(
        self, intent: ReleaseIntent, blob: RegistryBlob
    ) -> str:
        index = intent.required_blobs.index(blob)
        self._mutate(f"registry_blob_{index:03d}_upload_start")
        return f"https://ghcr.io/v2/{intent.image_repository}/blobs/uploads/{index}"

    def finish_registry_blob_upload(
        self, intent: ReleaseIntent, blob: RegistryBlob, upload_url: str
    ) -> None:
        index = intent.required_blobs.index(blob)
        self._mutate(
            f"registry_blob_{index:03d}_upload_finish",
            lambda: self.blobs.add(blob.digest),
        )

    def registry_manifest_present(
        self, intent: ReleaseIntent, manifest: RegistryManifest
    ) -> bool:
        return manifest.digest in self.manifests

    def put_registry_manifest(
        self, intent: ReleaseIntent, manifest: RegistryManifest
    ) -> None:
        index = intent.required_manifests.index(manifest)
        self._mutate(
            f"registry_manifest_{index:03d}_put",
            lambda: self.manifests.add(manifest.digest),
        )

    def read_registry_probe(
        self, intent: ReleaseIntent, reference: str
    ) -> RegistryProbeObservation:
        current = self.probes.get(reference)
        if current is None:
            return RegistryProbeObservation(None, None, None)
        manifest, etag = current
        return RegistryProbeObservation(manifest.digest, etag, manifest.payload)

    def put_registry_probe(
        self,
        intent: ReleaseIntent,
        operation: str,
        reference: str,
        manifest: RegistryManifest,
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> RegistryProbeWrite:
        current = self.probes.get(reference)
        allowed = (if_none_match and current is None) or (
            if_match is not None and current is not None and current[1] == if_match
        )
        digest = manifest.digest if allowed else None
        mutation = None
        if allowed:

            def store_probe() -> None:
                self.probes[reference] = (manifest, f'"{manifest.digest}"')

            mutation = store_probe
        self._mutate(operation, mutation)
        return RegistryProbeWrite(201 if allowed else 412, digest)

    def conditional_create_version(self, intent: ReleaseIntent) -> None:
        self._mutate(
            "registry_version_conditional_create",
            lambda: self._set_state(version_digest=intent.image.oci_index),
        )

    def anonymous_pull_smoke(
        self,
        intent: ReleaseIntent,
        candidate_docker_engine_id_sha256: str,
    ) -> ImageIdentity:
        assert candidate_docker_engine_id_sha256 == CANDIDATE_ENGINE_ID_SHA256
        self.events.append("anonymous_smoke")
        return intent.image

    def create_release(self, intent: ReleaseIntent) -> None:
        self._mutate(
            "github_release_create",
            lambda: self._set_state(
                release=ReleaseView(
                    tag=intent.version_tag,
                    title=intent.release_title,
                    draft=False,
                    prerelease=False,
                    body=intent.release_notes,
                ),
            ),
        )

    def update_latest(
        self,
        intent: ReleaseIntent,
        expected_prior_digest: str | None,
    ) -> None:
        if self.state.latest_digest != expected_prior_digest:
            raise PublicationBlocked("registry_latest_changed_before_compare_and_set")
        self._mutate(
            "registry_latest_update",
            lambda: self._set_state(latest_digest=intent.image.oci_index),
        )

    def remove_lock_exact(self, intent: ReleaseIntent, object_sha: str) -> None:
        self._mutate("lock_ref_remove", lambda: self._set_state(lock=None))


def _publisher(adapter: FakeAdapter, sink: MemorySink) -> Task12Publisher:
    publisher = Task12Publisher(
        adapter,
        sink,
        token_factory=lambda: "c" * 64,
    )
    original_read = adapter.read_public_state

    def read_with_lock_hash(
        intent: ReleaseIntent, lock_document_sha256: str
    ) -> PublicState:
        adapter._checkpoint_lock_hash = lock_document_sha256
        return original_read(intent, lock_document_sha256)

    adapter.read_public_state = read_with_lock_hash  # type: ignore[method-assign]
    return publisher


def test_complete_publication_is_exact_and_checks_actions_around_every_write() -> None:
    intent, receipt = _intent_with_capability()
    adapter = FakeAdapter(intent)
    sink = MemorySink()
    checkpoint = _publisher(adapter, sink).publish(intent)

    assert checkpoint.phase == "complete"
    assert checkpoint.lock_removed is True
    assert adapter.state.main_commit == intent.release_commit
    assert adapter.state.version_digest == intent.image.oci_index
    assert adapter.state.latest_digest == intent.image.oci_index
    assert adapter.state.release is not None
    assert adapter.state.release.body == intent.release_notes
    assert adapter.state.lock is None
    writes = [event for event in adapter.events if event in MUTATIONS]
    assert writes == list(MUTATIONS)
    for index, event in enumerate(adapter.events):
        if event in MUTATIONS:
            assert "actions" in adapter.events[max(0, index - 3) : index]
            assert adapter.events[index + 1] == "actions"


@pytest.mark.parametrize("operation", MUTATIONS)
def test_lost_response_at_every_mutation_reconciles_without_foreign_overwrite(
    operation: str,
) -> None:
    intent, receipt = _intent_with_capability()
    adapter = FakeAdapter(intent)
    adapter.fail_after = operation
    sink = MemorySink()
    publisher = _publisher(adapter, sink)

    with pytest.raises(PublicationBlocked, match=f"{operation}_response_ambiguous"):
        publisher.publish(intent)

    recovered = publisher.publish(intent, recovery=sink.latest)
    assert recovered.phase == "complete"
    assert adapter.state.lock is None
    assert adapter.state.latest_digest == intent.image.oci_index
    if operation not in {
        "lock_object_create",
        "version_tag_object_create",
        "registry_blob_000_upload_start",
        "registry_blob_001_upload_start",
        "registry_version_probe_seed",
        "registry_version_probe_reject",
        "registry_latest_probe_seed",
        "registry_latest_probe_cas",
        "registry_latest_probe_stale",
    }:
        assert adapter.events.count(operation) == 1


@pytest.mark.parametrize("operation", MUTATIONS)
def test_disk_journal_recovers_lost_response_at_every_mutation(
    operation: str,
    tmp_path: Path,
) -> None:
    intent, receipt = _intent_with_capability()
    adapter = FakeAdapter(intent)
    adapter.fail_after = operation
    journal = FileReceiptJournal(tmp_path / operation)
    publisher = Task12Publisher(
        adapter,
        journal,
        token_factory=lambda: "c" * 64,
    )
    original_read = adapter.read_public_state

    def read_with_lock_hash(
        candidate: ReleaseIntent, lock_document_sha256: str
    ) -> PublicState:
        adapter._checkpoint_lock_hash = lock_document_sha256
        return original_read(candidate, lock_document_sha256)

    adapter.read_public_state = read_with_lock_hash  # type: ignore[method-assign]
    with journal.exclusive():
        with pytest.raises(PublicationBlocked):
            publisher.publish(intent)
        recovery = journal.load_latest()
        assert recovery is not None
        completed = publisher.publish(intent, recovery=recovery)
    assert completed.phase == "complete"
    assert adapter.state.lock is None
    assert adapter.state.latest_digest == intent.image.oci_index


@pytest.mark.parametrize("operation", ["lock_ref_create", "lock_ref_remove"])
def test_lock_ref_pending_receipt_recovers_hard_crash_after_remote_commit(
    operation: str,
) -> None:
    intent, _ = _intent_with_capability()
    adapter = FakeAdapter(intent)
    adapter.fail_after = operation
    sink = MemorySink()
    publisher = _publisher(adapter, sink)

    with pytest.raises(PublicationBlocked, match=f"{operation}_response_ambiguous"):
        publisher.publish(intent)
    pending = next(
        copy.deepcopy(receipt)
        for receipt in sink.receipts
        if receipt.phase == f"{operation}_pending"
    )
    assert adapter.events.count(operation) == 1

    completed = publisher.publish(intent, recovery=pending)
    assert completed.phase == "complete"
    assert adapter.state.lock is None
    assert adapter.state.latest_digest == intent.image.oci_index
    assert adapter.events.count(operation) == 1


def test_ambiguous_latest_that_remains_prior_is_not_retried() -> None:
    intent, receipt = _intent_with_capability()
    adapter = FakeAdapter(intent)
    adapter.fail_before = "registry_latest_update"
    sink = MemorySink()
    publisher = _publisher(adapter, sink)

    with pytest.raises(
        PublicationBlocked, match="registry_latest_update_response_ambiguous"
    ):
        publisher.publish(intent)
    with pytest.raises(PublicationBlocked, match="registry_latest_outcome_unresolved"):
        publisher.publish(intent, recovery=sink.latest)

    assert adapter.events.count("registry_latest_update") == 1
    assert adapter.events.count("lock_ref_remove") == 0
    assert adapter.state.latest_digest == intent.prior_latest_digest
    assert adapter.state.lock is not None
    assert sink.latest.phase == "registry_latest_outcome_unresolved"

    before = list(adapter.events)
    with pytest.raises(PublicationBlocked, match="registry_latest_outcome_unresolved"):
        publisher.publish(intent, recovery=sink.latest)
    assert not [event for event in adapter.events[len(before) :] if event in MUTATIONS]


def test_delayed_latest_commit_remains_owned_until_recovery_completes() -> None:
    intent, _ = _intent_with_capability()

    class DelayedLatestAdapter(FakeAdapter):
        def __init__(self, candidate: ReleaseIntent) -> None:
            super().__init__(candidate)
            self.delayed_commit = False

        def update_latest(
            self,
            candidate: ReleaseIntent,
            expected_prior_digest: str | None,
        ) -> None:
            assert self.state.latest_digest == expected_prior_digest
            self.events.append("registry_latest_update")
            self.delayed_commit = True
            raise OSError("simulated response loss before delayed server commit")

        def complete_delayed_latest(self, candidate: ReleaseIntent) -> None:
            assert self.delayed_commit is True
            self._set_state(latest_digest=candidate.image.oci_index)

    adapter = DelayedLatestAdapter(intent)
    sink = MemorySink()
    publisher = _publisher(adapter, sink)
    with pytest.raises(
        PublicationBlocked,
        match="registry_latest_update_response_ambiguous",
    ):
        publisher.publish(intent)

    with pytest.raises(PublicationBlocked, match="registry_latest_outcome_unresolved"):
        publisher.publish(intent, recovery=sink.latest)
    assert adapter.state.lock is not None
    assert adapter.events.count("registry_latest_update") == 1
    assert adapter.events.count("lock_ref_remove") == 0

    adapter.complete_delayed_latest(intent)
    completed = publisher.publish(intent, recovery=sink.latest)
    assert completed.phase == "complete"
    assert adapter.state.latest_digest == intent.image.oci_index
    assert adapter.state.lock is None
    assert adapter.events.count("registry_latest_update") == 1


def test_unresolved_latest_recovery_revalidates_retained_probe_references() -> None:
    intent, _ = _intent_with_capability()
    adapter = FakeAdapter(intent)
    adapter.fail_before = "registry_latest_update"
    sink = MemorySink()
    publisher = _publisher(adapter, sink)

    with pytest.raises(PublicationBlocked):
        publisher.publish(intent)
    with pytest.raises(PublicationBlocked, match="registry_latest_outcome_unresolved"):
        publisher.publish(intent, recovery=sink.latest)

    checkpoint = sink.latest
    assert checkpoint.latest_cas_probe is not None
    del adapter.probes[checkpoint.latest_cas_probe.reference]
    with pytest.raises(PublicationBlocked, match="registry_probe_postcondition_failed"):
        publisher.publish(intent, recovery=checkpoint)


@pytest.mark.parametrize("operation", MUTATIONS)
def test_actions_run_set_change_after_every_mutation_blocks_next_write(
    operation: str,
) -> None:
    intent, receipt = _intent_with_capability()
    adapter = FakeAdapter(intent)
    adapter.change_actions_after = operation
    sink = MemorySink()

    with pytest.raises(PublicationBlocked, match="hosted_actions_run_set_changed"):
        _publisher(adapter, sink).publish(intent)

    writes = [event for event in adapter.events if event in MUTATIONS]
    assert writes[-1] == operation
    assert len(writes) == MUTATIONS.index(operation) + 1
    assert sink.latest.phase == f"{operation}_ambiguous"


def test_foreign_partial_state_without_owned_recovery_is_blocked() -> None:
    intent, receipt = _intent_with_capability()
    adapter = FakeAdapter(intent)
    adapter._set_state(main_commit=intent.release_commit)
    sink = MemorySink()

    with pytest.raises(PublicationBlocked, match="unowned_partial_publication"):
        _publisher(adapter, sink).publish(intent)
    assert not [event for event in adapter.events if event in MUTATIONS]


def test_replaced_or_missing_held_lock_blocks_recovery() -> None:
    intent, receipt = _intent_with_capability()
    adapter = FakeAdapter(intent)
    adapter.fail_after = "main_fast_forward"
    sink = MemorySink()
    publisher = _publisher(adapter, sink)
    with pytest.raises(PublicationBlocked):
        publisher.publish(intent)
    checkpoint = sink.latest

    adapter._set_state(lock=LockObservation("d" * 40, checkpoint.lock_document_sha256))
    with pytest.raises(
        PublicationBlocked, match="publication_lock_missing_or_replaced"
    ):
        publisher.publish(intent, recovery=checkpoint)

    adapter._set_state(lock=None)
    with pytest.raises(
        PublicationBlocked, match="publication_lock_missing_or_replaced"
    ):
        publisher.publish(intent, recovery=checkpoint)


def test_existing_release_requires_exact_final_newline_and_lf_bytes() -> None:
    intent, receipt = _intent_with_capability()
    for foreign_body in (
        intent.release_notes.rstrip(b"\n"),
        intent.release_notes.replace(b"\n", b"\r\n"),
        b"\xef\xbb\xbf" + intent.release_notes,
    ):
        adapter = FakeAdapter(intent)
        adapter._set_state(
            release=ReleaseView(
                tag=intent.version_tag,
                title=intent.release_title,
                draft=False,
                prerelease=False,
                body=foreign_body,
            )
        )
        with pytest.raises(PublicationBlocked, match="github_release_foreign"):
            _publisher(adapter, MemorySink()).publish(intent)


def test_live_probes_are_retained_only_under_reserved_nonrelease_refs() -> None:
    intent, _ = _intent_with_capability()
    adapter = FakeAdapter(intent)
    checkpoint = _publisher(adapter, MemorySink()).publish(intent)

    assert checkpoint.version_create_probe is not None
    assert checkpoint.version_create_probe.verification_sha256 is not None
    assert checkpoint.latest_cas_probe is not None
    assert checkpoint.latest_cas_probe.verification_sha256 is not None
    assert len(adapter.probes) == 2
    assert {
        checkpoint.version_create_probe.reference,
        checkpoint.latest_cas_probe.reference,
    } == set(adapter.probes)
    assert all(reference.startswith("task12-probe-") for reference in adapter.probes)
    assert intent.version_tag not in adapter.probes
    assert "latest" not in adapter.probes
    assert not any(
        event.startswith("registry_probe_delete") for event in adapter.events
    )


@pytest.mark.parametrize(
    "operation,kind",
    [
        ("registry_version_probe_seed", "create"),
        ("registry_version_probe_reject", "create"),
        ("registry_latest_probe_seed", "cas"),
        ("registry_latest_probe_cas", "cas"),
        ("registry_latest_probe_stale", "cas"),
    ],
)
def test_ambiguous_probe_write_recovers_the_same_retained_reference(
    operation: str,
    kind: str,
) -> None:
    intent, _ = _intent_with_capability()
    adapter = FakeAdapter(intent)
    adapter.fail_after = operation
    sink = MemorySink()
    publisher = _publisher(adapter, sink)

    with pytest.raises(PublicationBlocked, match=f"{operation}_response_ambiguous"):
        publisher.publish(intent)
    ambiguous = sink.latest
    plan = (
        ambiguous.version_create_probe
        if kind == "create"
        else ambiguous.latest_cas_probe
    )
    assert plan is not None
    retained_reference = plan.reference

    recovered = publisher.publish(intent, recovery=ambiguous)
    recovered_plan = (
        recovered.version_create_probe
        if kind == "create"
        else recovered.latest_cas_probe
    )
    assert recovered.phase == "complete"
    assert recovered_plan is not None
    assert recovered_plan.reference == retained_reference
    assert recovered_plan.stage == "verified"
    assert len(adapter.probes) == 2


@pytest.mark.parametrize(
    "operation,attribute,armed_stage",
    [
        (
            "registry_version_probe_reject",
            "version_create_probe",
            "reject_armed",
        ),
        (
            "registry_latest_probe_stale",
            "latest_cas_probe",
            "stale_armed",
        ),
    ],
)
def test_ambiguous_retained_probe_write_persists_winner_etag_before_recovery(
    operation: str,
    attribute: str,
    armed_stage: str,
    tmp_path: Path,
) -> None:
    intent, _ = _intent_with_capability()
    adapter = FakeAdapter(intent)
    adapter.fail_after = operation
    journal = FileReceiptJournal(tmp_path / operation)
    publisher = Task12Publisher(
        adapter,
        journal,
        token_factory=lambda: "c" * 64,
    )
    original_read = adapter.read_public_state

    def read_with_lock_hash(
        candidate: ReleaseIntent, lock_document_sha256: str
    ) -> PublicState:
        adapter._checkpoint_lock_hash = lock_document_sha256
        return original_read(candidate, lock_document_sha256)

    adapter.read_public_state = read_with_lock_hash  # type: ignore[method-assign]
    with journal.exclusive():
        with pytest.raises(PublicationBlocked, match=f"{operation}_response_ambiguous"):
            publisher.publish(intent)
        recovery = journal.load_latest()
        assert recovery is not None
        receipt = getattr(recovery, attribute)
        assert receipt is not None
        assert receipt.stage == armed_stage
        assert receipt.winner_etag is not None
        manifest, retained_etag = adapter.probes[receipt.reference]
        assert retained_etag == receipt.winner_etag
        adapter.probes[receipt.reference] = (manifest, '"replacement-generation"')

        with pytest.raises(
            PublicationBlocked,
            match="registry_probe_winner_etag_changed",
        ):
            publisher.publish(intent, recovery=recovery)
    assert adapter.state.lock is not None
    assert "lock_ref_remove" not in adapter.events


@pytest.mark.parametrize(
    "operation,attribute",
    [
        ("registry_version_conditional_create", "version_create_probe"),
        ("registry_latest_update", "latest_cas_probe"),
    ],
)
@pytest.mark.parametrize("tamper", ["missing", "forged_hash"])
def test_recovery_rejects_missing_or_forged_probe_evidence_and_retains_lock(
    operation: str,
    attribute: str,
    tamper: str,
) -> None:
    intent, _ = _intent_with_capability()
    adapter = FakeAdapter(intent)
    adapter.fail_after = operation
    sink = MemorySink()
    publisher = _publisher(adapter, sink)

    with pytest.raises(PublicationBlocked, match=f"{operation}_response_ambiguous"):
        publisher.publish(intent)
    checkpoint = sink.latest
    if tamper == "missing":
        setattr(checkpoint, attribute, None)
        expected = "probe_evidence_missing"
    else:
        plan = getattr(checkpoint, attribute)
        assert plan is not None
        plan.verification_sha256 = "f" * 64
        expected = "registry_probe_receipt_invalid"

    with pytest.raises(PublicationBlocked, match=expected):
        publisher.publish(intent, recovery=checkpoint)
    assert adapter.state.lock is not None
    assert "lock_ref_remove" not in adapter.events


@pytest.mark.parametrize(
    "operation,attribute",
    [
        ("registry_version_conditional_create", "version_create_probe"),
        ("registry_latest_update", "latest_cas_probe"),
    ],
)
def test_recovery_rejects_missing_retained_probe_reference_and_keeps_lock(
    operation: str,
    attribute: str,
) -> None:
    intent, _ = _intent_with_capability()
    adapter = FakeAdapter(intent)
    adapter.fail_after = operation
    sink = MemorySink()
    publisher = _publisher(adapter, sink)

    with pytest.raises(PublicationBlocked, match=f"{operation}_response_ambiguous"):
        publisher.publish(intent)
    checkpoint = sink.latest
    plan = getattr(checkpoint, attribute)
    assert plan is not None
    del adapter.probes[plan.reference]

    with pytest.raises(PublicationBlocked, match="registry_probe_postcondition_failed"):
        publisher.publish(intent, recovery=checkpoint)
    assert adapter.state.lock is not None
    assert "lock_ref_remove" not in adapter.events


def test_completed_recovery_revalidates_both_retained_probe_references() -> None:
    intent, _ = _intent_with_capability()
    adapter = FakeAdapter(intent)
    sink = MemorySink()
    publisher = _publisher(adapter, sink)
    checkpoint = publisher.publish(intent)
    assert checkpoint.latest_cas_probe is not None
    del adapter.probes[checkpoint.latest_cas_probe.reference]

    with pytest.raises(PublicationBlocked, match="registry_probe_postcondition_failed"):
        publisher.publish(intent, recovery=checkpoint)
    assert adapter.state.lock is None


@pytest.mark.parametrize("attribute", ["version_create_probe", "latest_cas_probe"])
def test_completed_recovery_rejects_replaced_retained_probe_generation(
    attribute: str,
) -> None:
    intent, _ = _intent_with_capability()
    adapter = FakeAdapter(intent)
    checkpoint = _publisher(adapter, MemorySink()).publish(intent)
    receipt = getattr(checkpoint, attribute)
    assert receipt is not None
    manifest, _etag = adapter.probes[receipt.reference]
    adapter.probes[receipt.reference] = (manifest, '"replacement-generation"')

    with pytest.raises(PublicationBlocked, match="registry_probe_winner_etag_changed"):
        _publisher(adapter, MemorySink()).publish(intent, recovery=checkpoint)


def test_every_registry_mutation_occurs_while_the_exact_lock_is_held() -> None:
    intent, _ = _intent_with_capability()

    class LockAssertingAdapter(FakeAdapter):
        def _mutate(self, operation: str, mutation: Any = None) -> None:
            if operation.startswith("registry_"):
                assert self.state.lock is not None
                assert self.state.lock.document_sha256 == self._checkpoint_lock_hash
            super()._mutate(operation, mutation)

    adapter = LockAssertingAdapter(intent)
    checkpoint = _publisher(adapter, MemorySink()).publish(intent)
    assert checkpoint.phase == "complete"


def test_ignored_conditional_create_probe_blocks_before_version_ref() -> None:
    intent, _ = _intent_with_capability()

    class IgnoreCreatePrecondition(FakeAdapter):
        def put_registry_probe(
            self,
            candidate: ReleaseIntent,
            operation: str,
            reference: str,
            manifest: RegistryManifest,
            *,
            if_none_match: bool = False,
            if_match: str | None = None,
        ) -> RegistryProbeWrite:
            if operation == "registry_version_probe_reject":
                self._mutate(
                    operation,
                    lambda: self.probes.__setitem__(
                        reference, (manifest, f'"{manifest.digest}"')
                    ),
                )
                return RegistryProbeWrite(201, manifest.digest)
            return super().put_registry_probe(
                candidate,
                operation,
                reference,
                manifest,
                if_none_match=if_none_match,
                if_match=if_match,
            )

    adapter = IgnoreCreatePrecondition(intent)
    with pytest.raises(PublicationBlocked, match="registry_probe_response_invalid"):
        _publisher(adapter, MemorySink()).publish(intent)
    assert "registry_version_conditional_create" not in adapter.events


def test_ignored_stale_cas_probe_blocks_before_latest() -> None:
    intent, _ = _intent_with_capability()

    class IgnoreStalePrecondition(FakeAdapter):
        def put_registry_probe(
            self,
            candidate: ReleaseIntent,
            operation: str,
            reference: str,
            manifest: RegistryManifest,
            *,
            if_none_match: bool = False,
            if_match: str | None = None,
        ) -> RegistryProbeWrite:
            if operation == "registry_latest_probe_stale":
                self._mutate(
                    operation,
                    lambda: self.probes.__setitem__(
                        reference, (manifest, f'"{manifest.digest}"')
                    ),
                )
                return RegistryProbeWrite(201, manifest.digest)
            return super().put_registry_probe(
                candidate,
                operation,
                reference,
                manifest,
                if_none_match=if_none_match,
                if_match=if_match,
            )

    adapter = IgnoreStalePrecondition(intent)
    with pytest.raises(PublicationBlocked, match="registry_probe_response_invalid"):
        _publisher(adapter, MemorySink()).publish(intent)
    assert "registry_latest_update" not in adapter.events


def test_probe_response_is_audit_hash_only_and_never_contains_credentials() -> None:
    intent, _ = _intent_with_capability()
    checkpoint = _publisher(FakeAdapter(intent), MemorySink()).publish(intent)
    encoded = canonical_json_bytes(checkpoint.snapshot())
    assert b"Authorization" not in encoded
    assert b"registry-secret-value" not in encoded
    assert b"probe_transcript" not in encoded


def test_completed_recovery_is_idempotent_and_performs_no_public_write() -> None:
    intent, receipt = _intent_with_capability()
    adapter = FakeAdapter(intent)
    sink = MemorySink()
    publisher = _publisher(adapter, sink)
    completed = publisher.publish(intent)
    before = list(adapter.events)
    recovered = publisher.publish(intent, recovery=completed)
    assert recovered.phase == "complete"
    assert not [event for event in adapter.events[len(before) :] if event in MUTATIONS]


class ScriptedHttp:
    def __init__(self, responses: Sequence[HttpResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str, Mapping[str, str], bytes | None]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        self.requests.append((method, url, dict(headers), body))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self.responses.pop(0)


class NoCommands:
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        environment: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandResult:
        raise AssertionError(f"unexpected command: {argv}")


def _response(status: int, document: object, **headers: str) -> HttpResponse:
    return HttpResponse(status, headers, canonical_json_bytes(document))


def test_actions_adapter_fetches_complete_more_than_one_hundred_run_set() -> None:
    intent, _ = _intent_with_capability()
    pages = []
    for start, stop in ((1, 101), (101, 201), (201, 206)):
        pages.append(
            _response(
                200,
                {
                    "total_count": 205,
                    "workflow_runs": [
                        {"id": identifier, "status": "completed"}
                        for identifier in range(start, stop)
                    ],
                },
            )
        )
    http = ScriptedHttp(pages)
    adapter = Task12HttpCommandAdapter(NoCommands(), http, _bound_adapter_config())
    assert adapter.fetch_all_actions_run_ids(intent) == list(range(1, 206))
    assert [request[1].rsplit("=", 1)[-1] for request in http.requests] == [
        "1",
        "2",
        "3",
    ]


def test_actions_adapter_blocks_duplicate_or_incomplete_pagination() -> None:
    intent, _ = _intent_with_capability()
    first = _response(
        200,
        {
            "total_count": 101,
            "workflow_runs": [
                {"id": identifier, "status": "completed"}
                for identifier in range(1, 101)
            ],
        },
    )
    duplicate = _response(
        200,
        {"total_count": 101, "workflow_runs": [{"id": 100, "status": "completed"}]},
    )
    adapter = Task12HttpCommandAdapter(
        NoCommands(), ScriptedHttp([first, duplicate]), AdapterConfig()
    )
    with pytest.raises(PublicationBlocked, match="actions_run_id_duplicate"):
        adapter.fetch_all_actions_run_ids(intent)


def test_registry_version_put_is_raw_conditional_create_only() -> None:
    intent, _ = _intent_with_capability()
    http = ScriptedHttp(
        [HttpResponse(201, {"Docker-Content-Digest": intent.image.oci_index}, b"")]
    )
    adapter = Task12HttpCommandAdapter(NoCommands(), http, _bound_adapter_config())
    adapter.conditional_create_version(intent)
    method, url, headers, body = http.requests[0]
    assert method == "PUT"
    assert url.endswith("/manifests/v0.5.0")
    assert headers["If-None-Match"] == "*"
    assert body == intent.sealed_manifest


def test_registry_exact_412_is_reconciled_and_absent_412_blocks() -> None:
    intent, _ = _intent_with_capability()
    exact_http = ScriptedHttp(
        [
            HttpResponse(412, {}, b""),
            HttpResponse(
                200,
                {"Docker-Content-Digest": intent.image.oci_index},
                intent.sealed_manifest,
            ),
        ]
    )
    Task12HttpCommandAdapter(
        NoCommands(), exact_http, _bound_adapter_config()
    ).conditional_create_version(intent)

    absent_http = ScriptedHttp([HttpResponse(412, {}, b""), HttpResponse(404, {}, b"")])
    with pytest.raises(PublicationBlocked, match="registry_conditional_create_failed"):
        Task12HttpCommandAdapter(
            NoCommands(), absent_http, _bound_adapter_config()
        ).conditional_create_version(intent)


def test_release_create_uses_fresh_404_and_verifies_exact_bytes() -> None:
    intent, _ = _intent_with_capability()
    exact_release = {
        "tag_name": intent.version_tag,
        "name": intent.release_title,
        "draft": False,
        "prerelease": False,
        "body": intent.release_notes.decode("utf-8"),
    }
    http = ScriptedHttp(
        [
            HttpResponse(404, {}, b""),
            _response(201, {"id": 1}),
            _response(200, exact_release),
        ]
    )
    adapter = Task12HttpCommandAdapter(NoCommands(), http, AdapterConfig())
    adapter.create_release(intent)
    assert [request[0] for request in http.requests] == ["GET", "POST", "GET"]
    posted = __import__("json").loads(http.requests[1][3])
    assert posted["body"].encode("utf-8") == intent.release_notes
    assert posted["draft"] is False
    assert posted["prerelease"] is False


@pytest.mark.parametrize("status", [200, 401, 403, 302, 409, 429, 500])
def test_release_non_404_precheck_never_authorizes_create(status: int) -> None:
    intent, _ = _intent_with_capability()
    http = ScriptedHttp([HttpResponse(status, {}, b"{}\n")])
    adapter = Task12HttpCommandAdapter(NoCommands(), http, AdapterConfig())
    with pytest.raises(PublicationBlocked):
        adapter.create_release(intent)
    assert [request[0] for request in http.requests] == ["GET"]


def test_release_postcondition_rejects_crlf_and_missing_final_newline() -> None:
    intent, _ = _intent_with_capability()
    for changed in (
        intent.release_notes.decode("utf-8").replace("\n", "\r\n"),
        intent.release_notes.decode("utf-8").rstrip("\n"),
    ):
        http = ScriptedHttp(
            [
                HttpResponse(404, {}, b""),
                _response(201, {"id": 1}),
                _response(
                    200,
                    {
                        "tag_name": intent.version_tag,
                        "name": intent.release_title,
                        "draft": False,
                        "prerelease": False,
                        "body": changed,
                    },
                ),
            ]
        )
        with pytest.raises(
            PublicationBlocked, match="github_release_postcondition_failed"
        ):
            Task12HttpCommandAdapter(
                NoCommands(), http, AdapterConfig()
            ).create_release(intent)


class RecordingCommands:
    def __init__(self, stdout: bytes = b"") -> None:
        self.stdout = stdout
        self.calls: list[
            tuple[tuple[str, ...], bytes | None, Mapping[str, str] | None, Path | None]
        ] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        environment: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandResult:
        self.calls.append((tuple(argv), stdin, environment, cwd))
        return CommandResult(0, self.stdout, b"")


def test_git_main_is_non_force_and_lock_removal_uses_exact_lease() -> None:
    intent, _ = _intent_with_capability()
    commands = RecordingCommands()
    git_secret = "Authorization: Bearer test-only-secret"
    http = ScriptedHttp([_response(200, {"object": {"sha": intent.release_commit}})])
    adapter = Task12HttpCommandAdapter(
        commands,
        http,
        AdapterConfig(
            git_environment={
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": git_secret,
            }
        ),
    )
    adapter.advance_main(intent)
    adapter.remove_lock_exact(intent, LOCK_OBJECT)
    method, url, _headers, body = http.requests[0]
    assert method == "PATCH"
    assert url.endswith("/git/refs/heads/main")
    assert __import__("json").loads(body or b"") == {
        "sha": intent.release_commit,
        "force": False,
    }
    assert commands.calls[0][0][1:] == ("init", "--bare", "--quiet")
    assert commands.calls[1][0][1:] == (
        "push",
        "--porcelain",
        f"--force-with-lease={intent.lock_ref}:{LOCK_OBJECT}",
        CANONICAL_GIT_REMOTE_URL,
        f":{intent.lock_ref}",
    )
    isolated_cwds = [call[3] for call in commands.calls]
    assert isolated_cwds[0] == isolated_cwds[1]
    assert isolated_cwds[0] is not None
    assert isolated_cwds[0] != Path.cwd().resolve()
    assert not isolated_cwds[0].exists()
    init_environment = commands.calls[0][2]
    push_environment = commands.calls[1][2]
    assert init_environment is not None
    assert push_environment is not None
    assert git_secret not in init_environment.values()
    assert git_secret in push_environment.values()


def test_anonymous_smoke_binds_digest_config_diff_order_and_revision() -> None:
    intent, _ = _intent_with_capability()
    reference = f"{intent.image_repository}@{intent.image.oci_index}"
    smoke = canonical_json_bytes(
        {
            "schema": "subgen.task12.anonymous-smoke/v1",
            "reference": reference,
            "anonymous": True,
            "distinct_engine": True,
            "empty_before": True,
            "empty_after": True,
            "http_success": True,
            "docker_host": ANONYMOUS_DOCKER_HOST,
            "docker_client_contract": ANONYMOUS_DOCKER_CLIENT_CONTRACT,
            "anonymous_engine_id_sha256": ANONYMOUS_ENGINE_ID_SHA256,
            "candidate_engine_id_sha256": CANDIDATE_ENGINE_ID_SHA256,
            "image": asdict_image(intent.image),
        }
    )
    commands = RecordingCommands(smoke)
    adapter = Task12HttpCommandAdapter(
        commands,
        ScriptedHttp([]),
        _bound_adapter_config(),
    )
    adapter._release_blob = lambda *_args: b"release tool source\n"  # type: ignore[method-assign]
    assert (
        adapter.anonymous_pull_smoke(intent, CANDIDATE_ENGINE_ID_SHA256) == intent.image
    )
    argv, stdin, environment, cwd = commands.calls[0]
    assert argv[0] == sys.executable
    assert argv[1] == "-I"
    assert argv[2].endswith("anonymous_smoke.py")
    assert reference.encode("ascii") in (stdin or b"")
    assert environment == {
        "PATH": os.defpath,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    assert cwd is not None and cwd != Path.cwd().resolve()
    assert not cwd.exists()


def asdict_image(image: ImageIdentity) -> dict[str, object]:
    return {
        "oci_index": image.oci_index,
        "config_digest": image.config_digest,
        "ordered_diff_ids": list(image.ordered_diff_ids),
        "revision_label": image.revision_label,
    }


def _intent_document(intent: ReleaseIntent) -> dict[str, object]:
    def encoded(payload: bytes) -> str:
        return base64.b64encode(payload).decode("ascii")

    return {
        "schema": "subgen.task12.publication-intent/v2",
        "repository": intent.repository,
        "image_repository": intent.image_repository,
        "prior_main_commit": intent.prior_main_commit,
        "runtime_commit": intent.runtime_commit,
        "sampler_commit": intent.sampler_commit,
        "release_commit": intent.release_commit,
        "annotated_tag": {
            "object_sha": intent.annotated_tag.object_sha,
            "target_commit": intent.annotated_tag.target_commit,
            "tag": intent.annotated_tag.tag,
            "message": intent.annotated_tag.message,
            "tagger_name": intent.annotated_tag.tagger_name,
            "tagger_email": intent.annotated_tag.tagger_email,
            "tagger_date": intent.annotated_tag.tagger_date,
        },
        "release_title": intent.release_title,
        "release_notes_base64": encoded(intent.release_notes),
        "release_notes_blob": intent.release_notes_blob,
        "task11b_verifier_receipt_base64": encoded(intent.task11b_verifier_receipt),
        "task11b_verifier_receipt_sha256": intent.task11b_verifier_receipt_sha256,
        "sealed_manifest_base64": encoded(intent.sealed_manifest),
        "image": asdict_image(intent.image),
        "required_blobs": [
            {
                "digest": blob.digest,
                "size": blob.size,
                "media_type": blob.media_type,
                "payload_base64": encoded(blob.payload),
            }
            for blob in intent.required_blobs
        ],
        "required_manifests": [
            {
                "digest": manifest.digest,
                "size": manifest.size,
                "media_type": manifest.media_type,
                "payload_base64": encoded(manifest.payload),
            }
            for manifest in intent.required_manifests
        ],
        "prior_latest_digest": intent.prior_latest_digest,
        "main_ref": intent.main_ref,
        "version_tag": intent.version_tag,
        "lock_ref": intent.lock_ref,
    }


def _config_document() -> dict[str, object]:
    return {
        "schema": "subgen.task12.publisher-config/v3",
        "repository_root": str(Path.cwd().resolve()),
        "lock_tagger": {
            "name": "Release Owner",
            "email": "release@example.invalid",
            "date": "2026-09-01T12:00:00Z",
        },
        "release_verifier_inputs": _release_verifier_inputs().as_document(),
    }


def test_cli_decodes_exact_canonical_intent_and_rejects_byte_drift() -> None:
    intent, _ = _intent_with_capability()
    raw = canonical_json_bytes(_intent_document(intent))
    assert cli.decode_intent(raw) == intent
    with pytest.raises(PublicationBlocked):
        cli.decode_intent(raw.replace(b"\n", b"\r\n"))


def test_cli_decodes_typed_ordered_profiler_attempts() -> None:
    expected = _release_verifier_inputs()

    decoded = cli.decode_config(
        canonical_json_bytes(_config_document()),
        {
            "SUBGEN_TASK12_GITHUB_TOKEN": "github-secret",
            "SUBGEN_TASK12_REGISTRY_TOKEN": "registry-secret",
        },
    )

    assert decoded.release_verifier_inputs == expected
    assert decoded.release_verifier_inputs is not None
    assert decoded.release_verifier_inputs.as_document() == expected.as_document()


def test_cli_rejects_legacy_publisher_config_schema() -> None:
    config = _config_document()
    config["schema"] = "subgen.task12.publisher-config/v2"

    with pytest.raises(PublicationBlocked, match="publisher_config_schema"):
        cli.decode_config(
            canonical_json_bytes(config),
            {
                "SUBGEN_TASK12_GITHUB_TOKEN": "github-secret",
                "SUBGEN_TASK12_REGISTRY_TOKEN": "registry-secret",
            },
        )


@pytest.mark.parametrize("member", ["evidence", "evidence_seal", "boundary_manifest"])
def test_cli_rejects_incomplete_profiler_attempt(member: str) -> None:
    config = _config_document()
    verifier_inputs = config["release_verifier_inputs"]
    assert isinstance(verifier_inputs, dict)
    attempts = verifier_inputs["profiler_attempts"]
    assert isinstance(attempts, list)
    del attempts[0][member]

    with pytest.raises(PublicationBlocked, match="release_verifier_inputs_invalid"):
        cli.decode_config(
            canonical_json_bytes(config),
            {
                "SUBGEN_TASK12_GITHUB_TOKEN": "github-secret",
                "SUBGEN_TASK12_REGISTRY_TOKEN": "registry-secret",
            },
        )


def test_cli_rejects_mismatched_legacy_profiler_lists() -> None:
    config = _config_document()
    verifier_inputs = config["release_verifier_inputs"]
    assert isinstance(verifier_inputs, dict)
    attempts = verifier_inputs.pop("profiler_attempts")
    assert isinstance(attempts, list)
    verifier_inputs.update(
        {
            "profiler_evidence": [attempt["evidence"] for attempt in attempts],
            "profiler_evidence_seal": [attempts[0]["evidence_seal"]],
            "profiler_boundary_manifest": [
                attempt["boundary_manifest"] for attempt in attempts
            ],
        }
    )

    with pytest.raises(PublicationBlocked, match="release_verifier_inputs_invalid"):
        cli.decode_config(
            canonical_json_bytes(config),
            {
                "SUBGEN_TASK12_GITHUB_TOKEN": "github-secret",
                "SUBGEN_TASK12_REGISTRY_TOKEN": "registry-secret",
            },
        )


def test_cli_rejects_extra_or_duplicate_profiler_input() -> None:
    for mutation in ("extra", "duplicate"):
        config = _config_document()
        verifier_inputs = config["release_verifier_inputs"]
        assert isinstance(verifier_inputs, dict)
        attempts = verifier_inputs["profiler_attempts"]
        assert isinstance(attempts, list)
        if mutation == "extra":
            attempts[0]["unexpected"] = attempts[0]["evidence"]
        else:
            attempts[1]["evidence"] = attempts[0]["evidence"]

        with pytest.raises(PublicationBlocked, match="release_verifier_inputs_invalid"):
            cli.decode_config(
                canonical_json_bytes(config),
                {
                    "SUBGEN_TASK12_GITHUB_TOKEN": "github-secret",
                    "SUBGEN_TASK12_REGISTRY_TOKEN": "registry-secret",
                },
            )


def test_cli_validate_only_issues_no_command_or_request(monkeypatch: Any) -> None:
    intent, _ = _intent_with_capability()
    files = {
        "intent.json": canonical_json_bytes(_intent_document(intent)),
        "config.json": canonical_json_bytes(_config_document()),
    }
    monkeypatch.setattr(
        cli,
        "_read_owner_only",
        lambda path, _label: files[path.name],
    )
    output = io.StringIO()
    errors = io.StringIO()
    tokens = {
        "SUBGEN_TASK12_GITHUB_TOKEN": "github-secret-value",
        "SUBGEN_TASK12_REGISTRY_TOKEN": "registry-secret-value",
    }
    result = cli.main(
        [
            "publish",
            "--intent",
            "intent.json",
            "--config",
            "config.json",
            "--state-dir",
            "state",
            "--validate-only",
        ],
        environment=tokens,
        stdout=output,
        stderr=errors,
    )
    assert result == 0
    assert "no command or request" in output.getvalue()
    assert errors.getvalue() == ""
    assert not any(secret in output.getvalue() for secret in tokens.values())


def test_cli_missing_credential_fails_with_safe_code_only(monkeypatch: Any) -> None:
    intent, _ = _intent_with_capability()
    files = {
        "intent.json": canonical_json_bytes(_intent_document(intent)),
        "config.json": canonical_json_bytes(_config_document()),
    }
    monkeypatch.setattr(
        cli,
        "_read_owner_only",
        lambda path, _label: files[path.name],
    )
    errors = io.StringIO()
    result = cli.main(
        [
            "publish",
            "--intent",
            "intent.json",
            "--config",
            "config.json",
            "--state-dir",
            "state",
            "--validate-only",
        ],
        environment={
            "SUBGEN_TASK12_GITHUB_TOKEN": "secret-one",
        },
        stderr=errors,
    )
    assert result == 2
    assert errors.getvalue() == "Task 12 blocked: registry_token_missing\n"
    assert "secret" not in errors.getvalue()


def test_cli_requires_explicit_publish_verb() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


@pytest.mark.parametrize("status", [200, 401, 403, 302, 409, 429, 500])
def test_only_registry_201_or_exact_412_can_set_immutable_version(status: int) -> None:
    intent, _ = _intent_with_capability()
    response = HttpResponse(status, {}, b"")
    adapter = Task12HttpCommandAdapter(
        NoCommands(), ScriptedHttp([response]), _bound_adapter_config()
    )
    with pytest.raises(PublicationBlocked):
        adapter.conditional_create_version(intent)


def test_latest_foreign_digest_arriving_after_state_read_is_never_overwritten() -> None:
    intent, receipt = _intent_with_capability()
    foreign = "sha256:" + "f" * 64

    class ForeignLatestRace(FakeAdapter):
        injected = False

        def assert_lock(
            self,
            candidate: ReleaseIntent,
            object_sha: str,
            lock_document_sha256: str,
        ) -> None:
            super().assert_lock(candidate, object_sha, lock_document_sha256)
            if (
                not self.injected
                and self.state.release is not None
                and self.state.latest_digest == candidate.prior_latest_digest
            ):
                self.injected = True
                self._set_state(latest_digest=foreign)

    adapter = ForeignLatestRace(intent)
    with pytest.raises(PublicationBlocked, match="registry_latest_foreign"):
        _publisher(adapter, MemorySink()).publish(intent)
    assert adapter.injected is True
    assert adapter.state.latest_digest == foreign
    assert "registry_latest_update" not in adapter.events


def test_latest_update_uses_the_fresh_strong_etag_as_compare_and_set() -> None:
    intent, _ = _intent_with_capability()
    etag = '"prior-etag"'
    http = ScriptedHttp(
        [
            HttpResponse(
                200,
                {"Docker-Content-Digest": PRIOR_LATEST, "ETag": etag},
                b"prior",
            ),
            HttpResponse(
                201,
                {"Docker-Content-Digest": intent.image.oci_index},
                b"",
            ),
        ]
    )
    adapter = Task12HttpCommandAdapter(NoCommands(), http, _bound_adapter_config())
    adapter.update_latest(intent, PRIOR_LATEST)
    assert http.requests[1][2]["If-Match"] == etag
    assert "If-None-Match" not in http.requests[1][2]


def test_latest_rejects_composite_etag_before_any_put() -> None:
    intent, _ = _intent_with_capability()
    http = ScriptedHttp(
        [
            HttpResponse(
                200,
                {
                    "Docker-Content-Digest": PRIOR_LATEST,
                    "ETag": '"prior", "foreign"',
                },
                b"prior",
            )
        ]
    )
    adapter = Task12HttpCommandAdapter(NoCommands(), http, _bound_adapter_config())
    with pytest.raises(PublicationBlocked, match="registry_etag_invalid"):
        adapter.update_latest(intent, PRIOR_LATEST)
    assert [request[0] for request in http.requests] == ["GET"]


def test_latest_rejects_duplicate_etag_fields_before_any_put() -> None:
    intent, _ = _intent_with_capability()
    http = ScriptedHttp(
        [
            HttpResponse(
                200,
                (
                    ("Docker-Content-Digest", PRIOR_LATEST),
                    ("ETag", '"prior"'),
                    ("eTaG", '"foreign"'),
                ),
                b"prior",
            )
        ]
    )
    adapter = Task12HttpCommandAdapter(NoCommands(), http, _bound_adapter_config())
    with pytest.raises(PublicationBlocked, match="http_singleton_header_duplicate"):
        adapter.update_latest(intent, PRIOR_LATEST)
    assert [request[0] for request in http.requests] == ["GET"]


def test_registry_put_rejects_duplicate_physical_etag_fields() -> None:
    intent, _ = _intent_with_capability()
    manifest = RegistryManifest(
        digest=intent.image.oci_index,
        size=len(intent.sealed_manifest),
        media_type="application/vnd.oci.image.index.v1+json",
        payload=intent.sealed_manifest,
    )
    http = ScriptedHttp(
        [
            HttpResponse(
                201,
                (
                    ("Docker-Content-Digest", manifest.digest),
                    ("ETag", '"first"'),
                    ("etag", '"second"'),
                ),
                b"",
            )
        ]
    )
    adapter = Task12HttpCommandAdapter(NoCommands(), http, _bound_adapter_config())
    with pytest.raises(PublicationBlocked, match="http_singleton_header_duplicate"):
        adapter.put_registry_probe(
            intent,
            "registry_version_probe_seed",
            "task12-probe-duplicate-etag",
            manifest,
            if_none_match=True,
        )


def test_urllib_transport_preserves_duplicate_physical_headers() -> None:
    class Headers:
        @staticmethod
        def raw_items() -> list[tuple[str, str]]:
            return [("ETag", '"first"'), ("etag", '"second"')]

    class Response:
        status = 200
        headers = Headers()

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def geturl() -> str:
            return "https://example.invalid/probe"

        @staticmethod
        def read() -> bytes:
            return b"ok"

    class Opener:
        @staticmethod
        def open(_request: object, timeout: int) -> Response:
            assert timeout == 60
            return Response()

    client = UrllibHttpClient()
    client._opener = Opener()  # type: ignore[assignment]
    response = client.request(
        "GET",
        "https://example.invalid/probe",
        headers={},
    )
    assert response.headers == (("ETag", '"first"'), ("etag", '"second"'))


def test_latest_compare_and_set_precondition_never_overwrites_foreign_state() -> None:
    intent, _ = _intent_with_capability()
    foreign = "sha256:" + "f" * 64
    http = ScriptedHttp(
        [
            HttpResponse(
                200,
                {"Docker-Content-Digest": PRIOR_LATEST, "ETag": '"prior"'},
                b"prior",
            ),
            HttpResponse(412, {}, b""),
            HttpResponse(
                200,
                {"Docker-Content-Digest": foreign, "ETag": '"foreign"'},
                b"foreign",
            ),
        ]
    )
    adapter = Task12HttpCommandAdapter(NoCommands(), http, _bound_adapter_config())
    with pytest.raises(
        PublicationBlocked, match="registry_latest_compare_and_set_failed"
    ):
        adapter.update_latest(intent, PRIOR_LATEST)
    assert http.requests[1][2]["If-Match"] == '"prior"'


def test_publisher_config_rejects_unpinned_registry_origin_override() -> None:
    config = _config_document()
    config["registry_api"] = "http://foreign-registry.invalid"
    with pytest.raises(PublicationBlocked, match="publisher_config_schema"):
        cli.decode_config(
            canonical_json_bytes(config),
            {
                "SUBGEN_TASK12_GITHUB_TOKEN": "github-secret",
                "SUBGEN_TASK12_REGISTRY_TOKEN": "registry-secret",
            },
        )


def test_main_advance_uses_github_non_force_api_and_never_local_git() -> None:
    intent, _ = _intent_with_capability()
    commands = RecordingCommands(b"unexpected")
    http = ScriptedHttp([_response(200, {"object": {"sha": intent.release_commit}})])
    adapter = Task12HttpCommandAdapter(commands, http, AdapterConfig())
    adapter.advance_main(intent)
    assert commands.calls == []
    assert __import__("json").loads(http.requests[0][3] or b"") == {
        "sha": intent.release_commit,
        "force": False,
    }


def test_oci_config_bytes_bind_diff_order_and_revision() -> None:
    intent, _ = _intent_with_capability()
    changed_identity = replace(
        intent.image,
        ordered_diff_ids=("sha256:" + "d" * 64,),
    )
    with pytest.raises(PublicationBlocked, match="image_config"):
        replace(intent, image=changed_identity).validate()


def test_oci_selected_manifest_platform_must_match_config() -> None:
    intent, _ = _intent_with_capability()
    document = __import__("json").loads(intent.sealed_manifest)
    document["manifests"][0]["platform"] = {
        "architecture": "arm64",
        "os": "windows",
    }
    changed_manifest = canonical_json_bytes(document)
    changed_identity = replace(
        intent.image,
        oci_index="sha256:" + sha256_bytes(changed_manifest),
    )
    with pytest.raises(PublicationBlocked, match="image_config_identity_mismatch"):
        replace(
            intent,
            sealed_manifest=changed_manifest,
            image=changed_identity,
        ).validate()


def test_oci_diff_id_is_derived_from_a_real_compressed_tar_layer() -> None:
    with pytest.raises(PublicationBlocked, match="registry_layer_compression_invalid"):
        derive_layer_diff_id(
            b"not-a-gzip-layer",
            "application/vnd.oci.image.layer.v1.tar+gzip",
        )


def test_oci_index_rejects_duplicate_child_descriptors() -> None:
    intent, _ = _intent_with_capability()
    document = __import__("json").loads(intent.sealed_manifest)
    document["manifests"].append(copy.deepcopy(document["manifests"][0]))
    changed_manifest = canonical_json_bytes(document)
    changed_identity = replace(
        intent.image,
        oci_index="sha256:" + sha256_bytes(changed_manifest),
    )
    with pytest.raises(PublicationBlocked, match="sealed_manifest_descriptor"):
        replace(
            intent,
            sealed_manifest=changed_manifest,
            image=changed_identity,
        ).validate()


def test_source_proof_command_is_package_owned_isolated_and_cwd_pinned() -> None:
    intent, _ = _intent_with_capability()
    proof = canonical_json_bytes(
        {
            "schema": "subgen.task12.source-proof/v1",
            "binding_sha256": intent.binding_sha256,
            "clean_worktree": True,
            "workflows_manual_only": True,
            "runtime_commit": intent.runtime_commit,
            "sampler_commit": intent.sampler_commit,
            "release_commit": intent.release_commit,
            "runtime_is_ancestor_of_sampler": True,
            "sampler_is_ancestor_of_release": True,
            "annotated_tag_object": intent.annotated_tag.object_sha,
            "annotated_tag_target": intent.release_commit,
            "release_notes_blob": intent.release_notes_blob,
            "release_notes_base64": base64.b64encode(intent.release_notes).decode(
                "ascii"
            ),
            "task11b_verifier_receipt_sha256": (intent.task11b_verifier_receipt_sha256),
            "candidate_docker_engine_id_sha256": CANDIDATE_ENGINE_ID_SHA256,
            "image": asdict_image(intent.image),
            "git_remote_url": CANONICAL_GIT_REMOTE_URL,
        }
    )
    commands = RecordingCommands(proof)
    verifier_inputs = _release_verifier_inputs()
    adapter = Task12HttpCommandAdapter(
        commands,
        ScriptedHttp([]),
        AdapterConfig(release_verifier_inputs=verifier_inputs),
    )
    adapter._release_blob = lambda *_args: b"release tool source\n"  # type: ignore[method-assign]
    assert (
        adapter.verify_local_sources(intent).git_remote_url == CANONICAL_GIT_REMOTE_URL
    )
    argv, stdin, environment, cwd = commands.calls[0]
    assert argv[0] == sys.executable
    assert argv[1] == "-I"
    assert argv[2].endswith("source_proof.py")
    assert intent.binding_sha256.encode("ascii") in (stdin or b"")
    request = __import__("json").loads(stdin or b"")
    assert request["schema"] == source_proof._SOURCE_PROOF_REQUEST_SCHEMA
    assert request["release_verifier_inputs"] == verifier_inputs.as_document()
    assert environment == {
        "PATH": os.defpath,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    assert cwd is not None and cwd != Path.cwd().resolve()
    assert not cwd.exists()


def test_source_proof_command_rejects_missing_typed_verifier_inputs() -> None:
    intent, _ = _intent_with_capability()
    commands = RecordingCommands(b"unexpected")
    adapter = Task12HttpCommandAdapter(commands, ScriptedHttp([]), AdapterConfig())

    with pytest.raises(PublicationBlocked, match="release_verifier_inputs_invalid"):
        adapter.verify_local_sources(intent)

    assert commands.calls == []


def test_release_tool_blob_is_read_from_exact_commit_with_replacements_disabled() -> (
    None
):
    intent, _ = _intent_with_capability()
    commands = RecordingCommands(b"committed release tool\n")
    adapter = Task12HttpCommandAdapter(commands, ScriptedHttp([]), AdapterConfig())
    assert (
        adapter._release_blob(intent, "release_tools/anonymous_smoke.py")
        == b"committed release tool\n"
    )
    argv, stdin, environment, cwd = commands.calls[0]
    assert argv[1:3] == ("--no-replace-objects", "-c")
    assert argv[-2:] == (
        "show",
        f"{intent.release_commit}:release_tools/anonymous_smoke.py",
    )
    assert stdin is None
    assert environment is not None
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert cwd == Path.cwd().resolve()


def test_release_tool_executes_committed_bytes_after_worktree_script_mutation(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "release-tool-repository"
    repository.mkdir()

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr.decode(
            "utf-8", errors="replace"
        )
        return completed.stdout.decode("ascii").strip()

    git("init", "--quiet")
    git("config", "user.name", "Task12 Test")
    git("config", "user.email", "task12@example.invalid")
    package = repository / "release_tools"
    package.mkdir()
    for name in ("__init__.py", "task12.py", "adapters.py", "journal.py"):
        (package / name).write_text(
            '"""Committed release-tool fixture."""\n',
            encoding="utf-8",
            newline="\n",
        )
    script = package / "anonymous_smoke.py"
    script.write_text(
        "import sys\nsys.stdin.buffer.read()\nsys.stdout.buffer.write(b'committed\\n')\n",
        encoding="utf-8",
        newline="\n",
    )
    git("add", "release_tools")
    git("commit", "--quiet", "-m", "committed release tool")
    release_commit = git("rev-parse", "HEAD")
    script.write_text(
        "import sys\nsys.stdout.buffer.write(b'mutated\\n')\n",
        encoding="utf-8",
        newline="\n",
    )

    intent, _ = _intent_with_capability()
    committed_intent = replace(intent, release_commit=release_commit)
    adapter = Task12HttpCommandAdapter(
        SubprocessCommandRunner(),
        ScriptedHttp([]),
        AdapterConfig(repository_root=repository.resolve()),
    )
    assert (
        adapter._run_release_tool(
            committed_intent,
            "anonymous_smoke.py",
            "anonymous_smoke",
            stdin=b"request\n",
        )
        == b"committed\n"
    )


def test_source_proof_rejects_duplicate_or_nonmanual_workflow_triggers() -> None:
    assert source_proof._workflow_is_manual_only(b"on:\n  workflow_dispatch:\n")
    assert not source_proof._workflow_is_manual_only(
        b"on: workflow_dispatch\non:\n  push:\n"
    )
    assert not source_proof._workflow_is_manual_only(
        b"on:\n  workflow_dispatch:\n  push:\n"
    )


def test_source_proof_runs_materialized_verifier_and_derives_candidate_identity(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    intent, _ = _intent_with_capability()
    candidate_record = tmp_path / "candidate.json"
    daemon_identity = {
        "schema": "subgen.task11b.docker-daemon/v1",
        "engine_id_sha256": CANDIDATE_ENGINE_ID_SHA256,
        "host_boot_id_sha256": "b" * 64,
        "docker_host": "unix:///var/run/docker.sock",
        "os_type": "linux",
    }
    candidate_record.write_bytes(
        canonical_json_bytes(
            {
                "schema": "subgen.task11b.candidate-identity/v2",
                "docker_daemon_identity_sha256": sha256_bytes(
                    canonical_json_bytes(daemon_identity)
                ),
                "candidate_identity": {
                    "container_id": "a" * 64,
                    "runtime_commit": intent.runtime_commit,
                    "oci_index": intent.image.oci_index,
                    "config_digest": intent.image.config_digest,
                    "layer_diff_ids": list(intent.image.ordered_diff_ids),
                },
            }
        )
    )
    execution_boundary = tmp_path / "execution-boundary.json"
    execution_boundary.write_bytes(
        canonical_json_bytes(
            {
                "schema": "subgen.task11b.execution-boundary/v1",
                "docker_daemon_identity": daemon_identity,
            }
        )
    )
    if os.name != "nt":
        candidate_record.chmod(0o600)
        execution_boundary.chmod(0o600)
    payloads = {
        source_proof._EVIDENCE_PATH: b"evidence",
        source_proof._OBSERVER_PATH: b"observer",
        source_proof._OBSERVER_TEST_PATH: b"observer-test",
        source_proof._SAMPLER_PATH: b"sampler",
        source_proof._SAMPLER_TEST_PATH: b"sampler-test",
        source_proof._PRODUCER_PATH: b"producer",
    }

    def fake_git(_root: Path, *arguments: str, **_kwargs: object) -> bytes:
        assert arguments[0] == "show"
        return payloads[arguments[1].split(":", 1)[1]]

    monkeypatch.setattr(source_proof, "_git", fake_git)
    monkeypatch.setattr(
        source_proof.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=b"TASK11B_RELEASE_VERIFY_OK\n",
            stderr=b"",
        ),
    )
    profiler_attempt: dict[str, str] = {}
    for key in source_proof._PROFILER_ATTEMPT_PATH_KEYS:
        path = tmp_path / f"profiler-{key}.bin"
        path.write_bytes(f"profiler:{key}\n".encode("ascii"))
        if os.name != "nt":
            path.chmod(0o600)
        profiler_attempt[key] = str(path)
    verifier_inputs = {
        "binding_prefix": "Task-11B-Sampler-Binding: ",
        **{key: str(candidate_record) for key in source_proof._VERIFIER_PATH_KEYS},
        "execution_boundary_manifest": str(execution_boundary),
        source_proof._PROFILER_ATTEMPTS_KEY: [profiler_attempt],
    }
    observed = source_proof._run_release_verifier(
        tmp_path,
        runtime=intent.runtime_commit,
        sampler=intent.sampler_commit,
        release=intent.release_commit,
        expected_receipt=b"TASK11B_RELEASE_VERIFY_OK\n",
        image=intent.image,
        verifier_inputs=verifier_inputs,
    )
    assert observed == (intent.image, CANDIDATE_ENGINE_ID_SHA256)
    with pytest.raises(PublicationBlocked, match="source_candidate_identity_mismatch"):
        source_proof._run_release_verifier(
            tmp_path,
            runtime=intent.runtime_commit,
            sampler=intent.sampler_commit,
            release=intent.release_commit,
            expected_receipt=b"TASK11B_RELEASE_VERIFY_OK\n",
            image=replace(intent.image, revision_label="f" * 40),
            verifier_inputs=verifier_inputs,
        )


def test_internal_anonymous_smoke_binds_remote_index_manifest_and_config(
    monkeypatch: Any,
) -> None:
    intent, _ = _intent_with_capability()
    child = intent.required_manifests[0]
    config = next(
        blob
        for blob in intent.required_blobs
        if blob.digest == intent.image.config_digest
    )
    layer = next(
        blob
        for blob in intent.required_blobs
        if blob.digest != intent.image.config_digest
    )

    def get(
        _client: object,
        path: str,
        _accept: str,
        *,
        maximum: int = 32 * 1024 * 1024,
    ) -> tuple[Mapping[str, str], bytes]:
        assert maximum > 0
        if path.endswith(intent.image.oci_index):
            return {
                "Docker-Content-Digest": intent.image.oci_index
            }, intent.sealed_manifest
        if path.endswith(child.digest):
            return {"Docker-Content-Digest": child.digest}, child.payload
        if path.endswith(config.digest):
            return {"Docker-Content-Digest": config.digest}, config.payload
        if path.endswith(layer.digest):
            return {"Docker-Content-Digest": layer.digest}, layer.payload
        raise AssertionError(path)

    monkeypatch.setattr(anonymous_smoke.AnonymousRegistryClient, "get", get)
    docker_calls: list[tuple[str, ImageIdentity, str]] = []

    def docker_smoke(
        reference: str,
        image: ImageIdentity,
        *,
        candidate_engine_id_sha256: str,
    ) -> str:
        docker_calls.append((reference, image, candidate_engine_id_sha256))
        return ANONYMOUS_ENGINE_ID_SHA256

    monkeypatch.setattr(anonymous_smoke, "_docker_smoke", docker_smoke)
    request = canonical_json_bytes(
        {
            "schema": "subgen.task12.anonymous-smoke-request/v1",
            "registry_origin": REGISTRY_API_ORIGIN,
            "client_contract": REGISTRY_CLIENT_CONTRACT,
            "credential_transport": CREDENTIAL_TRANSPORT_CONTRACT,
            "docker_host": ANONYMOUS_DOCKER_HOST,
            "docker_client_contract": ANONYMOUS_DOCKER_CLIENT_CONTRACT,
            "candidate_docker_engine_id_sha256": CANDIDATE_ENGINE_ID_SHA256,
            "reference": f"{intent.image_repository}@{intent.image.oci_index}",
            "image": asdict_image(intent.image),
        }
    )
    result = __import__("json").loads(anonymous_smoke.run_smoke(request))
    assert result["anonymous"] is True
    assert result["image"] == asdict_image(intent.image)
    assert docker_calls == [
        (
            f"{intent.image_repository}@{intent.image.oci_index}",
            intent.image,
            CANDIDATE_ENGINE_ID_SHA256,
        )
    ]
    assert result["anonymous_engine_id_sha256"] == ANONYMOUS_ENGINE_ID_SHA256
    assert result["candidate_engine_id_sha256"] == CANDIDATE_ENGINE_ID_SHA256


def test_docker_smoke_uses_distinct_empty_engine_and_cleans_exact_digest(
    monkeypatch: Any,
) -> None:
    intent, _ = _intent_with_capability()
    reference = f"{intent.image_repository}@{intent.image.oci_index}"
    full_reference = f"ghcr.io/{reference}"
    encoded_reference = __import__("urllib.parse").parse.quote(full_reference, safe="")
    pull_target = "/images/create?" + __import__("urllib.parse").parse.urlencode(
        {"fromImage": full_reference, "platform": "linux/amd64"}
    )
    events: list[tuple[str, str, bool] | str] = []
    sessions: list[Any] = []

    class DockerSession:
        def __init__(self, _config_root: Path) -> None:
            sessions.append(self)

        def request(
            self,
            method: str,
            target: str,
            *,
            maximum: int = 32 * 1024 * 1024,
            close_connection: bool = False,
        ) -> bytes:
            assert maximum == 32 * 1024 * 1024
            events.append((method, target, close_connection))
            if target == "/info":
                return __import__("json").dumps({"ID": ANONYMOUS_DAEMON_ID}).encode()
            if target == "/images/json?all=1":
                return b"[]"
            if target.startswith("/images/create?"):
                return b'{"status":"Downloaded"}\n'
            if target == f"/images/{encoded_reference}/json":
                return (
                    __import__("json")
                    .dumps(
                        {
                            "RepoDigests": [full_reference],
                            "RootFS": {"Layers": list(intent.image.ordered_diff_ids)},
                            "Config": {
                                "Labels": {
                                    "org.opencontainers.image.revision": (
                                        intent.image.revision_label
                                    )
                                }
                            },
                            "Architecture": "amd64",
                            "Os": "linux",
                            "Id": intent.image.config_digest,
                        },
                        separators=(",", ":"),
                    )
                    .encode()
                )
            if target == f"/images/{encoded_reference}?noprune=1":
                return b'[{"Deleted":"exact-reference"}]'
            raise AssertionError((method, target))

        def finish(self) -> None:
            events.append("finish")

    monkeypatch.setattr(anonymous_smoke, "_DockerEngineSession", DockerSession)
    anonymous_smoke._docker_smoke(
        reference,
        intent.image,
        candidate_engine_id_sha256=CANDIDATE_ENGINE_ID_SHA256,
    )
    assert len(sessions) == 1
    assert [event for event in events if event != "finish" and event[1] != "/info"] == [
        ("GET", "/images/json?all=1", False),
        ("POST", pull_target, False),
        ("GET", f"/images/{encoded_reference}/json", False),
        ("DELETE", f"/images/{encoded_reference}?noprune=1", False),
        ("GET", "/images/json?all=1", False),
    ]
    info_events = [
        event for event in events if event != "finish" and event[1] == "/info"
    ]
    assert len(info_events) == 12
    assert info_events[-1] == ("GET", "/info", True)
    assert events[-1] == "finish"


def test_docker_engine_session_reuses_one_dial_stdio_connection(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "docker"
    executable.write_bytes(b"")
    body = b'{"ID":"anonymous-daemon-001"}'
    responses = (
        b"HTTP/1.1 200 OK\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\nContent-Type: application/json\r\n\r\n"
        + body
        + b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        + b"2\r\n[]\r\n0\r\n\r\n"
    )
    popen_calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    class Process:
        def __init__(self) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(responses)
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: int) -> int:
            assert timeout == 5
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    process = Process()

    def popen(arguments: tuple[str, ...], **kwargs: Any) -> Process:
        popen_calls.append((arguments, kwargs))
        return process

    monkeypatch.setattr(anonymous_smoke, "_DOCKER_EXECUTABLE", executable)
    monkeypatch.setattr(anonymous_smoke.subprocess, "Popen", popen)
    session = anonymous_smoke._DockerEngineSession(tmp_path)
    assert session.request("GET", "/info") == body
    assert session.request("GET", "/images/json?all=1", close_connection=True) == b"[]"
    written_requests = process.stdin.getvalue()
    session.finish()

    assert len(popen_calls) == 1
    assert popen_calls[0][0] == (str(executable), "system", "dial-stdio")
    assert written_requests.count(b"HTTP/1.1\r\n") == 2
    assert b"Connection: keep-alive\r\n" in written_requests
    assert b"Connection: close\r\n" in written_requests


def test_docker_engine_session_rejects_unsolicited_bytes_before_request() -> None:
    session = object.__new__(anonymous_smoke._DockerEngineSession)
    session._closed = False
    session._poisoned = False
    session._input = io.BytesIO()
    read_descriptor, write_descriptor = os.pipe()
    session._output = os.fdopen(read_descriptor, "rb", buffering=0)
    try:
        os.write(
            write_descriptor,
            b"HTTP/1.1 200 forged\r\nContent-Length: 0\r\n\r\n",
        )
        with pytest.raises(
            PublicationBlocked,
            match="anonymous_docker_command_failed",
        ):
            session.request("GET", "/info")
        assert session._input.getvalue() == b""
    finally:
        os.close(write_descriptor)
        session._output.close()


def test_docker_engine_session_finish_rejects_trailing_transcript_bytes() -> None:
    class Process:
        returncode = 0

        @staticmethod
        def wait(timeout: int) -> int:
            assert timeout == 5
            return 0

        @staticmethod
        def poll() -> int:
            return 0

    session = object.__new__(anonymous_smoke._DockerEngineSession)
    session._closed = True
    session._poisoned = False
    session._input = io.BytesIO()
    session._output = io.BytesIO(b"UNSOLICITED")
    session._stderr = tempfile.TemporaryFile()
    session._process = Process()

    with pytest.raises(PublicationBlocked, match="anonymous_docker_command_failed"):
        session.finish()


def test_docker_engine_session_read_exact_accepts_fragmented_pipe_reads() -> None:
    class FragmentedStream:
        def __init__(self) -> None:
            self.fragments = iter((b"ab", b"c", b"de"))

        def read(self, size: int) -> bytes:
            fragment = next(self.fragments, b"")
            assert len(fragment) <= size
            return fragment

    assert (
        anonymous_smoke._DockerEngineSession._read_exact(
            FragmentedStream(),
            5,
        )
        == b"abcde"
    )


def test_docker_engine_session_process_control_failure_still_closes_streams() -> None:
    class Process:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        @staticmethod
        def terminate() -> None:
            raise OSError("simulated terminate denial")

        def wait(self, timeout: int) -> int:
            assert timeout == 5
            self.returncode = -9
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    session = object.__new__(anonymous_smoke._DockerEngineSession)
    session._closed = False
    session._poisoned = False
    session._input = io.BytesIO()
    session._output = io.BytesIO()
    session._stderr = tempfile.TemporaryFile()
    stderr = session._stderr
    session._process = Process()

    with pytest.raises(PublicationBlocked, match="anonymous_docker_command_failed"):
        session.finish()
    assert session._input.closed is True
    assert session._output.closed is True
    assert stderr.closed is True


def test_docker_engine_session_clean_wait_failure_still_closes_streams() -> None:
    class Process:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: int) -> int:
            assert timeout == 5
            if self.returncode is None:
                raise OSError("simulated wait failure")
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    session = object.__new__(anonymous_smoke._DockerEngineSession)
    session._closed = True
    session._poisoned = False
    session._input = io.BytesIO()
    session._output = io.BytesIO()
    session._stderr = tempfile.TemporaryFile()
    stderr = session._stderr
    session._process = Process()

    with pytest.raises(PublicationBlocked, match="anonymous_docker_command_failed"):
        session.finish()
    assert session._input.closed is True
    assert session._output.closed is True
    assert stderr.closed is True


def test_docker_smoke_removes_private_config_when_finish_itself_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    intent, _ = _intent_with_capability()
    config_root = tmp_path / "docker-config"
    config_root.mkdir()

    class FinishFailureSession:
        def __init__(self, _config_root: Path) -> None:
            pass

        @staticmethod
        def request(
            method: str,
            target: str,
            *,
            maximum: int = 32 * 1024 * 1024,
            close_connection: bool = False,
        ) -> bytes:
            assert method == "GET"
            assert target == "/info"
            assert maximum == 32 * 1024 * 1024
            return __import__("json").dumps({"ID": CANDIDATE_DAEMON_ID}).encode()

        @staticmethod
        def finish() -> None:
            raise OSError("simulated finish failure")

    monkeypatch.setattr(anonymous_smoke, "_DockerEngineSession", FinishFailureSession)
    monkeypatch.setattr(
        anonymous_smoke.tempfile,
        "mkdtemp",
        lambda **_kwargs: str(config_root),
    )
    with pytest.raises(PublicationBlocked, match="anonymous_docker_not_distinct_empty"):
        anonymous_smoke._docker_smoke(
            f"{intent.image_repository}@{intent.image.oci_index}",
            intent.image,
            candidate_engine_id_sha256=CANDIDATE_ENGINE_ID_SHA256,
        )
    assert not config_root.exists()


def test_docker_smoke_rejects_engine_switch_between_bound_requests(
    monkeypatch: Any,
) -> None:
    intent, _ = _intent_with_capability()
    reference = f"{intent.image_repository}@{intent.image.oci_index}"
    daemon_ids = iter(
        [ANONYMOUS_DAEMON_ID, ANONYMOUS_DAEMON_ID, "switched-daemon", "switched-daemon"]
    )
    actions: list[tuple[str, str]] = []

    class SwitchingDockerSession:
        def __init__(self, _config_root: Path) -> None:
            pass

        def request(
            self,
            method: str,
            target: str,
            *,
            maximum: int = 32 * 1024 * 1024,
            close_connection: bool = False,
        ) -> bytes:
            assert maximum == 32 * 1024 * 1024
            if target == "/info":
                return __import__("json").dumps({"ID": next(daemon_ids)}).encode()
            actions.append((method, target))
            return b"[]"

        def finish(self) -> None:
            pass

    monkeypatch.setattr(
        anonymous_smoke,
        "_DockerEngineSession",
        SwitchingDockerSession,
    )
    with pytest.raises(PublicationBlocked, match="anonymous_docker_identity_changed"):
        anonymous_smoke._docker_smoke(
            reference,
            intent.image,
            candidate_engine_id_sha256=CANDIDATE_ENGINE_ID_SHA256,
        )
    assert actions == [("GET", "/images/json?all=1")]


def test_docker_smoke_rejects_the_candidate_engine_before_pull(
    monkeypatch: Any,
) -> None:
    intent, _ = _intent_with_capability()
    reference = f"{intent.image_repository}@{intent.image.oci_index}"
    calls: list[tuple[str, str, bool]] = []

    class CandidateDockerSession:
        def __init__(self, _config_root: Path) -> None:
            pass

        def request(
            self,
            method: str,
            target: str,
            *,
            maximum: int = 32 * 1024 * 1024,
            close_connection: bool = False,
        ) -> bytes:
            assert maximum == 32 * 1024 * 1024
            calls.append((method, target, close_connection))
            return __import__("json").dumps({"ID": CANDIDATE_DAEMON_ID}).encode()

        def finish(self) -> None:
            pass

    monkeypatch.setattr(
        anonymous_smoke,
        "_DockerEngineSession",
        CandidateDockerSession,
    )
    with pytest.raises(PublicationBlocked, match="anonymous_docker_not_distinct_empty"):
        anonymous_smoke._docker_smoke(
            reference,
            intent.image,
            candidate_engine_id_sha256=CANDIDATE_ENGINE_ID_SHA256,
        )
    assert calls == [
        ("GET", "/info", False),
        ("GET", "/info", True),
    ]


def test_journal_rejects_competing_writer_and_mixed_run_tokens(
    tmp_path: Path,
) -> None:
    intent, receipt = _intent_with_capability()
    memory = MemorySink()
    _publisher(FakeAdapter(intent), memory).publish(intent)
    checkpoint = memory.receipts[0]
    first = FileReceiptJournal(tmp_path / "single-writer")
    second = FileReceiptJournal(tmp_path / "single-writer")
    with first.exclusive():
        with pytest.raises(PublicationBlocked, match="receipt_lock_unavailable"):
            with second.exclusive():
                raise AssertionError("second writer unexpectedly acquired lock")
        first.append(checkpoint)
        foreign = copy.deepcopy(checkpoint)
        foreign.run_token = "d" * 64
        with pytest.raises(PublicationBlocked, match="receipt_run_identity_changed"):
            first.append(foreign)


def test_new_posix_journal_directory_syncs_parent_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "parent-sync"
    synced: list[Path] = []
    monkeypatch.setattr(
        FileReceiptJournal,
        "_fsync_created_directory_parent",
        lambda self, _parent_descriptor=None, _directory_descriptor=None: synced.append(
            self.directory.parent
        ),
        raising=False,
    )

    journal = FileReceiptJournal(directory)
    journal._ensure_directory()

    assert synced == [tmp_path]


def test_journal_requires_existing_parent_without_recursive_creation(
    tmp_path: Path,
) -> None:
    missing_parent = tmp_path / "missing" / "nested"

    with pytest.raises(
        PublicationBlocked,
        match="receipt_directory_parent_unavailable",
    ):
        FileReceiptJournal(missing_parent / "journal")

    assert not (tmp_path / "missing").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink proof")
def test_journal_rejects_aliased_parent_before_leaf_creation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(
        PublicationBlocked,
        match="receipt_directory_parent_aliased",
    ):
        FileReceiptJournal(alias / "journal")

    assert not (target / "journal").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink proof")
def test_journal_never_creates_descendants_through_intermediate_alias(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(
        PublicationBlocked,
        match="receipt_directory_parent_unavailable",
    ):
        FileReceiptJournal(alias / "missing" / "journal")

    assert not (target / "missing").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode proof")
def test_journal_rejects_group_writable_parent(tmp_path: Path) -> None:
    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir(mode=0o770)
    unsafe_parent.chmod(0o770)

    with pytest.raises(
        PublicationBlocked,
        match="receipt_directory_parent_permissions",
    ):
        FileReceiptJournal(unsafe_parent / "journal")

    assert not (unsafe_parent / "journal").exists()


def test_posix_journal_directory_parent_sync_targets_exact_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "journal"
    directory.mkdir()
    opened: list[tuple[Path, int]] = []
    synced: list[int] = []
    closed: list[int] = []
    descriptor = 73

    class FakePosixOs:
        name = "posix"
        O_RDONLY = 1
        O_DIRECTORY = 2
        O_CLOEXEC = 4
        O_NOFOLLOW = 8

        @staticmethod
        def open(path: Path, flags: int) -> int:
            opened.append((path, flags))
            raise AssertionError("validated parent descriptor must not be reopened")

        @staticmethod
        def fstat(candidate: int) -> os.stat_result:
            assert candidate == descriptor
            return tmp_path.lstat()

        @staticmethod
        def stat(
            name: str,
            *,
            dir_fd: int,
            follow_symlinks: bool,
        ) -> os.stat_result:
            assert name == directory.name
            assert dir_fd == descriptor
            assert follow_symlinks is False
            return directory.lstat()

        @staticmethod
        def fsync(candidate: int) -> None:
            synced.append(candidate)

        @staticmethod
        def close(candidate: int) -> None:
            closed.append(candidate)

    journal = object.__new__(FileReceiptJournal)
    journal.directory = directory
    monkeypatch.setattr(journal_module, "os", FakePosixOs)

    journal._fsync_created_directory_parent(descriptor)

    assert opened == []
    assert synced == [descriptor]
    assert closed == []


def test_posix_journal_parent_open_is_bound_to_preopen_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = 74
    closed: list[int] = []

    def parent_info(*, inode: int, uid: int) -> SimpleNamespace:
        return SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700,
            st_uid=uid,
            st_dev=1,
            st_ino=inode,
            st_size=0,
            st_mtime_ns=1,
            st_ctime_ns=1,
        )

    initial = parent_info(inode=10, uid=1000)
    replacement = parent_info(inode=11, uid=2000)

    class FakeParent:
        def __init__(self) -> None:
            self.reads = 0

        def lstat(self) -> SimpleNamespace:
            self.reads += 1
            return initial if self.reads == 1 else replacement

        def resolve(self, *, strict: bool) -> FakeParent:
            assert strict is True
            return self

    parent = FakeParent()

    class FakeDirectory:
        name = "journal"

        @property
        def parent(self) -> FakeParent:
            return parent

    class FakePosixOs:
        name = "posix"
        O_RDONLY = 1
        O_DIRECTORY = 2
        O_CLOEXEC = 4
        O_NOFOLLOW = 8

        @staticmethod
        def geteuid() -> int:
            return 1000

        @staticmethod
        def open(path: FakeParent, flags: int) -> int:
            assert path is parent
            assert flags == 15
            return descriptor

        @staticmethod
        def fstat(candidate: int) -> SimpleNamespace:
            assert candidate == descriptor
            return replacement

        @staticmethod
        def close(candidate: int) -> None:
            closed.append(candidate)

    journal = object.__new__(FileReceiptJournal)
    journal.directory = FakeDirectory()
    monkeypatch.setattr(journal_module, "os", FakePosixOs)

    with pytest.raises(
        PublicationBlocked,
        match="receipt_directory_parent_changed",
    ):
        journal._open_validated_directory_parent()

    assert closed == [descriptor]


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor proof")
def test_journal_child_chmod_cannot_follow_post_open_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    directory = parent / "journal"
    victim = tmp_path / "victim"
    victim.write_bytes(b"unchanged")
    victim.chmod(0o600)
    displaced_parent = tmp_path / "displaced-parent"
    original_open = os.open
    swapped = False

    def open_and_swap(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == directory.name and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(displaced_parent)
            parent.mkdir(mode=0o700)
            (parent / directory.name).symlink_to(victim)
        return descriptor

    monkeypatch.setattr(journal_module.os, "open", open_and_swap)

    with pytest.raises(PublicationBlocked, match="receipt_directory_changed"):
        FileReceiptJournal(directory)

    assert swapped is True
    assert stat.S_IMODE(victim.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor proof")
def test_exclusive_rejects_path_replacement_after_verified_child_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = FileReceiptJournal(tmp_path / "journal")
    original = journal.directory
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o777)
    replacement.chmod(0o777)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    original_descriptor = os.open(original, flags)
    journal.directory = replacement
    monkeypatch.setattr(
        journal,
        "_ensure_directory",
        lambda **_kwargs: original_descriptor,
    )

    with pytest.raises(PublicationBlocked, match="receipt_directory_changed"):
        with journal.exclusive():
            raise AssertionError("replacement directory was accepted")

    with pytest.raises(OSError):
        os.fstat(original_descriptor)


def test_journal_hash_chain_rejects_rewritten_history(tmp_path: Path) -> None:
    intent, receipt = _intent_with_capability()
    memory = MemorySink()
    _publisher(FakeAdapter(intent), memory).publish(intent)
    first_checkpoint = memory.receipts[0]
    second_checkpoint = memory.receipts[1]
    journal = FileReceiptJournal(tmp_path / "hash-chain")
    with journal.exclusive():
        journal.append(first_checkpoint)
        journal.append(second_checkpoint)
        first_path = tmp_path / "hash-chain" / "checkpoint-00000001.json"
        first_document = __import__("json").loads(first_path.read_bytes())
        first_document["phase"] = "rewritten"
        first_path.write_bytes(canonical_json_bytes(first_document))
        with pytest.raises(PublicationBlocked):
            journal.load_latest()


def test_journal_rejects_forged_complete_first_checkpoint(tmp_path: Path) -> None:
    intent, _ = _intent_with_capability()
    checkpoint = _publisher(FakeAdapter(intent), MemorySink()).publish(intent)
    journal = FileReceiptJournal(tmp_path / "forged-bootstrap")
    with (
        journal.exclusive(),
        pytest.raises(
            PublicationBlocked,
            match="receipt_initial_checkpoint_invalid",
        ),
    ):
        journal.append(checkpoint)


def test_journal_rejects_tail_rewrite_of_latest_write_arming(tmp_path: Path) -> None:
    intent, _ = _intent_with_capability()
    adapter = FakeAdapter(intent)
    adapter.fail_before = "registry_latest_update"
    memory = MemorySink()
    publisher = _publisher(adapter, memory)
    with pytest.raises(PublicationBlocked):
        publisher.publish(intent)

    journal = FileReceiptJournal(tmp_path / "tail-regression")
    with journal.exclusive():
        for receipt in memory.receipts:
            journal.append(receipt)
        tail = sorted((tmp_path / "tail-regression").glob("checkpoint-*.json"))[-1]
        document = __import__("json").loads(tail.read_bytes())
        assert document["latest_write_attempted"] is True
        document["latest_write_attempted"] = False
        tail.write_bytes(canonical_json_bytes(document))
        with pytest.raises(
            PublicationBlocked,
            match="receipt_latest_write_attempted_regressed",
        ):
            journal.load_latest()


def test_journal_rejects_probe_identity_regression_and_etag_mutation() -> None:
    intent, _ = _intent_with_capability()
    checkpoint = _publisher(FakeAdapter(intent), MemorySink()).publish(intent)
    assert checkpoint.version_create_probe is not None
    assert checkpoint.latest_cas_probe is not None

    previous_create = copy.deepcopy(checkpoint.version_create_probe)
    previous_create.stage = "reject_armed"
    previous_create.verification_sha256 = None
    changed_reference = copy.deepcopy(previous_create)
    changed_reference.reference += "-changed"
    with pytest.raises(PublicationBlocked, match="receipt_probe_identity_changed"):
        FileReceiptJournal._validate_probe_transition(
            previous_create,
            changed_reference,
            kind="create",
        )

    regressed = copy.deepcopy(previous_create)
    regressed.stage = "seed_armed"
    with pytest.raises(PublicationBlocked, match="receipt_probe_transition_invalid"):
        FileReceiptJournal._validate_probe_transition(
            previous_create,
            regressed,
            kind="create",
        )

    previous_cas = copy.deepcopy(checkpoint.latest_cas_probe)
    previous_cas.stage = "stale_armed"
    previous_cas.verification_sha256 = None
    changed_etag = copy.deepcopy(previous_cas)
    changed_etag.prior_etag = '"foreign"'
    with pytest.raises(PublicationBlocked, match="receipt_probe_etag_changed"):
        FileReceiptJournal._validate_probe_transition(
            previous_cas,
            changed_etag,
            kind="cas",
        )


def test_journal_rejects_obsolete_hash_only_v2_checkpoint() -> None:
    intent, _ = _intent_with_capability()
    checkpoint = _publisher(FakeAdapter(intent), MemorySink()).publish(intent)
    document = checkpoint.snapshot()
    document["schema"] = "subgen.task12.publication-checkpoint/v2"
    document = __import__("json").loads(canonical_json_bytes(document))
    with pytest.raises(PublicationBlocked, match="receipt_schema_invalid"):
        FileReceiptJournal._decode(document)


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner/mode proof")
def test_owner_only_input_read_uses_no_follow_descriptor(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(b"proof\n")
    source.chmod(0o600)
    assert cli._read_owner_only(source.resolve(), "source") == b"proof\n"
    alias = tmp_path / "alias.json"
    alias.symlink_to(source)
    with pytest.raises(PublicationBlocked):
        cli._read_owner_only(alias.absolute(), "source")


def test_journal_round_trip_is_append_only_and_detects_alias(tmp_path: Path) -> None:
    intent, receipt = _intent_with_capability()
    adapter = FakeAdapter(intent)
    memory = MemorySink()
    _publisher(adapter, memory).publish(intent)
    first_checkpoint = memory.receipts[0]
    second_checkpoint = memory.receipts[1]
    journal = FileReceiptJournal(tmp_path / "receipts")
    with journal.exclusive():
        journal.append(first_checkpoint)
        assert journal.load_latest() == first_checkpoint
        journal.append(second_checkpoint)
        assert journal.load_latest() == second_checkpoint
    assert sorted(
        path.name for path in (tmp_path / "receipts").glob("checkpoint-*.json")
    ) == [
        "checkpoint-00000001.json",
        "checkpoint-00000002.json",
    ]
