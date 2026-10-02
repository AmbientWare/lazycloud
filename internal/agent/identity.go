package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/feature/ec2/imds"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// identity is the host's enrolled identity, persisted 0600 in the state
// directory. The join token is used once; later starts reuse the host token.
type identity struct {
	HostID    string `json:"host_id"`
	HostToken string `json:"host_token"`
}

func identityPath(stateDir string) string { return filepath.Join(stateDir, "identity.json") }

// loadOrEnroll returns the saved identity or enrolls with the join token or
// cloud identity. A host that fails an error-severity preflight check still
// enrolls, so the server records why, but its identity is not kept.
func loadOrEnroll(ctx context.Context, cfg Config, offered offer, metadata *imds.Client) (identity, error) {
	var id identity
	data, err := os.ReadFile(identityPath(cfg.StateDir))
	switch {
	case err == nil:
		if err := json.Unmarshal(data, &id); err != nil || id.HostID == "" || id.HostToken == "" {
			return identity{}, fmt.Errorf("host identity %s is invalid", identityPath(cfg.StateDir))
		}
		return id, nil
	case !errors.Is(err, fs.ErrNotExist):
		return identity{}, fmt.Errorf("read host identity: %w", err)
	}
	joinToken := cfg.JoinToken
	if joinToken == "" && cfg.JoinTokenFile != "" {
		data, err := os.ReadFile(cfg.JoinTokenFile)
		if err != nil {
			return identity{}, fmt.Errorf("read join token: %w", err)
		}
		joinToken = strings.TrimSpace(string(data))
	}
	if joinToken == "" && cfg.CloudHostID == "" {
		return identity{}, errors.New("the host is not enrolled and no join token was given")
	}
	if cfg.CloudHostID != "" && metadata == nil {
		return identity{}, errors.New("cloud enrollment needs the instance metadata endpoint")
	}
	conn, err := dialServer(cfg, "")
	if err != nil {
		return identity{}, err
	}
	defer func() { _ = conn.Close() }()
	hostname := cfg.Hostname
	if hostname == "" {
		hostname, _ = os.Hostname()
	}
	request := &hostproto.EnrollRequest{
		JoinToken:    joinToken,
		Hostname:     hostname,
		Capacity:     offered.capacity,
		Preflight:    offered.checks,
		Architecture: runtime.GOARCH,
	}
	delay := 250 * time.Millisecond
	for {
		if cfg.CloudHostID != "" {
			// A proof expires within a minute, so each attempt signs anew.
			if request.CloudIdentity, err = cloudIdentity(ctx, metadata, cfg.CloudHostID); err != nil {
				return identity{}, err
			}
		}
		response, err := hostproto.NewHostServiceClient(conn).Enroll(ctx, request)
		if err == nil {
			id = identity{HostID: response.GetHostId(), HostToken: response.GetHostToken()}
			break
		}
		code := status.Code(err)
		if slices.Contains(refusals, code) {
			return identity{}, fmt.Errorf("%w: %w", ErrEnrollmentRefused, err)
		}
		if code != codes.Unavailable {
			return identity{}, fmt.Errorf("enroll: %w", err)
		}
		cfg.Logger.Warn("server unavailable for enrollment", "error", err)
		if !sleep(ctx, delay) {
			return identity{}, fmt.Errorf("enroll: %w", ctx.Err())
		}
		delay = min(2*delay, 10*time.Second)
	}
	if failed := offered.failed(); len(failed) > 0 {
		removeJoinToken(cfg)
		return identity{}, fmt.Errorf("%w: %s", ErrPreflightFailed, describeChecks(failed))
	}
	data, err = json.Marshal(id)
	if err != nil {
		return identity{}, fmt.Errorf("encode host identity: %w", err)
	}
	if err := writeFileAtomic(identityPath(cfg.StateDir), data, 0o600); err != nil {
		return identity{}, fmt.Errorf("save host identity: %w", err)
	}
	removeJoinToken(cfg)
	cfg.Logger.Info("enrolled", "host_id", id.HostID)
	return id, nil
}

// refusals are Enroll answers that a retry cannot change.
var refusals = []codes.Code{codes.Unauthenticated, codes.PermissionDenied, codes.InvalidArgument, codes.FailedPrecondition, codes.NotFound} //nolint:gochecknoglobals // constant table

// removeJoinToken deletes the used token file.
func removeJoinToken(cfg Config) {
	if cfg.JoinTokenFile == "" {
		return
	}
	if err := os.Remove(cfg.JoinTokenFile); err != nil && !errors.Is(err, fs.ErrNotExist) {
		cfg.Logger.Warn("removing the used join token failed", "error", err)
	}
}

// forgetIdentity deletes a revoked identity, so a later join enrolls anew.
func forgetIdentity(stateDir string) error {
	if err := os.Remove(identityPath(stateDir)); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("remove revoked host identity: %w", err)
	}
	return nil
}

// writeFileAtomic writes data beside path and renames it into place.
func writeFileAtomic(path string, data []byte, mode os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), filepath.Base(path)+".*.tmp")
	if err != nil {
		return fmt.Errorf("create temporary file: %w", err)
	}
	defer func() { _ = os.Remove(tmp.Name()) }()
	if err := tmp.Chmod(mode); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("chmod: %w", err)
	}
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("sync: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close: %w", err)
	}
	if err := os.Rename(tmp.Name(), path); err != nil {
		return fmt.Errorf("rename: %w", err)
	}
	return nil
}
