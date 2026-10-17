"""Probe coordinator approval and cooldown tests."""
import pytest

from tusker_gateway.protocol_probe_coordinator import ProbeCoordinator


@pytest.mark.asyncio
async def test_probe_requires_approval():
    coordinator = ProbeCoordinator()
    async def work():
        return "ok"
    with pytest.raises(PermissionError):
        await coordinator.run(provider="p", endpoint_id="e", model="m",
                              approved=False, operation=work)


@pytest.mark.asyncio
async def test_probe_cooldown_prevents_duplicate_billable_calls():
    coordinator = ProbeCoordinator(cooldown_seconds=300)
    calls = []
    async def work():
        calls.append(1)
        return "ok"
    assert await coordinator.run(provider="p", endpoint_id="e", model="m",
                                 approved=True, operation=work) == "ok"
    with pytest.raises(RuntimeError):
        await coordinator.run(provider="p", endpoint_id="e", model="m",
                              approved=True, operation=work)
    assert len(calls) == 1
