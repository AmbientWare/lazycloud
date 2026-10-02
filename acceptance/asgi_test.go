package acceptance

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

const asgiApp = `
import asyncio
import json

import lazycloud

app = lazycloud.App("web")


async def respond(send, status, body, content_type=b"application/json"):
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", content_type)]})
    await send({"type": "http.response.body", "body": body})


async def service_app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            await send({"type": message["type"] + ".complete"})
            if message["type"] == "lifespan.shutdown":
                return
    if scope["type"] == "websocket":
        await receive()
        await send({"type": "websocket.accept"})
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
            await send({"type": "websocket.send", "text": "echo:" + message.get("text", "")})
    path = scope["path"]
    if path == "/headers":
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        await respond(send, 200, json.dumps({"headers": headers, "client": scope.get("client"), "scheme": scope.get("scheme")}).encode())
        return
    if path == "/events":
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        for n in range(3):
            await send({"type": "http.response.body", "body": f"data: {n}\n\n".encode(), "more_body": True})
            await asyncio.sleep(0.3)
        await send({"type": "http.response.body", "body": b""})
        return
    if path == "/upload":
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await respond(send, 200, json.dumps({"bytes": total}).encode())
        return
    await respond(send, 404, b"{}")


service = app.asgi(name="service", concurrent_requests=8)(service_app)


@app.realtime(name="talk")
def talk(message: str):
    if message == "many":
        return (word for word in ["a", "b", "c"])
    return {"heard": message}
`

func asgiSpec(source, name, handler string, kind apitypes.HttpKind, concurrency int) apitypes.WorkloadSpec {
	s := spec(name, handler, source, &apitypes.HttpSpec{Kind: kind})
	s.Concurrency = &concurrency
	return s
}

