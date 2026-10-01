package control

import (
	"errors"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
)

// The reference minted these labels (shared/deployment_subdomains.py) for
// the same identities.
func TestSubdomainMatchesReference(t *testing.T) {
	ws := uuid.MustParse("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
	cases := []struct {
		app, name string
		kind      apitypes.WorkloadKind
		want      string
	}{
		{"api_demo", "count_words", apitypes.WorkloadKindEndpoint, "count-words-ab830fac"},
		{"web_app", "service", apitypes.WorkloadKindAsgi, "service-a1890712"},
		{"reports", "summarize", apitypes.WorkloadKindFunction, "summarize-b75627f9"},
		{"x", "__", apitypes.WorkloadKindFunction, "fde6fa56"},
		{"x", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", apitypes.WorkloadKindEndpoint, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-cbd797ef"},
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
		*h.Route != "/" || len(*h.Methods) != 2 || !*spec.Authorized || *h.Workers != 1 {
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
	want := []string{Subdomain(uuid.UUID(ws), "reports", "count", apitypes.WorkloadKindEndpoint), Subdomain(uuid.UUID(ws), "reports", "count", apitypes.WorkloadKindFunction)}
	if len(kinds) != 2 || kinds[0] != "endpoint" || kinds[1] != "function" || subdomains[0] != want[0] || subdomains[1] != want[1] {
		t.Fatalf("routes %v %v; want endpoint and function with %v", kinds, subdomains, want)
	}

	// Pruning keeps listed workloads by kind and name.
	pruned := deploy(t, c, ws, true, endpoint("count", apitypes.HttpSpec{Kind: apitypes.HttpKindEndpoint}))
	if len(pruned.Pruned) != 1 || pruned.Pruned[0] != "count" {
		t.Fatalf("pruned %v; want the function count", pruned.Pruned)
	}
	var deleted string
	if err := pool.QueryRow(t.Context(), `select kind from workloads where desired_state = 'deleted'`).Scan(&deleted); err != nil || deleted != "function" {
		t.Fatalf("deleted workload kind %q (%v); want function", deleted, err)
	}

	// The deleted function gave up its subdomain, so deploying it again
	// claims the same one.
	deploy(t, c, ws, false, function("count"))
	var claimed string
	if err := pool.QueryRow(t.Context(), `select r.subdomain from http_routes r join workloads w on w.id = r.workload_id
		where w.kind = 'function' and w.desired_state = 'active'`).Scan(&claimed); err != nil || claimed != want[1] {
		t.Fatalf("redeployed function subdomain %q (%v); want %s", claimed, err, want[1])
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
insert into custom_domains (user_id, hostname, phase)
select user_id, $2, 'awaiting_verification' from workspace_members where workspace_id = $1 and role = 'owner'`, uuid.UUID(ws), domain); err != nil {
		t.Fatal(err)
	}
	// Serving a custom domain needs the owner's plan to include them.
	if _, err := pool.Exec(t.Context(), `update billing_accounts set complimentary_since = null`); err != nil {
		t.Fatal(err)
	}
	_, err = c.Deploy(t.Context(), ws, "reports", apitypes.DeploymentRequest{Functions: []apitypes.FunctionSpec{spec}})
	var payment *billing.PaymentRequiredError
	if !errors.As(err, &payment) {
		t.Fatalf("deploy a custom domain on the Free plan: %v; want PaymentRequiredError", err)
	}
	if _, err := pool.Exec(t.Context(), `
update billing_accounts set terms_version = 'team-v3'
where user_id = (select user_id from workspace_members where workspace_id = $1 and role = 'owner')`, uuid.UUID(ws)); err != nil {
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
