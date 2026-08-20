# Tusker-Gateway Capability Catalog (v0.1.0)

Tusker-Gateway is a specialized AI gateway optimized for high-reliability provider routing, token lifecycle management, and enterprise auth-compliant LLM access.

## Core Capabilities

### 1. Token Lifecycle Management
*   **OAuth Token Exchange**: Automates device-code (RFC 8628) enrollment for Copilot/Codex; automatically refreshes tokens via GHE-derived endpoints.
*   **Rotator Logic**: `CodexTokenRotator` manages a multi-credential pool. Automatically swaps to a fresh token upon 401/near-expiry.
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
