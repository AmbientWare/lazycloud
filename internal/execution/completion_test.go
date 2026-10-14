package execution

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

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

// completeBatch reports f's attempts as one batch, the first failed and the
// rest succeeded, with a completion of another host's attempt and a repeat
// of the second in the middle. The two extras are stale and change nothing.
func completeBatch(t *testing.T, e *Execution, f batchFixture) {
	t.Helper()
	var batch []Completion
	for i, attempt := range f.attempts {
		o := AttemptOutcome{Attempt: attempt, State: AttemptSucceeded, Result: &Payload{Encoding: EncodingJSON, Data: []byte(`1`)}}
		if i == 0 {
			o = AttemptOutcome{Attempt: attempt, State: AttemptFailed, Failure: &Failure{Kind: FailureUserError, Message: "bad"}}
		}
		batch = append(batch, Completion{Container: f.container, Outcome: o})
	}
	mid := len(batch) / 2
	stale := []Completion{
		{Container: f.container, Outcome: AttemptOutcome{Attempt: f.otherHosts, State: AttemptSucceeded, Result: &Payload{Encoding: EncodingJSON, Data: []byte(`2`)}}},
		batch[1],
	}
	batch = append(batch[:mid], append(stale, batch[mid:]...)...)
	for i, err := range e.CompleteAttempts(t.Context(), f.host, batch) {
		isStale := i == mid || i == mid+1
		if isStale != errors.Is(err, ErrStaleAttempt) || (!isStale && err != nil) {
			t.Fatalf("completion %d of %d: %v, stale %v", i, len(batch), err, isStale)
		}
	}
}

// A batch of completions succeeds or retries the host's running attempts
// and rejects the stale ones, waking every finished task's waiters, in as
// many statements and notifications at 50 completions as at 5.
func TestABatchRejectsItsStaleCompletionsAndCommitsTheRest(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	e, counter := countedExecution(t, pool)
	spec := `{"retry_policy": {"max_attempts": 2}}`
	small, large := runningBatch(t, pool, spec, 5), runningBatch(t, pool, spec, 50)
	taskNotes := notifications(t, pool, database.ChannelTask)

	completeBatch(t, e, small)
	smallStatements, smallNotes := counter.take(), taskNotes()
	completeBatch(t, e, large)
	largeStatements, largeNotes := counter.take(), taskNotes()
	if largeStatements > smallStatements || len(largeNotes) > len(smallNotes) {
		t.Fatalf("50 completions took %d statements and %d notifications, 5 took %d and %d",
			largeStatements, len(largeNotes), smallStatements, len(smallNotes))
	}

	n := len(large.tasks)
	var succeeded, results, requeued, otherRunning int
	err := pool.QueryRow(t.Context(), `
select count(*) filter (where t.status = 'succeeded'),
       count(r.task_id),
       count(*) filter (where t.status = 'queued' and t.current_attempt_id is null),
       (select count(*) from attempts where id = $2 and state = 'running')
from tasks t left join task_results r on r.task_id = t.id
where t.id = any($1)`, large.tasks, uuid.UUID(large.otherHosts)).Scan(&succeeded, &results, &requeued, &otherRunning)
	if err != nil {
		t.Fatal(err)
	}
	if succeeded != n-1 || results != n-1 || requeued != 1 || otherRunning != 1 {
		t.Fatalf("succeeded %d with %d results, requeued %d, other host's attempt running %d; want %d, %d, 1, 1",
			succeeded, results, requeued, otherRunning, n-1, n-1)
	}
	woken := map[string]bool{}
	for _, payload := range largeNotes {
		for id := range strings.SplitSeq(payload, "\n") {
			woken[id] = true
		}
	}
	for _, task := range large.tasks {
		if !woken[task.String()] {
			t.Fatalf("task %s finished without waking its waiters", task)
		}
	}
}

// A completion the database refuses fails alone: the batch's other
// completions are written.
func TestACompletionThatFailsItsWriteLeavesTheOthersWritten(t *testing.T) {
	t.Parallel()
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
	t.Parallel()
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
