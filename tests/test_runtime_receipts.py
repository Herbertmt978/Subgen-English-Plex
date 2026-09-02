import json
import os
from pathlib import Path

import pytest

from subgen_core import runtime_receipts as receipts


TOKEN = "1" * 64
PHASE_A = "a" * 64
PHASE_B = "b" * 64
MODEL_IDENTITY = "d" * 64


class MemoryJournal:
    instances = []

    def __init__(self, path):
        self.path = path
        self.payloads = []
        self.closed = False
        self.failure = None
        type(self).instances.append(self)

    def append(self, payload):
        if self.failure is not None:
            raise self.failure
        self.payloads.append(payload)

    def close(self):
        self.closed = True


@pytest.fixture
def memory_journal(monkeypatch):
    MemoryJournal.instances.clear()
    monkeypatch.setattr(receipts, "_SecureJournal", MemoryJournal)
    return MemoryJournal


def identity(*, started=100):
    return receipts.RuntimeIdentity(epoch="e" * 32, started_monotonic_ns=started)


def config(tmp_path):
    return receipts.GateReceiptConfig(
        receipt_file=(tmp_path / "receipts.jsonl").resolve(),
        gate_token_sha256=TOKEN,
        phase_a_workload_sha256=PHASE_A,
        phase_b_workload_sha256=PHASE_B,
    )


def runtime_state(**changes):
    state = {
        "source_generation": None,
        "observation_digest": None,
        "transition_observation_digest": None,
        "transition_sequence": 0,
        "heartbeat_age_ms": None,
        "source_age_ms": None,
        "policy_sha256": None,
        "priority_state": "unavailable",
        "controller_phase": "recovering",
        "recovery_reason": "priority_pressure",
        "admission_open": False,
        "distinct_clear_count": 0,
        "model_resident": False,
        "model_load_generation": 0,
        "model_unload_generation": 0,
        "model_identity_sha256": None,
        "cuda_oom_generation": 0,
        "media_failure_generation": 0,
    }
    state.update(changes)
    return state


def documents(journal):
    return [json.loads(payload) for payload in journal.payloads]


def coordinator(tmp_path, memory_journal, *, clock=lambda: 200):
    item = receipts.RuntimeReceiptCoordinator(
        identity=identity(),
        config=config(tmp_path),
        monotonic_ns=clock,
    )
    return item, memory_journal.instances[-1]


def test_runtime_identity_is_exact_process_local_state():
    values = iter((bytes(range(16)), bytes(reversed(range(16)))))
    first = receipts.RuntimeIdentity.create(
        random_bytes=lambda size: next(values), monotonic_ns=lambda: 123
    )
    second = receipts.RuntimeIdentity.create(
        random_bytes=lambda size: next(values), monotonic_ns=lambda: 124
    )

    assert first.snapshot() == {
        "epoch": "000102030405060708090a0b0c0d0e0f",
        "started_monotonic_ns": 123,
    }
    assert second.epoch != first.epoch
    assert set(first.snapshot()) == {"epoch", "started_monotonic_ns"}


@pytest.mark.parametrize(
    ("random_value", "clock_value"),
    [(b"short", 1), (b"0" * 16, 0), (b"0" * 16, True)],
)
def test_runtime_identity_rejects_invalid_entropy_or_time(random_value, clock_value):
    with pytest.raises(receipts.RuntimeReceiptError):
        receipts.RuntimeIdentity.create(
            random_bytes=lambda size: random_value,
            monotonic_ns=lambda: clock_value,
        )


def test_gate_configuration_is_disabled_only_when_all_four_values_are_empty():
    item = receipts.GateReceiptConfig.from_environment({}, concurrent_transcriptions=99)
    assert item == receipts.GateReceiptConfig()
    assert item.enabled is False


@pytest.mark.parametrize("missing", receipts.GATE_ENVIRONMENT_KEYS)
def test_gate_configuration_rejects_every_nonempty_proper_subset(tmp_path, missing):
    environment = {
        receipts.GATE_RECEIPT_FILE: str((tmp_path / "journal").resolve()),
        receipts.GATE_TOKEN_SHA256: TOKEN,
        receipts.PHASE_A_WORKLOAD_SHA256: PHASE_A,
        receipts.PHASE_B_WORKLOAD_SHA256: PHASE_B,
    }
    environment[missing] = ""

    with pytest.raises(receipts.RuntimeReceiptError, match="entirely enabled"):
        receipts.GateReceiptConfig.from_environment(
            environment, concurrent_transcriptions=1
        )


