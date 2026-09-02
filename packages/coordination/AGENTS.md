# Coordination package

Backend-neutral Redis clients and settings, key and serialization helpers, and
the hot state, events, locks, leases, and pub/sub built on them.

Domain Redis repositories and the workflow decisions that use them stay with
their owners. Apps, database, providers, and product services stay out.

The point of this package is that atomicity, lease semantics, serialization, and
reconnect behavior are decided once and correctly. Keep those decisions explicit
and keep them here, rather than letting each consumer reimplement a slightly
different version of the same guarantee.

## Stream tailing

`stream_tail.RedisStreamTailBroker` is how a process follows Redis Streams for
many subscribers at once. One task issues a multi-key `XREAD BLOCK` over every
stream with a local subscriber and fans entries out to bounded per-subscriber
queues, so a process holds one blocked Redis connection however many SSE
clients it serves. Redis Streams remain the record: a subscriber registers
against a per-stream barrier under the same lock that advances the cursor, then
reads anything older than the barrier straight from Redis, and a queue that
overflows is discarded and caught up from Redis the same way. Nothing is
delivered twice and nothing is dropped.

The decision this replaces is Pub/Sub notification with a reconciliation timer.
A reconciliation read already delivers every entry on its own; Pub/Sub would only
make it faster, at the cost of a producer-side publish, a second Redis
subscription, and a reconnect path of its own. A short block interval buys the
same latency for free. Consumer groups were rejected because they hand an entry
to one consumer, which starves the subscribers held by every other replica.

The cost to know about is that a stream joins the read only when the current
block returns, so a new subscriber's first live entry can lag by one block
interval. A Redis outage keeps the cursors and resumes from them; only a stream
trimmed past a cursor loses entries, and that surfaces to the client as an
expired cursor.
