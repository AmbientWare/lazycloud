package compute

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

var (
	// ErrInvalidJoinToken means the join token is unknown, used or expired.
	ErrInvalidJoinToken = errors.New("invalid join token")
	// ErrUnknownHost means the host credential matches no enrolled host, or
	// the host is retired.
	ErrUnknownHost = errors.New("unknown host")
)

// Capacity is what a host offers to containers.
type Capacity struct {
	CPUMillis   int64
	MemoryBytes int64
}

// CreateJoinToken issues a single-use token that enrolls one host before
// ttl passes.
func (c *Compute) CreateJoinToken(ctx context.Context, ttl time.Duration) (string, time.Time, error) {
	token, digest, err := identity.NewToken()
	if err != nil {
		return "", time.Time{}, err
	}
	expires, err := c.queries.InsertJoinToken(ctx, InsertJoinTokenParams{TokenHash: digest, TtlSeconds: ttl.Seconds()})
	if err != nil {
		return "", time.Time{}, fmt.Errorf("insert join token: %w", err)
	}
	return token, expires, nil
}

// Enroll spends a join token and registers a host. The host token is
// returned once; the host presents it on every later call. The host stays
// offline until its first session opens.
func (c *Compute) Enroll(ctx context.Context, joinToken, name string, capacity Capacity) (HostID, string, error) {
	hostToken, digest, err := identity.NewToken()
	if err != nil {
		return HostID{}, "", err
	}
	var id uuid.UUID
	err = pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		if _, err := q.UseJoinToken(ctx, identity.HashToken(joinToken)); errors.Is(err, pgx.ErrNoRows) {
			return ErrInvalidJoinToken
		} else if err != nil {
			return fmt.Errorf("use join token: %w", err)
		}
		id, err = q.InsertHost(ctx, InsertHostParams{
			Name: name, TokenHash: digest, CpuMillis: capacity.CPUMillis, MemoryBytes: capacity.MemoryBytes,
		})
		if err != nil {
			return fmt.Errorf("insert host: %w", err)
		}
		return nil
	})
	if err != nil {
		return HostID{}, "", err
	}
	return HostID(id), hostToken, nil
}

// AuthenticateHost resolves a host token.
func (c *Compute) AuthenticateHost(ctx context.Context, token string) (HostID, error) {
	id, err := c.queries.HostByToken(ctx, identity.HashToken(token))
	if errors.Is(err, pgx.ErrNoRows) {
		return HostID{}, ErrUnknownHost
	}
	if err != nil {
		return HostID{}, fmt.Errorf("authenticate host: %w", err)
	}
	return HostID(id), nil
}

// SessionEpoch orders a host's sessions; only the latest acts for the host.
type SessionEpoch int64

// OpenSession brings host online with the capacity and boot it reported and
// returns the epoch that supersedes every earlier session.
func (c *Compute) OpenSession(ctx context.Context, host HostID, bootID string, capacity Capacity) (SessionEpoch, error) {
	epoch, err := c.queries.OpenHostSession(ctx, OpenHostSessionParams{
		ID: uuid.UUID(host), BootID: bootID, CpuMillis: capacity.CPUMillis, MemoryBytes: capacity.MemoryBytes,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, ErrUnknownHost
	}
	if err != nil {
		return 0, fmt.Errorf("open host session: %w", err)
	}
	return SessionEpoch(epoch), nil
}

// Touch records that the session with epoch is alive. It reports false once
// a newer session opened or the host was declared lost; that session must
// close.
func (c *Compute) Touch(ctx context.Context, host HostID, epoch SessionEpoch) (bool, error) {
	touched, err := c.queries.TouchHost(ctx, TouchHostParams{ID: uuid.UUID(host), SessionEpoch: int64(epoch)})
	if err != nil {
		return false, fmt.Errorf("touch host: %w", err)
	}
	return touched == 1, nil
}
