"""Centralized Copilot / OAuth constants used across the gateway.

Avoid scattering `_JWT_REFRESH_MARGIN_SECONDS`, exchange URLs, and editor
version strings across multiple modules.
"""
from __future__ import annotations

# Device-code constants
COPILOT_OAUTH_CLIENT_ID = "Ov23li8tweQw6odWQebz"
_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_POLL_INTERVAL = 5.0
_POLL_SAFETY = 3.0
_TIMEOUT = 300.0

# Exchange / header constants
PUBLIC_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
EDITOR_VERSION = "vscode/1.104.1"
EXCHANGE_USER_AGENT = "GitHubCopilotChat/0.26.7"

# Auto-refresh buffer: refresh 120 s before expiry
JWT_REFRESH_MARGIN_SECONDS = 120

# Vision model heuristics used by Copilot header injection
_VISION_MARKERS = ("vision", "claude", "gemini", "gpt-4o", "gpt-5")


def is_likely_vision_model(model: str) -> bool:
    """Return True if a model id suggests vision capability (heuristic)."""
    lower = (model or "").lower()
    return any(m in lower for m in _VISION_MARKERS)
