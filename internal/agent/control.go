package agent

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// tunnelProtocol is the upgrade the supervisor answers with a raw byte
// tunnel, to SSH or to a port.
const tunnelProtocol = "lazycloud-tunnel"

// maxPortErrorBytes bounds the refusal body kept from the supervisor.
const maxPortErrorBytes = 1 << 10

// socketTransport speaks HTTP/1.1 to a supervisor socket, keeping
// connections alive between requests.
func socketTransport(socket string) *http.Transport {
	return &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		},
		MaxIdleConnsPerHost: maxIdleRequestConns,
		IdleConnTimeout:     90 * time.Second,
		DisableCompression:  true,
	}
}

// portTransport reaches the container's ports through its supervisor. Each
// connection is a tunnel the supervisor opens to 127.0.0.1:port inside the
// container, which the transport then speaks HTTP over; requests address
// the port as http://127.0.0.1:port, so each port keeps its own pool.
func portTransport(control string) *http.Transport {
	return &http.Transport{
		DialContext: func(ctx context.Context, _, address string) (net.Conn, error) {
			_, portText, err := net.SplitHostPort(address)
			if err != nil {
				return nil, &net.OpError{Op: "dial", Net: "tcp", Err: err}
			}
			port, err := strconv.Atoi(portText)
			if err != nil {
				return nil, &net.OpError{Op: "dial", Net: "tcp", Err: err}
			}
			return dialPort(ctx, control, port)
		},
		MaxIdleConnsPerHost: maxIdleRequestConns,
		IdleConnTimeout:     90 * time.Second,
		DisableCompression:  true,
	}
}

// dialPort asks the supervisor for a tunnel to port. Any failure before the
// tunnel opens is a dial error: the workload never saw the request.
func dialPort(ctx context.Context, control string, port int) (net.Conn, error) {
	conn, err := (&net.Dialer{}).DialContext(ctx, "unix", control)
	if err != nil {
		return nil, fmt.Errorf("reach the supervisor: %w", err)
	}
	fail := func(err error) (net.Conn, error) {
		_ = conn.Close()
		return nil, &net.OpError{Op: "dial", Net: "tcp", Err: err}
	}
	// A cancelled dial interrupts the handshake.
	stop := context.AfterFunc(ctx, func() { _ = conn.SetDeadline(time.Unix(1, 0)) })
	request := fmt.Sprintf("GET /ports/%d HTTP/1.1\r\nHost: container\r\nConnection: Upgrade\r\nUpgrade: %s\r\n\r\n", port, tunnelProtocol)
	if _, err := io.WriteString(conn, request); err != nil {
		stop()
		return fail(fmt.Errorf("request a tunnel to port %d: %w", port, err))
	}
	reader := bufio.NewReader(conn)
	response, err := http.ReadResponse(reader, nil)
	if !stop() {
		return fail(fmt.Errorf("tunnel to port %d: %w", port, ctx.Err()))
	}
	if err != nil {
		return fail(fmt.Errorf("tunnel to port %d: %w", port, err))
	}
	if response.StatusCode != http.StatusSwitchingProtocols {
		body, _ := io.ReadAll(io.LimitReader(response.Body, maxPortErrorBytes))
		_ = response.Body.Close()
		return fail(fmt.Errorf("port %d refused: %s %s", port, response.Status, strings.TrimSpace(string(body))))
	}
	return &bufferedConn{Conn: conn, reader: reader}, nil
}

// bufferedConn reads first what the handshake's reader already buffered.
type bufferedConn struct {
	net.Conn
	reader *bufio.Reader
}

func (c *bufferedConn) Read(p []byte) (int, error) {
	return c.reader.Read(p) //nolint:wrapcheck // Readers compare io.EOF.
}
