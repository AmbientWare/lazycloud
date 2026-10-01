// Package schedules owns cron occurrences: when each scheduled function is
// next due and the task each occurrence admitted. The expression itself is
// part of the release spec control deploys; deploy writes it here through
// Apply in the same transaction. Occurrences enter through execution
// admission, in the transaction that advances the schedule.
package schedules

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// fireBatch bounds the schedules one transaction claims.
const fireBatch = 100

// noArguments is the cloudpickled `{"args": [], "kwargs": {}}` every
// occurrence passes, as in the reference: a scheduled function takes no
// arguments, and a pickled input keeps a cloudpickled result, so any return
// value is kept. It is plain protocol-4 pickle of builtins.
var noArguments = []byte("\x80\x04\x95\x19\x00\x00\x00\x00\x00\x00\x00}\x94(\x8c\x04args\x94]\x94\x8c\x06kwargs\x94}\x94u.") //nolint:gochecknoglobals // constant bytes

// ErrInvalidCursor means a list cursor was not produced by List.
var ErrInvalidCursor = errors.New("invalid cursor")

// Apply makes workload's schedule match expression, the normalized cron of
// its active release, inside the deploy transaction tx. An unchanged
// expression keeps its next and last runs; a new one starts from now; nil
// removes the schedule.
func Apply(ctx context.Context, tx pgx.Tx, workload uuid.UUID, expression *string) error {
	q := New(tx)
	if expression == nil {
		if err := q.DeleteSchedule(ctx, workload); err != nil {
			return fmt.Errorf("delete schedule: %w", err)
		}
		return nil
	}
	current, err := q.ScheduleExpression(ctx, workload)
	if err == nil && current == *expression {
		return nil
	}
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return fmt.Errorf("read schedule: %w", err)
	}
	cron, err := ParseCron(*expression)
	if err != nil {
		return err
	}
	next, ok := cron.Next(time.Now())
	if !ok {
		return &InvalidCronError{Message: "invalid cron expression: " + *expression}
	}
	if err := q.ReplaceSchedule(ctx, ReplaceScheduleParams{WorkloadID: workload, Expression: *expression, NextFireAt: next}); err != nil {
		return fmt.Errorf("replace schedule: %w", err)
	}
	return nil
}

// Schedules runs due occurrences and reports schedule state.
type Schedules struct {
	pool      *pgxpool.Pool
	queries   *Queries
	execution *execution.Execution
}

// NewSchedules returns the schedules owner, admitting through e.
func NewSchedules(pool *pgxpool.Pool, e *execution.Execution) *Schedules {
	return &Schedules{pool: pool, queries: New(pool), execution: e}
}

// FireResult counts one Fire pass.
type FireResult struct {
	Admitted int
	Skipped  int
}

// Fire admits every due occurrence, in batches of fireBatch. Each schedule
// commits with its occurrence: the task, when admission accepts it, and the
// next due time, computed from now so missed occurrences collapse into one.
// A rejected or failed admission records why and still advances, so one
// broken schedule never holds back the rest.
func (s *Schedules) Fire(ctx context.Context, logger *slog.Logger) (FireResult, error) {
	var total FireResult
	for {
		result, claimed, err := s.fireBatch(ctx, logger)
		total.Admitted += result.Admitted
		total.Skipped += result.Skipped
		if err != nil || claimed < fireBatch {
			return total, err
		}
	}
}

func (s *Schedules) fireBatch(ctx context.Context, logger *slog.Logger) (FireResult, int, error) {
	var result FireResult
	var claimed int
	err := pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		result = FireResult{}
		q := s.queries.WithTx(tx)
		due, err := q.LockDueSchedules(ctx, fireBatch)
		if err != nil {
			return fmt.Errorf("lock due schedules: %w", err)
		}
		claimed = len(due)
		for _, row := range due {
			task, reason := s.admit(ctx, tx, row)
			next, err := nextAfter(row.Expression, row.Now)
			if err != nil {
				return err
			}
			params := RecordOccurrenceParams{WorkloadID: row.WorkloadID, NextFireAt: next, FiredAt: &row.NextFireAt}
			if task != nil {
				id := uuid.UUID(*task)
				params.TaskID = &id
				result.Admitted++
				logger.InfoContext(ctx, "schedule admitted task", "workload_id", row.WorkloadID, "scheduled_for", row.NextFireAt, "task_id", id)
			} else {
				params.Error = &reason
				result.Skipped++
				logger.InfoContext(ctx, "schedule admitted no task", "workload_id", row.WorkloadID, "scheduled_for", row.NextFireAt, "reason", reason)
			}
			if err := q.RecordOccurrence(ctx, params); err != nil {
				return fmt.Errorf("record occurrence: %w", err)
			}
		}
		return nil
	})
	if err != nil {
		return FireResult{}, 0, fmt.Errorf("fire schedules: %w", err)
	}
	return result, claimed, nil
}

