import os
import subprocess
import sys
import textwrap
from copy import deepcopy
from pathlib import Path

import pytest

from subgen_core import segmentation, segmented_result

_MISSING = object()


class FakeWord:
    def __init__(
        self,
        word,
        start,
        end,
        probability=None,
        tokens=None,
        **_kwargs,
    ):
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability
        self.tokens = deepcopy(tokens)
        self.id = None
        self.segment = None

    def to_dict(self):
        return {
            "word": self.word,
            "start": self.start,
            "end": self.end,
            "probability": self.probability,
            "tokens": deepcopy(self.tokens),
        }


class FakeSegment:
    def __init__(
        self,
        start,
        end,
        text,
        words=_MISSING,
        tokens=None,
        id=None,
        ignore_unused_args=False,
        **_kwargs,
    ):
        self.start = start
        self.end = end
        self.text = text
        self.tokens = deepcopy(tokens)
        self.id = id
        self.result = None
        self._words_present = words is not _MISSING
        self.words = (
            None
            if words is _MISSING or words is None
            else [
                word if isinstance(word, FakeWord) else FakeWord(**word)
                for word in words
            ]
        )

    def to_dict(self):
        payload = {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "tokens": deepcopy(self.tokens),
        }
        if self._words_present:
            payload["words"] = (
                None if self.words is None else [word.to_dict() for word in self.words]
            )
        return payload


def _payload(segment):
    return segment.to_dict() if isinstance(segment, FakeSegment) else deepcopy(segment)


def _duration(segment):
    return float(segment["end"]) - float(segment["start"])


def _merge(left, right):
    merged = deepcopy(left)
    merged["end"] = right["end"]
    merged["text"] = f"{left['text']}{right['text']}"
    left_tokens = left.get("tokens")
    right_tokens = right.get("tokens")
    merged["tokens"] = (
        deepcopy(left_tokens) + deepcopy(right_tokens)
        if isinstance(left_tokens, list) and isinstance(right_tokens, list)
        else None
    )
    if isinstance(left.get("words"), list) and isinstance(right.get("words"), list):
        merged["words"] = deepcopy(left["words"]) + deepcopy(right["words"])
    else:
        merged.pop("words", None)
    return merged


def _stable_segment_min_duration(segments, minimum):
    normalized = [deepcopy(segment) for segment in segments]
    for index in reversed(range(len(normalized))):
        if _duration(normalized[index]) >= minimum or len(normalized) == 1:
            continue
        if index == len(normalized) - 1:
            normalized[index - 1] = _merge(
                normalized[index - 1],
                normalized[index],
            )
            normalized.pop(index)
        elif index == 0:
            normalized[0] = _merge(normalized[0], normalized[1])
            normalized.pop(1)
        elif _duration(normalized[index + 1]) < _duration(normalized[index - 1]):
            normalized[index - 1] = _merge(
                normalized[index - 1],
                normalized[index],
            )
            normalized.pop(index)
        else:
            normalized[index] = _merge(normalized[index], normalized[index + 1])
            normalized.pop(index + 1)
    return normalized


class FakeWhisperResult:
    def __init__(self, payload, render_calls=None):
        self.language = payload.get("language")
        self.segments = [FakeSegment(**segment) for segment in payload["segments"]]
        self._render_calls = render_calls
        self.reassign_ids()

    def reassign_ids(self):
        for segment_id, segment in enumerate(self.segments):
            segment.id = segment_id
            segment.result = self
            for word_id, word in enumerate(segment.words or ()):
                word.id = word_id
                word.segment = segment

    def to_srt_vtt(
        self,
        filepath=None,
        *,
        word_level=False,
        vtt=False,
        min_dur=0.02,
        **_options,
    ):
        assert filepath is None
        assert vtt is False
        if self._render_calls is not None:
            self._render_calls.append((word_level, len(self.segments)))
        payloads = [segment.to_dict() for segment in self.segments]
        payloads = _stable_segment_min_duration(payloads, min_dur)
        return "\n\n".join(
            f"{index}\n{segment['start']:.3f} --> {segment['end']:.3f}\n"
            f"{segment['text']}"
            for index, segment in enumerate(payloads, 1)
        )


