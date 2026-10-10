package agent

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	cerrdefs "github.com/containerd/errdefs"
	"github.com/google/uuid"
	containertypes "github.com/moby/moby/api/types/container"
	"github.com/moby/moby/api/types/mount"
	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/platformimages"
)

// Volumes mount per workspace: GeeseFS runs in a mount container that holds
// the workspace's credentials, and workload containers bind-mount their
// volumes' directories from it. Workloads never see the credentials. The
// mount container shares its mount with the host through a
// shared-propagation bind of the mount directory, so it works for root and
// unprivileged agents alike and outlives agent restarts, like the workload
// containers that use it.
//
// A workspace's mount containers are generations. Each mounts at
// mounts/<workspace>/<generation> and is labelled with the fingerprint of
// what it was made with: the GeeseFS binary, the trust bundle and the
// bucket's location. Only a mounted generation whose fingerprint is current
// takes new users. A stale one keeps the users whose binds point into it
// and stops when the last one goes.
const (
	labelKind        = "lazycloud.kind"
	labelWorkspace   = "lazycloud.workspace-id"
	labelWorkspaces  = "lazycloud.workspaces"
	labelGeneration  = "lazycloud.mount-generation"
	labelFingerprint = "lazycloud.mount-fingerprint"
	kindMount        = "volume-mount"
	kindBucket       = "bucket-mount"
	// grantWait bounds how long a start waits for a workspace's first grant.
	grantWait = time.Minute
	// mountWait bounds how long a new mount takes to appear once its
	// container runs.
	mountWait = 30 * time.Second
	// mountIdle is how long a current mount nothing uses stays up.
	mountIdle = 5 * time.Minute
	// geesefsMemoryMiB bounds the cache of one workspace mount, and
	// mounterMemoryBytes the whole mount container.
	geesefsMemoryMiB   = 512
	mounterMemoryBytes = 1 << 30
	mounterPidsLimit   = 256
	// grantMargin is how long a stored key must still be valid to mount.
	grantMargin = time.Minute
)

// volumes owns the host's mount containers and their credentials.
type volumes struct {
	a *Agent
	// geesefsDigest is the digest of the agent's GeeseFS binary, which does
	// not change while the agent runs.
	geesefsDigest func() (string, error)

	mu sync.Mutex
	// granted is closed once a workspace's first grant is written.
	granted map[string]chan struct{}
	// generations are the workspace mounts, by Docker name.
	generations map[string]*mounter
	// buckets are each container's cloud bucket mounts.
	buckets map[string][]*mounter
}

// mounter is one mount container: a workspace generation, or one
// container's cloud bucket.
type mounter struct {
	// name is the Docker name; dir the host path of the mount.
	name, dir string
	// workspace and fingerprint are set for a workspace generation.
	workspace, fingerprint string
	// ready closes once the mount appeared or failed; err says why it
	// failed.
	ready chan struct{}
	err   error
	// exited closes when the container stops running.
	exited chan struct{}

	// The fields below are guarded by volumes.mu.
	// users are the workload containers whose binds point into the mount.
	users map[string]struct{}
	// idleSince is when the mount lost its last user.
	idleSince time.Time
	// up: the mount appeared.
	up bool
	// retired: the generation takes no new users.
	retired bool
	// stopping: the container stops on purpose, so its exit fails no user.
	stopping bool
}

func newMounter(name, dir string) *mounter {
	return &mounter{name: name, dir: dir, ready: make(chan struct{}), exited: make(chan struct{}), users: map[string]struct{}{}}
}

func newVolumes(a *Agent) *volumes {
	return &volumes{
		a:             a,
		geesefsDigest: sync.OnceValues(func() (string, error) { return fileDigest(a.cfg.GeeseFSPath) }),
		granted:       map[string]chan struct{}{},
		generations:   map[string]*mounter{},
		buckets:       map[string][]*mounter{},
	}
}

func (v *volumes) storageDir(workspace string) string {
	return filepath.Join(v.a.cfg.StateDir, "storage", workspace)
}

func (v *volumes) mountDir() string { return filepath.Join(v.a.cfg.StateDir, "mounts") }

func generationName(workspace, generation string) string {
	return "lazycloud-mount-" + workspace + "-" + generation
}

