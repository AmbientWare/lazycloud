package acceptance

import (
	"strings"
	"testing"

	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/jackc/pgx/v5/pgxpool"
)

// managedTemplate is the platform's image template in these tests.
const managedTemplate = "docker.io/library/python:{version}-slim"

// publishManagedImage records the template for pythonVersion as a published
// managed image, as its build would, so containers without an image of
// their own start without a registry to build into. The managed build
// itself is the images owner's test; here the host pulls the template by
// digest.
func publishManagedImage(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	tag := strings.ReplaceAll(managedTemplate, "{version}", pythonVersion)
	ref, err := name.ParseReference(tag)
	if err != nil {
		t.Fatal(err)
	}
	desc, err := remote.Head(ref, remote.WithContext(t.Context()))
	if err != nil {
		t.Skipf("resolve %s: %v", tag, err)
	}
	reference := ref.Context().Name() + "@" + desc.Digest.String()
	if _, err := pool.Exec(t.Context(), `
with image as (
    insert into images (digest, id, dockerfile, python_version, architecture, reference, ready_at)
    values (sha256($1::bytea), 'img_' || left(encode(sha256($1::bytea), 'hex'), 24), 'FROM ' || $1, $2, 'amd64', $1, now())
    returning digest
), managed as (
    insert into managed_images (python_version, template, architecture, image_digest)
    select $2, $3, 'amd64', digest from image
), layer as (
    insert into image_layers (id, blob_digest, diff_id, index_bytes, data_bytes, entries, frames)
    values (gen_random_uuid(), 'sha256:' || encode(sha256($1::bytea), 'hex'), 'sha256:' || encode(sha256($1::bytea), 'hex'), 1, 0, 0, 0)
    returning id
)
insert into image_reference_layers (reference, position, layer_id) select $1, 0, id from layer`,
		reference, pythonVersion, managedTemplate); err != nil {
		t.Fatal(err)
	}
}
