package notifications

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// resendStub plays Resend's send endpoint. Each recipient gets a scripted
// list of status codes; the last one repeats.
type resendStub struct {
	mu       sync.Mutex
	script   map[string][]int
	sent     map[string]int    // recipient -> accepted sends
	keys     map[string]string // idempotency key -> provider id
	requests []string          // idempotency keys in order
}

func newResendStub(t *testing.T) (*resendStub, *Resend) {
	s := &resendStub{script: map[string][]int{}, sent: map[string]int{}, keys: map[string]string{}}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/emails" || r.Header.Get("Authorization") != "Bearer re_key" {
			http.Error(w, `{"message":"bad key"}`, http.StatusUnauthorized)
			return
		}
		var body struct {
			From    string   `json:"from"`
			To      []string `json:"to"`
			Subject string   `json:"subject"`
			HTML    string   `json:"html"`
			Text    string   `json:"text"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || len(body.To) != 1 || body.HTML == "" || body.Text == "" || body.From != "LazyCloud <noreply@lazycloud.dev>" {
			http.Error(w, `{"message":"malformed"}`, http.StatusUnprocessableEntity)
			return
		}
		key := r.Header.Get("Idempotency-Key")
		s.mu.Lock()
		defer s.mu.Unlock()
		s.requests = append(s.requests, key)
		to := body.To[0]
		status := http.StatusOK
		if script := s.script[to]; len(script) > 0 {
			status = script[0]
			if len(script) > 1 {
				s.script[to] = script[1:]
			}
		}
		if status != http.StatusOK {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"name":"refused","message":"scripted refusal"}`))
			return
		}
		// Resend answers a repeated key with the original send.
		id, ok := s.keys[key]
		if !ok {
			id = "re_" + key
			s.keys[key] = id
			s.sent[to]++
		}
		_ = json.NewEncoder(w).Encode(map[string]string{"id": id})
	}))
	t.Cleanup(server.Close)
	return s, NewResend(ResendConfig{APIURL: server.URL, APIKey: "re_key", From: "LazyCloud <noreply@lazycloud.dev>"})
}