func (v *volumes) grantChannel(workspace string) chan struct{} {
	ch, ok := v.granted[workspace]
	if !ok {
		ch = make(chan struct{})
		v.granted[workspace] = ch
	}
	return ch
}

// processCredentials is the AWS credential_process output GeeseFS reads,
// again whenever the previous one expires.
type processCredentials struct {
	Version         int    `json:"Version"`
	AccessKeyID     string `json:"AccessKeyId"`
	SecretAccessKey string `json:"SecretAccessKey"`
	SessionToken    string `json:"SessionToken,omitempty"`
	// Expiration is absent for keys that do not expire, such as a cloud
	// bucket's own.
	Expiration string `json:"Expiration,omitempty"`
}

// grant stores a workspace's credentials where its mount reads them.
func (v *volumes) grant(g *hostproto.StorageGrant) error {
	workspace := g.GetWorkspaceId()
	if !isUUID(workspace) {
		return fmt.Errorf("storage grant for %q: not a workspace id", workspace)
	}
	if g.GetEndpoint() == "" || g.GetBucket() == "" {
		return fmt.Errorf("storage grant for %s names no endpoint or bucket", workspace)
	}
	dir := v.storageDir(workspace)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("create storage directory: %w", err)
	}
	creds, err := json.Marshal(processCredentials{
		Version: 1, AccessKeyID: g.GetAccessKeyId(), SecretAccessKey: g.GetSecretAccessKey(),
		SessionToken: g.GetSessionToken(), Expiration: g.GetExpiresAt().AsTime().UTC().Format(time.RFC3339),
	})
	if err != nil {
		return fmt.Errorf("encode credentials: %w", err)
	}
	if err := writeFileAtomic(filepath.Join(dir, "credentials.json"), creds, 0o600); err != nil {
		return fmt.Errorf("write credentials: %w", err)
	}
	config := "[default]\ncredential_process = cat /creds/credentials.json\n"
	if err := writeFileAtomic(filepath.Join(dir, "config"), []byte(config), 0o644); err != nil { //nolint:gosec // Holds no secret.
		return fmt.Errorf("write credential config: %w", err)
	}
	location, err := json.Marshal(bucketLocation{
		Endpoint: g.GetEndpoint(), Region: g.GetRegion(), Bucket: g.GetBucket(), PathStyle: g.GetForcePathStyle(),
	})
	if err != nil {
		return fmt.Errorf("encode grant: %w", err)
	}
	if err := writeFileAtomic(filepath.Join(dir, "location.json"), location, 0o600); err != nil {
		return fmt.Errorf("write bucket location: %w", err)
	}
	v.mu.Lock()
	ch := v.grantChannel(workspace)
	select {
	case <-ch:
	default:
		close(ch)
	}
	v.mu.Unlock()
	return nil
}

// bucketLocation is where a workspace's bucket is, kept beside its
// credentials so a restarted agent can remount.
type bucketLocation struct {
	Endpoint  string `json:"endpoint"`
	Region    string `json:"region"`
	Bucket    string `json:"bucket"`
	PathStyle bool   `json:"path_style"`
}

func (v *volumes) location(workspace string) (bucketLocation, error) {
	var loc bucketLocation
	data, err := os.ReadFile(filepath.Join(v.storageDir(workspace), "location.json"))
	if err != nil {
		return loc, fmt.Errorf("read bucket location: %w", err)
	}
	if err := json.Unmarshal(data, &loc); err != nil {
		return loc, fmt.Errorf("decode bucket location: %w", err)
	}
	return loc, nil
}

// fingerprint identifies what a workspace mount at loc is made with now. The
// trust bundle is read each time, so a bundle the host replaces reaches the
// next mount.
func (v *volumes) fingerprint(loc bucketLocation) (string, error) {
	geesefs, err := v.geesefsDigest()
	if err != nil {
		return "", fmt.Errorf("GeeseFS binary: %w", err)
	}
	bundle, err := fileDigest(v.a.cfg.TrustBundle)
	if err != nil {
		return "", fmt.Errorf("trust bundle: %w", err)
	}
	sum := sha256.New()
	for _, part := range []string{geesefs, bundle, loc.Endpoint, loc.Region, loc.Bucket, strconv.FormatBool(loc.PathStyle)} {
		sum.Write([]byte(part))
		sum.Write([]byte{0})
	}
	return hex.EncodeToString(sum.Sum(nil)), nil
}

