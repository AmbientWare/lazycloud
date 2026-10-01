package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"github.com/moby/moby/api/types/mount"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// A cloud bucket is a user's own S3 bucket, mounted for one container with
// the keys its workspace secrets hold. Each mount runs in its own mount
// container, like workspace volumes, and goes when the container does.

func bucketMountName(container string, n int) string {
	return "lazycloud-bucket-" + container + "-" + strconv.Itoa(n)
}

// bucketBind mounts a cloud bucket for container and returns its bind.
func (v *volumes) bucketBind(ctx context.Context, container string, n int, spec *hostproto.VolumeMount) (mount.Mount, error) {
	b := spec.GetCloudBucket()
	if b.GetBucket() == "" || b.GetAccessKeyId() == "" || b.GetSecretAccessKey() == "" {
		return mount.Mount{}, fmt.Errorf("cloud bucket at %s needs a bucket and keys", spec.GetMountPath())
	}
	name := bucketMountName(container, n)
	creds := filepath.Join(v.a.cfg.StateDir, "storage", "buckets", name)
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
	v.mu.Lock()
	v.buckets[container] = append(v.buckets[container], name)
	v.mu.Unlock()
	id, err := v.runMount(ctx, mountSpec{
		name: name, dir: name, creds: creds,
		source: b.GetBucket() + ":" + b.GetPrefix(), endpoint: b.GetEndpoint(), region: b.GetRegion(),
		pathStyle: b.GetForcePathStyle() || b.GetEndpoint() != "", readOnly: spec.GetReadOnly(),
		labels: map[string]string{labelKind: kindBucket, labelContainer: container},
	})
	if err != nil {
		return mount.Mount{}, err
	}
	v.a.goOwned(func(ctx context.Context) { v.watch(ctx, name, id, func() []string { return []string{container} }) })
	root := filepath.Join(v.mountDir(), name)
	deadline := time.Now().Add(mountWait)
	for !mounted(root) {
		if !v.mountRunning(ctx, name) {
			return mount.Mount{}, fmt.Errorf("the cloud bucket mount at %s exited: %s", spec.GetMountPath(), v.mounterLogs(ctx, name))
		}
		if time.Now().After(deadline) || !sleep(ctx, 100*time.Millisecond) {
			return mount.Mount{}, fmt.Errorf("the cloud bucket mount at %s did not appear within %v", spec.GetMountPath(), mountWait)
		}
	}
	return mount.Mount{Type: mount.TypeBind, Source: root, Target: spec.GetMountPath(), ReadOnly: spec.GetReadOnly()}, nil
}

// releaseBuckets stops the container's cloud bucket mounts and deletes
// their keys.
func (v *volumes) releaseBuckets(ctx context.Context, container string) {
	v.mu.Lock()
	names := v.buckets[container]
	delete(v.buckets, container)
	v.mu.Unlock()
	for _, name := range names {
		if err := v.stopMount(ctx, name); err != nil {
			v.a.log.Warn("stopping a cloud bucket mount failed", "mount", name, "error", err)
		}
		if err := os.RemoveAll(filepath.Join(v.a.cfg.StateDir, "storage", "buckets", name)); err != nil {
			v.a.log.Warn("removing cloud bucket keys failed", "mount", name, "error", err)
		}
	}
}
