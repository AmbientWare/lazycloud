package observability

import (
	"context"
	"fmt"
	"math"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/prometheus/client_golang/prometheus"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/execution"
)

const (
	// ingestQueueBatches bounds host batches waiting to be stored. Metrics
	// are observations: past the bound they are dropped, never queued
	// against the session that carries commands.
	ingestQueueBatches = 512
	// flushSamples and flushInterval bound one insert: whichever comes
	// first writes everything queued in one statement.
	flushSamples  = 5000
	flushInterval = time.Second
)

// MetricSample is one container's use over IntervalMs, as its host
// reported it. ReceivedAt is the server's clock, so host clock skew cannot
// misplace a sample.
type MetricSample struct {
	Host         compute.HostID
	Container    execution.ContainerID
	ReceivedAt   time.Time
	IntervalMs   uint32
	CPUUsageUsec uint64
	MemoryRSS    uint64
	MemorySwap   uint64
	NetworkRx    uint64
	NetworkTx    uint64
	DiskRead     uint64
	DiskWrite    uint64
	// GPU is the container's GPUs combined; nil without one.
	GPU *GPUUse
}

// GPUUse combines a container's GPUs: utilization averaged, memory summed.
type GPUUse struct {
	UtilizationPct float32
	MemoryUsed     uint64
	MemoryTotal    uint64
	Type           string
}

type ingestQueue struct {
	batches chan []MetricSample
	stored  prometheus.Counter
	dropped prometheus.Counter
	flush   prometheus.Histogram
}

func newIngestQueue(registerer prometheus.Registerer) ingestQueue {
	q := ingestQueue{
		batches: make(chan []MetricSample, ingestQueueBatches),
		stored: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "lazycloud_metric_samples_stored_total", Help: "Container metric samples written.",
		}),
		dropped: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "lazycloud_metric_samples_dropped_total",
			Help: "Container metric samples dropped because ingest fell behind or the write failed.",
		}),
		flush: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name: "lazycloud_metric_flush_seconds", Help: "Duration of one batched metric sample insert.",
			Buckets: prometheus.ExponentialBuckets(0.0005, 2, 14),
		}),
	}
	if registerer != nil {
		registerer.MustRegister(q.stored, q.dropped, q.flush)
	}
	return q
}

// OfferSamples queues a host's batch for storage without waiting. It
// reports false when the queue is full and the batch was dropped.
func (o *Observability) OfferSamples(samples []MetricSample) bool {
	if len(samples) == 0 {
		return true
	}
	select {
	case o.ingest.batches <- samples:
		return true
	default:
		o.ingest.dropped.Add(float64(len(samples)))
		return false
	}
}

// RunIngest stores queued samples until ctx ends, one statement per flush
// across every host, then writes what is still queued.
func (o *Observability) RunIngest(ctx context.Context) error {
	ticker := time.NewTicker(flushInterval)
	defer ticker.Stop()
	var pending []MetricSample
	flush := func(ctx context.Context) {
		if len(pending) == 0 {
			return
		}
		if _, err := o.StoreSamples(ctx, pending); err != nil {
			o.logger.WarnContext(ctx, "storing container metrics failed", "samples", len(pending), "error", err)
			o.ingest.dropped.Add(float64(len(pending)))
		}
		pending = pending[:0]
	}
	for {
		select {
		case <-ctx.Done():
			for {
				select {
				case batch := <-o.ingest.batches:
					pending = append(pending, batch...)
					continue
				default:
				}
				break
			}
			final, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
			flush(final)
			cancel()
			return nil
		case batch := <-o.ingest.batches:
			pending = append(pending, batch...)
			if len(pending) >= flushSamples {
				flush(ctx)
			}
		case <-ticker.C:
			flush(ctx)
		}
	}
}

