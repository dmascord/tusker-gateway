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


def _db_summary(config: AuditConfig) -> tuple[bool, int, Counter[str], dict[str, int]]:
    """Summarize and verify the database-backed audit chain."""
    from tusker_gateway.storage import shared_database

    counts: Counter[str] = Counter()
    policy: Counter[str] = Counter()
    db = shared_database(
        config.database_path, timeout=10.0, fallback_policy="critical"
    )
    with db.connection() as conn:
        if not conn.degraded:
            for event_type, count in conn.execute(
                "SELECT event_type, COUNT(*) FROM audit_events GROUP BY event_type"
            ).fetchall():
                counts[str(event_type or "unknown")] += int(count)
    valid, total = AuditLogger.verify_db(config)
    return valid, total, counts, policy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=os.environ.get("TUSKER_AUDIT_LOG_PATH", ""))
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--database", action="store_true",
        help="verify the database-backed audit chain instead of a JSONL file",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.database:
        config = AuditConfig(
            backend="database",
            database_path=os.environ.get("TUSKER_AUDIT_DATABASE_PATH", ""),
            hmac_key=os.environ.get("TUSKER_AUDIT_HMAC_KEY", ""),
        )
        valid, total, counts, policy = _db_summary(config)
        result = {
            "backend": "database", "valid": valid, "records": total,
            "event_counts": dict(counts), "policy_outcomes": dict(policy),
        }
    else:
        if not args.path:
            print("audit path is required", flush=True)
            return 2
        path = Path(args.path)
        valid, total, counts, policy = _summary(path)
        result = {"path": str(path), "valid": valid, "records": total, "event_counts": dict(counts), "policy_outcomes": dict(policy)}
    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"audit_backend={result.get('backend', 'file')}")
        if result.get("path"):
            print(f"audit_file={result['path']}")
        print(f"valid={valid} records={total}")
        for name, count in counts.most_common():
            print(f"event.{name}={count}")
        for name, count in sorted(policy.items()):
            print(f"policy.{name}={count}")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
