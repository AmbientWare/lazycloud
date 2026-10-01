package execution

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/ssh"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

type podFixture struct {
	workspace identity.WorkspaceID
	workload  uuid.UUID
	release   uuid.UUID
}

const podSpec = `{"name": "web", "source": {"sha256": "00"}, "image": {"python_version": "3.12"},
 "resources": {"cpu_millis": 500, "memory_mib": 256}, "keep_warm_seconds": %d,
 "pod": {"kind": "%s", "command": ["python", "-m", "http.server", "8080"], "ports": {"http": 8080}}}`

// deployedPod inserts an active pod "tools/web" of kind (pod, devbox or
// sandbox) with keepWarm.
func deployedPod(t *testing.T, pool *pgxpool.Pool, kind string, keepWarm int) podFixture {
	t.Helper()
	var f podFixture
	var ws uuid.UUID
	workloadKind := "pod"
	if kind == "sandbox" {
		workloadKind = "sandbox"
	}
	spec := strings.Replace(strings.Replace(podSpec, "%d", itoa(keepWarm), 1), "%s", kind, 1)
	err := pool.QueryRow(t.Context(), `
with ws as (insert into workspaces (name) values ('ws') returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'tools', 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, $2, 'web', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, $1::jsonb, sha256('spec'), sha256('src') from wl returning id, workload_id)
select ws.id, rel.workload_id, rel.id from ws, rel`, spec, workloadKind).Scan(&ws, &f.workload, &f.release)
	if err != nil {
		t.Fatalf("insert pod: %v", err)
	}
	dbtest.OwnWorkspaces(t, pool)
	if _, err := pool.Exec(t.Context(), "update workloads set active_release_id = $1, next_version = 2", f.release); err != nil {
		t.Fatal(err)
	}
	f.workspace = identity.WorkspaceID(ws)
	return f
}

func itoa(n int) string {
	if n < 0 {
		return "-" + itoa(-n)
	}
	digits := ""
	for {
		digits = string(rune('0'+n%10)) + digits
		n /= 10
		if n == 0 {
			return digits
		}
	}
}

// readyOnHost places container on a new host and marks it ready.
func readyOnHost(t *testing.T, pool *pgxpool.Pool, e *Execution, container ContainerID) uuid.UUID {
	t.Helper()
	var host uuid.UUID
	if err := pool.QueryRow(t.Context(), `insert into hosts (name, token_hash, state, cpu_millis, memory_bytes)
values ('h', sha256(gen_random_uuid()::text::bytea), 'online', 4000, 1 << 32) returning id`).Scan(&host); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(t.Context(), "update containers set state = 'starting', host_id = $2, assigned_at = now() where id = $1", uuid.UUID(container), host); err != nil {
		t.Fatal(err)
	}
	if err := e.queries.MarkContainerReady(t.Context(), uuid.UUID(container)); err != nil {
		t.Fatal(err)
	}
	return host
}

// age moves a container's activity window into the past.
func age(t *testing.T, pool *pgxpool.Pool, container ContainerID, by time.Duration) {
	t.Helper()
	if _, err := pool.Exec(t.Context(), "update containers set active_until = active_until - make_interval(secs => $2) where id = $1",
		uuid.UUID(container), by.Seconds()); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(t.Context(), "update container_leases set expires_at = expires_at - make_interval(secs => $2) where container_id = $1",
		uuid.UUID(container), by.Seconds()); err != nil {
		t.Fatal(err)
	}
}

