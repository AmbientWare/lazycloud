package compute

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// DefaultJoinTTL is how long a machine join command works.
const DefaultJoinTTL = 30 * time.Minute

// Machine is a joined machine as its account sees it.
type Machine struct {
	ID             HostID
	Name           string
	Workspaces     []string
	Phase          Phase
	PhaseMessage   string
	PhaseAt        time.Time
	Failure        *Failure
	Connected      bool
	CPUMillis      int64
	MemoryBytes    int64
	GPUType        string
	GPUCount       int
	CapacityState  CapacityState
	CapacityReason string
	Preflight      []PreflightCheck
	AgentVersion   string
	LastSeenAt     *time.Time
	CreatedAt      time.Time
	UpdatedAt      time.Time
}

// Schedulable reports whether new work can be placed on the machine.
func (m Machine) Schedulable() bool {
	return m.Phase == PhaseReady && m.Connected && m.CapacityState == CapacityAvailable
}

// Remediation lists what the host owner must fix: the remediation of every
// failed check.
func (m Machine) Remediation() []string {
	var out []string
	for _, c := range m.Preflight {
		if !c.OK && c.Remediation != "" {
			out = append(out, c.Remediation)
		}
	}
	return out
}

// PreflightCheck is one check a host ran before joining.
type PreflightCheck struct {
	Name        string `json:"name"`
	OK          bool   `json:"ok"`
	Message     string `json:"message"`
	Severity    string `json:"severity"`
	Remediation string `json:"remediation"`
}

// JoinCommand is what a host runs to join as a machine.
type JoinCommand struct {
	Command   string
	ExpiresAt time.Time
	Machine   Machine
}

// MachineJoin is a request to mint a machine join command.
type MachineJoin struct {
	Account identity.UserID
	Name    string
	// Workspaces the machine serves; the caller checked the account owns
	// each.
	Workspaces []uuid.UUID
	GPUs       []string
	TTL        time.Duration
}

// CreateMachineJoin creates the named machine, or reuses one that never
// joined or failed its checks, sets the workspaces it serves and mints a
// single-use join token bound to it. Unused earlier tokens stop working.
func (c *Compute) CreateMachineJoin(ctx context.Context, req MachineJoin) (JoinCommand, error) {
	release, err := c.queries.TargetRelease(ctx)
	if errors.Is(err, pgx.ErrNoRows) {
		return JoinCommand{}, &UnavailableError{Message: "no agent release is published"}
	}
	if err != nil {
		return JoinCommand{}, fmt.Errorf("read agent release: %w", err)
	}
	ttl := req.TTL
	if ttl == 0 {
		ttl = DefaultJoinTTL
	}
	token, digest, err := identity.NewToken()
	if err != nil {
		return JoinCommand{}, err
	}
	account := uuid.UUID(req.Account)
	gpu := ""
	if len(req.GPUs) > 0 {
		gpu = req.GPUs[0]
	}
	var host uuid.UUID
	var expires time.Time
	err = pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		if ws, err := q.MachineNameConflict(ctx, MachineNameConflictParams{
			Name: req.Name, AccountID: account, WorkspaceIds: req.Workspaces,
		}); err == nil {
			return &ConflictError{Message: fmt.Sprintf("machine %q already serves workspace %q under another owner", req.Name, ws)}
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return fmt.Errorf("check machine name: %w", err)
		}
		existing, err := q.LockMachineByName(ctx, LockMachineByNameParams{AccountID: &account, Name: req.Name})
		switch {
		case errors.Is(err, pgx.ErrNoRows):
			host, err = q.InsertMachine(ctx, InsertMachineParams{Name: req.Name, AccountID: &account, GpuType: gpu})
			if isUniqueViolation(err) {
				return &ConflictError{Message: fmt.Sprintf("machine %q is already joined", req.Name)}
			}
			if err != nil {
				return fmt.Errorf("insert machine: %w", err)
			}
		case err != nil:
			return fmt.Errorf("lock machine: %w", err)
		case Phase(existing.Phase) != PhaseRequested && Phase(existing.Phase) != PhaseFailed:
			return &ConflictError{Message: fmt.Sprintf(
				"machine %q is already joined; remove it with `lazycloud machine remove` before joining another host under that name", req.Name)}
		default:
			host = existing.ID
			if err := q.ResetMachine(ctx, ResetMachineParams{ID: host, GpuType: gpu}); err != nil {
				return fmt.Errorf("reset machine: %w", err)
			}
		}
		if err := q.ReplaceMachineWorkspaces(ctx, ReplaceMachineWorkspacesParams{HostID: host, WorkspaceIds: req.Workspaces}); err != nil {
			return fmt.Errorf("set machine workspaces: %w", err)
		}
		if err := q.RevokeMachineJoinTokens(ctx, &host); err != nil {
			return fmt.Errorf("revoke join tokens: %w", err)
		}
		expires, err = q.InsertJoinToken(ctx, InsertJoinTokenParams{TokenHash: digest, TtlSeconds: ttl.Seconds(), HostID: &host})
		if err != nil {
			return fmt.Errorf("insert join token: %w", err)
		}
		return notifyMachines(ctx, tx, host)
	})
	if err != nil {
		return JoinCommand{}, fmt.Errorf("create machine join: %w", err)
	}
	machine, err := c.machine(ctx, HostID(host))
	if err != nil {
		return JoinCommand{}, err
	}
	return JoinCommand{Command: c.joinCommand(token, release), ExpiresAt: expires, Machine: machine}, nil
}

