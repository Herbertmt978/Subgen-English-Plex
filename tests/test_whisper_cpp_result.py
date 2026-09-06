import json

import pytest

from subgen_core.whisper_cpp_result import (
    MAX_RESULT_BYTES, WhisperCppResultError, decode_whisper_cpp_result,
)


def document(segments=None):
    return {
        "params": {"model": "/private/models/base.bin"},
        "result": {"language": "en"},
        "transcription": segments if segments is not None else [{
            "offsets": {"from": 320, "to": 2000},
            "text": " Hello world.",
            "tokens": [{"text": "[_BEG_]"}, {"text": " Hel"}, {"text": "lo"}],
        }],
    }


def decode(value, duration=3):
    return decode_whisper_cpp_result(json.dumps(value).encode(), duration_seconds=duration)


def test_preserves_actual_offsets_and_text_without_claiming_token_word_alignment():
    result = decode(document())
    assert result == {
        "language": "en", "text": " Hello world.",
        "segments": [{"start": .32, "end": 2., "text": " Hello world."}],
    }
    assert "/private" not in str(result)
    assert "tokens" not in result["segments"][0]
    assert "words" not in result["segments"][0]


@pytest.mark.parametrize("start,end", [(-1, 100), (200, 100), (0, 3001), (True, 100), (0.5, 100), (0, 0)])
def test_invalid_times_are_rejected_not_repaired(start, end):
    with pytest.raises(WhisperCppResultError):
        decode(document([{"offsets": {"from": start, "to": end}, "text": "speech"}]))


def test_overlapping_segments_are_rejected():
    with pytest.raises(WhisperCppResultError, match="overlapping"):
        decode(document([
            {"offsets": {"from": 0, "to": 2000}, "text": "one"},
            {"offsets": {"from": 1000, "to": 3000}, "text": "two"},
        ]))


def test_captured_native_terminal_overrun_still_requires_producer_boundary_handling():
    # Native code now owns its PCM boundary; the consumer must not repair raw
    # untrusted endpoints, including this captured 310-second GPU regression.
    segment = {"offsets": {"from": 304780, "to": 311420}, "text": " final words"}
    with pytest.raises(WhisperCppResultError, match="out of bounds"):
        decode(document([segment]), duration=310)
    segment["offsets"]["to"] = 310000
    assert decode(document([segment]), duration=310)["segments"] == [
        {"start": 304.78, "end": 310.0, "text": " final words"},
    ]


def test_empty_result_is_preserved_as_empty_not_fabricated_speech():
    assert decode(document([]))["segments"] == []


def test_digital_silence_does_not_invent_a_detected_language():
    value = document([])
    value["result"]["language"] = "und"
    assert decode(value) == {"language": "und", "segments": [], "text": ""}


def test_existing_segment_owner_applies_extraction_offset_once():
    from subgen_core.segmentation import ChunkWindow, stage_chunk_result
    result = decode(document([{
        "offsets": {"from": 6000, "to": 8000}, "text": "later speech",
    }]), duration=20)
    staged = stage_chunk_result(result, ChunkWindow(1, 30, 10, 20, 5, 25))
    segment, = staged.segments
    assert (segment["start"], segment["end"], segment["text"]) == (11, 13, "later speech")
    assert "words" not in segment


@pytest.mark.parametrize("duration", [0, -1, True, float("nan"), float("inf"), 86_401])
def test_invalid_audio_duration(duration):
    with pytest.raises(WhisperCppResultError):
        decode(document(), duration)


@pytest.mark.parametrize("payload", [
    b'{"result":1,"result":2}', b'{"result":NaN}', b'\xff', b'[]', b'{}',
])
def test_invalid_or_unbounded_json(payload):
    with pytest.raises(WhisperCppResultError):
        decode_whisper_cpp_result(payload, duration_seconds=3)


def test_oversized_json_is_rejected_before_decoding():
    with pytest.raises(WhisperCppResultError, match="byte limit"):
        decode_whisper_cpp_result(b"x" * (MAX_RESULT_BYTES + 1), duration_seconds=3)


def test_invalid_unicode_is_rejected_before_subtitle_rendering():
    value = document()
    value["transcription"][0]["text"] = "\ud800"
    with pytest.raises(WhisperCppResultError, match="Unicode"):
        decode(value)


def test_extreme_json_integer_is_a_result_error():
    with pytest.raises(WhisperCppResultError):
        decode_whisper_cpp_result(b'{"value":' + b"9" * 5000 + b'}', duration_seconds=3)


@pytest.mark.parametrize("language", [None, "", "auto", "en\n", "../private"])
def test_invalid_detected_language(language):
    value = document()
    value["result"]["language"] = language
    with pytest.raises(WhisperCppResultError):
        decode(value)
