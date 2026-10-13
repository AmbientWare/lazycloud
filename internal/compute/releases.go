package compute

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"maps"
	"slices"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// AgentRelease is a published agent version with its archive digest per
// architecture.
type AgentRelease struct {
	Version string
	// SHA256 maps amd64 and arm64 to the archive's hex digest.
	SHA256 map[string]string
	// RolloutPercent is the share of updatable hosts, by host id, that move
	// to the release while it is the target; zero publishes it to none.
	RolloutPercent int
	// Build digests the archives' contents but their VERSION file: hosts and
	// reserves on a release of the target's build need no update.
	Build string
}

// updateWindow is how long an updating agent may stay away before its host
// counts as lost: three wrapper starts and a rollback fit in it.
const updateWindow = 5 * time.Minute

func releaseOf(version string, amd64, arm64 *string, rollout int32, build *string) AgentRelease {
	r := AgentRelease{Version: version, SHA256: map[string]string{}, RolloutPercent: int(rollout), Build: deref(build)}
	if amd64 != nil {
		r.SHA256["amd64"] = *amd64
	}
	if arm64 != nil {
		r.SHA256["arm64"] = *arm64
	}
	return r
}

// VersionFile names the release in its archive, beside the agent; the
// binaries carry no version, so releases that change no host code archive
// the same bytes but it.
const VersionFile = "VERSION"

// ReleaseBuild digests the release archives of each architecture, gzipped
// tars, over every member's name, mode and contents but VersionFile.
func ReleaseBuild(archives map[string]io.Reader) (string, error) {
	h := sha256.New()
	for _, arch := range slices.Sorted(maps.Keys(archives)) {
		gz, err := gzip.NewReader(archives[arch])
		if err != nil {
			return "", fmt.Errorf("read the %s archive: %w", arch, err)
		}
		r := tar.NewReader(gz)
		for {
			header, err := r.Next()
			if errors.Is(err, io.EOF) {
				break
			}
			if err != nil {
				return "", fmt.Errorf("read the %s archive: %w", arch, err)
			}
			if header.Name == VersionFile {
				continue
			}
			_, _ = fmt.Fprintf(h, "%s %s %o %d\n", arch, header.Name, header.Mode, header.Size) // A hash never fails a write.
			if _, err := io.CopyN(h, r, header.Size); err != nil {
				return "", fmt.Errorf("read %s from the %s archive: %w", header.Name, arch, err)
			}
		}
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// PublishAgentRelease records a release and makes it the target every
// updatable agent moves to. A version is immutable: republishing it with
// other digests is a conflict.
func (c *Compute) PublishAgentRelease(ctx context.Context, release AgentRelease) error {
	digest := func(arch string) *string {
		if d, ok := release.SHA256[arch]; ok {
			return &d
		}
		return nil
	}
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		n, err := q.UpsertAgentRelease(ctx, UpsertAgentReleaseParams{
			Version: release.Version, Sha256Amd64: digest("amd64"), Sha256Arm64: digest("arm64"),
			RolloutPercent: int32(release.RolloutPercent), //nolint:gosec // The schema bounds it to 0-100.
			Build:          nilIfEmpty(release.Build),
		})
		if err != nil {
			return fmt.Errorf("insert release: %w", err)
		}
		if n == 0 {
			return &ConflictError{Message: fmt.Sprintf("agent release %s was published with other digests", release.Version)}
		}
		if err := q.ClearTargetRelease(ctx, release.Version); err != nil {
			return fmt.Errorf("clear target release: %w", err)
		}
		if _, err := q.SetTargetRelease(ctx, release.Version); err != nil {
			return fmt.Errorf("set target release: %w", err)
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("publish agent release: %w", err)
	}
	return nil
}

// TargetRelease returns the release agents move to, or ErrNotFound.
func (c *Compute) TargetRelease(ctx context.Context) (AgentRelease, error) {
	row, err := c.queries.TargetRelease(ctx)
	if errors.Is(err, pgx.ErrNoRows) {
		return AgentRelease{}, ErrNotFound
	}
	if err != nil {
		return AgentRelease{}, fmt.Errorf("read target release: %w", err)
	}
	return releaseOf(row.Version, row.Sha256Amd64, row.Sha256Arm64, row.RolloutPercent, row.Build), nil
}

// Release returns a published release, or ErrNotFound.
func (c *Compute) Release(ctx context.Context, version string) (AgentRelease, error) {
	row, err := c.queries.AgentRelease(ctx, version)
	if errors.Is(err, pgx.ErrNoRows) {
		return AgentRelease{}, ErrNotFound
	}
	if err != nil {
		return AgentRelease{}, fmt.Errorf("read release: %w", err)
	}
	return releaseOf(row.Version, row.Sha256Amd64, row.Sha256Arm64, row.RolloutPercent, row.Build), nil
}

// AgentUpdate is the release a host should install.
type AgentUpdate struct {
	Version string
	URL     string
	SHA256  string
}

// UpdateFor returns the update host's agent must install, or nil: the
// target release when, by the agent's last Hello, it can update itself,
// runs another release, did not roll back from the target, the rollout
// reaches it and an archive exists for its architecture. Placement gives
// such a host no work.
func (c *Compute) UpdateFor(ctx context.Context, host HostID) (*AgentUpdate, error) {
	row, err := c.queries.AgentUpdate(ctx, uuid.UUID(host))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read agent update: %w", err)
	}
	return &AgentUpdate{
		Version: row.Version,
		URL:     fmt.Sprintf("%s/install/agent/%s/linux/%s", strings.TrimRight(c.config.InstallURL, "/"), row.Version, row.Architecture),
		SHA256:  row.Sha256,
	}, nil
}

// UpdateSent records that host was told to update: while it restarts into
// the release, within updateWindow, it is not lost.
func (c *Compute) UpdateSent(ctx context.Context, host HostID) error {
	if err := c.queries.MarkUpdating(ctx, MarkUpdatingParams{ID: uuid.UUID(host), Seconds: updateWindow.Seconds()}); err != nil {
		return fmt.Errorf("mark updating: %w", err)
	}
	return nil
}
