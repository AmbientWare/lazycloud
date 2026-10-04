package compute

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// PublishedMarket is one market's plan as the planner publishes it in
// fleet_markets, for every replica and the admin page.
type PublishedMarket struct {
	Preemptible   bool          `json:"preemptible"`
	GPUType       string        `json:"gpu_type"`
	Load          FleetCapacity `json:"load"`
	WarmTarget    FleetCapacity `json:"warm_target"`
	WarmFree      FleetCapacity `json:"warm_free"`
	StoppedTarget FleetCapacity `json:"stopped_target"`
	ReserveReady  FleetCapacity `json:"reserve_ready"`
	Reason        MarketReason  `json:"reason"`
	// FloorShortSince is when the stopped floor went short; the next pass
	// reads it back.
	FloorShortSince *time.Time           `json:"floor_short_since,omitempty"`
	States          []FleetStateCapacity `json:"states"`
	// Decision summarizes the market's plan; the planner logs it when it
	// changes.
	Decision string `json:"decision"`
	// GeneratedAt and ExpiresAt bound the plan; after ExpiresAt it is no
	// plan.
	GeneratedAt time.Time `json:"-"`
	ExpiresAt   time.Time `json:"-"`
}

// publishedMarket is the published form of a market plan and its pass's
// actions in the market.
func publishedMarket(mp MarketPlan, actions []FleetAction) PublishedMarket {
	out := PublishedMarket{
		Preemptible: mp.Market.Preemptible, GPUType: mp.Market.GPU, Load: mp.Load, WarmTarget: mp.WarmTarget, WarmFree: mp.WarmFree,
		StoppedTarget: mp.StoppedTarget, ReserveReady: mp.ReserveReady, Reason: mp.Reason, States: mp.States,
		FloorShortSince: mp.FloorShortSince,
	}
	var names []string
	for _, a := range actions {
		switch {
		case a.Host != nil:
			names = append(names, string(a.Kind)+" "+a.Host.String())
		case a.Offer != nil:
			names = append(names, string(a.Kind)+" "+a.Offer.Key())
		}
	}
	out.Decision = fmt.Sprintf("load %s, running free %s of %s, starting %s, reserve ready %s of %s, preparing %s, short %s and %s, actions [%s], reason %q",
		describe(mp.Load), describe(mp.WarmFree), describe(mp.WarmTarget), describe(mp.WarmPending), describe(mp.ReserveReady),
		describe(mp.StoppedTarget), describe(mp.ReservePending), describe(mp.Shortfall), describe(mp.StoppedShortfall),
		strings.Join(names, ", "), mp.Reason)
	return out
}

func describe(c FleetCapacity) string {
	s := fmt.Sprintf("%gvCPU/%gGiB", float64(c.CPUMillis)/1000, float64(c.MemoryBytes)/float64(gib))
	if c.GPUs > 0 {
		s += fmt.Sprintf("/%dGPU", c.GPUs)
	}
	return s
}

// marketRow is a fleet_markets row as UpsertFleetMarkets takes it.
type marketRow struct {
	Market      string          `json:"market"`
	Plan        json.RawMessage `json:"plan"`
	GeneratedAt time.Time       `json:"generated_at"`
	ExpiresAt   time.Time       `json:"expires_at"`
}

// PublishedPlan returns every market's last published plan, current or
// expired; the caller treats an expired one as no plan.
func (c *Compute) PublishedPlan(ctx context.Context) ([]PublishedMarket, error) {
	rows, err := c.queries.FleetMarkets(ctx)
	if err != nil {
		return nil, fmt.Errorf("read fleet markets: %w", err)
	}
	out := make([]PublishedMarket, 0, len(rows))
	for _, r := range rows {
		var m PublishedMarket
		if err := json.Unmarshal(r.Plan, &m); err != nil {
			return nil, fmt.Errorf("decode the plan of market %s: %w", r.Market, err)
		}
		m.GeneratedAt, m.ExpiresAt = r.GeneratedAt, r.ExpiresAt
		out = append(out, m)
	}
	return out, nil
}
