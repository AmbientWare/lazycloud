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
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/secrets"
)

const (
	// BuildTimeout bounds one build across its attempts.
	BuildTimeout = time.Hour
	// maxAttempts is how many containers a build may use. Only a container
	// lost without an outcome earns another; a failed build step does not.
	maxAttempts = 2
	// Build containers reserve this much and may use the host's CPU and up
	// to buildMemoryLimit.
	buildCPUMillis   = 1000
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

// imageTarget is where a build pushes and whether its result is the
// workspace's alone. A build on a connected account's or joined machine's
// host runs where the customer controls it, so only the shared image a
// platform host built may be published for every workspace.
func (i *Images) imageTarget(ctx context.Context, q *Queries, host compute.HostID, workspace uuid.UUID, digest []byte, forced bool) (string, bool, error) {
	kind, err := q.HostKind(ctx, uuid.UUID(host))
	if err != nil {
		return "", false, fmt.Errorf("read host kind: %w", err)
	}
	if forced || compute.HostKind(kind) != compute.KindPlatform {
		return i.config.workspaceImageRepository(workspace, digest), true, nil
	}
	return i.config.imageRepository(digest), false, nil
}

func (c Config) cacheRepository(workspace uuid.UUID) string {
	return c.Repository + "/cache/" + workspace.String()
}

func (c Config) filesystemRepository(workspace identity.WorkspaceID) string {
	return c.Repository + "/filesystems/" + workspace.String()
}

// repositoryOf is the repository path of reference, an image in Registry
// under Repository.
func (c Config) repositoryOf(reference string) (string, bool) {
	path, ok := strings.CutPrefix(reference, c.Registry+"/")
	if !ok {
		return "", false
	}
	path, _, _ = strings.Cut(path, "@")
	if slash := strings.LastIndex(path, "/"); strings.Contains(path[slash+1:], ":") {
		path = path[:slash+1] + strings.SplitN(path[slash+1:], ":", 2)[0]
	}
	return path, strings.HasPrefix(path, c.Repository+"/")
}

func (c Config) registryHost() string { return c.Registry }

// Images is the images owner.
type Images struct {
	pool      *pgxpool.Pool
	queries   *Queries
	execution *execution.Execution
	// secrets holds the workspace secrets builds read.
	secrets  *secrets.Secrets
	resolver *resolver
	config   Config
	login    *platformLogin
	ecr      ecrToken
}

// NewImages returns the images owner.
func NewImages(pool *pgxpool.Pool, exec *execution.Execution, vault *secrets.Secrets, config Config) *Images {
	return &Images{
		pool: pool, queries: New(pool), execution: exec, secrets: vault, resolver: newResolver(config.Registry), config: config,
		login: newPlatformLogin(config), ecr: exchangeECR,
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
	CreatedAt       time.Time
	ReadyAt         *time.Time
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
	// reference is set when the image is its base: nothing to build.
	reference *string
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
	if s.dockerfile != "" {
		base = dockerfileBase(s.dockerfile)
	}
	if base == "" {
		return prepared{}, invalid("the Dockerfile has no FROM instruction")
	}
	baseHost, err := registryHost(base)
	if err != nil {
		return prepared{}, err
	}
	var given map[string]string
	if def.BaseImageCredentials != nil {
		given = *def.BaseImageCredentials
	}
	baseAuth, err := registryAuth(ctx, baseHost, given, i.ecr)
	if err != nil {
		return prepared{}, err
	}
	out := prepared{spec: s, auth: map[string]Auth{}, secretVersions: map[string]string{}}
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
	if s.base == "" && s.dockerfile == "" {
		managed = s.baseReference(i.config.ManagedBase)
	}
	pin := func(ref string) (string, error) {
		host, err := registryHost(ref)
		if err != nil {
			return "", err
		}
		platform := host == i.config.registryHost()
		if ref != managed {
			if platform {
				return "", invalid("%s is in the platform registry; use a built image by its id with Image.from_id", ref)
			}
			if err := checkRegistryHost(host); err != nil {
				return "", err
			}
		}
		var auth *Auth
		switch {
		case platform:
			if auth, err = i.login.auth(ctx); err != nil {
				return "", err
			}
		case host == baseHost && baseAuth != nil:
			auth = baseAuth
		}
		p, err := i.resolver.pin(ctx, ref, auth, platform && i.config.Insecure)
		if err != nil {
			return "", err
		}
		return p.ref, nil
	}
	out.dockerfile, err = s.render(i.config.ManagedBase, pin)
	if err != nil {
		return prepared{}, err
	}
	inputs := buildInputs{Secrets: out.secretVersions, GPU: s.gpu}
	if len(s.secrets) > 0 {
		// Another workspace's secrets of the same names build another image.
		inputs.Workspace = uuid.UUID(workspace).String()
	}
	out.digest = imageDigest(out.dockerfile, s.architecture, s.context, inputs)
	out.id = "img_" + hex.EncodeToString(out.digest)[:24]
	if !needsBuild(out.dockerfile) && baseAuth == nil {
		ref := dockerfileBase(out.dockerfile)
		out.reference = &ref
	}
	return out, nil
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
		out = Resolution{Image: image}
		build, err := q.ActiveBuild(ctx, ActiveBuildParams{ImageDigest: p.digest})
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("read active build: %w", err)
		}
		b, err := i.buildOut(ctx, q, build.ID, image.ID, build.State, build.Failure, build.CreatedAt, build.FinishedAt)
		out.Build = &b
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
	var auth []byte
	if len(p.auth) > 0 {
		// The logins are stored until the build ends; see image_builds.
		if auth, err = json.Marshal(p.auth); err != nil { //nolint:gosec // The build needs the password.
			return Resolution{}, fmt.Errorf("encode registry logins: %w", err)
		}
	}
	var out Resolution
	err = pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		image, err := i.upsert(ctx, q, workspace, p)
		if err != nil {
			return err
		}
		out = Resolution{Image: image}
		// An image that is its base has nothing to build.
		if p.reference != nil || (image.Reference != nil && !force) {
			return nil
		}
		// Forcing an image nobody published yet is its first build. A
		// workspace whose builds run on its connected account's hosts
		// builds for itself alone.
		customer, err := q.WorkspaceOnCustomerHosts(ctx, uuid.UUID(workspace))
		if err != nil {
			return fmt.Errorf("read workspace hosts: %w", err)
		}
		forced := (force && image.globalReference != nil) || customer
		active, err := q.ActiveBuild(ctx, ActiveBuildParams{ImageDigest: p.digest, Forced: forced, WorkspaceID: uuid.UUID(workspace)})
		switch {
		case err == nil:
			b, err := i.buildOut(ctx, q, active.ID, image.ID, active.State, active.Failure, active.CreatedAt, active.FinishedAt)
			out.Build = &b
			return err
		case !errors.Is(err, pgx.ErrNoRows):
			return fmt.Errorf("read active build: %w", err)
		}
		// The image row lock serializes requests for this image, so no other
		// build can start between the check and the insert.
		row, err := q.InsertBuild(ctx, InsertBuildParams{
			ImageDigest: p.digest, WorkspaceID: uuid.UUID(workspace), Forced: forced, ContextSha256: p.spec.context,
			RegistryAuth: auth, TimeoutSeconds: BuildTimeout.Seconds(),
		})
		if err != nil {
			return fmt.Errorf("insert build: %w", err)
		}
		if _, err := i.execution.CreateBuildContainer(ctx, tx, workspace, row.ID, buildCPUMillis, buildMemoryBytes, p.spec.gpu); err != nil {
			return err
		}
		if err := database.Notify(ctx, tx, database.ChannelImageBuild, row.ID.String()); err != nil {
			return err
		}
		out.Build = &Build{
			ID: row.ID, ImageID: image.ID, Status: BuildBuilding, Phase: PhaseQueued, Attempt: 1, CreatedAt: row.CreatedAt,
		}
		return nil
	})
	if err != nil {
		return Resolution{}, fmt.Errorf("build image: %w", err)
	}
	return out, nil
}

