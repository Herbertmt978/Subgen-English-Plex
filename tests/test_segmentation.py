from __future__ import annotations

import gc
import math
import weakref
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from subgen_core import segmentation
from subgen_core.resource_management import AdaptiveChunkState, MemoryPressureYield


class FakeWord:
    def __init__(
        self,
        word,
        start,
        end,
        probability=None,
        tokens=None,
        id=None,
        segment=None,
        **_ignored,
    ):
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability
        self.tokens = None if tokens is None else list(tokens)
        self.id = id
        self.segment = segment

    def to_dict(self):
        return {
            "word": self.word,
            "start": self.start,
            "end": self.end,
            "probability": self.probability,
            "tokens": None if self.tokens is None else self.tokens.copy(),
        }


class FakeSegment:
    def __init__(
        self,
        start=0.0,
        end=0.0,
        text="",
        seek=None,
        tokens=None,
        temperature=None,
        avg_logprob=None,
        compression_ratio=None,
        no_speech_prob=None,
        words=None,
        id=None,
        result=None,
        ignore_unused_args=False,
        **_ignored,
    ):
        del ignore_unused_args
        self._start = start
        self._end = end
        self._text = text
        self._tokens = [] if tokens is None else list(tokens)
        self.seek = seek
        self.temperature = temperature
        self.avg_logprob = avg_logprob
        self.compression_ratio = compression_ratio
        self.no_speech_prob = no_speech_prob
        self.words = (
            None
            if words is None
            else [
                item if isinstance(item, FakeWord) else FakeWord(**item, segment=self)
                for item in words
            ]
        )
        for word in self.words or ():
            word.segment = self
        self.id = id
        self.result = result

    @property
    def start(self):
        return self.words[0].start if self.words else self._start

    @property
    def end(self):
        return self.words[-1].end if self.words else self._end

    @property
    def text(self):
        return "".join(word.word for word in self.words) if self.words else self._text

    @property
    def tokens(self):
        if self.words and all(word.tokens is not None for word in self.words):
            return [token for word in self.words for token in word.tokens]
        return self._tokens

    def to_dict(self):
        payload = {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "seek": self.seek,
            "tokens": self.tokens.copy(),
            "temperature": self.temperature,
            "avg_logprob": self.avg_logprob,
            "compression_ratio": self.compression_ratio,
            "no_speech_prob": self.no_speech_prob,
        }
        if self.words is not None:
            payload["words"] = [word.to_dict() for word in self.words]
        return payload


class FakeWhisperResult:
    """Faithful enough to expose stable-ts's mixed-wordless constructor trap."""

    def __init__(self, payload):
        self.language = payload.get("language")
        self.segments = [
            FakeSegment(**item, ignore_unused_args=True)
            for item in payload.get("segments", ())
        ]
        remove_every_wordless = any(segment.words for segment in self.segments)
        self.segments = [
            segment
            for segment in self.segments
            if not (
                (remove_every_wordless or segment.words is not None)
                and not segment.words
            )
        ]
        self.reassign_ids()

    def reassign_ids(self):
        for segment_id, segment in enumerate(self.segments):
            segment.id = segment_id
            segment.result = self
            for word_id, word in enumerate(segment.words or ()):
                word.id = word_id
                word.segment = segment

    def to_dict(self):
        return {
            "language": self.language,
            "segments": [segment.to_dict() for segment in self.segments],
        }

    def to_srt_vtt(self, **_kwargs):
        return "\n".join(
            f"{index}\n{segment.start:.3f} --> {segment.end:.3f}\n{segment.text}\n"
            for index, segment in enumerate(self.segments, 1)
        )


class RecordingResultFactory:
    def __init__(self, result_type=FakeWhisperResult):
        self.result_type = result_type
        self.calls = []

    def __call__(self, payload):
        self.calls.append(deepcopy(payload))
        return self.result_type(payload)


