package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/google/uuid"
	containertypes "github.com/moby/moby/api/types/container"
	"github.com/moby/moby/api/types/mount"
	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/platformimages"
)

// A container's platform volumes share one mount container, which mounts the
// volumes/ prefix of its workspace bucket; each cloud bucket it names gets
// one more. GeeseFS holds the credentials there, and the workload binds each
// volume's own directory of the mount, never the credentials or other
// volumes.
// A mount container shares its mount with the host through a
// shared-propagation bind of the mount directory, so it works for root and
// unprivileged agents alike and outlives agent restarts, like its workload.
// Mounts start before their container, stop after it and run in its slice
// (slices.go), inside the memory the server reserved for them.
const (
	labelKind = "lazycloud.kind"
	kindMount = "volume-mount"
	// grantWait bounds how long a start waits for a workspace's first grant.
	grantWait = time.Minute
	// mountWait bounds how long a new mount takes to appear once its
	// container runs.
	mountWait = 30 * time.Second
	// GeeseFS reserves its buffers before reading and stays under
	// geesefsMemoryMiB (--use-enomem), and its runtime collects garbage
	// before geesefsGoMemoryMiB. Without the reservation, readahead in
	// flight grows with the readers: 256 parallel 32 MiB reads took 2.4 GiB.
	// With it they peak near 410 MiB, inside hostproto.MounterMemoryBytes.
	geesefsMemoryMiB   = 192
	geesefsGoMemoryMiB = 320
	mounterPidsLimit   = 1024
	// grantMargin is how long a stored key must still be valid to mount.
	grantMargin = time.Minute
)

// volumes owns the host's mount containers and their credentials.
type volumes struct {
	a *Agent

	mu sync.Mutex
	// granted is closed once a workspace's first grant is written.
	granted map[string]chan struct{}
	// mounts are the mount containers of each container with a slice.
	mounts map[string][]*mounter
}

// mounter is one mount container.
type mounter struct {
	// name is the Docker name; dir the host path of the mount.
	name, dir string
	// container is the workload the mount serves.
	container string
	// exited closes when the mount container stops running.
	exited chan struct{}

	// The fields below are guarded by volumes.mu.
	// up: the mount appeared.
	up bool
	// stopping: the container stops on purpose, so its exit fails no one.
	stopping bool
}

func newVolumes(a *Agent) *volumes {
	return &volumes{a: a, granted: map[string]chan struct{}{}, mounts: map[string][]*mounter{}}
}

func (v *volumes) storageDir(workspace string) string {
	return filepath.Join(v.a.cfg.StateDir, "storage", workspace)
}

func (v *volumes) mountDir() string { return filepath.Join(v.a.cfg.StateDir, "mounts") }

const mountPrefix = "lazycloud-mount-"

// newMounter is the mount container name, serving container. Each mounts in
// its own directory directly in the mount directory, so no mount lies inside
// another's.
func (v *volumes) newMounter(name, container string) *mounter {
	return &mounter{
		name: name, dir: filepath.Join(v.mountDir(), strings.TrimPrefix(name, mountPrefix)),
		container: container, exited: make(chan struct{}),
	}
}

func mounterName(container string, n int) string {
	return mountPrefix + container + "-" + strconv.Itoa(n)
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

// writeCredentials writes the credential_process files a mount reads its
// keys from into dir.
func writeCredentials(dir string, creds processCredentials) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("create credential directory: %w", err)
	}
	data, err := json.Marshal(creds) //nolint:gosec // The 0600 file GeeseFS reads its key from.
	if err != nil {
		return fmt.Errorf("encode credentials: %w", err)
	}
	if err := writeFileAtomic(filepath.Join(dir, "credentials.json"), data, 0o600); err != nil {
		return fmt.Errorf("write credentials: %w", err)
	}
	config := "[default]\ncredential_process = cat /creds/credentials.json\n"
	if err := writeFileAtomic(filepath.Join(dir, "config"), []byte(config), 0o644); err != nil { //nolint:gosec // Holds no secret.
		return fmt.Errorf("write credential config: %w", err)
	}
	return nil
}

