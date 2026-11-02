package callbacks

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// maxWebhooks bounds a workspace's webhooks, so every event fans out to a
// bounded number of deliveries.
const maxWebhooks = 20

// ErrNotFound means the workspace has no such webhook.
var ErrNotFound = errors.New("webhook not found")

// ErrLimit means the workspace already holds maxWebhooks webhooks.
var ErrLimit = fmt.Errorf("a workspace holds at most %d webhooks", maxWebhooks)

// InvalidError rejects a webhook field or a page cursor.
type InvalidError struct{ Reason string }

func (e *InvalidError) Error() string { return e.Reason }

// TargetURL checks a callback or webhook target: http or https, a host, no
// credentials and no fragment. Whether the host is public is checked when
// each delivery dials it.
func TargetURL(value string) (string, error) {
	trimmed := strings.TrimSpace(value)
	parsed, err := url.Parse(trimmed)
	if err != nil {
		return "", errors.New("is not a URL")
	}
	switch {
	case parsed.Scheme != "http" && parsed.Scheme != "https":
		return "", errors.New("must use http or https")
	case parsed.Hostname() == "":
		return "", errors.New("must include a hostname")
	case parsed.User != nil:
		return "", errors.New("must not contain credentials")
	case parsed.Fragment != "" || strings.Contains(trimmed, "#"):
		return "", errors.New("must not contain a fragment")
	}
	return trimmed, nil
}

// Webhooks owns the workspace's webhooks: the URLs its events are delivered
// to. Owners of the changes write the events, through lc_webhook_event, in
// the changes' transactions; Callbacks delivers them.
type Webhooks struct {
	pool    *pgxpool.Pool
	queries *Queries
}

// NewWebhooks returns the webhook owner.
func NewWebhooks(pool *pgxpool.Pool) *Webhooks {
	return &Webhooks{pool: pool, queries: New(pool)}
}

// List returns a page of the workspace's webhooks, oldest first.
func (h *Webhooks) List(ctx context.Context, workspace identity.WorkspaceID, cursor string, limit int) (apitypes.WebhookPage, error) {
	after := uuid.Nil
	if cursor != "" {
		parsed, err := uuid.Parse(cursor)
		if err != nil {
			return apitypes.WebhookPage{}, &InvalidError{Reason: "the cursor is not from this listing"}
		}
		after = parsed
	}
	rows, err := h.queries.ListWebhooks(ctx, ListWebhooksParams{WorkspaceID: uuid.UUID(workspace), After: after, MaxRows: int32(limit + 1)}) //nolint:gosec // The API bounds limit.
	if err != nil {
		return apitypes.WebhookPage{}, fmt.Errorf("list webhooks: %w", err)
	}
	page := apitypes.WebhookPage{Webhooks: []apitypes.Webhook{}}
	for n, row := range rows {
		if n == limit {
			next := rows[n-1].ID.String()
			page.NextCursor = &next
			break
		}
		page.Webhooks = append(page.Webhooks, webhookOut(row))
	}
	return page, nil
}

// Create registers a webhook. The workspace row lock orders creates, so the
// limit holds.
func (h *Webhooks) Create(ctx context.Context, workspace identity.WorkspaceID, in apitypes.WebhookCreate) (apitypes.Webhook, error) {
	target, err := webhookURL(in.Url)
	if err != nil {
		return apitypes.Webhook{}, err
	}
	params := InsertWebhookParams{
		WorkspaceID: uuid.UUID(workspace), Apps: appNames(in.Apps), Url: target,
		Events: eventNames(in.Events), Enabled: in.Enabled == nil || *in.Enabled,
	}
	if in.Description != nil {
		params.Description = *in.Description
	}
	var row Webhook
	err = pgx.BeginFunc(ctx, h.pool, func(tx pgx.Tx) error {
		q := h.queries.WithTx(tx)
		count, err := q.LockWorkspaceWebhooks(ctx, uuid.UUID(workspace))
		if err != nil {
			return fmt.Errorf("lock workspace: %w", err)
		}
		if count >= maxWebhooks {
			return ErrLimit
		}
		row, err = q.InsertWebhook(ctx, params)
		if err != nil {
			return fmt.Errorf("insert webhook: %w", err)
		}
		return nil
	})
	if err != nil {
		return apitypes.Webhook{}, fmt.Errorf("create webhook: %w", err)
	}
	return webhookOut(row), nil
}

// Get returns one webhook.
func (h *Webhooks) Get(ctx context.Context, workspace identity.WorkspaceID, id uuid.UUID) (apitypes.Webhook, error) {
	row, err := h.queries.GetWebhook(ctx, GetWebhookParams{WorkspaceID: uuid.UUID(workspace), ID: id})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.Webhook{}, ErrNotFound
	}
	if err != nil {
		return apitypes.Webhook{}, fmt.Errorf("read webhook: %w", err)
	}
	return webhookOut(row), nil
}

