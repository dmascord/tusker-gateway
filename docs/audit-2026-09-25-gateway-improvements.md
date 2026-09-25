# Gateway Audit Improvements Plan

**Audit Date:** 2026-09-25  
**Auditor:** omp agent  
**Scope:** tusker-gateway codebase, k8s manifests, configuration

This document captures all findings from the 2026-09-25 security, API correctness, reliability, and operations audit, with concrete implementation steps for each item.

---

## Legend

| Symbol | Meaning |
|--------|---------|
| 🔴 P0 | Must fix before external/multi-tenant use |
| 🟠 P1 | High priority, fix in current sprint |
| 🟡 P2 | Medium priority, fix in next sprint |
| 🟢 P3 | Lower priority, backlog |
| ✅ | Already correct / no action needed |
| ❌ | Bug confirmed - needs fix |
| ⚠️ | Design risk or latent issue |

---

## Phase 1: Release-Blocking (Before External Use)

### 1.1 Enterprise Deadline and Idempotency Controls Are Unwired

**Severity:** 🔴 P0  
**Evidence:** `tusker_gateway/app.py:158-176,563-573`

**What happens:**
- `attach_deadline_middleware()` and `attach_idempotency_middleware()` are imported but never called
- Request deadlines are not enforced
- `Idempotency-Key` header deduplication is inactive
- Startup log falsely reports these controls as enabled

**Implementation Steps:**

```python
# In tusker_gateway/app.py, after attach_authorization_middleware(app)
# around line 569-573

# Add these two calls:
attach_deadline_middleware(app, deadline_cfg)
attach_idempotency_middleware(app, idempotency)
```

**Verification:**
1. Add test in `tests/test_enterprise_controls.py` that asserts middleware is in `app.middlewares`
2. Add integration test: send request with `Idempotency-Key`, verify deduplication works
3. Add integration test: send request exceeding deadline, verify 504 response

**Files to change:**
- `tusker_gateway/app.py` (add 2 lines)
- `tests/test_enterprise_controls.py` (add middleware presence assertion)

---

### 1.2 Metrics and Dashboard Authentication Fails Open

**Severity:** 🔴 P0  
**Evidence:** `tusker_gateway/app.py:545-554`

**What happens:**
- When `TUSKER_METRICS_TOKEN` is unset (empty), `/metrics` and `/dashboard` are served without authentication
- Exposes pool state, cooldowns, breaker status, quality scores

**Implementation Steps:**

Option A - Fail closed (recommended):
```python
# In tusker_gateway/app.py, modify the metrics/dashboard auth check
if not metrics_token:
    # Instead of passing through, require Bearer auth or fail
    return web.json_response(
        {"error": {"message": "metrics token not configured", "type": "invalid_request_error"}},
        status=401
    )
```

Option B - Use Bearer with admin:read scope:
```python
# Integrate with existing auth system - require valid Bearer + admin:read
```

**Also update k8s:**
```yaml
# In k8s/deployment.yaml, add:
- name: TUSKER_METRICS_TOKEN
  valueFrom:
    secretKeyRef:
      name: tusker-env-vault
      key: TUSKER_METRICS_TOKEN
      optional: true  # Allow startup, but code fails closed
```

**Verification:**
1. Start gateway without `TUSKER_METRICS_TOKEN`, verify `/metrics` returns 401
2. Start with valid token, verify access works
3. Verify dashboard partials also require auth

**Files to change:**
- `tusker_gateway/app.py`
- `k8s/deployment.yaml`

---

### 1.3 Approval State Not Tenant-Scoped (Cross-Tenant Approval Binding)

**Severity:** 🔴 P0  
**Evidence:** `tusker_gateway/approval_store.py:30-76`

**What happens:**
- Approval table has no `caller_fingerprint` column
- `load_active()` returns all pending approvals to a process-global dict
- Tenant B's answer could bind to Tenant A's pending approval

