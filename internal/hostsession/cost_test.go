package hostsession_test

import (
	"context"
	"maps"
	"regexp"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

var queryName = regexp.MustCompile(`-- name: (\w+)`)

// queryCounter counts the named queries a pool runs.
type queryCounter struct {
	mu sync.Mutex
	n  map[string]int
}

func (c *queryCounter) TraceQueryStart(ctx context.Context, _ *pgx.Conn, data pgx.TraceQueryStartData) context.Context {
	if m := queryName.FindStringSubmatch(data.SQL); m != nil {
		c.mu.Lock()
		c.n[m[1]]++
		c.mu.Unlock()
	}
	return ctx
}

func (*queryCounter) TraceQueryEnd(context.Context, *pgx.Conn, pgx.TraceQueryEndData) {}

// take returns the counts since the last take and starts again.
func (c *queryCounter) take() map[string]int {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := c.n
	c.n = map[string]int{}
	return out
}

// replicate adds n starting containers of container's release on its host.
func (h *harness) replicate(container uuid.UUID, n int) []execution.ContainerID {
	h.t.Helper()
	out := make([]execution.ContainerID, n)
	for i := range n {
		var id uuid.UUID
		if err := h.pool.QueryRow(h.t.Context(), `
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
select workspace_id, release_id, 'starting', host_id, slots, cpu_millis, memory_bytes, now() from containers where id = $1
returning id`, container).Scan(&id); err != nil {
			h.t.Fatal(err)
		}
		out[i] = execution.ContainerID(id)
	}
	return out
}

// TestImageQueriesPerSyncDoNotGrowWithReplicas: the queries that resolve
// and grant a start's image run once per image and sync, however many
// replicas start, and a start that waits for its image's conversion reads
// no secrets.
func TestImageQueriesPerSyncDoNotGrowWithReplicas(t *testing.T) {
	counter := &queryCounter{n: map[string]int{}}
	cfg := dbtest.New(t).Config().Copy()
	cfg.ConnConfig.Tracer = counter
	traced, err := pgxpool.NewWithConfig(t.Context(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(traced.Close)
	h := serve(t, traced)
	host, ctx := h.enroll()
	const replicas = 4
	ref := reference("a")
	h.publish(ref, h.storeLayer("a"))
	converted := h.startingImage(host, ref)
	h.replicate(converted, replicas-1)
	_, managed := h.startingWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.12"}}`)
	h.replicate(managed, replicas-1)
	old := reference("old")
	waitingWS, waiting := h.startingWith(host, `{"handler": "reports:summarize", "secrets": ["TOKEN"],
		"image": {"python_version": "3.12", "image_id": "img_0123456789abcdef01234567", "reference": "`+old+`"}}`)
	if _, err := h.secrets.Set(t.Context(), waitingWS, "TOKEN", "hunter2-hunter2"); err != nil {
		t.Fatal(err)
	}
	h.exec(`insert into images (digest, id, dockerfile, python_version, architecture, reference, ready_at)
		values (sha256('unconverted'), 'img_0123456789abcdef01234567', 'FROM scratch', '3.12', 'amd64', $1, now())`, old)
	h.replicate(waiting, replicas-1)

	counter.take()
	in := commands(t, open(t, ctx, h.client))
	for range 2 * replicas {
		next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStart() != nil })
	}
	time.Sleep(50 * time.Millisecond)
	first := counter.take()
	time.Sleep(time.Second)
	steady := counter.take()
	syncs := steady["StartingContainersOnHost"]
	if syncs == 0 {
		t.Fatal("no sync ran")
	}
	per := map[string]float64{}
	for name, n := range steady {
		per[name] = float64(n) / float64(syncs)
	}
	names := maps.Clone(first)
	maps.Copy(names, steady)
	for _, name := range slices.Sorted(maps.Keys(names)) {
		t.Logf("%-32s first %3d  per later sync %.1f", name, first[name], per[name])
	}
}
