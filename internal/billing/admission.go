package billing

import (
	"context"
	"errors"
	"fmt"
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
}

// Grant is what Admit allows.
type Grant struct {
	// Start is how many of the requested containers may start.
	Start int
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
	return s, nil
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
	if req.Start == 0 && !req.Cold {
		return Grant{}, nil
	}
	live, err := q.OwnerLiveContainers(ctx, owner)
	if err != nil {
		return Grant{}, fmt.Errorf("count live containers: %w", err)
	}
	limit := s.entitlements.MaxCPUContainers
	if req.Cold && int(live) >= limit {
		return Grant{}, &LimitError{Message: fmt.Sprintf(
			"this account already has %d containers running or queued, which is the most its plan allows (%d)", live, limit)}
	}
	return Grant{Start: max(0, min(req.Start, limit-int(live)))}, nil
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
	if l := s.entitlements.MaxWorkspaces; !l.Unlimited && int(owned) >= l.Max {
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
	s, err := readStanding(ctx, q, owner)
	if err != nil {
		return err
	}
	if s.changePending {
		return planChangePending()
	}
	limit := s.entitlements.MaxMembers
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
