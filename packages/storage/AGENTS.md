# Storage Package

Protocol-backed object and cache storage, S3-compatible clients, mounted storage,
and the durable records that describe stored objects.

API and CLI formatting, scheduler and worker loops, providers, duplicate
persistence, and local JSON stores stay out.

Volume metering is billable, so a metering window is priced in the transaction
that records it rather than by a later pass. A record committed without its cost
is money this platform measured and can no longer charge for, and the usage row
alone cannot say whether the cost was skipped or never owed.

A sized block device with its own filesystem and a restore point is a primitive
this package does not have, and that is a decision rather than an oversight.
It is what anything stateful actually wants, and a shared volume mount is not
it. Under bring-your-own capacity the disk sits on the customer's own
infrastructure and is usually already provisioned, so the gap costs them
nothing they cannot solve where the hardware is. Reach for this note before
concluding that snapshots were simply forgotten.

This package handles user data, so its invariants are the ones whose failure
cannot be undone. Validate paths and keys against traversal, verify checksums,
track multipart state, scope credentials and presigned URLs narrowly and briefly,
keep concurrent writers from corrupting each other, preserve sibling objects on
every delete, and clean up what a failed operation left behind.
