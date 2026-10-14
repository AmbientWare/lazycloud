package compute

import (
	"encoding/json"
	"fmt"
	"os"
	"sync"
	"time"

	"github.com/google/uuid"
)

// Temporary: dumps every simulated pass's decisions to compare planners.
var dumpMu sync.Mutex

type dumpIDs map[uuid.UUID]int

func (d dumpIDs) of(id uuid.UUID) int {
	if n, ok := d[id]; ok {
		return n
	}
	d[id] = len(d)
	return d[id]
}

var dumpNames = map[*sim]dumpIDs{}

func dumpPlan(s *sim, plan FleetPlan) {
	path := os.Getenv("FLEET_DUMP")
	if path == "" {
		return
	}
	dumpMu.Lock()
	defer dumpMu.Unlock()
	ids := dumpNames[s]
	if ids == nil {
		ids = dumpIDs{}
		dumpNames[s] = ids
	}
	host := func(h *HostID) string {
		if h == nil {
			return "-"
		}
		return fmt.Sprint(ids.of(uuid.UUID(*h)))
	}
	var lines []string
	for _, a := range plan.Actions {
		offer := "-"
		if a.Offer != nil {
			offer = a.Offer.Key()
		}
		var cs []int
		for _, c := range a.Containers {
			cs = append(cs, ids.of(c))
		}
		mode := "-"
		if a.Mode != nil {
			mode = string(*a.Mode)
		}
		lines = append(lines, fmt.Sprintf("A %s %s h=%s o=%s m=%s c=%v holds=%v", a.Kind, a.Market, host(a.Host), offer, mode, cs, a.Holds))
	}
	for _, w := range plan.Waits {
		wait := "-"
		if w.Wait != nil {
			wait = string(*w.Wait)
		}
		act := -1
		if w.Action != nil {
			act = *w.Action
		}
		lines = append(lines, fmt.Sprintf("W %d %s h=%s a=%d", ids.of(w.Container), wait, host(w.Host), act))
	}
	for _, m := range plan.Markets {
		m.States = nil
		raw, _ := json.Marshal(m)
		lines = append(lines, "M "+string(raw))
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		panic(err)
	}
	defer f.Close()
	out, _ := json.Marshal(struct {
		Test  string
		At    time.Duration
		Batch time.Duration
		Lines []string
	}{s.t.Name(), s.now.Sub(s.start), plan.BatchWait, lines})
	fmt.Fprintln(f, string(out))
}
