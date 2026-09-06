"""Real private pipes and CUDA entrypoint with a harmless injected backend."""
import hashlib
import io
from pathlib import Path
import sys
import threading
import wave

import pytest

from subgen_core.cuda_worker import CudaCohortWorker
from subgen_core.model_envelope_catalog import (
    ArtifactValidationError, ModelArtifactIdentity, ModelSupportFileIdentity,
    verify_ct2_model_directory,
)
from subgen_core.resident_worker import WorkerCancelled, WorkerProtocolError, WorkerAllocationFailure
from subgen_core.segmentation import NonMonotonicResult


VERSIONS = {name: 'test' for name in ('stable-ts', 'faster-whisper', 'ctranslate2', 'torch')}


@pytest.mark.parametrize('language,multilingual,task,expected', [
    ('auto', True, 'translate', True), ('auto', True, 'transcribe', True),
    ('fr', True, 'translate', False), ('auto', False, 'transcribe', False),
])
def test_real_cuda_adapter_forwards_segment_detection_without_overriding_explicit_language(
        tmp_path, language, multilingual, task, expected):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from subgen_core.cuda_worker_entry import StableCudaBackend
    path = tmp_path / 'sample.f32le'
    path.write_bytes(b'\0' * 4)
    backend = StableCudaBackend.__new__(StableCudaBackend)
    backend.np = SimpleNamespace(fromfile=lambda *a, **kw: SimpleNamespace(size=1),
        isfinite=lambda value: SimpleNamespace(all=lambda: True))
    transcribe = Mock(return_value=SimpleNamespace(to_dict=lambda: {'segments': []}))
    backend.model = SimpleNamespace(model=SimpleNamespace(is_multilingual=multilingual), transcribe=transcribe)
    backend.transcribe(path, 1/16000, language, task, None)
    assert transcribe.call_args.kwargs['multilingual'] is expected
    assert transcribe.call_args.kwargs['condition_on_previous_text'] is (language != 'auto')
    assert transcribe.call_args.kwargs['language'] == (None if language == 'auto' else language)
    assert transcribe.call_args.kwargs['task'] == task

PROGRAM = r'''
from subgen_core.cuda_worker_entry import serve
import sys, time
mode = sys.argv[1]
class Fake:
    def __init__(self, directory, index, precision, uuid):
        self.index, self.uuid, self.precision = index, uuid, precision
        if mode == 'load_oom': raise MemoryError('private allocation details')
        if mode == 'hang_load': time.sleep(20)
        if mode == 'replace_tokenizer': (directory / 'tokenizer.json').write_text('{ }')
    def memory(self):
        return {'physical_uuid': 'f'*32 if mode == 'wrong_uuid' else self.uuid,
                'device_index': self.index, 'total_bytes': 24*1024**3,
                'free_bytes': 20*1024**3, 'scope': 'cuda_runtime'}
    def receipt(self):
        versions = {name:'test' for name in ('stable-ts','faster-whisper','ctranslate2','torch')}
        if mode == 'wrong_runtime': versions['torch'] = 'different'
        return {'compute_type': 'float32' if mode == 'wrong_precision' else self.precision,
                'multilingual': mode == 'wrong_language', 'runtime':versions, 'cuda_runtime':'12.8'}
    def transcribe(self, path, duration, language, task, progress):
        from pathlib import Path
        assert Path(path).stat().st_size == duration*16000*4
        progress(0, duration)
        if mode == 'hang': time.sleep(20)
        if mode == 'failure': raise RuntimeError('private backend text')
        if mode == 'inference_oom': raise RuntimeError('CUDA out of memory. private allocation details')
        if mode == 'memory_words': raise RuntimeError('video description mentions out of memory')
        progress(duration, duration)
        return {'language':language, 'segments':[{'start':duration if mode == 'bad_timing' else 0,
                'end':0 if mode == 'bad_timing' else duration, 'text':' example'}]}
    def release(self):
        if mode == 'hang_release': time.sleep(20)
raise SystemExit(serve(sys.stdin.buffer, sys.stdout.buffer, backend_factory=Fake))
'''


