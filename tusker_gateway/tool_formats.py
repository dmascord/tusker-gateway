"""Tool-call normalization across OpenAI, Anthropic, and text-emitting models.

The gateway's public contract is OpenAI chat-completions tool calls. Provider
adapters may use native Anthropic blocks or emit XML/DSML in assistant text;
this module converts those representations without executing tools.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from tusker_gateway.errors import BadRequestError

_ANTHROPIC_IMAGE_DATA_URL_RE = re.compile(
    r"^data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/]*={0,2})$"
)


def _openai_content_to_anthropic(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise BadRequestError("Message content must be a string or array", code="invalid_content")

    blocks: list[dict[str, Any]] = []
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            raise BadRequestError(
                f"Message content block {index} must be an object",
                code="invalid_content",
            )
        block_type = block.get("type")
        if block_type in {"text", "input_text"}:
            text = block.get("text")
            if not isinstance(text, str):
                raise BadRequestError(
                    f"Message text block {index} must contain a string text field",
                    code="invalid_content",
                )
            blocks.append({"type": "text", "text": text})
            continue
        if block_type not in {"image_url", "input_image"}:
            raise BadRequestError(
                f"Content block type '{block_type}' is not supported by Anthropic",
                code="unsupported_content_type",
            )

        image_url = block.get("image_url")
        if isinstance(image_url, str):
            url = image_url
        elif isinstance(image_url, dict):
            url = image_url.get("url")
        else:
            url = None
        if not isinstance(url, str) or not url:
            raise BadRequestError(
                f"Message image block {index} must contain a non-empty image_url",
                code="invalid_image_url",
            )
        data_match = _ANTHROPIC_IMAGE_DATA_URL_RE.fullmatch(url)
        if data_match:
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": data_match.group(1),
                    "data": data_match.group(2),
                },
            })
        elif url.startswith("https://"):
            blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        else:
            raise BadRequestError(
                f"Message image block {index} must use HTTPS or a base64 image data URL",
                code="invalid_image_url",
            )
    return blocks


def _json_args(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"raw": str(value)}, ensure_ascii=False)


def normalize_tools(tools: Any) -> list[dict[str, Any]]:
    """Normalize function/tool declarations to OpenAI's tools shape."""
    result: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return result
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type", "function") == "function" and isinstance(tool.get("function"), dict):
            fn = tool["function"]
        elif isinstance(tool.get("name"), str):
            fn = tool
        else:
            continue
        name = str(fn.get("name", "")).strip()
        if not name:
            continue
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(fn.get("description", "")),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return result


def normalize_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """Normalize OpenAI-like, Anthropic-like, and Bedrock-like tool calls."""
    calls: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return calls
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("function"), dict):
            fn = item["function"]
            name = fn.get("name", "")
            args = fn.get("arguments", "{}")
            call_id = item.get("id") or item.get("call_id")
        elif item.get("type") in {"tool_use", "function_call"}:
            name = item.get("name", "")
            args = item.get("input", item.get("arguments", item.get("args", {})))
            call_id = item.get("id") or item.get("call_id")
        elif isinstance(item.get("toolUse"), dict):
            tool = item["toolUse"]
            name = tool.get("name", "")
            args = tool.get("input", {})
            call_id = tool.get("toolUseId") or item.get("id")
        elif "name" in item and ("arguments" in item or "input" in item or "args" in item):
            name = item["name"]
            args = item.get("arguments") or item.get("input") or item.get("args") or {}
            call_id = item.get("id") or item.get("call_id")
        else:
            continue
        name = str(name).strip()
        if not name:
            continue
        call_id = str(call_id or f"call_{index}_{hashlib.sha256(name.encode()).hexdigest()[:10]}")
        calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": _json_args(args)},
        })
    return calls


def openai_to_anthropic_tools(tools: Any) -> list[dict[str, Any]]:
    """Convert OpenAI declarations to Anthropic input_schema declarations."""
    return [
        {
            "name": t["function"]["name"][:200],
            "description": t["function"].get("description", ""),
            "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}}),
        }
        for t in normalize_tools(tools)
    ]


