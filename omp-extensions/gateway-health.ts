/**
 * Render the Tusker gateway's pool health inside `omp usage`.
 *
 * The gateway is a router in front of many providers and pools several
 * credentials per provider. It is the only component that knows which pools
 * are cooling down, so this extension asks it directly rather than trying to
 * derive anything from a chat response.
 *
 * Data source: GET <baseUrl>/provider-health on the gateway, authenticated
 * with the provider's own API key. Nothing is sent upstream and no vendor is
 * impersonated.
 *
 * Each provider becomes two rows:
 *   - Availability: how many of the pool's routes are cooling down.
 *   - Requests today: success rate over today's traffic.
 *
 * `usedPercent` means "share of this pool currently unavailable", not an
 * upstream vendor quota. The gateway pools several seats, and one seat's
 * vendor quota would misrepresent the pool.
 */
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

const PROVIDER = "hermes-gateway";

type ProviderRow = {
  provider: string;
  ready: boolean;
  cooling_routes: number;
  next_reset_seconds: number;
  requests_today: number;
  failures_today: number;
  success_rate_today: number | null;
};

type HealthPayload = {
  generated_at: number;
  summary: { providers: number; ready: number; degraded: number };
  providers: ProviderRow[];
};

/** Strip a trailing /v1 so the health path is not doubled. */
function healthUrl(baseUrl: string): string {
  const trimmed = baseUrl.trim().replace(/\/+$/, "");
  const root = trimmed.endsWith("/v1") ? trimmed.slice(0, -3) : trimmed;
  return `${root}/v1/provider-health`;
}

export default function (pi: ExtensionAPI) {
  
  pi.registerProvider(PROVIDER, {
    baseUrl: process.env.TUSKER_GATEWAY_URL ?? "https://hermes.tusker.net.au/v1",
    // A literal is required when declaring models. The real key is read
    // inside fetchUsage so that exporting TUSKER_GATEWAY_KEY after the
    // session started still works.
    apiKey: process.env.TUSKER_GATEWAY_KEY ?? "unset-at-load",
    api: "openai-completions",
    models: [{ id: "hermes-code", name: "Hermes Gateway" }],
    usage: {
      id: PROVIDER,
      // Distinct from any built-in resolver so a process without this
      // extension never serves our cached rows, or we theirs.
      cacheVersion: "tusker-provider-health-v1",

      async fetchUsage(params: any, ctx: any) {

        const envUrl = process.env.TUSKER_GATEWAY_URL;
        const baseUrl: string | undefined =
          envUrl ?? params?.baseUrl ?? "https://hermes.tusker.net.au/v1";
        const key: string | undefined =
          process.env.TUSKER_GATEWAY_KEY ||
          (params?.credential?.apiKey &&
          params.credential.apiKey !== "unset-at-load"
            ? params.credential.apiKey
            : undefined);
        if (!baseUrl || !key) return null;

        const url = healthUrl(baseUrl);
        let payload: HealthPayload;
        try {
          const res = await ctx.fetch(url, {
            headers: { Authorization: `Bearer ${key}`, Accept: "application/json" },
            signal: params?.signal,
          });
          if (!res.ok) return null;
          payload = await res.json();
        } catch {
          return null;
        }
        if (!payload || !Array.isArray(payload.providers)) return null;

        const ready = payload.summary?.ready ?? 0;
        const total = payload.summary?.providers ?? payload.providers.length;

        // A bar fills as things go wrong, matching how a quota bar fills as
        // budget is spent: full bar = fully degraded. `unit` is only used for
        // the numeric suffix, so omit it rather than invent a value OMP
        // cannot render.
        const degradedCount = total - ready;
        const limits: any[] = [
          {
            id: "pool-availability",
            label: "Providers degraded",
            scope: { provider: PROVIDER, shared: true },
            amount: {
              used: degradedCount,
              limit: total,
              remaining: ready,
              unit: "unknown",
              usedFraction: total > 0 ? degradedCount / total : 0,
            },
          },
        ];

        // Surface the least healthy provider so the bar points at the problem
        // rather than averaging it away.
        const degraded = payload.providers
          .filter((p) => !p.ready)
          .sort((a, b) => b.cooling_routes - a.cooling_routes);
        for (const row of degraded.slice(0, 3)) {
          const cooling = row.cooling_routes;
          limits.push({
            id: "cooling:" + row.provider,
            label: row.provider + " cooling",
            scope: { provider: PROVIDER, shared: true },
            amount: {
              used: cooling,
              limit: Math.max(cooling, 1),
              remaining: 0,
              unit: "unknown",
              usedFraction: 1,
            },
            ...(row.next_reset_seconds > 0
              ? {
                  window: {
                    id: "cooldown:" + row.provider,
                    label: "cooldown",
                    resetsAt: Date.now() + row.next_reset_seconds * 1000,
                  },
                }
              : {}),
          });
        }

        return {
          provider: PROVIDER,
          fetchedAt: Date.now(),
          limits,
        };
      },
    },
  });
}
