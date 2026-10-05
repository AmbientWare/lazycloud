package images

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// conversionRetry is how long after a mirror build failed for its image's
// content the starts that need it fail too, before the next one builds it
// again.
const conversionRetry = time.Minute

// BuildWaitError means a start or deploy needs an image whose mirror build
// is under way. The build announces each change on database.ChannelImageBuild
// with its id. It is also ErrNotReady.
type BuildWaitError struct{ Build uuid.UUID }

func (e *BuildWaitError) Error() string {
	return "the image is converting in build " + e.Build.String()
}

func (e *BuildWaitError) Unwrap() error { return ErrNotReady }

// ConversionError means the image cannot be converted: its content cannot,
// such as a layer of a media type conversion cannot read.
type ConversionError struct{ Reason string }

func (e *ConversionError) Error() string { return "the image cannot be converted: " + e.Reason }

// A container starts only from a converted reference. An image without
// one gets it from a mirror build, a build with no steps that copies the
// image into the platform registry and converts its layers: the platform's
// managed Python image (ManagedPull), and a reference published or pinned
// without layer rows (ConvertedPull, Deployable, Prepare). A public or
// platform-global image mirrors on a platform host for every workspace; a
// workspace's own image mirrors where its builds run, for it alone. The
// first request that needs one starts it and the others join it; each gets
// a BuildWaitError until it publishes.

// Prepare makes image id of workspace ready to deploy. An image ready
// returns no build. A stored reference without layer rows starts or joins
// its mirror build, and an image building returns that build; the build
// is the workspace's to follow. A mirror that cannot be built is a
// ConversionError; an image with neither is ErrNotReady.
func (i *Images) Prepare(ctx context.Context, workspace identity.WorkspaceID, id string) (Resolution, error) {
	image, err := i.Get(ctx, workspace, id)
	if err != nil || image.Reference != nil {
		return Resolution{Image: image}, err
	}
	var building uuid.UUID
	if image.unconverted != nil {
		err := i.convertReference(ctx, workspace, id, *image.unconverted)
		var waiting *BuildWaitError
		switch {
		case errors.As(err, &waiting):
			building = waiting.Build
		case err != nil:
			return Resolution{}, err
		default:
			image, err = i.Get(ctx, workspace, id)
			return Resolution{Image: image}, err
		}
	} else {
		digest, err := i.queries.ImageDigestOf(ctx, id)
		if err != nil {
			return Resolution{}, fmt.Errorf("read image %s: %w", id, err)
		}
		// The build this workspace's request would join, by build's rule: a
		// workspace on its connected account's hosts builds for itself.
		customer, err := i.queries.WorkspaceOnCustomerHosts(ctx, uuid.UUID(workspace))
		if err != nil {
			return Resolution{}, fmt.Errorf("read workspace hosts: %w", err)
		}
		active, err := i.queries.ActiveBuild(ctx, ActiveBuildParams{ImageDigest: digest, Forced: customer, WorkspaceID: uuid.UUID(workspace)})
		if errors.Is(err, pgx.ErrNoRows) {
			return Resolution{}, ErrNotReady
		}
		if err != nil {
			return Resolution{}, fmt.Errorf("read active build: %w", err)
		}
		building = active.ID
	}
	build, err := i.GetBuild(ctx, nil, workspace, building, 0)
	if err != nil {
		return Resolution{}, err
	}
	return Resolution{Image: image, Build: &build}, nil
}

// ConvertedPull is how a container of workspace pulls reference, the image
// a release pinned when it deployed image id, once it is converted.
func (i *Images) ConvertedPull(ctx context.Context, workspace identity.WorkspaceID, id, reference string) (Pull, error) {
	if err := i.convertReference(ctx, workspace, id, reference); err != nil {
		return Pull{}, err
	}
	return i.PullOf(ctx, id, reference)
}