// Update changes the fields in names. Empty apps take the webhook to every
// app.
func (h *Webhooks) Update(ctx context.Context, workspace identity.WorkspaceID, id uuid.UUID, in apitypes.WebhookUpdate) (apitypes.Webhook, error) {
	params := UpdateWebhookParams{WorkspaceID: uuid.UUID(workspace), ID: id, Description: in.Description, Enabled: in.Enabled}
	if in.Url != nil {
		target, err := webhookURL(*in.Url)
		if err != nil {
			return apitypes.Webhook{}, err
		}
		params.Url = &target
	}
	if in.Apps != nil {
		params.Apps = appNames(in.Apps)
	}
	if in.Events != nil {
		params.Events = eventNames(*in.Events)
	}
	row, err := h.queries.UpdateWebhook(ctx, params)
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.Webhook{}, ErrNotFound
	}
	if err != nil {
		return apitypes.Webhook{}, fmt.Errorf("update webhook: %w", err)
	}
	return webhookOut(row), nil
}

// Delete deletes the webhook and its deliveries; one in flight still ends.
func (h *Webhooks) Delete(ctx context.Context, workspace identity.WorkspaceID, id uuid.UUID) error {
	n, err := h.queries.DeleteWebhook(ctx, DeleteWebhookParams{WorkspaceID: uuid.UUID(workspace), ID: id})
	if err != nil {
		return fmt.Errorf("delete webhook: %w", err)
	}
	if n == 0 {
		return ErrNotFound
	}
	return nil
}

// Test queues a webhook.test event for the webhook, enabled or not.
func (h *Webhooks) Test(ctx context.Context, workspace identity.WorkspaceID, id uuid.UUID) (apitypes.WebhookDelivery, error) {
	var row WebhookDelivery
	err := pgx.BeginFunc(ctx, h.pool, func(tx pgx.Tx) error {
		q := h.queries.WithTx(tx)
		var err error
		row, err = q.InsertTestDelivery(ctx, InsertTestDeliveryParams{WorkspaceID: uuid.UUID(workspace), ID: id})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("queue test delivery: %w", err)
		}
		if err := q.WakeDelivery(ctx); err != nil {
			return fmt.Errorf("wake delivery: %w", err)
		}
		return nil
	})
	if err != nil {
		return apitypes.WebhookDelivery{}, fmt.Errorf("test webhook: %w", err)
	}
	return deliveryOut(row), nil
}

// Deliveries returns a page of the webhook's deliveries, newest first.
func (h *Webhooks) Deliveries(ctx context.Context, workspace identity.WorkspaceID, id uuid.UUID, cursor string, limit int) (apitypes.WebhookDeliveryPage, error) {
	if _, err := h.Get(ctx, workspace, id); err != nil {
		return apitypes.WebhookDeliveryPage{}, err
	}
	before := int64(1<<63 - 1)
	if cursor != "" {
		parsed, err := strconv.ParseInt(cursor, 10, 64)
		if err != nil {
			return apitypes.WebhookDeliveryPage{}, &InvalidError{Reason: "the cursor is not from this listing"}
		}
		before = parsed
	}
	rows, err := h.queries.ListWebhookDeliveries(ctx, ListWebhookDeliveriesParams{
		WorkspaceID: uuid.UUID(workspace), WebhookID: id, Before: before, MaxRows: int32(limit + 1), //nolint:gosec // The API bounds limit.
	})
	if err != nil {
		return apitypes.WebhookDeliveryPage{}, fmt.Errorf("list deliveries: %w", err)
	}
	page := apitypes.WebhookDeliveryPage{Deliveries: []apitypes.WebhookDelivery{}}
	for n, row := range rows {
		if n == limit {
			next := strconv.FormatInt(rows[n-1].ID, 10)
			page.NextCursor = &next
			break
		}
		page.Deliveries = append(page.Deliveries, deliveryOut(row))
	}
	return page, nil
}

func webhookURL(value string) (string, error) {
	target, err := TargetURL(value)
	if err != nil {
		return "", &InvalidError{Reason: "url " + err.Error()}
	}
	return target, nil
}

// appNames is the distinct names of apps, empty for every app.
func appNames(apps *[]apitypes.AppName) []string {
	names := []string{}
	if apps != nil {
		for _, app := range *apps {
			if !slices.Contains(names, app) {
				names = append(names, app)
			}
		}
	}
	return names
}

func eventNames(events []apitypes.WebhookEvent) []string {
	names := make([]string, 0, len(events))
	for _, event := range events {
		if !slices.Contains(names, string(event)) {
			names = append(names, string(event))
		}
	}
	return names
}

