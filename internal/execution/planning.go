package execution

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// planningBatch bounds the releases one planning transaction decides.
	planningBatch = 100
	// cancelBatch bounds the queued tasks of a stopped workload cancelled per
	// release and pass; the release stays a candidate until none remain.
	cancelBatch = 1000
	// After consecutive failed starts a release waits startRetryBase before
	// its next container, doubling per failure up to startRetryMax, so a
	// release that cannot start does not cycle containers on its way to
	// startFailureLimit.
	startRetryBase = 5 * time.Second
	startRetryMax  = time.Minute
)

// PlanResult summarizes one planning pass.
type PlanResult struct {
	// Skipped means another planner held the planning lock.
	Skipped bool
	// Created counts new pending containers; placement should run.
	Created int
	// RetryAt is the earliest time a release held back after failed starts
	// may start a container; zero when none is held.
	RetryAt time.Time
}

// releasePlan is what one release's decision changed.
type releasePlan struct {
	release   uuid.UUID
	created   []uuid.UUID
	stopped   []uuid.UUID
	drained   []drainedContainer
	cancelled int
	retryAt   time.Time
}

type drainedContainer struct {
	id   uuid.UUID
	host *uuid.UUID
}

func (r *PlanResult) add(p releasePlan) {
	r.Created += len(p.created)
	if !p.retryAt.IsZero() && (r.RetryAt.IsZero() || p.retryAt.Before(r.RetryAt)) {
		r.RetryAt = p.retryAt
	}
}

// holdStart reports whether the release must wait before another container
// because its last starts failed, the newest container stopping at
// shape.LastStoppedAt, and records when it may start one.
func (p *releasePlan) holdStart(shape ReleaseShape) bool {
	if shape.StartFailures <= 0 || shape.LastStoppedAt == nil {
		return false
	}
	at := shape.LastStoppedAt.Add(startRetryDelay(shape.StartFailures))
	if !time.Now().Before(at) {
		return false
	}
	p.retryAt = at
	return true
}

// startRetryDelay is how long a release waits after its newest container
// stopped, the last of failures >= 1 consecutive failed starts.
func startRetryDelay(failures int32) time.Duration {
	if failures >= 8 {
		return startRetryMax
	}
	return min(startRetryBase<<(failures-1), startRetryMax)
}

// create inserts the release's pending containers with slots each.
func (s ReleaseShape) create(workspace uuid.UUID, slots int32) CreatePendingContainersParams {
	return CreatePendingContainersParams{
		WorkspaceID: workspace, ReleaseID: s.ID, Slots: slots, CpuMillis: s.CpuMillis,
		MemoryBytes: s.MemoryBytes + mountReserve(s.Mounters), GpuCount: s.GpuCount,
		RateClass: string(billing.RateClassFor(s.Pinned, s.Preemptible)), AllowList: []string{},
	}
}

// planReleases decides each release list returns after a cursor, in
// batches of planningBatch. Each batch is one transaction holding the
// planning lock, so counts hold across scheduler replicas, and each release
// is decided in a savepoint: a release that fails is logged and rolls back
// alone while the rest proceed.
func planReleases[R any](ctx context.Context, e *Execution, logger *slog.Logger,
	list func(context.Context, *Queries, uuid.UUID) ([]R, error), shape func(R) ReleaseShape,
	decide func(context.Context, pgx.Tx, R, *releasePlan) error,
) (PlanResult, error) {
	var result PlanResult
	after := uuid.Nil
	for {
		var plans []releasePlan
		var rows []R
		skipped := false
		err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
			plans, skipped = nil, false
			q := e.queries.WithTx(tx)
			locked, err := q.TryPlanningLock(ctx)
			if err != nil {
				return fmt.Errorf("try planning lock: %w", err)
			}
			if !locked {
				skipped = true
				return nil
			}
			if rows, err = list(ctx, q, after); err != nil {
				return fmt.Errorf("list releases: %w", err)
			}
			for _, row := range rows {
				plan := releasePlan{release: shape(row).ID}
				err := pgx.BeginFunc(ctx, tx, func(savepoint pgx.Tx) error { return decide(ctx, savepoint, row, &plan) })
				if err != nil {
					if ctx.Err() != nil {
						return ctx.Err()
					}
					logger.ErrorContext(ctx, "plan release", "release_id", plan.release, "error", err)
					continue
				}
				plans = append(plans, plan)
			}
			return nil
		})
		if err != nil {
			return result, fmt.Errorf("planning batch: %w", err)
		}
		if skipped {
			result.Skipped = true
			return result, nil
		}
		for _, plan := range plans {
			result.add(plan)
			logPlan(ctx, logger, plan)
		}
		if len(rows) < planningBatch {
			return result, nil
		}
		after = shape(rows[len(rows)-1]).ID
	}
}

