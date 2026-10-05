package hostsession

import (
	"context"
	"errors"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
)

type traceKey struct {
	workspace identity.WorkspaceID
	reference string
}

// startTrace is what a start carries of its image's startup trace: the
// frames to prefetch, and whether to record a new trace.
type startTrace struct {
	prefetch *hostproto.ImageTrace
	record   bool
}

// traceCache holds the traces read during one sync, so the replicas of one
// image cost one query.
type traceCache map[traceKey]startTrace

// startTrace returns the startup trace of workspace's starts of reference.
// A start is sent without one when it cannot be read: prefetching only
// speeds the start up.
func (s *Server) startTrace(ctx context.Context, cache traceCache, workspace identity.WorkspaceID, reference string) startTrace {
	key := traceKey{workspace: workspace, reference: reference}
	if t, ok := cache[key]; ok {
		return t
	}
	reads, record, err := s.images.StartupTrace(ctx, workspace, reference)
	if err != nil {
		s.logger.WarnContext(ctx, "reading the startup trace failed", "image", reference, "error", err)
		return startTrace{}
	}
	t := startTrace{record: record}
	if len(reads) > 0 {
		t.prefetch = &hostproto.ImageTrace{Reads: make([]*hostproto.FrameRead, len(reads))}
		for n, r := range reads {
			t.prefetch.Reads[n] = &hostproto.FrameRead{Layer: r.Layer, Frame: r.Frame}
		}
	}
	cache[key] = t
	return t
}

// recordTrace stores the startup trace a host reported for one of its live
// containers, under the container's own workspace and image. A trace that
// cannot be stored is logged and dropped; a later start records another.
func (sess *session) recordTrace(ctx context.Context, report *hostproto.StartupTrace) {
	id, err := uuid.Parse(report.GetContainerId())
	if err != nil || len(report.GetTrace().GetReads()) == 0 {
		return
	}
	workspace, reference, ok, err := sess.server.execution.ContainerImage(ctx, sess.host, execution.ContainerID(id))
	if err == nil && ok {
		reads := make([]images.FrameRead, len(report.GetTrace().GetReads()))
		for n, r := range report.GetTrace().GetReads() {
			reads[n] = images.FrameRead{Layer: r.GetLayer(), Frame: r.GetFrame()}
		}
		err = sess.server.images.RecordTrace(ctx, workspace, reference, reads)
	}
	if err != nil && ctx.Err() == nil {
		msg := "storing a startup trace failed"
		if errors.Is(err, images.ErrInvalidTrace) {
			msg = "the host reported a startup trace that does not fit its image"
		}
		sess.server.logger.WarnContext(ctx, msg, "host", sess.host.String(), "container", id.String(), "error", err)
	}
}
