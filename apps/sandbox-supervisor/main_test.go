package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestSlowStartReaderDoesNotBlockWorkloadReaping(t *testing.T) {
	s := &supervisor{
		workload:       exec.Command("sh", "-c", "exit 0"),
		workloadExit:   make(chan int, 1),
		directChildren: make(map[int]struct{}),
	}
	server, client := net.Pipe()
	done := make(chan struct{})
	go func() {
		defer close(done)
		defer server.Close()
		s.handleStartWorkload(json.NewEncoder(server))
	}()
	defer func() {
		client.Close()
		<-done
	}()
	select {
	case code := <-s.workloadExit:
		if code != 0 {
			t.Fatalf("workload exited with %d", code)
		}
	case <-time.After(time.Second):
		t.Fatal("stalled start response blocked workload reaping")
	}
	s.mu.RLock()
	remaining := len(s.directChildren)
	s.mu.RUnlock()
	if remaining != 0 {
		t.Fatalf("workload left %d unreaped direct children", remaining)
	}
}

func TestDrainClosesAcceptedConnectionsBeforeControlEOF(t *testing.T) {
	token, tokenPath := testControlToken(t)
	s := &supervisor{
		processes:      make(map[int]*processState),
		directChildren: make(map[int]struct{}),
		connections:    make(map[net.Conn]struct{}),
		tokenPath:      tokenPath,
	}
	otherServer, otherClient := net.Pipe()
	controlServer, controlClient := net.Pipe()
	s.trackConnection(otherServer)
	s.trackConnection(controlServer)
	go s.handleConnection(controlServer)
	if err := otherClient.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}

	// A pipe refuses a deadline once the far end has closed, and the far end
	// closes as soon as the drain is answered.
	if err := controlClient.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if err := json.NewEncoder(controlClient).Encode(request{Version: protocolVersion, Op: "drain", Token: token}); err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bufio.NewReader(controlClient))
	var drained response
	if err := decoder.Decode(&drained); err != nil {
		t.Fatal(err)
	}
	if drained.Version != protocolVersion || drained.Type != "drained" {
		t.Fatalf("unexpected drain response: %#v", drained)
	}
	if err := decoder.Decode(&response{}); err != io.EOF {
		t.Fatalf("control connection did not close after drain: %v", err)
	}
	buffer := make([]byte, 1)
	if _, err := otherClient.Read(buffer); err != io.EOF {
		t.Fatalf("accepted connection remained open after drain: %v", err)
	}

	s.mu.RLock()
	remaining := len(s.connections)
	s.mu.RUnlock()
	if remaining != 0 {
		t.Fatalf("drain returned with %d tracked connections", remaining)
	}
	_ = otherClient.Close()
	_ = controlClient.Close()
}

func TestControlRequestRejectsWrongToken(t *testing.T) {
	_, tokenPath := testControlToken(t)
	s := &supervisor{
		processes:      make(map[int]*processState),
		directChildren: make(map[int]struct{}),
		connections:    make(map[net.Conn]struct{}),
		tokenPath:      tokenPath,
	}
	server, client := net.Pipe()
	go s.handleConnection(server)
	defer client.Close()

	if err := json.NewEncoder(client).Encode(request{Version: protocolVersion, Op: "ready", Token: "wrong"}); err != nil {
		t.Fatal(err)
	}
	var result response
	if err := json.NewDecoder(client).Decode(&result); err != nil {
		t.Fatal(err)
	}
	if result.Type != "error" || result.Error != "unauthorized" {
		t.Fatalf("unexpected unauthorized response: %#v", result)
	}
}

func TestStreamAckEvictsPersistedChunks(t *testing.T) {
	s := &supervisor{}
	s.bufferBytes.Store(2)
	state := &processState{
		budget:       &s.bufferBytes,
		pid:          12,
		running:      false,
		exitCode:     0,
		nextSeq:      2,
		logs:         []logChunk{{Seq: 1, Stream: "stdout", Data: []byte("ok")}},
		pendingBytes: 2,
		changed:      make(chan struct{}),
	}
	input := bytes.NewBufferString(`{"version":1,"op":"ack","pid":12,"ack_seq":1,"ok":true}` + "\n")
	var output bytes.Buffer
	s.streamProcess(json.NewDecoder(input), json.NewEncoder(&output), state, 0)

	if len(state.logs) != 0 || state.pendingBytes != 0 || state.ackSeq != 1 {
		t.Fatalf("acknowledged chunks retained: %#v", state)
	}
}