func (i *Images) upsert(ctx context.Context, q *Queries, workspace identity.WorkspaceID, p prepared) (Image, error) {
	versions, err := json.Marshal(p.secretVersions)
	if err != nil {
		return Image{}, fmt.Errorf("encode secret versions: %w", err)
	}
	row, err := q.UpsertImage(ctx, UpsertImageParams{
		Digest: p.digest, ID: p.id, Dockerfile: p.dockerfile, PythonVersion: p.spec.python,
		Architecture: p.spec.architecture, BuildSecrets: versions, BuildGpu: p.spec.gpu, Reference: p.reference,
	})
	if err != nil {
		return Image{}, fmt.Errorf("upsert image: %w", err)
	}
	if err := q.GrantImage(ctx, GrantImageParams{WorkspaceID: uuid.UUID(workspace), ImageDigest: p.digest}); err != nil {
		return Image{}, fmt.Errorf("grant image: %w", err)
	}
	return i.workspaceImage(ctx, q, workspace, row.ID)
}

func (i *Images) workspaceImage(ctx context.Context, q *Queries, workspace identity.WorkspaceID, id string) (Image, error) {
	row, err := q.WorkspaceImage(ctx, WorkspaceImageParams{WorkspaceID: uuid.UUID(workspace), ID: id})
	if errors.Is(err, pgx.ErrNoRows) {
		return Image{}, ErrNotFound
	}
	if err != nil {
		return Image{}, fmt.Errorf("read image: %w", err)
	}
	return Image{
		ID: row.ID, PythonVersion: row.PythonVersion, Architecture: row.Architecture,
		Reference: row.Reference, globalReference: row.GlobalReference, CreatedAt: row.CreatedAt, ReadyAt: row.ReadyAt,
	}, nil
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
	if image.Reference == nil {
		return "", ErrNotReady
	}
	if minorVersion(image.PythonVersion) != python {
		return "", invalid("image %s has Python %s, not %s", id, image.PythonVersion, python)
	}
	return *image.Reference, nil
}

