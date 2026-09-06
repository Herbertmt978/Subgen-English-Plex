"""Model-free Vulkan result/adapter gate against the installed stable-ts.

Uses synthetic backend receipts, not GPU inference or a memory-pressure test.
Run outside pytest's ML stubs with this checkout on PYTHONPATH. The existing
segment journal remains the only commit/join owner; no public file is written.
"""

from functools import partial
import io
import json
from pathlib import Path
import tempfile
import wave

from subgen_core.backend_release import unload_verified_backend
from subgen_core.resource_management import AdaptiveChunkState
from subgen_core.segmentation import run_segmented_transcription
from subgen_core.segmented_result import SegmentJournal
from subgen_core.vulkan_transcription import VulkanTranscriptionAdapter
from subgen_core.whisper_cpp_result import WhisperCppResultError, decode_whisper_cpp_result


SPEECH = ((1, 2, " Opening."), (298, 302, " First boundary."),
          (598, 602, " Second boundary."), (618, 620, " Ending."))
EXPECTED = ((1, 2, " Opening."), (300, 302, " First boundary."),
            (600, 602, " Second boundary."), (618, 620, " Ending."))


def _wav(duration):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        for _ in range(int(duration)):
            stream.writeframesraw(b"\0" * 32000)
    return output.getvalue()


class ReceiptWorker:
    """Fixture transport only: no model, native subprocess or device claims."""

    model_is_loaded = True

    def __init__(self, *, malformed=False, empty=False):
        self.window = None
        self.requests = 0
        self.malformed = malformed
        self.empty = empty

    def transcribe(self, path, *, duration_seconds, progress, **_options):
        assert Path(path).stat().st_size == duration_seconds * 16000 * 4
        self.requests += 1
        progress(0)
        source = []
        if not self.empty:
            for start, end, text in SPEECH:
                if start >= self.window.extract_start and end <= self.window.extract_end:
                    source.append({"offsets": {
                        "from": int((start - self.window.extract_start) * 1000),
                        "to": int((end - self.window.extract_start) * 1000)}, "text": text,
                        "tokens": [{"text": "subword", "offsets": {"from": 0, "to": 0}}]})
        if self.malformed and self.requests == 2:
            source[0]["offsets"] = {"from": 2000, "to": 1000}
        progress(100)
        return decode_whisper_cpp_result(json.dumps({
            "result": {"language": "en"}, "transcription": source,
        }).encode(), duration_seconds=duration_seconds)

    def unload_model(self):
        self.model_is_loaded = False


def check_vulkan_result(result_type, segment_type):
    factory = partial(result_type, force_order=False, check_sorted=True, show_unsorted=False)
    checks = []
    for case in ("speech", "empty", "malformed"):
        with tempfile.TemporaryDirectory(prefix="subgen-vulkan-result-gate-") as directory:
            worker = ReceiptWorker(malformed=case == "malformed", empty=case == "empty")
            adapter = VulkanTranscriptionAdapter(worker, result_factory=factory,
                scratch_directory=directory, timeout_seconds=10)
            progress, committed = [], []
            finalized = False
            with SegmentJournal(directory=directory, result_factory=result_type,
                                segment_factory=segment_type) as journal:
                def extract(window):
                    worker.window = window
                    return _wav(window.extract_duration)

                def commit(window, staged, state):
                    journal.commit_chunk(window, staged, state)
                    committed.append(window.ordinal)

                def finalize(state):
                    nonlocal finalized
                    finalized = True
                    return journal.finalize(state)

                def unexpected_recovery(*_args):
                    raise AssertionError("A timing failure must not be treated as memory pressure")

                try:
                    result = run_segmented_transcription(
                        media_duration=620, adaptive=AdaptiveChunkState(300),
                        extract_chunk=extract,
                        transcribe_chunk=lambda audio, window, callback: adapter.transcribe(
                            audio, language="en", progress_callback=callback),
                        release_failure=unexpected_recovery, wait_for_recovery=unexpected_recovery,
                        persist_chunk=commit, finalize_assembly=finalize,
                        progress_callback=lambda seek, total: progress.append((seek, total)),
                    )
                except WhisperCppResultError:
                    assert case == "malformed"
                    assert committed == [0] and not finalized
                    checks.append("malformed second chunk rejected; prior commit retained; no join")
                else:
                    assert case != "malformed", "Malformed timestamps reached final rendering"
                    assert committed == [0, 1, 2] and finalized
                    signature = tuple((segment.start, segment.end, segment.text) for segment in result.segments)
                    assert signature == (() if case == "empty" else EXPECTED)
                    assert all(not segment.has_words for segment in result.segments)
                    assert all(total == 620 and 0 <= seek <= total for seek, total in progress)
                    srt = result.to_srt_vtt(word_level=False)
                    if case == "empty":
                        assert srt == ""
                    else:
                        assert srt.count(" --> ") == 4
                        for _, _, text in EXPECTED:
                            assert srt.count(text.strip()) == 1
                        assert "00:05:00,000 --> 00:05:02,000" in srt
                        assert "00:10:00,000 --> 00:10:02,000" in srt
                    checks.append(f"{case}: three chunks constructed, committed and rendered")
                finally:
                    unload_verified_backend(adapter)
                assert worker.model_is_loaded is False
            assert list(Path(directory).iterdir()) == [], "Temporary PCM or journal leaked"
    return checks


if __name__ == "__main__":
    import stable_whisper
    from stable_whisper.result import Segment, WhisperResult

    print(json.dumps({"kind": "synthetic installed-backend contract test",
        "stable_ts_version": stable_whisper.__version__,
        "checks": check_vulkan_result(WhisperResult, Segment),
        "gpu_inference": False, "memory_pressure_qualification": False}))
