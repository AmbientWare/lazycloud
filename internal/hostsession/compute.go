package hostsession

import (
	"context"
	"fmt"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

func capacityIn(c *hostproto.Capacity) compute.Capacity {
	return compute.Capacity{
		CPUMillis: c.GetCpuMillis(), MemoryBytes: c.GetMemoryBytes(),
		GPUType: c.GetGpuType(), GPUCount: int(c.GetGpuCount()),
	}
}

func hostReportIn(req *hostproto.EnrollRequest) compute.HostReport {
	report := compute.HostReport{
		Hostname: req.GetHostname(), Capacity: capacityIn(req.GetCapacity()), Architecture: req.GetArchitecture(),
	}
	for _, c := range req.GetPreflight() {
		report.Preflight = append(report.Preflight, compute.PreflightCheck{
			Name: c.GetName(), OK: c.GetOk(), Message: c.GetMessage(), Severity: c.GetSeverity(), Remediation: c.GetRemediation(),
		})
	}
	return report
}

// enroll exchanges a join token or a cloud identity proof for a host
// identity.
func (s *Server) enroll(ctx context.Context, req *hostproto.EnrollRequest) (compute.HostID, string, error) {
	report := hostReportIn(req)
	if proof := req.GetCloudIdentity(); proof != nil {
		host, err := uuid.Parse(proof.GetHostId())
		if err != nil {
			return compute.HostID{}, "", status.Error(codes.InvalidArgument, "cloud_identity.host_id is not a UUID")
		}
		return s.compute.EnrollCloud(ctx, compute.HostID(host), compute.IdentityProof{
			URL: proof.GetUrl(), Method: proof.GetMethod(), Headers: proof.GetHeaders(), Body: proof.GetBody(),
		}, report)
	}
	return s.compute.Enroll(ctx, req.GetJoinToken(), report)
}

// gpusOf is how many GPUs a release's container reserves: gpu_count, or
// one when it names models without a count.
func gpusOf(r apitypes.Resources) int32 {
	n := int32(0)
	if r.GpuCount != nil {
		n = int32(*r.GpuCount) //nolint:gosec // The schema caps gpu_count at 8.
	}
	if n == 0 && r.Gpu != nil && len(*r.Gpu) > 0 {
		n = 1
	}
	return n
}

// sessionOpenIn is what a Hello says about the host. An attempt id that is
// not a UUID names no attempt the server made.
func sessionOpenIn(hello *hostproto.Hello) compute.SessionOpen {
	open := compute.SessionOpen{
		BootID: hello.GetBootId(), Capacity: capacityIn(hello.GetCapacity()), AgentVersion: hello.GetAgentVersion(),
		SleptSeconds: hello.GetSleptSeconds(),
	}
	if attempt, err := uuid.Parse(hello.GetSleepAttemptId()); err == nil {
		open.SleepAttempt = &attempt
	}
	return open
}

// reserveAttempt is the PrepareReserve this session sent and awaits.
type reserveAttempt struct {
	request string
	attempt uuid.UUID
	sentAt  time.Time
}

// syncReserve asks a host the planner is returning to the reserve to prove
// it may stop, and asks again with a fresh attempt when an answer is
// overdue. Only the newest attempt's answer counts.
func (sess *session) syncReserve(ctx context.Context) error {
	request, err := sess.server.compute.PendingReserve(ctx, sess.host)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	if request == nil {
		sess.reserve = nil
		return nil
	}
	if sess.reserve != nil && sess.reserve.request == request.ID && time.Since(sess.reserve.sentAt) < compute.ReserveAttemptTimeout {
		return nil
	}
	attempt := uuid.New()
	mode := hostproto.ReserveMode_RESERVE_MODE_STOP
	if request.Mode == compute.ReserveHibernate {
		mode = hostproto.ReserveMode_RESERVE_MODE_HIBERNATE
	}
	msg := &hostproto.ServerMessage{CommandId: "reserve:" + attempt.String(), Body: &hostproto.ServerMessage_PrepareReserve{PrepareReserve: &hostproto.PrepareReserve{
		RequestId: request.ID, AttemptId: attempt.String(), Mode: mode, Gpus: int32(request.GPUs), //nolint:gosec // GPU counts are small.
	}}}
	if err := sess.stream.Send(msg); err != nil {
		return fmt.Errorf("send prepare reserve: %w", err)
	}
	sess.reserve = &reserveAttempt{request: request.ID, attempt: attempt, sentAt: time.Now()}
	return nil
}

// answerReserve applies the answer to the attempt this session awaits; an
// answer to any other is stale.
func (sess *session) answerReserve(ctx context.Context, ready *hostproto.ReserveReady) error {
	if sess.reserve == nil || ready.GetAttemptId() != sess.reserve.attempt.String() || ready.GetRequestId() != sess.reserve.request {
		return nil
	}
	verdict, err := sess.server.compute.AnswerReserve(ctx, sess.host, compute.ReserveAnswer{
		RequestID: ready.GetRequestId(), Attempt: sess.reserve.attempt, BootID: ready.GetBootId(), AgentVersion: ready.GetAgentVersion(),
		GPUs: int(ready.GetGpus()), Refused: ready.GetRefused(),
	})
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	if verdict == compute.ReserveStale {
		// The request moved on or the answer came from another boot; the
		// next sync asks for the current one.
		return nil
	}
	sess.reserve = nil
	sess.server.logger.InfoContext(ctx, "reserve answer", "host", sess.host.String(), "verdict", string(verdict), "refused", ready.GetRefused())
	return nil
}

func updateMessage(u *compute.AgentUpdate) *hostproto.ServerMessage {
	return &hostproto.ServerMessage{CommandId: "update:" + u.Version, Body: &hostproto.ServerMessage_Update{Update: &hostproto.UpdateAgent{
		Version: u.Version, Url: u.URL, Sha256: u.SHA256,
	}}}
}
