package execution

import (
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// APITask is the task as the public API and the edge render it.
func APITask(t Task) apitypes.Task {
	out := apitypes.Task{
		Id: uuid.UUID(t.ID), App: t.App, Function: t.Function, ReleaseId: t.Release, Version: t.Version,
		Status: apitypes.TaskStatus(t.Status), Attempts: t.Attempts, MaxAttempts: t.MaxAttempts,
		ParentTaskId: (*uuid.UUID)(t.Parent), RootTaskId: uuid.UUID(t.Root), ContainerId: (*uuid.UUID)(t.Container),
		NextAttemptAt: t.NextAttemptAt, ScheduledFor: t.ScheduledFor,
		CreatedAt: t.CreatedAt, StartedAt: t.StartedAt, FinishedAt: t.FinishedAt,
	}
	if p := t.Pending; p != nil {
		out.Pending = &apitypes.TaskPendingProgress{
			Reason: apitypes.TaskPendingReason(p.Reason), Message: p.Reason.Message(),
			Since: p.Since, PendingSince: p.PendingSince, ObservedAt: p.ObservedAt,
		}
	}
	if f := t.Failure; f != nil {
		out.Failure = &apitypes.TaskFailure{Kind: apitypes.FailureKind(f.Kind), Message: f.Message}
		if f.Type != "" {
			out.Failure.Type = &f.Type
		}
		if f.Traceback != "" {
			out.Failure.Traceback = &f.Traceback
		}
		if len(f.Exception) > 0 {
			out.Failure.Exception = &f.Exception
		}
	}
	return out
}
