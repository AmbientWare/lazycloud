// Package storagetest points tests at the local Garage object store from
// compose.yaml.
package storagetest

import (
	"os"

	"github.com/AmbientWare/lazycloud/internal/storage"
)

// Config is the development object store. Set
// LAZYCLOUD_TEST_OBJECT_STORE_ENDPOINT to use another S3-compatible store with
// the same bucket and development key.
func Config() storage.Config {
	endpoint := os.Getenv("LAZYCLOUD_TEST_OBJECT_STORE_ENDPOINT")
	if endpoint == "" {
		endpoint = "http://127.0.0.1:23900"
	}
	return storage.Config{
		Endpoint:        endpoint,
		Region:          "garage",
		Bucket:          "lazycloud",
		AccessKeyID:     "GK1a2b3c4d5e6f708192a3b4c5",
		SecretAccessKey: "6c6f63616c2d6c617a79636c6f75642d6465762d7365637265742d6b65792d31", //nolint:gosec // Development credentials.
	}
}
