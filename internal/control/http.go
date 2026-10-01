package control

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"slices"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// WorkloadKind is a workload's kind. Realtime handlers are ASGI workloads.
type WorkloadKind string

const (
	KindFunction WorkloadKind = "function"
	KindEndpoint WorkloadKind = "endpoint"
	KindASGI     WorkloadKind = "asgi"
)

// KindOf is the workload kind a definition deploys as.
func KindOf(spec apitypes.FunctionSpec) WorkloadKind {
	if spec.Http == nil {
		return KindFunction
	}
	switch spec.Http.Kind {
	case apitypes.HttpKindEndpoint:
		return KindEndpoint
	case apitypes.HttpKindAsgi, apitypes.HttpKindRealtime:
		return KindASGI
	}
	return KindFunction
}

// RouteConflictError means another workload already answers on the
// subdomain or custom hostname a deploy claims.
type RouteConflictError struct {
	Function string
	Reason   string
}

func (e *RouteConflictError) Error() string {
	return fmt.Sprintf("function %s: %s", e.Function, e.Reason)
}

// HTTP workloads answer quickly and stay warm longer than functions.
const (
	httpTimeoutSeconds  = 180
	httpKeepWarmSeconds = 180
)

// defaultMethods are an endpoint's methods unless it names others.
var defaultMethods = []apitypes.HttpMethod{apitypes.HttpMethodGET, apitypes.HttpMethodPOST} //nolint:gochecknoglobals // a constant list

// resolveHTTP fills the HTTP defaults of a resolved spec. Methods apply only
// to endpoints, which answer on their route; ASGI apps own every path.
func resolveHTTP(spec apitypes.FunctionSpec, out *apitypes.FunctionSpec) error {
	if spec.Http == nil {
		return nil
	}
	h := *spec.Http
	if !h.Kind.Valid() {
		return &InvalidSpecError{Function: spec.Name, Reason: fmt.Sprintf("unknown http kind %q", h.Kind)}
	}
	if spec.TimeoutSeconds == nil {
		out.TimeoutSeconds = new(httpTimeoutSeconds)
	}
	if spec.KeepWarmSeconds == nil {
		out.KeepWarmSeconds = new(httpKeepWarmSeconds)
	}
	h.Authorized = orDefault(h.Authorized, true)
	h.Workers = orDefault(h.Workers, 1)
	if h.Kind == apitypes.HttpKindEndpoint {
		h.Route = orDefault(h.Route, "/")
		methods := slices.Clone(defaultMethods)
		if h.Methods != nil {
			methods = nil
			for _, m := range *h.Methods {
				if !slices.Contains(methods, m) {
					methods = append(methods, m)
				}
			}
		}
		h.Methods = &methods
	} else if (h.Methods != nil && !slices.Equal(*h.Methods, defaultMethods)) || (h.Route != nil && *h.Route != "/") {
		// The request validator fills the schema's defaults, so only values
		// other than them were asked for.
		return &InvalidSpecError{Function: spec.Name, Reason: "route and methods apply to endpoints; an ASGI app answers every path"}
	} else {
		h.Route, h.Methods = nil, nil
	}
	out.Http = &h
	return nil
}

// Deployment subdomains are DNS labels: a stem from the name, an 8-hex
// digest of the workload's identity, and room for the "-latest" or "-vN" a
// host may append.
const (
	maxLabelLength = 63
	digestLength   = 8
	maxStemLength  = maxLabelLength - 1 - digestLength - len("-latest")
)

// Subdomain is the DNS label every version of a workload answers on. It is a
// digest of the workspace, app, name and kind, so it survives redeploys and
// an app recreated under the same name keeps its URL. The digest is hex, so
// it is never mistaken for the "-vN" that may follow it.
func Subdomain(workspace uuid.UUID, app, name string, kind WorkloadKind) string {
	var stem strings.Builder
	dash := false
	for _, r := range strings.ToLower(strings.TrimSpace(name)) {
		if ('a' <= r && r <= 'z') || ('0' <= r && r <= '9') {
			if dash && stem.Len() > 0 {
				stem.WriteByte('-')
			}
			dash = false
			stem.WriteRune(r)
			continue
		}
		dash = true
	}
	label := stem.String()
	if len(label) > maxStemLength {
		label = strings.TrimRight(label[:maxStemLength], "-")
	}
	// NUL cannot appear in any field, so distinct identities never join into
	// the same digest input.
	sum := sha256.Sum256([]byte(strings.Join([]string{workspace.String(), app, name, string(kind)}, "\x00")))
	digest := hex.EncodeToString(sum[:])[:digestLength]
	if label == "" {
		return digest
	}
	return label + "-" + digest
}

// claimWorkloadRoute records the subdomain and custom hostname the workload answers
// on, in the deploy transaction. A custom hostname must be registered by an
// owner of the workspace.
func claimWorkloadRoute(ctx context.Context, q *Queries, workspace, workload uuid.UUID, app string, spec apitypes.FunctionSpec) error {
	var hostname *string
	if spec.Http != nil && spec.Http.Domain != nil {
		registered, err := q.OwnerRegisteredDomain(ctx, OwnerRegisteredDomainParams{WorkspaceID: workspace, Hostname: *spec.Http.Domain})
		if err != nil {
			return fmt.Errorf("read domain registration: %w", err)
		}
		if !registered {
			return &InvalidSpecError{
				Function: spec.Name,
				Reason:   *spec.Http.Domain + " is not registered to this account; register it with `lazycloud domain add` first",
			}
		}
		hostname = spec.Http.Domain
	}
	err := q.ClaimRoute(ctx, ClaimRouteParams{
		WorkloadID: workload,
		Subdomain:  Subdomain(workspace, app, spec.Name, KindOf(spec)),
		Hostname:   hostname,
	})
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		reason := "its subdomain collides with another deployment; rename it"
		if pgErr.ConstraintName == "http_routes_hostname_key" {
			reason = *hostname + " already serves another deployment"
		}
		return &RouteConflictError{Function: spec.Name, Reason: reason}
	}
	if err != nil {
		return fmt.Errorf("claim route: %w", err)
	}
	return nil
}
