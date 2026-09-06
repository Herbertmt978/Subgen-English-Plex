"""Bounded WAV adapter for an already admitted experimental Vulkan worker.

The runtime retains admission, pressure tickets, result construction and final
publication. This adapter does not select/load a model or invent word timing.
The optional provisioned-device runtime supplies this adapter; the default
single-device path is unchanged.
"""

from __future__ import annotations

import array
import io
from pathlib import Path
import sys
import tempfile
import time
import wave

from .whisper_cpp_worker import ResidentWhisperWorker, WorkerCancelled, _seconds


MAX_CHUNK_SECONDS = 1810
SAMPLE_RATE = 16000
MAX_FRAMES = MAX_CHUNK_SECONDS * SAMPLE_RATE
MAX_WAV_BYTES = MAX_FRAMES * 2 + 65536


class VulkanAudioError(ValueError):
    """Extracted audio is not a supported bounded mono PCM chunk."""


def write_float_audio(audio: bytes, destination, *, cancel=None, deadline=None) -> float:
    """Stream one extracted PCM16 WAV into little-endian float32 PCM.

    FFmpeg pipe WAVs may declare an unknown/huge data length. Bound the actual
    input and number of decoded frames instead of trusting that header count.
    Only one second's conversion buffers are retained at a time.
    """
    if not isinstance(audio, bytes) or len(audio) > MAX_WAV_BYTES:
        raise VulkanAudioError("Audio chunk exceeds the bounded WAV input limit")
    frames = 0
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
                raise VulkanAudioError("Vulkan chunks require mono 16 kHz PCM16 WAV audio")
            declared_frames = source.getnframes()
            while True:
                if cancel is not None and cancel.is_set():
                    raise WorkerCancelled("Subtitle audio preparation was cancelled")
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Subtitle audio preparation timed out")
                block = source.readframes(SAMPLE_RATE)
                if not block:
                    break
                if len(block) % 2:
                    raise VulkanAudioError("Audio chunk contains an incomplete PCM sample")
                frames += len(block) // 2
                if frames > MAX_FRAMES:
                    raise VulkanAudioError("Audio chunk exceeds the maximum segment duration")
                pcm = array.array("h", block)
                if sys.byteorder != "little":
                    pcm.byteswap()
                floating = array.array("f", (sample / 32768 for sample in pcm))
                if sys.byteorder != "little":
                    floating.byteswap()
                output = floating.tobytes()
                if destination.write(output) != len(output):
                    raise OSError("Temporary audio write was incomplete")
            if declared_frames != 0xFFFFFFFF // 2 and declared_frames != frames:
                raise VulkanAudioError("Audio chunk is shorter than its declared WAV data")
    except (wave.Error, EOFError) as error:
        raise VulkanAudioError("Audio chunk has an invalid WAV structure") from error
    if frames == 0:
        raise VulkanAudioError("Audio chunk contains no PCM samples")
    return frames / SAMPLE_RATE


class VulkanTranscriptionAdapter:
    """Match the existing transcribe / model.unload_model interface.

    Only already extracted WAV bytes are accepted. Filenames/uploads requiring
    unbounded decoding and unsupported decoder options
    are refused explicitly, not silently ignored. Public runtime dispatch must
    remain unchanged until those capability boundaries are integrated.
    """

    def __init__(self, worker, *, result_factory, scratch_directory, timeout_seconds, cancel=None):
        if worker.model_is_loaded is not True:
            raise RuntimeError("Vulkan adapter requires an admitted resident model")
        if not callable(result_factory):
            raise ValueError("Vulkan adapter requires the existing result factory")
        self._timeout = _seconds(timeout_seconds)
        self.model = worker  # Existing verified backend-release owner uses this.
        self._result_factory = result_factory
        self._scratch = Path(scratch_directory)
        if not self._scratch.is_dir() or self._scratch.is_symlink():
            raise ValueError("Vulkan scratch root must be an existing non-symlink directory")
        self._cancel = cancel

    def transcribe(self, audio, *, language, task="transcribe", verbose=None,
                   progress_callback=None, **options):
        deadline = time.monotonic() + self._timeout
        if not isinstance(language, str) or not language:
            raise ValueError("Vulkan requires a language code or auto")
        if task not in ("transcribe", "translate"):
            raise ValueError("Vulkan task must be transcribe or translate")
        if options:
            # Do not silently discard user decode/regroup/quality settings.
            raise ValueError("Unsupported decoder options for the experimental Vulkan adapter")
        if progress_callback is not None and not callable(progress_callback):
            raise TypeError("Vulkan progress callback must be callable")
        with tempfile.TemporaryDirectory(prefix="subgen-vulkan-", dir=self._scratch) as directory:
            path = Path(directory) / "chunk.f32le"
            with path.open("xb") as output:
                duration = write_float_audio(audio, output, cancel=self._cancel, deadline=deadline)
            def progress(percent):
                if progress_callback is not None:
                    progress_callback(duration * percent / 100, duration)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Subtitle audio preparation timed out")
            decoded = self.model.transcribe(
                str(path), duration_seconds=duration, language=language,
                translate=task == "translate", timeout=remaining,
                cancel=self._cancel, progress=progress,
            )
        # The file/journal owner applies source offsets and atomic joining.
        return self._result_factory(decoded)

class VulkanCohortWorker:
    """Cold native handle using the existing bounded WAV/result adapter.

    The cohort owns admission, cancellation and release ordering. This handle
    owns no resource policy or queue and never starts a child in its factory.
    """

    def __init__(self, worker, *, result_factory, scratch_directory):
        if not isinstance(worker, ResidentWhisperWorker) or worker.pid is not None or worker.release_confirmed:
            raise ValueError("Vulkan cohort requires a deferred native worker")
        if not callable(result_factory):
            raise ValueError("Vulkan cohort requires the existing result factory")
        scratch = Path(scratch_directory)
        if not scratch.is_dir() or scratch.is_symlink():
            raise ValueError("Vulkan scratch root must be an existing non-symlink directory")
        self.model = worker
        self._result_factory = result_factory
        self._scratch = scratch

    @property
    def model_is_loaded(self):
        return self.model.model_is_loaded

    @property
    def latest_observation(self):
        return self.model.latest_observation

    def load(self, *, timeout, cancel):
        self.model.load(timeout=timeout, cancel=cancel)

    def transcribe(self, audio, *, timeout, cancel, **options):
        adapter = VulkanTranscriptionAdapter(self.model, result_factory=self._result_factory,
            scratch_directory=self._scratch, timeout_seconds=timeout, cancel=cancel)
        return adapter.transcribe(audio, **options)

    def release(self, *, timeout):
        return self.model.release(timeout=timeout)
