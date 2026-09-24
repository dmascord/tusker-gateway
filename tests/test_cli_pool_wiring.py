from __future__ import annotations

import json
from pathlib import Path

import yaml


def _deployment_env():
    manifest = yaml.safe_load(
        (Path(__file__).parents[1] / "k8s" / "deployment.yaml").read_text()
    )
    return {
        item["name"]: item
        for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }


def test_opencode_and_kilo_routes_are_code_only_and_explicitly_enabled():
    env = _deployment_env()
    code = json.loads(env["TUSKER_POOL_CODE"]["value"])
    privacy = json.loads(env["TUSKER_POOL_PRIVACY"]["value"])
    code_routes = {(item["provider"], item["model"]) for item in code["models"]}
    privacy_providers = {item["provider"] for item in privacy["models"]}

    assert ("opencode-cli", "big-pickle") in code_routes
    assert ("kilo-cli", "kilo/kilo-auto/free") in code_routes
    assert ("claude-code-cli", "sonnet") in code_routes
    assert not ({"claude-code-cli", "opencode-cli", "kilo-cli"} & privacy_providers)
    assert env["TUSKER_CLAUDE_CODE_ENABLED"]["value"] == "true"
    assert env["TUSKER_OPENCODE_CLI_ENABLED"]["value"] == "true"
    assert env["TUSKER_OPENCODE_CLI_API_KEY"]["valueFrom"]["secretKeyRef"]["key"] == (
        "OPENCODE_ZEN_API_KEY"
    )
