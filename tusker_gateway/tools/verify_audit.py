"""Verify and summarize the gateway audit JSONL file.

Usage::

    python -m tusker_gateway.tools.verify_audit /home/tusker/.hermes/audit.jsonl

The command validates every hash link, reports event counts, and summarizes
policy decision/outcome coverage without printing event payloads.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
from typing import Iterable

from tusker_gateway.audit import AuditConfig, AuditLogger

def _summary(path: Path) -> tuple[bool, int, Counter[str], dict[str, int]]:
    counts: Counter[str] = Counter()
    policy: Counter[str] = Counter()
    request_events: set[str] = set()
    policy_requests: set[str] = set()
    approval_requests: set[str] = set()
    execution_results: Counter[str] = Counter()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                event_type = str(record.get("event_type") or "unknown")
                counts[event_type] += 1
                request_id = str(record.get("request_id") or "")
                if event_type == "gateway.request" and request_id:
                    request_events.add(request_id)
                if event_type.startswith("tool.policy.") and request_id:
                    policy_requests.add(request_id)
                if event_type.startswith("tool.approval.") and request_id:
                    approval_requests.add(request_id)
                if event_type == "tool.execution.result":
                    execution_results[str(record.get("execution_result") or "unknown")] += 1
                if event_type.startswith("tool.policy.") or event_type.startswith("tool.approval."):
                    decision = record.get("decision")
                    if decision:
                        policy[str(decision)] += 1
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return False, 0, counts, policy
    valid, total = AuditLogger.verify_file(AuditConfig(path=str(path), hmac_key=os.environ.get("TUSKER_AUDIT_HMAC_KEY", "")))
    policy["_policy_requests_correlated"] = len(policy_requests & request_events)
    policy["_policy_requests_uncorrelated"] = len(policy_requests - request_events)
    policy["_approval_requests_correlated"] = len(approval_requests & request_events)
    policy["execution_results"] = sum(execution_results.values())
    policy.update({f"execution_{key}": value for key, value in execution_results.items()})
    return valid, total, counts, policy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=os.environ.get("TUSKER_AUDIT_LOG_PATH", ""))
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.path:
        print("audit path is required", flush=True)
        return 2
    path = Path(args.path)
    valid, total, counts, policy = _summary(path)
    result = {"path": str(path), "valid": valid, "records": total, "event_counts": dict(counts), "policy_outcomes": dict(policy)}
    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"audit_file={path}")
        print(f"valid={valid} records={total}")
        for name, count in counts.most_common():
            print(f"event.{name}={count}")
        for name, count in sorted(policy.items()):
            print(f"policy.{name}={count}")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
