package storage_test

import (
	"errors"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
)

// An account with no credit takes on no new stored bytes, but its data can
// still be read and taken out.
func TestUnfundedWorkspacesStoreNothingNewButReadWhatTheyHave(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, volumeSpec)
	s := f.storage
	task := f.task()
	if _, err := s.CreateVolume(ctx, f.ws, "data"); err != nil {
		t.Fatal(err)
	}
	putFile(t, s, f, "kept.txt", []byte("kept"))
	for _, sql := range []string{
		"update billing_accounts set complimentary_since = null where user_id in (select user_id from workspace_members where workspace_id = $1)",
		"update billing_balances set balance_nanos = 0 where user_id in (select user_id from workspace_members where workspace_id = $1)",
	} {
		if _, err := f.pool.Exec(ctx, sql, f.ws); err != nil {
			t.Fatal(err)
		}
	}

	refused := func(what string, err error) {
		t.Helper()
		var payment *billing.PaymentRequiredError
		if !errors.As(err, &payment) {
			t.Fatalf("%s without credit: %v", what, err)
		}
	}
	_, err := s.CreateVolume(ctx, f.ws, "more")
	refused("a new volume", err)
	_, err = s.PresignVolumeFile(ctx, f.ws, "data", apitypes.PresignVolumeFileRequest{Path: "new.txt", Method: apitypes.PresignVolumeFileRequestMethodPut})
	refused("a file upload", err)
	_, err = s.CreateVolumeUpload(ctx, f.ws, "data", apitypes.CreateVolumeUploadRequest{Path: "big.bin", SizeBytes: 6 << 20})
	refused("a multipart upload", err)
	_, err = s.CreateArtifact(ctx, f.ws, apitypes.CreateArtifactRequest{TaskId: task, Filename: "x.txt", SizeBytes: 1})
	refused("an artifact", err)

	if _, err := s.PresignVolumeFile(ctx, f.ws, "data", apitypes.PresignVolumeFileRequest{Path: "kept.txt", Method: apitypes.PresignVolumeFileRequestMethodGet}); err != nil {
		t.Fatalf("reading stored data stays open: %v", err)
	}
	if _, err := s.RemoveVolumeFiles(ctx, f.ws, "data", "kept.txt"); err != nil {
		t.Fatalf("removing stored data stays open: %v", err)
	}
}