func webhookOut(row Webhook) apitypes.Webhook {
	events := make([]apitypes.WebhookEvent, len(row.Events))
	for n, event := range row.Events {
		events[n] = apitypes.WebhookEvent(event)
	}
	return apitypes.Webhook{
		Id: row.ID, Url: row.Url, Apps: row.Apps, Events: events,
		Description: row.Description, Enabled: row.Enabled, CreatedAt: row.CreatedAt,
	}
}

func deliveryOut(row WebhookDelivery) apitypes.WebhookDelivery {
	out := apitypes.WebhookDelivery{
		Id: strconv.FormatInt(row.ID, 10), EventId: row.EventID, Event: row.Event,
		State: apitypes.WebhookDeliveryState(row.State), Attempts: int(row.Deliveries),
		LastError: row.LastError, CreatedAt: row.CreatedAt, FinishedAt: row.FinishedAt,
	}
	if row.ResponseStatus != nil {
		status := int(*row.ResponseStatus)
		out.ResponseStatus = &status
	}
	return out
}

// claimWebhooks leases up to n due webhook deliveries.
func (c *Callbacks) claimWebhooks(ctx context.Context, n int) ([]ClaimDueWebhookDeliveriesRow, error) {
	rows, err := c.queries.ClaimDueWebhookDeliveries(ctx, ClaimDueWebhookDeliveriesParams{LeaseSeconds: leaseSeconds, BatchSize: int32(n)}) //nolint:gosec // At most deliveryConcurrency.
	if err != nil {
		return nil, fmt.Errorf("claim webhook deliveries: %w", err)
	}
	return rows, nil
}

// deliverWebhook sends one webhook delivery and records the outcome under
// its lease.
func (c *Callbacks) deliverWebhook(ctx context.Context, row ClaimDueWebhookDeliveriesRow) {
	log := c.logger.With("delivery_id", row.ID, "event_id", row.EventID, "event", row.Event, "delivery", row.Deliveries)
	status, retry, err := c.sendWebhook(ctx, row)
	if ctx.Err() != nil {
		// The lease expires and another pass sends it again.
		return
	}
	var answered *int32
	if status != 0 {
		code := int32(status) //nolint:gosec // An HTTP status code.
		answered = &code
	}
	var record error
	switch {
	case err == nil:
		log.InfoContext(ctx, "webhook delivered")
		record = c.queries.FinishWebhookDelivery(ctx, FinishWebhookDeliveryParams{
			ID: row.ID, Deliveries: row.Deliveries, State: "delivered", ResponseStatus: answered,
		})
	case retry && row.Deliveries < callbackAttempts:
		message := err.Error()
		log.InfoContext(ctx, "webhook delivery failed; retrying", "error", message)
		record = c.queries.RetryWebhookDelivery(ctx, RetryWebhookDeliveryParams{
			ID: row.ID, Deliveries: row.Deliveries, ResponseStatus: answered, LastError: &message,
			DelaySeconds: c.retryDelays[row.Deliveries-1].Seconds(),
		})
	default:
		message := err.Error()
		log.WarnContext(ctx, "webhook delivery failed", "error", message)
		record = c.queries.FinishWebhookDelivery(ctx, FinishWebhookDeliveryParams{
			ID: row.ID, Deliveries: row.Deliveries, State: "failed", ResponseStatus: answered, LastError: &message,
		})
	}
	if record != nil {
		log.ErrorContext(ctx, "recording webhook delivery outcome failed", "error", record)
	}
}

// sendWebhook posts one event: {id, type, created_at, workspace, data},
// signed as callbacks are, with the event id in X-Webhook-ID.
func (c *Callbacks) sendWebhook(ctx context.Context, row ClaimDueWebhookDeliveriesRow) (status int, retry bool, err error) {
	body, err := json.Marshal(map[string]any{
		"id":         row.EventID.String(),
		"type":       row.Event,
		"created_at": row.CreatedAt.UTC().Format(time.RFC3339Nano),
		"workspace":  row.WorkspaceName,
		"data":       json.RawMessage(row.Data),
	})
	if err != nil {
		return 0, false, fmt.Errorf("encode webhook event: %w", err)
	}
	key, err := c.secrets.SigningKey(ctx, identity.WorkspaceID(row.WorkspaceID))
	if err != nil {
		return 0, true, fmt.Errorf("read signing key: %w", err)
	}
	timestamp := strconv.FormatInt(time.Now().Unix(), 10)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, row.Url, bytes.NewReader(body))
	if err != nil {
		return 0, false, fmt.Errorf("build webhook request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Webhook-ID", row.EventID.String())
	req.Header.Set("X-Webhook-Event", row.Event)
	req.Header.Set("X-Webhook-Signature", Sign(key, body, timestamp))
	req.Header.Set("X-Webhook-Timestamp", timestamp)
	return c.post(req)
}
