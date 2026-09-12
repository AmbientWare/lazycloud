package main

import (
	"bytes"
	"encoding/json"
	"net"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestProbeShellListenerRequiresAuthenticatedProtocol(t *testing.T) {
	listener := listenForShellTest(t)
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			connection, err := listener.Accept()
			if err != nil {
				return
			}
			_, _ = connection.Write([]byte("not-a-shell"))
			_ = connection.Close()
		}
	}()

	port := listener.Addr().(*net.TCPAddr).Port
	err := probeShellListener(port, 150*time.Millisecond, "user", "password")
	_ = listener.Close()
	<-done

	if err == nil || !strings.Contains(err.Error(), "was not ready") {
		t.Fatalf("expected authenticated probe failure, got %v", err)
	}
}

func TestShellProtocolSupportsAuthDataResizeAndExit(t *testing.T) {
	t.Setenv("SHELL", "/bin/sh")
	t.Setenv(shellAuthUsernameEnv, "shell-user")
	t.Setenv(shellAuthPasswordEnv, "shell-password")
	t.Setenv("USERNAME", "workload-user")
	t.Setenv("PASSWORD", "workload-password")
	listener := startShellTestServer(t, "shell-user", "shell-password")
	port := listener.Addr().(*net.TCPAddr).Port

	if err := probeShellListener(port, 150*time.Millisecond, "shell-user", "wrong-password"); err == nil {
		t.Fatal("shell listener accepted invalid credentials")
	}
	if err := probeShellListener(port, time.Second, "shell-user", "shell-password"); err != nil {
		t.Fatalf("authenticated shell probe failed: %v", err)
	}

	connection, err := net.DialTimeout("tcp", listener.Addr().String(), time.Second)
	if err != nil {
		t.Fatalf("connect to shell listener: %v", err)
	}
	defer connection.Close()
	_ = connection.SetDeadline(time.Now().Add(5 * time.Second))

	authPayload, err := json.Marshal(shellAuth{
		Username: "shell-user",
		Password: "shell-password",
		Term:     "xterm-256color",
		Columns:  80,
		Rows:     24,
	})
	if err != nil {
		t.Fatalf("encode auth: %v", err)
	}
	if err := writeShellFrame(connection, shellFrameAuth, authPayload); err != nil {
		t.Fatalf("write auth: %v", err)
	}
	frameType, payload, err := readShellFrame(connection)
	if err != nil || frameType != shellFrameReady {
		t.Fatalf("expected ready frame, got type=%q payload=%q err=%v", frameType, payload, err)
	}

	resizePayload, err := json.Marshal(shellResize{Columns: 120, Rows: 40})
	if err != nil {
		t.Fatalf("encode resize: %v", err)
	}
	if err := writeShellFrame(connection, shellFrameResize, resizePayload); err != nil {
		t.Fatalf("write resize: %v", err)
	}
	command := "if env | grep -Eq '^LAZYCLOUD_SHELL_AUTH_(USERNAME|PASSWORD)='; then printf '\\ncredential-leak\\n'; else printf '\\ncredentials-scrubbed\\n'; fi; if [ \"$USERNAME\" = workload-user ] && [ \"$PASSWORD\" = workload-password ]; then printf '\\nworkload-preserved\\n'; fi; printf 'shell-ok\\n'; exit 7\n"
	if err := writeShellFrame(connection, shellFrameData, []byte(command)); err != nil {
		t.Fatalf("write data: %v", err)
	}

	var output bytes.Buffer
	exitCode := ""
	for exitCode == "" {
		frameType, payload, err = readShellFrame(connection)
		if err != nil {
			t.Fatalf("read shell frame: %v", err)
		}
		switch frameType {
		case shellFrameData:
			output.Write(payload)
		case shellFrameExit:
			exitCode = string(payload)
		case shellFrameError:
			t.Fatalf("shell returned error: %s", payload)
		}
	}

	if exitCode != strconv.Itoa(7) {
		t.Fatalf("expected exit code 7, got %q", exitCode)
	}
	if !strings.Contains(output.String(), "shell-ok") {
		t.Fatalf("missing command output: %q", output.String())
	}
	if !strings.Contains(output.String(), "\r\nworkload-preserved\r\n") {
		t.Fatal("workload credentials were not preserved")
	}
	if !strings.Contains(output.String(), "\r\ncredentials-scrubbed\r\n") || strings.Contains(output.String(), "\r\ncredential-leak\r\n") {
		t.Fatalf("credential environment was not scrubbed: %q", output.String())
	}
}

func TestShellListenerExpiresWhenNeverConnected(t *testing.T) {
	listener, done := startIdleShellTestServer(t, 60*time.Millisecond)

	waitForShellServerExit(t, done)
	if connection, err := net.DialTimeout("tcp", listener.Addr().String(), 50*time.Millisecond); err == nil {
		_ = connection.Close()
		t.Fatal("expired shell listener still accepted connections")
	}
}