func enqueue(t *testing.T, pool *pgxpool.Pool, to string) MessageID {
	t.Helper()
	var id MessageID
	err := pgx.BeginFunc(t.Context(), pool, func(tx pgx.Tx) error {
		var err error
		id, err = Enqueue(t.Context(), tx, Email{To: to, Subject: "Hello", HTML: "<p>hi</p>", Text: "hi"})
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	return id
}

type row struct {
	State    string
	Attempts int
	Provider *string
	Due      bool
}

func read(t *testing.T, pool *pgxpool.Pool, id MessageID) row {
	t.Helper()
	var r row
	err := pool.QueryRow(t.Context(), `select state, attempts, provider_message_id, next_attempt_at <= now() from email_outbox where id = $1`,
		uuid.UUID(id)).Scan(&r.State, &r.Attempts, &r.Provider, &r.Due)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func makeDue(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	if _, err := pool.Exec(t.Context(), "update email_outbox set next_attempt_at = now() where state = 'queued'"); err != nil {
		t.Fatal(err)
	}
}

func TestDeliveryRetriesAndFailures(t *testing.T) {
	pool := dbtest.New(t)
	stub, sender := newResendStub(t)
	n := NewNotifications(pool, sender, slog.New(slog.DiscardHandler))
	ctx := t.Context()

	ok := enqueue(t, pool, "ok@example.com")
	flaky := enqueue(t, pool, "flaky@example.com")
	bad := enqueue(t, pool, "bad@example.com")
	limited := enqueue(t, pool, "limited@example.com")
	stub.script["flaky@example.com"] = []int{http.StatusInternalServerError, http.StatusOK}
	stub.script["bad@example.com"] = []int{http.StatusUnprocessableEntity}
	stub.script["limited@example.com"] = []int{http.StatusTooManyRequests}

	result, err := n.Deliver(ctx)
	if err != nil || result.Sent != 1 || result.Retried != 2 || result.Failed != 1 || result.More {
		t.Fatalf("first pass %+v %v", result, err)
	}
	if r := read(t, pool, ok); r.State != "sent" || r.Provider == nil || *r.Provider != "re_"+ok.String() {
		t.Fatalf("ok %+v", r)
	}
	// A malformed message fails at once; outages and rate limits wait.
	if r := read(t, pool, bad); r.State != "failed" || r.Attempts != 1 {
		t.Fatalf("bad %+v", r)
	}
	if r := read(t, pool, flaky); r.State != "queued" || r.Attempts != 1 || r.Due {
		t.Fatalf("flaky %+v", r)
	}
	// Nothing is due until the backoff passes.
	if result, _ := n.Deliver(ctx); result != (DeliverResult{}) {
		t.Fatalf("pass before backoff %+v", result)
	}
	makeDue(t, pool)
	if result, err := n.Deliver(ctx); err != nil || result.Sent != 1 || result.Retried != 1 {
		t.Fatalf("second pass %+v %v", result, err)
	}
	if r := read(t, pool, flaky); r.State != "sent" || r.Attempts != 2 {
		t.Fatalf("flaky after retry %+v", r)
	}
	// The rate-limited message gives up after MaxAttempts.
	for range MaxAttempts {
		makeDue(t, pool)
		if _, err := n.Deliver(ctx); err != nil {
			t.Fatal(err)
		}
	}
	if r := read(t, pool, limited); r.State != "failed" || r.Attempts != MaxAttempts {
		t.Fatalf("limited %+v", r)
	}
	// Retries reuse the message id as the idempotency key.
	keys := map[string]int{}
	for _, key := range stub.requests {
		keys[key]++
	}
	if keys[flaky.String()] != 2 || keys[limited.String()] != MaxAttempts {
		t.Fatalf("idempotency keys %v", keys)
	}
}

func TestBackoffIsCapped(t *testing.T) {
	want := []time.Duration{10, 20, 40, 80, 160, 320, 600, 600}
	for n, w := range want {
		if got := RetryDelay(n + 1); got != w*time.Second {
			t.Errorf("attempt %d: %s, want %s", n+1, got, w*time.Second)
		}
	}
}

// A pass whose lease expired cannot settle a message another pass took,
// and a resend under the same key is accepted once by the provider.
func TestLeaseFencesStaleSettle(t *testing.T) {
	pool := dbtest.New(t)
	stub, sender := newResendStub(t)
	n := NewNotifications(pool, sender, slog.New(slog.DiscardHandler))
	ctx := t.Context()
	id := enqueue(t, pool, "slow@example.com")

	stale, err := n.queries.ClaimDue(ctx, ClaimDueParams{LeaseSeconds: 60, BatchSize: 10})
	if err != nil || len(stale) != 1 {
		t.Fatalf("claim %v %v", stale, err)
	}
	// The first pass "crashes" after the provider accepted the message.
	if _, err := sender.Send(ctx, id, Email{To: "slow@example.com", Subject: "s", HTML: "h", Text: "t"}); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, "update email_outbox set lease_until = now() - interval '1 second'"); err != nil {
		t.Fatal(err)
	}
	if result, err := n.Deliver(ctx); err != nil || result.Sent != 1 {
		t.Fatalf("second pass %+v %v", result, err)
	}
	settled, err := n.queries.MarkFailed(ctx, MarkFailedParams{LastError: "late", ID: stale[0].ID, Attempts: stale[0].Attempts})
	if err != nil || settled != 0 {
		t.Fatalf("stale settle changed %d rows %v", settled, err)
	}
	if r := read(t, pool, id); r.State != "sent" || r.Attempts != 2 {
		t.Fatalf("message %+v", r)
	}
	if stub.sent["slow@example.com"] != 1 {
		t.Fatalf("provider sent %d times", stub.sent["slow@example.com"])
	}
}

func TestDiscardPurgeAndNoSender(t *testing.T) {
	pool := dbtest.New(t)
	ctx := t.Context()
	off := NewNotifications(pool, nil, slog.New(slog.DiscardHandler))
	queued := enqueue(t, pool, "a@example.com")
	withdrawn := enqueue(t, pool, "b@example.com")
	// Without credentials nothing is sent and nothing pretends to be.
	if _, err := off.Deliver(ctx); !errors.Is(err, ErrNoSender) {
		t.Fatalf("no sender: %v", err)
	}
	err := pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error { return Discard(ctx, tx, []MessageID{withdrawn}) })
	if err != nil {
		t.Fatal(err)
	}
	states, err := States(ctx, pool, []MessageID{queued, withdrawn})
	if err != nil || states[queued] != StateQueued || states[withdrawn] != StateDiscarded {
		t.Fatalf("states %v %v", states, err)
	}
	// Bodies of settled messages are purged after two days; queued ones
	// keep theirs.
	if _, err := pool.Exec(ctx, "update email_outbox set settled_at = now() - interval '49 hours' where id = $1", uuid.UUID(withdrawn)); err != nil {
		t.Fatal(err)
	}
	if purged, err := off.Purge(ctx); err != nil || purged != 1 {
		t.Fatalf("purge %d %v", purged, err)
	}
	var html, text string
	if err := pool.QueryRow(ctx, "select html, body_text from email_outbox where id = $1", uuid.UUID(withdrawn)).Scan(&html, &text); err != nil || html != "" || text != "" {
		t.Fatalf("purged body %q %q %v", html, text, err)
	}
	if err := pool.QueryRow(ctx, "select html from email_outbox where id = $1", uuid.UUID(queued)).Scan(&html); err != nil || html == "" {
		t.Fatalf("queued body %q %v", html, err)
	}
}

func TestWebhookSignature(t *testing.T) {
	now := time.Unix(1_790_000_000, 0)
	secret := "whsec_c2VjcmV0LWtleS1mb3ItdGVzdHM="
	body := []byte(`{"type":"email.delivered"}`)
	sign := func(ts string) string {
		key, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(secret, "whsec_"))
		if err != nil {
			t.Fatal(err)
		}
		mac := hmac.New(sha256.New, key)
		mac.Write([]byte("id_1." + ts + "."))
		mac.Write(body)
		return base64.StdEncoding.EncodeToString(mac.Sum(nil))
	}
	ts := "1790000000"
	if err := VerifyWebhook(secret, "id_1", ts, "v1,bogus v1,"+sign(ts), body, now); err != nil {
		t.Fatalf("valid: %v", err)
	}
	if err := VerifyWebhook(secret, "id_2", ts, "v1,"+sign(ts), body, now); !errors.Is(err, ErrBadSignature) {
		t.Fatalf("other id: %v", err)
	}
	old := "1789999000"
	if err := VerifyWebhook(secret, "id_1", old, "v1,"+sign(old), body, now); !errors.Is(err, ErrBadSignature) {
		t.Fatalf("replayed: %v", err)
	}
	event, err := ParseWebhook([]byte(`{"type":"email.clicked","created_at":"2026-09-30T00:00:00Z","data":{"email_id":"x"}}`))
	if err != nil || event != nil {
		t.Fatalf("ignored type %+v %v", event, err)
	}
}
