"""Regression: one deploy must move the gateway and both CLI workers together.

The image is shared by all three workloads, but deploy.sh previously applied,
rolled, verified and annotated only tusker-gateway, leaving workers on stale
digests. See docs/incidents/2026-11-23-claude-code-cli-sse-escape.md.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

K8S = Path(__file__).resolve().parents[1] / "k8s"


def _script(name: str) -> str:
    return (K8S / name).read_text()


def test_deploy_script_covers_all_three_workloads():
    deploy = _script("deploy.sh")
    for target in ("tusker-gateway", "tusker-opencode-worker", "tusker-claude-worker"):
        assert target in deploy
    for manifest in (
        "k8s/deployment.yaml",
        "k8s/worker-opencode.yaml",
        "k8s/worker-claude.yaml",
    ):
        assert manifest in deploy
    # The rollout, image verification and annotation loops must be target loops,
    # not gateway-only commands.
    assert 'rollout status deployment/"${target}"' in deploy
    assert 'verify_image_digest "app=${target}" "${target}"' in deploy
    assert 'annotate deployment/"${target}"' in deploy


def test_provenance_check_covers_all_three_workloads():
    verify = _script("verify-provenance.sh")
    for name in ("tusker-gateway", "tusker-opencode-worker", "tusker-claude-worker"):
        assert name in verify
    for manifest in ("worker-opencode.yaml", "worker-claude.yaml"):
        assert manifest in verify
    assert "workload" not in verify.lower() or "WORKLOADS" in verify


def _pin_module():
    spec = importlib.util.spec_from_file_location("pin_manifest", K8S / "pin-manifest.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_deploy_renders_multi_document_worker_manifests():
    """Worker files contain a Service as well as a Deployment.

    `kubectl set image --local` rejects the Service document, so deploy.sh must
    render through pin-manifest.py --render, which preserves all documents.
    """
    deploy = _script("deploy.sh")
    assert "pin-manifest.py" in deploy and "--render" in deploy
    helper = _script("pin-manifest.py")
    assert '"--render"' in helper
    assert "sys.stdout.write(pinned)" in helper


def test_pin_manifest_handles_worker_app_labels(tmp_path):
    module = _pin_module()
    for app in ("tusker-gateway", "tusker-opencode-worker", "tusker-claude-worker"):
        text = (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            f"  name: {app}\n"
            "  labels:\n"
            f"    app: {app}\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      containers:\n"
            "      - name: c\n"
            "        image: registry.tusker.net.au:5000/tusker-gateway@sha256:" + "a" * 64 + "\n"
        )
        pinned = module.pin(text, "sha256:" + "b" * 64, "c" * 40, "swarm-alpine-test")
        assert f"    app: {app}\n  annotations:\n" in pinned
        assert "tusker.net.au/commit: " + "c" * 40 in pinned
        assert "image: registry.tusker.net.au:5000/tusker-gateway@sha256:" + "b" * 64 in pinned
