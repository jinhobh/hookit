"""Shared constant for the reserved event-type prefix."""

# Event types starting with this prefix are reserved for internal use.
# Schemas use this to prevent real clients from publishing or subscribing
# to event types in this namespace.
RESERVED_EVENT_TYPE_PREFIX = "__"
