from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading

import pytest

import subgen_core.priority_pressure as priority_pressure
from subgen_core.priority_pressure import (
    PriorityPressureReader,
    PrioritySignalSnapshot,
)


BOOT_ID = "123e4567-e89b-42d3-a456-426614174000"
BOOT_SHA = hashlib.sha256(BOOT_ID.encode("ascii")).hexdigest()
EPOCH = "1" * 32
POLICY = "2" * 64


def publication(
    *,
    sequence=1,
    source_generation=10,
    observed=9_000_000_000,
    source_observed=8_000_000_000,
    pressure=False,
    clear=True,
    reasons=(),
    epoch=EPOCH,
    observation_id=None,
):
    value = {
        "schema": 1,
        "boot_id_sha256": BOOT_SHA,
        "producer_epoch": epoch,
        "sequence": sequence,
        "observed_monotonic_ns": observed,
        "source_generation": source_generation,
        "source_observed_monotonic_ns": source_observed,
        "observation_id": observation_id or f"{sequence:064x}",
        "policy_sha256": POLICY,
        "pressure": pressure,
        "clear_eligible": clear,
        "reason_codes": list(reasons),
    }
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def snapshot(raw, **overrides):
    values = {
        "raw": raw,
        "parent_uid": 1000,
        "parent_mode": 0o700,
        "file_uid": 1000,
        "file_mode": 0o600,
    }
    values.update(overrides)
    return PrioritySignalSnapshot(**values)


def reader(raws, *, now=10_000_000_000, **kwargs):
    iterator = iter(raws)
    return PriorityPressureReader(
        "/run/subgen/priority.json",
        clock_ns=lambda: now,
        uid_reader=lambda: 1000,
        boot_id_reader=lambda: BOOT_ID + "\n",
        snapshot_reader=lambda _path: snapshot(next(iterator)),
        **kwargs,
    )


def test_disabled_and_configured_missing_are_distinct():
    disabled = PriorityPressureReader(None)
    missing = PriorityPressureReader(
        "/run/subgen/priority.json",
        clock_ns=lambda: 1,
        uid_reader=lambda: 1000,
        boot_id_reader=lambda: BOOT_ID,
        snapshot_reader=lambda _path: None,
    )

    assert disabled.read().state == "disabled"
    assert disabled.read().configured is False
    assert missing.read().state == "unavailable"
    assert missing.read().configured is True


def test_relative_and_noncanonical_paths_are_rejected():
    with pytest.raises(ValueError, match="absolute"):
        PriorityPressureReader("priority.json")
    with pytest.raises(ValueError, match="canonical"):
        PriorityPressureReader("/run/subgen/../priority.json")


@pytest.mark.parametrize(
    "override",
    [
        {"parent_uid": 1},
        {"file_uid": 1},
        {"parent_mode": 0o755},
        {"file_mode": 0o640},
        {"parent_is_directory": False},
        {"file_is_regular": False},
        {"stable_inode": False},
    ],
)
def test_unsafe_file_boundary_fails_closed(override):
    item = snapshot(publication(), **override)
    probe = PriorityPressureReader(
        "/run/subgen/priority.json",
        clock_ns=lambda: 10_000_000_000,
        uid_reader=lambda: 1000,
        boot_id_reader=lambda: BOOT_ID,
        snapshot_reader=lambda _path: item,
    )

    assert probe.read().state == "unavailable"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"x" * 4097,
        publication()[:-1],
        publication() + b"\n",
        publication().replace(b'"schema":1', b'"schema":1, "schema":1'),
        publication().replace(b'"schema":1', b'"schema":true'),
        publication().replace(b'"schema":1', b'"extra":1,"schema":1'),
    ],
)
def test_malformed_noncanonical_or_oversized_input_fails_closed(raw):
    assert reader([raw]).read().state == "unavailable"


def test_asserted_clear_and_neutral_truth_table():
    asserted = reader(
        [
            publication(
                pressure=True,
                clear=False,
                reasons=("higher_priority_busy",),
            )
        ]
    ).read()
    clear = reader([publication()]).read()
    neutral = reader([publication(clear=False)]).read()

    assert asserted.state == "asserted"
    assert asserted.reason_codes == ("higher_priority_busy",)
    assert clear.state == "clear"
    assert neutral.state == "neutral"


