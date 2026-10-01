package storage

import (
	"context"
	"errors"
	"fmt"
	"path"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// DefaultVolumeRoot is where a volume mounts when its spec names no path.
const DefaultVolumeRoot = "/volumes"

// volumePrefix is the key prefix of a volume's files in its workspace bucket.
func volumePrefix(volume uuid.UUID) string { return "volumes/" + volume.String() + "/" }

func volumeOut(id uuid.UUID, name string, size int64, measured *time.Time, created time.Time) apitypes.Volume {
	return apitypes.Volume{
		Id: id, Name: name, SizeBytes: size, SizeMeasuredAt: measured, CreatedAt: created, UsedBy: []apitypes.WorkloadRef{},
	}
}

// CreateVolume creates the named volume, or returns the active one.
func (s *Storage) CreateVolume(ctx context.Context, workspace identity.WorkspaceID, name string) (apitypes.Volume, error) {
	if err := s.queries.InsertVolume(ctx, InsertVolumeParams{WorkspaceID: uuid.UUID(workspace), Name: name}); err != nil {
		return apitypes.Volume{}, fmt.Errorf("insert volume: %w", err)
	}
	return s.GetVolume(ctx, workspace, name)
}

// GetVolume returns the active volume with name and the workloads using it.
func (s *Storage) GetVolume(ctx context.Context, workspace identity.WorkspaceID, name string) (apitypes.Volume, error) {
	row, err := s.queries.ActiveVolume(ctx, ActiveVolumeParams{WorkspaceID: uuid.UUID(workspace), Name: name})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.Volume{}, ErrNotFound
	}
	if err != nil {
		return apitypes.Volume{}, fmt.Errorf("read volume: %w", err)
	}
	out := []apitypes.Volume{volumeOut(row.ID, row.Name, row.SizeBytes, row.SizeMeasuredAt, row.CreatedAt)}
	if err := s.addVolumeUsers(ctx, workspace, out); err != nil {
		return apitypes.Volume{}, err
	}
	return out[0], nil
}

// ListVolumes returns a page of active volumes by name.
func (s *Storage) ListVolumes(ctx context.Context, workspace identity.WorkspaceID, cursor string, limit int) (apitypes.VolumePage, error) {
	rows, err := s.queries.ListVolumes(ctx, ListVolumesParams{WorkspaceID: uuid.UUID(workspace), After: cursor, MaxRows: int32(limit)}) //nolint:gosec // The schema caps limit.
	if err != nil {
		return apitypes.VolumePage{}, fmt.Errorf("list volumes: %w", err)
	}
	page := apitypes.VolumePage{Volumes: make([]apitypes.Volume, len(rows))}
	for n, row := range rows {
		page.Volumes[n] = volumeOut(row.ID, row.Name, row.SizeBytes, row.SizeMeasuredAt, row.CreatedAt)
	}
	if err := s.addVolumeUsers(ctx, workspace, page.Volumes); err != nil {
		return apitypes.VolumePage{}, err
	}
	if len(rows) == limit {
		page.NextCursor = &rows[len(rows)-1].Name
	}
	return page, nil
}

func (s *Storage) addVolumeUsers(ctx context.Context, workspace identity.WorkspaceID, volumes []apitypes.Volume) error {
	if len(volumes) == 0 {
		return nil
	}
	names := make([]string, len(volumes))
	index := map[string]int{}
	for n, v := range volumes {
		names[n] = v.Name
		index[v.Name] = n
	}
	users, err := s.queries.VolumeUsers(ctx, VolumeUsersParams{WorkspaceID: uuid.UUID(workspace), Names: names})
	if err != nil {
		return fmt.Errorf("read volume users: %w", err)
	}
	for _, u := range users {
		v := &volumes[index[u.Volume]]
		v.UsedBy = append(v.UsedBy, apitypes.WorkloadRef{App: u.App, Kind: apitypes.WorkloadRefKind(u.Kind), Name: u.Workload})
	}
	return nil
}

// DeleteVolume marks the volume deleting, which frees its name; the sweep
// removes its files. It is refused while a container that mounts it has not
// stopped. The volume row lock orders this against MountVolumes.
func (s *Storage) DeleteVolume(ctx context.Context, workspace identity.WorkspaceID, name string) error {
	err := pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		q := s.queries.WithTx(tx)
		id, err := q.LockActiveVolume(ctx, LockActiveVolumeParams{WorkspaceID: uuid.UUID(workspace), Name: name})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock volume: %w", err)
		}
		container, err := q.LiveMountOfVolume(ctx, id)
		if err == nil {
			return conflict("volume %s is mounted by container %s; stop it before deleting the volume", name, container)
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return fmt.Errorf("check volume mounts: %w", err)
		}
		if err := q.MarkVolumeDeleting(ctx, id); err != nil {
			return fmt.Errorf("mark volume deleting: %w", err)
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("delete volume %s: %w", name, err)
	}
	return nil
}

