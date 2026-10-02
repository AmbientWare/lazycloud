package hostsession

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"unicode/utf8"

	"golang.org/x/net/html"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// Limits of a ResultDisplay, from contracts/openapi.yaml.
const (
	maxDisplayText  = 64 << 10
	maxDisplayHTML  = 256 << 10
	maxDisplayImage = 1 << 20
)

const pngSignature = "\x89PNG\r\n\x1a\n"

// resultDisplay checks the display a runner sent with a cloudpickle result
// and returns it re-encoded. The runner runs beside user code, so the
// display is untrusted: it must be valid UTF-8 text within the limits and at
// most one rendering, a PNG image or an HTML fragment. The dashboard shows
// the HTML in a sandboxed frame; nothing here or there loads the pickle.
func resultDisplay(raw []byte) (json.RawMessage, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var d apitypes.ResultDisplay
	if err := decoder.Decode(&d); err != nil {
		return nil, fmt.Errorf("decode result display: %w", err)
	}
	if !utf8.ValidString(d.Text) || utf8.RuneCountInString(d.Text) > maxDisplayText {
		return nil, errors.New("display text is not UTF-8 of at most 65536 characters")
	}
	if r := d.Rich; r != nil {
		switch r.Kind {
		case apitypes.RichDisplayKindImage:
			if r.MediaType == nil || *r.MediaType != apitypes.Imagepng || r.ValueBase64 == nil || r.Html != nil {
				return nil, errors.New("an image display needs a PNG and nothing else")
			}
			image, err := base64.StdEncoding.DecodeString(*r.ValueBase64)
			if err != nil || len(image) > maxDisplayImage || !bytes.HasPrefix(image, []byte(pngSignature)) {
				return nil, errors.New("display image is not a PNG of at most 1 MiB")
			}
		case apitypes.RichDisplayKindHtml:
			if r.Html == nil || *r.Html == "" || r.MediaType != nil || r.ValueBase64 != nil {
				return nil, errors.New("an HTML display needs HTML and nothing else")
			}
			if !utf8.ValidString(*r.Html) || utf8.RuneCountInString(*r.Html) > maxDisplayHTML {
				return nil, errors.New("display HTML is not UTF-8 of at most 262144 characters")
			}
			safe := inertHTML(*r.Html)
			if safe == "" {
				return nil, errors.New("display HTML holds nothing to show")
			}
			r.Html = &safe
		default:
			return nil, fmt.Errorf("display kind %q is unknown", r.Kind)
		}
	}
	out, err := json.Marshal(d)
	if err != nil {
		return nil, fmt.Errorf("encode result display: %w", err)
	}
	return out, nil
}

// Elements inertHTML drops with their content, and void or wrapping
// elements it drops alone. Both can navigate the frame, load a document or
// run code: a refresh <meta>, <base>, frames, plugins, forms, SVG
// animation that rewrites a link.
var (
	droppedWithContent = []string{ //nolint:gochecknoglobals // constant table
		"script", "iframe", "frame", "frameset", "object", "applet", "noscript", "template", "form", "portal",
		"set", "animate", "animatemotion", "animatetransform",
	}
	droppedAlone = []string{"meta", "base", "link", "embed"} //nolint:gochecknoglobals // constant table
	// droppedAttributes navigate, load or submit; src is kept only for
	// inline images, and every on* handler is dropped too.
	droppedAttributes = []string{ //nolint:gochecknoglobals // constant table
		"href", "xlink:href", "action", "formaction", "srcset", "ping", "http-equiv", "srcdoc", "background",
		"poster", "data", "codebase", "manifest", "target",
	}
)

// inertHTML is an HTML fragment with everything that can navigate the frame
// it is shown in, load a resource from the network or run code removed. The
// dashboard frame forbids scripts and network loads as well; this keeps a
// result from redirecting it, as a refresh <meta> or a link would.
func inertHTML(fragment string) string {
	var out strings.Builder
	tokens := html.NewTokenizer(strings.NewReader(fragment))
	skip := 0
	raw := ""
	for {
		kind := tokens.Next()
		if kind == html.ErrorToken {
			return out.String()
		}
		token := tokens.Token()
		name := strings.ToLower(token.Data)
		switch kind {
		case html.StartTagToken, html.SelfClosingTagToken:
			if slices.Contains(droppedWithContent, name) {
				if kind == html.StartTagToken {
					skip++
				}
				continue
			}
			if skip > 0 || slices.Contains(droppedAlone, name) {
				continue
			}
			token.Attr = slices.DeleteFunc(token.Attr, func(a html.Attribute) bool {
				key := strings.ToLower(a.Key)
				if a.Namespace != "" {
					key = strings.ToLower(a.Namespace) + ":" + key
				}
				switch {
				case strings.HasPrefix(key, "on"), slices.Contains(droppedAttributes, key):
					return true
				case key == "src":
					return !strings.HasPrefix(strings.ToLower(strings.TrimSpace(a.Val)), "data:image/")
				}
				return false
			})
			if name == "style" && kind == html.StartTagToken {
				raw = name
			}
			out.WriteString(token.String())
		case html.EndTagToken:
			if slices.Contains(droppedWithContent, name) {
				skip = max(skip-1, 0)
				continue
			}
			if skip > 0 || slices.Contains(droppedAlone, name) {
				continue
			}
			raw = ""
			out.WriteString(token.String())
		case html.TextToken:
			if skip > 0 {
				continue
			}
			if raw != "" {
				// Style text is CSS; the tokenizer ended it at </style>.
				out.WriteString(token.Data)
				continue
			}
			out.WriteString(token.String())
		case html.CommentToken, html.DoctypeToken, html.ErrorToken:
		}
	}
}
