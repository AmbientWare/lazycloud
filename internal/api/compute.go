package api

import (
	"context"
	"errors"
)

// errComputeUnwired marks compute operations whose owner is not wired yet.
var errComputeUnwired = errors.New("compute operations are not wired yet")

// DisconnectAws disconnects the account's AWS account.
func (s *Server) DisconnectAws(context.Context, DisconnectAwsRequestObject) (DisconnectAwsResponseObject, error) {
	return nil, errComputeUnwired
}

// GetAwsConnection returns the account's AWS connection.
func (s *Server) GetAwsConnection(context.Context, GetAwsConnectionRequestObject) (GetAwsConnectionResponseObject, error) {
	return nil, errComputeUnwired
}

// ConnectAws starts an AWS connection.
func (s *Server) ConnectAws(context.Context, ConnectAwsRequestObject) (ConnectAwsResponseObject, error) {
	return nil, errComputeUnwired
}

// CancelAwsReconnect cancels a replacement authorization.
func (s *Server) CancelAwsReconnect(context.Context, CancelAwsReconnectRequestObject) (CancelAwsReconnectResponseObject, error) {
	return nil, errComputeUnwired
}

// ReconnectAws starts a replacement authorization.
func (s *Server) ReconnectAws(context.Context, ReconnectAwsRequestObject) (ReconnectAwsResponseObject, error) {
	return nil, errComputeUnwired
}

// RetryAwsConnection retries the connection's pending step.
func (s *Server) RetryAwsConnection(context.Context, RetryAwsConnectionRequestObject) (RetryAwsConnectionResponseObject, error) {
	return nil, errComputeUnwired
}

// ValidateAwsConnection checks the connection's authorization.
func (s *Server) ValidateAwsConnection(context.Context, ValidateAwsConnectionRequestObject) (ValidateAwsConnectionResponseObject, error) {
	return nil, errComputeUnwired
}

// ListComputeInstances lists the connection's instances.
func (s *Server) ListComputeInstances(context.Context, ListComputeInstancesRequestObject) (ListComputeInstancesResponseObject, error) {
	return nil, errComputeUnwired
}

// GetComputeSummary summarizes a workspace's compute.
func (s *Server) GetComputeSummary(context.Context, GetComputeSummaryRequestObject) (GetComputeSummaryResponseObject, error) {
	return nil, errComputeUnwired
}

// ListComputeWorkloads lists a workspace's workloads with their pins.
func (s *Server) ListComputeWorkloads(context.Context, ListComputeWorkloadsRequestObject) (ListComputeWorkloadsResponseObject, error) {
	return nil, errComputeUnwired
}

// ListWorkspaceMachines lists the machines serving a workspace.
func (s *Server) ListWorkspaceMachines(context.Context, ListWorkspaceMachinesRequestObject) (ListWorkspaceMachinesResponseObject, error) {
	return nil, errComputeUnwired
}

// ListMachines lists the account's machines.
func (s *Server) ListMachines(context.Context, ListMachinesRequestObject) (ListMachinesResponseObject, error) {
	return nil, errComputeUnwired
}

// CreateMachineJoinCommand mints a machine join command.
func (s *Server) CreateMachineJoinCommand(context.Context, CreateMachineJoinCommandRequestObject) (CreateMachineJoinCommandResponseObject, error) {
	return nil, errComputeUnwired
}

// RemoveMachine removes a machine.
func (s *Server) RemoveMachine(context.Context, RemoveMachineRequestObject) (RemoveMachineResponseObject, error) {
	return nil, errComputeUnwired
}

// UpdateMachine changes a machine's workspaces.
func (s *Server) UpdateMachine(context.Context, UpdateMachineRequestObject) (UpdateMachineResponseObject, error) {
	return nil, errComputeUnwired
}
