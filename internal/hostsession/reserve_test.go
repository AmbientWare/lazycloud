package hostsession_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

func (h *harness) exec(sql string, args ...any) {
	h.t.Helper()
	if _, err := h.pool.Exec(h.t.Context(), sql, args...); err != nil {
		h.t.Fatal(err)
	}
}

// reserveHost is a reserve-capable host's row as the session leaves it.
type reserveHost struct {
	phase, message  string
	attempt         *uuid.UUID
	sleepBoot       *string
	preparedVersion *string
	gpuProven       bool
	outcome         *string
}

func (h *harness) reserveHost(host compute.HostID) reserveHost {
	h.t.Helper()
	var r reserveHost
	if err := h.pool.QueryRow(h.t.Context(), `
select phase, phase_message, sleep_attempt_id, sleep_boot_id, prepared_agent_version, gpu_proven, last_resume_outcome
from hosts where id = $1`, uuid.UUID(host)).Scan(&r.phase, &r.message, &r.attempt, &r.sleepBoot, &r.preparedVersion, &r.gpuProven,
		&r.outcome); err != nil {
		h.t.Fatal(err)
	}
	return r
}

// waitPhase waits for the session to move host to phase.
func (h *harness) waitPhase(host compute.HostID, phase string) reserveHost {
	h.t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		r := h.reserveHost(host)
		if r.phase == phase {
			return r
		}
		if time.Now().After(deadline) {
			h.t.Fatalf("host is %s (%s), want %s", r.phase, r.message, phase)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func openHello(t *testing.T, ctx context.Context, client hostproto.HostServiceClient, hello *hostproto.Hello) hostStream {
	t.Helper()
	stream, err := client.Session(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if hello.Capacity == nil {
		hello.Capacity = &hostproto.Capacity{CpuMillis: 4000, MemoryBytes: 8 << 30}
	}
	if err := stream.Send(&hostproto.HostMessage{Body: &hostproto.HostMessage_Hello{Hello: hello}}); err != nil {
		t.Fatal(err)
	}
	return stream
}

// prepareReserve returns the next PrepareReserve the session sends.
func prepareReserve(t *testing.T, stream hostStream) *hostproto.PrepareReserve {
	t.Helper()
	for {
		if p := receive(t, stream).GetPrepareReserve(); p != nil {
			return p
		}
	}
}

func answer(t *testing.T, stream hostStream, ready *hostproto.ReserveReady) {
	t.Helper()
	if err := stream.Send(&hostproto.HostMessage{Body: &hostproto.HostMessage_ReserveReady{ReserveReady: ready}}); err != nil {
		t.Fatal(err)
	}
}

func readyFor(p *hostproto.PrepareReserve) *hostproto.ReserveReady {
	return &hostproto.ReserveReady{RequestId: p.GetRequestId(), AttemptId: p.GetAttemptId(), BootId: "boot-1", AgentVersion: "v1"}
}

// A host the planner returns to the reserve stops only once its agent
// answers the current request from the current boot; a refusal returns it
// to ready.
func TestReserveStopsOnlyOnTheAgentsProof(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	stream := open(t, ctx, h.client)
	h.waitPhase(host, "ready")

	h.exec("update hosts set phase = 'preparing', phase_at = now(), reserve_mode = 'hibernate' where id = $1", uuid.UUID(host))
	refused := prepareReserve(t, stream)
	if refused.GetMode() != hostproto.ReserveMode_RESERVE_MODE_HIBERNATE {
		t.Fatalf("prepare %v", refused)
	}
	ready := readyFor(refused)
	ready.Refused = "a volume is mounted"
	answer(t, stream, ready)
	if r := h.waitPhase(host, "ready"); !strings.Contains(r.message, "a volume is mounted") || r.attempt != nil {
		t.Fatalf("after a refusal %+v", r)
	}

	h.exec("update hosts set phase = 'preparing', phase_at = now() where id = $1", uuid.UUID(host))
	p := prepareReserve(t, stream)
	if p.GetRequestId() == refused.GetRequestId() {
		t.Fatal("a new return to the reserve reused the request id")
	}
	// Answers to the earlier request, from another boot, or to an attempt
	// the session is not awaiting change nothing.
	answer(t, stream, readyFor(refused))
	other := readyFor(p)
	other.BootId = "boot-0"
	answer(t, stream, other)
	unknown := readyFor(p)
	unknown.AttemptId = uuid.NewString()
	answer(t, stream, unknown)
	time.Sleep(300 * time.Millisecond)
	if r := h.reserveHost(host); r.phase != "preparing" {
		t.Fatalf("a stale answer moved the host to %s", r.phase)
	}

	answer(t, stream, readyFor(p))
	r := h.waitPhase(host, "stopping")
	if r.attempt == nil || r.attempt.String() != p.GetAttemptId() || *r.sleepBoot != "boot-1" || *r.preparedVersion != "v1" || r.gpuProven {
		t.Fatalf("after the proof %+v", r)
	}
}

// A reconnect sends a fresh attempt; the old attempt's answer is stale on
// the new session, as an answer to a superseded attempt is.
func TestReserveSupersededAttemptIsStale(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	first := open(t, ctx, h.client)
	h.waitPhase(host, "ready")
	h.exec("update hosts set phase = 'preparing', phase_at = now(), reserve_mode = 'stop' where id = $1", uuid.UUID(host))
	old := prepareReserve(t, first)
	_ = first.CloseSend()

	second := open(t, ctx, h.client)
	fresh := prepareReserve(t, second)
	if fresh.GetAttemptId() == old.GetAttemptId() || fresh.GetRequestId() != old.GetRequestId() {
		t.Fatalf("reconnect sent %v after %v", fresh, old)
	}
	answer(t, second, readyFor(old))
	time.Sleep(300 * time.Millisecond)
	if r := h.reserveHost(host); r.phase != "preparing" {
		t.Fatalf("the superseded attempt moved the host to %s", r.phase)
	}
	answer(t, second, readyFor(fresh))
	if r := h.waitPhase(host, "stopping"); r.attempt.String() != fresh.GetAttemptId() {
		t.Fatalf("stopping under attempt %v", r.attempt)
	}
}

func TestReserveRefusesUnprovenHosts(t *testing.T) {
	h := start(t)
	release := compute.AgentRelease{Version: "v2", SHA256: map[string]string{"amd64": strings.Repeat("a", 64)}, RolloutPercent: 100}
	if err := h.compute.PublishAgentRelease(h.t.Context(), release); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		name   string
		setup  func(compute.HostID)
		answer func(*hostproto.ReserveReady)
		reason string
	}{
		{"old agent", nil, func(r *hostproto.ReserveReady) { r.AgentVersion = "v1" }, "not the target release v2"},
		{"missing GPU", func(host compute.HostID) {
			h.exec("update hosts set gpu_count = 2, gpu_type = 'T4' where id = $1", uuid.UUID(host))
		}, func(r *hostproto.ReserveReady) { r.Gpus = 1 }, "the driver found 1 of 2 GPUs"},
		{"assigned container", func(host compute.HostID) { h.startingContainer(host) }, nil, "containers are still assigned to it (1)"},
	} {
		t.Run(c.name, func(t *testing.T) {
			host, ctx := h.enroll()
			stream := open(t, ctx, h.client)
			h.waitPhase(host, "ready")
			if c.setup != nil {
				c.setup(host)
			}
			h.exec("update hosts set phase = 'preparing', phase_at = now(), reserve_mode = 'stop' where id = $1", uuid.UUID(host))
			p := prepareReserve(t, stream)
			ready := readyFor(p)
			ready.AgentVersion, ready.Gpus = "v2", p.GetGpus()
			if c.answer != nil {
				c.answer(ready)
			}
			answer(t, stream, ready)
			if r := h.waitPhase(host, "ready"); !strings.Contains(r.message, c.reason) {
				t.Fatalf("returned with %q, want %q", r.message, c.reason)
			}
		})
	}
}

func TestGPUReserveRecordsItsProof(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	stream := open(t, ctx, h.client)
	h.waitPhase(host, "ready")
	h.exec("update hosts set phase = 'preparing', phase_at = now(), reserve_mode = 'stop', gpu_count = 4, gpu_type = 'T4' where id = $1", uuid.UUID(host))
	p := prepareReserve(t, stream)
	if p.GetGpus() != 4 {
		t.Fatalf("asked for %d GPUs", p.GetGpus())
	}
	ready := readyFor(p)
	ready.Gpus = 4
	answer(t, stream, ready)
	if r := h.waitPhase(host, "stopping"); !r.gpuProven {
		t.Fatal("the GPU proof was not recorded")
	}
}

// stoppedReserve puts host in phase as a platform AWS reserve that proved
// attempt in boot-1.
func (h *harness) stoppedReserve(host compute.HostID, phase, evidence string, mode *string, attempt uuid.UUID) {
	h.t.Helper()
	h.exec(`update hosts set provider = 'aws', instance_type = 'm7i.large', region = 'us-east-2', phase = $2, phase_at = now(),
        image_evidence = $3, reserve_mode = $4, sleep_attempt_id = $5, sleep_boot_id = 'boot-1',
        stop_requested_at = now() - interval '5 minutes', force_stop_at = now(), hibernate_refused_at = now(),
        stopped_at = now() - interval '4 minutes'
        where id = $1`, uuid.UUID(host), phase, evidence, mode, attempt)
}

// A Hello after a requested resume settles the attempt it names once:
// memory restored in the same boot or a cold boot in a new one. A reserve
// the planner refreshes prepares again.
func TestResumeSettlesTheSleepAttempt(t *testing.T) {
	h := start(t)
	stop := "stop"
	for _, c := range []struct {
		name     string
		evidence string
		mode     *string
		boot     string
		slept    float64
		phase    string
		outcome  string
		// proven is the hibernation evidence the resume report leaves.
		proven string
	}{
		{"hibernated", "saved", nil, "boot-1", 31, "ready", "memory_restored", "saved"},
		{"hibernation EC2 had not confirmed", "unknown", nil, "boot-1", 31, "ready", "memory_restored", "saved"},
		{"silent hibernation failure", "saved", nil, "boot-2", 0, "ready", "cold_boot", "failed"},
		{"plain stop", "unavailable", nil, "boot-2", 0, "ready", "cold_boot", "unavailable"},
		{"refresh", "unavailable", &stop, "boot-2", 0, "preparing", "cold_boot", "unavailable"},
	} {
		t.Run(c.name, func(t *testing.T) {
			host, ctx := h.enroll()
			first := open(t, ctx, h.client)
			h.waitPhase(host, "ready")
			_ = first.CloseSend()
			attempt := uuid.New()
			h.stoppedReserve(host, "resuming", c.evidence, c.mode, attempt)
			hello := &hostproto.Hello{BootId: c.boot, SleepAttemptId: attempt.String(), SleptSeconds: c.slept}
			openHello(t, ctx, h.client, hello)
			r := h.waitPhase(host, c.phase)
			if r.outcome == nil || *r.outcome != c.outcome || r.attempt != nil {
				t.Fatalf("after the resume %+v", r)
			}
			// The last stop's facts end with it, even though no actuator
			// recorded the start.
			var stopFacts int
			if err := h.pool.QueryRow(h.t.Context(), `select num_nonnulls(stop_requested_at, force_stop_at, hibernate_refused_at, stopped_at)
from hosts where id = $1`, uuid.UUID(host)).Scan(&stopFacts); err != nil {
				t.Fatal(err)
			}
			if stopFacts != 0 {
				t.Fatalf("%d facts of the last stop survived the resume", stopFacts)
			}
			var evidence string
			if err := h.pool.QueryRow(h.t.Context(), "select image_evidence from hosts where id = $1", uuid.UUID(host)).Scan(&evidence); err != nil {
				t.Fatal(err)
			}
			if evidence != c.proven {
				t.Fatalf("image evidence %s, want %s", evidence, c.proven)
			}
			// The same report again settles nothing more.
			openHello(t, ctx, h.client, hello)
			time.Sleep(300 * time.Millisecond)
			if again := h.reserveHost(host); again.phase != c.phase || *again.outcome != c.outcome {
				t.Fatalf("a repeated Hello moved the host to %+v", again)
			}
		})
	}
}

// A stopped or stopping reserve that comes back without a requested resume
// stays out of placement and prepares to stop again; one whose session only
// reconnected before its stop stays stopping.
func TestUnrequestedWakeGoesBackToTheReserve(t *testing.T) {
	h := start(t)
	hibernate := "hibernate"
	for _, c := range []struct {
		name  string
		phase string
		hello func(uuid.UUID) *hostproto.Hello
		want  string
	}{
		{"stopped reserve woke", "stopped", func(a uuid.UUID) *hostproto.Hello {
			return &hostproto.Hello{BootId: "boot-1", SleepAttemptId: a.String(), SleptSeconds: 600}
		}, "preparing"},
		{"stopping reserve rebooted", "stopping", func(a uuid.UUID) *hostproto.Hello {
			return &hostproto.Hello{BootId: "boot-2", SleepAttemptId: a.String()}
		}, "preparing"},
		{"reconnect before the stop", "stopping", func(a uuid.UUID) *hostproto.Hello {
			return &hostproto.Hello{BootId: "boot-1", SleepAttemptId: a.String()}
		}, "stopping"},
	} {
		t.Run(c.name, func(t *testing.T) {
			host, ctx := h.enroll()
			first := open(t, ctx, h.client)
			h.waitPhase(host, "ready")
			_ = first.CloseSend()
			attempt := uuid.New()
			h.stoppedReserve(host, c.phase, "saved", &hibernate, attempt)
			openHello(t, ctx, h.client, c.hello(attempt))
			time.Sleep(300 * time.Millisecond)
			if r := h.waitPhase(host, c.want); c.want == "stopping" && r.attempt == nil {
				t.Fatal("a reconnect settled the attempt")
			}
		})
	}
}

// The first session of a launched platform host bought for the reserve
// prepares instead of serving.
func TestFirstSessionOfALaunchedHost(t *testing.T) {
	h := start(t)
	for _, c := range []struct {
		name string
		mode *string
		want string
	}{
		{"serving", nil, "ready"},
		{"bought for the reserve", ptr("hibernate"), "preparing"},
	} {
		t.Run(c.name, func(t *testing.T) {
			host, ctx := h.enroll()
			h.exec(`update hosts set provider = 'aws', instance_type = 'm7i.large', region = 'us-east-2', reserve_mode = $2,
                created_at = now() - interval '90 seconds' where id = $1`, uuid.UUID(host), c.mode)
			open(t, ctx, h.client)
			h.waitPhase(host, c.want)
		})
	}
}

// A reserve the planner resumes while it is still stopping never slept, so
// it sends no new Hello: its session ends, and the agent's next Hello
// settles the resume.
func TestResumeOfAHostThatNeverStopped(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	stream := open(t, ctx, h.client)
	h.waitPhase(host, "ready")
	h.exec(`update hosts set phase = 'resuming', phase_at = now(), sleep_boot_id = 'boot-1'
        where id = $1`, uuid.UUID(host))
	ended := make(chan error, 1)
	go func() {
		for {
			if _, err := stream.Recv(); err != nil {
				ended <- err
				return
			}
		}
	}()
	select {
	case err := <-ended:
		if status.Code(err) != codes.Aborted {
			t.Fatalf("the session ended with %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the session of a host resumed before it stopped stayed open")
	}
	open(t, ctx, h.client)
	h.waitPhase(host, "ready")
}

// pump delivers a session's commands on a channel.
func pump(stream hostStream) <-chan *hostproto.ServerMessage {
	out := make(chan *hostproto.ServerMessage, 64)
	go func() {
		defer close(out)
		for {
			msg, err := stream.Recv()
			if err != nil {
				return
			}
			out <- msg
		}
	}()
	return out
}

func nextPrepare(t *testing.T, commands <-chan *hostproto.ServerMessage, within time.Duration) *hostproto.PrepareReserve {
	t.Helper()
	deadline := time.After(within)
	for {
		select {
		case msg, ok := <-commands:
			if !ok {
				t.Fatal("session closed")
			}
			if p := msg.GetPrepareReserve(); p != nil {
				return p
			}
		case <-deadline:
			return nil
		}
	}
}

// A reserve is not asked to stop while an agent update is offered or in
// flight, and a refusal for an update keeps it preparing instead of
// sending it into service.
func TestReserveWaitsOutAnAgentUpdate(t *testing.T) {
	h := start(t)
	if err := h.compute.PublishAgentRelease(h.t.Context(), compute.AgentRelease{
		Version: "v2", SHA256: map[string]string{"amd64": strings.Repeat("a", 64)}, RolloutPercent: 100,
	}); err != nil {
		t.Fatal(err)
	}
	host, ctx := h.enroll()
	old := openHello(t, ctx, h.client, &hostproto.Hello{BootId: "boot-1", AgentVersion: "v1", Updatable: true})
	commands := pump(old)
	h.waitPhase(host, "ready")
	h.exec("update hosts set phase = 'preparing', phase_at = now(), reserve_mode = 'hibernate' where id = $1", uuid.UUID(host))
	if p := nextPrepare(t, commands, time.Second); p != nil {
		t.Fatalf("asked an updating host to stop: %v", p)
	}
	_ = old.CloseSend()

	// The updated agent reconnects, still on trial.
	updated := openHello(t, ctx, h.client, &hostproto.Hello{BootId: "boot-1", AgentVersion: "v2", Updatable: true})
	commands = pump(updated)
	p := nextPrepare(t, commands, 5*time.Second)
	if p == nil {
		t.Fatal("the updated host was not asked to stop")
	}
	trial := readyFor(p)
	trial.AgentVersion, trial.Refused, trial.Updating = "v2", "an agent update is in flight", true
	answer(t, updated, trial)
	again := nextPrepare(t, commands, 5*time.Second)
	if again == nil || again.GetAttemptId() == p.GetAttemptId() {
		t.Fatalf("after the update refusal the host was asked %v", again)
	}
	if r := h.reserveHost(host); r.phase != "preparing" {
		t.Fatalf("an update refusal moved the host to %s", r.phase)
	}
	ready := readyFor(again)
	ready.AgentVersion = "v2"
	answer(t, updated, ready)
	h.waitPhase(host, "stopping")
}

func ptr[T any](v T) *T { return &v }
