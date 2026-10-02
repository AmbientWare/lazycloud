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

// TestFleetCatalogUsableMemoryIsWhatHostsReport checks a host's report
// replaces the memory estimate and leaves CPU alone.
func TestFleetCatalogUsableMemoryIsWhatHostsReport(t *testing.T) {
	for _, typ := range FleetCatalog() {
		estimated, reported := typ.Usable(0), typ.Usable(12*gib)
		if reported.MemoryBytes != 12*gib || reported.CPUMillis != estimated.CPUMillis || reported.GPUs != typ.GPUCount {
			t.Errorf("%s with reported memory: %+v", typ.Name, reported)
		}
	}
}
