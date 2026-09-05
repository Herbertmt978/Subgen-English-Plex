"""Synthetic timing regression probe for the actual installed backend, no model.

Run inside the candidate image, independently of pytest's ML stubs:
python /path/to/check_stable_ts_timing.py
"""

from copy import deepcopy


def check_timing(result_type, unsorted_exception) -> int:
    words = [
        {"word": " first", "start": 1.0, "end": 2.0},
        {"word": " middle", "start": 3.0, "end": 2.5},
        {"word": " last", "start": 4.0, "end": 5.0},
    ]
    empty = {"start": 0.0, "end": 0.5, "text": "", "words": []}

    def make(segments, **kwargs):
        return result_type(
            {"language": "en", "segments": deepcopy(segments)},
            show_unsorted=False,
            **kwargs,
        )

    def signature(result):
        return [(w.word, w.start, w.end) for w in result.all_words()]

    def assert_ordered(result):
        times = [float(t) for w in result.all_words() for t in (w.start, w.end)]
        assert all(a <= b for a, b in zip(times, times[1:])), "Nonmonotonic word timing"

    checks = 0
    # An empty leading/middle/trailing segment must not hide reversed words.
    for position in range(3):
        segments = [{"words": words[:1]}, {"words": words[1:]}]
        segments.insert(position, empty)
        corrected = make(segments, force_order=True)
        baseline = make([{"words": words[:1]}, {"words": words[1:]}], force_order=True)
        assert signature(corrected) == signature(baseline)
        assert [w.word for w in corrected.all_words()] == [w["word"] for w in words]
        assert_ordered(corrected)
        corrected.clamp_max().split_by_length(max_chars=84).split_by_length(max_chars=42, newline=True)
        assert_ordered(corrected)
        assert [w.word.strip() for w in corrected.all_words()] == [w["word"].strip() for w in words]
        checks += 1

    # The exact interior-word case: valid outer bounds can conceal the error.
    mixed = [empty, {"words": words}]
    result = make(mixed, force_order=True)
    assert_ordered(result)
    assert signature(result) == signature(make([{"words": words}], force_order=True))
    checks += 1

    # When callers forbid correction, validation must reject, not silently pass.
    try:
        make(mixed, force_order=False)
    except unsorted_exception:
        checks += 1
    else:
        raise AssertionError("Strict mode overlooked reversed interior words")

    valid_words = deepcopy(words)
    valid_words[1]["end"] = 3.5
    for force_order in (True, False):
        valid = make([empty, {"words": valid_words}], force_order=force_order)
        assert signature(valid) == [(w["word"], w["start"], w["end"]) for w in valid_words]
        checks += 1

    # Non-word-timestamp results and empty results retain their existing shape.
    wordless = make([{"start": 1.0, "end": 2.0, "text": " plain"}], force_order=True)
    assert len(wordless.segments) == 1
    segment = wordless.segments[0]
    assert (segment.start, segment.end, segment.text) == (1.0, 2.0, " plain")
    checks += 1
    # Explicitly empty word arrays are removed by the existing backend too.
    assert make([empty], force_order=True).segments == []
    checks += 1
    assert make([], force_order=True).segments == []
    checks += 1
    return checks


if __name__ == "__main__":
    from stable_whisper.result import WhisperResult, UnsortedException

    print(f"Installed stable-ts timing checks passed: {check_timing(WhisperResult, UnsortedException)}")
