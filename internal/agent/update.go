package agent

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// An installed agent lives in a root directory:
//
//	releases/<version>/{lazycloud-agent,supervisor,runtime/<python>}
//	current  -> releases/<version>
//	previous -> releases/<version>
//	agent-service.sh, the service's start wrapper
//
// An update unpacks the next release beside the current one, writes the
// trial marker, switches current and exits. The wrapper rolls current back
// to previous when the new release starts three times without opening a
// session.
const (
	// AgentExecutable is the agent's name inside a release.
	AgentExecutable = "lazycloud-agent"
	// TrialFile in the state directory names a release on trial; the wrapper
	// appends a line per start.
	TrialFile = "update-trial"
	// RejectedFile in the state directory names the release the wrapper
	// rolled back from.
	RejectedFile = "update-rejected"

	maxReleaseBytes   = 2 << 30
	maxReleaseEntries = 50_000
	smokeTimeout      = 30 * time.Second
	// commitAfter is how long a session must stay open before the release
	// on trial counts as working.
	commitAfter = 10 * time.Second
	// trialTimeout ends an agent on trial that opened no session, so the
	// wrapper counts another start.
	trialTimeout = 5 * time.Minute
)

// ErrUpdateInstalled ends Run after a new release is installed; the service
// restarts into it.
var ErrUpdateInstalled = errors.New("agent update installed")

var errTrialExpired = errors.New("the release on trial opened no session")

