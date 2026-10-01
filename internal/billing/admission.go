package billing

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// Request is work a workspace wants to take on.
type Request struct {
	Workspace uuid.UUID
	// Start is how many containers the caller creates now in its
	// transaction; Admit grants up to that many under the account's
	// concurrency limit.
	Start int
	// Cold marks work with no container to run on: Admit refuses it when
	// the account already runs the most containers its plan allows.
	Cold bool
	// GPUs is the cards each container holds; 0 is a CPU container. Such
	// containers count against the GPU pool by their cards instead of the
	// CPU pool.
	GPUs int
	// GPUModels are the models the work accepts, in preference order; empty
	// or GPUAny accepts any model the account may use.
	GPUModels []GPUType
	// Pinned marks placement in a chosen region or zone, which needs a plan
	// with region selection.
	Pinned bool
}

// GPUAny accepts whichever model has capacity.
const GPUAny GPUType = "any"

// Grant is what Admit allows.
type Grant struct {
	// Start is how many of the requested containers may start.
	Start int
	// GPUModels are the models placement may give a GPU container: the
	// requested ones the account may use, every allowed model for any.
	GPUModels []GPUType
}

// standing is an account's billing state as admission reads it.
type standing struct {
	user          uuid.UUID
	plan          Plan
	status        string
	hasCard       bool
	complimentary bool
	limit         *int64
	balance       int64
	monthSpent    int64
	changePending bool
	entitlements  Entitlements
	// limits are the workspace and member limits additions are held to.
	limits Entitlements
}

// readStanding returns the account of user, creating it on first use.
func readStanding(ctx context.Context, q *Queries, user uuid.UUID) (standing, error) {
	if err := ensureAccount(ctx, q, user); err != nil {
		return standing{}, err
	}
	row, err := q.AccountStanding(ctx, user)
	if err != nil {
		return standing{}, fmt.Errorf("read billing account: %w", err)
	}
	plan, err := planOfTerms(TermsVersion(row.TermsVersion))
	if err != nil {
		return standing{}, err
	}
	s := standing{
		user: user, plan: plan, status: row.Status, hasCard: row.PaymentMethodAttachedAt != nil,
		complimentary: row.ComplimentarySince != nil, limit: row.MonthlyUsageLimitNanos,
		balance: row.BalanceNanos - row.AccruedNanos, changePending: row.PlanChangePending,
	}
	// A month the rollup has not reached yet has spent nothing but what is
	// accruing.
	if row.MonthStartedAt.Equal(monthStart(row.Now)) {
		s.monthSpent = row.MonthSpentNanos
	}
	s.monthSpent += row.AccruedNanos
	s.entitlements, err = accountEntitlements(plan, s.hasCard, s.complimentary)
	if err != nil {
		return standing{}, err
	}
	s.limits = s.entitlements
	if row.ScheduledTermsVersion != nil && !s.complimentary {
		// While a cheaper plan is scheduled, additions are held to the
		// stricter of the two, so the account fits it when it applies.
		next, err := planOfTerms(TermsVersion(*row.ScheduledTermsVersion))
		if err != nil {
			return standing{}, err
		}
		s.limits.MaxWorkspaces = stricter(s.limits.MaxWorkspaces, next.Entitlements.MaxWorkspaces)
		s.limits.MaxMembers = stricter(s.limits.MaxMembers, next.Entitlements.MaxMembers)
	}
	return s, nil
}

func stricter(a, b Limit) Limit {
	switch {
	case a.Unlimited:
		return b
	case b.Unlimited:
		return a
	}
	return Limit{Max: min(a.Max, b.Max)}
}

// fundsRefusal is why the account may not start billed work, or nil.
func (s standing) fundsRefusal() error {
	switch {
	case s.complimentary:
		return nil
	case s.status == statusPastDue:
		return &PaymentRequiredError{Message: "a payment for this account did not go through; update the card on file to start new work"}
	case s.balance <= 0:
		return &PaymentRequiredError{Message: "add credit before starting more billed work"}
	case s.limit != nil && s.monthSpent >= *s.limit:
		return &PaymentRequiredError{Message: "the monthly usage limit has been reached"}
	}
	return nil
}

const statusPastDue = "past_due"

