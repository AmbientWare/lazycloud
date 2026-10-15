package hostsession_test

import (
	"bytes"
	"context"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
)

// TestStartupTracesStayWithTheirWorkspace: a start of an image the
// workspace has no trace of asks the host to record one; the trace the
// host reports for its container comes with the workspace's next start of
// that image, and with no start of another workspace's; a trace naming a
// frame the image does not have, or a container the host does not run, is
// dropped.
func TestStartupTracesStayWithTheirWorkspace(t *testing.T) {
	t.Parallel()
	h := start(t)
	host, ctx := h.enroll()
	base, app := h.storeLayer("base"), h.storeLayer("app")
	ref := reference("traced")
	h.publish(ref, base, app)
	mine := h.startingImage(host, ref)
	theirs := h.startingImage(host, ref)

	stream := open(t, ctx, h.client)
	in := commands(t, stream)
	for range 2 {
		s := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStart() != nil }).GetStart()
		if !s.GetRecordTrace() || s.GetPrefetch() != nil {
			t.Fatalf("the first start of an image records %v and prefetches %v", s.GetRecordTrace(), s.GetPrefetch())
		}
	}
	report := func(container uuid.UUID, reads ...*imagefsproto.FrameRead) {
		t.Helper()
		if err := stream.Send(&hostproto.HostMessage{Body: &hostproto.HostMessage_StartupTrace{StartupTrace: &hostproto.StartupTrace{
			ContainerId: container.String(), Trace: &hostproto.ImageTrace{Reads: reads},
		}}}); err != nil {
			t.Fatal(err)
		}
	}
	report(theirs, &imagefsproto.FrameRead{Layer: 0, Frame: 1})
	report(uuid.New(), &imagefsproto.FrameRead{Layer: 0, Frame: 0})
	traced := []*imagefsproto.FrameRead{{Layer: 1, Frame: 0}, {Layer: 0, Frame: 0}}
	report(mine, traced...)
	var stored int
	for deadline := time.Now().Add(5 * time.Second); stored == 0 && time.Now().Before(deadline); time.Sleep(20 * time.Millisecond) {
		if err := h.pool.QueryRow(t.Context(), `select count(*) from image_traces where reference = $1`, ref).Scan(&stored); err != nil {
			t.Fatal(err)
		}
	}
	if stored != 1 {
		t.Fatalf("%d traces stored, want the valid one", stored)
	}

	again := func(of uuid.UUID) uuid.UUID {
		t.Helper()
		var id uuid.UUID
		if err := h.pool.QueryRow(t.Context(), `insert into containers (workspace_id, release_id, workload_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
			select workspace_id, release_id, (select workload_id from releases where id = release_id), 'starting', host_id, 1, 1000, 1 << 28, now() from containers where id = $1 returning id`, of).Scan(&id); err != nil {
			t.Fatal(err)
		}
		return id
	}
	mineAgain, theirsAgain := again(mine), again(theirs)
	starts := map[string]*hostproto.StartContainer{}
	for len(starts) < 2 {
		s := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStart() != nil }).GetStart()
		starts[s.GetContainerId()] = s
	}
	got := starts[mineAgain.String()]
	pairs := func(reads []*imagefsproto.FrameRead) [][2]uint32 {
		out := make([][2]uint32, len(reads))
		for n, r := range reads {
			out[n] = [2]uint32{r.GetLayer(), r.GetFrame()}
		}
		return out
	}
	if got.GetRecordTrace() || !slices.Equal(pairs(got.GetPrefetch().GetReads()), pairs(traced)) {
		t.Fatalf("the workspace's next start records %v and prefetches %v, want %v", got.GetRecordTrace(), pairs(got.GetPrefetch().GetReads()), pairs(traced))
	}
	if other := starts[theirsAgain.String()]; !other.GetRecordTrace() || other.GetPrefetch() != nil {
		t.Fatalf("another workspace's start records %v and prefetches %v", other.GetRecordTrace(), other.GetPrefetch())
	}
}