// Pull is what a host needs to pull a ready image.
type Pull struct {
	Reference string
	// Auth logs in to the platform registry; nil for other registries,
	// which are public.
	Auth     *Auth
	Platform string
}

// PullOf returns how to pull reference, the image a release pinned when it
// deployed image id.
func (i *Images) PullOf(ctx context.Context, id, reference string) (Pull, error) {
	architecture, err := i.queries.ImageArchitecture(ctx, id)
	if err != nil {
		return Pull{}, fmt.Errorf("read image %s: %w", id, err)
	}
	pull := Pull{Reference: reference, Platform: "linux/" + architecture}
	if strings.HasPrefix(reference, i.config.Registry+"/") {
		repository, ok := i.config.repositoryOf(reference)
		if !ok {
			return Pull{}, fmt.Errorf("image %s is outside the platform's workload repositories", reference)
		}
		access := hostAccess{pull: []string{repository}}
		if pull.Auth, err = i.login.host(ctx, access, time.Now().Add(pullWindow)); err != nil {
			return Pull{}, err
		}
	}
	return pull, nil
}

func (i *Images) buildOut(ctx context.Context, q *Queries, id uuid.UUID, imageID, state string, failure *string, created time.Time, finished *time.Time) (Build, error) {
	b := Build{ID: id, ImageID: imageID, Status: BuildStatus(state), CreatedAt: created, FinishedAt: finished}
	if failure != nil {
		b.Failure = *failure
	}
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
	row, err := i.queries.BuildToStart(ctx, start.Build)
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
	// A workspace's images on one base share a cache. Workspaces never share
	// one: a build could write any cache entry it can push, and caches hold
	// the workspace's build contexts.
	scope := sha256.Sum256([]byte(row.WorkspaceID.String() + "\n" + row.Architecture + "\n" + dockerfileBase(row.Dockerfile)))
	image, _, err := i.imageTarget(ctx, i.queries, host, row.WorkspaceID, row.Digest, row.Forced)
	if err != nil {
		return BuildCommand{}, err
	}
	cache := i.config.cacheRepository(row.WorkspaceID)
	// The build pushes only its image and its workspace's cache, until its
	// deadline.
	platform, err := i.login.host(ctx, hostAccess{push: []string{image, cache}}, row.DeadlineAt.Add(time.Minute))
	if err != nil {
		return BuildCommand{}, err
	}
	if platform != nil {
		auth[i.config.registryHost()] = *platform
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

// failLocked fails build id, whose image and build rows tx locked, and stops
// its containers.
func (i *Images) failLocked(ctx context.Context, tx pgx.Tx, id uuid.UUID, reason string) error {
	if err := i.execution.StopBuildContainers(ctx, tx, id); err != nil {
		return err
	}
	if err := i.queries.WithTx(tx).FailBuild(ctx, FailBuildParams{ID: id, Failure: truncate(reason)}); err != nil {
		return fmt.Errorf("fail build: %w", err)
	}
	return database.Notify(ctx, tx, database.ChannelImageBuild, id.String())
}

// BuildResources are the reservations and ceilings of a build container.
func BuildResources() (cpuMillis, memoryBytes, memoryLimitBytes int64) {
	return buildCPUMillis, buildMemoryBytes, buildMemoryLimit
}

// BuildOutcome is what a build attempt produced: a pushed manifest digest or
// a failure.
type BuildOutcome struct {
	Digest  string
	Failure string
}

// CompleteBuild records the outcome a host reports for its build container.
// A success is checked against the registry before the image is published.
func (i *Images) CompleteBuild(ctx context.Context, host compute.HostID, container execution.ContainerID, outcome BuildOutcome) error {
	build, _, err := i.execution.LiveBuildContainer(ctx, host, container)
	if err != nil {
		return err
	}
	started, err := i.queries.BuildToStart(ctx, build)
	if err != nil {
		return fmt.Errorf("read build: %w", err)
	}
	digest := started.Digest
	repository, scoped, err := i.imageTarget(ctx, i.queries, host, started.WorkspaceID, digest, started.Forced)
	if err != nil {
		return err
	}
	var reference string
	if outcome.Failure == "" {
		if !manifestDigest.MatchString(outcome.Digest) {
			return invalid("digest %q is not sha256:<hex>", outcome.Digest)
		}
		reference = i.config.Registry + "/" + repository + "@" + outcome.Digest
		auth, err := i.login.auth(ctx)
		if err != nil {
			return err
		}
		if _, err := i.resolver.pin(ctx, reference, auth, i.config.Insecure); err != nil {
			return fmt.Errorf("check pushed image: %w", err)
		}
	}
	err = pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		// Lock order: image, then build.
		if _, err := q.LockImage(ctx, digest); err != nil {
			return fmt.Errorf("lock image: %w", err)
		}
		row, err := q.LockBuild(ctx, build)
		if err != nil {
			return fmt.Errorf("lock build: %w", err)
		}
		if BuildStatus(row.State) != BuildBuilding {
			return ErrStaleBuild
		}
		if outcome.Failure != "" {
			if err := q.FailBuild(ctx, FailBuildParams{ID: build, Failure: truncate(outcome.Failure)}); err != nil {
				return fmt.Errorf("fail build: %w", err)
			}
		} else {
			var err error
			// A workspace-scoped build, or one a customer's host ran, is
			// that workspace's image only; other workspaces that joined it
			// build again.
			if scoped {
				err = q.PublishWorkspaceImage(ctx, PublishWorkspaceImageParams{
					WorkspaceID: row.WorkspaceID, ImageDigest: digest, Reference: reference,
				})
			} else {
				err = q.PublishImage(ctx, PublishImageParams{Digest: digest, Reference: &reference})
			}
			if err != nil {
				return fmt.Errorf("publish image: %w", err)
			}
			if err := q.SucceedBuild(ctx, build); err != nil {
				return fmt.Errorf("finish build: %w", err)
			}
		}
		return database.Notify(ctx, tx, database.ChannelImageBuild, build.String())
	})
	if err != nil {
		return fmt.Errorf("complete build %s: %w", build, err)
	}
	return nil
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
		return i.insertLogs(ctx, tx, build, attempt, kept)
	}); err != nil {
		return fmt.Errorf("append build logs: %w", err)
	}
	return nil
}