def model_files(tmp_path):
    root = tmp_path / 'model'
    root.mkdir()
    weights = b'fixture weights'
    (root / 'model.bin').write_bytes(weights)
    identity = ModelArtifactIdentity('base.en', 'ctranslate2', 'float16',
        'sha256:' + hashlib.sha256(weights).hexdigest(), len(weights), 'sha256:' + 'a' * 64)
    support = []
    for name in ('config.json', 'tokenizer.json', 'preprocessor_config.json'):
        (root / name).write_bytes(b'{}')
        support.append(ModelSupportFileIdentity(name, 'sha256:' + hashlib.sha256(b'{}').hexdigest(), 2))
    return root, identity, tuple(support)


def worker(tmp_path, mode='ok'):
    root, identity, support = model_files(tmp_path)
    processes = []
    result = CudaCohortWorker([sys.executable, '-u', '-c', PROGRAM, mode],
        model_directory=root, artifact=identity, support_files=support,
        device_index=0, physical_uuid='1'*32, expected_runtime=VERSIONS,
        cuda_runtime='12.8', establish_limits=processes.append, scratch_directory=tmp_path,
        cwd=Path(__file__).resolve().parents[1])
    return result, processes


def audio():
    output = io.BytesIO()
    with wave.open(output, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b'\0\0' * 16000)
    return output.getvalue()


def test_cold_load_repeated_requests_observation_and_release(tmp_path):
    cuda, processes = worker(tmp_path)
    assert cuda.pid is None and not cuda.model_is_loaded
    cuda.load(timeout=3, cancel=None)
    try:
        assert cuda.model_is_loaded
        assert cuda.observe_memory()['physical_uuid'] == '1' * 32
        for task in ('transcribe', 'translate'):
            progress = []
            result = cuda.transcribe(audio(), timeout=2, cancel=None, language='en', task=task,
                progress_callback=lambda seek, total: progress.append((seek, total)))
            assert result['segments'][0]['end'] == 1
            assert progress == [(0, 1), (1, 1)]
            assert not list(tmp_path.glob('subgen-cuda-*'))
    finally:
        assert cuda.release(timeout=2) is True
    assert cuda.release(timeout=1) is True
    assert processes[0].poll() == 0 and not cuda.model_is_loaded
    with pytest.raises(WorkerProtocolError, match='generation'):
        cuda.load(timeout=1, cancel=None)


@pytest.mark.parametrize('mode', ['wrong_uuid', 'wrong_runtime', 'wrong_precision', 'wrong_language', 'replace_tokenizer'])
def test_mismatch_never_becomes_a_loaded_model(tmp_path, mode):
    cuda, processes = worker(tmp_path, mode)
    with pytest.raises(WorkerProtocolError):
        cuda.load(timeout=3, cancel=None)
    assert cuda.release_confirmed and not cuda.model_is_loaded
    assert processes[0].poll() is not None


@pytest.mark.parametrize('mode', ['hang_load', 'hang_release'])
def test_deadline_stops_load_or_release(tmp_path, mode):
    cuda, processes = worker(tmp_path, mode)
    if mode == 'hang_load':
        with pytest.raises(TimeoutError):
            cuda.load(timeout=.2, cancel=None)
    else:
        cuda.load(timeout=3, cancel=None)
        with pytest.raises(TimeoutError):
            cuda.release(timeout=.2)
    assert cuda.release_confirmed and processes[0].poll() is not None


def test_callback_pressure_cancels_and_removes_audio(tmp_path):
    cuda, processes = worker(tmp_path, 'hang')
    cuda.load(timeout=3, cancel=None)
    cancel = threading.Event()
    with pytest.raises(WorkerCancelled):
        cuda.transcribe(audio(), timeout=2, cancel=cancel, language='en',
            progress_callback=lambda seek, total: cancel.set())
    assert cuda.release_confirmed and processes[0].poll() is not None
    assert not list(tmp_path.glob('subgen-cuda-*'))


@pytest.mark.parametrize('mode,error', [('bad_timing', NonMonotonicResult), ('failure', WorkerProtocolError)])
def test_bad_inference_has_no_successful_result(tmp_path, mode, error):
    cuda, processes = worker(tmp_path, mode)
    cuda.load(timeout=3, cancel=None)
    with pytest.raises(error):
        cuda.transcribe(audio(), timeout=2, cancel=None, language='en')
    assert cuda.release_confirmed and processes[0].poll() is not None
    assert not list(tmp_path.glob('subgen-cuda-*'))


