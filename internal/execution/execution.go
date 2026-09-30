package execution

import (
	"github.com/jackc/pgx/v5/pgxpool"
)

// Execution is the execution owner. The server uses its admission, claim,
// completion and report operations; the scheduler uses planning, draining and
// recovery. Both share the transitions in this package.
type Execution struct {
	pool    *pgxpool.Pool
	queries *Queries
}

// NewExecution returns the execution owner over pool. sqlc's generated New
// constructs the package's Queries.
func NewExecution(pool *pgxpool.Pool) *Execution {
	return &Execution{pool: pool, queries: New(pool)}
}
