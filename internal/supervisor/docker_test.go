package supervisor

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/rand"
	"errors"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const busybox = "docker.io/library/busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"

// docker runs the docker CLI and returns its trimmed output. It outlives the
// test's context, which has ended when cleanups run.
func docker(t *testing.T, args ...string) (string, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.WithoutCancel(t.Context()), 2*time.Minute)
	defer cancel()
	out, err := exec.CommandContext(ctx, "docker", args...).CombinedOutput()
	return strings.TrimSpace(string(out)), err //nolint:wrapcheck // the test reads the exit status
}

func mustDocker(t *testing.T, args ...string) string {
	t.Helper()
	out, err := docker(t, args...)
	if err != nil {
		t.Fatalf("docker %s: %v: %s", strings.Join(args, " "), err, out)
	}
	return out
}

// dockerTest builds the static supervisor and pulls busybox; it skips when
// Docker is absent.
func dockerTest(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("docker"); err != nil {
		t.Skip("docker is not installed")
	}
	if _, err := docker(t, "image", "inspect", busybox); err != nil {
		mustDocker(t, "pull", busybox)
	}
	bin := filepath.Join(t.TempDir(), "supervisor")
	build := exec.CommandContext(t.Context(), "go", "build", "-o", bin, "../../cmd/supervisor")
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build the supervisor: %v: %s", err, out)
	}
	return bin
}

func uniqueName(prefix string) string {
	return prefix + "-" + strings.ToLower(rand.Text()[:10])
}

func runContainer(t *testing.T, args ...string) string {
	t.Helper()
	name := uniqueName("lc-supervisor-test")
	mustDocker(t, append([]string{"run", "-d", "--name", name}, args...)...)
	t.Cleanup(func() { _, _ = docker(t, "rm", "-f", name) })
	return name
}

func TestNetfilterBlocksAndAllowsEgressInAContainer(t *testing.T) {
	bin := dockerTest(t)
	target := runContainer(t, busybox, "sh", "-c", "echo ok > /tmp/index.html && exec httpd -f -p 8080 -h /tmp")
	targetIP := mustDocker(t, "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", target)
	subject := runContainer(t, busybox, "sleep", "300")
	fetch := func() bool {
		out, err := docker(t, "exec", subject, "wget", "-q", "-T", "2", "-O-", "http://"+targetIP+":8080/")
		return err == nil && out == "ok"
	}
	for deadline := time.Now().Add(10 * time.Second); !fetch(); time.Sleep(100 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("the target never answered")
		}
	}
	apply := func(policy string) (string, error) {
		return docker(t, "run", "--rm", "--network", "container:"+subject, "--cap-add", "NET_ADMIN",
			"-v", bin+":/supervisor:ro", "--entrypoint", "/supervisor", busybox, "netfilter", policy)
	}
	steps := []struct {
		policy  string
		reaches bool
	}{
		{`{"block":true}`, false},
		{`{"block":true,"allow":["` + targetIP + `/32"]}`, true},
		{`{"allow":["10.255.255.0/24","fd00::/8"]}`, false},
		{`{"block":false,"allow":[]}`, true},
	}
	for _, step := range steps {
		started := time.Now()
		if out, err := apply(step.policy); err != nil {
			t.Fatalf("apply %s: %v: %s", step.policy, err, out)
		}
		t.Logf("applied %s in %s", step.policy, time.Since(started))
		if got := fetch(); got != step.reaches {
			t.Fatalf("with %s the target is reachable: %v", step.policy, got)
		}
	}
	if out, err := apply(`{"allow":["not-a-cidr"]}`); err == nil || !strings.Contains(out, "not-a-cidr") {
		t.Fatalf("invalid range accepted: %v %s", err, out)
	}
}

// rawFrame sends a UDP packet in a hand-built Ethernet frame from a packet
// socket bound to an unassigned EtherType, so the kernel labels the packet
// with that protocol while the frame says IPv4.
const rawFrame = `import socket, struct, sys
dst_ip, dst_mac, src_ip, src_mac, payload = sys.argv[1:6]
payload = payload.encode()
mac = lambda m: bytes.fromhex(m.replace(":", ""))
udp = struct.pack("!HHHH", 40000, 9999, 8 + len(payload), 0) + payload
ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0, socket.inet_aton(src_ip), socket.inet_aton(dst_ip))
total = sum(struct.unpack("!10H", ip))
total = (total & 0xFFFF) + (total >> 16)
ip = ip[:10] + struct.pack("!H", ~total & 0xFFFF) + ip[12:]
sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
sock.bind(("eth0", 0x88B5))
for _ in range(3):
    try:
        sock.send(mac(dst_mac) + mac(src_mac) + b"\x08\x00" + ip + udp)
    except OSError as e:
        print("send:", e)
`

