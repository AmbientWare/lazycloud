// Package images owns image identity, deduplication, build attempts and
// publication. An image is global and content-addressed; a workspace may use
// one after it resolved the image's definition itself. Builds run in
// execution containers placed by scheduling, so images has no scheduler and
// no container lifecycle of its own.
package images

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"net/http"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/cpu"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/secrets"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// BuildTimeout bounds one build across its attempts.
	BuildTimeout = time.Hour
	// maxAttempts is how many containers a build may use. Only a container
	// lost without an outcome earns another; a failed build step does not.
	maxAttempts = 2
	// Build containers reserve this much and may use the host's CPU and up
	// to buildMemoryLimit.
	buildCPUMillis   = cpu.Millis(500)
	buildMemoryBytes = 2 << 30
	buildMemoryLimit = 8 << 30
	maxFailureBytes  = 64 << 10
	// maxLogBytes and maxLogLines bound the output one attempt stores.
	maxLogBytes   = 8 << 20
	maxLogLines   = 100_000
	recoveryBatch = 100
	logBatch      = 500
)

var (
	// ErrNotFound means the workspace has not resolved the image or build.
	ErrNotFound = errors.New("not found")
	// ErrNotReady means the image has not been published yet.
	ErrNotReady = errors.New("image is not ready")
	// ErrUnsupported means the definition asks for something the platform
	// cannot do yet.
	ErrUnsupported = errors.New("unsupported")
	// ErrStaleBuild means the build already finished; its late outcome is
	// discarded.
	ErrStaleBuild = errors.New("the build already finished")
)

// InvalidError rejects a definition.
type InvalidError struct{ Reason string }

func (e *InvalidError) Error() string { return e.Reason }

func invalid(format string, args ...any) error {
	return &InvalidError{Reason: fmt.Sprintf(format, args...)}
}

var manifestDigest = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// Config locates the platform registry builds publish to and the managed
// base images.
type Config struct {
	// Registry is the platform registry host[:port].
	Registry string
	// Repository is the path under Registry for images and the build cache.
	Repository string
	// Insecure registries speak plain HTTP.
	Insecure bool
	// Auth logs in to Registry; nil reads and pushes anonymously.
	Auth *Auth
	// ECR, when set, logs in to Registry, an ECR registry, with tokens minted
	// from these AWS credentials instead of Auth.
	ECR *aws.Config
	// HostRole is the role ECR logins for hosts are minted from, each in a
	// session whose policy names the repositories of one command. It may
	// read and write every repository under Repository.
	HostRole string
	// ManagedBase is the image a definition without a base starts from;
	// {version} is replaced with its Python version.
	ManagedBase string
}

// Repositories under Repository, by what they hold. A built image has a
// repository of its own, named by its digest, which every workspace that
// resolved its definition may pull; caches and filesystem images have one
// per workspace. Host logins name these repositories, so a host can reach
// only what its command needs.
func (c Config) imageRepository(digest []byte) string {
	return c.Repository + "/images/" + hex.EncodeToString(digest)
}

// workspaceImageRepository holds a workspace's own builds of an image: a
// forced rebuild, or any build on a host a customer controls. Only that
// workspace pulls from it.
func (c Config) workspaceImageRepository(workspace uuid.UUID, digest []byte) string {
	return c.Repository + "/workspace-images/" + workspace.String() + "/" + hex.EncodeToString(digest)
}

func (c Config) cacheRepository(workspace uuid.UUID) string {
	return c.Repository + "/cache/" + workspace.String()
}

func (c Config) filesystemRepository(workspace identity.WorkspaceID) string {
	return c.Repository + "/filesystems/" + workspace.String()
}

// repositoryOf is the repository path of reference, an image in Registry,
// and whether it is under Repository.
func (c Config) repositoryOf(reference string) (string, bool) {
	path, ok := strings.CutPrefix(reference, c.Registry+"/")
	if !ok {
		return "", false
	}
	path, _, _ = splitReference(path)
	return path, strings.HasPrefix(path, c.Repository+"/")
}

// buildTarget is where a build pushes and whom its result serves.
type buildTarget struct {
	repository string
	// scoped means the image, and the layers its build converts, are the
	// workspace's alone.
	scoped bool
}

// buildTarget is where a build of digest pushes. A build on a connected
// account's or joined machine's host runs where the customer controls it, so
// only the shared image a platform host built may be published for every
// workspace.
func (c Config) buildTarget(platformHost bool, workspace uuid.UUID, digest []byte, forced bool) buildTarget {
	if forced || !platformHost {
		return buildTarget{repository: c.workspaceImageRepository(workspace, digest), scoped: true}
	}
	return buildTarget{repository: c.imageRepository(digest)}
}

// Images is the images owner.
type Images struct {
	pool      *pgxpool.Pool
	queries   *Queries
	execution *execution.Execution
	// secrets holds the workspace secrets builds read.
	secrets *secrets.Secrets
	// storage holds the converted layers of published images.
	storage  *storage.Storage
	resolver *resolver
	config   Config
	login    *platformLogin
	// transfer stores the layer pairs of platform images.
	transfer *http.Client

	// managed holds the managed sources read, by Python version. The
	// source managed_images records for a version under
	// Config.ManagedBase never changes.
	managedMu sync.Mutex
	managed   map[string]string
}

// NewImages returns the images owner.
func NewImages(pool *pgxpool.Pool, exec *execution.Execution, vault *secrets.Secrets, store *storage.Storage, config Config) *Images {
	return &Images{
		pool: pool, queries: New(pool), execution: exec, secrets: vault, storage: store, resolver: newResolver(config.Registry), config: config,
		login: newPlatformLogin(config), transfer: &http.Client{}, managed: map[string]string{},
	}
}

