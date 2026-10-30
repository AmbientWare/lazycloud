package identity

import (
	"context"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// FeedbackKind is what a piece of feedback is about.
type FeedbackKind string

const (
	// FeedbackFeature asks for something the product does not do.
	FeedbackFeature FeedbackKind = "feature"
	// FeedbackBug reports something that does not work.
	FeedbackBug FeedbackKind = "bug"
	// FeedbackSupport asks for help.
	FeedbackSupport FeedbackKind = "support"
)

func (k FeedbackKind) check() error {
	switch k {
	case FeedbackFeature, FeedbackBug, FeedbackSupport:
		return nil
	}
	return &InvalidError{Message: fmt.Sprintf("unknown feedback kind %q", k)}
}

// FeedbackMaxLength bounds a feedback message, in characters.
const FeedbackMaxLength = 2000

// One account sends at most FeedbackPerHour pieces of feedback in any hour
// and FeedbackPerDay in any day.
const (
	FeedbackPerHour = 5
	FeedbackPerDay  = 20
)

// RateLimitedError means the caller sent as much as a limit allows and may
// try again later.
type RateLimitedError struct{ Message string }

func (e *RateLimitedError) Error() string { return e.Message }

// FeedbackID identifies a piece of feedback.
type FeedbackID uuid.UUID

// Feedback is what a person told the platform's administrators.
type Feedback struct {
	ID        FeedbackID
	Kind      FeedbackKind
	Message   string
	Author    User
	CreatedAt time.Time
}

// FeedbackPage is one page of feedback, newest first. Next is the cursor of
// the following page, absent on the last.
type FeedbackPage struct {
	Feedback []Feedback
	Next     *FeedbackID
}

// SubmitFeedback records the caller's feedback for the administrators,
// within the account's hourly and daily limits.
func (i *Identity) SubmitFeedback(ctx context.Context, p Principal, kind FeedbackKind, message string) error {
	if err := p.requireAccount("send feedback"); err != nil {
		return err
	}
	if err := kind.check(); err != nil {
		return err
	}
	message = strings.TrimSpace(message)
	if message == "" || utf8.RuneCountInString(message) > FeedbackMaxLength {
		return &InvalidError{Message: fmt.Sprintf("feedback is 1 to %d characters", FeedbackMaxLength)}
	}
	user := uuid.UUID(p.User)
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		if err := q.LockFeedbackAuthor(ctx, user); err != nil {
			return fmt.Errorf("lock user: %w", err)
		}
		recent, err := q.RecentFeedback(ctx, user)
		if err != nil {
			return fmt.Errorf("count feedback: %w", err)
		}
		switch {
		case recent.LastHour >= FeedbackPerHour:
			return &RateLimitedError{Message: fmt.Sprintf("you can send %d pieces of feedback an hour; try again later", FeedbackPerHour)}
		case recent.LastDay >= FeedbackPerDay:
			return &RateLimitedError{Message: fmt.Sprintf("you can send %d pieces of feedback a day; try again tomorrow", FeedbackPerDay)}
		}
		if err := q.InsertFeedback(ctx, InsertFeedbackParams{UserID: user, Kind: string(kind), Message: message}); err != nil {
			return fmt.Errorf("insert feedback: %w", err)
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("submit feedback: %w", err)
	}
	return nil
}

// DeleteFeedback hides a piece of feedback from administrators, for
// platform administrators. It counts against its author's rate limit until
// Housekeeping removes it.
func (i *Identity) DeleteFeedback(ctx context.Context, p Principal, id FeedbackID) error {
	if err := requireAccountAdmin(p); err != nil {
		return err
	}
	deleted, err := i.queries.DeleteFeedback(ctx, uuid.UUID(id))
	if err != nil {
		return fmt.Errorf("delete feedback: %w", err)
	}
	if deleted == 0 {
		return ErrNotFound
	}
	return nil
}

// FeedbackFilter narrows the feedback administrators list. Search matches
// the message and the author's email, display name or GitHub login.
type FeedbackFilter struct {
	Kind   *FeedbackKind
	Search string
}

// ListFeedback pages through every account's feedback, newest first, for
// platform administrators.
func (i *Identity) ListFeedback(ctx context.Context, p Principal, f FeedbackFilter, before *FeedbackID, limit int) (FeedbackPage, error) {
	if err := requireAccountAdmin(p); err != nil {
		return FeedbackPage{}, err
	}
	params := ListFeedbackParams{Before: (*uuid.UUID)(before), RowLimit: int32(limit + 1)} //nolint:gosec // The API bounds limit.
	if f.Search != "" {
		params.Pattern = "%" + strings.NewReplacer(`\`, `\\`, "%", `\%`, "_", `\_`).Replace(f.Search) + "%"
	}
	if f.Kind != nil {
		if err := f.Kind.check(); err != nil {
			return FeedbackPage{}, err
		}
		kind := string(*f.Kind)
		params.Kind = &kind
	}
	rows, err := i.queries.ListFeedback(ctx, params)
	if err != nil {
		return FeedbackPage{}, fmt.Errorf("list feedback: %w", err)
	}
	var page FeedbackPage
	for n, row := range rows {
		if n == limit {
			last := page.Feedback[n-1].ID
			page.Next = &last
			break
		}
		page.Feedback = append(page.Feedback, Feedback{
			ID: FeedbackID(row.ID), Kind: FeedbackKind(row.Kind), Message: row.Message, CreatedAt: row.CreatedAt,
			Author: User{
				ID: UserID(row.UserID), Email: deref(row.Email), DisplayName: row.DisplayName, AvatarURL: row.AvatarUrl,
				GitHubLogin: row.GithubLogin, IsAdmin: row.IsAdmin, Status: UserStatus(row.Status), CreatedAt: row.UserCreatedAt,
			},
		})
	}
	return page, nil
}
