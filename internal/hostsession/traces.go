package hostsession

import (
	"context"
	"errors"
	"slices"

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

// startTrace returns the startup trace of workspace's starts of reference,
// once per sync. A start is sent without one when it cannot be read:
// prefetching only speeds the start up.
func (s *Server) startTrace(ctx context.Context, cache *syncCache, workspace identity.WorkspaceID, reference string) startTrace {
	key := traceKey{workspace: workspace, reference: reference}
	if t, ok := cache.traces[key]; ok {
		return t
	}
	reads, record, err := s.images.StartupTrace(ctx, workspace, reference)
	if err != nil {
		s.logger.WarnContext(ctx, "reading the startup trace failed", "image", reference, "error", err)
		return startTrace{}
	}
	t := startTrace{record: record}
	if len(reads) > 0 {
		t.prefetch = &hostproto.ImageTrace{Reads: reads}
	}
	cache.traces[key] = t
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
		err = sess.server.images.RecordTrace(ctx, workspace, reference, report.GetTrace().GetReads())
	}
	if err != nil && ctx.Err() == nil {
		msg := "storing a startup trace failed"
		if errors.Is(err, images.ErrInvalidTrace) {
			msg = "the host reported a startup trace that does not fit its image or the bound"
		}
		sess.server.logger.WarnContext(ctx, msg, "host", sess.host.String(), "container", id.String(), "error", err)
	}
}

// volumeTrace returns what a start of workload that mounts platform volumes
// carries of its workload's volume trace, once per sync, and nil for a start
// that mounts none. A start is sent without its reads when they cannot be
// read: warming only speeds the start up.
func (s *Server) volumeTrace(ctx context.Context, cache *syncCache, start execution.StartCommand, mounts []*hostproto.VolumeMount) *hostproto.VolumeTrace {
	if !slices.ContainsFunc(mounts, func(m *hostproto.VolumeMount) bool { return m.GetVolume() != nil }) {
		return nil
	}
	if t, ok := cache.volumeTraces[start.Workload]; ok {
		return t
	}
	reads, record, err := s.storage.VolumeTrace(ctx, start.Workspace, start.Workload)
	if err != nil {
		s.logger.WarnContext(ctx, "reading the volume trace failed", "workload", start.Workload.String(), "error", err)
		return &hostproto.VolumeTrace{}
	}
	t := &hostproto.VolumeTrace{Reads: reads, Record: record}
	cache.volumeTraces[start.Workload] = t
	return t
}

// recordVolumeTrace stores the volume trace a host reported for one of its
// live containers, under the container's own workspace and workload. A
// trace that cannot be stored is logged and dropped; a later start records
// another.
func (sess *session) recordVolumeTrace(ctx context.Context, report *hostproto.ContainerVolumeTrace) {
	id, err := uuid.Parse(report.GetContainerId())
	if err != nil || len(report.GetReads()) == 0 {
		return
	}
	workspace, workload, ok, err := sess.server.execution.ContainerWorkload(ctx, sess.host, execution.ContainerID(id))
	if err == nil && ok {
		err = sess.server.storage.RecordVolumeTrace(ctx, workspace, workload, id, report.GetReads())
	}
	if err != nil && ctx.Err() == nil {
		sess.server.logger.WarnContext(ctx, "storing a volume trace failed", "host", sess.host.String(), "container", id.String(), "error", err)
	}
}
