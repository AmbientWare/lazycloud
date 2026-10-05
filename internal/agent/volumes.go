package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
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
)

// Volumes mount per workspace: one GeeseFS process per workspace bucket runs
// in a mount container that holds the workspace's credentials, and workload
// containers bind-mount their volumes' directories from it. Workloads never
// see the credentials. The mount container shares its mount with the host
// through a shared-propagation bind of the mount directory, so it works for
// root and unprivileged agents alike and outlives agent restarts, like the
// workload containers that use it.
const (
	labelKind       = "lazycloud.kind"
	labelWorkspace  = "lazycloud.workspace-id"
	labelWorkspaces = "lazycloud.workspaces"
	kindMount       = "volume-mount"
	kindBucket      = "bucket-mount"
	// grantWait bounds how long a start waits for a workspace's first grant.
	grantWait = time.Minute
	// mountWait bounds how long a new mount takes to appear.
	mountWait = 30 * time.Second
	// mountIdle is how long a mount nothing uses stays up.
	mountIdle = 5 * time.Minute
	// geesefsMemoryMiB bounds the cache of one workspace mount, and
	// mounterMemoryBytes the whole mount container.
	geesefsMemoryMiB   = 512
	mounterMemoryBytes = 1 << 30
	mounterPidsLimit   = 256
	// grantMargin is how long a stored key must still be valid to mount.
	grantMargin = time.Minute
)

// DefaultMountImage runs volume mount containers; GeeseFS needs only sh,
// cat, mkdir and umount beside it.
const DefaultMountImage = "docker.io/library/busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"

// volumes owns the host's workspace mounts and their credentials.
type volumes struct {
	a *Agent

	mu sync.Mutex
	// granted is closed once a workspace's first grant is written.
	granted map[string]chan struct{}
	// users are the containers using each workspace mount.
	users map[string]map[string]struct{}
	// idleSince is when a workspace mount lost its last user.
	idleSince map[string]time.Time
	// starting serializes starting and stopping each workspace's mount.
	starting map[string]*sync.Mutex
	// stopping marks mounts the agent itself is stopping, by name.
	stopping map[string]bool
	// buckets are each container's cloud bucket mounts, by name.
	buckets map[string][]string
}

func newVolumes(a *Agent) *volumes {
	return &volumes{
		a: a, granted: map[string]chan struct{}{}, users: map[string]map[string]struct{}{},
		idleSince: map[string]time.Time{}, starting: map[string]*sync.Mutex{}, stopping: map[string]bool{}, buckets: map[string][]string{},
	}
}

func (v *volumes) storageDir(workspace string) string {
	return filepath.Join(v.a.cfg.StateDir, "storage", workspace)
}

func (v *volumes) mountDir() string { return filepath.Join(v.a.cfg.StateDir, "mounts") }

func mounterName(workspace string) string { return "lazycloud-mount-" + workspace }

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
	location, err := json.Marshal(bucketLocation{Endpoint: g.GetEndpoint(), Region: g.GetRegion(), Bucket: g.GetBucket()})
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
	Endpoint string `json:"endpoint"`
	Region   string `json:"region"`
	Bucket   string `json:"bucket"`
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

// ensureMount returns the host path of the workspace's mount, starting its
// mount container if none runs, and records container as a user.
func (v *volumes) ensureMount(ctx context.Context, workspace, container string) (string, error) {
	if err := v.waitGrant(ctx, workspace); err != nil {
		return "", err
	}
	v.mu.Lock()
	lock := v.lockFor(workspace)
	v.addUser(workspace, container)
	v.mu.Unlock()
	lock.Lock()
	defer lock.Unlock()

	// A running mount container is never replaced, since live workloads bind
	// to its mount; a new one only replaces a container that has exited.
	root := filepath.Join(v.mountDir(), workspace)
	if !v.mountRunning(ctx, mounterName(workspace)) {
		if err := v.startMounter(ctx, workspace); err != nil {
			v.release(container)
			return "", err
		}
	}
	deadline := time.Now().Add(mountWait)
	for !mounted(root) {
		if !v.mountRunning(ctx, mounterName(workspace)) {
			v.release(container)
			return "", fmt.Errorf("the volume mount for workspace %s exited: %s", workspace, v.mounterLogs(ctx, mounterName(workspace)))
		}
		if time.Now().After(deadline) || !sleep(ctx, 100*time.Millisecond) {
			v.release(container)
			return "", fmt.Errorf("the volume mount for workspace %s did not appear within %v", workspace, mountWait)
		}
	}
	return root, nil
}

