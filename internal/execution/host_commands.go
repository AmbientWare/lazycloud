package execution

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/cpu"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// cancelResendWindow is how long after an attempt ends its cancel command is
// still derived. A host that reconnects later reports the attempt as running
// and gets the cancel from ApplyReport instead.
const cancelResendWindow = 10 * time.Minute

// StartCommand asks a host to start a container it was assigned.
type StartCommand struct {
	Container     ContainerID
	Workspace     identity.WorkspaceID
	WorkspaceName string
	Source        storage.Digest
	Spec          apitypes.WorkloadSpec
	Slots         int
	CPUMillis     cpu.Millis
	MemoryBytes   int64
	// CPULimitMillis and MemoryLimitBytes are the ceilings above the
	// reservations.
	CPULimitMillis   cpu.Millis
	MemoryLimitBytes int64
	Purpose          ContainerPurpose
	Workload         uuid.UUID
	Kind             apitypes.WorkloadKind
	// Command replaces the pod's command for an instance.
	Command []string
	Network NetworkPolicy
}

// StopCommand asks a host to stop a container.
type StopCommand struct {
	Container ContainerID
	// Grace is how long the container may take to finish its work; zero
	// leaves the host's default.
	Grace time.Duration
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
	Start    []StartCommand
	Stop     []StopCommand
	Cancel   []CancelCommand
	Snapshot []SnapshotCommand
	Publish  []PublishCommand
	Network  []NetworkCommand
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
		var spec apitypes.WorkloadSpec
		if err := json.Unmarshal(row.Spec, &spec); err != nil {
			return out, fmt.Errorf("decode release spec: %w", err)
		}
		var source storage.Digest
		copy(source[:], row.SourceSha256)
		out.Start = append(out.Start, StartCommand{
			Container: ContainerID(row.ID), Workspace: identity.WorkspaceID(row.WorkspaceID), WorkspaceName: row.WorkspaceName,
			Source: source, Spec: spec, Slots: int(row.Slots),
			CPUMillis: row.CpuMillis, MemoryBytes: row.MemoryBytes,
			CPULimitMillis:   cpuLimitMillis(spec.Resources),
			MemoryLimitBytes: memoryLimitMiB(spec.Resources) << 20,
			Purpose:          ContainerPurpose(row.Purpose), Workload: row.WorkloadID, Kind: apitypes.WorkloadKind(row.WorkloadKind),
			Command: row.Command, Network: NetworkPolicy{Block: row.BlockNetwork, Allow: row.AllowList},
		})
	}
	idle, err := e.queries.IdleDrainingContainersOnHost(ctx, hostUUID(host))
	if err != nil {
		return out, fmt.Errorf("list idle draining containers: %w", err)
	}
	for _, row := range idle {
		out.Stop = append(out.Stop, StopCommand{Container: ContainerID(row.ID), Grace: time.Duration(row.GraceSeconds) * time.Second})
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
	if err := e.workloadCommands(ctx, host, &out); err != nil {
		return out, err
	}
	return out, nil
}

// Default ceilings above a reservation. CPU is compressible, so a container
// may use its reservation plus 8 CPUs when the host has them free. Memory is
// not: without a stated limit a container may reach four times its
// reservation, at least 1 GiB and at most 8 GiB above it, before it is killed.
const (
	cpuBurstMillis     = cpu.Millis(8_000)
	memoryBurstFactor  = 4
	memoryBurstFloorMi = 1024
	memoryBurstCapMi   = 8192
)

func cpuLimitMillis(r apitypes.Resources) cpu.Millis {
	if r.CpuLimitMillis != nil {
		return cpu.Millis(*r.CpuLimitMillis)
	}
	return cpu.Millis(r.CpuMillis) + cpuBurstMillis
}

func memoryLimitMiB(r apitypes.Resources) int64 {
	if r.MemoryLimitMib != nil {
		return int64(*r.MemoryLimitMib)
	}
	reserved := int64(r.MemoryMib)
	return min(max(reserved*memoryBurstFactor, reserved+memoryBurstFloorMi), reserved+memoryBurstCapMi)
}

// LiveImagesOnHost lists the image references the host's live containers
// were started with.
func (e *Execution) LiveImagesOnHost(ctx context.Context, host compute.HostID) ([]string, error) {
	ids, err := e.queries.LiveImagesOnHost(ctx, hostUUID(host))
	if err != nil {
		return nil, fmt.Errorf("list live images on host: %w", err)
	}
	return ids, nil
}

// RecordImageReference records the image reference container is started
// with on host, before its start is sent. It reports false when the
// container no longer starts there.
func (e *Execution) RecordImageReference(ctx context.Context, host compute.HostID, container ContainerID, reference string) (bool, error) {
	n, err := e.queries.RecordImageReference(ctx, RecordImageReferenceParams{ID: uuid.UUID(container), HostID: hostUUID(host), Reference: &reference})
	if err != nil {
		return false, fmt.Errorf("record image reference: %w", err)
	}
	return n == 1, nil
}

// ContainerImage returns the workspace of a live container on host and the
// image reference it was started with, or false when the host runs no such
// container.
func (e *Execution) ContainerImage(ctx context.Context, host compute.HostID, container ContainerID) (identity.WorkspaceID, string, bool, error) {
	row, err := e.queries.ContainerImage(ctx, ContainerImageParams{ID: uuid.UUID(container), HostID: hostUUID(host)})
	if errors.Is(err, pgx.ErrNoRows) {
		return identity.WorkspaceID{}, "", false, nil
	}
	if err != nil {
		return identity.WorkspaceID{}, "", false, fmt.Errorf("read container image: %w", err)
	}
	return identity.WorkspaceID(row.WorkspaceID), row.Reference, true, nil
}
