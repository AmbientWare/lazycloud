package images_test

import (
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/moby/buildkit/frontend/dockerfile/parser"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/scheduling"
)

func dockerfileDef(dockerfile string) apitypes.ImageDefinition {
	return apitypes.ImageDefinition{PythonVersion: "3.12", Dockerfile: &dockerfile}
}

// Every image a build reads is pinned and access-checked, or the definition
// is refused: otherwise a workspace could share an image that copies from a
// private image it cannot read.
func TestDockerfilesCannotNameUncheckedImages(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	refused := map[string]string{
		"COPY --from an image":        "FROM scratch\nCOPY --from=docker.io/acme/private:1 /secret /secret\n",
		"RUN --mount from an image":   "FROM scratch\nRUN --mount=type=bind,from=docker.io/acme/private:1,target=/m true\n",
		"syntax directive":            "# syntax=docker/dockerfile:1\nFROM scratch\n",
		"escape directive":            "# escape=`\nFROM scratch\n",
		"variable base":               "ARG BASE=docker.io/library/python:3.12\nFROM $BASE\n",
		"variable COPY --from":        "ARG SRC=docker.io/acme/private:1\nFROM scratch\nCOPY --from=$SRC /a /a\n",
		"platform registry FROM":      "FROM " + f.registry + "/library/python:3.12-slim\n",
		"loopback registry FROM":      "FROM 127.0.0.1:5000/python:3.12\n",
		"continued COPY --from image": "FROM scratch\nCOPY \\\n  --from=docker.io/acme/private:1 /a /a\n",
	}
	for name, dockerfile := range refused {
		var invalid *images.InvalidError
		if _, err := f.images.Resolve(t.Context(), ws, dockerfileDef(dockerfile)); !errors.As(err, &invalid) {
			t.Errorf("%s: want the definition refused, got %v", name, err)
		}
	}
	stages := "FROM scratch AS tools\nFROM scratch\nCOPY --from=tools /a /a\nRUN --mount=type=bind,from=tools,target=/t true\n"
	if _, err := f.images.Resolve(t.Context(), ws, dockerfileDef(stages)); err != nil {
		t.Fatalf("copying from an earlier stage is allowed: %v", err)
	}
}

// Values written into the Dockerfile cannot add instructions to it.
func TestDefinitionValuesCannotInjectInstructions(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	injected := "\nFROM docker.io/acme/private:1\nCOPY . /"
	for name, def := range map[string]apitypes.ImageDefinition{
		"package":    {PythonVersion: "3.12", PythonPackages: &[]string{"six" + injected}},
		"step arg":   {PythonVersion: "3.12", Micromamba: ptr(true), Steps: &[]apitypes.ImageStep{{Kind: apitypes.Micromamba, Args: &[]string{"numpy" + injected}}}},
		"base image": {PythonVersion: "3.12", BaseImage: ptr("docker.io/library/python:3.12" + injected)},
	} {
		var invalid *images.InvalidError
		if _, err := f.images.Resolve(t.Context(), ws, def); !errors.As(err, &invalid) {
			t.Errorf("%s with a line break: want the definition refused, got %v", name, err)
		}
	}

	// A command is a heredoc body, so its lines stay one shell step.
	command := apitypes.ImageDefinition{PythonVersion: "3.12", Commands: &[]string{"echo one" + injected + "\nLAZYCLOUD_STEP\nENV X=1"}}
	r, err := f.images.Resolve(t.Context(), ws, command)
	if err != nil {
		t.Fatal(err)
	}
	var dockerfile string
	if err := f.pool.QueryRow(t.Context(), "select dockerfile from images where id = $1", r.Image.ID).Scan(&dockerfile); err != nil {
		t.Fatal(err)
	}
	parsed, err := parser.Parse(strings.NewReader(dockerfile))
	if err != nil {
		t.Fatal(err)
	}
	var instructions []string
	for _, node := range parsed.AST.Children {
		instructions = append(instructions, strings.ToUpper(node.Value))
	}
	if strings.Join(instructions, " ") != "FROM RUN" {
		t.Fatalf("want FROM and one RUN, got %v in:\n%s", instructions, dockerfile)
	}
}

// Images in the platform registry are reached by image id only, and user
// registries must be public.
func TestBaseImagesMustBePublicRegistries(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	digest := pushRandom(t, f.registry+"/lazycloud/images:someone-else")
	for _, base := range []string{
		f.registry + "/lazycloud/images@" + digest,
		"127.0.0.1:5000/python:3.12",
		"[::1]:5000/python:3.12",
		"10.1.2.3/python:3.12",
		"169.254.169.254/latest:meta-data",
		"localhost:5000/python:3.12",
	} {
		var invalid *images.InvalidError
		def := apitypes.ImageDefinition{PythonVersion: "3.12", BaseImage: ptr(base)}
		if _, err := f.images.Resolve(t.Context(), ws, def); !errors.As(err, &invalid) {
			t.Errorf("base %s: want the definition refused, got %v", base, err)
		}
	}
}