// convertReference gives reference, an image of image id, its layer rows: a
// mirror build converts exactly that image, and the mirror's layers become
// the reference's.
func (i *Images) convertReference(ctx context.Context, workspace identity.WorkspaceID, id, reference string) error {
	rows, err := i.queries.ReferenceLayers(ctx, reference)
	if err != nil {
		return fmt.Errorf("read image layers: %w", err)
	}
	if len(rows) > 0 {
		return nil
	}
	runtime, err := i.queries.ImageRuntime(ctx, id)
	if err != nil {
		return fmt.Errorf("read image %s: %w", id, err)
	}
	if !manifestDigest.MatchString(digestOf(reference)) {
		return &ConversionError{Reason: reference + " is not pinned by digest"}
	}
	dockerfile := "FROM " + reference + "\n"
	digest := imageDigest(dockerfile, runtime.Architecture, nil, buildInputs{})
	p := prepared{
		spec: spec{python: runtime.PythonVersion, architecture: runtime.Architecture}, dockerfile: dockerfile, digest: digest,
		id: imageID(digest), secretVersions: map[string]string{},
	}
	kind := buildSharedMirror
	if repository, platform := i.config.repositoryOf(reference); platform && !strings.HasPrefix(repository, i.config.Repository+"/images/") {
		kind = buildOwnMirror
	}
	mirror, err := i.buildOrWait(ctx, workspace, p, kind)
	if err != nil {
		return err
	}
	return i.adoptLayers(ctx, workspace, kind, reference, runtime.Architecture, *mirror.Reference)
}

func imageID(digest []byte) string { return "img_" + hex.EncodeToString(digest)[:24] }

// digestOf is the digest a reference by digest names.
func digestOf(reference string) string {
	_, digest, _ := strings.Cut(reference, "@")
	return digest
}

// adoptLayers records the converted layers of mirror, the platform's copy
// of reference, as reference's, once the registry shows both name the
// same blobs.
func (i *Images) adoptLayers(ctx context.Context, workspace identity.WorkspaceID, kind buildKind, reference, architecture, mirror string) error {
	converted, err := i.queries.ReferenceLayers(ctx, mirror)
	if err != nil {
		return fmt.Errorf("read image layers: %w", err)
	}
	for _, l := range converted {
		// A shared reference uses shared pairs only; the workspace's own
		// may use its own too.
		if l.WorkspaceID != nil && (kind != buildOwnMirror || *l.WorkspaceID != uuid.UUID(workspace)) {
			return &ConversionError{Reason: "the mirror of " + reference + " holds layers of one workspace alone"}
		}
	}
	var auth *Auth
	if _, platform := i.config.repositoryOf(reference); platform {
		if auth, err = i.login.auth(ctx); err != nil {
			return err
		}
	}
	layers, err := i.resolver.layers(ctx, reference, auth, i.config.Insecure, "linux/"+architecture)
	var rejected *InvalidError
	if errors.As(err, &rejected) {
		return &ConversionError{Reason: rejected.Reason}
	}
	if err != nil {
		return fmt.Errorf("read %s: %w", reference, err)
	}
	if len(layers) != len(converted) {
		return &ConversionError{Reason: fmt.Sprintf("%s has %d layers, its mirror %d", reference, len(layers), len(converted))}
	}
	ids := make([]uuid.UUID, len(layers))
	positions := make([]int32, len(layers))
	for n, l := range layers {
		if l.blob != converted[n].BlobDigest || l.diffID != converted[n].DiffID {
			return &ConversionError{Reason: fmt.Sprintf("layer %d of %s differs from its mirror", n, reference)}
		}
		ids[n], positions[n] = converted[n].ID, int32(n) //nolint:gosec // At most maxImageLayers.
	}
	if err := i.queries.RecordReferenceLayers(ctx, RecordReferenceLayersParams{Reference: reference, Positions: positions, LayerIds: ids}); err != nil {
		return fmt.Errorf("record image layers: %w", err)
	}
	return i.RecordUses(ctx, []string{reference})
}

