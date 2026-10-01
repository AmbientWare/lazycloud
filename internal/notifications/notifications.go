// Package notifications owns transactional email: an outbox written in the
// transaction of the change a message announces, delivery through Resend
// with retries, and the delivery reports Resend sends back.
package notifications

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/sync/errgroup"

	"github.com/AmbientWare/lazycloud/internal/database"
)

// Channel wakes delivery when a message is queued; the payload is empty.
const Channel database.Channel = "lc_email"

// MessageID identifies an outbox message.
type MessageID uuid.UUID

func (id MessageID) String() string { return uuid.UUID(id).String() }

// DeliveryState is what became of a message.
type DeliveryState string

const (
	// StateQueued: written, not yet accepted by the provider.
	StateQueued DeliveryState = "queued"
	// StateSent: the provider accepted it; nothing says it arrived yet.
	StateSent DeliveryState = "sent"
	// StateDelivered: the recipient's server took it.
	StateDelivered DeliveryState = "delivered"
	// StateBounced: the recipient's server refused it.
	StateBounced DeliveryState = "bounced"
	// StateComplained: delivered and marked as spam.
	StateComplained DeliveryState = "complained"
	// StateFailed: delivery gave up before the provider accepted it.
	StateFailed DeliveryState = "failed"
	// StateDiscarded: withdrawn before it was sent because its content
	// stopped being true.
	StateDiscarded DeliveryState = "discarded"
)

// Email is one complete message. Both bodies are required: clients that do
// not render HTML show the text.
type Email struct {
	To      string
	Subject string
	HTML    string
	Text    string
}

// Delivery policy. With these values a message gets about 20 minutes of
// retries; the failures that outlast that (a rejected key, an unverified
// domain) need an operator, not more waiting.
const (
	MaxAttempts   = 8
	BodyRetention = 48 * time.Hour
	retryBase     = 10 * time.Second
	retryCap      = 10 * time.Minute
	// leaseDuration outlasts a pass: batchSize messages at sendConcurrency,
	// each bounded by the sender's timeout.
	leaseDuration   = 2 * time.Minute
	batchSize       = 20
	sendConcurrency = 4
	purgeBatch      = 1000
)

// Enqueue queues email in tx, so it is sent exactly when tx commits.
func Enqueue(ctx context.Context, tx pgx.Tx, email Email) (MessageID, error) {
	id, err := New(tx).EnqueueEmail(ctx, EnqueueEmailParams{
		Recipient: email.To, Subject: email.Subject, Html: email.HTML, BodyText: email.Text,
	})
	if err != nil {
		return MessageID{}, fmt.Errorf("enqueue email: %w", err)
	}
	if err := database.Notify(ctx, tx, Channel, ""); err != nil {
		return MessageID{}, err
	}
	return MessageID(id), nil
}

// Discard withdraws queued messages in tx. A message already handed to the
// provider cannot be recalled.
func Discard(ctx context.Context, tx pgx.Tx, ids []MessageID) error {
	if len(ids) == 0 {
		return nil
	}
	if _, err := New(tx).DiscardQueued(ctx, uuids(ids)); err != nil {
		return fmt.Errorf("discard queued email: %w", err)
	}
	return nil
}

// States returns the delivery state of each message that still exists.
func States(ctx context.Context, db DBTX, ids []MessageID) (map[MessageID]DeliveryState, error) {
	out := make(map[MessageID]DeliveryState, len(ids))
	if len(ids) == 0 {
		return out, nil
	}
	rows, err := New(db).DeliveryStates(ctx, uuids(ids))
	if err != nil {
		return nil, fmt.Errorf("read delivery states: %w", err)
	}
	for _, row := range rows {
		out[MessageID(row.ID)] = DeliveryState(row.State)
	}
	return out, nil
}

func uuids(ids []MessageID) []uuid.UUID {
	out := make([]uuid.UUID, len(ids))
	for n, id := range ids {
		out[n] = uuid.UUID(id)
	}
	return out
}

// Notifications delivers the outbox.
type Notifications struct {
	queries *Queries
	sender  *Resend
	logger  *slog.Logger
}

// NewNotifications returns the outbox owner. sender delivers messages; a
// deployment without Resend credentials passes nil and its messages stay
// queued.
func NewNotifications(pool *pgxpool.Pool, sender *Resend, logger *slog.Logger) *Notifications {
	return &Notifications{queries: New(pool), sender: sender, logger: logger}
}

// DeliverResult counts one pass's outcomes. More means the pass claimed a
// full batch, so more messages may be due.
type DeliverResult struct {
	Sent, Retried, Failed int
	More                  bool
}

// ErrNoSender means delivery was asked for without provider credentials.
var ErrNoSender = errors.New("email delivery is not configured")

