package agent

import (
	"context"
	"net"
	"net/http"
	"path/filepath"
	"testing"
	"time"

	"github.com/moby/moby/client"
)

// A Docker that does not answer delays a session open by the listing's
// timeout, not longer.
func TestListingRunningPlatformImagesIsBounded(t *testing.T) {
	socket := filepath.Join(t.TempDir(), "docker.sock")
	lis, err := (&net.ListenConfig{}).Listen(t.Context(), "unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	hung := make(chan struct{})
	server := &http.Server{ReadHeaderTimeout: time.Second, Handler: http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-hung:
		}
	})}
	go func() { _ = server.Serve(lis) }()
	t.Cleanup(func() { close(hung); _ = server.Close() })
	docker, err := client.New(client.WithHost("unix://"+socket), client.WithAPIVersion("1.47"))
	if err != nil {
		t.Fatal(err)
	}
	a := &Agent{docker: docker}
	began := time.Now()
	if _, err := a.runningPlatformImages(context.Background()); err == nil {
		t.Fatal("a Docker that does not answer listed containers")
	}
	if waited := time.Since(began); waited > platformListTimeout+2*time.Second {
		t.Fatalf("the listing waited %s", waited)
	}
}
