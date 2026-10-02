package compute

import (
	"context"
	"errors"
	"net/http"
	"net/url"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	// ErrInvalidJoinToken means the join token is unknown, used or expired.
	ErrInvalidJoinToken = errors.New("invalid join token")
	// ErrUnknownHost means the host credential matches no enrolled host, or
	// the host is retired.
	ErrUnknownHost = errors.New("unknown host")
	// ErrNotFound means the requested compute resource does not exist.
	ErrNotFound = errors.New("not found")
)

// ConflictError refuses a request that conflicts with current state.
type ConflictError struct{ Message string }

func (e *ConflictError) Error() string { return e.Message }

// InvalidError refuses a malformed request.
type InvalidError struct{ Message string }

func (e *InvalidError) Error() string { return e.Message }

// NotFoundError names what was not found.
type NotFoundError struct{ Message string }

func (e *NotFoundError) Error() string { return e.Message }

// UnavailableError means a dependency the request needs is missing or down.
type UnavailableError struct{ Message string }

func (e *UnavailableError) Error() string { return e.Message }

// IdentityError refuses a cloud host's identity proof.
type IdentityError struct{ Message string }

func (e *IdentityError) Error() string { return e.Message }

// Containers is execution's side of host transitions: compute decides that a
// host stops serving and execution moves its containers in the same
// transaction. Execution already depends on compute for host liveness, so
// compute names this here rather than importing it.
type Containers interface {
	// StopHostContainers stops every live container on host; running
	// attempts are lost and retried by policy.
	StopHostContainers(ctx context.Context, tx pgx.Tx, host HostID, message string) (int, error)
	// DrainHostContainers stops claims on the host's ready containers and
	// stops the ones still starting.
	DrainHostContainers(ctx context.Context, tx pgx.Tx, host HostID) error
	// DrainHostWorkspaces drains the host's containers of workspaces.
	DrainHostWorkspaces(ctx context.Context, tx pgx.Tx, host HostID, workspaces []uuid.UUID) error
}

// Config is what compute needs to tell hosts how to reach the platform.
type Config struct {
	// InstallURL is the HTTP origin hosts download the agent from.
	InstallURL string
	// ServerAddress is the host:port agents dial for the host connection.
	ServerAddress string
	// ServerPlaintext means agents dial ServerAddress without TLS, which
	// they accept only for a loopback address. Otherwise they dial with TLS
	// and verify the server's certificate.
	ServerPlaintext bool
	// Fleet is the platform's AWS capacity.
	Fleet Fleet
}

// Compute is the compute owner.
type Compute struct {
	pool       *pgxpool.Pool
	queries    *Queries
	containers Containers
	config     Config
	fleet      Fleet
	// http sends identity proofs to STS.
	http *http.Client
}

// NewCompute returns the compute owner over pool. sqlc's generated New
// constructs the package's Queries.
func NewCompute(pool *pgxpool.Pool, containers Containers, config Config) *Compute {
	return &Compute{
		pool: pool, queries: New(pool), containers: containers, config: config, fleet: config.Fleet.withDefaults(),
		http: &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
	}
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

// CheckFleet refuses a configuration that would launch instances unable to
// reach the platform: with fleet networks or connections enabled, instances
// download the agent from InstallURL over HTTPS and dial ServerAddress with
// TLS, so neither may be empty or on loopback.
func (c Config) CheckFleet() error {
	if len(c.Fleet.Networks) == 0 && c.Fleet.PrincipalARN == "" {
		return nil
	}
	u, err := url.Parse(c.InstallURL)
	if c.InstallURL == "" || err != nil || u.Scheme != "https" || loopback(c.InstallURL) {
		return errors.New("LAZYCLOUD_INSTALL_URL must be the public https origin cloud instances download the agent from")
	}
	if c.ServerAddress == "" || PlaintextAgents(c.ServerAddress, false) || c.ServerPlaintext {
		return errors.New("LAZYCLOUD_AGENT_SERVER_ADDR must be the public host:port cloud instances dial with TLS")
	}
	return nil
}
