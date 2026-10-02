package database_test

import (
	"testing"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// Migrate is safe to rerun: a second run applies nothing and verifies the
// recorded digests.
func TestMigrateIsRepeatable(t *testing.T) {
	pool := dbtest.New(t)
	if err := database.Migrate(t.Context(), pool); err != nil {
		t.Fatalf("second migrate: %v", err)
	}
	var tables int
	if err := pool.QueryRow(t.Context(), "select count(*) from information_schema.tables where table_schema = 'public'").Scan(&tables); err != nil {
		t.Fatal(err)
	}
	if tables < 10 {
		t.Fatalf("expected the schema to exist, found %d tables", tables)
	}
}
