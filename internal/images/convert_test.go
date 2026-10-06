package images_test

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
)

// Concurrent first starts that need the managed image pin its template
// once and wait for the server's conversion, which no host runs; once it
// is converted every start pulls the copy from the platform registry, and
// it stays live with no release pinning it.
func TestManagedImageIsConvertedByTheServer(t *testing.T) {
	f := newFixture(t)
	host := f.host(t)

	const starts = 8
	waits := make(chan images.PlatformWaitError, starts)
	var wg sync.WaitGroup
	for range starts {
		wg.Go(func() {
			_, err := f.images.ManagedPull(t.Context(), host, "3.12")
			var waiting *images.PlatformWaitError
			if !errors.As(err, &waiting) || !errors.Is(err, images.ErrNotReady) {
				t.Errorf("an unconverted managed image is waited for, got %v", err)
				return
			}
			waits <- *waiting
		})
	}
	wg.Wait()
	close(waits)
	seen := map[images.PlatformWaitError]bool{}
	for w := range waits {
		seen[w] = true
	}
	digest, err := remote.Head(mustRef(t, f.registry+"/library/python:3.12-slim"), remote.WithContext(t.Context()))
	if err != nil {
		t.Fatal(err)
	}
	source := f.registry + "/library/python:3.12-slim@" + digest.Digest.String()
	want := images.PlatformWaitError{Reference: source, Architecture: "amd64"}
	if len(seen) != 1 || !seen[want] {
		t.Fatalf("concurrent first starts waited on %v, want %v", seen, want)
	}
	if n := f.count(t, "select count(*) from managed_images"); n != 1 {
		t.Fatalf("%d managed images recorded, want 1", n)
	}
	if n := f.count(t, "select count(*) from image_builds"); n != 0 {
		t.Fatalf("%d builds started for the managed image", n)
	}

	// The template moves on; the recorded image stays.
	pushRandom(t, f.registry+"/library/python:3.12-slim")
	if err := f.images.ConvertPlatformImage(t.Context(), source, "amd64"); err != nil {
		t.Fatal(err)
	}
	pull, err := f.images.ManagedPull(t.Context(), host, "3.12")
	if err != nil || !strings.HasPrefix(pull.Reference, f.registry+"/lazycloud/platform/") || pull.Platform != "linux/amd64" {
		t.Fatalf("every start pulls the converted copy: %+v %v", pull, err)
	}
	if _, err := f.images.LayerReadURLs(t.Context(), pull.Reference, host, time.Minute); err != nil {
		t.Fatalf("the managed image is readable: %v", err)
	}
	if _, err := f.images.SweepLayers(t.Context(), slog.New(slog.DiscardHandler)); err != nil {
		t.Fatal(err)
	}
	if n := f.count(t, "select count(*) from image_layers where unreferenced_since is not null"); n != 0 {
		t.Fatalf("%d of the managed image's pairs started their grace period", n)
	}
	if _, err := f.images.ManagedPull(t.Context(), host, "3.9"); err == nil {
		t.Fatal("a Python version without a managed image was pulled")
	}
}

// A server that pins the managed image while another server's pin of it
// commits gets the other's source.
func TestConcurrentPinsOfTheManagedImageAgree(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	tx, err := f.pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = tx.Rollback(context.WithoutCancel(ctx)) }()
	other := f.registry + "/library/python:3.12-slim@sha256:" + hex64("e")
	if _, err := tx.Exec(ctx, "insert into managed_images (python_version, template, source) values ('3.12', $1, $2)",
		f.registry+"/library/python:{version}-slim", other); err != nil {
		t.Fatal(err)
	}
	type pinned struct {
		source string
		err    error
	}
	done := make(chan pinned, 1)
	go func() {
		source, err := f.images.ManagedSource(ctx, "3.12")
		done <- pinned{source, err}
	}()
	time.Sleep(500 * time.Millisecond)
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	if got := <-done; got.err != nil || got.source != other {
		t.Fatalf("the pin gave %q (%v), want the committed %q", got.source, got.err, other)
	}
}

