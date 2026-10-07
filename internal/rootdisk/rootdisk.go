// Package rootdisk sizes a devbox's root disk, which holds the image's
// unpacked root once the first start copies it there. The deploy check and
// the supervisor's seed both suggest sizes by it, and the supervisor may not
// import the control plane, so the rule lives here.
package rootdisk

import (
	"fmt"
	"strings"
)

const (
	gib = int64(1) << 30
	// MinBytes is the smallest root disk a devbox takes. Disks bill for what
	// they store, so the floor costs nothing.
	MinBytes = 10 * gib
	// headroomBytes is the room the root disk keeps beyond the image.
	headroomBytes = 2 * gib
)

// Needed is the root disk size to suggest for an image whose unpacked root
// is imageBytes: the image rounded up to whole GiB plus headroom, and at
// least MinBytes.
func Needed(imageBytes int64) int64 {
	return max((imageBytes+gib-1)/gib*gib+headroomBytes, MinBytes)
}

// GiB is bytes in GiB to one decimal place, without a trailing ".0".
func GiB(bytes int64) string {
	return strings.TrimSuffix(fmt.Sprintf("%.1f", float64(bytes)/float64(gib)), ".0")
}
