import json
import os
import stat
import sys
from collections import Counter
from pathlib import Path

import pytest

import subgen_ops_safety as safety


def platform_has_secure_unlink_primitives() -> bool:
    return (
        sys.platform.startswith("linux")
        and hasattr(os, "O_DIRECTORY")
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


requires_secure_unlink = pytest.mark.skipif(
    not platform_has_secure_unlink_primitives(),
    reason="requires Linux descriptor-relative unlink primitives",
)


def create_symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")


def test_supports_secure_unlink_matches_required_platform_primitives():
    assert safety.supports_secure_unlink() is platform_has_secure_unlink_primitives()


def test_lexical_host_path_normalizes_without_resolving(tmp_path):
    raw_path = f"{tmp_path}{os.sep}media{os.sep}.{os.sep}episode.mkv"

    assert safety.lexical_host_path(raw_path) == Path(
        os.path.abspath(os.path.normpath(raw_path))
    )


@pytest.mark.parametrize(
    "raw_path",
    [
        f"media{os.sep}unused{os.sep}..{os.sep}episode.mkv",
        f"{os.sep}media{os.sep}..{os.sep}episode.mkv",
    ],
)
def test_lexical_host_path_rejects_every_raw_parent_component(raw_path):
    with pytest.raises(safety.UnsafePathError, match="parent traversal"):
        safety.lexical_host_path(raw_path)


def test_lexical_host_path_rejects_nul():
    with pytest.raises(safety.UnsafePathError, match="NUL"):
        safety.lexical_host_path("media/episode.mkv\0.srt")


def test_exact_path_key_preserves_case(tmp_path):
    upper = safety.exact_path_key(tmp_path / "Library" / "Episode.mkv")
    lower = safety.exact_path_key(tmp_path / "library" / "episode.mkv")

    assert upper != lower
    assert upper.endswith(os.path.join("Library", "Episode.mkv"))
    assert lower.endswith(os.path.join("library", "episode.mkv"))


def test_prepare_private_state_directory_rejects_symlink_leaf(tmp_path):
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    linked_state = tmp_path / "state"
    create_symlink_or_skip(linked_state, real_state)

    with pytest.raises(safety.UnsafePathError, match="real directory"):
        safety.prepare_private_state_directory(linked_state)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux path policy")
def test_prepare_private_state_directory_rejects_symlink_parent_before_creation(
    tmp_path,
):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    create_symlink_or_skip(linked_parent, real_parent)
    state_dir = linked_parent / "state"

    with pytest.raises(safety.UnsafePathError, match="symbolic links"):
        safety.prepare_private_state_directory(state_dir)

    assert not (real_parent / "state").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux ownership policy")
def test_prepare_private_state_directory_rejects_group_writable_directory(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o770)
    state_dir.chmod(0o770)

    with pytest.raises(safety.UnsafePathError, match="group/world writable"):
        safety.prepare_private_state_directory(state_dir)


def test_file_identity_is_immutable_and_json_serializable(tmp_path):
    target = tmp_path / "episode.mkv"
    target.write_bytes(b"media")

    identity = safety.file_identity(target.stat())

    target_stat = target.stat()
    assert identity == safety.FileIdentity(
        target_stat.st_dev,
        target_stat.st_ino,
        target_stat.st_size,
        target_stat.st_mtime_ns,
        target_stat.st_ctime_ns,
    )
    assert json.loads(json.dumps(identity)) == list(identity)
    with pytest.raises(AttributeError):
        identity.device = identity.device + 1


def test_secure_operations_fail_closed_when_platform_support_is_disabled(
    tmp_path, monkeypatch
):
    media_root = tmp_path / "media"
    media_root.mkdir()
    target = media_root / "episode.mkv"
    target.write_bytes(b"media")
    monkeypatch.setattr(safety, "_IS_LINUX", False)

    assert safety.supports_secure_unlink() is False
    with pytest.raises(safety.UnsafePathError, match="not supported"):
        safety.validate_regular_file_beneath(media_root, target)
    with pytest.raises(safety.UnsafePathError, match="not supported"):
        safety.secure_unlink_regular_beneath(media_root, target)
    assert target.read_bytes() == b"media"


@requires_secure_unlink
def test_validate_accepts_regular_file_and_json_round_tripped_identity(tmp_path):
    media_root = tmp_path / "media"
    target = media_root / "show" / "episode.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    identity = safety.file_identity(target.stat())
    serialized_identity = json.loads(json.dumps(identity))

    assert safety.validate_regular_file_beneath(
        media_root,
        target,
        expected_identity=serialized_identity,
        expected_size=5,
    ) == identity


@requires_secure_unlink
def test_validate_accepts_candidate_relative_to_media_root(tmp_path):
    media_root = tmp_path / "media"
    target = media_root / "show" / "episode.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")

    assert safety.validate_regular_file_beneath(
        media_root, Path("show") / "episode.mkv"
    ) == safety.file_identity(target.stat())