// A blocked container cannot slip IP packets past the filter by labelling
// them with another protocol from a raw socket.
func TestNetfilterDropsRawFramesThatMislabelTheirProtocol(t *testing.T) {
	bin := dockerTest(t)
	const python = "python:3.12-slim"
	if _, err := docker(t, "image", "inspect", python); err != nil {
		mustDocker(t, "pull", python)
	}
	target := runContainer(t, python, "python3", "-uc",
		"import socket\ns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\ns.bind(('0.0.0.0', 9999))\nwhile True: print(s.recv(100).decode(), flush=True)")
	subject := runContainer(t, python, "sleep", "300")
	address := func(name, field string) string {
		return mustDocker(t, "inspect", "-f", "{{range .NetworkSettings.Networks}}{{."+field+"}}{{end}}", name)
	}
	send := func(payload string) bool {
		mustDocker(t, "exec", subject, "python3", "-c", rawFrame,
			address(target, "IPAddress"), address(target, "MacAddress"), address(subject, "IPAddress"), address(subject, "MacAddress"), payload)
		for deadline := time.Now().Add(2 * time.Second); time.Now().Before(deadline); time.Sleep(100 * time.Millisecond) {
			if logs, _ := docker(t, "logs", target); strings.Contains(logs, payload) {
				return true
			}
		}
		return false
	}
	if !send("open-frame") {
		t.Fatal("a raw frame did not arrive without a policy")
	}
	if out, err := docker(t, "run", "--rm", "--network", "container:"+subject, "--cap-add", "NET_ADMIN",
		"-v", bin+":/supervisor:ro", "--entrypoint", "/supervisor", busybox, "netfilter", `{"block":true}`); err != nil {
		t.Fatalf("apply the policy: %v: %s", err, out)
	}
	if send("blocked-frame") {
		t.Fatal("a mislabelled raw frame passed the block")
	}
}

