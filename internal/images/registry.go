package images

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/remote/transport"
)

const (
	// digestTTL is how long a public tag's digest is reused. A moved tag is
	// picked up after this long.
	digestTTL       = 5 * time.Minute
	maxCachedTags   = 1024
	registryTimeout = 30 * time.Second
)

// Auth authenticates to one registry.
type Auth struct {
	Username      string
	Password      string
	IdentityToken string
}

func (a *Auth) authenticator() authn.Authenticator {
	if a == nil {
		return authn.Anonymous
	}
	return authn.FromConfig(authn.AuthConfig{Username: a.Username, Password: a.Password, IdentityToken: a.IdentityToken})
}

// resolver pins image references to digests with registry HEAD requests.
// Anonymous lookups of tags are cached; lookups with credentials always go
// to the registry, so one workspace's credentials never answer another's
// request.
type resolver struct {
	transport http.RoundTripper

	mu    sync.Mutex
	cache map[string]cachedDigest
}

type cachedDigest struct {
	digest  string
	expires time.Time
}

func newResolver(rt http.RoundTripper) *resolver {
	if rt == nil {
		rt = remote.DefaultTransport
	}
	return &resolver{transport: rt, cache: map[string]cachedDigest{}}
}

// pinned is a reference with its digest, written registry/repository:tag@digest.
type pinned struct {
	registry string
	ref      string
}

// pin resolves ref to its manifest digest. A reference that already names a
// digest is checked too, so a caller cannot use an image it cannot read.
func (r *resolver) pin(ctx context.Context, ref string, auth *Auth, insecure bool) (pinned, error) {
	options := []name.Option{name.WeakValidation}
	if insecure {
		options = append(options, name.Insecure)
	}
	parsed, err := name.ParseReference(ref, options...)
	if err != nil {
		return pinned{}, invalid("image reference %q is invalid: %v", ref, err)
	}
	repo := parsed.Context()
	registry := repo.RegistryStr()
	if registry == name.DefaultRegistry {
		registry = "docker.io"
	}
	display := registry + "/" + repo.RepositoryStr()
	tag := ""
	if t, ok := parsed.(name.Tag); ok {
		tag = t.TagStr()
	} else if _, explicit := splitTag(ref); explicit != "" {
		tag = explicit
	}
	if tag != "" {
		display += ":" + tag
	}

	key := parsed.Name()
	if auth == nil {
		r.mu.Lock()
		hit, ok := r.cache[key]
		r.mu.Unlock()
		if ok && time.Now().Before(hit.expires) {
			return pinned{registry: registry, ref: display + "@" + hit.digest}, nil
		}
	}
	ctx, cancel := context.WithTimeout(ctx, registryTimeout)
	defer cancel()
	desc, err := remote.Head(parsed, remote.WithContext(ctx), remote.WithAuth(auth.authenticator()), remote.WithTransport(r.transport))
	if err != nil {
		return pinned{}, registryError(display, err)
	}
	digest := desc.Digest.String()
	if d, ok := parsed.(name.Digest); ok && d.DigestStr() != digest {
		return pinned{}, invalid("image %s has digest %s, not %s", display, digest, d.DigestStr())
	}
	if auth == nil {
		r.mu.Lock()
		if len(r.cache) >= maxCachedTags {
			for k, v := range r.cache {
				if time.Now().After(v.expires) || len(r.cache) >= maxCachedTags {
					delete(r.cache, k)
				}
			}
		}
		r.cache[key] = cachedDigest{digest: digest, expires: time.Now().Add(digestTTL)}
		r.mu.Unlock()
	}
	return pinned{registry: registry, ref: display + "@" + digest}, nil
}

// splitTag returns ref without @digest and the tag it names, if any.
func splitTag(ref string) (string, string) {
	repository, _, _ := strings.Cut(ref, "@")
	slash := strings.LastIndex(repository, "/")
	if colon := strings.LastIndex(repository, ":"); colon > slash {
		return repository, repository[colon+1:]
	}
	return repository, ""
}

// registryHost is the registry an image reference names, docker.io for
// Docker Hub.
func registryHost(ref string) (string, error) {
	parsed, err := name.ParseReference(ref, name.WeakValidation)
	if err != nil {
		return "", invalid("image reference %q is invalid: %v", ref, err)
	}
	host := parsed.Context().RegistryStr()
	if host == name.DefaultRegistry {
		host = "docker.io"
	}
	return host, nil
}

// ErrRegistryUnavailable means a registry could not be reached or failed.
var ErrRegistryUnavailable = errors.New("registry unavailable")

func registryError(ref string, err error) error {
	var terr *transport.Error
	if errors.As(err, &terr) {
		switch terr.StatusCode {
		case http.StatusUnauthorized, http.StatusForbidden:
			return invalid("the registry refused access to %s; check the credentials", ref)
		case http.StatusNotFound:
			return invalid("image %s does not exist", ref)
		}
	}
	return fmt.Errorf("%w: look up %s: %w", ErrRegistryUnavailable, ref, err)
}
