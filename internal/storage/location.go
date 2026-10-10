package storage

import (
	"errors"

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

// locate resolves a bucket's address. A custom endpoint, as for Garage, R2
// or MinIO, takes path-style requests; without one the bucket is AWS S3 in
// region, addressed by subdomain unless forcePathStyle.
func locate(endpoint, region, bucket string, forcePathStyle bool) (Location, error) {
	if endpoint != "" {
		return Location{Endpoint: endpoint, Region: region, Bucket: bucket, PathStyle: true}, nil
	}
	if region == "" {
		return Location{}, ErrNoRegion
	}
	return Location{Endpoint: "https://s3." + region + ".amazonaws.com", Region: region, Bucket: bucket, PathStyle: forcePathStyle}, nil
}

// CloudBucketLocation is the address of a user's bucket.
func CloudBucketLocation(b apitypes.CloudBucketSpec) (Location, error) {
	return locate(deref(b.Endpoint), deref(b.Region), b.Bucket, b.ForcePathStyle != nil && *b.ForcePathStyle)
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
