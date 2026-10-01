package edge

import (
	"fmt"
	"net"
	"net/url"
	"strconv"
	"strings"

	"github.com/google/uuid"
)

// URLs builds the addresses workloads answer on under the edge's public
// base, such as https://lazycloud.run or http://lazycloud.localhost:8082.
type URLs struct {
	scheme string
	// base is the domain workload labels sit under.
	base string
	// port is appended to every host when the base names one.
	port string
}

// ParseURLs reads the edge's public base URL.
func ParseURLs(public string) (URLs, error) {
	u, err := url.Parse(public)
	if err != nil {
		return URLs{}, fmt.Errorf("parse edge URL: %w", err)
	}
	if (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || (u.Path != "" && u.Path != "/") {
		return URLs{}, fmt.Errorf("edge URL %q must be an http or https origin", public)
	}
	return URLs{scheme: u.Scheme, base: strings.ToLower(u.Hostname()), port: u.Port()}, nil
}

// Base is the domain workload labels sit under; customer domains CNAME to
// it.
func (u URLs) Base() string { return u.base }

func (u URLs) host(label string) string {
	host := label + "." + u.base
	if u.port != "" {
		host = net.JoinHostPort(host, u.port)
	}
	return host
}

func (u URLs) build(host, path string) string {
	out := url.URL{Scheme: u.scheme, Host: host}
	if path != "" && path != "/" {
		out.Path = path
	}
	return out.String()
}

// Deployment follows the workload's active release across deploys.
func (u URLs) Deployment(subdomain, route string) string {
	return u.build(u.host(subdomain), route)
}

// Version pins one release version.
func (u URLs) Version(subdomain string, version int, route string) string {
	return u.build(u.host(subdomain+"-v"+strconv.Itoa(version)), route)
}

// Release addresses one release by id, which is also how a preview is
// reached.
func (u URLs) Release(release uuid.UUID, route string) string {
	return u.build(u.host(release.String()), route)
}

// Container addresses one container.
func (u URLs) Container(container uuid.UUID, route string) string {
	return u.build(u.host(container.String()), route)
}

// Domain is a custom hostname, served on the default port of the scheme.
func (u URLs) Domain(hostname, route string) string {
	return u.build(hostname, route)
}

// hostLabel splits a request host into the label under the base, or reports
// that the host is not under it.
func (u URLs) hostLabel(host string) (string, bool) {
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	label, ok := strings.CutSuffix(host, "."+u.base)
	if !ok || label == "" || strings.Contains(label, ".") {
		return host, false
	}
	return label, true
}

// deploymentLabel is what a deployment host names: "<sub>" and
// "<sub>-latest" the active release, "<sub>-vN" version N. The subdomain
// ends in a hex digest, which is neither "latest" nor "vN", so the suffix
// splits off the right unambiguously.
func deploymentLabel(label string) (subdomain string, version int) {
	stem, marker, ok := cutLast(label, "-")
	if !ok {
		return label, 0
	}
	if marker == "latest" {
		return stem, 0
	}
	if digits, ok := strings.CutPrefix(marker, "v"); ok && digits != "" && digits[0] != '0' {
		if n, err := strconv.Atoi(digits); err == nil {
			return stem, n
		}
	}
	return label, 0
}

func cutLast(s, sep string) (string, string, bool) {
	i := strings.LastIndex(s, sep)
	if i < 0 {
		return s, "", false
	}
	return s[:i], s[i+len(sep):], true
}
