package execution

import (
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func TestContainerExitRetriesRunningAttemptsAndIsIdempotent(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := runningAttempt(t, pool, `{"retry_policy": {"max_attempts": 2}}`, 2)
	var container uuid.UUID
	if err := pool.QueryRow(t.Context(), "select container_id from attempts where id = $1", uuid.UUID(f.attempt)).Scan(&container); err != nil {
		t.Fatal(err)
	}
	exit := ContainerExit{Reason: StopHostLost}
	for range 2 {
		err := pgx.BeginFunc(t.Context(), pool, func(tx pgx.Tx) error {
			return e.containerExited(t.Context(), tx, ContainerID(container), exit)
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	var attemptState string
	if err := pool.QueryRow(t.Context(), "select state from attempts where id = $1", uuid.UUID(f.attempt)).Scan(&attemptState); err != nil {
		t.Fatal(err)
	}
	if status, _ := taskState(t, pool, f.task); status != string(TaskQueued) || attemptState != string(AttemptLost) {
		t.Fatalf("task %s, attempt %s; want queued after a lost attempt", status, attemptState)
	}
}

func TestLoadErrorFailsQueuedTasksOfTheRelease(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := runningAttempt(t, pool, `{}`, 1)
	// Finish the running attempt so the release has one queued task left.
	if _, err := pool.Exec(t.Context(), `
update attempts set state = 'succeeded', finished_at = now() where id = $1;`, uuid.UUID(f.attempt)); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(t.Context(), `update tasks set status = 'queued', current_attempt_id = null where id = $1`, f.task); err != nil {
		t.Fatal(err)
	}
	var container uuid.UUID
	if err := pool.QueryRow(t.Context(), "select container_id from attempts where id = $1", uuid.UUID(f.attempt)).Scan(&container); err != nil {
		t.Fatal(err)
	}
	loadError := &Failure{Kind: FailureLoadError, Type: "ModuleNotFoundError", Message: "No module named 'reports'"}
	err := pgx.BeginFunc(t.Context(), pool, func(tx pgx.Tx) error {
		return e.containerExited(t.Context(), tx, ContainerID(container), ContainerExit{Reason: StopLoadError, LoadError: loadError})
	})
	if err != nil {
		t.Fatal(err)
	}
	if status, kind := taskState(t, pool, f.task); status != string(TaskFailed) || kind == nil || *kind != string(FailureLoadError) {
		t.Fatalf("task %s (%v); want failed load_error", status, kind)
	}
}
