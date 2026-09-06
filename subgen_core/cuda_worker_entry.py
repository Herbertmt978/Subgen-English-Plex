"""Private resident CUDA child for bounded cohort inference, never an HTTP API.

Run with ``python -m subgen_core.cuda_worker_entry``. Heavy imports and model
allocation happen only after the parent has established limits and sent load.
The parent owns operation deadlines and can terminate this process on pressure.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict
import gc
import importlib
import importlib.metadata
import json
import math
from pathlib import Path
import re
import sys

from .model_envelope_catalog import (
    ModelArtifactIdentity, ModelSupportFileIdentity, verify_ct2_model_directory,
)
from .resident_worker import _unique_object, _invalid_constant
from .resource_management import is_allocation_failure

MAX_COMMAND_BYTES = 16384
MAX_PACKET_BYTES = 8 * 1024 * 1024


def runtime_versions():
    """Bind the actual stable_whisper distribution, including whisperless builds."""
    providers = importlib.metadata.packages_distributions().get('stable_whisper', [])
    if len(providers) != 1 or providers[0] not in ('stable-ts', 'stable-ts-whisperless'):
        raise RuntimeError('stable_whisper_distribution_unverified')
    return {name: importlib.metadata.version(name)
            for name in (providers[0], 'faster-whisper', 'ctranslate2', 'torch')}


def read_command(stream):
    line = stream.readline(MAX_COMMAND_BYTES + 1)
    if not line:
        return None
    if len(line) > MAX_COMMAND_BYTES or not line.endswith(b'\n'):
        raise ValueError('command_bound')
    value = json.loads(line.decode('utf-8'), object_pairs_hook=_unique_object,
                       parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('command_object')
    return value


class StableCudaBackend:
    def __init__(self, directory, index, precision, uuid):
        implementation = importlib.import_module('faster_whisper.transcribe')
        if getattr(implementation, 'SUBGEN_LANGUAGE_WINDOW_FIX', None) != 2:
            raise RuntimeError('language_window_backend_required')
        self.torch = importlib.import_module('torch')
        self.stable = importlib.import_module('stable_whisper')
        self.np = importlib.import_module('numpy')
        self.index = index
        self.uuid = uuid
        self.model = None
        self.memory()  # Check actual physical identity before loading weights.
        self.model = self.stable.load_faster_whisper(str(directory), device='cuda',
            device_index=index, compute_type=precision, cpu_threads=2,
            num_workers=1, local_files_only=True)
        actual_indexes = self.model.model.device_index
        if isinstance(actual_indexes, int):
            actual_indexes = [actual_indexes]
        if (self.model.model.device != 'cuda' or list(actual_indexes) != [index]
                or self.model.model.compute_type != precision):
            raise RuntimeError('loaded_device_or_precision_mismatch')

    def memory(self):
        properties = self.torch.cuda.get_device_properties(self.index)
        uuid = str(getattr(properties, 'uuid', '')).lower().removeprefix('gpu-').replace('-', '')
        if uuid != self.uuid or re.fullmatch(r'[0-9a-f]{32}', uuid) is None:
            raise RuntimeError('cuda_physical_identity_mismatch')
        free, total = self.torch.cuda.mem_get_info(self.index)
        return {'physical_uuid': uuid, 'device_index': self.index,
                'total_bytes': total, 'free_bytes': free, 'scope': 'cuda_runtime'}

    def receipt(self):
        return {'compute_type': self.model.model.compute_type,
                'multilingual': bool(self.model.model.is_multilingual),
                'runtime': runtime_versions(),
                'cuda_runtime': str(self.torch.version.cuda)}

    def transcribe(self, path, duration, language, task, progress):
        audio_path = Path(path)
        size = audio_path.stat().st_size
        if (not audio_path.is_file() or audio_path.is_symlink() or size % 4
                or not 0 < size <= 1810 * 16000 * 4 or size != round(duration * 16000) * 4):
            raise ValueError('bounded_float_audio_required')
        audio = self.np.fromfile(audio_path, dtype='<f4')
        if audio.size * 4 != size or not self.np.isfinite(audio).all():
            raise ValueError('invalid_float_audio')
        multilingual = bool(self.model.model.is_multilingual)
        if task == 'translate' and not multilingual:
            raise ValueError('multilingual_model_required')
        result = self.model.transcribe(audio, language=None if language == 'auto' else language, task=task,
            multilingual=language == 'auto' and multilingual,
            condition_on_previous_text=language != 'auto',
            verbose=None, progress_callback=progress, word_timestamps=True,
            regroup=False, vad=False)
        return result.to_dict()

    def release(self):
        if self.model is not None:
            self.model.model.unload_model()
            if self.model.model.model_is_loaded is not False:
                raise RuntimeError('cuda_release_unconfirmed')
            self.model = None
        gc.collect()
        self.torch.cuda.empty_cache()


def serve(source, target, *, backend_factory=StableCudaBackend):
    """Serve a single residency generation; injected backend is for unit tests."""
    backend = None
    allocation_context = None
    def emit(packet):
        encoded = json.dumps(packet, ensure_ascii=True, allow_nan=False).encode('ascii')
        if len(encoded) > MAX_PACKET_BYTES:
            raise ValueError('result_bound')
        target.write(encoded + b'\n')
        target.flush()
    try:
        load = read_command(source)
        if not load or set(load) != {'operation', 'directory', 'device_index', 'physical_uuid', 'weights', 'support_files'} or load['operation'] != 'load':
            raise ValueError('load_required')
        index, uuid = load['device_index'], load['physical_uuid']
        if type(index) is not int or not 0 <= index < 32 or not isinstance(uuid, str) or re.fullmatch(r'[0-9a-f]{32}', uuid) is None or uuid == '0' * 32:
            raise ValueError('explicit_cuda_device_required')
        weights = ModelArtifactIdentity(**load['weights'])
        support = tuple(ModelSupportFileIdentity(**value) for value in load['support_files'])
        if weights.precision not in ('float16', 'float32'):
            raise ValueError('explicit_weight_precision_required')
        directory = Path(load['directory'])
        if not directory.is_absolute():
            raise ValueError('local_model_required')
        verify_ct2_model_directory(directory, weights, support)
        allocation_context = ('load', 0)
        with redirect_stdout(sys.stderr):
            backend = backend_factory(directory, index, weights.precision, uuid)
        allocation_context = None
        verify_ct2_model_directory(directory, weights, support)
        emit({'event': 'ready', 'protocol': 1, 'backend': 'CUDA',
              'weights': asdict(weights), 'support_files': [asdict(v) for v in support],
              **backend.receipt(), 'memory': backend.memory()})
        last_request = 0
        while True:
            command = read_command(source)
            if command is None:
                raise ValueError('parent_closed_without_release')
            if command == {'operation': 'unload'}:
                with redirect_stdout(sys.stderr):
                    backend.release()
                emit({'event': 'released', 'protocol': 1, 'memory': backend.memory()})
                backend = None
                return 0
            if command == {'operation': 'observe'}:
                emit({'event': 'memory', 'memory': backend.memory()})
                continue
            if set(command) != {'operation', 'request_id', 'audio_path', 'duration_seconds', 'language', 'task'} or command['operation'] != 'transcribe':
                raise ValueError('unsupported_request')
            request, duration = command['request_id'], command['duration_seconds']
            if (type(request) is not int or request != last_request + 1
                    or isinstance(duration, bool) or not isinstance(duration, (float, int))
                    or not math.isfinite(duration) or not 0 < duration <= 1810
                    or not isinstance(command['audio_path'], str)
                    or not isinstance(command['language'], str)
                    or re.fullmatch(r'(?:[a-z]{2,3}|auto)', command['language']) is None
                    or command['task'] not in ('transcribe', 'translate')):
                raise ValueError('invalid_transcription_request')
            last_request = request
            last_progress = -1
            def progress(seek, total):
                nonlocal last_progress
                if not math.isfinite(seek) or not math.isfinite(total) or total <= 0:
                    raise ValueError('invalid_progress')
                percent = max(0, min(100, int(seek * 100 / total)))
                if percent > last_progress:
                    last_progress = percent
                    emit({'event': 'progress', 'request_id': request, 'percent': percent,
                          'memory': backend.memory()})
            with redirect_stdout(sys.stderr):
                allocation_context = ('transcribe', request)
                result = backend.transcribe(command['audio_path'], duration,
                    command['language'], command['task'], progress)
                allocation_context = None
            emit({'event': 'result', 'request_id': request, 'result': result,
                  'memory': backend.memory()})
    except Exception as error:
        # Do not send paths, subtitle dialogue or arbitrary backend exception text.
        try:
            if allocation_context is not None and is_allocation_failure(error):
                phase, request = allocation_context
                emit({'event': 'error', 'code': 'allocation_failure',
                      'phase': phase, 'request_id': request})
            else:
                emit({'event': 'error', 'code': type(error).__name__})
        except Exception:
            pass
        return 1
    finally:
        if backend is not None:
            with redirect_stdout(sys.stderr):
                backend.release()


if __name__ == '__main__':
    raise SystemExit(serve(sys.stdin.buffer, sys.stdout.buffer))
