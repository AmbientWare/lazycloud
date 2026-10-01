package supervisor

import (
	"archive/tar"
	"bytes"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

func (c *linkConn) configurePod(t *testing.T, pod *hostproto.PodProcess, controlSocket string) {
	t.Helper()
	c.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Configure{Configure: &hostproto.Configure{
		Pod: pod, ControlSocket: controlSocket,
	}}})
}

func (h *harness) exitResult(t *testing.T) error {
	t.Helper()
	select {
	case err := <-h.result:
		h.result <- err
		return err
	case <-time.After(10 * time.Second):
		t.Fatal("the supervisor did not exit")
		return nil
	}
}

func TestPodCommandExitEndsTheSupervisorWithItsCode(t *testing.T) {
	t.Setenv("WR_POD_SECRET", "hunter2-hunter2")
	h := startSupervisor(t)
	c := h.accept()
	c.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Configure{Configure: &hostproto.Configure{
		SecretEnv: []string{"WR_POD_SECRET"},
		Pod: &hostproto.PodProcess{
			Command:          []string{"sh", "-c", `echo "in $(pwd) $WR_POD_SECRET"; echo partial >&2; printf tail; kill -USR1 $$`},
			WorkingDirectory: t.TempDir(),
		},
	}}})
	m, output := c.until(t, func(m *hostproto.SupervisorMessage) bool { return m.GetCommandExited() != nil })
	if code := m.GetCommandExited().GetExitCode(); code != 128+int32(unix.SIGUSR1) {
		t.Fatalf("exit code %d", code)
	}
	if !strings.Contains(output[""], "in /") || !strings.Contains(output[""], Redacted) || strings.Contains(output[""], "hunter2") ||
		!strings.Contains(output[""], "partial") || !strings.HasSuffix(output[""], "tail") {
		t.Fatalf("pod output %q", output[""])
	}
	var exited *CommandExitedError
	if err := h.exitResult(t); !errors.As(err, &exited) || exited.Code != 128+int(unix.SIGUSR1) {
		t.Fatalf("run returned %v", err)
	}
}

func TestPodIsReadyOnceItsPortAnswersAndDrainStopsIt(t *testing.T) {
	listener, err := (&net.ListenConfig{}).Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	_ = listener.Close()
	h := startSupervisor(t)
	c := h.accept()
	started := time.Now()
	// The command listens only after a delay; readiness waits for it.
	c.configurePod(t, &hostproto.PodProcess{
		Command: []string{"python3", "-c", "import socket, time\n" +
			"time.sleep(0.5)\n" +
			"s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n" +
			"s.bind(('127.0.0.1', " + strconv.Itoa(port) + ")); s.listen()\n" +
			"print('listening', flush=True)\n" +
			"while True: s.accept()[0].close()\n"},
		Ports: []int32{int32(port)}, //nolint:gosec // a port
	}, "")
	ready, output := c.until(t, isReady)
	if elapsed := time.Since(started); ready.GetReady().GetSlots() != 1 || elapsed < 500*time.Millisecond {
		t.Fatalf("ready %v after %s", ready, elapsed)
	}
	t.Logf("configure to ready for a command listening after 0.5 s: %s (output %q)", time.Since(started), output[""])
	c.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Drain{Drain: &hostproto.Drain{}}})
	if err := h.exitResult(t); err != nil {
		t.Fatalf("drained pod returned %v", err)
	}
}

func TestPodHealthCheckGatesReadiness(t *testing.T) {
	listener, err := (&net.ListenConfig{}).Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	healthy := make(chan struct{})
	server := &http.Server{ReadHeaderTimeout: time.Second, Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-healthy:
			if r.URL.Path == "/healthz" {
				w.WriteHeader(http.StatusNoContent)
				return
			}
		default:
		}
		w.WriteHeader(http.StatusServiceUnavailable)
	})}
	go func() { _ = server.Serve(listener) }()
	defer func() { _ = server.Close() }()
	h := startSupervisor(t)
	c := h.accept()
	c.configurePod(t, &hostproto.PodProcess{
		Command: []string{"sleep", "100"},
		Health:  &hostproto.HealthCheck{Path: "healthz", Port: int32(port)}, //nolint:gosec // a port
	}, "")
	time.AfterFunc(300*time.Millisecond, func() { close(healthy) })
	started := time.Now()
	c.until(t, isReady)
	if time.Since(started) < 300*time.Millisecond {
		t.Fatalf("ready before the health check passed")
	}
}

