package agent

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

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
	t.Parallel()
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