// Admit decides, inside the caller's transaction, whether the workspace's
// billing account may take on req. It refuses work the account cannot pay
// for with a PaymentRequiredError and cold work at the concurrency limit
// with a LimitError. Starting containers takes the account's container lock
// for the rest of tx, so concurrent starts count each other.
func Admit(ctx context.Context, tx pgx.Tx, req Request) (Grant, error) {
	q := New(tx)
	owner, err := q.WorkspaceOwner(ctx, req.Workspace)
	if err != nil {
		return Grant{}, fmt.Errorf("read workspace owner: %w", err)
	}
	if req.Start > 0 {
		if err := q.LockAccountContainers(ctx, owner); err != nil {
			return Grant{}, fmt.Errorf("lock account containers: %w", err)
		}
	}
	s, err := readStanding(ctx, q, owner)
	if err != nil {
		return Grant{}, err
	}
	if err := s.fundsRefusal(); err != nil {
		return Grant{}, err
	}
	if req.Pinned && !s.entitlements.RegionSelection {
		return Grant{}, &PaymentRequiredError{Message: "region or availability zone selection requires the Team plan"}
	}
	var grant Grant
	if req.GPUs > 0 {
		models, err := s.entitlements.gpuModels(req.GPUModels)
		if err != nil {
			return Grant{}, err
		}
		grant.GPUModels = models
	}
	if req.Start == 0 && !req.Cold {
		return grant, nil
	}
	live, err := q.OwnerLiveContainers(ctx, owner)
	if err != nil {
		return Grant{}, fmt.Errorf("count live containers: %w", err)
	}
	if req.GPUs > 0 {
		limit := s.entitlements.MaxGPUs
		if req.Cold && int(live.Gpus)+req.GPUs > limit {
			return Grant{}, &LimitError{Message: fmt.Sprintf(
				"this account already holds %d GPUs across the containers it is running or has queued, and %d more would pass the most its plan allows (%d)",
				live.Gpus, req.GPUs, limit)}
		}
		grant.Start = max(0, min(req.Start, (limit-int(live.Gpus))/req.GPUs))
		return grant, nil
	}
	limit := s.entitlements.MaxCPUContainers
	if req.Cold && int(live.CpuContainers) >= limit {
		return Grant{}, &LimitError{Message: fmt.Sprintf(
			"this account already has %d containers running or queued, which is the most its plan allows (%d)", live.CpuContainers, limit)}
	}
	grant.Start = max(0, min(req.Start, limit-int(live.CpuContainers)))
	return grant, nil
}

// gpuModels narrows requested models to the ones the account may use. A
// request for any model, or for none by name, gets every allowed model; a
// named model the account may not use is refused.
func (e Entitlements) gpuModels(requested []GPUType) ([]GPUType, error) {
	var named []GPUType
	for _, m := range requested {
		if m == GPUAny {
			return e.GPUTypes, nil
		}
		if !slices.Contains(e.GPUTypes, m) {
			return nil, &PaymentRequiredError{Message: fmt.Sprintf(
				"add a payment method to use %s; without a saved card, this account can use %s", m, joinModels(e.GPUTypes))}
		}
		named = append(named, m)
	}
	if len(named) == 0 {
		return e.GPUTypes, nil
	}
	return named, nil
}

func joinModels(models []GPUType) string {
	names := make([]string, len(models))
	for n, m := range models {
		names[n] = string(m)
	}
	return strings.Join(names, ", ")
}

// planChangePending refuses plan-limited additions while a change is
// settling, because the limit they would be held to is about to change.
func planChangePending() error {
	return &ConflictError{Message: "this account cannot add plan-limited resources while a plan change is pending"}
}

// AdmitWorkspace applies the owner's workspace limit to a new workspace,
// inside the creating transaction. An account's first workspace is always
// allowed.
func AdmitWorkspace(ctx context.Context, tx pgx.Tx, owner uuid.UUID) error {
	q := New(tx)
	// Concurrent additions count each other.
	if err := q.LockAccountContainers(ctx, owner); err != nil {
		return fmt.Errorf("lock account: %w", err)
	}
	owned, err := q.OwnedWorkspaceCount(ctx, owner)
	if err != nil {
		return fmt.Errorf("count owned workspaces: %w", err)
	}
	if owned == 0 {
		return nil
	}
	s, err := readStanding(ctx, q, owner)
	if err != nil {
		return err
	}
	if s.changePending {
		return planChangePending()
	}
	if l := s.limits.MaxWorkspaces; !l.Unlimited && int(owned) >= l.Max {
		return &LimitError{Message: fmt.Sprintf(
			"this account already owns %d workspaces, which is the most its plan allows (%d)", owned, l.Max)}
	}
	return nil
}

// Candidate is who would join a workspace: an invited email, or the
// signed-in user accepting.
type Candidate struct {
	Email string
	User  *uuid.UUID
}

