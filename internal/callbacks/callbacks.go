// Package callbacks delivers callbacks: rows execution writes in the
// transaction of each task's retry or terminal transition, and rows the edge
// writes with the record of each endpoint or ASGI request, posted signed to
// the release's callback_url. Delivery never changes a task or request. It
// is apart from notifications because signing reads workspace secrets, which
// depend on identity, and identity sends its email through notifications.
package callbacks

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/netip"
	"strconv"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/secrets"
)

const (
	// callbackAttempts is how many times one callback is sent, as in the
	// reference.
	callbackAttempts = 3
	// callbackTimeout bounds one delivery; the lease outlasts it.
	callbackTimeout = 5 * time.Second
	leaseSeconds    = 30
	claimBatch      = 64
	// deliveryConcurrency bounds callbacks in flight per scheduler.
	deliveryConcurrency = 16
	maxResponseBytes    = 64 << 10
	// maxCallbackResultBytes bounds the result a callback carries; a larger
	// one is left out and marked, and the receiver reads it from the API.
	maxCallbackResultBytes = 256 << 10
	// Finished callbacks are kept a week for inspection, then purged.
	retainSeconds = 7 * 24 * 3600
	purgeBatch    = 1000
)

// retryDelays separate the attempts, as in the reference.
var retryDelays = [...]time.Duration{250 * time.Millisecond, 750 * time.Millisecond} //nolint:gochecknoglobals // constant table

// CallbackConfig configures delivery.
type CallbackConfig struct {
	// AllowPrivateTargets permits callback hosts that resolve to private,
	// loopback or link-local addresses. Only local development sets it.
	AllowPrivateTargets bool
}

// Callbacks delivers task callbacks.
type Callbacks struct {
	pool    *pgxpool.Pool
	queries *Queries
	secrets *secrets.Secrets
	client  *http.Client
	logger  *slog.Logger
}

