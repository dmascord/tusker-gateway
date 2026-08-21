from tusker_gateway.config import load_config
c = load_config()
print("=== CURRENT POOLS ===")
for n, p in c.get("pools", {}).items():
    print("--- pool:", n, "---")
    for m in p.models:
        print("  -", m.get("provider"), "/", m.get("model"))
