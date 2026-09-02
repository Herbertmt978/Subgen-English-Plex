from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from release_tools.journal import FileReceiptJournal
from release_tools.task12 import (
    ActionsBaseline,
    PublicationBlocked,
    PublicationCheckpoint,
    canonical_json_bytes,
)


def _checkpoint(phase: str = "prepared") -> PublicationCheckpoint:
    return PublicationCheckpoint(
        intent_sha256="a" * 64,
        run_token="b" * 64,
        actions_baseline=ActionsBaseline.create((101, 202)),
        lock_document_sha256="c" * 64,
        phase=phase,
    )


def _append_two(journal: FileReceiptJournal) -> None:
    journal.append(_checkpoint())
    journal.append(_checkpoint("lock_create_pending"))


def _write_owner_only(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    if os.name != "nt":
        path.chmod(0o600)


def _transaction_payloads(directory: Path) -> tuple[bytes, bytes]:
    transaction = json.loads(
        (directory / ".journal-append-transaction.json").read_bytes()
    )
    return (
        canonical_json_bytes(transaction["receipt"]),
        canonical_json_bytes(transaction["next_head"]),
    )


def _leave_transaction_before_receipt(
    directory: Path,
    monkeypatch: Any,
) -> tuple[bytes, bytes]:
    journal = FileReceiptJournal(directory)
    ensure_file = journal._ensure_transaction_bound_file

    def crash_before_receipt(
        staged_path: Path,
        target_path: Path,
        payload: bytes,
        **codes: str,
    ) -> None:
        if target_path.name.startswith("checkpoint-"):
            raise PublicationBlocked("simulated_crash_before_receipt")
        ensure_file(staged_path, target_path, payload, **codes)

    monkeypatch.setattr(
        journal,
        "_ensure_transaction_bound_file",
        crash_before_receipt,
    )
    with (
        journal.exclusive(),
        pytest.raises(
            PublicationBlocked,
            match="simulated_crash_before_receipt",
        ),
    ):
        journal.append(_checkpoint())
    return _transaction_payloads(directory)


def _leave_transaction_before_head(
    directory: Path,
    monkeypatch: Any,
) -> tuple[bytes, bytes]:
    journal = FileReceiptJournal(directory)

    def crash_before_head(*_args: Any) -> None:
        raise PublicationBlocked("simulated_crash_before_head")

    monkeypatch.setattr(journal, "_install_head", crash_before_head)
    with (
        journal.exclusive(),
        pytest.raises(
            PublicationBlocked,
            match="simulated_crash_before_head",
        ),
    ):
        journal.append(_checkpoint())
    return _transaction_payloads(directory)


def test_empty_journal_has_no_head_and_loads_as_empty(tmp_path: Path) -> None:
    directory = tmp_path / "empty"
    journal = FileReceiptJournal(directory)

    with journal.exclusive():
        assert journal.load_latest() is None

    assert not (directory / ".journal-head.json").exists()


def test_head_is_canonical_owner_only_and_binds_tail_sequence_and_sha(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "bound-head"
    journal = FileReceiptJournal(directory)

    with journal.exclusive():
        _append_two(journal)

    head_payload = (directory / ".journal-head.json").read_bytes()
    head = json.loads(head_payload)
    tail_payload = (directory / "checkpoint-00000002.json").read_bytes()
    assert head_payload == canonical_json_bytes(head)
    assert head == {
        "receipt_sequence": 2,
        "receipt_sha256": hashlib.sha256(tail_payload).hexdigest(),
        "schema": "subgen.task12.publication-journal-head/v1",
    }
    if os.name != "nt":
        assert stat.S_IMODE((directory / ".journal-head.json").stat().st_mode) == 0o600


def test_head_rejects_rewritten_terminal_checkpoint(tmp_path: Path) -> None:
    directory = tmp_path / "rewritten-tail"
    journal = FileReceiptJournal(directory)

    with journal.exclusive():
        _append_two(journal)
        tail = directory / "checkpoint-00000002.json"
        document = json.loads(tail.read_bytes())
        document["phase"] = "attacker_selected_phase"
        tail.write_bytes(canonical_json_bytes(document))

        with pytest.raises(PublicationBlocked, match="receipt_head_mismatch"):
            journal.load_latest()


def test_head_rejects_truncated_terminal_checkpoint(tmp_path: Path) -> None:
    directory = tmp_path / "truncated-tail"
    journal = FileReceiptJournal(directory)

    with journal.exclusive():
        _append_two(journal)
        (directory / "checkpoint-00000002.json").unlink()

        with pytest.raises(PublicationBlocked, match="receipt_head_mismatch"):
            journal.load_latest()


def test_head_rejects_complete_checkpoint_truncation(tmp_path: Path) -> None:
    directory = tmp_path / "fully-truncated-tail"
    journal = FileReceiptJournal(directory)

    with journal.exclusive():
        journal.append(_checkpoint())
        (directory / "checkpoint-00000001.json").unlink()

        with pytest.raises(PublicationBlocked, match="receipt_head_state_invalid"):
            journal.load_latest()


def test_nonempty_journal_rejects_missing_or_noncanonical_head(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "invalid-head"
    journal = FileReceiptJournal(directory)

    with journal.exclusive():
        journal.append(_checkpoint())
        head = directory / ".journal-head.json"
        payload = head.read_bytes()
        head.unlink()
        with pytest.raises(PublicationBlocked, match="receipt_head_missing"):
            journal.load_latest()

        head.write_bytes(payload + b"\n")
        head.chmod(0o600)
        with pytest.raises(PublicationBlocked, match="receipt_head_not_canonical"):
            journal.load_latest()


def test_journal_rejects_hardlinked_head_alias(tmp_path: Path) -> None:
    directory = tmp_path / "aliased-head"
    journal = FileReceiptJournal(directory)

    with journal.exclusive():
        journal.append(_checkpoint())
        head = directory / ".journal-head.json"
        payload = head.read_bytes()
        head.unlink()
        alias_source = directory / "attacker-head.json"
        alias_source.write_bytes(payload)
        if os.name != "nt":
            alias_source.chmod(0o600)
        os.link(alias_source, head)

        with pytest.raises(PublicationBlocked, match="receipt_head_aliased"):
            journal.load_latest()


def test_torn_transaction_staging_is_discarded_before_remote_progress(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "torn-transaction-stage"
    journal = FileReceiptJournal(directory)
    staged = directory / ".journal-append-transaction.staged.json"
    _write_owner_only(staged, b'{"schema":"torn')

    with journal.exclusive():
        assert journal.load_latest() is None

    assert not staged.exists()


def test_torn_published_transaction_without_binding_fails_closed(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "torn-published-transaction"
    journal = FileReceiptJournal(directory)
    transaction = directory / ".journal-append-transaction.json"
    payload = b'{"schema":"torn'
    _write_owner_only(transaction, payload)

    with journal.exclusive(), pytest.raises(PublicationBlocked):
        journal.load_latest()

    assert transaction.read_bytes() == payload


def test_transaction_staging_alias_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "aliased-transaction-stage"
    journal = FileReceiptJournal(directory)
    alias_source = directory / "alias-source"
    staged = directory / ".journal-append-transaction.staged.json"
    _write_owner_only(alias_source, b"torn")
    os.link(alias_source, staged)

    with (
        journal.exclusive(),
        pytest.raises(
            PublicationBlocked,
            match="receipt_head_aliased",
        ),
    ):
        journal.load_latest()


@pytest.mark.parametrize(
    "name",
    ["checkpoint-00000001.json", ".checkpoint-00000001.staged.json"],
)
def test_transaction_repairs_strict_prefix_torn_receipt(
    tmp_path: Path,
    monkeypatch: Any,
    name: str,
) -> None:
    directory = tmp_path / f"torn-receipt-{name[0] == '.'}"
    receipt_payload, _ = _leave_transaction_before_receipt(directory, monkeypatch)
    _write_owner_only(directory / name, receipt_payload[: len(receipt_payload) // 2])

    recovered = FileReceiptJournal(directory)
    with recovered.exclusive():
        latest = recovered.load_latest()

    assert latest is not None
    assert (directory / "checkpoint-00000001.json").read_bytes() == receipt_payload
    assert not (directory / ".checkpoint-00000001.staged.json").exists()


def test_transaction_recovers_link_published_receipt_before_stage_cleanup(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    directory = tmp_path / "linked-receipt"
    receipt_payload, _ = _leave_transaction_before_receipt(directory, monkeypatch)
    staged = directory / ".checkpoint-00000001.staged.json"
    target = directory / "checkpoint-00000001.json"
    _write_owner_only(staged, receipt_payload)
    os.link(staged, target)

    recovered = FileReceiptJournal(directory)
    with recovered.exclusive():
        latest = recovered.load_latest()

    assert latest is not None
    assert target.read_bytes() == receipt_payload
    assert target.stat().st_nlink == 1
    assert not staged.exists()


@pytest.mark.parametrize(
    "name",
    [".journal-head.next.json", ".journal-head.next.staged.json"],
)
def test_transaction_repairs_strict_prefix_torn_head_next(
    tmp_path: Path,
    monkeypatch: Any,
    name: str,
) -> None:
    directory = tmp_path / f"torn-head-next-{name.endswith('staged.json')}"
    _, next_head_payload = _leave_transaction_before_head(directory, monkeypatch)
    _write_owner_only(
        directory / name,
        next_head_payload[: len(next_head_payload) // 2],
    )

    recovered = FileReceiptJournal(directory)
    with recovered.exclusive():
        latest = recovered.load_latest()

    assert latest is not None
    assert (directory / ".journal-head.json").read_bytes() == next_head_payload
    assert not (directory / ".journal-head.next.json").exists()
    assert not (directory / ".journal-head.next.staged.json").exists()


def test_foreign_receipt_collision_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    directory = tmp_path / "foreign-receipt"
    _leave_transaction_before_receipt(directory, monkeypatch)
    target = directory / "checkpoint-00000001.json"
    foreign_payload = b"foreign-not-a-receipt"
    _write_owner_only(target, foreign_payload)

    recovered = FileReceiptJournal(directory)
    with (
        recovered.exclusive(),
        pytest.raises(
            PublicationBlocked,
            match="receipt_head_transaction_mismatch",
        ),
    ):
        recovered.load_latest()

    assert target.read_bytes() == foreign_payload


def test_atomic_publication_refuses_to_overwrite_collision(tmp_path: Path) -> None:
    directory = tmp_path / "atomic-no-replace"
    journal = FileReceiptJournal(directory)
    staged = directory / ".staged"
    target = directory / "target"
    foreign_payload = b"foreign"

    with journal.exclusive():
        journal._write_staged_file(
            staged,
            b"expected",
            collision_code="test_collision",
            write_code="test_write_failed",
            changed_code="test_changed",
        )
        _write_owner_only(target, foreign_payload)
        with pytest.raises(PublicationBlocked, match="test_collision"):
            journal._publish_staged_no_replace(
                staged,
                target,
                collision_code="test_collision",
                write_code="test_write_failed",
            )

    assert target.read_bytes() == foreign_payload
    assert staged.read_bytes() == b"expected"


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only mode proof")
def test_published_journal_files_remain_owner_only(tmp_path: Path) -> None:
    directory = tmp_path / "owner-only-publication"
    journal = FileReceiptJournal(directory)

    with journal.exclusive():
        journal.append(_checkpoint())

    for name in (
        ".journal-head.json",
        "checkpoint-00000001.json",
        ".publisher.lock",
    ):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert not list(directory.glob("*.staged.json"))


def test_transaction_recovers_crash_after_intent_before_receipt(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    directory = tmp_path / "crash-before-receipt"
    journal = FileReceiptJournal(directory)
    ensure_file = journal._ensure_transaction_bound_file

    def crash_before_receipt(
        staged_path: Path,
        target_path: Path,
        payload: bytes,
        **codes: str,
    ) -> None:
        if target_path.name.startswith("checkpoint-"):
            raise PublicationBlocked("simulated_crash_before_receipt")
        ensure_file(staged_path, target_path, payload, **codes)

    monkeypatch.setattr(
        journal,
        "_ensure_transaction_bound_file",
        crash_before_receipt,
    )
    with (
        journal.exclusive(),
        pytest.raises(
            PublicationBlocked,
            match="simulated_crash_before_receipt",
        ),
    ):
        journal.append(_checkpoint())

    recovered = FileReceiptJournal(directory)
    with recovered.exclusive():
        latest = recovered.load_latest()

    assert latest is not None
    assert latest.receipt_sequence == 1
    assert (directory / "checkpoint-00000001.json").exists()
    assert not (directory / ".journal-append-transaction.json").exists()


def test_transaction_recovers_crash_after_receipt_before_head(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    directory = tmp_path / "crash-before-head"
    journal = FileReceiptJournal(directory)

    def crash_before_head(*_args: Any) -> None:
        raise PublicationBlocked("simulated_crash_before_head")

    monkeypatch.setattr(journal, "_install_head", crash_before_head)
    with (
        journal.exclusive(),
        pytest.raises(
            PublicationBlocked,
            match="simulated_crash_before_head",
        ),
    ):
        journal.append(_checkpoint())

    recovered = FileReceiptJournal(directory)
    with recovered.exclusive():
        latest = recovered.load_latest()

    assert latest is not None
    assert latest.receipt_sequence == 1
    assert not (directory / ".journal-append-transaction.json").exists()


def test_transaction_recovers_crash_after_head_before_commit_cleanup(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    directory = tmp_path / "crash-after-head"
    journal = FileReceiptJournal(directory)

    def crash_before_cleanup(*_args: Any) -> None:
        raise PublicationBlocked("simulated_crash_before_cleanup")

    monkeypatch.setattr(journal, "_clear_transaction", crash_before_cleanup)
    with (
        journal.exclusive(),
        pytest.raises(
            PublicationBlocked,
            match="simulated_crash_before_cleanup",
        ),
    ):
        journal.append(_checkpoint())

    recovered = FileReceiptJournal(directory)
    with recovered.exclusive():
        latest = recovered.load_latest()

    assert latest is not None
    assert latest.receipt_sequence == 1
    assert not (directory / ".journal-append-transaction.json").exists()
