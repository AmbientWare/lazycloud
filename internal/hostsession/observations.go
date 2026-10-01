package hostsession

import (
	"context"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/observability"
)

// offerMetrics hands a host's samples to observability without waiting, so
// a slow metrics store never delays the session's commands.
func (s *Server) offerMetrics(host compute.HostID, m *hostproto.ContainerMetrics) {
	if s.config.Observability == nil {
		return
	}
	received := time.Now()
	samples := make([]observability.MetricSample, 0, len(m.GetSamples()))
	for _, c := range m.GetSamples() {
		id, err := uuid.Parse(c.GetContainerId())
		if err != nil || c.GetIntervalMs() == 0 {
			continue
		}
		sample := observability.MetricSample{
			Host: host, Container: execution.ContainerID(id), ReceivedAt: received, IntervalMs: c.GetIntervalMs(),
			CPUUsageUsec: c.GetCpuUsageUsec(), MemoryRSS: c.GetMemoryRssBytes(), MemorySwap: c.GetMemorySwapBytes(),
			NetworkRx: c.GetNetworkRxBytes(), NetworkTx: c.GetNetworkTxBytes(),
			DiskRead: c.GetDiskReadBytes(), DiskWrite: c.GetDiskWriteBytes(),
		}
		if gpus := c.GetGpus(); len(gpus) > 0 {
			use := &observability.GPUUse{Type: gpus[0].GetName()}
			for _, g := range gpus {
				use.UtilizationPct += float32(g.GetUtilizationPercent())
				use.MemoryUsed += g.GetMemoryUsedBytes()
				use.MemoryTotal += g.GetMemoryTotalBytes()
			}
			use.UtilizationPct /= float32(len(gpus))
			sample.GPU = use
		}
		samples = append(samples, sample)
	}
	if !s.config.Observability.OfferSamples(samples) {
		s.logger.Debug("dropped container metrics; ingest is behind", "host_id", host.String(), "samples", len(samples))
	}
}

// recordStartup stores the start stages a report carries. They are
// observations: a failure is logged and the session continues.
func (s *Server) recordStartup(ctx context.Context, host compute.HostID, report *hostproto.ContainerReport) {
	if s.config.Observability == nil || len(report.GetStartup()) == 0 {
		return
	}
	id, err := uuid.Parse(report.GetContainerId())
	if err != nil {
		return
	}
	stages := make([]observability.StartupStage, 0, len(report.GetStartup()))
	for _, st := range report.GetStartup() {
		kind, ok := stageKind(st.GetKind())
		if !ok || st.GetStartedAt() == nil || st.GetFinishedAt() == nil {
			continue
		}
		stages = append(stages, observability.StartupStage{
			Kind: kind, StartedAt: st.GetStartedAt().AsTime(), FinishedAt: st.GetFinishedAt().AsTime(), Cached: st.GetCached(),
		})
	}
	if err := s.config.Observability.RecordStartup(ctx, host, execution.ContainerID(id), stages); err != nil {
		s.logger.WarnContext(ctx, "recording startup stages failed", "container_id", id.String(), "error", err)
	}
}

func stageKind(k hostproto.StartupStageKind) (observability.StartupStageKind, bool) {
	switch k {
	case hostproto.StartupStageKind_STARTUP_STAGE_KIND_IMAGE:
		return observability.StageImage, true
	case hostproto.StartupStageKind_STARTUP_STAGE_KIND_SOURCE:
		return observability.StageSource, true
	case hostproto.StartupStageKind_STARTUP_STAGE_KIND_CREATE:
		return observability.StageCreate, true
	case hostproto.StartupStageKind_STARTUP_STAGE_KIND_RUNTIME:
		return observability.StageRuntime, true
	case hostproto.StartupStageKind_STARTUP_STAGE_KIND_DISK:
		return observability.StageDisk, true
	case hostproto.StartupStageKind_STARTUP_STAGE_KIND_UNSPECIFIED:
	}
	return "", false
}