**Implementation Steps:**

```sql
-- Migration: Add caller_fingerprint to approval table
ALTER TABLE tusker_native_approvals ADD COLUMN caller_fingerprint TEXT NOT NULL;
```

```python
# In tusker_gateway/approval_store.py

def put(self, approval_id: str, pending: dict[str, Any], caller_fingerprint: str) -> None:
    # ... existing code ...
    # Add caller_fingerprint to insert
    conn.execute(f"""
        INSERT INTO {_TABLE} (approval_id, caller_fingerprint, payload, expires_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(approval_id) DO UPDATE SET
            caller_fingerprint = excluded.caller_fingerprint,
            payload = excluded.payload,
            expires_at = excluded.expires_at,
            updated_at = excluded.updated_at
    """, (approval_id, caller_fingerprint, encoded, expires_at, now))

def load_active(self, caller_fingerprint: str, now: float | None = None) -> dict[str, dict[str, Any]]:
    # Filter by caller_fingerprint
    conn.execute(f"""
        SELECT approval_id, payload, expires_at FROM {_TABLE}
        WHERE caller_fingerprint = ? AND expires_at > ?
    """, (caller_fingerprint, now))
```

**Verification:**
1. Create approval with API key A
2. Try to load/answer with API key B
3. Verify rejection

**Files to change:**
- `tusker_gateway/approval_store.py`
- `tusker_gateway/native_question.py` (pass caller fingerprint through)

---

### 1.4 MCP Approval Uses Caller-Asserted Approval + Hardcoded Key

**Severity:** 🔴 P0  
**Evidence:** `tusker_gateway/mcp_guard.py:41-44,79-86,150-165`

**What happens:**
- Approval is self-asserted (caller says "approved: true")
- Hardcoded fallback key: `"tusker-dev-approval-key"`
- Replay set clears entirely at 4096 entries

**Implementation Steps:**

```python
# In tusker_gateway/mcp_guard.py

# 1. Remove hardcoded fallback - fail if no key configured
def _approval_key() -> bytes:
    key = (
        os.environ.get("TUSKER_APPROVAL_HMAC_KEY")
        or os.environ.get("TUSKER_AUDIT_HMAC_KEY")
    )
    if not key:
        raise RuntimeError(
            "TUSKER_APPROVAL_HMAC_KEY or TUSKER_AUDIT_HMAC_KEY must be configured "
            "when MCP guard is enabled"
        )
    return hashlib.sha256(key.encode()).digest()

# 2. For high-impact tools, require approval from a DISTINCT principal
# The current design is "automation confirmation" not "human approval"
# Document this explicitly or implement separate human approval flow

# 3. Fix replay set - use per-bucket TTL instead of clear
from collections import defaultdict
import time

_USED_STATES: dict[str, float] = {}
_USED_STATES_TTL = 300  # 5 minutes

def _consume_state(state: str) -> bool:
    now = time.time()
    # Prune expired entries
    expired = [k for k, v in _USED_STATES.items() if now - v > _USED_STATES_TTL]
    for k in expired:
        del _USED_STATES[k]
    
    if state in _USED_STATES:
        return False
    _USED_STATES[state] = now
    return True
```

**Verification:**
1. Start without HMAC key - verify RuntimeError at startup
2. Test approval replay is blocked within TTL window
3. Test approval succeeds after TTL expires

**Files to change:**
- `tusker_gateway/mcp_guard.py`

---

### 1.5 Hardcoded Development API Key

**Severity:** 🔴 P0 (latent)  
**Evidence:** `tusker_gateway/auth.py:18,74-96`

**What happens:**
- `"sk-secret-dev"` accepted when API_KEYS list is empty
- Currently unreachable (config generates random key), but shipped in source

**Implementation Steps:**

