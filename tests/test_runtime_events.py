from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from subgen_core import runtime_events


def event_document(line: str) -> dict[str, object]:
    assert line.startswith(runtime_events.SENTINEL)
    return json.loads(line.removeprefix(runtime_events.SENTINEL))


def test_multichunk_success_line_is_canonical_one_line_and_privacy_safe() -> None:
    workload_id = "a" * 32

    line = runtime_events.multichunk_success_line(
        workload_id=workload_id,
        chunks_total=6,
        event_sequence=7,
        monotonic_ns=8_000_000_009,
    )

    assert line == (
        'SUBGEN_RUNTIME_EVENT {"atomic_publish":"succeeded","chunks_total":6,'
        '"event":"multichunk_transcription_completed","event_sequence":7,'
        '"monotonic_ns":8000000009,"outcome":"success",'
        '"schema":"subgen.runtime-event/v1",'
        '"workload_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
    )
    assert "\n" not in line
    assert event_document(line) == {
        "atomic_publish": "succeeded",
        "chunks_total": 6,
        "event": "multichunk_transcription_completed",
        "event_sequence": 7,
        "monotonic_ns": 8_000_000_009,
        "outcome": "success",
        "schema": "subgen.runtime-event/v1",
        "workload_id": workload_id,
    }
    for private_fragment in ("/media/", "episode.mkv", "film title", "subtitle text"):
        assert private_fragment not in line


@pytest.mark.parametrize("chunks_total", [True, -1, 0, 1, 1.5, "2"])
def test_multichunk_success_line_rejects_non_multichunk_counts(chunks_total) -> None:
    with pytest.raises(ValueError, match="multi-chunk"):
        runtime_events.multichunk_success_line(
            workload_id="b" * 32,
            chunks_total=chunks_total,
            event_sequence=1,
            monotonic_ns=1,
        )


def test_emit_multichunk_success_uses_adjacent_process_sequences(monkeypatch) -> None:
    monotonic_values = iter((100, 200))
    monkeypatch.setattr(runtime_events.time, "monotonic_ns", monotonic_values.__next__)
    runtime = SimpleNamespace(logging=MagicMock())

    first = runtime_events.emit_multichunk_success(
        runtime,
        workload_id="c" * 32,
        chunks_total=2,
    )
    second = runtime_events.emit_multichunk_success(
        runtime,
        workload_id="d" * 32,
        chunks_total=3,
    )

    first_document = event_document(first)
    second_document = event_document(second)
    assert second_document["event_sequence"] == first_document["event_sequence"] + 1
    assert first_document["monotonic_ns"] == 100
    assert second_document["monotonic_ns"] == 200
    assert [entry.args for entry in runtime.logging.info.call_args_list] == [
        ("%s", first),
        ("%s", second),
    ]
