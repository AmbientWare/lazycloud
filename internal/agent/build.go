package agent

import (
	"bufio"
	"cmp"
	"context"
	"debug/elf"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	cerrdefs "github.com/containerd/errdefs"
	"github.com/moby/moby/api/pkg/stdcopy"
	containertypes "github.com/moby/moby/api/types/container"
	"github.com/moby/moby/api/types/mount"
	"github.com/moby/moby/client"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/platformimages"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// labelBuildHost marks build containers. It differs from workload labels,
// so adopt never takes a build container for a workload.
const labelBuildHost = "lazycloud.build-host-id"

// Paths inside a build container.
const (
	buildContextDir    = "/build/context"
	buildDockerfileDir = "/build/dockerfile"
	buildOutDir        = "/build/out"
	buildDockerConfig  = "/build/docker"
	buildSecretsDir    = "/build/secrets"
	buildCDIDir        = "/build/cdi"
	// buildCacheDir is the workspace's build cache state the builder holds.
	buildCacheDir = "/build/cache"
)

// layerCompression is how BuildKit compresses the layers and cache it
// exports. zstd exports several times faster than gzip, and the agent's
// conversion reads it faster too.
const layerCompression = "zstd"

// imagePushed is the line builderScript prints once the image is in the
// registry and its metadata file is written.
const imagePushed = "lazycloud: image pushed; exporting the build cache"

// builderScript runs in the builder. It starts one BuildKit daemon on the
// build's cache state and refuses any snapshotter but overlayfs, the one
// that diffs layers without copying them. One solve builds and pushes the
// image and writes its metadata, then a second exports the build cache. The
// second finds every step in the daemon's state, so it costs about the
// export, and the agent converts the image while it runs. A prune then
// bounds the state, whatever the build's outcome. The positional arguments
// are the flags both solves take.
const builderScript = `set -eu
addr=unix://$XDG_RUNTIME_DIR/buildkit/buildkitd.sock
rootlesskit buildkitd --root ` + buildCacheDir + `/buildkit --addr "$addr" ${BUILDKITD_FLAGS:-} >/tmp/buildkitd.log 2>&1 &
daemon=$!
trap 'status=$?; buildctl --addr "$addr" prune --keep-storage ` + buildStateKeepMB + ` >/dev/null 2>&1 || true; kill $daemon 2>/dev/null || true; wait $daemon 2>/dev/null || true; exit $status' EXIT
tries=0
until buildctl --addr "$addr" debug workers >/dev/null 2>&1; do
  tries=$((tries + 1))
  if [ $tries -gt 200 ] || ! kill -0 $daemon 2>/dev/null; then
    echo "BuildKit did not start:" >&2
    cat /tmp/buildkitd.log >&2
    exit 1
  fi
  sleep 0.05
done
snapshotter=$(buildctl --addr "$addr" debug workers --verbose | sed -n 's/.*worker\.snapshotter:[[:space:]]*//p')
if [ "$snapshotter" != overlayfs ]; then
  echo "BuildKit chose the ${snapshotter:-unknown} snapshotter; builds need overlayfs on the build cache's filesystem" >&2
  cat /tmp/buildkitd.log >&2
  exit 1
fi
buildctl --addr "$addr" "$@" --progress plain --output "$LAZYCLOUD_IMAGE_OUTPUT" --metadata-file ` + buildOutDir + `/metadata.json
echo "` + imagePushed + `"
if [ -n "$LAZYCLOUD_CACHE_EXPORT" ]; then
  buildctl --addr "$addr" "$@" --progress quiet --export-cache "$LAZYCLOUD_CACHE_EXPORT"
fi
`

const (
	// buildTail is how many output lines a failure message carries.
	buildTail = 20
)

// startBuild runs a build container for spec. Like start it is idempotent by
// container id.
func (a *Agent) startBuild(spec *hostproto.StartContainer) {
	id := spec.GetContainerId()
	a.mu.Lock()
	c, known := a.containers[id]
	if !known {
		c = a.newContainer(id, "", 1, nil, hostproto.ContainerPhase_CONTAINER_PHASE_PREPARING)
		c.isBuild = true
		a.containers[id] = c
	}
	a.mu.Unlock()
	c.report()
	if !known {
		a.goOwned(func(ctx context.Context) {
			c.runBuild(ctx, spec)
			a.buildCaches.evict(ctx)
		})
	}
}

// runBuild prepares the build's files, runs the builder, streams its output
// and reports the outcome, converting the layers the server asks for, then
// the exit. If the agent stops first, the build is abandoned: the next agent
// removes its container and the server gives the build a new one.
func (c *container) runBuild(ctx context.Context, spec *hostproto.StartContainer) { //nolint:contextcheck // The build works within the container's work, which outlives ctx.
	build := spec.GetBuild()
	began := time.Now()
	logs := newBuildLogs(c.a.host, c.id, c.log)
	c.a.goOwned(func(context.Context) { logs.run(c.work) }) //nolint:contextcheck // output lives as long as the container's work
	work, cancel := context.WithDeadline(c.work, build.GetDeadline().AsTime())
	defer cancel()
	work, span := telemetry.StartIn(work, c.a.tracer(), spec.GetTraceparent(), "agent.build", trace.WithAttributes( //nolint:contextcheck // as runBuilder
		telemetry.Container(c.id), telemetry.Host(c.a.identity.HostID), attribute.String(telemetry.AttrBuild, build.GetBuildId()),
		attribute.Int("lazycloud.attempt", int(build.GetAttempt()))))
	defer span.End()

	// An early publish converts the pushed image while the builder exports
	// its cache.
	var early sync.WaitGroup
	var layers *layerPublish
	publish := func(outcome *hostproto.CompleteImageBuildRequest) {
		publishCtx, publish := telemetry.Start(work, "agent.build_publish")
		c.publishBuild(trace.ContextWithSpan(ctx, publish), publishCtx, layers, outcome, logs) //nolint:contextcheck // as runBuilder
		publish.End()
	}
	var outcome *hostproto.CompleteImageBuildRequest
	var exit *hostproto.ContainerExit
	// The state stays held until the conversions that read it end.
	cache, err := c.a.buildCaches.acquire(work, build.GetWorkspaceId()) //nolint:contextcheck // as runBuilder
	if err == nil {
		defer c.a.buildCaches.release(ctx, cache)
		var stop func()
		if layers, stop, err = c.newBuildLayers(build, cache, logs); err == nil {
			defer stop()
		}
	}
	if err != nil {
		exit = c.buildStartFailure(err)
	} else {
		// Conversions ahead of the server's answer end with the build.
		ahead, stopAhead := context.WithCancel(work)
		defer stopAhead()
		exported := func(manifest string) { layers.ahead(ahead, manifest, began) }
		outcome, exit = c.runBuilder(work, spec, cache, logs, exported, func(pushed *hostproto.CompleteImageBuildRequest) { //nolint:contextcheck // stopping the container ends its build
			early.Go(func() { publish(pushed) })
		})
	}
	early.Wait()
	span.SetAttributes(attribute.String("lazycloud.exit", exit.GetMessage()))
	if ctx.Err() != nil {
		return
	}
	if outcome != nil && !c.isStopping() {
		publish(outcome)
	}
	logs.waitFlushed(c.work, logs.mark()) //nolint:contextcheck // output lives as long as the container's work
	c.exited(exit)
}

// newBuildLayers returns the build's layer publish, reading the content
// store of the BuildKit state in cache, and a function that waits for its
// conversions and removes their files.
func (c *container) newBuildLayers(build *hostproto.ImageBuild, cache string, logs buildLogs) (*layerPublish, func(), error) {
	if err := os.MkdirAll(c.dir, 0o755); err != nil { //nolint:gosec // The builder reads the build's files as another user.
		return nil, nil, fmt.Errorf("create the build directory: %w", err)
	}
	dir, err := os.MkdirTemp(c.dir, "layers")
	if err != nil {
		return nil, nil, fmt.Errorf("create the layer directory: %w", err)
	}
	blobs := filepath.Join(cache, "buildkit", "runc-overlayfs", "content", "blobs", "sha256")
	layers := newLayerPublish(c, build, dir, blobs, logs)
	return layers, func() {
		layers.running.Wait()
		_ = os.RemoveAll(dir)
	}, nil
}

// runBuilder runs the builder and returns what to report: an outcome unless
// the build was stopped, never ran or was handed to pushed, and the
// container's exit. pushed takes the outcome as soon as the image is in the registry,
// while the builder exports its cache, and returns at once; runBuilder calls
// it at most once, before it returns.
func (c *container) runBuilder(ctx context.Context, spec *hostproto.StartContainer, cache string, logs buildLogs,
	exported func(manifest string), pushed func(*hostproto.CompleteImageBuildRequest),
) (*hostproto.CompleteImageBuildRequest, *hostproto.ContainerExit) {
	build := spec.GetBuild()
	startFailed := func(err error) (*hostproto.CompleteImageBuildRequest, *hostproto.ContainerExit) {
		return nil, c.buildStartFailure(err)
	}
	logs.add(ctx, "preparing build container")
	began := time.Now()
	// The secrets leave the host when the build ends, whatever its outcome.
	defer func() { _ = os.RemoveAll(filepath.Join(c.dir, "secrets")) }()
	if err := telemetry.Step(ctx, "agent.build_prepare", func(ctx context.Context) error {
		builder, err := c.a.platformImage(ctx, platformimages.Builder)
		if err != nil {
			return err
		}
		if err := c.prepareBuildFiles(ctx, build); err != nil {
			return err
		}
		var gpu *buildGPU
		if n := int(spec.GetResources().GetGpuCount()); n > 0 {
			gpus, err := c.a.allocateGPUs(c, n)
			if err != nil {
				return err
			}
			if gpu, err = writeBuildCDI(ctx, filepath.Join(c.dir, "cdi"), gpus); err != nil {
				return err
			}
		}
		return c.a.createBuilder(ctx, c, builder, spec, cache, gpu)
	}); err != nil {
		return startFailed(err)
	}
	c.mu.Lock()
	c.started = true
	c.phase = hostproto.ContainerPhase_CONTAINER_PHASE_READY
	c.mu.Unlock()
	c.report()
	ctx, run := telemetry.Start(ctx, "agent.build_run")
	defer run.End()
	c.log.Info("build container started", "build_id", build.GetBuildId(), "attempt", build.GetAttempt(), "prepare_ms", time.Since(began).Milliseconds())

	// The outcome is claimed once: by pushed when builderScript says the
	// image is in the registry, or at the exit, which first waits for the
	// output so that line arrives before it.
	var claim sync.Once
	early := false
	defer claim.Do(func() {})
	claimed := func() bool {
		claim.Do(func() {})
		return early
	}
	metadata := filepath.Join(c.dir, "out", "metadata.json")
	tail := c.a.followBuildOutput(ctx, c.dockerName(), logs, func(line string) {
		if m := exportedManifest.FindStringSubmatch(line); m != nil {
			exported(m[1])
			return
		}
		if line != imagePushed || c.isStopping() {
			return
		}
		digest, err := readBuildDigest(metadata)
		if err != nil {
			c.log.Warn("the builder pushed an image whose metadata is unreadable", "error", err)
			return
		}
		claim.Do(func() {
			early = true
			logs.add(ctx, "pushed "+build.GetPushRepository()+"@"+digest)
			pushed(pushedBuild(c.id, digest))
		})
	})
	exited, err := c.a.waitExit(ctx, c.dockerName())
	if !exited {
		// Stopped, past the deadline or the agent is stopping: end the builder.
		_ = c.a.stopDocker(context.WithoutCancel(ctx), c.dockerName(), 0)
		if c.isStopping() {
			return nil, &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_STOPPED, Message: "build stopped"}
		}
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			// tail waits for the output, so a push line reaches claimed first.
			lines := tail()
			exit := &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_STOPPED, Message: "the build did not finish before its deadline"}
			if claimed() {
				exit.Message = "the build cache export did not finish before the build's deadline"
				return nil, exit
			}
			return failedBuild(c.id, exit.Message, lines), exit
		}
		return nil, &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_CRASHED, Message: fmt.Sprintf("waiting for the builder failed: %v", err)}
	}
	state, err := c.a.exitState(context.WithoutCancel(ctx), c.dockerName())
	if err != nil {
		return nil, &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_CRASHED, Message: err.Error()}
	}
	lines := tail()
	exit := &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_STOPPED, ExitCode: int32(state.ExitCode)} //nolint:gosec // exit codes fit
	switch {
	case state.OOMKilled:
		exit.Reason, exit.Message = hostproto.ExitReason_EXIT_REASON_OUT_OF_MEMORY, "the builder ran out of memory"
		return nil, exit
	case claimed():
		// pushed has the image; only the cache export can have failed.
		exit.Message = "build finished"
		if state.ExitCode != 0 {
			exit.Message = fmt.Sprintf("exporting the build cache failed with exit code %d", state.ExitCode)
			logs.add(ctx, exit.Message)
		}
		return nil, exit
	case state.ExitCode != 0:
		exit.Message = fmt.Sprintf("the build failed with exit code %d", state.ExitCode)
		return failedBuild(c.id, exit.Message, lines), exit
	}
	digest, err := readBuildDigest(metadata)
	if err != nil {
		exit.Message = err.Error()
		return failedBuild(c.id, err.Error(), lines), exit
	}
	logs.add(ctx, "pushed "+build.GetPushRepository()+"@"+digest)
	exit.Message = "build finished"
	return pushedBuild(c.id, digest), exit
}

