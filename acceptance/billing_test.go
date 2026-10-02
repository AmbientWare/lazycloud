package acceptance

import (
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// An endpoint whose account billing refuses answers 402 with billing's
// reason at once rather than wait out the cold start for a container
// planning will never start.
func TestRefusedAccountGetsPaymentRequiredAtOnce(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": endpointApp})
	p.deploy("api_demo",
		endpointSpec(source, "count_words", "app:count_words", "/word-count", apitypes.HttpMethodPOST),
		endpointSpec(source, "other", "app:count_words", "/word-count", apitypes.HttpMethodPOST))
	// A first request starts a container and the account with it.
	warm := p.describe("api_demo", apitypes.WorkloadKindEndpoint, "count_words")
	if status, _, body := p.call(http.MethodPost, warm.Url, `{"text": "a"}`); status != http.StatusOK {
		t.Fatalf("warm-up: %d %s", status, body)
	}
	if _, err := p.pool.Exec(t.Context(), `update billing_accounts set status = 'past_due', complimentary_since = null`); err != nil {
		t.Fatal(err)
	}

	cold := p.describe("api_demo", apitypes.WorkloadKindEndpoint, "other")
	began := time.Now()
	status, _, body := p.call(http.MethodPost, cold.Url, `{"text": "a"}`)
	took := time.Since(began)
	if status != http.StatusPaymentRequired || !strings.Contains(body, `"error":"a payment for this account did not go through`) {
		t.Fatalf("cold request of a past-due account: %d %s", status, body)
	}
	if took > 3*time.Second {
		t.Fatalf("the refusal took %v; it should not wait for a container", took)
	}
}
