package supervisor

import (
	"os"
	"slices"
	"strings"
)

// Redacted replaces a secret value in task output.
const Redacted = "********"

// minRedactBytes is the shortest value redacted. Shorter values would mask
// ordinary text without hiding anything worth hiding.
const minRedactBytes = 4

// redactor replaces secret values in output. Output arrives in chunks, so a
// stream holds back a tail that could begin a value until the next chunk
// shows whether it does.
type redactor struct {
	values   []string
	replacer *strings.Replacer
}

// newRedactor redacts the values of the named environment variables.
func newRedactor(names []string) *redactor {
	var values []string
	for _, name := range names {
		if v := os.Getenv(name); len(v) >= minRedactBytes {
			values = append(values, v)
		}
	}
	if len(values) == 0 {
		return nil
	}
	// Longer values first, so a value containing another is replaced whole.
	slices.SortFunc(values, func(a, b string) int { return len(b) - len(a) })
	values = slices.Compact(values)
	pairs := make([]string, 0, 2*len(values))
	for _, v := range values {
		pairs = append(pairs, v, Redacted)
	}
	return &redactor{values: values, replacer: strings.NewReplacer(pairs...)}
}

// all redacts a complete text, such as an error message.
func (r *redactor) all(text string) string {
	if r == nil {
		return text
	}
	return r.replacer.Replace(text)
}

// stream redacts pending+text and returns what can be emitted, keeping in
// pending a tail that may be the start of a value. flush emits everything.
func (r *redactor) stream(pending *string, text string, flush bool) string {
	if r == nil {
		return text
	}
	text = r.replacer.Replace(*pending + text)
	hold := 0
	if !flush {
		hold = r.partialTail(text)
	}
	*pending = text[len(text)-hold:]
	return text[:len(text)-hold]
}

// partialTail is the length of the longest suffix of text that is a proper
// prefix of a value.
func (r *redactor) partialTail(text string) int {
	longest := 0
	for _, v := range r.values {
		for n := min(len(v)-1, len(text)); n > longest; n-- {
			if strings.HasSuffix(text, v[:n]) {
				longest = n
				break
			}
		}
	}
	return longest
}
