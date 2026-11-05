"""Deterministic, non-secret Firebase App Check-shaped test values."""
from __future__ import annotations

import base64
import json


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def appcheck_token(*, oversized: bool = False) -> str:
    """Return a realistic-sized JWT-shaped App Check token.

    The signature is deterministic test data, not a Firebase credential. Tests
    exercise transport and redaction byte preservation, not Google signature
    verification (which requires Google's private signing key).
    """
    audience = ["projects/305357937906", "projects/etaproject-uat"]
    if oversized:
        audience.append("projects/" * 1200)
    payload = {
        "sub": "1:305357937906:android:0e6a4b72e0bb95c307046b",
        "aud": audience,
        "provider": "play_integrity",
        "iss": "https://firebaseappcheck.googleapis.com/305357937906",
        "exp": 1793862080,
        "iat": 1793858480,
        "jti": "LVl9twehrRgJIE_kD1xop8rE4GrBkwZiILOMw6O72GU",
    }
    return ".".join(
        (
            _b64url(b'{"kid":"A71KHw","typ":"JWT","alg":"RS256"}'),
            _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            _b64url(bytes(range(256))),
        )
    )
