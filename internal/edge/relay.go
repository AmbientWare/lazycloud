package edge

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// With several servers an agent's data connection reaches one of them. An
// edge that needs a host whose connection another edge holds relays the
// request's stream to that edge, which pipes it to one of its parked
// streams; container choice, admission and the response stay with the edge
// the client reached. Each edge registers its relay address and a token in
// the database, and records the hosts whose Listen call it holds.
const (
	// edgeTTL is how long an edge's registration lives unless renewed
	// every edgeRenew.
	edgeTTL   = 30 * time.Second
	edgeRenew = 10 * time.Second
	// peerTTL is how long the edge remembers which edge holds a host.
	peerTTL = 2 * time.Second
	// relayCloseWait bounds how long a finished relayed stream waits for
	// the other edge to end it.
	relayCloseWait = 30 * time.Second

	hostMetadata = "lazycloud-host"
)

// forwardStream is one request's stream to an agent: parked on this edge,
// or relayed through the edge that holds the agent's data connection.
type forwardStream interface {
	Send(*hostproto.ForwardDown) error
	Recv() (*hostproto.ForwardUp, error)
	Context() context.Context
}

// relaying is the edge's side of relays: its registration and the
// connections to other edges.
type relaying struct {
	address string
	token   string
	mu      sync.Mutex
	peers   map[string]*grpc.ClientConn
	// links remembers which edge holds a host: its relay address, or ""
	// for none.
	links map[uuid.UUID]peerLink
	// tokens caches other edges' token digests.
	tokens map[uuid.UUID][]byte
}

type peerLink struct {
	address string
	at      time.Time
}

func newRelaying(address string) (*relaying, error) {
	secret := make([]byte, 32)
	if _, err := rand.Read(secret); err != nil {
		return nil, fmt.Errorf("make relay token: %w", err)
	}
	return &relaying{
		address: address, token: hex.EncodeToString(secret),
		peers: map[string]*grpc.ClientConn{}, links: map[uuid.UUID]peerLink{}, tokens: map[uuid.UUID][]byte{},
	}, nil
}

// registerOnce writes the edge's registration, before agents can link
// hosts to it.
func (e *Edge) registerOnce(ctx context.Context) error {
	digest := sha256.Sum256([]byte(e.relay.token))
	if err := e.queries.RegisterEdge(ctx, RegisterEdgeParams{
		ID: e.id, RelayAddress: e.relay.address, TokenSha256: digest[:], ExpiresAt: time.Now().Add(edgeTTL),
	}); err != nil {
		return fmt.Errorf("register the edge: %w", err)
	}
	return nil
}

// register keeps the edge's registration and host links current, and
// forgets expired edges, until ctx ends, then removes the registration.
func (e *Edge) register(ctx context.Context) {
	digest := sha256.Sum256([]byte(e.relay.token))
	renew := func() {
		if err := e.queries.RegisterEdge(ctx, RegisterEdgeParams{
			ID: e.id, RelayAddress: e.relay.address, TokenSha256: digest[:], ExpiresAt: time.Now().Add(edgeTTL),
		}); err != nil {
			if ctx.Err() == nil {
				e.logger.WarnContext(ctx, "renew the edge registration", "error", err)
			}
			return
		}
		if hosts := e.hosts.listening(); len(hosts) > 0 {
			if err := e.queries.LinkHosts(ctx, LinkHostsParams{EdgeID: e.id, HostIds: hosts}); err != nil && ctx.Err() == nil {
				e.logger.WarnContext(ctx, "restate the hosts' data connections", "error", err)
			}
		}
		if err := e.queries.ForgetExpiredEdges(ctx); err != nil && ctx.Err() == nil {
			e.logger.WarnContext(ctx, "forget expired edges", "error", err)
		}
	}
	ticker := time.NewTicker(edgeRenew)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			// Its host links go with it; agents reconnect elsewhere.
			_ = e.queries.ForgetEdge(context.WithoutCancel(ctx), e.id)
			e.relay.closePeers()
			return
		case <-ticker.C:
			renew()
		}
	}
}

func (r *relaying) closePeers() {
	r.mu.Lock()
	defer r.mu.Unlock()
	for address, conn := range r.peers {
		_ = conn.Close()
		delete(r.peers, address)
	}
}

// linkHost records that this edge holds the host's data connection.
func (e *Edge) linkHost(ctx context.Context, host uuid.UUID) {
	if err := e.queries.LinkHost(ctx, LinkHostParams{HostID: host, EdgeID: e.id}); err != nil && ctx.Err() == nil {
		e.logger.WarnContext(ctx, "record the host's data connection", "host_id", host, "error", err)
	}
}

func (e *Edge) unlinkHost(ctx context.Context, host uuid.UUID) {
	if err := e.queries.UnlinkHost(context.WithoutCancel(ctx), UnlinkHostParams{HostID: host, EdgeID: e.id}); err != nil {
		e.logger.WarnContext(ctx, "forget the host's data connection", "host_id", host, "error", err)
	}
}

// stream returns a stream to host's agent: one of this edge's when the
// agent's data connection is here, else one relayed through the edge that
// has it, else this edge's once the agent connects.
func (e *Edge) stream(ctx context.Context, host uuid.UUID) (*parkedStream, error) {
	if !e.hosts.connected(host) {
		peer, err := e.peerFor(ctx, host)
		if err != nil {
			return nil, err
		}
		if peer != "" {
			return e.relayTo(ctx, peer, host)
		}
	}
	return e.hosts.take(ctx, host)
}