func fileDigest(path string) (string, error) {
	f, err := os.Open(path) //nolint:gosec // The agent's own configured files.
	if err != nil {
		return "", fmt.Errorf("open: %w", err)
	}
	defer func() { _ = f.Close() }()
	sum := sha256.New()
	if _, err := io.Copy(sum, f); err != nil {
		return "", fmt.Errorf("read %s: %w", path, err)
	}
	return hex.EncodeToString(sum.Sum(nil)), nil
}

// waitGrant waits for the first grant of workspace in this agent's life.
func (v *volumes) waitGrant(ctx context.Context, workspace string) error {
	v.mu.Lock()
	ch := v.grantChannel(workspace)
	v.mu.Unlock()
	timer := time.NewTimer(grantWait)
	defer timer.Stop()
	select {
	case <-ch:
		return nil
	case <-ctx.Done():
		return fmt.Errorf("wait for storage grant: %w", ctx.Err())
	case <-timer.C:
		return fmt.Errorf("no storage grant for workspace %s arrived within %v", workspace, grantWait)
	}
}

// binds prepares the container's volume mounts and returns its bind mounts
// and the workspaces whose mounts it uses.
func (v *volumes) binds(ctx context.Context, container string, specs []*hostproto.VolumeMount) ([]mount.Mount, []string, error) {
	var binds []mount.Mount
	var workspaces []string
	if len(specs) > 0 && v.a.cfg.GeeseFSPath == "" {
		return nil, nil, errNoVolumeSupport
	}
	for n, spec := range specs {
		if !filepath.IsAbs(spec.GetMountPath()) {
			return nil, nil, fmt.Errorf("volume mount path %q is not absolute", spec.GetMountPath())
		}
		if spec.GetCloudBucket() != nil {
			bind, err := v.bucketBind(ctx, container, n, spec)
			if err != nil {
				return nil, nil, err
			}
			binds = append(binds, bind)
			continue
		}
		volume := spec.GetVolume()
		if volume == nil {
			return nil, nil, fmt.Errorf("volume at %s names no source", spec.GetMountPath())
		}
		workspace, prefix := volume.GetWorkspaceId(), volume.GetPrefix()
		if !isUUID(workspace) || !isUUID(volume.GetVolumeId()) || prefix != "volumes/"+volume.GetVolumeId()+"/" {
			return nil, nil, fmt.Errorf("volume at %s has an invalid location", spec.GetMountPath())
		}
		root, err := v.ensureMount(ctx, workspace, container)
		if err != nil {
			return nil, nil, err
		}
		source := filepath.Join(root, volume.GetVolumeId())
		if err := os.MkdirAll(source, 0o777); err != nil { //nolint:gosec // Workloads run as any user.
			return nil, nil, fmt.Errorf("create volume directory: %w", err)
		}
		binds = append(binds, mount.Mount{Type: mount.TypeBind, Source: source, Target: spec.GetMountPath(), ReadOnly: spec.GetReadOnly()})
		if !slices.Contains(workspaces, workspace) {
			workspaces = append(workspaces, workspace)
		}
	}
	return binds, workspaces, nil
}

// ensureMount returns the host path of the workspace's current mount and
// records container as its user. It starts a new generation when none is
// current, and waits for it without holding up starts of other mounts.
func (v *volumes) ensureMount(ctx context.Context, workspace, container string) (string, error) {
	if err := v.waitGrant(ctx, workspace); err != nil {
		return "", err
	}
	loc, err := v.location(workspace)
	if err != nil {
		return "", err
	}
	fingerprint, err := v.fingerprint(loc)
	if err != nil {
		return "", err
	}
	for {
		g, created := v.join(workspace, fingerprint, container)
		if created {
			v.a.goOwned(func(ctx context.Context) { v.startGeneration(ctx, g, loc) })
		}
		select {
		case <-g.ready:
		case <-ctx.Done():
			v.release(container)
			return "", fmt.Errorf("wait for the volume mount: %w", ctx.Err())
		}
		if g.err != nil {
			v.release(container)
			return "", g.err
		}
		// A generation mounted earlier may have lost its mount since.
		if created || mounted(g.dir) {
			return g.dir, nil
		}
		v.abandon(g, container)
	}
}

