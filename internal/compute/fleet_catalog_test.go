package compute

import (
	"encoding/json"
	"os"
	"testing"
)

// TestFleetCatalogPricesMatchTheAWSPriceList checks every catalog type is
// priced exactly where the recorded AWS price list sells it, and nowhere
// else.
func TestFleetCatalogPricesMatchTheAWSPriceList(t *testing.T) {
	raw, err := os.ReadFile("testdata/fleet/on_demand_prices.json")
	if err != nil {
		t.Fatal(err)
	}
	var list struct {
		HourlyMicros map[string]map[string]int64 `json:"hourly_micros"`
	}
	if err := json.Unmarshal(raw, &list); err != nil {
		t.Fatal(err)
	}
	if len(list.HourlyMicros) != len(FleetCatalog()) {
		t.Fatalf("price list has %d types, catalog %d", len(list.HourlyMicros), len(FleetCatalog()))
	}
	for _, typ := range FleetCatalog() {
		sold, ok := list.HourlyMicros[typ.Name]
		if !ok {
			t.Errorf("%s is not in the price list", typ.Name)
		}
		for _, region := range regionOrder() {
			price, priced := typ.OnDemandMicros(region)
			want, listed := sold[region]
			if priced != listed || price != want {
				t.Errorf("%s in %s: catalog %d (%v), price list %d (%v)", typ.Name, region, price, priced, want, listed)
			}
		}
	}
}

func TestFleetCatalogHibernatesOnlyCPUTypesUnderTheRAMLimit(t *testing.T) {
	for _, typ := range FleetCatalog() {
		if typ.Hibernates && (typ.MemoryBytes >= hibernationMemoryLimit || typ.GPUCount > 0) {
			t.Errorf("%s hibernates with %d GiB and %d GPUs", typ.Name, typ.MemoryBytes/gib, typ.GPUCount)
		}
		want := int64(rootVolumeGiB)
		if typ.Hibernates {
			want += typ.MemoryBytes / gib
		}
		if got := typ.RootGiB(true); got != want {
			t.Errorf("%s hibernating root %d GiB, want %d", typ.Name, got, want)
		}
	}
}

