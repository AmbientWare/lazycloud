package compute

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// Capacity is what a host offers to containers.
type Capacity struct {
	CPUMillis   int64
	MemoryBytes int64
	GPUType     string
	GPUCount    int
}

// HostReport is what a host states about itself when it enrolls.
type HostReport struct {
	Hostname     string
	Capacity     Capacity
	Architecture string
	Preflight    []PreflightCheck
}

func (r HostReport) architecture() string {
	if r.Architecture == "arm64" {
		return "arm64"
	}
	return "amd64"
}

// CreateJoinToken issues a single-use token that enrolls one platform host
// before ttl passes.
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

// Enroll spends a join token and registers the host. A machine token binds
// the host to its machine; a platform token creates a platform host. The
// host token is returned once; the host presents it on every later call. A
// machine whose error-severity preflight check failed is enrolled as failed
// so its owner sees why, and never takes work.
func (c *Compute) Enroll(ctx context.Context, joinToken string, report HostReport) (HostID, string, error) {
	hostToken, digest, err := identity.NewToken()
	if err != nil {
		return HostID{}, "", err
	}
	preflight, err := json.Marshal(nonNil(report.Preflight))
	if err != nil {
		return HostID{}, "", fmt.Errorf("encode preflight: %w", err)
	}
	var id uuid.UUID
	err = pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		used, err := q.UseJoinToken(ctx, identity.HashToken(joinToken))
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrInvalidJoinToken
		}
		if err != nil {
			return fmt.Errorf("use join token: %w", err)
		}
		if used.HostID == nil {
			id, err = q.InsertPlatformHost(ctx, InsertPlatformHostParams{
				Name: report.Hostname, TokenHash: digest,
				CpuMillis: report.Capacity.CPUMillis, MemoryBytes: report.Capacity.MemoryBytes,
				GpuType: report.Capacity.GPUType, GpuCount: int32(report.Capacity.GPUCount), //nolint:gosec // GPU counts are small.
				Architecture: report.architecture(), Preflight: preflight,
			})
			if err != nil {
				return fmt.Errorf("insert host: %w", err)
			}
			return nil
		}
		phase, message, failure := PhaseJoining, PhaseJoining.Message(), (*string)(nil)
		if failed := failedChecks(report.Preflight); len(failed) > 0 {
			phase, message = PhaseFailed, strings.Join(failed, "; ")
			failure = ptr(string(FailurePreflight))
		}
		id, err = q.EnrollMachine(ctx, EnrollMachineParams{
			ID: *used.HostID, TokenHash: digest, Phase: string(phase), PhaseMessage: message, Failure: failure,
			CpuMillis: report.Capacity.CPUMillis, MemoryBytes: report.Capacity.MemoryBytes,
			GpuType: report.Capacity.GPUType, GpuCount: int32(report.Capacity.GPUCount), //nolint:gosec // GPU counts are small.
			Architecture: report.architecture(), Preflight: preflight,
		})
		if errors.Is(err, pgx.ErrNoRows) {
			// The machine joined with another token meanwhile or was removed.
			return ErrInvalidJoinToken
		}
		if err != nil {
			return fmt.Errorf("enroll machine: %w", err)
		}
		return notifyMachines(ctx, tx, id)
	})
	if err != nil {
		return HostID{}, "", fmt.Errorf("enroll: %w", err)
	}
	return HostID(id), hostToken, nil
}

// failedChecks are the messages of failed error-severity checks.
func failedChecks(checks []PreflightCheck) []string {
	var out []string
	for _, c := range checks {
		if !c.OK && c.Severity == "error" {
			out = append(out, c.Message)
		}
	}
	return out
}

func nonNil[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}

func ptr[T any](v T) *T { return &v }

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

// SessionOpen is what a host reports when its session opens.
type SessionOpen struct {
	BootID       string
	Capacity     Capacity
	AgentVersion string
}

// OpenSession brings host online with the capacity it reported and returns
// the epoch that supersedes every earlier session. A joining host becomes
// ready.
func (c *Compute) OpenSession(ctx context.Context, host HostID, open SessionOpen) (SessionEpoch, error) {
	var epoch int64
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		var err error
		epoch, err = c.queries.WithTx(tx).OpenHostSession(ctx, OpenHostSessionParams{
			ID: uuid.UUID(host), BootID: open.BootID, AgentVersion: open.AgentVersion,
			CpuMillis: open.Capacity.CPUMillis, MemoryBytes: open.Capacity.MemoryBytes,
			GpuType: open.Capacity.GPUType, GpuCount: int32(open.Capacity.GPUCount), //nolint:gosec // GPU counts are small.
		})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrUnknownHost
		}
		if err != nil {
			return fmt.Errorf("open host session: %w", err)
		}
		return notifyMachines(ctx, tx, uuid.UUID(host))
	})
	if err != nil {
		return 0, fmt.Errorf("open session: %w", err)
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
