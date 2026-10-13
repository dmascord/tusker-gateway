"""Pin the CLI drift-check wiring to the Dockerfile and the repo script.

The weekly CronJob runs a ConfigMap copy of
``k8s/check_cli_version_drift.py`` with the CLI pins duplicated in env
(there is no repo checkout in-cluster). These tests make that
duplication self-checking: bump the Dockerfile ARG without mirroring it
in ``k8s/cli-drift-cronjob.yaml`` and CI fails.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "k8s" / "check_cli_version_drift.py"
MANIFEST = ROOT / "k8s" / "cli-drift-cronjob.yaml"
DOCKERFILE = ROOT / "Dockerfile"

PINS = ("CLAUDE_CODE_CLI_VERSION", "OPENCODE_CLI_VERSION", "KILO_CLI_VERSION")


def _docs() -> tuple[dict, dict]:
    config_map, cron_job = yaml.safe_load_all(MANIFEST.read_text())
    return config_map, cron_job


def _container() -> dict:
    _, cron_job = _docs()
    return cron_job["spec"]["jobTemplate"]["spec"]["template"]["spec"][
        "containers"
    ][0]


def test_dockerfile_declares_all_cli_pins():
    text = DOCKERFILE.read_text()
    for arg in PINS:
        assert re.search(rf"^ARG {arg}=\S+$", text, re.MULTILINE), arg


def test_cronjob_env_pins_mirror_the_dockerfile():
    env = {item["name"]: str(item["value"]) for item in _container().get("env", [])}
    text = DOCKERFILE.read_text()
    dockerfile_pins = {
        arg: re.search(rf"^ARG {arg}=(\S+)$", text, re.MULTILINE).group(1)
        for arg in PINS
    }
    assert {name: env[name] for name in PINS} == dockerfile_pins


def test_cronjob_mounts_repo_script_verbatim():
    config_map, _ = _docs()
    key = "check_cli_version_drift.py"
    assert config_map["data"][key] == SCRIPT.read_text()
    assert _container()["command"] == ["python3", f"/check/{key}"]
