package compute

import "time"

// The fleet simulation's glue to the planner: what it reads from the
// policy and what the planner's published plans carry between passes.

// simWindow is how long market m keeps capacity once it grows.
func simWindow(p Policy, m ReserveMarket) time.Duration { return p.Window(m) }

// simSpotWait is how long Spot-tolerant GPU work waits for Spot before it
// buys on-demand.
func simSpotWait(p Policy) time.Duration { return p.SpotWait }

// simQueueTimeout is how long pending work waits for capacity before
// execution ends it; zero waits without limit.
func simQueueTimeout(p Policy) time.Duration { return p.QueueTimeout }

// simFloor is the room placement keeps free on an on-demand CPU host.
func simFloor(p Policy) FleetCapacity { return p.OnDemand.Running.Floor }

// simArrivalWindow is how far back RecentShapes counts arrivals, and
// simArrivalMarkets the markets it counts them for.
func simArrivalWindow(p Policy) time.Duration { return p.ArrivalWindow() }
func simArrivalMarkets() []ReserveMarket      { return []ReserveMarket{{Preemptible: true}, {}} }

// simCounted is when RecentShapes counts a placed container as arrived.
func simCounted(c *simContainer) time.Time { return c.started }

// simPending is a pending container as PendingDemand reads it.
func simPending(c *simContainer) PendingContainer {
	return PendingContainer{ID: c.id, Since: c.arrived, Host: c.host}
}

// simBusy records a container a host's fleet view counts.
func simBusy(*FleetHost, *simContainer) {}

// simCarry is what each market's published plan carries to the next pass.
type simCarry struct{ holds map[ReserveMarket]MarketHolds }

func (c *simCarry) into(s *FleetSnapshot)          { s.Holds = c.holds }
func (c *simCarry) idle(_ []*simHost, _ FleetPlan) {}
func (c *simCarry) from(plan FleetPlan) {
	c.holds = map[ReserveMarket]MarketHolds{}
	for _, mp := range plan.Markets {
		c.holds[mp.Market] = mp.Holds
	}
}
