package diskengine

import (
	"errors"
	"testing"
)

// The engine refuses a malformed request before touching anything.
func TestAttachRefusesMalformedRequests(t *testing.T) {
	t.Parallel()
	e := New(t.TempDir(), nil, nil)
	valid := AttachRequest{DiskID: "0b6f6c3e-5d0a-4c55-9a51-2f1f4c1d7e10", SizeBytes: 1 << 30, Mountpoint: "/mnt/d"}
	for name, mutate := range map[string]func(*AttachRequest){
		"unaligned size":    func(r *AttachRequest) { r.SizeBytes = 1<<30 + 512 },
		"zero size":         func(r *AttachRequest) { r.SizeBytes = 0 },
		"relative mount":    func(r *AttachRequest) { r.Mountpoint = "mnt/d" },
		"disk id with dots": func(r *AttachRequest) { r.DiskID = "../etc" },
		"base without a digest": func(r *AttachRequest) {
			r.Base = &Generation{Generation: 1}
		},
	} {
		req := valid
		mutate(&req)
		if err := e.Attach(t.Context(), req); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: got %v, want ErrInvalid", name, err)
		}
	}
}
