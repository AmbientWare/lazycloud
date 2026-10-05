package execution

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
)

// ErrStaleAttempt means the attempt is no longer running on the caller's
// container, so its outcome is discarded.
var ErrStaleAttempt = errors.New("attempt is no longer running")

// Payload is an encoded task input or result.
type Payload struct {
	Encoding Encoding
	Data     []byte
	// Display is how a cloudpickle result shows without loading it, an
	// apitypes.ResultDisplay the host session checked; nil without one.
	Display json.RawMessage
}

// AttemptOutcome is how an attempt ended.
type AttemptOutcome struct {
	Attempt AttemptID
	// State is any state except AttemptRunning.
	State AttemptState
	// Result is set when State is AttemptSucceeded.
	Result *Payload
	// Failure is set when State is AttemptFailed, AttemptTimedOut or
	// AttemptLost.
	Failure *Failure
}

func (o AttemptOutcome) validate() error {
	switch o.State {
	case AttemptSucceeded:
		if o.Result == nil {
			return errors.New("succeeded attempt without a result")
		}
	case AttemptFailed, AttemptTimedOut, AttemptLost:
		if o.Failure == nil {
			return fmt.Errorf("%s attempt without a failure", o.State)
		}
	case AttemptCancelled:
	case AttemptRunning:
		return errors.New("an outcome cannot be running")
	}
	return nil
}

// finishAttempts records outcomes in tx and advances their tasks: success
// stores the result, a retryable failure with attempts left requeues after
// the release's retry delay, anything else fails the task. It returns which
// outcomes were stale and changed nothing: the attempt was not running, was
// repeated earlier in outcomes, or, with host set, was not assigned to host
// or not on containers[n]. containers is nil or parallel to outcomes. With
// host set, an attempt that ran on a draining container wakes the host,
// which may now stop the container.
func (e *Execution) finishAttempts(ctx context.Context, tx pgx.Tx, host *compute.HostID, containers []ContainerID, outcomes []AttemptOutcome) ([]bool, error) {
	stale := make([]bool, len(outcomes))
	if len(outcomes) == 0 {
		return stale, nil
	}
	ids := make([]uuid.UUID, 0, len(outcomes))
	seen := make(map[uuid.UUID]bool, len(outcomes))
	for n, o := range outcomes {
		if err := o.validate(); err != nil {
			return nil, err
		}
		id := uuid.UUID(o.Attempt)
		if seen[id] {
			stale[n] = true
			continue
		}
		seen[id] = true
		ids = append(ids, id)
	}
	q := e.queries.WithTx(tx)
	taskRows, err := q.LockTasksForAttempts(ctx, ids)
	if err != nil {
		return nil, fmt.Errorf("lock tasks: %w", err)
	}
	tasks := make(map[uuid.UUID]LockTasksForAttemptsRow, len(taskRows))
	for _, row := range taskRows {
		tasks[row.AttemptID] = row
	}
	attemptRows, err := q.LockRunningAttempts(ctx, ids)
	if err != nil {
		return nil, fmt.Errorf("lock attempts: %w", err)
	}
	running := make(map[uuid.UUID]LockRunningAttemptsRow, len(attemptRows))
	for _, row := range attemptRows {
		running[row.ID] = row
	}

	var set SetAttemptStatesParams
	var live []liveOutcome
	wakeHost := false
	for n, o := range outcomes {
		if stale[n] {
			continue
		}
		id := uuid.UUID(o.Attempt)
		task, locked := tasks[id]
		attempt, ok := running[id]
		if !locked || !ok ||
			(host != nil && (attempt.HostID == nil || *attempt.HostID != uuid.UUID(*host))) ||
			(containers != nil && attempt.ContainerID != uuid.UUID(containers[n])) {
			stale[n] = true
			continue
		}
		set.Ids = append(set.Ids, id)
		set.States = append(set.States, string(o.State))
		wakeHost = wakeHost || (host != nil && ContainerState(attempt.ContainerState) == ContainerDraining)
		// A task that moved on, for example through cancellation, keeps
		// this outcome on the attempt row only.
		if TaskStatus(task.Status) == TaskRunning && task.CurrentAttemptID != nil && *task.CurrentAttemptID == id &&
			o.State != AttemptCancelled {
			live = append(live, liveOutcome{task: task, outcome: o})
		}
	}
	if len(set.Ids) > 0 {
		if err := q.SetAttemptStates(ctx, set); err != nil {
			return nil, fmt.Errorf("set attempt states: %w", err)
		}
	}
	if err := e.advanceTasks(ctx, tx, live); err != nil {
		return nil, err
	}
	if wakeHost {
		if err := database.Notify(ctx, tx, database.ChannelHost, host.String()); err != nil {
			return nil, err
		}
	}
	return stale, nil
}

// liveOutcome is an outcome of its task's current attempt.
type liveOutcome struct {
	task    LockTasksForAttemptsRow
	outcome AttemptOutcome
}

