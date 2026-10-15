package compute

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// TestPlanOnProdSnapshot runs PlanFleet on a live stack's snapshot, read
// only, and prints its decisions: the way to see why prod's planner did
// what it did. It needs PROD_DB (the stack's database URL) and the
// scheduler's LAZYCLOUD_FLEET_* and LAZYCLOUD_OBJECT_STORE_REGION
// environment, and skips without them.
func TestPlanOnProdSnapshot(t *testing.T) {
	url := os.Getenv("PROD_DB")
	if url == "" {
		t.Skip("PROD_DB unset")
	}
	ctx := context.Background()
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		t.Fatal(err)
	}
	cfg.ConnConfig.RuntimeParams["default_transaction_read_only"] = "on"
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	fleet, err := LoadFleet(ctx, os.Getenv)
	if err != nil {
		t.Fatal(err)
	}
	p := DefaultPolicy()
	now := time.Now()
	cooldown := 10 * time.Minute
	r, err := readFleet(ctx, New(pool), p, now, cooldown)
	if err != nil {
		t.Fatal(err)
	}
	groups, err := pendingGroups(r.pending)
	if err != nil {
		t.Fatal(err)
	}
	var hosts []FleetHost
	for _, row := range r.hosts {
		if ownedBy(row, nil) && !stuckPreparing(row, now) {
			hosts = append(hosts, fleetHostOf(row, now))
		}
	}
	var pending []DemandGroup
	for _, g := range groups {
		if g.connection == nil {
			pending = append(pending, g.group)
		}
	}
	in := r.inputs(fleet.StorageRegion, fleet.Networks, nil, hosts)
	held, reserves := 0, 0
	for _, h := range hosts {
		if h.reserve() {
			reserves++
		}
		if !h.reserve() || h.State == FleetPreparing || h.State == FleetStopping {
			held++
		}
	}
	recent, arrived, disks := shapesByMarket(r.recent)
	s := FleetSnapshot{Now: now, Hosts: hosts, Pending: pending, Recent: recent, RecentDisks: disks, Arrived: arrived, Offers: in,
		HostRoom: max(0, fleet.MaxHosts-held), ReserveRoom: max(0, fleet.MaxHosts-reserves), BatchWait: r.batchWait}
	s.Holds = (&fleetPass{r: r}).holds(nil)
	fmt.Printf("HOSTS %d (held %d reserves %d) pending groups %d offers spot=%d cooldowns=%d\n", len(hosts), held, reserves, len(pending), len(in.Spot), len(in.Cooldowns))
	for _, h := range hosts {
		if h.GPU == "L4" {
			fmt.Printf("HOST %s %s %s %s state=%s protected=%v stoppable=%v requested=%v usable=%+v\n", h.ID, h.market(), h.Region, h.InstanceType, h.State, h.Protected, h.Stoppable, h.Requested, h.Usable)
		}
	}
	plan, _ := planOwner(p, s, cooldown)
	for _, a := range plan.Actions {
		desc := ""
		if a.Offer != nil {
			desc = a.Offer.Region + "/" + a.Offer.ZoneID + "/" + a.Offer.Type.Name
		}
		fmt.Printf("ACTION %s %s %s %s host=%v\n", a.Market, a.Kind, a.Reason, desc, a.Host)
	}
	for _, mp := range plan.Markets {
		fmt.Printf("MARKET %s load=%+v stopped=%+v ready=%+v pending=%+v short=%+v/%+v reason=%q\n", mp.Market, mp.Load.GPUs, mp.StoppedTarget, mp.ReserveReady.GPUs, mp.ReservePending.GPUs, mp.Shortfall.CPUMillis, mp.StoppedShortfall, mp.Reason)
	}
}