// join records container as a user of the workspace's generation with
// fingerprint, making one when none takes users, and reports whether it made
// it. A generation with another fingerprint retires.
func (v *volumes) join(workspace, fingerprint, container string) (*mounter, bool) {
	var stale []*mounter
	defer func() { v.stopLater(stale) }()
	v.mu.Lock()
	defer v.mu.Unlock()
	var g *mounter
	for name, m := range v.generations {
		switch {
		case m.workspace != workspace || m.retired:
		case m.fingerprint == fingerprint:
			g = m
		default:
			m.retired = true
			if m.up && len(m.users) == 0 {
				delete(v.generations, name)
				stale = append(stale, m)
			}
		}
	}
	created := g == nil
	if created {
		generation := uuid.NewString()
		g = newMounter(generationName(workspace, generation), filepath.Join(v.mountDir(), workspace, generation))
		g.workspace, g.fingerprint = workspace, fingerprint
		v.generations[g.name] = g
	}
	g.users[container] = struct{}{}
	g.idleSince = time.Time{}
	return g, created
}

// startGeneration starts g's mount container; a generation that fails to
// mount is gone and its waiters fail with why.
func (v *volumes) startGeneration(ctx context.Context, g *mounter, loc bucketLocation) {
	err := v.start(ctx, g, "the volume mount for workspace "+g.workspace, mountSpec{
		creds: v.storageDir(g.workspace), source: loc.Bucket + ":volumes/", endpoint: loc.Endpoint, region: loc.Region, pathStyle: loc.PathStyle,
		labels: map[string]string{
			labelKind: kindMount, labelWorkspace: g.workspace, labelGeneration: filepath.Base(g.dir), labelFingerprint: g.fingerprint,
		},
	})
	if err != nil {
		v.mu.Lock()
		g.err = err
		g.retired = true
		if v.generations[g.name] == g {
			delete(v.generations, g.name)
		}
		v.mu.Unlock()
	}
	close(g.ready)
}

// abandon retires g, whose mount is gone, and forgets container as its user.
func (v *volumes) abandon(g *mounter, container string) {
	var empty []*mounter
	v.mu.Lock()
	g.retired = true
	delete(g.users, container)
	if len(g.users) == 0 && v.generations[g.name] == g {
		delete(v.generations, g.name)
		empty = append(empty, g)
	}
	v.mu.Unlock()
	v.stopLater(empty)
}

// release forgets container as a user of every workspace mount. A stale
// generation stops with its last user.
func (v *volumes) release(container string) {
	var empty []*mounter
	v.mu.Lock()
	for name, g := range v.generations {
		if _, ok := g.users[container]; !ok {
			continue
		}
		delete(g.users, container)
		if len(g.users) > 0 {
			continue
		}
		g.idleSince = time.Now()
		if g.retired && g.up {
			delete(v.generations, name)
			empty = append(empty, g)
		}
	}
	v.mu.Unlock()
	v.stopLater(empty)
}

// stopLater stops mounts the caller took out of the generations.
func (v *volumes) stopLater(mounts []*mounter) {
	for _, m := range mounts {
		v.a.goOwned(func(ctx context.Context) {
			if err := v.stop(ctx, m); err != nil {
				v.a.log.Warn("stopping a volume mount failed", "mount", m.name, "error", err)
				return
			}
			v.a.log.Info("stopped a volume mount nothing uses", "mount", m.name)
		})
	}
}

// mounted reports whether path is the root of a mount: its device differs
// from its parent's.
func mounted(path string) bool {
	var self, parent syscall.Stat_t
	if syscall.Stat(path, &self) != nil || syscall.Stat(filepath.Dir(path), &parent) != nil {
		return false
	}
	return self.Dev != parent.Dev
}

// mountFailure is why a new mount did not come up.
type mountFailure string

const (
	mountExited   mountFailure = "exited"
	mountTimedOut mountFailure = "timed out"
)

// mountError is a new mount that did not come up. Its container is removed;
// output is the end of what it printed.
type mountError struct {
	mount  string
	reason mountFailure
	output string
}

func (e *mountError) Error() string {
	var what string
	switch e.reason {
	case mountExited:
		what = "exited before it mounted"
	case mountTimedOut:
		what = fmt.Sprintf("did not mount within %v", mountWait)
	}
	output := e.output
	if output == "" {
		output = "it printed nothing"
	}
	return fmt.Sprintf("%s %s: %s", e.mount, what, output)
}

