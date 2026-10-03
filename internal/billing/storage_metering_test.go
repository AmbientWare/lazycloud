package billing

import (
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

func TestContainersPriceTheirGPUPlacementAndMachine(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	// Stopped now, as a stop sets it, so each pass's look-back finds it.
	stopped := time.Now().UTC().Truncate(time.Microsecond)
	base := stopped.Add(-10 * time.Minute)
	host := f.host(time.Now())
	for _, c := range []struct {
		owner BillingOwner
		class RateClass
		gpu   GPUType
		cards int
	}{
		{OwnerPlatformFleet, ClassNonPreemptible, GPUT4, 1},
		{OwnerPlatformFleet, ClassPinned, noGPU, 0},
		{OwnerConnectedCloud, ClassAuto, GPUA10G, 2},
		{OwnerSelfHosted, ClassAuto, GPUH100, 1},
	} {
		id := f.container(containerSpec{workspace: ws, host: host, release: &rel.id, ready: base, stopped: &stopped, cpuMillis: 2000, memoryBytes: 8 << 30})
		f.exec("update containers set billing_owner = $2, rate_class = $3, gpu_type = $4, gpu_count = $5 where id = $1",
			id, string(c.owner), string(c.class), string(c.gpu), c.cards)
		f.meter()
		var got int64
		for _, e := range f.ledger(id) {
			got += e.cost
		}
		shape := Shape{Owner: c.owner, Class: c.class, GPU: c.gpu, GPUCount: c.cards, CPUMillis: 2000, MemoryBytes: 8 << 30}
		want := f.expected(base, shape, 10*time.Minute)
		if abs(got-want) > 4 || (want > 0 && got == 0) {
			t.Fatalf("%+v: cost %d, want %d", c, got, want)
		}
		if c.owner == OwnerSelfHosted && want != 0 {
			t.Fatalf("a joined machine priced %d", want)
		}
	}
	// A non-preemptible CPU container costs three times an automatic one.
	card, err := f.billing.rates.cardAt(base)
	if err != nil {
		t.Fatal(err)
	}
	auto, _ := card.computeRate(OwnerPlatformFleet, ClassAuto, noGPU)
	firm, _ := card.computeRate(OwnerPlatformFleet, ClassNonPreemptible, noGPU)
	pinned, _ := card.computeRate(OwnerPlatformFleet, ClassPinnedNonPreemptible, noGPU)
	if firm.CPUCoreHour != 3*auto.CPUCoreHour || pinned.CPUCoreHour*2 != 9*auto.CPUCoreHour {
		t.Fatalf("multipliers: auto %d, non-preemptible %d, pinned non-preemptible %d", auto.CPUCoreHour, firm.CPUCoreHour, pinned.CPUCoreHour)
	}
	// A GPU container whose model placement never recorded is not priced
	// as free; it waits.
	unknown := f.container(containerSpec{workspace: ws, host: host, release: &rel.id, ready: base, stopped: &stopped, cpuMillis: 1000, memoryBytes: 1 << 30})
	f.exec("update containers set gpu_count = 1 where id = $1", unknown)
	if result := f.meter(); result.Failed != 1 || len(f.ledger(unknown)) != 0 {
		t.Fatalf("unknown GPU model: %+v", result)
	}
}

func TestAdmitCountsGPUsInTheirOwnPool(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	f.setAccount(owner, "payment_method_attached_at = now()")
	f.exec(`insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes, gpu_count)
		select $1, $2, 'pending', 1, 1000, 1 << 30, 2 from generate_series(1, 2)`, ws, rel.id)
	// Free with a card: five cards. Four are held; one two-card container
	// would pass the limit.
	if _, err := f.admit(Request{Workspace: ws, Cold: true, GPUs: 2}); !limitReached(err) {
		t.Fatalf("GPU past the limit: %v", err)
	}
	if grant, err := f.admit(Request{Workspace: ws, Start: 3, GPUs: 1}); err != nil || grant.Start != 1 {
		t.Fatalf("grant %+v %v; want 1", grant, err)
	}
	// The GPU pool leaves the CPU pool alone.
	if grant, err := f.admit(Request{Workspace: ws, Start: 5}); err != nil || grant.Start != 5 {
		t.Fatalf("CPU grant %+v %v", grant, err)
	}
	// Without a card only the trial models, and region selection needs Team.
	f.setAccount(owner, "payment_method_attached_at = null")
	if _, err := f.admit(Request{Workspace: ws, GPUs: 1, GPUModels: []GPUType{GPUL40S}}); !paymentRequired(err) {
		t.Fatalf("L40S without a card: %v", err)
	}
	grant, err := f.admit(Request{Workspace: ws, GPUs: 1, GPUModels: []GPUType{GPUAny}})
	if err != nil || len(grant.GPUModels) != 3 {
		t.Fatalf("any model without a card: %+v %v", grant, err)
	}
	if _, err := f.admit(Request{Workspace: ws, Pinned: true}); !paymentRequired(err) {
		t.Fatalf("pinned region on Free: %v", err)
	}
	f.setAccount(owner, "terms_version = 'team-v3'")
	if _, err := f.admit(Request{Workspace: ws, Pinned: true}); err != nil {
		t.Fatalf("pinned region on Team: %v", err)
	}
}

// A model not offered yet is refused at admission with a typed error, so its
// work never waits for capacity the fleet will not buy; one offered model in
// the preference admits the work on that model alone.
func TestAdmitRefusesGPUModelsNotOfferedYet(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	f.setAccount(owner, "payment_method_attached_at = now()")
	_, err := f.admit(Request{Workspace: ws, Cold: true, GPUs: 1, GPUModels: []GPUType{GPUH100, GPUA10080}})
	var unavailable *GPUUnavailableError
	if !errors.As(err, &unavailable) || !strings.Contains(err.Error(), "H100, A100-80 are coming soon") {
		t.Fatalf("H100 or A100-80: %v", err)
	}
	grant, err := f.admit(Request{Workspace: ws, GPUs: 1, GPUModels: []GPUType{GPUH100, GPUL4}})
	if err != nil || !slices.Equal(grant.GPUModels, []GPUType{GPUL4}) {
		t.Fatalf("H100 then L4: %+v %v", grant, err)
	}
	grant, err = f.admit(Request{Workspace: ws, GPUs: 1, GPUModels: []GPUType{GPUAny}})
	if err != nil || !slices.Equal(grant.GPUModels, []GPUType{GPUT4, GPUA10G, GPUL4, GPUL40S}) {
		t.Fatalf("any: %+v %v", grant, err)
	}
	catalog, err := f.billing.Catalog(time.Now())
	if err != nil {
		t.Fatal(err)
	}
	for _, rate := range catalog.GpuRates {
		if rate.Enabled != GPUEnabled(rate.GpuType) || rate.NanosPerCardHour.PlatformFleet <= 0 {
			t.Errorf("catalog GPU %+v", rate)
		}
	}
}

func TestStorageIsMeteredAndShownOnTheUsagePage(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	created := time.Now().UTC().Truncate(time.Hour).Add(-3 * time.Hour)
	var volume, disk uuid.UUID
	if err := f.pool.QueryRow(t.Context(), "insert into volumes (workspace_id, name, size_bytes, created_at) values ($1, 'data', $2, $3) returning id",
		ws, int64(10<<30), created).Scan(&volume); err != nil {
		t.Fatal(err)
	}
	if err := f.pool.QueryRow(t.Context(), "insert into disks (workspace_id, name, size_bytes, stored_bytes, created_at) values ($1, 'root', $2, $3, $4) returning id",
		ws, int64(100<<30), int64(5<<30), created).Scan(&disk); err != nil {
		t.Fatal(err)
	}
	f.exec(`insert into artifacts (workspace_id, app_id, filename, content_type, size_bytes, state, retention_seconds, stored_at, expires_at)
		values ($1, $2, 'report.html', 'text/html', $3, 'stored', 86400, $4::timestamptz, $4::timestamptz + interval '1 day')`, ws, rel.app, int64(2<<30), created)
	f.meter()

	closed := time.Now().UTC().Truncate(MeteringPeriod)
	held := closed.Sub(created)
	card, err := f.billing.rates.cardAt(created)
	if err != nil {
		t.Fatal(err)
	}
	cost := func(kind string, id uuid.UUID) int64 {
		t.Helper()
		var total int64
		if err := f.pool.QueryRow(t.Context(), "select coalesce(sum(cost_nanos), 0) from ledger_entries where source_kind = $1 and source_id = $2", kind, id).Scan(&total); err != nil {
			t.Fatal(err)
		}
		return total
	}
	if got, want := cost(kindVolume, volume), storageCost(card.Platform.VolumeGiBMonth, 10<<30, held); abs(got-want) > 20 {
		t.Fatalf("volume cost %d, want about %d", got, want)
	}
	if got, want := cost(kindDisk, disk), storageCost(card.Disk.StoredGiBMonth, 5<<30, held); abs(got-want) > 20 {
		t.Fatalf("an unheld disk costs %d, want its stored bytes' %d", got, want)
	}
	if got, want := cost(kindArtifacts, rel.app), storageCost(card.Platform.VolumeGiBMonth, 2<<30, held); abs(got-want) > 20 {
		t.Fatalf("artifacts cost %d, want about %d", got, want)
	}
	// A repeated pass writes nothing more.
	before := len(f.ledger(volume))
	f.meter()
	if after := len(f.ledger(volume)); after != before {
		t.Fatalf("a repeated pass wrote %d entries", after-before)
	}

	start, end := created.Truncate(24*time.Hour), closed.Add(time.Hour)
	page, err := f.billing.Costs(t.Context(), owner, CostQuery{Start: start, End: end, GroupBy: apitypes.UsageCostGroupApp, Limit: 50})
	if err != nil {
		t.Fatal(err)
	}
	rows := map[string]apitypes.UsageCostRow{}
	for _, r := range page.Rows {
		key := "app"
		switch {
		case r.Category != nil:
			key = string(*r.Category)
		case r.AppId == nil:
			key = "none"
		}
		rows[key] = r
	}
	if r := rows[string(apitypes.UsageCostCategoryDisk)]; r.DiskId == nil || *r.DiskId != disk || r.DiskName == nil || *r.DiskName != "root" ||
		len(r.Components) != 1 || r.Components[0].Component != apitypes.UsageCostComponentKindDisk {
		t.Fatalf("disk row %+v", r)
	}
	if r := rows[string(apitypes.UsageCostCategoryUnattributed)]; len(r.Components) != 1 || r.Components[0].Dimension != apitypes.BilledDimensionVolumeStorage {
		t.Fatalf("volume row %+v", r)
	}
	if r := rows["app"]; r.AppId == nil || *r.AppId != rel.app {
		t.Fatalf("app row with its artifacts %+v", r)
	}
	byWorkload, err := f.billing.Costs(t.Context(), owner, CostQuery{Start: start, End: end, GroupBy: apitypes.UsageCostGroupWorkload, App: &rel.app, Limit: 50})
	if err != nil || len(byWorkload.Rows) != 1 || byWorkload.Rows[0].WorkloadName == nil || *byWorkload.Rows[0].WorkloadName != "Artifacts" {
		t.Fatalf("an app's artifacts by workload: %+v %v", byWorkload.Rows, err)
	}
	series, err := f.billing.CostSeries(t.Context(), owner, start, start.Add(48*time.Hour), apitypes.Day)
	if err != nil {
		t.Fatal(err)
	}
	dimensions := map[apitypes.BilledDimension]bool{}
	for _, i := range series.Intervals {
		for _, d := range i.Dimensions {
			dimensions[d.Dimension] = true
		}
	}
	if !dimensions[apitypes.BilledDimensionDisk] || !dimensions[apitypes.BilledDimensionVolumeStorage] || series.CostNanos != page.CostNanos {
		t.Fatalf("series dimensions %v, cost %d against the page's %d", dimensions, series.CostNanos, page.CostNanos)
	}
}

func TestEgressIsPricedOncePerClosedQuarter(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	old := time.Now().UTC().Add(-time.Hour)
	for range 2 {
		if err := RecordEgress(t.Context(), f.pool, Egress{Workspace: ws, App: &rel.app, Workload: &rel.workload, Bytes: 1 << 30, At: old}); err != nil {
			t.Fatal(err)
		}
	}
	// The current quarter is still open to the edge.
	if err := RecordEgress(t.Context(), f.pool, Egress{Workspace: ws, Bytes: 1 << 30, At: time.Now()}); err != nil {
		t.Fatal(err)
	}
	f.meter()
	f.meter()
	var bytes, nanos int64
	var entries int
	if err := f.pool.QueryRow(t.Context(), "select count(*), coalesce(sum(egress_bytes), 0), coalesce(sum(egress_nanos), 0) from ledger_entries where source_kind = 'egress'").
		Scan(&entries, &bytes, &nanos); err != nil {
		t.Fatal(err)
	}
	card, err := f.billing.rates.cardAt(old)
	if err != nil {
		t.Fatal(err)
	}
	if entries != 1 || bytes != 2<<30 || nanos != 2*card.Platform.EgressGiB {
		t.Fatalf("%d egress entries of %d bytes costing %d, want one of 2 GiB costing %d", entries, bytes, nanos, 2*card.Platform.EgressGiB)
	}
	var pending int
	if err := f.pool.QueryRow(t.Context(), "select count(*) from egress_quarters").Scan(&pending); err != nil || pending != 1 {
		t.Fatalf("%d open egress quarters, want the current one: %v", pending, err)
	}
}

func TestHeldDisksPayForTheirSizeAndRetainedDataIsFree(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	host := f.host(time.Now())
	holder := f.container(containerSpec{workspace: ws, host: host, release: &rel.id, ready: time.Now(), cpuMillis: 1000, memoryBytes: 1 << 30})
	created := time.Now().UTC().Truncate(time.Hour).Add(-time.Hour)
	var disk uuid.UUID
	if err := f.pool.QueryRow(t.Context(), `insert into disks (workspace_id, name, size_bytes, stored_bytes, created_at, holder_container_id, lease_token)
		values ($1, 'root', $2, 0, $3, $4, 'lease') returning id`, ws, int64(100<<30), created, holder).Scan(&disk); err != nil {
		t.Fatal(err)
	}
	var volume uuid.UUID
	if err := f.pool.QueryRow(t.Context(), "insert into volumes (workspace_id, name, size_bytes, created_at) values ($1, 'data', $2, $3) returning id",
		ws, int64(10<<30), created).Scan(&volume); err != nil {
		t.Fatal(err)
	}
	f.account(owner)
	f.exec("insert into unfunded_periods (user_id) values ($1)", owner)
	f.meter()
	var attached, volumeCost int64
	if err := f.pool.QueryRow(t.Context(), "select coalesce(sum(attached_nanos), 0) from ledger_entries where source_id = $1", disk).Scan(&attached); err != nil {
		t.Fatal(err)
	}
	if err := f.pool.QueryRow(t.Context(), "select coalesce(sum(cost_nanos), 0) from ledger_entries where source_id = $1", volume).Scan(&volumeCost); err != nil {
		t.Fatal(err)
	}
	if attached != 0 || volumeCost != 0 {
		t.Fatalf("during unfunded retention the disk cost %d attached and the volume %d", attached, volumeCost)
	}
	// With credit the held disk pays for its declared size.
	f.exec("delete from unfunded_periods")
	f.exec("delete from ledger_entries; delete from usage_cursors")
	f.meter()
	if err := f.pool.QueryRow(t.Context(), "select coalesce(sum(attached_nanos), 0) from ledger_entries where source_id = $1", disk).Scan(&attached); err != nil {
		t.Fatal(err)
	}
	card, err := f.billing.rates.cardAt(created)
	if err != nil {
		t.Fatal(err)
	}
	if want := storageCost(card.Disk.AttachedGiBMonth, 100<<30, time.Now().UTC().Truncate(MeteringPeriod).Sub(created)); abs(attached-want) > 20 {
		t.Fatalf("held disk attached cost %d, want about %d", attached, want)
	}
}
