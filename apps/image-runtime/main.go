package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/beam-cloud/clip/pkg/clip"
	clipcommon "github.com/beam-cloud/clip/pkg/common"
	clipstorage "github.com/beam-cloud/clip/pkg/storage"
	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/v1"
	"github.com/hanwen/go-fuse/v2/fuse"
)

const maxRequestBytes = 1024 * 1024

type serverConfig struct {
	socketPath string
	imageRoot  string
	mountRoot  string
	cacheRoot  string
	buildRoot  string
}

type registryCredentials struct {
	Registry      string `json:"registry"`
	Username      string `json:"username"`
	Password      string `json:"password"`
	Auth          string `json:"auth"`
	IdentityToken string `json:"identity_token"`
	RegistryToken string `json:"registry_token"`
}

type request struct {
	ID              string              `json:"id"`
	Action          string              `json:"action"`
	ImageID         string              `json:"image_id"`
	ArchiveSHA256   string              `json:"archive_sha256"`
	ArchivePath     string              `json:"archive_path"`
	MountPoint      string              `json:"mount_point"`
	CachePath       string              `json:"cache_path"`
	LocalLayoutPath string              `json:"local_layout_path"`
	StorageImageRef string              `json:"storage_image_ref"`
	OutputPath      string              `json:"output_path"`
	Architecture    string              `json:"architecture"`
	Preload         bool                `json:"preload"`
	Credentials     registryCredentials `json:"credentials"`
}

type response struct {
	ID         string `json:"id"`
	OK         bool   `json:"ok"`
	MountPoint string `json:"mount_point,omitempty"`
	Mounts     int    `json:"mounts,omitempty"`
	Error      string `json:"error,omitempty"`
}

type mountedImage struct {
	server        *fuse.Server
	archiveSHA256 string
	mountPoint    string
}

type imageRuntime struct {
	config      serverConfig
	credentials *mutableCredentialProvider
	mu          sync.Mutex
	mounts      map[string]mountedImage
	locks       map[string]*sync.Mutex
}

type mutableCredentialProvider struct {
	mu          sync.RWMutex
	credentials map[string]*authn.AuthConfig
}

func newMutableCredentialProvider() *mutableCredentialProvider {
	return &mutableCredentialProvider{credentials: make(map[string]*authn.AuthConfig)}
}

func (p *mutableCredentialProvider) Name() string { return "lazycloud-broker" }

func (p *mutableCredentialProvider) GetCredentials(_ context.Context, registry, _ string) (*authn.AuthConfig, error) {
	p.mu.RLock()
	credentials := p.credentials[registry]
	p.mu.RUnlock()
	if credentials == nil {
		return nil, clipcommon.ErrNoCredentials
	}
	copy := *credentials
	return &copy, nil
}

func (p *mutableCredentialProvider) update(credentials registryCredentials) error {
	registry := strings.TrimSpace(credentials.Registry)
	if registry == "" {
		return errors.New("registry credentials do not name a registry")
	}
	p.mu.Lock()
	p.credentials[registry] = &authn.AuthConfig{
		Username:      credentials.Username,
		Password:      credentials.Password,
		Auth:          credentials.Auth,
		IdentityToken: credentials.IdentityToken,
		RegistryToken: credentials.RegistryToken,
	}
	p.mu.Unlock()
	return nil
}

func main() {
	config := serverConfig{}
	indexOnly := flag.Bool("index", false, "create one image index from a stdin request")
	flag.StringVar(&config.socketPath, "socket", "/run/lazycloud/image-runtime.sock", "private Unix socket")
	flag.StringVar(&config.imageRoot, "image-root", "/var/lib/lazycloud/images", "image index root")
	flag.StringVar(&config.mountRoot, "mount-root", "/var/lib/lazycloud/image-mounts", "image mount root")
	flag.StringVar(&config.cacheRoot, "cache-root", "/var/lib/lazycloud/cache", "content cache root")
	flag.StringVar(&config.buildRoot, "build-root", "/var/lib/lazycloud/image-builds", "image build root")
	flag.Parse()

	if err := clip.SetLogLevel("warn"); err != nil {
		slog.Error("image runtime log configuration failed", "error", err)
		os.Exit(1)
	}
	runtime := &imageRuntime{
		config:      config,
		credentials: newMutableCredentialProvider(),
		mounts:      make(map[string]mountedImage),
		locks:       make(map[string]*sync.Mutex),
	}
	if *indexOnly {
		var req request
		decoder := json.NewDecoder(io.LimitReader(os.Stdin, maxRequestBytes))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&req); err != nil {
			slog.Error("invalid image index request")
			os.Exit(1)
		}
		if err := runtime.index(req); err != nil {
			slog.Error("image index failed", "error", err)
			os.Exit(1)
		}
		return
	}
	if err := runtime.serve(); err != nil {
		slog.Error("image runtime stopped", "error", err)
		os.Exit(1)
	}
}

