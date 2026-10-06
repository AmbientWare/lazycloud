package images

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

const (
	// traceAge is when a stored startup trace is recorded again: a
	// release's code decides what its containers read at startup, and
	// changes with deploys while the image stays.
	traceAge = 24 * time.Hour
	// MaxTraceReads bounds a stored trace, as the table does.
	MaxTraceReads = 4096
)

// ErrInvalidTrace means a reported trace names a layer or frame the image
// does not have, or more frames than a trace holds.
var ErrInvalidTrace = errors.New("the trace does not fit the image")

// FrameRead is a frame of the layer at a position in an image's layers.
type FrameRead struct {
	Layer, Frame uint32
}

// StartupTrace returns the frames the workspace's containers of reference
// read before they were ready, empty when none is stored, and whether the
// next start should record them again. Traces are kept per workspace: what
// a container reads says something of its code, so no other workspace's
// host gets it.
func (i *Images) StartupTrace(ctx context.Context, workspace identity.WorkspaceID, reference string) ([]FrameRead, bool, error) {
	row, err := i.queries.StartupTrace(ctx, StartupTraceParams{Reference: reference, WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, true, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("read startup trace: %w", err)
	}
	reads := make([]FrameRead, min(len(row.Layers), len(row.Frames)))
	for n := range reads {
		reads[n] = FrameRead{Layer: uint32(row.Layers[n]), Frame: uint32(row.Frames[n])} //nolint:gosec // stored checked non-negative
	}
	return reads, time.Since(row.RecordedAt) >= traceAge, nil
}

// RecordTrace stores the frames a container of the workspace read from
// reference before it was ready, unless a trace younger than traceAge is
// stored. A trace that does not fit the reference's layers is
// ErrInvalidTrace.
func (i *Images) RecordTrace(ctx context.Context, workspace identity.WorkspaceID, reference string, reads []FrameRead) error {
	if len(reads) == 0 {
		return nil
	}
	if len(reads) > MaxTraceReads {
		return fmt.Errorf("%w: %d frames, at most %d", ErrInvalidTrace, len(reads), MaxTraceReads)
	}
	frames, err := i.queries.ReferenceFrames(ctx, reference)
	if err != nil {
		return fmt.Errorf("read image layers: %w", err)
	}
	params := RecordTraceParams{
		Reference: reference, WorkspaceID: uuid.UUID(workspace), MaxAgeSeconds: traceAge.Seconds(),
		Layers: make([]int32, len(reads)), Frames: make([]int32, len(reads)),
	}
	for n, r := range reads {
		if int(r.Layer) >= len(frames) || int64(r.Frame) >= int64(frames[r.Layer]) {
			return fmt.Errorf("%w: frame %d of layer %d", ErrInvalidTrace, r.Frame, r.Layer)
		}
		params.Layers[n], params.Frames[n] = int32(r.Layer), int32(r.Frame) //nolint:gosec // checked against the layer counts
	}
	if _, err := i.queries.RecordTrace(ctx, params); err != nil {
		return fmt.Errorf("record startup trace: %w", err)
	}
	return nil
}
