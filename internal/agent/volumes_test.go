package agent

import (
	"cmp"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"io"
	"math/big"
	"net"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
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

// reserveMounters adds the memory of n mounters to start's, as the server
// does.
func reserveMounters(start *hostproto.ServerMessage, n int64) {
	r := start.GetStart().GetResources()
	r.MountReserveBytes = n * hostproto.MounterMemoryBytes
	r.MemoryBytes += r.MountReserveBytes
}

func platformVolume(workspace, volume, path string, readOnly bool) *hostproto.VolumeMount {
	return &hostproto.VolumeMount{
		MountPath: path, ReadOnly: readOnly,
		Source: &hostproto.VolumeMount_Volume{Volume: &hostproto.PlatformVolume{
			VolumeId: volume, WorkspaceId: workspace, Prefix: "volumes/" + volume + "/",
		}},
	}
}

func volumeStart(e *env, source *hostproto.Source, workspace, volume string, readOnly bool) *hostproto.ServerMessage {
	start := e.startCommand("app:handle", 1)
	start.GetStart().Source = source
	start.GetStart().Volumes = []*hostproto.VolumeMount{platformVolume(workspace, volume, "/volumes/data", readOnly)}
	reserveMounters(start, 1)
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

// mounters lists the mount containers of container, with stopped ones when
// all is set.
func (e *env) mounters(all bool, container string) []containertypes.Summary {
	e.t.Helper()
	filters := client.Filters{}.Add("label", "lazycloud.agent="+e.id).Add("label", labelKind+"="+kindMount).
		Add("label", labelContainer+"="+container)
	list, err := e.docker.ContainerList(context.Background(), client.ContainerListOptions{All: all, Filters: filters})
	if err != nil {
		e.t.Fatal(err)
	}
	return list.Items
}

// mounter is container's one running mount container.
func (e *env) mounter(container string) containertypes.Summary {
	e.t.Helper()
	mounters := e.mounters(false, container)
	if len(mounters) != 1 {
		e.t.Fatalf("%d mounts run for %s", len(mounters), container)
	}
	return mounters[0]
}

// sliceDir is the cgroup of container's slice.
func (e *env) sliceDir(container string) string {
	a := &Agent{identity: identity{HostID: e.server.hostID}}
	return filepath.Join(cgroupRoot, "lazycloud.slice", "lazycloud-workloads.slice", a.workloadSlice(container))
}

// plantSlice makes a slice of this host's that names no container and
// returns its cgroup.
func (e *env) plantSlice() string {
	e.t.Helper()
	v := newVolumes(&Agent{identity: identity{HostID: e.server.hostID}})
	defer v.close()
	name := v.a.slicePrefix() + "planted.slice"
	if err := v.startSlice(e.t.Context(), name, containertypes.Resources{Memory: 64 << 20, CPUShares: 1024}); err != nil {
		e.t.Fatal(err)
	}
	dir := filepath.Join(cgroupRoot, "lazycloud.slice", "lazycloud-workloads.slice", name)
	if _, err := os.Stat(dir); err != nil {
		e.t.Fatal(err)
	}
	return dir
}

// cgroupOf is the cgroup of the Docker container id's first process.
func (e *env) cgroupOf(id string) string {
	e.t.Helper()
	inspect, err := e.docker.ContainerInspect(context.Background(), id, client.ContainerInspectOptions{})
	if err != nil {
		e.t.Fatal(err)
	}
	dir, err := cgroupDir(inspect.Container.State.Pid)
	if err != nil {
		e.t.Fatal(err)
	}
	return dir
}

// killGeeseFS kills the GeeseFS of a mount container, as the kernel's OOM
// killer would.
func (e *env) killGeeseFS(id string) {
	e.t.Helper()
	kill, err := e.docker.ExecCreate(context.Background(), id, client.ExecCreateOptions{Cmd: []string{"killall", "-KILL", "geesefs"}})
	if err != nil {
		e.t.Fatal(err)
	}
	if _, err := e.docker.ExecStart(context.Background(), kill.ID, client.ExecStartOptions{Detach: true}); err != nil {
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

// volumeEnv starts an agent that mounts volumes, configured by configure,
// and returns its session and the test's object store.
func volumeEnv(t *testing.T, configure ...func(*Config)) (*env, *serverSession, testStore) {
	t.Helper()
	geesefs := testGeeseFS(t)
	// Made first so that the mounts stop before the bucket goes.
	store := newTestStore(t)
	e := newEnv(t)
	t.Cleanup(e.stopMounts)
	e.geesefs = geesefs
	e.startAgent(configure...)
	return e, e.session(), store
}

// TestVolumesMountThroughWorkspaceBucket runs two containers on one volume:
// what one writes lands in the workspace bucket under the volume's prefix
// and the other reads it, while neither sees a credential. Each has a mount
// of its own in its slice. The workload is limited to the memory it asked
// for and the slice to that plus its mounter's reserve.
func TestVolumesMountThroughWorkspaceBucket(t *testing.T) {
	e, s, store := volumeEnv(t)
	source := serveSource(t, "testdata/volumes")
	workspace, volume := uuid.NewString(), uuid.NewString()

	start := volumeStart(e, source, workspace, volume, false)
	s.send(t, start)
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

	for _, id := range []string{writer, reader.GetStart().GetContainerId()} {
		slice := e.sliceDir(id)
		if workload, mount := e.cgroupOf("lazycloud-"+id), e.cgroupOf(e.mounter(id).ID); filepath.Dir(workload) != slice || filepath.Dir(mount) != slice {
			t.Fatalf("the workload runs in %s and its mount in %s, not both in %s", workload, mount, slice)
		}
		limit, err := os.ReadFile(filepath.Join(slice, "memory.max")) //nolint:gosec // A cgroup file.
		if err != nil || strings.TrimSpace(string(limit)) != strconv.Itoa(256<<20+hostproto.MounterMemoryBytes) {
			t.Fatalf("the slice's memory limit is %q (%v), want the container's with its mounter's", limit, err)
		}
		inspect, err := e.docker.ContainerInspect(t.Context(), "lazycloud-"+id, client.ContainerInspectOptions{})
		if err != nil {
			t.Fatal(err)
		}
		if r := inspect.Container.HostConfig.Resources; r.Memory != 256<<20 || r.MemoryReservation != 256<<20 {
			t.Fatalf("the workload's memory limit %d and reservation %d, want the 256 MiB it asked for", r.Memory, r.MemoryReservation)
		}
	}
}

// list lists the directory path in container through a task.
func (e *env) list(container, path string) []string {
	e.t.Helper()
	var names []string
	if err := json.Unmarshal([]byte(result(e.t, e.completion(e.task(container, `{"args": ["list", "`+path+`"]}`)))), &names); err != nil {
		e.t.Fatal(err)
	}
	return names
}

// TestPlatformVolumesShareOneMounter: a container's two volumes run on one
// mounter of the workspace's volumes, and the container sees those two
// only, each at its own path, not a third volume in the same bucket.
func TestPlatformVolumesShareOneMounter(t *testing.T) {
	e, s, store := volumeEnv(t)
	workspace, first, second, other := uuid.NewString(), uuid.NewString(), uuid.NewString(), uuid.NewString()
	if _, err := storagetest.Client().PutObject(t.Context(), &s3.PutObjectInput{
		Bucket: aws.String(store.bucket), Key: aws.String("volumes/" + other + "/private.txt"), Body: strings.NewReader("not yours"),
	}); err != nil {
		t.Fatal(err)
	}
	s.send(t, store.grant(workspace))

	start := e.startCommand("app:handle", 1)
	start.GetStart().Source = serveSource(t, "testdata/volumes")
	start.GetStart().Volumes = []*hostproto.VolumeMount{
		platformVolume(workspace, first, "/volumes/first", false),
		platformVolume(workspace, second, "/volumes/second", true),
	}
	reserveMounters(start, 1)
	id := start.GetStart().GetContainerId()
	s.send(t, start)
	s.phase(t, id, ready)
	e.mounter(id)

	e.write(id, "/volumes/first/a.txt", "first")
	if got := e.read(id, "/volumes/first/a.txt"); got != `"first"` {
		t.Fatalf("read: %s", got)
	}
	if got := e.list(id, "/volumes"); !slices.Equal(got, []string{"first", "second"}) {
		t.Fatalf("the container sees volumes %v", got)
	}
	if got := e.list(id, "/volumes/second"); len(got) != 0 {
		t.Fatalf("the second volume holds %v", got)
	}
	if failure := e.completion(e.task(id, `{"args": ["write", "/volumes/second/b.txt", "x"]}`)).GetFailure(); failure == nil {
		t.Fatal("the read-only volume accepted a write")
	}
	if _, err := storagetest.Client().HeadObject(t.Context(), &s3.HeadObjectInput{
		Bucket: aws.String(store.bucket), Key: aws.String("volumes/" + first + "/a.txt"),
	}); err != nil {
		t.Fatalf("the first volume's file is not under its prefix: %v", err)
	}
}

// dockerProxy passes the agent's Docker connections through to the daemon;
// cut drops those open, as a daemon restart does.
type dockerProxy struct {
	mu    sync.Mutex
	conns []net.Conn
}

// proxyDocker points the agents the test starts at a new dockerProxy.
func proxyDocker(t *testing.T) *dockerProxy {
	t.Helper()
	daemon := strings.TrimPrefix(cmp.Or(os.Getenv("DOCKER_HOST"), "unix:///var/run/docker.sock"), "unix://")
	listener, err := (&net.ListenConfig{}).Listen(t.Context(), "unix", filepath.Join(shortDir(t), "docker.sock"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	t.Setenv("DOCKER_HOST", "unix://"+listener.Addr().String())
	p := &dockerProxy{}
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			upstream, err := (&net.Dialer{}).DialContext(context.Background(), "unix", daemon)
			if err != nil {
				_ = conn.Close()
				continue
			}
			p.mu.Lock()
			p.conns = append(p.conns, conn, upstream)
			p.mu.Unlock()
			go pipe(conn, upstream)
			go pipe(upstream, conn)
		}
	}()
	return p
}

func pipe(dst, src net.Conn) {
	_, _ = io.Copy(dst, src)
	_ = dst.Close()
	_ = src.Close()
}

func (p *dockerProxy) cut() {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, conn := range p.conns {
		_ = conn.Close()
	}
	p.conns = nil
}

// TestAMountThatDiesFailsOnlyItsContainer: two containers on one volume each
// have a mount of their own. One's mount dying, as an out-of-memory kill
// would, stops that container only, also after the agent lost its watch on
// Docker; the other reads and writes on, and its mount and slice go when it
// stops.
func TestAMountThatDiesFailsOnlyItsContainer(t *testing.T) {
	var docker *dockerProxy
	e, s, store := volumeEnv(t, func(*Config) { docker = proxyDocker(t) })
	source := serveSource(t, "testdata/volumes")
	workspace, volume := uuid.NewString(), uuid.NewString()
	s.send(t, store.grant(workspace))
	heavy := e.startVolume(s, source, workspace, volume)
	other := e.startVolume(s, source, workspace, volume)
	e.write(heavy, "/volumes/data/shared.txt", "shared")

	docker.cut()
	e.killGeeseFS(e.mounter(heavy).ID)
	if exit := s.phase(t, heavy, exited).GetExit(); !strings.Contains(exit.GetMessage(), "volume mount") {
		t.Fatalf("exit of the container on the dead mount: %v", exit)
	}
	e.write(other, "/volumes/data/still.txt", "still here")
	if got := e.read(other, "/volumes/data/shared.txt"); got != `"shared"` {
		t.Fatalf("the other container reads %s", got)
	}
	e.eventually("the dead mount and its slice go", func() bool {
		_, err := os.Stat(e.sliceDir(heavy))
		return len(e.mounters(true, heavy)) == 0 && os.IsNotExist(err)
	})

	s.send(t, stopCommand(other, 1))
	s.phase(t, other, exited)
	e.eventually("the mount and slice go with their container", func() bool {
		_, err := os.Stat(e.sliceDir(other))
		return len(e.mounters(true, other)) == 0 && os.IsNotExist(err)
	})
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
	e.eventually("the mount that never mounted is removed", func() bool {
		return len(e.mounters(true, untrusted.GetStart().GetContainerId())) == 0
	})

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
// reads and writes on, and mounts a new start with its own. The old mount
// stops with its workload; the new one's runs on.
func TestVolumeMountsSurviveAnAgentUpgrade(t *testing.T) {
	var before string
	e, s, store := volumeEnv(t, func(c *Config) {
		before = copyFile(t, c.GeeseFSPath, "")
		c.GeeseFSPath, c.TrustBundle = before, copyFile(t, HostTrustBundle(), "")
	})
	first, after := e.running, copyFile(t, e.geesefs, "")
	source := serveSource(t, "testdata/volumes")
	workspace, volume := uuid.NewString(), uuid.NewString()
	s.send(t, store.grant(workspace))
	old := e.startVolume(s, source, workspace, volume)
	e.write(old, "/volumes/data/before.txt", "before")
	first.stop()

	e.startAgent(func(c *Config) {
		c.GeeseFSPath, c.TrustBundle = after, copyFile(t, HostTrustBundle(), "\n")
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
	binds := func(id, path string) bool {
		return slices.ContainsFunc(e.mounter(id).Mounts, func(m containertypes.MountPoint) bool { return m.Source == path })
	}
	if !binds(old, before) || !binds(current, after) {
		t.Fatal("a mount does not run the GeeseFS of the agent that started it")
	}

	s.send(t, stopCommand(old, 1))
	s.phase(t, old, exited)
	e.eventually("the old mount stops with its workload", func() bool { return len(e.mounters(true, old)) == 0 })
	e.write(current, "/volumes/data/later.txt", "later")
	if got := e.read(current, "/volumes/data/before.txt"); got != `"before"` {
		t.Fatalf("the new workload reads %s after the old mount stopped", got)
	}
}

// bucketMount mounts prefix of the store's bucket read-only at path.
func (s testStore) bucketMount(path, prefix string) *hostproto.VolumeMount {
	return &hostproto.VolumeMount{
		MountPath: path, ReadOnly: true,
		Source: &hostproto.VolumeMount_CloudBucket{CloudBucket: &hostproto.CloudBucket{
			Bucket: s.bucket, Prefix: prefix, Region: s.region, Endpoint: s.endpoint, ForcePathStyle: true,
			AccessKeyId: s.accessKey, SecretAccessKey: s.secretKey,
		}},
	}
}

// cloudBucketStart starts a container on mounts of one bucket.
func cloudBucketStart(e *env, mounts ...*hostproto.VolumeMount) *hostproto.ServerMessage {
	start := e.startCommand("app:handle", 1)
	start.GetStart().Source = serveSource(e.t, "testdata/volumes")
	start.GetStart().Volumes = mounts
	reserveMounters(start, 1)
	return start
}

// settle waits until container is ready or has exited.
func (s *serverSession) settle(t *testing.T, container string) *hostproto.ContainerReport {
	t.Helper()
	return s.until(t, 120*time.Second, func(m *hostproto.HostMessage) bool {
		r := m.GetContainer()
		return r.GetContainerId() == container && (r.GetPhase() == ready || r.GetPhase() == exited)
	}).GetContainer()
}

// TestABucketPrefixStaysInsideItsMount: a container's mounts of one bucket
// share a mounter, and each binds its own directory of the mount. A prefix
// that climbs out of the mount fails the start instead of binding the
// agent's state.
func TestABucketPrefixStaysInsideItsMount(t *testing.T) {
	e, s, store := volumeEnv(t)
	start := cloudBucketStart(e, store.bucketMount("/inside", "a/"), store.bucketMount("/outside", "a/../../"))
	s.send(t, start)
	r := s.settle(t, start.GetStart().GetContainerId())
	if r.GetPhase() != exited || r.GetExit().GetReason() != hostproto.ExitReason_EXIT_REASON_START_FAILED ||
		!strings.Contains(r.GetExit().GetMessage(), "outside its bucket's mount") {
		t.Fatalf("a start with a prefix outside its mount: %v", r)
	}
}

// TestAdoptKeepsLiveMountsAndRemovesOrphans: a restarted agent keeps a
// running container's mount, fails a container whose mount died while it
// was away, and removes the mount, keys and slice of a container that went,
// a slice that names no container and a mount directory without a mounter.
func TestAdoptKeepsLiveMountsAndRemovesOrphans(t *testing.T) {
	e, s, store := volumeEnv(t)
	first := e.running
	source := serveSource(t, "testdata/volumes")
	workspace, volume := uuid.NewString(), uuid.NewString()
	s.send(t, store.grant(workspace))
	kept := e.startVolume(s, source, workspace, volume)
	e.write(kept, "/volumes/data/kept.txt", "kept")
	lost := e.startVolume(s, source, workspace, volume)
	bucket := cloudBucketStart(e, store.bucketMount("/models", "test-buckets/"+uuid.NewString()+"/"))
	gone := bucket.GetStart().GetContainerId()
	s.send(t, bucket)
	s.phase(t, gone, ready)
	keys := filepath.Join(e.stateDir, "storage", "buckets", mounterName(gone, 0))
	if _, err := os.Stat(keys); err != nil {
		t.Fatalf("the bucket's keys: %v", err)
	}
	first.stop()
	e.killGeeseFS(e.mounter(lost).ID)
	if _, err := e.docker.ContainerRemove(t.Context(), "lazycloud-"+gone, client.ContainerRemoveOptions{Force: true}); err != nil {
		t.Fatal(err)
	}
	stray := filepath.Join(e.stateDir, "mounts", "stray")
	if err := os.Mkdir(stray, 0o700); err != nil {
		t.Fatal(err)
	}
	planted := e.plantSlice()

	// The agent comes back without GeeseFS, which adopting needs no more
	// than removing does.
	e.geesefs = ""
	e.startAgent()
	s = e.session()
	s.adoptedReady(t, kept)
	e.write(kept, "/volumes/data/again.txt", "again")
	if got := e.read(kept, "/volumes/data/kept.txt"); got != `"kept"` {
		t.Fatalf("the adopted container reads %s", got)
	}
	if exit := s.adoptedExit(t, lost); !strings.Contains(exit.GetMessage(), "volume mount") {
		t.Fatalf("exit of a container whose mount died while the agent was away: %v", exit)
	}
	if left := e.mounters(true, gone); len(left) != 0 {
		t.Fatalf("the orphaned mount was left: %v", left[0].Names)
	}
	if _, err := os.Stat(keys); !os.IsNotExist(err) {
		t.Fatalf("the orphaned bucket's keys were left: %v", err)
	}
	for _, left := range []string{e.sliceDir(gone), planted, stray} {
		if _, err := os.Stat(left); !os.IsNotExist(err) {
			t.Fatalf("%s was left: %v", left, err)
		}
	}
	e.eventually("the dead mount goes with its container", func() bool { return len(e.mounters(true, lost)) == 0 })
}

// TestCloudBucketMountsWithItsKeys mounts two prefixes of a user's bucket
// through one mounter with keys from the start, one of them empty, and
// removes the mount and its keys when the container goes.
func TestCloudBucketMountsWithItsKeys(t *testing.T) {
	e, s, store := volumeEnv(t)
	prefix := "test-buckets/" + uuid.NewString() + "/"
	if _, err := storagetest.Client().PutObject(t.Context(), &s3.PutObjectInput{
		Bucket: aws.String(store.bucket), Key: aws.String(prefix + "weights.txt"), Body: strings.NewReader("from the bucket"),
	}); err != nil {
		t.Fatal(err)
	}

	start := cloudBucketStart(e, store.bucketMount("/models", prefix), store.bucketMount("/empty", prefix+"nothing/"))
	id := start.GetStart().GetContainerId()
	s.send(t, start)
	s.phase(t, id, ready)
	e.mounter(id)
	if got := e.read(id, "/models/weights.txt"); got != `"from the bucket"` {
		t.Fatalf("read: %s", got)
	}
	if got := e.list(id, "/empty"); len(got) != 0 {
		t.Fatalf("the empty prefix holds %v", got)
	}

	s.send(t, stopCommand(id, 1))
	s.phase(t, id, exited)
	keys := filepath.Join(e.stateDir, "storage", "buckets", mounterName(id, 0))
	e.eventually("the bucket's mount and keys go with its container", func() bool {
		_, err := os.Stat(keys)
		return os.IsNotExist(err) && len(e.mounters(true, id)) == 0
	})
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
// the state directory is removed, then its slices.
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
	v := newVolumes(&Agent{identity: identity{HostID: e.server.hostID}})
	defer v.close()
	sliced, err := v.hostSlices(ctx)
	if err != nil {
		e.t.Errorf("list slices: %v", err)
		return
	}
	if err := v.stopSlices(ctx, sliced...); err != nil {
		e.t.Errorf("stop slices: %v", err)
	}
}

// TestAMountThatDiesDuringItsStartFailsIt: a mount that dies while the start
// waits for another fails the start, so the workload never runs on its dead
// bind.
func TestAMountThatDiesDuringItsStartFailsIt(t *testing.T) {
	e, s, store := volumeEnv(t)
	// The bucket mounts; the volume waits for a grant that never comes.
	start := e.startCommand("app:handle", 1)
	start.GetStart().Source = serveSource(t, "testdata/volumes")
	start.GetStart().Volumes = []*hostproto.VolumeMount{
		store.bucketMount("/models", "test-buckets/"+uuid.NewString()+"/"),
		platformVolume(uuid.NewString(), uuid.NewString(), "/volumes/data", false),
	}
	reserveMounters(start, 2)
	id := start.GetStart().GetContainerId()
	s.send(t, start)
	bucket := filepath.Join(e.stateDir, "mounts", mounterName(id, 0))
	e.eventually("the bucket mounts", func() bool { return mounted(bucket) })
	// The start sees the mount within a poll; a mount that dies sooner fails
	// it as one that never came up.
	time.Sleep(3 * mountPoll)

	e.killGeeseFS(e.mounter(id).ID)
	if r := s.settle(t, id); r.GetPhase() != exited || !strings.Contains(r.GetExit().GetMessage(), "volume mount "+mounterName(id, 0)+" exited") {
		t.Fatalf("a start whose mount died: %v", r)
	}
}

// TestSlicesOutliveADroppedSystemdConnection: the agent connects to systemd
// again once its connection drops.
func TestSlicesOutliveADroppedSystemdConnection(t *testing.T) {
	v := newVolumes(&Agent{identity: identity{HostID: uuid.NewString()}})
	defer v.close()
	conn, err := v.systemd(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	conn.Close()
	if _, err := v.hostSlices(t.Context()); err != nil {
		t.Fatalf("list slices after the connection dropped: %v", err)
	}
}

// TestADyingMountFailsItsStartOrItsContainer: whichever the agent sees
// first, a mount appearing or its container exiting, a mount that dies
// either fails the start that waits for it or is lost to its container.
func TestADyingMountFailsItsStartOrItsContainer(t *testing.T) {
	v := newVolumes(&Agent{})
	exitedFirst := v.newMounter(mounterName(uuid.NewString(), 0), "")
	if v.markExited(exitedFirst) || v.markUp(exitedFirst) {
		t.Fatal("a mount whose container exited before it appeared came up")
	}
	upFirst := v.newMounter(mounterName(uuid.NewString(), 0), "")
	if !v.markUp(upFirst) || !v.markExited(upFirst) {
		t.Fatal("a mount that came up and exited was not lost")
	}
}