// StoreSamples writes samples in one statement. A sample whose container
// is not assigned to the host that sent it is dropped. It returns the
// samples stored.
func (o *Observability) StoreSamples(ctx context.Context, samples []MetricSample) (int64, error) {
	n := len(samples)
	p := InsertMetricSamplesParams{
		ContainerIds: make([]uuid.UUID, n), HostIds: make([]uuid.UUID, n), SampledAt: make([]time.Time, n),
		IntervalMs: make([]int32, n), CpuUsageUsec: make([]int64, n), MemoryRssBytes: make([]int64, n),
		MemorySwapBytes: make([]int64, n), NetworkRxBytes: make([]int64, n), NetworkTxBytes: make([]int64, n),
		DiskReadBytes: make([]int64, n), DiskWriteBytes: make([]int64, n), GpuUtilizationPct: make([]float32, n),
		GpuMemoryUsedBytes: make([]int64, n), GpuMemoryTotalBytes: make([]int64, n), GpuType: make([]string, n),
	}
	for i, s := range samples {
		p.ContainerIds[i] = uuid.UUID(s.Container)
		p.HostIds[i] = uuid.UUID(s.Host)
		p.SampledAt[i] = s.ReceivedAt
		p.IntervalMs[i] = int32(min(s.IntervalMs, math.MaxInt32)) //nolint:gosec // Clamped.
		p.CpuUsageUsec[i] = clamp(s.CPUUsageUsec)
		p.MemoryRssBytes[i] = clamp(s.MemoryRSS)
		p.MemorySwapBytes[i] = clamp(s.MemorySwap)
		p.NetworkRxBytes[i] = clamp(s.NetworkRx)
		p.NetworkTxBytes[i] = clamp(s.NetworkTx)
		p.DiskReadBytes[i] = clamp(s.DiskRead)
		p.DiskWriteBytes[i] = clamp(s.DiskWrite)
		// The statement reads NaN and -1 as absent, since arrays of a
		// scalar type cannot hold null through this binding.
		p.GpuUtilizationPct[i] = float32(math.NaN())
		p.GpuMemoryUsedBytes[i], p.GpuMemoryTotalBytes[i] = -1, -1
		if g := s.GPU; g != nil {
			p.GpuUtilizationPct[i] = g.UtilizationPct
			p.GpuMemoryUsedBytes[i] = clamp(g.MemoryUsed)
			p.GpuMemoryTotalBytes[i] = clamp(g.MemoryTotal)
			p.GpuType[i] = g.Type
		}
	}
	began := time.Now()
	stored, err := o.queries.InsertMetricSamples(ctx, p)
	o.ingest.flush.Observe(time.Since(began).Seconds())
	if err != nil {
		return 0, fmt.Errorf("insert metric samples: %w", err)
	}
	o.ingest.stored.Add(float64(stored))
	if dropped := int64(n) - stored; dropped > 0 {
		o.ingest.dropped.Add(float64(dropped))
	}
	return stored, nil
}

func clamp(v uint64) int64 { return int64(min(v, math.MaxInt64)) } //nolint:gosec // Clamped.

const (
	// sampleRetention is how long 5-second samples stay; minute points
	// stay minuteRetention.
	sampleRetention = time.Hour
	minuteRetention = 7 * 24 * time.Hour
	// deleteBatch bounds one retention delete.
	deleteBatch = 20000
)

// RollupResult is what one rollup pass did.
type RollupResult struct {
	Folded         int64
	DeletedSamples int64
	DeletedMinutes int64
	// Behind is set when the pass stopped short of the present, so the
	// caller can run another at once.
	Behind bool
}

// RollUp folds finished minutes of samples into minute points, then
// deletes samples and minute points past retention in bounded batches. Its
// work is proportional to the samples written since the last pass.
func (o *Observability) RollUp(ctx context.Context) (RollupResult, error) {
	var result RollupResult
	var rolledThrough time.Time
	err := pgx.BeginFunc(ctx, o.pool, func(tx pgx.Tx) error {
		q := o.queries.WithTx(tx)
		window, err := q.LockRollup(ctx)
		if err != nil {
			return fmt.Errorf("lock rollup: %w", err)
		}
		rolledThrough = window.FromTs
		if !window.ToTs.After(window.FromTs) {
			return nil
		}
		folded, err := q.FoldMinutes(ctx, FoldMinutesParams(window))
		if err != nil {
			return fmt.Errorf("fold minutes: %w", err)
		}
		if err := q.AdvanceRollup(ctx, window.ToTs); err != nil {
			return fmt.Errorf("advance rollup: %w", err)
		}
		result.Folded = folded
		rolledThrough = window.ToTs
		result.Behind = time.Since(window.ToTs) > 5*time.Minute
		return nil
	})
	if err != nil {
		return result, fmt.Errorf("roll up metrics: %w", err)
	}
	// Samples not yet folded stay, whatever their age.
	before := time.Now().Add(-sampleRetention)
	if rolledThrough.Before(before) {
		before = rolledThrough
	}
	if result.DeletedSamples, err = o.queries.DeleteSamplesBefore(ctx, DeleteSamplesBeforeParams{Before: before, MaxRows: deleteBatch}); err != nil {
		return result, fmt.Errorf("delete expired samples: %w", err)
	}
	if result.DeletedMinutes, err = o.queries.DeleteMinutesBefore(ctx, DeleteMinutesBeforeParams{
		Before: time.Now().Add(-minuteRetention), MaxRows: deleteBatch,
	}); err != nil {
		return result, fmt.Errorf("delete expired minute points: %w", err)
	}
	result.Behind = result.Behind || result.DeletedSamples == deleteBatch || result.DeletedMinutes == deleteBatch
	return result, nil
}