// Image is a resolved image.
type Image struct {
	ID            string
	PythonVersion string
	Architecture  string
	// Reference is the pullable reference by digest once published, as the
	// workspace that read the image sees it.
	Reference *string
	// globalReference is the reference every workspace without its own
	// rebuild sees.
	globalReference *string
	// unconverted is the reference stored for the workspace while it has no
	// layer rows; a mirror build converts it.
	unconverted *string
	digest      []byte
	CreatedAt   time.Time
	// ReadyAt is when Reference was last published.
	ReadyAt *time.Time
}

// BuildStatus is a build's outcome so far.
type BuildStatus string

const (
	BuildBuilding  BuildStatus = "building"
	BuildSucceeded BuildStatus = "succeeded"
	BuildFailed    BuildStatus = "failed"
)

// BuildPhase is where a running build is.
type BuildPhase string

const (
	PhaseQueued   BuildPhase = "queued"
	PhaseStarting BuildPhase = "starting"
	PhaseBuilding BuildPhase = "building"
	PhaseFinished BuildPhase = "finished"
)

// Build is one build of an image.
type Build struct {
	ID         uuid.UUID
	ImageID    string
	Status     BuildStatus
	Phase      BuildPhase
	Attempt    int
	Failure    string
	CreatedAt  time.Time
	FinishedAt *time.Time
	// traceparent is the trace of the request that started the build.
	traceparent string
}

// Resolution is an image and the build that runs for it, if any.
type Resolution struct {
	Image Image
	Build *Build
}

// prepared is a definition rendered to its identity.
type prepared struct {
	spec       spec
	dockerfile string
	digest     []byte
	id         string
	// auth holds base image logins by registry host.
	auth map[string]Auth
	// secretVersions are the versions of the secrets the build reads, by
	// name.
	secretVersions map[string]string
}

// prepare validates def for workspace, pins its base images and computes its
// identity. Registry lookups happen here, outside any transaction.
func (i *Images) prepare(ctx context.Context, workspace identity.WorkspaceID, def apitypes.ImageDefinition) (prepared, error) {
	s, err := validate(def)
	if err != nil {
		return prepared{}, err
	}
	if s.context != nil {
		ok, err := i.queries.SourceRegistered(ctx, SourceRegisteredParams{WorkspaceID: uuid.UUID(workspace), Sha256: s.context})
		if err != nil {
			return prepared{}, fmt.Errorf("read build context: %w", err)
		}
		if !ok {
			return prepared{}, invalid("context %s is not uploaded to this workspace", hex.EncodeToString(s.context))
		}
	}
	base := s.baseReference(i.config.ManagedBase)
	baseHost, err := registryHost(base)
	if err != nil {
		return prepared{}, err
	}
	var given map[string]string
	if def.BaseImageCredentials != nil {
		given = *def.BaseImageCredentials
	}
	baseAuth, err := registryAuth(ctx, baseHost, given, exchangeECR)
	if err != nil {
		return prepared{}, err
	}
	out := prepared{auth: map[string]Auth{}, secretVersions: map[string]string{}}
	if baseAuth != nil {
		out.auth[baseHost] = *baseAuth
	}
	for _, name := range s.secrets {
		secret, err := i.secrets.Get(ctx, workspace, name)
		var missing *secrets.NotFoundError
		if errors.As(err, &missing) {
			return prepared{}, invalid("secret %s does not exist in this workspace", name)
		}
		if err != nil {
			return prepared{}, fmt.Errorf("read build secret: %w", err)
		}
		out.secretVersions[name] = secret.UpdatedAt.UTC().Format(time.RFC3339Nano)
	}
	if len(out.secretVersions) > 0 {
		versions, err := json.Marshal(out.secretVersions)
		if err != nil {
			return prepared{}, fmt.Errorf("encode secret versions: %w", err)
		}
		sum := sha256.Sum256(versions)
		s.secretVersionsKey = hex.EncodeToString(sum[:16])
	}
	// The managed base is the platform's choice; every other image is the
	// user's and must come from a public registry other than the platform's.
	managed := ""
	if s.managed() {
		managed = base
	}
	pin := func(ref string) (string, error) {
		host, err := registryHost(ref)
		if err != nil {
			return "", err
		}
		if ref != managed {
			if host == i.config.Registry {
				return "", invalid("%s is in the platform registry; use a built image by its id with Image.from_id", ref)
			}
			if err := checkRegistryHost(host); err != nil {
				return "", err
			}
		}
		var auth *Auth
		if host == baseHost {
			auth = baseAuth
		}
		pinned, err := i.pin(ctx, ref, host, auth)
		if err != nil || ref != managed {
			return pinned, err
		}
		return untagged(pinned), nil
	}
	if out.dockerfile, err = s.render(i.config.ManagedBase, pin); err != nil {
		return prepared{}, err
	}
	inputs := buildInputs{Secrets: out.secretVersions, GPU: s.gpu}
	if len(s.secrets) > 0 {
		// Another workspace's secrets of the same names build another image.
		inputs.Workspace = uuid.UUID(workspace).String()
	}
	out.spec = s
	out.digest = imageDigest(out.dockerfile, s.architecture, s.context, inputs)
	out.id = imageID(out.digest)
	return out, nil
}

// pin resolves ref, an image in host, to its digest: with the server's own
// login in the platform registry, and with auth elsewhere.
func (i *Images) pin(ctx context.Context, ref, host string, auth *Auth) (string, error) {
	login, insecure, err := i.registryLogin(ctx, host)
	if err != nil {
		return "", err
	}
	if host == i.config.Registry {
		auth = login
	}
	return i.resolver.pin(ctx, ref, auth, insecure)
}

