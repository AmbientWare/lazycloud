package edge

import (
	"context"
	"crypto/sha256"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

const (
	// authTTL is how long an authorized token and workspace pair skips the
	// database. A revoked token keeps working for at most this long.
	authTTL = 5 * time.Second
	// maxAuthEntries bounds the cache; it starts over when full.
	maxAuthEntries = 10_000
)

type authKey struct {
	token     [sha256.Size]byte
	workspace uuid.UUID
}

// authCache remembers recent successful authorizations, so a warm request
// costs no database round trip.
type authCache struct {
	mu      sync.Mutex
	entries map[authKey]time.Time
}

func bearerToken(r *http.Request) string {
	scheme, token, ok := strings.Cut(r.Header.Get("Authorization"), " ")
	if !ok || !strings.EqualFold(scheme, "bearer") {
		return ""
	}
	return strings.TrimSpace(token)
}

// authorize checks that the request's bearer token may act in the
// workload's workspace, with the policy every API entry point applies.
func (e *Edge) authorize(ctx context.Context, r *http.Request, w *workload) error {
	token := bearerToken(r)
	if token == "" {
		return identity.ErrUnauthenticated
	}
	key := authKey{token: sha256.Sum256([]byte(token)), workspace: uuid.UUID(w.workspace)}
	now := time.Now()
	e.auth.mu.Lock()
	expires, cached := e.auth.entries[key]
	e.auth.mu.Unlock()
	if cached && now.Before(expires) {
		return nil
	}
	principal, err := e.identity.Authenticate(ctx, token)
	if err != nil {
		return err
	}
	ws, err := e.identity.AuthorizeWorkspace(ctx, principal, w.workspaceName)
	if err != nil {
		return err
	}
	if ws.ID != w.workspace {
		return identity.ErrForbidden
	}
	e.auth.mu.Lock()
	if len(e.auth.entries) >= maxAuthEntries {
		clear(e.auth.entries)
	}
	e.auth.entries[key] = now.Add(authTTL)
	e.auth.mu.Unlock()
	return nil
}
