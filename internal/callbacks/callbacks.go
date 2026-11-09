// Package callbacks owns signed outbound deliveries and the workspace
// webhooks they reach. Callbacks are rows execution writes in the
// transaction of each task's retry or terminal transition, and rows the edge
// writes with the record of each endpoint or ASGI request, posted to the
// release's callback_url. Webhook deliveries are rows lc_webhook_event writes
// in the transaction of each change a workspace's webhooks take. Delivery
// never changes a task or request. It is apart from notifications because
// signing reads workspace secrets, which depend on identity, and identity
// sends its email through notifications.
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
	"slices"
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
	// callbackAttempts is how many times one callback is sent.
	callbackAttempts = 3
	// callbackTimeout bounds one delivery; the lease outlasts it.
	callbackTimeout = 5 * time.Second
	leaseSeconds    = 30
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

// CallbackConfig configures delivery.
type CallbackConfig struct {
	// AllowPrivateTargets permits callback hosts that resolve to private,
	// loopback or link-local addresses. Only local development sets it.
	AllowPrivateTargets bool
	// RetryDelays separate the attempts: the first after the first
	// failure, the second after the second.
	RetryDelays [callbackAttempts - 1]time.Duration
}

// DefaultCallbackConfig retries after 250 ms and then 750 ms and reaches
// public addresses only.
func DefaultCallbackConfig() CallbackConfig {
	return CallbackConfig{RetryDelays: [callbackAttempts - 1]time.Duration{250 * time.Millisecond, 750 * time.Millisecond}}
}

// Callbacks delivers task callbacks.
type Callbacks struct {
	pool        *pgxpool.Pool
	queries     *Queries
	secrets     *secrets.Secrets
	client      *http.Client
	retryDelays [callbackAttempts - 1]time.Duration
	logger      *slog.Logger
}