var releaseVersion = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$`)

// updatable reports whether executable is a release inside root, which
// the service wrapper runs and can roll back.
func updatable(root, executable string) bool {
	if root == "" || executable == "" {
		return false
	}
	releases, err := filepath.EvalSymlinks(filepath.Join(root, "releases"))
	if err != nil {
		return false
	}
	exe, err := filepath.EvalSymlinks(executable)
	if err != nil {
		return false
	}
	rel, err := filepath.Rel(releases, exe)
	return err == nil && filepath.IsLocal(rel) && filepath.Dir(rel) != "." && filepath.Dir(filepath.Dir(rel)) == "."
}

// readMarker returns the first line of a marker file in the state
// directory, or "" when it does not exist.
func readMarker(stateDir, name string) string {
	data, err := os.ReadFile(filepath.Join(stateDir, name)) //nolint:gosec // a marker name this package defines
	if err != nil {
		return ""
	}
	line, _, _ := strings.Cut(string(data), "\n")
	return strings.TrimSpace(line)
}

// update installs an offered release and ends Run so the service restarts
// into it. Offers of the running, rejected or in-flight release are ignored.
func (a *Agent) update(offer *hostproto.UpdateAgent) {
	version := offer.GetVersion()
	if !a.updatable {
		a.log.Info("ignoring an update; this agent does not run under its service", "version", version)
		return
	}
	if version == a.cfg.Version || version == readMarker(a.cfg.StateDir, RejectedFile) {
		return
	}
	a.mu.Lock()
	busy := a.updating
	a.updating = true
	a.mu.Unlock()
	if busy {
		return
	}
	a.goOwned(func(ctx context.Context) {
		err := installRelease(ctx, a.http, a.cfg.AgentRoot, a.cfg.StateDir, offer)
		a.mu.Lock()
		a.updating = false
		a.mu.Unlock()
		if err != nil {
			a.log.Error("agent update failed", "version", version, "error", err)
			return
		}
		a.log.Info("agent update installed; restarting", "from", a.cfg.Version, "to", version)
		a.restart(ErrUpdateInstalled)
	})
}

// installRelease downloads, verifies, unpacks and smoke-runs a release,
// then makes it current with the trial marker written first, so a crash at
// any point leaves either the old release or a release on trial.
func installRelease(ctx context.Context, httpClient *http.Client, root, stateDir string, offer *hostproto.UpdateAgent) error {
	version := offer.GetVersion()
	if !releaseVersion.MatchString(version) {
		return fmt.Errorf("release version %q is not a version name", version)
	}
	if !sha256Hex.MatchString(offer.GetSha256()) {
		return fmt.Errorf("release digest %q is not a lowercase SHA-256", offer.GetSha256())
	}
	releases := filepath.Join(root, "releases")
	archive, err := downloadRelease(ctx, httpClient, releases, offer.GetUrl(), offer.GetSha256())
	if err != nil {
		return err
	}
	defer func() { _ = os.Remove(archive) }()
	staging, err := os.MkdirTemp(releases, ".release-")
	if err != nil {
		return fmt.Errorf("create release directory: %w", err)
	}
	defer func() { _ = os.RemoveAll(staging) }()
	if err := extractRelease(archive, staging); err != nil {
		return err
	}
	if err := smokeRun(ctx, filepath.Join(staging, AgentExecutable), version); err != nil {
		return err
	}
	target := filepath.Join(releases, version)
	if err := removeTree(target); err != nil {
		return err
	}
	if err := os.Rename(staging, target); err != nil {
		return fmt.Errorf("install release: %w", err)
	}
	current, err := os.Readlink(filepath.Join(root, "current"))
	if err != nil {
		return fmt.Errorf("read current release: %w", err)
	}
	if err := replaceLink(filepath.Join(root, "previous"), current); err != nil {
		return err
	}
	if err := writeFileAtomic(filepath.Join(stateDir, TrialFile), []byte(version+"\n"), 0o600); err != nil {
		return fmt.Errorf("write update trial: %w", err)
	}
	return replaceLink(filepath.Join(root, "current"), filepath.Join("releases", version))
}

func downloadRelease(ctx context.Context, httpClient *http.Client, dir, url, digest string) (string, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return "", fmt.Errorf("release URL: %w", err)
	}
	response, err := httpClient.Do(request)
	if err != nil {
		return "", fmt.Errorf("download release: %w", err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusOK {
		return "", fmt.Errorf("download release: HTTP %d", response.StatusCode)
	}
	file, err := os.CreateTemp(dir, ".download-")
	if err != nil {
		return "", fmt.Errorf("create release download: %w", err)
	}
	keep := false
	defer func() {
		_ = file.Close()
		if !keep {
			_ = os.Remove(file.Name())
		}
	}()
	hash := sha256.New()
	n, err := io.Copy(io.MultiWriter(file, hash), io.LimitReader(response.Body, maxReleaseBytes+1))
	if err != nil {
		return "", fmt.Errorf("download release: %w", err)
	}
	if n > maxReleaseBytes {
		return "", fmt.Errorf("release archive exceeds %d bytes", maxReleaseBytes)
	}
	if got := hex.EncodeToString(hash.Sum(nil)); got != digest {
		return "", fmt.Errorf("release archive digest is %s, expected %s", got, digest)
	}
	if err := file.Close(); err != nil {
		return "", fmt.Errorf("write release download: %w", err)
	}
	keep = true
	return file.Name(), nil
}

// extractRelease unpacks a tar.gz of regular files and directories into
// dir, refusing paths that leave it.
func extractRelease(archive, dir string) error {
	f, err := os.Open(archive) //nolint:gosec // a path this agent created
	if err != nil {
		return fmt.Errorf("open release archive: %w", err)
	}
	defer func() { _ = f.Close() }()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return fmt.Errorf("read release archive: %w", err)
	}
	reader := tar.NewReader(gz)
	var total int64
	for entries := 0; ; entries++ {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return fmt.Errorf("read release archive: %w", err)
		}
		if entries >= maxReleaseEntries {
			return fmt.Errorf("release archive has more than %d entries", maxReleaseEntries)
		}
		name := filepath.Clean(strings.TrimPrefix(header.Name, "./"))
		if name == "." {
			continue
		}
		if !filepath.IsLocal(name) {
			return fmt.Errorf("release archive entry %q leaves the release directory", header.Name)
		}
		path := filepath.Join(dir, name)
		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(path, 0o755); err != nil { //nolint:gosec // releases are world-readable
				return fmt.Errorf("unpack release: %w", err)
			}
		case tar.TypeReg:
			total += header.Size
			if total > maxReleaseBytes {
				return fmt.Errorf("release archive unpacks to more than %d bytes", maxReleaseBytes)
			}
			if err := writeReleaseFile(path, reader, header.FileInfo().Mode().Perm()); err != nil {
				return err
			}
		default:
			return fmt.Errorf("release archive entry %q is not a file or directory", header.Name)
		}
	}
	return nil
}

func writeReleaseFile(path string, r io.Reader, mode fs.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil { //nolint:gosec // releases are world-readable
		return fmt.Errorf("unpack release: %w", err)
	}
	out, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, mode&0o755) //nolint:gosec // inside the staging directory
	if err != nil {
		return fmt.Errorf("unpack release: %w", err)
	}
	if _, err := io.Copy(out, r); err != nil { //nolint:gosec // total size is bounded by the caller
		_ = out.Close()
		return fmt.Errorf("unpack release: %w", err)
	}
	if err := out.Close(); err != nil {
		return fmt.Errorf("unpack release: %w", err)
	}
	return nil
}

// smokeRun checks that the new agent runs on this host and is the version
// offered.
func smokeRun(ctx context.Context, executable, version string) error {
	ctx, cancel := context.WithTimeout(ctx, smokeTimeout)
	defer cancel()
	out, err := exec.CommandContext(ctx, executable, "--version").Output() //nolint:gosec // the verified release
	if err != nil {
		return fmt.Errorf("run new agent --version: %w", err)
	}
	if got := string(bytes.TrimSpace(out)); got != version {
		return fmt.Errorf("new agent reports version %q, expected %q", got, version)
	}
	return nil
}

// replaceLink points link at target atomically.
func replaceLink(link, target string) error {
	tmp := link + ".new"
	_ = os.Remove(tmp)
	if err := os.Symlink(target, tmp); err != nil {
		return fmt.Errorf("link %s: %w", link, err)
	}
	if err := os.Rename(tmp, link); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("link %s: %w", link, err)
	}
	return nil
}

// removeTree renames a directory aside before deleting it, so an
// interrupted removal never leaves a partial release under its name.
func removeTree(path string) error {
	if _, err := os.Lstat(path); errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	doomed := filepath.Join(filepath.Dir(path), fmt.Sprintf(".removing-%s-%d", filepath.Base(path), time.Now().UnixNano()))
	if err := os.Rename(path, doomed); err != nil {
		return fmt.Errorf("remove %s: %w", path, err)
	}
	if err := os.RemoveAll(doomed); err != nil {
		return fmt.Errorf("remove %s: %w", path, err)
	}
	return nil
}

// watchTrial ends an agent on trial that does not commit in time, so the
// wrapper counts another start towards rollback.
func (a *Agent) watchTrial(ctx context.Context) {
	timer := time.NewTimer(trialTimeout)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-a.committed:
	case <-timer.C:
		a.log.Error("the release on trial opened no session; exiting so the service can roll back", "version", a.cfg.Version)
		a.restart(errTrialExpired)
	}
}

// commitAfterSession commits the release on trial once a session has
// stayed open for commitAfter.
func (a *Agent) commitAfterSession(ctx context.Context) {
	if !sleep(ctx, commitAfter) {
		return
	}
	a.mu.Lock()
	if a.trial == "" || a.updating {
		a.mu.Unlock()
		return
	}
	a.trial = ""
	// Pruning must not race an update's download into the releases directory.
	a.updating = true
	a.mu.Unlock()
	defer func() {
		a.mu.Lock()
		a.updating = false
		a.mu.Unlock()
	}()
	if err := os.Remove(filepath.Join(a.cfg.StateDir, TrialFile)); err != nil && !errors.Is(err, fs.ErrNotExist) {
		a.log.Error("committing the agent update failed", "error", err)
		return
	}
	close(a.committed)
	a.log.Info("agent update committed", "version", a.cfg.Version)
	if err := a.pruneReleases(ctx); err != nil {
		a.log.Warn("pruning old agent releases failed", "error", err)
	}
}

// pruneReleases removes releases other than current and previous that no
// container on this host still mounts, and leftovers of interrupted
// installs.
func (a *Agent) pruneReleases(ctx context.Context) error {
	root := a.cfg.AgentRoot
	releases := filepath.Join(root, "releases")
	keep := []string{}
	for _, link := range []string{"current", "previous"} {
		if target, err := filepath.EvalSymlinks(filepath.Join(root, link)); err == nil {
			keep = append(keep, target)
		}
	}
	list, err := a.docker.ContainerList(ctx, client.ContainerListOptions{
		All:     true,
		Filters: client.Filters{}.Add("label", labelHost+"="+a.identity.HostID),
	})
	if err != nil {
		return fmt.Errorf("list containers: %w", err)
	}
	var mounted []string
	for _, summary := range list.Items {
		for _, m := range summary.Mounts {
			mounted = append(mounted, m.Source)
		}
	}
	resolved, err := filepath.EvalSymlinks(releases)
	if err != nil {
		return fmt.Errorf("resolve releases: %w", err)
	}
	entries, err := os.ReadDir(resolved)
	if err != nil {
		return fmt.Errorf("list releases: %w", err)
	}
	for _, entry := range entries {
		path := filepath.Join(resolved, entry.Name())
		inUse := slices.Contains(keep, path) || slices.ContainsFunc(mounted, func(source string) bool {
			return source == path || strings.HasPrefix(source, path+string(filepath.Separator))
		})
		if inUse {
			continue
		}
		if err := os.RemoveAll(path); err != nil {
			return fmt.Errorf("remove release %s: %w", entry.Name(), err)
		}
	}
	return nil
}
