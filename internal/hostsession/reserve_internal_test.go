package hostsession

import (
	"log/slog"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// sentStream is a session stream that keeps what the server sends.
type sentStream struct {
	grpc.BidiStreamingServer[hostproto.HostMessage, hostproto.ServerMessage]
	sent []*hostproto.PrepareReserve
}

func (s *sentStream) Send(m *hostproto.ServerMessage) error {
	if p := m.GetPrepareReserve(); p != nil {
		s.sent = append(s.sent, p)
	}
	return nil
}

// A PrepareReserve left unanswered for ReserveAttemptTimeout is superseded
// by a fresh attempt of the same request, and only the fresh one's answer
// counts.
func TestAnUnansweredReserveAttemptIsSupersededAfterItsTimeout(t *testing.T) {
	pool := dbtest.New(t)
	c := compute.NewCompute(pool, execution.NewExecution(pool), compute.Config{})
	var host uuid.UUID
	if err := pool.QueryRow(t.Context(), `
insert into hosts (name, state, cpu_millis, memory_bytes, phase, phase_at, reserve_mode)
values ('h', 'online', 4000, 8::bigint << 30, 'preparing', now(), 'stop') returning id`).Scan(&host); err != nil {
		t.Fatal(err)
	}
	stream := &sentStream{}
	sess := &session{server: &Server{compute: c, logger: slog.New(slog.DiscardHandler)}, stream: stream, host: compute.HostID(host)}
	sync := func(sentAgo time.Duration) {
		t.Helper()
		if sess.reserve != nil {
			sess.reserve.sentAt = time.Now().Add(-sentAgo)
		}
		if err := sess.syncReserve(t.Context()); err != nil {
			t.Fatal(err)
		}
	}
	sync(0)
	sync(compute.ReserveAttemptTimeout - 5*time.Second)
	if len(stream.sent) != 1 {
		t.Fatalf("sent %d attempts before the timeout, want 1", len(stream.sent))
	}
	sync(compute.ReserveAttemptTimeout)
	if len(stream.sent) != 2 {
		t.Fatalf("sent %d attempts once the first was overdue, want 2", len(stream.sent))
	}
	first, fresh := stream.sent[0], stream.sent[1]
	if fresh.GetAttemptId() == first.GetAttemptId() || fresh.GetRequestId() != first.GetRequestId() {
		t.Fatalf("superseded %v with %v, want a new attempt of the same request", first, fresh)
	}
	answer := func(p *hostproto.PrepareReserve) string {
		t.Helper()
		if err := sess.answerReserve(t.Context(), &hostproto.ReserveReady{
			RequestId: p.GetRequestId(), AttemptId: p.GetAttemptId(), AgentVersion: "v1",
		}); err != nil {
			t.Fatal(err)
		}
		var phase string
		if err := pool.QueryRow(t.Context(), "select phase from hosts where id = $1", host).Scan(&phase); err != nil {
			t.Fatal(err)
		}
		return phase
	}
	if phase := answer(first); phase != string(compute.PhasePreparing) {
		t.Fatalf("the superseded attempt's answer moved the host to %s", phase)
	}
	if phase := answer(fresh); phase != string(compute.PhaseStopping) {
		t.Fatalf("the fresh attempt's answer left the host %s, want stopping", phase)
	}
}
