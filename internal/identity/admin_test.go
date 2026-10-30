package identity

import (
	"errors"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestAccountAdministrationNeedsAnAdministratorAccount(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	ctx := t.Context()
	admin := f.account("admin@example.com", true)
	member := f.account("member@example.com", false)
	if _, err := f.id.CreateWorkspace(ctx, "acme", "admin@example.com"); err != nil {
		t.Fatal(err)
	}
	restricted := f.tokenPrincipal("admin@example.com", "acme")

	if _, err := f.id.SetUserRole(ctx, member, admin.User, PlatformMember); !errors.Is(err, ErrAdminRequired) {
		t.Fatalf("member demotes: %v", err)
	}
	var accountErr *AccountError
	if _, err := f.id.SetUserStatus(ctx, restricted, member.User, UserDisabled); !errors.As(err, &accountErr) {
		t.Fatalf("restricted disable: %v", err)
	}

	// Nobody changes their own account, so the last administrator stays.
	var conflict *ConflictError
	if _, err := f.id.SetUserRole(ctx, admin, admin.User, PlatformMember); !errors.As(err, &conflict) {
		t.Fatalf("self demote: %v", err)
	}
	if _, err := f.id.SetUserStatus(ctx, admin, admin.User, UserDisabled); !errors.As(err, &conflict) {
		t.Fatalf("self disable: %v", err)
	}

	promoted, err := f.id.SetUserRole(ctx, admin, member.User, PlatformAdministrator)
	if err != nil || !promoted.IsAdmin || promoted.Status != UserActive {
		t.Fatalf("promote: %+v %v", promoted, err)
	}
	if _, err := f.id.SetUserRole(ctx, admin, UserID(uuid.New()), PlatformMember); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown user: %v", err)
	}
}

func TestDisablingEndsCredentials(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	ctx := t.Context()
	admin := f.account("admin@example.com", true)
	sessionToken := f.sessionToken(gitHubAccount{ID: 9, Login: "dev", Email: "dev@example.com", Verified: true}, "code-dev")
	person, err := f.id.AuthenticateSession(ctx, sessionToken)
	if err != nil {
		t.Fatal(err)
	}
	apiToken, err := f.id.CreateToken(ctx, "dev@example.com", "", "ci")
	if err != nil {
		t.Fatal(err)
	}
	// Approved in the browser, not yet collected by the CLI.
	start, err := f.id.StartDeviceLogin(ctx, "cli")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.id.ApproveDeviceLogin(ctx, person, start.UserCode); err != nil {
		t.Fatal(err)
	}

	disabled, err := f.id.SetUserStatus(ctx, admin, person.User, UserDisabled)
	if err != nil || disabled.Status != UserDisabled {
		t.Fatalf("disable: %+v %v", disabled, err)
	}
	enabled, err := f.id.SetUserStatus(ctx, admin, person.User, UserActive)
	if err != nil || enabled.Status != UserActive {
		t.Fatalf("enable: %+v %v", enabled, err)
	}
	// Enabling again restores none of the credentials disabling ended.
	if _, err := f.id.Authenticate(ctx, apiToken); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("token after re-enable: %v", err)
	}
	if _, err := f.id.AuthenticateSession(ctx, sessionToken); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("session after re-enable: %v", err)
	}
	if poll, err := f.id.PollDeviceLogin(ctx, start.DeviceCode); err != nil || poll.Status != DeviceExpired || poll.Token != "" {
		t.Fatalf("device poll after re-enable: %+v %v", poll, err)
	}
	var live int
	if err := f.pool.QueryRow(ctx, `select (select count(*) from api_tokens where user_id = $1 and revoked_at is null)
		+ (select count(*) from sessions where user_id = $1)`, person.User.String()).Scan(&live); err != nil || live != 0 {
		t.Fatalf("live credentials %d %v", live, err)
	}
}

