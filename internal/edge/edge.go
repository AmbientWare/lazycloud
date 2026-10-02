package edge

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/sync/errgroup"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// Config tunes the edge.
type Config struct {
	// URL is the public base URL workloads answer under, such as
	// https://lazycloud.run.
	URL string
	// Domains registers customer hostnames; nil leaves custom domains
	// unavailable.
	Domains Provider
	// RelayAddress is where other servers' edges reach this one's
	// EdgeRelay service.
	RelayAddress string
	// TCPURL is the tls://host:port TCP pods answer under; empty serves
	// none.
	TCPURL string
	// ListenPool holds the route listener's connection; nil uses the
	// edge's pool.
	ListenPool *pgxpool.Pool
}

// Edge routes workload traffic. One edge runs in each server process; each
// publishes its own demand under its own id.
type Edge struct {
	id        uuid.UUID
	pool      *pgxpool.Pool
	listen    *pgxpool.Pool
	queries   *Queries
	identity  *identity.Identity
	execution *execution.Execution
	listener  *database.Listener
	urls      URLs
	domains   Provider
	logger    *slog.Logger

	routes atomic.Pointer[routeTable]
	// reloading serializes route reloads; reloadStarted is when the last
	// finished one began.
	reloading     sync.Mutex
	reloadStarted time.Time
	auth          authCache
	hosts         hostStreams
	bodies        bodyBudget
	relay         *relaying
	tcp           tcpURLs
	tcpTargets    tcpTargets
	podWaits      podWaits
	holds         containerHolds
	// records queues finished requests for writeRequests; droppedRecords
	// counts those a full queue refused.
	records        chan requestRecord
	droppedRecords atomic.Int64

	// mu guards releases, versions, workloads and loads. It is held only for
	// map and counter updates, never across I/O.
	mu        sync.Mutex
	releases  map[uuid.UUID]*release
	versions  map[versionKey]uuid.UUID
	workloads map[uuid.UUID]*workloadState
	loads     map[uuid.UUID]*releaseLoad
	misses    map[uuid.UUID]time.Time
	// refresh holds workloads whose container set changed; publish kicks
	// the demand publisher.
	refresh chan uuid.UUID
	publish chan struct{}
}

type versionKey struct {
	workload uuid.UUID
	version  int
}

// NewEdge returns the edge. listener must listen on database.ChannelTask
// for function invocations.
func NewEdge(pool *pgxpool.Pool, id *identity.Identity, exec *execution.Execution, listener *database.Listener, cfg Config, logger *slog.Logger) (*Edge, error) {
	urls, err := ParseURLs(cfg.URL)
	if err != nil {
		return nil, err
	}
	e := &Edge{
		id: uuid.New(), pool: pool, listen: cmp.Or(cfg.ListenPool, pool), queries: New(pool), identity: id, execution: exec, listener: listener,
		urls: urls, domains: cfg.Domains, logger: logger,
		releases:  map[uuid.UUID]*release{},
		versions:  map[versionKey]uuid.UUID{},
		workloads: map[uuid.UUID]*workloadState{},
		loads:     map[uuid.UUID]*releaseLoad{},
		misses:    map[uuid.UUID]time.Time{},
		refresh:   make(chan uuid.UUID, refreshQueue),
		publish:   make(chan struct{}, 1),
		records:   make(chan requestRecord, requestQueue),
	}
	e.auth.entries = map[authKey]time.Time{}
	e.holds.first = make(chan struct{}, 1)
	if cfg.TCPURL != "" {
		if e.tcp, err = parseTCPURL(cfg.TCPURL); err != nil {
			return nil, err
		}
	}
	relay, err := newRelaying(cfg.RelayAddress)
	if err != nil {
		return nil, err
	}
	e.relay = relay
	e.hosts.hosts = map[uuid.UUID]*hostPool{}
	e.hosts.shut = make(chan struct{})
	e.routes.Store(&routeTable{
		bySubdomain: map[string]*workload{}, byHostname: map[string]*workload{}, byID: map[uuid.UUID]*workload{},
	})
	return e, nil
}

// URLs builds the addresses workloads answer on.
func (e *Edge) URLs() URLs { return e.urls }

// refreshQueue bounds workloads waiting for their container set to reload;
// a full queue drops the wake and the next wait reloads instead.
const refreshQueue = 1024

// Run keeps the route table and container sets current from NOTIFY, and
// publishes demand, until ctx ends.
func (e *Edge) Run(ctx context.Context) error {
	if err := e.registerOnce(ctx); err != nil {
		return err
	}
	routes, err := e.loadRoutes(ctx)
	if err != nil {
		return err
	}
	e.routes.Store(routes)
	g, ctx := errgroup.WithContext(ctx)
	g.Go(func() error { return e.watch(ctx) })
	g.Go(func() error { e.refreshContainers(ctx); return nil })
	g.Go(func() error { e.publishLoads(ctx); return nil })
	g.Go(func() error { e.reconcileDomains(ctx); return nil })
	g.Go(func() error { e.writeRequests(ctx); return nil })
	g.Go(func() error { e.register(ctx); return nil })
	g.Go(func() error { e.renewHolds(ctx); return nil })
	if err := g.Wait(); err != nil && !errors.Is(err, context.Canceled) {
		return fmt.Errorf("edge: %w", err)
	}
	return nil
}