```python
# In tusker_gateway/auth.py, remove these lines (around lines 18, 86-93)

# DELETE:
# _DEV_KEY = "sk-secret-dev"

# And in verify() method, remove the entire dev-bypass branch:
# if not allowed and not db_keys_authoritative and secrets.compare_digest(token, _DEV_KEY):
#     logger.debug('auth OK (dev key)')
#     ...

# Alternative: require explicit env flag
if os.environ.get("TUSKER_DEV_KEY_ENABLED", "").lower() in ("1", "true"):
    # Only then check dev key
    pass
```

**Verification:**
1. Search for `"sk-secret-dev"` in codebase - should find none after change
2. Verify normal auth still works

**Files to change:**
- `tusker_gateway/auth.py`

---

## Phase 2: API Correctness (Current Sprint)

### 2.1 Provider 429 Responses Become Gateway 502

**Severity:** 🟠 P1  
**Evidence:** `tusker_gateway/endpoints.py:3388-3453`

**What happens:**
- Provider rate limit returns 429
- Gateway converts to 502 with generic message
- Client loses Retry-After guidance

**Implementation Steps:**

```python
# In tusker_gateway/endpoints.py, add handling for RateLimitError

from tusker_gateway.errors import RateLimitError

async def _handle_provider_response(exc: BaseException, ...) -> web.Response:
    if isinstance(exc, RateLimitError):
        # Extract retry-after if available
        retry_after = getattr(exc, 'retry_after', None)
        if not retry_after:
            # Try to parse from body
            try:
                body = json.loads(exc.body)
                retry_after = body.get('retry_after') or body.get('error', {}).get('retry_after')
            except:
                retry_after = 5  # default
        
        return web.json_response(
            openai_error(
                "Rate limit exceeded",
                code="rate_limit_error",
                error_type="rate_limit_error"
            ),
            status=429,
            headers={"Retry-After": str(int(retry_after))}
        )
    
    # Existing handling for other errors
    return _public_provider_failure_response(exc, route_kind=...)
```

**Verification:**
1. Mock provider returning 429
2. Verify gateway returns 429 with Retry-After header
3. Verify client receives correct error code

**Files to change:**
- `tusker_gateway/endpoints.py`

---

### 2.2 Swarm Routes Recognized But Not Dispatched

**Severity:** 🟠 P1  
**Evidence:** `tusker_gateway/routing.py:154-158`, `tusker_gateway/endpoints.py:5288-5317`

**What happens:**
- `hermes-gateway/*` and `hermes-reflect/*` routes resolve to `kind="swarm"`
- `_route_target()` raises BadRequestError for unknown kinds

**Implementation Steps:**

Option A - Implement swarm dispatch (if needed):
```python
# In tusker_gateway/endpoints.py, extend _route_target()

if route.kind == "swarm":
    # TODO: Implement swarm dispatch logic
    # For now, reject with clear message
    raise BadRequestError(
        f"Swarm routing not yet implemented for {route.model}",
        code="unsupported_route"
    )
```

Option B - Remove unsupported routes (recommended if not planned):
```python
# In tusker_gateway/routing.py, remove swarm markers or document as unsupported

# Comment out or remove:
# SWARM_ROLE_MARKERS = ("hermes-gateway/", "hermes-reflect/")
# Or add explicit documentation that these are reserved for future use
```

**Verification:**
1. Request `hermes-gateway/some-model`
2. Verify clear error message (not generic "Unsupported model route")

**Files to change:**
- `tusker_gateway/routing.py` and/or `tusker_gateway/endpoints.py`

---

### 2.3 Provider Model Names Leak Into Client Responses

**Severity:** 🟠 P1  
**Evidence:** `tusker_gateway/endpoints.py:6313-6330`

**What happens:**
- Request for `hermes-code` returns `"model": "claude-sonnet-4.6-20250514"`
- Breaks caching, client expectations

**Implementation Steps:**

