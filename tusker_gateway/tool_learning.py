"""Privacy-preserving observations for provider tool-shape compatibility.

This module records tool argument *shapes*, never argument values.  It is an
observation and review aid: candidate aliases are surfaced for operators but
are not applied automatically.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import logging
from threading import Lock
from typing import Any

from tusker_gateway.tool_formats import normalize_tool_calls, normalize_tools

logger = logging.getLogger(__name__)

_LOCK = Lock()
_OBSERVATIONS: dict[tuple[str, str, str], dict[str, int]] = defaultdict(
    lambda: {"total": 0, "incomplete": 0, "invalid": 0, "complete": 0}
)
_ALIASES: dict[tuple[str, str, str, str], dict[str, int]] = defaultdict(
    lambda: {"observed": 0, "successful_correction": 0}
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _audit(audit: Any, event: dict[str, Any]) -> None:
    writer = getattr(audit, "write_sync", None)
    if writer is None:
        return
    try:
        writer(event)
    except Exception:  # pragma: no cover - audit policy is tested separately
        logger.debug("tool-shape audit emission failed", exc_info=True)


def _tool_schemas(tools: Any) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for tool in normalize_tools(tools):
        function = tool.get("function") or {}
        name = str(function.get("name") or "").strip()
        if name:
            schemas[name] = function
    return schemas


def _schema_fingerprint(function: dict[str, Any] | None) -> str:
    schema = (function or {}).get("parameters") or {}
    return hashlib.sha256(_canonical(schema).encode("utf-8")).hexdigest()[:16]


def _parse_arguments(call: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    function = call.get("function") or {}
    value = function.get("arguments", {})
    text = value if isinstance(value, str) else _canonical(value)
    try:
        parsed = json.loads(text or "{}")
    except (TypeError, json.JSONDecodeError):
        return None, "invalid_json"
    if not isinstance(parsed, dict):
        return None, "arguments_not_object"
    return parsed, ""


def _previous_calls(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    calls: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        raw = message.get("tool_calls")
        if raw is None and message.get("function_call") is not None:
            raw = [message["function_call"]]
        calls.extend(normalize_tool_calls(raw))
    return calls


def observe_tool_calls(
    calls: list[dict[str, Any]],
    tools: Any,
    *,
    messages: Any = None,
    provider: str = "unknown",
    model: str = "unknown",
    request_id: str | None = None,
    audit: Any = None,
) -> None:
    """Record safe argument-shape observations and correction candidates."""
    schemas = _tool_schemas(tools)
    previous = _previous_calls(messages)
    previous_by_name: dict[str, list[set[str]]] = defaultdict(list)
    for prior in previous:
        prior_function = prior.get("function") or {}
        prior_name = str(prior_function.get("name") or "").strip()
        parsed, _ = _parse_arguments(prior)
        if prior_name and parsed is not None:
            previous_by_name[prior_name].append(set(parsed))

    current_complete: dict[str, set[str]] = {}
    for call in calls:
        function = call.get("function") or {}
        name = str(function.get("name") or "").strip() or "unknown"
        schema_function = schemas.get(name)
        schema = (schema_function or {}).get("parameters") or {}
        required = {
            str(item).strip()
            for item in schema.get("required", [])
            if str(item).strip()
        } if isinstance(schema, dict) and isinstance(schema.get("required"), list) else set()
        properties = set(schema.get("properties", {})) if isinstance(schema, dict) and isinstance(schema.get("properties"), dict) else set()
        parsed, reason = _parse_arguments(call)
        keys = set(parsed) if parsed is not None else set()
        missing = sorted(required - keys) if parsed is not None else []
        extra = sorted(keys - properties) if properties else []
        fingerprint = _schema_fingerprint(schema_function)
        outcome = "invalid" if reason else "incomplete" if missing else "complete"
        observation_key = (name, fingerprint, reason or "valid")
        with _LOCK:
            stats = _OBSERVATIONS[observation_key]
            stats["total"] += 1
            stats[outcome] += 1
        _audit(audit, {
            "event_type": "tool.schema.observation",
            "request_id": request_id or "unknown",
            "provider": provider or "unknown",
            "model": model or "unknown",
            "tool_name": name,
            "schema_fingerprint": fingerprint,
            "outcome": outcome,
            "failure_reason": reason or None,
            "argument_keys": sorted(keys),
            "missing_required": missing,
            "unexpected_keys": extra,
        })
        if parsed is not None and not missing:
            current_complete[name] = keys

    # A follow-up assistant call with the same tool and a now-complete shape
    # is evidence for a candidate alias, but only when exactly one required
    # field was missing and exactly one new key appeared.
    for call in calls:
        function = call.get("function") or {}
        name = str(function.get("name") or "").strip()
        if name not in current_complete or name not in schemas:
            continue
        schema = schemas[name].get("parameters") or {}
        required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
        properties = set(schema.get("properties", {})) if isinstance(schema, dict) and isinstance(schema.get("properties"), dict) else set()
        current_keys = current_complete[name]
        for prior_keys in previous_by_name.get(name, ()):
            missing = required - prior_keys
            prior_extra = prior_keys - properties
            if len(missing) != 1 or len(prior_extra) != 1:
                continue
            target, candidate = next(iter(missing)), next(iter(prior_extra))
            alias_key = (name, _schema_fingerprint(schemas[name]), candidate, target)
            with _LOCK:
                _ALIASES[alias_key]["observed"] += 1
                _ALIASES[alias_key]["successful_correction"] += 1
            _audit(audit, {
                "event_type": "tool.schema.correction_candidate",
                "request_id": request_id or "unknown",
                "provider": provider or "unknown",
                "model": model or "unknown",
                "tool_name": name,
                "schema_fingerprint": alias_key[1],
                "candidate_source_key": candidate,
                "candidate_target_key": target,
                "evidence": "prior_incomplete_call_followed_by_complete_call",
            })


def snapshot(*, limit: int = 100) -> dict[str, Any]:
    """Return aggregate observations and reviewed-by-operator candidates."""
    with _LOCK:
        observations = [
            {
                "tool_name": name,
                "schema_fingerprint": fingerprint,
                "shape": shape,
                **stats,
            }
            for (name, fingerprint, shape), stats in _OBSERVATIONS.items()
        ]
        aliases = [
            {
                "tool_name": name,
                "schema_fingerprint": fingerprint,
                "source_key": source,
                "target_key": target,
                **stats,
            }
            for (name, fingerprint, source, target), stats in _ALIASES.items()
        ]
    observations.sort(key=lambda item: item["total"], reverse=True)
    aliases.sort(key=lambda item: item["observed"], reverse=True)
    return {"observations": observations[:limit], "candidate_aliases": aliases[:limit]}


def reset() -> None:
    with _LOCK:
        _OBSERVATIONS.clear()
        _ALIASES.clear()
