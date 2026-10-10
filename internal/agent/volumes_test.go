package agent

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"math/big"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	containertypes "github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// testGeeseFS is the pinned GeeseFS binary deploy/local/fetch-geesefs.sh
// installs.
func testGeeseFS(t *testing.T) string {
	t.Helper()
	path, err := filepath.Abs("../../bin/geesefs")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Skipf("volume mounts need GeeseFS at %s; run deploy/local/fetch-geesefs.sh", path)
	}
	return path
}

// testStore is the test object store's bucket and keys.
type testStore struct {
	endpoint, region, bucket, accessKey, secretKey string
}

func newTestStore(t *testing.T) testStore {
	t.Helper()
	cfg := storagetest.Config(t)
	return testStore{endpoint: cfg.Endpoint, region: cfg.Region, bucket: cfg.Bucket, accessKey: cfg.AccessKeyID, secretKey: cfg.SecretAccessKey}
}

// grant gives workspace the store's bucket for an hour.
func (s testStore) grant(workspace string) *hostproto.ServerMessage {
	return &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_StorageGrant{StorageGrant: &hostproto.StorageGrant{
		WorkspaceId: workspace, Endpoint: s.endpoint, Region: s.region, Bucket: s.bucket, ForcePathStyle: true,
		AccessKeyId: s.accessKey, SecretAccessKey: s.secretKey, ExpiresAt: timestamppb.New(time.Now().Add(time.Hour)),
	}}}
}

func volumeStart(e *env, source *hostproto.Source, workspace, volume string, readOnly bool) *hostproto.ServerMessage {
	start := e.startCommand("app:handle", 1)
	start.GetStart().Source = source
	start.GetStart().Volumes = []*hostproto.VolumeMount{{
		MountPath: "/volumes/data", ReadOnly: readOnly,
		Source: &hostproto.VolumeMount_Volume{Volume: &hostproto.PlatformVolume{
			VolumeId: volume, WorkspaceId: workspace, Prefix: "volumes/" + volume + "/",
		}},
	}}
	return start
}

// startVolume starts a container on the volume and waits until it is ready.
func (e *env) startVolume(s *serverSession, source *hostproto.Source, workspace, volume string) string {
	e.t.Helper()
	start := volumeStart(e, source, workspace, volume, false)
	s.send(e.t, start)
	s.phase(e.t, start.GetStart().GetContainerId(), ready)
	return start.GetStart().GetContainerId()
}

// write writes text to path in container through a task.
func (e *env) write(container, path, text string) {
	e.t.Helper()
	attempt := e.task(container, `{"args": ["write", "`+path+`", "`+text+`"]}`)
	if failure := e.completion(attempt).GetFailure(); failure != nil {
		e.t.Fatalf("write %s in %s: %v", path, container, failure)
	}
}

// read reads path in container through a task.
func (e *env) read(container, path string) string {
	e.t.Helper()
	return result(e.t, e.completion(e.task(container, `{"args": ["read", "`+path+`"]}`)))
}

// mounters lists the mount containers this test's agents made, with
// stopped ones when all is set, for one workspace or every cloud bucket.
func (e *env) mounters(all bool, kind, workspace string) []containertypes.Summary {
	e.t.Helper()
	filters := client.Filters{}.Add("label", "lazycloud.agent="+e.id).Add("label", labelKind+"="+kind)
	if workspace != "" {
		filters = filters.Add("label", labelWorkspace+"="+workspace)
	}
	list, err := e.docker.ContainerList(context.Background(), client.ContainerListOptions{All: all, Filters: filters})
	if err != nil {
		e.t.Fatal(err)
	}
	return list.Items
}

// stopMounter stops a mount container as a crash or an operator would.
func (e *env) stopMounter(id string) {
	e.t.Helper()
	timeout := 10
	if _, err := e.docker.ContainerStop(context.Background(), id, client.ContainerStopOptions{Timeout: &timeout}); err != nil {
		e.t.Fatal(err)
	}
}

// adoptedExit is how an adopted container exited: in the Hello, or in a
// later report.
func (s *serverSession) adoptedExit(t *testing.T, container string) *hostproto.ContainerExit {
	t.Helper()
	for _, r := range s.hello.GetContainers() {
		if r.GetContainerId() == container && r.GetPhase() == exited {
			return r.GetExit()
		}
	}
	return s.phase(t, container, exited).GetExit()
}

