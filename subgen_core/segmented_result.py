"""Disk-backed committed chunks and bounded stable-ts result rendering.

The journal is intentionally ephemeral: it survives model unload/retry inside
one running task, but it is anonymous and automatically removed when closed.
Only one committed chunk is decoded or rendered at a time.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import struct
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from pathlib import Path

from .segmentation import (
    AssemblyState,
    ChunkWindow,
    NonMonotonicResult,
    SegmentationError,
    StagedChunk,
)

_JOURNAL_VERSION = 1
_DEFAULT_MIN_DURATION = 0.02
_REVERSE_FRAME_LENGTH = struct.Struct(">Q")
_SRT_CUE_START = re.compile(r"(?m)^(?P<index>[0-9]+)\n(?=[^\n]+ --> [^\n]+\n)")
_POSIX_FADVISE = getattr(os, "posix_fadvise", None)
_POSIX_FADV_DONTNEED = getattr(os, "POSIX_FADV_DONTNEED", None)


class SegmentJournalError(RuntimeError):
    """The ephemeral committed-chunk store could not preserve its contract."""


class SegmentJournalCommitError(SegmentJournalError):
    """One record failed but the journal rolled back to its prior commit."""


class SegmentJournalPoisonedError(SegmentJournalError):
    """Rollback failed, so the journal can no longer be read or written safely."""


class ResultConstructionError(SegmentationError):
    """A bounded stable-ts construction or render changed validated content."""


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SegmentationError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise SegmentationError(f"{label} must be a finite number")
    return number


def _segment_payload(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        source = value
    else:
        to_dict = getattr(value, "to_dict", None)
        if not callable(to_dict):
            raise TypeError("Appended segment must be a mapping or provide to_dict()")
        source = to_dict()
    if not isinstance(source, Mapping):
        raise TypeError("Appended segment structured data must be a mapping")

    payload = deepcopy(dict(source))
    payload.pop("id", None)
    payload.pop("result", None)
    start = _finite(payload.get("start"), "segment start")
    end = _finite(payload.get("end"), "segment end")
    if start > end:
        raise NonMonotonicResult("Segment start exceeds end")
    text = payload.get("text")
    if not isinstance(text, str):
        raise SegmentationError("Segment text must be a string")
    payload["start"] = start
    payload["end"] = end
    return payload


def _segment_has_words(segment: Mapping[str, object]) -> bool:
    words = segment.get("words")
    return isinstance(words, list) and bool(words)


def _segment_duration(segment: Mapping[str, object]) -> float:
    return float(segment["end"]) - float(segment["start"])


def _merge_segment_payloads(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> dict[str, object]:
    """Return the safe segment-level equivalent of stable-ts ``left += right``.

    stable-ts 2.19.1 raises when a minimum-duration merge crosses different
    ``ori_has_words`` states.  This journal deliberately drops partial word
    timing for that merged cue instead: a rare mixed payload must still publish
    usable segment-level subtitles rather than fail the completed transcript.
    """

    merged = deepcopy(dict(left))
    merged["end"] = float(right["end"])
    merged["text"] = f"{left['text']}{right['text']}"

    left_tokens = left.get("tokens")
    right_tokens = right.get("tokens")
    merged["tokens"] = (
        deepcopy(left_tokens) + deepcopy(right_tokens)
        if isinstance(left_tokens, list) and isinstance(right_tokens, list)
        else None
    )

    left_words = left.get("words")
    right_words = right.get("words")
    if isinstance(left_words, list) and isinstance(right_words, list):
        merged["words"] = deepcopy(left_words) + deepcopy(right_words)
    else:
        # A mixed/wordless result must remain globally segment-level.  Keeping
        # synthetic partial word timing would make stable-ts highlight only
        # some of a cue after a merge.
        merged.pop("words", None)
    return merged


def _best_effort_dontneed(
    file_object: object,
    fadvise: Callable[[int, int, int, int], object] | None,
    advice: int | None,
) -> None:
    """Ask the kernel to evict clean spool pages; never affect correctness."""

    if not callable(fadvise) or advice is None:
        return
    fileno = getattr(file_object, "fileno", None)
    if not callable(fileno):
        return
    try:
        fadvise(fileno(), 0, 0, advice)
    except Exception:  # noqa: BLE001,S110 - this optional hint cannot affect output
        # Advisory eviction is an optimization only.  Unsupported filesystems,
        # kernels, injected test seams, and closed descriptors must not alter
        # journal correctness.
        pass


def _approximately_equal(left: object, right: object) -> bool:
    try:
        return math.isclose(float(left), float(right), abs_tol=0.001)
    except (TypeError, ValueError):
        return False


def _assign_payload_ids(segments: list[dict[str, object]]) -> None:
    for segment_id, segment in enumerate(segments):
        segment["id"] = segment_id
        words = segment.get("words")
        if not words:
            continue
        for word_id, word in enumerate(words):
            word["id"] = word_id


def _construct_segment(
    payload: Mapping[str, object],
    *,
    segment_factory: Callable[..., object],
    segment_id: int,
    result: object,
) -> object:
    segment = segment_factory(
        **deepcopy(dict(payload)),
        ignore_unused_args=True,
    )
    segment.id = segment_id
    segment.result = result
    for word_id, word in enumerate(getattr(segment, "words", None) or ()):
        word.id = word_id
        word.segment = segment
    return segment


def _fallback_reassign_ids(result: object) -> None:
    for segment_id, segment in enumerate(_field(result, "segments", ())):
        segment.id = segment_id
        segment.result = result
        for word_id, word in enumerate(getattr(segment, "words", None) or ()):
            word.id = word_id
            word.segment = segment


def _verify_bounded_result(
    result: object,
    expected_segments: Sequence[Mapping[str, object]],
    language: str | None,
) -> None:
    result_segments = tuple(_field(result, "segments", ()))
    if len(result_segments) != len(expected_segments):
        raise ResultConstructionError(
            "Result factory dropped or added validated segments"
        )
    if _field(result, "language") != language:
        raise ResultConstructionError("Result factory changed aggregate language")

    for segment_id, (segment, expected) in enumerate(
        zip(result_segments, expected_segments)
    ):
        if _field(segment, "id") != segment_id:
            raise ResultConstructionError("Result segment IDs are not sequential")
        if _field(segment, "result") is not result:
            raise ResultConstructionError("Result segment back-reference is missing")
        if not _approximately_equal(_field(segment, "start"), expected["start"]):
            raise ResultConstructionError("Result factory changed segment timestamps")
        if not _approximately_equal(_field(segment, "end"), expected["end"]):
            raise ResultConstructionError("Result factory changed segment timestamps")
        if _field(segment, "text") != expected["text"]:
            raise ResultConstructionError("Result factory changed segment text")

        expected_words = expected.get("words")
        result_words = _field(segment, "words")
        if not expected_words:
            if "words" not in expected and result_words is not None:
                raise ResultConstructionError(
                    "Result factory changed a wordless segment"
                )
            if "words" in expected and result_words != []:
                raise ResultConstructionError(
                    "Result factory changed an empty-word segment"
                )
            continue
        actual_words = tuple(result_words or ())
        if len(actual_words) != len(expected_words):
            raise ResultConstructionError("Result factory changed owned words")
        for word_id, (word, expected_word) in enumerate(
            zip(actual_words, expected_words)
        ):
            if _field(word, "id") != word_id:
                raise ResultConstructionError("Result word IDs are not sequential")
            if _field(word, "segment") is not segment:
                raise ResultConstructionError("Result word back-reference is missing")
            if _field(word, "word") != expected_word["word"]:
                raise ResultConstructionError("Result factory changed word text")
            if not _approximately_equal(_field(word, "start"), expected_word["start"]):
                raise ResultConstructionError("Result factory changed word timestamps")
            if not _approximately_equal(_field(word, "end"), expected_word["end"]):
                raise ResultConstructionError("Result factory changed word timestamps")


def _build_bounded_result(
    segments: Sequence[Mapping[str, object]],
    *,
    language: str | None,
    result_factory: Callable[[dict[str, object]], object],
    segment_factory: Callable[..., object],
) -> object:
    payload_segments = deepcopy([dict(segment) for segment in segments])
    _assign_payload_ids(payload_segments)
    payload = {"language": language, "segments": payload_segments}
    result = result_factory(deepcopy(payload))
    result.segments = [
        _construct_segment(
            segment,
            segment_factory=segment_factory,
            segment_id=segment_id,
            result=result,
        )
        for segment_id, segment in enumerate(payload_segments)
    ]
    result.language = language
    reassign_ids = getattr(result, "reassign_ids", None)
    if callable(reassign_ids):
        reassign_ids()
    else:
        _fallback_reassign_ids(result)
    _verify_bounded_result(result, payload_segments, language)
    return result


def _write_renumbered_srt(
    output,
    fragment: str,
    *,
    first_index: int,
) -> int:
    normalized = fragment.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_SRT_CUE_START.finditer(normalized))
    if not matches:
        if normalized.strip():
            raise ResultConstructionError("Bounded SRT renderer returned invalid cues")
        return first_index
    if normalized[: matches[0].start()].strip():
        raise ResultConstructionError("Bounded SRT renderer returned invalid prefix")

    next_index = first_index
    for position, match in enumerate(matches):
        block_end = (
            matches[position + 1].start()
            if position + 1 < len(matches)
            else len(normalized)
        )
        block = normalized[match.start() : block_end].strip("\n")
        lines = block.split("\n")
        if len(lines) < 3 or " --> " not in lines[1]:
            raise ResultConstructionError("Bounded SRT renderer returned invalid cue")
        lines[0] = str(next_index)
        if next_index > 1:
            output.write("\n\n")
        output.write("\n".join(lines))
        next_index += 1
    return next_index


class SegmentJournal:
    """Transactional anonymous store for committed bounded chunk payloads."""

    def __init__(
        self,
        *,
        directory: str | os.PathLike[str] | None = None,
        result_factory: Callable[[dict[str, object]], object] | None = None,
        segment_factory: Callable[..., object] | None = None,
        file_factory: Callable[..., object] = tempfile.TemporaryFile,
        fsync: Callable[[int], object] = os.fsync,
        fadvise: Callable[[int, int, int, int], object] | None = _POSIX_FADVISE,
        dontneed_advice: int | None = _POSIX_FADV_DONTNEED,
    ) -> None:
        if not callable(file_factory):
            raise TypeError("file_factory must be callable")
        if not callable(fsync):
            raise TypeError("fsync must be callable")
        self._file = file_factory(mode="w+b", dir=directory)
        self._result_factory = result_factory
        self._segment_factory = segment_factory
        self._fsync = fsync
        self._fadvise = fadvise
        self._dontneed_advice = dontneed_advice
        self._lock = threading.RLock()
        self._committed_offset = 0
        self._chunk_count = 0
        self._segment_count = 0
        self._last_segment: dict[str, object] | None = None
        self._last_segment_end = 0.0
        self._all_segments_have_words = True
        self._closed = False
        self._poisoned = False
        self._encoder = json.JSONEncoder(
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

    @property
    def committed_offset(self) -> int:
        return self._committed_offset

    @property
    def chunk_count(self) -> int:
        return self._chunk_count

    @property
    def segment_count(self) -> int:
        return self._segment_count

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    @property
    def all_segments_have_words(self) -> bool:
        """Whether every committed/appended segment has usable word timing."""

        return self._all_segments_have_words

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_usable(self) -> None:
        if self._closed:
            raise SegmentJournalError("Segment journal is closed")
        if self._poisoned:
            raise SegmentJournalPoisonedError("Segment journal is poisoned")

    def _sync(self) -> None:
        self._file.flush()
        fileno = getattr(self._file, "fileno", None)
        if callable(fileno):
            self._fsync(fileno())

    def _advise_dontneed(self) -> None:
        _best_effort_dontneed(
            self._file,
            self._fadvise,
            self._dontneed_advice,
        )

    def _rollback(self, offset: int) -> None:
        self._file.seek(offset)
        self._file.truncate(offset)
        self._sync()

    def _commit_record(
        self,
        record: Mapping[str, object],
        segments: Sequence[Mapping[str, object]],
        *,
        chunk_delta: int,
    ) -> None:
        self._require_usable()
        start_offset = self._committed_offset
        next_chunk_count = self._chunk_count + chunk_delta
        next_segment_count = self._segment_count + len(segments)
        next_last_segment = (
            deepcopy(dict(segments[-1])) if segments else self._last_segment
        )
        next_last_segment_end = (
            float(segments[-1]["end"]) if segments else self._last_segment_end
        )
        next_all_segments_have_words = self._all_segments_have_words and all(
            _segment_has_words(segment) for segment in segments
        )
        try:
            self._file.seek(start_offset)
            self._file.truncate(start_offset)
            for encoded in self._encoder.iterencode(record):
                self._file.write(encoded.encode("utf-8"))
            self._file.write(b"\n")
            self._sync()
            next_offset = self._file.tell()
        except BaseException as error:
            try:
                self._rollback(start_offset)
            except BaseException as rollback_error:
                self._poisoned = True
                raise SegmentJournalPoisonedError(
                    "Segment journal commit and rollback both failed"
                ) from rollback_error
            raise SegmentJournalCommitError(
                "Segment journal commit failed and was rolled back"
            ) from error

        self._advise_dontneed()
        self._committed_offset = next_offset
        self._chunk_count = next_chunk_count
        self._segment_count = next_segment_count
        self._last_segment = next_last_segment
        self._last_segment_end = next_last_segment_end
        self._all_segments_have_words = next_all_segments_have_words

    def commit_chunk(
        self,
        window: ChunkWindow,
        staged: StagedChunk,
        state: AssemblyState,
    ) -> None:
        """Durably append one validated logical chunk or leave no trace."""

        with self._lock:
            self._require_usable()
            if window.ordinal != self._chunk_count:
                raise SegmentJournalError("Journal chunk ordinal is not sequential")
            if state.completed_chunks != self._chunk_count + 1:
                raise SegmentJournalError("Journal state chunk count is inconsistent")
            if state.cursor != window.core_end:
                raise SegmentJournalError("Journal state cursor is inconsistent")
            if state.segment_count != self._segment_count + len(staged.segments):
                raise SegmentJournalError("Journal state segment count is inconsistent")
            if state.last_segment_end != (
                float(staged.segments[-1]["end"])
                if staged.segments
                else self._last_segment_end
            ):
                raise SegmentJournalError("Journal state segment end is inconsistent")
            record = {
                "version": _JOURNAL_VERSION,
                "kind": "chunk",
                "ordinal": window.ordinal,
                "core_start": window.core_start,
                "core_end": window.core_end,
                "language": state.language,
                "segments": staged.segments,
            }
            self._commit_record(record, staged.segments, chunk_delta=1)

    def append_segment(self, segment: object) -> None:
        """Append one synthetic output segment without materializing prior data."""

        payload = _segment_payload(segment)
        with self._lock:
            self._require_usable()
            if payload["start"] < self._last_segment_end:
                raise NonMonotonicResult("Appended segment overlaps committed output")
            record = {
                "version": _JOURNAL_VERSION,
                "kind": "append",
                "segments": (payload,),
            }
            self._commit_record(record, (payload,), chunk_delta=0)

    def last_segment_payload(self) -> dict[str, object]:
        with self._lock:
            self._require_usable()
            if self._last_segment is None:
                raise IndexError("Segment journal is empty")
            return deepcopy(self._last_segment)

    @staticmethod
    def _decode_record(encoded: bytes) -> dict[str, object]:
        try:
            record = json.loads(encoded.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise SegmentJournalError("Committed journal record is invalid") from error
        if not isinstance(record, dict) or record.get("version") != _JOURNAL_VERSION:
            raise SegmentJournalError("Committed journal version is invalid")
        if record.get("kind") not in {"chunk", "append"}:
            raise SegmentJournalError("Committed journal record kind is invalid")
        segments = record.get("segments")
        if not isinstance(segments, list):
            raise SegmentJournalError("Committed journal segments are invalid")
        if any(not isinstance(segment, dict) for segment in segments):
            raise SegmentJournalError("Committed journal segment is invalid")
        return record

    def iter_committed_chunks(self) -> Iterator[tuple[dict[str, object], ...]]:
        """Yield one decoded committed chunk at a time through a fixed snapshot."""

        with self._lock:
            self._require_usable()
            limit = self._committed_offset
            expected_ordinal = 0
            self._file.seek(0)
            try:
                while self._file.tell() < limit:
                    remaining = limit - self._file.tell()
                    encoded = self._file.readline(remaining)
                    if not encoded.endswith(b"\n"):
                        raise SegmentJournalError(
                            "Committed journal record is truncated"
                        )
                    record = self._decode_record(encoded[:-1])
                    kind = record.get("kind")
                    if kind == "chunk":
                        if record.get("ordinal") != expected_ordinal:
                            raise SegmentJournalError(
                                "Committed journal ordinals are not sequential"
                            )
                        expected_ordinal += 1
                    segments = record.get("segments")
                    yield tuple(segments)
                    self._advise_dontneed()
                if expected_ordinal != self._chunk_count:
                    raise SegmentJournalError(
                        "Committed journal chunk count is inconsistent"
                    )
            finally:
                if not self._closed:
                    self._advise_dontneed()
                    self._file.seek(self._committed_offset)

    def iter_segment_payloads(self) -> Iterator[dict[str, object]]:
        for chunk in self.iter_committed_chunks():
            yield from chunk

    def iter_segment_payloads_reverse(self) -> Iterator[dict[str, object]]:
        """Yield payloads newest-first without retaining a duration-sized index."""

        with self._lock:
            self._require_usable()
            position = self._committed_offset
            expected_ordinal = self._chunk_count - 1
            try:
                while position:
                    self._file.seek(position - 1)
                    if self._file.read(1) != b"\n":
                        raise SegmentJournalError(
                            "Committed journal record is truncated"
                        )
                    record_end = position - 1
                    scan_position = record_end
                    record_start = 0
                    while scan_position:
                        block_start = max(0, scan_position - 64 * 1024)
                        self._file.seek(block_start)
                        block = self._file.read(scan_position - block_start)
                        separator = block.rfind(b"\n")
                        if separator >= 0:
                            record_start = block_start + separator + 1
                            break
                        scan_position = block_start

                    self._file.seek(record_start)
                    encoded = self._file.read(record_end - record_start)
                    record = self._decode_record(encoded)
                    if record["kind"] == "chunk":
                        if record.get("ordinal") != expected_ordinal:
                            raise SegmentJournalError(
                                "Committed journal ordinals are not sequential"
                            )
                        expected_ordinal -= 1
                    yield from reversed(record["segments"])
                    self._advise_dontneed()
                    position = record_start
                if expected_ordinal != -1:
                    raise SegmentJournalError(
                        "Committed journal chunk count is inconsistent"
                    )
            finally:
                if not self._closed:
                    self._advise_dontneed()
                    self._file.seek(self._committed_offset)

    def iter_min_duration_segment_payloads(
        self,
        min_duration: float,
    ) -> Iterator[dict[str, object]]:
        """Apply stable-ts segment-level short-cue merging with bounded state.

        stable-ts processes this fallback newest-to-oldest.  A reverse-framed
        anonymous spool restores forward publication order without retaining a
        transcript-sized Python list or chunk-offset index.
        """

        threshold = _finite(min_duration, "minimum subtitle duration")
        if threshold < 0:
            raise SegmentationError("Minimum subtitle duration must not be negative")
        if self._segment_count == 0:
            return

        source = iter(self.iter_segment_payloads_reverse())
        current_raw = next(source)
        remaining = self._segment_count
        right: dict[str, object] | None = None
        carry: dict[str, object] | None = None

        with tempfile.TemporaryFile(mode="w+b") as reverse_spool:
            pending_spool_bytes = 0

            def flush_reverse_spool() -> None:
                nonlocal pending_spool_bytes
                reverse_spool.flush()
                try:
                    os.fsync(reverse_spool.fileno())
                except (AttributeError, io.UnsupportedOperation, OSError):
                    return
                _best_effort_dontneed(
                    reverse_spool,
                    self._fadvise,
                    self._dontneed_advice,
                )
                pending_spool_bytes = 0

            def write_reverse(payload: Mapping[str, object]) -> None:
                nonlocal pending_spool_bytes
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                reverse_spool.write(encoded)
                reverse_spool.write(_REVERSE_FRAME_LENGTH.pack(len(encoded)))
                pending_spool_bytes += len(encoded) + _REVERSE_FRAME_LENGTH.size
                if pending_spool_bytes >= 1024 * 1024:
                    flush_reverse_spool()

            while remaining:
                left_raw = next(source) if remaining > 1 else None
                current = deepcopy(current_raw)
                if carry is not None:
                    current = _merge_segment_payloads(current, carry)
                    carry = None

                is_short = _segment_duration(current) < threshold
                if not is_short:
                    if right is not None:
                        write_reverse(right)
                    right = current
                elif right is None:
                    if left_raw is None:
                        right = current
                    else:
                        carry = current
                elif left_raw is None:
                    right = _merge_segment_payloads(current, right)
                elif _segment_duration(right) < _segment_duration(left_raw):
                    carry = current
                else:
                    right = _merge_segment_payloads(current, right)

                current_raw = left_raw
                remaining -= 1

            if carry is not None:
                if right is None:
                    right = carry
                else:  # Defensive: the loop normally consumes this at index zero.
                    right = _merge_segment_payloads(carry, right)
            if right is not None:
                write_reverse(right)
            flush_reverse_spool()

            position = reverse_spool.tell()
            try:
                while position:
                    if position < _REVERSE_FRAME_LENGTH.size:
                        raise SegmentJournalError("Minimum-duration spool is truncated")
                    reverse_spool.seek(position - _REVERSE_FRAME_LENGTH.size)
                    encoded_length = reverse_spool.read(_REVERSE_FRAME_LENGTH.size)
                    (length,) = _REVERSE_FRAME_LENGTH.unpack(encoded_length)
                    record_start = position - _REVERSE_FRAME_LENGTH.size - length
                    if record_start < 0:
                        raise SegmentJournalError("Minimum-duration spool is truncated")
                    reverse_spool.seek(record_start)
                    encoded = reverse_spool.read(length)
                    try:
                        payload = json.loads(encoded.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError) as error:
                        raise SegmentJournalError(
                            "Minimum-duration spool is invalid"
                        ) from error
                    if not isinstance(payload, dict):
                        raise SegmentJournalError(
                            "Minimum-duration spool segment is invalid"
                        )
                    yield payload
                    _best_effort_dontneed(
                        reverse_spool,
                        self._fadvise,
                        self._dontneed_advice,
                    )
                    position = record_start
            finally:
                _best_effort_dontneed(
                    reverse_spool,
                    self._fadvise,
                    self._dontneed_advice,
                )

    def finalize(self, state: AssemblyState) -> "SegmentedWhisperResult":  # noqa: UP037
        with self._lock:
            self._require_usable()
            if not state.complete:
                raise SegmentationError("Cannot finalize a partial segmented result")
            if state.completed_chunks != self._chunk_count:
                raise SegmentJournalError("Final assembly chunk count is inconsistent")
            if state.segment_count != self._segment_count:
                raise SegmentJournalError(
                    "Final assembly segment count is inconsistent"
                )
            if state.last_segment_end != self._last_segment_end:
                raise SegmentJournalError("Final assembly segment end is inconsistent")
            if not callable(self._result_factory) or not callable(
                self._segment_factory
            ):
                raise TypeError(
                    "Journal finalization requires result and segment factories"
                )
            return SegmentedWhisperResult(
                journal=self,
                state=state,
                result_factory=self._result_factory,
                segment_factory=self._segment_factory,
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._file.close()

    def __enter__(self) -> "SegmentJournal":  # noqa: PYI034,UP037
        self._require_usable()
        return self

    def __exit__(self, _error_type, _error, _traceback) -> None:
        self.close()


class JournalSegmentSequence(Sequence[object]):
    """List-like lazy segment view backed only by committed journal records."""

    def __init__(
        self,
        journal: SegmentJournal,
        *,
        segment_factory: Callable[..., object],
        result: "SegmentedWhisperResult",  # noqa: UP037
    ) -> None:
        self._journal = journal
        self._segment_factory = segment_factory
        self._result = result

    def __len__(self) -> int:
        return self._journal.segment_count

    def __iter__(self) -> Iterator[object]:
        for segment_id, payload in enumerate(self._journal.iter_segment_payloads()):
            yield _construct_segment(
                payload,
                segment_factory=self._segment_factory,
                segment_id=segment_id,
                result=self._result,
            )

    def __getitem__(self, index):
        if isinstance(index, slice):
            return list(self)[index]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("Segment index must be an integer or slice")
        length = len(self)
        normalized = index + length if index < 0 else index
        if normalized < 0 or normalized >= length:
            raise IndexError("Segment index out of range")
        if normalized == length - 1:
            payload = self._journal.last_segment_payload()
            return _construct_segment(
                payload,
                segment_factory=self._segment_factory,
                segment_id=normalized,
                result=self._result,
            )
        for segment_id, segment in enumerate(self):
            if segment_id == normalized:
                return segment
        raise IndexError("Segment index out of range")

    def append(self, segment: object) -> None:
        self._journal.append_segment(segment)


class SegmentedWhisperResult:
    """Minimal stable-ts-shaped result whose segment storage remains on disk."""

    def __init__(
        self,
        *,
        journal: SegmentJournal,
        state: AssemblyState,
        result_factory: Callable[[dict[str, object]], object],
        segment_factory: Callable[..., object],
    ) -> None:
        self.language = state.language
        self._journal = journal
        self._result_factory = result_factory
        self._segment_factory = segment_factory
        self.segments = JournalSegmentSequence(
            journal,
            segment_factory=segment_factory,
            result=self,
        )

    @property
    def text(self) -> str:
        """Materialize text only for callers that explicitly request it."""

        return "".join(str(segment.text) for segment in self.segments)

    def to_srt_vtt(
        self,
        filepath: str | os.PathLike[str] | None = None,
        *,
        word_level: bool = False,
        vtt: bool = False,
        **render_options,
    ) -> str | None:
        """Render SRT without retaining transcript-sized Python state."""

        if vtt:
            raise NotImplementedError("Disk-backed local results currently render SRT")

        effective_word_level = bool(
            word_level and self._journal.all_segments_have_words
        )
        min_duration = _finite(
            render_options.get("min_dur", _DEFAULT_MIN_DURATION),
            "minimum subtitle duration",
        )
        if min_duration < 0:
            raise SegmentationError("Minimum subtitle duration must not be negative")

        owned_output = filepath is not None
        output = (
            Path(filepath).open("w", encoding="utf-8", newline="")  # noqa: SIM115
            if owned_output
            else io.StringIO()
        )
        next_index = 1
        # stable-ts applies the whole-result minimum-duration pass before it
        # decides whether word-level output is possible.  Normalize that pass
        # across every journal boundary, then render one cue at a time.
        render_batches: Iterator[Sequence[Mapping[str, object]]] = (
            (segment,)
            for segment in self._journal.iter_min_duration_segment_payloads(
                min_duration
            )
        )
        try:
            for chunk in render_batches:
                bounded = _build_bounded_result(
                    chunk,
                    language=self.language,
                    result_factory=self._result_factory,
                    segment_factory=self._segment_factory,
                )
                # WhisperResult.apply_min_dur() applies the word pass whenever
                # the source had multiple segments, even if segment merging
                # leaves only one.  A one-cue bounded WhisperResult returns
                # early, so invoke the same stable-ts Segment method directly.
                if self._journal.segment_count > 1:
                    for segment in _field(bounded, "segments", ()):
                        apply_min_duration = getattr(
                            segment,
                            "apply_min_dur",
                            None,
                        )
                        if callable(apply_min_duration):
                            apply_min_duration(min_duration, inplace=True)
                fragment = bounded.to_srt_vtt(
                    filepath=None,
                    word_level=effective_word_level,
                    vtt=False,
                    **render_options,
                )
                if not isinstance(fragment, str):
                    raise ResultConstructionError(
                        "Bounded SRT renderer did not return text"
                    )
                next_index = _write_renumbered_srt(
                    output,
                    fragment,
                    first_index=next_index,
                )
            if owned_output:
                return None
            return output.getvalue()
        finally:
            if owned_output:
                output.close()

    def close(self) -> None:
        """Idempotently release the anonymous transcript spool."""

        self._journal.close()

    def __enter__(self) -> "SegmentedWhisperResult":  # noqa: PYI034,UP037
        if self._journal.closed:
            raise SegmentJournalError("Segmented result is closed")
        return self

    def __exit__(self, _error_type, _error, _traceback) -> None:
        self.close()


__all__ = [
    "JournalSegmentSequence",
    "ResultConstructionError",
    "SegmentJournal",
    "SegmentJournalCommitError",
    "SegmentJournalError",
    "SegmentJournalPoisonedError",
    "SegmentedWhisperResult",
]
