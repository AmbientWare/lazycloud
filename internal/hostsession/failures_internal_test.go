package hostsession

import (
	"errors"
	"fmt"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/secrets"
)

// Only errors no retry can fix fail a container, with a reason fit for its
// owner; anything else returns to the session to be built again.
func TestOnlyPermanentStartErrorsFailTheContainer(t *testing.T) {
	cases := []struct {
		err       error
		permanent bool
		reason    string
	}{
		{fmt.Errorf("resolve: %w", &secrets.NotFoundError{Name: "API_KEY"}), true, "the release names secret API_KEY, which the workspace does not have"},
		{&secrets.UnreadableError{Name: "API_KEY", Err: errors.New("key 7 gone")}, true, "secret API_KEY cannot be read"},
		{fmt.Errorf("release names image img_1: %w", errImageUnpinned), true, "the release's image has no pinned reference; deploy it again"},
		{errors.New("presign source download: connection refused"), false, ""},
		{fmt.Errorf("read image img_1: %w", errors.New("database is restarting")), false, ""},
		{nil, false, ""},
	}
	for _, c := range cases {
		reason, permanent := permanentStartFailure(c.err)
		if permanent != c.permanent || reason != c.reason {
			t.Errorf("%v: permanent %v %q; want %v %q", c.err, permanent, reason, c.permanent, c.reason)
		}
	}
}