// TestVolumeTracesComeBackWithTheirWorkload: a workload's first start that
// mounts a volume asks the host to record what it reads, and the trace the
// host reports for its container comes with the workload's next start. A
// trace for a container another host runs is dropped.
func TestVolumeTracesComeBackWithTheirWorkload(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	elsewhere, _ := h.enroll()
	spec := `{"handler": "a:b", "image": {"python_version": "3.12"}, "volumes": [{"name": "data", "mount_path": "data"}]}`
	_, mine := h.startingWith(host, spec)
	theirWorkspace, theirs := h.startingWith(elsewhere, spec)
	theirMounts, err := h.store.MountVolumes(t.Context(), theirWorkspace, theirs, []apitypes.VolumeMountSpec{{Name: "data"}})
	if err != nil {
		t.Fatal(err)
	}

	stream := open(t, ctx, h.client)
	in := commands(t, stream)
	first := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStart() != nil }).GetStart()
	if trace := first.GetVolumeTrace(); !trace.GetRecord() || len(trace.GetReads()) != 0 {
		t.Fatalf("the workload's first start carries %v", trace)
	}
	report := func(container uuid.UUID, volume string, paths ...string) {
		t.Helper()
		reads := make([]*imagefsproto.VolumeRead, len(paths))
		for n, p := range paths {
			reads[n] = &imagefsproto.VolumeRead{VolumeId: volume, Path: p}
		}
		if err := stream.Send(&hostproto.HostMessage{Body: &hostproto.HostMessage_VolumeTrace{VolumeTrace: &hostproto.ContainerVolumeTrace{
			ContainerId: container.String(), Reads: reads,
		}}}); err != nil {
			t.Fatal(err)
		}
	}
	report(theirs, theirMounts[0].Volume.String(), "theirs.bin")
	volume := first.GetVolumes()[0].GetVolume().GetVolumeId()
	report(mine, volume, "model.bin", "vae.bin")
	var stored int
	for deadline := time.Now().Add(5 * time.Second); stored == 0 && time.Now().Before(deadline); time.Sleep(20 * time.Millisecond) {
		if err := h.pool.QueryRow(t.Context(), `select count(*) from volume_traces`).Scan(&stored); err != nil {
			t.Fatal(err)
		}
	}
	if stored != 1 {
		t.Fatalf("%d volume traces stored, want the host's own container's", stored)
	}

	var again uuid.UUID
	if err := h.pool.QueryRow(t.Context(), `insert into containers (workspace_id, release_id, workload_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
		select workspace_id, release_id, workload_id, 'starting', host_id, 1, 1000, 1 << 28, now() from containers where id = $1 returning id`, mine).Scan(&again); err != nil {
		t.Fatal(err)
	}
	next := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStart().GetContainerId() == again.String() }).GetStart()
	var paths []string
	for _, r := range next.GetVolumeTrace().GetReads() {
		paths = append(paths, r.GetPath())
	}
	if next.GetVolumeTrace().GetRecord() || !slices.Equal(paths, []string{"model.bin", "vae.bin"}) {
		t.Fatalf("the workload's next start carries %v", next.GetVolumeTrace())
	}
}

// openPeer opens a session whose Hello names the host's volume peer address.
func openPeer(t *testing.T, ctx context.Context, client hostproto.HostServiceClient, address string) hostStream {
	t.Helper()
	stream, err := client.Session(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&hostproto.HostMessage{Body: &hostproto.HostMessage_Hello{Hello: &hostproto.Hello{
		BootId: "boot-1", Capacity: &hostproto.Capacity{CpuMillis: 4000, MemoryBytes: 8 << 30}, PeerAddress: address,
	}}}); err != nil {
		t.Fatal(err)
	}
	return stream
}

// TestVolumePeersStayInTheirRegionAndWorkspace: a start that mounts a
// platform volume names the other hosts of its region that run the
// workspace's containers and serve a volume cache, not the host itself, a
// host of another region or one running another workspace's containers,
// and the workspace's storage grant carries the same peer key on every
// host of the workspace.
func TestVolumePeersStayInTheirRegionAndWorkspace(t *testing.T) {
	t.Parallel()
	h := start(t)
	host, ctx := h.enroll()
	sibling, siblingCtx := h.enroll()
	elsewhere, elsewhereCtx := h.enroll()
	other, otherCtx := h.enroll()
	h.exec(`update hosts set region = case when id = $1 then 'us-west-2' else 'us-east-2' end`, uuid.UUID(elsewhere))
	spec := `{"handler": "a:b", "image": {"python_version": "3.12"}, "volumes": [{"name": "data", "mount_path": "data"}]}`
	ws, mine := h.startingWith(host, spec)
	for _, on := range []compute.HostID{sibling, elsewhere} {
		h.exec(`insert into containers (workspace_id, release_id, workload_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
			select workspace_id, release_id, workload_id, 'starting', $2, 1, 1000, 1 << 28, now() from containers where id = $1`, mine, uuid.UUID(on))
	}
	h.startingWith(other, spec)

	// The others are online with their addresses before the host syncs:
	// each has sent its first command.
	grant := func(ctx context.Context, address string) *hostproto.StorageGrant {
		t.Helper()
		in := commands(t, openPeer(t, ctx, h.client, address))
		return next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStorageGrant() != nil }).GetStorageGrant()
	}
	siblingGrant := grant(siblingCtx, "10.0.0.2:12122")
	grant(elsewhereCtx, "10.0.1.2:12122")
	grant(otherCtx, "10.0.0.3:12122")

	in := commands(t, openPeer(t, ctx, h.client, "10.0.0.1:12122"))
	mineGrant := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStorageGrant() != nil }).GetStorageGrant()
	if mineGrant.GetWorkspaceId() != ws.String() || len(mineGrant.GetPeerKey()) != 32 || !bytes.Equal(mineGrant.GetPeerKey(), siblingGrant.GetPeerKey()) {
		t.Fatalf("the host's grant for %s carries peer key %x, the sibling's %x", mineGrant.GetWorkspaceId(), mineGrant.GetPeerKey(), siblingGrant.GetPeerKey())
	}
	s := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStart() != nil }).GetStart()
	var peers []string
	for _, p := range s.GetVolumePeers() {
		peers = append(peers, p.GetHostId()+" "+p.GetAddress())
	}
	if want := []string{sibling.String() + " 10.0.0.2:12122"}; !slices.Equal(peers, want) {
		t.Fatalf("the start names peers %v, want %v", peers, want)
	}
}