// start runs m's container and waits for its mount. One that does not mount
// within mountWait is removed.
func (v *volumes) start(ctx context.Context, m *mounter, what string, spec mountSpec) error {
	id, err := v.runMount(ctx, m, spec)
	if err != nil {
		if rmErr := v.remove(context.WithoutCancel(ctx), m); rmErr != nil {
			v.a.log.Warn("removing a volume mount that did not start failed", "mount", m.name, "error", rmErr)
		}
		return err
	}
	v.a.goOwned(func(ctx context.Context) { v.watch(ctx, m, id) })
	timer := time.NewTimer(mountWait)
	defer timer.Stop()
	poll := time.NewTicker(100 * time.Millisecond)
	defer poll.Stop()
	var reason mountFailure
	for reason == "" {
		if mounted(m.dir) {
			v.mu.Lock()
			m.up = true
			v.mu.Unlock()
			return nil
		}
		select {
		case <-poll.C:
		case <-m.exited:
			reason = mountExited
		case <-timer.C:
			reason = mountTimedOut
		case <-ctx.Done():
			return fmt.Errorf("wait for %s: %w", what, ctx.Err())
		}
	}
	cleanup := context.WithoutCancel(ctx)
	failure := &mountError{mount: what, reason: reason, output: v.a.containerOutput(cleanup, m.name)}
	if err := v.stop(cleanup, m); err != nil {
		v.a.log.Warn("removing a volume mount that did not mount failed", "mount", m.name, "error", err)
	}
	return failure
}

// mountSpec is what one GeeseFS mount container mounts.
type mountSpec struct {
	// creds holds its credential_process files.
	creds string
	// source is bucket:prefix.
	source, endpoint, region string
	pathStyle, readOnly      bool
	labels                   map[string]string
}

// HostTrustBundle is the CA bundle the host's own TLS uses, found as Go
// finds it on Linux: SSL_CERT_FILE, then the distributions' usual paths.
// Empty means the host has none.
func HostTrustBundle() string {
	for _, path := range []string{
		os.Getenv("SSL_CERT_FILE"),
		"/etc/ssl/certs/ca-certificates.crt",
		"/etc/pki/tls/certs/ca-bundle.crt",
		"/etc/ssl/ca-bundle.pem",
		"/etc/pki/tls/cacert.pem",
		"/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
		"/etc/ssl/cert.pem",
	} {
		if path == "" {
			continue
		}
		if info, err := os.Stat(path); err == nil && info.Mode().IsRegular() { //nolint:gosec // The host's own setting, as Go's TLS reads it.
			return path
		}
	}
	return ""
}

