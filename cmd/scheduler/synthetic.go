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
	// the newest probe task, so restarts do not call them more often.
	syntheticInterval = 6 * time.Hour
	// syntheticTick paces the check: it submits a due round, then reads the
	// round until every probe finished or syntheticTimeout passed.
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
// an app the operator deploys into a workspace it owns; a round in flight
// when the scheduler stops goes unreported.
type syntheticCheck struct {
	exec      *execution.Execution
	workspace identity.WorkspaceID
	app       string
	// leading reports whether this replica runs timed passes; only the
	// leader calls the probes, so replicas never call them twice.
	leading func() bool
	logger  *slog.Logger
	// round is the probes submitted and not yet reported; only the loop's
	// pass reads or writes it.
	round []submittedProbe
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
	if !s.leading() {
		return false
	}
	if len(s.round) > 0 {
		s.report(ctx, time.Now())
		return false
	}
	newest, err := s.exec.NewestAppTask(ctx, s.workspace, s.app)
	if err != nil {
		s.logger.ErrorContext(ctx, "synthetic check", "error", err)
		return false
	}
	if newest != nil && time.Since(*newest) < syntheticInterval {
		return false
	}
	for _, p := range syntheticProbes() {
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

// splitLatency divides a finished call from submit to finish. Capacity wait
// runs from the container's creation until its host became ready, zero on a
// host already serving, and to the finish for a container never placed;
// admission and placement are the time before and after it until the
// container was placed; execution is the container's start and the call.
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