// ManagedPull is how a container of workspace on host with no image of its
// own pulls the platform's image for python: Config.ManagedBase mirrored
// into the platform registry and converted. Hosts pull the mirror, never
// the template's registry.
func (i *Images) ManagedPull(ctx context.Context, host compute.HostID, workspace identity.WorkspaceID, python string) (Pull, error) {
	architecture, err := i.queries.HostArchitecture(ctx, uuid.UUID(host))
	if err != nil {
		return Pull{}, fmt.Errorf("read host architecture: %w", err)
	}
	key := ManagedImageParams{WorkspaceID: uuid.UUID(workspace), PythonVersion: python, Template: i.config.ManagedBase, Architecture: architecture}
	row, err := i.queries.ManagedImage(ctx, key)
	var p prepared
	switch {
	case err == nil && row.Reference != nil && row.Converted:
		return i.PullOf(ctx, row.ID, *row.Reference)
	case err == nil:
		// The recorded image, not the template's current digest: the image
		// stays the same until the template changes.
		p = prepared{
			spec: spec{python: row.PythonVersion, architecture: row.Architecture}, dockerfile: row.Dockerfile, digest: row.Digest, id: row.ID,
			secretVersions: map[string]string{},
		}
	case errors.Is(err, pgx.ErrNoRows):
		arch := apitypes.ImageDefinitionArchitecture(architecture)
		p, err = i.prepare(ctx, workspace, apitypes.ImageDefinition{PythonVersion: python, Architecture: &arch})
		var invalid *InvalidError
		if errors.As(err, &invalid) {
			return Pull{}, &ConversionError{Reason: invalid.Reason}
		}
		if err != nil {
			return Pull{}, err
		}
		image, err := i.buildOrWait(ctx, workspace, p, buildSharedMirror)
		var waiting *BuildWaitError
		if err != nil && !errors.As(err, &waiting) {
			return Pull{}, err
		}
		// The image row exists once its build was asked for.
		if err := i.queries.RecordManagedImage(ctx, RecordManagedImageParams{
			PythonVersion: python, Template: i.config.ManagedBase, Architecture: architecture, ImageDigest: p.digest,
		}); err != nil {
			return Pull{}, fmt.Errorf("record managed image: %w", err)
		}
		if err != nil {
			return Pull{}, err
		}
		return i.PullOf(ctx, image.ID, *image.Reference)
	default:
		return Pull{}, fmt.Errorf("read managed image: %w", err)
	}
	image, err := i.buildOrWait(ctx, workspace, p, buildSharedMirror)
	if err != nil {
		return Pull{}, err
	}
	return i.PullOf(ctx, image.ID, *image.Reference)
}

// buildOrWait returns p's image once its mirror build published it for
// workspace. A build under way is a BuildWaitError, found without writing,
// so a request repeated while it waits changes nothing. A build that failed
// for the image's content within conversionRetry is a ConversionError.
// Otherwise it starts the mirror build and waits for it.
func (i *Images) buildOrWait(ctx context.Context, workspace identity.WorkspaceID, p prepared, kind buildKind) (Image, error) {
	latest, err := i.queries.LatestBuild(ctx, p.digest)
	switch {
	case errors.Is(err, pgx.ErrNoRows):
	case err != nil:
		return Image{}, fmt.Errorf("read latest build: %w", err)
	case BuildStatus(latest.State) == BuildBuilding:
		return Image{}, &BuildWaitError{Build: latest.ID}
	case BuildStatus(latest.State) == BuildFailed && !latest.FailureTransient &&
		latest.FinishedAt != nil && time.Since(*latest.FinishedAt) < conversionRetry:
		reason := "its build failed"
		if latest.Failure != nil {
			reason = *latest.Failure
		}
		return Image{}, &ConversionError{Reason: reason}
	}
	r, err := i.build(ctx, workspace, p, false, kind)
	var unpaid *billing.PaymentRequiredError
	var limit *billing.LimitError
	if errors.As(err, &unpaid) || errors.As(err, &limit) {
		return Image{}, &ConversionError{Reason: err.Error()}
	}
	if err != nil {
		return Image{}, err
	}
	if r.Image.Reference != nil {
		return r.Image, nil
	}
	if r.Build == nil {
		return Image{}, fmt.Errorf("image %s is neither published nor building", r.Image.ID)
	}
	return Image{}, &BuildWaitError{Build: r.Build.ID}
}
