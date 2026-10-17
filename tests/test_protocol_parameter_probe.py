"""Parameter-dialect probe tests.

``probe_parameter`` must never turn a generic 200 into "passed". A proxy
that silently ignores an unknown field answers exactly like one that
honours it, so acceptance alone is inconclusive without provider-specific
confirmation.
"""
import asyncio

import pytest

from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
from tusker_gateway.protocol_parameter_probe import probe_parameter


def _store(tmp_path):
    return ProtocolEvidenceStore(str(tmp_path / "evidence.db"))


def _ok_response():
    return {
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }],
    }


async def test_unknown_feature_is_rejected_before_any_upstream_call(tmp_path):
    called = False

    async def execute(request):
        nonlocal called
        called = True
        return _ok_response()

    with pytest.raises(ValueError):
        await probe_parameter(
            provider="p", endpoint_id="e", model="m", feature="not_a_feature",
            execute=execute, store=_store(tmp_path),
        )
    assert called is False


async def test_accepted_parameter_is_inconclusive_not_passed(tmp_path):
    """The core false-positive guard: a 200 proves nothing by itself."""
    sent: list[dict] = []

    async def execute(request):
        sent.append(request)
        return _ok_response()

    store = _store(tmp_path)
    result = await probe_parameter(
        provider="deepseek", endpoint_id="native", model="deepseek-reasoner",
        feature="reasoning_effort", execute=execute, store=store,
    )
    assert result.status == "unknown"
    assert result.failure_class == "parameter_may_be_ignored"
    assert sent[0]["reasoning_effort"] == "low"
    # Inconclusive evidence is never "verified", so routing will not treat
    # the dialect as confirmed.
    assert not store.get(
        "deepseek", "native", "deepseek-reasoner", "reasoning_effort"
    ).verified


async def test_thinking_parameter_uses_native_shape(tmp_path):
    sent: list[dict] = []

    async def execute(request):
        sent.append(request)
        return _ok_response()

    await probe_parameter(
        provider="zai", endpoint_id="native", model="glm-4.6",
        feature="thinking_parameter", execute=execute, store=_store(tmp_path),
    )
    assert sent[0]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in sent[0]


@pytest.mark.parametrize(
    "response,failure_class",
    [
        ({"choices": []}, "invalid_envelope"),
        ({"choices": [{"message": "not-a-dict"}]}, "invalid_message"),
        (["not", "a", "dict"], "invalid_envelope"),
    ],
)
async def test_malformed_envelope_is_unknown_not_passed(
    response, failure_class, tmp_path
):
    async def execute(request):
        return response

    result = await probe_parameter(
        provider="p", endpoint_id="e", model="m", feature="reasoning_effort",
        execute=execute, store=_store(tmp_path),
    )
    assert result.status == "unknown"
    assert result.failure_class == failure_class


@pytest.mark.parametrize("exc", [ConnectionError("upstream 502"), asyncio.TimeoutError()])
async def test_transport_failure_is_unavailable_not_unsupported(exc, tmp_path):
    """A transient upstream state must not be recorded as lack of support."""

    async def execute(request):
        raise exc

    result = await probe_parameter(
        provider="p", endpoint_id="e", model="m", feature="thinking_parameter",
        execute=execute, store=_store(tmp_path), timeout_seconds=5.0,
    )
    assert result.status == "unavailable"
    assert result.failure_class == "transport"


async def test_invalid_limits_are_rejected(tmp_path):
    async def execute(request):
        return _ok_response()

    for timeout in (0, -1, 61):
        with pytest.raises(ValueError):
            await probe_parameter(
                provider="p", endpoint_id="e", model="m", feature="reasoning_effort",
                execute=execute, store=_store(tmp_path), timeout_seconds=timeout,
            )
