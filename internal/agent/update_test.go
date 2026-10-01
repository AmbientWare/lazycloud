package agent

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

type entry struct {
	name, body string
	kind       byte
}

func releaseArchive(t *testing.T, entries ...entry) []byte {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for _, e := range entries {
		header := &tar.Header{Name: e.name, Mode: 0o755, Typeflag: e.kind, Size: int64(len(e.body))}
		if e.kind == tar.TypeSymlink {
			header.Linkname, header.Size = e.body, 0
		}
		if err := tw.WriteHeader(header); err != nil {
			t.Fatal(err)
		}
		if e.kind == tar.TypeReg {
			if _, err := tw.Write([]byte(e.body)); err != nil {
				t.Fatal(err)
			}
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

// agentRelease is an archive whose agent reports version.
func agentRelease(t *testing.T, version string) []byte {
	t.Helper()
	return releaseArchive(t,
		entry{"./lazycloud-agent", "#!/bin/sh\necho " + version + "\n", tar.TypeReg},
		entry{"./supervisor", "", tar.TypeReg},
		entry{"./runtime/3.12/", "", tar.TypeDir},
	)
}

// serveRelease serves archive and returns the update offering it.
func serveRelease(t *testing.T, version string, archive []byte) *hostproto.UpdateAgent {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(archive) }))
	t.Cleanup(server.Close)
	sum := sha256.Sum256(archive)
	return &hostproto.UpdateAgent{Version: version, Url: server.URL + "/release.tar.gz", Sha256: hex.EncodeToString(sum[:])}
}

// installRoot lays out an install root running release 1.0.0.
func installRoot(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	release := filepath.Join(root, "releases", "1.0.0")
	if err := os.MkdirAll(release, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(release, AgentExecutable), []byte("#!/bin/sh\necho 1.0.0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("releases/1.0.0", filepath.Join(root, "current")); err != nil {
		t.Fatal(err)
	}
	return root
}

func linkTarget(t *testing.T, root, name string) string {
	t.Helper()
	target, _ := os.Readlink(filepath.Join(root, name))
	return target
}

func TestInstallReleaseRefusesBadReleases(t *testing.T) {
	good := agentRelease(t, "2.0.0")
	tampered := serveRelease(t, "2.0.0", good)
	tampered.Sha256 = strings.Repeat("0", 64)
	cases := map[string]*hostproto.UpdateAgent{
		"digest mismatch": tampered,
		"escaping path":   serveRelease(t, "2.0.0", releaseArchive(t, entry{"../escape", "x", tar.TypeReg})),
		"symlink":         serveRelease(t, "2.0.0", releaseArchive(t, entry{"lazycloud-agent", "/bin/sh", tar.TypeSymlink})),
		"wrong version":   serveRelease(t, "2.0.0", agentRelease(t, "1.9.9")),
		"crashing agent":  serveRelease(t, "2.0.0", releaseArchive(t, entry{"lazycloud-agent", "#!/bin/sh\nexit 3\n", tar.TypeReg})),
		"bad version":     serveRelease(t, "../2.0.0", good),
	}
	for name, offer := range cases {
		t.Run(name, func(t *testing.T) {
			root, state := installRoot(t), t.TempDir()
			err := installRelease(t.Context(), http.DefaultClient, root, state, offer)
			if err == nil {
				t.Fatal("installed a bad release")
			}
			t.Log(err)
			if linkTarget(t, root, "current") != "releases/1.0.0" {
				t.Fatalf("current moved to %q", linkTarget(t, root, "current"))
			}
			if _, err := os.Stat(filepath.Join(state, TrialFile)); !os.IsNotExist(err) {
				t.Fatalf("a refused release left a trial: %v", err)
			}
			if _, err := os.Stat(filepath.Join(filepath.Dir(root), "escape")); !os.IsNotExist(err) {
				t.Fatal("the archive wrote outside the release")
			}
			entries, _ := os.ReadDir(filepath.Join(root, "releases"))
			if len(entries) != 1 {
				t.Fatalf("releases after a refused install: %v", entries)
			}
		})
	}
}

func TestAgentUpdatesItselfAndCommitsTheRelease(t *testing.T) {
	e := newEnv(t)
	root := installRoot(t)
	if err := os.WriteFile(filepath.Join(e.stateDir, RejectedFile), []byte("0.9.0\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	running := func(version string) func(*Config) {
		return func(cfg *Config) {
			cfg.AgentRoot, cfg.Version = root, version
			cfg.Executable = filepath.Join(root, "releases", version, AgentExecutable)
		}
	}
	first := e.startAgent(running("1.0.0"))
	s := e.session()
	if !s.hello.GetUpdatable() || s.hello.GetRejectedVersion() != "0.9.0" || s.hello.GetAgentVersion() != "1.0.0" {
		t.Fatalf("hello %v", s.hello)
	}
	id := e.startReady(s, 1)

	// The rejected release is not installed again.
	s.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Update{Update: serveRelease(t, "0.9.0", agentRelease(t, "0.9.0"))}})
	began := time.Now()
	s.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Update{Update: serveRelease(t, "2.0.0", agentRelease(t, "2.0.0"))}})
	if err := first.exited(t); !errors.Is(err, ErrUpdateInstalled) {
		t.Fatalf("Run returned %v", err)
	}
	t.Logf("update offer to agent exit: %s", time.Since(began))
	if linkTarget(t, root, "current") != "releases/2.0.0" || linkTarget(t, root, "previous") != "releases/1.0.0" {
		t.Fatalf("current %q, previous %q", linkTarget(t, root, "current"), linkTarget(t, root, "previous"))
	}
	if got := readMarker(e.stateDir, TrialFile); got != "2.0.0" {
		t.Fatalf("trial marker %q", got)
	}
	if _, err := os.Stat(filepath.Join(root, "releases", "0.9.0")); !os.IsNotExist(err) {
		t.Fatal("the rejected release was installed")
	}

	// A stale release no container uses is pruned once the new one commits.
	if err := os.MkdirAll(filepath.Join(root, "releases", "0.5.0"), 0o755); err != nil {
		t.Fatal(err)
	}
	e.startAgent(running("2.0.0"))
	s = e.session()
	if containers := s.hello.GetContainers(); len(containers) != 1 || containers[0].GetContainerId() != id || containers[0].GetPhase() == exited {
		t.Fatalf("the container did not survive the update: %v", containers)
	}
	e.eventually("the release commits", func() bool { return readMarker(e.stateDir, TrialFile) == "" })
	e.eventually("the stale release is pruned", func() bool {
		_, err := os.Stat(filepath.Join(root, "releases", "0.5.0"))
		return os.IsNotExist(err)
	})
	for _, version := range []string{"1.0.0", "2.0.0"} {
		if _, err := os.Stat(filepath.Join(root, "releases", version)); err != nil {
			t.Fatalf("release %s was pruned: %v", version, err)
		}
	}
}
