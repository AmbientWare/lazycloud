package acceptance

import (
	"bufio"
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
// data connection; it relays the request to the edge that has it, and the
// response, a stream included, comes back the same way.
func TestRequestRelaysToTheEdgeHoldingTheHost(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": endpointApp})
	p.deploy("api_demo", endpointSpec(source, "count_words", "app:count_words", "/word-count", apitypes.HttpMethodPOST))
	w := p.describe("api_demo", apitypes.WorkloadKindEndpoint, "count_words")
	// Warm through the first edge, which holds the agent's connection.
	if status, _, body := p.call(http.MethodPost, w.Url, `{"text": "a b"}`); status != http.StatusOK {
		t.Fatalf("through the agent's edge: %d %s", status, body)
	}

	other := p.startEdge()
	client := &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, network, other)
	}}}
	var samples []time.Duration
	for range 100 {
		began := time.Now()
		req, err := http.NewRequestWithContext(t.Context(), http.MethodPost, w.Url, strings.NewReader(`{"text": "one two three"}`))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Authorization", "Bearer "+p.token)
		resp, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		body, _ := bufio.NewReader(resp.Body).ReadString('}')
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusOK || body != `{"words": 3}` {
			t.Fatalf("through the other edge: %d %s", resp.StatusCode, body)
		}
		samples = append(samples, time.Since(began))
	}
	t.Logf("warm through a relaying edge over 100: p50 %v, p95 %v", percentile(samples, 0.5), percentile(samples, 0.95))
}

// Through a relaying edge a WebSocket upgrades and echoes, and a response
// that breaks off ends the relayed request and frees its capacity.
func TestRelayCarriesUpgradesAndBrokenResponses(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": faultApp, "web.py": asgiApp})
	p.deploy("faults", faultSpec(source), asgiSpec(source, "service", "web:service", apitypes.HttpKindAsgi, 4))
	base := p.describe("faults", apitypes.WorkloadKindAsgi, "faults").Url
	service := p.describe("faults", apitypes.WorkloadKindAsgi, "service").Url
	expectOK(t, p, base+"/ok", "warm-up")
	expectOK(t, p, service+"/headers", "warm-up")

	other := p.startEdge()
	ws := p.dialWebSocketVia(other, service+"/ws", p.token)
	ws.send("relayed")
	if got := ws.receive(); got != "echo:relayed" {
		t.Fatalf("websocket echo through the relay %q", got)
	}
	ws.close()

	client := &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{DialContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, network, other)
	}}}
	call := func(path string) (int, error) {
		req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, base+path, nil)
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Authorization", "Bearer "+p.token)
		resp, err := client.Do(req)
		if err != nil {
			return 0, err
		}
		defer func() { _ = resp.Body.Close() }()
		_, err = io.ReadAll(resp.Body)
		return resp.StatusCode, err
	}
	for range 3 {
		if _, err := call("/broken"); err == nil {
			t.Fatal("a broken response arrived complete through the relay")
		}
	}
	if status, err := call("/ok"); err != nil || status != http.StatusOK {
		t.Fatalf("after broken responses through the relay: %d %v", status, err)
	}
}
