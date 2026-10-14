//go:build hostruntime

package acceptance

import (
	"context"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// With several servers a request may reach an edge without the agent's
// data connection; it relays the request to the edge that has it. Through
// the relay a request answers, an event stream streams, a WebSocket
// upgrades and echoes, and a response that breaks off ends the relayed
// request and frees its capacity.
func TestRequestRelaysToTheEdgeHoldingTheHost(t *testing.T) {
	t.Parallel()
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": faultApp, "web.py": asgiApp})
	p.deploy("faults", faultSpec(source), asgiSpec(source, "service", "web:service", apitypes.HttpKindAsgi, 4))
	base := p.describe("faults", apitypes.WorkloadKindAsgi, "faults").Url
	service := p.describe("faults", apitypes.WorkloadKindAsgi, "service").Url
	// Warm through the first edge, which holds the agent's connection.
	expectOK(t, p, base+"/ok", "warm-up")
	expectOK(t, p, service+"/headers", "warm-up")

	other := p.startEdge()
	client := &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{DialContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, network, other)
	}}}
	call := func(url string) (int, string, error) {
		req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, url, nil)
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Authorization", "Bearer "+p.token)
		resp, err := client.Do(req)
		if err != nil {
			return 0, "", err
		}
		defer func() { _ = resp.Body.Close() }()
		body, err := io.ReadAll(resp.Body)
		return resp.StatusCode, string(body), err
	}
	if status, body, err := call(base + "/ok"); err != nil || status != http.StatusOK || body != "ok" {
		t.Fatalf("through the other edge: %d %q %v", status, body, err)
	}
	if status, body, err := call(service + "/events"); err != nil || status != http.StatusOK || !strings.Contains(body, "data: 2") {
		t.Fatalf("events through the other edge: %d %q %v", status, body, err)
	}

	ws := p.dialWebSocketVia(other, service+"/ws", p.token)
	ws.send("relayed")
	if got := ws.receive(); got != "echo:relayed" {
		t.Fatalf("websocket echo through the relay %q", got)
	}
	ws.close()

	for range 3 {
		if _, _, err := call(base + "/broken"); err == nil {
			t.Fatal("a broken response arrived complete through the relay")
		}
	}
	if status, _, err := call(base + "/ok"); err != nil || status != http.StatusOK {
		t.Fatalf("after broken responses through the relay: %d %v", status, err)
	}
}
