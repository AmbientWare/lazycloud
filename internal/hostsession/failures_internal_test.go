package hostsession

import (
	"context"
	"errors"
	"fmt"
	"testing"

	"github.com/aws/smithy-go"

	"github.com/AmbientWare/lazycloud/internal/secrets"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// Only errors no retry can fix, and storage a start needs that is
// unavailable, fail a container, with a reason fit for its owner that holds
// none of the platform's own error text; anything else returns to the
// session to be built again.
func TestOnlyStartErrorsOfTheContainerFailIt(t *testing.T) {
	cases := []struct {
		err    error
		failed bool
		reason string
	}{
		{fmt.Errorf("resolve: %w", &secrets.NotFoundError{Name: "API_KEY"}), true, "secret not found: API_KEY"},
		{&secrets.UnreadableError{Name: "API_KEY", Err: errors.New("key 7 gone")}, true, "secret API_KEY cannot be read"},
		{fmt.Errorf("release names image img_1: %w", errImageUnpinned), true, "the release's image has no pinned reference; deploy it again"},
		{&storageRefusal{what: "volumes", err: fmt.Errorf("mount volumes: %w", storage.ErrBucketsUnconfigured)}, true,
			"volumes unavailable: workspace buckets are not configured"},
		{&storageRefusal{what: "cloud bucket at /data", err: storage.ErrNoRegion}, true,
			"cloud bucket at /data unavailable: an AWS S3 bucket needs a region"},
		{&storageRefusal{what: "storage grant", err: fmt.Errorf("assume role: %w", &smithy.GenericAPIError{
			Code: "AccessDenied", Message: "arn:aws:iam::111111111111:role/platform is not authorized",
		})}, true, "storage grant unavailable: the object store refused it (AccessDenied)"},
		{&storageRefusal{what: "volumes", err: errors.New("lock volume data: database is restarting")}, true,
			"volumes unavailable: it failed on the platform's side"},
		{&storageRefusal{what: "volumes", err: context.Canceled}, false, ""},
		{errors.New("presign source download: connection refused"), false, ""},
		{fmt.Errorf("read image img_1: %w", errors.New("database is restarting")), false, ""},
		{nil, false, ""},
	}
	for _, c := range cases {
		reason, failed := startFailure(c.err)
		if failed != c.failed || reason != c.reason {
			t.Errorf("%v: failed %v %q; want %v %q", c.err, failed, reason, c.failed, c.reason)
		}
	}
}