// peerFor returns the relay address of the other edge that holds host, or
// "" for none.
func (e *Edge) peerFor(ctx context.Context, host uuid.UUID) (string, error) {
	r := e.relay
	now := time.Now()
	r.mu.Lock()
	link, known := r.links[host]
	r.mu.Unlock()
	if known && now.Sub(link.at) < peerTTL {
		return link.address, nil
	}
	row, err := e.queries.HostEdge(ctx, host)
	address := ""
	switch {
	case errors.Is(err, pgx.ErrNoRows):
	case err != nil:
		return "", fmt.Errorf("read the host's edge: %w", err)
	case row.ID != e.id:
		address = row.RelayAddress
	}
	r.mu.Lock()
	if len(r.links) >= maxCachedReleases {
		clear(r.links)
	}
	r.links[host] = peerLink{address: address, at: now}
	r.mu.Unlock()
	return address, nil
}

func (r *relaying) conn(address string) (*grpc.ClientConn, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if c := r.peers[address]; c != nil {
		return c, nil
	}
	// Relays stay inside the cluster; the token authenticates the edge.
	c, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, fmt.Errorf("dial edge %s: %w", address, err)
	}
	r.peers[address] = c
	return c, nil
}

// relayTo opens a relayed stream to host through the edge at address.
func (e *Edge) relayTo(ctx context.Context, address string, host uuid.UUID) (*parkedStream, error) {
	conn, err := e.relay.conn(address)
	if err != nil {
		return nil, err
	}
	// The stream outlives the call that opened it only through finish.
	streamCtx, cancel := context.WithCancel(context.WithoutCancel(ctx))
	streamCtx = metadata.AppendToOutgoingContext(streamCtx,
		"authorization", "Bearer "+e.id.String()+":"+e.relay.token, hostMetadata, host.String())
	stream, err := hostproto.NewEdgeRelayClient(conn).Forward(streamCtx)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("relay to edge %s: %w", address, err)
	}
	p := &parkedStream{stream: stream, done: make(chan struct{})}
	p.onFinish = func(err error) {
		if err != nil {
			cancel()
			return
		}
		// Done with the stream: the other edge ends its agent's stream
		// when it sees the half-close, and this side waits for that.
		_ = stream.CloseSend()
		go func() {
			defer cancel()
			timer := time.AfterFunc(relayCloseWait, cancel)
			defer timer.Stop()
			for {
				if _, err := stream.Recv(); err != nil {
					return
				}
			}
		}()
	}
	return p, nil
}

// RelayServer serves EdgeRelay for other edges.
type RelayServer struct {
	hostproto.UnimplementedEdgeRelayServer
	edge *Edge
}

// RelayServer returns the EdgeRelay service, for the relay listener.
func (e *Edge) RelayServer() *RelayServer { return &RelayServer{edge: e} }

// authenticate checks the calling edge's id and token and returns the
// host the call names.
func (s *RelayServer) authenticate(ctx context.Context) (uuid.UUID, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	unauthenticated := status.Error(codes.Unauthenticated, "an edge token is required")
	values := md.Get("authorization")
	if len(values) != 1 {
		return uuid.Nil, unauthenticated
	}
	credential, ok := strings.CutPrefix(values[0], "Bearer ")
	if !ok {
		return uuid.Nil, unauthenticated
	}
	rawID, token, ok := strings.Cut(credential, ":")
	caller, err := uuid.Parse(rawID)
	if !ok || err != nil {
		return uuid.Nil, unauthenticated
	}
	r := s.edge.relay
	r.mu.Lock()
	digest, known := r.tokens[caller]
	r.mu.Unlock()
	if !known {
		digest, err = s.edge.queries.EdgeToken(ctx, caller)
		if errors.Is(err, pgx.ErrNoRows) {
			return uuid.Nil, unauthenticated
		}
		if err != nil {
			return uuid.Nil, status.Error(codes.Unavailable, "read the edge's registration")
		}
		r.mu.Lock()
		r.tokens[caller] = digest
		r.mu.Unlock()
	}
	sum := sha256.Sum256([]byte(token))
	if subtle.ConstantTimeCompare(sum[:], digest) != 1 {
		return uuid.Nil, unauthenticated
	}
	hosts := md.Get(hostMetadata)
	if len(hosts) != 1 {
		return uuid.Nil, status.Error(codes.InvalidArgument, "the relay names no host")
	}
	host, err := uuid.Parse(hosts[0])
	if err != nil {
		return uuid.Nil, status.Error(codes.InvalidArgument, "the relay's host is not a UUID")
	}
	return host, nil
}

// Forward pipes another edge's request stream to one of this edge's parked
// streams of the host, until either side ends.
func (s *RelayServer) Forward(stream hostproto.EdgeRelay_ForwardServer) error {
	ctx := stream.Context()
	host, err := s.authenticate(ctx)
	if err != nil {
		return err
	}
	p, err := s.edge.hosts.take(ctx, host)
	if err != nil {
		return status.Error(codes.Unavailable, err.Error())
	}
	// The relaying edge's messages go to the agent. Its half-close means
	// it is done with the stream; its cancel, that the client left.
	toAgent := make(chan struct{})
	go func() { //nolint:gocritic // waited for below
		defer close(toAgent)
		for {
			msg, err := stream.Recv()
			if errors.Is(err, io.EOF) {
				p.finish(nil)
				return
			}
			if err != nil {
				p.finish(status.Error(codes.Canceled, "the relaying edge's client left"))
				return
			}
			if p.stream.Send(msg) != nil {
				return
			}
		}
	}()
	stop := context.AfterFunc(ctx, func() { p.finish(status.Error(codes.Canceled, "the relaying edge left")) })
	defer stop()
	for {
		up, err := p.stream.Recv()
		if err != nil {
			break
		}
		if stream.Send(up) != nil {
			p.finish(status.Error(codes.Canceled, "the relaying edge left"))
			break
		}
	}
	<-toAgent
	return nil
}
