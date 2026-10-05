package execution

import (
	"context"
	"errors"
	"fmt"
	"slices"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/cpu"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// An image build runs in containers that execution owns like any other:
// scheduling places them, hosts report them and containerExited stops them.
// A build container has no release; the images owner decides what it runs
// and when its build is done.

// BuildContainer is one container of an image build.
type BuildContainer struct {
	ID          ContainerID
	State       ContainerState
	Host        *compute.HostID
	StopReason  StopReason
	ExitMessage string
}

// CreateBuildContainer requests a container for build in tx and wakes
// placement. A build on gpu, a model, holds one card of it; "" is a CPU
// build. workspace is charged for its capacity, so billing admits it first
// and refuses it when the account cannot pay, may not use the model, or runs
// the most containers or GPUs its plan allows.
func (e *Execution) CreateBuildContainer(ctx context.Context, tx pgx.Tx, workspace identity.WorkspaceID, build uuid.UUID, cpuMillis, memoryBytes int64, gpu string) (ContainerID, error) {
	req := billing.Request{Workspace: uuid.UUID(workspace), Start: 1, Cold: true}
	if gpu != "" {
		req.GPUs, req.GPUModels = 1, []billing.GPUType{billing.GPUType(gpu)}
	}
	grant, err := billing.Admit(ctx, tx, req)
	if err != nil {
		return ContainerID{}, err
	}
	if grant.Start == 0 {
		return ContainerID{}, &billing.LimitError{Message: "the account holds the most containers or GPUs its plan allows"}
	}
	// Placement gives the build the model admission granted, which is the
	// one it named.
	if gpu != "" && !slices.Contains(grant.GPUModels, billing.GPUType(gpu)) {
		return ContainerID{}, &billing.PaymentRequiredError{Message: "this account may not use " + gpu}
	}
	id, err := e.queries.WithTx(tx).CreateBuildContainer(ctx, CreateBuildContainerParams{
		WorkspaceID: uuid.UUID(workspace), ImageBuildID: &build, CpuMillis: cpu.Millis(cpuMillis), MemoryBytes: memoryBytes,
		GpuCount: int32(len(req.GPUModels)), //nolint:gosec // At most one GPU.
	})
	if err != nil {
		return ContainerID{}, fmt.Errorf("create build container: %w", err)
	}
	if err := database.Notify(ctx, tx, database.ChannelExecution, id.String()); err != nil {
		return ContainerID{}, err
	}
	return ContainerID(id), nil
}

// BuildContainers lists build's containers in tx, oldest first.
func (e *Execution) BuildContainers(ctx context.Context, tx pgx.Tx, build uuid.UUID) ([]BuildContainer, error) {
	rows, err := e.queries.WithTx(tx).BuildContainers(ctx, &build)
	if err != nil {
		return nil, fmt.Errorf("list build containers: %w", err)
	}
	out := make([]BuildContainer, len(rows))
	for n, row := range rows {
		out[n] = BuildContainer{ID: ContainerID(row.ID), State: ContainerState(row.State)}
		if row.HostID != nil {
			host := compute.HostID(*row.HostID)
			out[n].Host = &host
		}
		if row.StopReason != nil {
			out[n].StopReason = StopReason(*row.StopReason)
		}
		if row.ExitMessage != nil {
			out[n].ExitMessage = *row.ExitMessage
		}
	}
	return out, nil
}

// StopBuildContainers ends build's containers in tx: pending ones stop at
// once and live ones drain, which makes their hosts stop them.
func (e *Execution) StopBuildContainers(ctx context.Context, tx pgx.Tx, build uuid.UUID) error {
	q := e.queries.WithTx(tx)
	if err := q.StopPendingBuildContainers(ctx, &build); err != nil {
		return fmt.Errorf("stop pending build containers: %w", err)
	}
	drained, err := q.DrainBuildContainers(ctx, &build)
	if err != nil {
		return fmt.Errorf("drain build containers: %w", err)
	}
	var hosts []string
	for _, row := range drained {
		if row.HostID != nil {
			hosts = append(hosts, row.HostID.String())
		}
	}
	return database.NotifyAll(ctx, tx, database.ChannelHost, hosts)
}

// BuildStart asks a host to start a build container it was assigned.
type BuildStart struct {
	Container   ContainerID
	Build       uuid.UUID
	Attempt     int
	CPUMillis   cpu.Millis
	MemoryBytes int64
}

// BuildStarts are the build containers starting on host. Like HostCommands
// they are derived again after every wake and reconnect.
func (e *Execution) BuildStarts(ctx context.Context, host compute.HostID) ([]BuildStart, error) {
	rows, err := e.queries.StartingBuildContainersOnHost(ctx, hostUUID(host))
	if err != nil {
		return nil, fmt.Errorf("list starting build containers: %w", err)
	}
	out := make([]BuildStart, len(rows))
	for n, row := range rows {
		out[n] = BuildStart{
			Container: ContainerID(row.ID), Build: row.ImageBuildID, Attempt: int(row.Attempt),
			CPUMillis: row.CpuMillis, MemoryBytes: row.MemoryBytes,
		}
	}
	return out, nil
}

// LiveBuildContainer returns the build and attempt that container runs on
// host. ErrNotAssigned means the container is not a live build container of
// that host.
func (e *Execution) LiveBuildContainer(ctx context.Context, host compute.HostID, container ContainerID) (uuid.UUID, int, error) {
	row, err := e.queries.LiveBuildContainer(ctx, LiveBuildContainerParams{ID: uuid.UUID(container), HostID: hostUUID(host)})
	if errors.Is(err, pgx.ErrNoRows) {
		return uuid.UUID{}, 0, ErrNotAssigned
	}
	if err != nil {
		return uuid.UUID{}, 0, fmt.Errorf("read build container: %w", err)
	}
	return row.ImageBuildID, int(row.Attempt), nil
}
