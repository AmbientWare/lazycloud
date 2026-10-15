package agent

import (
	"slices"
	"strings"
	"testing"
)

// Build output keeps streaming past a line longer than any batch, so the
// lines after it, such as the push, still arrive.
func TestBuildOutputReadsPastAnOverlongLine(t *testing.T) {
	t.Parallel()
	long := strings.Repeat("x", 2<<20)
	var lines []string
	err := readBuildLines(strings.NewReader("first\r\n"+long+"\n\n"+imagePushed), func(line string) { lines = append(lines, line) })
	if want := []string{"first", long[:maxBuildLine], "", imagePushed}; err != nil || !slices.Equal(lines, want) {
		t.Fatalf("read %d lines (%v), want %d", len(lines), err, len(want))
	}
}
