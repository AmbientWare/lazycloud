package hostsession_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/hostsession"
)

type apiReply struct {
	status int
	body   []byte
}

// call makes one container API request as container, as the agent does.
func call(ctx context.Context, client hostproto.HostServiceClient, container execution.ContainerID, method, target string, body []byte, headers map[string]string) (apiReply, error) {
	stream, err := client.ContainerAPI(ctx)
	if err != nil {
		return apiReply{}, err
	}
	head := &hostproto.APIRequestHead{ContainerId: container.String(), Method: method, Target: target}
	for name, value := range headers {
		head.Headers = append(head.Headers, &hostproto.APIHeader{Name: name, Value: value})
	}
	if body != nil {
		head.Headers = append(head.Headers, &hostproto.APIHeader{Name: "Content-Type", Value: "application/json"})
	}
	msg := &hostproto.APIRequest{Head: head}
	for {
		n := min(len(body), 1<<20)
		msg.Body, body = body[:n], body[n:]
		if err := stream.Send(msg); err != nil {
			break
		}
		msg = &hostproto.APIRequest{}
		if len(body) == 0 {
			break
		}
	}
	_ = stream.CloseSend()
	var reply apiReply
	for {
		resp, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			return reply, nil
		}
		if err != nil {
			return reply, err
		}
		if resp.GetHead() != nil {
			reply.status = int(resp.GetHead().GetStatus())
		}
		reply.body = append(reply.body, resp.GetBody()...)
	}
}

func TestContainerAPIReachesOnlyTheContainersWorkspace(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	ws, container := h.startingContainer(host)
	if _, err := h.secrets.Set(t.Context(), ws, "TOKEN", "hunter2-hunter2"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.pool.Exec(t.Context(), "insert into workspaces (name) values ('other')"); err != nil {
		t.Fatal(err)
	}

	reply, err := call(ctx, h.client, container, "GET", "/v1/workspaces/ws/secrets/TOKEN/value", nil, nil)
	if err != nil || reply.status != 200 || !bytes.Contains(reply.body, []byte("hunter2-hunter2")) {
		t.Fatalf("own workspace: %d %s %v", reply.status, reply.body, err)
	}
	reply, err = call(ctx, h.client, container, "GET", "/v1/workspaces/other/secrets", nil, nil)
	if err != nil || reply.status != 403 {
		t.Fatalf("another workspace: %d %s %v", reply.status, reply.body, err)
	}
	// A bearer token in the request changes nothing: the container is the caller.
	reply, _ = call(ctx, h.client, container, "GET", "/v1/me", nil, map[string]string{"Authorization": "Bearer lc_nope"})
	if reply.status != 403 {
		t.Fatalf("/v1/me from a container: %d %s", reply.status, reply.body)
	}
	_, err = call(ctx, h.client, container, "GET", "/v1/workspaces/ws/secrets", nil, map[string]string{hostsession.TaskHeader: uuid.NewString()})
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("a task that does not run here: %v", err)
	}
	_, err = call(ctx, h.client, execution.ContainerID(uuid.New()), "GET", "/v1/workspaces/ws/secrets", nil, nil)
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("a container of no host: %v", err)
	}
	reply, err = call(ctx, h.client, container, "POST", "/v1/workspaces/ws/secrets", bytes.Repeat([]byte("x"), hostsession.MaxContainerAPIBody+1), nil)
	if err != nil || reply.status != 413 {
		t.Fatalf("an oversized body: %d %v", reply.status, err)
	}
}

func TestSpawnFromARunningTaskRecordsItAsParent(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	ws, container := h.startingContainer(host)
	if _, err := h.pool.Exec(t.Context(), "update containers set state = 'ready', ready_at = now()"); err != nil {
		t.Fatal(err)
	}
	tasks, err := h.execution.Submit(t.Context(), execution.SubmitRequest{
		Workspace: ws, App: "reports", Function: "summarize",
		Inputs: []execution.Payload{{Encoding: execution.EncodingJSON, Data: []byte(`{"args": []}`)}},
	})
	if err != nil {
		t.Fatal(err)
	}
	parent := tasks[0].ID
	claim, err := h.client.ClaimTasks(ctx, &hostproto.ClaimTasksRequest{ContainerId: container.String(), MaxTasks: 1})
	if err != nil || len(claim.GetTasks()) != 1 || claim.GetTasks()[0].GetRootTaskId() != parent.String() {
		t.Fatalf("claim %v, %v", claim, err)
	}

	body := []byte(`{"inputs": [{"encoding": "json", "value": {"args": [1]}}]}`)
	reply, err := call(ctx, h.client, container, "POST", "/v1/workspaces/ws/apps/reports/functions/summarize/tasks", body,
		map[string]string{hostsession.TaskHeader: parent.String()})
	if err != nil || reply.status != 201 {
		t.Fatalf("spawn: %d %s %v", reply.status, reply.body, err)
	}
	var spawned struct {
		Tasks []struct {
			ParentTaskID string `json:"parent_task_id"`
			RootTaskID   string `json:"root_task_id"`
		} `json:"tasks"`
	}
	if err := json.Unmarshal(reply.body, &spawned); err != nil {
		t.Fatal(err)
	}
	if got := spawned.Tasks[0]; got.ParentTaskID != parent.String() || got.RootTaskID != parent.String() {
		t.Fatalf("spawned task lineage %+v", got)
	}
}

func TestStartCarriesNamedSecretsAndFailsWithoutThem(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	ws, container := h.startingContainerWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.12"},
		"secrets": ["TOKEN"], "concurrency": 1, "max_pending_tasks": 10}`)
	if _, err := h.secrets.Set(t.Context(), ws, "TOKEN", "hunter2-hunter2"); err != nil {
		t.Fatal(err)
	}
	start := receive(t, open(t, ctx, h.client)).GetStart()
	if start.GetSecrets()["TOKEN"] != "hunter2-hunter2" || start.GetWorkspace() != "ws" || len(start.GetSecrets()) != 1 {
		t.Fatalf("start %v", start)
	}

	if err := h.secrets.Delete(t.Context(), ws, "TOKEN"); err != nil {
		t.Fatal(err)
	}
	if _, err := h.pool.Exec(t.Context(), "update containers set state = 'stopped', stopped_at = now() where id = $1", uuid.UUID(container)); err != nil {
		t.Fatal(err)
	}
	var release uuid.UUID
	if err := h.pool.QueryRow(t.Context(), "select release_id from containers where id = $1", uuid.UUID(container)).Scan(&release); err != nil {
		t.Fatal(err)
	}
	var second uuid.UUID
	if err := h.pool.QueryRow(t.Context(), `
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
values ($1, $2, 'starting', $3, 1, 1000, 1 << 28, now()) returning id`, uuid.UUID(ws), release, uuid.UUID(host)).Scan(&second); err != nil {
		t.Fatal(err)
	}
	open(t, ctx, h.client)
	deadline := time.Now().Add(5 * time.Second)
	var state, message string
	for time.Now().Before(deadline) {
		if err := h.pool.QueryRow(t.Context(), "select state, coalesce(exit_message, '') from containers where id = $1", second).Scan(&state, &message); err != nil {
			t.Fatal(err)
		}
		if state == "stopped" {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if state != "stopped" || message != "secret not found: TOKEN" {
		t.Fatalf("a start without its secret: %s %q", state, message)
	}
}
