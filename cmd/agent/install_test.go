package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

const installScript = "../../internal/compute/install.sh"

// fakeRelease is a release archive whose agent reports version and records
// the arguments it was started with.
func fakeRelease(t *testing.T, version string) []byte {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	files := map[string]string{
		"lazycloud-agent":        "#!/bin/sh\nif [ \"$1\" = --version ]; then echo " + version + "; exit 0; fi\nprintf '%s\\n' \"$@\" >\"$HOME/agent-args\"\n",
		"supervisor":             "#!/bin/sh\n",
		"runtime/3.12/marker.py": "",
	}
	for name, body := range files {
		if err := tw.WriteHeader(&tar.Header{Name: name, Mode: 0o755, Size: int64(len(body)), Typeflag: tar.TypeReg}); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// gateway serves archive at the versioned and the unversioned path.
func gateway(t *testing.T, version string, archive []byte) string {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/install/agent/" + version + "/linux/amd64", "/install/agent/linux/amd64":
			_, _ = w.Write(archive)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	return server.URL
}

func runInstall(t *testing.T, home string, args ...string) (string, error) {
	t.Helper()
	cmd := exec.CommandContext(context.Background(), "sh", append([]string{installScript}, args...)...)
	cmd.Env = append(os.Environ(), "HOME="+home)
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func TestInstallScriptInstallsAReleaseAndRunsTheAgent(t *testing.T) {
	if out, err := exec.CommandContext(context.Background(), "uname", "-m").Output(); err != nil || strings.TrimSpace(string(out)) != "x86_64" {
		t.Skip("the fake gateway serves linux/amd64")
	}
	if os.Geteuid() == 0 {
		t.Skip("a root install writes to /opt; this test installs for a user")
	}
	archive := fakeRelease(t, "1.2.3")
	sum := sha256.Sum256(archive)
	url := gateway(t, "1.2.3", archive)
	home := t.TempDir()

	out, err := runInstall(t, home, "--gateway", url+"/", "--server", "127.0.0.1:1", "--join-token", "lc_join_x",
		"--agent-version", "1.2.3", "--agent-sha256", hex.EncodeToString(sum[:]), "--max-gpus", "1", "--hostname", "gpu box")
	t.Log(out)
	if err != nil {
		t.Fatalf("install failed: %v", err)
	}
	if !strings.Contains(out, "=> Starting lazycloud-agent") {
		t.Fatalf("install did not announce the agent start:\n%s", out)
	}
	root := filepath.Join(home, ".lazycloud", "agent")
	if target, _ := os.Readlink(filepath.Join(root, "current")); target != "releases/1.2.3" {
		t.Fatalf("current links to %q", target)
	}
	args, err := os.ReadFile(filepath.Join(home, "agent-args"))
	if err != nil {
		t.Fatalf("the agent did not run: %v", err)
	}
	want := strings.Join([]string{
		"join", "--server", "127.0.0.1:1", "--state-dir", filepath.Join(root, "state"),
		"--join-token", "lc_join_x", "--max-gpus", "1", "--hostname", "gpu box",
		"--runtime-dir", filepath.Join(root, "current", "runtime"), "--supervisor", filepath.Join(root, "current", "supervisor"),
	}, "\n") + "\n"
	if string(args) != want {
		t.Fatalf("agent arguments:\n%s\nwant:\n%s", args, want)
	}

	// A second install of the server's default release, with the
	// placeholders left unfilled, learns the version from the agent and
	// keeps the first as previous.
	archive = fakeRelease(t, "1.3.0")
	url = gateway(t, "", archive)
	if out, err := runInstall(t, home, "--gateway", url, "--server", "127.0.0.1:1", "--cloud-host-id", "h-1"); err != nil {
		t.Fatalf("second install failed: %v\n%s", err, out)
	}
	if target, _ := os.Readlink(filepath.Join(root, "current")); target != "releases/1.3.0" {
		t.Fatalf("current links to %q", target)
	}
	if target, _ := os.Readlink(filepath.Join(root, "previous")); target != "releases/1.2.3" {
		t.Fatalf("previous links to %q", target)
	}
}

func TestInstallScriptRefusesBadInput(t *testing.T) {
	archive := fakeRelease(t, "1.2.3")
	url := gateway(t, "1.2.3", archive)
	cases := []struct {
		name string
		args []string
		want string
	}{
		{"digest mismatch", []string{"--gateway", url, "--server", "s:1", "--join-token", "t", "--agent-version", "1.2.3", "--agent-sha256", strings.Repeat("0", 64)}, "error: agent artifact SHA-256 mismatch"},
		{"two credentials", []string{"--gateway", url, "--server", "s:1", "--join-token", "t", "--cloud-host-id", "h"}, "error: --join-token and --cloud-host-id cannot be combined"},
		{"no server", []string{"--gateway", url, "--join-token", "t"}, "error: --server is required"},
	}
	if os.Geteuid() != 0 {
		cases = append(cases, struct {
			name string
			args []string
			want string
		}{"background without root", []string{"--gateway", url, "--server", "s:1", "--join-token", "t", "--background"}, "error: background installation requires root; rerun with sudo or use --foreground"})
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			out, err := runInstall(t, t.TempDir(), c.args...)
			if err == nil || !strings.Contains(out, c.want) {
				t.Fatalf("got %v:\n%s\nwant %q", err, out, c.want)
			}
		})
	}
}
