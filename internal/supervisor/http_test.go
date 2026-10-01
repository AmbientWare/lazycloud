package supervisor

import (
	"bufio"
	"context"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const httpApp = `
import asyncio
import sys
import time

import lazycloud

app = lazycloud.App("demo")


@app.endpoint()
def slow(seconds: float = 0.0, word: str = "VERSION"):
    print("handling", word, flush=True)
    time.sleep(seconds)
    return {"word": word}


async def events(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
    await send({"type": "http.response.body", "body": b"data: first\n\n", "more_body": True})
    await asyncio.sleep(0.5)
    await send({"type": "http.response.body", "body": b"data: second\n\n"})


stream = app.asgi(name="stream")(events)
`

// httpHarness runs a supervisor serving one HTTP workload with the real
// Python runner from the repository's virtual environment.
type httpHarness struct {
	*harness
	link      *linkConn
	workspace string
	socket    string
	client    *http.Client
}

func startHTTP(t *testing.T, handler string, kind hostproto.HttpKind, workers, concurrency int32) *httpHarness {
	t.Helper()
	python, err := filepath.Abs("../../.venv/bin/python")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(python); err != nil {
		t.Skipf("the HTTP runner needs the repository virtual environment (uv sync --group dev): %v", err)
	}
	h := startSupervisor(t)
	t.Setenv("PYTHONPATH", "")
	workspace := t.TempDir()
	if err := os.WriteFile(filepath.Join(workspace, "app.py"), []byte(httpApp), 0o600); err != nil {
		t.Fatal(err)
	}
	socket := filepath.Join(filepath.Dir(h.socket), "http.sock")
	c := h.accept()
	c.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Configure{Configure: &hostproto.Configure{
		Handler: handler, Slots: workers, RunnerCommand: []string{python, "-m", "runner"}, WorkingDirectory: workspace,
		Http: &hostproto.HttpServing{Kind: kind, Concurrency: concurrency}, HttpSocket: socket,
	}}})
	started := time.Now()
	c.until(t, isReady)
	t.Logf("configure to SlotsReady with %d HTTP workers: %s", workers, time.Since(started))
	client := &http.Client{Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		},
	}}
	t.Cleanup(client.CloseIdleConnections)
	return &httpHarness{harness: h, link: c, workspace: workspace, socket: socket, client: client}
}

func (h *httpHarness) get(t *testing.T, query string) (int, http.Header, string) {
	t.Helper()
	req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, "http://container/?"+query, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := h.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return resp.StatusCode, resp.Header, string(body)
}

func TestSupervisorAdmitsWorkersTimesConcurrencyAndAnswersBusy(t *testing.T) {
	h := startHTTP(t, "app:slow", hostproto.HttpKind_HTTP_KIND_ENDPOINT, 2, 1)

	status, _, body := h.get(t, "word=hello")
	if status != http.StatusOK || body != `{"word": "hello"}` {
		t.Fatalf("request: %d %q", status, body)
	}

	// Two workers of one request each are full while two slow requests run.
	var wg sync.WaitGroup
	for range 2 {
		wg.Go(func() {
			if status, _, _ := h.get(t, "seconds=1"); status != http.StatusOK {
				t.Errorf("slow request: %d", status)
			}
		})
	}
	time.Sleep(300 * time.Millisecond)
	status, header, _ := h.get(t, "")
	if status != http.StatusServiceUnavailable || header.Get(BusyHeader) != BusyFull {
		t.Fatalf("third request: %d busy=%q; want 503 full", status, header.Get(BusyHeader))
	}
	wg.Wait()
	if status, _, _ := h.get(t, ""); status != http.StatusOK {
		t.Fatalf("after the slow requests: %d", status)
	}

	// Output outside attempts reaches the agent without an attempt id.
	_, output := h.link.until(t, func(m *hostproto.SupervisorMessage) bool {
		return strings.Contains(m.GetOutput().GetData(), "handling hello")
	})
	if !strings.Contains(output[""], "handling hello") {
		t.Fatalf("container output %q", output[""])
	}
}

func TestSupervisorStreamsResponsesAsTheyAreWritten(t *testing.T) {
	h := startHTTP(t, "app:stream", hostproto.HttpKind_HTTP_KIND_ASGI, 1, 4)
	req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, "http://container/events", nil)
	if err != nil {
		t.Fatal(err)
	}
	sent := time.Now()
	resp, err := h.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	reader := bufio.NewReader(resp.Body)
	first, err := reader.ReadString('\n')
	if err != nil || first != "data: first\n" {
		t.Fatalf("first event %q: %v", first, err)
	}
	firstAt := time.Since(sent)
	rest, err := io.ReadAll(reader)
	if err != nil || !strings.Contains(string(rest), "data: second") {
		t.Fatalf("rest %q: %v", rest, err)
	}
	if total := time.Since(sent); total-firstAt < 400*time.Millisecond {
		t.Fatalf("first event after %s of %s: the stream was buffered", firstAt, total)
	}
	t.Logf("first SSE event through the supervisor after %s", firstAt)
}

func TestSupervisorDrainFinishesRequestsAndRefusesNewOnes(t *testing.T) {
	h := startHTTP(t, "app:slow", hostproto.HttpKind_HTTP_KIND_ENDPOINT, 1, 2)
	done := make(chan int, 1)
	go func() {
		status, _, _ := h.get(t, "seconds=1")
		done <- status
	}()
	time.Sleep(300 * time.Millisecond)
	h.link.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Drain{Drain: &hostproto.Drain{}}})
	time.Sleep(100 * time.Millisecond)
	if status, header, _ := h.get(t, ""); status != http.StatusServiceUnavailable || header.Get(BusyHeader) != BusyDraining {
		t.Fatalf("request while draining: %d busy=%q", status, header.Get(BusyHeader))
	}
	if status := <-done; status != http.StatusOK {
		t.Fatalf("request in flight at the drain: %d", status)
	}
	select {
	case err := <-h.result:
		h.result <- err
		if err != nil {
			t.Fatalf("drain returned %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("supervisor did not exit after draining")
	}
}

func TestSupervisorReloadServesNewSourceAfterRequestsFinish(t *testing.T) {
	h := startHTTP(t, "app:slow", hostproto.HttpKind_HTTP_KIND_ENDPOINT, 1, 2)
	done := make(chan string, 1)
	go func() {
		_, _, body := h.get(t, "seconds=1")
		done <- body
	}()
	time.Sleep(300 * time.Millisecond)
	edited := strings.Replace(httpApp, `word: str = "VERSION"`, `word: str = "EDITED"`, 1)
	if err := os.WriteFile(filepath.Join(h.workspace, "app.py"), []byte(edited), 0o600); err != nil {
		t.Fatal(err)
	}
	reloaded := time.Now()
	h.link.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Reload{Reload: &hostproto.Reload{}}})
	if body := <-done; body != `{"word": "VERSION"}` {
		t.Fatalf("request in flight at the reload: %q", body)
	}
	deadline := time.Now().Add(20 * time.Second)
	for {
		status, _, body := h.get(t, "")
		if status == http.StatusOK && body == `{"word": "EDITED"}` {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("after reload: %d %q", status, body)
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Logf("reload to the edited source answering: %s", time.Since(reloaded))
}
