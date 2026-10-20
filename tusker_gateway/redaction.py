"""Reversible redaction: hide secrets from upstream models, restore for the client.

Upstream sees only ``[TUSKER:<type>:<b64url-aes-siv-ciphertext>]`` tokens. The
original value is carried inside the token itself, sealed with AES-SIV
(RFC 5297, AES-CMAC based, deterministic and misuse-resistant authenticated
encryption) under ``TUSKER_REDACTION_KEY``. Deterministic encryption gives
stable tokens across turns without a storage layer: the same email always
produces the same token, so the model keeps coherent context while never
seeing the real value. Only a gateway with the key can restore.

Design constraints baked in:

- Fail-open on missing key: with no ``TUSKER_REDACTION_KEY`` the tokenizer is
  disabled. Tokenizing without the ability to restore would destroy data.
- Authenticated: any tampering with a token fails decryption (AESSIV raises)
  and the token is left verbatim rather than guessed at.
- Namespace forgery: client-supplied ``[TUSKER:...]`` literals cannot collide
  with real tokens — only ciphertext sealed under this gateway's key decrypts;
  anything else passes through verbatim on restore.
- Restore never invents text: unrecognized or invalid tokens pass through
  byte-identical.

Token type is bound as AES-SIV associated data, so a token of one type
cannot be replayed as another.
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

_TOKEN_PREFIX = "[TUSKER:"
_TOKEN_SUFFIX = "]"
_TOKEN_CORE_RE = re.compile(
    r"\[TUSKER:([a-z0-9_-]+):([A-Za-z0-9_\-=]+)\]"
)
# Models sometimes preserve the opaque ciphertext from a redaction token but
# drop its ``[TUSKER:<type>:`` wrapper while composing a tool argument (for
# example, searching for the token's base64 payload as though it were an ID).
# Bare payloads remain authenticated: restore only after AES-SIV verification
# with one of our known token types. Requiring a long, standalone base64url
# run keeps this from interpreting ordinary short identifiers as tokens.
_BARE_TOKEN_CORE_RE = re.compile(
    r"(?<![A-Za-z0-9_=-])([A-Za-z0-9_-]{32,}={0,2})(?![A-Za-z0-9_=-])"
)

_B64URL = base64.urlsafe_b64encode
_B64URL_DE = base64.urlsafe_b64decode

# AES-SIV associated data: domain-separates redaction tokens from other
# uses of this key.
_CTX_GATEWAY = b"tusker-redaction/token"

TOKEN_TYPE_EMAIL = "email"
TOKEN_TYPE_CC = "card"
TOKEN_TYPE_KEY = "key"
TOKEN_TYPE_TEXT = "text"

_EMAIL_RE = re.compile(
    r"(?<![a-zA-Z0-9._%+-])"
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
    r"(?![a-zA-Z0-9])"
)
# 13-19 digit runs with optional space/dash grouping; Luhn-checksummed before
# tokenizing so order numbers survive untouched (same policy as the guard).
_CC_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
# High-entropy credential shapes: provider API keys and common token prefixes.
_KEY_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|npm_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[A-Za-z0-9_-]{30,})\b"
)

# Entropy-gated fallback: an unprefixed high-entropy run near a credential
# context word. Conservative: 32+ chars of [A-Za-z0-9_-] (deliberately
# excludes '=' and '.' to avoid colliding with base64/hex blob bodies inside
# our own tokens and with file paths), within 80 chars after a context word
# like "key", "token", "secret", "password", "bearer", "auth", "credential".
# Disabled by default: this rule can false-positive on long identifiers in
# code; operators opt in when they need the coverage.
# The context word is matched with a lookbehind-style bounded scan so the
# word itself is never consumed (and therefore never replaced). A plain
# variable-width lookbehind is unavailable, so the word is captured in the
# match and re-emitted verbatim by the substitution callback.
_ENTROPY_CONTEXT_RE = re.compile(
    r"((?:key|token|secret|password|passwd|pwd|api[-_]?key|access[-_]?token"
    r"|auth(?:entication|orization)?|bearer|credential|private[-_]?key)"
    r"[A-Za-z0-9_:,;.\s=\"'()]{0,80}?)"
    r"([A-Za-z0-9_\-]{32,})"
    r"(?![A-Za-z0-9_\-])",
    re.IGNORECASE,
)


def _luhn(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def load_redaction_key(env: dict[str, str] | None = None) -> bytes | None:
    """Read TUSKER_REDACTION_KEY; accept 32-byte raw or 64-char hex; None if unset/invalid."""
    e = os.environ if env is None else env
    raw = e.get("TUSKER_REDACTION_KEY", "")
    if not raw:
        return None
    raw = raw.strip()
    try:
        data = bytes.fromhex(raw)
        if len(data) == 32:
            return data
    except ValueError:
        pass
    data = raw.encode("utf-8")
    if len(data) == 32:
        return data
    return None


@dataclass
class RedactionRuleSet:
    redact_emails: bool = True
    redact_credit_cards: bool = True
    redact_api_keys: bool = True
    # Entropy-gated fallback: a context-word ("key", "token", "secret", ...)
    # within 80 chars before a 32+ char unprefixed run. Disabled by default
    # to avoid sealing ordinary identifiers in code.
    redact_entropy_fallback: bool = False
    # Extra regexes from config: each is compiled once; matches are tokenized
    # as free text. Invalid patterns are skipped (logged by the caller).
    extra_patterns: list[re.Pattern[str]] = field(default_factory=list)


@dataclass
class Tokenizer:
    """Seals detected values into self-contained AES-SIV tokens."""

    key: bytes
    rules: RedactionRuleSet

    def _seal(self, value: str, token_type: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESSIV

        # AES-SIV derives its IV from the input + associated data; no nonce.
        aessiv = AESSIV(self.key)
        ct = aessiv.encrypt(value.encode("utf-8"), [_CTX_GATEWAY + b"|" + token_type.encode()])
        encoded = _B64URL(ct).rstrip(b"=").decode("ascii")
        return f"{_TOKEN_PREFIX}{token_type}:{encoded}{_TOKEN_SUFFIX}"

    def _open(self, token_type: str, encoded: str) -> str | None:
        from cryptography.hazmat.primitives.ciphers.aead import AESSIV
        try:
            ct = _B64URL_DE(encoded + "=" * (-len(encoded) % 4))
            if not ct:
                return None
            aessiv = AESSIV(self.key)
            pt = aessiv.decrypt(ct, [_CTX_GATEWAY + b"|" + token_type.encode()])
            return pt.decode("utf-8")
        except Exception:
            return None

    def tokenize_text(self, text: str) -> str:
        """Escape client tokens, then tokenize detected secrets."""
        if not isinstance(text, str) or not text:
            return text
        out = text
        if self.rules.redact_emails:
            out = _EMAIL_RE.sub(lambda m: self._seal(m.group(0), TOKEN_TYPE_EMAIL), out)
        if self.rules.redact_credit_cards:
            out = _CC_RE.sub(
                lambda m: (
                    self._seal(m.group(0), TOKEN_TYPE_CC)
                    if _luhn(m.group(0))
                    else m.group(0)
                ),
                out,
            )
        if self.rules.redact_api_keys:
            out = _KEY_RE.sub(lambda m: self._seal(m.group(0), TOKEN_TYPE_KEY), out)
        if self.rules.redact_entropy_fallback:
            out = _ENTROPY_CONTEXT_RE.sub(
                lambda m: m.group(1) + self._seal(m.group(2), TOKEN_TYPE_TEXT), out
            )
        for pattern in self.rules.extra_patterns:
            out = pattern.sub(lambda m: self._seal(m.group(0), TOKEN_TYPE_TEXT), out)
        return out

    def restore_text(self, text: str, *, json_escape: bool = False) -> str:
        """Decrypt tokens back to values; unescape client literals; pass through the rest.

        ``json_escape`` is for callers restoring inside a JSON document (a
        tool-call ``arguments`` string): the plaintext is re-encoded with
        JSON string escaping so a sealed value containing ``"``, ``\\`` or a
        control character cannot terminate or corrupt the surrounding JSON.
        Without it the substitution is raw, which is correct for content and
        reasoning fields that are not JSON.
        """
        if not isinstance(text, str) or not text:
            return text

        def _emit(value: str) -> str:
            if not json_escape:
                return value
            # Strip the surrounding quotes json.dumps adds; the escape
            # sequences are exactly what a JSON string literal needs.
            return json.dumps(value, ensure_ascii=False)[1:-1]

        def _restore(m: re.Match[str]) -> str:
            value = self._open(m.group(1), m.group(2))
            return m.group(0) if value is None else _emit(value)

        restored = _TOKEN_CORE_RE.sub(_restore, text)

        def _restore_bare(m: re.Match[str]) -> str:
            encoded = m.group(1)
            # The type is authenticated as associated data. Trying each
            # supported type cannot turn arbitrary text into a value: only a
            # payload sealed by this key for one of these namespaces opens.
            for token_type in (
                TOKEN_TYPE_EMAIL,
                TOKEN_TYPE_CC,
                TOKEN_TYPE_KEY,
                TOKEN_TYPE_TEXT,
            ):
                value = self._open(token_type, encoded)
                if value is not None:
                    return _emit(value)
            return m.group(0)

        return _BARE_TOKEN_CORE_RE.sub(_restore_bare, restored)

    def has_pending_token(self, text: str) -> bool:
        """True if text ends mid-token (split carry needed while streaming)."""
        idx = text.rfind(_TOKEN_PREFIX)
        return idx >= 0 and _TOKEN_SUFFIX not in text[idx:]


@dataclass
class _BufferedRestorer:
    """Carry-buffered streaming restore.

    Accumulates text until a token boundary: if the tail may hold an
    unterminated ``[TUSKER:`` prefix, hold the tail; otherwise flush. On the
    final flush the whole remaining buffer is restored at once, so tokens are
    never split across emissions and clients see clean text.

    ``json_escape`` is set for buffers that carry a JSON document (a
    tool-call ``arguments`` string). Restored plaintext is then re-encoded
    as a JSON string body so a sealed value containing ``"`` or ``\\``
    cannot corrupt the document it is embedded in.
    """

    tokenizer: Tokenizer
    json_escape: bool = False

    def feed(self, delta: str) -> str:
        """Accumulate; emit only text that cannot still end inside a token."""
        if not isinstance(delta, str) or not delta:
            return ""
        self._pending += delta
        # Hold from the first unterminated token start, if any.
        idx = self._pending.find(_TOKEN_PREFIX)
        while idx >= 0:
            end = self._pending.find(_TOKEN_SUFFIX, idx + len(_TOKEN_PREFIX))
            if end == -1:
                head = self._pending[:idx]
                self._pending = self._pending[idx:]
                return self._restore(head)
            idx = self._pending.find(_TOKEN_PREFIX, end + 1)
        # No unterminated token: flush everything except a short tail that
        # could be a partial "[TUSKER:" prefix split across chunks.
        safe = len(self._pending)
        max_probe = min(len(_TOKEN_PREFIX) - 1, len(self._pending))
        for j in range(1, max_probe + 1):
            if _TOKEN_PREFIX.startswith(self._pending[len(self._pending) - j:]):
                safe = len(self._pending) - j
                break
        # A model can omit the wrapper and emit only the base64url payload.
        # Keep a trailing token-like run intact across SSE deltas so the
        # authenticated bare-payload recovery in restore_text sees the whole
        # ciphertext. Holding the final word until its delimiter arrives is
        # bounded and preserves normal streaming for preceding text.
        # A bare authenticated payload can arrive one character at a time.
        # Buffer its complete trailing run, including the first 31 bytes;
        # releasing those bytes early makes recovery impossible.
        bare_tail = re.search(r"[A-Za-z0-9_-]+={0,2}$", self._pending)
        if bare_tail is not None:
            safe = min(safe, bare_tail.start())
        out = self._pending[:safe]
        self._pending = self._pending[safe:]
        return self._restore(out)

    def flush(self) -> str:
        """Restore and emit everything still held. Call once at stream end."""
        out = self._restore(self._pending)
        self._pending = ""
        return out

    def _restore(self, text: str) -> str:
        if not text:
            return ""
        return self.tokenizer.restore_text(text, json_escape=self.json_escape)

    _pending: str = field(default="", init=False)

    def reset(self) -> None:
        """Clear held state (use between requests sharing a restorer)."""
        self._pending = ""

def restore_response(result: Any, tokenizer: Tokenizer | None) -> Any:
    """Walk an upstream response object and restore tokens in every string.

    Handles OpenAI chat.completion and responses-API shapes: message content,
    reasoning content, tool-call arguments (JSON text), refusal, and any
    nested dict/list the provider added. Unknown token-like sequences that do
    not decrypt pass through unchanged; when the tokenizer is None this is a
    no-op returning the original object.

    Tool-call ``function.arguments`` and ``function_call.arguments`` strings
    are restored with JSON escaping so a sealed value containing ``"`` or
    ``\\`` cannot corrupt the surrounding JSON document.
    """
    if tokenizer is None:
        return result

    _JSON_STRING_KEYS = {"arguments"}

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return tokenizer.restore_text(node)
        if isinstance(node, dict):
            return {k: walk_value(k, v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    def walk_value(key: str, value: Any) -> Any:
        if isinstance(value, str) and key in _JSON_STRING_KEYS:
            return tokenizer.restore_text(value, json_escape=True)
        return walk(value)

    return walk(result)
