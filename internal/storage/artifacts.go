package storage

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"mime"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// artifactPartBytes is the part size of artifact uploads; smaller artifacts
// upload with one plain PUT.
const artifactPartBytes = 64 << 20

// ArtifactRetention is how long a workspace's new artifacts are kept: its
// owner's plan decides, one day on Free, 30 on Team and 90 on Business.
func (s *Storage) ArtifactRetention(ctx context.Context, workspace identity.WorkspaceID) (time.Duration, error) {
	retention, err := billing.Retention(ctx, s.pool, uuid.UUID(workspace))
	if err != nil {
		return 0, fmt.Errorf("read artifact retention: %w", err)
	}
	return retention, nil
}

func artifactKey(workspace identity.WorkspaceID, id uuid.UUID) string {
	return fmt.Sprintf("workspaces/%s/artifacts/%s", workspace, id)
}

func artifactOut(a Artifact) apitypes.Artifact {
	out := apitypes.Artifact{
		Id: a.ID, TaskId: a.TaskID, App: a.AppName, Filename: a.Filename, ContentType: a.ContentType,
		SizeBytes: a.SizeBytes, State: apitypes.ArtifactState(a.State), CreatedAt: a.CreatedAt,
		StoredAt: a.StoredAt, ExpiresAt: a.ExpiresAt,
	}
	return out
}

// CreateArtifact records a pending artifact for a task in the workspace and
// presigns its upload: one PUT up to artifactPartBytes, otherwise a
// multipart upload.
func (s *Storage) CreateArtifact(ctx context.Context, workspace identity.WorkspaceID, req apitypes.CreateArtifactRequest) (apitypes.ArtifactUpload, error) {
	task, err := s.queries.ArtifactTask(ctx, ArtifactTaskParams{TaskID: req.TaskId, WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.ArtifactUpload{}, ErrNotFound
	}
	if err != nil {
		return apitypes.ArtifactUpload{}, fmt.Errorf("read artifact task: %w", err)
	}
	retention, err := s.ArtifactRetention(ctx, workspace)
	if err != nil {
		return apitypes.ArtifactUpload{}, err
	}
	contentType := "application/octet-stream"
	if req.ContentType != nil {
		contentType = *req.ContentType
	}
	id, err := uuid.NewV7()
	if err != nil {
		return apitypes.ArtifactUpload{}, fmt.Errorf("artifact id: %w", err)
	}
	key := artifactKey(workspace, id)
	upload := apitypes.Upload{ExpiresAt: time.Now().Add(uploadLifetime)}
	if req.SizeBytes <= artifactPartBytes {
		// The signed length makes the store refuse other bytes. The content
		// type is applied when the artifact is read, so clients send no
		// signed headers.
		r, err := s.presign.PresignPutObject(ctx, &s3.PutObjectInput{
			Bucket: aws.String(s.bucket), Key: aws.String(key), ContentLength: aws.Int64(req.SizeBytes),
		}, s3.WithPresignExpires(uploadLifetime))
		if err != nil {
			return apitypes.ArtifactUpload{}, fmt.Errorf("presign artifact upload: %w", err)
		}
		upload.Parts = []apitypes.UploadPart{{Number: 1, Offset: 0, SizeBytes: req.SizeBytes, Url: r.URL}}
	} else {
		uploadID, parts, err := s.startMultipart(ctx, s.bucket, key, contentType, req.SizeBytes, artifactPartBytes, uploadLifetime)
		if err != nil {
			return apitypes.ArtifactUpload{}, err
		}
		partSize := int64(artifactPartBytes)
		upload.UploadId, upload.Parts, upload.PartSizeBytes = &uploadID, parts, &partSize
	}
	// Nothing is stored at key until the client uploads, after this row
	// exists; the sweep removes an upload abandoned for a day with its row.
	row, err := s.queries.InsertArtifact(ctx, InsertArtifactParams{
		ID: id, WorkspaceID: uuid.UUID(workspace), TaskID: &req.TaskId, AppID: &task.AppID, AppName: &task.AppName,
		Filename: req.Filename, ContentType: contentType, SizeBytes: req.SizeBytes, UploadID: upload.UploadId,
		RetentionSeconds: int64(retention.Seconds()),
	})
	if err != nil {
		if upload.UploadId != nil {
			err = errors.Join(err, s.abortMultipart(context.WithoutCancel(ctx), s.bucket, key, *upload.UploadId))
		}
		return apitypes.ArtifactUpload{}, fmt.Errorf("record artifact: %w", err)
	}
	return apitypes.ArtifactUpload{Artifact: artifactOut(row), Upload: upload}, nil
}

// CompleteArtifact stores an uploaded artifact once its bytes match the
// declared size; retention starts now.
func (s *Storage) CompleteArtifact(ctx context.Context, workspace identity.WorkspaceID, id uuid.UUID, parts []apitypes.CompletedPart) (apitypes.Artifact, error) {
	var out apitypes.Artifact
	err := pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		q := s.queries.WithTx(tx)
		row, err := q.LockUploadingArtifact(ctx, LockUploadingArtifactParams{ID: id, WorkspaceID: uuid.UUID(workspace)})
		if errors.Is(err, pgx.ErrNoRows) {
			// A retried completion of a stored artifact succeeds.
			stored, err := q.StoredArtifact(ctx, StoredArtifactParams{ID: id, WorkspaceID: uuid.UUID(workspace)})
			if errors.Is(err, pgx.ErrNoRows) {
				return ErrNotFound
			}
			out = artifactOut(stored)
			return err
		}
		if err != nil {
			return fmt.Errorf("lock artifact: %w", err)
		}
		key := artifactKey(workspace, id)
		if row.UploadID != nil {
			if len(parts) == 0 {
				return invalid("a multipart artifact needs its part ETags")
			}
			if err := s.completeMultipart(ctx, s.bucket, key, *row.UploadID, parts); err != nil {
				return err
			}
		}
		o, err := s.head(ctx, s.bucket, key)
		if errors.Is(err, ErrNotFound) {
			return invalid("the artifact's bytes were not uploaded")
		}
		if err != nil {
			return err
		}
		if o.Size != row.SizeBytes {
			return invalid("uploaded %d bytes, declared %d", o.Size, row.SizeBytes)
		}
		stored, err := q.StoreArtifact(ctx, id)
		if err != nil {
			return fmt.Errorf("store artifact: %w", err)
		}
		out = artifactOut(stored)
		return nil
	})
	if err != nil {
		return apitypes.Artifact{}, fmt.Errorf("complete artifact: %w", err)
	}
	return out, nil
}

