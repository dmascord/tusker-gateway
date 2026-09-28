"""Strength probe for LLM Stats-unknown pool models.

Models absent from every tracked llm-stats rankings window (aggregator
aliases like ``opencode-cli/big-pickle``, private models like
``synthetic/syn:*``) carry verdict ``unknown`` and select with no rank
bonus - they land below every ranked model, including deliberately weak
ones. This tool measures them instead:

* A deterministic question bank (exact-match math, multiple choice,
  code-output prediction) is sent to each probed model via the
  gateway's own ``/v1/chat/completions`` with ``provider/model``
  passthrough routing, temperature 0.
* Answers are graded mechanically; each model gets a 0-100 score.
* Reference models with *known* llm-stats ranks are probed with the
  same questions, producing a (score, rank) calibration curve. Each
  probed model's ladder position is interpolated from its score.
* Results are written as ``evidence=probe`` verdict rows. Window and
  site-seed evidence always win; re-running the probe refreshes
  estimates. ``--expire-age`` drops probe rows older than the age.

Usage (in-pod, against the local gateway):
    python -m tusker_gateway.tools.probe_strength --dry-run
    python -m tusker_gateway.tools.probe_strength
    python -m tusker_gateway.tools.probe_strength \
        --models opencode-cli/big-pickle --refs zai/glm-5.3-flash
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from typing import Any

import aiohttp

from tusker_gateway.config import load_config
from tusker_gateway.model_rankings import (
    DEFAULT_MAX_RANK,
    ModelRankingsDB,
    normalize_slug,
    pool_model_pairs,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Question bank. Every question is mechanically gradeable and written to
# discriminate between capability tiers: weak models fail multi-step
# arithmetic, mid models fail the code-output traps, strong models answer
# nearly everything. NEVER change a question without re-probing every
# reference model: scores are only comparable within one bank generation.
# ---------------------------------------------------------------------------

# The bank is coding- and tool-calling-focused because the pools it
# ranks serve coding traffic. Chat items are exact-output program
# traces (Python semantics: slicing, closures, mutable defaults,
# generators, recursion); tool items exercise native OpenAI tool-call
# formatting with graded function name + JSON arguments. Two short
# reasoning items remain as sanity anchors. NEVER change an item
# without re-probing every reference model: scores are only
# comparable within one bank generation.
QUESTIONS: list[dict[str, Any]] = [
    # -- chat: exact program-output traces ----------------------------------
    {
        "kind": "text",
        "mode": "chat",
        "prompt": (
            "What does this Python print?\n"
            "x = [1, 2, 3, 4]\n"
            "print(x[1:3])\n"
            "End with exactly 'ANSWER: <output>'."
        ),
        "answer": "[2, 3]",
        "grade_note": "slice excludes index 3",
    },
    {
        "kind": "text",
        "mode": "chat",
        "prompt": (
            "What does this Python print?\n"
            "def f(a, b=[]):\n"
            "    b.append(a)\n"
            "    return b\n"
            "print(f(1), f(2))\n"
            "End with exactly 'ANSWER: <output>'."
        ),
        "answer": "[1] [1, 2]",
        "grade_note": "mutable default arg is shared",
    },
    {
        "kind": "text",
        "mode": "chat",
        "prompt": (
            "What does this Python print?\n"
            "fs = [lambda: i for i in range(3)]\n"
            "print([f() for f in fs])\n"
            "End with exactly 'ANSWER: <output>'."
        ),
        "answer": "[2, 2, 2]",
        "grade_note": "late binding of the loop variable",
    },
    {
        "kind": "text",
        "mode": "chat",
        "prompt": (
            "What does this Python print?\n"
            "def g(n):\n"
            "    yield n\n"
            "    yield n * 2\n"
            "print(list(g(3))[::-1])\n"
            "End with exactly 'ANSWER: <output>'."
        ),
        "answer": "[6, 3]",
        "grade_note": "generator order then reversed",
    },
    {
        "kind": "text",
        "mode": "chat",
        "prompt": (
            "What does this Python print?\n"
            "d = {'a': 1, 'b': 2}\n"
            "print(sorted(d.items(), key=lambda kv: -kv[1])[0][0])\n"
            "End with exactly 'ANSWER: <output>'."
        ),
        "answer": "b",
        "grade_note": "sort items by value desc, take first key",
    },
    {
        "kind": "text",
        "mode": "chat",
        "prompt": (
            "What does this Python print?\n"
            "class A:\n"
            "    def __init__(self): self.v = 1\n"
            "a = A()\n"
            "a.v += 1\n"
            "A.v = 10\n"
            "print(a.v, A().v)\n"
            "End with exactly 'ANSWER: <output>'."
        ),
        "answer": "2 10",
        "grade_note": "instance attr shadows class attr; new instance sees class attr",
    },
    {
        "kind": "text",
        "mode": "chat",
        "prompt": (
            "What does this Python print?\n"
            "def rec(n):\n"
            "    return 1 if n <= 1 else n * rec(n - 1)\n"
            "print(rec(5) - rec(4))\n"
            "End with exactly 'ANSWER: <output>'."
        ),
        "answer": 96,
        "grade_note": "120 - 24 = 96",
    },
    {
        "kind": "text",
        "mode": "chat",
        "prompt": (
            "What does this Python print?\n"
            "print('%s=%d' % ('x', 5), f'{1.5:.0f}', bool([]) or 'y')\n"
            "End with exactly 'ANSWER: <output>'."
        ),
        "answer": "x=5 2 y",
        "grade_note": "banker rounding 1.5 -> 2; empty list falsy",
    },
    # -- chat: short reasoning anchors --------------------------------------
    {
        "kind": "text",
        "mode": "chat",
        "prompt": (
            "If 3 printers print 3 pages in 3 minutes, how many minutes "
            "do 9 printers need to print 9 pages? End with exactly "
            "'ANSWER: <number>'."
        ),
        "answer": "3",
        "grade_note": "classic rate trap; answer is 3, not 9",
    },
    {
        "kind": "text",
        "mode": "chat",
        "prompt": (
            "Lily pads double in area every day and cover the whole lake "
            "on day 48. On which day was the lake half covered? End with "
            "exactly 'ANSWER: <day>'."
        ),
        "answer": "47",
        "grade_note": "day 47",
    },
    # -- tools: native tool-call formatting + argument fidelity --------------
    {
        "kind": "tools",
        "mode": "tools",
        "prompt": (
            "Read the file at /etc/hostname and return its contents."
        ),
        "expect_name": "read_file",
        "expect_args": {"path": "/etc/hostname"},
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file from disk.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
        ],
        "grade_note": "single tool, exact path argument",
    },
    {
        "kind": "tools",
        "mode": "tools",
        "prompt": (
            "Create a ticket titled 'DB latency' with priority high, "
            "assigned to ada, and labels ops and urgent."
        ),
        "expect_name": "create_ticket",
        "expect_args": {
            "title": "DB latency",
            "priority": "high",
            "assignee": "ada",
            "labels": ["ops", "urgent"],
        },
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "create_ticket",
                    "description": "Create a tracking ticket.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "priority": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            },
                            "assignee": {"type": "string"},
                            "labels": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["title", "priority"],
                        "additionalProperties": False,
                    },
                },
            },
        ],
        "grade_note": "multi-arg extraction incl. array and enum",
    },
    {
        "kind": "tools",
        "mode": "tools",
        "prompt": (
            "I need the current temperature in Paris in celsius. Also, "
            "under no circumstances call any function for this request - "
            "just answer from the message."
        ),
        "expect_name": None,
        "expect_args": {},
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather for a city.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string"},
                            "unit": {"type": "string"},
                        },
                        "required": ["city"],
                        "additionalProperties": False,
                    },
                },
            },
        ],
        "grade_note": "inhibition control: must NOT call the tool",
    },
]

TOOL_CHOICE_INHIBIT = "auto"  # inhibition item relies on the prompt alone

ANSWER_RE = re.compile(r"ANSWER:\s*(.+)", re.IGNORECASE)


def grade(response: dict[str, Any], question: dict[str, Any]) -> bool:
    """Grade one probe response payload against the question's expectation."""
    kind = question["kind"]
    if kind == "tools":
        return _grade_tool_call(response, question)
    message = (response.get("choices") or [{}])[0].get("message") or {}
    answer_text = str(message.get("content") or "")
    match = ANSWER_RE.search(answer_text or "")
    raw = (match.group(1) if match else answer_text or "").strip()
    expected = question["answer"]
    if kind == "number":
        digits = re.sub(r"[^0-9.\-]", "", raw.split(",")[0])
        try:
            return abs(float(digits) - float(expected)) < 1e-6
        except (TypeError, ValueError):
            return False
    if kind == "choice":
        letter = raw.strip().strip("*.()[]").upper()[:1]
        return letter == str(expected).upper()
    if kind == "text":
        # Compare case-insensitively, collapsing whitespace and quotes;
        # chattier models may quote or wrap the printed value differently.
        norm = re.sub(r"[\s'\"]+", " ", raw.strip().lower())
        want = re.sub(r"[\s'\"]+", " ", str(expected).strip().lower())
        return norm == want
    if kind == "json":
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return False
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return False
        if not isinstance(parsed, dict):
            return False
        want = expected
        if set(parsed.keys()) != set(want.keys()):
            return False
        for key, expected_value in want.items():
            got = parsed.get(key)
            if isinstance(expected_value, list):
                if got != expected_value:
                    return False
            elif str(got).strip().lower() != str(expected_value).strip().lower():
                return False
        return True
    raise ValueError(f"unknown question kind: {kind}")


