package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"

	"github.com/moby/moby/api/types/mount"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// A cloud bucket is a user's own S3 bucket, mounted for one container with
// the keys its workspace secrets hold. Each mount runs in its own mount
// container, like workspace volumes, and goes when the container does.

func bucketMountName(container string, n int) string {
	return "lazycloud-bucket-" + container + "-" + strconv.Itoa(n)
}

func (v *volumes) bucketKeys(name string) string {
	return filepath.Join(v.a.cfg.StateDir, "storage", "buckets", name)
}

// bucketBind mounts a cloud bucket for container and returns its bind.
func (v *volumes) bucketBind(ctx context.Context, container string, n int, spec *hostproto.VolumeMount) (mount.Mount, error) {
	b := spec.GetCloudBucket()
	if b.GetBucket() == "" || b.GetEndpoint() == "" || b.GetAccessKeyId() == "" || b.GetSecretAccessKey() == "" {
		return mount.Mount{}, fmt.Errorf("cloud bucket at %s needs a bucket, an endpoint and keys", spec.GetMountPath())
	}
	name := bucketMountName(container, n)
	creds := v.bucketKeys(name)
	if err := os.MkdirAll(creds, 0o700); err != nil {
		return mount.Mount{}, fmt.Errorf("create bucket credential directory: %w", err)
	}
	data, err := json.Marshal(processCredentials{Version: 1, AccessKeyID: b.GetAccessKeyId(), SecretAccessKey: b.GetSecretAccessKey()}) //nolint:gosec // The 0600 file GeeseFS reads its key from.
	if err != nil {
		return mount.Mount{}, fmt.Errorf("encode bucket credentials: %w", err)
	}
	if err := writeFileAtomic(filepath.Join(creds, "credentials.json"), data, 0o600); err != nil {
		return mount.Mount{}, fmt.Errorf("write bucket credentials: %w", err)
	}
	config := "[default]\ncredential_process = cat /creds/credentials.json\n"
	if err := writeFileAtomic(filepath.Join(creds, "config"), []byte(config), 0o644); err != nil { //nolint:gosec // Holds no secret.
		return mount.Mount{}, fmt.Errorf("write bucket credential config: %w", err)
	}
	m := newMounter(name, filepath.Join(v.mountDir(), name))
	m.users[container] = struct{}{}
	v.mu.Lock()
	v.buckets[container] = append(v.buckets[container], m)
	v.mu.Unlock()
	err = v.start(ctx, m, "the cloud bucket mount at "+spec.GetMountPath(), mountSpec{
		creds: creds, source: b.GetBucket() + ":" + b.GetPrefix(), endpoint: b.GetEndpoint(), region: b.GetRegion(),
		pathStyle: b.GetForcePathStyle(), readOnly: spec.GetReadOnly(),
		labels: map[string]string{labelKind: kindBucket, labelContainer: container},
	})
	if err != nil {
		return mount.Mount{}, err
	}
	return mount.Mount{Type: mount.TypeBind, Source: m.dir, Target: spec.GetMountPath(), ReadOnly: spec.GetReadOnly()}, nil
}

// releaseBuckets stops the container's cloud bucket mounts and deletes
// their keys.
func (v *volumes) releaseBuckets(ctx context.Context, container string) {
	v.mu.Lock()
	mounts := v.buckets[container]
	delete(v.buckets, container)
	v.mu.Unlock()
	for _, m := range mounts {
		if err := v.stop(ctx, m); err != nil {
			v.a.log.Warn("stopping a cloud bucket mount failed", "mount", m.name, "error", err)
		}
		if err := os.RemoveAll(v.bucketKeys(m.name)); err != nil {
			v.a.log.Warn("removing cloud bucket keys failed", "mount", m.name, "error", err)
		}
	}
}
