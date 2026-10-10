// Package cpu owns LazyCloud's CPU units. The platform counts CPUs, one
// physical core each: the SDK, the API, placement, billing and the host
// protocol all speak Millis. Hardware counts vCPUs, one hardware thread
// each. Only code that touches hardware holds a VCPUMillis, and Topology is
// the one conversion between them.
package cpu

import (
	"fmt"
	"math"
	"strconv"
)

// Millis is thousandths of a CPU, one physical core.
type Millis int64

// VCPUMillis is thousandths of a vCPU, one hardware thread.
type VCPUMillis int64

// Topology is a machine's hardware threads and the cores they run on. A
// machine whose cores run different numbers of threads converts at its
// average.
type Topology struct {
	Threads int
	Cores   int
}

// Validate refuses a topology no machine has.
func (t Topology) Validate() error {
	if t.Cores < 1 || t.Threads < t.Cores {
		return fmt.Errorf("%d threads on %d cores", t.Threads, t.Cores)
	}
	return nil
}

// CPUMillis is the machine's size in CPUs.
func (t Topology) CPUMillis() Millis { return Millis(t.Cores) * 1000 }

// VCPUs is c as hardware threads, rounded down.
func (t Topology) VCPUs(c Millis) VCPUMillis {
	return VCPUMillis(int64(c) * int64(t.Threads) / int64(t.Cores))
}

// CPUTime is time spent on hardware threads as time on cores.
func (t Topology) CPUTime(vcpuUsec uint64) uint64 {
	return vcpuUsec * uint64(t.Cores) / uint64(t.Threads) //nolint:gosec // Validate keeps both positive.
}

// ParseCores reads a positive decimal count of CPUs, such as "4" or "0.5".
func ParseCores(s string) (Millis, error) {
	cores, err := strconv.ParseFloat(s, 64)
	if err != nil || cores <= 0 || math.IsInf(cores, 0) {
		return 0, fmt.Errorf("%q is not a positive number of CPUs", s)
	}
	return Millis(math.Round(cores * 1000)), nil
}
