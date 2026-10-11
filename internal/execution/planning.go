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
	"github.com/AmbientWare/lazycloud/internal/cpu"
	"github.com/AmbientWare/lazycloud/internal/database"
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
	// Stopped counts pending containers stopped before placement.
	Stopped int
	// Drained counts containers moved to draining.
	Drained int
	// Cancelled counts queued tasks of stopped workloads.
	Cancelled int
	// RetryAt is the earliest time a release held back after failed starts
	// may start a container; zero when none is held.
	RetryAt time.Time
}

// releasePlan is what one release's decision changed.
type releasePlan struct {
	release   PlanningReleasesRow
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
	r.Stopped += len(p.stopped)
	r.Drained += len(p.drained)
	r.Cancelled += p.cancelled
	if !p.retryAt.IsZero() && (r.RetryAt.IsZero() || p.retryAt.Before(r.RetryAt)) {
		r.RetryAt = p.retryAt
	}
}

// holdStart reports whether the release must wait before another container
// because its last `failures` starts failed, the newest container stopping at
// stopped, and records when it may start one.
func (p *releasePlan) holdStart(failures int32, stopped *time.Time) bool {
	if failures <= 0 || stopped == nil {
		return false
	}
	at := stopped.Add(startRetryDelay(failures))
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

// Plan decides how many containers each release with demand or live
// containers needs, creates pending containers for placement and drains the
// ones no longer needed. Releases are decided in batches, each in one
// transaction holding the planning lock; a release that fails is logged and
// the rest proceed.
func (e *Execution) Plan(ctx context.Context, logger *slog.Logger) (PlanResult, error) {
	var result PlanResult
	after := uuid.Nil
	for {
		batch, err := e.planBatch(ctx, logger, after)
		if err != nil {
			return result, err
		}
		if batch.skipped {
			result.Skipped = true
			return result, nil
		}
		for _, plan := range batch.plans {
			result.add(plan)
			logPlan(ctx, logger, plan)
		}
		if batch.releases < planningBatch {
			return result, nil
		}
		after = batch.last
	}
}

type planBatchResult struct {
	skipped  bool
	plans    []releasePlan
	releases int
	last     uuid.UUID
}

func (e *Execution) planBatch(ctx context.Context, logger *slog.Logger, after uuid.UUID) (planBatchResult, error) {
	var out planBatchResult
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		out = planBatchResult{}
		q := e.queries.WithTx(tx)
		locked, err := q.TryPlanningLock(ctx)
		if err != nil {
			return fmt.Errorf("try planning lock: %w", err)
		}
		if !locked {
			out.skipped = true
			return nil
		}
		releases, err := q.PlanningReleases(ctx, PlanningReleasesParams{AfterID: after, BatchSize: planningBatch})
		if err != nil {
			return fmt.Errorf("list planning releases: %w", err)
		}
		out.releases = len(releases)
		for _, release := range releases {
			out.last = release.ReleaseID
			plan, err := e.planReleaseIsolated(ctx, tx, release)
			if err != nil {
				if ctx.Err() != nil {
					return ctx.Err()
				}
				logger.ErrorContext(ctx, "plan release", "release_id", release.ReleaseID, "error", err)
				continue
			}
			out.plans = append(out.plans, plan)
		}
		return nil
	})
	if err != nil {
		return planBatchResult{}, fmt.Errorf("planning batch: %w", err)
	}
	return out, nil
}

// planReleaseIsolated runs one release's decision in a savepoint, so its
// failure rolls back only its own changes.
func (e *Execution) planReleaseIsolated(ctx context.Context, tx pgx.Tx, release PlanningReleasesRow) (releasePlan, error) {
	savepoint, err := tx.Begin(ctx)
	if err != nil {
		return releasePlan{}, fmt.Errorf("begin savepoint: %w", err)
	}
	plan := releasePlan{release: release}
	if release.Stopping {
		err = e.stopRelease(ctx, savepoint, &plan)
	} else {
		err = e.planRelease(ctx, savepoint, &plan)
	}
	if err != nil {
		return releasePlan{}, errors.Join(err, savepoint.Rollback(ctx))
	}
	if err := savepoint.Commit(ctx); err != nil {
		return releasePlan{}, fmt.Errorf("release savepoint: %w", err)
	}
	return plan, nil
}

