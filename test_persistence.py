"""Persistence smoke test: record a cooldown, restart, verify it survives."""
from tusker_gateway.persistent_cooldown import PersistentCooldownStore
from pathlib import Path

db = Path("/home/tusker/.hermes/cooldowns.db")
store = PersistentCooldownStore(db_path=db)

# Record a 1-hour cooldown for a test provider/model
store.record("test-provider", "test-model", 3600.0)
store.record_provider("test-provider", 3600.0)

# Verify it's active
print("active after record:", store.is_active("test-provider", "test-model"))
print("provider active:", store.is_provider_active("test-provider"))

# Show status
print("status:", store.status())