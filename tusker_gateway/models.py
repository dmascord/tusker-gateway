"""Typed gateway configuration and credential models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    chat_path: str
    auth_type: str = "bearer"
    model_header: str | None = None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "ProviderConfig":
        return cls(
            base_url=str(raw["base_url"]),
            chat_path=str(raw["chat_path"]),
            auth_type=str(raw.get("auth_type", "bearer")),
            model_header=raw.get("model_header"),
        )

    def to_raw(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "base_url": self.base_url,
            "chat_path": self.chat_path,
            "auth_type": self.auth_type,
        }
        if self.model_header:
            out["model_header"] = self.model_header
        return out


@dataclass
class Credential:
    """Normalized Hermes or legacy OAuth credential."""
    access_token: str
    refresh_token: str | None = None
    expires_at_ms: int = 0
    provider: str = "openai-codex"
    label: str = ""
    raw: dict[str, Any] | None = None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any], provider: str | None = None) -> "Credential":
        expires_ms = raw.get("expires_at_ms")
        if expires_ms is None and raw.get("expires_at"):
            expires_ms = int(float(raw["expires_at"]) * 1000)
        return cls(
            access_token=str(raw.get("access_token") or raw.get("token") or ""),
            refresh_token=raw.get("refresh_token"),
            expires_at_ms=int(expires_ms or 0),
            provider=str(provider or raw.get("provider") or "openai-codex"),
            label=str(raw.get("label") or ""),
            raw=dict(raw),
        )

    def to_raw(self) -> dict[str, Any]:
        out = dict(self.raw or {})
        out.update({
            "access_token": self.access_token,
            "provider": self.provider,
        })
        if self.refresh_token:
            out["refresh_token"] = self.refresh_token
        if self.expires_at_ms:
            out["expires_at_ms"] = self.expires_at_ms
        if self.label:
            out["label"] = self.label
        return out


PROVIDER_CONFIGS: dict[str, ProviderConfig] = {}
