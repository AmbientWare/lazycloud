package images_test

import (
	"context"
	"crypto/rand"
	"errors"
	"log/slog"
	"net/http"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/v1/random"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/scheduling"
	"github.com/AmbientWare/lazycloud/internal/secrets"
)

const registryImage = "registry:3.1.2@sha256:ddf754342cfc8acc51a56d5d0ab6af06826461864460636d8bd5c546dab2a7b8"

// startRegistry runs a Distribution registry on a free loopback port for the
// test and returns its host:port.
func startRegistry(t *testing.T) string {
	t.Helper()
	out, err := exec.CommandContext(t.Context(), "docker", "run", "-d", "--rm", "-p", "127.0.0.1::5000", registryImage).Output()
	if err != nil {
		t.Fatalf("start registry: %v", err)
	}
	id := strings.TrimSpace(string(out))
	t.Cleanup(func() { _ = exec.CommandContext(context.Background(), "docker", "rm", "-f", id).Run() })
	port, err := exec.CommandContext(t.Context(), "docker", "port", id, "5000/tcp").Output()
	if err != nil {
		t.Fatalf("registry port: %v", err)
	}
	address := strings.TrimSpace(strings.Split(string(port), "\n")[0])
	deadline := time.Now().Add(30 * time.Second)
	for {
		resp, err := http.Get("http://" + address + "/v2/") //nolint:noctx // Readiness probe.
		if err == nil {
			_ = resp.Body.Close()
			return address
		}
		if time.Now().After(deadline) {
			t.Fatalf("registry did not start: %v", err)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// pushRandom writes a random image to ref and returns its digest.
func pushRandom(t *testing.T, ref string) string {
	t.Helper()
	img, err := random.Image(256, 1)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := name.ParseReference(ref, name.Insecure)
	if err != nil {
		t.Fatal(err)
	}
	if err := remote.Write(parsed, img, remote.WithContext(t.Context())); err != nil {
		t.Fatalf("push %s: %v", ref, err)
	}
	digest, err := img.Digest()
	if err != nil {
		t.Fatal(err)
	}
	return digest.String()
}

type fixture struct {
	pool      *pgxpool.Pool
	images    *images.Images
	execution *execution.Execution
	secrets   *secrets.Secrets
	registry  string
	listener  *database.Listener
}

// newVault returns a secrets owner with a random master key.
func newVault(t *testing.T, pool *pgxpool.Pool) *secrets.Secrets {
	t.Helper()
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		t.Fatal(err)
	}
	key, err := secrets.NewFileKey(raw)
	if err != nil {
		t.Fatal(err)
	}
	return secrets.NewSecrets(pool, key)
}

func newFixture(t *testing.T) fixture {
	t.Helper()
	registry := startRegistry(t)
	pool := dbtest.New(t)
	exec := execution.NewExecution(pool)
	vault := newVault(t, pool)
	im := images.NewImages(pool, exec, vault, images.Config{
		Registry: registry, Repository: "lazycloud", Insecure: true,
		ManagedBase: registry + "/library/python:{version}-slim",
	})
	pushRandom(t, registry+"/library/python:3.12-slim")
	listener := database.NewListener(pool, slog.New(slog.DiscardHandler), database.ChannelImageBuild, database.ChannelImageBuildLog)
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Go(func() { _ = listener.Run(ctx) })
	t.Cleanup(func() { cancel(); wg.Wait() })
	return fixture{pool: pool, images: im, execution: exec, secrets: vault, registry: registry, listener: listener}
}

func (f fixture) workspace(t *testing.T, name string) identity.WorkspaceID {
	t.Helper()
	var id uuid.UUID
	if err := f.pool.QueryRow(t.Context(), "insert into workspaces (name) values ($1) returning id", name).Scan(&id); err != nil {
		t.Fatal(err)
	}
	dbtest.OwnWorkspaces(t, f.pool)
	return identity.WorkspaceID(id)
}

// host inserts an online host with room for builds.
func (f fixture) host(t *testing.T) compute.HostID {
	t.Helper()
	var id uuid.UUID
	err := f.pool.QueryRow(t.Context(), `
insert into hosts (name, token_hash, state, cpu_millis, memory_bytes, last_seen_at)
values ('h', sha256(gen_random_uuid()::text::bytea), 'online', 8000, 1::bigint << 34, now()) returning id`).Scan(&id)
	if err != nil {
		t.Fatal(err)
	}
	return compute.HostID(id)
}

// imageRepository is where builds of image id push: its own repository,
// named by its digest.
func (f fixture) imageRepository(t *testing.T, id string) string {
	t.Helper()
	var digest string
	if err := f.pool.QueryRow(t.Context(), "select encode(digest, 'hex') from images where id = $1", id).Scan(&digest); err != nil {
		t.Fatal(err)
	}
	return f.registry + "/lazycloud/images/" + digest
}

// workspaceImageRepository is where workspace's own builds of image id
// push: forced rebuilds and builds on customer hosts.
func (f fixture) workspaceImageRepository(t *testing.T, workspace identity.WorkspaceID, id string) string {
	t.Helper()
	shared := f.imageRepository(t, id)
	return f.registry + "/lazycloud/workspace-images/" + uuid.UUID(workspace).String() + "/" + shared[strings.LastIndex(shared, "/")+1:]
}

func numpy() apitypes.ImageDefinition {
	return apitypes.ImageDefinition{PythonVersion: "3.12", PythonPackages: &[]string{"numpy"}}
}

func TestEqualDefinitionsShareOneImageAuthorizedPerWorkspace(t *testing.T) {
	f := newFixture(t)
	a, b, c := f.workspace(t, "a"), f.workspace(t, "b"), f.workspace(t, "c")

	plain, err := f.images.Resolve(t.Context(), a, apitypes.ImageDefinition{PythonVersion: "3.12"})
	if err != nil {
		t.Fatal(err)
	}
	if plain.Image.Reference == nil || !strings.HasPrefix(*plain.Image.Reference, f.registry+"/library/python:3.12-slim@sha256:") {
		t.Fatalf("a definition with nothing to build is its base by digest, got %v", plain.Image.Reference)
	}
	first, err := f.images.Build(t.Context(), a, numpy(), false)
	if err != nil {
		t.Fatal(err)
	}
	second, err := f.images.Build(t.Context(), b, numpy(), false)
	if err != nil {
		t.Fatal(err)
	}
	if first.Image.ID != second.Image.ID || first.Build == nil || second.Build == nil || first.Build.ID != second.Build.ID {
		t.Fatalf("both workspaces share one image and one build: %+v %+v", first, second)
	}
	if _, err := f.images.Get(t.Context(), c, first.Image.ID); !errors.Is(err, images.ErrNotFound) {
		t.Fatalf("a workspace that never resolved the image cannot read it: %v", err)
	}
	if _, err := f.images.GetBuild(t.Context(), f.listener, c, first.Build.ID, 0); !errors.Is(err, images.ErrNotFound) {
		t.Fatalf("nor its build: %v", err)
	}
	other := numpy()
	other.Env = &map[string]string{"MODE": "fast"}
	changed, err := f.images.Resolve(t.Context(), a, other)
	if err != nil {
		t.Fatal(err)
	}
	if changed.Image.ID == first.Image.ID {
		t.Fatal("a different definition is a different image")
	}
}

func TestConcurrentBuildRequestsJoinOneBuild(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	const requests = 8
	ids := make(chan uuid.UUID, requests)
	var wg sync.WaitGroup
	for range requests {
		wg.Go(func() {
			r, err := f.images.Build(t.Context(), ws, numpy(), false)
			if err != nil || r.Build == nil {
				t.Errorf("build: %v %+v", err, r)
				return
			}
			ids <- r.Build.ID
		})
	}
	wg.Wait()
	close(ids)
	seen := map[uuid.UUID]bool{}
	for id := range ids {
		seen[id] = true
	}
	var containers int
	if err := f.pool.QueryRow(t.Context(), "select count(*) from containers where image_build_id is not null").Scan(&containers); err != nil {
		t.Fatal(err)
	}
	if len(seen) != 1 || containers != 1 {
		t.Fatalf("got %d builds and %d containers, want 1 and 1", len(seen), containers)
	}
}

// placeAndStart places the build's pending container on host with real
// placement and reports it ready.
func placeAndStart(t *testing.T, f fixture, host compute.HostID) execution.ContainerID {
	t.Helper()
	if _, err := scheduling.NewScheduling(f.pool, slog.New(slog.DiscardHandler)).Place(t.Context()); err != nil {
		t.Fatal(err)
	}
	starts, err := f.execution.BuildStarts(t.Context(), host)
	if err != nil || len(starts) != 1 {
		t.Fatalf("want one build start on the host, got %v %v", starts, err)
	}
	if _, err := f.execution.ApplyReport(t.Context(), host, execution.ContainerReport{
		Container: starts[0].Container, Phase: execution.ReportReady, ObservedAt: time.Now(),
	}); err != nil {
		t.Fatal(err)
	}
	return starts[0].Container
}

func TestCompletedBuildPublishesOnlyAPushedDigest(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	host := f.host(t)
	r, err := f.images.Build(t.Context(), ws, numpy(), false)
	if err != nil {
		t.Fatal(err)
	}
	container := placeAndStart(t, f, host)
	build, err := f.images.GetBuild(t.Context(), f.listener, ws, r.Build.ID, 0)
	if err != nil || build.Phase != images.PhaseBuilding {
		t.Fatalf("a ready build container is building: %+v %v", build, err)
	}
	if err := f.images.AppendLogs(t.Context(), host, container, []images.LogLine{{Data: "#5 [2/2] RUN pip install numpy", Time: time.Now()}}); err != nil {
		t.Fatal(err)
	}

	missing := "sha256:" + strings.Repeat("ab", 32)
	if err := f.images.CompleteBuild(t.Context(), host, container, images.BuildOutcome{Digest: missing}); err == nil {
		t.Fatal("a digest the registry does not hold is rejected")
	}
	if err := f.images.CompleteBuild(t.Context(), f.host(t), container, images.BuildOutcome{Digest: missing}); !errors.Is(err, execution.ErrNotAssigned) {
		t.Fatalf("another host cannot complete the build: %v", err)
	}
	if err := f.images.CompleteBuild(t.Context(), host, container,
		images.BuildOutcome{Digest: pushRandom(t, f.registry+"/lazycloud/images:upload")}); err == nil {
		t.Fatal("a digest pushed outside the image's repository is rejected")
	}
	repository := f.imageRepository(t, r.Image.ID)
	digest := pushRandom(t, repository+":upload")
	if err := f.images.CompleteBuild(t.Context(), host, container, images.BuildOutcome{Digest: digest}); err != nil {
		t.Fatal(err)
	}
	image, err := f.images.Get(t.Context(), ws, r.Image.ID)
	if err != nil || image.Reference == nil || *image.Reference != repository+"@"+digest {
		t.Fatalf("the image is published by digest: %+v %v", image, err)
	}
	if err := f.images.CompleteBuild(t.Context(), host, container, images.BuildOutcome{Failure: "late"}); !errors.Is(err, images.ErrStaleBuild) {
		t.Fatalf("a late outcome is stale: %v", err)
	}
	var lines []string
	if err := f.images.StreamLogs(t.Context(), f.listener, ws, r.Build.ID, 0, true, time.Second, func(batch []images.LogEntry) error {
		for _, e := range batch {
			lines = append(lines, e.Data)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if len(lines) != 1 || lines[0] != "#5 [2/2] RUN pip install numpy" {
		t.Fatalf("a followed log of a finished build ends with its lines, got %q", lines)
	}
	again, err := f.images.Build(t.Context(), ws, numpy(), false)
	if err != nil || again.Build != nil || again.Image.Reference == nil {
		t.Fatalf("a ready image is not built again: %+v %v", again, err)
	}
	var credentials *string
	if err := f.pool.QueryRow(t.Context(), "select registry_auth::text from image_builds where id = $1", r.Build.ID).Scan(&credentials); err != nil || credentials != nil {
		t.Fatalf("registry logins are cleared when a build ends: %v %v", credentials, err)
	}
}

func TestLostBuildContainerRetriesOnceThenFails(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	host := f.host(t)
	logger := slog.New(slog.DiscardHandler)
	r, err := f.images.Build(t.Context(), ws, numpy(), false)
	if err != nil {
		t.Fatal(err)
	}
	for attempt := 1; attempt <= 2; attempt++ {
		container := placeAndStart(t, f, host)
		if _, err := f.execution.ApplyReport(t.Context(), host, execution.ContainerReport{
			Container: container, Phase: execution.ReportExited, ObservedAt: time.Now(),
			Exit: &execution.ContainerExit{Reason: execution.StopCrashed, Message: "builder died"},
		}); err != nil {
			t.Fatal(err)
		}
		if _, err := f.images.Recover(t.Context(), logger); err != nil {
			t.Fatal(err)
		}
		build, err := f.images.GetBuild(t.Context(), f.listener, ws, r.Build.ID, 0)
		if err != nil {
			t.Fatal(err)
		}
		want := images.BuildBuilding
		if attempt == 2 {
			want = images.BuildFailed
		}
		// The first loss starts a second container; the second ends the build.
		if build.Status != want || build.Attempt != 2 {
			t.Fatalf("after loss %d: %+v", attempt, build)
		}
		if attempt == 2 && !strings.Contains(build.Failure, "builder died") {
			t.Fatalf("the failure names the last loss: %q", build.Failure)
		}
	}
	next, err := f.images.Build(t.Context(), ws, numpy(), false)
	if err != nil || next.Build == nil || next.Build.ID == r.Build.ID {
		t.Fatalf("a failed build does not block a new one: %+v %v", next, err)
	}
}

func TestBuildPastItsDeadlineFailsAndStopsItsContainer(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	host := f.host(t)
	r, err := f.images.Build(t.Context(), ws, numpy(), false)
	if err != nil {
		t.Fatal(err)
	}
	container := placeAndStart(t, f, host)
	if _, err := f.pool.Exec(t.Context(), "update image_builds set deadline_at = now() - interval '1 second' where id = $1", r.Build.ID); err != nil {
		t.Fatal(err)
	}
	if n, err := f.images.Recover(t.Context(), slog.New(slog.DiscardHandler)); err != nil || n != 1 {
		t.Fatalf("recover: %d %v", n, err)
	}
	build, err := f.images.GetBuild(t.Context(), f.listener, ws, r.Build.ID, 0)
	if err != nil || build.Status != images.BuildFailed {
		t.Fatalf("the build failed: %+v %v", build, err)
	}
	commands, err := f.execution.HostCommands(t.Context(), host)
	if err != nil || len(commands.Stop) != 1 || commands.Stop[0].Container != container {
		t.Fatalf("the host is told to stop the build container: %+v %v", commands, err)
	}
}

func TestDeployableNeedsAReadyImageForTheRuntime(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	plain, err := f.images.Resolve(t.Context(), ws, apitypes.ImageDefinition{PythonVersion: "3.12"})
	if err != nil {
		t.Fatal(err)
	}
	if ref, err := f.images.Deployable(t.Context(), ws, plain.Image.ID, "3.12"); err != nil || ref != *plain.Image.Reference {
		t.Fatal(err)
	}
	var invalid *images.InvalidError
	if _, err := f.images.Deployable(t.Context(), ws, plain.Image.ID, "3.11"); !errors.As(err, &invalid) {
		t.Fatalf("a runtime of another Python version is rejected: %v", err)
	}
	built, err := f.images.Resolve(t.Context(), ws, numpy())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.images.Deployable(t.Context(), ws, built.Image.ID, "3.12"); !errors.Is(err, images.ErrNotReady) {
		t.Fatalf("an unbuilt image cannot deploy: %v", err)
	}
}

func TestDefinitionsNeedReadableInputs(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	var invalid *images.InvalidError
	// No managed base is published for 3.11.
	missingBase := apitypes.ImageDefinition{PythonVersion: "3.11"}
	if _, err := f.images.Resolve(t.Context(), ws, missingBase); !errors.As(err, &invalid) {
		t.Fatalf("a base that does not exist is rejected: %v", err)
	}
	context := apitypes.ImageDefinition{PythonVersion: "3.12", Context: &apitypes.SourceRef{Sha256: strings.Repeat("0", 64)}}
	if _, err := f.images.Resolve(t.Context(), ws, context); !errors.As(err, &invalid) {
		t.Fatalf("a context the workspace did not upload is rejected: %v", err)
	}
	for _, model := range []string{"A100", "any"} {
		gpu := apitypes.ImageDefinition{PythonVersion: "3.12", Gpu: ptr(model)}
		if _, err := f.images.Resolve(t.Context(), ws, gpu); !errors.As(err, &invalid) {
			t.Fatalf("a GPU build names one model, not %q: %v", model, err)
		}
	}
	secret := apitypes.ImageDefinition{PythonVersion: "3.12", Secrets: &[]string{"TOKEN"}}
	if _, err := f.images.Resolve(t.Context(), ws, secret); !errors.As(err, &invalid) {
		t.Fatalf("a secret the workspace does not have is rejected: %v", err)
	}
	unreachable := apitypes.ImageDefinition{PythonVersion: "3.12", BaseImage: ptr("registry.invalid/python:3.12")}
	if _, err := f.images.Resolve(t.Context(), ws, unreachable); !errors.Is(err, images.ErrRegistryUnavailable) {
		t.Fatalf("an unreachable registry is unavailable, not invalid: %v", err)
	}
}

func ptr[T any](v T) *T { return &v }

// withSecret is a definition with a build step whose RUN reads TOKEN.
func withSecret() apitypes.ImageDefinition {
	return apitypes.ImageDefinition{PythonVersion: "3.12", Commands: &[]string{`test -n "$TOKEN"`}, Secrets: &[]string{"TOKEN", "TOKEN"}}
}

// A build secret is part of the image identity with its workspace and
// version, reaches the build only as a secret mount the build step names,
// and a build whose secret was deleted before it started fails.
func TestBuildSecretsAreMountedAndKeyTheImage(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	a, b := f.workspace(t, "a"), f.workspace(t, "b")
	const value = "s3cr3t-token-value"
	for _, ws := range []identity.WorkspaceID{a, b} {
		if _, err := f.secrets.Set(ctx, ws, "TOKEN", value); err != nil {
			t.Fatal(err)
		}
	}
	inA, err := f.images.Resolve(ctx, a, withSecret())
	if err != nil {
		t.Fatal(err)
	}
	inB, err := f.images.Resolve(ctx, b, withSecret())
	if err != nil {
		t.Fatal(err)
	}
	if inA.Image.ID == inB.Image.ID {
		t.Fatal("another workspace's secret of the same name builds another image")
	}
	var dockerfile string
	if err := f.pool.QueryRow(ctx, "select dockerfile from images where id = $1", inA.Image.ID).Scan(&dockerfile); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(dockerfile, "RUN --mount=type=secret,id=TOKEN,env=TOKEN,required=true <<") || strings.Contains(dockerfile, value) {
		t.Fatalf("the step mounts the secret and the Dockerfile never holds its value:\n%s", dockerfile)
	}
	if _, err := f.secrets.Set(ctx, a, "TOKEN", "rotated"); err != nil {
		t.Fatal(err)
	}
	rotated, err := f.images.Build(ctx, a, withSecret(), false)
	if err != nil {
		t.Fatal(err)
	}
	if rotated.Image.ID == inA.Image.ID || rotated.Build == nil {
		t.Fatalf("a new secret version builds a new image: %+v", rotated)
	}
	// The steps' cache key changes too, so the build runs them again.
	versionsArg := func(id string) string {
		t.Helper()
		var d string
		if err := f.pool.QueryRow(ctx, "select dockerfile from images where id = $1", id).Scan(&d); err != nil {
			t.Fatal(err)
		}
		for _, line := range strings.Split(d, "\n") {
			if strings.HasPrefix(line, "ARG LAZYCLOUD_BUILD_SECRET_VERSIONS=") {
				return line
			}
		}
		t.Fatalf("no secret versions argument in:\n%s", d)
		return ""
	}
	if versionsArg(inA.Image.ID) == versionsArg(rotated.Image.ID) {
		t.Fatal("the secret versions argument follows the secret's version")
	}

	host := f.host(t)
	if _, err := scheduling.NewScheduling(f.pool, slog.New(slog.DiscardHandler)).Place(ctx); err != nil {
		t.Fatal(err)
	}
	starts, err := f.execution.BuildStarts(ctx, host)
	if err != nil || len(starts) != 1 {
		t.Fatalf("want one build start, got %v %v", starts, err)
	}
	command, err := f.images.BuildCommandOf(ctx, host, starts[0])
	if err != nil {
		t.Fatal(err)
	}
	if len(command.Secrets) != 1 || command.Secrets["TOKEN"] != "rotated" || command.GPUs != 0 {
		t.Fatalf("the build command carries the current secret value and no GPU: %+v", command)
	}
	if err := f.secrets.Delete(ctx, a, "TOKEN"); err != nil {
		t.Fatal(err)
	}
	if _, err := f.images.BuildCommandOf(ctx, host, starts[0]); !errors.Is(err, images.ErrStaleBuild) {
		t.Fatalf("a build whose secret is gone does not start: %v", err)
	}
	build, err := f.images.GetBuild(ctx, f.listener, a, rotated.Build.ID, 0)
	if err != nil || build.Status != images.BuildFailed || !strings.Contains(build.Failure, "secret TOKEN was deleted") {
		t.Fatalf("it fails naming the secret: %+v %v", build, err)
	}
}

// A GPU build is admitted like a GPU workload: an account without a card
// may build only on the models its plan allows and holds at most its GPUs.
func TestGPUBuildsAreAdmittedByTheirModelAndGPUs(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	ws := f.workspace(t, "a")
	if _, err := f.pool.Exec(ctx, `
update billing_accounts set complimentary_since = null, payment_method_attached_at = null;
update billing_balances set balance_nanos = 1000000000000`); err != nil {
		t.Fatal(err)
	}
	build := func(model, command string) error {
		def := numpy()
		def.Gpu, def.Commands = ptr(model), &[]string{command}
		_, err := f.images.Build(ctx, ws, def, false)
		return err
	}
	var unoffered *billing.GPUUnavailableError
	if err := build("H100", "true"); !errors.As(err, &unoffered) {
		t.Fatalf("a model not offered yet is refused: %v", err)
	}
	var unpaid *billing.PaymentRequiredError
	if err := build("L40S", "true"); !errors.As(err, &unpaid) {
		t.Fatalf("a model the plan does not allow is refused: %v", err)
	}
	if err := build("L4", "true"); err != nil {
		t.Fatal(err)
	}
	var limit *billing.LimitError
	if err := build("L4", "echo second"); !errors.As(err, &limit) {
		t.Fatalf("a GPU past the plan's GPUs is refused: %v", err)
	}
	var builds int
	if err := f.pool.QueryRow(ctx, "select count(*) from containers where image_build_id is not null and gpu_count = 1").Scan(&builds); err != nil || builds != 1 {
		t.Fatalf("only the admitted build has a container: %d %v", builds, err)
	}
}

// A GPU build waits for a host with its model, holds one of its GPUs and
// asks its steps for it.
func TestGPUBuildsArePlacedOnlyOnTheirModel(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	ws := f.workspace(t, "a")
	def := numpy()
	def.Gpu = ptr("L4")
	r, err := f.images.Build(ctx, ws, def, false)
	if err != nil {
		t.Fatal(err)
	}
	if plain, err := f.images.Resolve(ctx, ws, numpy()); err != nil || plain.Image.ID == r.Image.ID {
		t.Fatalf("the GPU model is part of the image identity: %v", err)
	}
	var dockerfile string
	if err := f.pool.QueryRow(ctx, "select dockerfile from images where id = $1", r.Image.ID).Scan(&dockerfile); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(dockerfile, "RUN --device=nvidia.com/gpu=* ") {
		t.Fatalf("the build steps ask for the GPU:\n%s", dockerfile)
	}
	place := func() {
		t.Helper()
		if _, err := scheduling.NewScheduling(f.pool, slog.New(slog.DiscardHandler)).Place(ctx); err != nil {
			t.Fatal(err)
		}
	}
	gpuHost := func(model string) compute.HostID {
		t.Helper()
		var id uuid.UUID
		if err := f.pool.QueryRow(ctx, `
insert into hosts (name, token_hash, state, cpu_millis, memory_bytes, gpu_type, gpu_count, last_seen_at)
values ('g', sha256(gen_random_uuid()::text::bytea), 'online', 8000, 1::bigint << 34, $1, 1, now()) returning id`, model).Scan(&id); err != nil {
			t.Fatal(err)
		}
		return compute.HostID(id)
	}
	cpu, other := f.host(t), gpuHost("T4")
	place()
	for _, host := range []compute.HostID{cpu, other} {
		if starts, err := f.execution.BuildStarts(ctx, host); err != nil || len(starts) != 0 {
			t.Fatalf("a GPU build never lands on a host without its model: %v %v", starts, err)
		}
	}
	l4 := gpuHost("L4")
	place()
	starts, err := f.execution.BuildStarts(ctx, l4)
	if err != nil || len(starts) != 1 {
		t.Fatalf("the build is placed on the L4 host: %v %v", starts, err)
	}
	command, err := f.images.BuildCommandOf(ctx, l4, starts[0])
	if err != nil || command.GPUs != 1 {
		t.Fatalf("the build holds one GPU: %+v %v", command, err)
	}
	var gpuType string
	var gpuCount int
	if err := f.pool.QueryRow(ctx, "select gpu_type, gpu_count from containers where id = $1", uuid.UUID(starts[0].Container)).Scan(&gpuType, &gpuCount); err != nil {
		t.Fatal(err)
	}
	if gpuType != "L4" || gpuCount != 1 {
		t.Fatalf("billing sees the build's GPU: %s x%d", gpuType, gpuCount)
	}
	// The host's one GPU is held: a second GPU build waits.
	second := def
	second.Commands = &[]string{"true"}
	if _, err := f.images.Build(ctx, ws, second, false); err != nil {
		t.Fatal(err)
	}
	place()
	if starts, err := f.execution.BuildStarts(ctx, l4); err != nil || len(starts) != 1 {
		t.Fatalf("the host's one GPU holds one build: %v %v", starts, err)
	}
}
