package diskengine

import (
	"context"
	"errors"
	"slices"
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
	var space *InsufficientSpaceError
	_, err = planRestore(p, 8*gib, gib, chain)
	if !errors.As(err, &space) || !errors.Is(err, ErrInsufficientSpace) {
		t.Fatalf("a chain whose committed base outgrows the volume was admitted: %v", err)
	}
	if space.Shortfall() <= 0 {
		t.Fatalf("a refused restore reports a shortfall of %d", space.Shortfall())
	}

	// Rewrites of the same data cannot grow the base past the disk's size.
	rewrites := []layerManifest{layerOf(4*gib, 4*gib), layerOf(4*gib, 4*gib), layerOf(4*gib, 4*gib)}
	plan, err = planRestore(p, 10*gib, gib, rewrites)
	if err != nil || !slices.Equal(plan, []bool{false, false, true}) {
		t.Fatalf("rewrites bounded by the virtual size: plan %v, err %v", plan, err)
	}
}

// The control plane rounds sizes; the engine only refuses what is not a
// whole number of filesystem blocks, before touching anything.
func TestAttachRefusesMalformedRequests(t *testing.T) {
	e := New(t.TempDir(), nil)
	credentials := func(context.Context) (Credentials, error) {
		return Credentials{AccessKeyID: "a", SecretAccessKey: "s"}, nil
	}
	valid := AttachRequest{
		DiskID: "0b6f6c3e-5d0a-4c55-9a51-2f1f4c1d7e10", SizeBytes: 1 << 30, Mountpoint: "/mnt/d",
		Store: Store{Region: "us-east-2", Bucket: "b", Credentials: credentials},
	}
	// Only where the host cannot attach, so the valid request stops at Check.
	if e.Check() != nil {
		if _, err := e.Attach(t.Context(), valid); errors.Is(err, ErrInvalid) {
			t.Fatalf("a well-formed request was refused as malformed: %v", err)
		}
	}
	for name, mutate := range map[string]func(*AttachRequest){
		"unaligned size":    func(r *AttachRequest) { r.SizeBytes = 1<<30 + 512 },
		"zero size":         func(r *AttachRequest) { r.SizeBytes = 0 },
		"relative mount":    func(r *AttachRequest) { r.Mountpoint = "mnt/d" },
		"disk id with dots": func(r *AttachRequest) { r.DiskID = "../etc" },
		"negative reserve":  func(r *AttachRequest) { r.MinFreeBytes = -1 },
		"store without credentials": func(r *AttachRequest) {
			r.Store.Credentials = nil
		},
	} {
		req := valid
		mutate(&req)
		if _, err := e.Attach(t.Context(), req); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: got %v, want ErrInvalid", name, err)
		}
	}
}
