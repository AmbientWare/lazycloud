package images_test

import (
	"errors"
	"log/slog"
	"testing"

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
	if err := f.images.RecordTrace(ctx, ws, reference, []images.FrameRead{{Layer: 0, Frame: 1}}); !errors.Is(err, images.ErrInvalidTrace) {
		t.Fatalf("a frame the layer does not have was stored: %v", err)
	}
	if err := f.images.RecordTrace(ctx, ws, reference, []images.FrameRead{{Layer: 0, Frame: 0}}); err != nil {
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