// advanceTasks applies the outcomes of the tasks' current attempts, whose
// rows the caller holds, and wakes the tasks' waiters and planning.
func (e *Execution) advanceTasks(ctx context.Context, tx pgx.Tx, live []liveOutcome) error {
	if len(live) == 0 {
		return nil
	}
	q := e.queries.WithTx(tx)
	policies, err := e.retryPolicies(ctx, q, live)
	if err != nil {
		return err
	}
	var results InsertTaskResultsParams
	var requeue RequeueTasksParams
	var retryFailures []Failure
	var failed FailRunningTasksParams
	var retryReleases, ids, releases []string
	for _, l := range live {
		task, o := l.task, l.outcome
		ids = append(ids, task.ID.String())
		releases = append(releases, task.ReleaseID.String())
		if o.State == AttemptSucceeded {
			results.TaskIds = append(results.TaskIds, task.ID)
			results.Encodings = append(results.Encodings, string(o.Result.Encoding))
			results.Data = append(results.Data, o.Result.Data)
			results.Displays = append(results.Displays, []byte(o.Result.Display))
			continue
		}
		if policy := policies[task.ReleaseID]; mayRetry(task, *o.Failure) && policy.Retries(o.Failure.Kind) {
			requeue.Ids = append(requeue.Ids, task.ID)
			requeue.DelaySeconds = append(requeue.DelaySeconds, policy.NextAttemptDelay(int(task.AttemptCount)+1).Seconds())
			retryFailures = append(retryFailures, *o.Failure)
			retryReleases = append(retryReleases, task.ReleaseID.String())
			continue
		}
		encoded, err := json.Marshal(o.Failure)
		if err != nil {
			return fmt.Errorf("encode failure: %w", err)
		}
		failed.Ids = append(failed.Ids, task.ID)
		failed.Failures = append(failed.Failures, encoded)
	}
	if len(results.TaskIds) > 0 {
		if err := q.InsertTaskResults(ctx, results); err != nil {
			return fmt.Errorf("insert results: %w", err)
		}
		if err := q.SucceedTasks(ctx, results.TaskIds); err != nil {
			return fmt.Errorf("succeed tasks: %w", err)
		}
		if err := recordCallbacks(ctx, q, CallbackSucceeded, results.TaskIds, nil); err != nil {
			return err
		}
	}
	if len(requeue.Ids) > 0 {
		if err := q.RequeueTasks(ctx, requeue); err != nil {
			return fmt.Errorf("requeue tasks: %w", err)
		}
		if err := recordCallbacks(ctx, q, CallbackRetry, requeue.Ids, retryFailures); err != nil {
			return err
		}
		if err := database.NotifyAll(ctx, tx, database.ChannelClaim, retryReleases); err != nil {
			return err
		}
	}
	if len(failed.Ids) > 0 {
		if err := q.FailRunningTasks(ctx, failed); err != nil {
			return fmt.Errorf("fail tasks: %w", err)
		}
		if err := recordCallbacks(ctx, q, CallbackFailed, failed.Ids, nil); err != nil {
			return err
		}
	}
	if err := e.resolveOutcomeDependents(ctx, tx, results.TaskIds, failed.Ids); err != nil {
		return err
	}
	if err := database.NotifyAll(ctx, tx, database.ChannelTask, ids); err != nil {
		return err
	}
	if err := notifyFinished(ctx, tx, slices.Concat(results.TaskIds, failed.Ids)); err != nil {
		return err
	}
	return database.NotifyAll(ctx, tx, database.ChannelExecution, releases)
}

// retryPolicies reads the retry policy of each release with a failed task
// that may retry.
func (e *Execution) retryPolicies(ctx context.Context, q *Queries, live []liveOutcome) (map[uuid.UUID]RetryPolicy, error) {
	var releases []uuid.UUID
	for _, l := range live {
		if l.outcome.Failure != nil && mayRetry(l.task, *l.outcome.Failure) {
			releases = append(releases, l.task.ReleaseID)
		}
	}
	if len(releases) == 0 {
		return nil, nil
	}
	rows, err := q.ReleaseSpecs(ctx, releases)
	if err != nil {
		return nil, fmt.Errorf("read release specs: %w", err)
	}
	policies := make(map[uuid.UUID]RetryPolicy, len(rows))
	for _, row := range rows {
		var spec apitypes.WorkloadSpec
		if err := json.Unmarshal(row.Spec, &spec); err != nil {
			return nil, fmt.Errorf("decode release spec: %w", err)
		}
		policies[row.ID] = RetryPolicyOf(spec)
	}
	return policies, nil
}

// mayRetry reports whether failure is retryable and task has attempts left.
// The release's retry policy then decides.
func mayRetry(task LockTasksForAttemptsRow, failure Failure) bool {
	return failure.Kind.Retryable() && task.AttemptCount < task.MaxAttempts
}

// resolveOutcomeDependents applies the outcomes of succeeded and failed
// upstream tasks to their dependents. When it takes more than one locking
// statement, every dependent locks first in a single id-ordered one, so
// transactions that finish several upstream tasks never deadlock on shared
// dependents. A dependent of failed tasks names its first failed upstream,
// as when that task failed alone.
func (e *Execution) resolveOutcomeDependents(ctx context.Context, tx pgx.Tx, succeeded, failed []uuid.UUID) error {
	if len(failed) == 0 {
		return e.resolveDependents(ctx, tx, succeeded, upstreamSucceeded)
	}
	if len(succeeded) == 0 && len(failed) == 1 {
		return e.resolveDependents(ctx, tx, failed, upstreamUnsuccessful)
	}
	locked, err := e.queries.WithTx(tx).LockOutcomeDependents(ctx, LockOutcomeDependentsParams{Succeeded: succeeded, Failed: failed})
	if err != nil {
		return fmt.Errorf("lock dependents: %w", err)
	}
	if len(locked) == 0 {
		return nil
	}
	if err := e.resolveDependents(ctx, tx, succeeded, upstreamSucceeded); err != nil {
		return err
	}
	if !slices.ContainsFunc(locked, func(row LockOutcomeDependentsRow) bool { return row.AfterFailure }) {
		return nil
	}
	for _, id := range failed {
		if err := e.resolveDependents(ctx, tx, []uuid.UUID{id}, upstreamUnsuccessful); err != nil {
			return err
		}
	}
	return nil
}
