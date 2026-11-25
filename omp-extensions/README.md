# OMP extensions

Runtime extensions for [oh-my-pi](https://github.com/can1357/oh-my-pi) (`omp`)
that surface gateway state inside the agent.

These are **client-side configuration**, not gateway code. They are kept here
so a reinstall or a new machine can restore them, and so changes are reviewable.

## Install

```bash
cp omp-extensions/gateway-health.ts ~/.omp/agent/extensions/
```

Then reference it from `~/.omp/agent/config.yml` (plain paths, not objects):

```yaml
extensions:
  - ~/.omp/agent/extensions/gateway-health.ts
```

Files in `~/.omp/agent/extensions/` are auto-discovered, so the `extensions:`
entry is only needed if the file lives elsewhere.

## gateway-health.ts

Registers a `UsageProvider` for the `hermes-gateway` provider so that
`omp usage` shows live pool health from `GET /v1/provider-health`.

See [`docs/provider-health.md`](../docs/provider-health.md) for the endpoint
and the usage-row contract.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TUSKER_GATEWAY_URL` | `https://hermes.tusker.net.au/v1` | Gateway base URL. |
| `TUSKER_GATEWAY_KEY` | *(none)* | Gateway API key. Read on every call, so exporting it after `omp` starts still works. Falls back to the stored provider credential. |

Without a key the extension makes no request and `omp usage` shows no data —
it never sends an empty or placeholder key upstream.

### Reloading

Editing the file takes effect in the **next** `omp` process. A running session
does not re-evaluate extension modules (upstream behaviour: "modules are never
unloaded or re-evaluated"). Changes to `extensions:` in `config.yml` do
re-scope live, but the already-imported module body is not re-run.
