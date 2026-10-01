package edge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// CloudflareAPI is the Cloudflare v4 API base.
const CloudflareAPI = "https://api.cloudflare.com/client/v4"

// Cloudflare is Cloudflare for SaaS custom hostnames on one zone. It acts
// on the identifier Cloudflare assigns, not the name: the name is the
// customer's and can be registered again, while the identifier names the
// object this platform created.
type Cloudflare struct {
	base   string
	zone   string
	token  string
	client *http.Client
}

// NewCloudflare returns the provider for zone. base is CloudflareAPI except
// in tests.
func NewCloudflare(base, zone, token string) *Cloudflare {
	return &Cloudflare{base: strings.TrimSuffix(base, "/"), zone: zone, token: token, client: &http.Client{Timeout: 30 * time.Second}}
}

// Certificate states Cloudflare reports while it is still working; anything
// else short of active is for the customer to fix.
var (
	cfValidating = map[string]bool{"initializing": true, "pending_issuance": true, "pending_deployment": true, "pending_cleanup": true} //nolint:gochecknoglobals // constant sets
	cfAwaiting   = map[string]bool{"pending_validation": true}                                                                          //nolint:gochecknoglobals // constant sets
)

type cfHostname struct {
	ID     string `json:"id"`
	Status string `json:"status"`
	SSL    struct {
		Status           string `json:"status"`
		ValidationErrors []struct {
			Message string `json:"message"`
		} `json:"validation_errors"`
		ValidationRecords []struct {
			TxtName  string `json:"txt_name"`
			TxtValue string `json:"txt_value"`
			Status   string `json:"status"`
		} `json:"validation_records"`
	} `json:"ssl"`
	OwnershipVerification *struct {
		Type  string `json:"type"`
		Name  string `json:"name"`
		Value string `json:"value"`
	} `json:"ownership_verification"`
}

type cfEnvelope struct {
	Success bool `json:"success"`
	Errors  []struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	} `json:"errors"`
	Result json.RawMessage `json:"result"`
}

var errCloudflareNotFound = errors.New("not found")

// CreateHostname asks Cloudflare to serve hostname. The CNAME the customer
// publishes to route traffic also proves control of the name (HTTP DCV), so
// one record does both.
func (c *Cloudflare) CreateHostname(ctx context.Context, hostname string) (ProviderHostname, error) {
	body := map[string]any{"hostname": hostname, "ssl": map[string]any{"method": "http", "type": "dv", "wildcard": false}}
	var out cfHostname
	if err := c.do(ctx, http.MethodPost, "/zones/"+url.PathEscape(c.zone)+"/custom_hostnames", body, &out); err != nil {
		return ProviderHostname{}, err
	}
	return out.state()
}

// GetHostname re-reads one hostname.
func (c *Cloudflare) GetHostname(ctx context.Context, id string) (ProviderHostname, bool, error) {
	var out cfHostname
	err := c.do(ctx, http.MethodGet, "/zones/"+url.PathEscape(c.zone)+"/custom_hostnames/"+url.PathEscape(id), nil, &out)
	if errors.Is(err, errCloudflareNotFound) {
		return ProviderHostname{}, false, nil
	}
	if err != nil {
		return ProviderHostname{}, false, err
	}
	state, err := out.state()
	return state, err == nil, err
}

// DeleteHostname stops serving a hostname; one already gone is the state
// asked for.
func (c *Cloudflare) DeleteHostname(ctx context.Context, id string) error {
	err := c.do(ctx, http.MethodDelete, "/zones/"+url.PathEscape(c.zone)+"/custom_hostnames/"+url.PathEscape(id), nil, nil)
	if errors.Is(err, errCloudflareNotFound) {
		return nil
	}
	return err
}

func (c *Cloudflare) do(ctx context.Context, method, path string, body any, result any) error {
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("encode cloudflare request: %w", err)
		}
		reader = bytes.NewReader(encoded)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, reader)
	if err != nil {
		return fmt.Errorf("build cloudflare request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.client.Do(req)
	if err != nil {
		return &ProviderError{Message: "Cloudflare request failed: " + err.Error()}
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode == http.StatusNotFound {
		return errCloudflareNotFound
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return &ProviderError{Message: "Cloudflare response unreadable: " + err.Error()}
	}
	var envelope cfEnvelope
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return &ProviderError{Message: fmt.Sprintf("Cloudflare returned an unreadable response (%d)", resp.StatusCode)}
	}
	if !envelope.Success {
		parts := make([]string, 0, len(envelope.Errors))
		for _, e := range envelope.Errors {
			parts = append(parts, fmt.Sprintf("%d: %s", e.Code, e.Message))
		}
		message := strings.Join(parts, "; ")
		if message == "" {
			message = fmt.Sprintf("Cloudflare rejected the request (%d)", resp.StatusCode)
		}
		// A rejected hostname is the caller's input; anything else is the
		// edge's problem and worth retrying.
		return &ProviderError{Rejected: resp.StatusCode >= 400 && resp.StatusCode < 500, Message: message}
	}
	if result == nil {
		return nil
	}
	if err := json.Unmarshal(envelope.Result, result); err != nil {
		return &ProviderError{Message: "Cloudflare reported success without a custom hostname"}
	}
	return nil
}

func (h cfHostname) state() (ProviderHostname, error) {
	if h.ID == "" || h.Status == "" || h.SSL.Status == "" {
		return ProviderHostname{}, &ProviderError{Message: "Cloudflare reported success without a custom hostname"}
	}
	out := ProviderHostname{ID: h.ID}
	switch {
	case h.SSL.Status == "active" && h.Status == "active":
		out.Phase = apitypes.DomainPhaseReady
	case cfAwaiting[h.SSL.Status]:
		out.Phase = apitypes.DomainPhaseAwaitingVerification
	case cfValidating[h.SSL.Status] || h.Status == "pending":
		out.Phase = apitypes.DomainPhaseValidating
	default:
		out.Phase = apitypes.DomainPhaseActionRequired
		code := apitypes.CertificateFailed
		out.ErrorCode = &code
	}
	// Only records still outstanding: a satisfied one listed as missing
	// sends a customer looking for a problem that is not there.
	for _, r := range h.SSL.ValidationRecords {
		if r.TxtName != "" && r.TxtValue != "" && r.Status != "active" {
			out.Records = append(out.Records, apitypes.DnsRecord{Type: "TXT", Name: r.TxtName, Value: r.TxtValue})
		}
	}
	if o := h.OwnershipVerification; h.Status == "pending" && o != nil && o.Name != "" && o.Value != "" {
		kind := strings.ToUpper(o.Type)
		if kind == "" {
			kind = "TXT"
		}
		out.Records = append(out.Records, apitypes.DnsRecord{Type: kind, Name: o.Name, Value: o.Value})
	}
	var messages []string
	for _, e := range h.SSL.ValidationErrors {
		if e.Message != "" {
			messages = append(messages, e.Message)
		}
	}
	if joined := strings.Join(messages, "; "); joined != "" {
		if len(joined) > 512 {
			joined = joined[:512]
		}
		out.ErrorMessage = &joined
	}
	return out, nil
}
