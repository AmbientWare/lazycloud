package imagefs

import (
	"bytes"
	"math/rand/v2"
	"testing"
)

// BenchmarkReadFrame reads full frames from a local server, as the
// snapshotter's frame cache fetches them.
func BenchmarkReadFrame(b *testing.B) {
	random := make([]byte, 4*FrameSize)
	_, _ = rand.NewChaCha8([32]byte{}).Read(random)
	text := bytes.Repeat([]byte("import torch\nfrom lazycloud import app\n"), 4*FrameSize/40)
	for _, c := range []struct {
		name string
		body []byte
	}{{"random", random}, {"text", text}} {
		name, body := c.name, c.body
		b.Run(name, func(b *testing.B) {
			var data bytes.Buffer
			ix, err := Convert(b.Context(), bytes.NewReader(fileTar(b, "f", body)), &data)
			if err != nil {
				b.Fatal(err)
			}
			object := serve(b, data.Bytes())
			b.SetBytes(FrameSize)
			b.ReportAllocs()
			b.RunParallel(func(pb *testing.PB) {
				i := 0
				for pb.Next() {
					if _, err := ix.ReadFrame(b.Context(), object, i%len(ix.Frames)); err != nil {
						b.Error(err)
						return
					}
					i++
				}
			})
		})
	}
}