// lockFor returns the workspace's mount lock; v.mu is held.
func (v *volumes) lockFor(workspace string) *sync.Mutex {
	lock, ok := v.starting[workspace]
	if !ok {
		lock = &sync.Mutex{}
		v.starting[workspace] = lock
	}
	return lock
}

// addUser records a user; v.mu is held.
func (v *volumes) addUser(workspace, container string) {
	users, ok := v.users[workspace]
	if !ok {
		users = map[string]struct{}{}
		v.users[workspace] = users
	}
	users[container] = struct{}{}
	delete(v.idleSince, workspace)
}

// release forgets container as a user of every workspace mount.
func (v *volumes) release(container string) {
	v.mu.Lock()
	defer v.mu.Unlock()
	for workspace, users := range v.users {
		if _, ok := users[container]; !ok {
			continue
		}
		delete(users, container)
		if len(users) == 0 {
			v.idleSince[workspace] = time.Now()
		}
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

func (v *volumes) mountRunning(ctx context.Context, name string) bool {
	inspect, err := v.a.docker.ContainerInspect(ctx, name, client.ContainerInspectOptions{})
	return err == nil && inspect.Container.State != nil && inspect.Container.State.Running
}

func (v *volumes) mounterLogs(ctx context.Context, name string) string {
	logs, err := v.a.docker.ContainerLogs(ctx, name, client.ContainerLogsOptions{ShowStdout: true, ShowStderr: true, Tail: "5"})
	if err != nil {
		return err.Error()
	}
	defer func() { _ = logs.Close() }()
	buf := make([]byte, 4096)
	n, _ := logs.Read(buf)
	return strings.TrimSpace(strings.ToValidUTF8(string(buf[:n]), ""))
}

// startMounter replaces any mount container of workspace with a new one.
// Its script lazily unmounts what a dead predecessor left, then runs
// GeeseFS in the foreground; GeeseFS unmounts when stopped.
func (v *volumes) startMounter(ctx context.Context, workspace string) error {
	grant, err := v.location(workspace)
	if err != nil {
		return err
	}
	id, err := v.runMount(ctx, mountSpec{
		name: mounterName(workspace), dir: workspace, creds: v.storageDir(workspace),
		source: grant.Bucket + ":volumes/", endpoint: grant.Endpoint, region: grant.Region, pathStyle: grant.Endpoint != "",
		labels: map[string]string{labelKind: kindMount, labelWorkspace: workspace},
	})
	if err != nil {
		return err
	}
	v.a.goOwned(func(ctx context.Context) {
		v.watch(ctx, mounterName(workspace), id, func() []string { return v.usersOf(workspace) })
	})
	return nil
}

// mountSpec is one GeeseFS mount container.
type mountSpec struct {
	// name is the Docker name; dir the mount's directory under the mount
	// directory; creds holds its credential_process files.
	name, dir, creds string
	// source is bucket:prefix.
	source, endpoint, region string
	pathStyle, readOnly      bool
	labels                   map[string]string
}

// runMount starts a GeeseFS mount container and returns its id.
func (v *volumes) runMount(ctx context.Context, m mountSpec) (string, error) {
	if err := v.a.images.ensure(ctx, v.a.cfg.MountImage, ""); err != nil {
		return "", err
	}
	if err := v.a.removeContainer(ctx, m.name); err != nil {
		return "", err
	}
	// Only the agent (and root, which Docker runs as) may walk into the
	// mounts; workloads reach their own volume through a bind.
	if err := os.MkdirAll(v.mountDir(), 0o700); err != nil {
		return "", fmt.Errorf("create mount directory: %w", err)
	}
	if err := os.Chmod(v.mountDir(), 0o700); err != nil { //nolint:gosec // A directory needs its search bit.
		return "", fmt.Errorf("restrict mount directory: %w", err)
	}
	uid, gid := "0", "0"
	if os.Geteuid() != 0 {
		uid, gid = strconv.Itoa(os.Geteuid()), strconv.Itoa(os.Getegid())
	}
	target := "/mnt/" + m.dir
	args := []string{
		"-f", "-o", "allow_other", "--uid", uid, "--gid", gid, "--dir-mode", "0777", "--file-mode", "0666",
		"--fsync-on-close", "--stat-cache-ttl", "1s", "--memory-limit", strconv.Itoa(geesefsMemoryMiB), "--no-preload-dir",
	}
	if m.endpoint != "" {
		args = append(args, "--endpoint", m.endpoint)
	}
	if m.region != "" {
		args = append(args, "--region", m.region)
	}
	if !m.pathStyle {
		args = append(args, "--subdomain")
	}
	if m.readOnly {
		args = append(args, "-o", "ro")
	}
	args = append(args, m.source, target)
	quoted := make([]string, len(args))
	for n, arg := range args {
		quoted[n] = "'" + strings.ReplaceAll(arg, "'", `'\''`) + "'"
	}
	// GeeseFS cannot unmount itself here and leaves a dead mount when it
	// exits. On stop the script lets it flush, then detaches the mount so
	// it exits; after any exit it unmounts what remains.
	script := fmt.Sprintf(`umount -l %[1]s 2>/dev/null; mkdir -p %[1]s || exit 1
/opt/lazycloud/bin/geesefs %[2]s &
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
	maps.Copy(labels, m.labels)
	labels[labelHost] = v.a.identity.HostID
	pids := int64(mounterPidsLimit)
	created, err := v.a.docker.ContainerCreate(ctx, client.ContainerCreateOptions{
		Name: m.name,
		Config: &containertypes.Config{
			Image:      v.a.cfg.MountImage,
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
				{Type: mount.TypeBind, Source: m.creds, Target: "/creds", ReadOnly: true},
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
	v.mu.Lock()
	delete(v.stopping, m.name)
	v.mu.Unlock()
	if _, err := v.a.docker.ContainerStart(ctx, m.name, client.ContainerStartOptions{}); err != nil {
		return "", fmt.Errorf("start volume mount container: %w", err)
	}
	return created.ID, nil
}

func (v *volumes) usersOf(workspace string) []string {
	v.mu.Lock()
	defer v.mu.Unlock()
	users := make([]string, 0, len(v.users[workspace]))
	for user := range v.users[workspace] {
		users = append(users, user)
	}
	return users
}

// watch waits for the mount container name to exit. Unless the agent stopped
// it, the mount under every user is dead, so their containers stop and
// report the exit rather than run on with a broken volume.
func (v *volumes) watch(ctx context.Context, name, id string, users func() []string) {
	wait := v.a.docker.ContainerWait(ctx, id, client.ContainerWaitOptions{Condition: containertypes.WaitConditionNotRunning})
	select {
	case <-ctx.Done():
		return
	case <-wait.Result:
	case err := <-wait.Error:
		if !cerrdefs.IsNotFound(err) {
			if ctx.Err() == nil {
				v.a.log.Warn("watching a volume mount failed", "mount", name, "error", err)
			}
			return
		}
	}
	v.mu.Lock()
	deliberate := v.stopping[name]
	v.mu.Unlock()
	if deliberate {
		return
	}
	affected := users()
	v.a.log.Error("volume mount exited; stopping its containers", "mount", name, "containers", len(affected), "logs", v.mounterLogs(ctx, name))
	for _, user := range affected {
		if c := v.a.lookup(user); c != nil {
			c.failVolume(ctx, "the volume mount "+name+" exited")
		}
	}
}

// stopMounter stops a workspace mount; its script unmounts on SIGTERM.
func (v *volumes) stopMounter(ctx context.Context, workspace string) error {
	return v.stopMount(ctx, mounterName(workspace))
}

// stopMount stops a mount container; its script unmounts on SIGTERM.
func (v *volumes) stopMount(ctx context.Context, name string) error {
	v.mu.Lock()
	v.stopping[name] = true
	v.mu.Unlock()
	if err := v.a.stopDocker(ctx, name, stopKillSeconds); err != nil {
		return err
	}
	return v.a.removeContainer(ctx, name)
}

// adopt restores mount users from the workload containers a previous agent
// left running, and the grants it stored. Mount containers keep running.
func (v *volumes) adopt(summaries []containertypes.Summary) {
	v.mu.Lock()
	defer v.mu.Unlock()
	for _, s := range summaries {
		if s.State != containertypes.StateRunning {
			continue
		}
		if s.Labels[labelKind] == kindBucket {
			container, name, id := s.Labels[labelContainer], s.Names[0][1:], s.ID
			v.buckets[container] = append(v.buckets[container], name)
			v.a.goOwned(func(ctx context.Context) { v.watch(ctx, name, id, func() []string { return []string{container} }) })
			continue
		}
		if s.Labels[labelKind] == kindMount {
			workspace, id := s.Labels[labelWorkspace], s.ID
			v.a.goOwned(func(ctx context.Context) {
				v.watch(ctx, mounterName(workspace), id, func() []string { return v.usersOf(workspace) })
			})
			continue
		}
		if s.Labels[labelWorkspaces] == "" {
			continue
		}
		for _, workspace := range strings.Split(s.Labels[labelWorkspaces], ",") {
			v.addUser(workspace, s.Labels[labelContainer])
		}
	}
	entries, err := os.ReadDir(filepath.Join(v.a.cfg.StateDir, "storage"))
	if err != nil {
		return
	}
	for _, entry := range entries {
		// A stored grant counts only while its key is valid; otherwise
		// starts wait for the fresh one the server sends.
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

// pruneIdle stops mounts nothing has used for mountIdle, and mount
// containers of workspaces no container uses.
func (v *volumes) pruneIdle(ctx context.Context) {
	list, err := v.a.docker.ContainerList(ctx, client.ContainerListOptions{
		All:     true,
		Filters: client.Filters{}.Add("label", labelHost+"="+v.a.identity.HostID).Add("label", labelKind+"="+kindMount),
	})
	if err != nil {
		v.a.log.Warn("listing volume mounts failed", "error", err)
		return
	}
	now := time.Now()
	for _, s := range list.Items {
		workspace := s.Labels[labelWorkspace]
		v.mu.Lock()
		idle, since := len(v.users[workspace]) == 0, v.idleSince[workspace]
		if idle && since.IsZero() {
			v.idleSince[workspace], since = now, now
		}
		lock := v.lockFor(workspace)
		v.mu.Unlock()
		if !idle || now.Sub(since) < mountIdle || !lock.TryLock() {
			continue
		}
		v.mu.Lock()
		stillIdle := len(v.users[workspace]) == 0
		v.mu.Unlock()
		if stillIdle {
			if err := v.stopMounter(ctx, workspace); err != nil && !cerrdefs.IsNotFound(err) {
				v.a.log.Warn("stopping an idle volume mount failed", "workspace", workspace, "error", err)
			} else {
				v.a.log.Info("stopped idle volume mount", "workspace", workspace)
			}
		}
		lock.Unlock()
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
