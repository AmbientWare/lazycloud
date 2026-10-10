package agent

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// Starts of one archive share its download, and a start whose deadline
// ends the shared download leaves a start with time left to fetch it.
func TestASharedDownloadOutlivesAnotherStartsDeadline(t *testing.T) {
	archive := []byte("the archive")
	sum := sha256.Sum256(archive)
	var requests atomic.Int32
	arrived := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if requests.Add(1) == 1 {
			close(arrived)
			<-r.Context().Done()
			return
		}
		_, _ = w.Write(archive)
	}))
	t.Cleanup(server.Close)
	cache := &sourceCache{dir: t.TempDir(), http: server.Client()}
	src := &hostproto.Source{Url: server.URL, Sha256: hex.EncodeToString(sum[:])}

	short, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	first := make(chan error, 1)
	go func() {
		_, err := cache.fetch(short, src)
		first <- err
	}()
	<-arrived
	if _, err := cache.fetch(t.Context(), src); err != nil {
		t.Fatalf("a start that joined a download another start's deadline ended failed: %v", err)
	}
	if err := <-first; !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("the start past its deadline gave %v", err)
	}
}