```python
# In tusker_gateway/endpoints.py

async def chat_completions_handler(request: web.Request):
    # ... existing code ...
    
    # After getting result from provider
    result = await client.chat(...)
    
    # Normalize model name to requested model
    requested_model = body.get("model")
    if requested_model and result.get("model"):
        # Only rewrite if the gateway owns this model (is an alias)
        if requested_model in POOL_ALIASES or requested_model.startswith("hermes-"):
            result["model"] = requested_model
    
    return web.json_response(result)

# Also fix streaming - in _normalize_stream or similar
def format_openai_chunk(chunk: dict, requested_model: str) -> dict:
    # Rewrite chunk['model'] if needed
    if chunk.get("model") and requested_model:
        if requested_model in POOL_ALIASES or requested_model.startswith("hermes-"):
            chunk["model"] = requested_model
    return chunk
```

**Verification:**
1. Request `hermes-code`
2. Verify response model is `hermes-code`, not concrete provider model
3. Test streaming response model names

**Files to change:**
- `tusker_gateway/endpoints.py`

---

### 2.4 Responses API Forwards Fields Chat Providers Reject

**Severity:** 🟠 P1  
**Evidence:** `tusker_gateway/endpoints.py:6481-6506`

**What happens:**
- Fields like `reasoning`, `text`, `store`, `metadata` forwarded to Chat Completions
- Some providers return 400

**Implementation Steps:**

```python
# In tusker_gateway/endpoints.py, in _responses_handler_impl

# Explicit field mapping - only known-compatible fields
_CHAT_COMPATIBLE_FIELDS = {
    "temperature", "top_p", "max_tokens", "max_completion_tokens",
    "stop", "presence_penalty", "frequency_penalty", "logit_bias",
    "seed", "response_format", "tools", "tool_choice", "n", "stream",
    "stream_options", "user", "reasoning_effort"
}

chat_body = {
    key: value
    for key, value in body.items()
    if key in _CHAT_COMPATIBLE_FIELDS
}

# Add the required fields
chat_body.update({
    "model": body.get("model"),
    "messages": messages,
    "stream": bool(body.get("stream", False)),
})
```

**Verification:**
1. Call `/v1/responses` with `reasoning: {"effort": "high"}`
2. Verify request succeeds against standard Chat provider (or fails with clear error)

**Files to change:**
- `tusker_gateway/endpoints.py`

---

### 2.5 `/v1/models` Is Incomplete

**Severity:** 🟡 P2  
**Evidence:** `tusker_gateway/endpoints.py:5320-5355`

**What happens:**
- Only returns gateway aliases and provider aliases
- Doesn't include concrete catalog models

**Implementation Steps:**

```python
# In tusker_gateway/endpoints.py, extend models_handler

# Add discovered models from catalog
for provider_name, provider_config in config.get("providers", {}).items():
    # ... existing alias handling ...
    
    # Add: include models from auto-catalog if available
    catalog = provider_config.get("_catalog", {})
    for model in catalog.get("models", []):
        data.append({
            "id": f"{provider_name}/{model['id']}",
            "object": "model",
            "owned_by": provider_name,
            # Note: these are dynamically discovered, may not be usable
        })

# Or document as: aliases are gateway-controlled, catalog is discovery-only
```

**Files to change:**
- `tusker_gateway/endpoints.py`

---

### 2.6 Unknown Bare Model Silently Falls Back to Code Pool

**Severity:** 🟡 P2  
**Evidence:** `tusker_gateway/routing.py:160-162`

**What happens:**
- Request for non-existent model succeeds with unrelated pool

**Implementation Steps:**

```python
# In tusker_gateway/routing.py

# Option A: Fail clearly
if model and model not in POOL_ALIASES:
    # Check if it's a known provider/model
    provider, bare = split_model(model)
    if not provider:
        raise ValueError(f"Unknown model: {model}")

# Option B: Document as compatibility mode
# Add comment explaining this is intentional for backwards compatibility
```

**Files to change:**
- `tusker_gateway/routing.py`

---