func (r *imageRuntime) serve() error {
	if err := os.MkdirAll(filepath.Dir(r.config.socketPath), 0o700); err != nil {
		return err
	}
	_ = os.Remove(r.config.socketPath)
	listener, err := net.Listen("unix", r.config.socketPath)
	if err != nil {
		return err
	}
	defer listener.Close()
	defer os.Remove(r.config.socketPath)
	if err := os.Chmod(r.config.socketPath, 0o600); err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		_ = listener.Close()
	}()
	slog.Info("image runtime ready", "socket", r.config.socketPath)

	for {
		connection, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil {
				r.unmountAll()
				return nil
			}
			return err
		}
		go r.handle(connection)
	}
}

func (r *imageRuntime) handle(connection net.Conn) {
	defer connection.Close()
	scanner := bufio.NewScanner(connection)
	scanner.Buffer(make([]byte, 64*1024), maxRequestBytes)
	for scanner.Scan() {
		var req request
		if err := json.Unmarshal(scanner.Bytes(), &req); err != nil {
			r.write(connection, response{ID: req.ID, Error: "invalid image runtime request"})
			continue
		}
		result := r.dispatch(req)
		r.write(connection, result)
	}
}

func (r *imageRuntime) write(connection net.Conn, result response) {
	encoded, err := json.Marshal(result)
	if err != nil {
		return
	}
	encoded = append(encoded, '\n')
	_, _ = connection.Write(encoded)
}

func (r *imageRuntime) dispatch(req request) (result response) {
	result.ID = req.ID
	defer func() {
		if recovered := recover(); recovered != nil {
			result.OK = false
			result.Error = "image runtime request failed"
		}
	}()
	var err error
	switch req.Action {
	case "health":
		r.mu.Lock()
		result.Mounts = len(r.mounts)
		r.mu.Unlock()
	case "mount":
		result.MountPoint, err = r.mount(req)
	case "credentials":
		err = r.credentials.update(req.Credentials)
	case "unmount":
		err = r.unmount(req.ImageID)
	default:
		err = fmt.Errorf("unsupported image runtime action %q", req.Action)
	}
	if err != nil {
		result.Error = err.Error()
		return result
	}
	result.OK = true
	return result
}

func (r *imageRuntime) index(req request) error {
	layout, err := pathWithin(r.config.buildRoot, req.LocalLayoutPath)
	if err != nil {
		return fmt.Errorf("invalid OCI layout path: %w", err)
	}
	output, err := pathWithin(r.config.buildRoot, req.OutputPath)
	if err != nil {
		return fmt.Errorf("invalid image index path: %w", err)
	}
	if strings.TrimSpace(req.StorageImageRef) == "" {
		return errors.New("image index requires a storage image reference")
	}
	architecture := req.Architecture
	if architecture == "" {
		architecture = runtime.GOARCH
	}
	if architecture != "amd64" && architecture != "arm64" {
		return fmt.Errorf("unsupported image architecture %q", architecture)
	}
	if err := os.MkdirAll(filepath.Dir(output), 0o700); err != nil {
		return err
	}
	layerIndexes, err := clipstorageDiskLayerIndexCache(filepath.Join(r.config.cacheRoot, "clip-layer-indexes"))
	if err != nil {
		return err
	}
	return clip.CreateFromOCIImage(context.Background(), clip.CreateFromOCIImageOptions{
		ImageRef:         req.StorageImageRef,
		StorageImageRef:  req.StorageImageRef,
		LocalLayoutPath:  layout,
		OutputPath:       output,
		CheckpointMiB:    2,
		Platform:         &v1.Platform{OS: "linux", Architecture: architecture},
		LayerIndexCache:  layerIndexes,
		IndexConcurrency: 8,
	})
}

func clipstorageDiskLayerIndexCache(root string) (*clipstorage.DiskLayerIndexCache, error) {
	return clipstorage.NewDiskLayerIndexCache(root)
}

