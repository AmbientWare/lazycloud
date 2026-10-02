package storage

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

// A SigV4 presigned URL dies with the credentials that signed it, and the
// server's AWS credentials are role sessions of a few hours. So a download
// URL is a link: the API checks its signature and expiry, presigns the read
// for linkRedirect and redirects, and the link lasts as long as it was asked
// to. Uploads and host transfers stay presigned and last at most an hour,
// capped at what the signing credentials have left (signedLifetime).

// linkRedirect is how long the presigned URL a link redirects to lasts; the
// client follows it at once.
const linkRedirect = 5 * time.Minute

// Links configures download links.
type Links struct {
	// URL is where the API serves links, ending in "/".
	URL string
	// Key signs links; every server holds the same one.
	Key []byte
}

// link is what a link names: one version of one object, the response
// headers its reads get, and when the link ends. ETag is the object's when
// the link was made, empty when nothing was there, so a link never serves
// what is written at its key later.
type link struct {
	Bucket      string `json:"b"`
	Key         string `json:"k"`
	ETag        string `json:"v"`
	ContentType string `json:"t,omitempty"`
	Disposition string `json:"d,omitempty"`
	Expires     int64  `json:"e"`
}

// linkURL returns the link for l, bound to the object's current version.
func (s *Storage) linkURL(ctx context.Context, l link) (string, error) {
	if s.config.Links.URL == "" || len(s.config.Links.Key) == 0 {
		return "", errors.New("download links are not configured")
	}
	etag, err := s.objectETag(ctx, l.Bucket, l.Key)
	if err != nil {
		return "", err
	}
	l.ETag = etag
	payload, err := json.Marshal(l)
	if err != nil {
		return "", fmt.Errorf("encode link: %w", err)
	}
	body := base64.RawURLEncoding.EncodeToString(payload)
	return s.config.Links.URL + body + "." + base64.RawURLEncoding.EncodeToString(s.linkMAC(body)), nil
}

func (s *Storage) linkMAC(body string) []byte {
	mac := hmac.New(sha256.New, s.config.Links.Key)
	mac.Write([]byte(body))
	return mac.Sum(nil)
}

// OpenLink returns a short-lived presigned GET, or HEAD when head is set,
// of the object token names. A token this server did not sign, or one past
// its expiry, is ErrNotFound.
func (s *Storage) OpenLink(ctx context.Context, token string, head bool) (string, error) {
	body, signature, ok := strings.Cut(token, ".")
	mac, err := base64.RawURLEncoding.DecodeString(signature)
	if !ok || err != nil || len(s.config.Links.Key) == 0 || !hmac.Equal(mac, s.linkMAC(body)) {
		return "", ErrNotFound
	}
	payload, err := base64.RawURLEncoding.DecodeString(body)
	if err != nil {
		return "", ErrNotFound
	}
	var l link
	if err := json.Unmarshal(payload, &l); err != nil {
		return "", ErrNotFound
	}
	left := time.Until(time.Unix(l.Expires, 0))
	if left <= 0 {
		return "", ErrNotFound
	}
	// The object must still be the one the link was made for. It could be
	// replaced in the moments before the client follows the redirect.
	etag, err := s.objectETag(ctx, l.Bucket, l.Key)
	if err != nil {
		return "", err
	}
	if etag == "" || etag != l.ETag {
		return "", ErrNotFound
	}
	lifetime, err := s.signedLifetime(ctx, min(linkRedirect, left))
	if err != nil {
		return "", err
	}
	expires := s3.WithPresignExpires(lifetime)
	if head {
		r, err := s.presign.PresignHeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(l.Bucket), Key: aws.String(l.Key)}, expires)
		if err != nil {
			return "", fmt.Errorf("presign link head: %w", err)
		}
		return r.URL, nil
	}
	input := &s3.GetObjectInput{Bucket: aws.String(l.Bucket), Key: aws.String(l.Key)}
	if l.ContentType != "" {
		input.ResponseContentType = aws.String(l.ContentType)
	}
	if l.Disposition != "" {
		input.ResponseContentDisposition = aws.String(l.Disposition)
	}
	r, err := s.presign.PresignGetObject(ctx, input, expires)
	if err != nil {
		return "", fmt.Errorf("presign link get: %w", err)
	}
	return r.URL, nil
}

// objectETag is the ETag of bucket/key, or "" when nothing is there.
func (s *Storage) objectETag(ctx context.Context, bucket, key string) (string, error) {
	head, err := s.client.HeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(bucket), Key: aws.String(key)})
	// HEAD has no body, so a missing bucket is NotFound too.
	if isNotFound(err) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("head %s: %w", key, err)
	}
	return aws.ToString(head.ETag), nil
}

// signedLifetime is want, capped at how long the credentials that sign a
// presigned URL now stay valid, a minute spared.
func (s *Storage) signedLifetime(ctx context.Context, want time.Duration) (time.Duration, error) {
	creds, err := s.client.Options().Credentials.Retrieve(ctx)
	if err != nil {
		return 0, fmt.Errorf("object store credentials: %w", err)
	}
	if !creds.CanExpire {
		return want, nil
	}
	left := time.Until(creds.Expires) - time.Minute
	if left < time.Second {
		return 0, errors.New("the object store credentials are about to expire")
	}
	return min(want, left), nil
}