func TestInstancesStopOnceIdleAndConnectionsKeepThemUp(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedPod(t, pool, "sandbox", 600)
	logger := slog.New(slog.DiscardHandler)

	inst, err := e.CreateInstance(t.Context(), f.workspace, InstanceRequest{Release: &f.release, Timeout: new(60)})
	if err != nil {
		t.Fatalf("create instance: %v", err)
	}
	if inst.Purpose != PurposeInstance || inst.State != ContainerPending || len(inst.Ports) != 1 || inst.Ports[0] != 8080 {
		t.Fatalf("instance = %+v", inst)
	}
	readyOnHost(t, pool, e, inst.ID)

	// Planning never counts or drains an instance.
	if _, err := e.PlanPods(t.Context(), logger); err != nil {
		t.Fatal(err)
	}
	if got := containerState(t, pool, uuid.UUID(inst.ID)); got != ContainerReady {
		t.Fatalf("after planning the instance is %s", got)
	}

	// A connection keeps it up past its lifetime, and for its window after
	// the connection ends.
	holder := uuid.New()
	if err := e.HoldContainers(t.Context(), holder, []ContainerID{inst.ID}); err != nil {
		t.Fatal(err)
	}
	age(t, pool, inst.ID, 61*time.Second)
	if err := e.HoldContainers(t.Context(), holder, []ContainerID{inst.ID}); err != nil {
		t.Fatal(err)
	}
	if n, err := e.StopIdle(t.Context(), logger); err != nil || n != 0 {
		t.Fatalf("stop idle with a connection: %d, %v", n, err)
	}
	if err := e.ReleaseContainers(t.Context(), holder, []ContainerID{inst.ID}); err != nil {
		t.Fatal(err)
	}
	if n, err := e.StopIdle(t.Context(), logger); err != nil || n != 0 {
		t.Fatalf("stop idle inside the window after the connection: %d, %v", n, err)
	}
	age(t, pool, inst.ID, 61*time.Second)
	if n, err := e.StopIdle(t.Context(), logger); err != nil || n != 1 {
		t.Fatalf("stop idle: %d, %v", n, err)
	}
	if got := containerState(t, pool, uuid.UUID(inst.ID)); got != ContainerDraining {
		t.Fatalf("idle instance is %s", got)
	}

	// A TTL of 0 keeps another one up without a limit; a call is activity.
	other, err := e.CreateInstance(t.Context(), f.workspace, InstanceRequest{Release: &f.release})
	if err != nil {
		t.Fatal(err)
	}
	readyOnHost(t, pool, e, other.ID)
	ttl, err := e.SetTTL(t.Context(), f.workspace, other.ID, 0)
	if err != nil || ttl.Seconds != -1 || ttl.ExpiresAt != nil {
		t.Fatalf("ttl = %+v, %v", ttl, err)
	}
	if n, err := e.StopIdle(t.Context(), logger); err != nil || n != 0 {
		t.Fatalf("stop idle without a limit: %d, %v", n, err)
	}
	if _, err := e.SetTTL(t.Context(), f.workspace, other.ID, 120); err != nil {
		t.Fatal(err)
	}
	age(t, pool, other.ID, 100*time.Second)
	if err := e.Touch(t.Context(), other.ID); err != nil {
		t.Fatal(err)
	}
	age(t, pool, other.ID, 30*time.Second)
	if n, err := e.StopIdle(t.Context(), logger); err != nil || n != 0 {
		t.Fatalf("a call did not count as activity: %d, %v", n, err)
	}
}

func TestInstancesNeedAPodOrAShell(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	fn := deployedFunction(t, pool, `{"name": "summarize", "handler": "reports:summarize", "source": {"sha256": "00"},
 "image": {"python_version": "3.12"}, "resources": {"cpu_millis": 250, "memory_mib": 128}, "max_pending_tasks": 100}`)
	if _, err := e.CreateInstance(t.Context(), fn.workspace, InstanceRequest{Release: &fn.release}); err == nil {
		t.Fatal("a function instance was created")
	}
	shell, err := e.CreateInstance(t.Context(), fn.workspace, InstanceRequest{Release: &fn.release, Shell: true})
	if err != nil {
		t.Fatalf("shell container: %v", err)
	}
	if shell.Purpose != PurposeShell || shell.KeepWarm == nil || *shell.KeepWarm != 30 {
		t.Fatalf("shell = %+v", shell)
	}
	readyOnHost(t, pool, e, shell.ID)
	// A shell container of a function's release claims no tasks.
	submit(t, e, fn, 1)
	host := uuid.Nil
	if err := pool.QueryRow(t.Context(), "select host_id from containers where id = $1", uuid.UUID(shell.ID)).Scan(&host); err != nil {
		t.Fatal(err)
	}
	claimed, err := e.ClaimTasks(t.Context(), listen(t, pool), compute.HostID(host), shell.ID, 1, 0)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(claimed) != 0 {
		t.Fatalf("a shell container claimed %d tasks", len(claimed))
	}
}

