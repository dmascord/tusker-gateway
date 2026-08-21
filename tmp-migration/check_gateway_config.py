from tusker_gateway.config import load_config
c = load_config()
print("api_keys:", len(c.get("api_keys", [])))
print("codex_credentials:", len(c.get("codex_credentials", [])))
for cred in c.get("codex_credentials", []):
    label = cred.get("label")
    provider = cred.get("provider")
    expires = cred.get("expires_at_ms")
    print("  - label=%r provider=%s expires_at_ms=%s" % (label, provider, expires))
print("providers:", list(c.get("providers", {}).keys()))
print("pool keys:", list(c.get("pools", {}).keys()))
for n, p in c.get("pools", {}).items():
    has_codex = any(m["provider"] == "openai-codex" for m in p.models)
    print("  %s: %d models, has openai-codex? %s" % (n, len(p.models), has_codex))