// buildStartFailure is the exit of a build that err kept from starting.
func (c *container) buildStartFailure(err error) *hostproto.ContainerExit {
	reason := hostproto.ExitReason_EXIT_REASON_START_FAILED
	if c.isStopping() {
		reason = hostproto.ExitReason_EXIT_REASON_STOPPED
	}
	c.log.Warn("build container did not start", "error", err)
	return &hostproto.ContainerExit{Reason: reason, Message: err.Error()}
}

func pushedBuild(container, digest string) *hostproto.CompleteImageBuildRequest {
	return &hostproto.CompleteImageBuildRequest{ContainerId: container, Outcome: &hostproto.CompleteImageBuildRequest_Digest{Digest: digest}}
}

func failedBuild(container, message string, tail []string) *hostproto.CompleteImageBuildRequest {
	if len(tail) > 0 {
		message += "\n" + strings.Join(tail, "\n")
	}
	return &hostproto.CompleteImageBuildRequest{ContainerId: container, Outcome: &hostproto.CompleteImageBuildRequest_Failure{Failure: message}}
}

// prepareBuildFiles writes the context, the Dockerfile, the registry logins
// and an output directory under the container's directory.
func (c *container) prepareBuildFiles(ctx context.Context, build *hostproto.ImageBuild) error {
	contextDir := filepath.Join(c.dir, "context")
	if src := build.GetContext(); src != nil {
		archive, err := c.a.sources.fetch(ctx, src)
		if err != nil {
			return err
		}
		if err := extractWorkspace(archive, contextDir); err != nil {
			return err
		}
	} else if err := os.MkdirAll(contextDir, 0o755); err != nil { //nolint:gosec // The builder reads it as another user.
		return fmt.Errorf("create build context: %w", err)
	}
	dockerfileDir := filepath.Join(c.dir, "dockerfile")
	if err := os.MkdirAll(dockerfileDir, 0o755); err != nil { //nolint:gosec // The builder reads it as another user.
		return fmt.Errorf("create dockerfile directory: %w", err)
	}
	if err := os.WriteFile(filepath.Join(dockerfileDir, "Dockerfile"), []byte(build.GetDockerfile()), 0o644); err != nil { //nolint:gosec // As above.
		return fmt.Errorf("write Dockerfile: %w", err)
	}
	// The builder runs as its own unprivileged user.
	for _, dir := range []string{"out", "docker"} {
		path := filepath.Join(c.dir, dir)
		if err := os.MkdirAll(path, 0o700); err != nil {
			return fmt.Errorf("create build directory: %w", err)
		}
		if err := os.Chmod(path, 0o777); err != nil { //nolint:gosec // Only the builder and the agent use the directory.
			return fmt.Errorf("open build directory: %w", err)
		}
	}
	config, err := dockerConfig(build.GetRegistryAuth())
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(c.dir, "docker", "config.json"), config, 0o644); err != nil { //nolint:gosec // The builder reads it as another user; the directory goes when the container does.
		return fmt.Errorf("write registry logins: %w", err)
	}
	if len(build.GetSecrets()) == 0 {
		return nil
	}
	// One file per secret, which the builder hands BuildKit as a secret;
	// runBuild removes them when the build ends.
	secrets := filepath.Join(c.dir, "secrets")
	if err := os.MkdirAll(secrets, 0o700); err != nil {
		return fmt.Errorf("create build secrets directory: %w", err)
	}
	if err := os.Chmod(secrets, 0o755); err != nil { //nolint:gosec // The builder reads it as another user.
		return fmt.Errorf("open build secrets directory: %w", err)
	}
	for name, value := range build.GetSecrets() {
		if !envName.MatchString(name) {
			return fmt.Errorf("build secret name %q is invalid", name)
		}
		if err := os.WriteFile(filepath.Join(secrets, name), []byte(value), 0o644); err != nil { //nolint:gosec // As above.
			return fmt.Errorf("write build secret: %w", err)
		}
	}
	return nil
}