func TestASGIStreamsUploadsUpgradesAndStripsTheToken(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": asgiApp})
	p.deploy("web",
		asgiSpec(source, "service", "app:service", apitypes.HttpKindAsgi, 8),
		asgiSpec(source, "talk", "app:talk", apitypes.HttpKindRealtime, 4),
	)
	service := p.describe("web", apitypes.WorkloadKindAsgi, "service")

	// Headers: the platform token is gone, X-Forwarded-* describe the client.
	req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, service.Url+"/headers", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+p.token)
	req.Header.Set("X-Custom", "kept")
	resp, err := p.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	var echoed struct {
		Headers map[string]string `json:"headers"`
		Scheme  string            `json:"scheme"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&echoed); err != nil {
		t.Fatalf("decode headers (%s): %v", resp.Status, err)
	}
	_ = resp.Body.Close()
	host := strings.TrimPrefix(service.Url, "http://")
	if _, leaked := echoed.Headers["authorization"]; leaked || echoed.Headers["x-custom"] != "kept" ||
		echoed.Headers["x-forwarded-host"] != host || echoed.Headers["x-forwarded-proto"] != "http" ||
		!strings.HasPrefix(echoed.Headers["x-forwarded-for"], "127.0.0.1") || echoed.Headers["host"] != host {
		t.Fatalf("headers the workload saw: %v", echoed.Headers)
	}

	// SSE: each event arrives as it is written.
	resp, err = p.request(http.MethodGet, service.Url+"/events", p.token, nil)
	if err != nil {
		t.Fatal(err)
	}
	sent := time.Now()
	reader := bufio.NewReader(resp.Body)
	first, err := reader.ReadString('\n')
	firstAt := time.Since(sent)
	if err != nil || first != "data: 0\n" {
		t.Fatalf("first event %q: %v", first, err)
	}
	rest, err := io.ReadAll(reader)
	_ = resp.Body.Close()
	total := time.Since(sent)
	if err != nil || !strings.Contains(string(rest), "data: 2") || total-firstAt < 500*time.Millisecond {
		t.Fatalf("events %q after %s, first after %s: %v", rest, total, firstAt, err)
	}
	t.Logf("SSE through the edge: first event after %s, stream of 3 events over %s", firstAt, total)

	// A streamed upload too large to replay reaches the app whole.
	payload := bytes.Repeat([]byte("x"), 5<<20)
	upload, err := http.NewRequestWithContext(t.Context(), http.MethodPost, service.Url+"/upload", io.MultiReader(bytes.NewReader(payload)))
	if err != nil {
		t.Fatal(err)
	}
	upload.Header.Set("Authorization", "Bearer "+p.token)
	resp, err = p.client.Do(upload)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK || string(body) != fmt.Sprintf(`{"bytes": %d}`, len(payload)) {
		t.Fatalf("upload: %d %s", resp.StatusCode, body)
	}

	// WebSockets pass through.
	ws := p.dialWebSocket(service.Url+"/ws", p.token)
	ws.send("hi")
	if got := ws.receive(); got != "echo:hi" {
		t.Fatalf("websocket echo %q", got)
	}
	ws.close()

	// A realtime handler answers each message; an iterable sends several.
	talk := p.describe("web", apitypes.WorkloadKindAsgi, "talk")
	rt := p.dialWebSocket(talk.Url, p.token)
	rt.send("hello")
	if got := rt.receive(); got != `{"heard": "hello"}` {
		t.Fatalf("realtime answer %q", got)
	}
	rt.send("many")
	for _, want := range []string{"a", "b", "c"} {
		if got := rt.receive(); got != want {
			t.Fatalf("realtime item %q; want %q", got, want)
		}
	}
	rt.close()
}

// webSocket is a minimal RFC 6455 client for text frames.
type webSocket struct {
	t    *testing.T
	conn net.Conn
	r    *bufio.Reader
}

func (p *platform) dialWebSocket(rawURL, token string) *webSocket {
	p.t.Helper()
	return p.dialWebSocketVia(p.edgeAddr, rawURL, token)
}

// dialWebSocketVia upgrades through the edge at addr.
func (p *platform) dialWebSocketVia(addr, rawURL, token string) *webSocket {
	p.t.Helper()
	u, err := url.Parse(rawURL)
	if err != nil {
		p.t.Fatal(err)
	}
	conn, err := (&net.Dialer{}).DialContext(p.t.Context(), "tcp", addr)
	if err != nil {
		p.t.Fatal(err)
	}
	key := make([]byte, 16)
	_, _ = rand.Read(key)
	path := u.RequestURI()
	_, _ = fmt.Fprintf(conn, "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"+
		"Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer %s\r\n\r\n",
		path, u.Host, base64.StdEncoding.EncodeToString(key), token)
	r := bufio.NewReader(conn)
	resp, err := http.ReadResponse(r, nil) //nolint:bodyclose // the upgraded connection is the body
	if err != nil {
		p.t.Fatal(err)
	}
	if resp.StatusCode != http.StatusSwitchingProtocols {
		body, _ := io.ReadAll(resp.Body)
		p.t.Fatalf("websocket upgrade: %s %s", resp.Status, body)
	}
	_ = conn.SetDeadline(time.Now().Add(30 * time.Second))
	return &webSocket{t: p.t, conn: conn, r: r}
}

func (w *webSocket) send(text string) {
	w.t.Helper()
	mask := []byte{1, 2, 3, 4}
	frame := []byte{0x81, 0x80 | byte(len(text))}
	frame = append(frame, mask...)
	for n := range len(text) {
		frame = append(frame, text[n]^mask[n%4])
	}
	if _, err := w.conn.Write(frame); err != nil {
		w.t.Fatal(err)
	}
}

func (w *webSocket) receive() string {
	w.t.Helper()
	header := make([]byte, 2)
	if _, err := io.ReadFull(w.r, header); err != nil {
		w.t.Fatal(err)
	}
	size := int(header[1] & 0x7f)
	switch size {
	case 126:
		ext := make([]byte, 2)
		_, _ = io.ReadFull(w.r, ext)
		size = int(binary.BigEndian.Uint16(ext))
	case 127:
		ext := make([]byte, 8)
		_, _ = io.ReadFull(w.r, ext)
		size = int(binary.BigEndian.Uint64(ext)) //nolint:gosec // test frames are small
	}
	payload := make([]byte, size)
	if _, err := io.ReadFull(w.r, payload); err != nil {
		w.t.Fatal(err)
	}
	return string(payload)
}

func (w *webSocket) close() {
	_, _ = w.conn.Write([]byte{0x88, 0x80, 0, 0, 0, 0})
	_ = w.conn.Close()
}
