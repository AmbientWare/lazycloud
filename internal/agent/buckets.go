package agent

import (
	"fmt"
	"path/filepath"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// A cloud bucket is a user's own S3 bucket, mounted for one container with
// the keys its workspace secrets hold, which the agent keeps beside the
// mount until the mount goes.

func (v *volumes) bucketKeys(name string) string {
	return filepath.Join(v.a.cfg.StateDir, "storage", "buckets", name)
}

// bucketSpec writes the keys of the cloud bucket the mount named name
// mounts and says what it mounts.
func (v *volumes) bucketSpec(name string, s *hostproto.VolumeMount) (mountSpec, error) {
	b := s.GetCloudBucket()
	if b.GetBucket() == "" || b.GetEndpoint() == "" || b.GetAccessKeyId() == "" || b.GetSecretAccessKey() == "" {
		return mountSpec{}, fmt.Errorf("cloud bucket at %s needs a bucket, an endpoint and keys", s.GetMountPath())
	}
	creds := v.bucketKeys(name)
	if err := writeCredentials(creds, processCredentials{Version: 1, AccessKeyID: b.GetAccessKeyId(), SecretAccessKey: b.GetSecretAccessKey()}); err != nil {
		return mountSpec{}, err
	}
	return mountSpec{
		creds: creds, source: b.GetBucket() + ":" + b.GetPrefix(), endpoint: b.GetEndpoint(), region: b.GetRegion(),
		pathStyle: b.GetForcePathStyle(), readOnly: s.GetReadOnly(),
	}, nil
}
