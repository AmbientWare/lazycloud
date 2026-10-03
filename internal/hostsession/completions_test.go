package hostsession_test

import (
	"context"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// writeCounter counts the completion transactions a pool runs: each sets
// its attempts' states in one statement.
type writeCounter struct{ n atomic.Int64 }

func (c *writeCounter) TraceQueryStart(ctx context.Context, _ *pgx.Conn, data pgx.TraceQueryStartData) context.Context {
	if strings.Contains(data.SQL, "name: SetAttemptStates") {
		c.n.Add(1)
	}
	return ctx
}

func (*writeCounter) TraceQueryEnd(context.Context, *pgx.Conn, pgx.TraceQueryEndData) {}

// countedHarness serves the host service over a pool whose completion
// transactions writes counts; base is the same database untraced.
func countedHarness(t *testing.T) (h *harness, base *pgxpool.Pool, writes *writeCounter) {
	t.Helper()
	base = dbtest.New(t)
	writes = &writeCounter{}
	cfg := base.Config().Copy()
	cfg.ConnConfig.Tracer = writes
	traced, err := pgxpool.NewWithConfig(t.Context(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(traced.Close)
	return serve(t, traced), base, writes
}

// runningAttempts makes host's container ready with n running attempts.
func (h *harness) runningAttempts(host compute.HostID, n int) (execution.ContainerID, []string) {
	h.t.Helper()
	ctx := h.t.Context()
	_, container := h.startingContainer(host)
	rows, err := h.pool.Query(ctx, `
with ctr as (update containers set state = 'ready', ready_at = now(), slots = $2 where id = $1 returning id, workspace_id, release_id),
     task as (insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts, started_at)
              select ctr.workspace_id, r.workload_id, ctr.release_id, 'running', 1, 1, now()
              from ctr join releases r on r.id = ctr.release_id, generate_series(1, $2)
              returning id),
     att as (insert into attempts (task_id, number, container_id, state, deadline_at)
             select task.id, 1, ctr.id, 'running', now() + interval '1 hour' from task, ctr
             returning id)
select id::text from att`, uuid.UUID(container), n)
	if err != nil {
		h.t.Fatal(err)
	}
	attempts, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		h.t.Fatal(err)
	}
	if _, err := h.pool.Exec(ctx, "update tasks t set current_attempt_id = a.id from attempts a where a.task_id = t.id and a.container_id = $1",
		uuid.UUID(container)); err != nil {
		h.t.Fatal(err)
	}
	return container, attempts
}

func completeRequest(container execution.ContainerID, attempt string) *hostproto.CompleteTaskRequest {
	return &hostproto.CompleteTaskRequest{
		ContainerId: container.String(), AttemptId: attempt,
		Outcome: &hostproto.CompleteTaskRequest_Success{Success: &hostproto.TaskSuccess{
			Encoding: hostproto.PayloadEncoding_PAYLOAD_ENCODING_JSON, Result: []byte("1"),
		}},
	}
}

// Completions that arrive while a write runs share the next transaction
// and its notifications. A stale attempt among them is rejected alone.
func TestABurstOfCompletionsSharesTransactionsAndNotifications(t *testing.T) {
	h, base, writes := countedHarness(t)
	host, ctx := h.enroll()
	const n = 60
	container, attempts := h.runningAttempts(host, n)
	listen, err := base.Acquire(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer listen.Release()
	for _, channel := range []string{"lc_task", "lc_execution"} {
		if _, err := listen.Exec(t.Context(), "listen "+channel); err != nil {
			t.Fatal(err)
		}
	}

	// The first write waits on a lock on its task, so the rest arrive
	// while it runs.
	hold, err := base.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = hold.Rollback(context.WithoutCancel(t.Context())) }()
	if _, err := hold.Exec(t.Context(), "select 1 from tasks t join attempts a on a.task_id = t.id where a.id = $1 for update of t", attempts[0]); err != nil {
		t.Fatal(err)
	}
	errs := make([]error, n+1)
	var calls sync.WaitGroup
	calls.Go(func() { _, errs[0] = h.client.CompleteTask(ctx, completeRequest(container, attempts[0])) })
	for blocked, deadline := 0, time.Now().Add(5*time.Second); blocked == 0; time.Sleep(10 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("the first write never waited on the lock")
		}
		if err := base.QueryRow(t.Context(), `select count(*) from pg_stat_activity
where datname = current_database() and wait_event_type = 'Lock' and query like '%LockTasksForAttempts%'`).Scan(&blocked); err != nil {
			t.Fatal(err)
		}
	}
	for i := 1; i <= n; i++ {
		attempt := uuid.NewString()
		if i < n {
			attempt = attempts[i]
		}
		calls.Go(func() { _, errs[i] = h.client.CompleteTask(ctx, completeRequest(container, attempt)) })
	}
	time.Sleep(300 * time.Millisecond)
	if err := hold.Commit(t.Context()); err != nil {
		t.Fatal(err)
	}
	calls.Wait()

	for i, err := range errs {
		if i == n {
			if status.Code(err) != codes.FailedPrecondition {
				t.Fatalf("stale attempt: got %v, want FailedPrecondition", err)
			}
		} else if err != nil {
			t.Fatalf("completion %d: %v", i, err)
		}
	}
	var succeeded int
	if err := base.QueryRow(t.Context(), `select count(*) from attempts a join tasks t on t.id = a.task_id
where a.container_id = $1 and a.state = 'succeeded' and t.status = 'succeeded'`, uuid.UUID(container)).Scan(&succeeded); err != nil {
		t.Fatal(err)
	}
	notes := 0
	for {
		wait, cancel := context.WithTimeout(t.Context(), 300*time.Millisecond)
		_, err := listen.Conn().WaitForNotification(wait)
		cancel()
		if err != nil {
			break
		}
		notes++
	}
	t.Logf("%d completions: %d transactions, %d task and execution notifications", n+1, writes.n.Load(), notes)
	if succeeded != n {
		t.Fatalf("%d attempts succeeded, want %d", succeeded, n)
	}
	if got := writes.n.Load(); got > 3 {
		t.Fatalf("%d completions took %d transactions, want the burst in one after the first", n+1, got)
	}
	if notes >= n {
		t.Fatalf("%d completions sent %d notifications, want fewer than one each", n, notes)
	}
}

// A completion that arrives alone is written at once, in its own
// transaction, about as fast as writing it directly.
func TestALoneCompletionIsWrittenWithoutWaiting(t *testing.T) {
	h, _, writes := countedHarness(t)
	host, ctx := h.enroll()
	const n = 9
	container, attempts := h.runningAttempts(host, 2*n)
	var direct, queued []time.Duration
	for i, attempt := range attempts {
		before := writes.n.Load()
		start := time.Now()
		if i < n {
			id := execution.AttemptID(uuid.MustParse(attempt))
			err := h.execution.CompleteAttempt(t.Context(), host, container, execution.AttemptOutcome{
				Attempt: id, State: execution.AttemptSucceeded, Result: &execution.Payload{Encoding: execution.EncodingJSON, Data: []byte("1")},
			})
			if err != nil {
				t.Fatal(err)
			}
			direct = append(direct, time.Since(start))
		} else {
			if _, err := h.client.CompleteTask(ctx, completeRequest(container, attempt)); err != nil {
				t.Fatal(err)
			}
			queued = append(queued, time.Since(start))
		}
		if got := writes.n.Load() - before; got != 1 {
			t.Fatalf("completion %d took %d transactions, want 1", i, got)
		}
	}
	slices.Sort(direct)
	slices.Sort(queued)
	t.Logf("median write: direct %s, through CompleteTask %s", direct[n/2], queued[n/2])
	if queued[n/2] > direct[n/2]+25*time.Millisecond {
		t.Fatalf("a lone completion took %s, want about the %s of a direct write", queued[n/2], direct[n/2])
	}
}
