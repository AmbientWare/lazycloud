package storage

import (
	"errors"
	"strings"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// ErrNoRegion refuses an AWS S3 bucket that names no region, since its
// address depends on one.
var ErrNoRegion = errors.New("an AWS S3 bucket needs a region")

// Location is a bucket's address as hosts receive it.
type Location struct {
	Endpoint  string
	Region    string
	Bucket    string
	PathStyle bool
}

// locate resolves a bucket's address: at endpoint, such as Garage's, R2's or
// MinIO's, or without one AWS S3 in region. Requests name the bucket in the
// host name unless pathStyle, or the name holds a dot: TLS certificates
// cover one subdomain label, so a dotted bucket's host name fails
// verification.
func locate(endpoint, region, bucket string, pathStyle bool) (Location, error) {
	if endpoint == "" {
		if region == "" {
			return Location{}, ErrNoRegion
		}
		endpoint = "https://s3." + region + ".amazonaws.com"
	}
	return Location{Endpoint: endpoint, Region: region, Bucket: bucket, PathStyle: pathStyle || strings.Contains(bucket, ".")}, nil
}

// CloudBucketLocation is the address of a user's bucket, path-style as its
// spec forces.
func CloudBucketLocation(b apitypes.CloudBucketSpec) (Location, error) {
	return locate(deref(b.Endpoint), deref(b.Region), b.Bucket, b.ForcePathStyle != nil && *b.ForcePathStyle)
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
