"""Fail-closed filesystem helpers for Subgen's host operational scripts.

The destructive helper deliberately uses Linux descriptor-relative operations.
Once the media root and each parent directory have been opened without following
symbolic links, a later pathname retarget cannot redirect the final unlink.
"""

from __future__ import annotations

import os
import secrets
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, NamedTuple


class UnsafePathError(ValueError):
    """Raised when a filesystem operation cannot be proven safe."""


class CandidateUnavailableError(UnsafePathError):
    """Raised when the candidate or one of its parents no longer exists."""


class DeleteRecoveryRequiredError(UnsafePathError):
    """Raised when a durable intent must retain its token for safe recovery."""


class FileIdentity(NamedTuple):
    """Immutable file-generation fingerprint that JSON encodes as a list."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


PathValue = str | os.PathLike[str]
IdentityValue = FileIdentity | Mapping[str, int] | Sequence[int]

_IS_LINUX = sys.platform.startswith("linux")
_HAS_REQUIRED_PRIMITIVES = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.unlink in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.link in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.rmdir in os.supports_dir_fd
    and os.listdir in os.supports_fd
)

__all__ = [
    "FileIdentity",
    "CandidateUnavailableError",
    "DeleteRecoveryRequiredError",
    "UnsafePathError",
    "exact_path_key",
    "file_identity",
    "lexical_host_path",
    "new_delete_token",
    "prepare_private_state_directory",
    "secure_unlink_regular_beneath",
    "supports_secure_unlink",
    "validate_regular_file_beneath",
]


def supports_secure_unlink() -> bool:
    """Return whether the required Linux dir-fd primitives are available."""

    return _IS_LINUX and _HAS_REQUIRED_PRIMITIVES


def _checked_raw_path(path: PathValue) -> str:
    try:
        raw_path = os.fspath(path)
    except TypeError as exc:
        raise UnsafePathError("Refusing a non-path value") from exc
    if not isinstance(raw_path, str):
        raise UnsafePathError("Refusing a non-text host path")
    if "\0" in raw_path:
        raise UnsafePathError("Refusing host path containing NUL")
    if ".." in Path(raw_path).parts:
        raise UnsafePathError("Refusing host path with parent traversal")
    return raw_path


def lexical_host_path(path: PathValue) -> Path:
    """Return an absolute normalized path without resolving symbolic links."""

    raw_path = _checked_raw_path(path)
    return Path(os.path.abspath(os.path.normpath(raw_path)))


def exact_path_key(path: PathValue) -> str:
    """Return a lexical absolute path key without case folding."""

    return os.fspath(lexical_host_path(path))


def prepare_private_state_directory(path: PathValue) -> Path:
    """Create or validate a service-owned state directory without following its leaf."""

    directory = lexical_host_path(path)
    if _IS_LINUX:
        try:
            resolved_parent = directory.parent.resolve(strict=True)
        except OSError as exc:
            raise UnsafePathError(
                "State directory parent must already exist and be safe"
            ) from exc
        if resolved_parent != directory.parent:
            raise UnsafePathError(
                "State directory must not pass through symbolic links"
            )
    try:
        directory_stat = os.lstat(directory)
    except FileNotFoundError:
        try:
            directory.mkdir(parents=True, mode=0o700)
            directory_stat = os.lstat(directory)
        except OSError as exc:
            raise UnsafePathError("Could not create private state directory") from exc
    except OSError as exc:
        raise UnsafePathError("Could not inspect private state directory") from exc

    if not stat.S_ISDIR(directory_stat.st_mode):
        raise UnsafePathError("State directory must be a real directory, not a link")
    if _IS_LINUX:
        try:
            if directory.resolve(strict=True) != directory:
                raise UnsafePathError(
                    "State directory must not pass through symbolic links"
                )
        except OSError as exc:
            raise UnsafePathError("Could not resolve private state directory") from exc
    if _IS_LINUX and (
        directory_stat.st_uid != os.geteuid()
        or stat.S_IMODE(directory_stat.st_mode) & 0o022
    ):
        raise UnsafePathError(
            "State directory must be service-owned and not group/world writable"
        )
    return directory


def file_identity(stat_result: os.stat_result) -> FileIdentity:
    """Build an immutable, JSON-serializable identity from a stat result."""

    try:
        return FileIdentity(
            int(stat_result.st_dev),
            int(stat_result.st_ino),
            int(stat_result.st_size),
            int(stat_result.st_mtime_ns),
            int(stat_result.st_ctime_ns),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise UnsafePathError("Invalid filesystem identity source") from exc


def _expected_file_identity(value: IdentityValue | None) -> FileIdentity | None:
    if value is None:
        return None
    try:
        if isinstance(value, Mapping):
            device = value["device"]
            inode = value["inode"]
            size = value["size"]
            mtime_ns = value["mtime_ns"]
            ctime_ns = value["ctime_ns"]
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) != 5:
                raise ValueError
            device, inode, size, mtime_ns, ctime_ns = value
        else:
            raise TypeError
        return FileIdentity(
            int(device),
            int(inode),
            int(size),
            int(mtime_ns),
            int(ctime_ns),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise UnsafePathError("Invalid expected file identity") from exc


def _expected_file_size(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UnsafePathError("Invalid expected file size")
    return value


def new_delete_token() -> str:
    """Return an unpredictable token suitable for a durable delete intent."""

    return secrets.token_hex(16)


def _checked_delete_token(value: str | None) -> str:
    token = value if value is not None else new_delete_token()
    if (
        not isinstance(token, str)
        or len(token) != 32
        or any(character not in "0123456789abcdef" for character in token)
    ):
        raise UnsafePathError("Invalid delete operation token")
    return token


def _operation_path_parts(
    media_root: PathValue, candidate: PathValue
) -> tuple[Path, tuple[str, ...], str]:
    root_path = lexical_host_path(media_root)
    raw_candidate = _checked_raw_path(candidate)
    candidate_path = Path(raw_candidate)
    if not candidate_path.is_absolute():
        candidate_path = root_path / candidate_path
    candidate_path = Path(os.path.abspath(os.path.normpath(os.fspath(candidate_path))))
    try:
        relative_path = candidate_path.relative_to(root_path)
    except ValueError as exc:
        raise UnsafePathError(
            f"Refusing candidate outside media root: {candidate_path}"
        ) from exc
    if not relative_path.parts:
        raise UnsafePathError("Expected a regular file beneath the media root")
    return root_path, tuple(relative_path.parts[:-1]), relative_path.parts[-1]


def _require_secure_unlink_support() -> None:
    if not supports_secure_unlink():
        raise UnsafePathError(
            "Secure descriptor-relative unlink is not supported on this platform"
        )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


@contextmanager
def _pinned_parent_directory(
    root_path: Path, parent_parts: tuple[str, ...]
) -> Iterator[int]:
    descriptors: list[int] = []
    try:
        try:
            current_descriptor = os.open(root_path, _directory_open_flags())
        except FileNotFoundError as exc:
            raise CandidateUnavailableError("Media root is unavailable") from exc
        except (OSError, TypeError, NotImplementedError) as exc:
            raise UnsafePathError(f"Refusing unsafe media root: {root_path}") from exc
        descriptors.append(current_descriptor)

        for part in parent_parts:
            try:
                current_descriptor = os.open(
                    part,
                    _directory_open_flags(),
                    dir_fd=current_descriptor,
                )
            except FileNotFoundError as exc:
                raise CandidateUnavailableError(
                    "Candidate parent is unavailable"
                ) from exc
            except (OSError, TypeError, NotImplementedError) as exc:
                raise UnsafePathError(
                    f"Refusing unsafe path component beneath media root: {part}"
                ) from exc
            descriptors.append(current_descriptor)
        yield current_descriptor
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validated_leaf_identity(
    parent_descriptor: int,
    leaf_name: str,
    expected_identity: IdentityValue | None,
    expected_size: int | None,
) -> FileIdentity:
    wanted_identity = _expected_file_identity(expected_identity)
    wanted_size = _expected_file_size(expected_size)
    try:
        candidate_stat = os.stat(
            leaf_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as exc:
        raise CandidateUnavailableError("Candidate is unavailable") from exc
    except (OSError, TypeError, NotImplementedError) as exc:
        raise UnsafePathError("Refusing unsafe path: candidate is unavailable") from exc

    if not stat.S_ISREG(candidate_stat.st_mode):
        raise UnsafePathError("Refusing to operate on anything except a regular file")

    current_identity = file_identity(candidate_stat)
    if wanted_identity is not None and current_identity != wanted_identity:
        raise UnsafePathError("Refusing file because its identity changed")
    if wanted_size is not None and candidate_stat.st_size != wanted_size:
        raise UnsafePathError("Refusing file because its size changed")
    return current_identity


def _validated_quarantined_identity(
    quarantine_descriptor: int,
    expected_identity: IdentityValue,
    expected_size: int | None,
) -> FileIdentity:
    """Validate a moved candidate, allowing only rename-induced ctime change."""

    wanted_identity = _expected_file_identity(expected_identity)
    if wanted_identity is None:
        raise UnsafePathError("Quarantined candidate lacked a durable identity")
    current_identity = _validated_leaf_identity(
        quarantine_descriptor,
        "candidate",
        expected_identity=None,
        expected_size=expected_size,
    )
    stable_current = (
        current_identity.device,
        current_identity.inode,
        current_identity.size,
        current_identity.mtime_ns,
    )
    stable_wanted = (
        wanted_identity.device,
        wanted_identity.inode,
        wanted_identity.size,
        wanted_identity.mtime_ns,
    )
    if stable_current != stable_wanted or current_identity.ctime_ns < wanted_identity.ctime_ns:
        raise UnsafePathError("Refusing file because its identity changed")
    return wanted_identity


def validate_regular_file_beneath(
    media_root: PathValue,
    candidate: PathValue,
    expected_identity: IdentityValue | None = None,
    expected_size: int | None = None,
) -> FileIdentity:
    """Validate a regular file through a symlink-free, pinned directory chain."""

    _require_secure_unlink_support()
    root_path, parent_parts, leaf_name = _operation_path_parts(media_root, candidate)
    with _pinned_parent_directory(root_path, parent_parts) as parent_descriptor:
        return _validated_leaf_identity(
            parent_descriptor,
            leaf_name,
            expected_identity,
            expected_size,
        )


def _entry_exists(directory_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise UnsafePathError(f"Could not inspect private delete path: {name}") from exc
    return True


def _open_private_delete_directory(
    parent_descriptor: int,
    directory_name: str,
    *,
    create: bool,
) -> int:
    if create:
        try:
            os.mkdir(directory_name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        except OSError as exc:
            raise UnsafePathError("Could not create private delete quarantine") from exc
        else:
            try:
                os.fsync(parent_descriptor)
            except OSError as exc:
                raise UnsafePathError(
                    "Could not persist private delete quarantine"
                ) from exc
    try:
        descriptor = os.open(
            directory_name,
            _directory_open_flags(),
            dir_fd=parent_descriptor,
        )
    except FileNotFoundError as exc:
        raise CandidateUnavailableError("Delete quarantine is unavailable") from exc
    except OSError as exc:
        raise UnsafePathError("Refusing unsafe delete quarantine") from exc

    try:
        directory_stat = os.fstat(descriptor)
        # NFS setgid parents may add special bits; they do not grant access.
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.geteuid()
            or (stat.S_IMODE(directory_stat.st_mode) & 0o777) != 0o700
        ):
            raise UnsafePathError(
                "Delete quarantine must be a private directory owned by the service user"
            )
        entries = set(os.listdir(descriptor))
        if not entries.issubset({"candidate", "tombstone"}):
            raise UnsafePathError("Delete quarantine contains unexpected entries")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _restore_quarantined_candidate(
    quarantine_descriptor: int,
    parent_descriptor: int,
    leaf_name: str,
    quarantine_name: str,
) -> None:
    try:
        os.link(
            "candidate",
            leaf_name,
            src_dir_fd=quarantine_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise UnsafePathError(
            "Candidate changed during deletion; the raced file was preserved in "
            f"{quarantine_name}/candidate"
        ) from exc
    except OSError as exc:
        raise UnsafePathError(
            "Could not restore the quarantined candidate; it remains in "
            f"{quarantine_name}/candidate"
        ) from exc
    try:
        os.unlink("candidate", dir_fd=quarantine_descriptor)
    except OSError as exc:
        raise UnsafePathError(
            "Candidate was restored, but its private quarantine hardlink remains in "
            f"{quarantine_name}/candidate"
        ) from exc
    try:
        os.fsync(parent_descriptor)
        os.fsync(quarantine_descriptor)
    except OSError as exc:
        raise DeleteRecoveryRequiredError(
            "Candidate restore requires recovery to confirm directory durability"
        ) from exc


def _ensure_delete_tombstone(quarantine_descriptor: int) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(
            "tombstone",
            flags,
            0o600,
            dir_fd=quarantine_descriptor,
        )
    except FileExistsError:
        try:
            marker_stat = os.stat(
                "tombstone",
                dir_fd=quarantine_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise UnsafePathError("Could not validate delete tombstone") from exc
        if not stat.S_ISREG(marker_stat.st_mode) or marker_stat.st_size != 0:
            raise UnsafePathError("Refusing invalid delete tombstone")
    except OSError as exc:
        raise UnsafePathError("Could not create delete tombstone") from exc
    else:
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    try:
        os.fsync(quarantine_descriptor)
    except OSError as exc:
        raise UnsafePathError("Could not persist delete tombstone") from exc


def _complete_private_delete(
    parent_descriptor: int,
    quarantine_descriptor: int,
    active_name: str,
    completed_name: str,
) -> None:
    if _entry_exists(parent_descriptor, completed_name):
        return
    try:
        os.rename(
            active_name,
            completed_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
    except OSError:
        # The tombstone remains in the active directory. The media candidate
        # has already been removed, so controller state can safely finalize.
        return

    try:
        os.fsync(parent_descriptor)
    except OSError:
        # The controller still has a durable intent. If the process or host
        # stops here, recovery accepts either the active tombstone or the
        # completed directory and never follows a replacement leaf.
        pass

    try:
        if _entry_exists(quarantine_descriptor, "tombstone"):
            os.unlink("tombstone", dir_fd=quarantine_descriptor)
        os.fsync(quarantine_descriptor)
    except OSError:
        return

    try:
        os.rmdir(completed_name, dir_fd=parent_descriptor)
    except OSError:
        pass


def _recover_completed_delete(
    parent_descriptor: int,
    completed_name: str,
    expected_identity: IdentityValue | None,
) -> FileIdentity:
    wanted_identity = _expected_file_identity(expected_identity)
    if wanted_identity is None:
        raise UnsafePathError("Completed deletion lacked a durable file identity")
    descriptor = _open_private_delete_directory(
        parent_descriptor,
        completed_name,
        create=False,
    )
    try:
        if _entry_exists(descriptor, "candidate"):
            raise UnsafePathError("Completed delete quarantine still contains a candidate")
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise DeleteRecoveryRequiredError(
                "Completed quarantine could not prove candidate absence"
            ) from exc
        if _entry_exists(descriptor, "tombstone"):
            try:
                os.unlink("tombstone", dir_fd=descriptor)
            except OSError:
                pass
    finally:
        os.close(descriptor)
    try:
        os.rmdir(completed_name, dir_fd=parent_descriptor)
    except OSError:
        pass
    return wanted_identity


def secure_unlink_regular_beneath(
    media_root: PathValue,
    candidate: PathValue,
    expected_identity: IdentityValue | None = None,
    expected_size: int | None = None,
    operation_token: str | None = None,
) -> FileIdentity:
    """Quarantine, revalidate, and unlink one regular file beneath a media root."""

    _require_secure_unlink_support()
    token = _checked_delete_token(operation_token)
    active_name = f".subgen-delete-{token}"
    completed_name = f".subgen-deleted-{token}"
    root_path, parent_parts, leaf_name = _operation_path_parts(media_root, candidate)
    with _pinned_parent_directory(root_path, parent_parts) as parent_descriptor:
        if _entry_exists(parent_descriptor, completed_name):
            return _recover_completed_delete(
                parent_descriptor,
                completed_name,
                expected_identity,
            )

        quarantine_descriptor = _open_private_delete_directory(
            parent_descriptor,
            active_name,
            create=True,
        )
        try:
            quarantined = _entry_exists(quarantine_descriptor, "candidate")
            tombstone = _entry_exists(quarantine_descriptor, "tombstone")
            if tombstone and not quarantined:
                current_identity = _expected_file_identity(expected_identity)
                if current_identity is None:
                    raise UnsafePathError(
                        "Completed deletion lacked a durable file identity"
                    )
                try:
                    os.fsync(quarantine_descriptor)
                except OSError as exc:
                    raise DeleteRecoveryRequiredError(
                        "Tombstone recovery could not prove candidate absence"
                    ) from exc
                _complete_private_delete(
                    parent_descriptor,
                    quarantine_descriptor,
                    active_name,
                    completed_name,
                )
                return current_identity

            if quarantined:
                current_identity = _validated_quarantined_identity(
                    quarantine_descriptor,
                    expected_identity,
                    expected_size,
                )
            else:
                current_identity = _validated_leaf_identity(
                    parent_descriptor,
                    leaf_name,
                    expected_identity,
                    expected_size,
                )
                try:
                    os.rename(
                        leaf_name,
                        "candidate",
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=quarantine_descriptor,
                    )
                except FileNotFoundError as exc:
                    raise CandidateUnavailableError(
                        "Candidate disappeared before quarantine"
                    ) from exc
                except OSError as exc:
                    raise UnsafePathError("Could not quarantine candidate") from exc
                try:
                    os.fsync(parent_descriptor)
                    os.fsync(quarantine_descriptor)
                except OSError as sync_error:
                    try:
                        _restore_quarantined_candidate(
                            quarantine_descriptor,
                            parent_descriptor,
                            leaf_name,
                            active_name,
                        )
                    except UnsafePathError as restore_error:
                        raise DeleteRecoveryRequiredError(
                            "Quarantine move requires recovery; candidate remains protected"
                        ) from restore_error
                    raise UnsafePathError(
                        "Quarantine move was restored because directory sync failed"
                    ) from sync_error
                try:
                    current_identity = _validated_quarantined_identity(
                        quarantine_descriptor,
                        current_identity,
                        expected_size,
                    )
                except Exception:
                    try:
                        _restore_quarantined_candidate(
                            quarantine_descriptor,
                            parent_descriptor,
                            leaf_name,
                            active_name,
                        )
                    except UnsafePathError as restore_error:
                        raise DeleteRecoveryRequiredError(
                            "Candidate race requires recovery from private quarantine"
                        ) from restore_error
                    raise

            try:
                _ensure_delete_tombstone(quarantine_descriptor)
            except Exception as tombstone_error:
                # A tombstone may already exist even when its fsync failed.
                # Never restore in that ambiguous state: keeping candidate and
                # tombstone together lets the persisted token resume safely.
                raise DeleteRecoveryRequiredError(
                    "Delete preparation requires recovery from private quarantine"
                ) from tombstone_error
            try:
                os.unlink("candidate", dir_fd=quarantine_descriptor)
            except OSError as exc:
                raise DeleteRecoveryRequiredError(
                    "Secure unlink failed; candidate remains in private quarantine "
                    f"{active_name}/candidate"
                ) from exc
            try:
                os.fsync(quarantine_descriptor)
            except OSError as exc:
                raise DeleteRecoveryRequiredError(
                    "Candidate was unlinked but quarantine durability requires recovery"
                ) from exc
            _complete_private_delete(
                parent_descriptor,
                quarantine_descriptor,
                active_name,
                completed_name,
            )
            return current_identity
        finally:
            os.close(quarantine_descriptor)
