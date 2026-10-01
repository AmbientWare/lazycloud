package api_test

import (
	"strings"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// A release pins the image by digest the workspace could deploy when it was
// created; a caller cannot send a reference, and a later publish of the image
// does not change the release.
func TestDeployPinsTheImageReference(t *testing.T) {
	e := newEnv(t)
	ctx := t.Context()
	first := "registry.example.com/lazycloud/images@sha256:" + strings.Repeat("1", 64)
	if _, err := e.pool.Exec(ctx, `
with ws as (select id from workspaces where name = 'acme'),
     src as (insert into source_objects (workspace_id, sha256, size_bytes) select id, sha256('src'), 1 from ws),
     img as (insert into images (digest, id, dockerfile, python_version, architecture, reference, ready_at)
             values (sha256('image'), 'img_000000000000000000000001', 'FROM x', '3.12', 'amd64', $1, now()) returning digest)
insert into workspace_images (workspace_id, image_digest) select ws.id, img.digest from ws, img`, first); err != nil {
		t.Fatal(err)
	}
	var source string
	if err := e.pool.QueryRow(ctx, "select encode(sha256('src'), 'hex')").Scan(&source); err != nil {
		t.Fatal(err)
	}
	imageID := "img_000000000000000000000001"
	forged := "docker.io/attacker/image@sha256:" + strings.Repeat("f", 64)
	spec := apitypes.FunctionSpec{
		Name: "summarize_sales", Handler: new("reports:summarize_sales"), Source: apitypes.SourceRef{Sha256: source},
		Image:     apitypes.ImageSpec{PythonVersion: apitypes.N312, ImageId: &imageID, Reference: &forged},
		Resources: apitypes.Resources{CpuMillis: 1000, MemoryMib: 512},
	}
	var apiErr apitypes.Error
	if status := e.do("POST", "/v1/workspaces/acme/apps/reports/deployments", e.owner,
		apitypes.DeploymentRequest{Functions: []apitypes.FunctionSpec{spec}}, &apiErr); status != 400 {
		t.Fatalf("a caller cannot choose the reference: %d %+v", status, apiErr)
	}
	spec.Image.Reference = nil
	deploy := func() apitypes.Release {
		t.Helper()
		var d apitypes.Deployment
		if status := e.do("POST", "/v1/workspaces/acme/apps/reports/deployments", e.owner,
			apitypes.DeploymentRequest{Functions: []apitypes.FunctionSpec{spec}}, &d); status != 200 || len(d.Releases) != 1 {
			t.Fatalf("deploy: %d %+v", status, d)
		}
		return d.Releases[0]
	}
	v1 := deploy()
	if r := v1.Spec.Image.Reference; r == nil || *r != first {
		t.Fatalf("the release pins the workspace's image, got %v", r)
	}
	second := "registry.example.com/lazycloud/images@sha256:" + strings.Repeat("2", 64)
	if _, err := e.pool.Exec(ctx, "update images set reference = $1", second); err != nil {
		t.Fatal(err)
	}
	var stored string
	if err := e.pool.QueryRow(ctx, "select spec -> 'image' ->> 'reference' from releases where id = $1", v1.Id).Scan(&stored); err != nil || stored != first {
		t.Fatalf("a published rebuild leaves the release alone: %q %v", stored, err)
	}
	if v2 := deploy(); v2.Version == nil || *v2.Version != 2 || *v2.Spec.Image.Reference != second {
		t.Fatalf("redeploying picks up the new image as a new release: %+v", v2)
	}
}
