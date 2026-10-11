package diskengine

import (
	"context"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
)

// One engine call can outlast one grant, so the store asks for credentials
// again before the ones it holds expire, and refuses ones already expired.
func TestStoreRefreshesCredentialsBeforeExpiry(t *testing.T) {
	store := testStore(t)
	fetch := store.Credentials
	var calls atomic.Int32
	// Held credentials count as due a credentialMargin before they expire.
	due := time.Now().Add(700 * time.Millisecond)
	store.Credentials = aws.CredentialsProviderFunc(func(ctx context.Context) (aws.Credentials, error) {
		calls.Add(1)
		current, err := fetch.Retrieve(ctx)
		current.CanExpire, current.Expires = true, due.Add(credentialMargin)
		return current, err
	})
	objects, err := openStore(store)
	if err != nil {
		t.Fatal(err)
	}
	head := func() {
		t.Helper()
		if err := objects.Put(t.Context(), "present", []byte("x")); err != nil {
			t.Fatal(err)
		}
	}
	head()
	head()
	if got := calls.Load(); got != 1 {
		t.Fatalf("asked for credentials %d times while the first were far from expiry", got)
	}
	time.Sleep(time.Until(due) + 50*time.Millisecond)
	head()
	if got := calls.Load(); got != 2 {
		t.Fatalf("asked for credentials %d times, want a second request within the margin", got)
	}

	store.Credentials = aws.CredentialsProviderFunc(func(ctx context.Context) (aws.Credentials, error) {
		current, err := fetch.Retrieve(ctx)
		current.CanExpire, current.Expires = true, time.Now().Add(-time.Second)
		return current, err
	})
	expired, err := openStore(store)
	if err != nil {
		t.Fatal(err)
	}
	if err := expired.Put(t.Context(), "present", []byte("x")); err == nil || !strings.Contains(err.Error(), "expired") {
		t.Fatalf("expired credentials returned %v", err)
	}
}
