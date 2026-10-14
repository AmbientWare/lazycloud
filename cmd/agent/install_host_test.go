//go:build hostruntime

package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// A foreground join installs the release, then the snapshotter, then runs
// the agent. It installs into /opt/lazycloud/agent, so it runs as root on a
// host without an agent, as in CI's host-runtime job.
func TestInstallScriptInstallsAReleaseAndRunsTheAgent(t *testing.T) {
	if out, err := exec.CommandContext(context.Background(), "uname", "-m").Output(); err != nil || strings.TrimSpace(string(out)) != "x86_64" {
		t.Skip("the fake gateway serves linux/amd64")
	}
	root := "/opt/lazycloud/agent"
	if os.Geteuid() != 0 {
		t.Skip("joining needs root")
	}
	if _, err := os.Stat(filepath.Dir(root)); err == nil {
		t.Skipf("%s exists; this test installs only where nothing is", filepath.Dir(root))
	}
	t.Cleanup(func() { _ = os.RemoveAll(filepath.Dir(root)) })
	archive := fakeRelease(t, "1.2.3")
	sum := sha256.Sum256(archive)
	url := gateway(t, "1.2.3", archive, nil)
	home := t.TempDir()
	state := filepath.Join(home, "state")

	if out, err := runInstall(t, home, "--gateway", url, "--server", "s:1", "--join-token", "t", "--state-dir", state,
		"--agent-version", "1.2.3", "--agent-sha256", strings.Repeat("0", 64)); err == nil || !strings.Contains(out, "error: agent artifact SHA-256 mismatch") {
		t.Fatalf("a release of another digest installs: %v\n%s", err, out)
	}
	out, err := runInstall(t, home, "--gateway", url+"/", "--server", "127.0.0.1:1", "--join-token", "lc_join_x", "--state-dir", state,
		"--agent-version", "1.2.3", "--agent-sha256", hex.EncodeToString(sum[:]), "--max-gpus", "1", "--hostname", "gpu box")
	t.Log(out)
	if err != nil {
		t.Fatalf("install failed: %v", err)
	}
	if !strings.Contains(out, "=> Starting lazycloud-agent") {
		t.Fatalf("install did not announce the agent start:\n%s", out)
	}
	if target, _ := os.Readlink(filepath.Join(root, "current")); target != "releases/1.2.3" {
		t.Fatalf("current links to %q", target)
	}
	args, err := os.ReadFile(filepath.Join(home, "agent-args"))
	if err != nil {
		t.Fatalf("the agent did not run: %v", err)
	}
	want := strings.Join([]string{
		"install-snapshotter",
		"join", "--server", "127.0.0.1:1", "--state-dir", state,
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
	url = gateway(t, "", archive, nil)
	if out, err := runInstall(t, home, "--gateway", url, "--server", "127.0.0.1:1", "--cloud-host-id", "h-1", "--state-dir", state); err != nil {
		t.Fatalf("second install failed: %v\n%s", err, out)
	}
	if target, _ := os.Readlink(filepath.Join(root, "current")); target != "releases/1.3.0" {
		t.Fatalf("current links to %q", target)
	}
	if target, _ := os.Readlink(filepath.Join(root, "previous")); target != "releases/1.2.3" {
		t.Fatalf("previous links to %q", target)
	}
}
