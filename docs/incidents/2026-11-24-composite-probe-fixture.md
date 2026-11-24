## 2026-11-24 — Composite image+tools probe asked for a stripe the fixture never had

### Why

Re-probing `ollama-cloud/deepseek-v4.1-flash:1017` (which had been held out of
privacy image+tools routing by a transient 429) returned a *definitive*
`unsupported` with `failure_class=non_strict_tool_contract`, HTTP 200. The
suspicion was a genuine model limitation. It was not: the probe was asking the
model to describe an image the fixture could not support.

### Root cause

`probe_image_tool_model` sends the modality probe's fixture
(`_TINY_IMAGE_DATA_URL`) and instructs the model to report the colour of the
image's **middle stripe**, accepting only `IMAGE_PROBE_COLOR` (`green`). The
fixture was a **2×2 quadrant grid** — red top-left, blue top-right, green
bottom-left, yellow bottom-right — with no stripe anywhere.

Decoding the fixture (128×128 RGB, all rows filter type 0):

```
y=0..63   left=(220,30,30) red    right=(30,120,220) blue
y=64..127 left=(30,180,70) green  right=(240,200,40) yellow
```

The model answered the prompt honestly and refused to invent an observation:

> **Four-color** — a 2×2 grid of red, blue, green, and yellow quadrants. I did
> not call `tusker_capability_probe`, because it requires a
> `middle_stripe_color`, and this image has no middle stripe — its center is
> the boundary point where four quadrants meet, so any red/green/blue value I
> supplied would be invented rather than observed.

The probe recorded that as `unsupported` / `non_strict_tool_contract`. Because
`unsupported` is a *definitive* failure in `_image_tools_evidence_allows`, any
model that answered truthfully about the fixture was permanently excluded from
image+tools routing. Only a model willing to guess — or one that emitted a
structurally invalid call that happened to be classified the same way — could
pass. The gate rewarded guessing.

Direct upstream probe with the same payload and the corrected fixture confirms
the route is capable:

```
RESULT {'provider': 'ollama-cloud', 'model': 'deepseek-v4.1-flash:1017',
        'capability': 'input_image_tools', 'source': 'composite_probe',
        'status': 'passed', 'http_status': 200, 'latency_ms': 1307.6}
```

A follow-up raw upstream call with the *old* fixture returned `FINISH stop` and
prose, no tool call — the model declining an impossible request.

### Fix

- `tusker_gateway/modality_qualification.py`: the fixture is now three
  horizontal stripes — red (rows 0–42), **green (43–85, the literal middle
  stripe the probe names)**, blue (86–127). Still a decodable 128×128 RGB PNG
  meeting the DashScope minimum-size check (268 bytes).
- `tusker_gateway/tool_qualification.py`: `COMPOSITE_CAPABILITY_PROBE_VERSION`
  `image-tool-contract-v2` → `image-tool-contract-v3`. Every v2 verdict was
  measured against an impossible prompt and must go stale.
- `tusker_gateway/pools.py`: `COMPOSITE_IMAGE_TOOLS_PROBE_VERSION` bumped in
  lockstep; v2 records are no longer authoritative.
- `tusker_gateway/tool_qualification.py` (`_result_from_stream`): a
  contract-shaped call carrying the *wrong* colour is now reported as
  `image_evidence_mismatch` instead of `non_strict_tool_contract`. The old
  class conflated "honoured the tool contract but observed differently" with
  "malformed call", which is what made this defect hard to see.

### Tests

- `tests/test_model_capability.py::test_probe_image_middle_stripe_matches_composite_probe_answer`
  decodes the fixture the probe actually sends and asserts the centre pixel is
  `IMAGE_PROBE_COLOR` while the top and bottom rows are not. Fails against
  `image-tool-contract-v2` source; passes after the fixture change.
- `tests/test_tool_capability.py::test_composite_probe_rejects_forced_call_without_image_evidence`
  now asserts `image_evidence_mismatch`. Fails against unfixed source.
- `tests/test_pools.py::test_composite_evidence_from_impossible_fixture_version_is_not_authoritative`
  pins the version boundary: a curated route is eligible under a v2
  `unsupported` but denied under v3; auto-discovered routes stay withheld
  until re-probed.
- Version literals in `tests/test_pools.py` now reference
  `COMPOSITE_IMAGE_TOOLS_PROBE_VERSION` instead of duplicating the string, so
  the next bump cannot silently desynchronise the tests.

### Caveats

- Existing v2 `input_image_tools` rows are now stale, not deleted. Routes that
  previously passed v2 are eligible again, but no route carries v3 evidence
  until the composite qualification cycle runs (interval 43 200 s, initial
  delay 600 s) or an operator runs
  `tusker-gateway-qualify-image-tools --model PROVIDER/MODEL`.
- The fixture is deliberately trivial (three blocks, one question). It proves
  the model performed *an* image-conditioned observation, not fine-grained
  visual reasoning. A model that happened to guess `green` at 1/3 odds would
  pass; tightening this further would need a randomised fixture per probe,
  which requires the probe to persist the expected answer alongside the
  verdict. That remains open.
- `image_evidence_mismatch` is a new failure class; nothing consumes it yet
  beyond diagnostics and the `unsupported` status. Dashboards keyed on
  `non_strict_tool_contract` will see that class drop.
