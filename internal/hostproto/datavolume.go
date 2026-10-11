package hostproto

// A host's data volume holds its snapshotter's frame cache and its disks'
// writes not yet published. The fleet sizes the volume for a frame cache of
// FrameCacheBytes and DiskDirtyBytes per disk the host holds; a host offers
// as many disk slots as its volume has room for.
const (
	DataRoot = "/var/lib/lazycloud-data"
	// FrameCacheBytes holds image layers' frames and a dozen disks' working
	// sets, which a devbox-like session measured at about 0.8 GiB.
	FrameCacheBytes = 32 << 30
	// DiskDirtyBytes is the room a disk keeps for writes not yet published,
	// counted in the whole frames a publish stores. A disk publishes once
	// half of it is used. Publishing runs at about 335 MiB/s on an 8-vCPU
	// host, faster than a gp3 data volume's 125 MiB/s baseline writes, so a
	// disk stays within it.
	DiskDirtyBytes = 4 << 30
	// DiskBlockBytes divides every disk's size, and is the block size of
	// its filesystem; MaxDiskBytes bounds a disk.
	DiskBlockBytes = 4096
	MaxDiskBytes   = 1 << 40
)

// DiskSlots is how many disks a data volume of volumeBytes holds room for
// beside the frame cache, rounded, since the filesystem keeps a little of
// the volume for itself.
func DiskSlots(volumeBytes int64) int64 {
	return max(0, (volumeBytes-FrameCacheBytes+DiskDirtyBytes/2)/DiskDirtyBytes)
}
