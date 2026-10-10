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

// Only errors no retry can fix, and the storage owner's refusals, fail a
// container, with a reason fit for its owner that holds none of the
// platform's own error text. A temporary storage failure, like any other
// error, returns to the session, which builds the start again.
func TestOnlyStartErrorsOfTheContainerFailIt(t *testing.T) {
	denied := &storage.StoreRefusedError{Code: "AccessDenied", Err: &smithy.GenericAPIError{
		Code: "AccessDenied", Message: "arn:aws:iam::111111111111:role/platform is not authorized",
	}}
	cases := []struct {
		err    error
		failed bool
		reason string
	}{
		{fmt.Errorf("resolve: %w", &secrets.NotFoundError{Name: "API_KEY"}), true, "secret not found: API_KEY"},
		{&secrets.UnreadableError{Name: "API_KEY", Err: errors.New("key 7 gone")}, true, "secret API_KEY cannot be read"},
		{fmt.Errorf("release names image img_1: %w", errImageUnpinned), true, "the release's image has no pinned reference; deploy it again"},
		{refusal("volumes", fmt.Errorf("mount volumes: %w", storage.ErrBucketsUnconfigured)), true,
			"volumes unavailable: workspace buckets are not configured"},
		{refusal("cloud bucket at /data", storage.ErrNoRegion), true, "cloud bucket at /data unavailable: an AWS S3 bucket needs a region"},
		{refusal("volumes", &storage.ConflictError{Reason: "volume data is being deleted"}), true,
			"volumes unavailable: volume data is being deleted"},
		{refusal("storage grant", fmt.Errorf("issue storage grant: %w", denied)), true,
			"storage grant unavailable: the object store refused it (AccessDenied)"},
		{refusal("volumes", errors.New("lock volume data: database is restarting")), false, ""},
		{refusal("storage grant", &smithy.GenericAPIError{Code: "SlowDown"}), false, ""},
		{refusal("volumes", context.Canceled), false, ""},
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
