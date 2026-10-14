package execution

import (
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Execution is the execution owner. The server uses its admission, claim,
// completion and report operations; the scheduler uses planning, draining and
// recovery. Both share the transitions in this package.
type Execution struct {
	// LogSettle bounds how long a log read without follow waits for lines
	// in flight. A transaction that runs longer holds back every newer
	// line, since visibility is cluster-wide. NewExecution sets
	// DefaultLogSettle.
	LogSettle time.Duration

	pool    *pgxpool.Pool
	queries *Queries
}

// DefaultLogSettle is how long production log reads wait for lines in
// flight.
const DefaultLogSettle = 2 * time.Second

// NewExecution returns the execution owner over pool. sqlc's generated New
// constructs the package's Queries.
func NewExecution(pool *pgxpool.Pool) *Execution {
	return &Execution{LogSettle: DefaultLogSettle, pool: pool, queries: New(pool)}
}
