"""Bounded whisper.cpp JSON conversion for the experimental Vulkan adapter.

Use reported segment offsets unchanged. Token timings are not word alignment;
the existing segment-only seam path owns trimming and ordered publication.
This converter neither launches inference nor establishes device/model identity.
"""

from __future__ import annotations

import json
import math
import re


MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_SEGMENTS = 20_000
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[a-z]{2,4})?\Z")


class WhisperCppResultError(ValueError):
    """The backend result cannot be represented without guessing or retiming."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WhisperCppResultError("Backend JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(_value):
    raise WhisperCppResultError("Backend JSON contains a non-finite number")


def _milliseconds(value, label):
    if type(value) is not int or not 0 <= value <= 86_400_000:
        raise WhisperCppResultError(f"{label} must be non-negative integer milliseconds")
    return value


def decode_whisper_cpp_result(payload: bytes, *, duration_seconds: float) -> dict:
    """Decode a bounded full-JSON result using the current extracted audio length."""
    if not isinstance(payload, bytes) or len(payload) > MAX_RESULT_BYTES:
        raise WhisperCppResultError("Backend JSON exceeds its byte limit or is not bytes")
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(duration_seconds)
        or not 0 < duration_seconds <= 86_400
    ):
        raise WhisperCppResultError("Extracted audio duration must be finite and positive")
    try:
        document = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
    except WhisperCppResultError:
        raise
    except (UnicodeError, ValueError, RecursionError) as error:
        raise WhisperCppResultError("Backend JSON is invalid") from error
    if not isinstance(document, dict):
        raise WhisperCppResultError("Backend result must be a JSON object")
    metadata = document.get("result")
    language = metadata.get("language") if isinstance(metadata, dict) else None
    if not isinstance(language, str) or _LANGUAGE.fullmatch(language) is None:
        raise WhisperCppResultError("Backend result has no valid detected language")
    source_segments = document.get("transcription")
    if not isinstance(source_segments, list) or len(source_segments) > MAX_SEGMENTS:
        raise WhisperCppResultError("Backend transcription must be a bounded segment list")
    segments = []
    previous_end = 0
    for source in source_segments:
        if not isinstance(source, dict) or not isinstance(source.get("offsets"), dict):
            raise WhisperCppResultError("Backend segment has no millisecond offsets")
        offsets = source["offsets"]
        start = _milliseconds(offsets.get("from"), "Segment start")
        end = _milliseconds(offsets.get("to"), "Segment end")
        if start < previous_end or end < start or end > duration_seconds * 1000:
            raise WhisperCppResultError("Backend segment timing is reversed, overlapping or out of bounds")
        text = source.get("text")
        if not isinstance(text, str) or "\x00" in text:
            raise WhisperCppResultError("Backend segment text is invalid")
        try:
            text.encode("utf-8")
        except UnicodeError as error:
            raise WhisperCppResultError("Backend segment text is not valid Unicode") from error
        if text.strip() and end == start:
            raise WhisperCppResultError("Backend speech has zero duration")
        # Validate all source intervals, including silent/empty entries.
        previous_end = end
        if text.strip():
            segments.append({"start": start / 1000, "end": end / 1000, "text": text})
    return {
        "language": language,
        "segments": segments,
        "text": "".join(segment["text"] for segment in segments),
    }
