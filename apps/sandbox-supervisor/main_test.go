package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

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
	if err := controlClient.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
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
	state := &processState{
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
	s := &supervisor{}
	s.streamProcess(json.NewDecoder(input), json.NewEncoder(&output), state, 0)

	if len(state.logs) != 0 || state.pendingBytes != 0 || state.ackSeq != 1 {
		t.Fatalf("acknowledged chunks retained: %#v", state)
	}
}

func TestExitedProcessRetentionIsBounded(t *testing.T) {
	s := &supervisor{processes: make(map[int]*processState)}
	for pid := 1; pid <= maxRetainedExitedProcess+5; pid++ {
		s.processes[pid] = &processState{
			pid:        pid,
			running:    false,
			finishedAt: time.Unix(int64(pid), 0),
			changed:    make(chan struct{}),
		}
	}
	s.pruneExitedProcesses()
	if len(s.processes) != maxRetainedExitedProcess {
		t.Fatalf("retained %d exited processes", len(s.processes))
	}
	for pid := 1; pid <= 5; pid++ {
		if _, ok := s.processes[pid]; ok {
			t.Fatalf("old process %d was retained", pid)
		}
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