class RecordingResultFactory:
    def __init__(self):
        self.payload_sizes = []
        self.render_calls = []

    def __call__(self, payload):
        self.payload_sizes.append(len(payload["segments"]))
        return FakeWhisperResult(payload, self.render_calls)


def make_window(start, end, *, duration, ordinal):
    return segmentation.ChunkWindow(
        media_duration=duration,
        core_start=start,
        core_end=end,
        extract_start=start,
        extract_end=end,
        ordinal=ordinal,
    )


def word_segment(start, end, text):
    return {
        "start": start,
        "end": end,
        "text": text,
        "tokens": [1],
        "words": [
            {
                "word": text,
                "start": start,
                "end": end,
                "probability": 0.9,
                "tokens": [1],
            }
        ],
    }


def wordless_segment(start, end, text):
    return {
        "start": start,
        "end": end,
        "text": text,
        "tokens": [],
    }


def commit(journal, state, window, *segments, language="en"):
    staged = segmentation.StagedChunk(
        language=language,
        segments=tuple(deepcopy(segments)),
    )
    return segmentation.commit_chunk(
        state,
        window,
        staged,
        persist_chunk=journal.commit_chunk,
    )


def make_journal(**kwargs):
    factory = RecordingResultFactory()
    journal = segmented_result.SegmentJournal(
        result_factory=factory,
        segment_factory=FakeSegment,
        **kwargs,
    )
    return journal, factory


@pytest.mark.parametrize('direction', ['iter_committed_chunks', 'iter_segment_payloads_reverse'])
def test_journal_iterator_can_be_closed_by_another_thread(direction):
    from concurrent.futures import ThreadPoolExecutor
    journal, _ = make_journal()
    state = segmentation.AssemblyState(media_duration=2)
    commit(journal, state, make_window(0, 2, duration=2, ordinal=0),
           word_segment(.1, .8, ' first'), word_segment(1.1, 1.8, ' last'))
    iterator = getattr(journal, direction)()
    try:
        next(iterator)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(iterator.close).result(timeout=2)
    finally:
        journal.close()


@pytest.mark.parametrize('direction', ['iter_segment_payloads', 'iter_segment_payloads_reverse'])
def test_journal_iterator_keeps_snapshot_while_another_thread_commits(direction):
    from concurrent.futures import ThreadPoolExecutor
    journal, _ = make_journal()
    state = segmentation.AssemblyState(media_duration=3)
    for ordinal in range(2):
        state = commit(journal, state, make_window(ordinal, ordinal+1, duration=3, ordinal=ordinal),
                       word_segment(ordinal+.1, ordinal+.8, f' item{ordinal}'))
    iterator = getattr(journal, direction)()
    try:
        payloads = [next(iterator)]
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(commit, journal, state, make_window(2, 3, duration=3, ordinal=2),
                        word_segment(2.1, 2.8, ' later')).result(timeout=2)
        payloads.extend(iterator)
        assert sorted(item['text'] for item in payloads) == [' item0', ' item1']
        assert len(list(journal.iter_segment_payloads())) == 3
    finally:
        iterator.close()
        journal.close()


def test_journal_finalizes_a_lazy_mixed_result_with_stable_ids_and_backrefs():
    journal, _factory = make_journal()
    state = segmentation.AssemblyState(media_duration=2)
    state = commit(
        journal,
        state,
        make_window(0, 1, duration=2, ordinal=0),
        word_segment(0.1, 0.8, " first"),
    )
    state = commit(
        journal,
        state,
        make_window(1, 2, duration=2, ordinal=1),
        wordless_segment(1.1, 1.8, "[music]"),
    )

    result = journal.finalize(state)

    assert not isinstance(result.segments, list)
    assert len(result.segments) == 2
    segments = list(result.segments)
    assert [segment.id for segment in segments] == [0, 1]
    assert all(segment.result is result for segment in segments)
    assert segments[0].words[0].id == 0
    assert segments[0].words[0].segment is segments[0]
    assert segments[1].words is None
    assert result.text == " first[music]"
    assert state.segment_count == 2
    assert not hasattr(state, "segments")

    result.close()


