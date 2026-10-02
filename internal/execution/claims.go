package execution

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
)

// MaxClaimInputBytes caps the total input bytes one claim returns, so a claim
// response stays well within the host message limit. A claim always returns
// its first due task, and one input is at most MaxPayloadBytes.
const MaxClaimInputBytes = 64 << 20

// ErrNotAssigned means the container is unknown or assigned to another host.
var ErrNotAssigned = errors.New("container is not assigned to this host")

// ClaimedTask is a task a container's slot runs as attempt Attempt.
type ClaimedTask struct {
	Task        TaskID
	Attempt     AttemptID
	Number      int
	MaxAttempts int
	Input       Payload
	Deadline    time.Time
	// Root is the root of the task's call graph, the task itself when no
	// task spawned it; Parent is set when one did.
	Root   TaskID
	Parent *TaskID
	// Dependencies are the results of the upstream tasks the input refers
	// to.
	Dependencies []DependencyResult
	// TraceParent is the trace of the request that submitted the task;
	// empty when it was not traced.
	TraceParent string
}

// DependencyResult is an upstream task's result.
type DependencyResult struct {
	Task   TaskID
	Result Payload
}

// ClaimTasks starts up to max attempts on container, bounded by its free
// slots and MaxClaimInputBytes, waiting up to wait for due queued tasks of its release. A container
// that is not ready claims nothing.
func (e *Execution) ClaimTasks(ctx context.Context, listener *database.Listener, host compute.HostID, container ContainerID, maxTasks int, wait time.Duration) ([]ClaimedTask, error) {
	release, err := e.queries.AssignedContainerRelease(ctx, AssignedContainerReleaseParams{
		ID: uuid.UUID(container), HostID: hostUUID(host),
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotAssigned
	}
	if err != nil {
		return nil, fmt.Errorf("read container: %w", err)
	}
	// Subscribe before the first claim so a submit that commits in between
	// still wakes this call.
	wake, cancel := listener.Subscribe(database.ChannelClaim, release.String())
	defer cancel()
	deadline := time.Now().Add(wait)
	for {
		claimed, next, err := e.claimOnce(ctx, host, container, maxTasks)
		if err != nil || len(claimed) > 0 {
			return claimed, err
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return nil, nil
		}
		if next != nil {
			// A retry delay ends before the wait does.
			remaining = min(remaining, max(time.Until(*next), 10*time.Millisecond))
		}
		timer := time.NewTimer(remaining)
		select {
		case <-wake:
		case <-timer.C:
		case <-ctx.Done():
			timer.Stop()
			return nil, nil
		}
		timer.Stop()
	}
}

// claimOnce claims in one transaction. When nothing is due it returns when
// the next queued task of the release becomes due, if one exists.
func (e *Execution) claimOnce(ctx context.Context, host compute.HostID, container ContainerID, maxTasks int) ([]ClaimedTask, *time.Time, error) {
	var claimed []ClaimedTask
	var next *time.Time
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		q := e.queries.WithTx(tx)
		c, err := q.LockContainerForClaim(ctx, uuid.UUID(container))
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotAssigned
		}
		if err != nil {
			return fmt.Errorf("lock container: %w", err)
		}
		if c.HostID == nil || *c.HostID != uuid.UUID(host) {
			return ErrNotAssigned
		}
		// An instance or shell container of a function's release runs no
		// tasks.
		if ContainerState(c.State) != ContainerReady || ContainerPurpose(c.Purpose) != PurposeServe {
			return nil
		}
		running, err := q.CountRunningAttemptsOnContainer(ctx, uuid.UUID(container))
		if err != nil {
			return fmt.Errorf("count running attempts: %w", err)
		}
		limit := min(maxTasks, int(c.Slots)-int(running))
		if limit <= 0 {
			return nil
		}
		var spec apitypes.WorkloadSpec
		if err := json.Unmarshal(c.Spec, &spec); err != nil {
			return fmt.Errorf("decode release spec: %w", err)
		}
		timeout := 3600
		if spec.TimeoutSeconds != nil {
			timeout = *spec.TimeoutSeconds
		}
		candidates, err := q.LockClaimCandidates(ctx, LockClaimCandidatesParams{
			ReleaseID: c.ReleaseID,
			MaxTasks:  int32(limit), //nolint:gosec // Bounded by the container's slots.
		})
		if err != nil {
			return fmt.Errorf("lock queued tasks: %w", err)
		}
		if len(candidates) == 0 {
			at, err := q.NextQueuedAt(ctx, c.ReleaseID)
			if err == nil {
				next = &at
			} else if !errors.Is(err, pgx.ErrNoRows) {
				return fmt.Errorf("read next queued task: %w", err)
			}
			return nil
		}
		rows, err := startAttempts(ctx, tx, uuid.UUID(container), float64(timeout), claimPrefix(candidates))
		if err != nil {
			return err
		}
		claimed = make([]ClaimedTask, len(rows))
		ids := make([]uuid.UUID, len(rows))
		index := make(map[uuid.UUID]int, len(rows))
		for n, row := range rows {
			claimed[n] = ClaimedTask{
				Task: TaskID(row.TaskID), Attempt: AttemptID(row.AttemptID), Number: int(row.Number),
				MaxAttempts: int(row.MaxAttempts),
				Input:       Payload{Encoding: Encoding(row.Encoding), Data: row.Data}, Deadline: row.DeadlineAt,
				Root: TaskID(row.RootTaskID), Parent: (*TaskID)(row.ParentTaskID),
			}
			if row.Traceparent != nil {
				claimed[n].TraceParent = *row.Traceparent
			}
			ids[n] = row.TaskID
			index[row.TaskID] = n
		}
		deps, err := q.DependencyResults(ctx, ids)
		if err != nil {
			return fmt.Errorf("read dependency results: %w", err)
		}
		for _, dep := range deps {
			task := &claimed[index[dep.TaskID]]
			task.Dependencies = append(task.Dependencies, DependencyResult{
				Task: TaskID(dep.DependsOn), Result: Payload{Encoding: Encoding(dep.Encoding), Data: dep.Data},
			})
		}
		return nil
	})
	if err != nil {
		return nil, nil, fmt.Errorf("claim on container %s: %w", container, err)
	}
	return claimed, next, nil
}