// Mount is a volume or bucket a container mounts.
type Mount struct {
	// Volume is set for platform volumes, which mount from the workspace
	// bucket under Prefix.
	Volume    *uuid.UUID
	Bucket    string
	Prefix    string
	MountPath string
	ReadOnly  bool
	// CloudBucket is set for a user's own S3 bucket.
	CloudBucket *apitypes.CloudBucketSpec
}

// MountPath is where a volume spec mounts in the container.
func MountPath(spec apitypes.VolumeMountSpec) string {
	if spec.MountPath == nil || *spec.MountPath == "" {
		return path.Join(DefaultVolumeRoot, spec.Name)
	}
	if path.IsAbs(*spec.MountPath) {
		return path.Clean(*spec.MountPath)
	}
	return path.Join(DefaultVolumeRoot, *spec.MountPath)
}

// MountVolumes records that container mounts the platform volumes in specs,
// creating volumes on first use, and returns every mount. Each volume is
// locked FOR SHARE while its mount row is written, so a concurrent delete
// either sees the mount or finishes first, in which case a new volume of the
// name is created. It is idempotent per container.
func (s *Storage) MountVolumes(ctx context.Context, workspace identity.WorkspaceID, container uuid.UUID, specs []apitypes.VolumeMountSpec) ([]Mount, error) {
	mounts := make([]Mount, len(specs))
	platform := false
	for n, spec := range specs {
		mounts[n] = Mount{MountPath: MountPath(spec), ReadOnly: spec.ReadOnly != nil && *spec.ReadOnly, CloudBucket: spec.CloudBucket}
		platform = platform || spec.CloudBucket == nil
	}
	if !platform {
		return mounts, nil
	}
	bucket, err := s.workspaceBucket(ctx, workspace)
	if err != nil {
		return nil, err
	}
	err = pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		q := s.queries.WithTx(tx)
		for n, spec := range specs {
			if spec.CloudBucket != nil {
				continue
			}
			key := ShareActiveVolumeParams{WorkspaceID: uuid.UUID(workspace), Name: spec.Name}
			if err := q.InsertVolume(ctx, InsertVolumeParams(key)); err != nil {
				return fmt.Errorf("create volume %s: %w", spec.Name, err)
			}
			id, err := q.ShareActiveVolume(ctx, key)
			if err != nil {
				return fmt.Errorf("lock volume %s: %w", spec.Name, err)
			}
			if err := q.InsertVolumeMount(ctx, InsertVolumeMountParams{VolumeID: id, ContainerID: container}); err != nil {
				return fmt.Errorf("record mount of %s: %w", spec.Name, err)
			}
			mounts[n].Volume, mounts[n].Bucket, mounts[n].Prefix = &id, bucket, volumePrefix(id)
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("mount volumes: %w", err)
	}
	return mounts, nil
}

// volumeFiles resolves an active volume to its bucket and key prefix.
func (s *Storage) volumeFiles(ctx context.Context, workspace identity.WorkspaceID, name string) (bucket, prefix string, err error) {
	row, err := s.queries.ActiveVolume(ctx, ActiveVolumeParams{WorkspaceID: uuid.UUID(workspace), Name: name})
	if errors.Is(err, pgx.ErrNoRows) {
		return "", "", ErrNotFound
	}
	if err != nil {
		return "", "", fmt.Errorf("read volume: %w", err)
	}
	bucket, err = s.workspaceBucket(ctx, workspace)
	if err != nil {
		return "", "", err
	}
	return bucket, volumePrefix(row.ID), nil
}

// cleanPath normalizes a path relative to the volume root. The root is "".
func cleanPath(p string) (string, error) {
	if strings.HasPrefix(p, "/") {
		return "", invalid("volume path %q must be relative to the volume root", p)
	}
	for _, segment := range strings.Split(p, "/") {
		if segment == ".." {
			return "", invalid("volume path %q leaves the volume", p)
		}
	}
	cleaned := path.Clean("/" + p)[1:]
	return cleaned, nil
}

// filePath is a path below the root.
func filePath(p string) (string, error) {
	cleaned, err := cleanPath(p)
	if err != nil {
		return "", err
	}
	if cleaned == "" {
		return "", invalid("a path below the volume root is required")
	}
	return cleaned, nil
}