// NewCallbacks returns the callback deliverer. Signing keys come from
// secrets. It refuses a retry delay that is not positive.
func NewCallbacks(pool *pgxpool.Pool, s *secrets.Secrets, config CallbackConfig, logger *slog.Logger) (*Callbacks, error) {
	for n, delay := range config.RetryDelays {
		if delay <= 0 {
			return nil, fmt.Errorf("callback retry delay %d is %s; it must be positive", n+1, delay)
		}
	}
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
		pool: pool, queries: New(pool), secrets: s, retryDelays: config.RetryDelays, logger: logger,
		client: &http.Client{
			Transport: transport,
			Timeout:   callbackTimeout,
			// A redirect could lead to an unchecked target.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}, nil
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

// Deliver sends due callbacks and webhook deliveries until none is due and
// returns how many it sent. The two drain side by side, so a webhook backlog
// never holds back callbacks. It returns once every delivery it started
// ended.
func (c *Callbacks) Deliver(ctx context.Context) (int, error) {
	var (
		wg           sync.WaitGroup
		callbacks    int
		callbacksErr error
	)
	wg.Go(func() { callbacks, callbacksErr = deliverDue(ctx, c.claimCallbacks, c.deliverOne) })
	webhooks, webhooksErr := deliverDue(ctx, c.claimWebhooks, c.deliverWebhook)
	wg.Wait()
	return callbacks + webhooks, errors.Join(callbacksErr, webhooksErr)
}

// deliverDue delivers what claim leases until nothing is due. At most
// deliveryConcurrency are in flight: each claim takes as many as there are
// free slots, so a slow target holds only its own slot while the others keep
// claiming. It returns once every delivery it started ended.
func deliverDue[T any](ctx context.Context, claim func(context.Context, int) ([]T, error), deliver func(context.Context, T)) (int, error) {
	slots := make(chan struct{}, deliveryConcurrency)
	var wg sync.WaitGroup
	defer wg.Wait()
	total := 0
	for {
		// Claim nothing that could not finish before the pass ends; a
		// delivery cut short is sent again once its lease lapses.
		if deadline, ok := ctx.Deadline(); ok && time.Until(deadline) < 2*callbackTimeout {
			return total, nil
		}
		// Wait for one free slot, then take every other free one.
		select {
		case slots <- struct{}{}:
		case <-ctx.Done():
			return total, nil
		}
		free := 1
	fill:
		for free < deliveryConcurrency {
			select {
			case slots <- struct{}{}:
				free++
			default:
				break fill
			}
		}
		release := func(n int) {
			for range n {
				<-slots
			}
		}
		rows, err := claim(ctx, free)
		if err != nil || len(rows) == 0 {
			release(free)
			return total, err
		}
		release(free - len(rows))
		for _, row := range rows {
			wg.Go(func() {
				defer release(1)
				deliver(ctx, row)
			})
		}
		total += len(rows)
	}
}

// claimedCallback is a leased callback with the deliveries count its lease
// fences.
type claimedCallback struct {
	CallbackDeliveriesRow
	deliveries int32
}

// claimCallbacks leases up to n due callbacks and reads them.
func (c *Callbacks) claimCallbacks(ctx context.Context, n int) ([]claimedCallback, error) {
	claimed, err := c.queries.ClaimDueCallbacks(ctx, ClaimDueCallbacksParams{LeaseSeconds: leaseSeconds, BatchSize: int32(n)}) //nolint:gosec // At most deliveryConcurrency.
	if err != nil {
		return nil, fmt.Errorf("claim callbacks: %w", err)
	}
	if len(claimed) == 0 {
		return nil, nil
	}
	ids := make([]int64, len(claimed))
	lease := make(map[int64]int32, len(claimed))
	for i, row := range claimed {
		ids[i], lease[row.ID] = row.ID, row.Deliveries
	}
	rows, err := c.queries.CallbackDeliveries(ctx, ids)
	if err != nil {
		return nil, fmt.Errorf("read callbacks: %w", err)
	}
	out := make([]claimedCallback, len(rows))
	for i, row := range rows {
		out[i] = claimedCallback{CallbackDeliveriesRow: row, deliveries: lease[row.ID]}
	}
	return out, nil
}

// Purge deletes callbacks and webhook deliveries that finished more than a
// week ago.
func (c *Callbacks) Purge(ctx context.Context) error {
	if _, err := c.queries.PurgeFinishedCallbacks(ctx, PurgeFinishedCallbacksParams{RetainSeconds: retainSeconds, BatchSize: purgeBatch}); err != nil {
		return fmt.Errorf("purge callbacks: %w", err)
	}
	if _, err := c.queries.PurgeFinishedWebhookDeliveries(ctx, PurgeFinishedWebhookDeliveriesParams{RetainSeconds: retainSeconds, BatchSize: purgeBatch}); err != nil {
		return fmt.Errorf("purge webhook deliveries: %w", err)
	}
	return nil
}

// deliverOne sends one callback and records the outcome under its lease.
func (c *Callbacks) deliverOne(ctx context.Context, claimed claimedCallback) {
	row, deliveries := claimed.CallbackDeliveriesRow, claimed.deliveries
	log := c.logger.With("task_id", row.TaskID, "request_id", row.RequestID, "event", row.Event, "attempt", row.Attempt, "delivery", deliveries)
	retry, err := c.send(ctx, row)
	c.settle(ctx, log, "callback", deliveries, retry, err,
		func(state string, lastError *string) error {
			return c.queries.FinishCallback(ctx, FinishCallbackParams{ID: row.ID, Deliveries: deliveries, State: state, LastError: lastError})
		},
		func(lastError *string, delaySeconds float64) error {
			return c.queries.RetryCallback(ctx, RetryCallbackParams{ID: row.ID, Deliveries: deliveries, LastError: lastError, DelaySeconds: delaySeconds})
		})
}

// settle records a delivery's outcome under its lease: delivered, sent
// again after the next retry delay while retry allows and attempts remain,
// else failed. A delivery the pass cut short records nothing; its lease
// expires and another pass sends it again.
func (c *Callbacks) settle(ctx context.Context, log *slog.Logger, what string, deliveries int32, retry bool, err error,
	finish func(state string, lastError *string) error, again func(lastError *string, delaySeconds float64) error,
) {
	if ctx.Err() != nil {
		return
	}
	var record error
	switch {
	case err == nil:
		log.InfoContext(ctx, what+" delivered")
		record = finish("delivered", nil)
	case retry && deliveries < callbackAttempts:
		message := err.Error()
		log.InfoContext(ctx, what+" failed; retrying", "error", message)
		record = again(&message, c.retryDelays[deliveries-1].Seconds())
	default:
		message := err.Error()
		log.WarnContext(ctx, what+" failed", "error", message)
		record = finish("failed", &message)
	}
	if record != nil {
		log.ErrorContext(ctx, "recording "+what+" outcome failed", "error", record)
	}
}

// signed is a JSON POST of body to target, signed with the workspace's key
// in the prefix's Signature and Timestamp headers. retry reports whether a
// later attempt could build it.
func (c *Callbacks) signed(ctx context.Context, workspace uuid.UUID, target string, body []byte, prefix string) (req *http.Request, retry bool, err error) {
	key, err := c.secrets.SigningKey(ctx, identity.WorkspaceID(workspace))
	if err != nil {
		return nil, true, fmt.Errorf("read signing key: %w", err)
	}
	timestamp := strconv.FormatInt(time.Now().Unix(), 10)
	req, err = http.NewRequestWithContext(ctx, http.MethodPost, target, bytes.NewReader(body))
	if err != nil {
		return nil, false, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(prefix+"Signature", Sign(key, body, timestamp))
	req.Header.Set(prefix+"Timestamp", timestamp)
	return req, false, nil
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
	req, retry, err := c.signed(ctx, row.WorkspaceID, row.Url, body, "X-Task-")
	if err != nil {
		return retry, fmt.Errorf("callback %w", err)
	}
	subject := subjectOf(row)
	idempotency := sha256.Sum256(fmt.Appendf(nil, "%s:%d:%s", subject, row.Attempt, row.Event))
	req.Header.Set("Idempotency-Key", hex.EncodeToString(idempotency[:]))
	// A request's callback names the request where a task's names the task.
	req.Header.Set("X-Task-ID", subject.String())
	if row.RequestID != nil {
		req.Header.Set("X-Request-ID", subject.String())
	}
	req.Header.Set("X-Task-Status", row.Event)
	req.Header.Set("X-Task-Attempt", strconv.Itoa(int(row.Attempt)))
	if _, retry, err = c.post(req); err != nil {
		return retry, fmt.Errorf("callback %w", err)
	}
	return false, nil
}

// post sends req and reads at most maxResponseBytes of the answer. status
// is the answer's code, zero without one; retry reports whether a later
// attempt could succeed.
func (c *Callbacks) post(req *http.Request) (status int, retry bool, err error) {
	resp, err := c.client.Do(req)
	if err != nil {
		return 0, !errors.Is(err, errPrivateTarget), fmt.Errorf("post: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxResponseBytes))
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return resp.StatusCode, false, nil
	}
	retryable := resp.StatusCode == http.StatusRequestTimeout || resp.StatusCode == http.StatusTooEarly ||
		resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500
	return resp.StatusCode, retryable, fmt.Errorf("answered %d", resp.StatusCode)
}

// Sign is the X-Task-Signature of body sent at timestamp: hex
// HMAC-SHA256, keyed by the workspace signing key, of the base64 body, a
// colon and the timestamp.
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

// callbackBody is the callback body, compact with sorted keys. A result
// above maxCallbackResultBytes is left out and data_omitted is set. A
// request's body has the shape of a task's: task_id and root_task_id are the
// request id, and data holds the response's status code and size.
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

// NextDue reports when the earliest pending callback or webhook delivery is
// due, and false when none is pending.
func (c *Callbacks) NextDue(ctx context.Context) (time.Time, bool, error) {
	next, err := c.queries.NextCallbackAt(ctx)
	if err != nil {
		return time.Time{}, false, fmt.Errorf("next callback: %w", err)
	}
	webhook, err := c.queries.NextWebhookDeliveryAt(ctx)
	if err != nil {
		return time.Time{}, false, fmt.Errorf("next webhook delivery: %w", err)
	}
	next = append(next, webhook...)
	if len(next) == 0 {
		return time.Time{}, false, nil
	}
	return slices.MinFunc(next, func(a, b time.Time) int { return a.Compare(b) }), true, nil
}
