package hostproto

import "net/http"

// HeadersOut lists h as wire headers, one per value.
func HeadersOut(h http.Header) []*Header {
	out := make([]*Header, 0, len(h))
	for name, values := range h {
		for _, value := range values {
			out = append(out, &Header{Name: name, Value: value})
		}
	}
	return out
}

// HeadersIn adds wire headers to h.
func HeadersIn(h http.Header, headers []*Header) {
	for _, header := range headers {
		h.Add(header.GetName(), header.GetValue())
	}
}