class SequencedAdaptive:
    def __init__(self, initial, successful_sizes=()):
        self.current_seconds = initial
        self.successful_sizes = list(successful_sizes)
        self.successes = []
        self.pressure_yields = 0

    def record_pressure_yield(self):
        self.pressure_yields += 1
        self.current_seconds = max(1, self.current_seconds // 2)
        return self.current_seconds

    def record_success(self, *, healthy):
        self.successes.append(healthy)
        if self.successful_sizes:
            self.current_seconds = self.successful_sizes.pop(0)
        return self.current_seconds


class RecordingAssembly:
    def __init__(self):
        self.chunks = []
        self.finalized = []

    @property
    def segments(self):
        return [
            deepcopy(segment)
            for _window, staged, _state in self.chunks
            for segment in staged.segments
        ]

    def persist(self, window, staged, state):
        self.chunks.append((window, deepcopy(staged), state))

    def finalize(self, state):
        self.finalized.append(state)
        return SimpleNamespace(
            language=state.language,
            segments=self.segments,
            state=state,
        )


def run_segmented_transcription(*, assembly=None, **kwargs):
    assembly = RecordingAssembly() if assembly is None else assembly
    return segmentation.run_segmented_transcription(
        persist_chunk=assembly.persist,
        finalize_assembly=assembly.finalize,
        **kwargs,
    )


def raw_result(*segments, language="en"):
    return SimpleNamespace(language=language, segments=list(segments))


def word_segment(*words, segment_id=99, **metadata):
    return FakeSegment(
        words=[
            FakeWord(
                word=text,
                start=start,
                end=end,
                tokens=None if token is None else [token],
                id=word_id,
            )
            for word_id, (text, start, end, token) in enumerate(words, 40)
        ],
        id=segment_id,
        **metadata,
    )


def planned_window(
    core_start,
    core_end,
    *,
    duration=30,
    extract_start=None,
    extract_end=None,
    ordinal=0,
):
    return segmentation.ChunkWindow(
        ordinal=ordinal,
        media_duration=duration,
        core_start=core_start,
        core_end=core_end,
        extract_start=core_start if extract_start is None else extract_start,
        extract_end=core_end if extract_end is None else extract_end,
    )


def commit_result(state, window, result, persisted=None):
    persisted = [] if persisted is None else persisted
    return segmentation.commit_chunk(
        state,
        window,
        segmentation.stage_chunk_result(result, window),
        persist_chunk=lambda _window, staged, _state: persisted.extend(
            deepcopy(staged.segments)
        ),
    )


def test_plan_windows_are_half_open_with_clamped_overlap_and_no_empty_tail():
    cursor = 0
    windows = []
    while True:
        window = segmentation.plan_next_window(
            cursor=cursor,
            media_duration=1300,
            core_seconds=600,
            ordinal=len(windows),
        )
        if window is None:
            break
        windows.append(window)
        cursor = window.core_end

    assert [
        (
            window.core_start,
            window.core_end,
            window.extract_start,
            window.extract_end,
            window.is_final,
        )
        for window in windows
    ] == [
        (0, 600, 0, 605, False),
        (600, 1200, 595, 1205, False),
        (1200, 1300, 1195, 1300, True),
    ]
    assert (
        segmentation.plan_next_window(
            cursor=1300,
            media_duration=1300,
            core_seconds=600,
            ordinal=3,
        )
        is None
    )

    exact = segmentation.plan_next_window(
        cursor=600,
        media_duration=1200,
        core_seconds=600,
        ordinal=1,
    )
    assert exact.is_final is True
    assert exact.core_end == 1200


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cursor": True, "media_duration": 10, "core_seconds": 5, "ordinal": 0},
        {"cursor": 0, "media_duration": math.inf, "core_seconds": 5, "ordinal": 0},
        {"cursor": 0, "media_duration": 10, "core_seconds": 0, "ordinal": 0},
        {"cursor": 11, "media_duration": 10, "core_seconds": 5, "ordinal": 0},
        {"cursor": 0, "media_duration": 10, "core_seconds": 5, "ordinal": True},
    ],
)
def test_window_planning_rejects_invalid_or_nonadvancing_values(kwargs):
    with pytest.raises(segmentation.SegmentationError):
        segmentation.plan_next_window(**kwargs)


def test_offset_midpoint_ownership_is_non_mutating_and_rebuilds_owned_words():
    source = word_segment(
        (" old", 0, 2, 1),
        (" keep", 4, 6, 2),
        (" next", 14, 16, 3),
        seek=500,
    )
    before = source.to_dict()
    window = planned_window(
        10,
        20,
        extract_start=5,
        extract_end=25,
    )

    staged = segmentation.stage_chunk_result(raw_result(source), window)

    assert len(staged.segments) == 1
    segment = staged.segments[0]
    assert (segment["start"], segment["end"], segment["text"]) == (
        10,
        11,
        " keep",
    )
    assert segment["seek"] is None
    assert segment["tokens"] == [2]
    assert segment["words"][0]["tokens"] == [2]
    assert source.to_dict() == before
    assert segment["words"] is not source.words


