package compute

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"regexp"
	"strings"
)

//go:embed install.sh
var installScript string

// versionPattern bounds release versions in install paths.
var versionPattern = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// ValidVersion reports whether v can name a release.
func ValidVersion(v string) bool { return versionPattern.MatchString(v) }

// InstallScript is the agent install script with the target release's
// version and digests filled in, or blanks when none is published.
func (c *Compute) InstallScript(ctx context.Context) (string, error) {
	release, err := c.TargetRelease(ctx)
	if err != nil && !errors.Is(err, ErrNotFound) {
		return "", err
	}
	return strings.NewReplacer(
		"__AGENT_VERSION__", release.Version,
		"__AGENT_AMD64_SHA256__", release.SHA256["amd64"],
		"__AGENT_ARM64_SHA256__", release.SHA256["arm64"],
	).Replace(installScript), nil
}

// ArchiveName is the release archive file for an architecture.
func ArchiveName(arch string) string {
	return fmt.Sprintf("lazycloud-agent-linux-%s.tar.gz", arch)
}