@pytest.mark.parametrize("concurrency", [0, 2, True, "2", "one", None])
def test_gate_configuration_requires_exact_single_transcription(tmp_path, concurrency):
    environment = {
        receipts.GATE_RECEIPT_FILE: str((tmp_path / "journal").resolve()),
        receipts.GATE_TOKEN_SHA256: TOKEN,
        receipts.PHASE_A_WORKLOAD_SHA256: PHASE_A,
        receipts.PHASE_B_WORKLOAD_SHA256: PHASE_B,
    }

    with pytest.raises(
        receipts.RuntimeReceiptError, match="CONCURRENT_TRANSCRIPTIONS=1"
    ):
        receipts.GateReceiptConfig.from_environment(
            environment, concurrent_transcriptions=concurrency
        )


def test_gate_configuration_accepts_integer_string_and_binds_expected_token(tmp_path):
    environment = {
        receipts.GATE_RECEIPT_FILE: str((tmp_path / "journal").resolve()),
        receipts.GATE_TOKEN_SHA256: TOKEN,
        receipts.PHASE_A_WORKLOAD_SHA256: PHASE_A,
        receipts.PHASE_B_WORKLOAD_SHA256: PHASE_B,
    }
    item = receipts.GateReceiptConfig.from_environment(
        environment,
        concurrent_transcriptions="1",
        expected_gate_token_sha256=TOKEN,
    )
    assert item.enabled is True
    assert item.receipt_file == (tmp_path / "journal").resolve()

    with pytest.raises(receipts.RuntimeReceiptError, match="execution boundary"):
        receipts.GateReceiptConfig.from_environment(
            environment,
            concurrent_transcriptions=1,
            expected_gate_token_sha256="2" * 64,
        )


@pytest.mark.parametrize(
    "updates",
    [
        {receipts.GATE_TOKEN_SHA256: "A" * 64},
        {receipts.PHASE_A_WORKLOAD_SHA256: "x" * 64},
        {receipts.PHASE_B_WORKLOAD_SHA256: PHASE_A},
        {receipts.GATE_RECEIPT_FILE: "relative.jsonl"},
    ],
)
def test_gate_configuration_rejects_invalid_hash_path_and_duplicate_phase(
    tmp_path, updates
):
    environment = {
        receipts.GATE_RECEIPT_FILE: str((tmp_path / "journal").resolve()),
        receipts.GATE_TOKEN_SHA256: TOKEN,
        receipts.PHASE_A_WORKLOAD_SHA256: PHASE_A,
        receipts.PHASE_B_WORKLOAD_SHA256: PHASE_B,
    }
    environment.update(updates)
    with pytest.raises(receipts.RuntimeReceiptError):
        receipts.GateReceiptConfig.from_environment(
            environment, concurrent_transcriptions=1
        )


def test_canonical_json_is_ascii_sorted_and_newline_terminated():
    document = {"z": "£", "a": 1}
    assert receipts.canonical_json_line(document) == b'{"a":1,"z":"\\u00a3"}\n'


def test_disabled_coordinator_aggregates_concurrent_workloads_with_opaque_tokens():
    item = receipts.RuntimeReceiptCoordinator(
        identity=identity(), config=receipts.GateReceiptConfig()
    )
    item.initialize()
    first = item.begin_workload(None, cursor_ms=0)
    second = item.begin_workload(None, cursor_ms=20)

    assert isinstance(first, receipts.WorkloadToken)
    assert first is not second
    assert item.workload_snapshot() == {
        "active": True,
        "chunk_uncommitted": False,
        "completion_generation": 0,
    }
    assert item.record_chunk(first, cursor_ms=0, chunk_uncommitted=True) is True
    assert item.record_chunk(second, cursor_ms=30, chunk_uncommitted=False) is True
    assert item.workload_snapshot()["chunk_uncommitted"] is True

    assert item.complete_workload(first, terminal_cursor_ms=100) == 1
    assert item.workload_snapshot() == {
        "active": True,
        "chunk_uncommitted": False,
        "completion_generation": 1,
    }
    item.abort_workload(second)
    assert item.workload_snapshot() == {
        "active": False,
        "chunk_uncommitted": False,
        "completion_generation": 1,
    }
    assert set(item.workload_snapshot()) == {
        "active",
        "chunk_uncommitted",
        "completion_generation",
    }