// copyFile copies src to a new file in a new directory, with extra appended.
func copyFile(t *testing.T, src string, extra string) string {
	t.Helper()
	data, err := os.ReadFile(src) //nolint:gosec // Test inputs.
	if err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(t.TempDir(), filepath.Base(src))
	if err := os.WriteFile(dst, append(data, extra...), 0o755); err != nil { //nolint:gosec // GeeseFS is executable.
		t.Fatal(err)
	}
	return dst
}

// TestVolumesMountThroughWorkspaceBucket runs two containers on one volume:
// what one writes lands in the workspace bucket under the volume's prefix
// and the other reads it, while neither sees a credential.
func TestVolumesMountThroughWorkspaceBucket(t *testing.T) {
	geesefs := testGeeseFS(t)
	// Made first so that the mounts stop before the bucket goes.
	store := newTestStore(t)
	e := newEnv(t)
	t.Cleanup(e.stopMounts)
	e.geesefs = geesefs
	e.startAgent()
	s := e.session()
	source := serveSource(t, "testdata/volumes")
	workspace, volume := uuid.NewString(), uuid.NewString()

	start := volumeStart(e, source, workspace, volume, false)
	s.send(t, start)
	// The start waits for the workspace's grant.
	time.Sleep(500 * time.Millisecond)
	s.send(t, store.grant(workspace))
	writer := start.GetStart().GetContainerId()
	s.phase(t, writer, ready)

	attempt := e.task(writer, `{"args": ["write", "/volumes/data/notes/hello.txt", "hello volume"]}`)
	if got := result(t, e.completion(attempt)); got != "12" {
		t.Fatalf("write: %s", got)
	}
	attempt = e.task(writer, `{"args": ["env", ""]}`)
	if got := result(t, e.completion(attempt)); got != "[]" {
		t.Fatalf("the container sees credentials: %s", got)
	}

	reader := volumeStart(e, source, workspace, volume, true)
	s.send(t, reader)
	s.phase(t, reader.GetStart().GetContainerId(), ready)
	if got := e.read(reader.GetStart().GetContainerId(), "/volumes/data/notes/hello.txt"); got != `"hello volume"` {
		t.Fatalf("read: %s", got)
	}
	attempt = e.task(reader.GetStart().GetContainerId(), `{"args": ["write", "/volumes/data/nope.txt", "x"]}`)
	if failure := e.completion(attempt).GetFailure(); failure == nil {
		t.Fatal("a read-only mount accepted a write")
	}

	key := "volumes/" + volume + "/notes/hello.txt"
	object, err := storagetest.Client().GetObject(t.Context(), &s3.GetObjectInput{Bucket: aws.String(store.bucket), Key: aws.String(key)})
	if err != nil {
		t.Fatalf("read %s from the bucket: %v", key, err)
	}
	body, _ := io.ReadAll(object.Body)
	_ = object.Body.Close()
	if string(body) != "hello volume" {
		t.Fatalf("bucket holds %q", body)
	}
	info, err := os.Stat(filepath.Join(e.stateDir, "mounts"))
	if err != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("mount directory mode %v err=%v, want 0700", info.Mode().Perm(), err)
	}

	// Both containers share one mount; when it dies, they stop.
	mounters := e.mounters(false, kindMount, workspace)
	if len(mounters) != 1 {
		t.Fatalf("%d mounts for one workspace", len(mounters))
	}
	e.stopMounter(mounters[0].ID)
	// The two exits arrive in either order.
	pending := map[string]bool{writer: true, reader.GetStart().GetContainerId(): true}
	for len(pending) > 0 {
		report := s.until(t, 60*time.Second, func(m *hostproto.HostMessage) bool {
			r := m.GetContainer()
			return pending[r.GetContainerId()] && r.GetPhase() == exited
		}).GetContainer()
		if !strings.Contains(report.GetExit().GetMessage(), "volume mount") {
			t.Fatalf("exit of a container on a dead mount: %v", report.GetExit())
		}
		delete(pending, report.GetContainerId())
	}
	e.eventually("the dead mount is removed", func() bool { return len(e.mounters(true, kindMount, workspace)) == 0 })
}

