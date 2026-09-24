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

A disk on a provider volume can be snapshotted whole. `snapshot-point` writes
`snapshot.json`, the published chain as a restore should find it, and syncs the
volume's filesystem; the control plane snapshots the volume after that. The
snapshot is crash-consistent and needs nothing stronger. Published layers never
change after their commit, and the head, the only file still being written, is
dropped on restore. Nothing is frozen, so a snapshot never pauses the workload.

A volume created from a snapshot holds the source's whole state, its attachment
and head included. Attach is told which volume it runs on. When `snapshot.json`
names a different volume and the state has not adopted that point, attach
replaces the state with the recorded chain, deletes every other file and starts
a fresh head. The source's attachment names devices on another machine, so
none of it is torn down. When the chain attach is given has moved past the
adopted generation, it downloads only the generations after it, and only while
the local chain holds nothing unpublished.

`heat` records which 1 MiB regions of the base layer were read or written since
the last sample: it ages every region by one window, marks those in the page
cache, then drops the file's cached pages so the next sample sees only the next
window. A region is one byte of age, and regions double in size past 131072 of
them, so the map stays under 128 KiB for any disk. It lives gzipped beside the
state, so each snapshot carries the map as of its generation. Flattening and
restoring drop the pages they read; otherwise the next sample would call every
region hot. After adopting a snapshot, attach starts `hydrate` in the
background: qcow2 metadata first, the hot regions most recent first, then the
rest at a bounded rate. It reads with O_DIRECT, so hydration neither fills the
page cache nor counts as use, and teardown stops it before the volume unmounts.

Nothing here falls back. A missing binary, a missing `nbd` module, a busy device
or a mismatched manifest fails the command with the reason on stderr. Hosts load
`nbd` with `nbds_max=128` at boot, and the worker image ships the tools.
