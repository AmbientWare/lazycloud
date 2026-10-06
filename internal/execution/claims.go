package execution

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
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

// traceClaim records the task's wait in the queue, from when it became due
// to its claim, in its trace, linked to the trace of the container that
// claimed it. A task without a trace, such as a cron task, records none.
func traceClaim(ctx context.Context, task ClaimedTask, due time.Time, container ContainerID, containerTrace string) {
	if !telemetry.SpanContextOf(task.TraceParent).IsValid() {
		return
	}
	_, span := telemetry.StartFor(ctx, task.TraceParent, "execution.queued", trace.WithTimestamp(due),
		trace.WithLinks(telemetry.Link(containerTrace)), trace.WithAttributes(telemetry.Task(task.Task.String()),
			attribute.String("lazycloud."+telemetry.KeyAttempt, task.Attempt.String()), telemetry.Container(container.String()),
			attribute.Int("lazycloud.attempt_number", task.Number)))
	span.End()
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
		rows, err := q.ClaimQueuedTasks(ctx, ClaimQueuedTasksParams{
			ReleaseID:      c.ReleaseID,
			MaxTasks:       int32(limit), //nolint:gosec // Bounded by the container's slots.
			MaxInputBytes:  MaxClaimInputBytes,
			ContainerID:    uuid.UUID(container),
			TimeoutSeconds: float64(timeout),
		})
		if err != nil {
			return fmt.Errorf("claim tasks: %w", err)
		}
		if len(rows) == 0 {
			at, err := q.NextQueuedAt(ctx, c.ReleaseID)
			if err == nil {
				next = &at
			} else if !errors.Is(err, pgx.ErrNoRows) {
				return fmt.Errorf("read next queued task: %w", err)
			}
			return nil
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
			claimed[n].TraceParent = deref(row.Traceparent)
			traceClaim(ctx, claimed[n], row.AvailableAt, container, deref(c.Traceparent))
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
