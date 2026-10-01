package compute

import (
	"github.com/jackc/pgx/v5/pgxpool"
)

// Compute is the compute owner.
type Compute struct {
	pool    *pgxpool.Pool
	queries *Queries
}

// NewCompute returns the compute owner over pool. sqlc's generated New
// constructs the package's Queries.
func NewCompute(pool *pgxpool.Pool) *Compute {
	return &Compute{pool: pool, queries: New(pool)}
}