def test_chunk_local_seek_is_ignored_even_when_malformed():
    source = FakeSegment(
        start=1,
        end=2,
        text="kept",
        words=None,
        seek="not-a-frame-index",
    )
    before = source.to_dict()

    staged = segmentation.stage_chunk_result(
        raw_result(source),
        planned_window(0, 10, duration=10),
    )

    assert staged.segments[0]["seek"] is None
    assert source.to_dict() == before


def test_persisted_multi_chunk_payload_drops_restarted_local_seek_values():
    persisted = []
    state = segmentation.AssemblyState(media_duration=20)
    state = commit_result(
        state,
        planned_window(0, 10, duration=20),
        raw_result(FakeSegment(start=1, end=2, text="first", seek=500)),
        persisted,
    )
    state = commit_result(
        state,
        planned_window(
            10,
            20,
            duration=20,
            extract_start=5,
            extract_end=20,
            ordinal=1,
        ),
        raw_result(FakeSegment(start=6, end=7, text="second", seek=500)),
        persisted,
    )

    assert [(segment["start"], segment["seek"]) for segment in persisted] == [
        (1, None),
        (11, None),
    ]
    assert state.segment_count == 2


def test_exact_internal_midpoint_is_owned_only_by_next_core():
    left_window = planned_window(
        10,
        20,
        extract_start=5,
        extract_end=25,
    )
    right_window = planned_window(
        20,
        30,
        extract_start=15,
        extract_end=30,
        ordinal=1,
    )
    left = segmentation.stage_chunk_result(
        raw_result(word_segment((" tie", 14, 16, 1))),
        left_window,
    )
    right = segmentation.stage_chunk_result(
        raw_result(word_segment((" tie", 4, 6, 1))),
        right_window,
    )

    assert left.segments == ()
    assert [segment["text"] for segment in right.segments] == [" tie"]
    assert right.segments[0]["start"] == 20
    assert right.segments[0]["end"] == 21


def test_final_core_owns_zero_duration_word_and_wordless_segment_at_media_end():
    final_window = planned_window(
        20,
        30,
        extract_start=15,
        extract_end=30,
    )
    staged = segmentation.stage_chunk_result(
        raw_result(
            word_segment((" end", 15, 15, 1)),
            FakeSegment(start=15, end=15, text="[end]", words=None),
        ),
        final_window,
    )

    assert [segment["text"] for segment in staged.segments] == [" end", "[end]"]
    assert [segment["start"] for segment in staged.segments] == [30, 30]

    nonfinal = planned_window(
        10,
        20,
        duration=30,
        extract_start=5,
        extract_end=25,
    )
    assert (
        segmentation.stage_chunk_result(
            raw_result(word_segment((" boundary", 15, 15, 1))),
            nonfinal,
        ).segments
        == ()
    )


def test_wordless_and_intentional_empty_word_lists_preserve_their_shape():
    window = planned_window(0, 10, duration=10)
    staged = segmentation.stage_chunk_result(
        raw_result(
            FakeSegment(start=1, end=2, text="wordless", words=None),
            FakeSegment(start=3, end=4, text="intentional", words=[]),
            FakeSegment(start=5, end=6, text="", words=[]),
        ),
        window,
    )

    assert [segment["text"] for segment in staged.segments] == [
        "wordless",
        "intentional",
    ]
    assert "words" not in staged.segments[0]
    assert staged.segments[1]["words"] == []


def test_empty_chunks_advance_and_build_one_real_empty_result_without_language():
    adaptive = SequencedAdaptive(600)
    windows = []
    assembly = RecordingAssembly()

    result = run_segmented_transcription(
        assembly=assembly,
        media_duration=1300,
        adaptive=adaptive,
        extract_chunk=lambda window: windows.append(window) or object(),
        transcribe_chunk=lambda _audio, _window, _progress: raw_result(language="de"),
        release_failure=MagicMock(),
        wait_for_recovery=MagicMock(),
    )

    assert [window.core_start for window in windows] == [0, 600, 1200]
    assert result.segments == []
    assert result.language is None
    assert len(assembly.finalized) == 1
    assert assembly.finalized[0].segment_count == 0
    assert adaptive.successes == [True, True, True]