func (e *Execution) planRelease(ctx context.Context, tx pgx.Tx, plan *releasePlan) error {
	release := plan.release
	if release.CpuMillis <= 0 || release.MemoryBytes <= 0 {
		return errors.New("release spec has no resources")
	}
	q := e.queries.WithTx(tx)
	hand, err := e.handOff(ctx, tx, plan, release.HandsOff)
	if err != nil {
		return err
	}
	minimum := 0
	// A release that cannot start keeps no warm minimum; admitting a task
	// retries it or fails the task at once.
	if release.Active && !release.LoadFailed && release.StartFailures < startFailureLimit {
		minimum = int(release.MinContainers)
	}
	desired := desiredContainers(
		int(release.QueuedAvailable)+int(release.Running)-hand.busy,
		int(release.TasksPerContainer), minimum, int(release.MaxContainers),
	)
	active, live := hand.counts(release.Pending, release.Starting, release.Ready, release.Draining)

	pairs, grow := hand.starts(active, desired, int(release.MaxContainers)-live)
	if pairs+grow > 0 {
		if plan.holdStart(release.StartFailures, release.LastStoppedAt) {
			return nil
		}
		return e.start(ctx, tx, plan, hand.unpaired[:pairs], pairs+grow, live == 0 && release.QueuedAvailable+release.Running == 0,
			billing.Request{
				Workspace: release.WorkspaceID, GPUs: int(release.GpuCount),
				GPUModels: billingModels(release.GpuModels), Pinned: release.Pinned, Machine: release.Machine,
			},
			CreatePendingContainersParams{
				WorkspaceID: release.WorkspaceID, ReleaseID: release.ReleaseID, Slots: release.Slots,
				CpuMillis: cpu.Millis(release.CpuMillis), MemoryBytes: release.MemoryBytes + mountReserve(release.Mounters),
				GpuCount: release.GpuCount, RateClass: string(billing.RateClassFor(release.Pinned, release.Preemptible)),
			})
	}
	if active > desired {
		excess := active - desired
		if pending := min(excess, int(release.Pending)); pending > 0 {
			stopped, err := q.StopPendingContainers(ctx, StopPendingContainersParams{
				ReleaseID: release.ReleaseID, Count: int32(pending), //nolint:gosec // Bounded by live containers.
			})
			if err != nil {
				return fmt.Errorf("stop pending containers: %w", err)
			}
			plan.stopped = stopped
			excess -= len(stopped)
		}
		if excess > 0 && release.Ready > 0 {
			return e.drainIdle(ctx, tx, plan, excess)
		}
	}
	return nil
}

// handOff is a release's containers that leave their host: its host hands
// Spot-tolerant work back and they can move, or a replacement is ready. A
// busy one, whose attempts finish there, and one with a ready replacement
// drain now; an idle one claims until its replacement is ready. drained
// counts those drained now and busy the busy ones among them; replacing
// counts replacements not ready yet; unpaired are idle ones without one,
// oldest first.
type handOff struct {
	drained, busy, replacing int
	unpaired                 []uuid.UUID
}

// handOff reads the release's leaving containers when leaves says it may
// have any, and drains those that drain now.
func (e *Execution) handOff(ctx context.Context, tx pgx.Tx, plan *releasePlan, leaves bool) (handOff, error) {
	var hand handOff
	if !leaves {
		return hand, nil
	}
	rows, err := e.queries.WithTx(tx).LeavingContainers(ctx, plan.release.ReleaseID)
	if err != nil {
		return hand, fmt.Errorf("read leaving containers: %w", err)
	}
	var drain []uuid.UUID
	for _, r := range rows {
		switch {
		case r.ReplacementReady || r.Busy:
			drain = append(drain, r.ID)
			if r.Busy {
				hand.busy++
			}
		case r.Replaced:
			hand.replacing++
		default:
			hand.unpaired = append(hand.unpaired, r.ID)
		}
	}
	hand.drained = len(drain)
	return hand, e.drainLeaving(ctx, tx, plan, drain)
}

