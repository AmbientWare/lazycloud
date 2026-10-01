package notifications

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// ResendAPIURL is Resend's API base URL.
const ResendAPIURL = "https://api.resend.com"

// sendTimeout bounds one send; a slow provider delays a message, never a
// person's request, because the message is already committed.
const sendTimeout = 10 * time.Second

// ResendConfig holds the Resend credentials. APIURL is ResendAPIURL in
// production.
type ResendConfig struct {
	APIURL string
	APIKey string
	// From must be on a domain the Resend account verified.
	From string
}

// Resend sends email through Resend's HTTP API.
type Resend struct {
	cfg    ResendConfig
	client *http.Client
}

// NewResend returns a sender for cfg.
func NewResend(cfg ResendConfig) *Resend {
	return &Resend{cfg: cfg, client: &http.Client{Timeout: sendTimeout}}
}

// RefusedError is a send Resend answered with an error. Permanent refusals
// (a malformed recipient) fail identically on every retry; rate limits,
// rejected keys and unverified domains are fixed by waiting or by an
// operator, so they retry.
type RefusedError struct {
	Status    int
	Message   string
	Permanent bool
}

func (e *RefusedError) Error() string {
	return fmt.Sprintf("resend refused the message (%d): %s", e.Status, e.Message)
}

// Send delivers email and returns Resend's id for it. The message id is the
// idempotency key, so a retry of a send whose answer was lost does not send
// the message twice.
func (r *Resend) Send(ctx context.Context, id MessageID, email Email) (string, error) {
	body, err := json.Marshal(map[string]any{
		"from": r.cfg.From, "to": []string{email.To}, "subject": email.Subject,
		"html": email.HTML, "text": email.Text,
	})
	if err != nil {
		return "", fmt.Errorf("encode email: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(r.cfg.APIURL, "/")+"/emails", bytes.NewReader(body))
	if err != nil {
		return "", fmt.Errorf("build resend request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+r.cfg.APIKey)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", id.String())
	resp, err := r.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("reach resend: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	if err != nil {
		return "", fmt.Errorf("read resend answer: %w", err)
	}
	if resp.StatusCode >= 300 {
		var detail struct {
			Message string `json:"message"`
			Name    string `json:"name"`
		}
		_ = json.Unmarshal(data, &detail)
		message := detail.Message
		if message == "" {
			message = detail.Name
		}
		if message == "" {
			message = http.StatusText(resp.StatusCode)
		}
		permanent := resp.StatusCode >= 400 && resp.StatusCode < 500 &&
			resp.StatusCode != http.StatusTooManyRequests && resp.StatusCode != http.StatusUnauthorized &&
			resp.StatusCode != http.StatusForbidden && resp.StatusCode != http.StatusConflict
		return "", &RefusedError{Status: resp.StatusCode, Message: message, Permanent: permanent}
	}
	var accepted struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(data, &accepted); err != nil || accepted.ID == "" {
		// Without the id no delivery report can find the message.
		return "", errors.New("resend accepted the message without an id")
	}
	return accepted.ID, nil
}

// Svix signs Resend's webhook deliveries with these headers.
const (
	WebhookIDHeader        = "svix-id"
	WebhookTimestampHeader = "svix-timestamp"
	WebhookSignatureHeader = "svix-signature"
	// webhookTolerance is how far a delivery's timestamp may be from now;
	// it stops a captured delivery from being replayed later.
	webhookTolerance = 5 * time.Minute
)

// ErrBadSignature means a webhook delivery is not provably from Resend.
var ErrBadSignature = errors.New("webhook signature does not verify")

// VerifyWebhook checks a Svix signature over body. secret is the endpoint
// secret Resend issued, with its whsec_ prefix.
func VerifyWebhook(secret, id, timestamp, signatures string, body []byte, now time.Time) error {
	sent, err := strconv.ParseInt(timestamp, 10, 64)
	if err != nil {
		return fmt.Errorf("%w: timestamp is not a unix time", ErrBadSignature)
	}
	if d := now.Sub(time.Unix(sent, 0)); d > webhookTolerance || d < -webhookTolerance {
		return fmt.Errorf("%w: timestamp is outside the accepted window", ErrBadSignature)
	}
	key, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(secret, "whsec_"))
	if err != nil {
		return fmt.Errorf("decode webhook secret: %w", err)
	}
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(id + "." + timestamp + "."))
	mac.Write(body)
	expected := base64.StdEncoding.EncodeToString(mac.Sum(nil))
	// Several space-separated v1 signatures are valid while a secret rotates.
	for _, candidate := range strings.Fields(signatures) {
		version, value, _ := strings.Cut(candidate, ",")
		if version == "v1" && hmac.Equal([]byte(value), []byte(expected)) {
			return nil
		}
	}
	return ErrBadSignature
}

// webhookStates maps the Resend events delivery records follow.
var webhookStates = map[string]DeliveryState{ //nolint:gochecknoglobals // Constant lookup table.
	"email.sent":       StateSent,
	"email.delivered":  StateDelivered,
	"email.bounced":    StateBounced,
	"email.complained": StateComplained,
}

// ParseWebhook reads a verified delivery. It returns nil for event types
// delivery records do not follow, which the endpoint acknowledges so Resend
// does not retry them.
func ParseWebhook(body []byte) (*DeliveryEvent, error) {
	var delivery struct {
		Type      string    `json:"type"`
		CreatedAt time.Time `json:"created_at"`
		Data      struct {
			EmailID string `json:"email_id"`
			Bounce  struct {
				Message string `json:"message"`
				SubType string `json:"subType"`
			} `json:"bounce"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &delivery); err != nil {
		return nil, fmt.Errorf("decode resend event: %w", err)
	}
	state, ok := webhookStates[delivery.Type]
	if !ok || delivery.Data.EmailID == "" || delivery.CreatedAt.IsZero() {
		return nil, nil //nolint:nilnil // An event nobody follows is not an error.
	}
	detail := strings.TrimSpace(delivery.Data.Bounce.SubType + " " + delivery.Data.Bounce.Message)
	return &DeliveryEvent{
		ProviderMessageID: delivery.Data.EmailID, State: state, OccurredAt: delivery.CreatedAt.UTC(), Detail: detail,
	}, nil
}