def test_failed_commit_rolls_back_before_a_same_cursor_retry():
    calls = 0

    def flaky_fsync(file_descriptor):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected journal sync failure")
        os.fsync(file_descriptor)

    journal, _factory = make_journal(fsync=flaky_fsync)
    state = segmentation.AssemblyState(media_duration=1)
    window = make_window(0, 1, duration=1, ordinal=0)

    with pytest.raises(segmented_result.SegmentJournalCommitError):
        commit(journal, state, window, wordless_segment(0.1, 0.9, "retry"))

    assert journal.committed_offset == 0
    assert journal.chunk_count == 0
    assert journal.segment_count == 0
    assert list(journal.iter_segment_payloads()) == []

    state = commit(journal, state, window, wordless_segment(0.1, 0.9, "retry"))
    assert state.complete is True
    assert [item["text"] for item in journal.iter_segment_payloads()] == ["retry"]
    journal.close()


def test_failed_commit_and_rollback_poison_the_journal():
    journal, _factory = make_journal(
        fsync=lambda _file_descriptor: (_ for _ in ()).throw(OSError("sync failed"))
    )
    state = segmentation.AssemblyState(media_duration=1)

    with pytest.raises(segmented_result.SegmentJournalPoisonedError):
        commit(
            journal,
            state,
            make_window(0, 1, duration=1, ordinal=0),
            wordless_segment(0.1, 0.9, "never committed"),
        )

    assert journal.poisoned is True
    with pytest.raises(segmented_result.SegmentJournalPoisonedError):
        list(journal.iter_segment_payloads())
    journal.close()


def test_clean_spool_pages_are_advised_away_after_commit_and_reads():
    advice_calls = []

    def fadvise(*args):
        advice_calls.append(args)

    journal, _factory = make_journal(fadvise=fadvise, dontneed_advice=91)
    state = commit(
        journal,
        segmentation.AssemblyState(media_duration=1),
        make_window(0, 1, duration=1, ordinal=0),
        wordless_segment(0.1, 0.9, "cached"),
    )
    assert state.complete is True
    assert next(iter(journal.iter_segment_payloads()))["text"] == "cached"

    assert len(advice_calls) >= 2
    assert all(call[1:] == (0, 0, 91) for call in advice_calls)
    journal.close()


def test_page_cache_advice_failure_never_changes_journal_correctness():
    journal, _factory = make_journal(
        fadvise=lambda *_args: (_ for _ in ()).throw(OSError("unsupported")),
        dontneed_advice=91,
    )
    state = commit(
        journal,
        segmentation.AssemblyState(media_duration=1),
        make_window(0, 1, duration=1, ordinal=0),
        wordless_segment(0.1, 0.9, "still committed"),
    )

    assert state.complete is True
    assert [item["text"] for item in journal.iter_segment_payloads()] == [
        "still committed"
    ]
    journal.close()


def test_appended_wordless_credit_forces_global_segment_level_rendering():
    journal, factory = make_journal()
    state = segmentation.AssemblyState(media_duration=2)
    state = commit(
        journal,
        state,
        make_window(0, 1, duration=2, ordinal=0),
        word_segment(0.1, 0.8, " first"),
    )
    state = commit(
        journal,
        state,
        make_window(1, 2, duration=2, ordinal=1),
        word_segment(1.1, 1.8, " second"),
    )
    result = journal.finalize(state)
    result.segments.append(FakeSegment(2.1, 2.8, " credit", words=[]))

    rendered = result.to_srt_vtt(word_level=True)

    assert journal.all_segments_have_words is False
    assert [word_level for word_level, _size in factory.render_calls] == [
        False,
        False,
        False,
    ]
    assert rendered.count(" --> ") == 3
    assert " first" in rendered
    assert " second" in rendered
    assert " credit" in rendered
    assert result.segments[-1].id == 2
    result.close()


