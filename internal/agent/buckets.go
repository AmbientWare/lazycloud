package agent

import (
	"fmt"
	"path/filepath"
	"strings"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// A cloud bucket is a user's own S3 bucket, mounted for one container with
// the keys its workspace secrets hold, which the agent keeps beside the
// mount until the mount goes.

func (v *volumes) bucketKeys(name string) string {
	return filepath.Join(v.a.cfg.StateDir, "storage", "buckets", name)
}

// bucketSpec writes the keys of the cloud bucket the mounter named name
// mounts for group, the container's mounts of that bucket. It mounts the
// directories their prefixes share, read-only when every mount is, and
// returns each mount's directory below it.
func (v *volumes) bucketSpec(name string, group []*hostproto.VolumeMount) (mountSpec, []string, error) {
	b := group[0].GetCloudBucket()
	if b.GetBucket() == "" || b.GetEndpoint() == "" || b.GetAccessKeyId() == "" || b.GetSecretAccessKey() == "" {
		return mountSpec{}, nil, fmt.Errorf("cloud bucket at %s needs a bucket, an endpoint and keys", group[0].GetMountPath())
	}
	shared, readOnly := b.GetPrefix(), true
	for _, s := range group {
		shared = sharedDirs(shared, s.GetCloudBucket().GetPrefix())
		readOnly = readOnly && s.GetReadOnly()
	}
	dirs := make([]string, len(group))
	for i, s := range group {
		dirs[i] = strings.TrimPrefix(s.GetCloudBucket().GetPrefix(), shared)
	}
	creds := v.bucketKeys(name)
	if err := writeCredentials(creds, processCredentials{Version: 1, AccessKeyID: b.GetAccessKeyId(), SecretAccessKey: b.GetSecretAccessKey()}); err != nil {
		return mountSpec{}, nil, err
	}
	return mountSpec{
		creds: creds, source: b.GetBucket() + ":" + shared, endpoint: b.GetEndpoint(), region: b.GetRegion(),
		pathStyle: b.GetForcePathStyle(), readOnly: readOnly,
	}, dirs, nil
}

// sharedDirs is the longest run of whole directories, each ending in "/",
// that both prefixes start with.
func sharedDirs(a, b string) string {
	n := 0
	for i := 0; i < min(len(a), len(b)) && a[i] == b[i]; i++ {
		if a[i] == '/' {
			n = i + 1
		}
	}
	return a[:n]
}
