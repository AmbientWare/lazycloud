package agent

import (
	"fmt"
	"slices"
	"strings"

	containertypes "github.com/moby/moby/api/types/container"
)

// labelGPUs lists a container's GPU UUIDs, so an agent that adopts it keeps
// those devices taken.
const labelGPUs = "lazycloud.gpus"

// allocateGPUs gives c n offered GPUs that no live container holds.
func (a *Agent) allocateGPUs(c *container, n int) ([]string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	taken := map[string]bool{}
	for _, other := range a.containers {
		if other == c || other.hasExited() {
			continue
		}
		for _, uuid := range other.gpus {
			taken[uuid] = true
		}
	}
	var free []string
	for _, gpu := range a.gpus {
		if !taken[gpu.UUID] {
			free = append(free, gpu.UUID)
		}
	}
	if n > len(free) {
		return nil, fmt.Errorf("the container needs %d GPUs and %d of the host's %d are free", n, len(free), len(a.gpus))
	}
	c.gpus = free[:n]
	return c.gpus, nil
}

// gpuRequest passes devices to the container through the NVIDIA runtime.
func gpuRequest(uuids []string) []containertypes.DeviceRequest {
	if len(uuids) == 0 {
		return nil
	}
	return []containertypes.DeviceRequest{{Driver: "nvidia", DeviceIDs: slices.Clone(uuids), Capabilities: [][]string{{"gpu"}}}}
}

func parseGPULabel(value string) []string {
	if value == "" {
		return nil
	}
	return strings.Split(value, ",")
}