// Plan decides how many containers each function release with demand or
// live containers needs, creates pending containers for placement and
// drains the ones no longer needed.
func (e *Execution) Plan(ctx context.Context, logger *slog.Logger) (PlanResult, error) {
	return planReleases(ctx, e, logger,
		func(ctx context.Context, q *Queries, after uuid.UUID) ([]PlanningReleasesRow, error) {
			return q.PlanningReleases(ctx, PlanningReleasesParams{AfterID: after, BatchSize: planningBatch})
		},
		func(r PlanningReleasesRow) ReleaseShape { return r.ReleaseShape }, e.planRelease)
}

func (e *Execution) planRelease(ctx context.Context, tx pgx.Tx, release PlanningReleasesRow, plan *releasePlan) error {
	if release.Stopping {
		return e.stopRelease(ctx, tx, release.Retiring, plan)
	}
	shape := release.ReleaseShape
	if shape.CpuMillis <= 0 || shape.MemoryBytes <= 0 {
		return errors.New("release spec has no resources")
	}
	leave, err := e.leaving(ctx, tx, plan, release.Leaving)
	if err != nil {
		return err
	}
	minimum := 0
	// A release that cannot start keeps no warm minimum; admitting a task
	// retries it or fails the task at once.
	if release.Active && !release.LoadFailed && shape.StartFailures < startFailureLimit {
		minimum = int(release.MinContainers)
	}
	desired := desiredContainers(
		int(release.QueuedAvailable)+int(release.Running)-leave.running,
		int(release.TasksPerContainer), minimum, int(release.MaxContainers),
	)
	active, live := leave.counts(release.Pending, release.Starting, release.Ready, release.Draining)

	pairs, grow := leave.starts(active, desired, int(release.MaxContainers)-live)
	if pairs+grow > 0 {
		return e.start(ctx, tx, plan, shape, shape.create(release.WorkspaceID, release.Slots),
			leave.unpaired[:pairs], pairs+grow, live == 0 && release.QueuedAvailable+release.Running == 0)
	}
	return e.trim(ctx, tx, plan, active-desired, release.Pending, func(count int) error {
		if release.Ready == 0 {
			return nil
		}
		return e.drainIdle(ctx, tx, plan, release.KeepWarmSeconds, count)
	})
}

// trim stops up to excess of the release's pending containers, newest
// first because the oldest are closest to placement, and has drain take the
// rest of the excess.
func (e *Execution) trim(ctx context.Context, tx pgx.Tx, plan *releasePlan, excess int, pending int32, drain func(count int) error) error {
	if n := min(excess, int(pending)); n > 0 {
		stopped, err := e.queries.WithTx(tx).StopPendingContainers(ctx, StopPendingContainersParams{
			ReleaseID: plan.release, Count: int32(n), //nolint:gosec // Bounded by live containers.
		})
		if err != nil {
			return fmt.Errorf("stop pending containers: %w", err)
		}
		plan.stopped = stopped
		excess -= len(stopped)
	}
	if excess <= 0 {
		return nil
	}
	return drain(excess)
}