def test_first_nonempty_chunk_language_wins_over_empty_and_later_chunks():
    state = segmentation.AssemblyState(media_duration=30)
    state = commit_result(
        state,
        planned_window(0, 10, ordinal=0),
        raw_result(language="de"),
    )
    state = commit_result(
        state,
        planned_window(10, 20, ordinal=1),
        raw_result(FakeSegment(start=1, end=2, text="English"), language=" en "),
    )
    state = commit_result(
        state,
        planned_window(20, 30, ordinal=2),
        raw_result(FakeSegment(start=1, end=2, text="French"), language="fr"),
    )

    assert state.language == "en"


def test_next_window_reads_mutable_chunk_size_only_after_each_commit():
    adaptive = SequencedAdaptive(600, successful_sizes=[300, 600, 600])
    windows = []

    run_segmented_transcription(
        media_duration=1500,
        adaptive=adaptive,
        extract_chunk=lambda window: windows.append(window) or object(),
        transcribe_chunk=lambda _audio, _window, _progress: raw_result(),
        release_failure=MagicMock(),
        wait_for_recovery=MagicMock(),
    )

    assert [
        (window.core_start, window.core_end, window.ordinal) for window in windows
    ] == [(0, 600, 0), (600, 900, 1), (900, 1500, 2)]


def test_pressure_yield_retries_same_cursor_after_shrink_and_appends_nothing():
    adaptive = AdaptiveChunkState(1200)
    windows = []
    callback_calls = 0
    pressure = MemoryPressureYield("shared host pressure")
    releases = []
    waits = []
    lifecycle = []

    def progress(_seek, _total):
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == 1:
            raise pressure

    def transcribe(_audio, window, callback):
        windows.append(window)
        callback(10, window.extract_duration)
        return raw_result(language="en")

    result = run_segmented_transcription(
        media_duration=1200,
        adaptive=adaptive,
        extract_chunk=lambda _window: object(),
        transcribe_chunk=transcribe,
        release_failure=lambda error, window: releases.append((error, window)),
        wait_for_recovery=lambda error, window: waits.append((error, window)),
        progress_callback=progress,
        chunk_started=lambda window: lifecycle.append(("started", window.core_start)),
        chunk_unwound=lambda window: lifecycle.append(("unwound", window.core_start)),
        chunk_committed=lambda _window, state: lifecycle.append(
            ("committed", state.cursor)
        ),
    )

    assert [
        (window.ordinal, window.core_start, window.core_end) for window in windows
    ] == [(0, 0, 1200), (0, 0, 600), (1, 600, 1200)]
    assert releases == [(pressure, windows[0])]
    assert waits == [(pressure, windows[0])]
    assert adaptive.current_seconds == 600
    assert adaptive.minimum_allocation_failures == 0
    assert result.segments == []
    assert lifecycle == [
        ("started", 0),
        ("unwound", 0),
        ("started", 0),
        ("committed", 600),
        ("started", 600),
        ("committed", 1200),
    ]


def test_pressure_checkpoint_discards_before_persist_and_retries_smaller():
    adaptive = AdaptiveChunkState(600)
    assembly = RecordingAssembly()
    pressure = MemoryPressureYield("pressure returned after inference")
    windows = []
    releases = []
    waits = []
    checkpoint_calls = 0

    def transcribe(_audio, window, _callback):
        windows.append(window)
        owned_start = window.core_start - window.extract_start + 1
        return raw_result(
            FakeSegment(
                start=owned_start,
                end=owned_start + 1,
                text="bounded",
            )
        )

    def check_before_commit():
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        if checkpoint_calls == 1:
            raise pressure
        return True

    def release(error, window):
        assert assembly.chunks == []
        releases.append((error, window))

    result = run_segmented_transcription(
        assembly=assembly,
        media_duration=600,
        adaptive=adaptive,
        extract_chunk=lambda _window: object(),
        transcribe_chunk=transcribe,
        release_failure=release,
        wait_for_recovery=lambda error, window: waits.append((error, window)),
        check_before_commit=check_before_commit,
    )

    assert [
        (window.ordinal, window.core_start, window.core_end) for window in windows
    ] == [(0, 0, 600), (0, 0, 300), (1, 300, 600)]
    assert [window.core_start for window, _staged, _state in assembly.chunks] == [
        0,
        300,
    ]
    assert len(result.segments) == 2
    assert releases == [(pressure, windows[0])]
    assert waits == [(pressure, windows[0])]


