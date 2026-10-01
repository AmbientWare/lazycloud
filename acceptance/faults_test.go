package acceptance

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// faultApp misbehaves on purpose. One worker admits one request, so a
// request that keeps its capacity after it ended blocks the next.
const faultApp = `
import asyncio

events = {"disconnected": False}


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    path = scope["path"]

    async def answer(body):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": body})

    if path == "/broken":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError("the response breaks off")
    if path == "/stream":
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        try:
            while True:
                await send({"type": "http.response.body", "body": b"data: tick\n\n", "more_body": True})
                await asyncio.sleep(0.05)
        except BaseException:
            events["disconnected"] = True
            raise
    if path == "/status":
        await answer(b"disconnected" if events["disconnected"] else b"running")
        return
    await answer(b"ok")
`

func faultSpec(source string) apitypes.FunctionSpec {
	s := spec("faults", "app:app", source, &apitypes.HttpSpec{Kind: apitypes.HttpKindAsgi})
	one, timeout := 1, 15
	s.Concurrency, s.TimeoutSeconds = &one, &timeout
	return s
}

// expectOK fails unless path answers ok within a few seconds: with one
// request of capacity, that means no earlier request still holds it.
func expectOK(t *testing.T, p *platform, url, why string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 8*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+p.token)
	resp, err := p.client.Do(req)
	if err != nil {
		t.Fatalf("%s: %v", why, err)
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("%s: %d %s", why, resp.StatusCode, body)
	}
}

// A response that breaks off mid-body releases its container capacity.
func TestBrokenResponseReleasesItsCapacity(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": faultApp})
	p.deploy("faults", faultSpec(source))
	base := p.describe("faults", apitypes.WorkloadKindAsgi, "faults").Url
	expectOK(t, p, base+"/ok", "warm-up")
	for range 3 {
		resp, err := p.request(http.MethodGet, base+"/broken", p.token, nil)
		if err == nil {
			_, err = io.ReadAll(resp.Body)
			_ = resp.Body.Close()
		}
		if err == nil {
			t.Fatal("a broken response arrived complete")
		}
	}
	expectOK(t, p, base+"/ok", "after broken responses")
}

// A client that leaves cancels its request in the container.
func TestClientThatLeavesCancelsTheRequest(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": faultApp})
	p.deploy("faults", faultSpec(source))
	base := p.describe("faults", apitypes.WorkloadKindAsgi, "faults").Url
	ctx, cancel := context.WithCancel(t.Context())
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/stream", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+p.token)
	resp, err := p.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := bufio.NewReader(resp.Body).ReadString('\n'); err != nil {
		t.Fatal(err)
	}
	cancel()
	_ = resp.Body.Close()
	// The status request needs the only capacity the stream held.
	deadline := time.Now().Add(10 * time.Second)
	for {
		status, _, body := p.call(http.MethodGet, base+"/status", "")
		if status == http.StatusOK && body == "disconnected" {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("status %d %q; the stream kept running after its client left", status, body)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// A client that stops sending its body after the app answered does not
// hold the request, or its capacity, open.
func TestStalledRequestBodyDoesNotHoldCapacity(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": faultApp})
	p.deploy("faults", faultSpec(source))
	base := p.describe("faults", apitypes.WorkloadKindAsgi, "faults").Url
	expectOK(t, p, base+"/ok", "warm-up")
	host := strings.TrimPrefix(strings.TrimSuffix(base, "/"), "http://")
	conn, err := (&net.Dialer{}).DialContext(t.Context(), "tcp", p.edgeAddr)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Close() }()
	// Declares 1 MiB, larger than the edge buffers, and sends 10 bytes.
	if _, err := fmt.Fprintf(conn, "POST /ok HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer %s\r\nContent-Length: %d\r\n\r\n0123456789", host, p.token, 1<<20); err != nil {
		t.Fatal(err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(60 * time.Second))
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatalf("read the early answer: %v", err)
	}
	_, _ = io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("early answer %d", resp.StatusCode)
	}
	expectOK(t, p, base+"/ok", "while a client stalls its body")
}
