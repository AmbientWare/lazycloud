package compute

import (
	"fmt"
	"time"
)

// sweepVariants are the headroom settings the sweep measures: each
// market's warm Lead and Memory, and its stopped Memory, one market at a
// time beside the defaults.
func sweepVariants() []sweepVariant {
	out := []sweepVariant{{name: "default", policy: func(*Policy) {}}}
	markets := []struct{ name, only string }{{"spot", ""}, {"on-demand", ""}, {"gpu", "GPU"}}
	for _, m := range markets {
		for _, lead := range []time.Duration{30 * time.Second, time.Minute, 2 * time.Minute} {
			for _, memory := range []time.Duration{0, 2 * time.Minute, 5 * time.Minute, 10 * time.Minute, 30 * time.Minute} {
				out = append(out, sweepVariant{name: fmt.Sprintf("%s warm %s/%s", m.name, lead, memory), only: m.only, policy: func(p *Policy) {
					setReserves(p, m.name, func(r *MarketReserve) { r.Warm.Lead, r.Warm.Memory = lead, memory })
				}})
			}
		}
		for _, memory := range []time.Duration{15 * time.Minute, 30 * time.Minute, time.Hour} {
			out = append(out, sweepVariant{name: fmt.Sprintf("%s stopped 5m/%s", m.name, memory), only: m.only, policy: func(p *Policy) {
				setReserves(p, m.name, func(r *MarketReserve) { r.Stopped.Lead, r.Stopped.Memory = 5*time.Minute, memory })
			}})
		}
	}
	return out
}

// setReserves changes the reserves of the named market or markets.
func setReserves(p *Policy, market string, f func(*MarketReserve)) {
	switch market {
	case "spot":
		f(&p.Spot)
	case "on-demand":
		f(&p.OnDemand)
	case "gpu":
		gpu := map[string]MarketReserve{}
		for model, r := range p.GPU {
			f(&r)
			gpu[model] = r
		}
		p.GPU = gpu
	}
}
