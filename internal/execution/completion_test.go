package execution

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// statementCounter counts the statements a pool sends that contain match.
type statementCounter struct {
	match string
	n     atomic.Int64
}

func (s *statementCounter) TraceQueryStart(ctx context.Context, _ *pgx.Conn, data pgx.TraceQueryStartData) context.Context {
	if strings.Contains(data.SQL, s.match) {
		s.n.Add(1)
	}
	return ctx
}

func (*statementCounter) TraceQueryEnd(context.Context, *pgx.Conn, pgx.TraceQueryEndData) {}

// tracedPool is a second pool on pool's database whose statements counter
// sees.
func tracedPool(t *testing.T, pool *pgxpool.Pool, counter *statementCounter) *pgxpool.Pool {
	t.Helper()
	cfg := pool.Config().Copy()
	cfg.ConnConfig.Tracer = counter
	traced, err := pgxpool.NewWithConfig(t.Context(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(traced.Close)
	return traced
}

// notifications collects the payloads committed on channel.
func notifications(t *testing.T, pool *pgxpool.Pool, channel database.Channel) func() []string {
	t.Helper()
	conn, err := pool.Acquire(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(conn.Release)
	if _, err := conn.Exec(t.Context(), "listen "+pgx.Identifier{string(channel)}.Sanitize()); err != nil {
		t.Fatal(err)
	}
	return func() []string {
		var got []string
		for {
			ctx, cancel := context.WithTimeout(t.Context(), 300*time.Millisecond)
			n, err := conn.Conn().WaitForNotification(ctx)
			cancel()
			if err != nil {
				return got
			}
			got = append(got, n.Payload)
		}
	}
}

type batchFixture struct {
	host       compute.HostID
	container  ContainerID
	tasks      []uuid.UUID
	attempts   []AttemptID
	otherHosts AttemptID
}

// runningBatch inserts n running tasks of one release with their attempts
// on one ready container, and one more running on another host.
func runningBatch(t *testing.T, pool *pgxpool.Pool, spec string, n int) batchFixture {
	t.Helper()
	ctx := t.Context()
	var f batchFixture
	var host, container, other uuid.UUID
	err := pool.QueryRow(ctx, `
with ws as (insert into workspaces (name) values ('ws-' || substr(md5(random()::text), 1, 8)) returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'app', 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, $1::jsonb, sha256('spec'), sha256('src') from wl returning id, workload_id),
     host as (insert into hosts (name, token_hash, state, cpu_millis, memory_bytes)
              select 'h' || n, sha256(random()::text::bytea), 'online', 4000, 1 << 32 from generate_series(1, 2) n
              returning id, name),
     ctr as (insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes)
             select ws.id, rel.id, 'ready', host.id, $2, 1000, 1 << 28 from ws, rel, host returning id, host_id),
     task as (insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts, started_at)
              select ws.id, rel.workload_id, rel.id, 'running', 1, 2, now() from ws, rel, generate_series(1, $2 + 1)
              returning id),
     numbered as (select id, row_number() over (order by id) as n from task),
     att as (insert into attempts (task_id, number, container_id, state, deadline_at)
             select numbered.id, 1, ctr.id, 'running', now() + interval '1 hour'
             from numbered join host on host.name = case when numbered.n <= $2 then 'h1' else 'h2' end
             join ctr on ctr.host_id = host.id
             returning id, container_id)
select (select id from host where name = 'h1'), (select ctr.id from ctr join host on host.id = ctr.host_id where host.name = 'h1'),
       (select att.id from att join ctr on ctr.id = att.container_id join host on host.id = ctr.host_id where host.name = 'h2')`, spec, n).Scan(&host, &container, &other)
	if err != nil {
		t.Fatalf("insert fixture: %v", err)
	}
	dbtest.OwnWorkspaces(t, pool)
	if _, err := pool.Exec(ctx, "update tasks t set current_attempt_id = a.id from attempts a where a.task_id = t.id"); err != nil {
		t.Fatal(err)
	}
	rows, err := pool.Query(ctx, "select task_id, id from attempts where container_id = $1 order by id", container)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var task, attempt uuid.UUID
		if err := rows.Scan(&task, &attempt); err != nil {
			t.Fatal(err)
		}
		f.tasks = append(f.tasks, task)
		f.attempts = append(f.attempts, AttemptID(attempt))
	}
	if rows.Err() != nil {
		t.Fatal(rows.Err())
	}
	f.host, f.container, f.otherHosts = compute.HostID(host), ContainerID(container), AttemptID(other)
	return f
}

// A batch commits in one transaction with one task notification: the
// completions of the host's running attempts succeed or retry, while an
// attempt of another host and a repeated completion are rejected as stale
// and change nothing.
func TestABatchRejectsItsStaleCompletionsAndCommitsTheRest(t *testing.T) {
	base := dbtest.New(t)
	counter := &statementCounter{match: "name: SetAttemptStates"}
	e := NewExecution(tracedPool(t, base, counter))
	const n = 50
	f := runningBatch(t, base, `{"retry_policy": {"max_attempts": 2}}`, n)
	taskNotes := notifications(t, base, database.ChannelTask)

	var batch []Completion
	for i, attempt := range f.attempts {
		o := AttemptOutcome{Attempt: attempt, State: AttemptSucceeded, Result: &Payload{Encoding: EncodingJSON, Data: []byte(`1`)}}
		if i == 0 {
			o = AttemptOutcome{Attempt: attempt, State: AttemptFailed, Failure: &Failure{Kind: FailureUserError, Message: "bad"}}
		}
		batch = append(batch, Completion{Container: f.container, Outcome: o})
	}
	stale := []Completion{
		{Container: f.container, Outcome: AttemptOutcome{Attempt: f.otherHosts, State: AttemptSucceeded, Result: &Payload{Encoding: EncodingJSON, Data: []byte(`2`)}}},
		batch[1],
	}
	batch = append(batch[:n/2], append(stale, batch[n/2:]...)...)
	errs := e.CompleteAttempts(t.Context(), f.host, batch)
	for i, err := range errs {
		isStale := i == n/2 || i == n/2+1
		if isStale != errors.Is(err, ErrStaleAttempt) || (!isStale && err != nil) {
			t.Fatalf("completion %d: %v, stale %v", i, err, isStale)
		}
	}
	if got := counter.n.Load(); got != 1 {
		t.Fatalf("the batch ran %d transactions, want 1", got)
	}

	var succeeded, results, requeued, otherRunning int
	err := base.QueryRow(t.Context(), `
select count(*) filter (where t.status = 'succeeded'),
       count(r.task_id),
       count(*) filter (where t.status = 'queued' and t.current_attempt_id is null),
       (select count(*) from attempts where id = $2 and state = 'running')
from tasks t left join task_results r on r.task_id = t.id
where t.id = any($1)`, f.tasks, uuid.UUID(f.otherHosts)).Scan(&succeeded, &results, &requeued, &otherRunning)
	if err != nil {
		t.Fatal(err)
	}
	if succeeded != n-1 || results != n-1 || requeued != 1 || otherRunning != 1 {
		t.Fatalf("succeeded %d with %d results, requeued %d, other host's attempt running %d; want %d, %d, 1, 1",
			succeeded, results, requeued, otherRunning, n-1, n-1)
	}
	notes := taskNotes()
	woken := map[string]bool{}
	for _, payload := range notes {
		for id := range strings.SplitSeq(payload, "\n") {
			woken[id] = true
		}
	}
	if len(notes) != 1 || len(woken) != n {
		t.Fatalf("%d task notifications woke %d tasks, want 1 waking %d", len(notes), len(woken), n)
	}
}

// A completion the database refuses fails alone: the batch's other
// completions are written.
func TestACompletionThatFailsItsWriteLeavesTheOthersWritten(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := runningBatch(t, pool, `{}`, 3)
	batch := make([]Completion, len(f.attempts))
	for i, attempt := range f.attempts {
		encoding := EncodingJSON
		if i == 1 {
			encoding = "bogus"
		}
		batch[i] = Completion{Container: f.container, Outcome: AttemptOutcome{
			Attempt: attempt, State: AttemptSucceeded, Result: &Payload{Encoding: encoding, Data: []byte(`1`)},
		}}
	}
	errs := e.CompleteAttempts(t.Context(), f.host, batch)
	if errs[0] != nil || errs[1] == nil || errors.Is(errs[1], ErrStaleAttempt) || errs[2] != nil {
		t.Fatalf("errors %v, want only the second to fail its write", errs)
	}
	for i, task := range f.tasks {
		want := string(TaskSucceeded)
		if i == 1 {
			want = string(TaskRunning)
		}
		if status, _ := taskState(t, pool, task); status != want {
			t.Fatalf("task %d is %s, want %s", i, status, want)
		}
	}
}

// Two batches that each finish one upstream task with success and one with
// failure lock their shared dependents in one id-ordered statement, so they
// wait on each other instead of deadlocking. A third transaction holds one
// dependent until both batches queue behind it.
func TestBatchesSharingDependentsDoNotDeadlock(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := runningBatch(t, pool, `{}`, 4)
	ctx := t.Context()
	// x waits on tasks 0 and 3, y on tasks 2 and 1.
	var x, y uuid.UUID
	err := pool.QueryRow(ctx, `
with up as (select * from tasks where id = any($1)),
     dep as (insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts, unmet_dependencies)
             select workspace_id, workload_id, release_id, 'queued', 0, 1, 2 from up limit 2 returning id),
     pair as (select id, row_number() over (order by id) n from dep),
     ins as (insert into task_inputs (task_id, encoding, data) select id, 'json', '1' from dep),
     edges as (insert into task_dependencies (task_id, depends_on)
               select pair.id, u from pair, unnest(case when pair.n = 1 then array[$2::uuid, $5::uuid] else array[$4::uuid, $3::uuid] end) u)
select (select id from pair where n = 1), (select id from pair where n = 2)`,
		f.tasks, f.tasks[0], f.tasks[1], f.tasks[2], f.tasks[3]).Scan(&x, &y)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, "update tasks set max_attempts = 1 where id = any($1)", f.tasks); err != nil {
		t.Fatal(err)
	}
	hold, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = hold.Rollback(context.WithoutCancel(ctx)) }()
	if _, err := hold.Exec(ctx, "select 1 from tasks where id = $1 for update", x); err != nil {
		t.Fatal(err)
	}
	waitForLockWaits := func(want int) {
		t.Helper()
		for deadline := time.Now().Add(5 * time.Second); ; time.Sleep(10 * time.Millisecond) {
			var waiting int
			if err := pool.QueryRow(ctx, `select count(*) from pg_stat_activity
where datname = current_database() and wait_event_type = 'Lock'`).Scan(&waiting); err != nil {
				t.Fatal(err)
			}
			if waiting >= want {
				return
			}
			if time.Now().After(deadline) {
				t.Fatalf("%d transactions wait on a lock, want %d", waiting, want)
			}
		}
	}
	finishPair := func(succeeds, fails int) error {
		return pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
			_, err := e.finishAttempts(ctx, tx, &f.host, nil, []AttemptOutcome{
				{Attempt: f.attempts[succeeds], State: AttemptSucceeded, Result: &Payload{Encoding: EncodingJSON, Data: []byte(`1`)}},
				{Attempt: f.attempts[fails], State: AttemptFailed, Failure: &Failure{Kind: FailureUserError, Message: "bad"}},
			})
			return err
		})
	}
	errs := make(chan error, 2)
	go func() { errs <- finishPair(0, 1) }()
	waitForLockWaits(1)
	go func() { errs <- finishPair(2, 3) }()
	waitForLockWaits(2)
	if err := hold.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := <-errs; err != nil {
			t.Fatalf("finish a batch: %v", err)
		}
	}
	for _, dependent := range []uuid.UUID{x, y} {
		if status, failure := taskState(t, pool, dependent); status != string(TaskFailed) || failure == nil || *failure != string(FailureDependencyFailed) {
			t.Fatalf("dependent is %s with failure %v, want failed by its upstream", status, failure)
		}
	}
}