// TestFleetCatalogUsableCapacityFollowsTheAgent pins what each type offers
// containers before any host of it reported, and that a report replaces the
// memory estimate.
func TestFleetCatalogUsableCapacityFollowsTheAgent(t *testing.T) {
	want := map[string]FleetCapacity{
		"m7i.large":     {CPUMillis: 1500, MemoryBytes: 7267084665},
		"m7i.xlarge":    {CPUMillis: 3500, MemoryBytes: 14534169329},
		"c6a.2xlarge":   {CPUMillis: 7200, MemoryBytes: 14534169329},
		"c7i.2xlarge":   {CPUMillis: 7200, MemoryBytes: 14534169329},
		"m6a.2xlarge":   {CPUMillis: 7200, MemoryBytes: 29068338659},
		"m7i.2xlarge":   {CPUMillis: 7200, MemoryBytes: 29068338659},
		"r6a.2xlarge":   {CPUMillis: 7200, MemoryBytes: 58136677318},
		"r7i.2xlarge":   {CPUMillis: 7200, MemoryBytes: 58136677318},
		"c6a.4xlarge":   {CPUMillis: 14400, MemoryBytes: 29068338659},
		"m6a.4xlarge":   {CPUMillis: 14400, MemoryBytes: 58136677318},
		"m7i.4xlarge":   {CPUMillis: 14400, MemoryBytes: 58136677318},
		"r6a.4xlarge":   {CPUMillis: 14400, MemoryBytes: 116273354637},
		"c6a.8xlarge":   {CPUMillis: 28800, MemoryBytes: 58136677318},
		"c6i.8xlarge":   {CPUMillis: 28800, MemoryBytes: 58136677318},
		"m6a.8xlarge":   {CPUMillis: 28800, MemoryBytes: 116273354637},
		"m7i.8xlarge":   {CPUMillis: 28800, MemoryBytes: 116273354637},
		"r6a.8xlarge":   {CPUMillis: 28800, MemoryBytes: 232546709275},
		"m7i.12xlarge":  {CPUMillis: 43200, MemoryBytes: 174410031956},
		"m7i.16xlarge":  {CPUMillis: 57600, MemoryBytes: 232546709275},
		"g4dn.xlarge":   {CPUMillis: 3500, MemoryBytes: 14534169329},
		"g4dn.2xlarge":  {CPUMillis: 7200, MemoryBytes: 29068338659},
		"g4dn.4xlarge":  {CPUMillis: 14400, MemoryBytes: 58136677318},
		"g4dn.8xlarge":  {CPUMillis: 28800, MemoryBytes: 116273354637},
		"g4dn.16xlarge": {CPUMillis: 57600, MemoryBytes: 232546709275},
		"g4dn.12xlarge": {CPUMillis: 43200, MemoryBytes: 174410031956},
		"g4dn.metal":    {CPUMillis: 86400, MemoryBytes: 348820063912},
		"g5.xlarge":     {CPUMillis: 3500, MemoryBytes: 14534169329},
		"g5.2xlarge":    {CPUMillis: 7200, MemoryBytes: 29068338659},
		"g5.4xlarge":    {CPUMillis: 14400, MemoryBytes: 58136677318},
		"g5.8xlarge":    {CPUMillis: 28800, MemoryBytes: 116273354637},
		"g5.16xlarge":   {CPUMillis: 57600, MemoryBytes: 232546709275},
		"g5.12xlarge":   {CPUMillis: 43200, MemoryBytes: 174410031956},
		"g5.24xlarge":   {CPUMillis: 86400, MemoryBytes: 348820063912},
		"g5.48xlarge":   {CPUMillis: 172800, MemoryBytes: 697640127824},
		"g6.xlarge":     {CPUMillis: 3500, MemoryBytes: 14534169329},
		"g6.2xlarge":    {CPUMillis: 7200, MemoryBytes: 29068338659},
		"g6.4xlarge":    {CPUMillis: 14400, MemoryBytes: 58136677318},
		"g6.8xlarge":    {CPUMillis: 28800, MemoryBytes: 116273354637},
		"g6.16xlarge":   {CPUMillis: 57600, MemoryBytes: 232546709275},
		"g6.12xlarge":   {CPUMillis: 43200, MemoryBytes: 174410031956},
		"g6.24xlarge":   {CPUMillis: 86400, MemoryBytes: 348820063912},
		"g6.48xlarge":   {CPUMillis: 172800, MemoryBytes: 697640127824},
		"g6e.xlarge":    {CPUMillis: 3500, MemoryBytes: 29068338659},
		"g6e.2xlarge":   {CPUMillis: 7200, MemoryBytes: 58136677318},
		"g6e.4xlarge":   {CPUMillis: 14400, MemoryBytes: 116273354637},
		"g6e.8xlarge":   {CPUMillis: 28800, MemoryBytes: 232546709275},
		"g6e.16xlarge":  {CPUMillis: 57600, MemoryBytes: 465093418549},
		"g6e.12xlarge":  {CPUMillis: 43200, MemoryBytes: 348820063912},
		"g6e.24xlarge":  {CPUMillis: 86400, MemoryBytes: 697640127824},
		"g6e.48xlarge":  {CPUMillis: 172800, MemoryBytes: 1395280255648},
		"p4d.24xlarge":  {CPUMillis: 86400, MemoryBytes: 1046460191736},
		"p4de.24xlarge": {CPUMillis: 86400, MemoryBytes: 1046460191736},
		"p5.4xlarge":    {CPUMillis: 14400, MemoryBytes: 232546709275},
		"p5.48xlarge":   {CPUMillis: 172800, MemoryBytes: 1860373674197},
		"p5en.48xlarge": {CPUMillis: 172800, MemoryBytes: 1860373674197},
	}
	for _, typ := range FleetCatalog() {
		w, ok := want[typ.Name]
		if !ok {
			t.Errorf("%s has no pinned usable capacity", typ.Name)
			continue
		}
		w.GPUs = typ.GPUCount
		if got := typ.Usable(0); got != w {
			t.Errorf("%s usable %+v, want %+v", typ.Name, got, w)
		}
		if got := typ.Usable(12 * gib); got.MemoryBytes != 12*gib || got.CPUMillis != w.CPUMillis {
			t.Errorf("%s with reported memory: %+v", typ.Name, got)
		}
	}
}
