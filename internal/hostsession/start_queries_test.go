package hostsession_test

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/hostsession"
)

// The statements a start runs do not grow with its volumes and cloud
// buckets: the platform volumes are recorded in one batch and every secret
// the start needs is read at once.
func TestStartStatementsDoNotGrowWithVolumes(t *testing.T) {
	count := func(volumes, buckets int) int64 {
		h, _, queries := countedHarness(t, func(c *hostsession.Config) { c.TouchInterval = time.Hour })
		host, ctx := h.enroll()
		var mounts []string
		for n := range volumes {
			mounts = append(mounts, fmt.Sprintf(`{"name": "data%d", "mount_path": "data%d"}`, n, n))
		}
		for n := range buckets {
			mounts = append(mounts, fmt.Sprintf(`{"name": "bucket%d", "mount_path": "bucket%d", "cloud_bucket": {"bucket": "customer-%d",
				"region": "us-east-2", "access_key_secret": "KEY_%d", "secret_key_secret": "SECRET_%d"}}`, n, n, n, n, n))
		}
		ws, _ := h.startingContainerWith(host, `{"handler": "a:b", "image": {"python_version": "3.12"}, "secrets": ["TOKEN"],
			"volumes": [`+strings.Join(mounts, ",")+`]}`)
		names := []string{"TOKEN"}
		for n := range buckets {
			names = append(names, fmt.Sprintf("KEY_%d", n), fmt.Sprintf("SECRET_%d", n))
		}
		for _, name := range names {
			if _, err := h.secrets.Set(t.Context(), ws, name, "value"); err != nil {
				t.Fatal(err)
			}
		}
		queries.settle()
		stream := open(t, ctx, h.client)
		for receive(t, stream).GetStart() == nil {
		}
		return queries.settle()
	}
	one, many := count(1, 1), count(4, 3)
	t.Logf("a start with one volume and one cloud bucket ran %d statements; with four and three, %d", one, many)
	if many != one {
		t.Fatalf("a start ran %d statements with four volumes and three cloud buckets, %d with one of each", many, one)
	}
}
