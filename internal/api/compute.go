package api

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

var errInvalidCursor = errors.New("the cursor is not from a previous page")

// account returns the caller's account for account-level compute: a user
// credential that is not restricted to one workspace.
func (s *Server) account(ctx context.Context, action string) (identity.Principal, error) {
	p, ok := principalFrom(ctx)
	if !ok {
		return identity.Principal{}, identity.ErrUnauthenticated
	}
	if p.TokenWorkspace != nil {
		return identity.Principal{}, &identity.AccountError{Action: action}
	}
	return p, nil
}

func encodeCursor(v any) *string {
	data, err := json.Marshal(v)
	if err != nil {
		return nil
	}
	c := base64.RawURLEncoding.EncodeToString(data)
	return &c
}

func decodeCursor(cursor *string, v any) error {
	if cursor == nil || *cursor == "" {
		return nil
	}
	data, err := base64.RawURLEncoding.DecodeString(*cursor)
	if err != nil || json.Unmarshal(data, v) != nil {
		return fmt.Errorf("%w: %w", errInvalidRequest, errInvalidCursor)
	}
	return nil
}

func pageLimit[T ~int](limit *T) int {
	if limit == nil {
		return defaultPageSize
	}
	return int(*limit)
}

// GetComputeSummary summarizes a workspace's compute.
func (s *Server) GetComputeSummary(ctx context.Context, req GetComputeSummaryRequestObject) (GetComputeSummaryResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	sum, err := s.owners.Compute.WorkspaceSummary(ctx, ws.ID)
	if err != nil {
		return nil, err
	}
	out := GetComputeSummary200JSONResponse{WorkloadCount: sum.WorkloadCount}
	out.Instances.Total, out.Instances.Ready, out.Instances.Pending, out.Instances.Degraded = sum.Total, sum.Ready, sum.Pending, sum.Degraded
	out.Cost.Currency, out.Cost.Estimated = apitypes.USD, true
	if sum.AWSAccountID != "" {
		out.Connection = &struct {
			AccountId string                      `json:"account_id"`
			Phase     apitypes.AwsConnectionPhase `json:"phase"`
		}{AccountId: sum.AWSAccountID, Phase: apitypes.AwsConnectionPhase(sum.ConnectionPhase)}
		if sum.HourlyMicros != nil {
			daily := *sum.HourlyMicros * 24
			out.Cost.HourlyMicros, out.Cost.DailyMicros = sum.HourlyMicros, &daily
		}
	}
	return out, nil
}

// ListComputeWorkloads lists a workspace's deployed workloads with their
// sizes and machine pins.
func (s *Server) ListComputeWorkloads(ctx context.Context, req ListComputeWorkloadsRequestObject) (ListComputeWorkloadsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	var after compute.WorkloadCursor
	if err := decodeCursor(req.Params.Cursor, &after); err != nil {
		return nil, err
	}
	limit := pageLimit(req.Params.Limit)
	rows, err := s.owners.Compute.Workloads(ctx, ws.ID, after, limit+1)
	if err != nil {
		return nil, err
	}
	out := ListComputeWorkloads200JSONResponse{Workloads: []apitypes.ComputeWorkload{}}
	if len(rows) > limit {
		rows = rows[:limit]
		last := rows[limit-1]
		out.NextCursor = encodeCursor(compute.WorkloadCursor{App: last.App, Name: last.Name})
	}
	for _, w := range rows {
		gpus := make([]apitypes.GpuType, len(w.GPUs))
		for n, g := range w.GPUs {
			gpus[n] = apitypes.GpuType(g)
		}
		out.Workloads = append(out.Workloads, apitypes.ComputeWorkload{
			DeploymentId: w.ID, App: w.App, Name: w.Name, Kind: apitypes.ComputeWorkloadKind(w.Kind), Machine: w.Machine,
			CpuMillicores: w.CPUMillis, MemoryMb: w.MemoryBytes >> 20, Gpu: gpus, GpuCount: w.GPUCount,
		})
	}
	return out, nil
}