// envName is a build secret's name, which is also the environment variable
// the steps read and a file name.
var envName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// buildCDIAnnotation lets BuildKit give the device to a step without a
// device entitlement.
const buildCDIAnnotation = "org.mobyproject.buildkit.device.autoallow"

// buildGPU is what the builder container needs for its steps to use the
// GPUs it holds: the host files and device nodes their CDI spec names, at
// the same paths.
type buildGPU struct {
	mounts  []mount.Mount
	devices []containertypes.DeviceMapping
}

// cdiEdits are the parts of CDI container edits that name host paths.
type cdiEdits struct {
	DeviceNodes []struct {
		Path     string `json:"path"`
		HostPath string `json:"hostPath"`
	} `json:"deviceNodes"`
	Mounts []cdiMount `json:"mounts"`
}

// cdiMount is one CDI mount.
type cdiMount struct {
	HostPath      string   `json:"hostPath"`
	ContainerPath string   `json:"containerPath"`
	Type          string   `json:"type,omitempty"`
	Options       []string `json:"options,omitempty"`
}

// sharedObjectName is the soname of the ELF library at path, or "" for any
// other file.
func sharedObjectName(path string) string {
	if !strings.Contains(filepath.Base(path), ".so") {
		return ""
	}
	f, err := elf.Open(path)
	if err != nil {
		return ""
	}
	defer func() { _ = f.Close() }()
	names, err := f.DynString(elf.DT_SONAME)
	if err != nil || len(names) == 0 || strings.Contains(names[0], "/") {
		return ""
	}
	return names[0]
}

