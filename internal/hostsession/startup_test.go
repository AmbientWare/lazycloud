package hostsession_test

import (
	"io"
	"log"
	"log/slog"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/google/go-containerregistry/pkg/name"
	memregistry "github.com/google/go-containerregistry/pkg/registry"
	"github.com/google/go-containerregistry/pkg/v1/random"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostsession"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/secrets"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// A server that starts converts the managed image for the architectures the
// fleet sells, with no host connected and no start asking, so the first start
// after a deploy finds it. Other architectures wait for a host to ask.
func TestAStartingServerConvertsTheManagedImage(t *testing.T) {
	pool := dbtest.New(t)
	server := httptest.NewServer(memregistry.New(memregistry.Logger(log.New(io.Discard, "", 0))))
	t.Cleanup(server.Close)
	registry := strings.TrimPrefix(server.URL, "http://")
	img, err := random.Image(4096, 2)
	if err != nil {
		t.Fatal(err)
	}
	template, err := name.ParseReference(registry+"/library/python:3.12-slim", name.Insecure)
	if err != nil {
		t.Fatal(err)
	}
	if err := remote.Write(template, img, remote.WithContext(t.Context())); err != nil {
		t.Fatal(err)
	}
	logger := slog.New(slog.NewTextHandler(t.Output(), nil))
	e := execution.NewExecution(pool)
	key, err := secrets.NewFileKey(make([]byte, 32))
	if err != nil {
		t.Fatal(err)
	}
	store := storage.NewStorage(pool, storagetest.Config())
	im := images.NewImages(pool, e, secrets.NewSecrets(pool, key), store, images.Config{
		Registry: registry, Repository: "lazycloud", Insecure: true, ManagedBase: registry + "/library/python:{version}-slim",
	})
	srv := hostsession.NewServer(compute.NewCompute(pool, e, compute.Config{}), e, store, im,
		database.NewListener(pool, logger, database.ChannelImageBuild), hostsession.Config{TouchInterval: time.Second}, logger)
	srv.ConvertAtStart(nil, compute.FleetArchitectures())
	// With no session open, Wait returns once the start's conversions end.
	srv.Wait()
	t.Cleanup(func() { srv.Shutdown(); srv.Wait() })

	var host uuid.UUID
	if err := pool.QueryRow(t.Context(), `insert into hosts (name, state, cpu_millis, memory_bytes) values ('h', 'offline', 1000, 1 << 30) returning id`).Scan(&host); err != nil {
		t.Fatal(err)
	}
	pull, err := im.ManagedPull(t.Context(), compute.HostID(host), "3.12")
	if err != nil || !strings.HasPrefix(pull.Reference, registry+"/lazycloud/platform/") {
		t.Fatalf("the managed image pulls %s (%v)", pull.Reference, err)
	}
	var converted []string
	if err := pool.QueryRow(t.Context(), "select array_agg(distinct architecture) from platform_images").Scan(&converted); err != nil ||
		!slices.Equal(converted, []string{"amd64"}) {
		t.Fatalf("the server converted for %v (%v), want only amd64, what the fleet sells", converted, err)
	}
	var builds int
	if err := pool.QueryRow(t.Context(), "select count(*) from image_builds").Scan(&builds); err != nil || builds != 0 {
		t.Fatalf("%d builds ran for it (%v)", builds, err)
	}
}
