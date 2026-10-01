package execution

import (
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// APITask is the task as the public API and the edge render it.
func APITask(t Task) apitypes.Task {
	out := apitypes.Task{
		Id: uuid.UUID(t.ID), App: t.App, Function: t.Function, ReleaseId: t.Release,
		Status: apitypes.TaskStatus(t.Status), Attempts: t.Attempts,
		CreatedAt: t.CreatedAt, StartedAt: t.StartedAt, FinishedAt: t.FinishedAt,
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
