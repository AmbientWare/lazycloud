package hostsession_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/hostsession"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/platformimages"
)

func (h *harness) count(query string, args ...any) int {
	h.t.Helper()
	var n int
	if err := h.pool.QueryRow(h.t.Context(), query, args...).Scan(&n); err != nil {
		h.t.Fatal(err)
	}
	return n
}

func openNaming(t *testing.T, ctx context.Context, client hostproto.HostServiceClient, platform ...string) hostStream {
	t.Helper()
	return openRunning(t, ctx, client, platform, nil)
}

func openRunning(t *testing.T, ctx context.Context, client hostproto.HostServiceClient, platform, running []string) hostStream {
	t.Helper()
	stream, err := client.Session(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&hostproto.HostMessage{Body: &hostproto.HostMessage_Hello{Hello: &hostproto.Hello{
		BootId: "boot-1", Capacity: &hostproto.Capacity{CpuMillis: 4000, MemoryBytes: 8 << 30}, PlatformImages: platform, RunningPlatformImages: running,
	}}}); err != nil {
		t.Fatal(err)
	}
	return stream
}

// TestSessionsSendPlatformImagesOnceConverted: a session answers the
// platform images its Hello names with each converted copy and its grants,
// and the reason of one that cannot be converted; once another replica
// records that one's copy the session sends it, and grants are renewed
// before they expire.
func TestSessionsSendPlatformImagesOnceConverted(t *testing.T) {
	h := start(t)
	const lifetime = 3 * time.Second
	hostsession.SetLayerLifetime(h.server, lifetime)
	_, ctx := h.enroll()
	ready, failing := platformimages.Builder, platformimages.Mount
	readyMirror, failingMirror := reference("builder-copy"), reference("mount-copy")
	readyLayer, failingLayer := h.storeLayer("builder"), h.storeLayer("mount")
	h.publish(readyMirror, readyLayer)
	if _, err := h.pool.Exec(t.Context(), `insert into platform_images (reference, architecture, mirror, failure, failed_at)
		values ($1, 'amd64', $2, null, null), ($3, 'amd64', null, 'a layer cannot be converted', now())`, ready, readyMirror, failing); err != nil {
		t.Fatal(err)
	}

	in := commands(t, openNaming(t, ctx, h.client, ready, failing))
	first := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetPlatformImages() != nil }).GetPlatformImages().GetImages()
	sent := map[string]*hostproto.PlatformImage{}
	for _, image := range first {
		sent[image.GetReference()] = image
	}
	if got := sent[ready]; got.GetImage() != readyMirror || got.GetPlatform() != "linux/amd64" || len(got.GetLayers()) != 1 ||
		got.GetLayers()[0].GetDiffId() != string(readyLayer.diffID) {
		t.Fatalf("the converted image is sent with its copy and grants: %v", got)
	}
	if got := sent[failing]; got.GetFailure() != "a layer cannot be converted" || got.GetImage() != "" {
		t.Fatalf("an image that cannot be converted is sent with its reason: %v", got)
	}

	h.publish(failingMirror, failingLayer)
	if _, err := h.pool.Exec(t.Context(), "update platform_images set mirror = $2, failure = null, failed_at = null where reference = $1",
		failing, failingMirror); err != nil {
		t.Fatal(err)
	}
	if _, err := h.pool.Exec(t.Context(), "select pg_notify($1, $2)", string(database.ChannelImageBuild), images.PlatformConverted); err != nil {
		t.Fatal(err)
	}
	converted := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool {
		for _, image := range m.GetPlatformImages().GetImages() {
			if image.GetReference() == failing && image.GetImage() != "" {
				return true
			}
		}
		return false
	}).GetPlatformImages().GetImages()
	for _, image := range converted {
		if image.GetReference() == failing && (image.GetImage() != failingMirror || image.GetLayers()[0].GetDiffId() != string(failingLayer.diffID)) {
			t.Fatalf("the image converted meanwhile is sent: %v", image)
		}
	}

	var refresh *hostproto.PlatformImage
	next(t, in, lifetime, func(m *hostproto.ServerMessage) bool {
		for _, image := range m.GetPlatformImages().GetImages() {
			if image.GetReference() == ready {
				refresh = image
			}
		}
		return refresh != nil
	})
	if !refresh.GetLayers()[0].GetExpiresAt().AsTime().After(sent[ready].GetLayers()[0].GetExpiresAt().AsTime()) ||
		!time.Now().Before(sent[ready].GetLayers()[0].GetExpiresAt().AsTime()) {
		t.Fatal("fresh grants come before the first expire")
	}
	if n := h.count("select count(*) from image_reference_uses where reference = any($1)", []string{readyMirror, failingMirror}); n != 2 {
		t.Fatalf("%d of the sent copies are live for the layer sweep", n)
	}
}

