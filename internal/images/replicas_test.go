package images_test

import (
	"log/slog"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// replicaRegion has a copy of the layer bucket in these tests. Garage
// signs requests for one region only, so the copy is another bucket of the
// same store under that region's name.
const replicaRegion = "garage"

// replicated is the development store with the platform bucket standing in
// for replicaRegion's copy of the layer bucket.
func replicated() storage.Config {
	cfg := storagetest.Config()
	cfg.LayerReplicas = map[string]string{replicaRegion: cfg.Bucket}
	return cfg
}

// replicate copies the pairs of reference's layers at positions into
// replicaRegion's copy, as S3 replication does.
func replicate(t *testing.T, pool *pgxpool.Pool, reference string, positions ...int) {
	t.Helper()
	cfg := replicated()
	client := s3.New(s3.Options{
		Region: cfg.Region, BaseEndpoint: aws.String(cfg.Endpoint), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider(cfg.AccessKeyID, cfg.SecretAccessKey, ""),
	})
	for _, position := range positions {
		var id uuid.UUID
		if err := pool.QueryRow(t.Context(), "select layer_id from image_reference_layers where reference = $1 and position = $2", reference, position).Scan(&id); err != nil {
			t.Fatal(err)
		}
		for _, object := range []string{"index", "data"} {
			key := "layers/" + id.String() + "/" + object
			if _, err := client.CopyObject(t.Context(), &s3.CopyObjectInput{
				Bucket: aws.String(cfg.LayerReplicas[replicaRegion]), Key: aws.String(key), CopySource: aws.String(cfg.LayerBucket + "/" + key),
			}); err != nil {
				t.Fatal(err)
			}
		}
	}
}

// bucketsOf names the bucket each layer's index and data URLs read.
func bucketsOf(t *testing.T, reads images.LayerReads) []string {
	t.Helper()
	var out []string
	for _, l := range reads.Layers {
		for _, raw := range []string{l.Index, l.Data} {
			u, err := url.Parse(raw)
			if err != nil {
				t.Fatal(err)
			}
			out = append(out, strings.SplitN(strings.TrimPrefix(u.Path, "/"), "/", 2)[0])
		}
	}
	return out
}

func readsOf(t *testing.T, im *images.Images, reference string, host compute.HostID) images.LayerReads {
	t.Helper()
	reads, err := im.LayerReadURLs(t.Context(), reference, host, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	return reads
}

// A host reads a layer from its region's copy only once a check found the
// copy holding it; until then, and in a region without a copy, it reads
// the layer bucket. A check that found a layer missing waits out the
// recheck period, and the sweep takes a layer's checks with it.
func TestGrantsReadTheirRegionsCopyOnceConfirmed(t *testing.T) {
	ctx := t.Context()
	pool := dbtest.New(t)
	cfg := replicated()
	store := storage.NewStorage(pool, cfg)
	im := images.NewImages(pool, execution.NewExecution(pool), newVault(t, pool), store, images.Config{})
	reference := "registry.test/lazycloud/images/regional@sha256:" + hex64("1")
	convertReference(t, pool, store, reference, [][]byte{[]byte("base layer"), []byte("app layer")})
	regional, elsewhere := hostIn(t, pool, replicaRegion), hostIn(t, pool, "us-west-2")
	main, copied := cfg.LayerBucket, cfg.LayerReplicas[replicaRegion]
	expect := func(host compute.HostID, unconfirmed string, buckets ...string) {
		t.Helper()
		reads := readsOf(t, im, reference, host)
		if got := bucketsOf(t, reads); strings.Join(got, " ") != strings.Join(buckets, " ") || reads.Unconfirmed != unconfirmed {
			t.Fatalf("reads %v, unconfirmed %q; want %v, %q", got, reads.Unconfirmed, buckets, unconfirmed)
		}
	}
	recheck := func() {
		t.Helper()
		if _, err := pool.Exec(ctx, "update image_layer_replicas set checked_at = checked_at - interval '2 minutes'"); err != nil {
			t.Fatal(err)
		}
	}
	confirm := func(want int, pending bool) {
		t.Helper()
		check, err := im.ConfirmReplicas(ctx, reference, replicaRegion, time.Minute)
		if err != nil || check.Checked != want || check.Pending != pending {
			t.Fatalf("checked %+v (%v), want %d checked, pending %v", check, err, want, pending)
		}
	}

	expect(regional, replicaRegion, main, main, main, main)
	expect(elsewhere, "", main, main, main, main)
	if check, err := im.ConfirmReplicas(ctx, reference, "us-west-2", time.Minute); err != nil || check.Checked != 0 {
		t.Fatalf("a region without a copy was checked: %+v %v", check, err)
	}

	confirm(2, true)
	expect(regional, replicaRegion, main, main, main, main)
	replicate(t, pool, reference, 0)
	confirm(0, true)
	expect(regional, replicaRegion, main, main, main, main)

	recheck()
	confirm(2, true)
	expect(regional, replicaRegion, copied, copied, main, main)
	if ix := readIndex(t, readsOf(t, im, reference, regional).Layers[0].Index); len(ix.Entries) == 0 {
		t.Fatal("the copy's index reads back empty")
	}

	replicate(t, pool, reference, 1)
	recheck()
	confirm(1, false)
	expect(regional, "", copied, copied, copied, copied)
	recheck()
	confirm(0, false)
	expect(elsewhere, "", main, main, main, main)

	if _, err := pool.Exec(ctx, "update image_layers set unreferenced_since = now() - interval '25 hours'"); err != nil {
		t.Fatal(err)
	}
	if _, err := im.SweepLayers(ctx, slog.New(slog.DiscardHandler)); err != nil {
		t.Fatal(err)
	}
	var left int
	if err := pool.QueryRow(ctx, "select count(*) from image_layer_replicas").Scan(&left); err != nil || left != 0 {
		t.Fatalf("%d checks outlived their swept layers (%v)", left, err)
	}
}

// Concurrent checks of one reference and region on two servers check each
// layer once between them.
func TestReplicaChecksRunOncePerLayerAcrossServers(t *testing.T) {
	ctx := t.Context()
	pool := dbtest.New(t)
	store := storage.NewStorage(pool, replicated())
	servers := []*images.Images{
		images.NewImages(pool, execution.NewExecution(pool), newVault(t, pool), store, images.Config{}),
		images.NewImages(pool, execution.NewExecution(pool), newVault(t, pool), storage.NewStorage(pool, replicated()), images.Config{}),
	}
	reference := "registry.test/lazycloud/images/deduped@sha256:" + hex64("2")
	convertReference(t, pool, store, reference, [][]byte{[]byte("one"), []byte("two"), []byte("three")})
	replicate(t, pool, reference, 0, 1, 2)

	var mu sync.Mutex
	var wg sync.WaitGroup
	total := 0
	for n := range 8 {
		wg.Go(func() {
			check, err := servers[n%2].ConfirmReplicas(ctx, reference, replicaRegion, time.Minute)
			if err != nil {
				t.Error(err)
			}
			mu.Lock()
			total += check.Checked
			mu.Unlock()
		})
	}
	wg.Wait()
	if total != 3 {
		t.Fatalf("8 concurrent checks checked %d layers, want 3", total)
	}
	reads := readsOf(t, servers[0], reference, hostIn(t, pool, replicaRegion))
	if reads.Unconfirmed != "" {
		t.Fatalf("a layer is unconfirmed after the checks: %v", bucketsOf(t, reads))
	}
}