func fileOut(prefix string, o objectInfo) apitypes.VolumeFile {
	modified := o.Modified
	return apitypes.VolumeFile{Path: strings.TrimPrefix(o.Key, prefix), IsDir: false, SizeBytes: o.Size, ModifiedAt: &modified}
}

func dirOut(rel string) apitypes.VolumeFile {
	return apitypes.VolumeFile{Path: strings.TrimSuffix(rel, "/"), IsDir: true}
}

// ListVolumeFiles returns one directory's entries. A missing directory is
// empty.
func (s *Storage) ListVolumeFiles(ctx context.Context, workspace identity.WorkspaceID, volume, dir, cursor string, limit int) (apitypes.VolumeFilePage, error) {
	bucket, prefix, err := s.volumeFiles(ctx, workspace, volume)
	if err != nil {
		return apitypes.VolumeFilePage{}, err
	}
	rel, err := cleanPath(dir)
	if err != nil {
		return apitypes.VolumeFilePage{}, err
	}
	listed := prefix
	if rel != "" {
		listed += rel + "/"
	}
	input := &s3.ListObjectsV2Input{
		Bucket: aws.String(bucket), Prefix: aws.String(listed), Delimiter: aws.String("/"),
		MaxKeys: aws.Int32(int32(limit)), //nolint:gosec // The schema caps limit.
	}
	if cursor != "" {
		input.ContinuationToken = aws.String(cursor)
	}
	out, err := s.client.ListObjectsV2(ctx, input)
	if err != nil {
		return apitypes.VolumeFilePage{}, fmt.Errorf("list volume files: %w", err)
	}
	page := apitypes.VolumeFilePage{Files: make([]apitypes.VolumeFile, 0, len(out.CommonPrefixes)+len(out.Contents))}
	for _, p := range out.CommonPrefixes {
		page.Files = append(page.Files, dirOut(strings.TrimPrefix(aws.ToString(p.Prefix), prefix)))
	}
	for _, o := range out.Contents {
		key := aws.ToString(o.Key)
		if key == listed {
			// A directory marker that FUSE mounts create.
			continue
		}
		page.Files = append(page.Files, fileOut(prefix, objectInfo{Key: key, Size: aws.ToInt64(o.Size), Modified: aws.ToTime(o.LastModified)}))
	}
	if aws.ToBool(out.IsTruncated) {
		page.NextCursor = out.NextContinuationToken
	}
	return page, nil
}

// StatVolumeFile returns a file, or a directory that holds anything.
func (s *Storage) StatVolumeFile(ctx context.Context, workspace identity.WorkspaceID, volume, p string) (apitypes.VolumeFile, error) {
	bucket, prefix, err := s.volumeFiles(ctx, workspace, volume)
	if err != nil {
		return apitypes.VolumeFile{}, err
	}
	rel, err := cleanPath(p)
	if err != nil {
		return apitypes.VolumeFile{}, err
	}
	if rel == "" {
		return dirOut(""), nil
	}
	return s.statKey(ctx, bucket, prefix, rel)
}

func (s *Storage) statKey(ctx context.Context, bucket, prefix, rel string) (apitypes.VolumeFile, error) {
	o, err := s.head(ctx, bucket, prefix+rel)
	if err == nil {
		return fileOut(prefix, o), nil
	}
	if !errors.Is(err, ErrNotFound) {
		return apitypes.VolumeFile{}, err
	}
	out, err := s.client.ListObjectsV2(ctx, &s3.ListObjectsV2Input{Bucket: aws.String(bucket), Prefix: aws.String(prefix + rel + "/"), MaxKeys: aws.Int32(1)})
	if err != nil {
		return apitypes.VolumeFile{}, fmt.Errorf("stat volume directory: %w", err)
	}
	if len(out.Contents) == 0 {
		return apitypes.VolumeFile{}, ErrNotFound
	}
	return dirOut(rel), nil
}

// objectsAt lists the object at rel and every object below it.
func (s *Storage) objectsAt(ctx context.Context, bucket, prefix, rel string) ([]objectInfo, error) {
	var found []objectInfo
	if o, err := s.head(ctx, bucket, prefix+rel); err == nil {
		found = append(found, o)
	} else if !errors.Is(err, ErrNotFound) {
		return nil, err
	}
	err := s.eachObject(ctx, bucket, prefix+rel+"/", func(batch []objectInfo) error {
		found = append(found, batch...)
		return nil
	})
	return found, err
}

