package supervisor

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

func TestRedactorHoldsBackAValueSplitAcrossChunks(t *testing.T) {
	t.Setenv("WR_REDACT_A", "hunter2-hunter2")
	r := newRedactor([]string{"WR_REDACT_A", "WR_REDACT_MISSING"})
	var held string
	out := r.stream(&held, "token hun", false)
	out += r.stream(&held, "ter2-hu", false)
	out += r.stream(&held, "nter2 end\n", false)
	out += r.stream(&held, "", true)
	if out != "token ******** end\n" || held != "" {
		t.Fatalf("streamed %q, held %q", out, held)
	}
	// A prefix that never completes is emitted unchanged.
	out = r.stream(&held, "hunt", false) + r.stream(&held, "ing\n", false)
	if out != "hunting\n" {
		t.Fatalf("partial prefix %q", out)
	}
	if newRedactor([]string{"WR_REDACT_MISSING"}) != nil {
		t.Fatal("a redactor without values")
	}
}

func (c *linkConn) configureWith(t *testing.T, cfg *hostproto.Configure) {
	t.Helper()
	workspace, err := filepath.Abs("testdata/workspace")
	if err != nil {
		t.Fatal(err)
	}
	cfg.RunnerCommand = []string{"python3", "-m", "runner"}
	cfg.WorkingDirectory = workspace
	c.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Configure{Configure: cfg}})
}

func TestSupervisorRedactsSecretsFromOutputAndFailures(t *testing.T) {
	t.Setenv("WR_SECRET", "hunter2-hunter2")
	h := startSupervisor(t)
	c := h.accept()
	c.configureWith(t, &hostproto.Configure{Handler: "app:handle", Slots: 1, SecretEnv: []string{"WR_SECRET"}})
	c.until(t, isReady)

	c.run(t, "leak", `{"args": ["echo", "hunter2-hunter2"]}`)
	m, output := c.until(t, finished("leak"))
	if strings.Contains(output["leak"], "hunter2") || !strings.Contains(output["leak"], "********") {
		t.Fatalf("output %q", output["leak"])
	}
	e := m.GetFinished().GetFailure().GetError()
	if e.GetMessage() != "failed with ********" || strings.Contains(e.GetTraceback(), "hunter2") {
		t.Fatalf("failure %v", e)
	}
	if exc := m.GetFinished().GetFailure().GetException(); len(exc) != 0 {
		t.Fatalf("an exception pickle holding the secret was kept: %q", exc)
	}
	c.run(t, "plain", `{"args": ["echo", "harmless"]}`)
	m, _ = c.until(t, finished("plain"))
	if len(m.GetFinished().GetFailure().GetException()) == 0 {
		t.Fatal("an exception without secrets lost its pickle")
	}
}

func TestInProcessSlotsShareOneRunnerAndCancelRestartsIt(t *testing.T) {
	h := startSupervisor(t)
	c := h.accept()
	c.configureWith(t, &hostproto.Configure{Handler: "app:handle", Slots: 2, InProcess: true})
	ready, _ := c.until(t, isReady)
	if ready.GetReady().GetSlots() != 2 {
		t.Fatalf("ready %v", ready)
	}

	began := time.Now()
	c.run(t, "s1", `{"args": ["sleep", 1]}`)
	c.run(t, "s2", `{"args": ["sleep", 1]}`)
	outputs := map[string]string{}
	for done := 0; done < 2; {
		m, output := c.until(t, func(m *hostproto.SupervisorMessage) bool { return m.GetFinished() != nil })
		for k, v := range output {
			outputs[k] += v
		}
		if m.GetFinished().GetSuccess() == nil {
			t.Fatalf("attempt %v", m)
		}
		done++
	}
	if elapsed := time.Since(began); elapsed > 1900*time.Millisecond {
		t.Fatalf("two 1 s attempts on two threads took %s", elapsed)
	}
	if outputs["s1"] != "thread runs s1\n" || outputs["s2"] != "thread runs s2\n" {
		t.Fatalf("attributed output %q", outputs)
	}

	// Cancelling one attempt kills the shared process: its sibling reports a
	// crash, and the runner comes back for the next attempt.
	c.run(t, "hung", `{"args": ["hang"]}`)
	c.run(t, "sibling", `{"args": ["sleep", 30]}`)
	c.until(t, func(m *hostproto.SupervisorMessage) bool { return m.GetOutput().GetAttemptId() == "sibling" })
	c.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Cancel{Cancel: &hostproto.CancelSlot{AttemptId: "hung"}}})
	m, _ := c.until(t, func(m *hostproto.SupervisorMessage) bool { return m.GetFinished() != nil })
	if m.GetFinished().GetAttemptId() != "sibling" ||
		m.GetFinished().GetFailure().GetKind() != hostproto.AttemptFailureKind_ATTEMPT_FAILURE_KIND_CRASHED {
		t.Fatalf("after cancel: %v", m)
	}
	c.run(t, "next", `{"args": ["total", [2, 3]]}`)
	m, _ = c.until(t, finished("next"))
	if string(m.GetFinished().GetSuccess().GetResult()) != "5" {
		t.Fatalf("after restart: %v", m)
	}
}