// add gives the builder the paths edits name and returns the edits a
// rootless builder can apply. BuildKit applies the spec inside the builder,
// so each path must exist there as on the host. Its hooks are dropped: the
// toolkit's hook binary needs glibc, which the builder lacks. What they do
// that steps need, giving the driver libraries their sonames, is done with
// a second mount of each library under its soname.
func (g *buildGPU) add(raw json.RawMessage) (json.RawMessage, error) {
	if len(raw) == 0 {
		return raw, nil
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return nil, fmt.Errorf("decode CDI container edits: %w", err)
	}
	delete(fields, "hooks")
	var edits cdiEdits
	if err := json.Unmarshal(raw, &edits); err != nil {
		return nil, fmt.Errorf("decode CDI container edits: %w", err)
	}
	mounts := slices.Clone(edits.Mounts)
	for _, m := range edits.Mounts {
		soname := sharedObjectName(m.HostPath)
		if soname != "" && soname != filepath.Base(m.ContainerPath) {
			mounts = append(mounts, cdiMount{HostPath: m.HostPath, ContainerPath: filepath.Join(filepath.Dir(m.ContainerPath), soname), Options: m.Options})
		}
	}
	if len(mounts) > 0 {
		encoded, err := json.Marshal(mounts)
		if err != nil {
			return nil, fmt.Errorf("encode CDI mounts: %w", err)
		}
		fields["mounts"] = encoded
	}
	bind := func(path string) {
		if path == "" || slices.ContainsFunc(g.mounts, func(m mount.Mount) bool { return m.Source == path }) {
			return
		}
		if _, err := os.Stat(path); err == nil {
			g.mounts = append(g.mounts, mount.Mount{Type: mount.TypeBind, Source: path, Target: path, ReadOnly: true})
		}
	}
	for _, m := range mounts {
		bind(m.HostPath)
	}
	for _, d := range edits.DeviceNodes {
		host := cmp.Or(d.HostPath, d.Path)
		g.devices = append(g.devices, containertypes.DeviceMapping{PathOnHost: host, PathInContainer: d.Path, CgroupPermissions: "rwm"})
	}
	out, err := json.Marshal(fields)
	if err != nil {
		return nil, fmt.Errorf("encode CDI container edits: %w", err)
	}
	return out, nil
}

