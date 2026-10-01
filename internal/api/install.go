package api

import (
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
)

// Agent installs are unauthenticated downloads: the script, and release
// archives from Config.AgentDistDir laid out as
// <version>/lazycloud-agent-linux-<arch>.tar.gz.
func (s *Server) installRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /install/agent", s.installScript)
	mux.HandleFunc("GET /install/agent/{os}/{arch}", s.installArchive)
	mux.HandleFunc("GET /install/agent/{version}/{os}/{arch}", s.installArchive)
}

func (s *Server) installScript(w http.ResponseWriter, r *http.Request) {
	script, err := s.owners.Compute.InstallScript(r.Context())
	if err != nil {
		s.writeError(w, r, err)
		return
	}
	w.Header().Set("Content-Type", "text/x-shellscript; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	_, _ = io.WriteString(w, script)
}

// installArchive serves a release archive. Publishing hashed it into the
// release row and the installer checks that digest, so serving does not
// read it twice. Without a version it serves the target release.
func (s *Server) installArchive(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	osName, arch, version := r.PathValue("os"), r.PathValue("arch"), r.PathValue("version")
	if osName != "linux" || (arch != "amd64" && arch != "arm64") {
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, "agent archives exist for linux/amd64 and linux/arm64")
		return
	}
	if s.cfg.AgentDistDir == "" {
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, "agent binary artifacts are not configured")
		return
	}
	var release compute.AgentRelease
	var err error
	if version == "" {
		release, err = s.owners.Compute.TargetRelease(ctx)
	} else if compute.ValidVersion(version) {
		release, err = s.owners.Compute.Release(ctx, version)
	} else {
		err = compute.ErrNotFound
	}
	if errors.Is(err, compute.ErrNotFound) {
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, "agent binary version not found")
		return
	}
	if err != nil {
		s.writeError(w, r, err)
		return
	}
	if _, ok := release.SHA256[arch]; !ok {
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, "agent binary artifact not found")
		return
	}
	path := filepath.Join(s.cfg.AgentDistDir, release.Version, compute.ArchiveName(arch))
	f, err := os.Open(path) //nolint:gosec // The version is validated and the file name is fixed.
	if err != nil {
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, "agent binary artifact not found")
		return
	}
	defer func() { _ = f.Close() }()
	info, err := f.Stat()
	if err != nil {
		s.writeError(w, r, err)
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("X-Lazycloud-Agent-Version", release.Version)
	if version != "" {
		w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
	} else {
		w.Header().Set("Cache-Control", "no-store")
	}
	http.ServeContent(w, r, compute.ArchiveName(arch), info.ModTime(), f)
}