def test_sub_20ms_segment_at_chunk_boundary_matches_whole_result_contract():
    journal, _factory = make_journal()
    state = segmentation.AssemblyState(media_duration=3)
    first = wordless_segment(0.5, 1.0, "A")
    tiny = wordless_segment(1.0, 1.01, " tiny")
    last = wordless_segment(1.01, 2.5, " B")
    state = commit(
        journal,
        state,
        make_window(0, 1, duration=3, ordinal=0),
        first,
    )
    state = commit(
        journal,
        state,
        make_window(1, 3, duration=3, ordinal=1),
        tiny,
        last,
    )
    result = journal.finalize(state)
    whole_result = FakeWhisperResult(
        {"language": "en", "segments": [first, tiny, last]}
    )

    rendered = result.to_srt_vtt()

    assert rendered == whole_result.to_srt_vtt()
    assert "1.000 --> 2.500\n tiny B" in rendered
    assert rendered.count(" --> ") == 2
    result.close()


def test_word_timed_sub_20ms_boundary_segment_uses_the_whole_result_neighbor():
    journal, _factory = make_journal()
    state = segmentation.AssemblyState(media_duration=3)
    first = word_segment(0.1, 1.0, " left")
    tiny = word_segment(1.0, 1.01, " tiny")
    last = word_segment(1.01, 1.3, " right")
    state = commit(
        journal,
        state,
        make_window(0, 1, duration=3, ordinal=0),
        first,
    )
    state = commit(
        journal,
        state,
        make_window(1, 3, duration=3, ordinal=1),
        tiny,
        last,
    )
    result = journal.finalize(state)
    whole_result = FakeWhisperResult(
        {"language": "en", "segments": [first, tiny, last]}
    )

    rendered = result.to_srt_vtt(word_level=False)

    assert rendered == whole_result.to_srt_vtt(word_level=False)
    assert "0.100 --> 1.010\n left tiny" in rendered
    assert rendered.count(" --> ") == 2
    result.close()


def test_short_mixed_word_state_deliberately_falls_back_instead_of_raising():
    """Preserve output where stable-ts 2.19.1 rejects mixed ori_has_words."""

    journal, _factory = make_journal()
    state = segmentation.AssemblyState(media_duration=2)
    state = commit(
        journal,
        state,
        make_window(0, 1, duration=2, ordinal=0),
        wordless_segment(0.0, 0.5, "left"),
    )
    state = commit(
        journal,
        state,
        make_window(1, 2, duration=2, ordinal=1),
        word_segment(0.5, 0.51, " tiny"),
        wordless_segment(0.51, 2.0, " right"),
    )
    result = journal.finalize(state)

    rendered = result.to_srt_vtt(word_level=True)

    assert rendered == (
        "1\n0.000 --> 0.500\nleft\n\n"
        "2\n0.500 --> 2.000\n tiny right"
    )
    assert journal.all_segments_have_words is False
    result.close()


_STABLE_TS_2_19_1_GOLDEN = textwrap.dedent(
    r"""
    from types import SimpleNamespace

    import stable_whisper

    from subgen_core import segmentation, segmented_result

    assert stable_whisper.__version__ == "2.19.1"

    def word_segment(start, end, text):
        return {
            "start": start,
            "end": end,
            "text": text,
            "tokens": [1],
            "words": [
                {
                    "word": text,
                    "start": start,
                    "end": end,
                    "probability": 0.9,
                    "tokens": [1],
                }
            ],
        }

    def window(core_start, core_end, extract_start, extract_end, ordinal):
        return segmentation.ChunkWindow(
            media_duration=4.0,
            core_start=core_start,
            core_end=core_end,
            extract_start=extract_start,
            extract_end=extract_end,
            ordinal=ordinal,
        )

    def stage_and_commit(journal, state, chunk_window, segments):
        staged = segmentation.stage_chunk_result(
            SimpleNamespace(language="en", segments=segments),
            chunk_window,
        )
        return segmentation.commit_chunk(
            state,
            chunk_window,
            staged,
            persist_chunk=journal.commit_chunk,
        )

    journal = segmented_result.SegmentJournal(
        result_factory=stable_whisper.WhisperResult,
        segment_factory=stable_whisper.Segment,
    )
    try:
        state = segmentation.AssemblyState(media_duration=4.0)
        state = stage_and_commit(
            journal,
            state,
            window(0.0, 2.0, 0.0, 2.2, 0),
            [word_segment(1.1, 2.1, " left")],
        )
        state = stage_and_commit(
            journal,
            state,
            window(2.0, 3.0, 1.0, 3.2, 1),
            [
                word_segment(0.1, 1.1, " DUPLICATE"),
                word_segment(1.0, 1.01, " tiny"),
                word_segment(1.01, 1.3, " right"),
            ],
        )
        state = stage_and_commit(
            journal,
            state,
            window(3.0, 4.0, 2.8, 4.0, 2),
            [],
        )

        payloads = list(journal.iter_segment_payloads())
        assert [segment["text"] for segment in payloads] == [
            " left",
            " tiny",
            " right",
        ]
        bounded = journal.finalize(state)
        whole = stable_whisper.WhisperResult(
            {"language": "en", "segments": payloads}
        )

        for word_level in (False, True):
            actual = bounded.to_srt_vtt(word_level=word_level)
            expected = whole.to_srt_vtt(word_level=word_level)
            assert actual == expected, (word_level, expected, actual)

        bounded.segments.append(
            stable_whisper.Segment(
                start=4.0,
                end=4.5,
                text=" credit",
                words=[],
            )
        )
        whole.segments.append(
            stable_whisper.Segment(
                start=4.0,
                end=4.5,
                text=" credit",
                words=[],
            )
        )
        whole.reassign_ids()
        for word_level in (False, True):
            actual = bounded.to_srt_vtt(word_level=word_level)
            expected = whole.to_srt_vtt(word_level=word_level)
            assert actual == expected, ("credit", word_level, expected, actual)
    finally:
        journal.close()
    """
)