// registryLogin is the server's login to the registry at host and whether
// that registry speaks plain HTTP: the platform registry's login, and
// anonymous HTTPS for every other.
func (i *Images) registryLogin(ctx context.Context, host string) (*Auth, bool, error) {
	if host != i.config.Registry {
		return nil, false, nil
	}
	auth, err := i.login.auth(ctx)
	return auth, i.config.Insecure, err
}

// layersOf reads the layers of reference, an image by digest, for linux on
// architecture.
func (i *Images) layersOf(ctx context.Context, reference, architecture string) ([]registryLayer, error) {
	host, err := registryHost(reference)
	if err != nil {
		return nil, err
	}
	auth, insecure, err := i.registryLogin(ctx, host)
	if err != nil {
		return nil, err
	}
	return i.resolver.layers(ctx, reference, auth, insecure, architecture)
}

// Resolve computes def's image and grants workspace access to it. It never
// starts a build.
func (i *Images) Resolve(ctx context.Context, workspace identity.WorkspaceID, def apitypes.ImageDefinition) (Resolution, error) {
	p, err := i.prepare(ctx, workspace, def)
	if err != nil {
		return Resolution{}, err
	}
	var out Resolution
	err = pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		image, err := i.upsert(ctx, q, workspace, p)
		if err != nil {
			return err
		}
		out.Image = image
		out.Build, err = i.activeBuild(ctx, q, image, workspace, false)
		return err
	})
	if err != nil {
		return Resolution{}, fmt.Errorf("resolve image: %w", err)
	}
	return out, nil
}

// Build resolves def and starts a build unless the image is ready, or joins
// the build already running for it. force builds a published image again for
// workspace alone: the result replaces the image for that workspace, and
// other workspaces keep the published one.
func (i *Images) Build(ctx context.Context, workspace identity.WorkspaceID, def apitypes.ImageDefinition, force bool) (Resolution, error) {
	p, err := i.prepare(ctx, workspace, def)
	if err != nil {
		return Resolution{}, err
	}
	return i.build(ctx, workspace, p, force, buildDefinition)
}

// buildKind is what a build makes of its Dockerfile.
type buildKind string

const (
	// buildDefinition runs a definition's steps.
	buildDefinition buildKind = "definition"
	// buildSharedMirror copies a public or platform-global image into the
	// platform registry and converts it, on a platform host, for every
	// workspace.
	buildSharedMirror buildKind = "shared_mirror"
	// buildOwnMirror copies one of the workspace's own images and converts
	// it where the workspace's builds run, for that workspace alone.
	buildOwnMirror buildKind = "own_mirror"
)

// buildsAlone reports whether workspace's request for image builds for the
// workspace alone: a forced rebuild of a published image, a mirror of one
// of its own images, or any definition of a workspace whose builds run on
// its connected account's hosts.
func (i *Images) buildsAlone(ctx context.Context, q *Queries, workspace identity.WorkspaceID, image Image, force bool, kind buildKind) (bool, error) {
	if force && image.globalReference != nil {
		return true, nil
	}
	switch kind {
	case buildSharedMirror:
		return false, nil
	case buildOwnMirror:
		return true, nil
	case buildDefinition:
	}
	customer, err := q.WorkspaceOnCustomerHosts(ctx, uuid.UUID(workspace))
	if err != nil {
		return false, fmt.Errorf("read workspace hosts: %w", err)
	}
	return customer, nil
}

// activeBuild is the build of image running for workspace: the global one,
// or with alone the workspace's own. It is nil when none runs.
func (i *Images) activeBuild(ctx context.Context, q *Queries, image Image, workspace identity.WorkspaceID, alone bool) (*Build, error) {
	row, err := q.ActiveBuild(ctx, ActiveBuildParams{ImageDigest: image.digest, Forced: alone, WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil //nolint:nilnil // No build runs.
	}
	if err != nil {
		return nil, fmt.Errorf("read active build: %w", err)
	}
	b, err := i.buildOut(ctx, q, row.ID, image.ID, row.State, row.Failure, row.CreatedAt, row.FinishedAt)
	b.traceparent = deref(row.Traceparent)
	return &b, err
}

// build starts a build of p for workspace unless the image is ready, or
// joins the one running. A definition whose stored reference lost its layer
// rows is converted again by mirroring that reference, not rebuilt.
func (i *Images) build(ctx context.Context, workspace identity.WorkspaceID, p prepared, force bool, kind buildKind) (Resolution, error) {
	var auth []byte
	var err error
	if len(p.auth) > 0 {
		// The logins are stored until the build ends; see image_builds.
		if auth, err = json.Marshal(p.auth); err != nil { //nolint:gosec // The build needs the password.
			return Resolution{}, fmt.Errorf("encode registry logins: %w", err)
		}
	}
	var out Resolution
	reconvert := false
	err = pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		image, err := i.upsert(ctx, q, workspace, p)
		if err != nil {
			return err
		}
		out = Resolution{Image: image}
		// Every image builds, even one that only names its base: the build
		// converts its layers.
		if !force && image.Reference != nil {
			return nil
		}
		if !force && kind == buildDefinition && image.unconverted != nil {
			reconvert = true
			return nil
		}
		alone, err := i.buildsAlone(ctx, q, workspace, image, force, kind)
		if err != nil {
			return err
		}
		if out.Build, err = i.activeBuild(ctx, q, image, workspace, alone); err != nil || out.Build != nil {
			return err
		}
		// The image row lock serializes requests for this image, so no other
		// build can start between the check and the insert.
		row, err := q.InsertBuild(ctx, InsertBuildParams{
			ImageDigest: p.digest, WorkspaceID: uuid.UUID(workspace), Forced: alone, Mirror: kind == buildSharedMirror, ContextSha256: p.spec.context,
			RegistryAuth: auth, TimeoutSeconds: BuildTimeout.Seconds(), Traceparent: traceparent(ctx),
		})
		if err != nil {
			return fmt.Errorf("insert build: %w", err)
		}
		if kind == buildSharedMirror {
			_, err = i.execution.CreatePlatformBuildContainer(ctx, tx, workspace, row.ID, buildCPUMillis, buildMemoryBytes)
		} else {
			_, err = i.execution.CreateBuildContainer(ctx, tx, workspace, row.ID, buildCPUMillis, buildMemoryBytes, p.spec.gpu)
		}
		if err != nil {
			return err
		}
		if err := database.Notify(ctx, tx, database.ChannelImageBuild, row.ID.String()); err != nil {
			return err
		}
		out.Build = &Build{
			ID: row.ID, ImageID: image.ID, Status: BuildBuilding, Phase: PhaseQueued, Attempt: 1, CreatedAt: row.CreatedAt,
			traceparent: telemetry.TraceParentOf(ctx),
		}
		return nil
	})
	if err != nil {
		return Resolution{}, fmt.Errorf("build image: %w", err)
	}
	if reconvert {
		return i.Prepare(ctx, workspace, p.id)
	}
	return out, nil
}

