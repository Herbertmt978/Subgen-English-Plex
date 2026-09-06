"""N-worker scheduling against installed stable-ts; no GPU inference claim.

Run with the checkout on PYTHONPATH, outside pytest's ML mocks. Synthetic
out-of-order receipts exercise the real spool, seam checks, journal and SRT
renderer. No media, subtitle destinations or models are opened.
"""

import json
from pathlib import Path
import tempfile

from subgen_core.segmentation import ParallelChunkCoordinator
from subgen_core.segmented_result import PendingChunkStore, SegmentJournal


def run_checks(result_type, segment_type):
    checks = []
    for count in (2, 3, 4, 5):
        for shrink in (False, True):
            duration = 7200
            speech = [(start, start + 2, f" Line {start}.") for start in range(1, duration - 2, 37)]
            with tempfile.TemporaryDirectory(prefix="subgen-n-worker-result-") as directory:
                store = PendingChunkStore(directory=directory, maximum_entries=2 * count)
                with SegmentJournal(directory=directory, result_factory=result_type,
                                    segment_factory=segment_type) as journal:
                    coordinator = ParallelChunkCoordinator(
                        media_duration=duration, workers=tuple(f"simulated:{n}" for n in range(count)),
                        store_result=store.store, read_result=store.read, discard_result=store.discard,
                        persist_chunk=journal.commit_chunk,
                    )
                    retries = 0
                    largest_pending = 0
                    try:
                        while not coordinator.state.complete:
                            assignments = [coordinator.claim(worker, core_seconds=150 if retries else 300)
                                           for worker in coordinator.workers]
                            assignments = [a for a in assignments if a is not None]
                            assert assignments, "Scheduler stopped making forward progress"
                            for assignment in reversed(assignments):
                                if shrink and not retries and assignment is assignments[0]:
                                    coordinator.yielded(assignment)
                                    retries += 1
                                    continue
                                window = assignment.window
                                segments = [
                                    {"start": start - window.extract_start,
                                     "end": end - window.extract_start, "text": text}
                                    for start, end, text in speech
                                    if start >= window.extract_start and end <= window.extract_end
                                ]
                                # Use the installed WhisperResult, not a pytest stub.
                                result = result_type({"language": "en", "segments": segments},
                                                     force_order=False, check_sorted=True, show_unsorted=False)
                                coordinator.completed(assignment, result)
                            largest_pending = max(largest_pending, coordinator.pending_count)
                            coordinator.commit_ready(check_before_commit=lambda: True)
                        result = journal.finalize(coordinator.state)
                        srt = result.to_srt_vtt(word_level=False)
                        assert srt.count(" --> ") == len(speech)
                        for _, _, text in speech:
                            assert srt.count(text.strip()) == 1
                        actual = [(segment.start, segment.end) for segment in result.segments]
                        assert all(0 <= start <= end <= duration for start, end in actual)
                        assert all(actual[i][0] >= actual[i - 1][1] for i in range(1, len(actual)))
                        checks.append({"simulated_workers": count, "source_seconds": duration,
                                       "shrinking_retries": retries, "joined_chunks": coordinator.state.completed_chunks,
                                       "cues": len(speech), "maximum_pending": largest_pending})
                    finally:
                        coordinator.close()
                        store.close()
                assert not list(Path(directory).iterdir()), "Ephemeral staging leaked"
    return checks


if __name__ == "__main__":
    import stable_whisper
    from stable_whisper.result import WhisperResult, Segment

    print(json.dumps({"kind": "synthetic multi-worker installed-result contract",
                      "stable_ts_version": stable_whisper.__version__,
                      "gpu_inference": False, "memory_pressure_qualification": False,
                      "checks": run_checks(WhisperResult, Segment)}))
