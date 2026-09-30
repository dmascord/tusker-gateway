from __future__ import annotations

import json

from tusker_gateway.audit import AuditConfig, AuditLogger
from tusker_gateway.tools.verify_audit import main

def test_verify_audit_reports_valid_chain_and_counts(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TUSKER_AUDIT_HMAC_KEY", "test-key")
    path = tmp_path / "audit.jsonl"
    audit = AuditLogger(AuditConfig(path=str(path), hmac_key="test-key"))
    audit._append({"event_type": "tool.policy.evaluation", "decision": "allow"})
    audit._append({"event_type": "tool.policy.denied", "decision": "deny"})

    assert main([str(path), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["valid"] is True
    assert output["records"] == 2
    assert output["event_counts"]["tool.policy.denied"] == 1
    assert output["policy_outcomes"]["deny"] == 1



def test_verify_audit_accepts_legacy_then_hmac_records(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    legacy = AuditLogger(AuditConfig(path=str(path)))
    legacy._append({"event_type": "gateway.request"})
    monkeypatch.setenv("TUSKER_AUDIT_HMAC_KEY", "test-key")
    signed = AuditLogger(AuditConfig(path=str(path), hmac_key="test-key"))
    signed._append({"event_type": "tool.policy.evaluation"})
    assert AuditLogger.verify_file(AuditConfig(path=str(path), hmac_key="test-key")) == (True, 2)
def test_verify_audit_returns_failure_for_tampered_chain(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    audit = AuditLogger(AuditConfig(path=str(path)))
    audit._append({"event_type": "gateway.request"})
    record = json.loads(path.read_text())
    record["event_type"] = "tampered"
    path.write_text(json.dumps(record) + "\n")

    assert main([str(path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False
