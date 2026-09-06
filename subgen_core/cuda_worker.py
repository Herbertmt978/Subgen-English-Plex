"""Cold CUDA cohort handle using the shared bounded resident-pipe owner.

The child runs in the same filesystem/containment as its parent. This is not a
Docker-CLI proxy: killing an attach client cannot prove container model release.
Physical UUID, model/supporting bytes, precision and runtime versions are checked.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import re
import tempfile
import time

from .model_envelope_catalog import verify_ct2_model_directory
from .resident_worker import ResidentPipeWorker, WorkerProtocolError, WorkerAllocationFailure, _seconds
from .segmentation import ChunkWindow, stage_chunk_result
from .vulkan_transcription import write_float_audio


class CudaCohortWorker(ResidentPipeWorker):
    def __init__(self, command, *, model_directory, artifact, support_files,
                 device_index, physical_uuid, expected_runtime, cuda_runtime,
                 establish_limits, scratch_directory, env=None, cwd=None):
        if (not isinstance(command, (tuple, list)) or not command
                or any(not isinstance(v, str) for v in command)):
            raise ValueError('CUDA worker requires an explicit argument sequence')
        if type(device_index) is not int or not 0 <= device_index < 32:
            raise ValueError('CUDA worker requires an explicit device index')
        if not isinstance(physical_uuid, str) or re.fullmatch(r'[0-9a-f]{32}', physical_uuid) is None or physical_uuid == '0' * 32:
            raise ValueError('CUDA worker requires a physical UUID')
        if not callable(establish_limits):
            raise ValueError('CUDA worker requires a process-limit owner')
        required = {'faster-whisper', 'ctranslate2', 'torch'}
        if (not isinstance(expected_runtime, dict) or not required <= set(expected_runtime)
                or set(expected_runtime) - required not in ({'stable-ts'}, {'stable-ts-whisperless'})
                or any(not isinstance(v, str) or not v for v in expected_runtime.values())):
            raise ValueError('CUDA runtime versions must be provisioned explicitly')
        if not isinstance(cuda_runtime, str) or not cuda_runtime:
            raise ValueError('CUDA runtime identity is required')
        self.directory = Path(model_directory)
        self.scratch = Path(scratch_directory)
        if not self.directory.is_absolute() or not self.scratch.is_absolute() or not self.scratch.is_dir() or self.scratch.is_symlink():
            raise ValueError('CUDA worker requires local absolute model and scratch directories')
        self.artifact, self.support_files = artifact, support_files
        self.index, self.uuid = device_index, physical_uuid
        self.expected_runtime, self.cuda_runtime = dict(expected_runtime), cuda_runtime
        self.command = tuple(command)
        self.establish_limits = establish_limits
        self.env, self.cwd = None if env is None else dict(env), cwd
        self.latest_observation = None
        self.ready = None
        self._remote_phase = None
        super().__init__(max_result_bytes=8 * 1024 * 1024)

    def _verify(self, deadline, cancel):
        verify_ct2_model_directory(self.directory, self.artifact, self.support_files,
                                   check_cancelled=lambda: self._check(deadline, cancel))
        if self.artifact.precision not in ('float16', 'float32'):
            raise ValueError('Mixed CUDA workers require explicit matching weight precision')

    def load(self, *, timeout, cancel):
        deadline = time.monotonic() + _seconds(timeout)
        if not self._busy.acquire(blocking=False):
            raise WorkerProtocolError('CUDA worker already has an active operation')
        try:
            if self._attempted_load or self._released:
                raise WorkerProtocolError('Create a new CUDA worker for another generation')
            self._attempted_load = True
            self._verify(deadline, cancel)
            self._spawn(self.command, establish_limits=self.establish_limits, env=self.env, cwd=self.cwd)
            self._send({'operation': 'load', 'directory': str(self.directory),
                'device_index': self.index, 'physical_uuid': self.uuid,
                'weights': asdict(self.artifact), 'support_files': [asdict(v) for v in self.support_files]})
            self._remote_phase = 'load'
            ready = self._receive(deadline, cancel)
            self._remote_phase = None
            if (ready.get('event') != 'ready' or type(ready.get('protocol')) is not int
                    or ready['protocol'] != 1 or ready.get('backend') != 'CUDA'
                    or ready.get('weights') != asdict(self.artifact)
                    or ready.get('support_files') != [asdict(v) for v in self.support_files]
                    or ready.get('compute_type') != self.artifact.precision
                    or type(ready.get('multilingual')) is not bool
                    or ready['multilingual'] != (not self.artifact.model.endswith('.en'))
                    or ready.get('runtime') != self.expected_runtime
                    or ready.get('cuda_runtime') != self.cuda_runtime):
                raise WorkerProtocolError('CUDA child did not confirm the provisioned model and runtime')
            self._verify(deadline, cancel)
            self.ready = ready
            self._loaded = True
        except BaseException:
            self._terminate()
            raise
        finally:
            self._remote_phase = None
            self._busy.release()

    def _raise_remote_error(self, packet):
        if (set(packet) == {'event', 'code', 'phase', 'request_id'}
                and packet['code'] == 'allocation_failure'
                and self._remote_phase in ('load', 'transcribe')
                and packet['phase'] == self._remote_phase
                and type(packet['request_id']) is int
                and packet['request_id'] == (0 if self._remote_phase == 'load' else self._request)):
            raise WorkerAllocationFailure(self._remote_phase)
        super()._raise_remote_error(packet)

    def _accept_memory(self, packet):
        memory = packet.get('memory')
        if (not isinstance(memory, dict)
                or set(memory) != {'physical_uuid', 'device_index', 'total_bytes', 'free_bytes', 'scope'}
                or memory['physical_uuid'] != self.uuid
                or type(memory['device_index']) is not int or memory['device_index'] != self.index
                or type(memory['total_bytes']) is not int or type(memory['free_bytes']) is not int
                or not 0 <= memory['free_bytes'] <= memory['total_bytes'] <= 2**60
                or memory['total_bytes'] == 0 or memory['scope'] != 'cuda_runtime'):
            raise WorkerProtocolError('CUDA memory observation has missing or conflicting physical identity')
        self.latest_observation = dict(memory, observed_at=time.monotonic())

    def observe_memory(self, *, timeout=5):
        deadline = time.monotonic() + _seconds(timeout)
        if not self._busy.acquire(blocking=False):
            raise WorkerProtocolError('CUDA worker already has an active operation')
        try:
            if not self.model_is_loaded:
                raise WorkerProtocolError('CUDA model is not resident')
            self._send({'operation': 'observe'})
            if self._receive(deadline, None).get('event') != 'memory':
                raise WorkerProtocolError('CUDA worker did not provide an observation')
            return self.latest_observation
        except BaseException:
            self._terminate()
            raise
        finally:
            self._busy.release()

    def transcribe(self, audio, *, timeout, cancel, language, task='transcribe',
                   verbose=None, progress_callback=None, **options):
        deadline = time.monotonic() + _seconds(timeout)
        if options or task not in ('transcribe', 'translate') or not isinstance(language, str) or re.fullmatch(r'(?:[a-z]{2,3}|auto)', language) is None:
            raise ValueError('CUDA cohort requires a language code or auto and supported decoder options')
        if progress_callback is not None and not callable(progress_callback):
            raise TypeError('CUDA progress callback must be callable')
        if not self._busy.acquire(blocking=False):
            raise WorkerProtocolError('CUDA worker already has an active operation')
        try:
            if not self.model_is_loaded:
                raise WorkerProtocolError('CUDA model is not resident')
            with tempfile.TemporaryDirectory(prefix='subgen-cuda-', dir=self.scratch) as directory:
                path = Path(directory) / 'chunk.f32le'
                with path.open('xb') as stream:
                    duration = write_float_audio(audio, stream, cancel=cancel, deadline=deadline)
                self._check(deadline, cancel)
                self._request += 1
                self._send({'operation': 'transcribe', 'request_id': self._request,
                    'audio_path': str(path), 'duration_seconds': duration, 'language': language, 'task': task})
                self._remote_phase = 'transcribe'
                last = -1
                while True:
                    packet = self._receive(deadline, cancel)
                    if type(packet.get('request_id')) is not int or packet['request_id'] != self._request:
                        raise WorkerProtocolError('CUDA result belongs to another request')
                    if packet.get('event') == 'progress':
                        percent = packet.get('percent')
                        if type(percent) is not int or not last < percent <= 100 or percent < 0:
                            raise WorkerProtocolError('CUDA progress is invalid or out of order')
                        last = percent
                        if progress_callback is not None:
                            progress_callback(duration * percent / 100, duration)
                    elif packet.get('event') == 'result':
                        # Reuse the canonical timing/word validation, with zero
                        # offset. The file scheduler later owns actual offsets.
                        window = ChunkWindow(0, duration, 0, duration, 0, duration)
                        staged = stage_chunk_result(packet.get('result'), window)
                        self._check(deadline, cancel)
                        return {'language': staged.language, 'segments': list(staged.segments),
                                'text': ''.join(s['text'] for s in staged.segments)}
                    else:
                        raise WorkerProtocolError('CUDA child sent an unexpected chunk event')
        except BaseException:
            self._terminate()
            raise
        finally:
            self._remote_phase = None
            self._busy.release()