func TestIdlePodServesTheControlAPIUntilDrained(t *testing.T) {
	h := startSupervisor(t)
	c := h.accept()
	socket := filepath.Join(filepath.Dir(h.socket), "control.sock")
	c.configurePod(t, &hostproto.PodProcess{}, socket)
	c.until(t, isReady)
	if info, err := os.Stat(socket); err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("control socket %v %v", info, err)
	}
	ctl := &controlHarness{t: t, socket: socket, client: unixClient(socket)}
	cwd := t.TempDir()
	var p apitypes.Process
	if status, _ := ctl.do(http.MethodPost, "/processes", apitypes.ProcessRequest{Args: []string{"sleep", "100"}, Cwd: &cwd}, &p); status != http.StatusCreated {
		t.Fatalf("start: %d", status)
	}
	c.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Drain{Drain: &hostproto.Drain{}}})
	if err := h.exitResult(t); err != nil {
		t.Fatalf("drained idle pod returned %v", err)
	}
	// Stopping the control API killed the process it started.
	if err := unix.Kill(p.Pid, 0); !errors.Is(err, unix.ESRCH) {
		t.Fatalf("process %d after the drain: %v", p.Pid, err)
	}
}

func TestArchiveKeepsLinksAndAttributesAndSkipsMountsAndSockets(t *testing.T) {
	root := t.TempDir()
	write := func(name, content string) {
		if err := os.MkdirAll(filepath.Dir(filepath.Join(root, name)), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(root, name), []byte(content), 0o640); err != nil {
			t.Fatal(err)
		}
	}
	write("a.txt", "first")
	write("dir/inner.txt", "inner")
	write("mnt/hidden.txt", "mounted")
	if err := os.Link(filepath.Join(root, "a.txt"), filepath.Join(root, "b.txt")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("a.txt", filepath.Join(root, "c.link")); err != nil {
		t.Fatal(err)
	}
	sock, err := (&net.ListenConfig{}).Listen(t.Context(), "unix", filepath.Join(root, "d.sock"))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sock.Close() }()
	xattr := unix.Setxattr(filepath.Join(root, "a.txt"), "user.lazycloud", []byte("kept"), 0) == nil

	var archive bytes.Buffer
	if err := writeArchive(&archive, root, map[string]bool{filepath.Join(root, "mnt"): true}); err != nil {
		t.Fatal(err)
	}
	entries := map[string]*tar.Header{}
	contents := map[string]string{}
	reader := tar.NewReader(&archive)
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		data, _ := io.ReadAll(reader)
		entries[header.Name], contents[header.Name] = header, string(data)
	}
	if len(entries) != 5 || entries["mnt/"] != nil || entries["d.sock"] != nil {
		t.Fatalf("entries %v", entries)
	}
	if a := entries["a.txt"]; a.Typeflag != tar.TypeReg || contents["a.txt"] != "first" || a.Mode&0o777 != 0o640 ||
		(xattr && a.PAXRecords["SCHILY.xattr.user.lazycloud"] != "kept") {
		t.Fatalf("a.txt %+v", a)
	}
	if b := entries["b.txt"]; b.Typeflag != tar.TypeLink || b.Linkname != "a.txt" {
		t.Fatalf("hardlink %+v", b)
	}
	if c := entries["c.link"]; c.Typeflag != tar.TypeSymlink || c.Linkname != "a.txt" {
		t.Fatalf("symlink %+v", c)
	}
	if entries["dir/"].Typeflag != tar.TypeDir || contents["dir/inner.txt"] != "inner" {
		t.Fatalf("directory %v", entries)
	}
}
