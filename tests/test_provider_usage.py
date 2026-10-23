"""Tests for provider capacity protection and usage accounting."""
from __future__ import annotations

import asyncio
import gc
import json

import pytest

from tusker_gateway.cooldown import CooldownTracker
from tusker_gateway.errors import ProviderCapacityError
from tusker_gateway.passthrough import _stream_error_from_frame
from tusker_gateway.persistent_cooldown import PersistentCooldownStore
from tusker_gateway.provider_usage import (
    ProviderUsageDB,
    capacity_controller,
    capacity_group_for_route,
)


def test_nvidia_routes_share_one_capacity_group():
    assert capacity_group_for_route("nvidia", "meta/llama") == "nvidia"
    assert capacity_group_for_route(
        "openrouter", "nvidia/nemotron-3:free"
    ) == "nvidia"
    assert capacity_group_for_route("openrouter", "openai/gpt-oss") is None


def test_capacity_controller_fails_fast_at_local_limit(monkeypatch):
    monkeypatch.setenv("TUSKER_NVIDIA_MAX_CONCURRENT", "1")
    controller = capacity_controller()
    controller.reset()

    first = controller.acquire("nvidia")
    assert first is not None
    assert controller.acquire("nvidia") is None
    assert controller.snapshot()["nvidia"]["rejections"] == 1

    first.release()
    second = controller.acquire("nvidia")
    assert second is not None
    second.release()


def test_usage_ledger_aggregates_daily_provider_counters(tmp_path):
    db = ProviderUsageDB(str(tmp_path / "provider_usage.db"))
    db.record(
        provider="openrouter",
        model="nvidia/nemotron-3:free",
        group="nvidia",
        success=True,
        prompt_tokens=12,
        completion_tokens=8,
    )
    db.record(
        provider="openrouter",
        model="nvidia/nemotron-3:free",
        group="nvidia",
        success=False,
        capacity_rejected=True,
    )

    nvidia = db.status()["groups"]["nvidia"]
    assert nvidia == {
        "requests": 2,
        "successes": 1,
        "failures": 1,
        "capacity_rejections": 1,
        "prompt_tokens": 12,
        "completion_tokens": 8,
    }


def test_capacity_sse_error_is_classified_and_contains_no_execution_path():
    payload = {
        "error": {
            "code": 502,
            "message": "Upstream error from Nvidia: ResourceExhausted: Worker local total request limit reached (16/16)",
        }
    }
    error = _stream_error_from_frame(
        f"data: {json.dumps(payload)}\n\n".encode(),
        provider="openrouter",
        model="nvidia/nemotron-3:free",
    )

    assert isinstance(error, ProviderCapacityError)
    assert error.capacity_group == "nvidia"
    assert error.upstream_status == 502
    assert error.capacity_rejected is False


def test_persistent_capacity_group_cooldown_hydrates(tmp_path):
    store = PersistentCooldownStore(tmp_path / "cooldowns.db")
    store.record_group("nvidia", 60.0)
    tracker = CooldownTracker()

    assert store.hydrate_groups(tracker) == 1
    assert tracker.is_group_cooldown("nvidia")
    assert tracker.is_cooldown("openrouter", "nvidia/nemotron-3:free")


