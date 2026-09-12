# Pinned image filesystem dependency

Source: `github.com/beam-cloud/clip`, version
`v0.0.0-20260802023200-1316f48c56cc`, commit
`1316f48c56cc`. The upstream LICENSE applies. This directory contains the three
production packages used by the image runtime and their module files.

LazyCloud carries these changes in the dependency's existing index and FUSE
owners:

- Preserve named pipes, all permission bits, and sorted binary extended
  attributes in OCI layer indexes, including attributes on hardlinks.
- Serve extended attributes through FUSE, including Linux file capabilities.
- Write archive header version 2. Older readers reject these archives. The new
  reader also accepts version 1 so existing immutable images remain readable.
- Use layer index cache version 2 so cached indexes are regenerated with the
  complete metadata.

Image build identity contract version 3 prevents new builds from reusing a
completed image whose index omitted this metadata. Existing image IDs retain
their published bytes. Workers must receive the updated image runtime before
they consume newly built images; older workers reject the new archive header.

Both local builds and the worker Docker build use this module through the
relative replacement in `apps/image-runtime/go.mod`.

Sandbox snapshot export rejects FIFOs with their path because the current
sandbox isolation policy forbids opening restored host FIFOs. It also rejects
other unsupported special files. The OCI index preserves FIFO metadata without
changing that runtime policy.