// drainExcess drains count of the release's starting and ready containers,
// idle ones first and then the newest.
func (e *Execution) drainExcess(ctx context.Context, tx pgx.Tx, plan *releasePlan) func(count int) error {
	return func(count int) error {
		rows, err := e.queries.WithTx(tx).DrainExcessContainers(ctx, DrainExcessContainersParams{
			ReleaseID: plan.release, Count: int32(count), //nolint:gosec // Bounded by live containers.
		})
		if err != nil {
			return fmt.Errorf("drain containers: %w", err)
		}
		return plan.record(ctx, tx, len(rows), func(n int) drainedContainer { return drainedContainer{rows[n].ID, rows[n].HostID} })
	}
}

// leaving is a release's containers that leave their host
// (container_leaves). One that runs attempts, which finish there, and one
// with a ready replacement drain now; an idle one claims until its
// replacement is ready. drained counts those drained now and running the
// attempts they run; replacing counts replacements not ready yet; unpaired
// are idle ones without one, oldest first.
type leaving struct {
	drained, running, replacing int
	unpaired                    []uuid.UUID
}

// leaving reads the release's leaving containers when may says it has any,
// and drains those that drain now.
func (e *Execution) leaving(ctx context.Context, tx pgx.Tx, plan *releasePlan, may bool) (leaving, error) {
	var l leaving
	if !may {
		return l, nil
	}
	rows, err := e.queries.WithTx(tx).LeavingContainers(ctx, plan.release)
	if err != nil {
		return l, fmt.Errorf("read leaving containers: %w", err)
	}
	var drain []uuid.UUID
	for _, r := range rows {
		switch {
		case r.ReplacementReady || r.Running > 0:
			drain = append(drain, r.ID)
			l.running += int(r.Running)
		case r.Replaced:
			l.replacing++
		default:
			l.unpaired = append(l.unpaired, r.ID)
		}
	}
	l.drained = len(drain)
	return l, e.drainLeaving(ctx, tx, plan, drain)
}

// counts are the release's active containers, which take its demand, and
// its live ones, which its maximum holds. A leaving container and its
// replacement are one of either: the leaving one bills until the
// replacement is ready. Draining containers hold resources, so they count
// against the maximum until their hosts stop them.
func (l leaving) counts(pending, starting, ready, draining int32) (active, live int) {
	n := int(pending + starting + ready)
	return n - l.drained - l.replacing, n + int(draining) - l.replacing
}

// starts are how many containers to start: pairs replace the unpaired
// leaving containers demand still needs, and grow are new ones the room
// under the maximum allows.
func (l leaving) starts(active, desired, room int) (pairs, grow int) {
	pairs = min(len(l.unpaired), max(desired-active+len(l.unpaired), 0))
	return pairs, max(min(desired-active, room), 0)
}

// start admits and creates count containers of the release, the first of
// them replacing replaces, unless failed starts hold the release back. The
// leaving containers the account admits no replacement for drain, so their
// replacements start once they stop. rollout says only a warm minimum asks
// for the containers.
func (e *Execution) start(ctx context.Context, tx pgx.Tx, plan *releasePlan, shape ReleaseShape,
	create CreatePendingContainersParams, replaces []uuid.UUID, count int, rollout bool,
) error {
	if plan.holdStart(shape) {
		return nil
	}
	grant, err := billing.Admit(ctx, tx, billing.Request{
		Workspace: create.WorkspaceID, Start: count, GPUs: int(shape.GpuCount),
		GPUModels: billingModels(shape.GpuModels), Pinned: shape.Pinned, Machine: shape.Machine,
	})
	if refusedToWait(err) {
		// Its tasks wait and submission refuses new ones; connections wait
		// and time out.
		return nil
	}
	if err != nil {
		return fmt.Errorf("admit containers: %w", err)
	}
	if paired := min(grant.Start, len(replaces)); paired < len(replaces) {
		if err := e.drainLeaving(ctx, tx, plan, replaces[paired:]); err != nil {
			return err
		}
		replaces = replaces[:paired]
	}
	if grant.Start == 0 {
		return nil
	}
	q := e.queries.WithTx(tx)
	ctx, span, err := e.scaleUp(ctx, q, shape.ID, grant.Start, rollout)
	if err != nil {
		return err
	}
	defer span.End()
	create.Count, create.Replaces, create.Traceparent = int32(grant.Start), replaces, telemetry.StoredTraceParent(ctx) //nolint:gosec // Bounded by max_containers.
	created, err := q.CreatePendingContainers(ctx, create)
	if err != nil {
		return fmt.Errorf("create containers: %w", err)
	}
	plan.created = created
	return nil
}