// NewCallbacks returns the callback deliverer. Signing keys come from
// secrets.
func NewCallbacks(pool *pgxpool.Pool, s *secrets.Secrets, config CallbackConfig, logger *slog.Logger) *Callbacks {
	dialer := &net.Dialer{Timeout: callbackTimeout}
	transport := &http.Transport{
		// Each connection goes to an address checked at dial time, so a
		// name cannot resolve to a public address for the check and a
		// private one for the request.
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			host, port, err := net.SplitHostPort(address)
			if err != nil {
				return nil, fmt.Errorf("callback address: %w", err)
			}
			ip, err := publicAddress(ctx, host, config.AllowPrivateTargets)
			if err != nil {
				return nil, err
			}
			return dialer.DialContext(ctx, network, net.JoinHostPort(ip.String(), port))
		},
		Proxy:                 nil,
		TLSHandshakeTimeout:   callbackTimeout,
		ResponseHeaderTimeout: callbackTimeout,
		MaxIdleConnsPerHost:   2,
	}
	return &Callbacks{
		pool: pool, queries: New(pool), secrets: s, logger: logger,
		client: &http.Client{
			Transport: transport,
			Timeout:   callbackTimeout,
			// A redirect could lead to an unchecked target.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}
}

// errPrivateTarget means the callback host resolves to an address that is
// not public. It is not retried.
var errPrivateTarget = errors.New("callback target resolves to a non-public address")

// publicAddress resolves host and returns its first address when every
// address it resolves to is public.
func publicAddress(ctx context.Context, host string, allowPrivate bool) (netip.Addr, error) {
	addrs, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
	if err != nil {
		return netip.Addr{}, fmt.Errorf("resolve callback host: %w", err)
	}
	if len(addrs) == 0 {
		return netip.Addr{}, fmt.Errorf("callback host %s has no address", host)
	}
	if !allowPrivate {
		for _, addr := range addrs {
			if !isPublic(addr.Unmap()) {
				return netip.Addr{}, errPrivateTarget
			}
		}
	}
	return addrs[0].Unmap(), nil
}

// nonPublic are global unicast ranges that still reach private networks:
// carrier-grade NAT and the NAT64 prefixes, which embed IPv4 addresses.
var nonPublic = []netip.Prefix{ //nolint:gochecknoglobals // constant prefixes
	netip.MustParsePrefix("100.64.0.0/10"),
	netip.MustParsePrefix("64:ff9b::/96"),
	netip.MustParsePrefix("64:ff9b:1::/48"),
}

func isPublic(addr netip.Addr) bool {
	if !addr.IsGlobalUnicast() || addr.IsPrivate() {
		return false
	}
	for _, prefix := range nonPublic {
		if prefix.Contains(addr) {
			return false
		}
	}
	return true
}

// Deliver sends due callbacks until none is due, with at most
// deliveryConcurrency in flight, and returns how many it sent.
func (c *Callbacks) Deliver(ctx context.Context) (int, error) {
	total := 0
	for {
		claimed, err := c.queries.ClaimDueCallbacks(ctx, ClaimDueCallbacksParams{LeaseSeconds: leaseSeconds, BatchSize: claimBatch})
		if err != nil {
			return total, fmt.Errorf("claim callbacks: %w", err)
		}
		if len(claimed) == 0 {
			return total, nil
		}
		ids := make([]int64, len(claimed))
		lease := make(map[int64]int32, len(claimed))
		for n, row := range claimed {
			ids[n], lease[row.ID] = row.ID, row.Deliveries
		}
		rows, err := c.queries.CallbackDeliveries(ctx, ids)
		if err != nil {
			return total, fmt.Errorf("read callbacks: %w", err)
		}
		var wg sync.WaitGroup
		limit := make(chan struct{}, deliveryConcurrency)
		for _, row := range rows {
			limit <- struct{}{}
			wg.Go(func() {
				defer func() { <-limit }()
				c.deliverOne(ctx, row, lease[row.ID])
			})
		}
		wg.Wait()
		total += len(rows)
		if len(claimed) < claimBatch {
			return total, nil
		}
	}
}

// Purge deletes callbacks that finished more than a week ago.
func (c *Callbacks) Purge(ctx context.Context) (int64, error) {
	n, err := c.queries.PurgeFinishedCallbacks(ctx, PurgeFinishedCallbacksParams{RetainSeconds: retainSeconds, BatchSize: purgeBatch})
	if err != nil {
		return 0, fmt.Errorf("purge callbacks: %w", err)
	}
	return n, nil
}

// deliverOne sends one callback and records the outcome under its lease.
func (c *Callbacks) deliverOne(ctx context.Context, row CallbackDeliveriesRow, deliveries int32) {
	log := c.logger.With("task_id", row.TaskID, "request_id", row.RequestID, "event", row.Event, "attempt", row.Attempt, "delivery", deliveries)
	retry, err := c.send(ctx, row)
	if ctx.Err() != nil {
		// The lease expires and another pass sends it again.
		return
	}
	var record error
	switch {
	case err == nil:
		log.InfoContext(ctx, "callback delivered")
		record = c.queries.FinishCallback(ctx, FinishCallbackParams{ID: row.ID, Deliveries: deliveries, State: "delivered"})
	case retry && deliveries < callbackAttempts:
		message := err.Error()
		log.InfoContext(ctx, "callback failed; retrying", "error", message)
		record = c.queries.RetryCallback(ctx, RetryCallbackParams{
			ID: row.ID, Deliveries: deliveries, LastError: &message,
			DelaySeconds: retryDelays[min(int(deliveries), len(retryDelays))-1].Seconds(),
		})
	default:
		message := err.Error()
		log.WarnContext(ctx, "callback failed", "error", message)
		record = c.queries.FinishCallback(ctx, FinishCallbackParams{ID: row.ID, Deliveries: deliveries, State: "failed", LastError: &message})
	}
	if record != nil {
		log.ErrorContext(ctx, "recording callback outcome failed", "error", record)
	}
}

// send posts one callback. retry reports whether a later attempt could
// succeed.
func (c *Callbacks) send(ctx context.Context, row CallbackDeliveriesRow) (retry bool, err error) {
	var result *CallbackResultRow
	if row.Event == "succeeded" && row.TaskID != nil {
		r, err := c.queries.CallbackResult(ctx, CallbackResultParams{TaskID: *row.TaskID, MaxBytes: maxCallbackResultBytes})
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return true, fmt.Errorf("read task result: %w", err)
		}
		if err == nil {
			result = &r
		}
	}
	body, err := callbackBody(row, result)
	if err != nil {
		return false, err
	}
	key, err := c.secrets.SigningKey(ctx, identity.WorkspaceID(row.WorkspaceID))
	if err != nil {
		return true, fmt.Errorf("read signing key: %w", err)
	}
	timestamp := strconv.FormatInt(time.Now().Unix(), 10)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, row.Url, bytes.NewReader(body))
	if err != nil {
		return false, fmt.Errorf("build callback request: %w", err)
	}
	subject := subjectOf(row)
	idempotency := sha256.Sum256(fmt.Appendf(nil, "%s:%d:%s", subject, row.Attempt, row.Event))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", hex.EncodeToString(idempotency[:]))
	// A request's callback names the request where a task's names the task,
	// as the reference's per-request task did.
	req.Header.Set("X-Task-ID", subject.String())
	if row.RequestID != nil {
		req.Header.Set("X-Request-ID", subject.String())
	}
	req.Header.Set("X-Task-Status", row.Event)
	req.Header.Set("X-Task-Attempt", strconv.Itoa(int(row.Attempt)))
	req.Header.Set("X-Task-Signature", Sign(key, body, timestamp))
	req.Header.Set("X-Task-Timestamp", timestamp)
	resp, err := c.client.Do(req)
	if err != nil {
		return !errors.Is(err, errPrivateTarget), fmt.Errorf("post callback: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxResponseBytes))
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return false, nil
	}
	retryable := resp.StatusCode == http.StatusRequestTimeout || resp.StatusCode == http.StatusTooEarly ||
		resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500
	return retryable, fmt.Errorf("callback answered %d", resp.StatusCode)
}

