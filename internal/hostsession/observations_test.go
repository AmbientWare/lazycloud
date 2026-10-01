package hostsession_test

import (
	"errors"
	"io"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/observability"
)

// Samples and start stages a host sends over its session are stored for its
// own containers only, and never hold up the session's commands.
func TestSessionStoresMetricsAndStartStagesOfTheHostsContainers(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	ws, container := h.startingContainer(host)
	stream := open(t, ctx, h.client)
	receive(t, stream) // start

	began := time.Now().Add(-2 * time.Second)
	if err := stream.Send(&hostproto.HostMessage{Body: &hostproto.HostMessage_Container{Container: &hostproto.ContainerReport{
		ContainerId: container.String(), Phase: hostproto.ContainerPhase_CONTAINER_PHASE_READY, ObservedAt: timestamppb.Now(),
		Startup: []*hostproto.StartupStage{{
			Kind: hostproto.StartupStageKind_STARTUP_STAGE_KIND_IMAGE, Cached: true,
			StartedAt: timestamppb.New(began), FinishedAt: timestamppb.New(began.Add(300 * time.Millisecond)),
		}},
	}}}); err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&hostproto.HostMessage{Body: &hostproto.HostMessage_Metrics{Metrics: &hostproto.ContainerMetrics{
		Samples: []*hostproto.ContainerSample{
			{ContainerId: container.String(), IntervalMs: 5000, CpuUsageUsec: 2_500_000, MemoryRssBytes: 64 << 20,
				Gpus: []*hostproto.GPUSample{
					{Uuid: "GPU-a", Name: "NVIDIA L4", UtilizationPercent: 40, MemoryUsedBytes: 1 << 30, MemoryTotalBytes: 24 << 30},
					{Uuid: "GPU-b", Name: "NVIDIA L4", UtilizationPercent: 60, MemoryUsedBytes: 1 << 30, MemoryTotalBytes: 24 << 30},
				}},
			// Another host's container, or none at all, is dropped.
			{ContainerId: uuid.NewString(), IntervalMs: 5000, CpuUsageUsec: 1},
		},
	}}}); err != nil {
		t.Fatal(err)
	}

	deadline := time.Now().Add(10 * time.Second)
	var metrics observability.MetricsQuery
	for {
		got, err := h.obs.ContainerMetrics(t.Context(), ws, container, metrics)
		if err != nil {
			t.Fatal(err)
		}
		if len(got.Points) == 1 {
			p := got.Points[0]
			if p.CpuMillicores != 500 || p.MemoryRssBytes != 64<<20 || *p.GpuUtilizationPct != 50 ||
				*p.GpuMemoryTotalBytes != 48<<30 || *p.GpuType != "NVIDIA L4" {
				t.Fatalf("point %+v", p)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("no stored sample: %+v", got)
		}
		time.Sleep(50 * time.Millisecond)
	}
	var stored int
	if err := h.pool.QueryRow(t.Context(), "select count(*) from container_metric_samples").Scan(&stored); err != nil || stored != 1 {
		t.Fatalf("%d samples stored: %v", stored, err)
	}
	lifecycle, err := h.obs.ContainerLifecycle(t.Context(), ws, container)
	if err != nil {
		t.Fatal(err)
	}
	if len(lifecycle.Stages) != 2 || lifecycle.Stages[0].Stage != "image" || *lifecycle.Stages[0].DurationMs != 300 {
		t.Fatalf("lifecycle %+v", lifecycle)
	}

	// Metrics leave the session open, so the host closing it ends it cleanly.
	if err := stream.CloseSend(); err != nil {
		t.Fatal(err)
	}
	for {
		if _, err := stream.Recv(); errors.Is(err, io.EOF) {
			break
		} else if err != nil {
			t.Fatalf("session ended: %v", err)
		}
	}
}
