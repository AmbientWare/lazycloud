package execution

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/compute"
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
// placement. workspace is charged for its capacity, so billing admits it
// first and refuses it when the account cannot pay or runs the most
// containers its plan allows.
func (e *Execution) CreateBuildContainer(ctx context.Context, tx pgx.Tx, workspace identity.WorkspaceID, build uuid.UUID, cpuMillis, memoryBytes int64) (ContainerID, error) {
	if _, err := billing.Admit(ctx, tx, billing.Request{Workspace: uuid.UUID(workspace), Start: 1, Cold: true}); err != nil {
		return ContainerID{}, err
	}
	id, err := e.queries.WithTx(tx).CreateBuildContainer(ctx, CreateBuildContainerParams{
		WorkspaceID: uuid.UUID(workspace), ImageBuildID: &build, CpuMillis: cpuMillis, MemoryBytes: memoryBytes,
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
	for _, row := range drained {
		if row.HostID != nil {
			if err := database.Notify(ctx, tx, database.ChannelHost, row.HostID.String()); err != nil {
				return err
			}
		}
	}
	return nil
}

// BuildStart asks a host to start a build container it was assigned.
type BuildStart struct {
	Container   ContainerID
	Build       uuid.UUID
	Attempt     int
	CPUMillis   int64
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
