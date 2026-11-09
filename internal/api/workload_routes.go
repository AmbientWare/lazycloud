package api

import (
	"net/http"
	"strings"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/edge"
)

// workloadRoutes serves deployed endpoints and ASGI apps on the API host,
// so the dashboard reaches them same-origin with its session:
//
//	/v1/workspaces/{workspace}/apps/{app}/workloads/{endpoint|asgi}/{name}/invoke[/{path...}]
//
// The edge forwards them like its own hosts. They stream, so the API's body
// limit and operation router do not apply.
func (s *Server) workloadRoutes(mux *http.ServeMux) {
	for _, kind := range []apitypes.WorkloadKind{apitypes.WorkloadKindEndpoint, apitypes.WorkloadKindAsgi} {
		base := "/v1/workspaces/{workspace}/apps/{app}/workloads/" + string(kind) + "/{name}/invoke"
		handler := s.authenticate(s.serveWorkload(kind))
		mux.Handle(base, handler)
		mux.Handle(base+"/{path...}", handler)
	}
}

func (s *Server) serveWorkload(kind apitypes.WorkloadKind) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ws, err := s.workspace(r.Context(), r.PathValue("workspace"))
		if err != nil {
			s.writeError(w, r, err)
			return
		}
		app, name := r.PathValue("app"), r.PathValue("name")
		stripCredentials(r)
		// The app learns where it is mounted, to build its own links.
		r.Header.Set("X-Forwarded-Prefix", edge.InvokePath(ws.Name, app, kind, name))
		s.owners.Edge.ServeWorkload(w, r, ws.ID, app, kind, name, r.PathValue("path"))
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
