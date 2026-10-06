package images

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"slices"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/remote/transport"
	"github.com/google/go-containerregistry/pkg/v1/types"
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

// newResolver returns a resolver whose requests reach only public addresses,
// except trusted, the platform registry's host[:port]. A user's base image
// names the registry, and the registry names its token realm, so either
// could otherwise point lookups at internal services. The check runs on the
// address actually dialed, after DNS.
func newResolver(trusted string) *resolver {
	allowed := map[string]bool{}
	if trusted != "" {
		if _, _, err := net.SplitHostPort(trusted); err == nil {
			allowed[trusted] = true
		} else {
			allowed[net.JoinHostPort(trusted, "443")] = true
			allowed[net.JoinHostPort(trusted, "80")] = true
		}
	}
	plain := &net.Dialer{Timeout: registryTimeout, KeepAlive: 30 * time.Second}
	public := &net.Dialer{Timeout: registryTimeout, KeepAlive: 30 * time.Second, Control: refusePrivate}
	transport, _ := remote.DefaultTransport.(*http.Transport) //nolint:errcheck // Checked below.
	if transport == nil {
		transport = &http.Transport{}
	}
	transport = transport.Clone()
	transport.Proxy = nil
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		if allowed[address] {
			return plain.DialContext(ctx, network, address)
		}
		return public.DialContext(ctx, network, address)
	}
	return &resolver{transport: transport, cache: map[string]cachedDigest{}}
}

// errPrivateAddress means a registry lookup would reach a non-public address.
var errPrivateAddress = errors.New("the registry resolves to a private address")

func refusePrivate(_, address string, _ syscall.RawConn) error {
	addrPort, err := netip.ParseAddrPort(address)
	if err != nil {
		return fmt.Errorf("%w: %s", errPrivateAddress, address)
	}
	if !publicAddress(addrPort.Addr()) {
		return fmt.Errorf("%w: %s", errPrivateAddress, address)
	}
	return nil
}

func publicAddress(ip netip.Addr) bool {
	ip = ip.Unmap()
	// 100.64.0.0/10 is carrier-grade NAT space, private in practice.
	shared := ip.Is4() && ip.As4()[0] == 100 && ip.As4()[1]&0xc0 == 64
	return ip.IsValid() && ip.IsGlobalUnicast() && !ip.IsPrivate() && !shared
}

// checkRegistryHost refuses a registry a user names when it is a loopback,
// private or link-local address or localhost. Hostnames are checked again
// when dialed.
func checkRegistryHost(host string) error {
	name := host
	if h, _, err := net.SplitHostPort(host); err == nil {
		name = h
	}
	name = strings.Trim(name, "[]")
	if strings.EqualFold(name, "localhost") || strings.HasSuffix(strings.ToLower(name), ".localhost") {
		return invalid("registry %s is not a public registry", host)
	}
	if ip, err := netip.ParseAddr(name); err == nil && !publicAddress(ip) {
		return invalid("registry %s is not a public registry", host)
	}
	return nil
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

// maxImageLayers is the most layers an image may have; Docker refuses to
// build deeper ones.
const maxImageLayers = 127

// registryLayer is one layer of an image: its blob in the registry and the
// uncompressed digest the image config names for it.
type registryLayer struct {
	blob   string
	diffID string
	// size is the blob's, as the manifest names it.
	size int64
}

// layers reads the layers of ref, an image by digest, for platform
// (os/arch). The registry checks each blob against its digest, so the blobs
// are the image's bytes; the diff_ids are what the config claims. An image
// the platform cannot convert is an InvalidError saying why.
func (r *resolver) layers(ctx context.Context, ref string, auth *Auth, insecure bool, platform string) ([]registryLayer, error) {
	options := []name.Option{name.WeakValidation}
	if insecure {
		options = append(options, name.Insecure)
	}
	parsed, err := name.NewDigest(ref, options...)
	if err != nil {
		return nil, fmt.Errorf("image reference %q is invalid: %w", ref, err)
	}
	p, err := v1.ParsePlatform(platform)
	if err != nil {
		return nil, fmt.Errorf("platform %q is invalid: %w", platform, err)
	}
	ctx, cancel := context.WithTimeout(ctx, registryTimeout)
	defer cancel()
	img, err := remote.Image(parsed, remote.WithContext(ctx), remote.WithAuth(auth.authenticator()), remote.WithTransport(r.transport), remote.WithPlatform(*p))
	if err != nil {
		return nil, fmt.Errorf("%w: read %s: %w", ErrRegistryUnavailable, ref, err)
	}
	manifest, err := img.Manifest()
	if err != nil {
		return nil, fmt.Errorf("%w: read the manifest of %s: %w", ErrRegistryUnavailable, ref, err)
	}
	config, err := img.ConfigFile()
	if err != nil {
		return nil, fmt.Errorf("%w: read the config of %s: %w", ErrRegistryUnavailable, ref, err)
	}
	if len(manifest.Layers) > maxImageLayers {
		return nil, invalid("the image has %d layers; at most %d are supported", len(manifest.Layers), maxImageLayers)
	}
	if len(manifest.Layers) != len(config.RootFS.DiffIDs) {
		return nil, invalid("the image manifest has %d layers but its config names %d", len(manifest.Layers), len(config.RootFS.DiffIDs))
	}
	out := make([]registryLayer, len(manifest.Layers))
	for n, l := range manifest.Layers {
		// Conversion reads tar layers, compressed or not; foreign and
		// non-distributable layers are not in the registry.
		convertible := []types.MediaType{types.DockerLayer, types.DockerUncompressedLayer, types.OCILayer, types.OCILayerZStd, types.OCIUncompressedLayer}
		if !slices.Contains(convertible, l.MediaType) {
			return nil, invalid("layer %d has media type %s, which cannot be converted", n, l.MediaType)
		}
		diffID := config.RootFS.DiffIDs[n]
		if l.Digest.Algorithm != "sha256" || diffID.Algorithm != "sha256" {
			return nil, invalid("layer %d is not addressed by sha256", n)
		}
		out[n] = registryLayer{blob: l.Digest.String(), diffID: diffID.String(), size: l.Size}
	}
	return out, nil
}

// splitTag returns ref without @digest and the tag it names, if any.
// untagged is ref, a reference by digest, without its tag. The managed
// base's tags name the release that pushed them, and an unchanged base
// must keep the identity of every image built on it.
func untagged(ref string) string {
	repository, _, digest := splitReference(ref)
	return repository + "@" + digest
}

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
	if errors.Is(err, errPrivateAddress) {
		return invalid("looking up %s reached a private address; images must come from public registries", ref)
	}
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