func (i *Images) upsert(ctx context.Context, q *Queries, workspace identity.WorkspaceID, p prepared) (Image, error) {
	versions, err := json.Marshal(p.secretVersions)
	if err != nil {
		return Image{}, fmt.Errorf("encode secret versions: %w", err)
	}
	// A new row is locked by its insert, an existing one by LockImage, so
	// build requests for one image run one at a time.
	id, err := q.InsertImage(ctx, InsertImageParams{
		Digest: p.digest, ID: p.id, Dockerfile: p.dockerfile, PythonVersion: p.spec.python,
		Architecture: p.spec.architecture, BuildSecrets: versions, BuildGpu: p.spec.gpu,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		var row LockImageRow
		row, err = q.LockImage(ctx, p.digest)
		id = row.ID
	}
	if err != nil {
		return Image{}, fmt.Errorf("upsert image: %w", err)
	}
	if err := q.GrantImage(ctx, GrantImageParams{WorkspaceID: uuid.UUID(workspace), ImageDigest: p.digest}); err != nil {
		return Image{}, fmt.Errorf("grant image: %w", err)
	}
	return i.workspaceImage(ctx, q, workspace, id)
}

func (i *Images) workspaceImage(ctx context.Context, q *Queries, workspace identity.WorkspaceID, id string) (Image, error) {
	row, err := q.WorkspaceImage(ctx, WorkspaceImageParams{WorkspaceID: uuid.UUID(workspace), ID: id})
	if errors.Is(err, pgx.ErrNoRows) {
		return Image{}, ErrNotFound
	}
	if err != nil {
		return Image{}, fmt.Errorf("read image: %w", err)
	}
	image := Image{
		ID: row.ID, PythonVersion: row.PythonVersion, Architecture: row.Architecture, digest: row.Digest,
		Reference: row.Reference, globalReference: row.GlobalReference, CreatedAt: row.CreatedAt, ReadyAt: row.ReadyAt,
	}
	// A reference is published only while it has layer rows; without them
	// the image reads as unpublished until a build converts it.
	if !row.Converted {
		image.unconverted, image.Reference, image.ReadyAt = row.Reference, nil, nil
	}
	if !row.GlobalConverted {
		image.globalReference = nil
	}
	return image, nil
}

// Get returns an image the workspace resolved, as that workspace sees it.
func (i *Images) Get(ctx context.Context, workspace identity.WorkspaceID, id string) (Image, error) {
	return i.workspaceImage(ctx, i.queries, workspace, id)
}

// Deployable checks that workspace may run image id with the runtime for
// python, a minor version, and returns the reference a release pins.
func (i *Images) Deployable(ctx context.Context, workspace identity.WorkspaceID, id, python string) (string, error) {
	image, err := i.Get(ctx, workspace, id)
	if err != nil {
		return "", err
	}
	if minorVersion(image.PythonVersion) != python {
		return "", invalid("image %s has Python %s, not %s", id, image.PythonVersion, python)
	}
	if image.Reference != nil {
		return *image.Reference, nil
	}
	if image.unconverted == nil {
		return "", ErrNotReady
	}
	// A stored reference without layer rows converts through a mirror build;
	// until it publishes the deploy gets a BuildWaitError.
	if err := i.convertReference(ctx, workspace, image.PythonVersion, image.Architecture, *image.unconverted); err != nil {
		return "", err
	}
	return *image.unconverted, nil
}

// Pull is what a host needs to pull a ready image.
type Pull struct {
	Reference string
	// Auth logs in to the platform registry; nil for other registries,
	// which are public.
	Auth     *Auth
	Platform string
}

// pull is how a host pulls reference for linux on architecture: from the
// platform registry with a login to its repository alone that stays valid
// for window at least, and from any other registry anonymously.
func (i *Images) pull(ctx context.Context, reference, architecture string, window time.Duration) (Pull, error) {
	pull := Pull{Reference: reference, Platform: "linux/" + architecture}
	if !strings.HasPrefix(reference, i.config.Registry+"/") {
		return pull, nil
	}
	repository, ok := i.config.repositoryOf(reference)
	if !ok {
		return Pull{}, fmt.Errorf("image %s is outside the platform's workload repositories", reference)
	}
	auth, err := i.login.host(ctx, hostAccess{pull: []string{repository}}, time.Now().Add(window))
	if err != nil {
		return Pull{}, err
	}
	pull.Auth = auth
	return pull, nil
}

func (i *Images) buildOut(ctx context.Context, q *Queries, id uuid.UUID, imageID, state string, failure *string, created time.Time, finished *time.Time) (Build, error) {
	b := Build{ID: id, ImageID: imageID, Status: BuildStatus(state), Failure: deref(failure), CreatedAt: created, FinishedAt: finished}
	phase, err := q.BuildPhase(ctx, &id)
	if err != nil {
		return Build{}, fmt.Errorf("read build phase: %w", err)
	}
	b.Attempt = int(phase.Attempts)
	switch {
	case b.Status != BuildBuilding:
		b.Phase = PhaseFinished
	case phase.ContainerState == string(execution.ContainerStarting):
		b.Phase = PhaseStarting
	case phase.ContainerState == string(execution.ContainerReady), phase.ContainerState == string(execution.ContainerDraining):
		b.Phase = PhaseBuilding
	default:
		b.Phase = PhaseQueued
	}
	return b, nil
}

// GetBuild reads a build of an image the workspace resolved, waiting up to
// wait for it to finish.
func (i *Images) GetBuild(ctx context.Context, listener *database.Listener, workspace identity.WorkspaceID, id uuid.UUID, wait time.Duration) (Build, error) {
	var wake <-chan struct{}
	if wait > 0 {
		var cancel func()
		wake, cancel = listener.Subscribe(database.ChannelImageBuild, id.String())
		defer cancel()
	}
	deadline := time.Now().Add(wait)
	for {
		row, err := i.queries.WorkspaceBuild(ctx, WorkspaceBuildParams{WorkspaceID: uuid.UUID(workspace), ID: id})
		if errors.Is(err, pgx.ErrNoRows) {
			return Build{}, ErrNotFound
		}
		if err != nil {
			return Build{}, fmt.Errorf("read build: %w", err)
		}
		build, err := i.buildOut(ctx, i.queries, row.ID, row.ImageID, row.State, row.Failure, row.CreatedAt, row.FinishedAt)
		if err != nil {
			return Build{}, err
		}
		remaining := time.Until(deadline)
		if build.Status != BuildBuilding || remaining <= 0 {
			return build, nil
		}
		timer := time.NewTimer(remaining)
		select {
		case <-wake:
		case <-timer.C:
		case <-ctx.Done():
			timer.Stop()
			return build, nil
		}
		timer.Stop()
	}
}

// LogEntry is one line of build output.
type LogEntry struct {
	ID      int64
	Attempt int
	Data    string
	Time    time.Time
}

// StreamLogs emits the build's output after the cursor in batches. With
// follow it keeps going until the build finishes, and emits an empty batch
// after heartbeat without output.
func (i *Images) StreamLogs(ctx context.Context, listener *database.Listener, workspace identity.WorkspaceID, id uuid.UUID, after int64, follow bool, heartbeat time.Duration, emit func([]LogEntry) error) error {
	var wake, logged <-chan struct{}
	var idle *time.Timer
	if follow {
		var cancel, cancelLogged func()
		wake, cancel = listener.Subscribe(database.ChannelImageBuild, id.String())
		defer cancel()
		logged, cancelLogged = listener.Subscribe(database.ChannelImageBuildLog, id.String())
		defer cancelLogged()
		idle = time.NewTimer(heartbeat)
		defer idle.Stop()
	}
	for {
		// The state is read first: lines written before a build finished are
		// all visible to the reads that follow.
		build, err := i.queries.WorkspaceBuild(ctx, WorkspaceBuildParams{WorkspaceID: uuid.UUID(workspace), ID: id})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("read build: %w", err)
		}
		for {
			rows, err := i.queries.BuildLogsAfter(ctx, BuildLogsAfterParams{BuildID: id, After: after, MaxEntries: logBatch})
			if err != nil {
				return fmt.Errorf("read build logs: %w", err)
			}
			if len(rows) > 0 {
				batch := make([]LogEntry, len(rows))
				for n, row := range rows {
					batch[n] = LogEntry{ID: row.ID, Attempt: int(row.Attempt), Data: row.Data, Time: row.LoggedAt}
				}
				if err := emit(batch); err != nil {
					return err
				}
				if idle != nil {
					idle.Reset(heartbeat)
				}
				after = rows[len(rows)-1].ID
			}
			if len(rows) < logBatch {
				break
			}
		}
		if !follow || BuildStatus(build.State) != BuildBuilding {
			return nil
		}
		select {
		case <-wake:
		case <-logged:
		case <-idle.C:
			if err := emit(nil); err != nil {
				return err
			}
			idle.Reset(heartbeat)
		case <-ctx.Done():
			return nil
		}
	}
}