// Sign is the X-Task-Signature of body sent at timestamp: hex
// HMAC-SHA256, keyed by the workspace signing key, of the base64 body, a
// colon and the timestamp, as in the reference.
func Sign(key string, body []byte, timestamp string) string {
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write([]byte(base64.StdEncoding.EncodeToString(body) + ":" + timestamp))
	return hex.EncodeToString(mac.Sum(nil))
}

// callbackFailure is the error field of a callback body.
type callbackFailure struct {
	Kind      string `json:"kind"`
	Type      string `json:"type,omitempty"`
	Message   string `json:"message"`
	Traceback string `json:"traceback,omitempty"`
}

// subjectOf is the task or request a callback reports.
func subjectOf(row CallbackDeliveriesRow) uuid.UUID {
	if row.TaskID != nil {
		return *row.TaskID
	}
	return *row.RequestID
}

// callbackBody is the reference's body, compact with sorted keys. A result
// above maxCallbackResultBytes is left out and data_omitted is set. A
// request's body reports it as the reference reported the task it made for
// the request: task_id and root_task_id are the request id, and data holds
// the response's status code and size.
func callbackBody(row CallbackDeliveriesRow, result *CallbackResultRow) ([]byte, error) {
	subject := subjectOf(row)
	root := subject
	if row.RootTaskID != nil {
		root = *row.RootTaskID
	}
	body := map[string]any{
		"task_id":         subject.String(),
		"root_task_id":    root.String(),
		"status":          row.Event,
		"attempt_number":  row.Attempt,
		"max_attempts":    row.MaxAttempts,
		"retry_scheduled": row.Event == "retry",
		"data":            nil,
		"error":           nil,
		"finished_at":     nil,
	}
	if row.FinishedAt != nil && row.Event != "retry" {
		body["finished_at"] = row.FinishedAt.UTC().Format(time.RFC3339Nano)
	}
	failure := row.TaskFailure
	if row.Event == "retry" {
		failure = row.Failure
	}
	if len(failure) > 0 {
		var f callbackFailure
		if err := json.Unmarshal(failure, &f); err != nil {
			return nil, fmt.Errorf("decode failure: %w", err)
		}
		body["error"] = f
	}
	if row.RequestID != nil && row.RequestStatus != nil {
		body["request_id"] = subject.String()
		body["data"] = map[string]any{"status_code": *row.RequestStatus, "body_size_bytes": row.ResponseBytes}
		if row.Event == "failed" {
			body["error"] = callbackFailure{Kind: "http_status", Message: fmt.Sprintf("the request ended with status %d", *row.RequestStatus)}
		}
	}
	switch {
	case result == nil:
	case result.Data == nil:
		body["data_omitted"] = true
	case result.Encoding == "json":
		body["data"] = map[string]any{"encoding": "json", "value": json.RawMessage(result.Data)}
	default:
		body["data"] = map[string]any{"encoding": result.Encoding, "data": result.Data}
	}
	encoded, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("encode callback: %w", err)
	}
	return encoded, nil
}

// NextDue reports when the earliest pending callback is due, and false when
// none is pending.
func (c *Callbacks) NextDue(ctx context.Context) (time.Time, bool, error) {
	next, err := c.queries.NextCallbackAt(ctx)
	if err != nil {
		return time.Time{}, false, fmt.Errorf("next callback: %w", err)
	}
	if len(next) == 0 {
		return time.Time{}, false, nil
	}
	return next[0], true, nil
}
