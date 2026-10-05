package compute_test

import (
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// A connected workspace's mirror build is demand on the platform, where
// placement runs it; its other builds are demand on the connection.
func TestMirrorBuildDemandIsThePlatforms(t *testing.T) {
	pool := dbtest.New(t)
	owner := newUser(t, pool, "owner@example.com")
	ws := newWorkspace(t, pool, "connected", owner)
	connection := scan[uuid.UUID](t, pool, `
with conn as (insert into cloud_connections (account_id, aws_account_id, phase) values ($1, '123456789012', 'ready') returning id)
update workspaces set connection_id = (select id from conn) where id = $2 returning connection_id`, uuid.UUID(owner), ws)
	build := func(mirror bool, seed string) {
		run(t, pool, `
with image as (
    insert into images (digest, id, dockerfile, python_version, architecture)
    values (sha256($1::bytea), 'img_' || left(encode(sha256($1::bytea), 'hex'), 24), 'FROM x', '3.12', 'amd64') returning digest
), build as (
    insert into image_builds (image_digest, state, workspace_id, mirror, deadline_at)
    select digest, 'building', $2, $3, now() + interval '1 hour' from image returning id
)
insert into containers (workspace_id, image_build_id, state, slots, cpu_millis, memory_bytes)
select $2, id, 'pending', 1, $4, 1 << 30 from build`, []byte(seed), ws, mirror, map[bool]int64{true: 500, false: 700}[mirror])
	}
	build(true, "mirror")
	build(false, "own")

	rows, err := compute.New(pool).PendingDemand(t.Context(), 10)
	if err != nil {
		t.Fatal(err)
	}
	targets := map[int64]*uuid.UUID{}
	for _, row := range rows {
		targets[row.CpuMillis] = row.ConnectionID
	}
	if len(targets) != 2 || targets[500] != nil || targets[700] == nil || *targets[700] != connection {
		t.Fatalf("mirror demand on %v, own build on %v; want the platform and the connection", targets[500], targets[700])
	}
}