def test_changed_support_file_prevents_spawn(tmp_path):
    cuda, processes = worker(tmp_path)
    (cuda.directory / 'tokenizer.json').write_bytes(b'[]')
    with pytest.raises(ArtifactValidationError):
        cuda.load(timeout=1, cancel=None)
    assert not processes and cuda.release_confirmed


def test_unprovisioned_optional_vocabulary_is_refused(tmp_path):
    root, identity, support = model_files(tmp_path)
    (root / 'vocabulary.json').write_bytes(b'{}')
    with pytest.raises(ArtifactValidationError, match='unprovisioned'):
        verify_ct2_model_directory(root, identity, support)


@pytest.mark.parametrize('name', ['../config.json', '/config.json', 'model.bin', 'x\\config.json'])
def test_support_file_names_do_not_allow_paths(name):
    with pytest.raises(ArtifactValidationError):
        ModelSupportFileIdentity(name, 'sha256:' + 'a' * 64, 2)


@pytest.mark.parametrize('provider', ['stable-ts', 'stable-ts-whisperless'])
def test_runtime_receipt_records_the_actual_stable_distribution(monkeypatch, provider):
    from subgen_core import cuda_worker_entry as entry
    monkeypatch.setattr(entry.importlib.metadata, 'packages_distributions', lambda: {'stable_whisper': [provider]})
    monkeypatch.setattr(entry.importlib.metadata, 'version', lambda name: 'test')
    assert set(entry.runtime_versions()) == {provider, 'faster-whisper', 'ctranslate2', 'torch'}


@pytest.mark.parametrize('providers', [[], ['unexpected'], ['stable-ts', 'stable-ts-whisperless']])
def test_unknown_or_ambiguous_distribution_is_refused(monkeypatch, providers):
    from subgen_core import cuda_worker_entry as entry
    monkeypatch.setattr(entry.importlib.metadata, 'packages_distributions', lambda: {'stable_whisper': providers})
    with pytest.raises(RuntimeError, match='unverified'):
        entry.runtime_versions()


@pytest.mark.parametrize('mode,phase', [('load_oom','load'), ('inference_oom','transcribe')])
def test_strong_backend_allocation_error_is_typed_and_child_exits(tmp_path, mode, phase):
    cuda, processes = worker(tmp_path, mode)
    with pytest.raises(WorkerAllocationFailure) as failure:
        cuda.load(timeout=3, cancel=None)
        cuda.transcribe(audio(), timeout=2, cancel=None, language='en')
    assert failure.value.phase == phase
    assert 'private' not in str(failure.value)
    assert cuda.release_confirmed and processes[0].poll() is not None
    assert not list(tmp_path.glob('subgen-cuda-*'))


def test_incidental_memory_words_do_not_become_allocation_control(tmp_path):
    cuda, processes = worker(tmp_path, 'memory_words')
    cuda.load(timeout=3, cancel=None)
    with pytest.raises(WorkerProtocolError):
        cuda.transcribe(audio(), timeout=2, cancel=None, language='en')
    assert cuda.release_confirmed and processes[0].poll() is not None


@pytest.mark.parametrize('change', [
    {'request_id':0}, {'request_id':True}, {'phase':'load'}, {'extra':'ignored?'},
    {'code':'MemoryError'}, {'phase':'observe'},
])
def test_allocation_packet_must_match_current_request(tmp_path, change):
    cuda, _ = worker(tmp_path)
    cuda._request, cuda._remote_phase = 1, 'transcribe'
    packet = dict(event='error', code='allocation_failure', phase='transcribe', request_id=1)
    packet.update(change)
    with pytest.raises(WorkerProtocolError):
        cuda._raise_remote_error(packet)


def test_allocation_packet_not_accepted_outside_backend_operation(tmp_path):
    cuda, _ = worker(tmp_path)
    with pytest.raises(WorkerProtocolError):
        cuda._raise_remote_error(dict(event='error',code='allocation_failure',phase='load',request_id=0))