// A registry's token realm is dialed with the same address check, so a
// registry cannot point lookups at internal services.
func TestRegistryLookupsNeverDialPrivateAddresses(t *testing.T) {
	var realmHits atomic.Int32
	realm := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		realmHits.Add(1)
		_, _ = w.Write([]byte(`{"token":"t"}`))
	}))
	t.Cleanup(realm.Close)
	registry := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		// A name, not an address: only the dialed address shows it is private.
		w.Header().Set("WWW-Authenticate", fmt.Sprintf(`Bearer realm="%s/token",service="registry"`, strings.Replace(realm.URL, "127.0.0.1", "localhost", 1)))
		w.WriteHeader(http.StatusUnauthorized)
	}))
	t.Cleanup(registry.Close)
	host := strings.TrimPrefix(registry.URL, "http://")
	pool := dbtest.New(t)
	im := images.NewImages(pool, execution.NewExecution(pool), images.Config{
		Registry: host, Repository: "lazycloud", Insecure: true, ManagedBase: host + "/library/python:{version}-slim",
	})
	ws := fixture{pool: pool}.workspace(t, "a")
	_, err := im.Resolve(t.Context(), ws, apitypes.ImageDefinition{PythonVersion: "3.12"})
	var invalid *images.InvalidError
	if !errors.As(err, &invalid) || !strings.Contains(err.Error(), "private address") {
		t.Fatalf("want the realm on a private address refused, got %v", err)
	}
	if realmHits.Load() != 0 {
		t.Fatal("the realm was dialed")
	}
}

// A forced rebuild replaces the image for the workspace that asked; others
// keep the published image, and caches are never shared across workspaces.
func TestForcedRebuildsAndCachesStayInTheirWorkspace(t *testing.T) {
	f := newFixture(t)
	a, b := f.workspace(t, "a"), f.workspace(t, "b")
	host := f.host(t)
	first, err := f.images.Build(t.Context(), a, numpy(), false)
	if err != nil {
		t.Fatal(err)
	}
	container := placeAndStart(t, f, host)
	published := pushRandom(t, f.registry+"/lazycloud/images:first")
	if err := f.images.CompleteBuild(t.Context(), host, container, images.BuildOutcome{Digest: published}); err != nil {
		t.Fatal(err)
	}
	if _, err := f.images.Resolve(t.Context(), b, numpy()); err != nil {
		t.Fatal(err)
	}

	forced, err := f.images.Build(t.Context(), a, numpy(), true)
	if err != nil || forced.Build == nil || forced.Build.ID == first.Build.ID {
		t.Fatalf("force starts a new build: %+v %v", forced, err)
	}
	joined, err := f.images.Build(t.Context(), b, numpy(), false)
	if err != nil || joined.Build != nil || joined.Image.Reference == nil {
		t.Fatalf("another workspace keeps the published image and does not join: %+v %v", joined, err)
	}
	other := numpy()
	other.Env = &map[string]string{"B": "1"}
	if _, err := f.images.Build(t.Context(), b, other, false); err != nil {
		t.Fatal(err)
	}
	if _, err := scheduling.NewScheduling(f.pool, slog.New(slog.DiscardHandler)).Place(t.Context()); err != nil {
		t.Fatal(err)
	}
	starts, err := f.execution.BuildStarts(t.Context(), host)
	if err != nil || len(starts) != 2 {
		t.Fatalf("want both builds starting, got %v %v", starts, err)
	}
	caches := map[string]bool{}
	var forcedContainer execution.ContainerID
	for _, start := range starts {
		command, err := f.images.BuildCommandOf(t.Context(), start)
		if err != nil {
			t.Fatal(err)
		}
		caches[command.CacheRef] = true
		if start.Build == forced.Build.ID {
			forcedContainer = start.Container
		}
	}
	if len(caches) != 2 {
		t.Fatalf("two workspaces' builds on one base use separate caches, got %v", caches)
	}
	if _, err := f.execution.ApplyReport(t.Context(), host, execution.ContainerReport{
		Container: forcedContainer, Phase: execution.ReportReady, ObservedAt: time.Now(),
	}); err != nil {
		t.Fatal(err)
	}
	rebuilt := pushRandom(t, f.registry+"/lazycloud/images:rebuilt")
	if err := f.images.CompleteBuild(t.Context(), host, forcedContainer, images.BuildOutcome{Digest: rebuilt}); err != nil {
		t.Fatal(err)
	}
	refA, err := f.images.Deployable(t.Context(), a, first.Image.ID, "3.12")
	if err != nil || !strings.HasSuffix(refA, rebuilt) {
		t.Fatalf("the forcing workspace deploys its rebuild: %s %v", refA, err)
	}
	refB, err := f.images.Deployable(t.Context(), b, first.Image.ID, "3.12")
	if err != nil || !strings.HasSuffix(refB, published) {
		t.Fatalf("other workspaces keep the published image: %s %v", refB, err)
	}
}

// Stored build output is capped per attempt and the cut is marked once.
func TestBuildOutputIsCappedPerAttempt(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	host := f.host(t)
	r, err := f.images.Build(t.Context(), ws, numpy(), false)
	if err != nil {
		t.Fatal(err)
	}
	container := placeAndStart(t, f, host)
	line := strings.Repeat("x", 16<<10)
	batch := make([]images.LogLine, 64)
	for n := range batch {
		batch[n] = images.LogLine{Data: line, Time: time.Now()}
	}
	// 10 batches of 1 MiB, then more after the cap.
	for range 12 {
		if err := f.images.AppendLogs(t.Context(), host, container, batch); err != nil {
			t.Fatal(err)
		}
	}
	var rows, bytes, markers int
	if err := f.pool.QueryRow(t.Context(), `
select count(*), coalesce(sum(length(data)), 0), count(*) filter (where data like 'build output truncated%')
from image_build_logs where build_id = $1`, r.Build.ID).Scan(&rows, &bytes, &markers); err != nil {
		t.Fatal(err)
	}
	if bytes > 8<<20+1024 || markers != 1 || rows != 513 {
		t.Fatalf("want 512 lines, one marker and at most 8 MiB; got %d rows, %d bytes, %d markers", rows, bytes, markers)
	}
}