func (r *imageRuntime) mount(req request) (string, error) {
	if req.ImageID == "" || strings.ContainsAny(req.ImageID, "/\\\x00") {
		return "", errors.New("image mount requires a canonical image id")
	}
	archive, err := pathWithin(r.config.imageRoot, req.ArchivePath)
	if err != nil {
		return "", fmt.Errorf("invalid image archive path: %w", err)
	}
	mountPoint, err := pathWithin(r.config.mountRoot, req.MountPoint)
	if err != nil {
		return "", fmt.Errorf("invalid image mount point: %w", err)
	}
	cachePath, err := pathWithin(r.config.cacheRoot, req.CachePath)
	if err != nil {
		return "", fmt.Errorf("invalid image cache path: %w", err)
	}
	if err := r.credentials.update(req.Credentials); err != nil {
		return "", err
	}

	lock := r.imageLock(req.ImageID)
	lock.Lock()
	defer lock.Unlock()
	r.mu.Lock()
	existing, mounted := r.mounts[req.ImageID]
	r.mu.Unlock()
	if mounted {
		if existing.archiveSHA256 != req.ArchiveSHA256 || existing.mountPoint != mountPoint {
			return "", errors.New("mounted image identity does not match the request")
		}
		return mountPoint, nil
	}
	if len(req.ArchiveSHA256) != 64 {
		return "", errors.New("image mount requires the verified index digest")
	}
	actualDigest, err := fileSHA256(archive)
	if err != nil {
		return "", fmt.Errorf("image index could not be verified: %w", err)
	}
	if actualDigest != req.ArchiveSHA256 {
		return "", errors.New("image index digest does not match the authorized artifact")
	}
	if err := validateArchiveReference(archive, req.StorageImageRef); err != nil {
		return "", err
	}
	if err := os.MkdirAll(mountPoint, 0o755); err != nil {
		return "", err
	}
	options := clip.MountOptions{
		Context:              context.Background(),
		ArchivePath:          archive,
		MountPoint:           mountPoint,
		CachePath:            cachePath,
		UseCheckpoints:       true,
		RegistryCredProvider: r.credentials,
	}
	if req.Preload {
		options.PrepareConcurrency = 8
		if err := clip.PrepareArchiveContent(options); err != nil {
			return "", err
		}
		options.PrepareConcurrency = 0
	}
	start, serverErrors, fuseServer, err := clip.MountArchive(options)
	if err != nil {
		return "", err
	}
	if err := start(); err != nil {
		return "", err
	}
	if err := waitForMount(mountPoint, serverErrors); err != nil {
		_ = fuseServer.Unmount()
		return "", err
	}
	r.mu.Lock()
	r.mounts[req.ImageID] = mountedImage{
		server:        fuseServer,
		archiveSHA256: req.ArchiveSHA256,
		mountPoint:    mountPoint,
	}
	r.mu.Unlock()
	go func() {
		if serverErr, open := <-serverErrors; open && serverErr != nil {
			slog.Error("image mount stopped", "image_id", req.ImageID, "error", serverErr)
		}
		r.mu.Lock()
		delete(r.mounts, req.ImageID)
		r.mu.Unlock()
	}()
	return mountPoint, nil
}

func fileSHA256(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		return "", err
	}
	return fmt.Sprintf("%x", digest.Sum(nil)), nil
}

func validateArchiveReference(archivePath, expected string) error {
	metadata, err := clip.NewClipArchiver().ExtractMetadata(archivePath)
	if err != nil {
		return fmt.Errorf("image index metadata is invalid: %w", err)
	}
	var info clipcommon.OCIStorageInfo
	switch value := metadata.StorageInfo.(type) {
	case clipcommon.OCIStorageInfo:
		info = value
	case *clipcommon.OCIStorageInfo:
		if value == nil {
			return errors.New("image index OCI metadata is empty")
		}
		info = *value
	default:
		return errors.New("image index does not use OCI storage")
	}
	actual := fmt.Sprintf("%s/%s@%s", info.RegistryURL, info.Repository, info.Reference)
	if actual != expected {
		return errors.New("image index OCI reference does not match the authorized descriptor")
	}
	return nil
}

func waitForMount(mountPoint string, serverErrors <-chan error) error {
	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(5 * time.Millisecond)
	defer ticker.Stop()
	for {
		if mountedPath(mountPoint) {
			return nil
		}
		select {
		case serverErr, open := <-serverErrors:
			if open && serverErr != nil {
				return fmt.Errorf("image mount failed: %w", serverErr)
			}
			return errors.New("image mount stopped before becoming ready")
		case <-ticker.C:
		case <-deadline.C:
			return errors.New("image mount was not ready within 5 seconds")
		}
	}
}

func mountedPath(target string) bool {
	contents, err := os.ReadFile("/proc/self/mountinfo")
	if err != nil {
		return false
	}
	target, err = filepath.Abs(target)
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(contents), "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 5 && fields[4] == target {
			return true
		}
	}
	return false
}

func (r *imageRuntime) imageLock(imageID string) *sync.Mutex {
	r.mu.Lock()
	defer r.mu.Unlock()
	lock := r.locks[imageID]
	if lock == nil {
		lock = &sync.Mutex{}
		r.locks[imageID] = lock
	}
	return lock
}

func (r *imageRuntime) unmount(imageID string) error {
	lock := r.imageLock(imageID)
	lock.Lock()
	defer lock.Unlock()
	r.mu.Lock()
	mounted, ok := r.mounts[imageID]
	if ok {
		delete(r.mounts, imageID)
	}
	r.mu.Unlock()
	if !ok {
		return nil
	}
	return mounted.server.Unmount()
}

func (r *imageRuntime) unmountAll() {
	r.mu.Lock()
	ids := make([]string, 0, len(r.mounts))
	for imageID := range r.mounts {
		ids = append(ids, imageID)
	}
	r.mu.Unlock()
	for _, imageID := range ids {
		if err := r.unmount(imageID); err != nil {
			slog.Error("image unmount failed", "image_id", imageID, "error", err)
		}
	}
}

func pathWithin(root, target string) (string, error) {
	root, err := filepath.Abs(root)
	if err != nil {
		return "", err
	}
	target, err = filepath.Abs(target)
	if err != nil {
		return "", err
	}
	relative, err := filepath.Rel(root, target)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
		return "", errors.New("path escapes configured root")
	}
	return target, nil
}
