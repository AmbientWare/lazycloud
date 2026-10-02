package api

import (
	"context"
	"net/http"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

func orEmpty(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func limitOr(limit *int, def int) int {
	if limit == nil {
		return def
	}
	return *limit
}

// ListVolumes returns a page of volumes.
func (s *Server) ListVolumes(ctx context.Context, req ListVolumesRequestObject) (ListVolumesResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	page, err := s.owners.Storage.ListVolumes(ctx, ws.ID, orEmpty(req.Params.Cursor), limitOr(req.Params.Limit, 50))
	if err != nil {
		return nil, err
	}
	return ListVolumes200JSONResponse(page), nil
}

// CreateVolume creates a volume or returns the active one of the name.
func (s *Server) CreateVolume(ctx context.Context, req CreateVolumeRequestObject) (CreateVolumeResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	volume, err := s.owners.Storage.CreateVolume(ctx, ws.ID, req.Body.Name)
	if err != nil {
		return nil, err
	}
	return CreateVolume200JSONResponse(volume), nil
}

// GetVolume returns one volume.
func (s *Server) GetVolume(ctx context.Context, req GetVolumeRequestObject) (GetVolumeResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	volume, err := s.owners.Storage.GetVolume(ctx, ws.ID, req.Volume)
	if err != nil {
		return nil, err
	}
	return GetVolume200JSONResponse(volume), nil
}

// DeleteVolume deletes a volume that no live container mounts.
func (s *Server) DeleteVolume(ctx context.Context, req DeleteVolumeRequestObject) (DeleteVolumeResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Storage.DeleteVolume(ctx, ws.ID, req.Volume); err != nil {
		return nil, err
	}
	return DeleteVolume204Response{}, nil
}

// ListVolumeFiles lists one directory.
func (s *Server) ListVolumeFiles(ctx context.Context, req ListVolumeFilesRequestObject) (ListVolumeFilesResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	page, err := s.owners.Storage.ListVolumeFiles(ctx, ws.ID, req.Volume, orEmpty(req.Params.Path), orEmpty(req.Params.Cursor), limitOr(req.Params.Limit, 1000))
	if err != nil {
		return nil, err
	}
	return ListVolumeFiles200JSONResponse(page), nil
}

// StatVolumeFile describes a file or directory.
func (s *Server) StatVolumeFile(ctx context.Context, req StatVolumeFileRequestObject) (StatVolumeFileResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	file, err := s.owners.Storage.StatVolumeFile(ctx, ws.ID, req.Volume, orEmpty(req.Params.Path))
	if err != nil {
		return nil, err
	}
	return StatVolumeFile200JSONResponse(file), nil
}

// RemoveVolumeFiles removes a file or a directory tree.
func (s *Server) RemoveVolumeFiles(ctx context.Context, req RemoveVolumeFilesRequestObject) (RemoveVolumeFilesResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	removed, err := s.owners.Storage.RemoveVolumeFiles(ctx, ws.ID, req.Volume, req.Params.Path)
	if err != nil {
		return nil, err
	}
	return RemoveVolumeFiles200JSONResponse{Removed: removed}, nil
}

// MoveVolumeFile moves a file or directory.
func (s *Server) MoveVolumeFile(ctx context.Context, req MoveVolumeFileRequestObject) (MoveVolumeFileResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	file, err := s.owners.Storage.MoveVolumeFile(ctx, ws.ID, req.Volume, req.Body.From, req.Body.To)
	if err != nil {
		return nil, err
	}
	return MoveVolumeFile200JSONResponse(file), nil
}

// PresignVolumeFile presigns one file request.
func (s *Server) PresignVolumeFile(ctx context.Context, req PresignVolumeFileRequestObject) (PresignVolumeFileResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	url, err := s.owners.Storage.PresignVolumeFile(ctx, ws.ID, req.Volume, *req.Body)
	if err != nil {
		return nil, err
	}
	return PresignVolumeFile200JSONResponse(url), nil
}

// CreateVolumeUpload starts a multipart upload.
func (s *Server) CreateVolumeUpload(ctx context.Context, req CreateVolumeUploadRequestObject) (CreateVolumeUploadResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	upload, err := s.owners.Storage.CreateVolumeUpload(ctx, ws.ID, req.Volume, *req.Body)
	if err != nil {
		return nil, err
	}
	return CreateVolumeUpload200JSONResponse(upload), nil
}

// CompleteVolumeUpload completes a multipart upload.
func (s *Server) CompleteVolumeUpload(ctx context.Context, req CompleteVolumeUploadRequestObject) (CompleteVolumeUploadResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	file, err := s.owners.Storage.CompleteVolumeUpload(ctx, ws.ID, req.Volume, *req.Body)
	if err != nil {
		return nil, err
	}
	return CompleteVolumeUpload200JSONResponse(file), nil
}

// AbortVolumeUpload aborts a multipart upload.
func (s *Server) AbortVolumeUpload(ctx context.Context, req AbortVolumeUploadRequestObject) (AbortVolumeUploadResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Storage.AbortVolumeUpload(ctx, ws.ID, req.Volume, *req.Body); err != nil {
		return nil, err
	}
	return AbortVolumeUpload204Response{}, nil
}

// ListDisks returns a page of disks.
func (s *Server) ListDisks(ctx context.Context, req ListDisksRequestObject) (ListDisksResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	page, err := s.owners.Storage.ListDisks(ctx, ws.ID, orEmpty(req.Params.Cursor), limitOr(req.Params.Limit, 50))
	if err != nil {
		return nil, err
	}
	return ListDisks200JSONResponse(page), nil
}

// GetDisk returns one disk.
func (s *Server) GetDisk(ctx context.Context, req GetDiskRequestObject) (GetDiskResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	disk, err := s.owners.Storage.GetDisk(ctx, ws.ID, req.Disk)
	if err != nil {
		return nil, err
	}
	return GetDisk200JSONResponse(disk), nil
}

// DeleteDisk deletes a disk no container holds.
func (s *Server) DeleteDisk(ctx context.Context, req DeleteDiskRequestObject) (DeleteDiskResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Storage.DeleteDisk(ctx, ws.ID, req.Disk); err != nil {
		return nil, err
	}
	return DeleteDisk204Response{}, nil
}

// ListArtifacts returns a filtered page of artifacts.
func (s *Server) ListArtifacts(ctx context.Context, req ListArtifactsRequestObject) (ListArtifactsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	p := req.Params
	page, err := s.owners.Storage.ListArtifacts(ctx, ws.ID, storage.ArtifactFilter{
		Task: p.TaskId, App: p.App, Search: p.Search, ContentType: p.ContentType,
		CreatedAfter: p.CreatedAfter, CreatedBefore: p.CreatedBefore,
	}, orEmpty(p.Cursor), limitOr(p.Limit, 50))
	if err != nil {
		return nil, err
	}
	return ListArtifacts200JSONResponse(page), nil
}

// CreateArtifact starts saving an artifact.
func (s *Server) CreateArtifact(ctx context.Context, req CreateArtifactRequestObject) (CreateArtifactResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	// Inside a container an artifact belongs to the task whose attempt is
	// running there, which the container API established; the body cannot
	// name another task.
	if c, ok := containerFrom(ctx); ok && (c.Task == nil || *c.Task != req.Body.TaskId) {
		return nil, identity.ErrForbidden
	}
	upload, err := s.owners.Storage.CreateArtifact(ctx, ws.ID, *req.Body)
	if err != nil {
		return nil, err
	}
	return CreateArtifact201JSONResponse(upload), nil
}

// CompleteArtifact records an uploaded artifact.
func (s *Server) CompleteArtifact(ctx context.Context, req CompleteArtifactRequestObject) (CompleteArtifactResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	var parts []apitypes.CompletedPart
	if req.Body.Parts != nil {
		parts = *req.Body.Parts
	}
	artifact, err := s.owners.Storage.CompleteArtifact(ctx, ws.ID, req.Artifact, parts)
	if err != nil {
		return nil, err
	}
	return CompleteArtifact200JSONResponse(artifact), nil
}

// GetArtifactSummary counts stored artifacts.
func (s *Server) GetArtifactSummary(ctx context.Context, req GetArtifactSummaryRequestObject) (GetArtifactSummaryResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	summary, err := s.owners.Storage.ArtifactSummary(ctx, ws.ID)
	if err != nil {
		return nil, err
	}
	return GetArtifactSummary200JSONResponse(summary), nil
}

// GetArtifact returns one stored artifact.
func (s *Server) GetArtifact(ctx context.Context, req GetArtifactRequestObject) (GetArtifactResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	artifact, err := s.owners.Storage.GetArtifact(ctx, ws.ID, req.Artifact)
	if err != nil {
		return nil, err
	}
	return GetArtifact200JSONResponse(artifact), nil
}

// LinksPath is where download links are served under the public URL.
const LinksPath = "/v1/links/"

// openLink redirects a download link to a presigned read of its object.
func (s *Server) openLink(w http.ResponseWriter, r *http.Request) {
	url, err := s.owners.Storage.OpenLink(r.Context(), r.PathValue("token"), r.Method == http.MethodHead)
	if err != nil {
		s.writeError(w, r, err)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	http.Redirect(w, r, url, http.StatusFound) //nolint:gosec // The target is our object store, presigned for a link this server signed.
}

// PresignArtifact returns a download link of an artifact.
func (s *Server) PresignArtifact(ctx context.Context, req PresignArtifactRequestObject) (PresignArtifactResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	download := req.Body.Download != nil && *req.Body.Download
	url, err := s.owners.Storage.PresignArtifact(ctx, ws.ID, req.Artifact, req.Body.ExpiresSeconds, download)
	if err != nil {
		return nil, err
	}
	return PresignArtifact200JSONResponse(url), nil
}

// DeleteArtifact deletes one artifact.
func (s *Server) DeleteArtifact(ctx context.Context, req DeleteArtifactRequestObject) (DeleteArtifactResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	deleted, err := s.owners.Storage.DeleteArtifacts(ctx, ws.ID, []uuid.UUID{req.Artifact})
	if err != nil {
		return nil, err
	}
	if len(deleted) == 0 {
		return nil, storage.ErrNotFound
	}
	return DeleteArtifact204Response{}, nil
}

// DeleteArtifacts deletes up to 100 artifacts.
func (s *Server) DeleteArtifacts(ctx context.Context, req DeleteArtifactsRequestObject) (DeleteArtifactsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	deleted, err := s.owners.Storage.DeleteArtifacts(ctx, ws.ID, req.Body.Ids)
	if err != nil {
		return nil, err
	}
	return DeleteArtifacts200JSONResponse{Deleted: deleted}, nil
}

// ListQueues returns a page of queues.
func (s *Server) ListQueues(ctx context.Context, req ListQueuesRequestObject) (ListQueuesResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	page, err := s.owners.Storage.ListQueues(ctx, ws.ID, orEmpty(req.Params.Cursor), limitOr(req.Params.Limit, 50))
	if err != nil {
		return nil, err
	}
	return ListQueues200JSONResponse(page), nil
}

// GetQueue returns a queue's size.
func (s *Server) GetQueue(ctx context.Context, req GetQueueRequestObject) (GetQueueResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	info, err := s.owners.Storage.GetQueue(ctx, ws.ID, req.Queue)
	if err != nil {
		return nil, err
	}
	return GetQueue200JSONResponse(info), nil
}

// DeleteQueue deletes a queue.
func (s *Server) DeleteQueue(ctx context.Context, req DeleteQueueRequestObject) (DeleteQueueResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Storage.DeleteQueue(ctx, ws.ID, req.Queue); err != nil {
		return nil, err
	}
	return DeleteQueue204Response{}, nil
}

// PutQueueMessages appends messages.
func (s *Server) PutQueueMessages(ctx context.Context, req PutQueueMessagesRequestObject) (PutQueueMessagesResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Storage.PutQueueMessages(ctx, ws.ID, req.Queue, req.Body.Messages); err != nil {
		return nil, err
	}
	return PutQueueMessages204Response{}, nil
}

// PopQueueMessage removes the oldest message, waiting when asked.
func (s *Server) PopQueueMessage(ctx context.Context, req PopQueueMessageRequestObject) (PopQueueMessageResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	wait := time.Duration(limitOr(req.Params.WaitSeconds, 0)) * time.Second
	message, found, err := s.owners.Storage.PopQueueMessage(ctx, s.owners.Listener, ws.ID, req.Queue, wait)
	if err != nil {
		return nil, err
	}
	out := PopQueueMessage200JSONResponse{}
	if found {
		out.Message = &message
	}
	return out, nil
}

// PeekQueueMessage returns the oldest message.
func (s *Server) PeekQueueMessage(ctx context.Context, req PeekQueueMessageRequestObject) (PeekQueueMessageResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	message, found, err := s.owners.Storage.PeekQueueMessage(ctx, ws.ID, req.Queue)
	if err != nil {
		return nil, err
	}
	out := PeekQueueMessage200JSONResponse{}
	if found {
		out.Message = &message
	}
	return out, nil
}

// ListMaps returns a page of maps.
func (s *Server) ListMaps(ctx context.Context, req ListMapsRequestObject) (ListMapsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	page, err := s.owners.Storage.ListMaps(ctx, ws.ID, orEmpty(req.Params.Cursor), limitOr(req.Params.Limit, 50))
	if err != nil {
		return nil, err
	}
	return ListMaps200JSONResponse(page), nil
}

// GetMap returns a map's statistics.
func (s *Server) GetMap(ctx context.Context, req GetMapRequestObject) (GetMapResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	info, err := s.owners.Storage.GetMap(ctx, ws.ID, req.Map)
	if err != nil {
		return nil, err
	}
	return GetMap200JSONResponse(info), nil
}

// DeleteMap deletes a map.
func (s *Server) DeleteMap(ctx context.Context, req DeleteMapRequestObject) (DeleteMapResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Storage.DeleteMap(ctx, ws.ID, req.Map); err != nil {
		return nil, err
	}
	return DeleteMap204Response{}, nil
}

// ListMapKeys returns a page of keys.
func (s *Server) ListMapKeys(ctx context.Context, req ListMapKeysRequestObject) (ListMapKeysResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	page, err := s.owners.Storage.ListMapKeys(ctx, ws.ID, req.Map, orEmpty(req.Params.Prefix), orEmpty(req.Params.Cursor), limitOr(req.Params.Limit, 1000))
	if err != nil {
		return nil, err
	}
	return ListMapKeys200JSONResponse(page), nil
}

// GetMapEntry returns one key.
func (s *Server) GetMapEntry(ctx context.Context, req GetMapEntryRequestObject) (GetMapEntryResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	entry, err := s.owners.Storage.GetMapEntry(ctx, ws.ID, req.Map, req.Key)
	if err != nil {
		return nil, err
	}
	return GetMapEntry200JSONResponse(entry), nil
}

// SetMapEntry writes one key.
func (s *Server) SetMapEntry(ctx context.Context, req SetMapEntryRequestObject) (SetMapEntryResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	w := storage.SetMapEntry{Value: req.Body.Value, IfRevision: req.Body.IfRevision, IfAbsent: req.Body.IfAbsent != nil && *req.Body.IfAbsent}
	if req.Body.TtlSeconds != nil {
		ttl := time.Duration(*req.Body.TtlSeconds) * time.Second
		w.TTL = &ttl
	}
	written, err := s.owners.Storage.SetMapEntry(ctx, ws.ID, req.Map, req.Key, w)
	if err != nil {
		return nil, err
	}
	return SetMapEntry200JSONResponse(written), nil
}

// DeleteMapEntry deletes one key.
func (s *Server) DeleteMapEntry(ctx context.Context, req DeleteMapEntryRequestObject) (DeleteMapEntryResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Storage.DeleteMapEntry(ctx, ws.ID, req.Map, req.Key, req.Params.IfRevision); err != nil {
		return nil, err
	}
	return DeleteMapEntry204Response{}, nil
}