func TestDevboxRootPersistsWritesAndKeepsProc(t *testing.T) {
	bin := dockerTest(t)
	dir, err := os.MkdirTemp("", "lcdevbox")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		// The container wrote root-owned files.
		_, _ = docker(t, "run", "--rm", "-v", dir+":/cleanup", busybox, "rm", "-rf", "/cleanup/root", "/cleanup/link")
		_ = os.RemoveAll(dir)
	})
	link, root := filepath.Join(dir, "link"), filepath.Join(dir, "root")
	for _, d := range []string{link, root} {
		if err := os.Mkdir(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	h := &harness{t: t, socket: filepath.Join(link, "agent.sock"), server: &linkServer{conns: make(chan *linkConn, 4)}}
	h.listen()
	t.Cleanup(h.grpc.Stop)

	started := time.Now()
	name := runContainer(t, "--cap-add", "SYS_ADMIN", "--security-opt", "apparmor=unconfined",
		"-v", bin+":/opt/lazycloud/bin/supervisor:ro", "-v", link+":/run/lazycloud", "-v", root+":/lazycloud/root",
		"-e", SocketEnv+"=/run/lazycloud/agent.sock", "--entrypoint", "/opt/lazycloud/bin/supervisor", busybox)
	c := h.accept()
	c.configurePod(t, &hostproto.PodProcess{
		Root:             "/lazycloud/root",
		WorkingDirectory: "/",
		Command:          []string{"sh", "-c", "echo persisted > /persisted.txt; head -1 /proc/self/status > /proc-status.txt; exec sleep 600"},
	}, "/run/lazycloud/control.sock")
	c.until(t, isReady)
	t.Logf("container start to ready with a first-use seed: %s", time.Since(started))

	// The pod is ready once its command started; it writes these next.
	for deadline := time.Now().Add(10 * time.Second); ; time.Sleep(50 * time.Millisecond) {
		persisted, _ := os.ReadFile(filepath.Join(root, "persisted.txt"))
		status, _ := os.ReadFile(filepath.Join(root, "proc-status.txt"))
		if string(persisted) == "persisted\n" && strings.HasPrefix(string(status), "Name:") {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("persisted file %q, /proc inside the root %q", persisted, status)
		}
	}
	if _, err := os.Stat(filepath.Join(root, "bin/busybox")); err != nil {
		t.Fatalf("the root was not seeded from the image: %v", err)
	}

	socket := filepath.Join(link, "control.sock")
	ctl := &controlHarness{t: t, socket: socket, client: unixClient(socket)}
	var p apitypes.Process
	cwd := "/"
	if status, _ := ctl.do(http.MethodPost, "/processes", apitypes.ProcessRequest{
		Args: []string{"sh", "-c", "ls /; tr '\\0' ' ' < /proc/1/cmdline"}, Cwd: &cwd,
	}, &p); status != http.StatusCreated {
		t.Fatalf("start a process: %d", status)
	}
	for p.Running {
		ctl.do(http.MethodGet, "/processes/"+p.ProcessId+"?wait_seconds=5", nil, &p)
	}
	if !strings.Contains(p.Stdout, "persisted.txt") || !strings.Contains(p.Stdout, "/opt/lazycloud/bin/supervisor") {
		t.Fatalf("a process in the root sees %q (stderr %q)", p.Stdout, p.Stderr)
	}
	// Processes in the root never hold CAP_SYS_ADMIN, nor can gain it.
	if status, _ := ctl.do(http.MethodPost, "/processes", apitypes.ProcessRequest{
		Args: []string{"sh", "-c", "grep -E '^(CapBnd|CapEff|NoNewPrivs)' /proc/self/status; mkdir -p /m; mount -t tmpfs none /m && echo mounted"}, Cwd: &cwd,
	}, &p); status != http.StatusCreated {
		t.Fatalf("start a process: %d", status)
	}
	for p.Running {
		ctl.do(http.MethodGet, "/processes/"+p.ProcessId+"?wait_seconds=5", nil, &p)
	}
	caps := map[string]string{}
	for _, line := range strings.Split(strings.TrimSpace(p.Stdout), "\n") {
		if key, value, ok := strings.Cut(line, ":"); ok {
			caps[key] = strings.TrimSpace(value)
		}
	}
	for _, set := range []string{"CapBnd", "CapEff"} {
		bits, err := strconv.ParseUint(caps[set], 16, 64)
		if err != nil || bits&(1<<21) != 0 {
			t.Fatalf("%s is %q in the root: %s", set, caps[set], p.Stdout)
		}
	}
	if caps["NoNewPrivs"] != "1" || strings.Contains(p.Stdout, "mounted") {
		t.Fatalf("a process in the root may gain privilege: %s %s", p.Stdout, p.Stderr)
	}
	if status, _ := ctl.do(http.MethodPut, "/files/content?path=notes/rel.txt", []byte("relative"), nil); status != http.StatusNoContent {
		t.Fatalf("upload: %d", status)
	}
	if data, err := os.ReadFile(filepath.Join(root, "workspace/notes/rel.txt")); err != nil || string(data) != "relative" {
		t.Fatalf("relative upload %q %v", data, err)
	}

	var archive []byte
	if status, _ := ctl.do(http.MethodPost, "/filesystem", nil, &archive); status != http.StatusOK {
		t.Fatalf("filesystem: %d", status)
	}
	names := map[string]bool{}
	reader := tar.NewReader(bytes.NewReader(archive))
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		names[header.Name] = true
		if strings.HasPrefix(header.Name, "proc/") || strings.HasPrefix(header.Name, "run/lazycloud/") || header.Name == "etc/hosts" {
			t.Fatalf("the archive holds mounted %s", header.Name)
		}
	}
	if !names["persisted.txt"] || !names["bin/busybox"] || !names["workspace/notes/rel.txt"] {
		t.Fatalf("archive of %d entries lacks the root's files", len(names))
	}
	t.Logf("filesystem archive: %d entries, %d bytes", len(names), len(archive))

	c.send(t, &hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Drain{Drain: &hostproto.Drain{}}})
	if code := mustDocker(t, "wait", name); code != "0" {
		logs, _ := docker(t, "logs", name)
		t.Fatalf("drained devbox exited %s: %s", code, logs)
	}
}