## Phase 3: Reliability Under Load (Next Sprint)

### 3.1 Rate-Limit Token Consumption Is Non-Atomic

**Severity:** 🟠 P1  
**Evidence:** `tusker_gateway/rate_limit.py:157-186`

**What happens:**
- Concurrent requests can both pass check while consuming 1 token
- Race: read → compute → write is not atomic

**Implementation Steps:**

```python
# In tusker_gateway/rate_limit.py, use atomic conditional update

def check(self, api_key: str, cost: float | None = None) -> RateLimitDecision:
    # ... setup ...
    
    fp = _key_fingerprint(api_key)
    cost = cost if cost is not None else policy.cost_per_request
    now = time.time()
    
    # Atomic: UPDATE ... WHERE tokens >= cost
    with self._db.connection() as conn:
        # First, try to atomically consume tokens
        result = conn.execute("""
            UPDATE buckets 
            SET tokens = tokens - ?,
                last_refill_at = ?
            WHERE fingerprint = ? 
            AND tokens >= ?
        """, (cost, now, fp, cost))
        conn.commit()
        
        if result.rowcount == 0:
            # Either no row exists, or insufficient tokens
            # Check current state
            row = conn.execute(
                "SELECT tokens FROM buckets WHERE fingerprint = ?",
                (fp,)
            ).fetchone()
            
            if row is None:
                # First time - insert with full burst minus cost
                tokens = policy.burst - cost
                conn.execute("""
                    INSERT INTO buckets (fingerprint, tokens, last_refill_at)
                    VALUES (?, ?, ?)
                """, (fp, max(0, tokens), now))
            else:
                tokens = row[0]
                # Refill and check again
                elapsed = now - row[1]  # last_refill_at
                tokens = min(policy.burst, tokens + elapsed * policy.rate_per_sec)
                
                if tokens >= cost:
                    tokens -= cost
                    conn.execute("""
                        UPDATE buckets SET tokens = ?, last_refill_at = ?
                        WHERE fingerprint = ?
                    """, (tokens, now, fp))
                else:
                    conn.rollback()
                    return RateLimitDecision(allowed=False, remaining=tokens)
            
            conn.commit()
    
    return RateLimitDecision(allowed=True, remaining=tokens)
```

**Verification:**
1. Write concurrent test: 10 threads, 1 token limit, verify only 1 succeeds
2. Run against both SQLite and PostgreSQL

**Files to change:**
- `tusker_gateway/rate_limit.py`

---

### 3.2 Trace Parent State Is Process-Global

**Severity:** 🟠 P1  
**Evidence:** `tusker_gateway/tracing.py:158-175,258-278`

**What happens:**
- `_current` is a module-global list
- Concurrent requests can corrupt parent-child relationships

**Implementation Steps:**

```python
# In tusker_gateway/tracing.py

import contextvars

# Replace module-global list with ContextVar
_current: contextvars.ContextVar[list[Span]] = contextvars.ContextVar(
    'trace_current', default=[]
)

def _push_current(span: Span) -> None:
    current = _current.get()
    current.append(span)
    _current.set(current)

def _pop_current(span: Span) -> None:
    current = _current.get()
    if current and current[-1] is span:
        current.pop()
    else:
        try:
            current.remove(span)
        except ValueError:
            pass
    _current.set(current)

def _last_span_id() -> str | None:
    current = _current.get()
    return current[-1].span_id if current else None
```

**Verification:**
1. Write test: 10 concurrent requests, each creates nested spans
2. Verify each request's trace tree is isolated

**Files to change:**
- `tusker_gateway/tracing.py`

---

### 3.3 Circuit Breaker Half-Open Reservation

**Severity:** 🟠 P1  
**Evidence:** `tusker_gateway/circuit_breaker.py` (per audit)

**What happens:**
- Multiple pods can reserve same half-open probe
- Can overwhelm recovering provider

**Implementation Steps:**

