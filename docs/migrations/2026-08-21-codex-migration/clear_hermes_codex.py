"""Remove the openai-codex pool from hermes's auth.json (in-place rewrite).

Reads /home/tusker/.hermes/auth.json, clears credential_pool.openai-codex,
writes back atomically. Safe because we made a backup to ~/dev/hermes/tmp/.
"""
import json
import os
import shutil
import time
from pathlib import Path

AUTH = Path("/home/tusker/.hermes/auth.json")
BACKUP = Path("/home/tusker/.hermes/auth.json.bak.precodex-removal")

# Safety: refuse if auth.json doesn't look right
with AUTH.open() as f:
    d = json.load(f)
if "credential_pool" not in d:
    raise SystemExit("auth.json doesn't look like a Hermes doc — aborting")
before = len(d["credential_pool"].get("openai-codex", []))
print(f"openai-codex entries before: {before}")

# Backup (in-pod copy)
shutil.copy(AUTH, BACKUP)
print(f"backup written to {BACKUP} (in-pod)")

# Clear openai-codex
d["credential_pool"]["openai-codex"] = []
d["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())

# Atomic write
tmp = AUTH.with_suffix(".tmp")
with tmp.open("w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
os.chmod(tmp, 0o600)
tmp.replace(AUTH)
print(f"openai-codex entries after: {len(d['credential_pool']['openai-codex'])}")
print(f"updated_at: {d['updated_at']}")
print("done")
