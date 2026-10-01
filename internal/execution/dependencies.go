package execution

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database"
)

// MaxDependentInputBytes caps a task's input plus the upstream results it
// receives, so one attempt stays within the container link's message limit.
const MaxDependentInputBytes = 48 << 20

// dependenciesTooLarge fails a task whose input and upstream results exceed
// MaxDependentInputBytes.
var dependenciesTooLarge = Failure{
	Kind:    FailureDependencyFailed,
	Type:    "DependenciesTooLarge",
	Message: fmt.Sprintf("the input and upstream results exceed %d MiB", MaxDependentInputBytes>>20),
}

// MaxDependencies caps the upstream tasks one input may name.
const MaxDependencies = 100

// upstreamOutcome is how an upstream task ended.
type upstreamOutcome bool

const (
	upstreamSucceeded    upstreamOutcome = true
	upstreamUnsuccessful upstreamOutcome = false
)

// resolveDependents applies the terminal outcome of upstream tasks to the
// queued tasks that wait on them, in tx. Success lowers each dependent's
// unmet count, and a dependent that reaches zero becomes claimable unless its
// inputs exceed MaxDependentInputBytes. A failed or cancelled upstream fails
// every queued task that depends on it, directly or transitively. Dependents
// lock in id order after the upstream rows the caller holds.
func (e *Execution) resolveDependents(ctx context.Context, tx pgx.Tx, upstream []uuid.UUID, outcome upstreamOutcome) error {
	if len(upstream) == 0 {
		return nil
	}
	q := e.queries.WithTx(tx)
	if outcome == upstreamUnsuccessful {
		closure, err := q.LockDependentClosure(ctx, upstream)
		if err != nil {
			return fmt.Errorf("lock dependents: %w", err)
		}
		if len(closure) == 0 {
			return nil
		}
		message := "an upstream task failed or was cancelled"
		if len(upstream) == 1 {
			message = fmt.Sprintf("upstream task %s failed or was cancelled", upstream[0])
		}
		ids := make([]uuid.UUID, len(closure))
		releases := make([]uuid.UUID, len(closure))
		for n, row := range closure {
			ids[n], releases[n] = row.ID, row.ReleaseID
		}
		// The closure already holds every transitive dependent.
		return e.failQueued(ctx, tx, ids, releases, Failure{Kind: FailureDependencyFailed, Message: message}, false)
	}

	dependents, err := q.LockQueuedDependents(ctx, upstream)
	if err != nil {
		return fmt.Errorf("lock dependents: %w", err)
	}
	if len(dependents) == 0 {
		return nil
	}
	rows, err := q.SatisfyDependencies(ctx, SatisfyDependenciesParams{Upstream: upstream, Dependents: dependents})
	if err != nil {
		return fmt.Errorf("satisfy dependencies: %w", err)
	}
	var tooLarge, tooLargeReleases []uuid.UUID
	var ready []string
	for _, row := range rows {
		if row.UnmetDependencies > 0 {
			continue
		}
		if row.InputBytes > MaxDependentInputBytes {
			tooLarge = append(tooLarge, row.ID)
			tooLargeReleases = append(tooLargeReleases, row.ReleaseID)
			continue
		}
		ready = append(ready, row.ReleaseID.String())
	}
	if len(tooLarge) > 0 {
		if err := e.failQueued(ctx, tx, tooLarge, tooLargeReleases, dependenciesTooLarge, true); err != nil {
			return err
		}
	}
	if err := notifyAll(ctx, tx, database.ChannelClaim, ready); err != nil {
		return err
	}
	return notifyAll(ctx, tx, database.ChannelExecution, ready)
}

// failQueued fails queued tasks with failure and wakes their waiters and
// planning. With cascade their own dependents fail too.
func (e *Execution) failQueued(ctx context.Context, tx pgx.Tx, ids, releases []uuid.UUID, failure Failure, cascade bool) error {
	encoded, err := json.Marshal(failure)
	if err != nil {
		return fmt.Errorf("encode failure: %w", err)
	}
	if err := e.queries.WithTx(tx).FailTasks(ctx, FailTasksParams{Ids: ids, Failure: encoded}); err != nil {
		return fmt.Errorf("fail tasks: %w", err)
	}
	if err := notifyAll(ctx, tx, database.ChannelTask, uuidStrings(ids)); err != nil {
		return err
	}
	if err := notifyAll(ctx, tx, database.ChannelExecution, uuidStrings(releases)); err != nil {
		return err
	}
	if !cascade {
		return nil
	}
	return e.resolveDependents(ctx, tx, ids, upstreamUnsuccessful)
}

func uuidStrings(ids []uuid.UUID) []string {
	out := make([]string, len(ids))
	for n, id := range ids {
		out[n] = id.String()
	}
	return out
}
