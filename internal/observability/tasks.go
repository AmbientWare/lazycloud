package observability

import (
	"context"
	"errors"
	"fmt"
	"sort"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// TaskTimeline lists the task's submission, each attempt's start and
// outcome, retries and its final status, from the task and attempt rows.
func (o *Observability) TaskTimeline(ctx context.Context, ws identity.WorkspaceID, id execution.TaskID) (apitypes.TaskTimeline, error) {
	task, err := o.queries.TaskForTimeline(ctx, TaskForTimelineParams{ID: uuid.UUID(id), WorkspaceID: uuid.UUID(ws)})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.TaskTimeline{}, ErrNotFound
	}
	if err != nil {
		return apitypes.TaskTimeline{}, fmt.Errorf("read task: %w", err)
	}
	attempts, err := o.queries.TaskAttempts(ctx, uuid.UUID(id))
	if err != nil {
		return apitypes.TaskTimeline{}, fmt.Errorf("read attempts: %w", err)
	}
	status := apitypes.TaskStatus(task.Status)
	events := []apitypes.TaskEvent{{Kind: apitypes.TaskEventKindSubmitted, At: task.CreatedAt}}
	for i, a := range attempts {
		number := int(a.Number)
		attemptID, container := a.ID, a.ContainerID
		events = append(events, apitypes.TaskEvent{
			Kind: apitypes.TaskEventKindAttemptStarted, At: a.StartedAt,
			Attempt: &number, AttemptId: &attemptID, ContainerId: &container,
		})
		if a.FinishedAt == nil {
			continue
		}
		outcome := apitypes.AttemptOutcome(a.State)
		events = append(events, apitypes.TaskEvent{
			Kind: apitypes.TaskEventKindAttemptFinished, At: *a.FinishedAt,
			Attempt: &number, AttemptId: &attemptID, ContainerId: &container, Outcome: &outcome,
		})
		retried := i+1 < len(attempts)
		waiting := i+1 == len(attempts) && status == apitypes.TaskStatusQueued
		if retried || waiting {
			next := number + 1
			retry := apitypes.TaskEvent{Kind: apitypes.TaskEventKindRetryScheduled, At: *a.FinishedAt, Attempt: &next}
			if waiting {
				due := task.AvailableAt
				retry.DueAt = &due
			}
			events = append(events, retry)
		}
	}
	if task.FinishedAt != nil {
		events = append(events, apitypes.TaskEvent{Kind: apitypes.TaskEventKindFinished, At: *task.FinishedAt, Status: &status})
	}
	sort.SliceStable(events, func(i, j int) bool { return events[i].At.Before(events[j].At) })
	return apitypes.TaskTimeline{TaskId: uuid.UUID(id), Status: status, Events: events}, nil
}

// maxCallGraph bounds the tasks one call graph returns.
const maxCallGraph = 2000

// TaskCallGraph returns every task of id's call graph: its root and the
// tasks naming that root, oldest first.
func (o *Observability) TaskCallGraph(ctx context.Context, ws identity.WorkspaceID, id execution.TaskID) (apitypes.TaskCallGraph, error) {
	root, err := o.queries.TaskRoot(ctx, TaskRootParams{ID: uuid.UUID(id), WorkspaceID: uuid.UUID(ws)})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.TaskCallGraph{}, ErrNotFound
	}
	if err != nil {
		return apitypes.TaskCallGraph{}, fmt.Errorf("read task root: %w", err)
	}
	rows, err := o.queries.CallGraph(ctx, CallGraphParams{WorkspaceID: uuid.UUID(ws), RootID: root, MaxRows: maxCallGraph + 1})
	if err != nil {
		return apitypes.TaskCallGraph{}, fmt.Errorf("read call graph: %w", err)
	}
	out := apitypes.TaskCallGraph{RootTaskId: root, Truncated: len(rows) > maxCallGraph, Nodes: []apitypes.CallGraphNode{}}
	if out.Truncated {
		rows = rows[:maxCallGraph]
	}
	for _, r := range rows {
		node := apitypes.CallGraphNode{
			TaskId: r.ID, ParentTaskId: r.ParentTaskID, App: r.AppName, Function: r.FunctionName,
			Status: apitypes.TaskStatus(r.Status), CreatedAt: r.CreatedAt, StartedAt: r.StartedAt,
			FinishedAt: r.FinishedAt, DependsOn: r.DependsOn,
		}
		if node.DependsOn == nil {
			node.DependsOn = []uuid.UUID{}
		}
		if len(r.ContainerIds) > 0 {
			container := r.ContainerIds[0]
			node.ContainerId = &container
		}
		out.Nodes = append(out.Nodes, node)
	}
	return out, nil
}
