package compute

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
)

// PublishedMarket is one market's plan as the planner publishes it in
// fleet_markets, for every replica and the admin page.
type PublishedMarket struct {
	Preemptible           bool                 `json:"preemptible"`
	GPUType               string               `json:"gpu_type"`
	Quiet                 bool                 `json:"quiet"`
	Load                  FleetCapacity        `json:"load"`
	WarmTarget            FleetCapacity        `json:"warm_target"`
	WarmFree              FleetCapacity        `json:"warm_free"`
	WarmPending           FleetCapacity        `json:"warm_pending"`
	StoppedTarget         FleetCapacity        `json:"stopped_target"`
	ReserveCapacity       FleetCapacity        `json:"reserve_capacity"`
	ReserveReady          FleetCapacity        `json:"reserve_ready"`
	ReservePending        FleetCapacity        `json:"reserve_pending"`
	HibernationTarget     FleetCapacity        `json:"hibernation_target"`
	Hibernated            FleetCapacity        `json:"hibernated"`
	HibernationUnverified FleetCapacity        `json:"hibernation_unverified"`
	Shortfall             FleetCapacity        `json:"shortfall"`
	StoppedShortfall      FleetCapacity        `json:"stopped_shortfall"`
	HibernationShortfall  FleetCapacity        `json:"hibernation_shortfall"`
	UnmetShapes           []FleetCapacity      `json:"unmet_shapes"`
	UnmetStoppedShapes    []FleetCapacity      `json:"unmet_stopped_shapes"`
	Reason                MarketReason         `json:"reason"`
	States                []FleetStateCapacity `json:"states"`
	// Consolidating is the host the market is draining onto the others.
	Consolidating *uuid.UUID `json:"consolidating,omitempty"`
	// Decision summarizes the market's plan; the planner logs it when it
	// changes.
	Decision string `json:"decision"`
	// GeneratedAt and ExpiresAt bound the plan; after ExpiresAt it is no
	// plan.
	GeneratedAt time.Time `json:"-"`
	ExpiresAt   time.Time `json:"-"`
}

// publishedMarket is the published form of a market plan.
func publishedMarket(mp MarketPlan, growth []FleetAction) PublishedMarket {
	out := PublishedMarket{
		Preemptible: mp.Market.Preemptible, GPUType: mp.Market.GPU, Quiet: mp.Quiet, Load: mp.Load,
		WarmTarget: mp.WarmTarget, WarmFree: mp.WarmFree, WarmPending: mp.WarmPending,
		StoppedTarget: mp.StoppedTarget, ReserveCapacity: mp.ReserveCapacity,
		ReserveReady: mp.ReserveReady, ReservePending: mp.ReservePending,
		HibernationTarget: mp.HibernationTarget, Hibernated: mp.Hibernated,
		HibernationUnverified: mp.HibernationUnverified, Shortfall: mp.Shortfall,
		StoppedShortfall: mp.StoppedShortfall, HibernationShortfall: mp.HibernationShortfall,
		UnmetShapes: mp.UnmetShapes, UnmetStoppedShapes: mp.UnmetStoppedShapes,
		Reason: mp.Reason, States: mp.States,
	}
	if mp.Consolidates != nil {
		out.Consolidating = ptr(uuid.UUID(*mp.Consolidates))
	}
	out.Decision = decision(mp, growth)
	return out
}

// decision is the line logged when a market's plan changes: its load, its
// running and stopped headroom against their targets, its actions and its
// reason.
func decision(mp MarketPlan, growth []FleetAction) string {
	quiet := "loaded"
	if mp.Quiet {
		quiet = "quiet"
	}
	var actions []string
	for _, a := range growth {
		switch {
		case a.Host != nil:
			actions = append(actions, string(a.Kind)+" "+a.Host.String())
		case a.Offer != nil:
			actions = append(actions, string(a.Kind)+" "+a.Offer.Key())
		}
	}
	moving := "none"
	switch {
	case mp.Consolidates != nil:
		moving = mp.Consolidates.String()
	case mp.ConsolidationCandidate != nil:
		moving = "candidate " + mp.ConsolidationCandidate.String()
	}
	return fmt.Sprintf("%s, load %s, running free %s of %s, reserve ready %s of %s, preparing %s, image saved %s, "+
		"hibernation unverified %s, actions [%s], unmet shapes %d, stopped shapes %d, stopped shortfall %s, reason %q, consolidating %s",
		quiet, describe(mp.Load), describe(mp.WarmFree), describe(mp.WarmTarget), describe(mp.ReserveReady), describe(mp.StoppedTarget),
		describe(mp.ReservePending), describe(mp.Hibernated), describe(mp.HibernationUnverified), strings.Join(actions, ", "),
		len(mp.UnmetShapes), len(mp.UnmetStoppedShapes), describe(mp.StoppedShortfall), mp.Reason, moving)
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
	Market                     string          `json:"market"`
	Plan                       json.RawMessage `json:"plan"`
	GeneratedAt                time.Time       `json:"generated_at"`
	ExpiresAt                  time.Time       `json:"expires_at"`
	PressureSince              *time.Time      `json:"pressure_since"`
	ConsolidatingHost          *uuid.UUID      `json:"consolidating_host"`
	ConsolidationStartedAt     *time.Time      `json:"consolidation_started_at"`
	ConsolidationCooldownUntil *time.Time      `json:"consolidation_cooldown_until"`
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
