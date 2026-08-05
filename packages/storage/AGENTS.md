# Storage Package

Protocol-backed object and cache storage, S3-compatible clients, mounted storage,
and the durable records that describe stored objects.

API and CLI formatting, scheduler and worker loops, providers, duplicate
persistence, and local JSON stores stay out.

This package handles user data, so its invariants are the ones whose failure
cannot be undone. Validate paths and keys against traversal, verify checksums,
track multipart state, scope credentials and presigned URLs narrowly and briefly,
keep concurrent writers from corrupting each other, preserve sibling objects on
every delete, and clean up what a failed operation left behind.
