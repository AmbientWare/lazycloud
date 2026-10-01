package control

import (
	"errors"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// The reference minted these labels (shared/deployment_subdomains.py) for
// the same identities.
func TestSubdomainMatchesReference(t *testing.T) {
	ws := uuid.MustParse("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
	cases := []struct {
		app, name string
		kind      WorkloadKind
		want      string
	}{
		{"api_demo", "count_words", KindEndpoint, "count-words-ab830fac"},
		{"web_app", "service", KindASGI, "service-a1890712"},
		{"reports", "summarize", KindFunction, "summarize-b75627f9"},
		{"x", "__", KindFunction, "fde6fa56"},
		{"x", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", KindEndpoint, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-cbd797ef"},
	}
	for _, c := range cases {
		if got := Subdomain(ws, c.app, c.name, c.kind); got != c.want {
			t.Errorf("Subdomain(%s, %s, %s) = %s; want %s", c.app, c.name, c.kind, got, c.want)
		}
	}
}

func endpoint(name string, http apitypes.HttpSpec) apitypes.FunctionSpec {
	spec := function(name)
	spec.Http = &http
	return spec
}

func TestDeployEndpointResolvesHTTPDefaultsAndClaimsItsSubdomain(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)

	d := deploy(t, c, ws, false, endpoint("count", apitypes.HttpSpec{Kind: apitypes.HttpKindEndpoint}), function("count"))
	spec := d.Releases[0].Spec
	h := spec.Http
	if *spec.TimeoutSeconds != 180 || *spec.KeepWarmSeconds != 180 || spec.RetryPolicy.MaxAttempts != 1 ||
		*h.Route != "/" || len(*h.Methods) != 2 || !*h.Authorized || *h.Workers != 1 {
		t.Fatalf("endpoint defaults not resolved: %+v http %+v", spec, h)
	}

	// The endpoint and the function share a name but not a kind, so each is
	// its own workload with its own subdomain.
	var kinds []string
	var subdomains []string
	rows, err := pool.Query(t.Context(), `select w.kind, r.subdomain from workloads w join http_routes r on r.workload_id = w.id order by w.kind`)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var kind, sub string
		if err := rows.Scan(&kind, &sub); err != nil {
			t.Fatal(err)
		}
		kinds, subdomains = append(kinds, kind), append(subdomains, sub)
	}
	if rows.Err() != nil {
		t.Fatal(rows.Err())
	}
	want := []string{Subdomain(uuid.UUID(ws), "reports", "count", KindEndpoint), Subdomain(uuid.UUID(ws), "reports", "count", KindFunction)}
	if len(kinds) != 2 || kinds[0] != "endpoint" || kinds[1] != "function" || subdomains[0] != want[0] || subdomains[1] != want[1] {
		t.Fatalf("routes %v %v; want endpoint and function with %v", kinds, subdomains, want)
	}

	// Pruning keeps listed workloads by kind and name.
	pruned := deploy(t, c, ws, true, endpoint("count", apitypes.HttpSpec{Kind: apitypes.HttpKindEndpoint}))
	if len(pruned.Pruned) != 1 || pruned.Pruned[0] != "count" {
		t.Fatalf("pruned %v; want the function count", pruned.Pruned)
	}
	var stopped string
	if err := pool.QueryRow(t.Context(), `select kind from workloads where desired_state = 'stopped'`).Scan(&stopped); err != nil || stopped != "function" {
		t.Fatalf("stopped workload kind %q (%v); want function", stopped, err)
	}
}

func TestDeployRejectsRoutesAndMethodsOnASGI(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	route := "/api"
	_, err := c.Deploy(t.Context(), ws, "web", apitypes.DeploymentRequest{Functions: []apitypes.FunctionSpec{
		endpoint("service", apitypes.HttpSpec{Kind: apitypes.HttpKindAsgi, Route: &route}),
	}})
	var invalid *InvalidSpecError
	if !errors.As(err, &invalid) {
		t.Fatalf("deploy ASGI with a route: %v; want InvalidSpecError", err)
	}
}

func TestDeployCustomDomainNeedsAnOwnerRegistrationAndOneDeployment(t *testing.T) {
	pool, ws := fixture(t)
	c := NewControl(pool)
	domain := "api.acme.com"
	spec := endpoint("count", apitypes.HttpSpec{Kind: apitypes.HttpKindEndpoint, Domain: &domain})

	_, err := c.Deploy(t.Context(), ws, "reports", apitypes.DeploymentRequest{Functions: []apitypes.FunctionSpec{spec}})
	var invalid *InvalidSpecError
	if !errors.As(err, &invalid) {
		t.Fatalf("deploy with an unregistered domain: %v; want InvalidSpecError", err)
	}

	if _, err := pool.Exec(t.Context(), `
with u as (insert into users (email) values ('owner@acme.com') returning id),
     m as (insert into workspace_members (workspace_id, user_id, role) select $1, id, 'owner' from u)
insert into custom_domains (user_id, hostname, phase) select id, $2, 'awaiting_verification' from u`, uuid.UUID(ws), domain); err != nil {
		t.Fatal(err)
	}
	deploy(t, c, ws, false, spec)

	// Another app of the workspace cannot claim the same hostname.
	_, err = c.Deploy(t.Context(), ws, "other", apitypes.DeploymentRequest{Functions: []apitypes.FunctionSpec{spec}})
	var conflict *RouteConflictError
	if !errors.As(err, &conflict) {
		t.Fatalf("second deployment of %s: %v; want RouteConflictError", domain, err)
	}
}