func machineOut(m compute.Machine) apitypes.Machine {
	out := apitypes.Machine{
		Id: uuid.UUID(m.ID), Name: m.Name, Workspaces: m.Workspaces, Placement: "machine:" + m.ID.String(),
		Provider: apitypes.MachineAgent, Lifecycle: apitypes.MachineLifecycle(m.Phase),
		LifecycleMessage: m.PhaseMessage, LifecycleAt: m.PhaseAt, Cpu: m.CPUMillis, Memory: m.MemoryBytes >> 20,
		Gpu: m.GPUType, GpuCount: m.GPUCount, Connected: m.Connected, Schedulable: m.Schedulable(),
		CapacityState: apitypes.CapacityState(m.CapacityState), CapacityReason: m.CapacityReason,
		PreflightChecks: []apitypes.PreflightCheck{}, Remediation: m.Remediation(), AgentVersion: m.AgentVersion,
		LastSeenAt: m.LastSeenAt, CreatedAt: m.CreatedAt, UpdatedAt: m.UpdatedAt,
	}
	if out.Remediation == nil {
		out.Remediation = []string{}
	}
	if m.Failure != nil {
		f := apitypes.MachineFailure(*m.Failure)
		out.LifecycleFailure = &f
	}
	for _, c := range m.Preflight {
		out.PreflightChecks = append(out.PreflightChecks, apitypes.PreflightCheck{
			Name: c.Name, Ok: c.OK, Message: c.Message, Severity: apitypes.PreflightCheckSeverity(c.Severity), Remediation: c.Remediation,
		})
	}
	return out
}

func machinePage(rows []compute.Machine, limit int) apitypes.MachinePage {
	out := apitypes.MachinePage{Machines: []apitypes.Machine{}}
	if len(rows) > limit {
		rows = rows[:limit]
		out.NextCursor = encodeCursor(rows[limit-1].Name)
	}
	for _, m := range rows {
		out.Machines = append(out.Machines, machineOut(m))
	}
	return out
}

// ListWorkspaceMachines lists the joined machines that serve a workspace.
func (s *Server) ListWorkspaceMachines(ctx context.Context, req ListWorkspaceMachinesRequestObject) (ListWorkspaceMachinesResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	var after string
	if err := decodeCursor(req.Params.Cursor, &after); err != nil {
		return nil, err
	}
	limit := pageLimit(req.Params.Limit)
	rows, err := s.owners.Compute.WorkspaceMachines(ctx, ws.ID, after, limit+1)
	if err != nil {
		return nil, err
	}
	return ListWorkspaceMachines200JSONResponse(machinePage(rows, limit)), nil
}

// ListMachines lists the machines the caller's account joined.
func (s *Server) ListMachines(ctx context.Context, req ListMachinesRequestObject) (ListMachinesResponseObject, error) {
	p, err := s.account(ctx, "list machines")
	if err != nil {
		return nil, err
	}
	var after string
	if err := decodeCursor(req.Params.Cursor, &after); err != nil {
		return nil, err
	}
	limit := pageLimit(req.Params.Limit)
	rows, err := s.owners.Compute.AccountMachines(ctx, p.User, after, limit+1)
	if err != nil {
		return nil, err
	}
	return ListMachines200JSONResponse(machinePage(rows, limit)), nil
}

// ownedWorkspaces resolves names to workspaces the caller owns.
func (s *Server) ownedWorkspaces(ctx context.Context, p identity.Principal, names []apitypes.Name) ([]uuid.UUID, error) {
	if len(names) == 0 {
		return nil, &compute.InvalidError{Message: "a machine must serve at least one named workspace"}
	}
	ids := make([]uuid.UUID, 0, len(names))
	for _, name := range names {
		ws, err := s.owners.Identity.AuthorizeWorkspace(ctx, p, name)
		var role *identity.RoleError
		if errors.Is(err, identity.ErrForbidden) || errors.Is(err, identity.ErrNotFound) || errors.As(err, &role) ||
			(err == nil && ws.Role != identity.RoleOwner) {
			return nil, &compute.InvalidError{Message: fmt.Sprintf("workspace %q is not owned by this account", name)}
		}
		if err != nil {
			return nil, err
		}
		if !containsID(ids, uuid.UUID(ws.ID)) {
			ids = append(ids, uuid.UUID(ws.ID))
		}
	}
	return ids, nil
}

func containsID(ids []uuid.UUID, id uuid.UUID) bool {
	for _, v := range ids {
		if v == id {
			return true
		}
	}
	return false
}