// An agent of another release names a builder this server does not know:
// its session stays open, the image gets a failure and nothing converts,
// and the agent's update reaches it.
func TestAnAgentOfAnotherReleaseGetsItsUpdate(t *testing.T) {
	h := start(t)
	if err := h.compute.PublishAgentRelease(t.Context(), compute.AgentRelease{
		Version: "v2", SHA256: map[string]string{"amd64": strings.Repeat("a", 64)}, RolloutPercent: 100,
	}); err != nil {
		t.Fatal(err)
	}
	_, ctx := h.enroll()
	old := "docker.io/moby/buildkit:v0.30.0-rootless@sha256:" + strings.Repeat("a", 64)
	in := commands(t, openHello(t, ctx, h.client, &hostproto.Hello{
		BootId: "boot-1", AgentVersion: "v1", Updatable: true, PlatformImages: []string{old, platformimages.Mount},
	}))
	var failure string
	updated := false
	deadline := time.After(5 * time.Second)
	for failure == "" || !updated {
		select {
		case m, ok := <-in:
			if !ok {
				t.Fatal("the session ended")
			}
			for _, image := range m.GetPlatformImages().GetImages() {
				if image.GetReference() == old {
					failure = image.GetFailure()
				}
			}
			updated = updated || m.GetUpdate().GetVersion() == "v2"
		case <-deadline:
			t.Fatalf("failure %q, update %v", failure, updated)
		}
	}
	if failure != "not a platform image of this release" {
		t.Fatalf("the unknown builder failed with %q", failure)
	}
	if n := h.count("select count(*) from platform_images where reference = $1", old); n != 0 {
		t.Fatal("the server converts an image it does not know")
	}
}

// A platform image whose failure the session sent is read again once its
// retry period passes, with no other image's grants due, and is sent once
// converted.
func TestAFailedPlatformImageIsReadAgainAfterItsRetryPeriod(t *testing.T) {
	h := start(t)
	_, ctx := h.enroll()
	mirror, layer := reference("mount-copy"), h.storeLayer("mount")
	if _, err := h.pool.Exec(t.Context(), `insert into platform_images (reference, architecture, failure, failed_at)
		values ($1, 'amd64', 'a layer cannot be converted', now() - interval '10 minutes' + interval '2 seconds')`, platformimages.Mount); err != nil {
		t.Fatal(err)
	}
	in := commands(t, openNaming(t, ctx, h.client, platformimages.Mount))
	first := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetPlatformImages() != nil }).GetPlatformImages().GetImages()
	if len(first) != 1 || first[0].GetFailure() == "" {
		t.Fatalf("the failure is sent: %v", first)
	}
	// Another replica converted it meanwhile, announcing nothing this
	// session hears.
	h.publish(mirror, layer)
	if _, err := h.pool.Exec(t.Context(), "update platform_images set mirror = $2, failure = null, failed_at = null where reference = $1",
		platformimages.Mount, mirror); err != nil {
		t.Fatal(err)
	}
	got := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetPlatformImages() != nil }).GetPlatformImages().GetImages()
	if len(got) != 1 || got[0].GetImage() != mirror {
		t.Fatalf("the image converted after its failure is sent: %v", got)
	}
}

// The copies a Hello says the host's containers run get grants renewed for
// as long as the session lasts, even when the agent names another image
// now; a copy the server never recorded gets none.
func TestRunningPlatformCopiesKeepTheirGrants(t *testing.T) {
	h := start(t)
	const lifetime = 3 * time.Second
	hostsession.SetLayerLifetime(h.server, lifetime)
	_, ctx := h.enroll()
	current, old := reference("mount-copy"), reference("old-mount-copy")
	currentLayer, oldLayer := h.storeLayer("mount"), h.storeLayer("old mount")
	h.publish(current, currentLayer)
	h.publish(old, oldLayer)
	if _, err := h.pool.Exec(t.Context(), `insert into platform_images (reference, architecture, mirror)
		values ($1, 'amd64', $2), ('docker.io/library/busybox:1.36@sha256:' || repeat('d', 64), 'amd64', $3)`, platformimages.Mount, current, old); err != nil {
		t.Fatal(err)
	}
	unknown := reference("unknown-copy")
	in := commands(t, openRunning(t, ctx, h.client, []string{platformimages.Mount}, []string{old, current, unknown}))
	grants := map[string]int{}
	deadline := time.Now().Add(2 * lifetime)
	for time.Now().Before(deadline) {
		select {
		case m := <-in:
			for _, image := range m.GetPlatformImages().GetImages() {
				grants[image.GetImage()]++
				if image.GetImage() == old && image.GetLayers()[0].GetDiffId() != string(oldLayer.diffID) {
					t.Fatalf("the old copy's grant names %s", image.GetLayers()[0].GetDiffId())
				}
			}
		case <-time.After(100 * time.Millisecond):
		}
	}
	if grants[old] < 2 || grants[current] < 2 || grants[unknown] != 0 {
		t.Fatalf("grants sent per copy: %v", grants)
	}
}
