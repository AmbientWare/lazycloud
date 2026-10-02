// Package migrations holds the ordered SQL migration chain. Files are applied
// in name order and frozen once deployed.
package migrations

import "embed"

// Files is the migration chain.
//
//go:embed *.sql
var Files embed.FS