```python
# In tusker_gateway/circuit_breaker.py

def try_reserve_probe(self, provider: str, model: str) -> bool:
    """Atomically reserve a probe slot."""
    with self._db.connection() as conn:
        # Atomic conditional update
        result = conn.execute("""
            UPDATE circuit_breakers
            SET in_flight = 1
            WHERE provider = ? AND model = ?
            AND state = 'half_open' AND in_flight = 0
        """, (provider, model))
        conn.commit()
        
        return result.rowcount > 0
```

**Verification:**
1. Test concurrent probe reservation from multiple "pods" (connections)
2. Verify only one succeeds

**Files to change:**
- `tusker_gateway/circuit_breaker.py`

---

### 3.4 Permanent Failure State Is Process-Local

**Severity:** 🟡 P2  
**Evidence:** `tusker_gateway/cooldown.py`

**What happens:**
- Permanent 404/410 markers lost on restart
- Gateway immediately re-probes dead routes

**Implementation Steps:**

1. Add persistence to `PersistentCooldownStore`
2. Ensure `hydrate()` calls all hydrate methods:
   - `hydrate_permanent_failures()`
   - `hydrate_providers()`
   - `hydrate_models()`

```python
# In tusker_gateway/persistent_cooldown.py

def hydrate(self) -> None:
    """Load all cooldown state from persistent storage."""
    self._hydrate_models()
    self._hydrate_providers()
    self._hydrate_permanent_failures()  # ADD THIS

def _hydrate_permanent_failures(self) -> None:
    # Load from DB and populate in-memory state
    pass
```

**Files to change:**
- `tusker_gateway/persistent_cooldown.py`

---

### 3.5 State Degraded Mode Conflicts With Enforcement Controls

**Severity:** 🟡 P2  
**Evidence:** `k8s/deployment.yaml`, rate limiter fallback policy

**What happens:**
- `TUSKER_STATE_DEGRADED_MODE=advisory` means storage unavailability is logged but requests continue
- Rate limiter correctly fails closed, but budget/quota may not

**Implementation Steps:**

1. Audit each control's fallback behavior
2. Document required vs advisory dependencies
3. Add explicit readiness fields

```python
# In tusker_gateway/health.py

async def ready_handler(request):
    config = request.app.get("config", {})
    
    # Check required dependencies
    issues = []
    
    if config.get("ratelimit_enabled"):
        rl = request.app.get("ratelimit")
        if rl._db is None:
            issues.append("rate_limiting_unavailable")
    
    # ... other checks ...
    
    if issues:
        return web.json_response({
            "ready": False,
            "issues": issues
        }, status=503)
    
    return web.json_response({"ready": True})
```

**Files to change:**
- `tusker_gateway/health.py`
- Individual control modules

---

## Phase 4: Operational Quality

### 4.1 Production Has One Gateway Replica

**Severity:** 🟡 P2  
**Evidence:** `k8s/deployment.yaml:1-28`

**What happens:**
- Node failure = total outage
- Zero-unavailable rollout only helps planned updates

**Implementation Steps:**

1. First: verify shared state works with multiple replicas
2. Then: increase replica count
3. Add PodDisruptionBudget

```yaml
# In k8s/deployment.yaml
spec:
  replicas: 2  # or more
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
---
# Add PDB
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: tusker-gateway
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: tusker-gateway
```

**Files to change:**
- `k8s/deployment.yaml`
- Add `k8s/pdb.yaml`

---

### 4.2 Tracing Not Enabled in Production Manifest

**Severity:** 🟡 P2  
**Evidence:** No `TUSKER_OTLP_ENDPOINT` in deployment

**Implementation Steps:**

Option A - Enable with Tempo/collector:
```yaml
# In k8s/deployment.yaml
- name: TUSKER_OTLP_ENDPOINT
  value: "http://tempo:4318"
- name: TUSKER_OTLP_SERVICE_NAME
  value: "tusker-gateway"
```