def test_observation_digest_hashes_ascii_identifier_not_decoded_hex():
    observation_id = "ab" * 32
    result = reader([publication(observation_id=observation_id)]).read()

    assert result.observation_digest == hashlib.sha256(
        observation_id.encode("ascii")
    ).hexdigest()


def test_exact_duplicate_is_noop_but_same_sequence_mutation_latches_unavailable():
    first = publication()
    changed = publication(pressure=True, clear=False, reasons=("policy_drift",))
    next_clear = publication(
        sequence=2,
        source_generation=11,
        source_observed=8_500_000_000,
    )
    probe = reader([first, first, changed, first, next_clear])

    assert probe.read().new_publication is True
    duplicate = probe.read()
    assert duplicate.state == "clear"
    assert duplicate.new_publication is False
    assert probe.read().state == "unavailable"
    assert probe.read().state == "unavailable"
    assert probe.read().state == "clear"


def test_sequence_gap_advances_checkpoint_and_next_exact_increment_recovers():
    gap_publication = publication(
        sequence=3,
        source_generation=11,
        source_observed=8_500_000_000,
    )
    probe = reader(
        [
            publication(sequence=1),
            gap_publication,
            gap_publication,
            publication(
                sequence=4,
                source_generation=12,
                source_observed=8_700_000_000,
            ),
        ]
    )

    assert probe.read().state == "clear"
    gap = probe.read()
    assert gap.state == "unavailable"
    assert gap.sequence_gap is True
    assert probe.read().state == "unavailable"
    recovered = probe.read()
    assert recovered.state == "clear"
    assert recovered.sequence == 4


def test_new_epoch_requires_sequence_one_and_is_marked_critical_for_controller():
    second_epoch = "3" * 32
    probe = reader(
        [
            publication(sequence=1),
            publication(sequence=2, epoch=second_epoch),
            publication(sequence=1, epoch=second_epoch),
        ]
    )

    assert probe.read().producer_epoch_changed is True
    assert probe.read().state == "unavailable"
    changed = probe.read()
    assert changed.state == "clear"
    assert changed.producer_epoch_changed is True


def test_previously_accepted_epoch_cannot_be_replayed_after_epoch_change():
    second_epoch = "3" * 32
    first_epoch = [
        publication(
            sequence=sequence,
            source_generation=9 + sequence,
            source_observed=7_800_000_000 + sequence * 200_000_000,
            observation_id=f"{100 + sequence:064x}",
        )
        for sequence in range(1, 5)
    ]
    asserted = publication(
        epoch=second_epoch,
        sequence=1,
        source_generation=20,
        source_observed=8_800_000_000,
        pressure=True,
        clear=False,
        reasons=("higher_priority_busy",),
        observation_id=f"{200:064x}",
    )
    second_epoch_next = publication(
        epoch=second_epoch,
        sequence=2,
        source_generation=21,
        source_observed=8_900_000_000,
        observation_id=f"{201:064x}",
    )
    probe = reader([*first_epoch, asserted, *first_epoch, second_epoch_next])

    originals = [probe.read() for _ in first_epoch]
    asserted_result = probe.read()
    replayed = [probe.read() for _ in first_epoch]
    recovered = probe.read()

    assert all(item.accepted for item in originals)
    assert asserted_result.state == "asserted"
    assert asserted_result.producer_epoch_changed is True
    assert all(item.state == "unavailable" for item in replayed)
    assert all(item.accepted is False for item in replayed)
    assert all(item.new_publication is False for item in replayed)
    assert all(item.producer_epoch == second_epoch for item in replayed)
    assert recovered.state == "clear"
    assert recovered.accepted is True
    assert recovered.sequence == 2


def test_epoch_history_saturation_is_terminal_and_fail_closed(monkeypatch):
    monkeypatch.setattr(priority_pressure, "MAX_TRACKED_PRODUCER_EPOCHS", 2)
    second_epoch = "3" * 32
    third_epoch = "4" * 32
    fourth_epoch = "5" * 32
    probe = reader(
        [
            publication(epoch=EPOCH),
            publication(epoch=second_epoch),
            publication(epoch=third_epoch),
            publication(
                epoch=second_epoch,
                sequence=2,
                source_generation=11,
                source_observed=8_500_000_000,
            ),
            publication(epoch=fourth_epoch),
        ]
    )

    assert probe.read().accepted is True
    assert probe.read().accepted is True
    saturated = probe.read()
    prior_epoch_next = probe.read()
    unseen_epoch = probe.read()

    assert saturated.state == "unavailable"
    assert prior_epoch_next.state == "unavailable"
    assert unseen_epoch.state == "unavailable"
    assert saturated.accepted is False
    assert prior_epoch_next.accepted is False
    assert unseen_epoch.accepted is False
    assert prior_epoch_next.producer_epoch == second_epoch
    assert unseen_epoch.producer_epoch == second_epoch