func TestPodsFollowConnectionsScalesAndParks(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	logger := slog.New(slog.DiscardHandler)
	f := deployedPod(t, pool, "pod", 600)
	serve := func() []ContainerID {
		t.Helper()
		rows, err := pool.Query(t.Context(), `select c.id from containers c join releases r on r.id = c.release_id
where r.workload_id = $1 and c.purpose = 'serve' and c.state in ('pending', 'starting', 'ready') order by c.id`, f.workload)
		if err != nil {
			t.Fatal(err)
		}
		defer rows.Close()
		var out []ContainerID
		for rows.Next() {
			var id uuid.UUID
			if err := rows.Scan(&id); err != nil {
				t.Fatal(err)
			}
			out = append(out, ContainerID(id))
		}
		return out
	}
	plan := func() {
		t.Helper()
		if _, err := e.PlanPods(t.Context(), logger); err != nil {
			t.Fatal(err)
		}
	}

	plan()
	if n := len(serve()); n != 0 {
		t.Fatalf("an unconnected pod has %d containers", n)
	}
	if err := e.WakePod(t.Context(), f.workspace, f.workload); err != nil {
		t.Fatal(err)
	}
	plan()
	live := serve()
	if len(live) != 1 {
		t.Fatalf("a woken pod has %d containers", len(live))
	}
	readyOnHost(t, pool, e, live[0])
	// Past the wake window and its keep-warm, the container drains.
	if _, err := pool.Exec(t.Context(), "update pod_states set woken_at = now() - interval '1 hour'"); err != nil {
		t.Fatal(err)
	}
	plan()
	if n := len(serve()); n != 1 {
		t.Fatalf("a warm container was drained; %d live", n)
	}
	age(t, pool, live[0], 601*time.Second)
	plan()
	if n := len(serve()); n != 0 {
		t.Fatalf("an idle pod kept %d containers", n)
	}

	if err := e.ScalePod(t.Context(), f.workspace, f.workload, 2); err != nil {
		t.Fatalf("scale: %v", err)
	}
	plan()
	if n := len(serve()); n != 2 {
		t.Fatalf("a pod scaled to 2 has %d", n)
	}
	if err := e.ScalePod(t.Context(), f.workspace, f.workload, 0); err != nil {
		t.Fatal(err)
	}
	plan()
	if n := len(serve()); n != 0 {
		t.Fatalf("a pod scaled to 0 has %d", n)
	}

	// A parked devbox stays down until woken.
	if _, err := pool.Exec(t.Context(), "update pod_states set replicas = null"); err != nil {
		t.Fatal(err)
	}
	if err := e.ParkPod(t.Context(), f.workspace, f.workload); err != nil {
		t.Fatal(err)
	}
	plan()
	if n := len(serve()); n != 0 {
		t.Fatalf("a parked pod has %d", n)
	}
	view, err := e.PodView(t.Context(), f.workspace, f.workload, func(apitypes.FunctionSpec) (bool, error) { return false, nil })
	if err != nil || view.Phase != apitypes.DevboxPhaseStopping {
		t.Fatalf("stopping view = %+v, %v", view, err)
	}
	// Its host stops the drained containers.
	if _, err := pool.Exec(t.Context(), "update containers set state = 'stopped', stop_reason = 'stopped', stopped_at = now() where state = 'draining'"); err != nil {
		t.Fatal(err)
	}
	view, err = e.PodView(t.Context(), f.workspace, f.workload, func(apitypes.FunctionSpec) (bool, error) { return false, nil })
	if err != nil || view.Phase != apitypes.DevboxPhaseStopped {
		t.Fatalf("parked view = %+v, %v", view, err)
	}
	if err := e.WakePod(t.Context(), f.workspace, f.workload); err != nil {
		t.Fatal(err)
	}
	if view, err = e.PodView(t.Context(), f.workspace, f.workload, func(apitypes.FunctionSpec) (bool, error) { return false, nil }); err != nil || view.Phase != apitypes.DevboxPhaseQueued {
		t.Fatalf("woken view = %+v, %v", view, err)
	}
	plan()
	if n := len(serve()); n != 1 {
		t.Fatalf("a woken pod has %d", n)
	}
}