// joinCommand is the shell command that installs the release and joins with
// token. Off loopback it reruns the script under sudo when not root.
func (c *Compute) joinCommand(token string, release TargetReleaseRow) string {
	gateway := strings.TrimRight(c.config.InstallURL, "/")
	args := []string{"--gateway", shellQuote(gateway), "--server", shellQuote(c.config.ServerAddress)}
	if c.config.ServerTLS {
		args = append(args, "--server-tls")
	}
	args = append(args, "--join-token", shellQuote(token), "--agent-version", shellQuote(release.Version))
	if release.Sha256Amd64 != nil {
		args = append(args, "--agent-amd64-sha256", shellQuote(*release.Sha256Amd64))
	}
	if release.Sha256Arm64 != nil {
		args = append(args, "--agent-arm64-sha256", shellQuote(*release.Sha256Arm64))
	}
	fetch := "curl -fsSL " + shellQuote(gateway+"/install/agent") + " | "
	if loopback(gateway) {
		return fetch + "sh -s -- " + strings.Join(args, " ")
	}
	return fetch + `sh -c 'if [ "$(id -u)" -eq 0 ]; then exec sh -s -- "$@"; else exec sudo sh -s -- "$@"; fi' -- ` +
		strings.Join(args, " ")
}

func loopback(rawURL string) bool {
	u, err := url.Parse(rawURL)
	if err != nil {
		return false
	}
	host := u.Hostname()
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func shellQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

// AccountMachines lists the account's machines by name after the cursor
// name.
func (c *Compute) AccountMachines(ctx context.Context, account identity.UserID, after string, limit int) ([]Machine, error) {
	id := uuid.UUID(account)
	rows, err := c.queries.AccountMachines(ctx, AccountMachinesParams{AccountID: &id, AfterName: after, MaxRows: int32(limit)}) //nolint:gosec // The API caps limit.
	if err != nil {
		return nil, fmt.Errorf("list machines: %w", err)
	}
	out := make([]Machine, 0, len(rows))
	for _, row := range rows {
		out = append(out, machineOf(MachineByIDRow(row)))
	}
	return out, nil
}

// WorkspaceMachines lists the machines that serve a workspace.
func (c *Compute) WorkspaceMachines(ctx context.Context, workspace identity.WorkspaceID, after string, limit int) ([]Machine, error) {
	rows, err := c.queries.WorkspaceMachines(ctx, WorkspaceMachinesParams{
		WorkspaceID: uuid.UUID(workspace), AfterName: after, MaxRows: int32(limit), //nolint:gosec // The API caps limit.
	})
	if err != nil {
		return nil, fmt.Errorf("list workspace machines: %w", err)
	}
	out := make([]Machine, 0, len(rows))
	for _, row := range rows {
		out = append(out, machineOf(MachineByIDRow(row)))
	}
	return out, nil
}

func (c *Compute) machine(ctx context.Context, id HostID) (Machine, error) {
	row, err := c.queries.MachineByID(ctx, uuid.UUID(id))
	if errors.Is(err, pgx.ErrNoRows) {
		return Machine{}, ErrNotFound
	}
	if err != nil {
		return Machine{}, fmt.Errorf("read machine: %w", err)
	}
	return machineOf(row), nil
}

func machineOf(row MachineByIDRow) Machine {
	m := Machine{
		ID: HostID(row.ID), Name: row.Name, Workspaces: row.Workspaces,
		Phase: Phase(row.Phase), PhaseMessage: row.PhaseMessage, PhaseAt: row.PhaseAt,
		Connected: connected(row.State, row.LastSeenAt),
		CPUMillis: row.CpuMillis, MemoryBytes: row.MemoryBytes, GPUType: row.GpuType, GPUCount: int(row.GpuCount),
		CapacityState: CapacityState(row.CapacityState), CapacityReason: row.CapacityReason,
		AgentVersion: row.AgentVersion, LastSeenAt: row.LastSeenAt, CreatedAt: row.CreatedAt, UpdatedAt: row.UpdatedAt,
	}
	if row.Failure != nil {
		f := Failure(*row.Failure)
		m.Failure = &f
	}
	// The column holds what Enroll wrote, so it always decodes.
	_ = json.Unmarshal(row.Preflight, &m.Preflight)
	if m.Workspaces == nil {
		m.Workspaces = []string{}
	}
	return m
}

func connected(state string, lastSeen *time.Time) bool {
	return HostState(state) == HostOnline && lastSeen != nil && time.Since(*lastSeen) < LivenessTimeout
}

// UpdateMachineWorkspaces replaces the workspaces a machine serves. A
// workspace whose deployments still pin the machine cannot be dropped.
func (c *Compute) UpdateMachineWorkspaces(ctx context.Context, account identity.UserID, machine string, workspaces []uuid.UUID) (Machine, error) {
	acct := uuid.UUID(account)
	var host uuid.UUID
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, err := q.LockAccountMachine(ctx, LockAccountMachineParams{AccountID: &acct, Machine: machine})
		if errors.Is(err, pgx.ErrNoRows) || (err == nil && Phase(row.Phase) == PhaseDeleted) {
			return &NotFoundError{Message: fmt.Sprintf("machine not found: %s", machine)}
		}
		if err != nil {
			return fmt.Errorf("lock machine: %w", err)
		}
		host = row.ID
		if ws, err := q.MachineNameConflict(ctx, MachineNameConflictParams{Name: row.Name, AccountID: acct, WorkspaceIds: workspaces}); err == nil {
			return &ConflictError{Message: fmt.Sprintf("machine %q already serves workspace %q under another owner", row.Name, ws)}
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return fmt.Errorf("check machine name: %w", err)
		}
		current, err := q.MachineWorkspaceIDs(ctx, host)
		if err != nil {
			return fmt.Errorf("read machine workspaces: %w", err)
		}
		var dropped []uuid.UUID
		for _, ws := range current {
			if !containsUUID(workspaces, ws) {
				dropped = append(dropped, ws)
			}
		}
		if len(dropped) > 0 {
			pinned, err := q.PinnedDeployments(ctx, PinnedDeploymentsParams{WorkspaceIds: dropped, Name: row.Name})
			if err != nil {
				return fmt.Errorf("check pinned deployments: %w", err)
			}
			if len(pinned) > 0 {
				return &ConflictError{Message: fmt.Sprintf(
					"deployments in %s still name machine %q; remove them before dropping those workspaces", strings.Join(pinned, ", "), row.Name)}
			}
		}
		if err := q.ReplaceMachineWorkspaces(ctx, ReplaceMachineWorkspacesParams{HostID: host, WorkspaceIds: workspaces}); err != nil {
			return fmt.Errorf("set machine workspaces: %w", err)
		}
		return notifyMachines(ctx, tx, host)
	})
	if err != nil {
		return Machine{}, fmt.Errorf("update machine: %w", err)
	}
	return c.machine(ctx, HostID(host))
}