// RemoveVolumeFiles removes a file, or a directory and everything below it,
// and returns the removed paths. A missing path removes nothing.
func (s *Storage) RemoveVolumeFiles(ctx context.Context, workspace identity.WorkspaceID, volume, p string) ([]string, error) {
	bucket, prefix, err := s.volumeFiles(ctx, workspace, volume)
	if err != nil {
		return nil, err
	}
	rel, err := filePath(p)
	if err != nil {
		return nil, err
	}
	objects, err := s.objectsAt(ctx, bucket, prefix, rel)
	if err != nil {
		return nil, err
	}
	keys := make([]string, len(objects))
	removed := make([]string, 0, len(objects))
	for n, o := range objects {
		keys[n] = o.Key
		if !strings.HasSuffix(o.Key, "/") {
			removed = append(removed, strings.TrimPrefix(o.Key, prefix))
		}
	}
	if err := s.deleteKeys(ctx, bucket, keys); err != nil {
		return nil, err
	}
	return removed, nil
}

// MoveVolumeFile moves a file or directory within the volume. Objects are
// copied, then the sources deleted, so a failure part way leaves both.
func (s *Storage) MoveVolumeFile(ctx context.Context, workspace identity.WorkspaceID, volume, from, to string) (apitypes.VolumeFile, error) {
	bucket, prefix, err := s.volumeFiles(ctx, workspace, volume)
	if err != nil {
		return apitypes.VolumeFile{}, err
	}
	src, err := filePath(from)
	if err != nil {
		return apitypes.VolumeFile{}, err
	}
	dst, err := filePath(to)
	if err != nil {
		return apitypes.VolumeFile{}, err
	}
	if dst == src || strings.HasPrefix(dst, src+"/") {
		return apitypes.VolumeFile{}, invalid("cannot move %s into itself", src)
	}
	if _, err := s.statKey(ctx, bucket, prefix, dst); err == nil {
		return apitypes.VolumeFile{}, conflict("%s already exists", dst)
	} else if !errors.Is(err, ErrNotFound) {
		return apitypes.VolumeFile{}, err
	}
	objects, err := s.objectsAt(ctx, bucket, prefix, src)
	if err != nil {
		return apitypes.VolumeFile{}, err
	}
	if len(objects) == 0 {
		return apitypes.VolumeFile{}, ErrNotFound
	}
	keys := make([]string, len(objects))
	for n, o := range objects {
		if err := s.copyObject(ctx, bucket, o, prefix+dst+strings.TrimPrefix(o.Key, prefix+src)); err != nil {
			return apitypes.VolumeFile{}, err
		}
		keys[n] = o.Key
	}
	if err := s.deleteKeys(ctx, bucket, keys); err != nil {
		return apitypes.VolumeFile{}, err
	}
	return s.statKey(ctx, bucket, prefix, dst)
}

// PresignVolumeFile presigns one read, write or part upload of a file.
func (s *Storage) PresignVolumeFile(ctx context.Context, workspace identity.WorkspaceID, volume string, req apitypes.PresignVolumeFileRequest) (apitypes.PresignedUrl, error) {
	bucket, prefix, err := s.volumeFiles(ctx, workspace, volume)
	if err != nil {
		return apitypes.PresignedUrl{}, err
	}
	rel, err := filePath(req.Path)
	if err != nil {
		return apitypes.PresignedUrl{}, err
	}
	lifetime := presignLifetime(req.ExpiresSeconds)
	if req.Method == apitypes.PresignVolumeFileRequestMethodPut || req.Method == apitypes.PresignVolumeFileRequestMethodUploadPart {
		// A write URL outliving a delete would recreate files.
		lifetime = min(lifetime, uploadLifetime)
	}
	key := aws.String(prefix + rel)
	expires := s3.WithPresignExpires(lifetime)
	var url string
	switch req.Method {
	case apitypes.PresignVolumeFileRequestMethodGet:
		r, err := s.presign.PresignGetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(bucket), Key: key}, expires)
		if err != nil {
			return apitypes.PresignedUrl{}, fmt.Errorf("presign get: %w", err)
		}
		url = r.URL
	case apitypes.PresignVolumeFileRequestMethodHead:
		r, err := s.presign.PresignHeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(bucket), Key: key}, expires)
		if err != nil {
			return apitypes.PresignedUrl{}, fmt.Errorf("presign head: %w", err)
		}
		url = r.URL
	case apitypes.PresignVolumeFileRequestMethodPut:
		r, err := s.presign.PresignPutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(bucket), Key: key}, expires)
		if err != nil {
			return apitypes.PresignedUrl{}, fmt.Errorf("presign put: %w", err)
		}
		url = r.URL
	case apitypes.PresignVolumeFileRequestMethodUploadPart:
		if req.UploadId == nil || req.PartNumber == nil {
			return apitypes.PresignedUrl{}, invalid("upload_part needs upload_id and part_number")
		}
		url, err = s.presignPart(ctx, bucket, prefix+rel, *req.UploadId, int32(*req.PartNumber), lifetime) //nolint:gosec // The schema caps part numbers.
		if err != nil {
			return apitypes.PresignedUrl{}, err
		}
	default:
		return apitypes.PresignedUrl{}, invalid("unknown method %q", req.Method)
	}
	return apitypes.PresignedUrl{Url: url, ExpiresAt: time.Now().Add(lifetime)}, nil
}

