package hostproto

import (
	"fmt"
	"os"
	"strings"
)

// MounterMemoryBytes is the memory one volume mounter may use on a host. The
// server reserves it in a container's memory for each mounter the container
// runs; the agent caps each mounter at it.
const MounterMemoryBytes = 768 << 20

// MaxVolumeReads bounds the objects one volume trace records: a loader
// reads a model's weights from a few dozen files. The server stores traces
// under the same bound.
const MaxVolumeReads = 256

// Mount is one line of /proc/self/mountinfo.
type Mount struct {
	// Device is the major:minor of the mounted filesystem.
	Device string
	Path   string
	// Options are the per-mount options, such as ro and nosuid.
	Options []string
}

// mountPath undoes the octal escapes the kernel writes in a mount point's
// path.
var mountPath = strings.NewReplacer(`\040`, " ", `\011`, "\t", `\012`, "\n", `\134`, `\`) //nolint:gochecknoglobals // A constant table.

// ReadMounts lists this mount namespace's mounts in mount order. It reads
// /proc rather than asking a filesystem: a stat of a FUSE root waits for
// its server, for good once the server stopped serving.
func ReadMounts() ([]Mount, error) {
	data, err := os.ReadFile("/proc/self/mountinfo")
	if err != nil {
		return nil, fmt.Errorf("read mounts: %w", err)
	}
	var mounts []Mount
	for line := range strings.Lines(string(data)) {
		fields := strings.Fields(line)
		if len(fields) < 6 {
			return nil, fmt.Errorf("invalid mountinfo record %q", line)
		}
		mounts = append(mounts, Mount{Device: fields[2], Path: mountPath.Replace(fields[4]), Options: strings.Split(fields[5], ",")})
	}
	return mounts, nil
}