func TestConcurrentMutualDemotionKeepsAnAdministrator(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	ctx := t.Context()
	a := f.account("a@example.com", true)
	b := f.account("b@example.com", true)

	// Hold both rows so the two demotions queue on the locks together.
	holder, err := f.pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = holder.Rollback(ctx) }()
	if _, err := holder.Exec(ctx, `select id from users for update`); err != nil {
		t.Fatal(err)
	}
	errs := make([]error, 2)
	var wg sync.WaitGroup
	for n, pair := range [][2]Principal{{a, b}, {b, a}} {
		wg.Go(func() { _, errs[n] = f.id.SetUserRole(ctx, pair[0], pair[1].User, PlatformMember) })
	}
	for deadline := time.Now().Add(10 * time.Second); ; {
		var waiting int
		if err := f.pool.QueryRow(ctx, `select count(*) from pg_stat_activity
			where datname = current_database() and wait_event_type = 'Lock'`).Scan(&waiting); err != nil {
			t.Fatal(err)
		}
		if waiting == 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("%d demotions waiting", waiting)
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err := holder.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	wg.Wait()

	refused := 0
	for _, err := range errs {
		switch {
		case errors.Is(err, ErrAdminRequired):
			refused++
		case err != nil:
			t.Fatal(err)
		}
	}
	var admins int
	if err := f.pool.QueryRow(ctx, `select count(*) from users where is_admin and status = 'active'`).Scan(&admins); err != nil {
		t.Fatal(err)
	}
	if refused != 1 || admins != 1 {
		t.Fatalf("refused %d, administrators left %d", refused, admins)
	}
}

func TestFeedbackIsReadAndDeletedOnlyByAdministrators(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	ctx := t.Context()
	admin := f.account("admin@example.com", true)
	member := f.account("member@example.com", false)

	var invalid *InvalidError
	if err := f.id.SubmitFeedback(ctx, member, FeedbackBug, strings.Repeat("x", FeedbackMaxLength+1)); !errors.As(err, &invalid) {
		t.Fatalf("too long: %v", err)
	}
	if err := f.id.SubmitFeedback(ctx, member, FeedbackBug, " \n "); !errors.As(err, &invalid) {
		t.Fatalf("blank: %v", err)
	}
	if err := f.id.SubmitFeedback(ctx, member, FeedbackBug, "  deploys hang  "); err != nil {
		t.Fatal(err)
	}
	if err := f.id.SubmitFeedback(ctx, member, FeedbackFeature, strings.Repeat("é", FeedbackMaxLength)); err != nil {
		t.Fatal(err)
	}

	if _, err := f.id.ListFeedback(ctx, member, FeedbackFilter{}, nil, 10); !errors.Is(err, ErrAdminRequired) {
		t.Fatalf("member lists: %v", err)
	}
	first, err := f.id.ListFeedback(ctx, admin, FeedbackFilter{}, nil, 1)
	if err != nil || len(first.Feedback) != 1 || first.Next == nil || first.Feedback[0].Kind != FeedbackFeature {
		t.Fatalf("first page: %+v %v", first, err)
	}
	second, err := f.id.ListFeedback(ctx, admin, FeedbackFilter{}, first.Next, 1)
	if err != nil || len(second.Feedback) != 1 || second.Next != nil {
		t.Fatalf("second page: %+v %v", second, err)
	}
	if got := second.Feedback[0]; got.Message != "deploys hang" || got.Author.ID != member.User {
		t.Fatalf("feedback: %+v", got)
	}

	if err := f.id.DeleteFeedback(ctx, member, second.Feedback[0].ID); !errors.Is(err, ErrAdminRequired) {
		t.Fatalf("member deletes: %v", err)
	}
	if err := f.id.DeleteFeedback(ctx, admin, second.Feedback[0].ID); err != nil {
		t.Fatal(err)
	}
	if err := f.id.DeleteFeedback(ctx, admin, second.Feedback[0].ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("delete twice: %v", err)
	}
	if err := f.id.SubmitFeedback(ctx, member, FeedbackSupport, "how do volumes work?"); err != nil {
		t.Fatal(err)
	}
	bug, support := FeedbackBug, FeedbackSupport
	if bugs, err := f.id.ListFeedback(ctx, admin, FeedbackFilter{Kind: &bug}, nil, 10); err != nil || len(bugs.Feedback) != 0 {
		t.Fatalf("bugs after delete: %+v %v", bugs, err)
	}
	if asks, err := f.id.ListFeedback(ctx, admin, FeedbackFilter{Kind: &support}, nil, 10); err != nil || len(asks.Feedback) != 1 || asks.Feedback[0].Kind != FeedbackSupport {
		t.Fatalf("support: %+v %v", asks, err)
	}
	for search, want := range map[string]int{"VOLUMES": 1, "member@": 2, "100%": 0} {
		found, err := f.id.ListFeedback(ctx, admin, FeedbackFilter{Search: search}, nil, 10)
		if err != nil || len(found.Feedback) != want {
			t.Fatalf("search %q: %+v %v", search, found, err)
		}
	}
	feature := FeedbackFeature
	left, err := f.id.ListFeedback(ctx, admin, FeedbackFilter{Kind: &feature}, nil, 10)
	if err != nil || len(left.Feedback) != 1 || left.Feedback[0].ID != first.Feedback[0].ID {
		t.Fatalf("after delete: %+v %v", left, err)
	}
}