// uploadLifetime bounds how long an upload URL stays valid, and with it
// how long a deleted owner can still receive bytes.
const uploadLifetime = time.Hour

// CreateVolumeUpload starts a multipart upload of one file.
func (s *Storage) CreateVolumeUpload(ctx context.Context, workspace identity.WorkspaceID, volume string, req apitypes.CreateVolumeUploadRequest) (apitypes.MultipartUpload, error) {
	bucket, prefix, err := s.volumeFiles(ctx, workspace, volume)
	if err != nil {
		return apitypes.MultipartUpload{}, err
	}
	rel, err := filePath(req.Path)
	if err != nil {
		return apitypes.MultipartUpload{}, err
	}
	partSize := int64(5 << 20)
	if req.PartSizeBytes != nil {
		partSize = *req.PartSizeBytes
	}
	uploadID, parts, err := s.startMultipart(ctx, bucket, prefix+rel, "", req.SizeBytes, partSize, uploadLifetime)
	if err != nil {
		return apitypes.MultipartUpload{}, err
	}
	return apitypes.MultipartUpload{
		UploadId: uploadID, Path: rel, PartSizeBytes: partSize, Parts: parts, ExpiresAt: time.Now().Add(uploadLifetime),
	}, nil
}

// CompleteVolumeUpload assembles an uploaded file from its parts.
func (s *Storage) CompleteVolumeUpload(ctx context.Context, workspace identity.WorkspaceID, volume string, req apitypes.CompleteVolumeUploadRequest) (apitypes.VolumeFile, error) {
	bucket, prefix, err := s.volumeFiles(ctx, workspace, volume)
	if err != nil {
		return apitypes.VolumeFile{}, err
	}
	rel, err := filePath(req.Path)
	if err != nil {
		return apitypes.VolumeFile{}, err
	}
	if err := s.completeMultipart(ctx, bucket, prefix+rel, req.UploadId, req.Parts); err != nil {
		return apitypes.VolumeFile{}, err
	}
	return s.statKey(ctx, bucket, prefix, rel)
}

// AbortVolumeUpload discards an unfinished upload and its parts.
func (s *Storage) AbortVolumeUpload(ctx context.Context, workspace identity.WorkspaceID, volume string, req apitypes.AbortVolumeUploadRequest) error {
	bucket, prefix, err := s.volumeFiles(ctx, workspace, volume)
	if err != nil {
		return err
	}
	rel, err := filePath(req.Path)
	if err != nil {
		return err
	}
	return s.abortMultipart(ctx, bucket, prefix+rel, req.UploadId)
}

// reservedMountRoots are container paths the host runtime owns.
var reservedMountRoots = [...]string{"/opt/lazycloud", "/run/lazycloud", "/workspace", "/proc", "/sys", "/dev"} //nolint:gochecknoglobals // A constant table.

// ValidateVolumes checks a workload's volume specs: unique names and mount
// paths, none on a path the runtime owns. Cloud buckets are refused until
// workspace secrets can supply their keys.
func ValidateVolumes(specs []apitypes.VolumeMountSpec) error {
	names := map[string]bool{}
	paths := map[string]bool{}
	for _, spec := range specs {
		if spec.CloudBucket != nil {
			return invalid("cloud bucket %s: mounting a cloud bucket needs workspace secrets for its keys, which this platform does not provide yet", spec.Name)
		}
		target := MountPath(spec)
		if names[spec.Name] || paths[target] {
			return invalid("volume %s at %s: each volume and mount path may appear once", spec.Name, target)
		}
		names[spec.Name], paths[target] = true, true
		if target == "/" {
			return invalid("volume %s cannot mount at /", spec.Name)
		}
		for _, root := range reservedMountRoots {
			if target == root || strings.HasPrefix(target, root+"/") {
				return invalid("volume %s cannot mount under %s", spec.Name, root)
			}
		}
	}
	return nil
}