Option B - Document as intentionally disabled:
```markdown
<!-- In docs/observability.md -->
## Tracing

OTLP tracing is intentionally disabled in production deployment.
Enable by setting `TUSKER_OTLP_ENDPOINT` environment variable.
```

**Files to change:**
- `k8s/deployment.yaml` or documentation

---

### 4.3 Dependency Builds Not Reproducible

**Severity:** 🟡 P2  
**Evidence:** `pyproject.toml` uses version ranges

**Implementation Steps:**

```bash
# Generate constraints file
pip freeze > constraints.txt

# Or use pip-compile
pip-compile pyproject.toml --output-file constraints.txt
```

```dockerfile
# In Dockerfile, use constraints
COPY constraints.txt .
RUN pip install --constraint constraints.txt -r pyproject.toml
```

**Files to change:**
- `constraints.txt` (new)
- `Dockerfile`

---

### 4.4 Documentation Materially Lags Implementation

**Severity:** 🟢 P3  
**Evidence:** Multiple doc inconsistencies

**Implementation Steps:**

Create a living capability document:

```markdown
# docs/capability-status.md

## Capability Status

| Capability | Implemented | Enabled (Production) | Notes |
|------------|-------------|---------------------|-------|
| Exact-match cache | ✅ | ❌ | TUSKER_CACHE_ENABLED |
| Semantic cache | ✅ | ❌ | TUSKER_SEMANTIC_CACHE_ENABLED |
| Prometheus metrics | ✅ | ✅ | /metrics |
| OTLP tracing | ✅ | ❌ | TUSKER_OTLP_ENDPOINT |
| Circuit breaker | ✅ | ✅ | TUSKER_CIRCUIT_ENABLED |
| Rate limiting | ✅ | ✅ | TUSKER_RATELIMIT_ENABLED |
| Budget tracking | ✅ | ❌ | TUSKER_BUDGETS_ENABLED |
| Request deadlines | ⚠️ | ❌ | Unwired |
| Idempotency | ⚠️ | ❌ | Unwired |
| Anthropic /v1/messages | ✅ | ✅ | |
| MCP proxy | ✅ | ✅ | |
| Guardrails | ✅ | ❌ | TUSKER_GUARDRAILS_ENABLED |
| CLI tools | ✅ | ✅ | |
```

**Files to change:**
- `docs/capability-status.md` (new)
- Update `docs/feature-matrix-and-plan.md`
- Update `README.md`

---

### 4.5 Stream Diagnostics Retention

**Severity:** 🟢 P3  
**Evidence:** `tusker_gateway/stream_diagnostics.py`

**What happens:**
- Prompt-derived text stored on shared volume
- No eviction when cap reached

**Implementation Steps:**

```python
# Add age-based eviction
def _maybe_cleanup(self):
    # Keep last 100 files, remove oldest
    files = sorted(self._dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
    while len(files) > 100:
        files.pop(0).unlink()

# Add metric for dropped captures
def record_dropped_capture(self, reason: str):
    metrics.tusker_stream_diagnostics_dropped.inc({"reason": reason})
```

**Files to change:**
- `tusker_gateway/stream_diagnostics.py`

---

## Security & Governance Improvements

### 5.1 Trusted Client IP Headers

**Severity:** 🟡 P2  
**Evidence:** `tusker_gateway/observability.py:19-35`

**What happens:**
- `CF-Connecting-IP` and XFF accepted without proxy verification
- Logs/audit can be spoofed

**Implementation Steps:**

