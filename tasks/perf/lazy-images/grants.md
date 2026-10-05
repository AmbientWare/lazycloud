# Grants

## Scope

A host reads only the chunks of images it was told to run. Owns:

- the server side that issues scoped reads for an image's indexes and chunks
  with the start command (signed URLs or an equivalent scoped grant; choose
  from the publish and snapshotter packets' shapes and record why);
- `contracts/host/v1/host.proto` fields 100-109 for it;
- the snapshotter's use of those reads, replacing whatever scratch access the
  earlier packets used locally.

Stay off the format internals and the build path.

## Evidence to record

- A host given one workspace's image cannot read another image's chunks or
  index with what it was sent.
- Reads keep working past the grant's first expiry for a long-running
  container.
- Connected-account and self-hosted hosts follow the same path.

## Progress

## Intentional differences

## Gaps and unverified boundaries
