package images

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
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

// StartupTrace returns the frames the workspace's containers of reference
// read through their first task or request, empty when none is stored, and
// whether the next start should record them again. Traces are kept per
// workspace: what a container reads says something of its code, so no
// other workspace's host gets it.
func (i *Images) StartupTrace(ctx context.Context, workspace identity.WorkspaceID, reference string) ([]*imagefsproto.FrameRead, bool, error) {
	row, err := i.queries.StartupTrace(ctx, StartupTraceParams{Reference: reference, WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, true, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("read startup trace: %w", err)
	}
	// The table keeps the arrays of one length, and the record keeps them
	// non-negative.
	reads := make([]*imagefsproto.FrameRead, len(row.Layers))
	for n := range reads {
		reads[n] = &imagefsproto.FrameRead{Layer: uint32(row.Layers[n]), Frame: uint32(row.Frames[n])} //nolint:gosec // See above.
	}
	return reads, time.Since(row.RecordedAt) >= traceAge, nil
}

// RecordTrace stores the frames a container of the workspace read from
// reference through its first task or request, unless a trace younger than
// traceAge is stored. The trace lasts as long as the reference's converted
// layers. A trace that does not fit them is ErrInvalidTrace.
func (i *Images) RecordTrace(ctx context.Context, workspace identity.WorkspaceID, reference string, reads []*imagefsproto.FrameRead) error {
	if len(reads) == 0 {
		return nil
	}
	if len(reads) > MaxTraceReads {
		return fmt.Errorf("%w: %d frames, at most %d", ErrInvalidTrace, len(reads), MaxTraceReads)
	}
	params := RecordTraceParams{
		Reference: reference, WorkspaceID: uuid.UUID(workspace), MaxAgeSeconds: traceAge.Seconds(),
		Layers: make([]int32, len(reads)), Frames: make([]int32, len(reads)),
	}
	for n, r := range reads {
		// A read past the int32 range turns negative, which the query refuses.
		params.Layers[n], params.Frames[n] = int32(r.GetLayer()), int32(r.GetFrame()) //nolint:gosec // Checked by the query.
	}
	fits, err := i.queries.RecordTrace(ctx, params)
	if err != nil {
		return fmt.Errorf("record startup trace: %w", err)
	}
	if !fits {
		return fmt.Errorf("%w: %s", ErrInvalidTrace, reference)
	}
	return nil
}