func TestAlwaysOnPodsRefuseScalingToZero(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedPod(t, pool, "pod", -1)
	if _, err := e.PlanPods(t.Context(), slog.New(slog.DiscardHandler)); err != nil {
		t.Fatal(err)
	}
	var n int
	if err := pool.QueryRow(t.Context(), "select count(*) from containers where state = 'pending' and keep_warm_seconds is null").Scan(&n); err != nil || n != 1 {
		t.Fatalf("always-on pod has %d never-idle containers, %v", n, err)
	}
	if err := e.ScalePod(t.Context(), f.workspace, f.workload, 0); err == nil {
		t.Fatal("an always-on pod scaled to zero")
	}
}

func TestSnapshotsAreReportedOnceByTheirHost(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedPod(t, pool, "sandbox", 600)
	inst, err := e.CreateInstance(t.Context(), f.workspace, InstanceRequest{Release: &f.release})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := e.CreateSnapshot(t.Context(), f.workspace, inst.ID, nil); err == nil {
		t.Fatal("a pending container was snapshotted")
	}
	host := readyOnHost(t, pool, e, inst.ID)
	snap, err := e.CreateSnapshot(t.Context(), f.workspace, inst.ID, nil)
	if err != nil || snap.State != apitypes.MemorySnapshotStatePending {
		t.Fatalf("snapshot = %+v, %v", snap, err)
	}
	cmds, err := e.HostCommands(t.Context(), compute.HostID(host))
	if err != nil || len(cmds.Snapshot) != 1 || cmds.Snapshot[0].Snapshot != snap.ID {
		t.Fatalf("commands = %+v, %v", cmds.Snapshot, err)
	}
	if err := e.CompleteSnapshot(t.Context(), compute.HostID(uuid.New()), SnapshotOutcome{Snapshot: snap.ID, Container: inst.ID, Failure: "x"}); err == nil {
		t.Fatal("another host completed the snapshot")
	}
	if err := e.CompleteSnapshot(t.Context(), compute.HostID(host), SnapshotOutcome{
		Snapshot: snap.ID, Container: inst.ID, Failure: "CRIU is not installed", Unsupported: true,
	}); err != nil {
		t.Fatal(err)
	}
	got, err := e.Snapshot(t.Context(), snap.ID)
	if err != nil || got.State != apitypes.MemorySnapshotStateFailed {
		t.Fatalf("snapshot = %+v, %v", got, err)
	}
	var unsupported *UnsupportedError
	if failure := SnapshotFailure(got); !errors.As(failure, &unsupported) {
		t.Fatalf("failure = %v", failure)
	}
	if err := e.CompleteSnapshot(t.Context(), compute.HostID(host), SnapshotOutcome{Snapshot: snap.ID, Container: inst.ID, SizeBytes: 1, SHA256: strings.Repeat("a", 64)}); err == nil {
		t.Fatal("a finished snapshot was completed again")
	}

	stored, err := e.CreateSnapshot(t.Context(), f.workspace, inst.ID, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := e.CompleteSnapshot(t.Context(), compute.HostID(host), SnapshotOutcome{Snapshot: stored.ID, Container: inst.ID, SizeBytes: 10, SHA256: strings.Repeat("b", 64)}); err != nil {
		t.Fatal(err)
	}
	restored, err := e.CreateInstance(t.Context(), f.workspace, InstanceRequest{Snapshot: &stored.ID})
	if err != nil {
		t.Fatalf("restore: %v", err)
	}
	restore, err := e.RestoreFor(t.Context(), restored.ID)
	if err != nil || restore == nil || restore.Snapshot != stored.ID || restore.Automatic {
		t.Fatalf("restore = %+v, %v", restore, err)
	}
}

func TestSSHCertificatesAreSignedByTheWorkspaceAuthority(t *testing.T) {
	pool := dbtest.New(t)
	f := deployedPod(t, pool, "devbox", 1800)
	keys := NewSSHKeys(pool, plainSealer{})
	_, authority, err := keys.Authority(t.Context(), f.workspace)
	if err != nil {
		t.Fatal(err)
	}
	_, again, err := keys.Authority(t.Context(), f.workspace)
	if err != nil || again != authority {
		t.Fatalf("authority changed: %v", err)
	}
	signer, err := ssh.ParsePrivateKey([]byte(testUserKey))
	if err != nil {
		t.Fatal(err)
	}
	public := string(ssh.MarshalAuthorizedKey(signer.PublicKey()))
	cert, err := keys.Sign(t.Context(), f.workspace, "user:test", public)
	if err != nil {
		t.Fatal(err)
	}
	parsed, _, _, _, err := ssh.ParseAuthorizedKey([]byte(cert.Line))
	if err != nil {
		t.Fatal(err)
	}
	ca, _, _, _, err := ssh.ParseAuthorizedKey([]byte(authority))
	if err != nil {
		t.Fatal(err)
	}
	checker := ssh.CertChecker{IsUserAuthority: func(k ssh.PublicKey) bool { return string(k.Marshal()) == string(ca.Marshal()) }}
	c, ok := parsed.(*ssh.Certificate)
	if !ok || checker.CheckCert(SSHPrincipal, c) != nil || time.Until(cert.ExpiresAt) < 11*time.Hour {
		t.Fatalf("certificate does not verify for root for 12 hours: %+v", c)
	}
	if _, err := keys.Sign(t.Context(), f.workspace, "user:test", "ssh-rsa AAAA"); err == nil {
		t.Fatal("a non-ed25519 key was signed")
	}
	ws := identity.Workspace{ID: f.workspace, Name: "ws"}
	hosts, _, err := keys.Hosts(t.Context(), ws, SSHHostFilter{}, "", 10)
	if err != nil || len(hosts) != 1 || hosts[0].Alias != "lazycloud-ws-tools-web" || hosts[0].Role != apitypes.PodRoleDevbox || hosts[0].PublicKey == "" {
		t.Fatalf("hosts = %+v, %v", hosts, err)
	}
	pem, public2, err := keys.HostKey(t.Context(), f.workload)
	if err != nil || public2 != hosts[0].PublicKey || len(pem) == 0 {
		t.Fatalf("host key = %q, %v", public2, err)
	}
}

// plainSealer stands in for the secrets owner's sealing, whose own tests
// cover the encryption.
type plainSealer struct{}

func (plainSealer) Seal(_ context.Context, binding string, value []byte) ([]byte, error) {
	return append([]byte(binding+"|"), value...), nil
}

func (plainSealer) Open(_ context.Context, binding string, blob []byte) ([]byte, error) {
	return blob[len(binding)+1:], nil
}

const testUserKey = `-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACAvGnEDTUc4fdHk3MjJc7HCPj7aP81pIcZAQc5W0ENxEgAAAJCt9fxirfX8
YgAAAAtzc2gtZWQyNTUxOQAAACAvGnEDTUc4fdHk3MjJc7HCPj7aP81pIcZAQc5W0ENxEg
AAAEB8pXQ0j+M3l0eYkqsmqUgW6v4G6ExUm7h2vqC4M3wPOS8acQNNRzh90eTcyMlzscI+
Ptp/zWkhxkBBzlbQQ3ESAAAAB3Rlc3RrZXkBAgMEBQY=
-----END OPENSSH PRIVATE KEY-----
`
