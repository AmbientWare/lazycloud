package control

import (
	"log/slog"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/execution"
)

// A function release that stopped at the start failure limit stays down
// until a user acts: a resume and a redeploy of the same code each give it a
// fresh start, which its warm minimum then asks for.
func TestResumeAndUnchangedRedeployRestartAStoppedRelease(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	e := execution.NewExecution(pool)
	spec := function("summarize")
	spec.Autoscaler = &apitypes.Autoscaler{MinContainers: new(1)}
	release := deploy(t, c, ws, false, spec).Releases[0].Id
	started := func() int {
		t.Helper()
		result, err := e.Plan(t.Context(), slog.New(slog.DiscardHandler))
		if err != nil {
			t.Fatal(err)
		}
		return result.Created
	}
	if n := started(); n != 1 {
		t.Fatalf("a warm minimum of 1 started %d containers", n)
	}
	stopStarting(t, pool, release)
	if n := started(); n != 0 {
		t.Fatalf("a release at the start failure limit started %d containers", n)
	}

	if _, err := c.PauseApp(t.Context(), ws, "reports"); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ResumeApp(t.Context(), ws, "reports"); err != nil {
		t.Fatal(err)
	}
	if n := started(); n != 1 {
		t.Fatalf("after a resume the release started %d containers, want 1", n)
	}

	stopStarting(t, pool, release)
	if again := deploy(t, c, ws, false, spec).Releases[0].Id; again != release {
		t.Fatalf("an unchanged redeploy made release %s, want %s reused", again, release)
	}
	if n := started(); n != 1 {
		t.Fatalf("after an unchanged redeploy the release started %d containers, want 1", n)
	}
}

// stopStarting puts release at the start failure limit, its last start
// failing an hour ago.
func stopStarting(t *testing.T, pool *pgxpool.Pool, release uuid.UUID) {
	t.Helper()
	if _, err := pool.Exec(t.Context(), `
with live as (
    update containers set state = 'stopped', stop_reason = 'start_failed', exit_message = 'pull image: not found',
           stopped_at = now() - interval '1 hour'
    where release_id = $1 and state <> 'stopped'
)
update releases set start_failures = 3 where id = $1`, release); err != nil {
		t.Fatal(err)
	}
}
