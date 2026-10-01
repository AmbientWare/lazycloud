package images_test

import (
	"context"
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
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/scheduling"
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
	registry  string
	listener  *database.Listener
}

func newFixture(t *testing.T) fixture {
	t.Helper()
	registry := startRegistry(t)
	pool := dbtest.New(t)
	exec := execution.NewExecution(pool)
	im := images.NewImages(pool, exec, images.Config{
		Registry: registry, Repository: "lazycloud", Insecure: true,
		ManagedBase: registry + "/library/python:{version}-slim",
	})
	pushRandom(t, registry+"/library/python:3.12-slim")
	listener := database.NewListener(pool, slog.New(slog.DiscardHandler), database.ChannelImageBuild, database.ChannelImageBuildLog)
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Go(func() { _ = listener.Run(ctx) })
	t.Cleanup(func() { cancel(); wg.Wait() })
	return fixture{pool: pool, images: im, execution: exec, registry: registry, listener: listener}
}

func (f fixture) workspace(t *testing.T, name string) identity.WorkspaceID {
	t.Helper()
	var id uuid.UUID
	if err := f.pool.QueryRow(t.Context(), "insert into workspaces (name) values ($1) returning id", name).Scan(&id); err != nil {
		t.Fatal(err)
	}
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
	digest := pushRandom(t, f.registry+"/lazycloud/images:upload")
	if err := f.images.CompleteBuild(t.Context(), host, container, images.BuildOutcome{Digest: digest}); err != nil {
		t.Fatal(err)
	}
	image, err := f.images.Get(t.Context(), ws, r.Image.ID)
	if err != nil || image.Reference == nil || *image.Reference != f.registry+"/lazycloud/images@"+digest {
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
	gpu := apitypes.ImageDefinition{PythonVersion: "3.12", Gpu: ptr("A100")}
	if _, err := f.images.Resolve(t.Context(), ws, gpu); !errors.Is(err, images.ErrUnsupported) {
		t.Fatalf("GPU builds are unsupported: %v", err)
	}
	unreachable := apitypes.ImageDefinition{PythonVersion: "3.12", BaseImage: ptr("registry.invalid/python:3.12")}
	if _, err := f.images.Resolve(t.Context(), ws, unreachable); !errors.Is(err, images.ErrRegistryUnavailable) {
		t.Fatalf("an unreachable registry is unavailable, not invalid: %v", err)
	}
}

func ptr[T any](v T) *T { return &v }
