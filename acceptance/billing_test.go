//go:build hostruntime

package acceptance

import (
	"net/http"
	"strings"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// A cold endpoint whose account billing refuses answers 402 with billing's
// reason, and planning starts no container for it.
func TestRefusedAccountGetsPaymentRequired(t *testing.T) {
	t.Parallel()
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": endpointApp})
	d := p.deploy("api_demo", endpointSpec(source, "count_words", "app:count_words", "/word-count", apitypes.HttpMethodPOST))
	// The deploy's admission opened the account.
	if tag, err := p.pool.Exec(t.Context(), `update billing_accounts set status = 'past_due', complimentary_since = null`); err != nil || tag.RowsAffected() != 1 {
		t.Fatalf("make the account past due: %d rows, %v", tag.RowsAffected(), err)
	}
	w := p.describe("api_demo", apitypes.WorkloadKindEndpoint, "count_words")
	status, _, body := p.call(http.MethodPost, w.Url, `{"text": "a"}`)
	if status != http.StatusPaymentRequired || !strings.Contains(body, `"error":"a payment for this account did not go through`) {
		t.Fatalf("cold request of a past-due account: %d %s", status, body)
	}
	var containers int
	if err := p.pool.QueryRow(t.Context(), `select count(*) from containers where release_id = $1`, d.Releases[0].Id).Scan(&containers); err != nil || containers != 0 {
		t.Fatalf("the refused endpoint has %d containers, %v", containers, err)
	}
}
