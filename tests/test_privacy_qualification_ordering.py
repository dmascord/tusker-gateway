"""Privacy qualification must not starve providers behind a bounded batch.

Regression for 2026-11-22: the privacy probe used a plain ``sorted()[:limit]``
selection and treated fresh ``unavailable`` evidence as not due. The eight
alphabetically-first ``commandcode/Qwen/*`` routes were re-probed every cycle
(always returning ``unavailable``), so ~180 other auto-discovered privacy
routes never got a probe. Composite/tool qualification already interleaves due
routes across providers for exactly this reason.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_privacy_qualification_spans_providers_when_unavailable_is_due(
    tmp_path, monkeypatch
):
    from tusker_gateway import privacy_qualification as pq
    from tusker_gateway.model_capability import (
        PRIVACY_ZDR_CAPABILITY,
        PRIVACY_ZDR_PROBE_VERSION,
        ModelCapabilityDB,
    )

    cap_path = str(tmp_path / "cap.db")
    db = ModelCapabilityDB(cap_path)
    # Two providers, all routes freshly marked ``unavailable`` (the state the
    # real pool had). "aaa" sorts entirely before "zzz", so a plain sorted
    # batch would spend every slot on it.
    pairs = [(provider, f"model-{index}") for provider in ("aaa", "zzz") for index in range(3)]
    for provider, model in pairs:
        db.record(
            provider=provider,
            model=model,
            capability=PRIVACY_ZDR_CAPABILITY,
            status="unavailable",
            source="privacy_probe",
            probe_version=PRIVACY_ZDR_PROBE_VERSION,
            failure_class="rate_limit_exceeded",
            checked_at=time.time(),
        )

    monkeypatch.setattr(
        pq,
        "load_config",
        lambda: {
            "quality_db_path": str(tmp_path / "quality.db"),
            "model_capability_db_path": cap_path,
        },
    )
    specs = [SimpleNamespace(provider=p, model=m, auto_discovered=True) for p, m in pairs]
    manager = MagicMock()
    manager.models = {"privacy": specs}
    monkeypatch.setattr("tusker_gateway.pools.PoolManager", lambda _config: manager)

    class _Registry:
        async def refresh_all(self, session):
            return None

    monkeypatch.setattr(
        "tusker_gateway.modality_qualification._catalog_registry",
        lambda *_args, **_kwargs: _Registry(),
    )
    probed: list[tuple[str, str]] = []

    async def _fake_probe(_config, _session, *, provider, model, **_kwargs):
        probed.append((provider, model))
        return {"status": "passed", "http_status": 200}

    monkeypatch.setattr(pq, "probe_privacy_model", _fake_probe)

    results = await pq.run_privacy_qualification(
        max_concurrency=1, timeout_secs=1, max_age_secs=86_400.0, limit=2
    )

    assert len(results) == 2
    assert len(probed) == 2
    # The bounded batch reached both providers instead of stalling on "aaa".
    assert {provider for provider, _ in probed} == {"aaa", "zzz"}