// grant stores a workspace's credentials where its mounts read them.
func (v *volumes) grant(g *hostproto.StorageGrant) error {
	workspace := g.GetWorkspaceId()
	if !isUUID(workspace) {
		return fmt.Errorf("storage grant for %q: not a workspace id", workspace)
	}
	if g.GetEndpoint() == "" || g.GetBucket() == "" {
		return fmt.Errorf("storage grant for %s names no endpoint or bucket", workspace)
	}
	dir := v.storageDir(workspace)
	if err := writeCredentials(dir, processCredentials{
		Version: 1, AccessKeyID: g.GetAccessKeyId(), SecretAccessKey: g.GetSecretAccessKey(),
		SessionToken: g.GetSessionToken(), Expiration: g.GetExpiresAt().AsTime().UTC().Format(time.RFC3339),
	}); err != nil {
		return err
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

// mount starts the container's slice and its mounters, and returns the
// binds of its mounts. What it started goes in release, also after a
// failure.
func (v *volumes) mount(ctx context.Context, c *container, spec *hostproto.StartContainer) ([]mount.Mount, error) {
	specs := spec.GetVolumes()
	if len(specs) == 0 {
		return nil, nil
	}
	if v.a.cfg.GeeseFSPath == "" {
		return nil, errNoVolumeSupport
	}
	for _, s := range specs {
		if !filepath.IsAbs(s.GetMountPath()) {
			return nil, fmt.Errorf("volume mount path %q is not absolute", s.GetMountPath())
		}
	}
	v.mu.Lock()
	v.mounts[c.id] = nil
	v.mu.Unlock()
	budget := containerResources(spec.GetResources(), v.a.capacity, v.a.topology, pidsLimit, nil)
	budget.Memory += spec.GetResources().GetMountReserveBytes()
	if err := startSlice(ctx, v.a.workloadSlice(c.id), budget); err != nil {
		return nil, err
	}
	binds := make([]mount.Mount, 0, len(specs))
	for n, group := range mounterGroups(specs) {
		m := v.newMounter(mounterName(c.id, n), c.id)
		var ms mountSpec
		var dirs []string
		var what string
		var err error
		if b := group[0].GetCloudBucket(); b != nil {
			what = "the mount of cloud bucket " + b.GetBucket()
			ms, dirs, err = v.bucketSpec(m.name, group)
		} else {
			what = "the mount of the workspace's volumes"
			ms, dirs, err = v.volumeSpec(ctx, group)
		}
		if err != nil {
			return nil, err
		}
		v.mu.Lock()
		v.mounts[c.id] = append(v.mounts[c.id], m)
		v.mu.Unlock()
		if err := v.start(ctx, m, what, ms); err != nil {
			return nil, err
		}
		for i, s := range group {
			// A cloud bucket's prefixes come from the user; a directory
			// below them must stay inside the mount.
			if dirs[i] != "" && !filepath.IsLocal(dirs[i]) {
				return nil, fmt.Errorf("the volume at %s names a prefix outside its bucket's mount", s.GetMountPath())
			}
			source := filepath.Join(m.dir, dirs[i])
			err := os.MkdirAll(source, 0o755) //nolint:gosec // GeeseFS gives every directory --dir-mode.
			if errors.Is(err, syscall.EROFS) {
				// A read-only mount cannot make the directory of a prefix
				// that holds nothing, so the volume shows an empty one.
				source = filepath.Join(v.a.cfg.StateDir, "empty")
				err = os.MkdirAll(source, 0o555) //nolint:gosec // Workloads of any user list it.
			}
			if err != nil {
				return nil, fmt.Errorf("create the directory of the volume at %s: %w", s.GetMountPath(), err)
			}
			binds = append(binds, mount.Mount{Type: mount.TypeBind, Source: source, Target: s.GetMountPath(), ReadOnly: s.GetReadOnly()})
		}
	}
	return binds, nil
}

// mounterGroups splits a container's mounts among its mounters: one for all
// its platform volumes and one per distinct cloud bucket, in the order the
// start names them.
func mounterGroups(specs []*hostproto.VolumeMount) [][]*hostproto.VolumeMount {
	type key struct {
		cloud                                          bool
		bucket, region, endpoint, accessKey, secretKey string
		pathStyle                                      bool
	}
	var groups [][]*hostproto.VolumeMount
	index := map[key]int{}
	for _, s := range specs {
		var k key
		if b := s.GetCloudBucket(); b != nil {
			k = key{true, b.GetBucket(), b.GetRegion(), b.GetEndpoint(), b.GetAccessKeyId(), b.GetSecretAccessKey(), b.GetForcePathStyle()}
		}
		n, ok := index[k]
		if !ok {
			n = len(groups)
			index[k] = n
			groups = append(groups, nil)
		}
		groups[n] = append(groups[n], s)
	}
	return groups
}

// volumeSpec mounts the volumes/ prefix of the workspace bucket with the
// workspace's grant, and returns each volume's directory in it.
func (v *volumes) volumeSpec(ctx context.Context, group []*hostproto.VolumeMount) (mountSpec, []string, error) {
	workspace := group[0].GetVolume().GetWorkspaceId()
	dirs := make([]string, len(group))
	for i, s := range group {
		volume := s.GetVolume()
		if volume == nil {
			return mountSpec{}, nil, fmt.Errorf("volume at %s names no source", s.GetMountPath())
		}
		id := volume.GetVolumeId()
		if volume.GetWorkspaceId() != workspace || !isUUID(workspace) || !isUUID(id) || volume.GetPrefix() != "volumes/"+id+"/" {
			return mountSpec{}, nil, fmt.Errorf("volume at %s has an invalid location", s.GetMountPath())
		}
		dirs[i] = id
	}
	if err := v.waitGrant(ctx, workspace); err != nil {
		return mountSpec{}, nil, err
	}
	loc, err := v.location(workspace)
	if err != nil {
		return mountSpec{}, nil, err
	}
	return mountSpec{
		creds: v.storageDir(workspace), source: loc.Bucket + ":volumes/", endpoint: loc.Endpoint, region: loc.Region,
		pathStyle: loc.PathStyle,
	}, dirs, nil
}

// release stops the container's mounts, deletes their keys and stops its
// slice, after the container is gone.
func (v *volumes) release(ctx context.Context, container string) {
	v.mu.Lock()
	mounts, ok := v.mounts[container]
	delete(v.mounts, container)
	v.mu.Unlock()
	if !ok {
		return
	}
	for _, m := range mounts {
		if err := v.stop(ctx, m); err != nil {
			v.a.log.Warn("stopping a volume mount failed", "mount", m.name, "error", err)
		}
		if err := os.RemoveAll(v.bucketKeys(m.name)); err != nil {
			v.a.log.Warn("removing cloud bucket keys failed", "mount", m.name, "error", err)
		}
	}
	if err := stopSlices(ctx, v.a.workloadSlice(container)); err != nil {
		v.a.log.Warn("stopping a container's slice failed", "container", container, "error", err)
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
		return err
	}
	v.a.goOwned(func(ctx context.Context) { v.watch(ctx, m, id) })
	timer := time.NewTimer(mountWait)
	defer timer.Stop()
	poll := time.NewTicker(100 * time.Millisecond)
	defer poll.Stop()
	var reason mountFailure
	for reason == "" {
		if mounted(m.dir) && v.markUp(m) {
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

// runMount starts m's GeeseFS mount container in its container's slice and
// returns its id.
func (v *volumes) runMount(ctx context.Context, m *mounter, spec mountSpec) (string, error) {
	image, err := v.a.platformImage(ctx, platformimages.Mount)
	if err != nil {
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
	uid, gid := "0", "0"
	if os.Geteuid() != 0 {
		uid, gid = strconv.Itoa(os.Geteuid()), strconv.Itoa(os.Getegid())
	}
	target := "/mnt/" + filepath.Base(m.dir)
	args := []string{
		"-f", "-o", "allow_other", "--uid", uid, "--gid", gid, "--dir-mode", "0777", "--file-mode", "0666",
		"--fsync-on-close", "--stat-cache-ttl", "1s", "--no-preload-dir",
		"--memory-limit", strconv.Itoa(geesefsMemoryMiB), "--use-enomem",
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
	labels[labelKind] = kindMount
	labels[labelContainer] = m.container
	labels[labelHost] = v.a.identity.HostID
	pids := int64(mounterPidsLimit)
	id, err := v.a.createContainer(ctx, m.name, client.ContainerCreateOptions{
		Name: m.name,
		Config: &containertypes.Config{
			Image:      image,
			Entrypoint: []string{"/bin/sh", "-c", script},
			Env: []string{
				"AWS_SDK_LOAD_CONFIG=1", "AWS_CONFIG_FILE=/creds/config",
				"GOMEMLIMIT=" + strconv.Itoa(geesefsGoMemoryMiB) + "MiB",
			},
			Labels: labels,
		},
		HostConfig: &containertypes.HostConfig{
			NetworkMode: "host",
			CapAdd:      []string{"SYS_ADMIN"},
			SecurityOpt: []string{"apparmor=unconfined"},
			Resources: containertypes.Resources{
				CgroupParent: v.a.workloadSlice(m.container),
				Devices:      []containertypes.DeviceMapping{{PathOnHost: "/dev/fuse", PathInContainer: "/dev/fuse", CgroupPermissions: "rwm"}},
				// A swapping GeeseFS stalls every reader of the mount.
				Memory:     hostproto.MounterMemoryBytes,
				MemorySwap: hostproto.MounterMemoryBytes,
				PidsLimit:  &pids,
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
	if err := v.a.startDocker(ctx, m.name, client.ContainerStartOptions{}); err != nil {
		return "", fmt.Errorf("start volume mount container: %w", err)
	}
	return id, nil
}

// watch waits for m's container, id, to stop. Unless the agent stopped it or
// it never mounted, the mount under its workload is dead, so the workload
// stops and reports the exit rather than run on with a broken volume.
func (v *volumes) watch(ctx context.Context, m *mounter, id string) {
	for {
		exited, err := v.a.waitExit(ctx, id)
		if exited {
			break
		}
		if ctx.Err() != nil {
			return
		}
		v.a.log.Warn("watching a volume mount failed", "mount", m.name, "error", err)
		if !sleep(ctx, time.Second) {
			return
		}
	}
	if !v.markExited(m) {
		return
	}
	output := v.a.containerOutput(ctx, m.name)
	v.a.log.Error("volume mount exited; stopping its container", "mount", m.name, "container", m.container, "output", output)
	if c := v.a.lookup(m.container); c != nil {
		c.failVolume(ctx, "the volume mount "+m.name+" exited: "+output)
	}
}

// markUp records that m mounted, unless its container exited first.
func (v *volumes) markUp(m *mounter) bool {
	v.mu.Lock()
	defer v.mu.Unlock()
	select {
	case <-m.exited:
		return false
	default:
		m.up = true
		return true
	}
}

// markExited records that m's container exited and reports whether that lost
// a mount in use: one that came up and was not stopped on purpose.
func (v *volumes) markExited(m *mounter) bool {
	v.mu.Lock()
	defer v.mu.Unlock()
	close(m.exited)
	return m.up && !m.stopping
}

// stop stops m's container, whose script unmounts on SIGTERM, then removes
// it and its directory, which only goes when nothing is mounted on it and it
// is empty. No later mount uses the name, so a directory left behind is only
// logged.
func (v *volumes) stop(ctx context.Context, m *mounter) error {
	v.mu.Lock()
	m.stopping = true
	v.mu.Unlock()
	if err := v.a.stopDocker(ctx, m.name, stopKillSeconds); err != nil {
		return err
	}
	if err := v.a.removeContainer(ctx, m.name); err != nil {
		return err
	}
	if err := os.Remove(m.dir); err != nil && !errors.Is(err, os.ErrNotExist) {
		v.a.log.Warn("removing a mount directory failed", "mount", m.name, "error", err)
	}
	return nil
}

// adopt takes over the mounts and slices a previous agent left and the
// grants it stored; the agent has tracked the workload containers. A mount
// whose container runs on and that still mounts is kept; any other is
// removed, with the keys of every cloud bucket not mounted and the slices of
// containers the agent does not track. A running container bound into a
// mount that is gone fails.
func (v *volumes) adopt(ctx context.Context, summaries []containertypes.Summary) error {
	live, ids := map[string]*mounter{}, map[*mounter]string{}
	var dead []*mounter
	for _, s := range summaries {
		if s.Labels[labelKind] != kindMount || len(s.Names) == 0 {
			continue
		}
		m := v.newMounter(strings.TrimPrefix(s.Names[0], "/"), s.Labels[labelContainer])
		if c := v.a.lookup(m.container); c != nil && !c.hasExited() && s.State == containertypes.StateRunning && mounted(m.dir) {
			m.up = true
			live[m.dir], ids[m] = m, s.ID
		} else {
			dead = append(dead, m)
		}
	}
	var lost []string
	for _, s := range summaries {
		id := s.Labels[labelContainer]
		if s.Labels[labelKind] != "" || s.State != containertypes.StateRunning || v.a.lookup(id) == nil {
			continue
		}
		for _, point := range s.Mounts {
			rel, err := filepath.Rel(v.mountDir(), point.Source)
			if point.Type != mount.TypeBind || err != nil || rel == "." || strings.HasPrefix(rel, "..") {
				continue
			}
			if dir, _, _ := strings.Cut(rel, string(filepath.Separator)); live[filepath.Join(v.mountDir(), dir)] == nil {
				lost = append(lost, id)
				break
			}
		}
	}
	sliced := map[string]string{}
	if v.a.cfg.GeeseFSPath != "" {
		var err error
		if sliced, err = v.a.hostSlices(ctx); err != nil {
			return err
		}
	}
	var orphans []string
	v.mu.Lock()
	for container, slice := range sliced {
		if v.a.lookup(container) == nil {
			orphans = append(orphans, slice)
		} else {
			v.mounts[container] = nil
		}
	}
	for _, m := range live {
		v.mounts[m.container] = append(v.mounts[m.container], m)
	}
	v.mu.Unlock()
	for _, m := range live {
		id := ids[m]
		v.a.goOwned(func(ctx context.Context) { v.watch(ctx, m, id) })
	}
	for _, id := range lost {
		if c := v.a.lookup(id); c != nil {
			c.failVolume(ctx, "a volume mount of this container stopped while the agent was away")
		}
	}
	for _, m := range dead {
		if err := v.stop(ctx, m); err != nil {
			v.a.log.Warn("removing a volume mount the agent did not adopt failed", "mount", m.name, "error", err)
		}
	}
	if err := stopSlices(ctx, orphans...); err != nil {
		return err
	}
	keys, err := os.ReadDir(v.bucketKeys(""))
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("list cloud bucket keys: %w", err)
	}
	for _, entry := range keys {
		if live[v.newMounter(entry.Name(), "").dir] == nil {
			if err := os.RemoveAll(v.bucketKeys(entry.Name())); err != nil {
				return fmt.Errorf("remove cloud bucket keys: %w", err)
			}
		}
	}
	v.adoptGrants()
	return nil
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
