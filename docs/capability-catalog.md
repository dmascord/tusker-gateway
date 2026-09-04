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