// otherCA is a CA certificate that signed none of the test's servers.
func otherCA(t *testing.T) []byte {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "another CA"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

// TestVolumesMountThroughAnHTTPSStore: a mount verifies its store's
// certificate against the host's trust bundle. A store the bundle does not
// trust fails the start with GeeseFS's own words and leaves no mount; once
// the host trusts it, the next start mounts.
func TestVolumesMountThroughAnHTTPSStore(t *testing.T) {
	geesefs := testGeeseFS(t)
	store := newTestStore(t)
	target, err := url.Parse(store.endpoint)
	if err != nil {
		t.Fatal(err)
	}
	// Made before the agent so that the mounts stop before the proxy goes.
	proxy := httptest.NewTLSServer(httputil.NewSingleHostReverseProxy(target))
	t.Cleanup(proxy.Close)
	store.endpoint = proxy.URL
	bundle := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(bundle, otherCA(t), 0o600); err != nil {
		t.Fatal(err)
	}
	e := newEnv(t)
	t.Cleanup(e.stopMounts)
	e.geesefs = geesefs
	e.startAgent(func(c *Config) { c.TrustBundle = bundle })
	s := e.session()
	source := serveSource(t, "testdata/volumes")
	workspace, volume := uuid.NewString(), uuid.NewString()
	s.send(t, store.grant(workspace))

	untrusted := volumeStart(e, source, workspace, volume, false)
	began := time.Now()
	s.send(t, untrusted)
	exit := s.phase(t, untrusted.GetStart().GetContainerId(), exited).GetExit()
	t.Logf("start on an untrusted store failed after %s: %s", time.Since(began), exit.GetMessage())
	if exit.GetReason() != hostproto.ExitReason_EXIT_REASON_START_FAILED || !strings.Contains(exit.GetMessage(), "certificate") {
		t.Fatalf("start on an untrusted store: %v", exit)
	}
	if left := e.mounters(true, kindMount, workspace); len(left) != 0 {
		t.Fatalf("a mount that never mounted was left: %v", left[0].Names)
	}

	if err := os.WriteFile(bundle, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: proxy.Certificate().Raw}), 0o600); err != nil {
		t.Fatal(err)
	}
	id := e.startVolume(s, source, workspace, volume)
	e.write(id, "/volumes/data/hello.txt", "over https")
	if got := e.read(id, "/volumes/data/hello.txt"); got != `"over https"` {
		t.Fatalf("read: %s", got)
	}
}

// TestVolumeMountsSurviveAnAgentUpgrade: an agent restarted with another
// GeeseFS and trust bundle keeps the old mount for the workload on it, which
// reads and writes on, and gives a new start a mount of its own. The old
// mount stops once its last user goes, and the new one's users run on.
func TestVolumeMountsSurviveAnAgentUpgrade(t *testing.T) {
	geesefs := testGeeseFS(t)
	store := newTestStore(t)
	e := newEnv(t)
	t.Cleanup(e.stopMounts)
	first := e.startAgent(func(c *Config) {
		c.GeeseFSPath, c.TrustBundle = copyFile(t, geesefs, ""), copyFile(t, HostTrustBundle(), "")
	})
	s := e.session()
	source := serveSource(t, "testdata/volumes")
	workspace, volume := uuid.NewString(), uuid.NewString()
	s.send(t, store.grant(workspace))
	old := e.startVolume(s, source, workspace, volume)
	e.write(old, "/volumes/data/before.txt", "before")
	first.stop()

	e.startAgent(func(c *Config) {
		c.GeeseFSPath, c.TrustBundle = copyFile(t, geesefs, ""), copyFile(t, HostTrustBundle(), "\n")
	})
	s = e.session()
	s.adoptedReady(t, old)
	e.write(old, "/volumes/data/after.txt", "after")
	if got := e.read(old, "/volumes/data/before.txt"); got != `"before"` {
		t.Fatalf("the old workload reads %s", got)
	}
	current := e.startVolume(s, source, workspace, volume)
	if got := e.read(current, "/volumes/data/after.txt"); got != `"after"` {
		t.Fatalf("the new workload reads %s", got)
	}
	mounters := e.mounters(false, kindMount, workspace)
	if len(mounters) != 2 || mounters[0].Labels[labelFingerprint] == mounters[1].Labels[labelFingerprint] {
		t.Fatalf("mounts after the upgrade: %v", mounters)
	}

	s.send(t, stopCommand(old, 1))
	s.phase(t, old, exited)
	e.eventually("the stale mount stops", func() bool { return len(e.mounters(true, kindMount, workspace)) == 1 })
	e.write(current, "/volumes/data/later.txt", "later")
	if got := e.read(current, "/volumes/data/before.txt"); got != `"before"` {
		t.Fatalf("the new workload reads %s after the old mount stopped", got)
	}
}