@requires_secure_unlink
def test_validate_rejects_candidate_outside_media_root(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")

    with pytest.raises(safety.UnsafePathError, match="outside media root"):
        safety.validate_regular_file_beneath(media_root, outside)


@requires_secure_unlink
def test_validate_rejects_media_root_itself(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()

    with pytest.raises(safety.UnsafePathError, match="regular file beneath"):
        safety.validate_regular_file_beneath(media_root, media_root)


@requires_secure_unlink
def test_validate_rejects_directory_leaf(tmp_path):
    media_root = tmp_path / "media"
    directory = media_root / "show"
    directory.mkdir(parents=True)

    with pytest.raises(safety.UnsafePathError, match="regular file"):
        safety.validate_regular_file_beneath(media_root, directory)


@requires_secure_unlink
def test_validate_rejects_leaf_symlink(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    target = media_root / "target.mkv"
    target.write_bytes(b"media")
    link = media_root / "episode.mkv"
    create_symlink_or_skip(link, target)

    with pytest.raises(safety.UnsafePathError, match="regular file"):
        safety.validate_regular_file_beneath(media_root, link)


@requires_secure_unlink
def test_validate_rejects_parent_symlink(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "episode.mkv").write_bytes(b"outside")
    create_symlink_or_skip(media_root / "linked", outside)

    with pytest.raises(safety.UnsafePathError, match="unsafe path"):
        safety.validate_regular_file_beneath(
            media_root, media_root / "linked" / "episode.mkv"
        )


@requires_secure_unlink
def test_validate_rejects_symlink_media_root(tmp_path):
    real_root = tmp_path / "real-media"
    real_root.mkdir()
    target = real_root / "episode.mkv"
    target.write_bytes(b"media")
    linked_root = tmp_path / "media"
    create_symlink_or_skip(linked_root, real_root)

    with pytest.raises(safety.UnsafePathError, match="unsafe media root"):
        safety.validate_regular_file_beneath(linked_root, linked_root / target.name)


@requires_secure_unlink
def test_validate_rejects_changed_identity(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    target = media_root / "episode.mkv"
    target.write_bytes(b"original")
    original_identity = safety.file_identity(target.stat())
    replacement = media_root / "replacement.mkv"
    replacement.write_bytes(b"replacement")
    target.unlink()
    replacement.rename(target)

    assert safety.file_identity(target.stat()) != original_identity
    with pytest.raises(safety.UnsafePathError, match="identity changed"):
        safety.validate_regular_file_beneath(
            media_root, target, expected_identity=original_identity
        )


@requires_secure_unlink
def test_validate_rejects_in_place_content_change_with_same_inode(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    target = media_root / "episode.mkv"
    target.write_bytes(b"first")
    original_identity = safety.file_identity(target.stat())

    target.write_bytes(b"other")
    changed = target.stat()
    os.utime(
        target,
        ns=(changed.st_atime_ns, original_identity.mtime_ns + 1_000_000_000),
    )

    assert target.stat().st_ino == original_identity.inode
    with pytest.raises(safety.UnsafePathError, match="identity changed"):
        safety.validate_regular_file_beneath(
            media_root, target, expected_identity=original_identity
        )


@requires_secure_unlink
def test_validate_rejects_unexpected_size(tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    target = media_root / "episode.mkv"
    target.write_bytes(b"media")

    with pytest.raises(safety.UnsafePathError, match="size changed"):
        safety.validate_regular_file_beneath(media_root, target, expected_size=6)


@requires_secure_unlink
def test_secure_unlink_removes_validated_regular_file(tmp_path):
    media_root = tmp_path / "media"
    target = media_root / "show" / "episode.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    identity = safety.file_identity(target.stat())

    removed_identity = safety.secure_unlink_regular_beneath(
        media_root,
        target,
        expected_identity=identity,
        expected_size=5,
    )

    assert removed_identity == identity
    assert not target.exists()


@requires_secure_unlink
def test_secure_unlink_accepts_owner_only_quarantine_with_inherited_setgid(
    tmp_path, monkeypatch
):
    media_root = tmp_path / "media"
    target = media_root / "show" / "episode.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    identity = safety.file_identity(target.stat())
    real_mkdir = safety.os.mkdir

    def mkdir_with_inherited_setgid(path, mode=0o777, *, dir_fd=None):
        real_mkdir(path, mode, dir_fd=dir_fd)
        safety.os.chmod(path, 0o2700, dir_fd=dir_fd)

    monkeypatch.setattr(safety.os, "mkdir", mkdir_with_inherited_setgid)

    removed_identity = safety.secure_unlink_regular_beneath(
        media_root,
        target,
        expected_identity=identity,
        operation_token=safety.new_delete_token(),
    )

    assert removed_identity == identity
    assert not target.exists()


@requires_secure_unlink
def test_secure_unlink_rejects_quarantine_with_group_permissions(
    tmp_path, monkeypatch
):
    media_root = tmp_path / "media"
    target = media_root / "show" / "episode.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    identity = safety.file_identity(target.stat())
    real_mkdir = safety.os.mkdir

    def mkdir_with_group_permissions(path, mode=0o777, *, dir_fd=None):
        real_mkdir(path, mode, dir_fd=dir_fd)
        safety.os.chmod(path, 0o2750, dir_fd=dir_fd)

    monkeypatch.setattr(safety.os, "mkdir", mkdir_with_group_permissions)

    with pytest.raises(
        safety.UnsafePathError,
        match="private directory owned by the service user",
    ):
        safety.secure_unlink_regular_beneath(
            media_root,
            target,
            expected_identity=identity,
            operation_token=safety.new_delete_token(),
        )

    assert target.read_bytes() == b"media"


@requires_secure_unlink
def test_secure_unlink_uses_pinned_parent_during_final_parent_retarget_race(
    tmp_path, monkeypatch
):
    media_root = tmp_path / "media"
    original_parent = media_root / "show"
    original_parent.mkdir(parents=True)
    target = original_parent / "episode.mkv"
    target.write_bytes(b"inside")
    identity = safety.file_identity(target.stat())

    outside_parent = tmp_path / "outside"
    outside_parent.mkdir()
    outside_target = outside_parent / target.name
    outside_target.write_bytes(b"outside")
    moved_parent = media_root / "pinned-parent"
    real_unlink = os.unlink
    race_triggered = False

    def retarget_then_unlink(path, *, dir_fd=None):
        nonlocal race_triggered
        assert dir_fd is not None
        if not race_triggered:
            assert path == "candidate"
            original_parent.rename(moved_parent)
            create_symlink_or_skip(original_parent, outside_parent)
            race_triggered = True
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(safety.os, "unlink", retarget_then_unlink)

    assert safety.secure_unlink_regular_beneath(
        media_root, target, expected_identity=identity
    ) == identity
    assert race_triggered is True
    assert outside_target.read_bytes() == b"outside"
    assert not (moved_parent / target.name).exists()


@requires_secure_unlink
def test_secure_unlink_quarantines_before_leaf_swap_and_keeps_replacement(
    tmp_path, monkeypatch
):
    media_root = tmp_path / "media"
    parent = media_root / "show"
    parent.mkdir(parents=True)
    target = parent / "episode.mkv"
    target.write_bytes(b"offender")
    identity = safety.file_identity(target.stat())
    replacement = parent / "replacement.mkv"
    replacement.write_bytes(b"replacement")
    displaced_original = parent / "displaced-original.mkv"
    real_unlink = os.unlink
    race_triggered = False

    def swap_leaf_then_unlink(path, *, dir_fd=None):
        nonlocal race_triggered
        if not race_triggered:
            if target.exists():
                target.rename(displaced_original)
            replacement.rename(target)
            race_triggered = True
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(safety.os, "unlink", swap_leaf_then_unlink)

    assert safety.secure_unlink_regular_beneath(
        media_root,
        target,
        expected_identity=identity,
        operation_token=safety.new_delete_token(),
    ) == identity
    assert race_triggered is True
    assert target.read_bytes() == b"replacement"
    assert not displaced_original.exists()


@requires_secure_unlink
def test_secure_unlink_recovers_private_quarantine_after_process_crash(
    tmp_path, monkeypatch
):
    class SimulatedProcessCrash(BaseException):
        pass

    media_root = tmp_path / "media"
    parent = media_root / "show"
    parent.mkdir(parents=True)
    target = parent / "episode.mkv"
    target.write_bytes(b"offender")
    identity = safety.file_identity(target.stat())
    token = safety.new_delete_token()
    real_unlink = os.unlink

    def crash_before_unlink(path, *, dir_fd=None):
        raise SimulatedProcessCrash

    monkeypatch.setattr(safety.os, "unlink", crash_before_unlink)
    with pytest.raises(SimulatedProcessCrash):
        safety.secure_unlink_regular_beneath(
            media_root,
            target,
            expected_identity=identity,
            operation_token=token,
        )

    assert not target.exists()
    assert (parent / f".subgen-delete-{token}").is_dir()

    monkeypatch.setattr(safety.os, "unlink", real_unlink)
    assert safety.secure_unlink_regular_beneath(
        media_root,
        target,
        expected_identity=identity,
        operation_token=token,
    ) == identity
    assert not (parent / f".subgen-delete-{token}").exists()


@requires_secure_unlink
def test_secure_unlink_restores_candidate_when_quarantine_move_sync_fails(
    tmp_path, monkeypatch
):
    media_root = tmp_path / "media"
    parent = media_root / "show"
    parent.mkdir(parents=True)
    target = parent / "episode.mkv"
    target.write_bytes(b"offender")
    identity = safety.file_identity(target.stat())
    real_fsync = os.fsync
    directory_syncs = 0
    failure_injected = False

    def fail_second_directory_sync(descriptor):
        nonlocal directory_syncs, failure_injected
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_syncs += 1
            if directory_syncs == 2 and not failure_injected:
                failure_injected = True
                raise OSError("simulated move sync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(safety.os, "fsync", fail_second_directory_sync)

    with pytest.raises(safety.UnsafePathError, match="move was restored"):
        safety.secure_unlink_regular_beneath(
            media_root,
            target,
            expected_identity=identity,
            operation_token=safety.new_delete_token(),
        )

    assert failure_injected is True
    assert target.read_bytes() == b"offender"


@requires_secure_unlink
def test_tombstone_sync_failure_keeps_candidate_with_tombstone_for_recovery(
    tmp_path, monkeypatch
):
    media_root = tmp_path / "media"
    parent = media_root / "show"
    parent.mkdir(parents=True)
    target = parent / "episode.mkv"
    target.write_bytes(b"offender")
    identity = safety.file_identity(target.stat())
    token = safety.new_delete_token()
    real_fsync = os.fsync
    failure_injected = False

    def fail_tombstone_file_sync(descriptor):
        nonlocal failure_injected
        if not failure_injected and stat.S_ISREG(os.fstat(descriptor).st_mode):
            failure_injected = True
            raise OSError("simulated tombstone sync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(safety.os, "fsync", fail_tombstone_file_sync)

    with pytest.raises(safety.DeleteRecoveryRequiredError):
        safety.secure_unlink_regular_beneath(
            media_root,
            target,
            expected_identity=identity,
            operation_token=token,
        )

    quarantine = parent / f".subgen-delete-{token}"
    assert failure_injected is True
    assert not target.exists()
    assert (quarantine / "candidate").read_bytes() == b"offender"
    assert (quarantine / "tombstone").exists()

    monkeypatch.setattr(safety.os, "fsync", real_fsync)
    assert safety.secure_unlink_regular_beneath(
        media_root,
        target,
        expected_identity=identity,
        operation_token=token,
    ) == identity
    assert not quarantine.exists()


@requires_secure_unlink
def test_tombstone_only_recovery_requires_successful_directory_sync(
    tmp_path, monkeypatch
):
    media_root = tmp_path / "media"
    parent = media_root / "show"
    parent.mkdir(parents=True)
    target = parent / "episode.mkv"
    target.write_bytes(b"offender")
    identity = safety.file_identity(target.stat())
    token = safety.new_delete_token()
    real_unlink = os.unlink
    real_fsync = os.fsync
    candidate_unlinked = False
    first_failure_injected = False

    def track_candidate_unlink(path, *, dir_fd=None):
        nonlocal candidate_unlinked
        result = real_unlink(path, dir_fd=dir_fd)
        if path == "candidate":
            candidate_unlinked = True
        return result

    def fail_first_post_unlink_sync(descriptor):
        nonlocal first_failure_injected
        if (
            candidate_unlinked
            and not first_failure_injected
            and stat.S_ISDIR(os.fstat(descriptor).st_mode)
        ):
            first_failure_injected = True
            raise OSError("simulated post-unlink sync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(safety.os, "unlink", track_candidate_unlink)
    monkeypatch.setattr(safety.os, "fsync", fail_first_post_unlink_sync)
    with pytest.raises(safety.DeleteRecoveryRequiredError):
        safety.secure_unlink_regular_beneath(
            media_root,
            target,
            expected_identity=identity,
            operation_token=token,
        )

    quarantine = parent / f".subgen-delete-{token}"
    assert not (quarantine / "candidate").exists()
    assert (quarantine / "tombstone").exists()

    def fail_recovery_directory_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("simulated recovery sync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(safety.os, "unlink", real_unlink)
    monkeypatch.setattr(safety.os, "fsync", fail_recovery_directory_sync)
    with pytest.raises(
        safety.DeleteRecoveryRequiredError,
        match="could not prove candidate absence",
    ):
        safety.secure_unlink_regular_beneath(
            media_root,
            target,
            expected_identity=identity,
            operation_token=token,
        )
    assert quarantine.exists()

    monkeypatch.setattr(safety.os, "fsync", real_fsync)
    assert safety.secure_unlink_regular_beneath(
        media_root,
        target,
        expected_identity=identity,
        operation_token=token,
    ) == identity
    assert not quarantine.exists()


@requires_secure_unlink
def test_secure_unlink_closes_every_opened_directory_descriptor(
    tmp_path, monkeypatch
):
    media_root = tmp_path / "media"
    target = media_root / "show" / "season" / "episode.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    real_open = os.open
    real_close = os.close
    opened = []
    closed = []

    def tracking_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracking_close(descriptor):
        closed.append(descriptor)
        return real_close(descriptor)

    monkeypatch.setattr(safety.os, "open", tracking_open)
    monkeypatch.setattr(safety.os, "close", tracking_close)

    safety.secure_unlink_regular_beneath(media_root, target)

    assert opened
    assert Counter(opened) == Counter(closed)