// runMount starts m's GeeseFS mount container and returns its id.
func (v *volumes) runMount(ctx context.Context, m *mounter, spec mountSpec) (string, error) {
	image, err := v.a.platformImage(ctx, platformimages.Mount)
	if err != nil {
		return "", err
	}
	if err := v.a.removeContainer(ctx, m.name); err != nil {
		return "", err
	}
	// Only the agent (and root, which Docker runs as) may walk into the
	// mounts; workloads reach their own volume through a bind.
	if err := os.MkdirAll(m.dir, 0o700); err != nil {
		return "", fmt.Errorf("create mount directory: %w", err)
	}
	if err := os.Chmod(v.mountDir(), 0o700); err != nil { //nolint:gosec // A directory needs its search bit.
		return "", fmt.Errorf("restrict mount directory: %w", err)
	}
	rel, err := filepath.Rel(v.mountDir(), m.dir)
	if err != nil {
		return "", fmt.Errorf("mount directory: %w", err)
	}
	uid, gid := "0", "0"
	if os.Geteuid() != 0 {
		uid, gid = strconv.Itoa(os.Geteuid()), strconv.Itoa(os.Getegid())
	}
	target := "/mnt/" + filepath.ToSlash(rel)
	args := []string{
		"-f", "-o", "allow_other", "--uid", uid, "--gid", gid, "--dir-mode", "0777", "--file-mode", "0666",
		"--fsync-on-close", "--stat-cache-ttl", "1s", "--memory-limit", strconv.Itoa(geesefsMemoryMiB), "--no-preload-dir",
	}
	args = append(args, "--endpoint", spec.endpoint)
	if spec.region != "" {
		args = append(args, "--region", spec.region)
	}
	if !spec.pathStyle {
		args = append(args, "--subdomain")
	}
	if spec.readOnly {
		args = append(args, "-o", "ro")
	}
	args = append(args, spec.source, target)
	quoted := make([]string, len(args))
	for n, arg := range args {
		quoted[n] = "'" + strings.ReplaceAll(arg, "'", `'\''`) + "'"
	}
	// GeeseFS cannot unmount itself here and leaves a dead mount when it
	// exits. On stop the script lets it flush, then detaches the mount so
	// it exits; after any exit it unmounts what remains.
	script := fmt.Sprintf(`/opt/lazycloud/bin/geesefs %[2]s &
pid=$!
stop() { kill -TERM $pid 2>/dev/null; sleep 1; umount -l %[1]s; }
trap stop TERM INT
wait $pid
while kill -0 $pid 2>/dev/null; do wait $pid; done
umount -l %[1]s 2>/dev/null
exit 1`, target, strings.Join(quoted, " "))
	labels := maps.Clone(v.a.cfg.Labels)
	if labels == nil {
		labels = map[string]string{}
	}
	maps.Copy(labels, spec.labels)
	labels[labelHost] = v.a.identity.HostID
	pids := int64(mounterPidsLimit)
	created, err := v.a.docker.ContainerCreate(ctx, client.ContainerCreateOptions{
		Name: m.name,
		Config: &containertypes.Config{
			Image:      image,
			Entrypoint: []string{"/bin/sh", "-c", script},
			Env:        []string{"AWS_SDK_LOAD_CONFIG=1", "AWS_CONFIG_FILE=/creds/config"},
			Labels:     labels,
		},
		HostConfig: &containertypes.HostConfig{
			NetworkMode: "host",
			CapAdd:      []string{"SYS_ADMIN"},
			SecurityOpt: []string{"apparmor=unconfined"},
			Resources: containertypes.Resources{
				Devices:   []containertypes.DeviceMapping{{PathOnHost: "/dev/fuse", PathInContainer: "/dev/fuse", CgroupPermissions: "rwm"}},
				Memory:    mounterMemoryBytes,
				PidsLimit: &pids,
			},
			Mounts: []mount.Mount{
				{Type: mount.TypeBind, Source: v.a.cfg.GeeseFSPath, Target: "/opt/lazycloud/bin/geesefs", ReadOnly: true},
				{Type: mount.TypeBind, Source: v.a.cfg.TrustBundle, Target: "/etc/ssl/certs/ca-certificates.crt", ReadOnly: true},
				{Type: mount.TypeBind, Source: spec.creds, Target: "/creds", ReadOnly: true},
				{
					Type: mount.TypeBind, Source: v.mountDir(), Target: "/mnt",
					BindOptions: &mount.BindOptions{Propagation: mount.PropagationRShared},
				},
			},
		},
	})
	if err != nil {
		return "", fmt.Errorf("create volume mount container: %w", err)
	}
	if _, err := v.a.docker.ContainerStart(ctx, m.name, client.ContainerStartOptions{}); err != nil {
		return "", fmt.Errorf("start volume mount container: %w", err)
	}
	return created.ID, nil
}

// watch waits for m's container, id, to stop. Unless the agent stopped it or
// it never mounted, the mount under every user is dead, so their containers
// stop and report the exit rather than run on with a broken volume.
func (v *volumes) watch(ctx context.Context, m *mounter, id string) {
	wait := v.a.docker.ContainerWait(ctx, id, client.ContainerWaitOptions{Condition: containertypes.WaitConditionNotRunning})
	select {
	case <-ctx.Done():
		return
	case <-wait.Result:
	case err := <-wait.Error:
		if !cerrdefs.IsNotFound(err) {
			if ctx.Err() == nil {
				v.a.log.Warn("watching a volume mount failed", "mount", m.name, "error", err)
			}
			return
		}
	}
	close(m.exited)
	v.mu.Lock()
	lost := m.up && !m.stopping
	m.stopping = true
	if v.generations[m.name] == m {
		delete(v.generations, m.name)
	}
	users := slices.Collect(maps.Keys(m.users))
	v.mu.Unlock()
	if !lost {
		return
	}
	output := v.a.containerOutput(ctx, m.name)
	v.a.log.Error("volume mount exited; stopping its containers", "mount", m.name, "containers", len(users), "output", output)
	for _, user := range users {
		if c := v.a.lookup(user); c != nil {
			c.failVolume(ctx, "the volume mount "+m.name+" exited: "+output)
		}
	}
	if err := v.remove(ctx, m); err != nil {
		v.a.log.Warn("removing an exited volume mount failed", "mount", m.name, "error", err)
	}
}