// BuildCommand is what a host needs to run one build attempt.
type BuildCommand struct {
	Build      uuid.UUID
	Attempt    int
	Dockerfile string
	// Context is the archive the build reads; ContextWorkspace stores it.
	Context          []byte
	ContextWorkspace identity.WorkspaceID
	Platform         string
	PushRepository   string
	CacheRef         string
	Insecure         bool
	// Auth holds logins by registry host, the platform registry included.
	Auth     map[string]Auth
	Deadline time.Time
	// Secrets are the values of the build's secrets by name, current when
	// the build starts.
	Secrets map[string]string
	// GPUs is how many GPUs the build container holds.
	GPUs int
}

// buildGPUs is how many GPUs a build on gpu holds: one card of the model.
func buildGPUs(gpu string) int {
	if gpu == "" {
		return 0
	}
	return 1
}

// BuildCommandOf returns the command for a build container starting on
// host. A build whose secret was deleted since it was requested fails, and
// ErrStaleBuild says so.
func (i *Images) BuildCommandOf(ctx context.Context, host compute.HostID, start execution.BuildStart) (BuildCommand, error) {
	row, err := i.queries.BuildToStart(ctx, BuildToStartParams{ID: start.Build, Host: uuid.UUID(host)})
	if err != nil {
		return BuildCommand{}, fmt.Errorf("read build %s: %w", start.Build, err)
	}
	var versions map[string]string
	if err := json.Unmarshal(row.BuildSecrets, &versions); err != nil {
		return BuildCommand{}, fmt.Errorf("decode build secrets: %w", err)
	}
	values, err := i.secrets.Resolve(ctx, identity.WorkspaceID(row.WorkspaceID), slices.Sorted(maps.Keys(versions)))
	var missing *secrets.NotFoundError
	if errors.As(err, &missing) {
		if err := i.failBuild(ctx, start.Build, row.Digest, fmt.Sprintf("secret %s was deleted before the build started", missing.Name)); err != nil {
			return BuildCommand{}, err
		}
		return BuildCommand{}, ErrStaleBuild
	}
	if err != nil {
		return BuildCommand{}, fmt.Errorf("read build secrets: %w", err)
	}
	auth := map[string]Auth{}
	if len(row.RegistryAuth) > 0 {
		if err := json.Unmarshal(row.RegistryAuth, &auth); err != nil {
			return BuildCommand{}, fmt.Errorf("decode registry logins: %w", err)
		}
	}
	base := dockerfileBase(row.Dockerfile)
	// A workspace's images on one base share a cache. Workspaces never share
	// one: a build could write any cache entry it can push, and caches hold
	// the workspace's build contexts.
	scope := sha256.Sum256([]byte(row.WorkspaceID.String() + "\n" + row.Architecture + "\n" + base))
	image := i.config.buildTarget(row.PlatformHost, row.WorkspaceID, row.Digest, row.Forced).repository
	cache := i.config.cacheRepository(row.WorkspaceID)
	// The build pushes only its image and its workspace's cache, until its
	// deadline.
	access := hostAccess{push: []string{image, cache}}
	// A build reads its base from the platform registry only when it mirrors
	// an image there or starts from the managed base, which may sit outside
	// the workload repositories; definitions cannot name other platform
	// images.
	if repository, _ := i.config.repositoryOf(base); repository != "" {
		access.pull = []string{repository}
	}
	platform, err := i.login.host(ctx, access, row.DeadlineAt.Add(time.Minute))
	if err != nil {
		return BuildCommand{}, err
	}
	if platform != nil {
		auth[i.config.Registry] = *platform
	}
	return BuildCommand{
		Build: start.Build, Attempt: start.Attempt, Dockerfile: row.Dockerfile,
		Context: row.ContextSha256, ContextWorkspace: identity.WorkspaceID(row.WorkspaceID),
		Platform:       "linux/" + row.Architecture,
		PushRepository: i.config.Registry + "/" + image,
		CacheRef:       i.config.Registry + "/" + cache + ":" + hex.EncodeToString(scope[:16]),
		Insecure:       i.config.Insecure, Auth: auth, Deadline: row.DeadlineAt,
		Secrets: values, GPUs: buildGPUs(row.BuildGpu),
	}, nil
}

