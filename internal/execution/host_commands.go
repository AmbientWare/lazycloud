package execution

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// cancelResendWindow is how long after an attempt ends its cancel command is
// still derived. A host that reconnects later reports the attempt as running
// and gets the cancel from ApplyReport instead.
const cancelResendWindow = 10 * time.Minute

// StartCommand asks a host to start a container it was assigned.
type StartCommand struct {
	Container   ContainerID
	Workspace   identity.WorkspaceID
	Source      storage.Digest
	Spec        apitypes.FunctionSpec
	Slots       int
	CPUMillis   int64
	MemoryBytes int64
	// CPULimitMillis and MemoryLimitBytes are the ceilings above the
	// reservations.
	CPULimitMillis   int64
	MemoryLimitBytes int64
}

// StopCommand asks a host to stop a container.
type StopCommand struct {
	Container ContainerID
}

// CancelCommand asks a host to kill the slot running an attempt that
// already ended as Reason (cancelled or timed out).
type CancelCommand struct {
	Container ContainerID
	Attempt   AttemptID
	Reason    AttemptState
}

// HostCommands are the commands durable state implies for a host. They are
// derived again after every wake and reconnect, so hosts apply them
// idempotently.
type HostCommands struct {
	Start  []StartCommand
	Stop   []StopCommand
	Cancel []CancelCommand
}

// HostCommands derives the host's commands: start every container assigned
// to it that is starting, stop every draining container without running
// attempts, and kill slots whose attempts were recently cancelled or timed
// out.
func (e *Execution) HostCommands(ctx context.Context, host compute.HostID) (HostCommands, error) {
	var out HostCommands
	starting, err := e.queries.StartingContainersOnHost(ctx, hostUUID(host))
	if err != nil {
		return out, fmt.Errorf("list starting containers: %w", err)
	}
	for _, row := range starting {
		var spec apitypes.FunctionSpec
		if err := json.Unmarshal(row.Spec, &spec); err != nil {
			return out, fmt.Errorf("decode release spec: %w", err)
		}
		var source storage.Digest
		copy(source[:], row.SourceSha256)
		out.Start = append(out.Start, StartCommand{
			Container: ContainerID(row.ID), Workspace: identity.WorkspaceID(row.WorkspaceID),
			Source: source, Spec: spec, Slots: int(row.Slots),
			CPUMillis: row.CpuMillis, MemoryBytes: row.MemoryBytes,
			CPULimitMillis:   cpuLimitMillis(spec.Resources),
			MemoryLimitBytes: memoryLimitMiB(spec.Resources) << 20,
		})
	}
	idle, err := e.queries.IdleDrainingContainersOnHost(ctx, hostUUID(host))
	if err != nil {
		return out, fmt.Errorf("list idle draining containers: %w", err)
	}
	for _, id := range idle {
		out.Stop = append(out.Stop, StopCommand{Container: ContainerID(id)})
	}
	ended, err := e.queries.EndedAttemptsOnHost(ctx, EndedAttemptsOnHostParams{
		HostID: hostUUID(host), WithinSeconds: cancelResendWindow.Seconds(),
	})
	if err != nil {
		return out, fmt.Errorf("list ended attempts: %w", err)
	}
	for _, row := range ended {
		out.Cancel = append(out.Cancel, CancelCommand{
			Container: ContainerID(row.ContainerID), Attempt: AttemptID(row.ID), Reason: AttemptState(row.State),
		})
	}
	return out, nil
}

// Default ceilings above a reservation. CPU is compressible, so a container
// may use its reservation plus 16 cores when the host has them free. Memory is
// not: without a stated limit a container may reach four times its
// reservation, at least 1 GiB and at most 8 GiB above it, before it is killed.
const (
	cpuBurstMillis     = 16_000
	memoryBurstFactor  = 4
	memoryBurstFloorMi = 1024
	memoryBurstCapMi   = 8192
)

func cpuLimitMillis(r apitypes.Resources) int64 {
	if r.CpuLimitMillis != nil {
		return int64(*r.CpuLimitMillis)
	}
	return int64(r.CpuMillis) + cpuBurstMillis
}

func memoryLimitMiB(r apitypes.Resources) int64 {
	if r.MemoryLimitMib != nil {
		return int64(*r.MemoryLimitMib)
	}
	reserved := int64(r.MemoryMib)
	return min(max(reserved*memoryBurstFactor, reserved+memoryBurstFloorMi), reserved+memoryBurstCapMi)
}
