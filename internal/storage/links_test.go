package storage_test

import (
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	. "github.com/AmbientWare/lazycloud/internal/storage"
)

// A download is a link that lasts as long as it was asked to, since each
// use is presigned afresh; HEAD follows it too. Links the server did not
// sign, changed ones and expired ones open nothing.
func TestDownloadLinksLastTheirLifetimeAndOnlyTheirs(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, volumeSpec)
	s := f.storage
	if _, err := s.CreateVolume(ctx, f.ws, "data"); err != nil {
		t.Fatal(err)
	}
	putFile(t, s, f, "a.txt", []byte("alpha"))
	week := 7 * 24 * 3600
	read := func(method apitypes.PresignVolumeFileRequestMethod, seconds *int) apitypes.PresignedUrl {
		t.Helper()
		url, err := s.PresignVolumeFile(ctx, f.ws, "data", apitypes.PresignVolumeFileRequest{Path: "a.txt", Method: method, ExpiresSeconds: seconds})
		if err != nil {
			t.Fatal(err)
		}
		return url
	}
	long := read(apitypes.PresignVolumeFileRequestMethodGet, &week)
	if time.Until(long.ExpiresAt) < 7*24*time.Hour-time.Minute {
		t.Fatalf("a week-long link expires at %s", long.ExpiresAt)
	}
	if status, body, _ := get(t, long.Url); status != http.StatusOK || string(body) != "alpha" {
		t.Fatalf("follow a link: %d %q", status, body)
	}
	head := read(apitypes.PresignVolumeFileRequestMethodHead, nil)
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, head.Url, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK || resp.ContentLength != int64(len("alpha")) {
		t.Fatalf("HEAD through a link: %d length %d", resp.StatusCode, resp.ContentLength)
	}

	token := long.Url[strings.LastIndex(long.Url, "/")+1:]
	body, signature, _ := strings.Cut(token, ".")
	second := 1
	short := read(apitypes.PresignVolumeFileRequestMethodGet, &second)
	time.Sleep(1100 * time.Millisecond)
	for name, token := range map[string]string{
		"forged":  body + ".AAAA",
		"changed": "e30." + signature,
		"expired": short.Url[strings.LastIndex(short.Url, "/")+1:],
	} {
		if _, err := s.OpenLink(ctx, token, false); !errors.Is(err, ErrNotFound) {
			t.Errorf("%s link: %v, want ErrNotFound", name, err)
		}
	}
}
