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
	"log/slog"
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
	"github.com/moby/moby/api/pkg/authconfig"
	"github.com/moby/moby/api/pkg/stdcopy"
	containertypes "github.com/moby/moby/api/types/container"
	"github.com/moby/moby/api/types/mount"
	"github.com/moby/moby/api/types/registry"
	"github.com/moby/moby/client"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/platformimages"
)

// Labels on build containers. They differ from workload labels, so adopt
// never takes a build container for a workload.
const (
	labelBuildHost      = "lazycloud.build-host-id"
	labelBuildContainer = "lazycloud.build-container-id"
)

// Paths inside a build container.
const (
	buildContextDir    = "/build/context"
	buildDockerfileDir = "/build/dockerfile"
	buildOutDir        = "/build/out"
	buildDockerConfig  = "/build/docker"
	buildSecretsDir    = "/build/secrets"
	buildCDIDir        = "/build/cdi"
)

const (
	// buildTail is how many output lines a failure message carries.
	buildTail          = 20
	buildLogFlush      = 200 * time.Millisecond
	buildLogBatchBytes = 64 << 10
	// buildLogBuffer bounds unsent output; reading the build's output waits
	// above it.
	buildLogBuffer = 1 << 20
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
		a.goOwned(func(ctx context.Context) { c.runBuild(ctx, spec) })
	}
}

// runBuild prepares the build's files, runs the builder, streams its output
// and reports the outcome, converting the layers the server asks for, then
// the exit. If the agent stops first, the build is abandoned: the next agent
// removes its container and the server gives the build a new one.
func (c *container) runBuild(ctx context.Context, spec *hostproto.StartContainer) {
	build := spec.GetBuild()
	logs := newBuildLogs(c.a.host, c.id, c.log)
	c.a.goOwned(func(context.Context) { logs.run(c.work) }) //nolint:contextcheck // output lives as long as the container's work
	work, cancel := context.WithDeadline(c.work, build.GetDeadline().AsTime())
	defer cancel()

	outcome, exit := c.runBuilder(work, spec, logs) //nolint:contextcheck // stopping the container ends its build
	if ctx.Err() != nil {
		return
	}
	if outcome != nil && !c.isStopping() {
		c.publishBuild(ctx, work, build, outcome, logs) //nolint:contextcheck // as above
	}
	logs.close()
	c.exited(exit)
}

// runBuilder runs the builder and returns what to report: an outcome unless the
// build was stopped or never ran, and the container's exit.
func (c *container) runBuilder(ctx context.Context, spec *hostproto.StartContainer, logs *buildLogs) (*hostproto.CompleteImageBuildRequest, *hostproto.ContainerExit) {
	build := spec.GetBuild()
	startFailed := func(err error) (*hostproto.CompleteImageBuildRequest, *hostproto.ContainerExit) {
		reason := hostproto.ExitReason_EXIT_REASON_START_FAILED
		if c.isStopping() {
			reason = hostproto.ExitReason_EXIT_REASON_STOPPED
		}
		c.log.Warn("build container did not start", "error", err)
		return nil, &hostproto.ContainerExit{Reason: reason, Message: err.Error()}
	}
	logs.add("preparing build container")
	began := time.Now()
	builder, err := c.a.platformImage(ctx, platformimages.Builder)
	if err != nil {
		return startFailed(err)
	}
	// The secrets leave the host when the build ends, whatever its outcome.
	defer func() { _ = os.RemoveAll(filepath.Join(c.dir, "secrets")) }()
	if err := c.prepareBuildFiles(ctx, build); err != nil {
		return startFailed(err)
	}
	var gpu *buildGPU
	if n := int(spec.GetResources().GetGpuCount()); n > 0 {
		gpus, err := c.a.allocateGPUs(c, n)
		if err != nil {
			return startFailed(err)
		}
		if gpu, err = writeBuildCDI(ctx, filepath.Join(c.dir, "cdi"), gpus); err != nil {
			return startFailed(err)
		}
	}
	if err := c.a.createBuilder(ctx, c, builder, spec, gpu); err != nil {
		return startFailed(err)
	}
	c.mu.Lock()
	c.started = true
	c.phase = hostproto.ContainerPhase_CONTAINER_PHASE_READY
	c.mu.Unlock()
	c.report()
	c.log.Info("build container started", "build_id", build.GetBuildId(), "attempt", build.GetAttempt(), "prepare_ms", time.Since(began).Milliseconds())

	tail := c.a.followBuildOutput(ctx, c.dockerName(), logs)
	exited, err := c.a.waitExit(ctx, c.dockerName())
	if !exited {
		// Stopped, past the deadline or the agent is stopping: end the builder.
		_ = c.a.stopDocker(context.WithoutCancel(ctx), c.dockerName(), 0)
		if c.isStopping() {
			return nil, &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_STOPPED, Message: "build stopped"}
		}
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			failure := "the build did not finish before its deadline"
			return failedBuild(c.id, failure, tail()), &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_STOPPED, Message: failure}
		}
		return nil, &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_CRASHED, Message: fmt.Sprintf("waiting for the builder failed: %v", err)}
	}
	state, err := c.a.exitState(context.WithoutCancel(ctx), c.dockerName())
	if err != nil {
		return nil, &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_CRASHED, Message: err.Error()}
	}
	exit := &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_STOPPED, ExitCode: int32(state.ExitCode)} //nolint:gosec // exit codes fit
	switch {
	case state.OOMKilled:
		exit.Reason, exit.Message = hostproto.ExitReason_EXIT_REASON_OUT_OF_MEMORY, "the builder ran out of memory"
		return nil, exit
	case state.ExitCode != 0:
		exit.Message = fmt.Sprintf("the build failed with exit code %d", state.ExitCode)
		return failedBuild(c.id, exit.Message, tail()), exit
	}
	digest, err := readBuildDigest(filepath.Join(c.dir, "out", "metadata.json"))
	if err != nil {
		exit.Message = err.Error()
		return failedBuild(c.id, err.Error(), tail()), exit
	}
	logs.add("pushed " + build.GetPushRepository() + "@" + digest)
	exit.Message = "build finished"
	return &hostproto.CompleteImageBuildRequest{ContainerId: c.id, Outcome: &hostproto.CompleteImageBuildRequest_Digest{Digest: digest}}, exit
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

