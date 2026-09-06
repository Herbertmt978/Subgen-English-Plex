import hashlib
from types import SimpleNamespace

import pytest
import apply_faster_whisper_fix as fix


def digest(value):
    return hashlib.sha256(value).hexdigest()


def test_exact_build_correction_is_idempotent(tmp_path, monkeypatch):
    before = ''.join(old for old, _ in fix.REPLACEMENTS).encode()
    after = ''.join(new for _, new in fix.REPLACEMENTS).encode()
    monkeypatch.setattr(fix, 'ORIGINAL_SHA256', digest(before))
    monkeypatch.setattr(fix, 'PATCHED_SHA256', digest(after))
    path = tmp_path/'transcribe.py'
    path.write_bytes(before)
    assert fix.apply_fix(path)
    assert path.read_bytes() == after
    assert not fix.apply_fix(path)


@pytest.mark.parametrize('case', ['unknown', 'duplicate', 'target_hash'])
def test_correction_refuses_changed_source_without_writing(tmp_path, monkeypatch, case):
    before = ''.join(old for old, _ in fix.REPLACEMENTS).encode()
    if case == 'duplicate':
        before += fix.REPLACEMENTS[0][0].encode()
    if case != 'unknown':
        monkeypatch.setattr(fix, 'ORIGINAL_SHA256', digest(before))
    path = tmp_path/'transcribe.py'
    path.write_bytes(before)
    with pytest.raises(ValueError):
        fix.apply_fix(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('capability', [None, 1])
def test_cuda_refuses_uncorrected_backend_before_model_allocation(monkeypatch, capability):
    from subgen_core.cuda_worker_entry import StableCudaBackend
    calls=[]
    def load(name):
        calls.append(name)
        return SimpleNamespace(SUBGEN_LANGUAGE_WINDOW_FIX=capability)
    monkeypatch.setattr('subgen_core.cuda_worker_entry.importlib.import_module', load)
    with pytest.raises(RuntimeError, match='language_window_backend_required'):
        StableCudaBackend('unused', 0, 'float16', 'a'*32)
    assert calls == ['faster_whisper.transcribe']


def test_cuda_current_capability_reaches_normal_runtime_loading(monkeypatch):
    from subgen_core.cuda_worker_entry import StableCudaBackend
    calls=[]
    def load(name):
        calls.append(name)
        if name == 'faster_whisper.transcribe':
            return SimpleNamespace(SUBGEN_LANGUAGE_WINDOW_FIX=2)
        raise RuntimeError('stop before loading a real runtime')
    monkeypatch.setattr('subgen_core.cuda_worker_entry.importlib.import_module', load)
    with pytest.raises(RuntimeError, match='stop before loading a real runtime'):
        StableCudaBackend('unused', 0, 'float16', 'a'*32)
    assert calls == ['faster_whisper.transcribe', 'torch']


def test_language_lookahead_is_bounded_and_preserves_explicit_source():
    replacements=dict(fix.REPLACEMENTS)
    initial=replacements['                    features=features[..., seek:],\n']
    assert 'seek:seek+1000' in initial
    assert 'if multilingual else features[..., seek:]' in initial
    ongoing=replacements['            if options.multilingual:\n'
                         '                results = self.model.detect_language(encoder_output)\n']
    assert 'min(segment_size, 1000)' in ongoing
    assert ongoing.startswith('            if options.multilingual:')
    assert 'del language_encoder' in ongoing


def test_docker_applies_language_correction_before_application_copy():
    from pathlib import Path
    source=(Path(__file__).parents[1]/'Dockerfile').read_text()
    assert source.index('RUN python3 /subgen/apply_faster_whisper_fix.py') < source.index('COPY subgen_override.py')
