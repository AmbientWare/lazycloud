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
	"github.com/jackc/pgx/v5"
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

// linkURL returns the link for l in bucket b, bound to the object's
// current version.
func (s *Storage) linkURL(ctx context.Context, b bucketClient, l link) (string, error) {
	if s.config.Links.URL == "" || len(s.config.Links.Key) == 0 {
		return "", errors.New("download links are not configured")
	}
	o, err := head(ctx, b, l.Key)
	if err != nil && !errors.Is(err, ErrNotFound) {
		return "", err
	}
	l.Bucket, l.ETag = b.name, o.ETag
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

// OpenLink returns a short-lived presigned GET, or HEAD when headOnly is set,
// of the object token names. A token this server did not sign, or one past
// its expiry, is ErrNotFound.
func (s *Storage) OpenLink(ctx context.Context, token string, headOnly bool) (string, error) {
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
	b, err := s.linkBucket(ctx, l.Bucket)
	if err != nil {
		return "", err
	}
	// The object must still be the one the link was made for. It could be
	// replaced in the moments before the client follows the redirect.
	o, err := head(ctx, b, l.Key)
	if err != nil {
		return "", err
	}
	if o.ETag != l.ETag {
		return "", ErrNotFound
	}
	lifetime, err := s.signedLifetime(ctx, b.client, min(linkRedirect, left))
	if err != nil {
		return "", err
	}
	expires := s3.WithPresignExpires(lifetime)
	if headOnly {
		r, err := b.presign.PresignHeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(b.name), Key: aws.String(l.Key)}, expires)
		if err != nil {
			return "", fmt.Errorf("presign link head: %w", err)
		}
		return r.URL, nil
	}
	input := &s3.GetObjectInput{Bucket: aws.String(b.name), Key: aws.String(l.Key)}
	if l.ContentType != "" {
		input.ResponseContentType = aws.String(l.ContentType)
	}
	if l.Disposition != "" {
		input.ResponseContentDisposition = aws.String(l.Disposition)
	}
	r, err := b.presign.PresignGetObject(ctx, input, expires)
	if err != nil {
		return "", fmt.Errorf("presign link get: %w", err)
	}
	return r.URL, nil
}

// linkBucket is the bucket a link names: the platform bucket or a
// workspace bucket. A workspace bucket that is gone is ErrNotFound.
func (s *Storage) linkBucket(ctx context.Context, name string) (bucketClient, error) {
	if name == s.platform.name {
		return s.platform, nil
	}
	row, err := s.queries.BucketByName(ctx, name)
	if errors.Is(err, pgx.ErrNoRows) {
		return bucketClient{}, ErrNotFound
	}
	if err != nil {
		return bucketClient{}, fmt.Errorf("read link bucket: %w", err)
	}
	return s.storeOf(ctx, row.Bucket, row.Region, row.ConnectionID)
}

// signedLifetime is want, capped at how long client's credentials, which
// sign a presigned URL, stay valid, less a minute. Expiring credentials
// come from the default chain's cache, which reports them credentialWindow
// before they expire and renews them then, so a URL lasts at least the
// window less that minute.
func (s *Storage) signedLifetime(ctx context.Context, client *s3.Client, want time.Duration) (time.Duration, error) {
	provider := client.Options().Credentials
	creds, err := provider.Retrieve(ctx)
	if err != nil {
		return 0, fmt.Errorf("object store credentials: %w", err)
	}
	if !creds.CanExpire {
		return want, nil
	}
	if creds.Expired() {
		// The source issued them inside the renewal window, so the cache
		// stored them expired; it fetches again on this call.
		if creds, err = provider.Retrieve(ctx); err != nil {
			return 0, fmt.Errorf("object store credentials: %w", err)
		}
	}
	left := time.Until(creds.Expires) + credentialWindow - time.Minute
	if left < time.Second {
		return 0, errors.New("the object store credentials are about to expire")
	}
	return min(want, left), nil
}
