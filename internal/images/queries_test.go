package images_test

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/images"
)

// queryCounter counts the statements a pool sends.
type queryCounter struct{ n atomic.Int64 }

func (c *queryCounter) TraceQueryStart(ctx context.Context, _ *pgx.Conn, _ pgx.TraceQueryStartData) context.Context {
	c.n.Add(1)
	return ctx
}

func (*queryCounter) TraceQueryEnd(context.Context, *pgx.Conn, pgx.TraceQueryEndData) {}

// The image reads of a container start cost as many statements for a large
// image as for a small one: the pull of a pinned reference of one layer or
// of four, the grants of one reference or of four, and the record of a
// startup trace of one frame or of four.
func TestStartReadsDoNotGrowWithTheImage(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	ws := f.workspace(t, "a")
	host := f.host(t)
	r, err := f.images.Resolve(ctx, ws, numpy())
	if err != nil {
		t.Fatal(err)
	}
	repository := f.imageRepository(t, r.Image.ID)
	small, large := repository+"@sha256:"+hex64("8"), repository+"@sha256:"+hex64("9")
	convertReference(t, f.pool, f.storage, small, [][]byte{[]byte("small")})
	convertReference(t, f.pool, f.storage, large, [][]byte{[]byte("a"), []byte("b"), []byte("c"), []byte("d")})
	references := []string{small, large}
	for _, seed := range []string{"a", "b"} {
		reference := "registry.test/lazycloud/images/" + seed + "@sha256:" + hex64(seed)
		convertReference(t, f.pool, f.storage, reference, [][]byte{[]byte(seed)})
		references = append(references, reference)
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
	statements := func(call func() error) int64 {
		t.Helper()
		before := counter.n.Load()
		if err := call(); err != nil {
			t.Fatal(err)
		}
		return counter.n.Load() - before
	}
	reads := func(n int) []*imagefsproto.FrameRead {
		out := make([]*imagefsproto.FrameRead, n)
		for layer := range out {
			out[layer] = &imagefsproto.FrameRead{Layer: uint32(layer)} //nolint:gosec // At most four.
		}
		return out
	}
	costs := []struct {
		name      string
		one, four func() error
	}{
		{
			"pinned pull",
			func() error { _, err := im.ConvertedPull(ctx, ws, r.Image.ID, small); return err },
			func() error { _, err := im.ConvertedPull(ctx, ws, r.Image.ID, large); return err },
		},
		{
			"grants",
			func() error { _, err := im.LayerReadURLs(ctx, references[:1], host, time.Minute); return err },
			func() error { _, err := im.LayerReadURLs(ctx, references, host, time.Minute); return err },
		},
		{
			"trace record",
			func() error { return im.RecordTrace(ctx, ws, small, reads(1)) },
			func() error { return im.RecordTrace(ctx, ws, large, reads(4)) },
		},
	}
	// The first pull logs in to the registry.
	statements(costs[0].one)
	for _, c := range costs {
		one, four := statements(c.one), statements(c.four)
		t.Logf("%s: %d statements, then %d", c.name, one, four)
		if one != four {
			t.Errorf("%s took %d statements for one and %d for four", c.name, one, four)
		}
	}
}
