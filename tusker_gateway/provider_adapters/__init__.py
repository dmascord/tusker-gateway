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
    import os

    opencode_url = (
        os.environ.get("TUSKER_OPENCODE_WORKER_URL")
        or os.environ.get("TUSKER_CLI_WORKER_URL")
        or ""
    ).strip()
    claude_url = (
        os.environ.get("TUSKER_CLAUDE_WORKER_URL")
        or os.environ.get("TUSKER_CLI_WORKER_URL")
        or ""
    ).strip()

    if opencode_url or claude_url:
        # Delegate process-backed CLI providers to dedicated worker pods so
        # their memory does not land in the gateway container. Each provider
        # may point at its own worker URL; a missing URL leaves that provider
        # unregistered (disabled) rather than running in-process.
        from tusker_gateway.provider_adapters.remote_cli import RemoteCLIAdapter

        if opencode_url:
            provider_adapters.register(
                "opencode-cli", RemoteCLIAdapter("opencode-cli", opencode_url)
            )
        if claude_url:
            provider_adapters.register(
                "claude-code-cli", RemoteCLIAdapter("claude-code-cli", claude_url)
            )
    else:
        # In-process mode (local dev or a worker pod serving one CLI). Register
        # an adapter unless its CLI is explicitly disabled. Defaulting to
        # enabled preserves the original behaviour for tests and local runs
        # where the env var is unset.
        def _enabled(name: str) -> bool:
            raw = os.environ.get(name, "").strip().lower()
            if raw == "":
                return True
            return raw in {"1", "true", "yes", "on"}

        opencode_enabled = _enabled("TUSKER_OPENCODE_CLI_ENABLED")
        claude_enabled = _enabled("TUSKER_CLAUDE_CODE_ENABLED")
        if opencode_enabled:
            from tusker_gateway.provider_adapters.opencode_cli import OpenCodeCLIAdapter

            provider_adapters.register("opencode-cli", OpenCodeCLIAdapter())
        if claude_enabled:
            from tusker_gateway.provider_adapters.claude_code import ClaudeCodeCLIAdapter

            provider_adapters.register("claude-code-cli", ClaudeCodeCLIAdapter())



_register_builtins()
