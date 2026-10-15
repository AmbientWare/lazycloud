package storage_test

import (
	"fmt"
	"slices"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
)

// A workload's volume trace keeps only the reads under volumes its
// reporting container mounted in its workspace, in their order. It stands
// for a day, then the next start records it again, and no other
// workspace's start of the workload reads it.
func TestVolumeTracesStayWithTheirWorkload(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, volumeSpec)
	s := f.storage
	container := f.container()
	mounts, err := s.MountVolumes(ctx, f.ws, container, []apitypes.VolumeMountSpec{{Name: "data"}})
	if err != nil {
		t.Fatal(err)
	}
	mounted := mounts[0].Volume.String()
	unmounted, err := s.CreateVolume(ctx, f.ws, "other")
	if err != nil {
		t.Fatal(err)
	}
	var workload uuid.UUID
	f.exec(`select workload_id from releases where id = $1`, &workload, f.release)
	read := func(volume, path string) *imagefsproto.VolumeRead {
		return &imagefsproto.VolumeRead{VolumeId: volume, Path: path}
	}
	paths := func(reads []*imagefsproto.VolumeRead) []string {
		var out []string
		for _, r := range reads {
			out = append(out, r.GetPath())
		}
		return out
	}
	if err := s.RecordVolumeTrace(ctx, f.ws, workload, container, []*imagefsproto.VolumeRead{
		read(mounted, "model.bin"), read(unmounted.Id.String(), "secret.bin"), read(uuid.NewString(), "elsewhere.bin"), read(mounted, "vae.bin"),
	}); err != nil {
		t.Fatal(err)
	}
	reads, record, err := s.VolumeTrace(ctx, f.ws, workload)
	if err != nil || record || !slices.Equal(paths(reads), []string{"model.bin", "vae.bin"}) || reads[0].GetVolumeId() != mounted {
		t.Fatalf("the stored trace: %v record=%v err=%v", reads, record, err)
	}
	if reads, record, err := s.VolumeTrace(ctx, f.workspace("another"), workload); err != nil || len(reads) != 0 || !record {
		t.Fatalf("another workspace reads %v record=%v err=%v", reads, record, err)
	}

	if err := s.RecordVolumeTrace(ctx, f.ws, workload, container, []*imagefsproto.VolumeRead{read(mounted, "newer.bin")}); err != nil {
		t.Fatal(err)
	}
	if reads, _, _ := s.VolumeTrace(ctx, f.ws, workload); !slices.Equal(paths(reads), []string{"model.bin", "vae.bin"}) {
		t.Fatalf("a trace younger than a day was replaced by %v", paths(reads))
	}
	if _, err := f.pool.Exec(ctx, `update volume_traces set recorded_at = now() - interval '25 hours'`); err != nil {
		t.Fatal(err)
	}
	if _, record, _ := s.VolumeTrace(ctx, f.ws, workload); !record {
		t.Fatal("a trace older than a day is not recorded again")
	}
	if err := s.RecordVolumeTrace(ctx, f.ws, workload, container, []*imagefsproto.VolumeRead{read(mounted, "newer.bin")}); err != nil {
		t.Fatal(err)
	}
	if reads, record, _ := s.VolumeTrace(ctx, f.ws, workload); record || !slices.Equal(paths(reads), []string{"newer.bin"}) {
		t.Fatalf("the trace recorded again: %v record=%v", paths(reads), record)
	}
}

// A report of more reads than a trace holds stores its first
// MaxVolumeReads, in their order.
func TestAVolumeTraceKeepsItsFirstReads(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, volumeSpec)
	container := f.container()
	mounts, err := f.storage.MountVolumes(ctx, f.ws, container, []apitypes.VolumeMountSpec{{Name: "data"}})
	if err != nil {
		t.Fatal(err)
	}
	var workload uuid.UUID
	f.exec(`select workload_id from releases where id = $1`, &workload, f.release)
	reads := make([]*imagefsproto.VolumeRead, hostproto.MaxVolumeReads+44)
	for n := range reads {
		reads[n] = &imagefsproto.VolumeRead{VolumeId: mounts[0].Volume.String(), Path: fmt.Sprintf("shard-%03d", n)}
	}
	if err := f.storage.RecordVolumeTrace(ctx, f.ws, workload, container, reads); err != nil {
		t.Fatal(err)
	}
	stored, _, err := f.storage.VolumeTrace(ctx, f.ws, workload)
	if err != nil {
		t.Fatal(err)
	}
	if len(stored) != hostproto.MaxVolumeReads || stored[0].GetPath() != "shard-000" || stored[len(stored)-1].GetPath() != fmt.Sprintf("shard-%03d", hostproto.MaxVolumeReads-1) {
		t.Fatalf("the trace stored %d reads", len(stored))
	}
}