// BuildResources are the reservations and ceilings of a build container.
func BuildResources() (cpuMillis cpu.Millis, memoryBytes, memoryLimitBytes int64) {
	return buildCPUMillis, buildMemoryBytes, buildMemoryLimit
}

// BuildOutcome is what a build attempt produced: a pushed manifest digest or
// a failure. With the digest come the layers the host converted since an
// earlier completion named them, and those it uploaded to the URLs one
// returned.
type BuildOutcome struct {
	Digest  string
	Failure string
	// Transient says the failure is not the image's: a store or registry
	// stayed unreachable.
	Transient bool
	Converted []ConvertedLayer
	Uploaded  []UploadedLayer
}

// CompleteBuild records the outcome a host reports for its build container.
// A pushed image is read from the registry and published once every layer
// has a converted pair. Until then CompleteBuild returns the layers to
// convert, and the host reports the digest again with them converted.
func (i *Images) CompleteBuild(ctx context.Context, host compute.HostID, container execution.ContainerID, outcome BuildOutcome) ([]LayerUpload, error) {
	build, _, err := i.execution.LiveBuildContainer(ctx, host, container)
	if err != nil {
		return nil, err
	}
	started, err := i.queries.BuildToStart(ctx, BuildToStartParams{ID: build, Host: uuid.UUID(host)})
	if err != nil {
		return nil, fmt.Errorf("read build: %w", err)
	}
	fail := func(reason string, transient bool) error {
		return i.failReported(ctx, build, uuid.UUID(container), started.Digest, reason, transient)
	}
	if outcome.Failure != "" {
		return nil, fail(outcome.Failure, outcome.Transient)
	}
	if !manifestDigest.MatchString(outcome.Digest) {
		return nil, invalid("digest %q is not sha256:<hex>", outcome.Digest)
	}
	target := i.config.buildTarget(started.PlatformHost, started.WorkspaceID, started.Digest, started.Forced)
	pushed := publication{
		reference: i.config.Registry + "/" + target.repository + "@" + outcome.Digest,
		target:    target, workspace: started.WorkspaceID, container: uuid.UUID(container), deadline: started.DeadlineAt,
	}
	pushed.layers, err = i.layersOf(ctx, pushed.reference, started.Architecture)
	var rejected *InvalidError
	if errors.As(err, &rejected) {
		return nil, fail(rejected.Reason, false)
	}
	if err != nil {
		return nil, fmt.Errorf("check pushed image: %w", err)
	}
	converted, failure, err := i.checkConversions(ctx, pushed, outcome.Uploaded)
	if err != nil {
		return nil, err
	}
	if failure != "" {
		return nil, fail(failure, false)
	}
	var offered []offer
	err = i.endBuild(ctx, build, started.Digest, func(_ pgx.Tx, q *Queries, row LockBuildRow) (bool, string, error) {
		var failure string
		var err error
		offered, failure, err = i.recordLayers(ctx, q, pushed, converted, outcome.Converted)
		switch {
		case err != nil:
			return false, "", err
		case failure != "":
			offered = nil
			return true, failure, failIn(ctx, q, build, failure, false)
		case len(offered) > 0:
			return false, "", nil
		}
		// A workspace-scoped build, or one a customer's host ran, is that
		// workspace's image only; other workspaces that joined it build again.
		if target.scoped {
			err = q.PublishWorkspaceImage(ctx, PublishWorkspaceImageParams{
				WorkspaceID: row.WorkspaceID, ImageDigest: started.Digest, Reference: pushed.reference,
			})
		} else {
			err = q.PublishImage(ctx, PublishImageParams{Digest: started.Digest, Reference: &pushed.reference})
		}
		if err != nil {
			return false, "", fmt.Errorf("publish image: %w", err)
		}
		if err := q.SucceedBuild(ctx, build); err != nil {
			return false, "", fmt.Errorf("finish build: %w", err)
		}
		return true, "", nil
	})
	if err != nil {
		return nil, err
	}
	if len(offered) == 0 {
		return nil, i.endUploads(ctx, uuid.UUID(container))
	}
	return i.presignUploads(ctx, offered)
}