def test_reader_serializes_concurrent_reads():
    first_snapshot_entered = threading.Event()
    second_read_started = threading.Event()
    second_snapshot_entered = threading.Event()
    release_first = threading.Event()
    bookkeeping_lock = threading.Lock()
    active = 0
    maximum_active = 0
    calls = 0
    raws = [
        publication(sequence=1),
        publication(
            sequence=2,
            source_generation=11,
            source_observed=8_500_000_000,
        ),
    ]

    def snapshot_reader(_path):
        nonlocal active, maximum_active, calls
        with bookkeeping_lock:
            index = calls
            calls += 1
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            if index == 0:
                first_snapshot_entered.set()
                release_first.wait(timeout=5)
            else:
                second_snapshot_entered.set()
            return snapshot(raws[index])
        finally:
            with bookkeeping_lock:
                active -= 1

    probe = PriorityPressureReader(
        "/run/subgen/priority.json",
        clock_ns=lambda: 10_000_000_000,
        uid_reader=lambda: 1000,
        boot_id_reader=lambda: BOOT_ID,
        snapshot_reader=snapshot_reader,
    )

    def second_read():
        second_read_started.set()
        return probe.read()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(probe.read)
        second_future = None
        try:
            assert first_snapshot_entered.wait(timeout=2)
            second_future = executor.submit(second_read)
            assert second_read_started.wait(timeout=2)
            overlap_before_release = second_snapshot_entered.wait(timeout=0.5)
        finally:
            release_first.set()
        first = first_future.result(timeout=2)
        assert second_future is not None
        second = second_future.result(timeout=2)

    assert overlap_before_release is False
    assert maximum_active == 1
    assert [first.sequence, second.sequence] == [1, 2]
    assert first.accepted is True
    assert second.accepted is True


def test_source_regression_or_refreshed_duplicate_source_time_fails_closed():
    probe = reader(
        [
            publication(sequence=1),
            publication(sequence=2, source_generation=9),
            publication(sequence=2, source_generation=10, source_observed=9_000_000_000),
        ]
    )

    assert probe.read().state == "clear"
    assert probe.read().state == "unavailable"
    assert probe.read().state == "unavailable"


def test_wrong_boot_future_or_stale_publication_fails_closed():
    wrong = publication().replace(BOOT_SHA.encode(), b"f" * 64)
    future = publication(observed=11_000_000_000)
    stale = publication(observed=1, source_observed=1)

    assert reader([wrong]).read().state == "unavailable"
    assert reader([future]).read().state == "unavailable"
    assert reader([stale], now=50_000_000_000).read().state == "unavailable"


def test_last_accepted_metadata_is_retained_when_file_becomes_unavailable():
    raw = publication()
    snapshots = iter([snapshot(raw), None])
    times = iter([10_000_000_000, 15_000_000_000])
    probe = PriorityPressureReader(
        "/run/subgen/priority.json",
        clock_ns=lambda: next(times),
        uid_reader=lambda: 1000,
        boot_id_reader=lambda: BOOT_ID,
        snapshot_reader=lambda _path: next(snapshots),
    )

    accepted = probe.read()
    unavailable = probe.read()
    assert unavailable.state == "unavailable"
    assert unavailable.observation_digest == accepted.observation_digest
    assert unavailable.policy_sha256 == POLICY
    assert accepted.heartbeat_age_ms == 1_000
    assert accepted.source_age_ms == 2_000
    assert unavailable.heartbeat_age_ms == 6_000
    assert unavailable.source_age_ms == 7_000


def test_public_observation_never_contains_path_boot_id_or_observation_id():
    result = reader([publication()]).read()
    representation = repr(result)

    assert "/run/subgen" not in representation
    assert BOOT_ID not in representation
    assert f"{1:064x}" not in representation
