# Tusker-Gateway Capability Catalog (v0.1.0)

Tusker-Gateway is a specialized AI gateway optimized for high-reliability provider routing, token lifecycle management, and enterprise auth-compliant LLM access.

## Core Capabilities

### 1. Token Lifecycle Management
*   **OAuth Token Exchange**: Automates device-code (RFC 8628) enrollment for Copilot/Codex; automatically refreshes tokens via GHE-derived endpoints.
*   **Rotator Logic**: `CodexTokenRotator` manages a shared multi-credential pool, selects credentials round-robin, logs the redacted credential slot, and refreshes near-expiry tokens. Failed requests continue with the next slot.
*   **Hermes-Compatible Persistence**: Supports native Hermes `auth.json` format (`credential_pool` dict) for full interop with live deployments.

### 2. Provider Routing
*   **Role-Based Aliasing**: Pools (e.g., `hermes-code`, `hermes-privacy`) map virtual model names to sets of (provider, model) backends.
*   **Stickiness**: Implements session-based routing sticky sessions (1-hour TTL) for consistent multi-turn interaction.
*   **Quality-Aware**: Integration with a persistent SQLite `QualityDB` to route traffic to models based on recent success rates and latency.

### 3. Reliability & Cooldowns
*   **Persistent Cooldowns**: Tracks `429` rate-limit windows in a SQLite database, ensuring cooldowns survive pod restarts.
*   **Credential Health Probe**: `/ready` endpoint checks credential availability, ensuring the gateway doesn't claim readiness without valid auth.
*   **Fallback Logic**: Sophisticated `PassthroughClient` with provider-specific auth strategies (Bearer/OAuth) and header injection for vision model markers.

### 4. Enterprise Hardening
*   **ZDR Compliance**: Automatically excludes "heavyweight" models from privacy-compliant (ZDR) pools.
*   **Multi-Auth Strategies**: Decoupled auth modules for extensible provider support.
*   **Tenant Policy**: Fingerprint-keyed identities enforce API scopes and pool/model/provider allowlists.
*   **Audit Integrity**: Optional append-only request events use SHA-256 or HMAC-SHA-256 chaining.
*   **Retry Safety**: Bounded request deadlines and persistent idempotency prevent runaway and duplicate non-streaming work.
*   **Release Safety**: CI tests supported Python versions, scans dependencies, and blocks undefined-name defects.
* **Caller-Scoped Approvals**: Native approval records are bound to the authenticated caller fingerprint, preventing cross-tenant approval replay.
* **Trusted Client-IP Attribution**: `CF-Connecting-IP` and `X-Forwarded-For` are accepted only from peers in `TUSKER_TRUSTED_PROXY_RANGES`.
* **Guardrail Preflight**: Output-token clamping, email and Luhn-valid card redaction, anchored prompt-injection detection, and harness delimiter protection run before provider dispatch when enabled.
* **Concurrent-State Safety**: Rate-limit consumption and half-open circuit probes use atomic reservations; trace span stacks use `ContextVar` isolation; permanent failure markers persist across restarts.

### 5. Stream Diagnostics & Recovery
*   **Rejected-Turn Capture**: When enabled with `TUSKER_STREAM_CAPTURE`, persists one bounded rejected-provider record in the shared state database (`turn_captures`) with a seven-day TTL. Byte-complete request bodies can be queried through `/admin/captures` or replayed with `python3 -m tusker_gateway.tools.replay_capture`; privacy and unknown pools retain verdict metadata but no bodies. Reasoning-cycle diagnostics remain in the bounded private directory store. See `docs/turn-capture-replay.md` for the environment knobs, privacy policy, and exit codes.
*   **Pre-Visible Tool-Turn Failover**: Unusable tool turns (`MalformedToolCallError`, `UnusableToolResponseError`, and related tool-contract failures) are eligible for one alternative provider/model before visible assistant output reaches the client. The failed route is quarantined for `TUSKER_TOOL_FAILURE_COOLDOWN_SECS` (default 300s); request-dependent `output_budget_exhausted` failures are not quarantined. Once visible output has reached the client, the gateway does not re-route the stream.

### 6. Audio (OpenAI-compatible, bidirectional)
*   **Speech output — `POST /v1/audio/speech`**: OpenAI-shaped TTS request, binary `audio/*` response. Provider dispatch via `TTSHandler` (OpenAI, OpenRouter, Groq, Xiaomi, Workers AI), explicit `provider::model` pins, and capability-registry lookup.
*   **Speech input — `POST /v1/audio/transcriptions`**: OpenAI-shaped multipart upload (`file`, `model`, optional `language`/`prompt`/`response_format`/`temperature`), returning upstream JSON (`{"text": ...}`). `STTHandler` dispatches to OpenAI / Groq / OpenRouter transcription endpoints; Whisper models are discovered from the Groq catalog.
*   **Audio in chat**: `input_audio` content parts are detected as an `audio` modality requirement for pool selection, counted in prompt-token estimates (`TUSKER_AUDIO_TOKEN_ESTIMATE`, default 1000/part), and forwarded to the upstream unchanged. Audio-only models (e.g. Lyria) are excluded from chat pools by the catalog output-modality filter.
*   All three surfaces share the media preflight, budget accounting, capability recording, and `inference:audio` auth scope. See `docs/audio-routing.md`.
