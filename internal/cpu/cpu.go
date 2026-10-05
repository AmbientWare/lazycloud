// Package cpu owns LazyCloud's CPU units. The platform counts CPUs, one
// physical core each: the SDK, the API, placement, billing and the host
// protocol all speak Millis. Hardware counts vCPUs, one hardware thread
// each. Only code that touches hardware holds a VCPUMillis, and Topology is
// the one conversion between them.
package cpu

import "fmt"

// Millis is thousandths of a CPU, one physical core.
type Millis int64

// VCPUMillis is thousandths of a vCPU, one hardware thread.
type VCPUMillis int64

// Topology is how many hardware threads each core of a machine runs.
type Topology struct {
	ThreadsPerCore int
}

// Validate refuses a topology no machine has.
func (t Topology) Validate() error {
	if t.ThreadsPerCore < 1 {
		return fmt.Errorf("a core runs %d threads", t.ThreadsPerCore)
	}
	return nil
}

// VCPUs is c as hardware threads.
func (t Topology) VCPUs(c Millis) VCPUMillis {
	return VCPUMillis(int64(c) * int64(t.ThreadsPerCore))
}

// CPUs is v as cores, rounded down.
func (t Topology) CPUs(v VCPUMillis) Millis {
	return Millis(int64(v) / int64(t.ThreadsPerCore))
}

// CPUTime is time spent on hardware threads as time on cores.
func (t Topology) CPUTime(vcpuUsec uint64) uint64 {
	return vcpuUsec / uint64(t.ThreadsPerCore) //nolint:gosec // Validate keeps ThreadsPerCore positive.
}
