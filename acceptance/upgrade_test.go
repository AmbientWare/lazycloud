package acceptance

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	goruntime "runtime"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/agent"
	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

const upgradeApp = `
from pathlib import Path

HITS = Path("/volumes/data/hits")


def hits() -> int:
    count = int(HITS.read_text()) + 1 if HITS.exists() else 1
    HITS.write_text(str(count))
    return count


def peek() -> int:
    return int(HITS.read_text())
`

// An agent of the release this change replaces, serving a workload that
// holds a volume through its snapshotter's cache, updates itself to this
// tree's release when it becomes the target. The host keeps the old
// snapshotter while the workload runs: the workload's volume reads and
// writes on through it, and a new start of the workspace waits off the
// host. Once the workload stops, the agent replaces the snapshotter and
// the new start mounts the same volume.
func TestAnOldSnapshotterWithALiveVolumeServesUntilItIsReplaced(t *testing.T) {
	old := oldRelease(t)
	dist := t.TempDir()
	p := startServer(t, serverOptions{dist: dist})
	if p.geesefs == "" {
		t.Skip("volume mounts need GeeseFS; run deploy/local/fetch-geesefs.sh")
	}
	// The old snapshotter caches volumes as on its large GPU hosts.
	cacheVolumes(t)
	head := buildRelease(t, dist)
	root, state := p.installRelease(old)
	host := p.startHostAgent(root, state)
	defer host.stop()
	p.awaitHost()
	if v := p.agentVersion(); v != old.version {
		t.Fatalf("the host runs %q, want the old release %s", v, old.version)
	}

	source := p.upload(map[string]string{"app.py": upgradeApp})
	live := endpointSpec(source, "hits", "app:hits", "/")
	keepWarm := 600
	live.KeepWarmSeconds = &keepWarm
	live.Volumes = &[]apitypes.VolumeMountSpec{{Name: "data"}}
	p.deploy("live", live)
	hits := p.describe("live", apitypes.WorkloadKindEndpoint, "hits").Url
	if status, _, body := p.call(http.MethodGet, hits, ""); status != http.StatusOK || body != "1" {
		t.Fatalf("first request on the old agent: %d %s", status, body)
	}

	p.publish(head)
	for deadline := time.Now().Add(3 * time.Minute); ; time.Sleep(200 * time.Millisecond) {
		// The agent commits the release on trial once its session stayed
		// open.
		_, trial := os.Stat(filepath.Join(state, agent.TrialFile))
		if current, _ := os.Readlink(filepath.Join(root, "current")); filepath.Base(current) == head.version && errors.Is(trial, fs.ErrNotExist) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the host did not commit the new release")
		}
	}
	if v := p.agentVersion(); v != "" {
		t.Fatalf("a host that kept the old snapshotter reports release %q, want none", v)
	}
	peek := endpointSpec(source, "peek", "app:peek", "/")
	peek.Volumes = &[]apitypes.VolumeMountSpec{{Name: "data"}}
	p.deploy("second", peek)
	for n := 2; n <= 6; n++ {
		if status, _, body := p.call(http.MethodGet, hits, ""); status != http.StatusOK || body != strconv.Itoa(n) {
			t.Fatalf("request %d after the update: %d %s", n, status, body)
		}
		if assigned := p.assignedTo("second"); assigned > 0 {
			t.Fatalf("%d containers assigned to the host that kept the old snapshotter", assigned)
		}
		time.Sleep(5 * time.Second)
	}

	ctx := t.Context()
	id, err := p.control.FindWorkload(ctx, p.workspace.ID, control.WorkloadRef{App: "live", Kind: apitypes.WorkloadKindEndpoint, Name: "hits"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := p.control.StopWorkload(ctx, p.workspace.ID, id); err != nil {
		t.Fatal(err)
	}
	p.awaitAgentVersion(head.version, 3*time.Minute)
	if status, _, body := p.call(http.MethodGet, p.describe("second", apitypes.WorkloadKindEndpoint, "peek").Url, ""); status != http.StatusOK || body != "6" {
		t.Fatalf("second start once the snapshotter was replaced: %d %s", status, body)
	}
	installed, err := os.ReadFile(layersource.Binary)
	if err != nil {
		t.Fatal(err)
	}
	if built, _ := os.ReadFile(filepath.Join(head.dir, "lazycloud-snapshotter")); !bytes.Equal(installed, built) { //nolint:gosec // The test's own build.
		t.Fatal("the host runs another snapshotter than the new release's")
	}
}

// cacheVolumes has the snapshotter of the release this change replaces
// cache volumes until the test ends; this tree's ignores the setting.
func cacheVolumes(t *testing.T) {
	t.Helper()
	conf := "/etc/systemd/system/" + layersource.Unit + ".d/old-volume-cache.conf"
	if err := os.MkdirAll(filepath.Dir(conf), 0o755); err != nil { //nolint:gosec // A systemd drop-in directory.
		t.Fatal(err)
	}
	if err := os.WriteFile(conf, []byte("[Service]\nEnvironment=LAZYCLOUD_VOLUME_CACHE=true\n"), 0o644); err != nil { //nolint:gosec // A systemd drop-in.
		t.Fatal(err)
	}
	reload := func() {
		if out, err := exec.CommandContext(context.Background(), "systemctl", "daemon-reload").CombinedOutput(); err != nil {
			t.Errorf("systemctl daemon-reload: %v: %s", err, out)
		}
	}
	reload()
	t.Cleanup(func() {
		if err := os.Remove(conf); err != nil {
			t.Error(err)
		}
		reload()
	})
}

// assignedTo counts the containers of app's releases a host holds.
func (p *platform) assignedTo(app string) int {
	p.t.Helper()
	var n int
	if err := p.pool.QueryRow(p.t.Context(), `select count(*) from containers c
join releases r on r.id = c.release_id join workloads w on w.id = r.workload_id join apps a on a.id = w.app_id
where a.name = $1 and c.host_id is not null`, app).Scan(&n); err != nil {
		p.t.Fatal(err)
	}
	return n
}

// An agent of the release this change replaces that restarts after the
// target moved on, as a reserve resumed from an older release does, takes
// no work until it runs the target, then runs it.
func TestAnOldAgentRestartingAfterTheTargetChangedTakesNoWorkUntilItUpdates(t *testing.T) {
	old := oldRelease(t)
	dist := t.TempDir()
	p := startServer(t, serverOptions{dist: dist})
	if p.geesefs == "" {
		t.Skip("volume mounts need GeeseFS; run deploy/local/fetch-geesefs.sh")
	}
	head := buildRelease(t, dist)
	root, state := p.installRelease(old)
	agent := p.startHostAgent(root, state)
	p.awaitHost()
	agent.stop()

	p.publish(head)
	s := endpointSpec(p.upload(map[string]string{"app.py": upgradeApp}), "hits", "app:hits", "/")
	s.Volumes = &[]apitypes.VolumeMountSpec{{Name: "data"}}
	p.deploy("resumed", s)
	agent = p.startHostAgent(root, state)
	defer agent.stop()

	// No container is ever assigned to the host while it runs the old
	// release; each sample reads both in one snapshot.
	watched := make(chan error, 1)
	go func() {
		ctx := t.Context()
		for {
			var version string
			var assigned int
			if err := p.pool.QueryRow(ctx, `select h.agent_version, count(c.id)
from hosts h left join containers c on c.host_id = h.id
where h.state = 'online' group by h.agent_version`).Scan(&version, &assigned); err == nil {
				if version == old.version && assigned > 0 {
					watched <- fmt.Errorf("%d containers assigned to the host on the old release", assigned)
					return
				}
				if version == head.version {
					watched <- nil
					return
				}
			}
			select {
			case <-ctx.Done():
				watched <- ctx.Err()
				return
			case <-time.After(50 * time.Millisecond):
			}
		}
	}()
	if status, _, body := p.call(http.MethodGet, p.describe("resumed", apitypes.WorkloadKindEndpoint, "hits").Url, ""); status != http.StatusOK || body != "1" {
		t.Fatalf("request to the restarted host: %d %s", status, body)
	}
	if err := <-watched; err != nil {
		t.Fatal(err)
	}
}

// release is an agent release: its version and a directory holding its
// releaseFiles.
type release struct {
	version string
	dir     string
	sha256  string
}

// releaseFiles make up an agent release.
var releaseFiles = []string{"VERSION", "lazycloud-agent", "supervisor", "lazycloud-snapshotter"}

// oldRelease is the agent release this change replaces, built from the
// commit it is based on into LAZYCLOUD_TEST_OLD_AGENT.
func oldRelease(t *testing.T) release {
	t.Helper()
	dir := os.Getenv("LAZYCLOUD_TEST_OLD_AGENT")
	if dir == "" {
		t.Skip("LAZYCLOUD_TEST_OLD_AGENT names no directory holding the previous release's lazycloud-agent, supervisor and lazycloud-snapshotter")
	}
	out, err := exec.CommandContext(t.Context(), filepath.Join(dir, "lazycloud-agent"), "--version").Output() //nolint:gosec // The test's own build.
	if err != nil {
		t.Fatalf("old agent version: %v", err)
	}
	return release{version: strings.TrimSpace(string(out)), dir: dir}
}

// buildRelease builds this tree's agent, supervisor and snapshotter as a
// new release and writes its archive into dist as the API serves it.
func buildRelease(t *testing.T, dist string) release {
	t.Helper()
	suffix := make([]byte, 4)
	_, _ = rand.Read(suffix)
	r := release{version: "head-" + hex.EncodeToString(suffix), dir: t.TempDir()}
	for name, pkg := range map[string]string{"lazycloud-agent": "../cmd/agent", "lazycloud-snapshotter": "../cmd/snapshotter"} {
		build := exec.CommandContext(t.Context(), "go", "build", "-trimpath", "-o", filepath.Join(r.dir, name), pkg) //nolint:gosec // The test's own build.
		build.Env = append(os.Environ(), "CGO_ENABLED=0")
		if out, err := build.CombinedOutput(); err != nil {
			t.Fatalf("build %s: %v\n%s", name, err, out)
		}
	}
	copyFile(t, supervisorBinary, filepath.Join(r.dir, "supervisor"))
	if err := os.WriteFile(filepath.Join(r.dir, "VERSION"), []byte(r.version+"\n"), 0o644); err != nil { //nolint:gosec // The release's version.
		t.Fatal(err)
	}
	var archive bytes.Buffer
	gz := gzip.NewWriter(&archive)
	tw := tar.NewWriter(gz)
	for _, name := range releaseFiles {
		data, err := os.ReadFile(filepath.Join(r.dir, name)) //nolint:gosec // The test's own build.
		if err != nil {
			t.Fatal(err)
		}
		if err := tw.WriteHeader(&tar.Header{Name: name, Mode: 0o755, Size: int64(len(data)), Typeflag: tar.TypeReg}); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write(data); err != nil {
			t.Fatal(err)
		}
	}
	if err := errors.Join(tw.Close(), gz.Close()); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(archive.Bytes())
	r.sha256 = hex.EncodeToString(sum[:])
	path := filepath.Join(dist, r.version, compute.ArchiveName(goruntime.GOARCH))
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil { //nolint:gosec // Test files.
		t.Fatal(err)
	}
	if err := os.WriteFile(path, archive.Bytes(), 0o644); err != nil { //nolint:gosec // Test files.
		t.Fatal(err)
	}
	return r
}

func copyFile(t *testing.T, from, to string) {
	t.Helper()
	data, err := os.ReadFile(from) //nolint:gosec // The test's own files.
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(to, data, 0o755); err != nil { //nolint:gosec // An executable.
		t.Fatal(err)
	}
}

// installRelease lays r out in a new agent root as the install script
// does, and returns the root and a state directory.
func (p *platform) installRelease(r release) (root, state string) {
	t := p.t
	t.Helper()
	root, state = p.dir(), p.dir()
	dir := filepath.Join(root, "releases", r.version)
	if err := os.MkdirAll(dir, 0o755); err != nil { //nolint:gosec // Test files.
		t.Fatal(err)
	}
	for _, name := range releaseFiles {
		copyFile(t, filepath.Join(r.dir, name), filepath.Join(dir, name))
	}
	if err := os.Symlink(filepath.Join("releases", r.version), filepath.Join(root, "current")); err != nil {
		t.Fatal(err)
	}
	return root, state
}

// publish makes r the target release for every host.
func (p *platform) publish(r release) {
	p.t.Helper()
	if err := p.compute.PublishAgentRelease(p.t.Context(), compute.AgentRelease{
		Version: r.version, SHA256: map[string]string{goruntime.GOARCH: r.sha256}, RolloutPercent: 100,
	}); err != nil {
		p.t.Fatal(err)
	}
}

// hostAgent is an agent run as on a host: the release current links to,
// under the service wrapper, started again when it installs an update.
type hostAgent struct {
	cancel context.CancelFunc
	done   chan struct{}
}

// startHostAgent runs the agent installed in root until stop or the
// platform stops.
func (p *platform) startHostAgent(root, state string) *hostAgent {
	p.t.Helper()
	ctx, cancel := context.WithCancel(p.ctx)
	a := &hostAgent{cancel: cancel, done: make(chan struct{})}
	args := []string{
		"../cmd/agent/agent-service.sh", "join", "--server", p.hosts, "--server-plaintext", "--state-dir", state,
		"--socket-dir", p.socketDir, "--join-token", p.join, "--runtime-dir", runtimeDir(p.t),
		"--supervisor", filepath.Join(root, "current", "supervisor"), "--oci-runtime", ociRuntime(),
		"--geesefs", p.geesefs, "--trust-bundle", tlsStore.CA, "--build-network", "host",
		"--label", testLabel + "=" + p.t.Name(),
	}
	p.wg.Go(func() {
		defer close(a.done)
		for {
			cmd := exec.CommandContext(ctx, "sh", args...) //nolint:gosec // The test's own wrapper.
			cmd.Env = append(os.Environ(), "LAZYCLOUD_AGENT_ROOT="+root, "LAZYCLOUD_AGENT_STATE_DIR="+state)
			cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
			// The service stops the agent as systemd does.
			cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGTERM) }
			cmd.WaitDelay = 30 * time.Second
			err := cmd.Run()
			var exit *exec.ExitError
			switch {
			case ctx.Err() != nil:
				return
			case errors.As(err, &exit) && exit.ExitCode() == 75:
				// It installed an update; the unit restarts it.
			default:
				p.t.Errorf("agent exited: %v", err)
				return
			}
		}
	})
	return a
}

// stop stops the agent and waits for it to exit.
func (a *hostAgent) stop() {
	a.cancel()
	<-a.done
}

// agentVersion is the release the online host's agent last opened its
// session with.
func (p *platform) agentVersion() string {
	p.t.Helper()
	var version string
	if err := p.pool.QueryRow(p.t.Context(), "select agent_version from hosts where state = 'online'").Scan(&version); err != nil {
		p.t.Fatal(err)
	}
	return version
}

// awaitAgentVersion waits until the online host runs version.
func (p *platform) awaitAgentVersion(version string, within time.Duration) {
	p.t.Helper()
	for deadline := time.Now().Add(within); p.agentVersion() != version; time.Sleep(200 * time.Millisecond) {
		if time.Now().After(deadline) {
			p.t.Fatalf("the host runs %s %s after the target became %s", p.agentVersion(), within, version)
		}
	}
}