// TestAMountThatDiesFailsOnlyItsOwnUsers: with a stale and a current
// mount of one workspace, the stale one dying stops only the container on
// it; the current one's users and new starts carry on.
func TestAMountThatDiesFailsOnlyItsOwnUsers(t *testing.T) {
	geesefs := testGeeseFS(t)
	store := newTestStore(t)
	e := newEnv(t)
	t.Cleanup(e.stopMounts)
	e.geesefs = geesefs
	bundle := copyFile(t, HostTrustBundle(), "")
	e.startAgent(func(c *Config) { c.TrustBundle = bundle })
	s := e.session()
	source := serveSource(t, "testdata/volumes")
	workspace, volume := uuid.NewString(), uuid.NewString()
	s.send(t, store.grant(workspace))
	stale := e.startVolume(s, source, workspace, volume)
	before := e.mounters(false, kindMount, workspace)
	if len(before) != 1 {
		t.Fatalf("%d mounts for one workspace", len(before))
	}

	// The host's trust bundle changes; the next start gets a new mount.
	data, err := os.ReadFile(bundle) //nolint:gosec // The test's own file.
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(bundle, append(data, '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
	current := e.startVolume(s, source, workspace, volume)
	if n := len(e.mounters(false, kindMount, workspace)); n != 2 {
		t.Fatalf("%d mounts after the trust bundle changed", n)
	}

	e.stopMounter(before[0].ID)
	if exit := s.phase(t, stale, exited).GetExit(); !strings.Contains(exit.GetMessage(), "volume mount") {
		t.Fatalf("exit of the container on the dead mount: %v", exit)
	}
	e.write(current, "/volumes/data/still.txt", "still here")
	again := e.startVolume(s, source, workspace, volume)
	if got := e.read(again, "/volumes/data/still.txt"); got != `"still here"` {
		t.Fatalf("a new start reads %s", got)
	}
	if n := len(e.mounters(true, kindMount, workspace)); n != 1 {
		t.Fatalf("%d mounts after the stale one died, want the current one", n)
	}
}

// TestAMountThatDiesWhileTheAgentIsDownFailsItsUsers: a restarted agent
// stops the containers bound into a mount that exited while it was away,
// removes the mount, and mounts anew for the next start.
func TestAMountThatDiesWhileTheAgentIsDownFailsItsUsers(t *testing.T) {
	geesefs := testGeeseFS(t)
	store := newTestStore(t)
	e := newEnv(t)
	t.Cleanup(e.stopMounts)
	e.geesefs = geesefs
	first := e.startAgent()
	s := e.session()
	source := serveSource(t, "testdata/volumes")
	workspace, volume := uuid.NewString(), uuid.NewString()
	s.send(t, store.grant(workspace))
	id := e.startVolume(s, source, workspace, volume)
	e.write(id, "/volumes/data/kept.txt", "kept")
	first.stop()
	for _, m := range e.mounters(false, kindMount, workspace) {
		e.stopMounter(m.ID)
	}

	e.startAgent()
	s = e.session()
	if exit := s.adoptedExit(t, id); !strings.Contains(exit.GetMessage(), "volume mount") {
		t.Fatalf("exit of a container whose mount died while the agent was away: %v", exit)
	}
	if left := e.mounters(true, kindMount, workspace); len(left) != 0 {
		t.Fatalf("the dead mount was left: %v", left[0].Names)
	}
	next := e.startVolume(s, source, workspace, volume)
	if got := e.read(next, "/volumes/data/kept.txt"); got != `"kept"` {
		t.Fatalf("read after remounting: %s", got)
	}
}

func cloudBucketStart(e *env, store testStore, prefix string) *hostproto.ServerMessage {
	start := e.startCommand("app:handle", 1)
	start.GetStart().Source = serveSource(e.t, "testdata/volumes")
	start.GetStart().Volumes = []*hostproto.VolumeMount{{
		MountPath: "/models", ReadOnly: true,
		Source: &hostproto.VolumeMount_CloudBucket{CloudBucket: &hostproto.CloudBucket{
			Bucket: store.bucket, Prefix: prefix, Region: store.region, Endpoint: store.endpoint, ForcePathStyle: true,
			AccessKeyId: store.accessKey, SecretAccessKey: store.secretKey,
		}},
	}}
	return start
}

// TestCloudBucketMountsWithItsKeys mounts a user's bucket with keys from the
// start and removes the mount and its keys when the container goes.
func TestCloudBucketMountsWithItsKeys(t *testing.T) {
	geesefs := testGeeseFS(t)
	store := newTestStore(t)
	e := newEnv(t)
	t.Cleanup(e.stopMounts)
	e.geesefs = geesefs
	e.startAgent()
	s := e.session()
	prefix := "test-buckets/" + uuid.NewString() + "/"
	if _, err := storagetest.Client().PutObject(t.Context(), &s3.PutObjectInput{
		Bucket: aws.String(store.bucket), Key: aws.String(prefix + "weights.txt"), Body: strings.NewReader("from the bucket"),
	}); err != nil {
		t.Fatal(err)
	}

	start := cloudBucketStart(e, store, prefix)
	id := start.GetStart().GetContainerId()
	s.send(t, start)
	s.phase(t, id, ready)
	if got := e.read(id, "/models/weights.txt"); got != `"from the bucket"` {
		t.Fatalf("read: %s", got)
	}
	attempt := e.task(id, `{"args": ["env", ""]}`)
	if got := result(t, e.completion(attempt)); got != "[]" {
		t.Fatalf("the container sees credentials: %s", got)
	}

	s.send(t, stopCommand(id, 1))
	s.phase(t, id, exited)
	keys := filepath.Join(e.stateDir, "storage", "buckets", bucketMountName(id, 0))
	e.eventually("the bucket's mount and keys go with its container", func() bool {
		_, err := os.Stat(keys)
		return os.IsNotExist(err) && len(e.mounters(true, kindBucket, "")) == 0
	})
}

// TestAnOrphanedCloudBucketMountIsRemoved: a restarted agent removes a
// cloud bucket mount whose container went while it was away, and its keys.
func TestAnOrphanedCloudBucketMountIsRemoved(t *testing.T) {
	geesefs := testGeeseFS(t)
	store := newTestStore(t)
	e := newEnv(t)
	t.Cleanup(e.stopMounts)
	e.geesefs = geesefs
	first := e.startAgent()
	s := e.session()
	start := cloudBucketStart(e, store, "test-buckets/"+uuid.NewString()+"/")
	id := start.GetStart().GetContainerId()
	s.send(t, start)
	s.phase(t, id, ready)
	first.stop()
	if _, err := e.docker.ContainerRemove(t.Context(), "lazycloud-"+id, client.ContainerRemoveOptions{Force: true}); err != nil {
		t.Fatal(err)
	}

	e.startAgent()
	e.session()
	if left := e.mounters(true, kindBucket, ""); len(left) != 0 {
		t.Fatalf("the orphaned bucket mount was left: %v", left[0].Names)
	}
	if _, err := os.Stat(filepath.Join(e.stateDir, "storage", "buckets", bucketMountName(id, 0))); !os.IsNotExist(err) {
		t.Fatalf("the orphaned bucket's keys were left: %v", err)
	}
}

// TestAStorageGrantTheHostCannotStoreIsNotAcknowledged: the server sends a
// grant until the host acknowledges it, so only a stored grant is.
func TestAStorageGrantTheHostCannotStoreIsNotAcknowledged(t *testing.T) {
	e := newEnv(t)
	e.startAgent()
	s := e.session()
	store := testStore{endpoint: "http://127.0.0.1:1", bucket: "bucket", accessKey: "key", secretKey: "secret"}
	refused := store.grant("not-a-workspace")
	stored := store.grant(uuid.NewString())
	s.send(t, refused)
	s.send(t, stored)
	// Commands are acknowledged in the order they arrive.
	s.until(t, 30*time.Second, func(m *hostproto.HostMessage) bool {
		if acked(refused.GetCommandId())(m) {
			t.Fatal("the host acknowledged a grant it could not store")
		}
		return acked(stored.GetCommandId())(m)
	})
}

// stopMounts stops this test's mount containers so GeeseFS unmounts before
// the state directory is removed.
func (e *env) stopMounts() {
	ctx := context.Background()
	list, err := e.docker.ContainerList(ctx, client.ContainerListOptions{All: true, Filters: client.Filters{}.
		Add("label", "lazycloud.agent="+e.id).Add("label", labelKind)})
	if err != nil {
		e.t.Errorf("list mount containers: %v", err)
		return
	}
	timeout := 10
	for _, c := range list.Items {
		if _, err := e.docker.ContainerStop(ctx, c.ID, client.ContainerStopOptions{Timeout: &timeout}); err != nil {
			e.t.Errorf("stop mount container: %v", err)
		}
	}
}
