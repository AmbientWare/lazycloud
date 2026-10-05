package compute

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/cpu"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// Summary is a workspace's location and capacity.
type Summary struct {
	// AWSAccountID and ConnectionPhase are set when the workspace lives in
	// a connected account.
	AWSAccountID    string
	ConnectionPhase ConnectionPhase
	Total           int
	Ready           int
	Pending         int
	Degraded        int
	// HourlyMicros estimates what the connection's instances cost; nil on
	// platform compute.
	HourlyMicros  *int64
	WorkloadCount int
}

// WorkspaceSummary summarizes a workspace's compute.
func (c *Compute) WorkspaceSummary(ctx context.Context, workspace identity.WorkspaceID) (Summary, error) {
	row, err := c.queries.WorkspaceComputeSummary(ctx, WorkspaceComputeSummaryParams{
		WorkspaceID: uuid.UUID(workspace), TimeoutSeconds: LivenessTimeout.Seconds(),
	})
	if err != nil {
		return Summary{}, fmt.Errorf("read compute summary: %w", err)
	}
	s := Summary{
		Total: int(row.Total), Ready: int(row.Ready), Pending: int(row.Pending), Degraded: int(row.Degraded),
		WorkloadCount: int(row.WorkloadCount),
	}
	if row.AwsAccountID != nil {
		s.AWSAccountID = *row.AwsAccountID
		s.ConnectionPhase = ConnectionPhase(deref(row.ConnectionPhase))
		s.HourlyMicros = ptr(row.HourlyMicros)
	}
	return s, nil
}

// Workload is a deployed workload's size and machine pin.
type Workload struct {
	ID          uuid.UUID
	App         string
	Name        string
	Kind        string
	Machine     string
	CPUMillis   cpu.Millis
	MemoryBytes int64
	GPUs        []string
	GPUCount    int
}

// WorkloadCursor is the last workload of a page.
type WorkloadCursor struct {
	App  string `json:"a"`
	Name string `json:"n"`
}

// Workloads lists a workspace's deployed workloads by app and name after
// the cursor.
func (c *Compute) Workloads(ctx context.Context, workspace identity.WorkspaceID, after WorkloadCursor, limit int) ([]Workload, error) {
	rows, err := c.queries.ComputeWorkloads(ctx, ComputeWorkloadsParams{
		WorkspaceID: uuid.UUID(workspace), AfterApp: after.App, AfterName: after.Name, MaxRows: int32(limit), //nolint:gosec // The API caps limit.
	})
	if err != nil {
		return nil, fmt.Errorf("list compute workloads: %w", err)
	}
	out := make([]Workload, 0, len(rows))
	for _, row := range rows {
		var spec apitypes.WorkloadSpec
		if err := json.Unmarshal(row.Spec, &spec); err != nil {
			return nil, fmt.Errorf("decode release spec: %w", err)
		}
		w := Workload{
			ID: row.ID, App: row.App, Name: row.Name, Kind: row.Kind,
			CPUMillis: cpu.Millis(spec.Resources.CpuMillis), MemoryBytes: int64(spec.Resources.MemoryMib) << 20,
			GPUs: []string{},
		}
		if spec.Placement != nil && spec.Placement.Machine != nil {
			w.Machine = *spec.Placement.Machine
		}
		if spec.Resources.Gpu != nil {
			for _, g := range *spec.Resources.Gpu {
				w.GPUs = append(w.GPUs, string(g))
			}
		}
		if spec.Resources.GpuCount != nil {
			w.GPUCount = *spec.Resources.GpuCount
		}
		out = append(out, w)
	}
	return out, nil
}

// Instance is a connected account's cloud host.
type Instance struct {
	ID             HostID
	Connection     uuid.UUID
	Region         string
	Zone           string
	InstanceID     string
	InstanceType   string
	Market         Market
	Phase          Phase
	PhaseMessage   string
	PhaseAt        time.Time
	Failure        *Failure
	Connected      bool
	CapacityState  CapacityState
	CapacityReason string
	GPUType        string
	GPUCount       int
	CPUMillis      cpu.Millis
	MemoryBytes    int64
	LaunchAttempts int
	AgentVersion   string
	LaunchedAt     *time.Time
	CreatedAt      time.Time
}

// Instances lists the cloud hosts of account's connection, newest first,
// before the cursor host id.
func (c *Compute) Instances(ctx context.Context, account identity.UserID, before *uuid.UUID, limit int) ([]Instance, error) {
	cursor := uuid.Max
	if before != nil {
		cursor = *before
	}
	rows, err := c.queries.ConnectionInstances(ctx, ConnectionInstancesParams{
		AccountID: uuid.UUID(account), BeforeID: cursor, MaxRows: int32(limit), //nolint:gosec // The API caps limit.
	})
	if err != nil {
		return nil, fmt.Errorf("list instances: %w", err)
	}
	out := make([]Instance, 0, len(rows))
	for _, row := range rows {
		i := Instance{
			ID: HostID(row.ID), Region: row.Region, Zone: row.AvailabilityZone, InstanceID: deref(row.InstanceID),
			InstanceType: row.InstanceType, Market: marketOf(row.Market), Phase: Phase(row.Phase),
			PhaseMessage: row.PhaseMessage, PhaseAt: row.PhaseAt, Connected: connected(row.State, row.LastSeenAt),
			CapacityState: CapacityState(row.CapacityState), CapacityReason: row.CapacityReason,
			GPUType: row.GpuType, GPUCount: int(row.GpuCount), CPUMillis: row.CpuMillis, MemoryBytes: row.MemoryBytes,
			LaunchAttempts: max(int(row.LaunchAttempts), 1), AgentVersion: row.AgentVersion,
			LaunchedAt: row.LaunchedAt, CreatedAt: row.CreatedAt,
		}
		if row.ConnectionID != nil {
			i.Connection = *row.ConnectionID
		}
		if row.Failure != nil {
			f := Failure(*row.Failure)
			i.Failure = &f
		}
		out = append(out, i)
	}
	return out, nil
}