// Deliver sends the due messages of one batch. Each message settles in its
// own statement, so one failure does not undo another's result. A message
// whose settle is lost to a crash is claimed again after its lease and sent
// with the same idempotency key, so the provider sends it once.
func (n *Notifications) Deliver(ctx context.Context) (DeliverResult, error) {
	if n.sender == nil {
		return DeliverResult{}, ErrNoSender
	}
	claimed, err := n.queries.ClaimDue(ctx, ClaimDueParams{LeaseSeconds: leaseDuration.Seconds(), BatchSize: batchSize})
	if err != nil {
		return DeliverResult{}, fmt.Errorf("claim due email: %w", err)
	}
	outcomes := make([]outcome, len(claimed))
	group, gctx := errgroup.WithContext(ctx)
	group.SetLimit(sendConcurrency)
	for i, row := range claimed {
		group.Go(func() error {
			outcomes[i] = n.deliverOne(gctx, row)
			return nil
		})
	}
	_ = group.Wait() // deliverOne records failures in its outcome.
	result := DeliverResult{More: len(claimed) == batchSize}
	var errs []error
	for _, o := range outcomes {
		switch o.verdict {
		case verdictSent:
			result.Sent++
		case verdictRetry:
			result.Retried++
		case verdictFailed:
			result.Failed++
		}
		if o.err != nil {
			errs = append(errs, o.err)
		}
	}
	return result, errors.Join(errs...)
}

type verdict int

const (
	verdictSent verdict = iota + 1
	verdictRetry
	verdictFailed
)

type outcome struct {
	verdict verdict
	err     error
}

func (n *Notifications) deliverOne(ctx context.Context, row ClaimDueRow) outcome {
	id := MessageID(row.ID)
	providerID, sendErr := n.sender.Send(ctx, id, Email{To: row.Recipient, Subject: row.Subject, HTML: row.Html, Text: row.BodyText})
	// Settle even when the pass is stopping: the provider may have accepted
	// the message.
	settleCtx := context.WithoutCancel(ctx)
	var refused *RefusedError
	switch {
	case sendErr == nil:
		_, err := n.queries.MarkSent(settleCtx, MarkSentParams{ProviderMessageID: &providerID, ID: row.ID, Attempts: row.Attempts})
		if err != nil {
			return outcome{verdict: verdictSent, err: fmt.Errorf("record sent email %s: %w", id, err)}
		}
		return outcome{verdict: verdictSent}
	case errors.As(sendErr, &refused) && refused.Permanent, row.Attempts >= MaxAttempts:
		n.logger.ErrorContext(ctx, "email abandoned", "message_id", id.String(), "attempts", row.Attempts, "error", sendErr)
		_, err := n.queries.MarkFailed(settleCtx, MarkFailedParams{LastError: truncate(sendErr.Error()), ID: row.ID, Attempts: row.Attempts})
		if err != nil {
			return outcome{verdict: verdictFailed, err: fmt.Errorf("record failed email %s: %w", id, err)}
		}
		return outcome{verdict: verdictFailed}
	default:
		n.logger.WarnContext(ctx, "email delivery will retry", "message_id", id.String(), "attempts", row.Attempts, "error", sendErr)
		_, err := n.queries.MarkRetry(settleCtx, MarkRetryParams{
			DelaySeconds: RetryDelay(int(row.Attempts)).Seconds(), LastError: truncate(sendErr.Error()),
			ID: row.ID, Attempts: row.Attempts,
		})
		if err != nil {
			return outcome{verdict: verdictRetry, err: fmt.Errorf("record email retry %s: %w", id, err)}
		}
		return outcome{verdict: verdictRetry}
	}
}

// RetryDelay is the wait after the given number of failed attempts:
// exponential from 10 seconds, capped at 10 minutes.
func RetryDelay(attempts int) time.Duration {
	delay := retryBase
	for range max(attempts-1, 0) {
		delay *= 2
		if delay >= retryCap {
			return retryCap
		}
	}
	return delay
}

func truncate(s string) string {
	const limit = 500
	if len(s) > limit {
		return s[:limit]
	}
	return s
}

// Purge empties the bodies of messages settled more than BodyRetention ago
// and returns how many it purged. Their delivery records remain.
func (n *Notifications) Purge(ctx context.Context) (int64, error) {
	purged, err := n.queries.PurgeBodies(ctx, PurgeBodiesParams{RetentionSeconds: BodyRetention.Seconds(), BatchSize: purgeBatch})
	if err != nil {
		return 0, fmt.Errorf("purge email bodies: %w", err)
	}
	return purged, nil
}

// DeliveryEvent is a provider's report about a message it accepted.
type DeliveryEvent struct {
	ProviderMessageID string
	State             DeliveryState
	OccurredAt        time.Time
	Detail            string
}

// RecordDelivery applies a delivery event and reports whether a message
// matched it. Unknown messages are ordinary: the provider keeps history
// longer than the outbox does.
func (n *Notifications) RecordDelivery(ctx context.Context, event DeliveryEvent) (bool, error) {
	at := event.OccurredAt
	updated, err := n.queries.RecordDelivery(ctx, RecordDeliveryParams{
		State: string(event.State), OccurredAt: &at, Detail: truncate(event.Detail),
		ProviderMessageID: &event.ProviderMessageID,
	})
	if err != nil {
		return false, fmt.Errorf("record delivery: %w", err)
	}
	return updated > 0, nil
}