func TestUnauthenticatedConnectionCannotExtendIdleLifetime(t *testing.T) {
	listener, done := startIdleShellTestServer(t, 75*time.Millisecond)
	connection, err := net.DialTimeout("tcp", listener.Addr().String(), time.Second)
	if err != nil {
		t.Fatalf("connect to shell listener: %v", err)
	}
	defer connection.Close()
	_ = connection.SetReadDeadline(time.Now().Add(time.Second))

	waitForShellServerExit(t, done)
	buffer := make([]byte, 1)
	if _, err := connection.Read(buffer); err == nil {
		t.Fatal("unauthenticated shell connection survived idle expiration")
	}
}

func TestAuthenticatedReadinessProbeDoesNotExtendIdleLifetime(t *testing.T) {
	listener, done := startIdleShellTestServer(t, 100*time.Millisecond)
	port := listener.Addr().(*net.TCPAddr).Port
	started := time.Now()

	if err := probeShellListener(port, 50*time.Millisecond, "shell-user", "shell-password"); err != nil {
		t.Fatalf("authenticated shell probe failed: %v", err)
	}
	waitForShellServerExit(t, done)

	if elapsed := time.Since(started); elapsed > 250*time.Millisecond {
		t.Fatalf("readiness probe extended shell idle lifetime to %s", elapsed)
	}
}

func TestActiveShellSessionIsNeverKilledByIdleDeadline(t *testing.T) {
	listener, done := startIdleShellTestServer(t, 60*time.Millisecond)
	connection := connectAuthenticatedShell(t, listener.Addr().String())
	defer connection.Close()

	select {
	case err := <-done:
		t.Fatalf("shell server exited during active session: %v", err)
	case <-time.After(150 * time.Millisecond):
	}

	if err := writeShellFrame(connection, shellFrameData, []byte("printf active-session\\n\n")); err != nil {
		t.Fatalf("active shell connection was interrupted: %v", err)
	}
}

func TestDisconnectedShellGetsBoundedReconnectWindow(t *testing.T) {
	idleTimeout := 100 * time.Millisecond
	listener, done := startIdleShellTestServer(t, idleTimeout)
	first := connectAuthenticatedShell(t, listener.Addr().String())
	_ = first.Close()
	time.Sleep(idleTimeout / 2)
	second := connectAuthenticatedShell(t, listener.Addr().String())

	select {
	case err := <-done:
		t.Fatalf("shell server exited before reconnect window elapsed: %v", err)
	case <-time.After(75 * time.Millisecond):
	}
	_ = second.Close()
	waitForShellServerExit(t, done)
}

func listenForShellTest(t *testing.T) net.Listener {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen for test shell: %v", err)
	}
	return listener
}

func startShellTestServer(t *testing.T, username string, password string) net.Listener {
	t.Helper()
	listener := listenForShellTest(t)
	t.Cleanup(func() { _ = listener.Close() })
	go func() {
		_ = serveShellListener(listener, username, password, time.Minute)
	}()
	return listener
}

func startIdleShellTestServer(t *testing.T, idleTimeout time.Duration) (net.Listener, <-chan error) {
	t.Helper()
	listener := listenForShellTest(t)
	done := make(chan error, 1)
	go func() {
		done <- serveShellListener(listener, "shell-user", "shell-password", idleTimeout)
	}()
	t.Cleanup(func() { _ = listener.Close() })
	return listener, done
}

func connectAuthenticatedShell(t *testing.T, address string) net.Conn {
	t.Helper()
	connection, err := net.DialTimeout("tcp", address, time.Second)
	if err != nil {
		t.Fatalf("connect to shell listener: %v", err)
	}
	_ = connection.SetDeadline(time.Now().Add(time.Second))
	payload, err := json.Marshal(shellAuth{
		Username: "shell-user",
		Password: "shell-password",
		Term:     shellDefaultTerm,
		Columns:  shellDefaultColumns,
		Rows:     shellDefaultRows,
	})
	if err != nil {
		_ = connection.Close()
		t.Fatalf("encode shell authentication: %v", err)
	}
	if err := writeShellFrame(connection, shellFrameAuth, payload); err != nil {
		_ = connection.Close()
		t.Fatalf("write shell authentication: %v", err)
	}
	frameType, framePayload, err := readShellFrame(connection)
	if err != nil || frameType != shellFrameReady {
		_ = connection.Close()
		t.Fatalf("authenticate shell: type=%q payload=%q err=%v", frameType, framePayload, err)
	}
	_ = connection.SetDeadline(time.Time{})
	return connection
}

func waitForShellServerExit(t *testing.T, done <-chan error) {
	t.Helper()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("shell server exit: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("shell server did not exit after idle timeout")
	}
}