// writeBuildCDI writes a CDI spec into dir naming only the GPUs the build
// holds, from the host's NVIDIA Container Toolkit, and returns what the
// builder needs to apply it. BuildKit gives the GPUs to each RUN step that
// asks for nvidia.com/gpu=*.
func writeBuildCDI(ctx context.Context, dir string, gpus []string) (*buildGPU, error) {
	out, err := exec.CommandContext(ctx, "nvidia-ctk", "cdi", "generate", "--format=json", "--device-name-strategy=uuid").Output()
	if err != nil {
		return nil, fmt.Errorf("generate the GPU CDI spec: %w", err)
	}
	type device struct {
		Name           string            `json:"name"`
		Annotations    map[string]string `json:"annotations,omitempty"`
		ContainerEdits json.RawMessage   `json:"containerEdits"`
	}
	var spec struct {
		CDIVersion     string          `json:"cdiVersion"`
		Kind           string          `json:"kind"`
		Devices        []device        `json:"devices"`
		ContainerEdits json.RawMessage `json:"containerEdits,omitempty"`
	}
	if err := json.Unmarshal(out, &spec); err != nil {
		return nil, fmt.Errorf("decode the GPU CDI spec: %w", err)
	}
	gpu := &buildGPU{mounts: []mount.Mount{{Type: mount.TypeBind, Source: dir, Target: buildCDIDir, ReadOnly: true}}}
	if spec.ContainerEdits, err = gpu.add(spec.ContainerEdits); err != nil {
		return nil, err
	}
	held := spec.Devices[:0]
	for _, d := range spec.Devices {
		if slices.Contains(gpus, d.Name) {
			d.Annotations = map[string]string{buildCDIAnnotation: "true"}
			if d.ContainerEdits, err = gpu.add(d.ContainerEdits); err != nil {
				return nil, err
			}
			held = append(held, d)
		}
	}
	if len(held) != len(gpus) {
		return nil, fmt.Errorf("the GPU CDI spec names %d of the build's %d GPUs", len(held), len(gpus))
	}
	spec.Devices = held
	// Device annotations need CDI 0.6.0; the versions are 0.x.0.
	if spec.CDIVersion < "0.6.0" {
		spec.CDIVersion = "0.6.0"
	}
	encoded, err := json.Marshal(spec)
	if err != nil {
		return nil, fmt.Errorf("encode the GPU CDI spec: %w", err)
	}
	if err := os.MkdirAll(dir, 0o755); err != nil { //nolint:gosec // The builder reads it as another user.
		return nil, fmt.Errorf("create CDI directory: %w", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "nvidia.json"), encoded, 0o644); err != nil { //nolint:gosec // As above.
		return nil, fmt.Errorf("write the GPU CDI spec: %w", err)
	}
	return gpu, nil
}

