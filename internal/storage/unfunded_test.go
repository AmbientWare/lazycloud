package storage_test

import (
	"testing"

	"github.com/google/uuid"
)

func TestUnfundedWorkspaceDataIsDeletedExceptWhatIsInUse(t *testing.T) {
	f := newFixture(t, `{}`)
	container := f.container()
	var idle, mounted, disk, held uuid.UUID
	f.exec(`insert into volumes (workspace_id, name) values ($1, 'idle') returning id`, &idle, uuid.UUID(f.ws))
	f.exec(`insert into volumes (workspace_id, name) values ($1, 'mounted') returning id`, &mounted, uuid.UUID(f.ws))
	if _, err := f.pool.Exec(t.Context(), `insert into volume_mounts (volume_id, container_id) values ($1, $2)`, mounted, container); err != nil {
		t.Fatal(err)
	}
	f.exec(`insert into disks (workspace_id, name, size_bytes) values ($1, 'free', 1073741824) returning id`, &disk, uuid.UUID(f.ws))
	f.exec(`insert into disks (workspace_id, name, size_bytes, holder_container_id, lease_token)
		values ($1, 'held', 1073741824, $2, 'lease') returning id`, &held, uuid.UUID(f.ws), container)

	deleted, err := f.storage.DeleteUnfunded(t.Context(), f.ws)
	if err != nil || deleted != 2 {
		t.Fatalf("deleted %d: %v", deleted, err)
	}
	states := map[uuid.UUID]string{}
	rows, err := f.pool.Query(t.Context(), `select id, state from volumes union all select id, state from disks`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	for rows.Next() {
		var id uuid.UUID
		var state string
		if err := rows.Scan(&id, &state); err != nil {
			t.Fatal(err)
		}
		states[id] = state
	}
	if states[idle] != "deleting" || states[disk] != "deleting" || states[mounted] != "active" || states[held] != "active" {
		t.Fatalf("states %v", states)
	}
	// Once the container stops, what it used goes too.
	f.stop(container, "stopped")
	if _, err := f.pool.Exec(t.Context(), `update disks set holder_container_id = null where id = $1`, held); err != nil {
		t.Fatal(err)
	}
	if deleted, err := f.storage.DeleteUnfunded(t.Context(), f.ws); err != nil || deleted != 2 {
		t.Fatalf("second pass deleted %d: %v", deleted, err)
	}
}