// TestMain lets the test binary stand in for the file helper, which the
// supervisor runs as its own executable.
func TestMain(m *testing.M) {
	if len(os.Args) == 3 && os.Args[1] == "filesystem" {
		main()
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func TestFilesystemDownloadReturnsEveryByteAndItsCount(t *testing.T) {
	token, tokenPath := testControlToken(t)
	path := filepath.Join(t.TempDir(), "blob")
	want := bytes.Repeat([]byte("0123456789abcdef"), 300_000/16)
	if err := os.WriteFile(path, want, 0o600); err != nil {
		t.Fatal(err)
	}
	payload, err := json.Marshal(filesystemRequest{Operation: "download-file", Path: path})
	if err != nil {
		t.Fatal(err)
	}
	_, decoder := openControl(t, tokenPath, request{Version: protocolVersion, Op: "filesystem", Payload: string(payload), Token: token})
	var got []byte
	for {
		var event response
		if err := decoder.Decode(&event); err != nil {
			t.Fatalf("stream ended after %d bytes: %v", len(got), err)
		}
		if event.Type != "chunk" {
			var report downloadReport
			if event.Type != "exited" || event.ExitCode != 0 || json.Unmarshal([]byte(event.Stderr), &report) != nil {
				t.Fatalf("download ended with %#v", event)
			}
			if !bytes.Equal(got, want) || report.Bytes != int64(len(want)) {
				t.Fatalf("received %d bytes, reported %d, of %d", len(got), report.Bytes, len(want))
			}
			return
		}
		got = append(got, event.Data...)
	}
}

func TestExecReportsExitWhileADescendantKeepsWriting(t *testing.T) {
	token, tokenPath := testControlToken(t)
	script := "echo done; (while :; do echo tick; sleep 0.2; done) &"
	client, decoder := openControl(t, tokenPath, request{Version: protocolVersion, Op: "exec", Argv: []string{"sh", "-c", script}, Token: token})
	if err := client.SetReadDeadline(time.Now().Add(outputDrainGrace + 2*time.Second)); err != nil {
		t.Fatal(err)
	}
	encoder := json.NewEncoder(client)
	for {
		var event response
		if err := decoder.Decode(&event); err != nil {
			t.Fatalf("the exit was not reported while a descendant kept writing: %v", err)
		}
		switch event.Type {
		case "started":
			pid := event.PID
			t.Cleanup(func() { _ = syscall.Kill(-pid, syscall.SIGKILL) })
		case "chunk":
			if err := encoder.Encode(request{Version: protocolVersion, Op: "ack", PID: event.PID, AckSeq: event.Seq, OK: true}); err != nil {
				t.Fatal(err)
			}
		case "exited":
			return
		}
	}
}

// openControl sends one request to a fresh supervisor and returns the
// connection it answers on.
func openControl(t *testing.T, tokenPath string, command request) (net.Conn, *json.Decoder) {
	t.Helper()
	s := &supervisor{
		processes:      make(map[int]*processState),
		directChildren: make(map[int]struct{}),
		connections:    make(map[net.Conn]struct{}),
		tokenPath:      tokenPath,
	}
	server, client := net.Pipe()
	t.Cleanup(func() { _ = client.Close() })
	go s.handleConnection(server)
	if err := json.NewEncoder(client).Encode(command); err != nil {
		t.Fatal(err)
	}
	return client, json.NewDecoder(bufio.NewReader(client))
}

func TestCommandResultSurvivesNewCommandsAndPIDReuseUntilExpiry(t *testing.T) {
	s := &supervisor{processes: make(map[int]*processState), directChildren: make(map[int]struct{})}
	var first response
	for index := 0; index < 80; index++ {
		server, client := net.Pipe()
		go func() {
			defer server.Close()
			s.handleExec(json.NewDecoder(server), json.NewEncoder(server), request{Argv: []string{"sh", "-c", "printf output; printf error >&2; exit 7"}})
		}()
		decoder, encoder := json.NewDecoder(client), json.NewEncoder(client)
		for {
			var event response
			if err := decoder.Decode(&event); err != nil {
				t.Fatal(err)
			}
			if event.Type == "started" && index == 0 {
				first = event
			}
			if event.Type == "chunk" {
				if err := encoder.Encode(request{Version: protocolVersion, Op: "ack", PID: event.PID, AckSeq: event.Seq, OK: true}); err != nil {
					t.Fatal(err)
				}
			}
			if event.Type == "exited" {
				break
			}
			if event.Type == "error" {
				t.Fatal(event.Error)
			}
		}
		client.Close()
	}
	s.mu.Lock()
	s.processes[first.PID] = &processState{id: "reused", pid: first.PID, running: true}
	s.mu.Unlock()
	var output bytes.Buffer
	s.handleResult(json.NewEncoder(&output), request{ProcessID: first.ProcessID, WaitSeconds: 5})
	var result response
	if err := json.Unmarshal(output.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result.Type != "result" || result.ExitCode != 7 || result.Stdout != "output" || result.Stderr != "error" || result.ExpiresAt == nil {
		t.Fatalf("lost retained result: %#v", result)
	}
	state := s.results[first.ProcessID]
	state.mu.Lock()
	state.finishedAt = time.Now().Add(-processResultRetention)
	state.mu.Unlock()
	s.pruneExitedProcesses()
	output.Reset()
	s.handleResult(json.NewEncoder(&output), request{ProcessID: first.ProcessID})
	if err := json.Unmarshal(output.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result.Type != "error" || result.Error != "process result expired or not found" {
		t.Fatalf("expiry not explicit: %#v", result)
	}
	if s.processes[first.PID].id != "reused" {
		t.Fatal("expiry removed a reused PID")
	}
}

func TestResultTruncationPreservesPrefixAndBudget(t *testing.T) {
	s := &supervisor{processes: make(map[int]*processState), directChildren: make(map[int]struct{})}
	var output bytes.Buffer
	s.handleExec(json.NewDecoder(bytes.NewReader(nil)), json.NewEncoder(&output), request{Argv: []string{"sh", "-c", "printf prefix; head -c 300000 /dev/zero"}})
	var started response
	if err := json.NewDecoder(&output).Decode(&started); err != nil {
		t.Fatal(err)
	}
	output.Reset()
	s.handleResult(json.NewEncoder(&output), request{ProcessID: started.ProcessID, WaitSeconds: 5})
	var result response
	if err := json.Unmarshal(output.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result.Running || !result.StdoutTruncated || len(result.Stdout) != maxRetainedOutputBytes || result.Stdout[:6] != "prefix" {
		t.Fatalf("incorrect output retention: running=%v truncated=%v bytes=%d", result.Running, result.StdoutTruncated, len(result.Stdout))
	}
	state := s.results[started.ProcessID]
	state.mu.Lock()
	state.finishedAt = time.Now().Add(-processResultRetention)
	state.mu.Unlock()
	s.pruneExitedProcesses()
	if retained := s.bufferBytes.Load(); retained != 0 {
		t.Fatalf("expired result retained %d bytes", retained)
	}
}

func TestSlowLogReaderDoesNotLoseRetainedResult(t *testing.T) {
	s := &supervisor{processes: make(map[int]*processState), directChildren: make(map[int]struct{})}
	server, client := net.Pipe()
	defer client.Close()
	done := make(chan struct{})
	go func() {
		defer server.Close()
		defer close(done)
		s.handleExec(json.NewDecoder(server), json.NewEncoder(server), request{Argv: []string{"sh", "-c", "printf retained"}})
	}()
	decoder := json.NewDecoder(client)
	var started, chunk response
	if err := decoder.Decode(&started); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&chunk); err != nil {
		t.Fatal(err)
	}
	if chunk.Type != "chunk" {
		t.Fatalf("expected output, got %#v", chunk)
	}
	var output bytes.Buffer
	s.handleResult(json.NewEncoder(&output), request{ProcessID: started.ProcessID, WaitSeconds: 5})
	var result response
	if err := json.Unmarshal(output.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result.Running || result.Stdout != "retained" {
		t.Fatalf("result blocked on log reader: %#v", result)
	}
	state := s.results[started.ProcessID]
	state.mu.Lock()
	state.finishedAt = time.Now().Add(-processResultRetention)
	state.mu.Unlock()
	s.pruneExitedProcesses()
	if s.results[started.ProcessID] != state {
		t.Fatal("expired result removed during active log retrieval")
	}
	if err := json.NewEncoder(client).Encode(request{Version: protocolVersion, Op: "ack", PID: chunk.PID, AckSeq: chunk.Seq, OK: true}); err != nil {
		t.Fatal(err)
	}
	var exited response
	if err := decoder.Decode(&exited); err != nil {
		t.Fatal(err)
	}
	if exited.Type != "exited" {
		t.Fatalf("stream did not finish: %#v", exited)
	}
	<-done
	s.pruneExitedProcesses()
	if s.bufferBytes.Load() != 0 {
		t.Fatal("expired slow-reader output was not released")
	}
}

func testControlToken(t *testing.T) (string, string) {
	t.Helper()
	token := fmt.Sprintf("%064d", 1)
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte(token+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return token, path
}