// dockerConfig is a Docker config with a login per registry host.
func dockerConfig(auths map[string]*hostproto.RegistryAuth) ([]byte, error) {
	type entry struct {
		Auth          string `json:"auth,omitempty"`
		IdentityToken string `json:"identitytoken,omitempty"`
	}
	out := map[string]entry{}
	for host, a := range auths {
		key := host
		if host == "docker.io" {
			key = "https://index.docker.io/v1/"
		}
		e := entry{IdentityToken: a.GetIdentityToken()}
		if a.GetUsername() != "" || a.GetPassword() != "" {
			e.Auth = base64.StdEncoding.EncodeToString([]byte(a.GetUsername() + ":" + a.GetPassword()))
		}
		out[key] = e
	}
	encoded, err := json.Marshal(map[string]any{"auths": out})
	if err != nil {
		return nil, fmt.Errorf("encode registry logins: %w", err)
	}
	return encoded, nil
}

func (a *Agent) createBuilder(ctx context.Context, c *container, image string, spec *hostproto.StartContainer, cache string, gpu *buildGPU) error {
	build := spec.GetBuild()
	insecure := ""
	if build.GetInsecureRegistry() {
		insecure = ",registry.insecure=true"
	}
	args := []string{
		"build", "--frontend", "dockerfile.v0",
		"--local", "context=" + buildContextDir,
		"--local", "dockerfile=" + buildDockerfileDir,
		"--opt", "platform=" + build.GetPlatform(),
	}
	cacheExport := ""
	if ref := build.GetCacheRef(); ref != "" {
		args = append(args, "--import-cache", "type=registry,ref="+ref+insecure)
		cacheExport = "type=registry,ref=" + ref + ",mode=max,compression=" + layerCompression + insecure
	}
	mounts := []mount.Mount{
		{Type: mount.TypeBind, Source: filepath.Join(c.dir, "context"), Target: buildContextDir, ReadOnly: true},
		{Type: mount.TypeBind, Source: filepath.Join(c.dir, "dockerfile"), Target: buildDockerfileDir, ReadOnly: true},
		{Type: mount.TypeBind, Source: filepath.Join(c.dir, "out"), Target: buildOutDir},
		{Type: mount.TypeBind, Source: filepath.Join(c.dir, "docker"), Target: buildDockerConfig, ReadOnly: true},
		{Type: mount.TypeBind, Source: cache, Target: buildCacheDir},
	}
	// Secrets reach BuildKit as files of the builder's secret mount; the
	// Dockerfile mounts each into the steps that read it.
	if len(build.GetSecrets()) > 0 {
		mounts = append(mounts, mount.Mount{Type: mount.TypeBind, Source: filepath.Join(c.dir, "secrets"), Target: buildSecretsDir, ReadOnly: true})
		for _, name := range slices.Sorted(maps.Keys(build.GetSecrets())) {
			args = append(args, "--secret", "id="+name+",src="+buildSecretsDir+"/"+name)
		}
	}
	env := []string{
		"DOCKER_CONFIG=" + buildDockerConfig,
		"LAZYCLOUD_IMAGE_OUTPUT=type=image,name=" + build.GetPushRepository() + ",push-by-digest=true,push=true,compression=" + layerCompression + insecure,
		"LAZYCLOUD_CACHE_EXPORT=" + cacheExport,
	}
	resources := containerResources(spec.GetResources(), a.capacity, a.topology, int64(pidsLimit), nil)
	if gpu != nil {
		// The held GPUs reach the builder as the devices and files of their
		// CDI spec, which BuildKit then applies to the steps.
		mounts = append(mounts, gpu.mounts...)
		resources.Devices = gpu.devices
		env = append(env, "BUILDKITD_FLAGS=--cdi-spec-dir="+buildCDIDir)
	}
	labels := maps.Clone(a.cfg.Labels)
	if labels == nil {
		labels = map[string]string{}
	}
	labels[labelBuildHost] = a.identity.HostID
	options := client.ContainerCreateOptions{
		Name: c.dockerName(),
		Config: &containertypes.Config{
			Image:      image,
			Entrypoint: []string{"sh", "-c", builderScript, "builder"},
			Cmd:        args,
			Env:        env,
			Labels:     labels,
		},
		HostConfig: &containertypes.HostConfig{
			// Rootless BuildKit creates user namespaces and mounts procfs for
			// each step's sandbox. The builder runs under runc, as gVisor
			// cannot run rootless BuildKit.
			SecurityOpt: []string{"seccomp=unconfined", "apparmor=unconfined"},
			// Empty, not nil: Docker's systempaths=unconfined.
			MaskedPaths:   []string{},
			ReadonlyPaths: []string{},
			NetworkMode:   containertypes.NetworkMode(a.cfg.BuildNetwork),
			Mounts:        mounts,
			Resources:     resources,
		},
	}
	_, err := a.docker.ContainerCreate(ctx, options)
	if cerrdefs.IsConflict(err) {
		if err := a.removeContainer(ctx, c.dockerName()); err != nil {
			return err
		}
		_, err = a.docker.ContainerCreate(ctx, options)
	}
	if err != nil {
		return fmt.Errorf("create build container: %w", err)
	}
	if _, err := a.docker.ContainerStart(ctx, c.dockerName(), client.ContainerStartOptions{}); err != nil {
		return fmt.Errorf("start build container: %w", err)
	}
	return nil
}

