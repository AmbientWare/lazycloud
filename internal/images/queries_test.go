package images_test

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/images"
)

// queryCounter counts the statements a pool sends.
type queryCounter struct{ n atomic.Int64 }

func (c *queryCounter) TraceQueryStart(ctx context.Context, _ *pgx.Conn, _ pgx.TraceQueryStartData) context.Context {
	c.n.Add(1)
	return ctx
}

func (*queryCounter) TraceQueryEnd(context.Context, *pgx.Conn, pgx.TraceQueryEndData) {}

// The image reads of a container start cost one statement each once the
// image is converted: the pull of a pinned or managed image, a startup
// trace's record, and the grants of every reference a sync names. A sync
// that names no platform image reads nothing.
func TestStartReadsCostOneStatement(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	ws := f.workspace(t, "a")
	host := f.host(t)
	source, err := f.images.ManagedSource(ctx, "3.12")
	if err != nil {
		t.Fatal(err)
	}
	if err := f.images.ConvertPlatformImage(ctx, source, "amd64"); err != nil {
		t.Fatal(err)
	}
	r, err := f.images.Resolve(ctx, ws, numpy())
	if err != nil {
		t.Fatal(err)
	}
	pinned := f.imageRepository(t, r.Image.ID) + "@sha256:" + hex64("8")
	convertReference(t, f.pool, f.storage, pinned, [][]byte{[]byte("pinned")})
	var others []string
	for _, seed := range []string{"a", "b"} {
		reference := "registry.test/lazycloud/images/" + seed + "@sha256:" + hex64(seed)
		convertReference(t, f.pool, f.storage, reference, [][]byte{[]byte(seed)})
		others = append(others, reference)
	}

	counter := &queryCounter{}
	cfg := f.pool.Config().Copy()
	cfg.ConnConfig.Tracer = counter
	traced, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(traced.Close)
	im := images.NewImages(traced, f.execution, f.secrets, f.storage, images.Config{
		Registry: f.registry, Repository: "lazycloud", Insecure: true, ManagedBase: f.registry + "/library/python:{version}-slim",
	})
	if _, err := im.ManagedPull(ctx, host, "3.12"); err != nil {
		t.Fatal(err)
	}
	statements := func(name string, call func() error) int64 {
		t.Helper()
		before := counter.n.Load()
		if err := call(); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		n := counter.n.Load() - before
		t.Logf("%s: %d statements", name, n)
		return n
	}
	budgets := []struct {
		name string
		max  int64
		call func() error
	}{
		{"pinned pull", 1, func() error { _, err := im.ConvertedPull(ctx, ws, r.Image.ID, pinned); return err }},
		{"managed pull", 1, func() error { _, err := im.ManagedPull(ctx, host, "3.12"); return err }},
		{"no platform images", 0, func() error { _, err := im.PlatformPulls(ctx, host, nil); return err }},
		{"trace record", 1, func() error { return im.RecordTrace(ctx, ws, pinned, []images.FrameRead{{}}) }},
		{"grants of three references", 1, func() error {
			_, err := im.LayerReadURLsOf(ctx, append([]string{pinned}, others...), host, time.Minute)
			return err
		}},
	}
	for _, b := range budgets {
		if n := statements(b.name, b.call); n > b.max {
			t.Errorf("%s took %d statements, want at most %d", b.name, n, b.max)
		}
	}
}