// failReported fails build for a reason its host reported or its pushed
// image showed, and ends the uploads of its container. transient says the
// reason is not the image's content.
func (i *Images) failReported(ctx context.Context, build, container uuid.UUID, digest []byte, reason string, transient bool) error {
	err := i.endBuild(ctx, build, digest, func(_ pgx.Tx, q *Queries, _ LockBuildRow) (bool, string, error) {
		return true, reason, failIn(ctx, q, build, reason, transient)
	})
	if err != nil {
		return err
	}
	return i.endUploads(ctx, container)
}

// failBuild fails a running build with reason and stops its containers. A
// build that already finished is left as it is.
func (i *Images) failBuild(ctx context.Context, build uuid.UUID, digest []byte, reason string) error {
	err := i.endBuild(ctx, build, digest, func(tx pgx.Tx, q *Queries, _ LockBuildRow) (bool, string, error) {
		return true, reason, i.stopAndFail(ctx, tx, q, build, reason, false)
	})
	if errors.Is(err, ErrStaleBuild) {
		return nil
	}
	return err
}

// endBuild runs fn on the locked image and build rows of a building build.
// fn reports whether it ended the build, and the failure it ended it with;
// an ended build is announced and recorded as a span in the trace that
// asked for it. A build that already finished is ErrStaleBuild.
func (i *Images) endBuild(ctx context.Context, build uuid.UUID, digest []byte, fn func(pgx.Tx, *Queries, LockBuildRow) (bool, string, error)) error {
	var row LockBuildRow
	var ended bool
	var failure string
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		// Lock order: image, then build.
		if _, err := q.LockImage(ctx, digest); err != nil {
			return fmt.Errorf("lock image: %w", err)
		}
		var err error
		if row, err = q.LockBuild(ctx, build); err != nil {
			return fmt.Errorf("lock build: %w", err)
		}
		if BuildStatus(row.State) != BuildBuilding {
			return ErrStaleBuild
		}
		if ended, failure, err = fn(tx, q, row); err != nil || !ended {
			return err
		}
		return database.Notify(ctx, tx, database.ChannelImageBuild, build.String())
	})
	if err != nil {
		return fmt.Errorf("build %s: %w", build, err)
	}
	if ended {
		traceBuild(ctx, row, failure)
	}
	return nil
}

// failIn fails build, whose rows q's transaction locked.
func failIn(ctx context.Context, q *Queries, build uuid.UUID, reason string, transient bool) error {
	if err := q.FailBuild(ctx, FailBuildParams{ID: build, Failure: truncate(reason), Transient: transient}); err != nil {
		return fmt.Errorf("fail build: %w", err)
	}
	return nil
}

// stopAndFail fails build, whose rows tx locked, and stops its containers.
func (i *Images) stopAndFail(ctx context.Context, tx pgx.Tx, q *Queries, build uuid.UUID, reason string, transient bool) error {
	if err := i.execution.StopBuildContainers(ctx, tx, build); err != nil {
		return err
	}
	return failIn(ctx, q, build, reason, transient)
}

// LogLine is one line of build output from a host.
type LogLine struct {
	Data string
	Time time.Time
}