// admit submits one occurrence under a savepoint. It returns the task, or
// why there is none.
func (s *Schedules) admit(ctx context.Context, tx pgx.Tx, row LockDueSchedulesRow) (*execution.TaskID, string) {
	scheduledFor := row.NextFireAt
	var task *execution.TaskID
	err := pgx.BeginFunc(ctx, tx, func(sp pgx.Tx) error {
		tasks, err := s.execution.SubmitInTx(ctx, sp, execution.SubmitRequest{
			Workspace:    identity.WorkspaceID(row.WorkspaceID),
			App:          row.AppName,
			Function:     row.FunctionName,
			Inputs:       []execution.Payload{{Encoding: execution.EncodingCloudpickle, Data: noArguments}},
			ScheduledFor: &scheduledFor,
		})
		if err != nil {
			return err
		}
		task = &tasks[0].ID
		return nil
	})
	var tooMany *execution.TooManyPendingError
	switch {
	case err == nil:
		return task, ""
	case errors.Is(err, execution.ErrNotAccepting):
		return nil, "the function is stopped or its app is paused"
	case errors.As(err, &tooMany):
		return nil, tooMany.Error()
	case errors.Is(err, execution.ErrNotFound):
		return nil, "the function has no active release"
	}
	return nil, "admission failed: " + err.Error()
}

func nextAfter(expression string, now time.Time) (time.Time, error) {
	cron, err := ParseCron(expression)
	if err != nil {
		return time.Time{}, fmt.Errorf("stored schedule %q: %w", expression, err)
	}
	next, ok := cron.Next(now)
	if !ok {
		return time.Time{}, fmt.Errorf("stored schedule %q never fires", expression)
	}
	return next, nil
}

// Schedule is a function's schedule as users see it. Times are UTC.
type Schedule struct {
	Expression string
	NextRunAt  time.Time
	LastRunAt  *time.Time
	LastTask   *uuid.UUID
	LastError  *string
}

// ScheduledFunction is a schedule and the function it runs.
type ScheduledFunction struct {
	App      string
	Function string
	Schedule Schedule
}

// ForFunction returns the function's schedule, or nil when it has none.
func (s *Schedules) ForFunction(ctx context.Context, workspace identity.WorkspaceID, app, function string) (*Schedule, error) {
	row, err := s.queries.FunctionSchedule(ctx, FunctionScheduleParams{WorkspaceID: uuid.UUID(workspace), AppName: app, Name: function})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil //nolint:nilnil // An unscheduled function has no schedule.
	}
	if err != nil {
		return nil, fmt.Errorf("read schedule: %w", err)
	}
	return &Schedule{
		Expression: row.Expression, NextRunAt: row.NextFireAt,
		LastRunAt: row.LastFiredAt, LastTask: row.LastTaskID, LastError: row.LastError,
	}, nil
}

type listCursor struct {
	App      string `json:"a"`
	Function string `json:"f"`
}

// List returns up to limit schedules of workspace ordered by app and
// function after cursor, and the next page's cursor when more follow.
func (s *Schedules) List(ctx context.Context, workspace identity.WorkspaceID, cursor string, limit int) ([]ScheduledFunction, string, error) {
	var after listCursor
	if cursor != "" {
		raw, err := base64.RawURLEncoding.DecodeString(cursor)
		if err != nil || json.Unmarshal(raw, &after) != nil {
			return nil, "", ErrInvalidCursor
		}
	}
	rows, err := s.queries.ListSchedules(ctx, ListSchedulesParams{
		WorkspaceID: uuid.UUID(workspace), AfterApp: after.App, AfterFunction: after.Function,
		MaxRows: int32(limit) + 1, //nolint:gosec // The API caps limit at 1000.
	})
	if err != nil {
		return nil, "", fmt.Errorf("list schedules: %w", err)
	}
	next := ""
	if len(rows) > limit {
		rows = rows[:limit]
		last := rows[limit-1]
		raw, err := json.Marshal(listCursor{App: last.AppName, Function: last.FunctionName})
		if err != nil {
			return nil, "", fmt.Errorf("encode cursor: %w", err)
		}
		next = base64.RawURLEncoding.EncodeToString(raw)
	}
	out := make([]ScheduledFunction, len(rows))
	for n, row := range rows {
		out[n] = ScheduledFunction{App: row.AppName, Function: row.FunctionName, Schedule: Schedule{
			Expression: row.Expression, NextRunAt: row.NextFireAt,
			LastRunAt: row.LastFiredAt, LastTask: row.LastTaskID, LastError: row.LastError,
		}}
	}
	return out, next, nil
}
