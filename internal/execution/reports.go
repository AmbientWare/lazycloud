package execution

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
)

// ReportPhase is what a host observed of a container.
type ReportPhase string

const (
	// ReportPreparing: the host is fetching the image, source and runtime.
	ReportPreparing ReportPhase = "preparing"
	// ReportStarting: the container runs and its slots are loading.
	ReportStarting ReportPhase = "starting"
	// ReportReady: every slot loaded the handler.
	ReportReady ReportPhase = "ready"
	// ReportExited: the container is gone.
	ReportExited ReportPhase = "exited"
)

// ContainerReport is a host's observation of one container.
type ContainerReport struct {
	Container ContainerID
	Phase     ReportPhase
	// Exit is set for ReportExited.
	Exit *ContainerExit
	// Running lists the attempts the container's slots are running.
	Running []AttemptID
}

// ReportActions are commands the host must receive because of what it
// reported: containers it runs without assignment, and attempts it runs that
// already ended.
type ReportActions struct {
	Stop   []ContainerID
	Cancel []CancelCommand
}

func (a *ReportActions) merge(b ReportActions) {
	a.Stop = append(a.Stop, b.Stop...)
	a.Cancel = append(a.Cancel, b.Cancel...)
}

// ApplyReport applies one container report from host. Ready moves a starting
// container to ready and resets its release's start failures. Exited stops
// the container. A container the host runs but durable state does not assign
// to it, or has stopped, must stop.
func (e *Execution) ApplyReport(ctx context.Context, host compute.HostID, report ContainerReport) (ReportActions, error) {
	var actions ReportActions
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		actions = ReportActions{}
		q := e.queries.WithTx(tx)
		row, err := q.LockContainer(ctx, uuid.UUID(report.Container))
		if errors.Is(err, pgx.ErrNoRows) || (err == nil && (row.HostID == nil || *row.HostID != uuid.UUID(host))) {
			if report.Phase != ReportExited {
				actions.Stop = append(actions.Stop, report.Container)
			}
			return nil
		}
		if err != nil {
			return fmt.Errorf("lock container: %w", err)
		}
		state := ContainerState(row.State)
		if state == ContainerStopped {
			if report.Phase != ReportExited {
				actions.Stop = append(actions.Stop, report.Container)
			}
			return nil
		}
		switch report.Phase {
		case ReportPreparing, ReportStarting:
		case ReportReady:
			if state == ContainerStarting {
				if err := q.MarkContainerReady(ctx, row.ID); err != nil {
					return fmt.Errorf("mark container ready: %w", err)
				}
				if err := q.ResetStartFailures(ctx, row.ReleaseID); err != nil {
					return fmt.Errorf("reset start failures: %w", err)
				}
				if err := database.Notify(ctx, tx, database.ChannelExecution, row.ReleaseID.String()); err != nil {
					return err
				}
			}
		case ReportExited:
			exit := ContainerExit{Reason: StopCrashed}
			if report.Exit != nil {
				exit = *report.Exit
			}
			return e.containerExited(ctx, tx, report.Container, exit)
		}
		cancel, err := endedAttempts(ctx, q, report.Container, report.Running)
		if err != nil {
			return err
		}
		actions.Cancel = cancel
		return nil
	})
	if err != nil {
		return ReportActions{}, fmt.Errorf("apply report of container %s: %w", report.Container, err)
	}
	return actions, nil
}

// endedAttempts returns the reported running attempts whose durable attempt
// is no longer running on container, which covers cancels lost in a
// disconnect.
func endedAttempts(ctx context.Context, q *Queries, container ContainerID, running []AttemptID) ([]CancelCommand, error) {
	if len(running) == 0 {
		return nil, nil
	}
	ids := make([]uuid.UUID, len(running))
	for n, a := range running {
		ids[n] = uuid.UUID(a)
	}
	rows, err := q.AttemptStates(ctx, ids)
	if err != nil {
		return nil, fmt.Errorf("read attempt states: %w", err)
	}
	durable := map[uuid.UUID]AttemptStatesRow{}
	for _, row := range rows {
		durable[row.ID] = row
	}
	var cancel []CancelCommand
	for _, id := range ids {
		row, ok := durable[id]
		if ok && row.ContainerID == uuid.UUID(container) && AttemptState(row.State) == AttemptRunning {
			continue
		}
		reason := AttemptCancelled
		if ok && AttemptState(row.State) == AttemptTimedOut {
			reason = AttemptTimedOut
		}
		cancel = append(cancel, CancelCommand{Container: container, Attempt: AttemptID(id), Reason: reason})
	}
	return cancel, nil
}

// ReconcileHost applies the container list of a host's Hello. Reported
// containers are applied as reports. Ready or draining containers assigned
// to the host that it did not report are gone, so they stop as crashed and
// their attempts are lost. Starting containers it did not report get their
// start command again from HostCommands.
func (e *Execution) ReconcileHost(ctx context.Context, host compute.HostID, reports []ContainerReport) (ReportActions, error) {
	var actions ReportActions
	reported := make(map[ContainerID]bool, len(reports))
	for _, report := range reports {
		a, err := e.ApplyReport(ctx, host, report)
		if err != nil {
			return ReportActions{}, err
		}
		actions.merge(a)
		reported[report.Container] = true
	}
	live, err := e.queries.LiveContainersOnHost(ctx, hostUUID(host))
	if err != nil {
		return ReportActions{}, fmt.Errorf("list host containers: %w", err)
	}
	for _, row := range live {
		state := ContainerState(row.State)
		if (state != ContainerReady && state != ContainerDraining) || reported[ContainerID(row.ID)] {
			continue
		}
		err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
			return e.containerExited(ctx, tx, ContainerID(row.ID), ContainerExit{
				Reason: StopCrashed, Message: "the host no longer runs the container",
			})
		})
		if err != nil {
			return ReportActions{}, fmt.Errorf("stop missing container %s: %w", row.ID, err)
		}
	}
	return actions, nil
}
