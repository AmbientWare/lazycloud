package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"time"

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

func loadOrEnroll(ctx context.Context, cfg Config, capacity *hostproto.Capacity) (identity, error) {
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
	if cfg.JoinToken == "" {
		return identity{}, errors.New("the host is not enrolled and no join token was given")
	}
	conn, err := dialServer(cfg.Server, "", cfg.Telemetry)
	if err != nil {
		return identity{}, err
	}
	defer func() { _ = conn.Close() }()
	hostname, _ := os.Hostname()
	request := &hostproto.EnrollRequest{JoinToken: cfg.JoinToken, Hostname: hostname, Capacity: capacity}
	delay := 250 * time.Millisecond
	for {
		response, err := hostproto.NewHostServiceClient(conn).Enroll(ctx, request)
		if err == nil {
			id = identity{HostID: response.GetHostId(), HostToken: response.GetHostToken()}
			break
		}
		if status.Code(err) != codes.Unavailable {
			return identity{}, fmt.Errorf("enroll: %w", err)
		}
		cfg.Logger.Warn("server unavailable for enrollment", "error", err)
		if !sleep(ctx, delay) {
			return identity{}, fmt.Errorf("enroll: %w", ctx.Err())
		}
		delay = min(2*delay, 10*time.Second)
	}
	data, err = json.Marshal(id)
	if err != nil {
		return identity{}, fmt.Errorf("encode host identity: %w", err)
	}
	if err := writeFileAtomic(identityPath(cfg.StateDir), data, 0o600); err != nil {
		return identity{}, fmt.Errorf("save host identity: %w", err)
	}
	cfg.Logger.Info("enrolled", "host_id", id.HostID)
	return id, nil
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