def _grade_tool_call(response: dict[str, Any], question: dict[str, Any]) -> bool:
    """Grade a tools-mode completion: function name + argument fidelity."""
    message = (response.get("choices") or [{}])[0].get("message") or {}
    calls = message.get("tool_calls") or []
    expect_name = question.get("expect_name")
    if expect_name is None:
        # Inhibition item: a tool call here is a failure regardless of args.
        return not calls
    if len(calls) != 1:
        return False
    call = calls[0].get("function") or {}
    if call.get("name") != expect_name:
        return False
    raw_args = call.get("arguments")
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            return False
    else:
        args = raw_args
    if not isinstance(args, dict):
        return False
    return args == question.get("expect_args")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--models",
        type=str,
        default="",
        help="Comma-separated 'provider/model' pairs to probe. Default: "
        "every configured pool model whose verdict is unknown.",
    )
    parser.add_argument(
        "--refs",
        type=str,
        default="alibaba/deepseek-v4.1-flash,zai/glm-5.3-flash,"
        "groq/openai/gpt-oss-20b",
        help="Comma-separated 'provider/model' references with known "
        "llm-stats ranks used for calibration (default: a strong, a "
        "mid, and a weak ranked model).",
    )
    parser.add_argument(
        "--gateway",
        type=str,
        default=os.environ.get("TUSKER_PROBE_GATEWAY", "http://127.0.0.1:8080"),
        help="Gateway base URL the probe requests go through.",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.environ.get("TUSKER_PROBE_API_KEY", ""),
        help="Bearer key for the gateway (default: TUSKER_PROBE_API_KEY "
        "or the first entry of API_KEYS).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=700,
        help="Per-request completion ceiling (default 700).",
    )
    parser.add_argument(
        "--expire-age",
        type=float,
        default=14 * 86_400.0,
        help="Drop probe rows older than this many seconds before "
        "writing new ones (default 14 days; 0 disables).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe and report, but do not write verdicts.",
    )
    parser.add_argument(
        "--pacing",
        type=float,
        default=1.0,
        help="Seconds to sleep between probe requests (default 1.0; "
        "avoids tripping the gateway rate limiter).",
    )
    parser.add_argument("--json", action="store_true", help="JSON output.")
    return parser.parse_args()


