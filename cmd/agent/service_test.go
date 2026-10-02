package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// serviceRoot lays out an install root with releases 1 and 2, current on 2
// and previous on 1, and release 2 on trial. Each release's agent records
// that it ran; release 2 runs body.
func serviceRoot(t *testing.T, body string) (root, state string) {
	t.Helper()
	root = t.TempDir()
	state = filepath.Join(root, "state")
	for version, script := range map[string]string{
		"1": "echo 1 >>\"$LAZYCLOUD_AGENT_STATE_DIR/ran\"\n",
		"2": "echo 2 >>\"$LAZYCLOUD_AGENT_STATE_DIR/ran\"\n" + body,
	} {
		dir := filepath.Join(root, "releases", version)
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "lazycloud-agent"), []byte("#!/bin/sh\n"+script), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.MkdirAll(state, 0o700); err != nil {
		t.Fatal(err)
	}
	for link, target := range map[string]string{"current": "releases/2", "previous": "releases/1"} {
		if err := os.Symlink(target, filepath.Join(root, link)); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(state, "update-trial"), []byte("2\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return root, state
}

// start runs the wrapper once, as systemd does on each (re)start.
func start(t *testing.T, root, state string) {
	t.Helper()
	cmd := exec.CommandContext(context.Background(), "sh", "agent-service.sh", "join", "--server=x")
	cmd.Env = append(os.Environ(), "LAZYCLOUD_AGENT_ROOT="+root, "LAZYCLOUD_AGENT_STATE_DIR="+state)
	out, err := cmd.CombinedOutput()
	t.Logf("start: %s (exit %v)", strings.TrimSpace(string(out)), err)
}

func readTrimmed(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

func TestServiceWrapperRollsBackAReleaseThatNeverConnects(t *testing.T) {
	root, state := serviceRoot(t, "exit 1\n")
	for range 5 {
		start(t, root, state)
	}
	if got := readTrimmed(t, filepath.Join(state, "ran")); got != "2\n2\n2\n1\n1" {
		t.Fatalf("releases started:\n%s\nwant release 2 three times, then release 1", got)
	}
	if target, _ := os.Readlink(filepath.Join(root, "current")); target != "releases/1" {
		t.Fatalf("current links to %q after rollback", target)
	}
	if got := readTrimmed(t, filepath.Join(state, "update-rejected")); got != "2" {
		t.Fatalf("update-rejected is %q", got)
	}
	if _, err := os.Stat(filepath.Join(state, "update-trial")); !os.IsNotExist(err) {
		t.Fatalf("the trial marker survived the rollback: %v", err)
	}
}

func TestServiceWrapperKeepsAReleaseThatCommits(t *testing.T) {
	// Release 2 crashes once, then connects and commits on its second start.
	root, state := serviceRoot(t, `
if [ "$(wc -l <"$LAZYCLOUD_AGENT_STATE_DIR/ran")" -ge 2 ]; then rm -f "$LAZYCLOUD_AGENT_STATE_DIR/update-trial"; exit 0; fi
exit 1
`)
	for range 5 {
		start(t, root, state)
	}
	if got := readTrimmed(t, filepath.Join(state, "ran")); got != "2\n2\n2\n2\n2" {
		t.Fatalf("releases started:\n%s\nwant release 2 every time", got)
	}
	if target, _ := os.Readlink(filepath.Join(root, "current")); target != "releases/2" {
		t.Fatalf("current links to %q", target)
	}
	if _, err := os.Stat(filepath.Join(state, "update-rejected")); !os.IsNotExist(err) {
		t.Fatalf("a committed release was rejected: %v", err)
	}
}
