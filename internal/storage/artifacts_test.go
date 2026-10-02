package storage_test

import (
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	. "github.com/AmbientWare/lazycloud/internal/storage"
)

func saveArtifact(t *testing.T, f *fixture, task uuid.UUID, name, contentType string, body []byte) apitypes.Artifact {
	t.Helper()
	upload, err := f.storage.CreateArtifact(t.Context(), f.ws, apitypes.CreateArtifactRequest{
		TaskId: task, Filename: name, ContentType: &contentType, SizeBytes: int64(len(body)),
	})
	if err != nil {
		t.Fatal(err)
	}
	if upload.Artifact.State != apitypes.Uploading || upload.Upload.UploadId != nil || len(upload.Upload.Parts) != 1 {
		t.Fatalf("small artifact upload: %+v", upload)
	}
	if status, _ := send(t, upload.Upload.Parts[0].Url, body); status != http.StatusOK {
		t.Fatalf("upload: status %d", status)
	}
	stored, err := f.storage.CompleteArtifact(t.Context(), f.ws, upload.Artifact.Id, nil)
	if err != nil {
		t.Fatal(err)
	}
	return stored
}

func TestArtifactLifecycle(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, `{}`)
	s := f.storage
	task := f.task()

	if _, err := s.CreateArtifact(ctx, f.ws, apitypes.CreateArtifactRequest{TaskId: uuid.New(), Filename: "x", SizeBytes: 1}); !errors.Is(err, ErrNotFound) {
		t.Fatalf("artifact for an unknown task: %v", err)
	}
	report := saveArtifact(t, f, task, "report.html", "text/html", []byte("<p>hi</p>"))
	if report.State != apitypes.Stored || report.ExpiresAt == nil || report.App == nil || *report.App != "app" {
		t.Fatalf("stored artifact: %+v", report)
	}
	// The fixture's owner is waived, which keeps artifacts as long as
	// Business does.
	if got, want := report.ExpiresAt.Sub(*report.StoredAt), 90*24*time.Hour; got != want {
		t.Fatalf("retention %v, want %v", got, want)
	}
	// Completing again is idempotent.
	if again, err := s.CompleteArtifact(ctx, f.ws, report.Id, nil); err != nil || again.Id != report.Id {
		t.Fatalf("second completion: %+v err=%v", again, err)
	}
	image := saveArtifact(t, f, task, "plot.png", "image/png", []byte("png"))

	// Declared and uploaded sizes must match.
	short, err := s.CreateArtifact(ctx, f.ws, apitypes.CreateArtifactRequest{TaskId: task, Filename: "short.bin", SizeBytes: 10})
	if err != nil {
		t.Fatal(err)
	}
	if status, _ := send(t, short.Upload.Parts[0].Url, []byte("abc")); status < 400 {
		// The store may accept a body of another length; completion must not.
		var bad *InvalidError
		if _, err := s.CompleteArtifact(ctx, f.ws, short.Artifact.Id, nil); !errors.As(err, &bad) {
			t.Fatalf("complete with the wrong size: %v", err)
		}
	}

	page, err := s.ListArtifacts(ctx, f.ws, ArtifactFilter{}, "", 1)
	if err != nil || len(page.Artifacts) != 1 || page.Artifacts[0].Id != image.Id || page.NextCursor == nil {
		t.Fatalf("first page: %+v err=%v", page, err)
	}
	page, err = s.ListArtifacts(ctx, f.ws, ArtifactFilter{}, *page.NextCursor, 1)
	if err != nil || len(page.Artifacts) != 1 || page.Artifacts[0].Id != report.Id {
		t.Fatalf("second page: %+v err=%v", page, err)
	}
	images := "image/"
	if page, err := s.ListArtifacts(ctx, f.ws, ArtifactFilter{ContentType: &images}, "", 100); err != nil || len(page.Artifacts) != 1 {
		t.Fatalf("type filter: %+v err=%v", page, err)
	}
	search := "REPORT"
	if page, err := s.ListArtifacts(ctx, f.ws, ArtifactFilter{Search: &search, Task: &task}, "", 100); err != nil || len(page.Artifacts) != 1 {
		t.Fatalf("search: %+v err=%v", page, err)
	}

	url, err := s.PresignArtifact(ctx, f.ws, report.Id, nil, true)
	if err != nil {
		t.Fatal(err)
	}
	status, body, header := get(t, url.Url)
	if status != http.StatusOK || string(body) != "<p>hi</p>" || !strings.HasPrefix(header.Get("Content-Disposition"), "attachment") {
		t.Fatalf("download: %d %q disposition %q", status, body, header.Get("Content-Disposition"))
	}
	summary, err := s.ArtifactSummary(ctx, f.ws)
	if err != nil || summary.Count != 2 || summary.SizeBytes != int64(len("<p>hi</p>")+3) {
		t.Fatalf("summary: %+v err=%v", summary, err)
	}

	deleted, err := s.DeleteArtifacts(ctx, f.ws, []uuid.UUID{image.Id, uuid.New()})
	if err != nil || len(deleted) != 1 || deleted[0] != image.Id {
		t.Fatalf("delete: %v err=%v", deleted, err)
	}
	if _, err := s.GetArtifact(ctx, f.ws, image.Id); !errors.Is(err, ErrNotFound) {
		t.Fatalf("deleted artifact: %v", err)
	}

	// Expired artifacts disappear from reads at once and from the store at
	// the next sweep.
	if _, err := f.pool.Exec(ctx, `update artifacts set expires_at = now() - interval '1 second' where id = $1`, report.Id); err != nil {
		t.Fatal(err)
	}
	if _, err := s.GetArtifact(ctx, f.ws, report.Id); !errors.Is(err, ErrNotFound) {
		t.Fatalf("expired artifact: %v", err)
	}
	result, err := s.Sweep(ctx, slog.Default())
	if err != nil || result.Artifacts != 1 {
		t.Fatalf("sweep removed %d artifacts, err=%v", result.Artifacts, err)
	}
	if status, _, _ := get(t, url.Url); status != http.StatusNotFound {
		t.Fatalf("expired artifact's bytes: status %d", status)
	}
}

func TestArtifactMultipartUpload(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, `{}`)
	task := f.task()
	size := int64(64<<20 + 1)
	upload, err := f.storage.CreateArtifact(ctx, f.ws, apitypes.CreateArtifactRequest{TaskId: task, Filename: "big.bin", SizeBytes: size})
	if err != nil || upload.Upload.UploadId == nil || len(upload.Upload.Parts) != 2 {
		t.Fatalf("multipart artifact: %+v err=%v", upload.Upload, err)
	}
	if _, err := f.storage.CompleteArtifact(ctx, f.ws, upload.Artifact.Id, nil); err == nil {
		t.Fatal("a multipart artifact completed without its parts")
	}
	var parts []apitypes.CompletedPart
	for _, p := range upload.Upload.Parts {
		status, etag := send(t, p.Url, make([]byte, p.SizeBytes))
		if status != http.StatusOK {
			t.Fatalf("part %d: status %d", p.Number, status)
		}
		parts = append(parts, apitypes.CompletedPart{Number: p.Number, Etag: etag})
	}
	stored, err := f.storage.CompleteArtifact(ctx, f.ws, upload.Artifact.Id, parts)
	if err != nil || stored.SizeBytes != size {
		t.Fatalf("complete: %+v err=%v", stored, err)
	}
	if _, err := f.storage.DeleteArtifacts(ctx, f.ws, []uuid.UUID{stored.Id}); err != nil {
		t.Fatal(err)
	}
}
