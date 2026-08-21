"""Guard pipeline: input/output guards for chat-completion requests."""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class GuardResult:
    allowed: bool = True
    message: str | None = None
    modified_body: dict | None = None


class Guard(Protocol):
    async def check(self, body: dict[str, Any]) -> GuardResult: ...


@dataclass
class OutputLengthGuard:
    max_tokens: int = 4096

    async def check(self, body: dict[str, Any]) -> GuardResult:
        requested = body.get("max_tokens", 0)
        if requested > self.max_tokens:
            return GuardResult(
                allowed=False,
                message=f"output exceeds max_tokens limit of {self.max_tokens}",
            )
        return GuardResult()


_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_CC_RE = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b")


@dataclass
class PIIRedactionGuard:
    """Scan user messages for PII and replace with redaction tokens."""

    async def check(self, body: dict[str, Any]) -> GuardResult:
        messages = body.get("messages")
        if not isinstance(messages, list):
            return GuardResult(allowed=True)

        mutated = False
        new_messages: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, str):
                new_messages.append(msg)
                continue
            new_content = _EMAIL_RE.sub("[REDACTED-EMAIL]", content)
            new_content = _CC_RE.sub("[REDACTED-CC]", new_content)
            if new_content != content:
                mutated = True
                new_messages.append({**msg, "content": new_content})
            else:
                new_messages.append(msg)

        if mutated:
            return GuardResult(allowed=True, modified_body={**body, "messages": new_messages})
        return GuardResult(allowed=True)


_DEFAULT_INJECTION_PATTERNS: list[str] = [
    "ignore previous instructions",
    "ignore all previous",
    "you are now",
    "system prompt:",
    "disregard your instructions",
    "new instructions:",
    "forget everything",
    "override your",
    "act as if you have no",
    "pretend you are",
]


@dataclass
class PromptInjectionGuard:
    """Block requests that contain suspected prompt-injection text."""

    extra_patterns: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._patterns: list[str] = _DEFAULT_INJECTION_PATTERNS + self.extra_patterns

    async def check(self, body: dict[str, Any]) -> GuardResult:
        messages = body.get("messages")
        if not isinstance(messages, list):
            return GuardResult()

        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            lower = content.lower()
            for pat in self._patterns:
                if pat.lower() in lower:
                    return GuardResult(allowed=False, message="possible prompt injection detected")
        return GuardResult()


@dataclass
class GuardPipeline:
    """Ordered pipeline of guards; short-circuits on first block."""

    guards: list[Guard] = field(default_factory=list)

    async def run(self, body: dict[str, Any]) -> GuardResult:
        current = body
        for guard in self.guards:
            result = await guard.check(current)
            if not result.allowed:
                return result
            if result.modified_body is not None:
                current = result.modified_body
        return GuardResult(allowed=True, modified_body=current)


def load_guardrails_config_from_env(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Load guardrails configuration from environment variables."""
    e = os.environ if env is None else env
    enabled = e.get("TUSKER_GUARDRAILS_ENABLED", "false").strip().lower() in ("true", "1", "yes")
    max_output = int(e.get("TUSKER_MAX_OUTPUT_TOKENS", "4096"))
    injection_raw = e.get("TUSKER_GUARDRAILS_INJECTION_PATTERNS", "")
    extra_patterns = [p.strip() for p in injection_raw.split(",") if p.strip()]
    return {
        "enabled": enabled,
        "max_output_tokens": max_output,
        "injection_patterns": extra_patterns,
    }


def init_guard_pipeline(config: dict[str, Any]) -> GuardPipeline:
    """Build a GuardPipeline from a config dict."""
    if not config.get("enabled", False):
        return GuardPipeline()
    guards: list[Guard] = [
        OutputLengthGuard(max_tokens=config.get("max_output_tokens", 4096)),
        PIIRedactionGuard(),
        PromptInjectionGuard(extra_patterns=config.get("injection_patterns", [])),
    ]
    return GuardPipeline(guards=guards)


__all__ = [
    "GuardResult",
    "Guard",
    "OutputLengthGuard",
    "PIIRedactionGuard",
    "PromptInjectionGuard",
    "GuardPipeline",
    "load_guardrails_config_from_env",
    "init_guard_pipeline",
]
