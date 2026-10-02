package compute

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
)

// PlanCapacity is CPU, memory and GPUs in a published plan.
type PlanCapacity struct {
	CPUMillis   int64 `json:"cpu_millis"`
	MemoryBytes int64 `json:"memory_bytes"`
	GPUs        int   `json:"gpus"`
}

func planCapacity(c FleetCapacity) PlanCapacity {
	return PlanCapacity(c)
}

func planCapacities(cs []FleetCapacity) []PlanCapacity {
	out := make([]PlanCapacity, len(cs))
	for i, c := range cs {
		out[i] = planCapacity(c)
	}
	return out
}

// PlanState totals one state's hosts in a published market.
type PlanState struct {
	State     FleetState   `json:"state"`
	Machines  int          `json:"machines"`
	Capacity  PlanCapacity `json:"capacity"`
	Allocated PlanCapacity `json:"allocated"`
}

// PublishedMarket is one market's plan as the planner publishes it in
// fleet_markets, for every replica and the admin page.
type PublishedMarket struct {
	Preemptible           bool           `json:"preemptible"`
	GPUType               string         `json:"gpu_type"`
	Quiet                 bool           `json:"quiet"`
	Load                  PlanCapacity   `json:"load"`
	WarmTarget            PlanCapacity   `json:"warm_target"`
	WarmFree              PlanCapacity   `json:"warm_free"`
	WarmPending           PlanCapacity   `json:"warm_pending"`
	StoppedTarget         PlanCapacity   `json:"stopped_target"`
	ReserveCapacity       PlanCapacity   `json:"reserve_capacity"`
	ReserveReady          PlanCapacity   `json:"reserve_ready"`
	ReservePending        PlanCapacity   `json:"reserve_pending"`
	HibernationTarget     PlanCapacity   `json:"hibernation_target"`
	Hibernated            PlanCapacity   `json:"hibernated"`
	HibernationUnverified PlanCapacity   `json:"hibernation_unverified"`
	Shortfall             PlanCapacity   `json:"shortfall"`
	StoppedShortfall      PlanCapacity   `json:"stopped_shortfall"`
	HibernationShortfall  PlanCapacity   `json:"hibernation_shortfall"`
	UnmetShapes           []PlanCapacity `json:"unmet_shapes"`
	UnmetStoppedShapes    []PlanCapacity `json:"unmet_stopped_shapes"`
	Reason                MarketReason   `json:"reason"`
	States                []PlanState    `json:"states"`
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
		Preemptible: mp.Market.Preemptible, GPUType: mp.Market.GPU, Quiet: mp.Quiet, Load: planCapacity(mp.Load),
		WarmTarget: planCapacity(mp.WarmTarget), WarmFree: planCapacity(mp.WarmFree), WarmPending: planCapacity(mp.WarmPending),
		StoppedTarget: planCapacity(mp.StoppedTarget), ReserveCapacity: planCapacity(mp.ReserveCapacity),
		ReserveReady: planCapacity(mp.ReserveReady), ReservePending: planCapacity(mp.ReservePending),
		HibernationTarget: planCapacity(mp.HibernationTarget), Hibernated: planCapacity(mp.Hibernated),
		HibernationUnverified: planCapacity(mp.HibernationUnverified), Shortfall: planCapacity(mp.Shortfall),
		StoppedShortfall: planCapacity(mp.StoppedShortfall), HibernationShortfall: planCapacity(mp.HibernationShortfall),
		UnmetShapes: planCapacities(mp.UnmetShapes), UnmetStoppedShapes: planCapacities(mp.UnmetStoppedShapes),
		Reason: mp.Reason,
	}
	for _, s := range mp.States {
		out.States = append(out.States, PlanState{State: s.State, Machines: s.Machines, Capacity: planCapacity(s.Capacity), Allocated: planCapacity(s.Allocated)})
	}
	if mp.Consolidates != nil {
		out.Consolidating = ptr(uuid.UUID(*mp.Consolidates))
	}
	out.Decision = decision(mp, growth)
	return out
}

// decision is the line logged when a market's plan changes, as the
// reference logged it.
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
