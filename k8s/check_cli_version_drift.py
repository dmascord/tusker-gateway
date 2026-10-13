#!/usr/bin/env python3
"""Check pinned provider-CLI versions against the latest npm releases.

The gateway's CLI routes (opencode-cli, claude-code-cli, kilo-cli) run
binaries pinned in the Dockerfile. Upstream keeps deprecating models and
shipping new free ones, so a stale pin means a stale model catalog:
newer CLIs list new free models (the auto-catalog adopts them into the
code pool automatically) and drop deprecated entries from ``opencode
models``. Models that a newer CLI still lists but refuses to serve are
handled at request time by the adapter's 410 retired-model mapping, so
this check is about freshness, not correctness.

Pinned versions come from the Dockerfile when present; the weekly k8s
CronJob has no repo checkout and passes ``*_CLI_VERSION`` environment
variables instead (``k8s/cli-drift-cronjob.yaml``; a regression test
pins the manifest against the Dockerfile and this script). Exit code 1
means at least one package drifted. Upgrades stay a deliberate,
provenance-tracked deploy (``k8s/deploy.sh``) - this only raises the
flag.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Dockerfile ARG name -> npm package.
PACKAGES = {
    "CLAUDE_CODE_CLI_VERSION": "@anthropic-ai/claude-code",
    "OPENCODE_CLI_VERSION": "@opencode/cli",
    "KILO_CLI_VERSION": "@kilocode/cli",
}


def _pinned_from_dockerfile(arg: str) -> str | None:
    dockerfile = REPO_ROOT / "Dockerfile"
    if not dockerfile.exists():
        return None
    match = re.search(
        rf"^ARG {arg}=([0-9][0-9A-Za-z.+-]*)$", dockerfile.read_text(), re.MULTILINE
    )
    return match.group(1) if match else None


def _latest_version(package: str) -> str:
    encoded = package.replace("@", "%40").replace("/", "%2f")
    url = f"https://registry.npmjs.org/{encoded}/latest"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read())["version"]


def main() -> int:
    drift = 0
    print(f"{'PACKAGE':<28} {'PINNED':<12} {'LATEST':<12} STATUS")
    for arg, package in PACKAGES.items():
        pinned = os.environ.get(arg) or _pinned_from_dockerfile(arg)
        if not pinned:
            print(f"{package:<28} {'-':<12} {'-':<12} NO PIN FOUND")
            drift = 1
            continue
        try:
            latest = _latest_version(package)
        except Exception as exc:
            print(
                f"{package:<28} {pinned:<12} {'-':<12} "
                f"QUERY FAILED: {type(exc).__name__}"
            )
            drift = 1
            continue
        if pinned == latest:
            print(f"{package:<28} {pinned:<12} {latest:<12} up-to-date")
        else:
            print(
                f"{package:<28} {pinned:<12} {latest:<12} "
                "DRIFT - upgrade via Dockerfile ARG + k8s/deploy.sh"
            )
            drift = 1
    return drift


if __name__ == "__main__":
    sys.exit(main())