// followBuildOutput sends the builder's output as build logs until it ends,
// passing each line to watch, and returns a function giving the last lines.
func (a *Agent) followBuildOutput(ctx context.Context, name string, logs buildLogs, watch func(string)) func() []string {
	var mu sync.Mutex
	var tail []string
	done := make(chan struct{})
	a.goOwned(func(context.Context) {
		defer close(done)
		stream, err := a.docker.ContainerLogs(ctx, name, client.ContainerLogsOptions{ShowStdout: true, ShowStderr: true, Follow: true})
		if err != nil {
			logs.add(ctx, fmt.Sprintf("reading build output failed: %v", err))
			return
		}
		defer func() { _ = stream.Close() }()
		reader, writer := io.Pipe()
		// The copy ends when the stream does: the builder exited or ctx ended.
		a.goOwned(func(context.Context) {
			_, err := stdcopy.StdCopy(writer, writer, stream)
			writer.CloseWithError(err)
		})
		err = readBuildLines(reader, func(line string) {
			mu.Lock()
			tail = append(tail, line)
			if len(tail) > buildTail {
				tail = tail[len(tail)-buildTail:]
			}
			mu.Unlock()
			logs.add(ctx, line)
			watch(line)
		})
		if err != nil && ctx.Err() == nil {
			logs.add(ctx, fmt.Sprintf("reading build output failed: %v", err))
		}
		_ = reader.Close()
	})
	return func() []string {
		// The output ends with the container; wait briefly for its last lines.
		select {
		case <-done:
		case <-time.After(5 * time.Second):
		}
		mu.Lock()
		defer mu.Unlock()
		return slices.Clone(tail)
	}
}