// AdmitMember applies the workspace owner's member limit, counted in
// distinct people across every workspace the owner holds, the owner
// included. An invitation holds a seat while it is open, so sending one
// counts open offers; accepting one counts members. Someone already in
// another of the owner's workspaces takes no new seat.
func AdmitMember(ctx context.Context, tx pgx.Tx, workspace uuid.UUID, who Candidate) error {
	q := New(tx)
	owner, err := q.WorkspaceOwner(ctx, workspace)
	if err != nil {
		return fmt.Errorf("read workspace owner: %w", err)
	}
	// Concurrent invitations and acceptances count each other.
	if err := q.LockAccountContainers(ctx, owner); err != nil {
		return fmt.Errorf("lock account: %w", err)
	}
	s, err := readStanding(ctx, q, owner)
	if err != nil {
		return err
	}
	if s.changePending {
		return planChangePending()
	}
	limit := s.limits.MaxMembers
	if limit.Unlimited {
		return nil
	}
	var seated bool
	if who.User != nil {
		seated, err = q.OwnerHasMember(ctx, OwnerHasMemberParams{OwnerID: owner, MemberID: *who.User})
	} else {
		seated, err = q.OwnerHasMemberEmail(ctx, OwnerHasMemberEmailParams{OwnerID: owner, Email: strings.ToLower(who.Email)})
	}
	if err != nil {
		return fmt.Errorf("check membership: %w", err)
	}
	if seated {
		return nil
	}
	members, err := q.OwnerMemberCount(ctx, owner)
	if err != nil {
		return fmt.Errorf("count members: %w", err)
	}
	var offers int32
	if who.User == nil {
		if offers, err = q.OwnerOpenInvitations(ctx, owner); err != nil {
			return fmt.Errorf("count open invitations: %w", err)
		}
	}
	if int(members+offers) >= limit.Max {
		held := ""
		if offers > 0 {
			held = fmt.Sprintf(" and %d open invitations", offers)
		}
		return &LimitError{Message: fmt.Sprintf(
			"this account already has %d members%s, which is the most its plan allows (%d)", members, held, limit.Max)}
	}
	return nil
}

// AdmitDisk applies the plan's disk allowance inside the transaction that
// creates or grows a disk. declaredBytes is what the workspace's live disks
// would declare in total; Free allows none. The account must be able to pay
// for the disk.
func AdmitDisk(ctx context.Context, tx pgx.Tx, workspace uuid.UUID, declaredBytes int64) error {
	q := New(tx)
	owner, err := q.WorkspaceOwner(ctx, workspace)
	if err != nil {
		return fmt.Errorf("read workspace owner: %w", err)
	}
	s, err := readStanding(ctx, q, owner)
	if err != nil {
		return err
	}
	if err := s.fundsRefusal(); err != nil {
		return err
	}
	limit := int64(s.entitlements.MaxWorkspaceDiskGiB) * bytesPerGiB
	if declaredBytes > limit {
		return &LimitError{Message: fmt.Sprintf("this workspace's disks would declare %s GiB, more than its plan allows (%d GiB)",
			strings.TrimSuffix(fmt.Sprintf("%.1f", float64(declaredBytes)/float64(bytesPerGiB)), ".0"), s.entitlements.MaxWorkspaceDiskGiB)}
	}
	return nil
}

// AdmitCustomDomain refuses a custom domain to a workspace whose owner's
// plan has none; Team and Business include them.
func AdmitCustomDomain(ctx context.Context, tx pgx.Tx, workspace uuid.UUID) error {
	q := New(tx)
	owner, err := q.WorkspaceOwner(ctx, workspace)
	if err != nil {
		return fmt.Errorf("read workspace owner: %w", err)
	}
	return admitCapability(ctx, q, owner, func(e Entitlements) bool { return e.CustomDomains }, "custom domains require the Team plan")
}

// AdmitAccountCustomDomain refuses registering a custom domain to an
// account whose plan has none; registrations belong to accounts.
func AdmitAccountCustomDomain(ctx context.Context, tx pgx.Tx, user uuid.UUID) error {
	return admitCapability(ctx, New(tx), user, func(e Entitlements) bool { return e.CustomDomains }, "custom domains require the Team plan")
}

// AdmitConnectedCloud refuses connecting a cloud account to an account whose
// plan does not include it; Business does.
func AdmitConnectedCloud(ctx context.Context, tx pgx.Tx, user uuid.UUID) error {
	return admitCapability(ctx, New(tx), user, func(e Entitlements) bool { return e.ConnectedCloud }, "connected cloud accounts require the Business plan")
}

func admitCapability(ctx context.Context, q *Queries, user uuid.UUID, has func(Entitlements) bool, refusal string) error {
	s, err := readStanding(ctx, q, user)
	if err != nil {
		return err
	}
	if s.changePending {
		return planChangePending()
	}
	if !has(s.entitlements) {
		return &PaymentRequiredError{Message: refusal}
	}
	return nil
}

// Retention is how long logs and artifacts of the workspace are kept, from
// its owner's plan.
func Retention(ctx context.Context, db DBTX, workspace uuid.UUID) (time.Duration, error) {
	q := New(db)
	owner, err := q.WorkspaceOwner(ctx, workspace)
	if err != nil {
		return 0, fmt.Errorf("read workspace owner: %w", err)
	}
	row, err := q.AccountStanding(ctx, owner)
	free, ferr := PlanFor(PlanFree)
	if ferr != nil {
		return 0, ferr
	}
	if errors.Is(err, pgx.ErrNoRows) {
		return time.Duration(free.Entitlements.RetentionDays) * 24 * time.Hour, nil
	}
	if err != nil {
		return 0, fmt.Errorf("read billing account: %w", err)
	}
	plan, err := planOfTerms(TermsVersion(row.TermsVersion))
	if err != nil {
		return 0, err
	}
	e, err := accountEntitlements(plan, row.PaymentMethodAttachedAt != nil, row.ComplimentarySince != nil)
	if err != nil {
		return 0, err
	}
	return time.Duration(e.RetentionDays) * 24 * time.Hour, nil
}
