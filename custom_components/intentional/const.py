"""Constants for the Intentional integration."""

DOMAIN = "intentional"
MANUFACTURER = "Intentional"
DEFAULT_NAME = "Intentional"
DEFAULT_RULE_DIR = "/config/intentional/rules"

# Config entry keys
CONF_RULE_DIR = "rule_dir"

# Service names
SERVICE_FIRE = "fire"
SERVICE_RELOAD = "reload"

# Attribute keys on entities
ATTR_ACTIVE_INTENTS = "active_intents"
ATTR_RULE_ID = "rule_id"
ATTR_TARGET = "target"
ATTR_REASON = "reason"
ATTR_AUTHORITY = "authority"
ATTR_TTL_REMAINING = "ttl_remaining_ms"
ATTR_TICK_INTERVAL_MS = "tick_interval_ms"

# Storage key for persisting engine state across restarts
STORAGE_KEY = "intentional_state_v1"
STORAGE_VERSION = 1