// GetArtifact returns a stored, unexpired artifact.
func (s *Storage) GetArtifact(ctx context.Context, workspace identity.WorkspaceID, id uuid.UUID) (apitypes.Artifact, error) {
	row, err := s.queries.StoredArtifact(ctx, StoredArtifactParams{ID: id, WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.Artifact{}, ErrNotFound
	}
	if err != nil {
		return apitypes.Artifact{}, fmt.Errorf("read artifact: %w", err)
	}
	return artifactOut(row), nil
}

// ArtifactFilter narrows an artifact listing.
type ArtifactFilter struct {
	Task          *uuid.UUID
	App           *string
	Search        *string
	ContentType   *string
	CreatedAfter  *time.Time
	CreatedBefore *time.Time
}

type artifactCursor struct {
	CreatedAt time.Time `json:"t"`
	ID        uuid.UUID `json:"i"`
}

// ListArtifacts returns stored, unexpired artifacts, newest first.
func (s *Storage) ListArtifacts(ctx context.Context, workspace identity.WorkspaceID, f ArtifactFilter, cursor string, limit int) (apitypes.ArtifactPage, error) {
	params := ListArtifactsParams{
		WorkspaceID: uuid.UUID(workspace), TaskID: f.Task, AppName: f.App, Search: f.Search, ContentType: f.ContentType,
		CreatedAfter: f.CreatedAfter, CreatedBefore: f.CreatedBefore, MaxRows: int32(limit), //nolint:gosec // The schema caps limit.
	}
	if cursor != "" {
		var c artifactCursor
		raw, err := base64.RawURLEncoding.DecodeString(cursor)
		if err != nil || json.Unmarshal(raw, &c) != nil {
			return apitypes.ArtifactPage{}, invalid("the cursor is not one this listing returned")
		}
		params.CursorAt, params.CursorID = &c.CreatedAt, &c.ID
	}
	rows, err := s.queries.ListArtifacts(ctx, params)
	if err != nil {
		return apitypes.ArtifactPage{}, fmt.Errorf("list artifacts: %w", err)
	}
	page := apitypes.ArtifactPage{Artifacts: make([]apitypes.Artifact, len(rows))}
	for n, row := range rows {
		page.Artifacts[n] = artifactOut(row)
	}
	if len(rows) == limit {
		last := rows[len(rows)-1]
		raw, err := json.Marshal(artifactCursor{CreatedAt: last.CreatedAt, ID: last.ID})
		if err != nil {
			return apitypes.ArtifactPage{}, fmt.Errorf("encode cursor: %w", err)
		}
		next := base64.RawURLEncoding.EncodeToString(raw)
		page.NextCursor = &next
	}
	return page, nil
}

// ArtifactSummary counts the workspace's stored artifacts and what they
// cost.
func (s *Storage) ArtifactSummary(ctx context.Context, workspace identity.WorkspaceID) (apitypes.ArtifactSummary, error) {
	row, err := s.queries.ArtifactSummary(ctx, uuid.UUID(workspace))
	if err != nil {
		return apitypes.ArtifactSummary{}, fmt.Errorf("summarize artifacts: %w", err)
	}
	retention, err := s.ArtifactRetention(ctx, workspace)
	if err != nil {
		return apitypes.ArtifactSummary{}, err
	}
	charges, err := billing.ArtifactCost(ctx, s.pool, uuid.UUID(workspace), row.SizeBytes, time.Now())
	if err != nil {
		return apitypes.ArtifactSummary{}, err
	}
	return apitypes.ArtifactSummary{
		Count: row.Count, SizeBytes: row.SizeBytes, RetentionSeconds: int64(retention.Seconds()),
		EstimatedMonthlyNanos: charges.MonthlyNanos, AccruedNanos: charges.AccruedNanos, AccruedSince: charges.Since,
	}, nil
}

// PresignArtifact returns a GET for the artifact's bytes, lasting no longer
// than the artifact. download asks browsers to save rather than display it.
func (s *Storage) PresignArtifact(ctx context.Context, workspace identity.WorkspaceID, id uuid.UUID, expiresSeconds *int, download bool) (apitypes.PresignedUrl, error) {
	row, err := s.queries.StoredArtifact(ctx, StoredArtifactParams{ID: id, WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.PresignedUrl{}, ErrNotFound
	}
	if err != nil {
		return apitypes.PresignedUrl{}, fmt.Errorf("read artifact: %w", err)
	}
	lifetime := min(presignLifetime(expiresSeconds), time.Until(*row.ExpiresAt))
	if lifetime < time.Second {
		return apitypes.PresignedUrl{}, ErrNotFound
	}
	disposition := "inline"
	if download {
		disposition = "attachment"
	}
	r, err := s.presign.PresignGetObject(ctx, &s3.GetObjectInput{
		Bucket: aws.String(s.bucket), Key: aws.String(artifactKey(workspace, id)),
		ResponseContentType:        aws.String(row.ContentType),
		ResponseContentDisposition: aws.String(mime.FormatMediaType(disposition, map[string]string{"filename": row.Filename})),
	}, s3.WithPresignExpires(lifetime))
	if err != nil {
		return apitypes.PresignedUrl{}, fmt.Errorf("presign artifact: %w", err)
	}
	return apitypes.PresignedUrl{Url: r.URL, ExpiresAt: time.Now().Add(lifetime)}, nil
}

// DeleteArtifacts deletes the workspace's artifacts among ids and returns
// those it deleted. The rows are deleted in a transaction that commits only
// after their bytes are gone, so a failure leaves every row to retry.
func (s *Storage) DeleteArtifacts(ctx context.Context, workspace identity.WorkspaceID, ids []uuid.UUID) ([]uuid.UUID, error) {
	var deleted []uuid.UUID
	err := pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		rows, err := s.queries.WithTx(tx).DeleteArtifacts(ctx, DeleteArtifactsParams{WorkspaceID: uuid.UUID(workspace), Ids: ids})
		if err != nil {
			return fmt.Errorf("delete artifact rows: %w", err)
		}
		deleted = make([]uuid.UUID, len(rows))
		keys := make([]string, len(rows))
		for n, row := range rows {
			deleted[n] = row.ID
			keys[n] = artifactKey(workspace, row.ID)
			if row.UploadID != nil {
				if err := s.abortMultipart(ctx, s.bucket, keys[n], *row.UploadID); err != nil {
					return err
				}
			}
		}
		return s.deleteKeys(ctx, s.bucket, keys)
	})
	if err != nil {
		return nil, fmt.Errorf("delete artifacts: %w", err)
	}
	return deleted, nil
}
