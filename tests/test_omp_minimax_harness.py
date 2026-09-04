"""Deterministic tests for the OMP/MiniMax comparison harness."""
from __future__ import annotations

import json

from .omp_minimax_harness import (
    _StreamAccumulator,
    _skill_path,
    replay_compaction_state,
)


def test_skill_path_is_redacted_to_internal_uri_only():
    assert _skill_path(json.dumps({"path": "skill://delivery-lifecycle?x=1"})) == (
        "skill://delivery-lifecycle"
    )
    assert _skill_path(json.dumps({"path": "/private/file"})) is None


def test_compaction_replay_shows_preserved_state_prevents_repeats():
    lossy = replay_compaction_state(repeats=2, compaction_mode="lossy")
    preserved = replay_compaction_state(repeats=2, compaction_mode="preserved")

    assert lossy["eligible_repeats_after_compaction"] == 4
    assert lossy["repeats_prevented_by_preserved_state"] == 0
    assert preserved["eligible_repeats_after_compaction"] == 4
    assert preserved["repeats_prevented_by_preserved_state"] == 4


def test_stream_accumulator_tracks_indexes_by_call_id():
    accumulator = _StreamAccumulator()
    accumulator.consume({
        "choices": [{
            "index": 0,
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call-a",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"pa'},
                }],
            },
        }],
    })
    accumulator.consume({
        "choices": [{
            "index": 0,
            "delta": {
                "tool_calls": [
                    {
                        "index": 0,
                        "function": {
                            "arguments": 'th":"skill://delivery-lifecycle"}',
                        },
                    },
                ],
            },
        }],
    })

    result = accumulator.finish()
    assert result.missing_index_deltas == 0
    assert result.index_collisions == 0
    assert len(result.calls) == 1
    assert result.calls[0].name == "read"
    assert _skill_path(result.calls[0].arguments) == "skill://delivery-lifecycle"


def test_stream_accumulator_surfaces_index_collision():
    accumulator = _StreamAccumulator()
    accumulator.consume({
        "choices": [{
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "call-a",
                "function": {"name": "read", "arguments": "{}"},
            }]},
        }],
    })
    accumulator.consume({
        "choices": [{
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "call-b",
                "function": {"name": "bash", "arguments": "{}"},
            }]},
        }],
    })

    result = accumulator.finish()
    assert result.index_collisions == 1
    assert result.id_index_conflicts == 0