func (e *Execution) drainLeaving(ctx context.Context, tx pgx.Tx, plan *releasePlan, ids []uuid.UUID) error {
	if len(ids) == 0 {
		return nil
	}
	rows, err := e.queries.WithTx(tx).DrainLeavingContainers(ctx, ids)
	if err != nil {
		return fmt.Errorf("drain leaving containers: %w", err)
	}
	return plan.record(ctx, tx, len(rows), func(n int) drainedContainer { return drainedContainer{rows[n].ID, rows[n].HostID} })
}

// record adds n drained containers, row gives each, to p and wakes their
// hosts.
func (p *releasePlan) record(ctx context.Context, tx pgx.Tx, n int, row func(int) drainedContainer) error {
	drained := make([]drainedContainer, n)
	for i := range drained {
		drained[i] = row(i)
	}
	p.drained = append(p.drained, drained...)
	return notifyHosts(ctx, tx, drained)
}

// refusedToWait reports an admission refusal planning waits out rather
// than fails on: the account cannot pay, or the fleet offers no GPU model
// the work accepts.
func refusedToWait(err error) bool {
	var payment *billing.PaymentRequiredError
	var gpu *billing.GPUUnavailableError
	return errors.As(err, &payment) || errors.As(err, &gpu)
}

// desiredContainers is clamp(ceil(demand / tasksPerContainer), minimum,
// maximum). The maximum wins over a larger minimum.
func desiredContainers(demand, tasksPerContainer, minimum, maximum int) int {
	tasksPerContainer = max(tasksPerContainer, 1)
	needed := (demand + tasksPerContainer - 1) / tasksPerContainer
	return max(min(max(needed, minimum), maximum), 0)
}

// drainIdle moves up to count idle ready containers to draining. Locking and
// draining are separate statements: the drain's snapshot is taken after the
// locks, so it sees any attempt a claim committed before them, and SKIP
// LOCKED passes over containers a claim holds FOR SHARE.
func (e *Execution) drainIdle(ctx context.Context, tx pgx.Tx, plan *releasePlan, keepWarmSeconds int32, count int) error {
	q := e.queries.WithTx(tx)
	keepWarm := float64(keepWarmSeconds)
	locked, err := q.LockIdleContainers(ctx, LockIdleContainersParams{
		ReleaseID: plan.release, KeepWarmSeconds: keepWarm, Count: int32(count), //nolint:gosec // Bounded by live containers.
	})
	if err != nil {
		return fmt.Errorf("lock idle containers: %w", err)
	}
	if len(locked) == 0 {
		return nil
	}
	rows, err := q.DrainIdleContainers(ctx, DrainIdleContainersParams{Ids: locked, KeepWarmSeconds: keepWarm})
	if err != nil {
		return fmt.Errorf("drain idle containers: %w", err)
	}
	return plan.record(ctx, tx, len(rows), func(n int) drainedContainer { return drainedContainer{rows[n].ID, rows[n].HostID} })
}