// CreateMachineJoinCommand mints the command that joins a host as a named
// machine.
func (s *Server) CreateMachineJoinCommand(ctx context.Context, req CreateMachineJoinCommandRequestObject) (CreateMachineJoinCommandResponseObject, error) {
	p, err := s.account(ctx, "join machines")
	if err != nil {
		return nil, err
	}
	workspaces, err := s.ownedWorkspaces(ctx, p, req.Body.Workspaces)
	if err != nil {
		return nil, err
	}
	join := compute.MachineJoin{Account: p.User, Name: req.Body.Name, Workspaces: workspaces}
	if req.Body.Gpu != nil {
		join.GPUs = *req.Body.Gpu
	}
	if req.Body.TtlSeconds != nil {
		join.TTL = time.Duration(*req.Body.TtlSeconds) * time.Second
	}
	cmd, err := s.owners.Compute.CreateMachineJoin(ctx, join)
	if err != nil {
		return nil, err
	}
	return CreateMachineJoinCommand201JSONResponse{Command: cmd.Command, ExpiresAt: cmd.ExpiresAt, Machine: machineOut(cmd.Machine)}, nil
}

// UpdateMachine changes the workspaces a machine serves.
func (s *Server) UpdateMachine(ctx context.Context, req UpdateMachineRequestObject) (UpdateMachineResponseObject, error) {
	p, err := s.account(ctx, "update machines")
	if err != nil {
		return nil, err
	}
	workspaces, err := s.ownedWorkspaces(ctx, p, req.Body.Workspaces)
	if err != nil {
		return nil, err
	}
	m, err := s.owners.Compute.UpdateMachineWorkspaces(ctx, p.User, req.Machine, workspaces)
	if err != nil {
		return nil, err
	}
	return UpdateMachine200JSONResponse(machineOut(m)), nil
}

// RemoveMachine retires a machine.
func (s *Server) RemoveMachine(ctx context.Context, req RemoveMachineRequestObject) (RemoveMachineResponseObject, error) {
	p, err := s.account(ctx, "remove machines")
	if err != nil {
		return nil, err
	}
	if err := s.owners.Compute.RemoveMachine(ctx, p.User, req.Machine); err != nil {
		return nil, err
	}
	return RemoveMachine204Response{}, nil
}

// ListComputeInstances lists the instances of the caller's connected AWS
// account.
func (s *Server) ListComputeInstances(ctx context.Context, req ListComputeInstancesRequestObject) (ListComputeInstancesResponseObject, error) {
	p, err := s.account(ctx, "list compute instances")
	if err != nil {
		return nil, err
	}
	var before *uuid.UUID
	if err := decodeCursor(req.Params.Cursor, &before); err != nil {
		return nil, err
	}
	limit := pageLimit(req.Params.Limit)
	rows, err := s.owners.Compute.Instances(ctx, p.User, before, limit+1)
	if err != nil {
		return nil, err
	}
	out := ListComputeInstances200JSONResponse{Instances: []apitypes.ComputeInstance{}}
	if len(rows) > limit {
		rows = rows[:limit]
		last := uuid.UUID(rows[limit-1].ID)
		out.NextCursor = encodeCursor(last)
	}
	for _, i := range rows {
		item := apitypes.ComputeInstance{
			Id: uuid.UUID(i.ID), Placement: "connection:" + i.Connection.String(), Provider: apitypes.InstanceAws,
			Region: i.Region, AvailabilityZone: i.Zone, InstanceId: i.InstanceID, InstanceType: i.InstanceType,
			Lifecycle: apitypes.MachineLifecycle(i.Phase), LifecycleMessage: i.PhaseMessage, LifecycleAt: i.PhaseAt,
			Connected: i.Connected, CapacityState: apitypes.CapacityState(i.CapacityState), CapacityReason: i.CapacityReason,
			GpuCount: i.GPUCount, CpuMillicores: i.CPUMillis, MemoryMb: i.MemoryBytes >> 20, LaunchAttempt: i.LaunchAttempts,
			BootedTemplateVersion: i.AgentVersion, LaunchedAt: i.LaunchedAt, CreatedAt: i.CreatedAt,
		}
		if i.GPUType != "" {
			item.Gpu = &i.GPUType
		}
		if i.Market != "" {
			m := apitypes.ComputeInstanceMarket(i.Market)
			item.Market = &m
		}
		if i.Failure != nil {
			f := apitypes.MachineFailure(*i.Failure)
			item.LifecycleFailure = &f
		}
		out.Instances = append(out.Instances, item)
	}
	return out, nil
}

