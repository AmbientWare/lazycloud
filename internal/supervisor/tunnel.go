package supervisor

import (
	"bufio"
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"

	"github.com/AmbientWare/lazycloud/internal/apitypes" //nolint:depguard // the control API bodies are the public schemas; the rule denies internal/api by prefix
)

// TunnelProtocol is the Upgrade token of SSH and port tunnels.
const TunnelProtocol = "lazycloud-tunnel"

// stream is a byte stream that can end its writing half alone.
type stream interface {
	io.ReadWriteCloser
	CloseWrite() error
}

// hijackedConn is a hijacked connection that first returns what the HTTP
// server had already buffered.
type hijackedConn struct {
	net.Conn
	reader *bufio.Reader
}

func (c *hijackedConn) Read(p []byte) (int, error) { return c.reader.Read(p) } //nolint:wrapcheck // io.Reader semantics

func (c *hijackedConn) CloseWrite() error {
	if cw, ok := c.Conn.(interface{ CloseWrite() error }); ok {
		return cw.CloseWrite() //nolint:wrapcheck // passes the connection's own error
	}
	return nil
}

func hasToken(header http.Header, name, token string) bool {
	for _, value := range header.Values(name) {
		for part := range strings.SplitSeq(value, ",") {
			if strings.EqualFold(strings.TrimSpace(part), token) {
				return true
			}
		}
	}
	return false
}

// errHijacked ends a handler whose connection was taken over; nothing more
// can be answered on it.
var errHijacked = errors.New("the connection was hijacked") //nolint:gochecknoglobals // sentinel

// upgrade answers 101 to a tunnel request and returns its connection.
func upgrade(w http.ResponseWriter, r *http.Request) (*hijackedConn, error) {
	if !hasToken(r.Header, "Connection", "upgrade") || !hasToken(r.Header, "Upgrade", TunnelProtocol) {
		return nil, invalid("a tunnel needs Connection: Upgrade and Upgrade: %s", TunnelProtocol)
	}
	conn, rw, err := http.NewResponseController(w).Hijack()
	if err != nil {
		return nil, apiErr(http.StatusInternalServerError, apitypes.Internal, "hijack the connection: %v", err)
	}
	if _, err := conn.Write([]byte("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: " + TunnelProtocol + "\r\n\r\n")); err != nil {
		_ = conn.Close()
		return nil, errHijacked
	}
	return &hijackedConn{Conn: conn, reader: rw.Reader}, nil
}

// openPort tunnels to a TCP port of the container.
func (c *control) openPort(w http.ResponseWriter, r *http.Request) error {
	port, err := strconv.Atoi(r.PathValue("port"))
	if err != nil || port < 1 || port > 65535 {
		return invalid("port must be an integer from 1 to 65535")
	}
	dialCtx, cancel := context.WithTimeout(r.Context(), forwardDialTimeout)
	target, err := (&net.Dialer{}).DialContext(dialCtx, "tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	cancel()
	if err != nil {
		return apiErr(http.StatusBadGateway, apitypes.Unavailable, "nothing accepts connections on port %d: %v", port, err)
	}
	conn, err := upgrade(w, r)
	if err != nil {
		_ = target.Close()
		return err
	}
	//nolint:contextcheck,forcetypeassert // a tunnel lives until the control server closes; a tcp dial returns a TCPConn
	splice(c.ctx, conn, target.(*net.TCPConn))
	return nil
}

// splice copies both ways, passing each half-close on, until both
// directions end or ctx does, then closes both.
func splice(ctx context.Context, a, b stream) {
	stop := context.AfterFunc(ctx, func() {
		_ = a.Close()
		_ = b.Close()
	})
	defer stop()
	var copies sync.WaitGroup
	copies.Go(func() {
		_, _ = io.Copy(b, a)
		_ = b.CloseWrite()
	})
	copies.Go(func() {
		_, _ = io.Copy(a, b)
		_ = a.CloseWrite()
	})
	copies.Wait()
	_ = a.Close()
	_ = b.Close()
}
