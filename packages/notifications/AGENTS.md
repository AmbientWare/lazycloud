# Notifications

- Queue rendered messages in the caller's transaction with the change they announce.
  Requests never wait for the provider; drains send the stored message unchanged.
- Claim with fencing and bounded attempts, spending an attempt at claim. Recover
  expired claims; rely on provider deduplication for at-least-once delivery.
- Retain delivery/abandonment evidence; redact sent message bodies containing secrets.
- Provider acceptance and delivery outcome are distinct. Record provider message
  IDs and order reports by event time so delayed events cannot erase newer outcomes.
