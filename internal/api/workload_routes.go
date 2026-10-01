package api

import (
	"net/http"
	"strconv"
	"strings"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/edge"
)

// workloadRoutes serves deployed endpoints and ASGI apps on the API host,
// as the reference platform's API did, so the dashboard reaches them
// same-origin with its session:
//
//	/v1/workspaces/{workspace}/apps/{app}/endpoints/{endpoint}[/versions/{version}]/invoke[/{path...}]
//	/v1/workspaces/{workspace}/apps/{app}/asgi/{endpoint}[/versions/{version}]/invoke[/{path...}]
//
// The edge forwards them like its own hosts. They stream, so the API's body
// limit and operation router do not apply.
func (s *Server) workloadRoutes(mux *http.ServeMux) {
	for collection, kind := range map[string]apitypes.WorkloadKind{
		"endpoints": apitypes.WorkloadKindEndpoint, "asgi": apitypes.WorkloadKindAsgi,
	} {
		base := "/v1/workspaces/{workspace}/apps/{app}/" + collection + "/{endpoint}"
		for _, prefix := range []string{base, base + "/versions/{version}"} {
			handler := s.authenticate(s.serveWorkload(kind))
			mux.Handle(prefix+"/invoke", handler)
			mux.Handle(prefix+"/invoke/{path...}", handler)
		}
	}
}

func (s *Server) serveWorkload(kind apitypes.WorkloadKind) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ws, err := s.workspace(r.Context(), r.PathValue("workspace"))
		if err != nil {
			s.writeError(w, r, err)
			return
		}
		var version *int
		if raw := r.PathValue("version"); raw != "" {
			n, err := strconv.Atoi(raw)
			if err != nil || n < 1 {
				writeJSONError(w, http.StatusNotFound, apitypes.NotFound, "no such version")
				return
			}
			version = &n
		}
		app, name := r.PathValue("app"), r.PathValue("endpoint")
		stripCredentials(r)
		// The app learns where it is mounted, to build its own links.
		r.Header.Set("X-Forwarded-Prefix", edge.InvokePath(ws.Name, app, kind, name, version))
		s.owners.Edge.ServeWorkload(w, r, ws.ID, app, kind, name, version, r.PathValue("path"))
	}
}

// stripCredentials removes what authenticated the caller to the API: the
// bearer token and the session cookie never reach a workload.
func stripCredentials(r *http.Request) {
	r.Header.Del("Authorization")
	cookies := r.Cookies()
	r.Header.Del("Cookie")
	var kept []string
	for _, c := range cookies {
		if c.Name != SessionCookie {
			kept = append(kept, c.String())
		}
	}
	if len(kept) > 0 {
		r.Header.Set("Cookie", strings.Join(kept, "; "))
	}
}