def test_pressure_recovery_runs_after_audio_and_traceback_references_are_released():
    class Audio:
        pass

    adaptive = AdaptiveChunkState(600)
    references = []
    attempts = 0

    def extract(_window):
        audio = Audio()
        references.append(weakref.ref(audio))
        return audio

    def transcribe(_audio, _window, _callback):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise MemoryPressureYield("release this attempt")
        return raw_result()

    def release(_error, _window):
        gc.collect()
        assert references[0]() is None

    run_segmented_transcription(
        media_duration=600,
        adaptive=adaptive,
        extract_chunk=extract,
        transcribe_chunk=transcribe,
        release_failure=release,
        wait_for_recovery=MagicMock(),
    )

    assert attempts == 3


def test_noncontrol_failure_unwinds_the_uncommitted_chunk_before_propagation():
    failure = RuntimeError("backend failed")
    lifecycle = []

    with pytest.raises(RuntimeError) as raised:
        run_segmented_transcription(
            media_duration=600,
            adaptive=AdaptiveChunkState(600),
            extract_chunk=lambda _window: object(),
            transcribe_chunk=lambda _audio, _window, _callback: (_ for _ in ()).throw(
                failure
            ),
            release_failure=MagicMock(),
            wait_for_recovery=MagicMock(),
            chunk_started=lambda window: lifecycle.append(
                ("started", window.core_start)
            ),
            chunk_unwound=lambda window: lifecycle.append(
                ("unwound", window.core_start)
            ),
        )

    assert raised.value is failure
    assert lifecycle == [("started", 0), ("unwound", 0)]


def test_cancellation_during_pressure_yield_releases_before_skipping_shrink_and_wait():
    cancelled = False
    cancellation = RuntimeError("operator shutdown")
    adaptive = SequencedAdaptive(600)
    release = MagicMock()
    wait = MagicMock()
    assembly = RecordingAssembly()

    def check_cancelled():
        if cancelled:
            raise cancellation

    def transcribe(_audio, _window, _callback):
        nonlocal cancelled
        cancelled = True
        raise MemoryPressureYield("pressure during shutdown")

    with pytest.raises(RuntimeError) as raised:
        run_segmented_transcription(
            assembly=assembly,
            media_duration=600,
            adaptive=adaptive,
            extract_chunk=lambda _window: object(),
            transcribe_chunk=transcribe,
            release_failure=release,
            wait_for_recovery=wait,
            check_cancelled=check_cancelled,
        )

    assert raised.value is cancellation
    assert adaptive.pressure_yields == 0
    release.assert_called_once()
    wait.assert_not_called()
    assert assembly.finalized == []


def test_allocation_failure_releases_shrinks_and_retries_same_cursor():
    class AllocationFailure(RuntimeError):
        pass

    adaptive = AdaptiveChunkState(600)
    windows = []
    releases = []
    waits = []
    attempts = 0

    def transcribe(_audio, window, _callback):
        nonlocal attempts
        attempts += 1
        windows.append(window)
        if attempts == 1:
            raise AllocationFailure("decoder allocation failed")
        return raw_result()

    run_segmented_transcription(
        media_duration=600,
        adaptive=adaptive,
        extract_chunk=lambda _window: object(),
        transcribe_chunk=transcribe,
        release_failure=lambda error, window: releases.append((error, window)),
        wait_for_recovery=lambda error, window: waits.append((error, window)),
        is_allocation_failure=lambda error: isinstance(error, AllocationFailure),
    )

    assert [
        (window.ordinal, window.core_start, window.core_end) for window in windows
    ] == [(0, 0, 600), (0, 0, 300), (1, 300, 600)]
    assert len(releases) == len(waits) == 1
    assert adaptive.current_seconds == 300


def test_two_minimum_allocation_failures_release_then_raise_without_output():
    class AllocationFailure(RuntimeError):
        pass

    adaptive = AdaptiveChunkState(300)
    failure = AllocationFailure("minimum chunk cannot allocate")
    release = MagicMock()
    wait = MagicMock()
    assembly = RecordingAssembly()

    with pytest.raises(AllocationFailure) as raised:
        run_segmented_transcription(
            assembly=assembly,
            media_duration=600,
            adaptive=adaptive,
            extract_chunk=lambda _window: object(),
            transcribe_chunk=lambda _audio, _window, _callback: (_ for _ in ()).throw(
                failure
            ),
            release_failure=release,
            wait_for_recovery=wait,
            is_allocation_failure=lambda error: isinstance(
                error,
                AllocationFailure,
            ),
        )

    assert raised.value is failure
    assert adaptive.exhausted is True
    assert release.call_count == 2
    assert wait.call_count == 2
    assert assembly.finalized == []