// stop stops m's container, whose script unmounts on SIGTERM, then removes
// it and its directory.
func (v *volumes) stop(ctx context.Context, m *mounter) error {
	v.mu.Lock()
	m.stopping = true
	v.mu.Unlock()
	if err := v.a.stopDocker(ctx, m.name, stopKillSeconds); err != nil {
		return err
	}
	return v.remove(ctx, m)
}

// remove deletes m's container and then its directory, which only goes when
// nothing is mounted on it and it is empty; a workspace's directory goes with
// its last generation.
func (v *volumes) remove(ctx context.Context, m *mounter) error {
	if err := v.a.removeContainer(ctx, m.name); err != nil {
		return err
	}
	if m.dir == "" {
		return nil
	}
	if err := os.Remove(m.dir); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove mount directory: %w", err)
	}
	if parent := filepath.Dir(m.dir); parent != v.mountDir() {
		if err := os.Remove(parent); err != nil && !errors.Is(err, os.ErrNotExist) && !errors.Is(err, syscall.ENOTEMPTY) {
			return fmt.Errorf("remove workspace mount directory: %w", err)
		}
	}
	return nil
}

// adopt takes over the mount containers a previous agent left and the grants
// it stored; the agent has tracked the workload containers. A mount whose
// container stopped or never mounted, a stale generation nothing uses and a
// cloud bucket mount whose container is gone are removed, with the keys of
// every cloud bucket not mounted. Running workloads bound into a removed
// mount fail.
func (v *volumes) adopt(ctx context.Context, summaries []containertypes.Summary) error {
	workloads := map[string]containertypes.Summary{}
	for _, s := range summaries {
		if s.Labels[labelKind] == "" && s.State == containertypes.StateRunning && isUUID(s.Labels[labelContainer]) {
			workloads[s.Labels[labelContainer]] = s
		}
	}
	live, ids := map[string]*mounter{}, map[string]string{}
	var dead []*mounter
	for _, s := range summaries {
		kind := s.Labels[labelKind]
		if (kind != kindMount && kind != kindBucket) || len(s.Names) == 0 {
			continue
		}
		name := strings.TrimPrefix(s.Names[0], "/")
		var m *mounter
		if kind == kindMount {
			workspace, generation := s.Labels[labelWorkspace], s.Labels[labelGeneration]
			m = newMounter(name, "")
			if isUUID(workspace) && isUUID(generation) && name == generationName(workspace, generation) {
				m.dir = filepath.Join(v.mountDir(), workspace, generation)
				m.workspace, m.fingerprint = workspace, s.Labels[labelFingerprint]
			}
		} else {
			m = newMounter(name, filepath.Join(v.mountDir(), name))
			if container := s.Labels[labelContainer]; workloads[container].ID != "" {
				m.users[container] = struct{}{}
			}
		}
		if s.State == containertypes.StateRunning && m.dir != "" && mounted(m.dir) && (kind == kindMount || len(m.users) > 0) {
			m.up = true
			close(m.ready)
			live[name], ids[name] = m, s.ID
		} else {
			dead = append(dead, m)
		}
	}
	var lost []string
	for id, s := range workloads {
		for _, point := range s.Mounts {
			name, ok := v.mountOf(point.Source)
			if !ok || point.Type != mount.TypeBind {
				continue
			}
			if m := live[name]; m != nil {
				m.users[id] = struct{}{}
				continue
			}
			lost = append(lost, id)
			break
		}
	}
	// One current generation per workspace takes new users.
	current := map[string]bool{}
	for name, m := range live {
		if m.workspace == "" {
			continue
		}
		if !current[m.workspace] && v.isCurrent(m) {
			current[m.workspace] = true
			continue
		}
		m.retired = true
		if len(m.users) == 0 {
			delete(live, name)
			dead = append(dead, m)
		}
	}
	v.mu.Lock()
	for name, m := range live {
		if m.workspace != "" {
			v.generations[name] = m
			continue
		}
		for container := range m.users {
			v.buckets[container] = append(v.buckets[container], m)
		}
	}
	v.mu.Unlock()
	for name, m := range live {
		id := ids[name]
		v.a.goOwned(func(ctx context.Context) { v.watch(ctx, m, id) })
	}
	for _, id := range lost {
		if c := v.a.lookup(id); c != nil {
			c.failVolume(ctx, "a volume mount of this container stopped while the agent was away")
		}
	}
	for _, m := range dead {
		if err := v.stop(ctx, m); err != nil {
			return fmt.Errorf("remove volume mount %s: %w", m.name, err)
		}
	}
	keys, err := os.ReadDir(v.bucketKeys(""))
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("list cloud bucket keys: %w", err)
	}
	for _, entry := range keys {
		if live[entry.Name()] == nil {
			if err := os.RemoveAll(v.bucketKeys(entry.Name())); err != nil {
				return fmt.Errorf("remove cloud bucket keys: %w", err)
			}
		}
	}
	v.adoptGrants()
	return nil
}

