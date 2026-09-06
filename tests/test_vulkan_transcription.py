import io
from pathlib import Path
import struct
import threading
import wave

import pytest

from subgen_core import vulkan_transcription as adapter_module
from subgen_core.backend_release import unload_verified_backend
from subgen_core.vulkan_transcription import VulkanAudioError, VulkanTranscriptionAdapter, write_float_audio
from subgen_core.whisper_cpp_worker import WorkerCancelled


def wav(samples=(-32768, 0, 32767), rate=16000, channels=1, width=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    return output.getvalue()


class Worker:
    model_is_loaded = True
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []
    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))
        assert Path(path).is_file()
        assert Path(path).read_bytes() == struct.pack("<fff", -1, 0, 32767 / 32768)
        kwargs["progress"](50)
        if self.failure:
            raise self.failure
        return {"language": "en", "segments": [], "text": ""}
    def unload_model(self):
        self.model_is_loaded = False


def test_streamed_conversion_uses_normalized_little_endian_samples():
    output = io.BytesIO()
    assert write_float_audio(wav(), output) == 3 / 16000
    assert output.getvalue() == struct.pack("<fff", -1, 0, 32767 / 32768)


def test_unknown_ffmpeg_pipe_header_size_is_bounded_by_actual_frames():
    value = bytearray(wav())
    value[4:8] = b"\xff" * 4
    value[40:44] = b"\xff" * 4
    assert write_float_audio(bytes(value), io.BytesIO()) == 3 / 16000


def test_truncated_finite_wav_is_not_silently_shortened():
    value = bytearray(wav())
    value[40:44] = struct.pack("<I", 10)
    with pytest.raises(VulkanAudioError, match="declared WAV"):
        write_float_audio(bytes(value), io.BytesIO())


@pytest.mark.parametrize("settings", [{"rate": 48000}, {"channels": 2}, {"width": 1}])
def test_wrong_pcm_format_is_not_silently_resampled(settings):
    with pytest.raises(VulkanAudioError, match="mono 16 kHz"):
        write_float_audio(wav(**settings), io.BytesIO())


def test_empty_and_broken_wav_are_rejected():
    for audio in (b"", b"not WAV", wav(samples=())):
        with pytest.raises(VulkanAudioError):
            write_float_audio(audio, io.BytesIO())


def test_actual_sample_and_input_bounds(monkeypatch):
    monkeypatch.setattr(adapter_module, "MAX_FRAMES", 2)
    with pytest.raises(VulkanAudioError, match="duration"):
        write_float_audio(wav(), io.BytesIO())
    monkeypatch.setattr(adapter_module, "MAX_WAV_BYTES", 2)
    with pytest.raises(VulkanAudioError, match="input limit"):
        write_float_audio(wav(), io.BytesIO())


def test_cancellation_before_conversion():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(WorkerCancelled):
        write_float_audio(wav(), io.BytesIO(), cancel=cancel)


def test_conversion_obeys_request_deadline(monkeypatch):
    monkeypatch.setattr(adapter_module.time, 'monotonic', lambda: 20)
    with pytest.raises(TimeoutError, match='preparation timed out'):
        write_float_audio(wav(), io.BytesIO(), deadline=20)


def test_conversion_time_is_deducted_from_inference_budget(tmp_path, monkeypatch):
    readings = iter([0, 1, 2, 3])  # Start, conversion block/end, inference start.
    monkeypatch.setattr(adapter_module.time, 'monotonic', lambda: next(readings))
    worker = Worker()
    adapter = VulkanTranscriptionAdapter(worker, result_factory=dict,
        scratch_directory=tmp_path, timeout_seconds=10)
    adapter.transcribe(wav(), language='en')
    assert worker.calls[0][1]['timeout'] == 7
    assert not list(tmp_path.iterdir())


def test_chunk_interface_progress_result_factory_cleanup_and_existing_release(tmp_path):
    worker = Worker()
    results, progress = [], []
    def factory(data):
        results.append(data)
        return "existing result object"
    adapter = VulkanTranscriptionAdapter(worker, result_factory=factory,
        scratch_directory=tmp_path, timeout_seconds=10)
    assert adapter.transcribe(wav(), language="en", task="translate", verbose=None,
                              progress_callback=lambda seek, total: progress.append((seek, total))) == "existing result object"
    assert worker.calls[0][1]["translate"] is True
    assert progress == [(1.5 / 16000, 3 / 16000)]
    assert results == [{"language": "en", "segments": [], "text": ""}]
    assert not list(tmp_path.iterdir())
    unload_verified_backend(adapter)
    assert worker.model_is_loaded is False


def test_pressure_error_identity_is_preserved_and_temporary_audio_removed(tmp_path):
    pressure = RuntimeError("owner-controlled yield")
    worker = Worker(pressure)
    adapter = VulkanTranscriptionAdapter(worker, result_factory=dict,
        scratch_directory=tmp_path, timeout_seconds=10)
    with pytest.raises(RuntimeError) as error:
        adapter.transcribe(wav(), language="en")
    assert error.value is pressure
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("options", [{"beam_size": 1}, {"regroup": "custom"}, {"vad": True}])
def test_user_decoder_settings_are_not_silently_ignored(tmp_path, options):
    worker = Worker()
    adapter = VulkanTranscriptionAdapter(worker, result_factory=dict,
        scratch_directory=tmp_path, timeout_seconds=10)
    with pytest.raises(ValueError, match="Unsupported decoder"):
        adapter.transcribe(wav(), language="en", **options)
    assert not worker.calls and not list(tmp_path.iterdir())


def long_wav(seconds):
    output = io.BytesIO()
    with wave.open(output, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b'\0\0' * (seconds * 16000))
    return output.getvalue()


@pytest.mark.parametrize('task', ['transcribe', 'translate'])
def test_auto_language_delegates_whole_chunk_to_native_loop_without_extra_joins(tmp_path, task):
    class SwitchingWorker:
        model_is_loaded = True
        calls = []
        def transcribe(self, path, **options):
            self.calls.append(options)
            assert Path(path).stat().st_size == options['duration_seconds']*16000*4
            assert options['language'] == 'auto'
            assert options['translate'] is (task == 'translate')
            options['progress'](0)
            options['progress'](100)
            return {'language': 'en', 'segments': [
                {'start': 0, 'end': 20, 'text': ' first'},
                {'start': 20, 'end': 40, 'text': ' foreign'},
                {'start': 40, 'end': 60, 'text': ' last'}], 'text': ' first foreign last'}
    worker = SwitchingWorker()
    adapter = VulkanTranscriptionAdapter(worker, result_factory=dict,
        scratch_directory=tmp_path, timeout_seconds=10)
    progress = []
    result = adapter.transcribe(long_wav(60), language='auto', task=task,
        progress_callback=lambda seek, total: progress.append((seek, total)))
    assert [c['duration_seconds'] for c in worker.calls] == [60]
    assert [s['start'] for s in result['segments']] == [0, 20, 40]
    assert result['text'] == ' first foreign last'
    assert progress[-1] == (60, 60)
    assert all(a[0] <= b[0] for a,b in zip(progress, progress[1:]))
    assert worker.model_is_loaded and not list(tmp_path.iterdir())
