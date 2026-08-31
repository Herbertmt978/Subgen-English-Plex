"""Bounded chunk planning and transactional stable-ts result assembly.

This module deliberately knows nothing about media paths, FFmpeg commands,
model loading, output files, or webhooks.  Callers inject extraction,
transcription, recovery, cancellation, and stable-ts construction seams.
Only a completely staged and validated chunk advances the source cursor.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from numbers import Real
from typing import Protocol

from .resource_management import MemoryPressureYield

DEFAULT_OVERLAP_SECONDS = 5.0
_MISSING = object()
_SEGMENT_METADATA_FIELDS = (
    "temperature",
    "avg_logprob",
    "compression_ratio",
    "no_speech_prob",
)


class SegmentationError(ValueError):
    """A chunk window or structured result violates the merge contract."""


class NonMonotonicResult(SegmentationError):
    """Structured timestamps are reversed, overlapping, or out of order."""


class ResultConstructionError(SegmentationError):
    """The injected stable-ts construction seam changed validated content."""


class AdaptiveChunkPolicy(Protocol):
    """The resource-policy operations consumed by the coordinator."""

    current_seconds: int

    def record_pressure_yield(self) -> int: ...

    def record_success(self, *, healthy: bool) -> int: ...


@dataclass(frozen=True, slots=True)
class ChunkWindow:
    """One owned source core plus its inference-context extraction interval."""

    ordinal: int
    media_duration: float
    core_start: float
    core_end: float
    extract_start: float
    extract_end: float

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise SegmentationError("Chunk ordinal must be a non-negative integer")
        if self.ordinal < 0:
            raise SegmentationError("Chunk ordinal must be a non-negative integer")

        for field_name in (
            "media_duration",
            "core_start",
            "core_end",
            "extract_start",
            "extract_end",
        ):
            object.__setattr__(
                self,
                field_name,
                _finite_number(getattr(self, field_name), field_name),
            )

        if self.media_duration <= 0:
            raise SegmentationError("Media duration must be positive")
        if not (
            0
            <= self.extract_start
            <= self.core_start
            < self.core_end
            <= self.extract_end
            <= self.media_duration
        ):
            raise SegmentationError("Chunk window intervals are inconsistent")

    @property
    def is_final(self) -> bool:
        return self.core_end == self.media_duration

    @property
    def core_duration(self) -> float:
        return self.core_end - self.core_start

    @property
    def extract_duration(self) -> float:
        return self.extract_end - self.extract_start

    def owns_midpoint(self, midpoint: Real) -> bool:
        """Apply half-open core ownership, including only the final endpoint."""

        point = _finite_number(midpoint, "midpoint")
        if self.is_final:
            return self.core_start <= point <= self.core_end
        return self.core_start <= point < self.core_end


@dataclass(frozen=True, slots=True)
class StagedChunk:
    """Copied chunk content that is safe to consider for one atomic commit."""

    language: str | None
    segments: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class AssemblyState:
    """Immutable committed source progress and structured transcript content."""

    media_duration: float
    cursor: float = 0.0
    completed_chunks: int = 0
    language: str | None = None
    segments: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        duration = _finite_number(self.media_duration, "media_duration")
        cursor = _finite_number(self.cursor, "cursor")
        object.__setattr__(self, "media_duration", duration)
        object.__setattr__(self, "cursor", cursor)
        if duration <= 0:
            raise SegmentationError("Media duration must be positive")
        if not 0 <= cursor <= duration:
            raise SegmentationError("Assembly cursor is outside the media")
        if isinstance(self.completed_chunks, bool) or not isinstance(
            self.completed_chunks, int
        ):
            raise SegmentationError("Completed chunk count must be an integer")
        if self.completed_chunks < 0:
            raise SegmentationError("Completed chunk count must not be negative")
        if self.language is not None and not isinstance(self.language, str):
            raise SegmentationError("Aggregate language must be text or null")

    @property
    def complete(self) -> bool:
        return self.cursor == self.media_duration


def _finite_number(value: Real, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise SegmentationError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise SegmentationError(f"{label} must be a finite number")
    return number


def _positive_number(value: Real, label: str) -> float:
    number = _finite_number(value, label)
    if number <= 0:
        raise SegmentationError(f"{label} must be positive")
    return number


def plan_next_window(
    *,
    cursor: Real,
    media_duration: Real,
    core_seconds: Real,
    ordinal: int,
    overlap_seconds: Real = DEFAULT_OVERLAP_SECONDS,
) -> ChunkWindow | None:
    """Plan one window from the current committed cursor and working duration."""

    source_cursor = _finite_number(cursor, "cursor")
    duration = _positive_number(media_duration, "media_duration")
    working_seconds = _positive_number(core_seconds, "core_seconds")
    overlap = _finite_number(overlap_seconds, "overlap_seconds")
    if overlap < 0:
        raise SegmentationError("overlap_seconds must not be negative")
    if source_cursor < 0 or source_cursor > duration:
        raise SegmentationError("cursor is outside the media")
    if source_cursor == duration:
        return None

    core_end = min(duration, source_cursor + working_seconds)
    if core_end <= source_cursor:
        raise SegmentationError("Chunk planning could not advance the cursor")
    return ChunkWindow(
        ordinal=ordinal,
        media_duration=duration,
        core_start=source_cursor,
        core_end=core_end,
        extract_start=max(0.0, source_cursor - overlap),
        extract_end=min(duration, core_end + overlap),
    )


def chunk_progress_callback(
    window: ChunkWindow,
    callback: Callable[[float, float], object] | None,
) -> Callable[[float, float], object] | None:
    """Map extraction-local progress onto the owned whole-media timeline."""

    if callback is None:
        return None
    if not callable(callback):
        raise TypeError("progress callback must be callable")

    def mapped_progress(seek: Real, total: Real) -> object:
        local_seek = _finite_number(seek, "progress seek")
        local_total = _finite_number(total, "progress total")
        if local_total <= 0:
            source_seek = window.core_start
        else:
            bounded_seek = min(max(0.0, local_seek), window.extract_duration)
            source_seek = window.extract_start + bounded_seek
            source_seek = min(
                max(source_seek, window.core_start),
                window.core_end,
            )
        return callback(source_seek, window.media_duration)

    return mapped_progress


def _field(value: object, name: str, default: object = _MISSING) -> object:
    if isinstance(value, Mapping):
        if name in value:
            return value[name]
    elif hasattr(value, name):
        return getattr(value, name)
    if default is _MISSING:
        raise SegmentationError(f"Structured result is missing {name}")
    return default


def _canonical_mapping(value: object, label: str) -> dict[str, object]:
    if isinstance(value, Mapping):
        source = value
    else:
        to_dict = getattr(value, "to_dict", None)
        if not callable(to_dict):
            raise SegmentationError(f"{label} does not provide structured data")
        source = to_dict()
    if not isinstance(source, Mapping):
        raise SegmentationError(f"{label} structured data must be a mapping")
    return deepcopy(dict(source))


def _structured_sequence(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise SegmentationError(f"{label} must be a finite sequence")
    return tuple(value)


def _copy_tokens(value: object, label: str) -> list[object] | None:
    if value is None:
        return None
    return [deepcopy(token) for token in _structured_sequence(value, f"{label} tokens")]


def _validated_interval(
    start: object,
    end: object,
    *,
    label: str,
    media_duration: float | None = None,
) -> tuple[float, float]:
    start_number = _finite_number(start, f"{label} start")
    end_number = _finite_number(end, f"{label} end")
    if start_number > end_number:
        raise NonMonotonicResult(f"{label} start exceeds end")
    if media_duration is not None and not (
        0 <= start_number <= media_duration and 0 <= end_number <= media_duration
    ):
        raise NonMonotonicResult(f"{label} is outside the media timeline")
    return start_number, end_number


def _copy_word(
    word: object,
    *,
    offset: float,
    media_duration: float,
) -> dict[str, object]:
    source = _canonical_mapping(word, "word")
    text = source.get("word")
    if not isinstance(text, str):
        raise SegmentationError("Word text must be a string")
    start, end = _validated_interval(
        _finite_number(source.get("start"), "word start") + offset,
        _finite_number(source.get("end"), "word end") + offset,
        label="word",
        media_duration=media_duration,
    )
    return {
        "word": text,
        "start": start,
        "end": end,
        "probability": deepcopy(source.get("probability")),
        "tokens": _copy_tokens(source.get("tokens"), "word"),
    }


def _copy_segment_metadata(
    source: Mapping[str, object],
) -> dict[str, object]:
    # Faster-whisper ``seek`` is a chunk-local mel-frame index, not seconds.
    # This backend-neutral assembler cannot convert it into the merged source
    # coordinate space, so deliberately discard it instead of fabricating a
    # dimensionally invalid value.
    copied = {"seek": None}
    for field_name in _SEGMENT_METADATA_FIELDS:
        copied[field_name] = deepcopy(source.get(field_name))
    return copied


def _flatten_owned_tokens(words: Sequence[Mapping[str, object]]) -> list[object] | None:
    token_groups = [word.get("tokens") for word in words]
    if not token_groups or any(group is None for group in token_groups):
        return None
    flattened: list[object] = []
    for group in token_groups:
        flattened.extend(deepcopy(group))
    return flattened


def _stage_segment(
    segment: object,
    window: ChunkWindow,
) -> dict[str, object] | None:
    source = _canonical_mapping(segment, "segment")
    words_value = source.get("words", _MISSING)
    metadata = _copy_segment_metadata(source)

    if words_value is not _MISSING and words_value is not None:
        words = _structured_sequence(words_value, "segment words")
        if words:
            copied_words = [
                _copy_word(
                    word,
                    offset=window.extract_start,
                    media_duration=window.media_duration,
                )
                for word in words
            ]
            owned_words = [
                word
                for word in copied_words
                if window.owns_midpoint((word["start"] + word["end"]) / 2)
            ]
            if not owned_words:
                return None
            payload = {
                **metadata,
                "start": owned_words[0]["start"],
                "end": owned_words[-1]["end"],
                "text": "".join(str(word["word"]) for word in owned_words),
                "tokens": _flatten_owned_tokens(owned_words),
                "words": owned_words,
            }
            return payload

    text = source.get("text", "")
    if not isinstance(text, str):
        raise SegmentationError("Segment text must be a string")
    if text == "":
        return None
    start, end = _validated_interval(
        _finite_number(source.get("start"), "segment start") + window.extract_start,
        _finite_number(source.get("end"), "segment end") + window.extract_start,
        label="segment",
        media_duration=window.media_duration,
    )
    if not window.owns_midpoint((start + end) / 2):
        return None
    payload = {
        **metadata,
        "start": start,
        "end": end,
        "text": text,
        "tokens": _copy_tokens(source.get("tokens", []), "segment"),
    }
    if words_value is not _MISSING and words_value is not None:
        payload["words"] = []
    return payload


def _validate_monotonic_segments(
    segments: Sequence[Mapping[str, object]],
    *,
    media_duration: float,
) -> None:
    previous_segment_end = 0.0
    for segment_index, segment in enumerate(segments):
        start, end = _validated_interval(
            segment.get("start"),
            segment.get("end"),
            label=f"segment {segment_index}",
            media_duration=media_duration,
        )
        if start < previous_segment_end:
            raise NonMonotonicResult("Segment timestamps overlap or move backwards")

        words_value = segment.get("words", _MISSING)
        if words_value is not _MISSING and words_value:
            words = _structured_sequence(words_value, "segment words")
            previous_word_end = start
            for word_index, word in enumerate(words):
                if not isinstance(word, Mapping):
                    raise SegmentationError("Staged word must be a mapping")
                word_start, word_end = _validated_interval(
                    word.get("start"),
                    word.get("end"),
                    label=f"segment {segment_index} word {word_index}",
                    media_duration=media_duration,
                )
                if word_start < previous_word_end:
                    raise NonMonotonicResult(
                        "Word timestamps overlap or move backwards"
                    )
                previous_word_end = word_end
            if start != words[0]["start"] or end != words[-1]["end"]:
                raise NonMonotonicResult(
                    "Word-bearing segment bounds do not match its words"
                )
        previous_segment_end = end


def _normalized_language(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SegmentationError("Chunk language must be text or null")
    normalized = value.strip()
    return normalized or None


def stage_chunk_result(result: object, window: ChunkWindow) -> StagedChunk:
    """Copy, offset, own, and validate one successful uncommitted result."""

    segments_value = _field(result, "segments")
    source_segments = _structured_sequence(segments_value, "result segments")
    staged_segments = tuple(
        payload
        for segment in source_segments
        if (payload := _stage_segment(segment, window)) is not None
    )
    _validate_monotonic_segments(
        staged_segments,
        media_duration=window.media_duration,
    )
    language = (
        _normalized_language(_field(result, "language", None))
        if staged_segments
        else None
    )
    return StagedChunk(language=language, segments=staged_segments)


def _assign_fresh_ids(segments: list[dict[str, object]]) -> None:
    for segment_id, segment in enumerate(segments):
        segment["id"] = segment_id
        words_value = segment.get("words", _MISSING)
        if words_value is _MISSING or not words_value:
            continue
        for word_id, word in enumerate(words_value):
            word["id"] = word_id


def commit_chunk(
    state: AssemblyState,
    window: ChunkWindow,
    staged: StagedChunk,
) -> AssemblyState:
    """Atomically produce the next committed assembly state."""

    if window.media_duration != state.media_duration:
        raise SegmentationError("Chunk and assembly media durations differ")
    if window.ordinal != state.completed_chunks:
        raise SegmentationError("Chunk ordinal does not match committed progress")
    if window.core_start != state.cursor:
        raise SegmentationError("Chunk start does not match committed cursor")

    candidate = deepcopy(list(state.segments))
    candidate.extend(deepcopy(list(staged.segments)))
    _assign_fresh_ids(candidate)
    _validate_monotonic_segments(candidate, media_duration=state.media_duration)
    language = state.language or (staged.language if staged.segments else None)
    return AssemblyState(
        media_duration=state.media_duration,
        cursor=window.core_end,
        completed_chunks=state.completed_chunks + 1,
        language=language,
        segments=tuple(candidate),
    )


def _construct_segments(
    payload_segments: Sequence[Mapping[str, object]],
    segment_factory: Callable[..., object],
) -> list[object]:
    return [
        segment_factory(
            **deepcopy(dict(segment)),
            ignore_unused_args=True,
        )
        for segment in payload_segments
    ]


def _fallback_reassign_ids(result: object) -> None:
    for segment_id, segment in enumerate(_field(result, "segments")):
        segment.id = segment_id
        segment.result = result
        words = getattr(segment, "words", None)
        if not words:
            continue
        for word_id, word in enumerate(words):
            word.id = word_id
            word.segment = segment


def _approximately_equal(left: object, right: object) -> bool:
    try:
        return math.isclose(float(left), float(right), abs_tol=0.001)
    except (TypeError, ValueError):
        return False


def _verify_constructed_result(
    result: object,
    payload: Mapping[str, object],
) -> None:
    result_segments = _structured_sequence(
        _field(result, "segments"),
        "constructed result segments",
    )
    expected_segments = _structured_sequence(payload["segments"], "payload segments")
    if len(result_segments) != len(expected_segments):
        raise ResultConstructionError(
            "Result factory dropped or added validated segments"
        )
    if _field(result, "language", None) != payload["language"]:
        raise ResultConstructionError("Result factory changed aggregate language")

    for segment_id, (result_segment, expected) in enumerate(
        zip(result_segments, expected_segments)
    ):
        if _field(result_segment, "id", None) != segment_id:
            raise ResultConstructionError("Result segment IDs are not sequential")
        if _field(result_segment, "result", None) is not result:
            raise ResultConstructionError("Result segment back-reference is missing")
        if not _approximately_equal(
            _field(result_segment, "start"), expected["start"]
        ) or not _approximately_equal(_field(result_segment, "end"), expected["end"]):
            raise ResultConstructionError("Result factory changed segment timestamps")
        if _field(result_segment, "text") != expected["text"]:
            raise ResultConstructionError("Result factory changed segment text")

        expected_words = expected.get("words")
        if not expected_words:
            result_words = _field(result_segment, "words", None)
            if "words" not in expected and result_words is not None:
                raise ResultConstructionError(
                    "Result factory changed a wordless segment"
                )
            if "words" in expected and result_words != []:
                raise ResultConstructionError(
                    "Result factory changed an empty-word segment"
                )
            continue
        result_words = _structured_sequence(
            _field(result_segment, "words"),
            "constructed segment words",
        )
        if len(result_words) != len(expected_words):
            raise ResultConstructionError("Result factory changed owned words")
        for word_id, (result_word, expected_word) in enumerate(
            zip(result_words, expected_words)
        ):
            if _field(result_word, "id", None) != word_id:
                raise ResultConstructionError("Result word IDs are not sequential")
            if _field(result_word, "segment", None) is not result_segment:
                raise ResultConstructionError("Result word back-reference is missing")
            if _field(result_word, "word") != expected_word["word"]:
                raise ResultConstructionError("Result factory changed word text")
            if not _approximately_equal(
                _field(result_word, "start"), expected_word["start"]
            ) or not _approximately_equal(
                _field(result_word, "end"), expected_word["end"]
            ):
                raise ResultConstructionError("Result factory changed word timestamps")


def build_whisper_result(
    state: AssemblyState,
    *,
    result_factory: Callable[[dict[str, object]], object],
    segment_factory: Callable[..., object] | None = None,
) -> object:
    """Construct and verify one final stable-ts-shaped result.

    When ``segment_factory`` is supplied, validated segments are explicitly
    installed after the result constructor runs.  This preserves intentional
    wordless segments in a mixed result; stable-ts otherwise removes them from
    a payload that also contains word-timestamp segments.
    """

    if not state.complete:
        raise SegmentationError("Cannot build a partial segmented result")
    if not callable(result_factory):
        raise TypeError("result_factory must be callable")
    payload = {
        "language": state.language,
        "segments": deepcopy(list(state.segments)),
    }
    _assign_fresh_ids(payload["segments"])
    _validate_monotonic_segments(
        payload["segments"],
        media_duration=state.media_duration,
    )
    result = result_factory(deepcopy(payload))
    if segment_factory is not None:
        if not callable(segment_factory):
            raise TypeError("segment_factory must be callable")
        result.segments = _construct_segments(payload["segments"], segment_factory)
        result.language = state.language

    reassign_ids = getattr(result, "reassign_ids", None)
    if callable(reassign_ids):
        reassign_ids()
    else:
        _fallback_reassign_ids(result)
    _verify_constructed_result(result, payload)
    return result


def _noop_cancel_check() -> None:
    return None


def _healthy_default() -> bool:
    return True


def run_segmented_transcription(
    *,
    media_duration: Real,
    adaptive: AdaptiveChunkPolicy,
    extract_chunk: Callable[[ChunkWindow], object],
    transcribe_chunk: Callable[
        [object, ChunkWindow, Callable[[float, float], object] | None], object
    ],
    recover_pressure: Callable[[MemoryPressureYield, ChunkWindow], None],
    result_factory: Callable[[dict[str, object]], object],
    segment_factory: Callable[..., object] | None = None,
    check_cancelled: Callable[[], None] = _noop_cancel_check,
    healthy_after_chunk: Callable[[], bool] = _healthy_default,
    progress_callback: Callable[[float, float], object] | None = None,
    overlap_seconds: Real = DEFAULT_OVERLAP_SECONDS,
) -> object:
    """Synchronously transcribe bounded chunks and return one final result."""

    if not all(
        callable(callback)
        for callback in (
            extract_chunk,
            transcribe_chunk,
            recover_pressure,
            result_factory,
            check_cancelled,
            healthy_after_chunk,
        )
    ):
        raise TypeError("Segmented transcription dependencies must be callable")

    state = AssemblyState(
        media_duration=_positive_number(media_duration, "media_duration")
    )
    while not state.complete:
        check_cancelled()
        window = plan_next_window(
            cursor=state.cursor,
            media_duration=state.media_duration,
            core_seconds=adaptive.current_seconds,
            ordinal=state.completed_chunks,
            overlap_seconds=overlap_seconds,
        )
        if window is None:  # pragma: no cover - state.complete owns this condition
            break

        check_cancelled()
        audio = extract_chunk(window)
        chunk_result = None
        staged = None
        pressure_error = None
        try:
            check_cancelled()
            try:
                chunk_result = transcribe_chunk(
                    audio,
                    window,
                    chunk_progress_callback(window, progress_callback),
                )
            except MemoryPressureYield as exc:
                pressure_error = exc.with_traceback(None)
            if pressure_error is None:
                check_cancelled()
                staged = stage_chunk_result(chunk_result, window)
                check_cancelled()
        finally:
            chunk_result = None
            audio = None

        if pressure_error is not None:
            check_cancelled()
            adaptive.record_pressure_yield()
            recover_pressure(pressure_error, window)
            check_cancelled()
            continue

        healthy = bool(healthy_after_chunk())
        next_state = commit_chunk(state, window, staged)
        check_cancelled()
        state = next_state
        adaptive.record_success(healthy=healthy)

    check_cancelled()
    result = build_whisper_result(
        state,
        result_factory=result_factory,
        segment_factory=segment_factory,
    )
    check_cancelled()
    return result


__all__ = [
    "DEFAULT_OVERLAP_SECONDS",
    "AssemblyState",
    "ChunkWindow",
    "NonMonotonicResult",
    "ResultConstructionError",
    "SegmentationError",
    "StagedChunk",
    "build_whisper_result",
    "chunk_progress_callback",
    "commit_chunk",
    "plan_next_window",
    "run_segmented_transcription",
    "stage_chunk_result",
]
