package hostproto

// MounterMemoryBytes is the memory one volume mounter may use on a host. The
// server reserves it in a container's memory for each mounter the container
// runs; the agent caps each mounter at it.
const MounterMemoryBytes = 512 << 20