// counts are the release's active containers, which take its demand, and
// its live ones, which its maximum holds. A leaving container and its
// replacement are one of either: the leaving one bills until the
// replacement is ready. Draining containers hold resources, so they count
// against the maximum until their hosts stop them.
func (h handOff) counts(pending, starting, ready, draining int32) (active, live int) {
	running := int(pending + starting + ready)
	return running - h.drained - h.replacing, running + int(draining) - h.replacing
}

// starts are how many containers to start: pairs replace the unpaired
// leaving containers demand still needs, and grow are new ones the room
// under the maximum allows.
func (h handOff) starts(active, desired, room int) (pairs, grow int) {
	pairs = min(len(h.unpaired), max(desired-active+len(h.unpaired), 0))
	return pairs, max(min(desired-active, room), 0)
}

// start admits and creates count containers of the release, the first of
// them replacing replaces. The leaving containers the account admits no
// replacement for drain, so their replacements start once they stop.
func (e *Execution) start(ctx context.Context, tx pgx.Tx, plan *releasePlan, replaces []uuid.UUID, count int, rollout bool,
	admit billing.Request, create CreatePendingContainersParams,
) error {
	admit.Start = count
	grant, err := billing.Admit(ctx, tx, admit)
	if refusedToWait(err) {
		// Its work waits; submission refuses new work.
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
	ctx, span, err := e.scaleUp(ctx, q, create.ReleaseID, grant.Start, rollout)
	if err != nil {
		return err
	}
	defer span.End()
	create.Count, create.Replaces, create.Traceparent = int32(grant.Start), replaces, traceparent(ctx) //nolint:gosec // Bounded by max_containers.
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
func (e *Execution) drainIdle(ctx context.Context, tx pgx.Tx, plan *releasePlan, count int) error {
	q := e.queries.WithTx(tx)
	keepWarm := float64(plan.release.KeepWarmSeconds)
	locked, err := q.LockIdleContainers(ctx, LockIdleContainersParams{
		ReleaseID: plan.release.ReleaseID, KeepWarmSeconds: keepWarm, Count: int32(count), //nolint:gosec // Bounded by live containers.
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
// cancelled. Running tasks finish on their draining containers, unless the
// app or workload is deleted, which cancels them too. Tasks waiting on a
// cancelled task fail.
func (e *Execution) stopRelease(ctx context.Context, tx pgx.Tx, plan *releasePlan) error {
	q := e.queries.WithTx(tx)
	release := plan.release.ReleaseID
	stopped, err := q.StopAllPendingContainers(ctx, release)
	if err != nil {
		return fmt.Errorf("stop pending containers: %w", err)
	}
	plan.stopped = stopped
	drained, err := q.DrainAllContainers(ctx, release)
	if err != nil {
		return fmt.Errorf("drain containers: %w", err)
	}
	for _, row := range drained {
		plan.drained = append(plan.drained, drainedContainer{id: row.ID, host: row.HostID})
	}
	if err := notifyHosts(ctx, tx, plan.drained); err != nil {
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
	if err := recordCallbacks(ctx, q, CallbackCancelled, cancelled, nil); err != nil {
		return err
	}
	if err := database.NotifyAll(ctx, tx, database.ChannelTask, uuidStrings(cancelled)); err != nil {
		return err
	}
	if err := notifyFinished(ctx, tx, cancelled); err != nil {
		return err
	}
	if err := e.resolveDependents(ctx, tx, cancelled, upstreamUnsuccessful); err != nil {
		return err
	}
	if !plan.release.Retiring {
		return nil
	}
	// A deleted app or workload also cancels its running tasks, which kills
	// their slots so the draining containers stop.
	running, err := q.RunningTasksOfRelease(ctx, RunningTasksOfReleaseParams{ReleaseID: release, BatchSize: cancelBatch})
	if err != nil {
		return fmt.Errorf("list running tasks: %w", err)
	}
	for _, task := range running {
		if _, err := e.cancelTask(ctx, tx, nil, TaskID(task)); err != nil {
			return fmt.Errorf("cancel running task %s: %w", task, err)
		}
	}
	plan.cancelled += len(running)
	return nil
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
	release := plan.release.ReleaseID
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
