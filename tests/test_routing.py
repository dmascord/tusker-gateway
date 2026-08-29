"""Tests for role-alias routing and model splitting."""
from __future__ import annotations

from tusker_gateway.routing import resolve_route, split_model


def test_split_model():
    assert split_model("p1::m1") == ("p1", "m1")
    assert split_model("m1") == (None, "m1")
    assert split_model(None) == (None, None)


def test_resolve_route():
    # 1. Alias
    r1 = resolve_route("hermes-privacy", {})
    assert r1.kind == "pool"
    assert r1.pool_name == "privacy"
    
    # 2. Provider prefix
    r2 = resolve_route("p1::m1", {})
    assert r2.kind == "passthrough"
    assert r2.provider == "p1"
    assert r2.model == "m1"
    
    # 3. Slash form
    r3 = resolve_route("p1/m1", {})
    assert r3.kind == "passthrough"
    assert r3.provider == "p1"
    assert r3.model == "m1"
    
    # 4. Swarm role
    r4 = resolve_route("hermes-gateway/foo", {})
    assert r4.kind == "swarm"
    
    # 5. Default
    r5 = resolve_route(None, {})
    assert r5.kind == "pool"
    assert r5.pool_name == "code"


def test_gateway_qualified_pool_aliases_resolve_to_pools():
    for alias, pool in (
        ("hermes-code", "code"),
        ("hermes-privacy", "privacy"),
        ("hermes-premium", "premium"),
        ("hermes-swarm", "swarm"),
    ):
        for model in (f"tusker-gateway/{alias}", f"tusker-gateway::{alias}"):
            route = resolve_route(model, {})
            assert route.kind == "pool"
            assert route.pool_name == pool