def openai_messages_to_anthropic(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert an OpenAI transcript, including image input, to Anthropic blocks."""
    result: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        if role in {"system", "developer"}:
            role = "user"
        if role == "tool":
            result.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id", "")),
                "content": str(message.get("content", "")),
            }]})
            continue
        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            content = message.get("content")
            if content:
                converted = _openai_content_to_anthropic(content)
                if isinstance(converted, str):
                    blocks.append({"type": "text", "text": converted})
                else:
                    blocks.extend(converted)
            for call in normalize_tool_calls(message["tool_calls"]):
                fn = call["function"]
                try:
                    args = json.loads(fn["arguments"])
                except json.JSONDecodeError:
                    args = {"raw": fn["arguments"]}
                blocks.append({"type": "tool_use", "id": call["id"], "name": fn["name"][:200], "input": args})
            result.append({"role": "assistant", "content": blocks})
            continue
        content = _openai_content_to_anthropic(message.get("content", ""))
        result.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    return result


def _parse_json_call(text: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool_name")
    if not name:
        return None
    args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or {}
    return {"name": name, "arguments": args}


def parse_text_tool_calls(text: Any) -> list[dict[str, Any]]:
    """Parse common XML, DSML/MiMoML, JSON-fenced, and TOOL_CALL text forms."""
    if not isinstance(text, str) or not text.strip():
        return []
    calls: list[dict[str, Any]] = []
    # 1. JSON payload inside <tool_call> / <function_call>.
    for match in re.finditer(r"<(?:tool_call|function_call)>\s*(\{.*?\})\s*</(?:tool_call|function_call)>", text, re.I | re.S):
        call = _parse_json_call(match.group(1))
        if call:
            calls.append(call)
    # 2. Claude-style function_calls/invoke and DSML namespaced variants.
    # Handles: <invoke name="bash">, <dsml:invoke name="bash">, <ds:function name="bash">
    for match in re.finditer(r"<(?:[\w:-]+:)?(?:invoke|function|tool)\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)</(?:[\w:-]+:)?(?:invoke|function|tool)>", text, re.I | re.S):
        name, inner = match.groups()
        args: dict[str, Any] = {}
        for param in re.finditer(r"<(?:[\w:-]+:)?parameter(?:\s+name=[\"']([^\"']+)[\"'])?[^>]*>(.*?)</(?:[\w:-]+:)?parameter>", inner, re.I | re.S):
            key, value = param.groups()
            args[key or "value"] = value.strip()
        calls.append({"name": name.strip(), "arguments": args})
    # 3. Newer DSML: <tool_name> + <parameters> fields.
    for block in re.finditer(r"<tool_call[^>]*>(.*?)</tool_call>", text, re.I | re.S):
        inner = block.group(1)
        name_match = re.search(r"<(?:tool_name|name)>(.*?)</(?:tool_name|name)>", inner, re.I | re.S)
        if name_match:
            args_match = re.search(r"<(?:parameters|args)>(.*?)</(?:parameters|args)>", inner, re.I | re.S)
            args = _parse_json_call(args_match.group(1).strip()) if args_match else None
            calls.append({"name": name_match.group(1).strip(), "arguments": (args or {}).get("arguments", {})})
    # 4. Self-closing tool invocation with JSON arguments.
    # Handles: <tool_invocation name="bash" arguments={...} />
    for match in re.finditer(r"<tool_invocation\s+name=[\"']([^\"']+)[\"']\s+arguments=(\{.*?\})[^>]*?/?>", text, re.I | re.S):
        try:
            args: Any = json.loads(match.group(2))
        except json.JSONDecodeError:
            args = {"raw": match.group(2)}
        calls.append({"name": match.group(1), "arguments": args})
    # 5. TOOL_CALL: name({...}) fallback.
    for match in re.finditer(r"TOOL_CALL:\s*([\w.-]+)\s*\((.*?)\)", text, re.I | re.S):
        args = match.group(2).strip()
        try:
            args_obj: Any = json.loads(args) if args else {}
        except json.JSONDecodeError:
            args_obj = {"raw": args}
        calls.append({"name": match.group(1), "arguments": args_obj})

    # 6. Malformed Hermes/Qwen-style XML: <function=name>...<parameter=k>v</parameter>...</function>
    # The opening/closing tags for <function> may be bare or quoted; parameters are siblings, not nested.
    # Example:
    #   <tool_call>
    #   <function=bash>
    #   <parameter=command>ssh ...</parameter>
    #   <parameter=timeout>15</parameter>
    #   </function>
    #   </tool_call>
    for match in re.finditer(
        r"<\s*function\s*=\s*[\"']?([^\s\"'<>]+)[\"']?\s*>(.*?)<\s*/\s*function\s*>",
        text, re.I | re.S,
    ):
        name = match.group(1).strip()
        inner = match.group(2)
        args: dict[str, Any] = {}
        # Match each <parameter=name>value</parameter> sibling.
        for param in re.finditer(
            r"<\s*parameter\s*=\s*[\"']?([^\s\"'<>]*)[\"']?\s*>(.*?)<\s*/\s*parameter\s*>",
            inner, re.I | re.S,
        ):
            key = (param.group(1) or "value").strip()
            args[key] = param.group(2).strip()
        # Also handle bare <parameter>value</parameter> with no name attr.
        if not args:
            for param in re.finditer(
                r"<\s*parameter\s*>(.*?)<\s*/\s*parameter\s*>",
                inner, re.I | re.S,
            ):
                args[f"arg_{len(args)}"] = param.group(1).strip()
        calls.append({"name": name, "arguments": args})

    return normalize_tool_calls(calls)


def strip_tool_text(text: Any) -> Any:
    """Remove recognized tool markup while retaining ordinary assistant text."""
    if not isinstance(text, str):
        return text
    cleaned = re.sub(r"TOOL_CALL:.*$", "", text, flags=re.I | re.M)
    cleaned = re.sub(r"<(?:tool_call|function_call|tool_calls|function_calls|tool_use|tool_invocation|dsml:invoke|ds:function)\b[^>]*>.*?</(?:tool_call|function_call|tool_calls|function_calls|tool_use|tool_invocation|dsml:invoke|ds:function)>", "", cleaned, flags=re.I | re.S)
    # Strip well-formed <function=name>...</function> blocks (multi-line, possibly nested params).
    cleaned = re.sub(
        r"<\s*function\s*=\s*[\"']?[^\s\"'<>]+[\"']?\s*>.*?<\s*/\s*function\s*>",
        "", cleaned, flags=re.I | re.S,
    )
    # Strip malformed <function=name</parameter>...</function> blocks (legacy fallback).
    cleaned = re.sub(r"<function=[^\s<>]+</parameter>.*?</function>", "", cleaned, flags=re.I | re.S)
    cleaned = re.sub(r"<tool_invocation[^>]*/>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<(?:(?:MiMoML|DSML)[|｜]?|[|｜](?:MiMoML|DSML)[|｜]?)\s*[^>]*>", "", cleaned, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def normalize_response_tool_calls(response: dict[str, Any]) -> dict[str, Any]:
    """Normalize provider response tool calls and rescue text-formatted calls."""
    out = dict(response)
    choices = out.get("choices")
    if not isinstance(choices, list):
        return out
    new_choices = []
    for choice in choices:
        choice = dict(choice) if isinstance(choice, dict) else choice
        if not isinstance(choice, dict):
            new_choices.append(choice)
            continue
        message = dict(choice.get("message") or {})
        content = message.get("content")

        # Bedrock-style content lists: extract toolUse blocks and text parts.
        if isinstance(content, list):
            found_calls = []
            text_parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if "toolUse" in part:
                    found_calls.append(part["toolUse"])
                elif "text" in part:
                    text_parts.append(part["text"])
            if found_calls:
                message["tool_calls"] = found_calls
                content = "\n\n".join(text_parts) if text_parts else None

        calls = normalize_tool_calls(message.get("tool_calls"))
        if not calls:
            calls = parse_text_tool_calls(content)
            if calls:
                content = strip_tool_text(content)

        if calls:
            message["tool_calls"] = calls
            choice["finish_reason"] = "tool_calls"
        message["content"] = content
        choice["message"] = message
        new_choices.append(choice)
    out["choices"] = new_choices
    return out