func (a *Agent) createBuilder(ctx context.Context, c *container, image string, spec *hostproto.StartContainer, gpu *buildGPU) error {
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
		"--output", "type=image,name=" + build.GetPushRepository() + ",push-by-digest=true,push=true" + insecure,
		"--metadata-file", buildOutDir + "/metadata.json",
		"--progress", "plain",
	}
	if ref := build.GetCacheRef(); ref != "" {
		args = append(args,
			"--import-cache", "type=registry,ref="+ref+insecure,
			"--export-cache", "type=registry,ref="+ref+",mode=max"+insecure)
	}
	mounts := []mount.Mount{
		{Type: mount.TypeBind, Source: filepath.Join(c.dir, "context"), Target: buildContextDir, ReadOnly: true},
		{Type: mount.TypeBind, Source: filepath.Join(c.dir, "dockerfile"), Target: buildDockerfileDir, ReadOnly: true},
		{Type: mount.TypeBind, Source: filepath.Join(c.dir, "out"), Target: buildOutDir},
		{Type: mount.TypeBind, Source: filepath.Join(c.dir, "docker"), Target: buildDockerConfig, ReadOnly: true},
	}
	// Secrets reach BuildKit as files of the builder's secret mount; the
	// Dockerfile mounts each into the steps that read it.
	if len(build.GetSecrets()) > 0 {
		mounts = append(mounts, mount.Mount{Type: mount.TypeBind, Source: filepath.Join(c.dir, "secrets"), Target: buildSecretsDir, ReadOnly: true})
		for _, name := range slices.Sorted(maps.Keys(build.GetSecrets())) {
			args = append(args, "--secret", "id="+name+",src="+buildSecretsDir+"/"+name)
		}
	}
	env := []string{"DOCKER_CONFIG=" + buildDockerConfig}
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
	labels[labelBuildContainer] = c.id
	options := client.ContainerCreateOptions{
		Name: c.dockerName(),
		Config: &containertypes.Config{
			Image:      image,
			Entrypoint: []string{"buildctl-daemonless.sh"},
			Cmd:        args,
			Env:        env,
			Labels:     labels,
		},
		HostConfig: &containertypes.HostConfig{
			// Rootless BuildKit creates user namespaces and mounts procfs for
			// each step's sandbox. The builder runs with runc: gVisor does not
			// run it yet.
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
// and returns a function giving the last lines.
func (a *Agent) followBuildOutput(ctx context.Context, name string, logs *buildLogs) func() []string {
	var mu sync.Mutex
	var tail []string
	done := make(chan struct{})
	a.goOwned(func(context.Context) {
		defer close(done)
		stream, err := a.docker.ContainerLogs(ctx, name, client.ContainerLogsOptions{ShowStdout: true, ShowStderr: true, Follow: true})
		if err != nil {
			logs.add(fmt.Sprintf("reading build output failed: %v", err))
			return
		}
		defer func() { _ = stream.Close() }()
		reader, writer := io.Pipe()
		// The copy ends when the stream does: the builder exited or ctx ended.
		a.goOwned(func(context.Context) {
			_, err := stdcopy.StdCopy(writer, writer, stream)
			writer.CloseWithError(err)
		})
		scanner := bufio.NewScanner(reader)
		scanner.Buffer(make([]byte, 64<<10), 1<<20)
		for scanner.Scan() {
			line := strings.TrimRight(scanner.Text(), "\r")
			mu.Lock()
			tail = append(tail, line)
			if len(tail) > buildTail {
				tail = tail[len(tail)-buildTail:]
			}
			mu.Unlock()
			logs.add(line)
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
// reports their sizes, then uploads them to the URLs signed for those sizes
// and reports them uploaded, within the build's deadline (work). A layer
// that cannot be converted or stored, or layers still missing after a few
// rounds, fail the build.
func (c *container) publishBuild(ctx, work context.Context, build *hostproto.ImageBuild, request *hostproto.CompleteImageBuildRequest, logs *buildLogs) {
	fail := func(reason string, transient bool) {
		logs.add(reason)
		request := failedBuild(c.id, reason, nil)
		request.FailureTransient = transient
		c.completeBuild(ctx, request)
	}
	dir, err := os.MkdirTemp(c.dir, "layers")
	if err != nil {
		fail(fmt.Sprintf("create the layer directory: %v", err), true)
		return
	}
	defer func() { _ = os.RemoveAll(dir) }()
	layers := &layerPublish{a: c.a, build: build, dir: dir, logs: logs, done: map[string]*convertedLayer{}}
	for round := 1; ; round++ {
		uploads := c.completeBuild(ctx, request).GetLayerUploads()
		if len(uploads) == 0 {
			return
		}
		if round > maxPublishRounds {
			fail(fmt.Sprintf("%d layers were still unconverted after %d rounds", len(uploads), maxPublishRounds), true)
			return
		}
		converted, uploaded, err := layers.answer(work, uploads)
		if err != nil {
			// Only a layer's content fails the image; a store or registry
			// that stayed unreachable fails this build alone.
			fail(err.Error(), !errors.Is(err, errLayerContent))
			return
		}
		request.ConvertedLayers, request.UploadedLayers = converted, uploaded
	}
}

// completeBuild delivers request, retrying transient failures until the
// agent stops, and returns the answer, or nil when there is none to act
// on. The server's recovery covers an outcome never delivered.
func (c *container) completeBuild(ctx context.Context, request *hostproto.CompleteImageBuildRequest) *hostproto.CompleteImageBuildResponse {
	delay := 100 * time.Millisecond
	for {
		callCtx, cancel := context.WithTimeout(ctx, publishCallTimeout)
		resp, err := c.a.host.CompleteImageBuild(callCtx, request)
		cancel()
		switch {
		case err == nil:
			return resp
		case status.Code(err) == codes.FailedPrecondition:
			c.log.Info("discarding outcome of a finished build")
			return nil
		case !retryable(err):
			c.log.Error("completing build failed", "error", err)
			return nil
		}
		c.log.Warn("completing build failed; retrying", "error", err, "retry_in", delay)
		if !sleep(ctx, delay) {
			return nil
		}
		delay = min(2*delay, maxCompleteBackoff)
	}
}

// removeBuildContainers removes build containers a previous agent left. A
// build cannot resume, so the server starts it again in a new container.
func (a *Agent) removeBuildContainers(ctx context.Context) error {
	list, err := a.docker.ContainerList(ctx, client.ContainerListOptions{
		All:     true,
		Filters: client.Filters{}.Add("label", labelBuildHost+"="+a.identity.HostID),
	})
	if err != nil {
		return fmt.Errorf("list build containers: %w", err)
	}
	for _, summary := range list.Items {
		if err := a.removeContainer(ctx, summary.ID); err != nil {
			return err
		}
	}
	return nil
}

// removeHostContainers removes every container the agent created for this
// host, running or not.
func (a *Agent) removeHostContainers(ctx context.Context) error {
	list, err := a.docker.ContainerList(ctx, client.ContainerListOptions{
		All:     true,
		Filters: client.Filters{}.Add("label", labelHost+"="+a.identity.HostID),
	})
	if err != nil {
		return fmt.Errorf("list containers: %w", err)
	}
	for _, summary := range list.Items {
		if err := a.removeContainer(ctx, summary.ID); err != nil {
			return err
		}
	}
	return a.removeBuildContainers(ctx)
}

// pullOptions carries a login and platform to one pull.
func pullOptions(auth *hostproto.RegistryAuth, platform string) (client.ImagePullOptions, error) {
	var options client.ImagePullOptions
	if auth != nil {
		encoded, err := authconfig.Encode(registry.AuthConfig{
			Username: auth.GetUsername(), Password: auth.GetPassword(), IdentityToken: auth.GetIdentityToken(),
		})
		if err != nil {
			return options, fmt.Errorf("encode registry login: %w", err)
		}
		options.RegistryAuth = encoded
	}
	if platform != "" {
		os, arch, ok := strings.Cut(platform, "/")
		if !ok {
			return options, fmt.Errorf("platform %q is not os/arch", platform)
		}
		options.Platforms = []ocispec.Platform{{OS: os, Architecture: arch}}
	}
	return options, nil
}

// buildLogs sends a build container's output in ordered batches.
type buildLogs struct {
	host      hostproto.HostServiceClient
	container string
	log       *slog.Logger

	mu      sync.Mutex
	lines   []*hostproto.BuildLogLine
	bytes   int
	closed  bool
	changed chan struct{}
	done    chan struct{}
}

func newBuildLogs(host hostproto.HostServiceClient, container string, log *slog.Logger) *buildLogs {
	return &buildLogs{host: host, container: container, log: log, changed: make(chan struct{}), done: make(chan struct{})}
}

func (b *buildLogs) signal() {
	close(b.changed)
	b.changed = make(chan struct{})
}

// add queues a line, waiting while too much output is unsent.
func (b *buildLogs) add(line string) {
	b.mu.Lock()
	for b.bytes >= buildLogBuffer && !b.closed {
		changed := b.changed
		b.mu.Unlock()
		select {
		case <-changed:
		case <-b.done:
			return
		}
		b.mu.Lock()
	}
	b.lines = append(b.lines, &hostproto.BuildLogLine{Data: line, Time: timestamppb.Now()})
	b.bytes += len(line)
	b.signal()
	b.mu.Unlock()
}

// close sends what is queued and waits for run to finish.
func (b *buildLogs) close() {
	b.mu.Lock()
	b.closed = true
	b.signal()
	b.mu.Unlock()
	<-b.done
}

// run sends batches until close, or until ctx ends.
func (b *buildLogs) run(ctx context.Context) {
	defer close(b.done)
	for {
		b.mu.Lock()
		pending, closed, changed := len(b.lines), b.closed, b.changed
		b.mu.Unlock()
		if pending == 0 {
			if closed {
				return
			}
			select {
			case <-ctx.Done():
				return
			case <-changed:
			}
			continue
		}
		if !closed && !sleep(ctx, buildLogFlush) {
			return
		}
		b.mu.Lock()
		var batch []*hostproto.BuildLogLine
		size := 0
		for _, line := range b.lines {
			if len(batch) > 0 && size+len(line.GetData()) > buildLogBatchBytes {
				break
			}
			batch = append(batch, line)
			size += len(line.GetData())
		}
		b.mu.Unlock()
		b.send(ctx, batch)
		b.mu.Lock()
		b.lines = b.lines[len(batch):]
		b.bytes -= size
		b.signal()
		b.mu.Unlock()
	}
}

func (b *buildLogs) send(ctx context.Context, batch []*hostproto.BuildLogLine) {
	request := &hostproto.AppendImageBuildLogsRequest{ContainerId: b.container, Lines: batch}
	delay := 100 * time.Millisecond
	for {
		_, err := b.host.AppendImageBuildLogs(ctx, request)
		if err == nil || ctx.Err() != nil {
			return
		}
		if !retryable(err) {
			b.log.Warn("dropping build output", "lines", len(batch), "error", err)
			return
		}
		if !sleep(ctx, delay) {
			return
		}
		delay = min(2*delay, 5*time.Second)
	}
}
