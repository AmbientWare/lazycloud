package database_test

import (
	"errors"
	"io/fs"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/migrations"
)

// Migrate is safe to rerun: a second run applies nothing. A deployed
// migration whose file changed stops it.
func TestMigrateIsRepeatableAndRefusesAChangedMigration(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	if err := database.Migrate(t.Context(), pool); err != nil {
		t.Fatalf("second migrate: %v", err)
	}
	files, err := fs.Glob(migrations.Files, "*.sql")
	if err != nil {
		t.Fatal(err)
	}
	var applied int
	if err := pool.QueryRow(t.Context(), "select count(*) from schema_migrations").Scan(&applied); err != nil {
		t.Fatal(err)
	}
	if applied != len(files) {
		t.Fatalf("%d migrations recorded, want each of the %d files once", applied, len(files))
	}

	if _, err := pool.Exec(t.Context(), "update schema_migrations set sha256 = sha256('edited') where name = $1", files[0]); err != nil {
		t.Fatal(err)
	}
	if err := database.Migrate(t.Context(), pool); !errors.Is(err, database.ErrMigrationChanged) {
		t.Fatalf("migrate over a changed migration: %v, want ErrMigrationChanged", err)
	}
}
