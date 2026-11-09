package database

import (
	"errors"
	"unicode/utf8"

	"github.com/jackc/pgx/v5/pgconn"
)

// IsUniqueViolation reports whether err is PostgreSQL refusing a row that
// breaks a unique constraint.
func IsUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

// Value is *p, or T's zero value for a nil p, such as a NULL column.
func Value[T any](p *T) T {
	if p == nil {
		var zero T
		return zero
	}
	return *p
}

// Truncate keeps at most limit bytes of s from its start, cut on a rune
// boundary: PostgreSQL text refuses split UTF-8.
func Truncate(s string, limit int) string {
	if len(s) <= limit {
		return s
	}
	for limit > 0 && !utf8.RuneStart(s[limit]) {
		limit--
	}
	return s[:limit]
}

// TruncateTail keeps at most limit bytes of s from its end, cut on a rune
// boundary and marked with a leading ellipsis.
func TruncateTail(s string, limit int) string {
	if len(s) <= limit {
		return s
	}
	start := len(s) - limit
	for start < len(s) && !utf8.RuneStart(s[start]) {
		start++
	}
	return "…" + s[start:]
}