func (i *Images) insertLogs(ctx context.Context, tx pgx.Tx, build uuid.UUID, attempt int, lines []LogLine) error {
	data := make([]string, len(lines))
	times := make([]time.Time, len(lines))
	for n, l := range lines {
		data[n], times[n] = l.Data, l.Time
	}
	if err := i.queries.WithTx(tx).InsertBuildLogs(ctx, InsertBuildLogsParams{
		BuildID: build, Attempt: int32(attempt), Data: data, LoggedAt: times, //nolint:gosec // At most maxAttempts.
	}); err != nil {
		return fmt.Errorf("insert build logs: %w", err)
	}
	return database.Notify(ctx, tx, database.ChannelImageBuildLog, build.String())
}

// Recover settles builds whose container stopped without an outcome or that
// ran past their deadline. A lost container gets one successor; the second
// loss, or the deadline, fails the build. Each build commits on its own. It
// returns how many builds it changed.
func (i *Images) Recover(ctx context.Context, logger *slog.Logger) (int, error) {
	changed := 0
	params := BuildsToRecoverParams{BatchSize: recoveryBatch}
	for {
		rows, err := i.queries.BuildsToRecover(ctx, params)
		if err != nil {
			return changed, fmt.Errorf("list builds to recover: %w", err)
		}
		for _, row := range rows {
			did, err := i.recoverBuild(ctx, row.ID, row.ImageDigest)
			switch {
			case err == nil && did:
				changed++
			case err == nil:
			case ctx.Err() != nil:
				return changed, fmt.Errorf("recover build: %w", err)
			default:
				logger.ErrorContext(ctx, "recover build", "build_id", row.ID, "error", err)
			}
		}
		if len(rows) < recoveryBatch {
			return changed, nil
		}
		params.AfterID = rows[len(rows)-1].ID
	}
}