```python
# In tusker_gateway/observability.py

TRUSTED_PROXY_RANGES = os.environ.get(
    "TUSKER_TRUSTED_PROXY_RANGES",
    "173.245.48.0/20,103.21.244.0/20"  # Cloudflare IPs
)

def client_ip(request: web.Request) -> str:
    # Check if peer is trusted proxy
    peer = request.transport.get_extra_info('peername')
    if peer and _is_trusted_proxy(peer[0]):
        # Accept forwarded headers
        cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
        if cf_ip:
            return cf_ip
        xff = request.headers.get("X-Forwarded-For", "").strip()
        if xff:
            return xff.split(",")[0].strip()
    
    # Otherwise use direct peer
    return request.remote or "unknown"

def _is_trusted_proxy(ip: str) -> bool:
    # Check against TRUSTED_PROXY_RANGES
    import ipaddress
    for range_str in TRUSTED_PROXY_RANGES.split(","):
        if ipaddress.ip_address(ip) in ipaddress.ip_network(range_str.strip()):
            return True
    return False
```

**Files to change:**
- `tusker_gateway/observability.py`

---

### 5.2 Guardrail Matching Is Overly Broad

**Severity:** 🟡 P2  
**Evidence:** `tusker_gateway/guardrails.py:141-163,222-253`

**What happens:**
- Any email-like string redacted
- Any 16-digit sequence treated as credit card
- Prompt injection patterns can block legitimate code

**Implementation Steps:**

```python
# Improve email regex - require more context
_EMAIL_RE = re.compile(
    r'(?<![a-zA-Z0-9])([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})(?![a-zA-Z0-9])'
)

# Add Luhn check for credit card
def _is_valid_credit_card(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d = d * 2
            if d > 9:
                d = d - 9
        checksum += d
    return checksum % 10 == 0

# More specific injection patterns
_INJECTION_PATTERNS = [
    re.compile(r'^ignore\s+(previous|all)\s+instructions', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^you\s+are\s+(now|a|an?)\s+\w+', re.IGNORECASE | re.MULTILINE),
]
```

**Files to change:**
- `tusker_gateway/guardrails.py`

---

### 5.3 DB Key Revocation Can Be Defeated

**Severity:** 🟡 P2  
**Evidence:** `config_store.py:344`

**What happens:**
- Environment fallback keys merged with DB keys
- Revoking in DB doesn't revoke if key also in env

**Implementation Steps:**

```python
# In tusker_gateway/config_store.py

def _load_api_keys(self) -> list[str]:
    # If DB is authoritative, use ONLY DB keys (after bootstrap)
    if self._db_keys_authoritative:
        # Don't include fallback keys - they're only for bootstrap
        return self._managed_keys  # Only from DB
    
    # Legacy: merge both
    return self._fallback_keys + self._managed_keys
```

**Files to change:**
- `tusker_gateway/config_store.py`

---

## Summary: Implementation Order

| Priority | Item | Est. Effort |
|----------|------|-------------|
| 1 | Wire deadline/idempotency middleware | 1hr |
| 2 | Fail-close metrics/dashboard auth | 2hr |
| 3 | Scope approvals to caller | 4hr |
| 4 | Remove hardcoded MCP/auth keys | 1hr |
| 5 | Fix 429→502 response | 2hr |
| 6 | Fix response model normalization | 2hr |
| 7 | Remove/resolve swarm routes | 1hr |
| 8 | Atomic rate limiting | 4hr |
| 9 | Fix trace context isolation | 2hr |
| 10 | Atomic circuit breaker | 2hr |
| 11 | Persist permanent failures | 2hr |
| 12 | Update documentation | 4hr |

---

## Testing Requirements

For each fix, add:

1. **Unit test** - isolated behavior
2. **Integration test** - against real storage (SQLite + PostgreSQL)
3. **Concurrent test** - where race conditions are possible

Example concurrent test pattern:
```python
async def test_concurrent_rate_limit():
    limiter = RateLimiter(...)
    limiter.check("key", cost=1.0)  # Initialize
    
    results = await asyncio.gather(*[
        limiter.check("key", cost=1.0)
        for _ in range(10)
    ])
    
    allowed = sum(1 for r in results if r.allowed)
    assert allowed == 1  # Only one should pass
```
