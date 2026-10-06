# Regional layer copies

## Scope

Hosts read layer frames from their own region. Today the platform bucket is
in us-east-1 while the fleet buys in us-east-2 first, so most frame fetches
cross regions (about 12 to 15 ms more each from us-east-2, 60 to 70 ms from
the west), where beta9 reads from its own locality.

- A dedicated `layers` bucket in us-east-1 (versioned, noncurrent versions
  expire after a day), replicated by S3 Cross-Region Replication with
  Replication Time Control to one bucket per fleet region.
- Grants presign against the host's own region once the copy there is
  confirmed: the server checks a replica once and records it in Postgres
  (migration 0007); until then it presigns the main bucket.
- Local development keeps one Garage bucket and no replicas.

## Evidence to record

- Owner tests for which bucket a grant names before and after a replica is
  confirmed.
- On EC2 in us-east-2 and us-west-1: frame fetch latency from the regional
  copy against us-east-1.

## Progress

## Gaps and unverified boundaries
