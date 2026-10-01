package hostsession_test

import (
	"context"
	"crypto/rand"
	"log/slog"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/api"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/hostsession"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/schedules"
	"github.com/AmbientWare/lazycloud/internal/secrets"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

type harness struct {
	t         *testing.T
	pool      *pgxpool.Pool
	client    hostproto.HostServiceClient
	compute   *compute.Compute
	execution *execution.Execution
	secrets   *secrets.Secrets
}

// start serves the host service on a random local port against real
// PostgreSQL.
func start(t *testing.T) *harness {
	t.Helper()
	pool := dbtest.New(t)
	logger := slog.New(slog.DiscardHandler)
	listener := database.NewListener(pool, logger, database.ChannelHost, database.ChannelClaim)
	c := compute.NewCompute(pool)
	e := execution.NewExecution(pool)
	store := storage.NewStorage(pool, storagetest.Config())
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		t.Fatal(err)
	}
	key, err := secrets.NewFileKey(raw)
	if err != nil {
		t.Fatal(err)
	}
	vault := secrets.NewSecrets(pool, key)
	containerAPI, err := api.NewContainerHandler(api.Owners{
		Identity: identity.NewIdentity(pool, identity.Config{}), Control: control.NewControl(pool), Storage: store, Execution: e,
		Secrets: vault, Schedules: schedules.NewSchedules(pool, e), Listener: listener,
	}, api.Config{}, logger)
	if err != nil {
		t.Fatal(err)
	}
	im := images.NewImages(pool, e, images.Config{Registry: "127.0.0.1:1", Repository: "lazycloud"})
	srv := hostsession.NewServer(c, e, store, im, listener, hostsession.Config{
		ImageTemplate: "docker.io/library/python:{version}-slim",
		TouchInterval: 100 * time.Millisecond,
		Secrets:       vault,
		ContainerAPI:  containerAPI,
	}, logger)
	g := grpc.NewServer(srv.ServerOptions()...)
	hostproto.RegisterHostServiceServer(g, srv)
	lis, err := (&net.ListenConfig{}).Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	runCtx, stop := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Go(func() { _ = listener.Run(runCtx) })
	wg.Go(func() { _ = g.Serve(lis) })
	conn, err := grpc.NewClient(lis.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = conn.Close()
		srv.Shutdown()
		g.GracefulStop()
		srv.Wait()
		stop()
		wg.Wait()
	})
	return &harness{t: t, pool: pool, client: hostproto.NewHostServiceClient(conn), compute: c, execution: e, secrets: vault}
}

func (h *harness) enroll() (compute.HostID, context.Context) {
	h.t.Helper()
	join, _, err := h.compute.CreateJoinToken(h.t.Context(), time.Minute)
	if err != nil {
		h.t.Fatal(err)
	}
	resp, err := h.client.Enroll(h.t.Context(), &hostproto.EnrollRequest{
		JoinToken: join, Hostname: "host-a", Capacity: &hostproto.Capacity{CpuMillis: 4000, MemoryBytes: 8 << 30},
	})
	if err != nil {
		h.t.Fatal(err)
	}
	ctx := metadata.AppendToOutgoingContext(h.t.Context(), "authorization", "Bearer "+resp.GetHostToken())
	return compute.HostID(uuid.MustParse(resp.GetHostId())), ctx
}

// startingContainer inserts a deployed function and a container of it
// assigned to host in the starting state.
func (h *harness) startingContainer(host compute.HostID) (identity.WorkspaceID, execution.ContainerID) {
	h.t.Helper()
	return h.startingContainerWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.12"}, "environment": {"MODE": "test"},
		  "concurrency": 2, "timeout_seconds": 60, "max_pending_tasks": 10}`)
}

// startingContainerWith does the same with the release spec JSON.
func (h *harness) startingContainerWith(host compute.HostID, spec string) (identity.WorkspaceID, execution.ContainerID) {
	h.t.Helper()
	ctx := h.t.Context()
	var ws, release, container uuid.UUID
	err := h.pool.QueryRow(ctx, `