// mountOf names the mount container behind a bind source under the mount
// directory.
func (v *volumes) mountOf(source string) (string, bool) {
	rel, err := filepath.Rel(v.mountDir(), source)
	if err != nil || rel == "." || strings.HasPrefix(rel, "..") {
		return "", false
	}
	parts := strings.Split(rel, string(filepath.Separator))
	if !isUUID(parts[0]) {
		return parts[0], true
	}
	if len(parts) < 2 {
		return "", true
	}
	return generationName(parts[0], parts[1]), true
}

// isCurrent reports whether generation m matches what its workspace would
// mount with now.
func (v *volumes) isCurrent(m *mounter) bool {
	loc, err := v.location(m.workspace)
	if err != nil {
		return false
	}
	fingerprint, err := v.fingerprint(loc)
	return err == nil && fingerprint == m.fingerprint
}

// adoptGrants counts each stored grant as received while its key is valid;
// otherwise starts wait for the fresh one the server sends.
func (v *volumes) adoptGrants() {
	entries, err := os.ReadDir(filepath.Join(v.a.cfg.StateDir, "storage"))
	if err != nil {
		return
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	for _, entry := range entries {
		c, err := v.credentials(entry.Name())
		if _, statErr := os.Stat(filepath.Join(v.storageDir(entry.Name()), "location.json")); err == nil && statErr == nil && time.Until(c.expires) > grantMargin {
			ch := v.grantChannel(entry.Name())
			select {
			case <-ch:
			default:
				close(ch)
			}
		}
	}
}

// pruneIdle stops stale generations nothing uses and current ones nothing
// has used for mountIdle.
func (v *volumes) pruneIdle(ctx context.Context) {
	now := time.Now()
	var idle []*mounter
	v.mu.Lock()
	for name, g := range v.generations {
		if len(g.users) > 0 || !g.up {
			continue
		}
		if g.idleSince.IsZero() {
			g.idleSince = now
		}
		if g.retired || now.Sub(g.idleSince) >= mountIdle {
			g.retired = true
			delete(v.generations, name)
			idle = append(idle, g)
		}
	}
	v.mu.Unlock()
	for _, g := range idle {
		if err := v.stop(ctx, g); err != nil {
			v.a.log.Warn("stopping an idle volume mount failed", "mount", g.name, "error", err)
		}
	}
}

// storedCredentials is a workspace's current key with its expiry parsed.
type storedCredentials struct {
	processCredentials
	expires time.Time
}

// credentials reads the workspace's current key.
func (v *volumes) credentials(workspace string) (storedCredentials, error) {
	var c storedCredentials
	data, err := os.ReadFile(filepath.Join(v.storageDir(workspace), "credentials.json"))
	if err != nil {
		return c, fmt.Errorf("read storage credentials: %w", err)
	}
	if err := json.Unmarshal(data, &c.processCredentials); err != nil {
		return c, fmt.Errorf("decode storage credentials: %w", err)
	}
	if c.expires, err = time.Parse(time.RFC3339, c.Expiration); err != nil {
		return c, fmt.Errorf("storage credential expiry: %w", err)
	}
	return c, nil
}

var errNoVolumeSupport = errors.New("this agent has no GeeseFS binary for volume mounts")

func isUUID(s string) bool {
	_, err := uuid.Parse(s)
	return err == nil
}
