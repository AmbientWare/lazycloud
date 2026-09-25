package main

import (
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"
)

func layerOf(bytes, virt int64) layerManifest {
	return layerManifest{VirtualSizeBytes: virt, Chunks: []manifestChunk{{Length: bytes}}}
}

// A restore is planned before the local copy is wiped, so a chain the plan
// admits must never run out of room halfway. Every retry would wipe and fail
// again.
func TestPlanRestoreCountsBaseGrowthFromCommits(t *testing.T) {
	const gib = int64(1) << 30
	p := diskPaths{root: "/vol", id: "d"}
	// A 10 GiB disk with a 1 GiB base and three 3 GiB layers of new data.
	chain := []layerManifest{layerOf(gib, 10*gib), layerOf(3*gib, 10*gib), layerOf(3*gib, 10*gib), layerOf(3*gib, 10*gib)}

	plan, err := planRestore(p, 20*gib, gib, chain)
	if err != nil || slices.Contains(plan, true) {
		t.Fatalf("a chain that fits whole: plan %v, err %v", plan, err)
	}

	// Base and largest layer (4 GiB) fit in 8 GiB less a 1 GiB reserve, but
	// committing grows the base with each layer. After two commits the base
	// holds 7 GiB and the last layer does not fit beside it.
	var space *insufficientSpaceError
	if _, err := planRestore(p, 8*gib, gib, chain); !errors.As(err, &space) {
		t.Fatalf("a chain whose committed base outgrows the volume was admitted: %v", err)
	}

	// Rewrites of the same data cannot grow the base past the disk's size.
	rewrites := []layerManifest{layerOf(4*gib, 4*gib), layerOf(4*gib, 4*gib), layerOf(4*gib, 4*gib)}
	plan, err = planRestore(p, 10*gib, gib, rewrites)
	if err != nil || !slices.Equal(plan, []bool{false, false, true}) {
		t.Fatalf("rewrites bounded by the virtual size: plan %v, err %v", plan, err)
	}
}

// The worker reads attach output with a model whose warnings field is a list,
// so an attach with nothing to warn about must not print a JSON null there.
func TestAttachResultWithoutWarningsHasNoNullList(t *testing.T) {
	out, err := json.Marshal(attachResult{Mountpoint: "/m"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(out), "null") {
		t.Fatalf("attach result has a null field: %s", out)
	}
}