with ws as (insert into workspaces (name) values ('ws') returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'reports', 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'summarize', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, $1::jsonb, sha256('spec'), sha256('src') from wl returning id)
select ws.id, rel.id from ws, rel`, spec).Scan(&ws, &release)
	if err != nil {
		h.t.Fatal(err)
	}
	dbtest.OwnWorkspaces(h.t, h.pool)
	if _, err := h.pool.Exec(ctx, "update workloads set active_release_id = $1, next_version = 2", release); err != nil {
		h.t.Fatal(err)
	}
	err = h.pool.QueryRow(ctx, `
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
values ($1, $2, 'starting', $3, 2, 1000, 1 << 28, now()) returning id`, ws, release, uuid.UUID(host)).Scan(&container)
	if err != nil {
		h.t.Fatal(err)
	}
	return identity.WorkspaceID(ws), execution.ContainerID(container)
}

type hostStream = grpc.BidiStreamingClient[hostproto.HostMessage, hostproto.ServerMessage]

func open(t *testing.T, ctx context.Context, client hostproto.HostServiceClient, containers ...*hostproto.ContainerReport) hostStream {
	t.Helper()
	stream, err := client.Session(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(&hostproto.HostMessage{Body: &hostproto.HostMessage_Hello{Hello: &hostproto.Hello{
		BootId: "boot-1", Capacity: &hostproto.Capacity{CpuMillis: 4000, MemoryBytes: 8 << 30}, Containers: containers,
	}}}); err != nil {
		t.Fatal(err)
	}
	return stream
}

// receive returns the next command, failing after a few seconds.
func receive(t *testing.T, stream hostStream) *hostproto.ServerMessage {
	t.Helper()
	got := make(chan *hostproto.ServerMessage, 1)
	failed := make(chan error, 1)
	go func() {
		msg, err := stream.Recv()
		if err != nil {
			failed <- err
			return
		}
		got <- msg
	}()
	select {
	case msg := <-got:
		return msg
	case err := <-failed:
		t.Fatalf("receive: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("no command within 5s")
	}
	return nil
}

func TestSessionReconcilesAndResendsAfterReconnect(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	_, container := h.startingContainer(host)
	unknown := uuid.NewString()

	sessionCtx, closeSession := context.WithCancel(ctx)
	stream := open(t, sessionCtx, h.client, &hostproto.ContainerReport{
		ContainerId: unknown, Phase: hostproto.ContainerPhase_CONTAINER_PHASE_READY, ObservedAt: timestamppb.Now(),
	})
	stop := receive(t, stream)
	if stop.GetStop().GetContainerId() != unknown {
		t.Fatalf("first command %v, want a stop for the unknown container", stop)
	}
	first := receive(t, stream).GetStart()
	if first.GetContainerId() != container.String() ||
		first.GetImage() != "docker.io/library/python:3.12-slim" ||
		first.GetFunction().GetSlots() != 2 || first.GetFunction().GetHandler() != "reports:summarize" ||
		first.GetEnvironment()["MODE"] != "test" ||
		!strings.Contains(first.GetSource().GetUrl(), "/sources/"+first.GetSource().GetSha256()+".zip") {
		t.Fatalf("start command %v", first)
	}
	closeSession()

	// A new session sends the start again because the container is still
	// starting.
	stream = open(t, ctx, h.client)
	if again := receive(t, stream).GetStart(); again.GetContainerId() != container.String() {
		t.Fatalf("after reconnect got %v, want the start again", again)
	}

	// A newer session supersedes this one at its next touch.
	newer := open(t, ctx, h.client)
	receive(t, newer)
	_, err := stream.Recv()
	for err == nil {
		_, err = stream.Recv()
	}
	if status.Code(err) != codes.Aborted {
		t.Fatalf("superseded session ended with %v, want Aborted", err)
	}
}

func TestReadyContainerClaimsCompletesAndReceivesCancels(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	ws, container := h.startingContainer(host)
	stream := open(t, ctx, h.client)
	receive(t, stream) // start
	if err := stream.Send(&hostproto.HostMessage{Body: &hostproto.HostMessage_Container{Container: &hostproto.ContainerReport{
		ContainerId: container.String(), Phase: hostproto.ContainerPhase_CONTAINER_PHASE_READY, ObservedAt: timestamppb.Now(),
	}}}); err != nil {
		t.Fatal(err)
	}

	tasks, err := h.execution.Submit(t.Context(), execution.SubmitRequest{
		Workspace: ws, App: "reports", Function: "summarize",
		Inputs: []execution.TaskInput{
			{Payload: execution.Payload{Encoding: execution.EncodingJSON, Data: []byte(`{"args": [1], "kwargs": {}}`)}},
			{Payload: execution.Payload{Encoding: execution.EncodingJSON, Data: []byte(`{"args": [2], "kwargs": {}}`)}},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	// The ready report may still be in flight; the claim waits for it.
	var claimed []*hostproto.ClaimedTask
	for deadline := time.Now().Add(5 * time.Second); len(claimed) < 2 && time.Now().Before(deadline); {
		resp, err := h.client.ClaimTasks(ctx, &hostproto.ClaimTasksRequest{ContainerId: container.String(), MaxTasks: 2, WaitSeconds: 1})
		if err != nil {
			t.Fatal(err)
		}
		claimed = append(claimed, resp.GetTasks()...)
	}
	if len(claimed) != 2 {
		t.Fatalf("claimed %d tasks, want 2", len(claimed))
	}

	complete := func(attempt string) error {
		_, err := h.client.CompleteTask(ctx, &hostproto.CompleteTaskRequest{
			ContainerId: container.String(), AttemptId: attempt,
			Outcome: &hostproto.CompleteTaskRequest_Success{Success: &hostproto.TaskSuccess{
				Encoding: hostproto.PayloadEncoding_PAYLOAD_ENCODING_JSON, Result: []byte("3"),
			}},
		})
		return err
	}
	if err := complete(uuid.NewString()); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("unknown attempt: got %v, want FailedPrecondition", err)
	}
	if err := complete(claimed[0].GetAttemptId()); err != nil {
		t.Fatal(err)
	}

	// Cancelling the other running task pushes a cancel to the session.
	other := claimed[1]
	var cancelled execution.TaskID
	for _, task := range tasks {
		if task.ID.String() == other.GetTaskId() {
			cancelled = task.ID
		}
	}
	if _, err := h.execution.CancelTask(t.Context(), ws, cancelled); err != nil {
		t.Fatal(err)
	}
	cancel := receive(t, stream).GetCancel()
	if cancel.GetAttemptId() != other.GetAttemptId() || cancel.GetReason() != hostproto.CancelReason_CANCEL_REASON_CANCELLED {
		t.Fatalf("cancel command %v", cancel)
	}
	if err := complete(other.GetAttemptId()); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("late completion: got %v, want FailedPrecondition", err)
	}

	// Another host's token cannot claim for this container.
	_, otherCtx := h.enroll()
	if _, err := h.client.ClaimTasks(otherCtx, &hostproto.ClaimTasksRequest{ContainerId: container.String(), MaxTasks: 1}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("foreign claim: got %v, want PermissionDenied", err)
	}
	if _, err := h.client.ClaimTasks(t.Context(), &hostproto.ClaimTasksRequest{ContainerId: container.String()}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("claim without a token: got %v, want Unauthenticated", err)
	}
}