func authorizationOut(a *compute.Authorization) *apitypes.AwsAuthorizationGeneration {
	if a == nil {
		return nil
	}
	out := &apitypes.AwsAuthorizationGeneration{
		Generation: a.Generation, AuthorizationMode: apitypes.AwsAuthorizationGenerationAuthorizationMode(a.Mode),
		Phase: apitypes.AwsAuthorizationPhase(a.Phase), LastValidationStartedAt: a.ValidationStarted,
		LastValidatedAt: a.LastValidated, CreatedAt: a.CreatedAt, UpdatedAt: a.UpdatedAt,
	}
	if a.ErrorCode != nil {
		code := apitypes.AwsAuthorizationError(*a.ErrorCode)
		out.ErrorCode, out.ErrorMessage = &code, &a.ErrorMessage
	}
	if a.Mode == compute.ModeManagedStack {
		out.ManagedAuthorization = &struct {
			Generation      int     `json:"generation"`
			Region          string  `json:"region"`
			StackId         *string `json:"stack_id,omitempty"`
			StackName       string  `json:"stack_name"`
			TemplateSha256  string  `json:"template_sha256"`
			TemplateVersion string  `json:"template_version"`
		}{Generation: a.Generation, Region: a.Region, StackName: a.StackName, TemplateSha256: a.TemplateSHA256, TemplateVersion: a.TemplateVersion}
		if a.StackID != "" {
			out.ManagedAuthorization.StackId = &a.StackID
		}
	}
	return out
}

func stackOut(st *compute.StackAction) *apitypes.AwsStackAction {
	if st == nil {
		return nil
	}
	out := &apitypes.AwsStackAction{AccountId: st.AccountID, Region: st.Region, TemplateSha256: st.TemplateSHA256}
	out.Request.StackName, out.Request.TemplateBody = st.StackName, st.TemplateBody
	out.Request.Capabilities = []apitypes.AwsStackActionRequestCapabilities{apitypes.CAPABILITYNAMEDIAM}
	out.Request.OnFailure = apitypes.DELETE
	for _, p := range st.Parameters {
		out.Request.Parameters = append(out.Request.Parameters, apitypes.AwsStackParameter{ParameterKey: p[0], ParameterValue: p[1]})
	}
	return out
}

func (s *Server) connectionOut(conn compute.Connection) apitypes.AwsConnection {
	conn = s.owners.Compute.View(conn)
	out := apitypes.AwsConnection{
		Id: conn.ID, AccountId: conn.AWSAccountID, Phase: apitypes.AwsConnectionPhase(conn.Phase), Revision: conn.Revision,
		HostsWorkloads: conn.HostsWorkloads(), CanManageExistingCapacity: conn.ManagesCapacity(),
		AvailableActions: []apitypes.AwsConnectionAction{}, Detail: conn.Phase.Detail(), NextRetryAt: conn.NextRetryAt,
		ActiveAuthorization: authorizationOut(conn.Active), PendingAuthorization: authorizationOut(conn.Pending),
		RetiringAuthorization: authorizationOut(conn.Retiring), CreatedAt: conn.CreatedAt, UpdatedAt: conn.UpdatedAt,
	}
	for _, a := range conn.Actions() {
		out.AvailableActions = append(out.AvailableActions, apitypes.AwsConnectionAction(a))
	}
	switch {
	case conn.Stack != nil:
		out.CustomerAction = &apitypes.AwsCustomerAction{Label: "Create the connection stack", Stack: stackOut(conn.Stack)}
	case conn.ActionLabel != "":
		out.CustomerAction = &apitypes.AwsCustomerAction{Label: conn.ActionLabel}
		if conn.ActionURL != "" {
			out.CustomerAction.Url = &conn.ActionURL
		}
	}
	return out
}

func (s *Server) authorizationResponse(conn compute.Connection) apitypes.AwsConnectionAuthorization {
	out := apitypes.AwsConnectionAuthorization{Connection: s.connectionOut(conn)}
	if conn.Pending != nil {
		if conn.Pending.Mode == compute.ModeExistingRole {
			id := conn.Pending.ExternalID()
			out.Authorization.ExternalId = &id
		} else {
			out.Authorization.Stack = out.Connection.CustomerAction.Stack
		}
	}
	return out
}

