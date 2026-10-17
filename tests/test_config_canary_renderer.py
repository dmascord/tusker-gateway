"""Regression tests for the canary renderer's env-isolation logic.

The canary clones the live production env so the operator can validate a
config-store change against real provider credentials. Two invariants are
non-negotiable and easy to silently break:

1. ``_image_with_digest`` must preserve the registry host, port, and repo
   path. A naive tag-strip that splits on the first colon deletes the
   registry port (``registry:5000/repo`` -> ``registry``) and produces
   an un-pullable reference.

2. The upstream-calling background loops defined in ``SPEND_GUARD_ENV``
   must be re-emitted as their disabled values, and the dropped entries
   must not survive a duplicate shadow in the rendered container env.

Both regressions are silent: the rendered YAML is structurally valid
and ``kubectl apply`` accepts it, but the pod cannot pull its image or
runs an unbounded billable background loop against shared prod
credentials.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_K8S = Path(__file__).resolve().parent.parent / "k8s"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _K8S / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_canary = _load_module("config_canary", "config-canary.py")

_DIGEST = "sha256:" + "c8e7980c55fe529791f722bd057c3a2c09ce4e3d281d4c205a8262688a2509f5"


def _live(image: str = "registry.tusker.net.au:5000/tusker-gateway:latest",
          extra_env: list[dict] | None = None) -> dict:
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "image": image,
                            "env": extra_env
                            or [
                                {"name": "TUSKER_QUALIFICATION_MAINTENANCE_ENABLED", "value": "true"},
                                {"name": "TUSKER_STRUCTURED_QUALIFICATION_ENABLED", "value": "true"},
                                {"name": "TUSKER_MEDIA_QUALIFICATION_ENABLED", "value": "true"},
                                {"name": "TUSKER_OPENCODE_WARM_ENABLED", "value": "1"},
                            ],
                            "envFrom": [],
                        }
                    ]
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# _image_with_digest: registry port preservation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "image",
    [
        "registry.tusker.net.au:5000/tusker-gateway:latest",
        "registry.tusker.net.au:5000/tusker-gateway",
        "registry.tusker.net.au:5000/tusker-gateway@sha256:" + "8" * 64,
        "docker.io/library/postgres:16-alpine",
        "postgres:16",
    ],
)
def test_image_with_digest_preserves_registry_and_repo(image: str) -> None:
    out = _canary._image_with_digest(_live(image), _DIGEST)
    # Must end with @<digest>; never a bare tag.
    assert out.endswith("@" + _DIGEST), out
    # For images that had a registry host:port or path, the rendered ref
    # must keep the host (and its port) plus the repo path.
    if "/" in image:
        before_at = image.split("@", 1)[0]
        host_segment = before_at.split("/", 1)[0]
        assert out.startswith(host_segment + "/"), (image, out)
    else:
        # Short refs (e.g. "postgres:16") must keep the repo name.
        repo = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
        assert out.startswith(repo + "@"), (image, out)


def test_image_with_digest_already_full_ref_passthrough() -> None:
    full = f"registry.tusker.net.au:5000/tusker-gateway@{_DIGEST}"
    assert _canary._image_with_digest(_live(), full) == full


def test_image_with_digest_rejects_empty() -> None:
    with pytest.raises(ValueError):
        _canary._image_with_digest(_live(), "")


# ---------------------------------------------------------------------------
# SPEND_GUARD_ENV: bounded-spend isolation
# ---------------------------------------------------------------------------

def test_spend_guards_disabled_in_rendered_deployment() -> None:
    dep = _canary._build_canary_deployment(_live(), f"registry.tusker.net.au:5000/tusker-gateway@{_DIGEST}")
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
    by_name = {e["name"]: e["value"] for e in env if "value" in e}
    for name, expected in _canary.SPEND_GUARD_ENV:
        assert by_name.get(name) == expected, (name, by_name.get(name), expected)


def test_spend_guards_do_not_duplicate_live_enabled_value() -> None:
    """A copied prod 'true' shadowing the appended 'false' would re-enable the loop."""
    dep = _canary._build_canary_deployment(_live(), f"registry.tusker.net.au:5000/tusker-gateway@{_DIGEST}")
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
    for name, _ in _canary.SPEND_GUARD_ENV:
        occurrences = [e for e in env if e.get("name") == name]
        assert len(occurrences) == 1, (name, occurrences)
        assert occurrences[0]["value"] == "false"


def test_dropped_vars_use_canary_specific_secrets() -> None:
    dep = _canary._build_canary_deployment(_live(), f"registry.tusker.net.au:5000/tusker-gateway@{_DIGEST}")
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
    db = next(e for e in env if e["name"] == "TUSKER_STATE_DATABASE_URL")
    assert db["valueFrom"]["secretKeyRef"]["name"] == _canary.CANARY_POSTGRES_SECRET
    enc = next(e for e in env if e["name"] == "TUSKER_KEY_ENCRYPTION_KEY")
    assert enc["valueFrom"]["secretKeyRef"]["name"] == _canary.CANARY_ENCRYPTION_SECRET


def test_unrelated_env_passes_through() -> None:
    """A non-guarded prod value must survive the canary render unchanged."""
    dep = _canary._build_canary_deployment(_live(), f"registry.tusker.net.au:5000/tusker-gateway@{_DIGEST}")
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "TUSKER_OPENCODE_WARM_ENABLED", "value": "1"} in env



def test_spend_guard_covers_every_qualification_loop_toggle() -> None:
    """Every interval-driven qualification toggle the code reads must be guarded.

    Regression: ``TUSKER_MODALITY_QUALIFICATION_ENABLED`` was missing from the
    guard while production ran it ``true``, so the canary would have probed
    every candidate model upstream on a 12h timer against shared credentials.
    Derive the required set from the gateway source (not the guard literal) so a
    newly added loop cannot silently escape the guard.
    """
    import re

    src_root = Path(__file__).resolve().parent.parent / "tusker_gateway"
    pattern = re.compile(
        r'os\.environ\.get\(\s*"'
        r'(TUSKER_[A-Z0-9_]*QUALIFICATION[A-Z0-9_]*ENABLED)'
        r'"'
    )
    toggles: set[str] = set()
    for path in src_root.rglob("*.py"):
        toggles.update(pattern.findall(path.read_text(encoding="utf-8")))

    assert toggles, "expected to discover qualification toggles in tusker_gateway/"
    guarded = {name for name, _ in _canary.SPEND_GUARD_ENV}
    missing = toggles - guarded
    assert not missing, f"qualification loops not spend-guarded: {sorted(missing)}"