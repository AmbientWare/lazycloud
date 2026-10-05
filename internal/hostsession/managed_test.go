package hostsession_test

import (
	"testing"
	"time"
)

// A start that needs the managed image waits while it builds, every sync
// joining the one build, and is sent with the image once it is published.
func TestStartWaitsForTheManagedImageBuild(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	if _, err := h.pool.Exec(t.Context(), `
with image as (
    insert into images (digest, id, dockerfile, python_version, architecture)
    values (sha256('managed 3.11'::bytea), 'img_' || left(encode(sha256('managed 3.11'::bytea), 'hex'), 24), 'FROM python', '3.11', 'amd64')
    returning digest
)
insert into managed_images (python_version, template, architecture, image_digest) select '3.11', $1, 'amd64', digest from image`,
		managedTemplate); err != nil {
		t.Fatal(err)
	}
	_, container := h.startingContainerWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.11"}}`)
	stream := open(t, ctx, h.client)

	got := make(chan string, 1)
	go func() {
		for {
			msg, err := stream.Recv()
			if err != nil {
				return
			}
			if start := msg.GetStart(); start.GetContainerId() == container.String() {
				got <- start.GetImage()
				return
			}
		}
	}()
	select {
	case image := <-got:
		t.Fatalf("the start was sent with %s before its image was published", image)
	case <-time.After(time.Second):
	}
	var builds, containers int
	if err := h.pool.QueryRow(t.Context(), `
select count(*), (select count(*) from containers where image_build_id is not null)
from image_builds where image_digest = sha256('managed 3.11'::bytea) and state = 'building'`).Scan(&builds, &containers); err != nil {
		t.Fatal(err)
	}
	if builds != 1 || containers != 1 {
		t.Fatalf("waiting syncs started %d builds and %d build containers, want 1 and 1", builds, containers)
	}
	// Syncs while the start waits read the build and write nothing.
	versions := func() string {
		var v string
		if err := h.pool.QueryRow(t.Context(), `
select string_agg(xmin::text, ',' order by digest) from images
union all select string_agg(xmin::text, ',' order by image_digest) from workspace_images`).Scan(&v); err != nil {
			t.Fatal(err)
		}
		return v
	}
	before := versions()
	time.Sleep(time.Second)
	if after := versions(); after != before {
		t.Fatalf("waiting syncs rewrote image rows: %s, then %s", before, after)
	}

	reference := managedReference("3.11")
	if _, err := h.pool.Exec(t.Context(), `
with layer as (
    insert into image_layers (id, blob_digest, diff_id, index_bytes, data_bytes, entries, frames)
    values (gen_random_uuid(), 'sha256:' || repeat('d', 64), 'sha256:' || repeat('d', 64), 1, 0, 0, 0)
    returning id
), refs as (
    insert into image_reference_layers (reference, position, layer_id) select $1, 0, id from layer
)
update images set reference = $1, ready_at = now() where digest = sha256('managed 3.11'::bytea)`, reference); err != nil {
		t.Fatal(err)
	}
	select {
	case image := <-got:
		if image != reference {
			t.Fatalf("the start pulls %s, want %s", image, reference)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the start was not sent after its image was published")
	}
}