// AppendLogs stores output of the build container on host.
func (i *Images) AppendLogs(ctx context.Context, host compute.HostID, container execution.ContainerID, lines []LogLine) error {
	if len(lines) == 0 {
		return nil
	}
	build, attempt, err := i.execution.LiveBuildContainer(ctx, host, container)
	if err != nil {
		return err
	}
	if err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		// The build row lock orders appends, so the counts stay exact.
		row, err := q.LockBuild(ctx, build)
		if err != nil {
			return fmt.Errorf("lock build: %w", err)
		}
		keep, size := 0, int64(0)
		for _, line := range lines {
			if row.LogBytes+size+int64(len(line.Data)) > maxLogBytes || int(row.LogLines)+keep >= maxLogLines {
				break
			}
			keep++
			size += int64(len(line.Data))
		}
		// More lines than the cap means the cut is already marked.
		if int(row.LogLines) > maxLogLines {
			return nil
		}
		kept := lines[:keep]
		count := CountBuildLogsParams{ID: build, Bytes: size, Lines: int32(keep)} //nolint:gosec // Bounded by maxLogLines.
		if keep < len(lines) {
			// Mark the cut once; a count above the cap drops later output.
			kept = append(slices.Clone(kept), LogLine{
				Data: fmt.Sprintf("build output truncated: an attempt keeps at most %d lines and %d MiB", maxLogLines, maxLogBytes>>20),
				Time: time.Now(),
			})
			count.Bytes, count.Lines = size, int32(maxLogLines+1)-row.LogLines //nolint:gosec // Bounded by maxLogLines.
		}
		if err := q.CountBuildLogs(ctx, count); err != nil {
			return fmt.Errorf("count build logs: %w", err)
		}
		return insertLogs(ctx, tx, q, build, attempt, kept)
	}); err != nil {
		return fmt.Errorf("append build logs: %w", err)
	}
	return nil
}

func insertLogs(ctx context.Context, tx pgx.Tx, q *Queries, build uuid.UUID, attempt int, lines []LogLine) error {
	data := make([]string, len(lines))
	times := make([]time.Time, len(lines))
	for n, l := range lines {
		data[n], times[n] = l.Data, l.Time
	}
	if err := q.InsertBuildLogs(ctx, InsertBuildLogsParams{
		BuildID: build, Attempt: int32(attempt), Data: data, LoggedAt: times, //nolint:gosec // At most maxAttempts.
	}); err != nil {
		return fmt.Errorf("insert build logs: %w", err)
	}
	return database.Notify(ctx, tx, database.ChannelImageBuildLog, build.String())
}

// Recover settles builds whose container stopped without an outcome or that
// ran past their deadline. A lost container gets one successor; the second
// loss, or the deadline, fails the build. Each build commits on its own.
func (i *Images) Recover(ctx context.Context, logger *slog.Logger) error {
	params := BuildsToRecoverParams{BatchSize: recoveryBatch}
	for {
		rows, err := i.queries.BuildsToRecover(ctx, params)
		if err != nil {
			return fmt.Errorf("list builds to recover: %w", err)
		}
		for _, row := range rows {
			err := i.recoverBuild(ctx, row.ID, row.ImageDigest)
			switch {
			case err == nil, errors.Is(err, ErrStaleBuild):
			case ctx.Err() != nil:
				return fmt.Errorf("recover build: %w", err)
			default:
				logger.ErrorContext(ctx, "recover build", "build_id", row.ID, "error", err)
			}
		}
		if len(rows) < recoveryBatch {
			return nil
		}
		params.AfterID = rows[len(rows)-1].ID
	}
}

func (i *Images) recoverBuild(ctx context.Context, id uuid.UUID, digest []byte) error {
	return i.endBuild(ctx, id, digest, func(tx pgx.Tx, q *Queries, build LockBuildRow) (bool, string, error) {
		containers, err := i.execution.BuildContainers(ctx, tx, id)
		if err != nil {
			return false, "", err
		}
		fail := func(reason string, transient bool) (bool, string, error) {
			return true, reason, i.stopAndFail(ctx, tx, q, id, reason, transient)
		}
		// A build that ran out of time or of attempts may fail the same way
		// every time, so its failure is the image's; one that never reached
		// a host waited for capacity, which says nothing of the image.
		if time.Now().After(build.DeadlineAt) {
			ran := slices.ContainsFunc(containers, func(c execution.BuildContainer) bool { return c.Host != nil })
			return fail(fmt.Sprintf("the build did not finish within %s", BuildTimeout), !ran)
		}
		if len(containers) == 0 {
			return fail("the build has no container", true)
		}
		last := containers[len(containers)-1]
		if last.State != execution.ContainerStopped {
			return false, "", nil
		}
		reason := fmt.Sprintf("build container stopped (%s)", last.StopReason)
		if last.ExitMessage != "" {
			reason += ": " + last.ExitMessage
		}
		if len(containers) >= maxAttempts {
			return fail(reason, false)
		}
		if err := insertLogs(ctx, tx, q, id, len(containers), []LogLine{{Data: reason + "; retrying", Time: time.Now()}}); err != nil {
			return false, "", err
		}
		if err := q.ResetBuildLogCount(ctx, id); err != nil {
			return false, "", fmt.Errorf("reset build log count: %w", err)
		}
		// The next attempt's container joins the build's trace.
		ctx := telemetry.WithTraceParent(ctx, deref(build.Traceparent))
		if build.Mirror {
			_, err = i.execution.CreatePlatformBuildContainer(ctx, tx, identity.WorkspaceID(build.WorkspaceID), id, buildCPUMillis, buildMemoryBytes)
			return false, "", err
		}
		gpu, err := q.ImageBuildGPU(ctx, digest)
		if err != nil {
			return false, "", fmt.Errorf("read build GPU: %w", err)
		}
		_, err = i.execution.CreateBuildContainer(ctx, tx, identity.WorkspaceID(build.WorkspaceID), id, buildCPUMillis, buildMemoryBytes, gpu)
		var unpaid *billing.PaymentRequiredError
		var limit *billing.LimitError
		var unoffered *billing.GPUUnavailableError
		if errors.As(err, &unpaid) || errors.As(err, &limit) || errors.As(err, &unoffered) {
			return fail(reason+"; "+err.Error(), false)
		}
		return false, "", err
	})
}

func truncate(s string) string {
	if len(s) <= maxFailureBytes {
		return s
	}
	return "…" + s[len(s)-maxFailureBytes:]
}