def test_complete_external_pressure_recovery_separates_minimum_allocation_failures():
    class AllocationFailure(RuntimeError):
        pass

    adaptive = AdaptiveChunkState(300)
    attempts = 0
    recovery_windows = iter(((4, 5), (5, 5)))

    def transcribe(_audio, _window, _callback):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise AllocationFailure("minimum chunk cannot allocate")
        return raw_result()

    run_segmented_transcription(
        media_duration=300,
        adaptive=adaptive,
        extract_chunk=lambda _window: object(),
        transcribe_chunk=transcribe,
        release_failure=MagicMock(),
        wait_for_recovery=lambda _error, _window: next(recovery_windows),
        is_allocation_failure=lambda error: isinstance(error, AllocationFailure),
    )

    assert attempts == 3
    assert adaptive.exhausted is False


def test_ordinary_recovery_does_not_separate_minimum_allocation_failures():
    class AllocationFailure(RuntimeError):
        pass

    adaptive = AdaptiveChunkState(300)
    failure = AllocationFailure("minimum chunk cannot allocate")

    with pytest.raises(AllocationFailure) as raised:
        run_segmented_transcription(
            media_duration=300,
            adaptive=adaptive,
            extract_chunk=lambda _window: object(),
            transcribe_chunk=lambda _audio, _window, _callback: (_ for _ in ()).throw(
                failure
            ),
            release_failure=MagicMock(),
            wait_for_recovery=lambda _error, _window: (9, 9),
            is_allocation_failure=lambda error: isinstance(error, AllocationFailure),
        )

    assert raised.value is failure
    assert adaptive.exhausted is True


def test_chunk_progress_maps_overlap_to_owned_source_timeline():
    window = planned_window(
        10,
        20,
        extract_start=5,
        extract_end=25,
    )
    calls = []
    callback = segmentation.chunk_progress_callback(
        window,
        lambda seek, total: calls.append((seek, total)) or "kept",
    )

    assert callback(0, 20) == "kept"
    callback(5, 20)
    callback(10, 20)
    callback(20, 20)
    callback(999, 20)
    callback(8, 0)

    assert calls == [
        (10, 30),
        (10, 30),
        (15, 30),
        (20, 30),
        (20, 30),
        (10, 30),
    ]


def test_progress_callback_failure_propagates_without_recovery_or_result():
    failure = RuntimeError("display failed")
    release = MagicMock()
    wait = MagicMock()
    assembly = RecordingAssembly()
    adaptive = SequencedAdaptive(10)

    def transcribe(_audio, _window, callback):
        callback(1, 10)
        return raw_result(FakeSegment(start=1, end=2, text="partial"))

    with pytest.raises(RuntimeError) as raised:
        run_segmented_transcription(
            assembly=assembly,
            media_duration=10,
            adaptive=adaptive,
            extract_chunk=lambda _window: object(),
            transcribe_chunk=transcribe,
            release_failure=release,
            wait_for_recovery=wait,
            progress_callback=MagicMock(side_effect=failure),
        )

    assert raised.value is failure
    release.assert_not_called()
    wait.assert_not_called()
    assert assembly.finalized == []
    assert adaptive.successes == []


def test_nonpressure_failure_after_success_never_builds_a_partial_result():
    failure = ValueError("decoder rejected second chunk")
    adaptive = SequencedAdaptive(10)
    assembly = RecordingAssembly()
    release = MagicMock()
    wait = MagicMock()
    calls = 0

    def transcribe(_audio, _window, _callback):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure
        return raw_result(FakeSegment(start=1, end=2, text="committed"))

    with pytest.raises(ValueError) as raised:
        run_segmented_transcription(
            assembly=assembly,
            media_duration=20,
            adaptive=adaptive,
            extract_chunk=lambda _window: object(),
            transcribe_chunk=transcribe,
            release_failure=release,
            wait_for_recovery=wait,
        )

    assert raised.value is failure
    assert adaptive.successes == [True]
    release.assert_not_called()
    wait.assert_not_called()
    assert assembly.finalized == []


@pytest.mark.parametrize(
    "segment",
    [
        FakeSegment(start=math.nan, end=2, text="bad"),
        FakeSegment(start=1, end=math.inf, text="bad"),
        FakeSegment(start=2, end=1, text="bad"),
        word_segment((" first", 2, 3, 1), (" backwards", 1, 2, 2)),
    ],
)
def test_nonfinite_reversed_or_nonmonotonic_chunk_is_rejected(segment):
    window = planned_window(0, 10, duration=10)

    with pytest.raises(segmentation.SegmentationError):
        segmentation.stage_chunk_result(raw_result(segment), window)


