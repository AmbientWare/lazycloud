package compute

import (
	"context"
	"fmt"
	"time"
)

// spendWindow is the span FleetSpend totals.
const spendWindow = 24 * time.Hour

// SpendShare is what a group of hosts costs per hour at this moment.
type SpendShare struct {
	Hosts        int
	HourlyMicros int64
}

// FleetSpend is what the platform's cloud hosts cost in the platform's own
// AWS account; connection hosts and joined machines are not the platform's
// to pay for. A running host costs the hourly price it launched at, its
// volumes and address included; a stopped one costs its volumes at the size
// and throughput it launched with.
type FleetSpend struct {
	ObservedAt time.Time
	// Running and Stopped are what hosts in each state cost per hour now;
	// together they are the fleet's hourly rate.
	Running, Stopped SpendShare
	// WindowMicros is what the hosts cost over the spendWindow up to
	// ObservedAt.
	WindowMicros int64
}

// HourlyMicros is the fleet's cost per hour at ObservedAt.
func (s FleetSpend) HourlyMicros() int64 { return s.Running.HourlyMicros + s.Stopped.HourlyMicros }

// billed reports whether EC2 bills an instance's hours in p;
// PlatformHostSpend counts the same phases.
func (p Phase) billed() bool {
	switch p {
	case PhaseProvisioning, PhaseBooting, PhaseJoining, PhaseReady, PhaseDraining, PhasePreparing, PhaseStopping, PhaseResuming:
		return true
	case PhaseRequested, PhaseStopped, PhaseTerminating, PhaseDeleted, PhaseFailed:
	}
	return false
}

// FleetSpend prices the platform fleet now and over the spendWindow from
// each host's phase history.
func (c *Compute) FleetSpend(ctx context.Context) (FleetSpend, error) {
	now := time.Now()
	rows, err := c.queries.PlatformHostSpend(ctx, now.Add(-spendWindow))
	if err != nil {
		return FleetSpend{}, fmt.Errorf("read platform host spend: %w", err)
	}
	out := FleetSpend{ObservedAt: now}
	var window float64
	for _, r := range rows {
		var stopped int64
		if t, ok := CatalogTypeNamed(r.InstanceType); ok {
			stopped = t.volumesMicros(r.Region, r.HibernationConfigured, false)
		}
		window += float64(r.HourlyMicros)*r.RunningSeconds/3600 + float64(stopped)*r.StoppedSeconds/3600
		switch phase := Phase(r.Phase); {
		case phase.billed():
			out.Running.Hosts++
			out.Running.HourlyMicros += r.HourlyMicros
		case phase == PhaseStopped:
			out.Stopped.Hosts++
			out.Stopped.HourlyMicros += stopped
		}
	}
	out.WindowMicros = int64(window)
	return out, nil
}
