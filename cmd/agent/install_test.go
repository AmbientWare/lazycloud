package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
)

const installScript = "../../internal/compute/install.sh"

// fakeRelease is a release archive whose agent reports version and records
// the arguments of each run.
func fakeRelease(t *testing.T, version string) []byte {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	files := map[string]string{
		"lazycloud-agent":        "#!/bin/sh\nif [ \"$1\" = --version ]; then echo " + version + "; exit 0; fi\nprintf '%s\\n' \"$@\" >>\"$HOME/agent-args\"\n",
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

// gateway serves archive at the versioned and the unversioned path and
// counts the requests in requests, if given.
func gateway(t *testing.T, version string, archive []byte, requests *atomic.Int32) string {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if requests != nil {
			requests.Add(1)
		}
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

// Without root a join stops before it downloads anything, in either mode.
func TestInstallScriptRefusesToJoinWithoutRoot(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("runs as a user")
	}
	var requests atomic.Int32
	url := gateway(t, "1.2.3", fakeRelease(t, "1.2.3"), &requests)
	for _, mode := range []string{"--foreground", "--background"} {
		out, err := runInstall(t, t.TempDir(), "--gateway", url, "--server", "s:1", "--join-token", "t", mode)
		if err == nil || !strings.Contains(out, "error: joining requires root: the agent installs its snapshotter and reconfigures Docker; rerun with sudo") {
			t.Fatalf("%s without root: %v\n%s", mode, err, out)
		}
	}
	if n := requests.Load(); n != 0 {
		t.Fatalf("a refused join made %d requests", n)
	}
}

func TestInstallScriptRefusesBadInput(t *testing.T) {
	archive := fakeRelease(t, "1.2.3")
	url := gateway(t, "1.2.3", archive, nil)
	cases := []struct {
		name string
		args []string
		want string
	}{
		{"two credentials", []string{"--gateway", url, "--server", "s:1", "--join-token", "t", "--cloud-host-id", "h"}, "error: --join-token and --cloud-host-id cannot be combined"},
		{"no server", []string{"--gateway", url, "--join-token", "t"}, "error: --server is required"},
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
