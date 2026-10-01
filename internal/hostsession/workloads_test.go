package hostsession_test

import (
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// A function container may drive only the instances it started, and never
// workload control; a container of any other kind or purpose reaches nothing.
func TestContainersCannotControlWorkloadsTheyDidNotStart(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	_, container := h.startingContainer(host)
	var other uuid.UUID
	if err := h.pool.QueryRow(t.Context(), `insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, purpose)
select workspace_id, release_id, 'ready', host_id, 1, 1000, 1 << 28, now(), 'instance' from containers where id = $1 returning id`,
		uuid.UUID(container)).Scan(&other); err != nil {
		t.Fatal(err)
	}
	refused := []struct{ method, target string }{
		{"PUT", "/v1/workspaces/ws/containers/" + other.String() + "/network"},
		{"PUT", "/v1/workspaces/ws/containers/" + other.String() + "/ttl"},
		{"POST", "/v1/workspaces/ws/containers/" + other.String() + "/snapshots"},
		{"POST", "/v1/workspaces/ws/containers/" + other.String() + "/filesystem-images"},
		{"GET", "/v1/workspaces/ws/containers/" + other.String() + "/processes"},
		{"POST", "/v1/workspaces/ws/containers/" + other.String() + "/stop"},
		{"POST", "/v1/workspaces/ws/ssh/certificates"},
		{"GET", "/v1/workspaces/ws/ssh/hosts"},
	}
	bodies := map[string][]byte{
		"network": []byte(`{"block_network": false, "allow_list": []}`), "ttl": []byte(`{"ttl": 60}`),
		"certificates": []byte(`{"public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC8acQNNRzh90eTcyMlzscI+Ptp/zWkhxkBBzlbQQ3ES"}`),
	}
	for _, r := range refused {
		var body []byte
		for suffix, b := range bodies {
			if strings.HasSuffix(r.target, suffix) {
				body = b
			}
		}
		reply, err := call(ctx, h.client, container, r.method, r.target, body, nil)
		if err != nil || reply.status != 403 {
			t.Errorf("%s %s from a function container: %d %s %v", r.method, r.target, reply.status, reply.body, err)
		}
	}
	// The instance itself reaches nothing, not even its workspace's secrets.
	reply, err := call(ctx, h.client, execution.ContainerID(other), "GET", "/v1/workspaces/ws/secrets", nil, nil)
	if err != nil || reply.status != 403 {
		t.Fatalf("secrets from an instance: %d %s %v", reply.status, reply.body, err)
	}
}

// A host may only report an image by digest in the workspace's filesystem
// repository.
func TestFilesystemImagesMustLandInTheWorkspaceRepository(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	ws, container := h.startingContainer(host)
	if _, err := h.pool.Exec(t.Context(), "update containers set state = 'ready', ready_at = now()"); err != nil {
		t.Fatal(err)
	}
	digest := strings.Repeat("a", 64)
	for _, c := range []struct {
		reference string
		published bool
	}{
		{"docker.io/library/busybox@sha256:" + digest, false},
		{"127.0.0.1:1/lazycloud/filesystems/" + uuid.UUID(ws).String() + "@sha256:" + digest, true},
	} {
		request, err := h.execution.CreateFilesystemImage(t.Context(), ws, container)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := h.client.CompleteFilesystemImage(ctx, &hostproto.CompleteFilesystemImageRequest{
			ContainerId: container.String(), RequestId: request.String(), Reference: c.reference, Architecture: "amd64",
		}); err != nil {
			t.Fatal(err)
		}
		var state string
		if err := h.pool.QueryRow(t.Context(), "select state from filesystem_images where id = $1", request).Scan(&state); err != nil {
			t.Fatal(err)
		}
		if (state == "published") != c.published {
			t.Fatalf("%s: state %s", c.reference, state)
		}
	}
}

// A pod is ready once its first port answers; a sandbox's ports are for
// what runs in it later, so its start names none to wait for.
func TestSandboxesDoNotWaitForTheirPorts(t *testing.T) {
	for _, c := range []struct {
		kind  string
		ports int
	}{{"sandbox", 0}, {"pod", 1}} {
		t.Run(c.kind, func(t *testing.T) {
			h := start(t)
			host, ctx := h.enroll()
			_, container := h.startingContainerWith(host, `{"name": "box", "image": {"python_version": "3.12"},
			  "pod": {"kind": "`+c.kind+`", "command": ["sleep", "infinity"], "ports": {"http": 8000}}}`)
			if _, err := h.pool.Exec(t.Context(), "update workloads set kind = $1", c.kind); err != nil {
				t.Fatal(err)
			}
			pod := receive(t, open(t, ctx, h.client)).GetStart().GetPod()
			if pod == nil || len(pod.GetPorts()) != c.ports {
				t.Fatalf("%s start of %s waits for ports %v", c.kind, container, pod.GetPorts())
			}
		})
	}
}
