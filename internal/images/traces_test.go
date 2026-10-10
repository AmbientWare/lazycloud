package images_test

import (
	"context"
	"errors"
	"log/slog"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/images"
)

// A startup trace lasts as long as its reference's converted layers, not
// its last recorded use: a workload that starts less than once a grace
// period still prefetches. It goes when the sweep retires the layers. A
// trace naming a frame the image does not have is refused.
func TestStartupTracesLastAsLongAsTheirLayers(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	ws := f.workspace(t, "a")
	logger := slog.New(slog.DiscardHandler)
	reference := "registry.test/lazycloud/images/traced@sha256:" + hex64("7")
	convertReference(t, f.pool, f.storage, reference, [][]byte{[]byte("one frame")})
	if err := f.images.RecordUses(ctx, []string{reference}); err != nil {
		t.Fatal(err)
	}
	if err := f.images.RecordTrace(ctx, ws, reference, []*imagefsproto.FrameRead{{Layer: 0, Frame: 1}}); !errors.Is(err, images.ErrInvalidTrace) {
		t.Fatalf("a frame the layer does not have was stored: %v", err)
	}
	if err := f.images.RecordTrace(ctx, ws, reference, []*imagefsproto.FrameRead{{Layer: 0, Frame: 0}}); err != nil {
		t.Fatal(err)
	}
	stored := func() int {
		t.Helper()
		reads, _, err := f.images.StartupTrace(ctx, ws, reference)
		if err != nil {
			t.Fatal(err)
		}
		return len(reads)
	}

	// The use lapses and the layers start their grace period.
	if _, err := f.pool.Exec(ctx, "update image_reference_uses set used_at = now() - interval '25 hours'"); err != nil {
		t.Fatal(err)
	}
	if err := f.images.SweepLayers(ctx, logger); err != nil {
		t.Fatal(err)
	}
	if f.count(t, "select count(*) from image_reference_uses") != 0 || stored() != 1 {
		t.Fatal("the trace went with the reference's use")
	}

	if _, err := f.pool.Exec(ctx, "update image_layers set unreferenced_since = now() - interval '25 hours'"); err != nil {
		t.Fatal(err)
	}
	if err := f.images.SweepLayers(ctx, logger); err != nil {
		t.Fatal(err)
	}
	if stored() != 0 {
		t.Fatal("the trace outlived the reference's layers")
	}
}

// A trace recorded while the sweep retires its reference's layer is not
// stored: it would outlive the layers it reads.
func TestATraceRacingItsLayersRetirementIsDropped(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	ws := f.workspace(t, "a")
	reference := "registry.test/lazycloud/images/retiring@sha256:" + hex64("9")
	convertReference(t, f.pool, f.storage, reference, [][]byte{[]byte("one frame")})
	var layer uuid.UUID
	if err := f.pool.QueryRow(ctx, "update image_layers set unreferenced_since = now() - interval '25 hours' returning id").Scan(&layer); err != nil {
		t.Fatal(err)
	}
	tx, err := f.pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = tx.Rollback(context.WithoutCancel(ctx)) }()
	if err := images.New(tx).RetireLayer(ctx, images.RetireLayerParams{ID: layer, GraceSeconds: (24 * time.Hour).Seconds()}); err != nil {
		t.Fatal(err)
	}
	recorded := make(chan error, 1)
	go func() { recorded <- f.images.RecordTrace(ctx, ws, reference, []*imagefsproto.FrameRead{{}}) }()
	time.Sleep(300 * time.Millisecond)
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	if err := <-recorded; err != nil {
		t.Fatal(err)
	}
	if n := f.count(t, "select count(*) from image_traces"); n != 0 {
		t.Fatal("a trace outlived its reference's layers")
	}
}