def test_owned_word_timestamps_are_clamped_to_their_core_before_commit():
    persisted = []
    first_window = planned_window(
        0,
        10,
        duration=20,
        extract_end=15,
    )
    second_window = planned_window(
        10,
        20,
        duration=20,
        extract_start=5,
        ordinal=1,
    )
    first_source = word_segment((" left", 9.0, 10.4, 1))
    second_source = word_segment((" right", 4.6, 5.8, 2))
    source_snapshots = (first_source.to_dict(), second_source.to_dict())

    state = commit_result(
        segmentation.AssemblyState(media_duration=20),
        first_window,
        raw_result(first_source),
        persisted,
    )
    state = commit_result(
        state,
        second_window,
        raw_result(second_source),
        persisted,
    )

    assert [
        (segment["start"], segment["end"], segment["text"])
        for segment in persisted
    ] == [
        (9.0, 10.0, " left"),
        (10.0, 10.8, " right"),
    ]
    assert [
        (segment["words"][0]["start"], segment["words"][0]["end"])
        for segment in persisted
    ] == [(9.0, 10.0), (10.0, 10.8)]
    assert [segment["tokens"] for segment in persisted] == [[1], [2]]
    assert [segment["words"][0]["tokens"] for segment in persisted] == [
        [1],
        [2],
    ]
    assert (first_source.to_dict(), second_source.to_dict()) == source_snapshots
    assert state.cursor == 20
    assert state.completed_chunks == 2
    assert state.complete is True


def test_ambiguous_overlapping_repeated_words_are_both_preserved():
    persisted = []
    first_window = planned_window(0, 10, duration=20, extract_end=15)
    second_window = planned_window(
        10,
        20,
        duration=20,
        extract_start=5,
        ordinal=1,
    )
    state = commit_result(
        segmentation.AssemblyState(media_duration=20),
        first_window,
        raw_result(
            FakeSegment(
                words=[
                    FakeWord(
                        word=" no",
                        start=9.4,
                        end=10.1,
                        tokens=None,
                    )
                ]
            )
        ),
        persisted,
    )

    state = commit_result(
        state,
        second_window,
        raw_result(
            FakeSegment(
                words=[
                    FakeWord(
                        word=" no",
                        start=4.8,
                        end=5.6,
                        tokens=None,
                    )
                ]
            )
        ),
        persisted,
    )

    assert [
        (segment["start"], segment["end"], segment["text"], segment["tokens"])
        for segment in persisted
    ] == [
        (9.4, 10.0, " no", None),
        (10.0, 10.6, " no", None),
    ]
    assert state.complete is True


def test_matching_context_does_not_delete_a_possibly_repeated_word():
    persisted = []
    first_window = planned_window(0, 10, duration=20, extract_end=15)
    second_window = planned_window(
        10,
        20,
        duration=20,
        extract_start=5,
        ordinal=1,
    )
    state = commit_result(
        segmentation.AssemblyState(media_duration=20),
        first_window,
        raw_result(
            word_segment(
                (" before", 8.0, 9.0, None),
                (" echo", 9.0, 10.4, None),
                (" next", 10.4, 11.2, None),
            )
        ),
        persisted,
    )

    state = commit_result(
        state,
        second_window,
        raw_result(
            word_segment(
                (" before", 3.0, 4.0, None),
                (" echo", 4.6, 5.8, None),
                (" next", 5.8, 6.6, None),
            )
        ),
        persisted,
    )

    assert [segment["text"] for segment in persisted] == [
        " before echo",
        " echo next",
    ]
    assert [
        (word["start"], word["end"], word["word"], word["tokens"])
        for segment in persisted
        for word in segment["words"]
    ] == [
        (8.0, 9.0, " before", None),
        (9.0, 10.0, " echo", None),
        (10.0, 10.8, " echo", None),
        (10.8, 11.6, " next", None),
    ]


def test_nonoverlapping_repeated_seam_words_are_both_preserved():
    persisted = []
    first_window = planned_window(0, 10, duration=20, extract_end=15)
    second_window = planned_window(
        10,
        20,
        duration=20,
        extract_start=5,
        ordinal=1,
    )
    state = commit_result(
        segmentation.AssemblyState(media_duration=20),
        first_window,
        raw_result(word_segment((" no", 9.0, 9.8, 9))),
        persisted,
    )

    state = commit_result(
        state,
        second_window,
        raw_result(word_segment((" no", 5.1, 5.8, 9))),
        persisted,
    )

    assert [
        (segment["start"], segment["end"], segment["text"])
        for segment in persisted
    ] == [(9.0, 9.8, " no"), (10.1, 10.8, " no")]