// claimPrefix is the longest prefix of the locked candidates whose input
// bytes total at most MaxClaimInputBytes, and always the first, so the
// claim response stays within the host message limit.
func claimPrefix(candidates []LockClaimCandidatesRow) []uuid.UUID {
	tasks := make([]uuid.UUID, 0, len(candidates))
	var total int64
	for _, c := range candidates {
		total += c.InputBytes
		if len(tasks) > 0 && total > MaxClaimInputBytes {
			break
		}
		tasks = append(tasks, c.ID)
	}
	return tasks
}

// startAttempts starts a running attempt on container for each locked task
// in one round trip and returns them in the order of tasks. The batch runs
// in order, so StartClaimedTasks finds the attempts InsertClaimAttempts
// created.
func startAttempts(ctx context.Context, tx pgx.Tx, container uuid.UUID, timeout float64, tasks []uuid.UUID) ([]StartClaimedTasksRow, error) {
	var started []StartClaimedTasksRow
	batch := &pgx.Batch{}
	batch.Queue(insertClaimAttempts, container, timeout, tasks)
	batch.Queue(startClaimedTasks, tasks).Query(func(rows pgx.Rows) error {
		var err error
		if started, err = pgx.CollectRows(rows, pgx.RowToStructByPos[StartClaimedTasksRow]); err != nil {
			return fmt.Errorf("read started tasks: %w", err)
		}
		return nil
	})
	if err := tx.SendBatch(ctx, batch).Close(); err != nil {
		return nil, fmt.Errorf("start attempts: %w", err)
	}
	if len(started) != len(tasks) {
		return nil, fmt.Errorf("start attempts: %d of %d tasks started", len(started), len(tasks))
	}
	order := make(map[uuid.UUID]int, len(tasks))
	for n, task := range tasks {
		order[task] = n
	}
	slices.SortFunc(started, func(a, b StartClaimedTasksRow) int { return order[a.TaskID] - order[b.TaskID] })
	return started, nil
}
