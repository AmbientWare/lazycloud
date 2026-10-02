package hostsession

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"unicode/utf8"

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
