"""In-process provider transports for providers that are not HTTP APIs.

Adapters return the same OpenAI chat-completion shape as HTTP providers, so
the normal gateway validation, audit, and stream handling remains in force.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol


class ProviderAdapter(Protocol):
    """Transport contract for a non-HTTP chat provider."""

    async def chat(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
    ) -> dict[str, Any] | AsyncIterator[bytes]: ...


class ProviderAdapterRegistry:
    """Explicit allowlist of built-in provider transports."""

    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, provider: str, adapter: ProviderAdapter) -> None:
        key = provider.strip().lower().replace("_", "-")
        if not key:
            raise ValueError("provider name must not be empty")
        self._adapters[key] = adapter

    def get(self, provider: str) -> ProviderAdapter | None:
        return self._adapters.get(provider.strip().lower().replace("_", "-"))


provider_adapters = ProviderAdapterRegistry()


def _register_builtins() -> None:
    from tusker_gateway.provider_adapters.claude_code import ClaudeCodeCLIAdapter
    from tusker_gateway.provider_adapters.kilo_cli import KiloCLIAdapter
    from tusker_gateway.provider_adapters.opencode_cli import OpenCodeCLIAdapter

    provider_adapters.register("claude-code-cli", ClaudeCodeCLIAdapter())
    provider_adapters.register("opencode-cli", OpenCodeCLIAdapter())
    provider_adapters.register("kilo-cli", KiloCLIAdapter())


_register_builtins()
