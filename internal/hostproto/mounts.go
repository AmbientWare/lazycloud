package hostproto

// MounterMemoryBytes is the memory one volume mounter may use on a host. The
// server reserves it in a container's memory for each mounter the container
// runs; the agent caps each mounter at it.
const MounterMemoryBytes = 768 << 20

// MaxVolumeReads bounds the objects one volume trace records: a loader
// reads a model's weights from a few dozen files. The server stores traces
// under the same bound.
const MaxVolumeReads = 256