func mustRef(t *testing.T, ref string) name.Reference {
	t.Helper()
	parsed, err := name.ParseReference(ref, name.Insecure)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func TestDeployConvertsAStoredReferenceWithoutLayers(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	r, err := f.images.Resolve(t.Context(), ws, numpy())
	if err != nil {
		t.Fatal(err)
	}
	old := f.registry + "/library/python@" + pushRandom(t, f.registry+"/library/python:old")
	if _, err := f.pool.Exec(t.Context(), "update images set reference = $1, ready_at = now() where id = $2", old, r.Image.ID); err != nil {
		t.Fatal(err)
	}
	if image, err := f.images.Get(t.Context(), ws, r.Image.ID); err != nil || image.Reference != nil {
		t.Fatalf("an unconverted image reads as unpublished: %+v %v", image, err)
	}
	var waits []uuid.UUID
	for range 2 {
		_, err := f.images.Deployable(t.Context(), ws, r.Image.ID, "3.12")
		var waiting *images.BuildWaitError
		if !errors.As(err, &waiting) || !errors.Is(err, images.ErrNotReady) {
			t.Fatalf("deploying an unconverted reference waits for its conversion: %v", err)
		}
		waits = append(waits, waiting.Build)
	}
	// Preparing the image by id follows the same build.
	if prepared, err := f.images.Prepare(t.Context(), ws, r.Image.ID); err != nil || prepared.Build == nil || prepared.Build.ID != waits[0] {
		t.Fatalf("preparing the image returns its conversion build: %+v %v", prepared, err)
	}
	if waits[0] != waits[1] || f.count(t, "select count(*) from image_builds where mirror and state = 'building'") != 1 {
		t.Fatalf("repeated deploys wait on builds %v", waits)
	}
	var mirrorID string
	if err := f.pool.QueryRow(t.Context(), "select i.id from images i join image_builds b on b.image_digest = i.digest where b.id = $1", waits[0]).Scan(&mirrorID); err != nil {
		t.Fatal(err)
	}
	parsed, err := name.ParseReference(old, name.Insecure)
	if err != nil {
		t.Fatal(err)
	}
	original, err := remote.Image(parsed, remote.WithContext(t.Context()))
	if err != nil {
		t.Fatal(err)
	}
	host := f.host(t)
	repository := f.imageRepository(t, mirrorID)
	f.publish(t, host, placeAndStart(t, f, host), repository, pushImage(t, repository+":mirror", original))
	if prepared, err := f.images.Prepare(t.Context(), ws, r.Image.ID); err != nil || prepared.Build != nil || prepared.Image.Reference == nil {
		t.Fatalf("a converted image prepares at once: %+v %v", prepared, err)
	}
	if ref, err := f.images.Deployable(t.Context(), ws, r.Image.ID, "3.12"); err != nil || ref != old {
		t.Fatalf("the converted reference deploys: %s %v", ref, err)
	}
	if _, err := f.images.LayerReadURLs(t.Context(), old, host, time.Minute); err != nil {
		t.Fatal(err)
	}
}

// A mirror build whose failure says nothing of the image is built again by
// the next request, while a failure of its content holds requests off.
func TestTransientMirrorFailuresBuildAgain(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	host := f.host(t)
	r, err := f.images.Resolve(t.Context(), ws, numpy())
	if err != nil {
		t.Fatal(err)
	}
	reference := f.registry + "/lazycloud/images/old@" + pushRandom(t, f.registry+"/lazycloud/images/old:t")
	wait := func() uuid.UUID {
		t.Helper()
		_, err := f.images.ConvertedPull(t.Context(), ws, r.Image.ID, reference)
		var waiting *images.BuildWaitError
		if !errors.As(err, &waiting) {
			t.Fatalf("want a wait, got %v", err)
		}
		return waiting.Build
	}
	first := wait()
	if _, err := f.images.CompleteBuild(t.Context(), host, placeAndStart(t, f, host),
		images.BuildOutcome{Failure: "the store stayed unreachable", Transient: true}); err != nil {
		t.Fatal(err)
	}
	if second := wait(); second == first {
		t.Fatal("a transient failure was not built again")
	}
	if _, err := f.images.CompleteBuild(t.Context(), host, placeAndStart(t, f, host), images.BuildOutcome{Failure: "layer 0 is foreign"}); err != nil {
		t.Fatal(err)
	}
	_, err = f.images.ConvertedPull(t.Context(), ws, r.Image.ID, reference)
	var failed *images.ConversionError
	if !errors.As(err, &failed) {
		t.Fatalf("a content failure holds requests off: %v", err)
	}
}

// A connected account's workspace converts a mirror on a platform host,
// into pairs every workspace may use.
func TestMirrorBuildsRunOnPlatformHosts(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	if _, err := f.pool.Exec(t.Context(), `
with owner as (insert into users (email) values ('owner@example.com') returning id),
     conn as (insert into cloud_connections (account_id, aws_account_id, phase) select id, '123456789012', 'ready' from owner returning id)
update workspaces set connection_id = (select id from conn) where id = $1`, uuid.UUID(ws)); err != nil {
		t.Fatal(err)
	}
	r, err := f.images.Resolve(t.Context(), ws, numpy())
	if err != nil {
		t.Fatal(err)
	}
	reference := f.registry + "/lazycloud/images/old@" + pushRandom(t, f.registry+"/lazycloud/images/old:m")
	if _, err := f.images.ConvertedPull(t.Context(), ws, r.Image.ID, reference); !errors.Is(err, images.ErrNotReady) {
		t.Fatalf("want a wait, got %v", err)
	}
	var forced, mirror bool
	if err := f.pool.QueryRow(t.Context(), "select forced, mirror from image_builds").Scan(&forced, &mirror); err != nil {
		t.Fatal(err)
	}
	if forced || !mirror {
		t.Fatalf("a mirror build is forced %v, mirror %v", forced, mirror)
	}
	// Placement puts it on the platform host, not the account's.
	host := f.host(t)
	repository := f.imageRepository(t, mirrorOf(t, f))
	f.publish(t, host, placeAndStart(t, f, host), repository, pushRandom(t, repository+":m"))
	if n := f.count(t, "select count(*) from image_layers where workspace_id is null"); n != 1 {
		t.Fatalf("the mirror's pair serves every workspace: %d shared pairs", n)
	}
}

// mirrorOf is the image of the only mirror build.
func mirrorOf(t *testing.T, f fixture) string {
	t.Helper()
	var id string
	if err := f.pool.QueryRow(t.Context(), "select i.id from images i join image_builds b on b.image_digest = i.digest where b.mirror").Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

// A filesystem image a host pushed is ready only once the build that
// converts it publishes.
func TestFilesystemImagesPublishConverted(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	repo := f.registry + "/lazycloud/filesystems/" + uuid.UUID(ws).String()
	pushed := repo + "@" + pushRandom(t, repo+":snap")
	id, err := f.images.RegisterFilesystem(t.Context(), ws, pushed, "amd64", "3.12")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.images.Deployable(t.Context(), ws, id, "3.12"); !errors.Is(err, images.ErrNotReady) {
		t.Fatalf("an unconverted filesystem image deploys: %v", err)
	}
	if again, err := f.images.RegisterFilesystem(t.Context(), ws, pushed, "amd64", "3.12"); err != nil || again != id ||
		f.count(t, "select count(*) from image_builds where state = 'building'") != 1 {
		t.Fatalf("registering again joins the one build: %s %v", again, err)
	}
	host := f.host(t)
	// A filesystem is the workspace's own: it converts into its repository.
	repository := f.workspaceImageRepository(t, ws, id)
	parsed, err := name.ParseReference(pushed, name.Insecure)
	if err != nil {
		t.Fatal(err)
	}
	snapshot, err := remote.Image(parsed, remote.WithContext(t.Context()))
	if err != nil {
		t.Fatal(err)
	}
	digest := pushImage(t, repository+":mirror", snapshot)
	f.publish(t, host, placeAndStart(t, f, host), repository, digest)
	if ref, err := f.images.Deployable(t.Context(), ws, id, "3.12"); err != nil || ref != repository+"@"+digest {
		t.Fatalf("the converted filesystem image deploys: %s %v", ref, err)
	}
	if n := f.count(t, "select count(*) from image_layers where workspace_id is distinct from $1", uuid.UUID(ws)); n != 0 {
		t.Fatalf("%d of the filesystem's pairs serve other workspaces", n)
	}
}

// A reference a release pinned without layer rows converts on its first
// start: concurrent starts join one build that mirrors exactly that image,
// whose layers then become the reference's. A failed conversion ends the
// wait with a typed error.
func TestPinnedReferencesConvertOnceOrFailTyped(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	host := f.host(t)
	r, err := f.images.Resolve(t.Context(), ws, numpy())
	if err != nil {
		t.Fatal(err)
	}
	pinned := func(tag string) string {
		return f.registry + "/lazycloud/images/old@" + pushRandom(t, f.registry+"/lazycloud/images/old:"+tag)
	}
	reference := pinned("first")

	const starts = 6
	builds := make(chan uuid.UUID, starts)
	var wg sync.WaitGroup
	for range starts {
		wg.Go(func() {
			_, err := f.images.ConvertedPull(t.Context(), ws, r.Image.ID, reference)
			var waiting *images.BuildWaitError
			if !errors.As(err, &waiting) {
				t.Errorf("an unconverted reference is waited for, got %v", err)
				return
			}
			builds <- waiting.Build
		})
	}
	wg.Wait()
	close(builds)
	seen := map[uuid.UUID]bool{}
	for id := range builds {
		seen[id] = true
	}
	if len(seen) != 1 || f.count(t, "select count(*) from containers where image_build_id is not null") != 1 {
		t.Fatalf("concurrent starts waited on %d builds", len(seen))
	}

	// The build mirrors the reference: its push holds the same layers.
	var mirrorID string
	if err := f.pool.QueryRow(t.Context(), "select i.id from images i join image_builds b on b.image_digest = i.digest where b.state = 'building'").Scan(&mirrorID); err != nil {
		t.Fatal(err)
	}
	parsed, err := name.ParseReference(reference, name.Insecure)
	if err != nil {
		t.Fatal(err)
	}
	original, err := remote.Image(parsed, remote.WithContext(t.Context()))
	if err != nil {
		t.Fatal(err)
	}
	repository := f.imageRepository(t, mirrorID)
	f.publish(t, host, placeAndStart(t, f, host), repository, pushImage(t, repository+":mirror", original))
	pull, err := f.images.ConvertedPull(t.Context(), ws, r.Image.ID, reference)
	if err != nil || pull.Reference != reference {
		t.Fatalf("the start pulls the pinned reference once converted: %+v %v", pull, err)
	}
	reads, err := f.images.LayerReadURLs(t.Context(), reference, host, time.Minute)
	if err != nil || len(reads.Layers) != len(diffIDs(t, original)) || reads.Layers[0].DiffID != diffIDs(t, original)[0] {
		t.Fatalf("the pinned reference reads the mirror's layers: %+v %v", reads, err)
	}

	failing := pinned("second")
	_, err = f.images.ConvertedPull(t.Context(), ws, r.Image.ID, failing)
	var waiting *images.BuildWaitError
	if !errors.As(err, &waiting) {
		t.Fatalf("want a wait, got %v", err)
	}
	if _, err := f.images.CompleteBuild(t.Context(), host, placeAndStart(t, f, host), images.BuildOutcome{Failure: "registry gone"}); err != nil {
		t.Fatal(err)
	}
	_, err = f.images.ConvertedPull(t.Context(), ws, r.Image.ID, failing)
	var failed *images.ConversionError
	if !errors.As(err, &failed) || !strings.Contains(failed.Reason, "registry gone") {
		t.Fatalf("a failed conversion ends the wait with its reason, got %v", err)
	}
}

// connect binds ws to a connected AWS account and returns a host of it.
func (f fixture) connect(t *testing.T, ws identity.WorkspaceID) compute.HostID {
	t.Helper()
	var host uuid.UUID
	if err := f.pool.QueryRow(t.Context(), `
with owner as (insert into users (email) values ('owner-' || $1::text || '@example.com') returning id),
     conn as (insert into cloud_connections (account_id, aws_account_id, phase) select id, '123456789012', 'ready' from owner returning id),
     ws as (update workspaces set connection_id = (select id from conn) where id = $1::uuid)
insert into hosts (name, token_hash, state, cpu_millis, memory_bytes, last_seen_at, kind, connection_id)
select 'c', sha256(gen_random_uuid()::text::bytea), 'online', 8000, 1::bigint << 34, now(), 'connection', id from conn
returning id`, uuid.UUID(ws)).Scan(&host); err != nil {
		t.Fatal(err)
	}
	return compute.HostID(host)
}

// A connected workspace's own reference converts on its own hosts, into
// its own repository, in pairs only it may use.
func TestAWorkspacesOwnReferenceConvertsOnItsOwnHosts(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	r, err := f.images.Resolve(t.Context(), ws, numpy())
	if err != nil {
		t.Fatal(err)
	}
	own := f.workspaceImageRepository(t, ws, r.Image.ID)
	reference := own + "@" + pushRandom(t, own+":built")
	if _, err := f.pool.Exec(t.Context(), "update workspace_images set reference = $1, ready_at = now() where workspace_id = $2", reference, uuid.UUID(ws)); err != nil {
		t.Fatal(err)
	}
	f.host(t)
	connected := f.connect(t, ws)
	if _, err := f.images.ConvertedPull(t.Context(), ws, r.Image.ID, reference); !errors.Is(err, images.ErrNotReady) {
		t.Fatalf("want a wait, got %v", err)
	}
	var forced, mirror bool
	if err := f.pool.QueryRow(t.Context(), "select forced, mirror from image_builds").Scan(&forced, &mirror); err != nil {
		t.Fatal(err)
	}
	if !forced || mirror {
		t.Fatalf("an own mirror is forced %v, shared %v", forced, mirror)
	}
	container := placeAndStart(t, f, connected)
	var build uuid.UUID
	if err := f.pool.QueryRow(t.Context(), "select id from image_builds").Scan(&build); err != nil {
		t.Fatal(err)
	}
	command, err := f.images.BuildCommandOf(t.Context(), connected, execution.BuildStart{Container: container, Build: build, Attempt: 1})
	if err != nil {
		t.Fatal(err)
	}
	var mirrorID string
	if err := f.pool.QueryRow(t.Context(), "select i.id from images i join image_builds b on b.image_digest = i.digest").Scan(&mirrorID); err != nil {
		t.Fatal(err)
	}
	repository := f.workspaceImageRepository(t, ws, mirrorID)
	if command.PushRepository != repository {
		t.Fatalf("the mirror pushes to %s, want %s", command.PushRepository, repository)
	}
	parsed, err := name.ParseReference(reference, name.Insecure)
	if err != nil {
		t.Fatal(err)
	}
	original, err := remote.Image(parsed, remote.WithContext(t.Context()))
	if err != nil {
		t.Fatal(err)
	}
	f.publish(t, connected, container, repository, pushImage(t, repository+":mirror", original))
	if pull, err := f.images.ConvertedPull(t.Context(), ws, r.Image.ID, reference); err != nil || pull.Reference != reference {
		t.Fatalf("the reference converts: %+v %v", pull, err)
	}
	if n := f.count(t, "select count(*) from image_layers where workspace_id is distinct from $1", uuid.UUID(ws)); n != 0 {
		t.Fatalf("%d pairs serve other workspaces", n)
	}
	if n := f.count(t, "select count(*) from image_reference_layers where reference like '%/lazycloud/images/%'"); n != 0 {
		t.Fatalf("%d layer rows entered the shared images", n)
	}
}

// An image whose layers were swept while it kept its reference converts
// again by mirroring that reference, never by running its steps again.
func TestASweptImageReconvertsByMirroringItsReference(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	host := f.host(t)
	r, container := f.startBuild(t, ws, numpy(), host)
	repository := f.imageRepository(t, r.Image.ID)
	digest := pushRandom(t, repository+":built")
	f.publish(t, host, container, repository, digest)
	reference := repository + "@" + digest
	if n := f.count(t, "select count(*) from image_reference_uses where reference = $1", reference); n != 1 {
		t.Fatal("a published reference is not recorded as used")
	}
	if _, err := f.pool.Exec(t.Context(), "delete from image_reference_layers where reference = $1", reference); err != nil {
		t.Fatal(err)
	}

	again, err := f.images.Build(t.Context(), ws, numpy(), false)
	if err != nil || again.Build == nil {
		t.Fatalf("a build request converts the image again: %+v %v", again, err)
	}
	var mirror bool
	if err := f.pool.QueryRow(t.Context(), "select mirror from image_builds where id = $1", again.Build.ID).Scan(&mirror); err != nil || !mirror {
		t.Fatalf("the build is a mirror of the reference: %v %v", mirror, err)
	}
	if n := f.count(t, "select count(*) from image_builds b join images i on i.digest = b.image_digest where i.id = $1", r.Image.ID); n != 1 {
		t.Fatalf("the image's steps ran %d times", n)
	}
	mirrorRepository := f.imageRepository(t, mirrorOf(t, f))
	parsed, err := name.ParseReference(reference, name.Insecure)
	if err != nil {
		t.Fatal(err)
	}
	original, err := remote.Image(parsed, remote.WithContext(t.Context()))
	if err != nil {
		t.Fatal(err)
	}
	f.publish(t, host, placeAndStart(t, f, host), mirrorRepository, pushImage(t, mirrorRepository+":mirror", original))
	if ref, err := f.images.Deployable(t.Context(), ws, r.Image.ID, "3.12"); err != nil || ref != reference {
		t.Fatalf("the image deploys its own reference again: %s %v", ref, err)
	}
}

// A mirror build that lost its containers every attempt fails for the
// image, not as a transient failure.
func TestAMirrorThatExhaustsItsAttemptsFailsTheImage(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	host := f.host(t)
	r, err := f.images.Resolve(t.Context(), ws, numpy())
	if err != nil {
		t.Fatal(err)
	}
	reference := f.registry + "/lazycloud/images/old@" + pushRandom(t, f.registry+"/lazycloud/images/old:x")
	if _, err := f.images.ConvertedPull(t.Context(), ws, r.Image.ID, reference); !errors.Is(err, images.ErrNotReady) {
		t.Fatalf("want a wait, got %v", err)
	}
	for range 2 {
		container := placeAndStart(t, f, host)
		if _, err := f.execution.ApplyReport(t.Context(), host, execution.ContainerReport{
			Container: container, Phase: execution.ReportExited, ObservedAt: time.Now(),
			Exit: &execution.ContainerExit{Reason: execution.StopCrashed, Message: "a layer killed the builder"},
		}); err != nil {
			t.Fatal(err)
		}
		if _, err := f.images.Recover(t.Context(), slog.New(slog.DiscardHandler)); err != nil {
			t.Fatal(err)
		}
	}
	_, err = f.images.ConvertedPull(t.Context(), ws, r.Image.ID, reference)
	var failed *images.ConversionError
	if !errors.As(err, &failed) || !strings.Contains(failed.Reason, "a layer killed the builder") {
		t.Fatalf("want the image's failure, got %v", err)
	}
}

// Preparing an image waits only on a build that publishes for the
// workspace: another workspace's connected-account build does not.
func TestPrepareWaitsOnlyOnBuildsForTheWorkspace(t *testing.T) {
	f := newFixture(t)
	a, b := f.workspace(t, "a"), f.workspace(t, "b")
	f.connect(t, b)
	r, err := f.images.Build(t.Context(), b, numpy(), false)
	if err != nil || r.Build == nil {
		t.Fatalf("b builds for itself: %+v %v", r, err)
	}
	if _, err := f.images.Resolve(t.Context(), a, numpy()); err != nil {
		t.Fatal(err)
	}
	if _, err := f.images.Prepare(t.Context(), a, r.Image.ID); !errors.Is(err, images.ErrNotReady) {
		t.Fatalf("a was handed b's build: %v", err)
	}
	if prepared, err := f.images.Prepare(t.Context(), b, r.Image.ID); err != nil || prepared.Build == nil || prepared.Build.ID != r.Build.ID {
		t.Fatalf("b waits on its own build: %+v %v", prepared, err)
	}
}

// A mirror that timed out waiting for a host failed for want of capacity:
// the next request builds it again. One that ran and timed out failed for
// the image.
func TestADeadlineFailsTheImageOnlyIfTheBuildRan(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	r, err := f.images.Resolve(t.Context(), ws, numpy())
	if err != nil {
		t.Fatal(err)
	}
	reference := f.registry + "/lazycloud/images/old@" + pushRandom(t, f.registry+"/lazycloud/images/old:d")
	wait := func() uuid.UUID {
		t.Helper()
		_, err := f.images.ConvertedPull(t.Context(), ws, r.Image.ID, reference)
		var waiting *images.BuildWaitError
		if !errors.As(err, &waiting) {
			t.Fatalf("want a wait, got %v", err)
		}
		return waiting.Build
	}
	expire := func(build uuid.UUID) {
		t.Helper()
		if _, err := f.pool.Exec(t.Context(), "update image_builds set deadline_at = now() - interval '1 second' where id = $1", build); err != nil {
			t.Fatal(err)
		}
		if _, err := f.images.Recover(t.Context(), slog.New(slog.DiscardHandler)); err != nil {
			t.Fatal(err)
		}
	}
	first := wait()
	expire(first)
	second := wait()
	if second == first {
		t.Fatal("a mirror that never reached a host was not built again")
	}
	placeAndStart(t, f, f.host(t))
	expire(second)
	_, err = f.images.ConvertedPull(t.Context(), ws, r.Image.ID, reference)
	var failed *images.ConversionError
	if !errors.As(err, &failed) {
		t.Fatalf("a mirror that ran out of time fails the image: %v", err)
	}
}

// A shared mirror converts an image for every workspace, so it is the
// platform's work: an account that may start nothing of its own still gets
// a public image it stored mirrored, the retry after its container was
// lost runs whatever that account's standing, and the container is neither
// counted nor charged to the workspace that asked.
func TestSharedMirrorsAreThePlatformsWork(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	a, b := f.workspace(t, "a"), f.workspace(t, "b")
	host := f.host(t)
	if _, err := f.pool.Exec(ctx, `
update billing_accounts set complimentary_since = null, payment_method_attached_at = null;
update billing_balances set balance_nanos = 0`); err != nil {
		t.Fatal(err)
	}
	var unpaid *billing.PaymentRequiredError
	if _, err := f.images.Build(ctx, a, numpy(), false); !errors.As(err, &unpaid) {
		t.Fatalf("the account may start no work of its own: %v", err)
	}
	r, err := f.images.Resolve(ctx, a, numpy())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.images.Resolve(ctx, b, numpy()); err != nil {
		t.Fatal(err)
	}
	stored := f.registry + "/library/python@" + pushRandom(t, f.registry+"/library/python:stored")
	if _, err := f.pool.Exec(ctx, "update images set reference = $1, ready_at = now() where id = $2", stored, r.Image.ID); err != nil {
		t.Fatal(err)
	}
	wait := func(ws identity.WorkspaceID) uuid.UUID {
		t.Helper()
		_, err := f.images.ConvertedPull(ctx, ws, r.Image.ID, stored)
		var waiting *images.BuildWaitError
		if !errors.As(err, &waiting) {
			t.Fatalf("the shared mirror is built whatever the account's standing: %v", err)
		}
		return waiting.Build
	}
	build := wait(a)
	if wait(b) != build {
		t.Fatal("another workspace does not join the shared mirror")
	}

	container := placeAndStart(t, f, host)
	if _, err := billing.NewBilling(f.pool, billing.Config{}, slog.New(slog.DiscardHandler)).Meter(ctx); err != nil {
		t.Fatal(err)
	}
	if n := f.count(t, "select coalesce(sum(live_containers), 0)::int from billing_balances"); n != 0 {
		t.Fatalf("accounts hold %d live containers for the mirror", n)
	}
	if n := f.count(t, "select count(*) from usage_cursors where source_id = $1", container); n != 0 {
		t.Fatal("the mirror's container is metered")
	}

	if _, err := f.execution.ApplyReport(ctx, host, execution.ContainerReport{
		Container: container, Phase: execution.ReportExited, ObservedAt: time.Now(),
		Exit: &execution.ContainerExit{Reason: execution.StopCrashed, Message: "the builder crashed"},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := f.images.Recover(ctx, slog.New(slog.DiscardHandler)); err != nil {
		t.Fatal(err)
	}
	if wait(b) != build {
		t.Fatal("the retry failed the mirror for every workspace")
	}
	if n := f.count(t, "select count(*) from containers where image_build_id = $1 and state = 'pending'", build); n != 1 {
		t.Fatalf("%d retry containers, want 1", n)
	}
}
