package compute

import (
	"context"
	"fmt"
	"slices"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

// GPUAny in a preference accepts any model.
const GPUAny = "any"

// Requirement is what a container needs from its host.
type Requirement struct {
	Workspace uuid.UUID
	// Connection is the connected account the workspace lives in; nil on
	// platform compute.
	Connection *uuid.UUID
	// Machine pins the container to the joined machine of that name that
	// serves Workspace.
	Machine string
	// Region is a product region such as us-east; empty takes any.
	Region string
	// Zone is an availability zone name or id; empty takes any.
	Zone string
	// Preemptible allows Spot capacity.
	Preemptible bool
	// GPUs are accepted models in preference order; GPUAny takes any.
	GPUs        []string
	GPUCount    int
	CPUMillis   cpu.Millis
	MemoryBytes int64
	// Disks is how many durable disks the container attaches, each taking
	// one of the host's disk slots.
	Disks int
	// Prefer is the host whose frame cache likely holds one of the disks;
	// nil for none.
	Prefer *HostID
}

// GPUsNeeded is how many GPUs the container reserves: a model list without a
// count reserves one.
func (r Requirement) GPUsNeeded() int {
	if r.GPUCount == 0 && len(r.GPUs) > 0 {
		return 1
	}
	return r.GPUCount
}

// HostCapacity is a host's offer and its unreserved resources. Every
// container that is not stopped reserves its resources on its host.
type HostCapacity struct {
	Host     HostID
	Kind     HostKind
	Provider Provider
	// Connection is set for connection hosts.
	Connection *uuid.UUID
	// Name and Workspaces describe a joined machine.
	Name       string
	Workspaces []uuid.UUID
	Region     string
	Zone       string
	ZoneID     string
	Market     Market
	GPUType    string
	GPUCount   int

	CPUMillis       cpu.Millis
	MemoryBytes     int64
	FreeCPUMillis   cpu.Millis
	FreeMemoryBytes int64
	FreeGPUs        int
	FreeDiskSlots   int
}

// Accepts reports whether the host may run a container with r, ignoring
// free resources.
func (h HostCapacity) Accepts(r Requirement) bool {
	switch {
	case r.Machine != "":
		if h.Kind != KindMachine || h.Name != r.Machine || !slices.Contains(h.Workspaces, r.Workspace) {
			return false
		}
	case r.Connection != nil:
		if h.Kind != KindConnection || h.Connection == nil || *h.Connection != *r.Connection {
			return false
		}
	default:
		if h.Kind != KindPlatform {
			return false
		}
	}
	if r.Region != "" && ProductRegion(h.Region) != r.Region {
		return false
	}
	if r.Zone != "" && h.Zone != r.Zone && h.ZoneID != r.Zone {
		return false
	}
	if !r.Preemptible && h.Market == MarketSpot {
		return false
	}
	if n := r.GPUsNeeded(); n > 0 {
		return h.GPUCount >= n && GPUAccepted(r.GPUs, h.GPUType)
	}
	// Fleet GPU instances are bought for GPU work; joined machines and
	// operator hosts run whatever is pinned or sent to them.
	return h.GPUCount == 0 || h.Provider != ProviderAWS
}

// Fits reports whether the host accepts r and has room for it now.
func (h HostCapacity) Fits(r Requirement) bool {
	return h.Accepts(r) && h.FreeCPUMillis >= r.CPUMillis && h.FreeMemoryBytes >= r.MemoryBytes &&
		h.FreeGPUs >= r.GPUsNeeded() && h.FreeDiskSlots >= r.Disks
}

// floorHost reports an on-demand platform CPU host, which keeps the
// on-demand warm floor; keepsFloor one with floor free.
func (h HostCapacity) floorHost() bool {
	return h.Kind == KindPlatform && h.Market == MarketOnDemand && h.GPUCount == 0
}

func (h HostCapacity) keepsFloor(floor FleetCapacity) bool {
	return h.floorHost() && h.FreeCPUMillis >= floor.CPUMillis && h.FreeMemoryBytes >= floor.MemoryBytes
}

// ChooseHost is the host, by index, a container of r goes to, or -1: one
// that fits it, a Spot host before an on-demand one for work that can run
// on Spot, then the tightest fit, whose free CPU and memory as fractions
// of its size sum lowest after placement, which keeps large holes for
// large containers. r's preferred host wins among hosts that borrow alike.
// Spot-tolerant work borrows an on-demand platform CPU host only while one
// of them still keeps floor free afterwards, so work that cannot run on
// Spot starts at once.
func ChooseHost(hosts []HostCapacity, r Requirement, floor FleetCapacity) int {
	holders := 0
	if r.Preemptible && !floor.Empty() {
		for _, h := range hosts {
			if h.keepsFloor(floor) {
				holders++
			}
		}
	}
	best, bestBorrows, bestPreferred, bestScore := -1, false, false, 0.0
	for i, h := range hosts {
		if h.CPUMillis <= 0 || h.MemoryBytes <= 0 || !h.Fits(r) {
			continue
		}
		borrows := r.Preemptible && h.Market != MarketSpot
		if borrows && !floor.Empty() && h.floorHost() {
			after, left := h, holders
			after.Reserve(r)
			if h.keepsFloor(floor) && !after.keepsFloor(floor) {
				left--
			}
			if left < 1 {
				continue
			}
		}
		preferred := r.Prefer != nil && h.Host == *r.Prefer
		score := float64(h.FreeCPUMillis-r.CPUMillis)/float64(h.CPUMillis) + float64(h.FreeMemoryBytes-r.MemoryBytes)/float64(h.MemoryBytes)
		if best < 0 || bestBorrows && !borrows ||
			borrows == bestBorrows && (preferred && !bestPreferred || preferred == bestPreferred && score < bestScore) {
			best, bestBorrows, bestPreferred, bestScore = i, borrows, preferred, score
		}
	}
	return best
}

// Reserve subtracts r's resources from the host's free capacity.
func (h *HostCapacity) Reserve(r Requirement) {
	h.FreeCPUMillis -= r.CPUMillis
	h.FreeMemoryBytes -= r.MemoryBytes
	h.FreeGPUs -= r.GPUsNeeded()
	h.FreeDiskSlots -= r.Disks
}

// GPUAccepted reports whether a preference takes model. An empty
// preference takes nothing: the caller asked for no GPU.
func GPUAccepted(preference []string, model string) bool {
	if model == "" {
		return false
	}
	for _, p := range preference {
		if p == GPUAny || p == model {
			return true
		}
	}
	return false
}

// GPURank is model's position in preference, or -1.
func GPURank(preference []string, model string) int {
	for n, p := range preference {
		if p == GPUAny || p == model {
			return n
		}
	}
	return -1
}

// ProductRegion maps a provider region to the product region workloads
// name, or "" for a host with no region.
func ProductRegion(region string) string {
	switch region {
	case "us-east-1", "us-east-2":
		return "us-east"
	case "us-west-1", "us-west-2":
		return "us-west"
	case "eu-central-1", "eu-central-2":
		return "eu-central"
	case "eu-north-1":
		return "eu-north"
	case "ap-southeast-1", "ap-southeast-3", "ap-southeast-5":
		return "ap-southeast"
	}
	return ""
}

// AvailableCapacity returns the hosts that take new containers, with their
// unreserved resources, read in tx: online, reported within
// LivenessTimeout, ready and available, with no agent update due. The
// snapshot stays exact only while the caller serializes assignment.
func AvailableCapacity(ctx context.Context, tx pgx.Tx) ([]HostCapacity, error) {
	rows, err := New(tx).AvailableCapacity(ctx, LivenessTimeout.Seconds())
	if err != nil {
		return nil, fmt.Errorf("read host capacity: %w", err)
	}
	hosts := make([]HostCapacity, 0, len(rows))
	for _, row := range rows {
		hosts = append(hosts, HostCapacity{
			Host:            HostID(row.ID),
			Kind:            HostKind(row.Kind),
			Provider:        Provider(row.Provider),
			Connection:      row.ConnectionID,
			Name:            row.Name,
			Workspaces:      row.Workspaces,
			Region:          row.Region,
			Zone:            row.AvailabilityZone,
			ZoneID:          row.ZoneID,
			Market:          marketOf(row.Market),
			GPUType:         row.GpuType,
			GPUCount:        int(row.GpuCount),
			CPUMillis:       row.CpuMillis,
			MemoryBytes:     row.MemoryBytes,
			FreeCPUMillis:   cpu.Millis(row.FreeCpuMillis),
			FreeMemoryBytes: row.FreeMemoryBytes,
			FreeGPUs:        int(row.FreeGpus),
			FreeDiskSlots:   int(row.FreeDiskSlots),
		})
	}
	return hosts, nil
}

func marketOf(m *string) Market {
	if m == nil {
		return ""
	}
	return Market(*m)
}