// GetAwsConnection returns the account's AWS connection.
func (s *Server) GetAwsConnection(ctx context.Context, _ GetAwsConnectionRequestObject) (GetAwsConnectionResponseObject, error) {
	p, err := s.account(ctx, "read the AWS connection")
	if err != nil {
		return nil, err
	}
	conn, err := s.owners.Compute.AccountConnection(ctx, p.User)
	if err != nil {
		return nil, err
	}
	out := GetAwsConnection200JSONResponse{}
	if conn != nil {
		c := s.connectionOut(*conn)
		out.Connection = &c
	}
	return out, nil
}

// ConnectAws starts an AWS connection.
func (s *Server) ConnectAws(ctx context.Context, req ConnectAwsRequestObject) (ConnectAwsResponseObject, error) {
	p, err := s.account(ctx, "connect an AWS account")
	if err != nil {
		return nil, err
	}
	r := compute.ConnectRequest{AWSAccountID: req.Body.AccountId}
	if req.Body.RoleArn != nil {
		r.RoleARN = *req.Body.RoleArn
	}
	if req.Body.ExternalId != nil {
		r.ExternalID = *req.Body.ExternalId
	}
	if req.Body.Networks != nil {
		r.Networks = map[string]compute.Network{}
		for region, n := range *req.Body.Networks {
			network := compute.Network{VPCID: n.VpcId, SecurityGroupID: n.SecurityGroupId}
			for _, id := range n.SubnetIds {
				network.Subnets = append(network.Subnets, compute.Subnet{ID: id})
			}
			r.Networks[region] = network
		}
	}
	conn, err := s.owners.Compute.Connect(ctx, p.User, r)
	if err != nil {
		return nil, err
	}
	return ConnectAws201JSONResponse(s.authorizationResponse(conn)), nil
}

// DisconnectAws removes the account's AWS connection.
func (s *Server) DisconnectAws(ctx context.Context, _ DisconnectAwsRequestObject) (DisconnectAwsResponseObject, error) {
	p, err := s.account(ctx, "disconnect the AWS account")
	if err != nil {
		return nil, err
	}
	conn, err := s.owners.Compute.Disconnect(ctx, p.User)
	if err != nil {
		return nil, err
	}
	out := DisconnectAws202JSONResponse{}
	if conn != nil {
		c := s.connectionOut(*conn)
		out.Connection = &c
	}
	return out, nil
}

// ValidateAwsConnection checks the connection's authorization now.
func (s *Server) ValidateAwsConnection(ctx context.Context, _ ValidateAwsConnectionRequestObject) (ValidateAwsConnectionResponseObject, error) {
	p, err := s.account(ctx, "validate the AWS connection")
	if err != nil {
		return nil, err
	}
	conn, err := s.owners.Compute.Validate(ctx, p.User)
	if err != nil {
		return nil, err
	}
	return ValidateAwsConnection200JSONResponse(s.connectionOut(conn)), nil
}

// ReconnectAws starts a replacement authorization.
func (s *Server) ReconnectAws(ctx context.Context, req ReconnectAwsRequestObject) (ReconnectAwsResponseObject, error) {
	p, err := s.account(ctx, "reconnect the AWS account")
	if err != nil {
		return nil, err
	}
	role := ""
	if req.Body != nil && req.Body.RoleArn != nil {
		role = *req.Body.RoleArn
	}
	conn, err := s.owners.Compute.Reconnect(ctx, p.User, role)
	if err != nil {
		return nil, err
	}
	return ReconnectAws200JSONResponse(s.authorizationResponse(conn)), nil
}

// CancelAwsReconnect cancels a replacement authorization.
func (s *Server) CancelAwsReconnect(ctx context.Context, _ CancelAwsReconnectRequestObject) (CancelAwsReconnectResponseObject, error) {
	p, err := s.account(ctx, "reconnect the AWS account")
	if err != nil {
		return nil, err
	}
	conn, err := s.owners.Compute.CancelReconnect(ctx, p.User)
	if err != nil {
		return nil, err
	}
	return CancelAwsReconnect200JSONResponse(s.connectionOut(conn)), nil
}

// RetryAwsConnection retries the connection's pending step.
func (s *Server) RetryAwsConnection(ctx context.Context, _ RetryAwsConnectionRequestObject) (RetryAwsConnectionResponseObject, error) {
	p, err := s.account(ctx, "retry the AWS connection")
	if err != nil {
		return nil, err
	}
	conn, err := s.owners.Compute.Retry(ctx, p.User)
	if err != nil {
		return nil, err
	}
	return RetryAwsConnection200JSONResponse(s.connectionOut(conn)), nil
}