// Concurrent submissions count each other, so a burst cannot pass the
// hourly limit; deleting feedback does not refund its sender; one account's
// limit leaves another's alone; and the daily limit holds across hours.
func TestFeedbackIsRateLimitedPerAccount(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	ctx := t.Context()
	admin := f.account("admin@example.com", true)
	spammer := f.account("spammer@example.com", false)
	other := f.account("other@example.com", false)

	const burst = FeedbackPerHour * 3
	var (
		wg              sync.WaitGroup
		mu              sync.Mutex
		accepted, limit int
	)
	for range burst {
		wg.Go(func() {
			err := f.id.SubmitFeedback(ctx, spammer, FeedbackSupport, "help")
			var limited *RateLimitedError
			mu.Lock()
			defer mu.Unlock()
			switch {
			case err == nil:
				accepted++
			case errors.As(err, &limited):
				limit++
			default:
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if accepted != FeedbackPerHour || limit != burst-FeedbackPerHour {
		t.Fatalf("accepted %d and limited %d of %d", accepted, limit, burst)
	}
	var limited *RateLimitedError
	// Deleting the burst does not hand its sender more sends until
	// housekeeping removes it a day later.
	sent, err := f.id.ListFeedback(ctx, admin, FeedbackFilter{}, nil, burst)
	if err != nil {
		t.Fatal(err)
	}
	for _, item := range sent.Feedback {
		if err := f.id.DeleteFeedback(ctx, admin, item.ID); err != nil {
			t.Fatal(err)
		}
	}
	if err := f.id.SubmitFeedback(ctx, spammer, FeedbackSupport, "help"); !errors.As(err, &limited) {
		t.Fatalf("after deletion: %v", err)
	}
	if err := f.id.Housekeeping(ctx, slog.New(slog.DiscardHandler)); err != nil {
		t.Fatal(err)
	}
	if err := f.id.SubmitFeedback(ctx, spammer, FeedbackSupport, "help"); !errors.As(err, &limited) {
		t.Fatalf("housekeeping within the day: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `update feedback set deleted_at = now() - interval '25 hours', created_at = now() - interval '25 hours' where deleted_at is not null`); err != nil {
		t.Fatal(err)
	}
	if err := f.id.Housekeeping(ctx, slog.New(slog.DiscardHandler)); err != nil {
		t.Fatal(err)
	}
	var kept int
	if err := f.pool.QueryRow(ctx, `select count(*) from feedback`).Scan(&kept); err != nil || kept != 0 {
		t.Fatalf("after a day, %d rows kept: %v", kept, err)
	}

	if err := f.id.SubmitFeedback(ctx, other, FeedbackSupport, "help"); err != nil {
		t.Fatalf("another account: %v", err)
	}

	// A day's worth sent earlier today fills the daily limit, though none
	// of it is from the last hour.
	if _, err := f.pool.Exec(ctx, `insert into feedback (user_id, kind, message, created_at)
		select $1, 'support', 'help', now() - interval '2 hours' from generate_series(1, $2)`,
		uuid.UUID(other.User), FeedbackPerDay); err != nil {
		t.Fatal(err)
	}
	if err := f.id.SubmitFeedback(ctx, other, FeedbackSupport, "help"); !errors.As(err, &limited) {
		t.Fatalf("past the daily limit: %v", err)
	}
}
