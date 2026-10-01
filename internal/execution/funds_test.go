package execution

import (
	"errors"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// unwaive puts f's workspace owner on Free without a saved card: ten
// concurrent containers and the balance given.
func unwaive(t *testing.T, pool *pgxpool.Pool, f releaseFixture, balance int64) {
	t.Helper()
	exec(t, pool, `
with owner as (select user_id from workspace_members where workspace_id = $1 and role = 'owner'),
     account as (update billing_accounts set complimentary_since = null where user_id in (select user_id from owner))
update billing_balances set balance_nanos = $2, due = false, recheck_at = now() + interval '1 day'
where user_id in (select user_id from owner)`, f.workspace, balance)
}

func TestPlanningStartsWhatTheAccountMayRun(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := newRelease(t, pool, `{"autoscaler": {"max_containers": 40}}`)
	unwaive(t, pool, f, billing.NanosPerUSD)
	queueTasks(t, pool, f, 40, 0)

	plan(t, e)
	if got := containerStates(t, pool, f.release)[ContainerPending]; got != 10 {
		t.Fatalf("planning started %d containers, want the no-card limit of 10", got)
	}
	// With no credit left planning starts nothing more, and the tasks wait.
	exec(t, pool, "update containers set state = 'stopped', stopped_at = now(), stop_reason = 'stopped' where release_id = $1", f.release)
	unwaive(t, pool, f, 0)
	if result := plan(t, e); result.Created != 0 {
		t.Fatalf("planning started %d containers without credit", result.Created)
	}
}

func TestPlanningRecordsWhatBillingPrices(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := newRelease(t, pool, `{"resources": {"cpu_millis": 1000, "memory_mib": 512, "gpu": ["L4"]},
		"placement": {"region": "us-east", "preemptible": false}}`)
	queueTasks(t, pool, f, 1, 0)
	plan(t, e)
	var cards int
	var class string
	if err := pool.QueryRow(t.Context(), "select gpu_count, rate_class from containers where release_id = $1", f.release).Scan(&cards, &class); err != nil {
		t.Fatal(err)
	}
	if cards != 1 || class != "pinned_non_preemptible" {
		t.Fatalf("container holds %d GPUs at %s", cards, class)
	}
}

func TestSubmitNeedsCreditAndRoomForColdWork(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"handler": "reports:summarize", "max_pending_tasks": 100,
		"resources": {"cpu_millis": 1000, "memory_mib": 512}}`)
	rf := releaseFixture{workspace: uuid.UUID(f.workspace), release: f.release}
	submit := func() error {
		_, err := e.Submit(t.Context(), SubmitRequest{Workspace: f.workspace, App: "reports", Function: "summarize",
			Inputs: []TaskInput{{Payload: Payload{Encoding: EncodingJSON, Data: []byte("[1]")}}}})
		return err
	}
	unwaive(t, pool, rf, 0)
	var unpaid *billing.PaymentRequiredError
	if err := submit(); !errors.As(err, &unpaid) {
		t.Fatalf("submit without credit: %v", err)
	}
	// The account runs ten containers of another workspace: cold work here
	// is refused with the limit named.
	unwaive(t, pool, rf, billing.NanosPerUSD)
	other := identity.WorkspaceID(uuid.New())
	exec(t, pool, `
with ws as (insert into workspaces (id, name) values ($1, 'other') returning id),
     m as (insert into workspace_members (workspace_id, user_id, role)
           select ws.id, o.user_id, 'owner' from ws, workspace_members o where o.workspace_id = $2 and o.role = 'owner'),
     app as (insert into apps (workspace_id, name, state) select id, 'busy', 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, '{}'::jsonb, sha256('busy'), sha256('src') from wl returning id)
insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes)
select ws.id, rel.id, 'pending', 1, 1000, 1 << 29 from ws, rel, generate_series(1, 10)`, uuid.UUID(other), uuid.UUID(f.workspace))
	var limit *billing.LimitError
	if err := submit(); !errors.As(err, &limit) {
		t.Fatalf("cold submit at the limit: %v", err)
	}
	// Warm work queues behind its own containers.
	placedContainer(t, pool, f, ContainerReady, 1)
	if err := submit(); err != nil {
		t.Fatalf("warm submit at the limit: %v", err)
	}
}

func TestStopUnfundedStopsEveryRunningContainerAndSkipsDraining(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := newRelease(t, pool, `{}`)
	host := newHost(t, pool)
	exec(t, pool, `insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
		select $1, $2, 'ready', $3, 1, 1000, 1 << 29, now(), now() from generate_series(1, 150)`, f.workspace, f.release, host)
	exec(t, pool, `insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
		values ($1, $2, 'draining', $3, 1, 1000, 1 << 29, now(), now())`, f.workspace, f.release, host)
	stopped, err := e.StopUnfunded(t.Context(), identity.WorkspaceID(f.workspace), "the account's credit ran out")
	if err != nil || stopped != 150 {
		t.Fatalf("stopped %d: %v", stopped, err)
	}
	if states := containerStates(t, pool, f.release); states[ContainerReady] != 0 || states[ContainerDraining] != 151 {
		t.Fatalf("states %v", states)
	}
}

func TestUnfundedAccountsStopTheirContainers(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := newRelease(t, pool, `{}`)
	unwaive(t, pool, f, billing.NanosPerUSD)
	host := newHost(t, pool)
	container := readyContainer(t, pool, f, host, 0)
	attempt := attemptOn(t, pool, f, container, 0)

	// Credit runs out while the container runs: the metering pass records
	// it live, and the rollup leaves nothing to spend.
	exec(t, pool, `update billing_balances set balance_nanos = 0, live_containers = 1
		where user_id = (select user_id from workspace_members where workspace_id = $1 and role = 'owner')`, f.workspace)
	b := billing.NewBilling(pool, billing.Config{}, discardLogger())
	unfunded, err := b.UnfundedAccounts(t.Context())
	if err != nil || len(unfunded) != 1 || len(unfunded[0].Workspaces) != 1 {
		t.Fatalf("unfunded accounts %+v, %v", unfunded, err)
	}
	stopped, err := e.StopUnfunded(t.Context(), identity.WorkspaceID(unfunded[0].Workspaces[0]), unfunded[0].Reason)
	if err != nil || stopped != 1 {
		t.Fatalf("stopped %d: %v", stopped, err)
	}
	if state := containerState(t, pool, container); state != ContainerDraining {
		t.Fatalf("container %s, want draining", state)
	}
	var attemptState string
	if err := pool.QueryRow(t.Context(), "select state from attempts where id = $1", attempt).Scan(&attemptState); err != nil || attemptState != string(AttemptLost) {
		t.Fatalf("attempt %s: %v", attemptState, err)
	}
}