// maxBuildLine bounds one line of build output; the rest of a longer line
// is dropped.
const maxBuildLine = 64 << 10

// readBuildLines passes each line of r to line until r ends, and returns
// the error that ended it.
func readBuildLines(r io.Reader, line func(string)) error {
	reader := bufio.NewReaderSize(r, maxBuildLine)
	// cut is set while the rest of a line already passed on is dropped.
	cut := false
	for {
		chunk, err := reader.ReadSlice('\n')
		if errors.Is(err, bufio.ErrBufferFull) {
			if !cut {
				line(string(chunk))
			}
			cut = true
			continue
		}
		if len(chunk) > 0 && !cut {
			line(strings.TrimRight(string(chunk), "\r\n"))
		}
		cut = false
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err //nolint:wrapcheck // The caller names the stream.
		}
	}
}

func readBuildDigest(path string) (string, error) {
	data, err := os.ReadFile(path) //nolint:gosec // The path is the agent's own.
	if err != nil {
		return "", fmt.Errorf("read build metadata: %w", err)
	}
	var metadata struct {
		Digest string `json:"containerimage.digest"`
	}
	if err := json.Unmarshal(data, &metadata); err != nil {
		return "", fmt.Errorf("decode build metadata: %w", err)
	}
	if metadata.Digest == "" {
		return "", fmt.Errorf("the build metadata names no image digest")
	}
	return metadata.Digest, nil
}

// publishBuild reports the outcome. While the server answers with layers
// of the pushed image it has no converted pair of, it converts them and
// reports each one's sizes as it is converted, then uploads it to the URLs
// signed for those sizes and reports it uploaded, within the build's
// deadline (work). A layer that cannot be converted or stored, or one still
// missing after a few rounds, fails the build.
func (c *container) publishBuild(ctx, work context.Context, layers *layerPublish, request *hostproto.CompleteImageBuildRequest, logs buildLogs) {
	if err := layers.publish(work, request); err != nil {
		// Only a layer's content fails the image; a store or registry that
		// stayed unreachable fails this build alone.
		reason := err.Error()
		logs.add(ctx, reason)
		request := failedBuild(c.id, reason, nil)
		request.FailureTransient = !errors.Is(err, errLayerContent)
		c.completeBuild(ctx, request)
	}
}

// completeBuild delivers request until the agent stops and returns the
// answer, or nil when there is none to act on. The server's recovery
// covers an outcome never delivered.
func (c *container) completeBuild(ctx context.Context, request *hostproto.CompleteImageBuildRequest) *hostproto.CompleteImageBuildResponse {
	var resp *hostproto.CompleteImageBuildResponse
	deliverOutcome(ctx, c.a.drain, c.log, "build", publishCallTimeout, func(ctx context.Context) error {
		var err error
		resp, err = c.a.host.CompleteImageBuild(ctx, request)
		return err //nolint:wrapcheck // deliverOutcome reads the call's status.
	})
	return resp
}

// removeBuildContainers removes build containers a previous agent left. A
// build cannot resume, so the server starts it again in a new container.
func (a *Agent) removeBuildContainers(ctx context.Context) error {
	return a.removeLabeled(ctx, labelBuildHost)
}

// removeHostContainers removes every container the agent created for this
// host, running or not.
func (a *Agent) removeHostContainers(ctx context.Context) error {
	if err := a.removeLabeled(ctx, labelHost); err != nil {
		return err
	}
	return a.removeBuildContainers(ctx)
}

// removeLabeled removes every container whose label names this host.
func (a *Agent) removeLabeled(ctx context.Context, label string) error {
	list, err := a.docker.ContainerList(ctx, client.ContainerListOptions{
		All:     true,
		Filters: client.Filters{}.Add("label", label+"="+a.identity.HostID),
	})
	if err != nil {
		return fmt.Errorf("list containers: %w", err)
	}
	for _, summary := range list.Items {
		if err := a.removeContainer(ctx, summary.ID); err != nil {
			return err
		}
	}
	return nil
}
