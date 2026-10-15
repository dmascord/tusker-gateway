"""Store-level contracts for the rejected-turn capture database.

The privacy rule is the point of this module: a rejected turn must stay
auditable and replayable without persisting prompt material for a pool whose
ZDR policy was never proven.
"""
import base64
import sqlite3

import pytest

import tusker_gateway.turn_capture as turn_capture
from tusker_gateway.turn_capture import (
    BODY_CAPTURED,
    BODY_EXCLUDED_POOL,
    BODY_UNKNOWN_POOL,
    TurnCaptureConfig,
    TurnCaptureStore,
    capture_store,
    fetch_turn_capture,
    json_safe_capture,
    list_turn_captures,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A capture store on a throwaway sqlite file."""
    # A configured PostgreSQL DSN would take precedence over the file path.
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "")
    return TurnCaptureStore(
        TurnCaptureConfig(enabled=True, path=str(tmp_path / "captures.db"))
    )


def _record(store, **overrides):
    payload = {
        "request_id": "req-1",
        "provider": "alibaba",
        "model": "deepseek-v4.1-flash",
        "pool": "code",
        "request_source": "chat_completions",
        "reason": "reasoning_only_or_empty",
        "verdict": {"content_chars": 0, "tool_call_deltas": 0},
        "request_payload": b'{"model":"deepseek-v4.1-flash","stream":true}',
        "response_payload": b"data: [DONE]\n\n",
    }
    payload.update(overrides)
    return store.record(**payload)


def _expire(store, capture_id):
    """Force one row past its expiry without sleeping."""
    connection = sqlite3.connect(store.config.path)
    try:
        connection.execute(
            "UPDATE turn_captures SET expires_at = 0 WHERE capture_id = ?",
            (capture_id,),
        )
        connection.commit()
    finally:
        connection.close()


def _stored_ids(store):
    """The capture ids physically present in the database file."""
    connection = sqlite3.connect(store.config.path)
    try:
        rows = connection.execute("SELECT capture_id FROM turn_captures").fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def test_record_keeps_byte_complete_bodies_and_marks_replayable(store):
    request = b'{"model":"m","messages":[]}'
    response = b"data: [DONE]\n\n"
    capture_id = _record(store, request_payload=request, response_payload=response)

    record = store.fetch(capture_id)
    assert record["body_policy"] == BODY_CAPTURED
    assert record["request_body"] == request
    assert record["response_body"] == response
    assert record["request_bytes"] == len(request)
    assert record["response_bytes"] == len(response)
    # Only a byte-complete request may be replayed: an excerpt would send a
    # different request to the provider.
    assert record["replayable"] is True
    assert record["verdict"] == {"content_chars": 0, "tool_call_deltas": 0}


def test_excluded_pool_keeps_verdict_but_no_bodies(store):
    """The privacy pool is the ZDR boundary, so no bytes are persisted.

    The response is withheld too: it is model output generated from the prompt
    that the policy keeps out of the database.
    """
    capture_id = _record(store, pool="privacy")

    record = store.fetch(capture_id)
    assert record["body_policy"] == BODY_EXCLUDED_POOL
    assert record["request_body"] is None
    assert record["response_body"] is None
    assert record["request_bytes"] is None
    assert record["response_bytes"] is None
    assert record["replayable"] is False
    # The counters still make the rejection visible and countable.
    assert record["verdict"] == {"content_chars": 0, "tool_call_deltas": 0}
    assert record["reason"] == "reasoning_only_or_empty"


def test_unknown_pool_keeps_verdict_but_no_bodies(store):
    capture_id = _record(store, pool=None, request_payload=None)

    record = store.fetch(capture_id)
    assert record["pool"] is None
    assert record["body_policy"] == BODY_UNKNOWN_POOL
    assert record["request_body"] is None
    assert record["response_body"] is None
    assert record["replayable"] is False


def test_excluded_pool_names_are_case_insensitive(tmp_path, monkeypatch):
    """The policy is matched on the configured name, whatever case it uses."""
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "")
    monkeypatch.setenv("TUSKER_TURN_CAPTURE_EXCLUDE_POOLS", "Privacy, swarm")
    store = TurnCaptureStore(
        TurnCaptureConfig(
            enabled=True,
            path=str(tmp_path / "captures.db"),
            excluded_pools=("privacy", "swarm"),
        )
    )

    assert store._body_policy("PRIVACY") == BODY_EXCLUDED_POOL
    assert store._body_policy("swarm") == BODY_EXCLUDED_POOL
    assert store._body_policy("code") == BODY_CAPTURED


def test_oversize_payload_is_excerpted_and_not_replayable(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "")
    store = TurnCaptureStore(
        TurnCaptureConfig(
            enabled=True,
            path=str(tmp_path / "captures.db"),
            max_request_bytes=64,
        )
    )
    request = b"a" * 512
    capture_id = _record(store, request_payload=request)

    record = store.fetch(capture_id)
    assert record["body_policy"] == BODY_CAPTURED
    assert record["request_bytes"] == 512
    assert record["request_truncated_bytes"] == 512 - 64
    assert b"TUSKER-CAPTURE-TRUNCATED" in record["request_body"]
    assert record["replayable"] is False


def test_prune_removes_only_expired_rows(store):
    kept = _record(store, request_id="req-kept")
    dropped = _record(store, request_id="req-dropped")
    _expire(store, dropped)

    assert store.prune() == 1
    # Pruning reclaims the row instead of merely hiding it.
    assert _stored_ids(store) == {kept}
    assert store.fetch(kept) is not None


def test_expired_rows_are_reclaimed_on_write(store):
    """Pruning is throttled, so the next rejection reclaims what a sleep could not."""
    dropped = _record(store, request_id="req-old")
    _expire(store, dropped)
    store._last_prune = 0.0
    fresh = _record(store, request_id="req-new")

    # The expired bytes are gone from the file, not merely filtered out.
    assert _stored_ids(store) == {fresh}


def test_expired_rows_are_not_served_before_pruning(store):
    """Retention holds on read: a row past its TTL is invisible immediately."""
    dropped = _record(store, request_id="req-old")
    _expire(store, dropped)

    # Nothing has pruned it yet, so the row is still on disk.
    assert _stored_ids(store) == {dropped}
    assert store.fetch(dropped) is None
    assert store.list_captures(request_id="req-old") == []


def test_list_captures_filters_without_bodies(store):
    _record(store, provider="alibaba", reason="reasoning_only_or_empty")
    _record(store, provider="zai", reason="empty_stream")

    rows = store.list_captures(provider="zai")
    assert len(rows) == 1
    assert rows[0]["provider"] == "zai"
    assert rows[0]["reason"] == "empty_stream"
    # A listing carries metadata only: prompt bytes never travel in bulk.
    assert "request_body" not in rows[0]
    assert "response_body" not in rows[0]
    assert rows[0]["replayable"] is True


def test_list_captures_since_filters_older_rows(store):
    _record(store, request_id="req-old")
    _record(store, request_id="req-new")

    rows = store.list_captures(since=store.fetch(store.list_captures(limit=1)[0]["capture_id"])["created_at"])
    assert len(rows) == 1


def test_json_safe_capture_renders_text_and_base64(store):
    request = b'{"model":"m"}'
    capture_id = _record(store, request_payload=request)

    safe = json_safe_capture(store.fetch(capture_id))
    assert safe["request_text"] == request.decode()
    assert safe["request_base64"] == base64.b64encode(request).decode("ascii")
    assert "request_body" not in safe
    assert "response_body" not in safe


def test_json_safe_capture_keeps_absent_bodies_absent(store):
    capture_id = _record(store, pool="privacy")

    safe = json_safe_capture(store.fetch(capture_id))
    assert safe["request_text"] is None
    assert safe["request_base64"] is None


def test_disabled_store_persists_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "")
    store = TurnCaptureStore(
        TurnCaptureConfig(enabled=False, path=str(tmp_path / "captures.db"))
    )

    assert store.record(
        request_id="req-off",
        provider="alibaba",
        model="m",
        pool="code",
        request_source="chat_completions",
        reason="empty_stream",
        verdict={},
        request_payload=b"{}",
        response_payload=b"",
    ) is None
    assert not (tmp_path / "captures.db").exists()


def test_capture_store_is_off_unless_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_TURN_CAPTURE_PATH", str(tmp_path / "captures.db"))
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "")
    monkeypatch.setattr(turn_capture, "_STORE", None)
    monkeypatch.setattr(turn_capture, "_STORE_CONFIG", None)
    monkeypatch.delenv("TUSKER_STREAM_CAPTURE", raising=False)

    assert capture_store() is None
    assert fetch_turn_capture("missing") is None
    assert list_turn_captures() == []

    monkeypatch.setenv("TUSKER_STREAM_CAPTURE", "1")
    assert capture_store() is not None

    # Turning the switch off again must not hand out the stale handle.
    monkeypatch.delenv("TUSKER_STREAM_CAPTURE")
    assert capture_store() is None