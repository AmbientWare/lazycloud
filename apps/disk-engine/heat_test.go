package main

import (
	"slices"
	"testing"
)

// A snapshot carries the map, and a hydration orders its first reads by it, so
// the map must stay small for any disk and put the most recent use first.
func TestHeatMapStaysBoundedAndOrdersByRecency(t *testing.T) {
	const gib = int64(1) << 30
	for _, size := range []int64{1, 100 * gib, 1024*gib + 5*gib} {
		heat := newHeatMap(1)
		heat.fit(size)
		if len(heat.ages) > heatMaxRegions {
			t.Fatalf("a %d-byte base maps to %d regions, over %d", size, len(heat.ages), heatMaxRegions)
		}
		if heat.regionBytes()*int64(len(heat.ages)) < size {
			t.Fatalf("a %d-byte base is not covered by %d regions of %d bytes", size, len(heat.ages), heat.regionBytes())
		}
	}

	heat := newHeatMap(1)
	heat.fit(8 << 20)
	heat.touch(5 << 20)
	heat.age()
	heat.touch(2 << 20)
	heat.touch(7 << 20)
	if got := heat.hot(); !slices.Equal(got, []int{2, 7, 5}) {
		t.Fatalf("hot regions %v, want the latest window first and file order within it", got)
	}

	encoded, err := heat.encode()
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := decodeHeat(encoded)
	if err != nil || decoded.baseSeq != 1 || decoded.shift != heat.shift || !slices.Equal(decoded.ages, heat.ages) {
		t.Fatalf("round trip: %+v, %v", decoded, err)
	}

	// A base that outgrows the region limit merges regions, keeping the more recent age.
	heat.fit(int64(heatMaxRegions+1) << heatMinShift)
	if heat.shift != heatMinShift+1 || heat.ages[1] != 0 || heat.ages[2] != 1 || heat.ages[3] != 0 {
		t.Fatalf("merged map: shift %d, ages %v", heat.shift, heat.ages[:4])
	}

	for range heatCold {
		heat.age()
	}
	if len(heat.hot()) != 0 {
		t.Fatal("a region untouched for every window the map remembers is still hot")
	}
}

// The hot pass reads whole regions; the cold pass must read only what it left.
func TestColdHydrationSkipsRegionsTheHotPassRead(t *testing.T) {
	done := &regionSet{shift: 20, read: []bool{false, true, false, true}}
	got := outside([]extent{{0, 4 << 20}}, done)
	want := []extent{{0, 1 << 20}, {2 << 20, 1 << 20}}
	if !slices.Equal(got, want) {
		t.Fatalf("cold extents %v, want %v", got, want)
	}
}
