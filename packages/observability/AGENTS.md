# Observability

- Own events, metering, telemetry and streams. Apply published rates; do not
  maintain another rate card or derive reservation floors from current capacity.
- Bill each resource window at max(reserved, measured), independent of arrival
  order. Missing measurements retain the reservation floor.
- Price in the transaction that commits metering. Missing rates still commit usage
  and a durable unpriced error, never fabricated zero cost. Explicit zero is valid.
- Keep priced segments immutable; corrections use new records. Replays cannot
  reprice usage or debit historical provider exports against the wallet.
- Preserve workspace attribution, ordering and cursors. Use repositories for SQL,
  coordination for hot streams and lazy initialization for optional exporters.
- Broad error handlers on capacity, enrollment or billing paths must re-raise or
  record durable errors. Recording failures must not replace the original error.
