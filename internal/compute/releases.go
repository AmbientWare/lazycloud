package compute

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
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
}

// updateWindow is how long an updating agent may stay away before its host
// counts as lost: three wrapper starts and a rollback fit in it.
const updateWindow = 5 * time.Minute

func releaseOf(version string, amd64, arm64 *string, rollout int32) AgentRelease {
	r := AgentRelease{Version: version, SHA256: map[string]string{}, RolloutPercent: int(rollout)}
	if amd64 != nil {
		r.SHA256["amd64"] = *amd64
	}
	if arm64 != nil {
		r.SHA256["arm64"] = *arm64
	}
	return r
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
	return releaseOf(row.Version, row.Sha256Amd64, row.Sha256Arm64, row.RolloutPercent), nil
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
	return releaseOf(row.Version, row.Sha256Amd64, row.Sha256Arm64, row.RolloutPercent), nil
}

// AgentUpdate is the release a host should install.
type AgentUpdate struct {
	Version string
	URL     string
	SHA256  string
}

// UpdateFor returns the update a connecting agent should install: the
// target release when the agent can update itself, runs another version,
// did not roll back from the target, and an archive exists for its
// architecture.
func (c *Compute) UpdateFor(ctx context.Context, host HostID, version, rejected string, updatable bool) (*AgentUpdate, error) {
	if !updatable {
		return nil, nil
	}
	target, err := c.TargetRelease(ctx)
	if errors.Is(err, ErrNotFound) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if target.Version == version || target.Version == rejected || rolloutBucket(host) >= target.RolloutPercent {
		return nil, nil
	}
	arch, err := c.queries.HostArchitecture(ctx, uuid.UUID(host))
	if err != nil {
		return nil, fmt.Errorf("read host architecture: %w", err)
	}
	digest, ok := target.SHA256[arch]
	if !ok {
		return nil, nil
	}
	return &AgentUpdate{
		Version: target.Version,
		URL:     fmt.Sprintf("%s/install/agent/%s/linux/%s", strings.TrimRight(c.config.InstallURL, "/"), target.Version, arch),
		SHA256:  digest,
	}, nil
}

// rolloutBucket places a host in 0-99, stable across releases, so a
// percentage rollout reaches the same hosts first each time.
func rolloutBucket(host HostID) int {
	sum := sha256.Sum256(host[:])
	return int(binary.BigEndian.Uint16(sum[:2])) % 100
}

// UpdateSent records that host was told to update: while it restarts into
// the release, within updateWindow, it is not lost.
func (c *Compute) UpdateSent(ctx context.Context, host HostID) error {
	if err := c.queries.MarkUpdating(ctx, MarkUpdatingParams{ID: uuid.UUID(host), Seconds: updateWindow.Seconds()}); err != nil {
		return fmt.Errorf("mark updating: %w", err)
	}
	return nil
}