def test_disabled_coordinator_rejects_foreign_stale_and_regressing_tokens():
    first = receipts.RuntimeReceiptCoordinator(
        identity=identity(), config=receipts.GateReceiptConfig()
    )
    second = receipts.RuntimeReceiptCoordinator(
        identity=identity(started=101), config=receipts.GateReceiptConfig()
    )
    token = first.begin_workload(None, cursor_ms=10)
    foreign = second.begin_workload(None)

    with pytest.raises(receipts.RuntimeReceiptError, match="foreign"):
        first.record_chunk(foreign, cursor_ms=0, chunk_uncommitted=True)
    with pytest.raises(receipts.RuntimeReceiptError, match="regress"):
        first.record_chunk(token, cursor_ms=9, chunk_uncommitted=False)
    first.abort_workload(token)
    with pytest.raises(receipts.RuntimeReceiptError, match="no longer active"):
        first.complete_workload(token, terminal_cursor_ms=10)


def test_enabled_initial_receipt_has_exact_keys_types_and_null_workload(
    tmp_path, memory_journal
):
    item, journal = coordinator(tmp_path, memory_journal)
    item.initialize(runtime_state())

    assert len(journal.payloads) == 1
    payload = journal.payloads[0]
    assert payload.endswith(b"\n") and len(payload) <= receipts.MAX_RECEIPT_BYTES
    assert payload == receipts.canonical_json_line(json.loads(payload))
    document = json.loads(payload)
    assert set(document) == receipts.RECEIPT_KEYS
    assert document["schema"] == receipts.RECEIPT_SCHEMA
    assert document["runtime_epoch"] == "e" * 32
    assert document["gate_token_sha256"] == TOKEN
    assert document["sequence"] == 1
    assert document["workload_sha256"] is None
    assert document["active"] is False
    assert document["chunk_uncommitted"] is False
    assert document["active_cursor_ms"] is None
    assert document["completed_cursor_ms"] is None
    assert item.runtime_identity_snapshot() == identity().snapshot()


def test_enabled_gate_records_phase_a_then_phase_b_and_every_transition(
    tmp_path, memory_journal
):
    clock_values = iter([200, 200, 199, 203, 204, 205, 206, 207, 208, 209])
    item, journal = coordinator(
        tmp_path, memory_journal, clock=lambda: next(clock_values)
    )
    initial = runtime_state()
    item.initialize(initial)
    phase_a = item.begin_workload(PHASE_A, cursor_ms=0, runtime_state=initial)
    assert item.record_chunk(
        phase_a, cursor_ms=0, chunk_uncommitted=True, runtime_state=initial
    )
    assert item.record_chunk(
        phase_a, cursor_ms=0, chunk_uncommitted=False, runtime_state=initial
    )
    accepted = runtime_state(
        source_generation=7,
        observation_digest="7" * 64,
        transition_observation_digest="7" * 64,
        transition_sequence=1,
        heartbeat_age_ms=1,
        source_age_ms=2,
        policy_sha256="8" * 64,
        priority_state="clear",
        controller_phase="normal",
        recovery_reason=None,
        admission_open=True,
        distinct_clear_count=3,
    )
    item.record_runtime_change(accepted)
    loaded = dict(
        accepted,
        model_resident=True,
        model_load_generation=1,
        model_identity_sha256=MODEL_IDENTITY,
    )
    item.record_runtime_change(loaded)
    assert (
        item.complete_workload(phase_a, terminal_cursor_ms=1000, runtime_state=loaded)
        == 1
    )
    phase_b = item.begin_workload(PHASE_B, cursor_ms=0, runtime_state=loaded)
    assert (
        item.complete_workload(phase_b, terminal_cursor_ms=50, runtime_state=loaded)
        == 2
    )

    records = documents(journal)
    assert [record["sequence"] for record in records] == list(
        range(1, len(records) + 1)
    )
    assert [record["observed_monotonic_ns"] for record in records] == sorted(
        {record["observed_monotonic_ns"] for record in records}
    )
    assert records[0]["workload_sha256"] is None
    assert records[1]["workload_sha256"] == PHASE_A
    assert records[2]["chunk_uncommitted"] is True
    assert records[3]["chunk_uncommitted"] is False
    assert records[-3]["active"] is False
    assert records[-3]["completed_cursor_ms"] == 1000
    assert records[-3]["completion_generation"] == 1
    assert records[-2]["workload_sha256"] == PHASE_B
    assert records[-2]["completed_cursor_ms"] is None
    assert records[-1]["active"] is False
    assert records[-1]["completed_cursor_ms"] == 50
    assert records[-1]["completion_generation"] == 2


