package diskengine

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"
)

// One engine call can outlast one grant, so the store asks for credentials
// again before the ones it holds expire, and refuses ones already expired.
func TestStoreRefreshesCredentialsBeforeExpiry(t *testing.T) {
	store := testStore(t)
	fetch := store.Credentials
	var calls atomic.Int32
	// Held credentials count as due a credentialMargin before they expire.
	due := time.Now().Add(700 * time.Millisecond)
	store.Credentials = func(ctx context.Context) (Credentials, error) {
		calls.Add(1)
		current, err := fetch(ctx)
		current.ExpiresAt = due.Add(credentialMargin)
		return current, err
	}
	objects := mustOpenStore(t, store)
	head := func() {
		t.Helper()
		if _, err := objects.exists(t.Context(), store.Prefix+"absent"); err != nil {
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

	store.Credentials = func(ctx context.Context) (Credentials, error) {
		current, err := fetch(ctx)
		current.ExpiresAt = time.Now().Add(-time.Second)
		return current, err
	}
	_, err := mustOpenStore(t, store).exists(t.Context(), store.Prefix+"absent")
	if !errors.Is(err, ErrCredentialsExpired) {
		t.Fatalf("expired credentials returned %v", err)
	}
}