def test_real_stable_ts_2_19_1_whole_result_golden_contract():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import stable_whisper; print(stable_whisper.__version__)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip("stable-ts is unavailable in this lightweight test environment")
    installed_version = probe.stdout.strip().splitlines()[-1]
    if installed_version != "2.19.1":
        pytest.skip(f"exact stable-ts 2.19.1 gate, found {installed_version}")

    completed = subprocess.run(
        [sys.executable, "-c", _STABLE_TS_2_19_1_GOLDEN],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_twelve_hour_publication_keeps_factory_batches_chunk_bounded(tmp_path):
    chunk_seconds = 60
    chunk_count = 12 * 60
    duration = chunk_seconds * chunk_count
    journal, factory = make_journal()
    state = segmentation.AssemblyState(media_duration=duration)

    for ordinal in range(chunk_count):
        start = ordinal * chunk_seconds
        end = start + chunk_seconds
        state = commit(
            journal,
            state,
            make_window(start, end, duration=duration, ordinal=ordinal),
            wordless_segment(start + 0.1, start + 1.0, f" cue {ordinal}"),
        )

    result = journal.finalize(state)
    output = tmp_path / "twelve-hours.srt"

    assert result.to_srt_vtt(output) is None
    assert state.segment_count == chunk_count
    assert not hasattr(state, "segments")
    assert max(factory.payload_sizes) == 1
    assert output.read_text(encoding="utf-8").count(" --> ") == chunk_count
    result.close()


def test_segmented_result_close_and_context_manager_are_idempotent():
    journal, _factory = make_journal()
    state = commit(
        journal,
        segmentation.AssemblyState(media_duration=1),
        make_window(0, 1, duration=1, ordinal=0),
        wordless_segment(0.1, 0.9, "cleanup"),
    )
    result = journal.finalize(state)

    with result as entered:
        assert entered is result
    result.close()

    assert journal.closed is True
    with pytest.raises(segmented_result.SegmentJournalError, match="closed"):
        list(result.segments)


def test_filepath_publication_never_materializes_a_combined_result_string(tmp_path):
    journal, factory = make_journal()
    state = commit(
        journal,
        segmentation.AssemblyState(media_duration=2),
        make_window(0, 2, duration=2, ordinal=0),
        wordless_segment(0.1, 0.9, "one"),
        wordless_segment(1.0, 1.9, "two"),
    )
    result = journal.finalize(state)
    output = Path(tmp_path, "bounded.srt")

    assert result.to_srt_vtt(output) is None
    assert output.read_text(encoding="utf-8").count(" --> ") == 2
    assert max(factory.payload_sizes) == 1
    result.close()
