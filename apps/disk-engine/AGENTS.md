# Disk engine

`lazycloud-disk` runs one durable disk operation and exits. The worker calls it;
it never talks to the control plane. Everything it knows about a disk lives in
`<root>/<disk_id>/state.json`, beside the layer files and the daemon's sockets.

A disk is a qcow2 chain. Sealed layers below never change; the head on top takes
every write. One `qemu-storage-daemon` per attached disk exports the head over
NBD on a unix socket, `nbd-client` hands that socket to the kernel as
`/dev/nbdN`, and the device is mounted as ext4. The daemon outlives the command
that started it, so every command finds it again through the recorded pid and
QMP socket.

The control plane owns generations. A layer counts as published only after
`commit-published`, which the worker runs once the control plane has recorded
the generation. An upload the control plane never recorded stays pending, and
a retried publish of the same layer returns the same result. Attach reuses the
local chain only when its newest published generation and manifest digest
match the newest one in the chain it is given. Anything else is wiped and
restored, because another node may have published since.

Seal freezes the filesystem first, then decides. Checking before the freeze
misses dirty pages still in the page cache. Every thaw rewrites ext4's
superblock into the fresh head, so a head whose writes stop at block 0 counts
as unchanged. A head the engine did not create empty under the running daemon
is always sealed, because the daemon's statistics do not cover what it held
before.

Seal records the new head before switching the daemon to it. When the two
disagree, the next command drops the unswitched layer. Compact commits only
published layers into the base, and only while holding the disk lock, so
publish never reads a file that compaction is rewriting.

Chunk boundaries come from the gear table in `chunker.go`. Changing that table
or the size bounds does not break old disks, but every chunk becomes new, so a
publish after the change re-uploads whole disks.

Nothing here falls back. A missing binary, a missing `nbd` module, a busy device
or a mismatched manifest fails the command with the reason on stderr. Hosts load
`nbd` with `nbds_max=128` at boot, and the worker image ships the tools.