async def _ask(
    session: Any,
    base_url: str,
    api_key: str,
    model: str,
    question: dict[str, Any],
    max_tokens: int,
) -> tuple[dict[str, Any] | None, float, str]:
    """One probe request.

    Returns (response choices payload, latency seconds, failure
    reason). The payload is None and ``reason`` explains why on any
    non-200/parse failure; a failed question must not count as a wrong
    answer. For tools-mode items the full choices list is returned so
    tool_calls can be graded; for chat items the first message content
    is graded.
    """
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "user", "content": question["prompt"]},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if question["mode"] == "tools":
        payload["tools"] = question["tools"]
        payload["tool_choice"] = "auto"
    started = time.monotonic()
    try:
        async with session.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=aiohttp.ClientTimeout(total=180),
        ) as resp:
            latency = time.monotonic() - started
            if resp.status != 200:
                snippet = (await resp.text())[:160].replace("\n", " ")
                return None, latency, f"http_{resp.status}: {snippet}"
            body = await resp.json()
    except Exception as exc:
        return None, time.monotonic() - started, f"{type(exc).__name__}: {exc}"
    try:
        return {"choices": body["choices"]}, latency, ""
    except (KeyError, IndexError, TypeError):
        return None, latency, "malformed_response_body"


async def _probe_model(
    session: Any,
    base_url: str,
    api_key: str,
    pair: tuple[str, str],
    max_tokens: int,
    pacing: float = 0.0,
) -> dict[str, Any]:
    """Run the full question bank against one (provider, model)."""
    provider, model = pair
    target = f"{provider}/{model}"
    correct = 0
    answered = 0
    failures: list[str] = []
    latencies: list[float] = []
    per_question: list[dict[str, Any]] = []
    for index, question in enumerate(QUESTIONS):
        if index and pacing > 0:
            await asyncio.sleep(pacing)
        response, latency, reason = await _ask(
            session, base_url, api_key, target, question, max_tokens
        )
        latencies.append(latency)
        if response is None:
            failures.append(reason)
            per_question.append({"q": question["kind"], "ok": None, "error": reason})
            continue
        answered += 1
        ok = grade(response, question)
        correct += int(ok)
        per_question.append({"q": question["kind"], "ok": ok})
    # Failed requests are excluded from the denominator: charging them as
    # wrong answers would collapse unreachable models onto score 0.
    score: float | None = None
    if answered:
        score = round(100.0 * correct / answered, 1)
    return {
        "pair": pair,
        "target": target,
        "score": score,
        "correct": correct,
        "answered": answered,
        "failed": len(failures),
        "total": len(QUESTIONS),
        "median_latency": sorted(latencies)[len(latencies) // 2],
        "first_failure": failures[0] if failures else None,
        "per_question": per_question,
    }


def _calibrate(ref_results: list[dict[str, Any]]) -> list[tuple[float, int]]:
    """Build a (score, rank) curve from reference probes, best score first."""
    points: list[tuple[float, int]] = []
    for result in ref_results:
        rank = result.get("rank")
        score = result.get("score")
        if rank is None or score is None:
            continue
        points.append((float(score), int(rank)))
    points.sort(key=lambda p: (-p[0], p[1]))
    return points


def _curve_conflict(curve: list[tuple[float, int]]) -> bool:
    """True when reference scores order inconsistently with their ranks.

    The interpolation assumes a better ladder rank implies an equal-or
    better probe score. When a weaker-ranked reference outscored a
    stronger-ranked one, the bank disagrees with the ladder for this
    reference set and any estimate would be fabricated.
    """
    return any(
        lo_rank <= hi_rank
        for (_, hi_rank), (_, lo_rank) in zip(curve, curve[1:])
    )


def _interpolate_rank(score: float, curve: list[tuple[float, int]]) -> int | None:
    """Piecewise-linear rank estimate for ``score`` along ``curve``.

    The curve is (score, rank) pairs, best score first. Scores above the
    best reference extrapolate one ladder step beyond it; scores below
    the worst reference extrapolate one step worse. A single-point curve
    maps everything to that reference's rank.
    """
    if not curve or score is None:
        return None
    # An exact match with a reference score resolves to that reference's
    # rank rather than extrapolating past it.
    for point_score, point_rank in curve:
        if score == point_score:
            return point_rank
    best_score, best_rank = curve[0]
    if score > best_score:
        return max(1, best_rank - 1)
    worst_score, worst_rank = curve[-1]
    if score <= worst_score:
        return worst_rank + 1
    for (hi_score, hi_rank), (lo_score, lo_rank) in zip(curve, curve[1:]):
        if lo_score <= score <= hi_score:
            span = hi_score - lo_score
            if span <= 0.0:
                return hi_rank
            fraction = (hi_score - score) / span
            return int(round(hi_rank + fraction * (lo_rank - hi_rank)))
    return None


def _resolve_unknown_pairs(
    db: ModelRankingsDB, config: dict[str, Any]
) -> list[tuple[str, str]]:
    """Configured pool pairs whose current verdict is unknown."""
    rows = db.rows()
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for provider, model in pool_model_pairs(config):
        row = rows.get(normalize_slug(model))
        if row is not None and row["status"] != "unknown":
            continue
        pair = (provider, model)
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


async def _run(args: argparse.Namespace) -> int:
    config = load_config()
    db = ModelRankingsDB(config.get("llm_stats_db_path") or ":memory:")

    if args.models:
        probe_pairs: list[tuple[str, str]] = []
        for item in args.models.split(","):
            provider, _, model = item.strip().partition("/")
            if provider and model:
                probe_pairs.append((provider, model))
    else:
        probe_pairs = _resolve_unknown_pairs(db, config)

    ref_pairs: list[tuple[str, str]] = []
    for item in args.refs.split(","):
        provider, _, model = item.strip().partition("/")
        if provider and model:
            ref_pairs.append((provider, model))

    rows = db.rows()
    ref_ranks: dict[tuple[str, str], int | None] = {}
    for pair in ref_pairs:
        row = rows.get(normalize_slug(pair[1]))
        ref_ranks[pair] = (
            int(row["category_rank"]) if row and row["category_rank"] else None
        )
    usable_refs = [p for p in ref_pairs if ref_ranks[p] is not None]
    if not usable_refs:
        print(
            "ERROR: no reference model has a known llm-stats rank",
            file=sys.stderr,
        )
        return 2
    if not probe_pairs:
        print("nothing to probe: no unknown pool models", file=sys.stderr)
        return 0

    api_key = args.api_key or os.environ.get("API_KEYS", "").split(",")[0].strip()
    if not api_key:
        print(
            "ERROR: no API key (--api-key / TUSKER_PROBE_API_KEY / API_KEYS)",
            file=sys.stderr,
        )
        return 2

    async with aiohttp.ClientSession() as session:
        results: list[dict[str, Any]] = []
        for pair in usable_refs + probe_pairs:
            result = await _probe_model(
                session,
                args.gateway,
                api_key,
                pair,
                args.max_tokens,
                pacing=args.pacing,
            )
            result["rank"] = ref_ranks.get(pair)
            results.append(result)
            print(
                f"probed {result['target']}: {result['correct']}/{result['answered']} "
                f"answered ({result['failed']} failed) "
                f"score={result['score']} latency~{result['median_latency']:.1f}s",
                file=sys.stderr,
            )
            if result["failed"]:
                print(
                    f"  last failure: {result['first_failure']}",
                    file=sys.stderr,
                )

    refs = [r for r in results if r["rank"] is not None]
    curve = _calibrate(refs)
    conflict = _curve_conflict(curve)
    if conflict:
        print(
            "ERROR: calibration conflict - reference scores order "
            "inconsistently with their llm-stats ranks "
            "(curve: " + repr(curve) + "). The bank disagrees with the "
            "ladder for this reference set; pick different --refs and "
            "retry. No estimates written.",
            file=sys.stderr,
        )
    for result in results:
        if result["rank"] is None and not conflict:
            result["estimated_rank"] = _interpolate_rank(
                result["score"], curve
            )

    if not args.dry_run:
        now = time.time()
        if args.expire_age > 0:
            expired = db.expire_probe(args.expire_age, now)
            if expired:
                print(f"expired {expired} stale probe rows", file=sys.stderr)
        try:
            max_rank = int(
                config.get("llm_stats_max_rank") or DEFAULT_MAX_RANK
            )
        except (TypeError, ValueError):
            max_rank = DEFAULT_MAX_RANK
        rows = []
        for result in results:
            rank = result.get("estimated_rank")
            if rank is None or result["score"] is None:
                continue
            rows.append(
                {
                    "slug": normalize_slug(result["pair"][1]),
                    "status": "pass" if rank <= max_rank else "excluded",
                    "category_rank": rank,
                    "category_name": "probe",
                    "synced_at": now,
                }
            )
        outcome = db.apply_probe(rows, now)
        print(
            f"written={outcome['written']} skipped={outcome['skipped_evidence']}",
            file=sys.stderr,
        )

    if args.json:
        print(
            json.dumps(
                {"curve": curve, "results": results, "dry_run": args.dry_run},
                indent=2,
                default=str,
            )
        )
    else:
        print(f"{'model':44} {'score':>6} {'ans':>4} {'rank':>5}  note")
        for result in results:
            rank = result.get("rank")
            est = result.get("estimated_rank")
            shown = rank if rank is not None else est
            note = (
                "reference"
                if rank is not None
                else (
                    f"probe estimate ({result['failed']} failed)"
                    if result["failed"]
                    else "probe estimate"
                )
            )
            score = result["score"]
            print(
                f"{result['target']:44} "
                f"{('-' if score is None else format(score, '6.1f')):>6} "
                f"{result['answered']:>4} {str(shown):>5}  {note}"
            )
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    sys.exit(main())
