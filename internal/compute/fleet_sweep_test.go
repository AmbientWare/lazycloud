package compute

import (
	"fmt"
	"os"
	"runtime"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

// TestFleetPolicySweep runs policy variants over the platform scenarios;
// FLEET_SWEEP names the scenarios, semicolon-separated name prefixes.
func TestFleetPolicySweep(t *testing.T) {
	want := os.Getenv("FLEET_SWEEP")
	if want == "" {
		t.Skip("FLEET_SWEEP names no scenarios")
	}
	var scenarios []platformScenario
	for _, sc := range platformScenarios() {
		for _, w := range strings.Split(want, ";") {
			if strings.HasPrefix(sc.name, w) {
				scenarios = append(scenarios, sc)
			}
		}
	}
	type job struct {
		variant, scenario int
		seed              uint64
	}
	variants := sweepVariants()
	results := make([][][]platformOutcome, len(variants))
	var jobs []job
	for v := range variants {
		results[v] = make([][]platformOutcome, len(scenarios))
		for s, sc := range scenarios {
			if variants[v].only != "" && !strings.Contains(sc.name, variants[v].only) {
				continue
			}
			results[v][s] = make([]platformOutcome, sc.seeds)
			for n := range sc.seeds {
				jobs = append(jobs, job{v, s, uint64(n) + 1})
			}
		}
	}
	work := make(chan job)
	var wg sync.WaitGroup
	for range runtime.NumCPU() {
		wg.Go(func() {
			for j := range work {
				sc, v := scenarios[j.scenario], variants[j.variant]
				setup := sc.setup
				sc.setup = func(t *testing.T, s *sim) {
					if setup != nil {
						setup(t, s)
					}
					v.policy(&s.p)
				}
				results[j.variant][j.scenario][j.seed-1] = runPlatform(t, sc, j.seed)
			}
		})
	}
	started := time.Now()
	for _, j := range jobs {
		work <- j
	}
	close(work)
	wg.Wait()
	t.Logf("%d runs in %s", len(jobs), time.Since(started).Round(time.Second))
	for s, sc := range scenarios {
		for v, variant := range variants {
			outs := results[v][s]
			if len(outs) == 0 {
				continue
			}
			var profit, lost float64
			pooled := map[string][]time.Duration{}
			for _, o := range outs {
				profit += o.profit() / float64(len(outs))
				lost += float64(o.lost) / float64(len(outs))
				for fn, w := range o.waits {
					pooled[fn] = append(pooled[fn], w...)
				}
			}
			var starts []string
			for _, fn := range slices.Sorted(func(yield func(string) bool) {
				for fn := range pooled {
					if !yield(fn) {
						return
					}
				}
			}) {
				starts = append(starts, fmt.Sprintf("%s %.0f/%.0f", fn, quantile(pooled[fn], 0.5).Seconds(), quantile(pooled[fn], 0.95).Seconds()))
			}
			t.Logf("SWEEP|%s|%s|%.2f|%.1f|%s", sc.name, variant.name, profit, lost, strings.Join(starts, " "))
		}
	}
}

// sweepVariant is a policy the sweep measures; only, when set, limits it
// to scenarios whose name contains it.
type sweepVariant struct {
	name   string
	only   string
	policy func(*Policy)
}