// apiLink answers API calls by echoing the request.
type apiLink struct{ linkServer }

func (l *apiLink) API(stream hostproto.ContainerLink_APIServer) error {
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	body := append([]byte(nil), first.GetBody()...)
	for {
		msg, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return err
		}
		body = append(body, msg.GetBody()...)
	}
	head := &hostproto.APIResponseHead{Status: http.StatusTeapot}
	for _, h := range first.GetHead().GetHeaders() {
		head.Headers = append(head.Headers, &hostproto.APIHeader{Name: "Echo-" + h.GetName(), Value: h.GetValue()})
	}
	if err := stream.Send(&hostproto.APIResponse{Head: head, Body: []byte(first.GetHead().GetMethod() + " " + first.GetHead().GetTarget() + "\n")}); err != nil {
		return err
	}
	return stream.Send(&hostproto.APIResponse{Body: body})
}

func TestContainerAPISocketRelaysRequestsToTheAgent(t *testing.T) {
	dir, err := os.MkdirTemp("", "lcapi")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = os.RemoveAll(dir) }()
	socket := filepath.Join(dir, "api.sock")
	lis, err := (&net.ListenConfig{}).Listen(t.Context(), "unix", filepath.Join(dir, "agent.sock"))
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	hostproto.RegisterContainerLinkServer(server, &apiLink{})
	go func() { _ = server.Serve(lis) }()
	defer server.Stop()
	conn, err := grpc.NewClient("unix:"+filepath.Join(dir, "agent.sock"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Close() }()

	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	served := make(chan error, 1)
	go func() { served <- (&Supervisor{}).serveAPI(ctx, socket, hostproto.NewContainerLinkClient(conn)) }()
	client := &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", socket)
	}}}
	waitForSocket(t, socket)

	req, _ := http.NewRequestWithContext(t.Context(), http.MethodPost, "http://container/v1/x?y=1", strings.NewReader("payload"))
	req.Header.Set("Authorization", "Bearer lc_should_not_pass")
	req.Header.Set("LazyCloud-Task", "t-1")
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusTeapot || string(body) != "POST /v1/x?y=1\npayload" ||
		resp.Header.Get("Echo-Lazycloud-Task") != "t-1" || resp.Header.Get("Echo-Authorization") != "" {
		t.Fatalf("relayed %d %q %v", resp.StatusCode, body, resp.Header)
	}

	big, _ := http.NewRequestWithContext(t.Context(), http.MethodPost, "http://container/v1/x", bytes.NewReader(make([]byte, maxAPIBody+1)))
	resp, err = client.Do(big)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("an oversized body: %d", resp.StatusCode)
	}
	cancel()
	if err := <-served; err != nil {
		t.Fatal(err)
	}
}

func waitForSocket(t *testing.T, path string) {
	t.Helper()
	for range 100 {
		if _, err := os.Stat(path); err == nil {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("the API socket never appeared")
}

func TestInProcessOutputSurvivesLargeWritesAndSplitSecrets(t *testing.T) {
	t.Setenv("WR_SPLIT_SECRET", "hunter2-hunter2")
	h := startSupervisor(t)
	c := h.accept()
	c.configureWith(t, &hostproto.Configure{Handler: "app:handle", Slots: 2, InProcess: true, SecretEnv: []string{"WR_SPLIT_SECRET"}})
	c.until(t, isReady)

	c.run(t, "big", `{"args": ["big"]}`)
	m, output := c.until(t, finished("big"))
	if m.GetFinished().GetSuccess() == nil || len(output["big"]) != 2<<20+1 {
		t.Fatalf("a 2 MiB write: %v, %d bytes of output", m, len(output["big"]))
	}
	c.run(t, "split", `{"args": ["split", "hunter2-hunter2"]}`)
	_, output = c.until(t, finished("split"))
	if output["split"] != "********\n" {
		t.Fatalf("a secret split across frames came out as %q", output["split"])
	}
}
