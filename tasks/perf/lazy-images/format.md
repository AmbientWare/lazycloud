# Format and chunk store

## Scope

The shared library both the build path and the snapshotter use. Owns:

- new package `internal/imagefs`: layer index encoding and decoding, chunk
  naming (SHA-256 of the uncompressed bytes) and compression, and the object
  store client that reads and writes chunks and indexes by key;
- moving `internal/diskengine/chunker.go` into it, with the disk engine
  importing it from there (behavior unchanged, its tests move with it).

No database access (depguard already forbids it for host runtime packages;
extend the rule to `internal/imagefs`). Stay off the agent, images, compute
and contracts.

## Plan

Refined from the spike's report before work starts. It covers:

- an index format that streams (a container can mount before the whole index
  is read is not required; a single index fetch per layer is), versioned by
  a header so later changes are a hard cut, not a dual reader;
- converting an OCI layer tar stream into an index and chunk set in one pass
  with bounded memory, deduplicating chunks already in the store with one
  existence check per batch, not per chunk;
- reading a byte range of a file from its chunk list.

## Evidence to record

- Round trip of real layers (a Python base, a layer with hard links,
  symlinks, whiteouts and xattrs) through index and chunks, compared byte for
  byte with the original tar's files.
- Memory and time to index a 1 GiB layer.
- Disk engine tests still pass from the moved chunker.

## Progress

## Intentional differences

## Gaps and unverified boundaries
