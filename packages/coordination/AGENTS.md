# Coordination Package

Own backend-neutral Redis clients/settings, key and serialization helpers,
hot-state primitives, events, locks, and pub/sub. Domain Redis repositories and
workflow decisions stay with their owners; no apps, database, providers, or
product services. Accept changed atomicity, lease, serialization, reconnect, or
concurrency behavior against real Redis when it is not already proven by the
consumer.
