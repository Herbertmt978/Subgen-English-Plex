"""Durable, append-only recovery receipts for the Task 12 publisher."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .task12 import (
    ActionsBaseline,
    PublicationBlocked,
    PublicationCheckpoint,
    RegistryProbeReceipt,
    _strict_json_object,
    canonical_json_bytes,
)


_SCHEMA = "subgen.task12.publication-checkpoint/v3"
_HEAD_SCHEMA = "subgen.task12.publication-journal-head/v1"
_TRANSACTION_SCHEMA = "subgen.task12.publication-journal-transaction/v1"
_HEAD_NAME = ".journal-head.json"
_HEAD_NEXT_NAME = ".journal-head.next.json"
_HEAD_NEXT_STAGED_NAME = ".journal-head.next.staged.json"
_TRANSACTION_NAME = ".journal-append-transaction.json"
_TRANSACTION_STAGED_NAME = ".journal-append-transaction.staged.json"


def _blocked(code: str) -> None:
    raise PublicationBlocked(code)


class FileReceiptJournal:
    """Write checkpoints with a crash-recoverable terminal integrity anchor.

    The state directory and every receipt must be real, singly-linked filesystem
    objects.  A recovery never follows a symlink or silently replaces a receipt.
    Each committed head binds the newest sequence and receipt SHA-256.  A durable
    transaction containing the full next receipt lets recovery finish any crash
    before or after the atomic head advance without accepting an unanchored tail.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        if not self.directory.is_absolute():
            _blocked("receipt_directory_not_absolute")
        self._lock_descriptor: int | None = None
        self._directory_descriptor: int | None = None
        self._directory_parent_synced = False
        self._ensure_directory()

    def _ensure_directory(self, *, retain_descriptor: bool = False) -> int | None:
        if type(retain_descriptor) is not bool:
            raise TypeError("retain_descriptor must be a boolean")
        parent_descriptor = self._open_validated_directory_parent()
        directory_descriptor: int | None = None
        try:
            created = False
            try:
                if parent_descriptor is None:
                    self.directory.mkdir(
                        mode=0o700,
                        parents=False,
                        exist_ok=False,
                    )
                else:
                    os.mkdir(
                        self.directory.name,
                        mode=0o700,
                        dir_fd=parent_descriptor,
                    )
                created = True
            except FileExistsError:
                pass
            path_info = self.directory.lstat()
            resolved = self.directory.resolve(strict=True)
            if (
                not stat.S_ISDIR(path_info.st_mode)
                or stat.S_ISLNK(path_info.st_mode)
                or resolved != self.directory
            ):
                _blocked("receipt_directory_aliased")
            info = path_info
            if parent_descriptor is not None:
                directory_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                directory_descriptor = os.open(
                    self.directory.name,
                    directory_flags,
                    dir_fd=parent_descriptor,
                )
                info = os.fstat(directory_descriptor)
                if self._identity(info) != self._identity(path_info):
                    _blocked("receipt_directory_changed")
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                _blocked("receipt_directory_not_owned")
            try:
                if directory_descriptor is None:
                    os.chmod(self.directory, 0o700)
                else:
                    os.fchmod(directory_descriptor, 0o700)
            except OSError as exc:
                raise PublicationBlocked("receipt_directory_permissions") from exc
            if directory_descriptor is not None:
                info = os.fstat(directory_descriptor)
                current_path_info = self.directory.lstat()
                if self._identity(info) != self._identity(current_path_info):
                    _blocked("receipt_directory_changed")
            if (
                not stat.S_ISDIR(info.st_mode)
                or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
                or (os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o700)
            ):
                _blocked("receipt_directory_permissions")
            if directory_descriptor is not None:
                os.fsync(directory_descriptor)
            if created or not self._directory_parent_synced:
                self._fsync_created_directory_parent(
                    parent_descriptor,
                    directory_descriptor,
                )
                self._directory_parent_synced = True
            if retain_descriptor and directory_descriptor is not None:
                retained = directory_descriptor
                directory_descriptor = None
                return retained
            return None
        except OSError as exc:
            raise PublicationBlocked("receipt_directory_unavailable") from exc
        finally:
            if directory_descriptor is not None:
                os.close(directory_descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)

    def _open_validated_directory_parent(self) -> int | None:
        """Open the existing canonical parent used for leaf-only creation."""

        parent = self.directory.parent
        try:
            path_info = parent.lstat()
            resolved = parent.resolve(strict=True)
        except OSError as exc:
            raise PublicationBlocked("receipt_directory_parent_unavailable") from exc
        if (
            not stat.S_ISDIR(path_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or resolved != parent
        ):
            _blocked("receipt_directory_parent_aliased")
        if hasattr(os, "geteuid") and path_info.st_uid != os.geteuid():
            _blocked("receipt_directory_parent_not_owned")
        if os.name != "nt" and path_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            _blocked("receipt_directory_parent_permissions")
        if os.name == "nt":
            return None

        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(parent, flags)
            descriptor_info = os.fstat(descriptor)
            current_path_info = parent.lstat()
            if (
                not stat.S_ISDIR(descriptor_info.st_mode)
                or stat.S_ISLNK(current_path_info.st_mode)
                or self._identity(descriptor_info) != self._identity(path_info)
                or self._identity(descriptor_info) != self._identity(current_path_info)
                or (
                    hasattr(os, "geteuid")
                    and descriptor_info.st_uid != os.geteuid()
                )
                or (
                    os.name != "nt"
                    and descriptor_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                )
            ):
                _blocked("receipt_directory_parent_changed")
            return descriptor
        except PublicationBlocked:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise PublicationBlocked("receipt_directory_parent_unavailable") from exc

    def _fsync_created_directory_parent(
        self,
        parent_descriptor: int | None = None,
        directory_descriptor: int | None = None,
    ) -> None:
        if os.name == "nt":
            return
        parent = self.directory.parent
        descriptor = parent_descriptor
        owns_descriptor = False
        try:
            if descriptor is None:
                descriptor = self._open_validated_directory_parent()
                owns_descriptor = True
            if descriptor is None:
                _blocked("receipt_directory_parent_sync_failed")
            parent_info = os.fstat(descriptor)
            path_parent_info = parent.lstat()
            relative_child_info = os.stat(
                self.directory.name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            child_info = (
                os.fstat(directory_descriptor)
                if directory_descriptor is not None
                else relative_child_info
            )
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or stat.S_ISLNK(path_parent_info.st_mode)
                or self._identity(parent_info) != self._identity(path_parent_info)
                or self._identity(child_info) != self._identity(relative_child_info)
                or self._identity(child_info) != self._identity(self.directory.lstat())
            ):
                _blocked("receipt_directory_parent_changed")
            os.fsync(descriptor)
        except PublicationBlocked:
            raise
        except OSError as exc:
            raise PublicationBlocked("receipt_directory_parent_sync_failed") from exc
        finally:
            if owns_descriptor and descriptor is not None:
                os.close(descriptor)

    @contextmanager
    def exclusive(self) -> Iterator[FileReceiptJournal]:
        if self._lock_descriptor is not None:
            _blocked("receipt_lock_reentrant")
        directory_descriptor = self._ensure_directory(
            retain_descriptor=os.name != "nt"
        )
        lock_path = self.directory / ".publisher.lock"
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            if os.name == "nt":
                descriptor = os.open(lock_path, flags, 0o600)
            else:
                if directory_descriptor is None:
                    _blocked("receipt_directory_unavailable")
                directory_info = os.fstat(directory_descriptor)
                path_info = self.directory.lstat()
                if (
                    self._identity(directory_info) != self._identity(path_info)
                    or not stat.S_ISDIR(directory_info.st_mode)
                    or stat.S_IMODE(directory_info.st_mode) != 0o700
                    or (
                        hasattr(os, "geteuid")
                        and directory_info.st_uid != os.geteuid()
                    )
                ):
                    _blocked("receipt_directory_changed")
                descriptor = os.open(
                    lock_path.name,
                    flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
            path_lock_info = lock_path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
                or self._identity(info) != self._identity(path_lock_info)
            ):
                _blocked("receipt_lock_aliased")
            if os.name == "nt":
                import msvcrt

                if info.st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked_info = os.fstat(descriptor)
            locked_path_info = lock_path.lstat()
            if self._identity(locked_info) != self._identity(locked_path_info):
                _blocked("receipt_lock_changed")
        except (OSError, PublicationBlocked) as exc:
            try:
                os.close(descriptor)
            except (OSError, UnboundLocalError):
                pass
            try:
                if directory_descriptor is not None:
                    os.close(directory_descriptor)
            except (OSError, UnboundLocalError):
                pass
            if isinstance(exc, PublicationBlocked):
                raise
            raise PublicationBlocked("receipt_lock_unavailable") from exc
        self._lock_descriptor = descriptor
        self._directory_descriptor = directory_descriptor
        try:
            yield self
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                self._lock_descriptor = None
                self._directory_descriptor = None
                os.close(descriptor)
                if directory_descriptor is not None:
                    os.close(directory_descriptor)

    def _require_lock(self) -> None:
        if self._lock_descriptor is None:
            _blocked("receipt_lock_not_held")

    def _receipt_paths(self) -> list[Path]:
        self._require_lock()
        paths = sorted(self.directory.glob("checkpoint-*.json"))
        expected = [
            f"checkpoint-{index:08d}.json" for index in range(1, len(paths) + 1)
        ]
        if [path.name for path in paths] != expected:
            _blocked("receipt_sequence_invalid")
        for path in paths:
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_nlink != 1
            ):
                _blocked("receipt_file_aliased")
        return paths

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
        if os.name == "nt":
            return (info.st_dev, info.st_ino, info.st_size, 0, 0)
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def _read_owner_file(
        self,
        path: Path,
        *,
        invalid_code: str,
        alias_code: str,
        changed_code: str,
    ) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            if os.name == "nt" or self._directory_descriptor is None:
                descriptor = os.open(path, flags)
            else:
                descriptor = os.open(
                    path.name,
                    flags,
                    dir_fd=self._directory_descriptor,
                )
        except OSError as exc:
            raise PublicationBlocked(invalid_code) from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or (os.name != "nt" and stat.S_IMODE(before.st_mode) & 0o077)
                or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
            ):
                _blocked(alias_code)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
            path_after = path.lstat()
            if self._identity(before) != self._identity(after) or self._identity(
                after
            ) != self._identity(path_after):
                _blocked(changed_code)
            return b"".join(chunks)
        except OSError as exc:
            raise PublicationBlocked(invalid_code) from exc
        finally:
            os.close(descriptor)

    def _read_receipt(self, path: Path) -> bytes:
        return self._read_owner_file(
            path,
            invalid_code="receipt_invalid",
            alias_code="receipt_file_aliased",
            changed_code="receipt_changed_while_reading",
        )

    def _read_control_file(self, path: Path) -> bytes:
        return self._read_owner_file(
            path,
            invalid_code="receipt_head_invalid",
            alias_code="receipt_head_aliased",
            changed_code="receipt_head_changed_while_reading",
        )

    def _read_optional_control_file(self, name: str) -> bytes | None:
        path = self.directory / name
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PublicationBlocked("receipt_head_invalid") from exc
        return self._read_control_file(path)

    @staticmethod
    def _validate_probe_transition(
        previous: RegistryProbeReceipt | None,
        current: RegistryProbeReceipt | None,
        *,
        kind: str,
    ) -> None:
        stages = (
            ("planned", "seed_armed", "reject_armed", "verified")
            if kind == "create"
            else ("planned", "seed_armed", "cas_armed", "stale_armed", "verified")
        )
        if previous is None:
            if current is not None and current.stage != "planned":
                _blocked("receipt_probe_transition_invalid")
            return
        if current is None:
            _blocked("receipt_probe_transition_invalid")
        if (
            previous.kind != current.kind
            or previous.kind != kind
            or previous.token != current.token
            or previous.reference != current.reference
        ):
            _blocked("receipt_probe_identity_changed")
        previous_stage = stages.index(previous.stage)
        current_stage = stages.index(current.stage)
        if current_stage not in {previous_stage, previous_stage + 1}:
            _blocked("receipt_probe_transition_invalid")
        if (
            previous.prior_etag is not None
            and current.prior_etag != previous.prior_etag
        ):
            _blocked("receipt_probe_etag_changed")
        if previous.verification_sha256 is not None and (
            current.verification_sha256 != previous.verification_sha256
        ):
            _blocked("receipt_probe_verification_changed")
        if previous.winner_etag is not None and (
            current.winner_etag != previous.winner_etag
        ):
            _blocked("receipt_probe_winner_etag_changed")
        if (
            kind == "cas"
            and previous.prior_etag is None
            and (
                current.prior_etag is not None
                and not (
                    previous.stage == "seed_armed" and current.stage == "cas_armed"
                )
            )
        ):
            _blocked("receipt_probe_etag_transition_invalid")
        if (
            previous.winner_etag is None
            and current.winner_etag is not None
            and (
                current_stage != previous_stage + 1
                or not (
                    (
                        kind == "create"
                        and previous.stage == "seed_armed"
                        and current.stage == "reject_armed"
                    )
                    or (
                        kind == "cas"
                        and previous.stage == "cas_armed"
                        and current.stage == "stale_armed"
                    )
                )
            )
        ):
            _blocked("receipt_probe_winner_etag_transition_invalid")

    @staticmethod
    def _validate_initial_checkpoint(checkpoint: PublicationCheckpoint) -> None:
        """Require the journal to begin before any public mutation is armed."""

        if (
            checkpoint.phase != "prepared"
            or checkpoint.lock_object_sha is not None
            or checkpoint.lock_acquired
            or checkpoint.lock_removed
            or checkpoint.completed_writes
            or checkpoint.anonymous_smoke_sha256 is not None
            or checkpoint.version_create_probe is not None
            or checkpoint.latest_cas_probe is not None
            or checkpoint.latest_write_attempted
            or checkpoint.blob_upload_urls
            or checkpoint.failure_code is not None
        ):
            _blocked("receipt_initial_checkpoint_invalid")

    @classmethod
    def _validate_checkpoint_transition(
        cls,
        previous: PublicationCheckpoint,
        current: PublicationCheckpoint,
    ) -> None:
        cls._validate_probe_transition(
            previous.version_create_probe,
            current.version_create_probe,
            kind="create",
        )
        cls._validate_probe_transition(
            previous.latest_cas_probe,
            current.latest_cas_probe,
            kind="cas",
        )
        if previous.anonymous_smoke_sha256 is not None and (
            current.anonymous_smoke_sha256 != previous.anonymous_smoke_sha256
        ):
            _blocked("receipt_anonymous_smoke_changed")
        if previous.lock_object_sha is not None and (
            current.lock_object_sha != previous.lock_object_sha
        ):
            _blocked("receipt_lock_object_changed")
        if previous.lock_acquired and not current.lock_acquired:
            _blocked("receipt_lock_acquired_regressed")
        if previous.lock_removed and not current.lock_removed:
            _blocked("receipt_lock_removed_regressed")
        if previous.latest_write_attempted and not current.latest_write_attempted:
            _blocked("receipt_latest_write_attempted_regressed")
        if current.completed_writes[: len(previous.completed_writes)] != (
            previous.completed_writes
        ) or len(current.completed_writes) < len(previous.completed_writes):
            _blocked("receipt_completed_writes_regressed")
        for digest, upload_url in previous.blob_upload_urls.items():
            if current.blob_upload_urls.get(digest) != upload_url:
                _blocked("receipt_blob_upload_changed")
        if current.lock_removed and not current.lock_acquired:
            _blocked("receipt_lock_state_invalid")
        if current.lock_acquired and current.lock_object_sha is None:
            _blocked("receipt_lock_state_invalid")

    @staticmethod
    def _head_document(sequence: int, receipt_sha256: str) -> dict[str, Any]:
        return {
            "schema": _HEAD_SCHEMA,
            "receipt_sequence": sequence,
            "receipt_sha256": receipt_sha256,
        }

    @staticmethod
    def _decode_head(document: Any) -> tuple[int, str]:
        if not isinstance(document, dict) or set(document) != {
            "schema",
            "receipt_sequence",
            "receipt_sha256",
        }:
            _blocked("receipt_head_schema_invalid")
        sequence = document["receipt_sequence"]
        receipt_sha256 = document["receipt_sha256"]
        if (
            document["schema"] != _HEAD_SCHEMA
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
            or not isinstance(receipt_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", receipt_sha256) is None
        ):
            _blocked("receipt_head_schema_invalid")
        return sequence, receipt_sha256

    @classmethod
    def _canonical_head_payload(cls, document: Any) -> bytes:
        cls._decode_head(document)
        return canonical_json_bytes(document)

    def _read_head(self) -> tuple[dict[str, Any], bytes] | None:
        payload = self._read_optional_control_file(_HEAD_NAME)
        if payload is None:
            return None
        document = _strict_json_object(payload, "receipt_head")
        if canonical_json_bytes(document) != payload:
            _blocked("receipt_head_not_canonical")
        self._decode_head(document)
        return document, payload

    @classmethod
    def _validate_records_head(
        cls,
        records: list[tuple[PublicationCheckpoint, bytes]],
        head: dict[str, Any] | None,
    ) -> None:
        if not records:
            if head is not None:
                _blocked("receipt_head_state_invalid")
            return
        if head is None:
            _blocked("receipt_head_missing")
        sequence, receipt_sha256 = cls._decode_head(head)
        if (
            sequence != len(records)
            or receipt_sha256 != hashlib.sha256(records[-1][1]).hexdigest()
        ):
            _blocked("receipt_head_mismatch")

    def _load_record_chain(
        self,
        paths: list[Path],
    ) -> list[tuple[PublicationCheckpoint, bytes]]:
        records: list[tuple[PublicationCheckpoint, bytes]] = []
        previous_sha: str | None = None
        first_identity: tuple[Any, ...] | None = None
        previous_checkpoint: PublicationCheckpoint | None = None
        for sequence, path in enumerate(paths, start=1):
            payload = self._read_receipt(path)
            document = _strict_json_object(payload, "receipt")
            if canonical_json_bytes(document) != payload:
                _blocked("receipt_not_canonical")
            checkpoint = self._decode(document)
            if (
                checkpoint.receipt_sequence != sequence
                or checkpoint.previous_receipt_sha256 != previous_sha
            ):
                _blocked("receipt_chain_invalid")
            identity = (
                checkpoint.intent_sha256,
                checkpoint.run_token,
                checkpoint.lock_document_sha256,
                checkpoint.actions_baseline,
            )
            if first_identity is None:
                first_identity = identity
                self._validate_initial_checkpoint(checkpoint)
            elif identity != first_identity:
                _blocked("receipt_run_identity_changed")
            if previous_checkpoint is not None:
                self._validate_checkpoint_transition(previous_checkpoint, checkpoint)
            previous_sha = hashlib.sha256(payload).hexdigest()
            records.append((checkpoint, payload))
            previous_checkpoint = checkpoint
        return records

    @classmethod
    def _validate_next_checkpoint(
        cls,
        records: list[tuple[PublicationCheckpoint, bytes]],
        checkpoint: PublicationCheckpoint,
    ) -> None:
        if not records:
            cls._validate_initial_checkpoint(checkpoint)
            return
        previous = records[-1][0]
        if (
            previous.intent_sha256 != checkpoint.intent_sha256
            or previous.run_token != checkpoint.run_token
            or previous.lock_document_sha256 != checkpoint.lock_document_sha256
            or previous.actions_baseline != checkpoint.actions_baseline
        ):
            _blocked("receipt_run_identity_changed")
        cls._validate_checkpoint_transition(previous, checkpoint)

    @staticmethod
    def _receipt_staged_name(sequence: int) -> str:
        return f".checkpoint-{sequence:08d}.staged.json"

    def _write_staged_file(
        self,
        path: Path,
        payload: bytes,
        *,
        collision_code: str,
        write_code: str,
        changed_code: str,
    ) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            if os.name == "nt" or self._directory_descriptor is None:
                descriptor = os.open(path, flags, 0o600)
            else:
                descriptor = os.open(
                    path.name,
                    flags,
                    0o600,
                    dir_fd=self._directory_descriptor,
                )
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    written = stream.write(payload)
                    if written != len(payload):
                        _blocked(write_code)
                    stream.flush()
                    os.fsync(stream.fileno())
                    written_info = os.fstat(stream.fileno())
                path_info = path.lstat()
                if (
                    not stat.S_ISREG(written_info.st_mode)
                    or written_info.st_nlink != 1
                    or (os.name != "nt" and stat.S_IMODE(written_info.st_mode) & 0o077)
                    or (hasattr(os, "geteuid") and written_info.st_uid != os.geteuid())
                    or self._identity(written_info) != self._identity(path_info)
                ):
                    _blocked(changed_code)
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            self._fsync_directory()
        except FileExistsError as exc:
            raise PublicationBlocked(collision_code) from exc
        except OSError as exc:
            raise PublicationBlocked(write_code) from exc

    def _unlink_file(self, path: Path, *, write_code: str) -> None:
        try:
            if os.name == "nt" or self._directory_descriptor is None:
                path.unlink()
            else:
                os.unlink(path.name, dir_fd=self._directory_descriptor)
            self._fsync_directory()
        except OSError as exc:
            raise PublicationBlocked(write_code) from exc

    def _complete_link_publication(
        self,
        staged_path: Path,
        target_path: Path,
        *,
        mismatch_code: str,
        write_code: str,
    ) -> None:
        try:
            staged_info = staged_path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PublicationBlocked(write_code) from exc
        try:
            target_info = target_path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PublicationBlocked(write_code) from exc
        if (
            not stat.S_ISREG(staged_info.st_mode)
            or not stat.S_ISREG(target_info.st_mode)
            or staged_info.st_nlink != 2
            or target_info.st_nlink != 2
            or self._identity(staged_info) != self._identity(target_info)
            or (os.name != "nt" and stat.S_IMODE(staged_info.st_mode) & 0o077)
            or (hasattr(os, "geteuid") and staged_info.st_uid != os.geteuid())
        ):
            _blocked(mismatch_code)
        self._unlink_file(staged_path, write_code=write_code)

    def _publish_staged_no_replace(
        self,
        staged_path: Path,
        target_path: Path,
        *,
        collision_code: str,
        write_code: str,
    ) -> None:
        try:
            if os.name == "nt" or self._directory_descriptor is None:
                os.rename(staged_path, target_path)
            else:
                os.link(
                    staged_path.name,
                    target_path.name,
                    src_dir_fd=self._directory_descriptor,
                    dst_dir_fd=self._directory_descriptor,
                    follow_symlinks=False,
                )
            self._fsync_directory()
        except FileExistsError as exc:
            raise PublicationBlocked(collision_code) from exc
        except OSError as exc:
            raise PublicationBlocked(write_code) from exc
        if os.name != "nt":
            self._unlink_file(staged_path, write_code=write_code)

    def _read_optional_owner_file(
        self,
        path: Path,
        *,
        invalid_code: str,
        alias_code: str,
        changed_code: str,
    ) -> bytes | None:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PublicationBlocked(invalid_code) from exc
        return self._read_owner_file(
            path,
            invalid_code=invalid_code,
            alias_code=alias_code,
            changed_code=changed_code,
        )

    def _ensure_transaction_bound_file(
        self,
        staged_path: Path,
        target_path: Path,
        expected_payload: bytes,
        *,
        collision_code: str,
        mismatch_code: str,
        write_code: str,
        changed_code: str,
    ) -> None:
        self._complete_link_publication(
            staged_path,
            target_path,
            mismatch_code=mismatch_code,
            write_code=write_code,
        )
        staged_payload = self._read_optional_owner_file(
            staged_path,
            invalid_code=write_code,
            alias_code=changed_code,
            changed_code=changed_code,
        )
        target_payload = self._read_optional_owner_file(
            target_path,
            invalid_code=write_code,
            alias_code=changed_code,
            changed_code=changed_code,
        )
        if staged_payload is not None and target_payload is not None:
            _blocked(mismatch_code)
        if target_payload is not None:
            if target_payload == expected_payload:
                return
            if not (
                len(target_payload) < len(expected_payload)
                and expected_payload.startswith(target_payload)
            ):
                _blocked(mismatch_code)
            self._unlink_file(target_path, write_code=write_code)
            target_payload = None
        if staged_payload is not None and staged_payload != expected_payload:
            if not (
                len(staged_payload) < len(expected_payload)
                and expected_payload.startswith(staged_payload)
            ):
                _blocked(mismatch_code)
            self._unlink_file(staged_path, write_code=write_code)
            staged_payload = None
        if staged_payload is None and target_payload is None:
            self._write_staged_file(
                staged_path,
                expected_payload,
                collision_code=collision_code,
                write_code=write_code,
                changed_code=changed_code,
            )
        self._publish_staged_no_replace(
            staged_path,
            target_path,
            collision_code=collision_code,
            write_code=write_code,
        )
        if (
            self._read_optional_owner_file(
                target_path,
                invalid_code=write_code,
                alias_code=changed_code,
                changed_code=changed_code,
            )
            != expected_payload
        ):
            _blocked(mismatch_code)

    def _install_head(
        self,
        next_payload: bytes,
        expected_current_payload: bytes | None,
    ) -> None:
        current = self._read_head()
        current_payload = current[1] if current is not None else None
        if current_payload != expected_current_payload:
            _blocked("receipt_head_changed")
        next_path = self.directory / _HEAD_NEXT_NAME
        self._ensure_transaction_bound_file(
            self.directory / _HEAD_NEXT_STAGED_NAME,
            next_path,
            next_payload,
            collision_code="receipt_head_writer_collision",
            mismatch_code="receipt_head_transaction_mismatch",
            write_code="receipt_head_write_failed",
            changed_code="receipt_head_changed_while_writing",
        )
        current = self._read_head()
        current_payload = current[1] if current is not None else None
        if current_payload != expected_current_payload:
            _blocked("receipt_head_changed")
        try:
            if os.name == "nt" or self._directory_descriptor is None:
                os.replace(next_path, self.directory / _HEAD_NAME)
            else:
                os.replace(
                    next_path.name,
                    _HEAD_NAME,
                    src_dir_fd=self._directory_descriptor,
                    dst_dir_fd=self._directory_descriptor,
                )
            self._fsync_directory()
        except OSError as exc:
            raise PublicationBlocked("receipt_head_write_failed") from exc

    def _recover_transaction_staging(self) -> None:
        staged_path = self.directory / _TRANSACTION_STAGED_NAME
        target_path = self.directory / _TRANSACTION_NAME
        self._complete_link_publication(
            staged_path,
            target_path,
            mismatch_code="receipt_head_transaction_mismatch",
            write_code="receipt_head_write_failed",
        )
        staged_payload = self._read_optional_owner_file(
            staged_path,
            invalid_code="receipt_head_invalid",
            alias_code="receipt_head_aliased",
            changed_code="receipt_head_changed_while_reading",
        )
        if staged_payload is not None:
            if self._read_optional_control_file(_TRANSACTION_NAME) is not None:
                _blocked("receipt_head_transaction_mismatch")
            self._unlink_file(staged_path, write_code="receipt_head_write_failed")

    def _publish_transaction(self, payload: bytes) -> None:
        staged_path = self.directory / _TRANSACTION_STAGED_NAME
        target_path = self.directory / _TRANSACTION_NAME
        if (
            self._read_optional_owner_file(
                staged_path,
                invalid_code="receipt_head_invalid",
                alias_code="receipt_head_aliased",
                changed_code="receipt_head_changed_while_reading",
            )
            is not None
            or self._read_optional_control_file(_TRANSACTION_NAME) is not None
        ):
            _blocked("receipt_head_transaction_exists")
        self._write_staged_file(
            staged_path,
            payload,
            collision_code="receipt_head_transaction_exists",
            write_code="receipt_head_write_failed",
            changed_code="receipt_head_changed_while_writing",
        )
        self._publish_staged_no_replace(
            staged_path,
            target_path,
            collision_code="receipt_head_transaction_exists",
            write_code="receipt_head_write_failed",
        )

    def _clear_transaction(self, expected_payload: bytes) -> None:
        current = self._read_optional_control_file(_TRANSACTION_NAME)
        if current != expected_payload:
            _blocked("receipt_head_transaction_mismatch")
        try:
            if os.name == "nt" or self._directory_descriptor is None:
                (self.directory / _TRANSACTION_NAME).unlink()
            else:
                os.unlink(_TRANSACTION_NAME, dir_fd=self._directory_descriptor)
            self._fsync_directory()
        except OSError as exc:
            raise PublicationBlocked("receipt_head_write_failed") from exc

    def _recover_transaction(self) -> None:
        self._recover_transaction_staging()
        transaction_payload = self._read_optional_control_file(_TRANSACTION_NAME)
        if transaction_payload is None:
            if (
                self._read_optional_control_file(_HEAD_NEXT_NAME) is not None
                or self._read_optional_owner_file(
                    self.directory / _HEAD_NEXT_STAGED_NAME,
                    invalid_code="receipt_head_invalid",
                    alias_code="receipt_head_aliased",
                    changed_code="receipt_head_changed_while_reading",
                )
                is not None
                or any(self.directory.glob(".checkpoint-*.staged.json"))
            ):
                _blocked("receipt_head_transaction_missing")
            return
        transaction = _strict_json_object(
            transaction_payload,
            "receipt_head_transaction",
        )
        if canonical_json_bytes(transaction) != transaction_payload:
            _blocked("receipt_head_transaction_not_canonical")
        if not isinstance(transaction, dict) or set(transaction) != {
            "schema",
            "previous_head",
            "next_head",
            "receipt",
        }:
            _blocked("receipt_head_transaction_invalid")
        if transaction["schema"] != _TRANSACTION_SCHEMA:
            _blocked("receipt_head_transaction_invalid")
        previous_head = transaction["previous_head"]
        if previous_head is not None:
            self._decode_head(previous_head)
        next_head = transaction["next_head"]
        next_sequence, next_receipt_sha256 = self._decode_head(next_head)
        receipt_document = transaction["receipt"]
        receipt_payload = canonical_json_bytes(receipt_document)
        checkpoint = self._decode(receipt_document)
        if (
            checkpoint.receipt_sequence != next_sequence
            or hashlib.sha256(receipt_payload).hexdigest() != next_receipt_sha256
        ):
            _blocked("receipt_head_transaction_invalid")
        previous_payload = (
            self._canonical_head_payload(previous_head)
            if previous_head is not None
            else None
        )
        next_payload = self._canonical_head_payload(next_head)
        if previous_head is None:
            previous_sequence = 0
            previous_receipt_sha256 = None
        else:
            previous_sequence, previous_receipt_sha256 = self._decode_head(
                previous_head
            )
        if (
            next_sequence != previous_sequence + 1
            or checkpoint.previous_receipt_sha256 != previous_receipt_sha256
        ):
            _blocked("receipt_head_transaction_invalid")

        current_head = self._read_head()
        current_payload = current_head[1] if current_head is not None else None
        if current_payload not in {previous_payload, next_payload}:
            _blocked("receipt_head_transaction_mismatch")
        if previous_payload is not None and current_payload is None:
            _blocked("receipt_head_transaction_mismatch")

        target = self.directory / f"checkpoint-{next_sequence:08d}.json"
        staged_target = self.directory / self._receipt_staged_name(next_sequence)
        staged_receipts = sorted(self.directory.glob(".checkpoint-*.staged.json"))
        if staged_receipts not in ([], [staged_target]):
            _blocked("receipt_head_transaction_mismatch")
        self._complete_link_publication(
            staged_target,
            target,
            mismatch_code="receipt_head_transaction_mismatch",
            write_code="receipt_write_failed",
        )
        paths = self._receipt_paths()
        if len(paths) not in {previous_sequence, next_sequence}:
            _blocked("receipt_head_transaction_mismatch")
        prior_records = self._load_record_chain(paths[:previous_sequence])
        self._validate_records_head(prior_records, previous_head)
        self._validate_next_checkpoint(prior_records, checkpoint)
        self._ensure_transaction_bound_file(
            staged_target,
            target,
            receipt_payload,
            collision_code="receipt_writer_collision",
            mismatch_code="receipt_head_transaction_mismatch",
            write_code="receipt_write_failed",
            changed_code="receipt_changed_while_writing",
        )

        if current_payload == previous_payload:
            self._install_head(next_payload, previous_payload)
        elif (
            self._read_optional_control_file(_HEAD_NEXT_NAME) is not None
            or self._read_optional_owner_file(
                self.directory / _HEAD_NEXT_STAGED_NAME,
                invalid_code="receipt_head_invalid",
                alias_code="receipt_head_aliased",
                changed_code="receipt_head_changed_while_reading",
            )
            is not None
        ):
            _blocked("receipt_head_transaction_mismatch")
        self._clear_transaction(transaction_payload)

    def _load_all(self) -> list[tuple[PublicationCheckpoint, bytes]]:
        self._recover_transaction()
        records = self._load_record_chain(self._receipt_paths())
        head = self._read_head()
        self._validate_records_head(records, head[0] if head is not None else None)
        return records

    def append(self, checkpoint: PublicationCheckpoint) -> None:
        self._require_lock()
        self._ensure_directory()
        records = self._load_all()
        paths = self._receipt_paths()
        self._validate_next_checkpoint(records, checkpoint)
        checkpoint.receipt_sequence = len(records) + 1
        checkpoint.previous_receipt_sha256 = (
            hashlib.sha256(records[-1][1]).hexdigest() if records else None
        )
        target = self.directory / f"checkpoint-{len(paths) + 1:08d}.json"
        payload = canonical_json_bytes(checkpoint.snapshot())
        previous_head = (
            self._head_document(
                len(records), hashlib.sha256(records[-1][1]).hexdigest()
            )
            if records
            else None
        )
        next_head = self._head_document(
            checkpoint.receipt_sequence,
            hashlib.sha256(payload).hexdigest(),
        )
        transaction_payload = canonical_json_bytes(
            {
                "schema": _TRANSACTION_SCHEMA,
                "previous_head": previous_head,
                "next_head": next_head,
                "receipt": checkpoint.snapshot(),
            }
        )
        self._publish_transaction(transaction_payload)
        self._ensure_transaction_bound_file(
            self.directory / self._receipt_staged_name(checkpoint.receipt_sequence),
            target,
            payload,
            collision_code="receipt_writer_collision",
            mismatch_code="receipt_head_transaction_mismatch",
            write_code="receipt_write_failed",
            changed_code="receipt_changed_while_writing",
        )
        previous_head_payload = (
            canonical_json_bytes(previous_head) if previous_head is not None else None
        )
        self._install_head(canonical_json_bytes(next_head), previous_head_payload)
        self._clear_transaction(transaction_payload)

    def load_latest(self) -> PublicationCheckpoint | None:
        self._require_lock()
        self._ensure_directory()
        records = self._load_all()
        if not records:
            return None
        return records[-1][0]

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        if self._directory_descriptor is None:
            _blocked("receipt_lock_not_held")
        os.fsync(self._directory_descriptor)

    @staticmethod
    def _decode(document: Any) -> PublicationCheckpoint:
        expected = {
            "schema",
            "intent_sha256",
            "run_token",
            "actions_baseline",
            "lock_document_sha256",
            "phase",
            "lock_object_sha",
            "lock_acquired",
            "lock_removed",
            "completed_writes",
            "anonymous_smoke_sha256",
            "version_create_probe",
            "latest_cas_probe",
            "latest_write_attempted",
            "blob_upload_urls",
            "failure_code",
            "receipt_sequence",
            "previous_receipt_sha256",
        }
        if not isinstance(document, dict) or set(document) != expected:
            _blocked("receipt_schema_invalid")
        baseline = document["actions_baseline"]
        if not isinstance(baseline, dict) or set(baseline) != {"run_ids", "sha256"}:
            _blocked("receipt_baseline_invalid")
        run_ids = baseline["run_ids"]
        if not isinstance(run_ids, list):
            _blocked("receipt_baseline_invalid")

        def decode_probe(value: Any, kind: str) -> RegistryProbeReceipt | None:
            if value is None:
                return None
            if not isinstance(value, dict) or set(value) != {
                "kind",
                "token",
                "reference",
                "stage",
                "prior_etag",
                "winner_etag",
                "verification_sha256",
            }:
                _blocked("receipt_probe_invalid")
            receipt = RegistryProbeReceipt(
                kind=value["kind"],
                token=value["token"],
                reference=value["reference"],
                stage=value["stage"],
                prior_etag=value["prior_etag"],
                winner_etag=value["winner_etag"],
                verification_sha256=value["verification_sha256"],
            )
            winner_etag_required = (
                kind == "create" and receipt.stage in {"reject_armed", "verified"}
            ) or (kind == "cas" and receipt.stage in {"stale_armed", "verified"})
            if (
                receipt.kind != kind
                or not isinstance(receipt.token, str)
                or re.fullmatch(r"[0-9a-f]{64}", receipt.token) is None
                or not isinstance(receipt.reference, str)
                or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", receipt.reference)
                is None
                or receipt.stage
                not in (
                    {"planned", "seed_armed", "reject_armed", "verified"}
                    if kind == "create"
                    else {
                        "planned",
                        "seed_armed",
                        "cas_armed",
                        "stale_armed",
                        "verified",
                    }
                )
                or (
                    receipt.prior_etag is not None
                    and (
                        not isinstance(receipt.prior_etag, str)
                        or re.fullmatch(r'"[\x21\x23-\x7e]*"', receipt.prior_etag)
                        is None
                    )
                )
                or (kind == "create" and receipt.prior_etag is not None)
                or (
                    receipt.winner_etag is not None
                    and (
                        not isinstance(receipt.winner_etag, str)
                        or re.fullmatch(r'"[\x21\x23-\x7e]*"', receipt.winner_etag)
                        is None
                    )
                )
                or (
                    kind == "cas"
                    and (receipt.stage in {"cas_armed", "stale_armed", "verified"})
                    != (receipt.prior_etag is not None)
                )
                or (
                    receipt.verification_sha256 is not None
                    and (
                        not isinstance(receipt.verification_sha256, str)
                        or re.fullmatch(r"[0-9a-f]{64}", receipt.verification_sha256)
                        is None
                    )
                )
                or (receipt.stage == "verified")
                != (receipt.verification_sha256 is not None)
                or winner_etag_required != (receipt.winner_etag is not None)
            ):
                _blocked("receipt_probe_invalid")
            return receipt

        checkpoint = PublicationCheckpoint(
            intent_sha256=document["intent_sha256"],
            run_token=document["run_token"],
            actions_baseline=ActionsBaseline(tuple(run_ids), baseline["sha256"]),
            lock_document_sha256=document["lock_document_sha256"],
            phase=document["phase"],
            lock_object_sha=document["lock_object_sha"],
            lock_acquired=document["lock_acquired"],
            lock_removed=document["lock_removed"],
            completed_writes=document["completed_writes"],
            anonymous_smoke_sha256=document["anonymous_smoke_sha256"],
            version_create_probe=decode_probe(
                document["version_create_probe"],
                "create",
            ),
            latest_cas_probe=decode_probe(document["latest_cas_probe"], "cas"),
            latest_write_attempted=document["latest_write_attempted"],
            blob_upload_urls=document["blob_upload_urls"],
            failure_code=document["failure_code"],
            receipt_sequence=document["receipt_sequence"],
            previous_receipt_sha256=document["previous_receipt_sha256"],
        )
        if document["schema"] != _SCHEMA:
            _blocked("receipt_schema_invalid")
        if (
            not isinstance(checkpoint.completed_writes, list)
            or not all(isinstance(item, str) for item in checkpoint.completed_writes)
            or not isinstance(checkpoint.lock_acquired, bool)
            or not isinstance(checkpoint.lock_removed, bool)
            or not isinstance(checkpoint.latest_write_attempted, bool)
            or not isinstance(checkpoint.blob_upload_urls, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in checkpoint.blob_upload_urls.items()
            )
            or not isinstance(checkpoint.phase, str)
            or isinstance(checkpoint.receipt_sequence, bool)
            or not isinstance(checkpoint.receipt_sequence, int)
            or checkpoint.receipt_sequence <= 0
            or (
                checkpoint.previous_receipt_sha256 is not None
                and (
                    not isinstance(checkpoint.previous_receipt_sha256, str)
                    or len(checkpoint.previous_receipt_sha256) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in checkpoint.previous_receipt_sha256
                    )
                )
            )
        ):
            _blocked("receipt_field_invalid")
        checkpoint.actions_baseline.validate()
        return checkpoint
