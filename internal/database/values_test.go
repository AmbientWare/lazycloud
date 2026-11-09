package database_test

import (
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/AmbientWare/lazycloud/internal/database"
)

func TestTruncateKeepsValidUTF8(t *testing.T) {
	t.Parallel()
	s := strings.Repeat("é", 300)
	for limit := range 8 {
		limit += 497
		if got := database.Truncate(s, limit); !utf8.ValidString(got) || len(got) > limit {
			t.Errorf("Truncate(%d) = %d bytes, valid %v", limit, len(got), utf8.ValidString(got))
		}
		if got := database.TruncateTail(s, limit); !utf8.ValidString(got) || len(got) > limit+len("…") {
			t.Errorf("TruncateTail(%d) = %d bytes, valid %v", limit, len(got), utf8.ValidString(got))
		}
	}
}
