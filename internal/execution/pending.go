package execution

import (
	"context"
	"fmt"
	"time"

	"github.com/google/uuid"
)

// PendingReason says why a queued task has not started.
type PendingReason string

const (
	PendingQueued              PendingReason = "queued"
	PendingDependencies        PendingReason = "dependencies"
	PendingRetry               PendingReason = "retry"
	PendingCapacityBusy        PendingReason = "capacity_busy"
	PendingCapacityUnavailable PendingReason = "capacity_unavailable"
	// PendingCapacityLimit and PendingProvisioningCompute come from the
	// capacity controller's mark on the release's pending containers.
	PendingCapacityLimit       PendingReason = "capacity_limit"
	PendingProvisioningCompute PendingReason = "provisioning_compute"
	PendingStartingContainer   PendingReason = "starting_container"
)

// Message is the sentence clients show for the reason.
func (r PendingReason) Message() string {
	switch r {
	case PendingQueued:
		return "Waiting for this function to start."
	case PendingDependencies:
		return "Waiting for input functions to finish."
	case PendingRetry:
		return "Waiting to retry this function."
	case PendingCapacityBusy:
		return "Waiting for an available function container."
	case PendingCapacityUnavailable:
		return "Waiting for compute availability. Retrying automatically."
	case PendingCapacityLimit:
		return "Waiting for capacity within the compute limit."
	case PendingProvisioningCompute:
		return "Starting compute for this function."
	case PendingStartingContainer:
		return "Compute is assigned. Starting the function container."
	}
	return string(r)
}

// PendingProgress explains a queued task. It is derived on every read from
// the task's and its release's current rows, so it needs no separate state.
type PendingProgress struct {
	Reason PendingReason
	// Since is when the current reason began.
	Since time.Time
	// PendingSince is when the task was submitted or its last attempt
	// ended.
	PendingSince time.Time
	ObservedAt   time.Time
}

// addPending sets Pending on the queued tasks in one query.
func (e *Execution) addPending(ctx context.Context, tasks []Task) error {
	var ids []uuid.UUID
	index := map[uuid.UUID]int{}
	for n, task := range tasks {
		if task.Status == TaskQueued {
			ids = append(ids, uuid.UUID(task.ID))
			index[uuid.UUID(task.ID)] = n
		}
	}
	if len(ids) == 0 {
		return nil
	}
	rows, err := e.queries.PendingFacts(ctx, ids)
	if err != nil {
		return fmt.Errorf("read pending facts: %w", err)
	}
	for _, row := range rows {
		progress := pendingProgress(row)
		tasks[index[row.ID]].Pending = &progress
	}
	return nil
}

// pendingProgress picks the reason in the order a task meets them: inputs
// first, then a retry delay, then the container that will run it.
func pendingProgress(row PendingFactsRow) PendingProgress {
	since := row.CreatedAt
	if row.AttemptCount > 0 && row.LastFinishedAt != nil {
		since = *row.LastFinishedAt
	}
	at := func(reason PendingReason, start *time.Time) PendingProgress {
		p := PendingProgress{Reason: reason, Since: since, PendingSince: since, ObservedAt: row.ObservedAt}
		if start != nil && start.After(since) {
			p.Since = *start
		}
		return p
	}
	switch {
	case row.UnmetDependencies > 0:
		return at(PendingDependencies, nil)
	case row.AttemptCount > 0 && row.AvailableAt.After(row.ObservedAt):
		return at(PendingRetry, nil)
	case row.StartingSince != nil:
		return at(PendingStartingContainer, row.StartingSince)
	case row.UnplacedSince != nil && row.Provisioning > 0:
		// Compute is buying a host for the container.
		return at(PendingProvisioningCompute, row.UnplacedSince)
	case row.UnplacedSince != nil && row.Limited > 0:
		// The fleet limit holds the purchase back.
		return at(PendingCapacityLimit, row.UnplacedSince)
	case row.UnplacedSince != nil:
		// Placement runs as soon as a container is requested, so one that
		// stays unplaced has no host with room and none is being bought.
		return at(PendingCapacityUnavailable, row.UnplacedSince)
	case row.Ready > 0:
		return at(PendingCapacityBusy, nil)
	}
	return at(PendingQueued, nil)
}
