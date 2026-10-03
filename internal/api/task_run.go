package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// RunTask admits one task and streams it: the admitted task, its log
// entries as they commit, then its state once it finishes or the wait
// passes. Admission fails as an error response, before the stream starts.
func (s *Server) RunTask(ctx context.Context, req RunTaskRequestObject) (RunTaskResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	submit, err := submitRequest(ctx, ws, req.App, req.Name, []apitypes.TaskInput{req.Body.Input}, req.Body.ReleaseId, req.Body.ParentTaskId)
	if err != nil {
		return nil, err
	}
	tasks, err := s.owners.Execution.Submit(ctx, submit)
	if err != nil {
		return nil, err
	}
	return taskRun{ctx: ctx, server: s, workspace: ws.ID, task: tasks[0], wait: invokeWait(req.Params.WaitSeconds)}, nil
}

// taskRun writes a RunTask response as NDJSON TaskRunEvents, flushing each
// line. Once the status is sent a failure can only end the stream early,
// which the client reads as a task still running.
type taskRun struct {
	// ctx is the request's context; the generated visitor does not pass one.
	ctx       context.Context //nolint:containedctx // Lives for one response.
	server    *Server
	workspace identity.WorkspaceID
	task      execution.Task
	wait      time.Duration
}

func (t taskRun) VisitRunTaskResponse(w http.ResponseWriter) error {
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.WriteHeader(http.StatusOK)
	flush := http.NewResponseController(w)
	enc := json.NewEncoder(w)
	write := func(event apitypes.TaskRunEvent) error {
		if err := enc.Encode(event); err != nil {
			return fmt.Errorf("write task event: %w", err)
		}
		if err := flush.Flush(); err != nil {
			return fmt.Errorf("flush task event: %w", err)
		}
		return nil
	}
	admitted := taskOut(t.task)
	if err := write(apitypes.TaskRunEvent{Task: &admitted}); err != nil {
		return nil //nolint:nilerr // The client is gone.
	}
	exec := t.server.owners.Execution
	if t.wait > 0 {
		follow, cancel := context.WithTimeout(t.ctx, t.wait)
		defer cancel()
		err := exec.StreamLogs(follow, t.server.owners.Listener, t.workspace,
			execution.LogSource{Kind: execution.LogsOfTask, ID: uuid.UUID(t.task.ID)},
			execution.LogQuery{Follow: true, Heartbeat: logHeartbeat},
			func(batch []execution.LogEntry) error {
				if len(batch) == 0 {
					if _, err := w.Write([]byte("\n")); err != nil {
						return fmt.Errorf("write heartbeat: %w", err)
					}
				}
				for _, entry := range batch {
					log := logEntryOut(entry)
					if err := enc.Encode(apitypes.TaskRunEvent{Log: &log}); err != nil {
						return fmt.Errorf("write log entry: %w", err)
					}
				}
				if err := flush.Flush(); err != nil {
					return fmt.Errorf("flush log entries: %w", err)
				}
				return nil
			})
		// The wait passing ends the stream with the task's current state.
		if err != nil && follow.Err() == nil {
			t.warn("task run log stream ended early", err)
			return nil
		}
	}
	task, err := exec.GetTask(t.ctx, t.server.owners.Listener, t.workspace, t.task.ID, 0)
	if err != nil {
		if t.ctx.Err() == nil {
			t.warn("task run read the task", err)
		}
		return nil
	}
	final := apitypes.TaskRunEvent{Task: new(taskOut(task))}
	if task.Status == execution.TaskSucceeded {
		result, err := exec.TaskResult(t.ctx, t.workspace, task.ID)
		if err != nil {
			// The client reads the result itself.
			t.warn("task run read the result", err)
		} else {
			final.Result = new(payloadOut(result))
		}
	}
	_ = write(final) //nolint:errcheck // The client is gone if this fails.
	return nil
}

func (t taskRun) warn(msg string, err error) {
	t.server.logger.WarnContext(t.ctx, msg, "task", uuid.UUID(t.task.ID), "error", err)
}