def test_enabled_gate_rejects_foreign_out_of_order_concurrent_and_repeated_work(
    tmp_path, memory_journal
):
    item, _journal = coordinator(tmp_path, memory_journal)
    state = runtime_state()
    item.initialize(state)
    with pytest.raises(receipts.RuntimeReceiptError, match="out of order"):
        item.begin_workload(PHASE_B, runtime_state=state)
    with pytest.raises(receipts.RuntimeReceiptError, match="foreign"):
        item.begin_workload("f" * 64, runtime_state=state)
    token = item.begin_workload(PHASE_A, runtime_state=state)
    with pytest.raises(receipts.RuntimeReceiptError, match="concurrent"):
        item.begin_workload(PHASE_A, runtime_state=state)
    item.complete_workload(token, terminal_cursor_ms=1, runtime_state=state)
    with pytest.raises(receipts.RuntimeReceiptError, match="out of order"):
        item.begin_workload(PHASE_A, runtime_state=state)


def test_enabled_abort_retains_hash_does_not_complete_and_blocks_phase_b(
    tmp_path, memory_journal
):
    item, journal = coordinator(tmp_path, memory_journal)
    state = runtime_state()
    item.initialize(state)
    token = item.begin_workload(PHASE_A, runtime_state=state)
    item.abort_workload(token, runtime_state=state)

    last = documents(journal)[-1]
    assert last["workload_sha256"] == PHASE_A
    assert last["active"] is False
    assert last["active_cursor_ms"] is None
    assert last["completed_cursor_ms"] is None
    assert last["completion_generation"] == 0
    with pytest.raises(receipts.RuntimeReceiptError, match="aborted"):
        item.begin_workload(PHASE_B, runtime_state=state)


def test_journal_failure_happens_before_workload_state_is_exposed(
    tmp_path, memory_journal
):
    item, journal = coordinator(tmp_path, memory_journal)
    state = runtime_state()
    item.initialize(state)
    journal.failure = receipts.RuntimeReceiptError("disk failure")

    with pytest.raises(receipts.RuntimeReceiptError, match="disk failure"):
        item.begin_workload(PHASE_A, runtime_state=state)
    assert item.workload_snapshot() == {
        "active": False,
        "chunk_uncommitted": False,
        "completion_generation": 0,
    }
    with pytest.raises(receipts.RuntimeReceiptError, match="failed closed"):
        item.record_runtime_change(state)


def test_completion_write_failure_does_not_advance_public_generation(
    tmp_path, memory_journal
):
    item, journal = coordinator(tmp_path, memory_journal)
    state = runtime_state()
    item.initialize(state)
    token = item.begin_workload(PHASE_A, runtime_state=state)
    journal.failure = receipts.RuntimeReceiptError("fsync failure")

    with pytest.raises(receipts.RuntimeReceiptError, match="fsync failure"):
        item.complete_workload(token, terminal_cursor_ms=10, runtime_state=state)
    assert item.workload_snapshot() == {
        "active": True,
        "chunk_uncommitted": False,
        "completion_generation": 0,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_generation", True),
        ("transition_sequence", -1),
        ("heartbeat_age_ms", 60_001),
        ("priority_state", "disabled"),
        ("controller_phase", "unknown"),
        ("admission_open", 1),
        ("distinct_clear_count", 4),
        ("model_resident", 1),
        ("model_load_generation", True),
        ("cuda_oom_generation", -1),
    ],
)
def test_runtime_receipt_rejects_invalid_exact_types(
    tmp_path, memory_journal, field, value
):
    item, _journal = coordinator(tmp_path, memory_journal)
    state = runtime_state()
    state[field] = value
    with pytest.raises(receipts.RuntimeReceiptError):
        item.initialize(state)


