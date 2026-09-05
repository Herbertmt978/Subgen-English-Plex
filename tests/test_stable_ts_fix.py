"""Build-patch safety; actual backend behaviour has a separate installed probe."""

import hashlib
import importlib.util
from types import SimpleNamespace

import pytest

import apply_stable_ts_fix as fix


def digest(source):
    return hashlib.sha256(source).hexdigest()


@pytest.fixture
def source_pair(monkeypatch):
    before = ("# preserved\n" + fix.ORIGINAL_BLOCK + "# unchanged\n").replace("\n", "\r\n").encode()
    after = ("# preserved\n" + fix.PATCHED_BLOCK + "# unchanged\n").replace("\n", "\r\n").encode()
    monkeypatch.setattr(fix, "ORIGINAL_SHA256", digest(before))
    monkeypatch.setattr(fix, "PATCHED_SHA256", digest(after))
    return before, after


def test_exact_replacement_preserves_other_bytes_and_is_idempotent(tmp_path, source_pair):
    before, after = source_pair
    target = tmp_path / "result.py"
    target.write_bytes(before)
    assert fix.apply_fix(target) is True
    assert target.read_bytes() == after
    assert fix.apply_fix(target) is False
    assert target.read_bytes() == after


@pytest.mark.parametrize("source", [b"", b"different version", fix.ORIGINAL_BLOCK.encode()])
def test_unknown_or_line_ending_changed_source_is_not_modified(tmp_path, source):
    target = tmp_path / "result.py"
    target.write_bytes(source)
    with pytest.raises(ValueError, match="Unexpected stable-ts"):
        fix.apply_fix(target)
    assert target.read_bytes() == source


def test_wrong_output_hash_cannot_write(tmp_path, source_pair, monkeypatch):
    before, _ = source_pair
    target = tmp_path / "result.py"
    target.write_bytes(before)
    monkeypatch.setattr(fix, "PATCHED_SHA256", "unreviewed")
    with pytest.raises(ValueError, match="Unexpected patched"):
        fix.apply_fix(target)
    assert target.read_bytes() == before


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_duplicate_block_cannot_write(tmp_path, monkeypatch, count):
    source = fix.ORIGINAL_BLOCK.replace("\n", "\r\n").encode() * count
    target = tmp_path / "result.py"
    target.write_bytes(source)
    monkeypatch.setattr(fix, "ORIGINAL_SHA256", digest(source))
    with pytest.raises(ValueError, match="exactly one"):
        fix.apply_fix(target)
    assert target.read_bytes() == source


def test_missing_source_is_not_created(tmp_path):
    target = tmp_path / "result.py"
    with pytest.raises(FileNotFoundError):
        fix.apply_fix(target)
    assert not target.exists()


def test_package_lookup_does_not_import_heavy_backend(monkeypatch, tmp_path):
    origin = tmp_path / "stable_whisper" / "__init__.py"
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: SimpleNamespace(origin=str(origin)))
    assert fix.installed_result_path() == origin.with_name("result.py")


@pytest.mark.parametrize("spec", [None, SimpleNamespace(origin=None)])
def test_unavailable_package_fails_explicitly(monkeypatch, spec):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: spec)
    with pytest.raises(RuntimeError, match="not installed"):
        fix.installed_result_path()
