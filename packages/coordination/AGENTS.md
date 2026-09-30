# Coordination

- Own Redis clients/settings, serialization, atomic operations, locks, leases,
  reconnects and streams. Domain repositories and workflows stay with their owners.
- Stream tailing shares one blocked multi-key read per process. Bound subscriber
  queues and recover overflow/reconnects from Redis cursors without dropping or
  duplicating entries; expired cursors surface explicitly.
- Register subscribers against a barrier without yielding. Advance cursors only
  for delivered pages; interrupt blocked reads when a new stream subscribes.
