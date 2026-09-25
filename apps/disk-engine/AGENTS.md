# Disk engine

`lazycloud-disk` runs one durable disk operation and exits. The worker calls it;
it never talks to the control plane. Everything it knows about a disk lives in
`<root>/<disk_id>/state.json`, beside the layer files and the daemon's sockets.

A joined machine passes one root for every disk. A provider machine passes each
disk's own volume as its root, so nothing under a root can be assumed to be the
host's whole view. That is why the lock around picking an NBD device sits at a
fixed path under `/run` rather than under the root.

A disk is a qcow2 chain. Sealed layers below never change; the head on top takes
every write. One `qemu-storage-daemon` per attached disk exports the head over
NBD on a unix socket, `nbd-client` hands that socket to the kernel as
`/dev/nbdN`, and the device is mounted as ext4. The daemon outlives the command
that started it, so every command finds it again through the recorded pid and
QMP socket.

The control plane owns generations. A layer counts as published only after
`commit-published`, which the worker runs once the control plane has recorded
the generation. An upload the control plane never recorded stays pending, and
a retried publish of the same layer returns the same result. The worker saves
each generation after committing it, so a worker that restarts asks `published`
where the chain stands rather than trusting its own record. Attach reuses the
local chain only when its newest published generation and manifest digest
match the newest one in the chain it is given. Anything else is wiped and
restored, because another node may have published since.

Seal freezes the filesystem first, then decides. Checking before the freeze
misses dirty pages still in the page cache. Every thaw rewrites ext4's
superblock into the fresh head, so a head whose writes stop at block 0 counts
as unchanged. A head the engine did not create empty under the running daemon
is always sealed, because the daemon's statistics do not cover what it held
before.

Compaction deletes the committed layers' nodes from the daemon as well as their
files. The monitor keeps the daemon's startup node and every head a seal added
open after a commit drops them from the chain, so deleting only the files would
free nothing until detach. `usage` reports the bytes above the base without
taking the disk lock, so the worker can watch a volume while a publish runs.

Seal records the new head before switching the daemon to it. When the two
disagree, the next command drops the unswitched layer. Compact commits only
published layers into the base, and only while holding the disk lock, so
publish never reads a file that compaction is rewriting.

A disk only grows. Attach with a larger `--size` resizes the head before the
daemon opens it and runs `resize2fs` once the filesystem is mounted; the layers
below keep their size, and reads past a smaller layer's end return zeroes. A
saved flag carries the resize across an attach that stops between the two
steps. Each published layer records its own size, so a chain can mix sizes as
long as none shrinks.

`publish --flatten` uploads the disk's raw contents, read range by range from
the layer files `qemu-img map` names. Writing a flattened copy first would
need a second full disk of space. Restoring one produces a sparse raw base,
which the chain above it backs onto like any other layer.

Recover seals a head that a dead daemon left holding data, so the next publish
uploads those writes instead of leaving them on one node. They are
crash-consistent, which is all a killed daemon allows.

Collect deletes only what a parentless generation makes unreachable. Chunks
named by an upload still waiting for its commit survive, because a retried
publish returns that upload's result without storing its chunks again. State
keeps a record of each committed generation for this decision.

Attach exits with code 3 when a restore would leave the root filesystem below
`--min-free-bytes`. On host storage that is the worker's cue to evict and
retry; a volume has nothing to evict. Attach plans the restore before removing
anything, committing the layers it holds into the base whenever the next
download would not fit, and counting how much each commit grows the base. It
refuses only when the plan runs out of room, so a restore never stops halfway
for space. Space is counted from the chunks a restore writes, not from layer
sizes, which include holes. A missing or unknown command exits 2 and every
other failure exits 1.

Chunk boundaries come from the gear table in `chunker.go`. Changing that table
or the size bounds does not break old disks, but every chunk becomes new, so a
publish after the change re-uploads whole disks.

A workspace storage grant can expire long before a restore, flatten or collect
finishes. The engine reads `STORE.json` again once its credentials are within
two minutes of their `expires_at`, and the worker replaces the file whole at
half each grant's remaining life for as long as the call runs. A file that still
holds expired credentials fails the call and names the expiry. Signing with them
anyway would only fail later, partway through an upload.

A restore does not download the chain first. Attach lays out each published
layer as a sparse file of its manifest's size, with the manifest beside it and
a bitmap of one bit per chunk, and marks it lazy. It fetches only what opening
a qcow2 layer reads (header cluster, L1 and refcount tables), because the
rebase opens every layer. `serve`, one process per disk started before the
daemon, exports the lazy layers read-only over NBD on `run/lazy.sock`, and the
daemon opens them through qemu's own nbd driver. The chain, the qcow2 driver
and the head stay qemu's. qemu's copy-on-read filter would still need something
that fetches a content-addressed chunk, and FUSE is a kernel dependency the
hosts do not have.

A read fetches each missing chunk it touches, once however many readers wait,
checks its length and sha256 against the manifest, writes it into the layer and
sets its bit. Unverified bytes are never written. The bitmap reaches the disk
only after the layer file is flushed, so a crash forgets chunks, which are
fetched again, and never names bytes that were not written. A chunk that still
cannot be fetched fails the read with EIO; one with no room to land fails it
too. `serve` records both in `run/serve.json`, which carries its pid so a new
attachment never reads an old record, and `usage` reports them for the worker
to stop the container as unreadable or full. Credentials come from a STORE.json
the worker keeps renewed while `serve` runs. Each GET has its own timeout, and a
stop cancels every fetch, so `serve` exits well inside the wait `stopServer`
gives it before killing it; an unflushed bit only costs a refetch.

`serve` hydrates in the background: first the chunks the heat map names, most
recently used first, then the rest, newest layer first, at a bounded rate.
Workload reads never queue behind it. Once every lazy layer is complete, the
next attach opens them as plain files and drops their bitmaps and manifests.

The heat map names chunks by the first bytes of their sha256, so it still
applies to whatever chain the next restore gets; a flatten re-chunks the disk
and starts it over. `serve` marks the chunks reads touch, ages the map every
publish interval, keeps at most 65536 entries and uploads it to
`disks/<id>/heat`. It only orders prefetching, so a map that cannot be fetched
or decoded is dropped, and deleted where it lies, rather than failing a start.

Compaction never writes a lazy layer: it commits into the lowest local layer
above them. Flattening reads layer files directly, so it waits until the lazy
layers are complete and the publish meanwhile is an ordinary layer. Collect
follows only a flatten, so it never deletes a chunk a lazy layer still needs.

A lazy restore needs room for its layers and for what every other lazy disk
under the same root has yet to fetch; their manifests and bitmaps are that
reservation. A chain that does not fit restores eagerly, committing layers into
the base as it goes. Recover and evict stop a `serve` an interrupted attach left
running before they touch the disk's files.

Nothing here falls back. A missing binary, a missing `nbd` module, a busy device
or a mismatched manifest fails the command with the reason on stderr. Hosts load
`nbd` with `nbds_max=128` at boot, and the worker image ships the tools.
