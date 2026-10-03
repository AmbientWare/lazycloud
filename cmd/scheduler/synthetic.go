package main

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

const (
	// syntheticInterval is how often the probes are called, counted from
	// the newest probe task so restarts do not call them more often.
	syntheticInterval = 6 * time.Hour
	// syntheticTick paces the check: a pass submits a due round, or reads
	// the round in flight until every probe finished or syntheticTimeout
	// passed.
	syntheticTick    = time.Minute
	syntheticTimeout = 15 * time.Minute
)

// syntheticProbe is one probe function and the first-call latency it is
// held to.
type syntheticProbe struct {
	function string
	budget   time.Duration
}

// syntheticProbes are the functions of deploy/synthetic/app.py: one vCPU
// from a warm host, 16 vCPU and one T4 from a reserve.
func syntheticProbes() []syntheticProbe {
	return []syntheticProbe{{"cpu_1", 2 * time.Second}, {"cpu_16", 20 * time.Second}, {"gpu_t4", 30 * time.Second}}
}

type submittedProbe struct {
	probe syntheticProbe
	task  execution.TaskID
}

// syntheticCheck calls the platform's probe functions and logs where each
// call's time went: admission and placement, capacity wait, and execution.
// A call past its budget, failed or unfinished logs an error. The probes are
// an app the operator deploys into a workspace it owns; while it is not
// deployed the check submits nothing. A round in flight when the scheduler
// stops goes unreported. Only the loop's pass reads or writes the fields
// after logger.
type syntheticCheck struct {
	exec      *execution.Execution
	workspace identity.WorkspaceID
	app       string
	// leading reports whether this replica runs timed passes; only the
	// leader calls the probes, so replicas never call them twice.
	leading func() bool
	logger  *slog.Logger
	// round is the probes submitted and not yet reported.
	round []submittedProbe
	// next is when a round is due; a failed submission waits for it too.
	next time.Time
	// absent is set once the check logged that the app is not deployed.
	absent bool
}

// newSyntheticCheck reads LAZYCLOUD_SYNTHETIC_APP, <workspace id>/<app>.
func newSyntheticCheck(setting string, exec *execution.Execution, leading func() bool, logger *slog.Logger) (*syntheticCheck, error) {
	workspace, app, ok := strings.Cut(setting, "/")
	id, err := uuid.Parse(workspace)
	if !ok || err != nil || app == "" {
		return nil, fmt.Errorf("LAZYCLOUD_SYNTHETIC_APP %q is not <workspace id>/<app>", setting)
	}
	return &syntheticCheck{exec: exec, workspace: identity.WorkspaceID(id), app: app, leading: leading, logger: logger}, nil
}

func (s *syntheticCheck) pass(ctx context.Context) bool {
	now := time.Now()
	switch {
	case !s.leading():
		return false
	case len(s.round) > 0:
		s.report(ctx, now)
		return false
	case now.Before(s.next):
		return false
	}
	newest, err := s.exec.NewestTasks(ctx, s.workspace, s.app)
	if err != nil {
		s.logger.ErrorContext(ctx, "synthetic check", "error", err)
		return false
	}
	var due []syntheticProbe
	var last time.Time
	for _, p := range syntheticProbes() {
		at, deployed := newest[p.function]
		if !deployed {
			continue
		}
		due = append(due, p)
		if at != nil && at.After(last) {
			last = *at
		}
	}
	if len(due) == 0 {
		if !s.absent {
			s.logger.InfoContext(ctx, "synthetic app not deployed; no probes run", "app", s.app)
		}
		s.absent = true
		return false
	}
	s.absent = false
	if s.next = last.Add(syntheticInterval); now.Before(s.next) {
		return false
	}
	s.next = now.Add(syntheticInterval)
	for _, p := range due {
		tasks, err := s.exec.Submit(ctx, execution.SubmitRequest{
			Workspace: s.workspace, App: s.app, Function: p.function,
			Inputs: []execution.TaskInput{{Payload: execution.Payload{Encoding: execution.EncodingJSON, Data: []byte(`{"args": [], "kwargs": {}}`)}}},
		})
		if err != nil {
			s.logger.ErrorContext(ctx, "synthetic call not submitted", "function", p.function, "error", err)
			continue
		}
		s.round = append(s.round, submittedProbe{probe: p, task: tasks[0].ID})
	}
	return false
}

// report logs each probe of the round that finished or timed out and keeps
// the rest for the next pass.
func (s *syntheticCheck) report(ctx context.Context, now time.Time) {
	ids := make([]execution.TaskID, len(s.round))
	for n, p := range s.round {
		ids[n] = p.task
	}
	latencies, err := s.exec.TaskLatencies(ctx, s.workspace, ids)
	if err != nil {
		s.logger.ErrorContext(ctx, "synthetic check", "error", err)
		return
	}
	var left []submittedProbe
	for _, p := range s.round {
		var l *execution.TaskLatency
		for n := range latencies {
			if latencies[n].Task == p.task {
				l = &latencies[n]
			}
		}
		switch {
		case l == nil:
			s.logger.ErrorContext(ctx, "synthetic call vanished", "function", p.probe.function, "task_id", p.task)
		case l.Status.Terminal():
			s.log(ctx, p.probe, *l)
		case now.Sub(l.Submitted) > syntheticTimeout:
			s.logger.ErrorContext(ctx, "synthetic call unfinished", "function", p.probe.function, "task_id", p.task,
				"status", l.Status, "timeout", syntheticTimeout)
		default:
			left = append(left, p)
		}
	}
	s.round = left
}

func (s *syntheticCheck) log(ctx context.Context, p syntheticProbe, l execution.TaskLatency) {
	split := splitLatency(l)
	attrs := []any{
		"function", p.function, "task_id", l.Task, "status", l.Status, "total_ms", split.total.Milliseconds(),
		"admission_placement_ms", split.admission.Milliseconds(), "capacity_wait_ms", split.capacity.Milliseconds(),
		"execution_ms", split.execution.Milliseconds(), "budget_ms", p.budget.Milliseconds(),
		"instance_type", l.InstanceType, "market", l.Market,
	}
	if l.Status != execution.TaskSucceeded || split.total > p.budget {
		s.logger.ErrorContext(ctx, "synthetic call over budget", attrs...)
		return
	}
	s.logger.InfoContext(ctx, "synthetic call", attrs...)
}

type latencySplit struct {
	total, admission, capacity, execution time.Duration
}

// splitLatency divides a call from submit to finish. Capacity wait runs from
// the container's creation until its host was ready: zero on a host already
// serving, to the finish for a container never placed. Admission and
// placement is the rest of the time until the container was placed;
// execution is the container's start and the call.
func splitLatency(l execution.TaskLatency) latencySplit {
	finished := l.Submitted
	if l.Finished != nil {
		finished = *l.Finished
	}
	// within is t inside [from, finished], or the finish when t is unknown.
	within := func(t *time.Time, from time.Time) time.Time {
		switch {
		case t == nil || t.After(finished):
			return finished
		case t.Before(from):
			return from
		}
		return *t
	}
	created := within(l.ContainerCreated, l.Submitted)
	placed := within(l.Placed, created)
	ready := placed
	if l.Placed != nil {
		ready = created
		if l.HostReady != nil && l.HostReady.After(created) {
			ready = within(l.HostReady, created)
		}
		if ready.After(placed) {
			ready = placed
		}
	}
	return latencySplit{
		total:     finished.Sub(l.Submitted),
		admission: created.Sub(l.Submitted) + placed.Sub(ready),
		capacity:  ready.Sub(created),
		execution: finished.Sub(placed),
	}
}
