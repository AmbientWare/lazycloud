package compute_test

import (
	"bytes"
	"log/slog"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// Prod's scheduler logged "host lost" with host_id as an array of 16 bytes.
func TestHostIDsLogAsUUIDs(t *testing.T) {
	id := uuid.MustParse("01a0ff02-8d0f-7540-a7c8-8453e2b993b9")
	var out bytes.Buffer
	slog.New(slog.NewJSONHandler(&out, nil)).Info("host lost", "host_id", compute.HostID(id))
	if !strings.Contains(out.String(), `"host_id":"01a0ff02-8d0f-7540-a7c8-8453e2b993b9"`) {
		t.Fatalf("log line %s, want the host id as a UUID string", out.String())
	}
}
