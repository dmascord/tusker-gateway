"""Live end-to-end tool call tests against OpenRouter free models."""
from __future__ import annotations
import asyncio
import json
import os
import sys
import tempfile
import time
from typing import Any

import aiohttp

sys.path.insert(0, "/Volumes/dev/dev/hermes/tusker-gateway")
from tusker_gateway.passthrough import PassthroughClient
from tusker_gateway.quality import QualityDB

KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not KEY:
    print("ERROR: OPENROUTER_API_KEY not set", file=sys.stderr)
    sys.exit(1)

# Well-known free models on OpenRouter (commonly free). We'll probe what is actually available.
CANDIDATE_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "meta-llama/llama-3.1-8b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "google/gemma-3-27b-it:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "cognitivecomputations/dolphin-mistral-24b:free",
    "deepseek/deepseek-chat:free",
    "open-r1/olympiccoder-32b:free",
]


def get_tool_spec() -> list[dict[str, Any]]:
    return [{
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a bash command and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Shell command"}},
                "required": ["command"],
            },
        },
    }]


async def probe_models(session: aiohttp.ClientSession) -> list[str]:
    """Discover which free models currently exist on OpenRouter."""
    url = "https://openrouter.ai/api/v1/models"
    async with session.get(url) as resp:
        data = await resp.json()
    free_with_tools = [
        m["id"] for m in data.get("data", [])
        if m["id"].endswith(":free")  # OpenRouter convention for free
    ]
    print(f"Discovered {len(free_with_tools)} free models", file=sys.stderr)
    for m in free_with_tools[:10]:
        print(f"  - {m}", file=sys.stderr)
    return free_with_tools


async def test_tool_call(session: aiohttp.ClientSession, model: str, config: dict[str, Any]) -> dict[str, Any]:
    """Send a real tool-calling request for one model."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = dict(config)
        cfg["quality_db_path"] = os.path.join(tmpdir, "q.db")
        qdb = QualityDB(cfg["quality_db_path"])
        client = PassthroughClient(cfg, qdb, session)

        messages = [{
            "role": "user",
            "content": "Use the bash tool to print just the word 'PONG' and nothing else."
        }]

        start = time.monotonic()
        try:
            result = await client.chat(
                "openrouter", model, messages,
                tools=get_tool_spec(),
                stream=False,
            )
            latency = (time.monotonic() - start) * 1000
            choice = result.get("choices", [{}])[0]
            msg = choice.get("message", {})
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []
            finish = choice.get("finish_reason", "?")
            return {
                "model": model, "ok": True, "latency_ms": int(latency),
                "finish_reason": finish,
                "has_tool_calls": len(tool_calls) > 0,
                "tool_call_count": len(tool_calls),
                "first_tool_name": tool_calls[0]["function"]["name"] if tool_calls else None,
                "first_tool_args": tool_calls[0]["function"]["arguments"] if tool_calls else None,
                "content_snippet": (content[:120] if isinstance(content, str) else "<list>"),
            }
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            return {
                "model": model, "ok": False, "latency_ms": int(latency),
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            }


async def main() -> None:
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        free_models = await probe_models(session)
        # Intersect candidates with what is actually available; fall back to the discovered list
        # if nothing in CANDIDATE_MODELS exists.
        available = [m for m in CANDIDATE_MODELS if m in free_models]
        if not available:
            print("No candidate free models found live; trying all discovered :free models.",
                  file=sys.stderr)
            available = free_models[:20]  # cap to keep test time reasonable
        config = {
            "api_keys": ["gateway-test"],
            "provider_api_keys": {"openrouter": KEY},
            "codex_credentials": [],
        }
        # Limit to 12 to stay within time budget and free-tier rate limits.
        available = available[:12]
        print(f"Testing {len(available)} models end-to-end with real tool calls",
              file=sys.stderr)

        results = await asyncio.gather(
            *[test_tool_call(session, m, config) for m in available],
            return_exceptions=True,
        )

    print("\n=== Live Tool-Call Results ===\n")
    ok_count = sum(1 for r in results if isinstance(r, dict) and r.get("ok") and r.get("has_tool_calls"))
    answered = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    print(f"Total models tested: {len(results)}")
    print(f"Successfully returned a response: {answered}")
    print(f"Models that produced real tool_calls: {ok_count}\n")

    print(f"{'model':<55} {'ok':<5} {'latency':<9} {'finish':<10} {'tool_calls':<10} {'error':<30}")
    print("-" * 130)
    for r in results:
        if not isinstance(r, dict):
            print(f"{'(exception)':<55} {'-':<5} {'-':<9} {'-':<10} {'-':<10} {str(r)[:30]:<30}")
            continue
        model = r["model"]
        ok = "yes" if r["ok"] else "no"
        lat = f"{r['latency_ms']}ms"
        finish = str(r.get("finish_reason", "-"))[:10]
        tc = str(r.get("tool_call_count", "-"))
        err = r.get("error", "")
        if r.get("has_tool_calls"):
            err = f"name={r.get('first_tool_name')} args={r.get('first_tool_args', '')[:40]}"
        print(f"{model:<55} {ok:<5} {lat:<9} {finish:<10} {tc:<10} {err[:30]:<30}")

    print("\n=== Per-model tool calls (verbose) ===\n")
    for r in results:
        if not isinstance(r, dict):
            continue
        print(f"--- {r['model']} ---")
        print(json.dumps(r, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
