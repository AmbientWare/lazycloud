package edge

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
)

// TCP pods answer raw TCP at tls://<release or container>-<port>.<tcp host>:
// the edge terminates TLS, reads the hostname the client sent as SNI and
// tunnels the bytes to the port through the container's supervisor. TCP
// pods are public; the workload authenticates its own clients.

const (
	// maxTCPConnections bounds the TCP connections one edge relays at once.
	maxTCPConnections   = 1024
	tcpHandshakeTimeout = 10 * time.Second
)

// TCPConfig is where TCP pods answer.
type TCPConfig struct {
	// URL is the public tls://host:port base, such as
	// tls://tcp.lazycloud.run:1995.
	URL string
	// Certificate covers *.<host>.
	Certificate tls.Certificate
}

type tcpURLs struct {
	host string
	port string
}

func parseTCPURL(raw string) (tcpURLs, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "tls" || u.Hostname() == "" {
		return tcpURLs{}, fmt.Errorf("tcp URL %q must be tls://host[:port]", raw)
	}
	return tcpURLs{host: strings.ToLower(u.Hostname()), port: u.Port()}, nil
}

func (e *Edge) tcpURL(id uuid.UUID, port int) string {
	if e.tcp.host == "" {
		return ""
	}
	host := id.String() + "-" + strconv.Itoa(port) + "." + e.tcp.host
	if e.tcp.port != "" {
		host = net.JoinHostPort(host, e.tcp.port)
	}
	return "tls://" + host
}

// ServeTCP relays TLS connections on listener to TCP pods until ctx ends.
func (e *Edge) ServeTCP(ctx context.Context, listener net.Listener, cfg TCPConfig) error {
	tlsConfig := &tls.Config{Certificates: []tls.Certificate{cfg.Certificate}, MinVersion: tls.VersionTLS12}
	slots := make(chan struct{}, maxTCPConnections)
	var conns sync.WaitGroup
	defer conns.Wait()
	stop := context.AfterFunc(ctx, func() { _ = listener.Close() })
	defer stop()
	for {
		raw, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return nil
			}
			return fmt.Errorf("accept tcp connection: %w", err)
		}
		select {
		case slots <- struct{}{}:
		default:
			_ = raw.Close()
			continue
		}
		conns.Go(func() {
			defer func() { <-slots }()
			e.relayTCP(ctx, tls.Server(raw, tlsConfig))
		})
	}
}

func (e *Edge) relayTCP(ctx context.Context, conn *tls.Conn) {
	defer func() { _ = conn.Close() }()
	handshake, cancel := context.WithTimeout(ctx, tcpHandshakeTimeout)
	err := conn.HandshakeContext(handshake)
	cancel()
	if err != nil {
		return
	}
	sni := strings.ToLower(conn.ConnectionState().ServerName)
	label, ok := strings.CutSuffix(sni, "."+e.tcp.host)
	if !ok {
		return
	}
	id, port, ok := portLabel(label)
	if !ok {
		return
	}
	t, err := e.resolvePod(ctx, id, port)
	if err != nil || t.authorized() || t.spec.Pod == nil || t.spec.Pod.Tcp == nil || !*t.spec.Pod.Tcp {
		return
	}
	connCtx, cancelConn := context.WithCancel(ctx)
	defer cancelConn()
	container, host, err := e.podContainer(connCtx, t, time.Now().Add(PodConnectTimeout))
	if err != nil {
		e.logger.InfoContext(ctx, "tcp connection found no container", "sni", sni, "error", err)
		return
	}
	defer e.holds.hold(uuid.UUID(container))()
	req, err := http.NewRequestWithContext(connCtx, http.MethodGet, "http://container/ports/"+strconv.Itoa(port), http.NoBody)
	if err != nil {
		return
	}
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Upgrade", TunnelProtocol)
	resp, err := e.RoundTrip(connCtx, host, uuid.UUID(container), ControlAPI, req)
	if err != nil {
		return
	}
	tunnel, ok := resp.Body.(interface {
		Read([]byte) (int, error)
		Write([]byte) (int, error)
		Close() error
	})
	if resp.StatusCode != http.StatusSwitchingProtocols || !ok {
		_ = resp.Body.Close()
		return
	}
	Splice(conn, conn, tunnel)
}

// TunnelProtocol is the Upgrade token the supervisor answers with a raw
// tunnel to SSH or a port.
const TunnelProtocol = "lazycloud-tunnel"
