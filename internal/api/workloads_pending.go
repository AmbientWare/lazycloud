package api

import (
	"context"
	"errors"
)

// errWorkloadsPending marks workload operations whose handlers are being written.
var errWorkloadsPending = errors.New("workload operations are not served yet")

func (s *Server) OpenSshTunnel(ctx context.Context, request OpenSshTunnelRequestObject) (OpenSshTunnelResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) ConnectContainer(ctx context.Context, request ConnectContainerRequestObject) (ConnectContainerResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) DeleteContainerDirectory(ctx context.Context, request DeleteContainerDirectoryRequestObject) (DeleteContainerDirectoryResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) CreateContainerDirectory(ctx context.Context, request CreateContainerDirectoryRequestObject) (CreateContainerDirectoryResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) DeleteContainerFile(ctx context.Context, request DeleteContainerFileRequestObject) (DeleteContainerFileResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) ListContainerFiles(ctx context.Context, request ListContainerFilesRequestObject) (ListContainerFilesResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) DownloadContainerFile(ctx context.Context, request DownloadContainerFileRequestObject) (DownloadContainerFileResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) UploadContainerFile(ctx context.Context, request UploadContainerFileRequestObject) (UploadContainerFileResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) FindInContainerFiles(ctx context.Context, request FindInContainerFilesRequestObject) (FindInContainerFilesResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) ReplaceInContainerFiles(ctx context.Context, request ReplaceInContainerFilesRequestObject) (ReplaceInContainerFilesResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) StatContainerFile(ctx context.Context, request StatContainerFileRequestObject) (StatContainerFileResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) CreateFilesystemImage(ctx context.Context, request CreateFilesystemImageRequestObject) (CreateFilesystemImageResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) GetContainerNetwork(ctx context.Context, request GetContainerNetworkRequestObject) (GetContainerNetworkResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) SetContainerNetwork(ctx context.Context, request SetContainerNetworkRequestObject) (SetContainerNetworkResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) StreamContainerOutput(ctx context.Context, request StreamContainerOutputRequestObject) (StreamContainerOutputResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) ListContainerPorts(ctx context.Context, request ListContainerPortsRequestObject) (ListContainerPortsResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) ExposeContainerPort(ctx context.Context, request ExposeContainerPortRequestObject) (ExposeContainerPortResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) ListProcesses(ctx context.Context, request ListProcessesRequestObject) (ListProcessesResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) StartProcess(ctx context.Context, request StartProcessRequestObject) (StartProcessResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) GetProcess(ctx context.Context, request GetProcessRequestObject) (GetProcessResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) KillProcess(ctx context.Context, request KillProcessRequestObject) (KillProcessResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) OpenContainerShell(ctx context.Context, request OpenContainerShellRequestObject) (OpenContainerShellResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) SnapshotContainer(ctx context.Context, request SnapshotContainerRequestObject) (SnapshotContainerResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) SetContainerTtl(ctx context.Context, request SetContainerTtlRequestObject) (SetContainerTtlResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) GetDevbox(ctx context.Context, request GetDevboxRequestObject) (GetDevboxResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) StartDevbox(ctx context.Context, request StartDevboxRequestObject) (StartDevboxResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) StopDevbox(ctx context.Context, request StopDevboxRequestObject) (StopDevboxResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) ScaleDeployment(ctx context.Context, request ScaleDeploymentRequestObject) (ScaleDeploymentResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) CreateInstance(ctx context.Context, request CreateInstanceRequestObject) (CreateInstanceResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) ListSandboxes(ctx context.Context, request ListSandboxesRequestObject) (ListSandboxesResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) GetSandboxStats(ctx context.Context, request GetSandboxStatsRequestObject) (GetSandboxStatsResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) CreateSshCertificate(ctx context.Context, request CreateSshCertificateRequestObject) (CreateSshCertificateResponseObject, error) {
	return nil, errWorkloadsPending
}

func (s *Server) ListSshHosts(ctx context.Context, request ListSshHostsRequestObject) (ListSshHostsResponseObject, error) {
	return nil, errWorkloadsPending
}
