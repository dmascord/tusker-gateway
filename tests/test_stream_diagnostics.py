import json
import stat

from tusker_gateway.stream_diagnostics import extract_cycle_text, store_reasoning_cycle


def test_reasoning_cycle_capture_is_private_and_contains_trigger_text(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_STREAM_DIAGNOSTICS_DIR", str(tmp_path / "private"))
    text = "repeat-me!" * 30
    name = store_reasoning_cycle(
        text,
        request_id="req-test",
        provider="provider-x",
        model="model-y",
        source="promoted_reasoning_content",
        cycle_chars=10,
        repeats=3,
    )

    assert name and name.startswith("cycle-")
    directory = tmp_path / "private"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    capture = directory / name
    assert stat.S_IMODE(capture.stat().st_mode) == 0o600
    record = json.loads(capture.read_text())
    assert record["text"] == text
    assert record["request_id"] == "req-test"
    assert record["source"] == "promoted_reasoning_content"


def test_cycle_capture_respects_record_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_STREAM_DIAGNOSTICS_DIR", str(tmp_path))
    import tusker_gateway.stream_diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "_MAX_RECORDS", 1)
    kwargs = dict(
        request_id=None, provider="p", model="m", source="reasoning_fields",
        cycle_chars=32, repeats=3,
    )
    assert store_reasoning_cycle("x" * 96, **kwargs)
    assert store_reasoning_cycle("y" * 96, **kwargs) is None


def test_extract_cycle_text_clips_to_capture_bound():
    assert extract_cycle_text("a" * 5000, 1024, 8) == "a" * 4096