func (i *Images) recoverBuild(ctx context.Context, id uuid.UUID, digest []byte) (bool, error) {
	changed := false
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		if _, err := q.LockImage(ctx, digest); err != nil {
			return fmt.Errorf("lock image: %w", err)
		}
		build, err := q.LockBuild(ctx, id)
		if err != nil {
			return fmt.Errorf("lock build: %w", err)
		}
		if BuildStatus(build.State) != BuildBuilding {
			return nil
		}
		containers, err := i.execution.BuildContainers(ctx, tx, id)
		if err != nil {
			return err
		}
		fail := func(reason string) error {
			changed = true
			return i.failLocked(ctx, tx, id, reason)
		}
		if time.Now().After(build.DeadlineAt) {
			return fail(fmt.Sprintf("the build did not finish within %s", BuildTimeout))
		}
		if len(containers) == 0 {
			return fail("the build has no container")
		}
		last := containers[len(containers)-1]
		if last.State != execution.ContainerStopped {
			return nil
		}
		reason := fmt.Sprintf("build container stopped (%s)", last.StopReason)
		if last.ExitMessage != "" {
			reason += ": " + last.ExitMessage
		}
		if len(containers) >= maxAttempts {
			return fail(reason)
		}
		changed = true
		if err := i.insertLogs(ctx, tx, id, len(containers), []LogLine{{Data: reason + "; retrying", Time: time.Now()}}); err != nil {
			return err
		}
		if err := q.ResetBuildLogCount(ctx, id); err != nil {
			return fmt.Errorf("reset build log count: %w", err)
		}
		gpu, err := q.ImageBuildGPU(ctx, digest)
		if err != nil {
			return fmt.Errorf("read build GPU: %w", err)
		}
		_, err = i.execution.CreateBuildContainer(ctx, tx, identity.WorkspaceID(build.WorkspaceID), id, buildCPUMillis, buildMemoryBytes, gpu)
		var unpaid *billing.PaymentRequiredError
		var limit *billing.LimitError
		var unoffered *billing.GPUUnavailableError
		if errors.As(err, &unpaid) || errors.As(err, &limit) || errors.As(err, &unoffered) {
			return fail(reason + "; " + err.Error())
		}
		return err
	})
	if err != nil {
		return false, fmt.Errorf("recover build %s: %w", id, err)
	}
	return changed, nil
}

func truncate(s string) string {
	if len(s) <= maxFailureBytes {
		return s
	}
	return "…" + s[len(s)-maxFailureBytes:]
}
