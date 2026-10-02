package hostsession

import (
	"context"

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

func updateMessage(u *compute.AgentUpdate) *hostproto.ServerMessage {
	return &hostproto.ServerMessage{CommandId: "update:" + u.Version, Body: &hostproto.ServerMessage_Update{Update: &hostproto.UpdateAgent{
		Version: u.Version, Url: u.URL, Sha256: u.SHA256,
	}}}
}