// stopRelease winds down a release whose workload is stopped or whose app is
// paused: pending containers stop, the rest drain, and queued tasks are
// cancelled. Running tasks finish on their draining containers, unless
// retiring says the app or workload is deleted, which cancels them too.
// Tasks waiting on a cancelled task fail.
func (e *Execution) stopRelease(ctx context.Context, tx pgx.Tx, retiring bool, plan *releasePlan) error {
	q := e.queries.WithTx(tx)
	release := plan.release
	stopped, err := q.StopAllPendingContainers(ctx, release)
	if err != nil {
		return fmt.Errorf("stop pending containers: %w", err)
	}
	plan.stopped = stopped
	drained, err := q.DrainAllContainers(ctx, release)
	if err != nil {
		return fmt.Errorf("drain containers: %w", err)
	}
	if err := plan.record(ctx, tx, len(drained), func(n int) drainedContainer { return drainedContainer{drained[n].ID, drained[n].HostID} }); err != nil {
		return err
	}
	queued, err := e.lockQueuedWithDependents(ctx, q, release, cancelBatch)
	if err != nil {
		return err
	}
	cancelled, err := q.CancelQueuedTasks(ctx, queued)
	if err != nil {
		return fmt.Errorf("cancel queued tasks: %w", err)
	}
	plan.cancelled = len(cancelled)
	if err := e.finishTasks(ctx, tx, CallbackCancelled, cancelled, true); err != nil {
		return err
	}
	if !retiring {
		return nil
	}
	// A deleted app or workload also cancels its running tasks, which kills
	// their slots so the draining containers stop.
	running, err := q.CancelRunningTasksOfRelease(ctx, CancelRunningTasksOfReleaseParams{ReleaseID: release, BatchSize: cancelBatch})
	if err != nil {
		return fmt.Errorf("cancel running tasks: %w", err)
	}
	if len(running) == 0 {
		return nil
	}
	plan.cancelled += len(running)
	ids := make([]uuid.UUID, len(running))
	outcomes := make([]AttemptOutcome, 0, len(running))
	hosts := make([]string, 0, len(running))
	for n, task := range running {
		ids[n] = task.ID
		if task.CurrentAttemptID != nil {
			outcomes = append(outcomes, AttemptOutcome{Attempt: AttemptID(*task.CurrentAttemptID), State: AttemptCancelled})
		}
		if task.HostID != nil {
			hosts = append(hosts, task.HostID.String())
		}
	}
	// The tasks are already cancelled, so this only records the attempts'
	// outcomes.
	if _, err := e.finishAttempts(ctx, tx, nil, nil, outcomes); err != nil {
		return err
	}
	if err := database.NotifyAll(ctx, tx, database.ChannelHost, hosts); err != nil {
		return err
	}
	// Dependents lock after the attempts, keeping task then attempt order.
	if err := e.finishTasks(ctx, tx, CallbackCancelled, ids, true); err != nil {
		return err
	}
	return database.Notify(ctx, tx, database.ChannelExecution, release.String())
}

func notifyHosts(ctx context.Context, tx pgx.Tx, drained []drainedContainer) error {
	hosts := make([]string, 0, len(drained))
	for _, container := range drained {
		if container.host != nil {
			hosts = append(hosts, container.host.String())
		}
	}
	return database.NotifyAll(ctx, tx, database.ChannelHost, hosts)
}

func logPlan(ctx context.Context, logger *slog.Logger, plan releasePlan) {
	release := plan.release
	for _, id := range plan.created {
		logger.InfoContext(ctx, "container requested", "release_id", release, "container_id", id)
	}
	for _, id := range plan.stopped {
		logger.InfoContext(ctx, "pending container stopped", "release_id", release, "container_id", id)
	}
	for _, container := range plan.drained {
		logger.InfoContext(ctx, "container draining", "release_id", release, "container_id", container.id, "host_id", container.host)
	}
	if plan.cancelled > 0 {
		logger.InfoContext(ctx, "queued tasks cancelled", "release_id", release, "count", plan.cancelled)
	}
}

// HasLiveWork reports whether a queued or running task or a live container
// exists, the state that time alone can advance; without it the scheduler's
// timed passes have nothing to do until a notification.
func (e *Execution) HasLiveWork(ctx context.Context) (bool, error) {
	live, err := e.queries.HasLiveWork(ctx)
	if err != nil {
		return false, fmt.Errorf("probe live work: %w", err)
	}
	return live, nil
}
