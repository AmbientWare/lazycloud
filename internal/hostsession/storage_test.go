package hostsession_test

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/hostsession"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// startingWith inserts a starting container of a release with spec.
func (h *harness) startingWith(host compute.HostID, spec string) (identity.WorkspaceID, uuid.UUID) {
	h.t.Helper()
	var ws, release, container uuid.UUID
	err := h.pool.QueryRow(h.t.Context(), `
with ws as (insert into workspaces (name) values ('ws-' || substr(md5(random()::text), 1, 8)) returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'reports', 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'summarize', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, $1::jsonb, sha256('spec'), sha256('src') from wl returning id)
select ws.id, rel.id from ws, rel`, spec).Scan(&ws, &release)
	if err != nil {
		h.t.Fatal(err)
	}
	err = h.pool.QueryRow(h.t.Context(), `
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
values ($1, $2, 'starting', $3, 1, 1000, 1 << 28, now()) returning id`, ws, release, uuid.UUID(host)).Scan(&container)
	if err != nil {
		h.t.Fatal(err)
	}
	return identity.WorkspaceID(ws), container
}

// TestStartSendsAWorkspaceGrantFirst covers volume delivery: the host gets
// a grant for the workspace bucket before the start that mounts from it,
// the start names the volume's prefix, and the recorded mount blocks
// deleting the volume.
func TestStartSendsAWorkspaceGrantFirst(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	ws, container := h.startingWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.12"},
		"resources": {"cpu_millis": 1000, "memory_mib": 256, "disk_mib": 2048},
		"volumes": [{"name": "data", "mount_path": "models", "read_only": true}]}`)
	stream := open(t, ctx, h.client)

	grant := receive(t, stream).GetStorageGrant()
	if grant.GetWorkspaceId() != ws.String() || grant.GetAccessKeyId() == "" || grant.GetBucket() == "" {
		t.Fatalf("first command %v, want the workspace's storage grant", grant)
	}
	start := receive(t, stream).GetStart()
	if start.GetContainerId() != container.String() || len(start.GetVolumes()) != 1 || start.GetResources().GetDiskLimitBytes() != 2048<<20 {
		t.Fatalf("start %v", start)
	}
	mount := start.GetVolumes()[0]
	volume := mount.GetVolume()
	if mount.GetMountPath() != "/volumes/models" || !mount.GetReadOnly() || volume.GetPrefix() != "volumes/"+volume.GetVolumeId()+"/" {
		t.Fatalf("volume mount %v", mount)
	}

	client := s3.New(s3.Options{
		Region: grant.GetRegion(), BaseEndpoint: aws.String(grant.GetEndpoint()), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider(grant.GetAccessKeyId(), grant.GetSecretAccessKey(), grant.GetSessionToken()),
	})
	if _, err := client.PutObject(t.Context(), &s3.PutObjectInput{
		Bucket: aws.String(grant.GetBucket()), Key: aws.String(volume.GetPrefix() + "x"), Body: strings.NewReader("x"),
	}); err != nil {
		t.Fatalf("write with the grant: %v", err)
	}

	store := storage.NewStorage(h.pool, storagetest.Config())
	var inUse *storage.ConflictError
	if err := store.DeleteVolume(t.Context(), ws, "data"); !errors.As(err, &inUse) {
		t.Fatalf("delete of a mounted volume: %v", err)
	}
}

// TestCloudBucketKeysComeFromWorkspaceSecrets: a cloud bucket's start
// carries the keys its secrets hold, and a missing secret fails the start.
func TestCloudBucketKeysComeFromWorkspaceSecrets(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	spec := `{"handler": "reports:summarize", "image": {"python_version": "3.12"},
		"volumes": [{"name": "models", "mount_path": "/models", "read_only": true,
		  "cloud_bucket": {"bucket": "user-models", "prefix": "v1/", "region": "us-east-2",
		    "access_key_secret": "AWS_KEY", "secret_key_secret": "AWS_SECRET"}}]}`
	ws, container := h.startingWith(host, spec)
	if _, err := h.secrets.Create(t.Context(), ws, "AWS_KEY", "AKIAEXAMPLE"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.secrets.Create(t.Context(), ws, "AWS_SECRET", "secret-value"); err != nil {
		t.Fatal(err)
	}
	stream := open(t, ctx, h.client)
	start := receive(t, stream).GetStart()
	bucket := start.GetVolumes()[0].GetCloudBucket()
	if start.GetContainerId() != container.String() || bucket.GetBucket() != "user-models" || bucket.GetPrefix() != "v1/" ||
		bucket.GetAccessKeyId() != "AKIAEXAMPLE" || bucket.GetSecretAccessKey() != "secret-value" || !start.GetVolumes()[0].GetReadOnly() {
		t.Fatalf("start %v", start)
	}
	if _, err := h.pool.Exec(t.Context(), `update containers set state = 'stopped', stop_reason = 'stopped' where id = $1`, container); err != nil {
		t.Fatal(err)
	}

	// Without its secret the bucket's container fails to start.
	_, missing := h.startingWith(host, strings.ReplaceAll(spec, "AWS_SECRET", "ABSENT"))
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		var state, reason string
		if err := h.pool.QueryRow(t.Context(), `select state, coalesce(stop_reason, '') from containers where id = $1`, missing).Scan(&state, &reason); err != nil {
			t.Fatal(err)
		}
		if state == "stopped" {
			if reason != "start_failed" {
				t.Fatalf("container without its bucket secret stopped as %s", reason)
			}
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("a container without its bucket secret was not failed")
}

// TestContainerStorageCallsActForTheirTask: queues, maps and artifacts work
// through the container API, and an artifact saved there belongs to the
// task running on the container, whatever the body names.
func TestContainerStorageCallsActForTheirTask(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	ws, container := h.startingContainer(host)
	if _, err := h.pool.Exec(t.Context(), "update containers set state = 'ready', ready_at = now()"); err != nil {
		t.Fatal(err)
	}
	tasks, err := h.execution.Submit(t.Context(), execution.SubmitRequest{
		Workspace: ws, App: "reports", Function: "summarize",
		Inputs: []execution.TaskInput{{Payload: execution.Payload{Encoding: execution.EncodingJSON, Data: []byte(`{"args": []}`)}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	running := tasks[0].ID.String()
	if claim, err := h.client.ClaimTasks(ctx, &hostproto.ClaimTasksRequest{ContainerId: container.String(), MaxTasks: 1}); err != nil || len(claim.GetTasks()) != 1 {
		t.Fatalf("claim %v, %v", claim, err)
	}
	asTask := map[string]string{hostsession.TaskHeader: running}

	reply, err := call(ctx, h.client, container, "POST", "/v1/workspaces/ws/queues/jobs/messages", []byte(`{"messages": ["IjEi"]}`), asTask)
	if err != nil || reply.status != 204 {
		t.Fatalf("queue put: %d %s %v", reply.status, reply.body, err)
	}
	reply, err = call(ctx, h.client, container, "POST", "/v1/workspaces/ws/queues/jobs/pop", nil, nil)
	if err != nil || reply.status != 200 || !strings.Contains(string(reply.body), "IjEi") {
		t.Fatalf("queue pop: %d %s %v", reply.status, reply.body, err)
	}
	reply, err = call(ctx, h.client, container, "PUT", "/v1/workspaces/ws/maps/cache/entries/k", []byte(`{"value": "MQ=="}`), nil)
	if err != nil || reply.status != 200 {
		t.Fatalf("map set: %d %s %v", reply.status, reply.body, err)
	}

	artifact := func(task string, headers map[string]string) int {
		body := []byte(`{"task_id": "` + task + `", "filename": "out.txt", "size_bytes": 1}`)
		reply, err := call(ctx, h.client, container, "POST", "/v1/workspaces/ws/artifacts", body, headers)
		if err != nil {
			t.Fatal(err)
		}
		return reply.status
	}
	if status := artifact(running, asTask); status != 201 {
		t.Fatalf("artifact for the running task: %d", status)
	}
	if status := artifact(uuid.NewString(), asTask); status != 403 {
		t.Fatalf("artifact naming another task: %d", status)
	}
	if status := artifact(running, nil); status != 403 {
		t.Fatalf("artifact without the calling task: %d", status)
	}
}

func TestDiskLeaseOverTheHostConnection(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	_, container := h.startingWith(host, `{"handler": "a:b", "image": {"python_version": "3.12"},
		"disks": [{"name": "root", "size_bytes": 1073741824, "mount_path": "/data"}]}`)

	lease, err := h.client.AcquireDisk(ctx, &hostproto.AcquireDiskRequest{ContainerId: container.String(), Name: "root"})
	if err != nil || lease.GetSizeBytes() != 1<<30 || len(lease.GetLeaseToken()) != 32 {
		t.Fatalf("acquire: %v err=%v", lease, err)
	}
	if _, err := h.client.AcquireDisk(ctx, &hostproto.AcquireDiskRequest{ContainerId: container.String(), Name: "other"}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("undeclared disk: %v", err)
	}
	record := &hostproto.RecordDiskGenerationRequest{
		ContainerId: container.String(), DiskId: lease.GetDiskId(), LeaseToken: lease.GetLeaseToken(),
		Generation: 1, ManifestKey: "disks/" + lease.GetDiskId() + "/manifests/000000000001-" + strings.Repeat("a", 64) + ".json",
		ManifestSha256: strings.Repeat("a", 64),
	}
	if _, err := h.client.RecordDiskGeneration(ctx, record); err != nil {
		t.Fatal(err)
	}
	record.Generation, record.ParentGeneration = 3, 1
	if _, err := h.client.RecordDiskGeneration(ctx, record); status.Code(err) != codes.Aborted {
		t.Fatalf("out-of-order generation: %v", err)
	}
	if _, err := h.client.ReleaseDisk(ctx, &hostproto.ReleaseDiskRequest{ContainerId: container.String(), DiskId: lease.GetDiskId(), LeaseToken: []byte("x")}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("release with a forged token: %v", err)
	}
	if _, err := h.client.ReleaseDisk(ctx, &hostproto.ReleaseDiskRequest{ContainerId: container.String(), DiskId: lease.GetDiskId(), LeaseToken: lease.GetLeaseToken()}); err != nil {
		t.Fatal(err)
	}
}