def test_runtime_receipt_requires_exact_keys_and_linked_priority_fields(
    tmp_path, memory_journal
):
    item, _journal = coordinator(tmp_path, memory_journal)
    extra = runtime_state(extra_private_path="/media/private.mkv")
    with pytest.raises(receipts.RuntimeReceiptError, match="keys"):
        item.initialize(extra)

    item2, _journal2 = coordinator(tmp_path, memory_journal)
    incomplete = runtime_state(source_generation=1)
    with pytest.raises(receipts.RuntimeReceiptError, match="last-accepted"):
        item2.initialize(incomplete)


def test_model_residency_generation_and_identity_transitions_are_lossless(
    tmp_path, memory_journal
):
    item, _journal = coordinator(tmp_path, memory_journal)
    initial = runtime_state()
    item.initialize(initial)
    bad_load = dict(
        initial,
        model_resident=True,
        model_load_generation=2,
        model_identity_sha256=MODEL_IDENTITY,
    )
    with pytest.raises(receipts.RuntimeReceiptError, match="not lossless"):
        item.record_runtime_change(bad_load)

    loaded = dict(
        initial,
        model_resident=True,
        model_load_generation=1,
        model_identity_sha256=MODEL_IDENTITY,
    )
    item.record_runtime_change(loaded)
    changed_identity = dict(loaded, model_identity_sha256="c" * 64)
    with pytest.raises(receipts.RuntimeReceiptError, match="identity changed"):
        item.record_runtime_change(changed_identity)
    bad_unload = dict(
        loaded,
        model_resident=False,
        model_identity_sha256=None,
        model_unload_generation=0,
    )
    with pytest.raises(receipts.RuntimeReceiptError, match="unload transition"):
        item.record_runtime_change(bad_unload)


def test_close_closes_journal_and_prevents_mutation(tmp_path, memory_journal):
    item, journal = coordinator(tmp_path, memory_journal)
    item.initialize(runtime_state())
    item.close()
    assert journal.closed is True
    with pytest.raises(receipts.RuntimeReceiptError, match="closed"):
        item.begin_workload(PHASE_A, runtime_state=runtime_state())


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX ownership semantics")
def test_secure_journal_requires_owner_mode_0700_parent_and_create_once(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    path = parent / "journal.jsonl"

    journal = receipts._SecureJournal(path)
    try:
        assert (path.stat().st_mode & 0o777) == 0o600
        assert path.stat().st_uid == os.geteuid()
    finally:
        journal.close()

    with pytest.raises(receipts.RuntimeReceiptError, match="already existed"):
        receipts._SecureJournal(path)

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    unsafe.chmod(0o755)
    with pytest.raises(receipts.RuntimeReceiptError, match="parent ownership"):
        receipts._SecureJournal(unsafe / "journal.jsonl")


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX ownership semantics")
def test_secure_journal_uses_one_checked_write_then_fsync(tmp_path, monkeypatch):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    journal = receipts._SecureJournal(parent / "journal.jsonl")
    original_write = os.write
    original_fsync = os.fsync
    events = []

    def observed_write(fd, payload):
        events.append(("write", bytes(payload)))
        return original_write(fd, payload)

    def observed_fsync(fd):
        events.append(("fsync", fd))
        return original_fsync(fd)

    monkeypatch.setattr(receipts.os, "write", observed_write)
    monkeypatch.setattr(receipts.os, "fsync", observed_fsync)
    try:
        journal.append(b'{"sequence":1}\n')
    finally:
        journal.close()

    assert [event[0] for event in events] == ["write", "fsync"]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX ownership semantics")
def test_secure_journal_rejects_short_write_and_inode_replacement(
    tmp_path, monkeypatch
):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    path = parent / "journal.jsonl"
    journal = receipts._SecureJournal(path)
    try:
        monkeypatch.setattr(receipts.os, "write", lambda fd, payload: len(payload) - 1)
        with pytest.raises(receipts.RuntimeReceiptError, match="partial write"):
            journal.append(b"{}\n")
    finally:
        journal.close()

    replacement = parent / "replacement"
    replacement.write_bytes(b"")
    replacement.chmod(0o600)
    path.unlink()
    replacement.rename(path)
    # A newly-created writer cannot open the replacement either.
    with pytest.raises(receipts.RuntimeReceiptError, match="already existed"):
        receipts._SecureJournal(path)