// RemoveMachine retires an account's machine by id or name: its host token
// stops working and its live containers stop in the same transaction, so
// their running attempts retry. Removing a removed machine succeeds.
func (c *Compute) RemoveMachine(ctx context.Context, account identity.UserID, machine string) error {
	acct := uuid.UUID(account)
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, err := q.LockAccountMachine(ctx, LockAccountMachineParams{AccountID: &acct, Machine: machine})
		if errors.Is(err, pgx.ErrNoRows) {
			return &NotFoundError{Message: fmt.Sprintf("machine not found: %s", machine)}
		}
		if err != nil {
			return fmt.Errorf("lock machine: %w", err)
		}
		if Phase(row.Phase) == PhaseDeleted {
			return nil
		}
		message := "Removed"
		if row.Enrolled {
			message = "Removed; the host's credential was revoked"
		}
		if err := q.RetireMachine(ctx, RetireMachineParams{ID: row.ID, Message: message}); err != nil {
			return fmt.Errorf("retire machine: %w", err)
		}
		if err := q.RevokeMachineJoinTokens(ctx, &row.ID); err != nil {
			return fmt.Errorf("revoke join tokens: %w", err)
		}
		if _, err := c.containers.StopHostContainers(ctx, tx, HostID(row.ID), "the machine was removed"); err != nil {
			return err
		}
		return notifyMachines(ctx, tx, row.ID)
	})
	if err != nil {
		return fmt.Errorf("remove machine: %w", err)
	}
	return nil
}

func containsUUID(list []uuid.UUID, id uuid.UUID) bool {
	for _, v := range list {
		if v == id {
			return true
		}
	}
	return false
}

// notifyMachines wakes dashboards and the host's session.
func notifyMachines(ctx context.Context, tx pgx.Tx, host uuid.UUID) error {
	if err := database.Notify(ctx, tx, database.ChannelHost, host.String()); err != nil {
		return err
	}
	return database.Notify(ctx, tx, ChannelCompute, host.String())
}