@pytest.mark.asyncio
async def test_local_capacity_error_is_client_safe(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from tusker_gateway.passthrough import PassthroughClient
    from tusker_gateway.quality import QualityDB

    monkeypatch.setenv("TUSKER_NVIDIA_MAX_CONCURRENT", "1")
    controller = capacity_controller()
    controller.reset()
    config = {
        "api_keys": ["gateway-test"],
        "provider_api_keys": {"nvidia": "nvidia-test"},
        "codex_credentials": [],
        "quality_db_path": str(tmp_path / "quality.db"),
    }
    client = PassthroughClient(
        config,
        QualityDB(config["quality_db_path"]),
        MagicMock(),
    )
    lease = controller.acquire("nvidia")
    assert lease is not None
    try:
        with pytest.raises(ProviderCapacityError) as caught:
            client._reserve_capacity("nvidia", "meta/llama")
    finally:
        lease.release()

    assert caught.value.capacity_rejected is True
    assert "ResourceExhausted" not in caught.value.message


def test_local_backends_have_independent_single_request_limits(monkeypatch):
    from tusker_gateway.provider_usage import (
        LOCAL_LLM_CAPACITY_GROUP,
        MLX_MAC_CAPACITY_GROUP,
        capacity_limit,
    )

    monkeypatch.delenv("TUSKER_LOCAL_LLM_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("TUSKER_MLX_MAC_MAX_CONCURRENT", raising=False)
    assert capacity_group_for_route("local-llm", "llama3.2:3b") == LOCAL_LLM_CAPACITY_GROUP
    assert capacity_group_for_route("mlx-mac", "mlx-community/Qwen3-8B") == MLX_MAC_CAPACITY_GROUP
    assert capacity_limit(LOCAL_LLM_CAPACITY_GROUP) == 1
    assert capacity_limit(MLX_MAC_CAPACITY_GROUP) == 1


def test_local_backend_capacity_controller_rejects_second_request(monkeypatch):
    monkeypatch.setenv("TUSKER_LOCAL_LLM_MAX_CONCURRENT", "1")
    monkeypatch.setenv("TUSKER_MLX_MAC_MAX_CONCURRENT", "1")
    controller = capacity_controller()
    controller.reset()

    local_first = controller.acquire("local-llm")
    mlx_first = controller.acquire("mlx-mac")
    assert local_first is not None
    assert mlx_first is not None
    assert controller.acquire("local-llm") is None
    assert controller.acquire("mlx-mac") is None

    local_first.release()
    mlx_first.release()
    assert controller.acquire("local-llm") is not None
    controller.reset()


def test_process_backed_providers_share_cli_capacity_group(monkeypatch):
    from tusker_gateway.provider_usage import CLI_CAPACITY_GROUP, capacity_limit

    monkeypatch.delenv("TUSKER_CLI_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("TUSKER_KILO_WORKER_URL", raising=False)
    assert capacity_group_for_route("opencode-cli", "openai/gpt-5") == CLI_CAPACITY_GROUP
    assert capacity_group_for_route("claude-code-cli", "sonnet") == CLI_CAPACITY_GROUP
    # Without the dedicated worker, kilo-cli runs its subprocess in-process.
    assert capacity_group_for_route("kilo-cli", "kilo/kilo-auto/free") == CLI_CAPACITY_GROUP
    # The worker pod bounds its own subprocesses, so proxied kilo requests
    # must not consume a gateway-side CLI slot.
    monkeypatch.setenv("TUSKER_KILO_WORKER_URL", "http://tusker-kilo-worker:8643")
    assert capacity_group_for_route("kilo-cli", "kilo/kilo-auto/free") is None
    # Streaming HTTP transports stay uncapped by the CLI budget.
    assert capacity_group_for_route("groq", "qwen/qwen3.8-27b") is None
    assert capacity_limit(CLI_CAPACITY_GROUP) == 1


def test_process_backed_capacity_gate_rejects_fourth_concurrent_request(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from tusker_gateway.passthrough import PassthroughClient
    from tusker_gateway.quality import QualityDB
    from tusker_gateway.provider_usage import CLI_CAPACITY_GROUP

    monkeypatch.setenv("TUSKER_CLI_MAX_CONCURRENT", "3")
    monkeypatch.delenv("TUSKER_KILO_WORKER_URL", raising=False)
    controller = capacity_controller()
    controller.reset()
    config = {
        "api_keys": ["gateway-test"],
        "provider_api_keys": {"opencode-cli": ""},
        "codex_credentials": [],
        "quality_db_path": str(tmp_path / "quality.db"),
    }
    client = PassthroughClient(
        config,
        QualityDB(config["quality_db_path"]),
        MagicMock(),
    )
    leases = [controller.acquire(CLI_CAPACITY_GROUP) for _ in range(3)]
    assert all(lease is not None for lease in leases)
    try:
        with pytest.raises(ProviderCapacityError) as caught:
            client._reserve_capacity("opencode-cli", "openai/gpt-5")
    finally:
        for lease in leases:
            lease.release()

    assert caught.value.capacity_rejected is True
    assert controller.snapshot()[CLI_CAPACITY_GROUP]["active"] == 0
    controller.reset()


@pytest.mark.asyncio
async def test_process_backed_dispatch_holds_lease_until_stream_ends(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from tusker_gateway import passthrough as passthrough_module
    from tusker_gateway.passthrough import PassthroughClient
    from tusker_gateway.provider_usage import CLI_CAPACITY_GROUP
    from tusker_gateway.quality import QualityDB

    monkeypatch.setenv("TUSKER_CLI_MAX_CONCURRENT", "3")
    controller = capacity_controller()
    controller.reset()
    config = {
        "api_keys": ["gateway-test"],
        "provider_api_keys": {},
        "codex_credentials": [],
        "quality_db_path": str(tmp_path / "quality.db"),
    }

    class _FakeAdapter:
        def __init__(self) -> None:
            self.active_during_stream = None

        async def chat(self, **_kwargs):
            async def stream():
                self.active_during_stream = controller.snapshot().get(
                    CLI_CAPACITY_GROUP, {}
                ).get("active", 0)
                yield b"data: partial\n\n"
                yield b"data: [DONE]\n\n"
            return stream()

    adapter = _FakeAdapter()
    monkeypatch.setattr(
        "tusker_gateway.provider_adapters.provider_adapters", {"opencode-cli": adapter},
    )
    client = PassthroughClient(config, QualityDB(config["quality_db_path"]), MagicMock())

    result = await client.chat(
        provider="opencode-cli", model="openai/gpt-5",
        messages=[{"role": "user", "content": "hi"}], stream=True,
    )
    frames = [frame async for frame in result]

    assert len(frames) == 2
    assert adapter.active_during_stream == 1
    assert controller.snapshot().get(CLI_CAPACITY_GROUP, {}).get("active", 0) == 0
    controller.reset()


@pytest.mark.asyncio
async def test_process_backed_dispatch_releases_lease_on_failure(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from tusker_gateway.errors import ProviderError
    from tusker_gateway.passthrough import PassthroughClient
    from tusker_gateway.provider_usage import CLI_CAPACITY_GROUP
    from tusker_gateway.quality import QualityDB

    monkeypatch.setenv("TUSKER_CLI_MAX_CONCURRENT", "3")
    controller = capacity_controller()
    controller.reset()
    config = {
        "api_keys": ["gateway-test"],
        "provider_api_keys": {},
        "codex_credentials": [],
        "quality_db_path": str(tmp_path / "quality.db"),
    }

    class _FailingAdapter:
        async def chat(self, **_kwargs):
            raise ProviderError("CLI exploded", code="opencode_cli_failed")

    monkeypatch.setattr(
        "tusker_gateway.provider_adapters.provider_adapters", {"opencode-cli": _FailingAdapter()},
    )
    client = PassthroughClient(config, QualityDB(config["quality_db_path"]), MagicMock())

    with pytest.raises(ProviderError):
        await client.chat(
            provider="opencode-cli", model="openai/gpt-5",
            messages=[{"role": "user", "content": "hi"}], stream=False,
        )

    assert controller.snapshot().get(CLI_CAPACITY_GROUP, {}).get("active", 0) == 0
    controller.reset()


@pytest.mark.asyncio
async def test_lease_released_stream_releases_when_created_but_never_iterated():
    """A stream that is constructed and then discarded must free its slot.

    Regression (2026-10-23): pool fallback creates a candidate stream and can
    reject it before the first frame. An async generator that never starts
    does not run its ``finally``, so the capacity lease used to be stranded
    for the life of the process. With ``TUSKER_MLX_MAC_MAX_CONCURRENT=1``
    that made every later mlx-mac request fail as permanently busy, which the
    client saw as "Upstream provider capacity is unavailable".
    """
    import gc

    from tusker_gateway.passthrough import _lease_released_stream

    controller = capacity_controller()
    controller.reset()
    lease = controller.acquire("mlx-mac")
    assert lease is not None
    assert controller.snapshot()["mlx-mac"]["active"] == 1

    async def _never_used():
        yield b"data: {}\n\n"

    stream = _lease_released_stream(_never_used(), lease)
    assert controller.snapshot()["mlx-mac"]["active"] == 1  # held while in flight

    del stream
    gc.collect()
    await asyncio.sleep(0.05)
    gc.collect()

    assert controller.snapshot().get("mlx-mac", {}).get("active", 0) == 0
    controller.reset()


@pytest.mark.asyncio
async def test_lease_released_stream_releases_on_normal_completion_and_aclose():
    """The guard must not regress the paths that already worked."""
    from tusker_gateway.passthrough import _lease_released_stream

    controller = capacity_controller()
    controller.reset()

    async def _frames():
        for i in range(3):
            yield f"data: {i}\n\n".encode()

    # Full consumption.
    lease = controller.acquire("mlx-mac")
    assert lease is not None
    async for _ in _lease_released_stream(_frames(), lease):
        pass
    assert controller.snapshot().get("mlx-mac", {}).get("active", 0) == 0

    # Partial consumption then explicit close.
    lease = controller.acquire("mlx-mac")
    assert lease is not None
    stream = _lease_released_stream(_frames(), lease)
    async for _ in stream:
        break
    await stream.aclose()
    assert controller.snapshot().get("mlx-mac", {}).get("active", 0) == 0

    # Explicit close before the first frame must release immediately.
    lease = controller.acquire("mlx-mac")
    assert lease is not None
    stream = _lease_released_stream(_frames(), lease)
    await stream.aclose()
    assert controller.snapshot().get("mlx-mac", {}).get("active", 0) == 0

    controller.reset()



@pytest.mark.asyncio
async def test_guard_stream_lease_releases_for_unstarted_generator():
    """``_stream_events``-shaped generators are guarded at construction time."""
    import gc

    from tusker_gateway.passthrough import _guard_stream_lease

    controller = capacity_controller()
    controller.reset()
    lease = controller.acquire("mlx-mac")
    assert lease is not None

    async def _events():
        try:
            yield b"data: {}\n\n"
        finally:
            lease.release()

    stream = _guard_stream_lease(_events(), lease)
    del stream
    gc.collect()
    await asyncio.sleep(0.05)
    gc.collect()

    assert controller.snapshot().get("mlx-mac", {}).get("active", 0) == 0
    controller.reset()
