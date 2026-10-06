// Package storagetest points tests at the local Garage object store from
// compose.yaml.
package storagetest

import (
	"os"

	"github.com/AmbientWare/lazycloud/internal/storage"
)

// Config is the development object store. Set
// LAZYCLOUD_TEST_OBJECT_STORE_ENDPOINT and LAZYCLOUD_TEST_GARAGE_ADMIN_URL
// to use another Garage with the same bucket, development key and admin
// token.
func Config() storage.Config {
	endpoint := os.Getenv("LAZYCLOUD_TEST_OBJECT_STORE_ENDPOINT")
	if endpoint == "" {
		endpoint = "http://127.0.0.1:23900"
	}
	admin := os.Getenv("LAZYCLOUD_TEST_GARAGE_ADMIN_URL")
	if admin == "" {
		admin = "http://127.0.0.1:23903"
	}
	return storage.Config{ //nolint:gosec // Development credentials.
		Endpoint:        endpoint,
		Region:          "garage",
		Bucket:          "lazycloud",
		LayerBucket:     "lazycloud-layers",
		AccessKeyID:     "GK1a2b3c4d5e6f708192a3b4c5",
		SecretAccessKey: "6c6f63616c2d6c617a79636c6f75642d6465762d7365637265742d6b65792d31",
		Workspaces: storage.WorkspaceBuckets{
			Provider:         storage.ProviderGarage,
			Prefix:           "lazycloud-test",
			GarageAdminURL:   admin,
			GarageAdminToken: "local-garage-admin",
		},
	}
}