def test_owned_wordless_timestamps_are_clamped_to_their_core_before_commit():
    persisted = []
    first_window = planned_window(
        0,
        10,
        duration=20,
        extract_end=15,
    )
    second_window = planned_window(
        10,
        20,
        duration=20,
        extract_start=5,
        ordinal=1,
    )

    state = commit_result(
        segmentation.AssemblyState(media_duration=20),
        first_window,
        raw_result(FakeSegment(start=9.0, end=10.4, text="left", words=None)),
        persisted,
    )
    state = commit_result(
        state,
        second_window,
        raw_result(FakeSegment(start=4.6, end=5.8, text="right", words=None)),
        persisted,
    )

    assert [
        (segment["start"], segment["end"], segment["text"])
        for segment in persisted
    ] == [
        (9.0, 10.0, "left"),
        (10.0, 10.8, "right"),
    ]


def test_matching_wordless_seam_segments_are_preserved_when_ambiguous():
    persisted = []
    first_window = planned_window(0, 10, duration=20, extract_end=15)
    second_window = planned_window(
        10,
        20,
        duration=20,
        extract_start=5,
        ordinal=1,
    )
    state = commit_result(
        segmentation.AssemblyState(media_duration=20),
        first_window,
        raw_result(
            FakeSegment(start=8.0, end=9.0, text="[intro]", tokens=[11]),
            FakeSegment(start=9.0, end=10.4, text="[music]", tokens=[12]),
            FakeSegment(start=10.4, end=11.2, text="[outro]", tokens=[13]),
        ),
        persisted,
    )

    state = commit_result(
        state,
        second_window,
        raw_result(
            FakeSegment(start=3.0, end=4.0, text="[intro]", tokens=[11]),
            FakeSegment(start=4.6, end=5.8, text="[music]", tokens=[12]),
            FakeSegment(start=5.8, end=6.6, text="[outro]", tokens=[13]),
        ),
        persisted,
    )

    assert [
        (segment["start"], segment["end"], segment["text"], segment["tokens"])
        for segment in persisted
    ] == [
        (8.0, 9.0, "[intro]", [11]),
        (9.0, 10.0, "[music]", [12]),
        (10.0, 10.8, "[music]", [12]),
        (10.8, 11.6, "[outro]", [13]),
    ]


def test_unreconciled_cross_chunk_overlap_is_rejected_without_mutating_state():
    first_window = planned_window(0, 10, duration=20)
    state = commit_result(
        segmentation.AssemblyState(media_duration=20),
        first_window,
        raw_result(FakeSegment(start=8, end=10, text="first")),
    )
    snapshot = state
    second_window = planned_window(
        10,
        20,
        duration=20,
        extract_start=5,
        extract_end=20,
        ordinal=1,
    )
    staged = segmentation.StagedChunk(
        language="en",
        segments=(
            {
                "start": 9.0,
                "end": 11.0,
                "text": "overlap",
                "tokens": [],
            },
        ),
    )

    persist = MagicMock()
    with pytest.raises(segmentation.NonMonotonicResult, match="overlap"):
        segmentation.commit_chunk(
            state,
            second_window,
            staged,
            persist_chunk=persist,
        )

    assert state.cursor == 10
    assert state.completed_chunks == 1
    assert state == snapshot
    persist.assert_not_called()


def test_cancellation_after_inference_prevents_commit_and_finalization():
    cancelled = RuntimeError("operator shutdown")
    checks = 0
    adaptive = SequencedAdaptive(10)
    assembly = RecordingAssembly()

    def check_cancelled():
        nonlocal checks
        checks += 1
        if checks == 4:
            raise cancelled

    with pytest.raises(RuntimeError) as raised:
        run_segmented_transcription(
            assembly=assembly,
            media_duration=10,
            adaptive=adaptive,
            extract_chunk=lambda _window: object(),
            transcribe_chunk=lambda _audio, _window, _callback: raw_result(
                FakeSegment(start=1, end=2, text="uncommitted")
            ),
            release_failure=MagicMock(),
            wait_for_recovery=MagicMock(),
            check_cancelled=check_cancelled,
        )

    assert raised.value is cancelled
    assert adaptive.successes == []
    assert assembly.chunks == []
    assert assembly.finalized == []
