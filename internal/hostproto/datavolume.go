package hostproto

// A host's data volume holds its snapshotter's frame cache and its disks'
// writes not yet published. The fleet sizes the volume for a frame cache of
// FrameCacheBytes and DiskDirtyBytes per disk the host holds; a host offers
// as many disk slots as its volume has room for.
const (
	DataRoot        = "/var/lib/lazycloud-data"
	FrameCacheBytes = 32 << 30
	// DiskDirtyBytes is the room a disk keeps for writes not yet published.
	// A disk publishes once half of it is used.
	DiskDirtyBytes = 8 << 30
)
