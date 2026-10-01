package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/moby/moby/api/types/jsonstream"
	"github.com/moby/moby/client"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"golang.org/x/sys/unix"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// maxPublishes bounds filesystem images being imported and pushed at once.
const maxPublishes = 2

// unlimitedDiskArchiveBytes bounds the archive of a container without a disk
// limit, beside its attached disks.
const unlimitedDiskArchiveBytes = 64 << 30

// publishFilesystem starts a PublishFilesystem once per request id.
func (a *Agent) publishFilesystem(request *hostproto.PublishFilesystem) {
	if !isUUID(request.GetRequestId()) {
		a.log.Warn("ignoring a filesystem publish with an invalid id", "request_id", request.GetRequestId())
		return
	}
	key := "filesystem:" + request.GetRequestId()
	if !a.claimOperation(key) {
		return
	}
	a.goOwned(func(ctx context.Context) {
		defer a.releaseOperation(key)
		deadline := hostDeadline(request.GetDeadline().AsTime(), request.GetDeadline() != nil)
		work, cancel := context.WithDeadline(ctx, deadline)
		reference, architecture, err := a.pushFilesystem(work, request)
		cancel()
		result := &hostproto.CompleteFilesystemImageRequest{ContainerId: request.GetContainerId(), RequestId: request.GetRequestId(),
			Reference: reference, Architecture: architecture}
		if err != nil {
			result = &hostproto.CompleteFilesystemImageRequest{ContainerId: request.GetContainerId(), RequestId: request.GetRequestId(), Failure: err.Error()}
			a.log.Warn("filesystem publish failed", "container_id", request.GetContainerId(), "request_id", request.GetRequestId(), "error", err)
		}
		a.deliver(ctx, deadline, "filesystem image", func(ctx context.Context) error {
			_, err := a.host.CompleteFilesystemImage(ctx, result)
			return err //nolint:wrapcheck // deliver inspects the status
		})
	})
}

// pushFilesystem streams the supervisor's tar of the container's root
// filesystem into a single-layer image with the source image's runtime
// configuration, pushes it and returns it by digest with its architecture.
// The local copy is removed afterwards.
func (a *Agent) pushFilesystem(ctx context.Context, request *hostproto.PublishFilesystem) (string, string, error) {
	c := a.lookup(request.GetContainerId())
	if c == nil || !c.reachable() {
		return "", "", errors.New("the container does not run on this host")
	}
	select {
	case a.publishes <- struct{}{}:
		defer func() { <-a.publishes }()
	case <-ctx.Done():
		return "", "", fmt.Errorf("wait for a publish slot: %w", ctx.Err())
	}
	inspect, err := a.docker.ContainerInspect(ctx, c.dockerName(), client.ContainerInspectOptions{})
	if err != nil {
		return "", "", fmt.Errorf("inspect container: %w", err)
	}
	source, err := a.docker.ImageInspect(ctx, inspect.Container.Image)
	if err != nil {
		return "", "", fmt.Errorf("inspect the container's image: %w", err)
	}
	var changes []string
	if config := source.Config; config != nil {
		if changes, err = imageChanges(config.Env, config.WorkingDir, config.Entrypoint, config.Cmd, config.User); err != nil {
			return "", "", err
		}
	}

	tarRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://container/filesystem", nil)
	if err != nil {
		return "", "", fmt.Errorf("filesystem request: %w", err)
	}
	response, err := (&http.Client{Transport: c.control}).Do(tarRequest)
	if err != nil {
		return "", "", fmt.Errorf("read the container's filesystem: %w", err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(response.Body, maxPortErrorBytes))
		return "", "", fmt.Errorf("read the container's filesystem: %s %s", response.Status, strings.TrimSpace(string(body)))
	}

	tag := request.GetRepository() + ":fs-" + request.GetRequestId()
	began := time.Now()
	// The supervisor's socket answers for the workload, so the archive is
	// held to what the container can store.
	var storage map[string]string
	if inspect.Container.HostConfig != nil {
		storage = inspect.Container.HostConfig.StorageOpt
	}
	archive := &countingReader{r: response.Body, max: c.archiveBound(storage)}
	imported, err := a.docker.ImageImport(ctx, client.ImageImportSource{Source: archive, SourceName: "-"}, tag, client.ImageImportOptions{
		Changes:  changes,
		Message:  "lazycloud filesystem of container " + c.id,
		Platform: ocispec.Platform{OS: source.Os, Architecture: source.Architecture, Variant: source.Variant},
	})
	if err != nil {
		return "", "", fmt.Errorf("import filesystem: %w", err)
	}
	err = readJSONStream(imported)
	_ = imported.Close()
	if err != nil {
		return "", "", fmt.Errorf("import filesystem: %w", err)
	}
	defer func() {
		if _, err := a.docker.ImageRemove(context.WithoutCancel(ctx), tag, client.ImageRemoveOptions{PruneChildren: true}); err != nil {
			a.log.Warn("removing a pushed filesystem image failed", "image", tag, "error", err)
		}
	}()

	options, err := pullOptions(request.GetRegistryAuth(), "")
	if err != nil {
		return "", "", err
	}
	importedAt := time.Now()
	pushed, err := a.docker.ImagePush(ctx, tag, client.ImagePushOptions{RegistryAuth: options.RegistryAuth})
	if err != nil {
		return "", "", fmt.Errorf("push %s: %w", tag, err)
	}
	defer func() { _ = pushed.Close() }()
	var digest string
	for message, err := range pushed.JSONMessages(ctx) {
		if err != nil {
			return "", "", fmt.Errorf("push %s: %w", tag, err)
		}
		if message.Error != nil {
			return "", "", fmt.Errorf("push %s: %w", tag, message.Error)
		}
		if message.Aux != nil {
			var aux struct {
				Digest string `json:"Digest"`
			}
			if json.Unmarshal(*message.Aux, &aux) == nil && aux.Digest != "" {
				digest = aux.Digest
			}
		}
	}
	if !strings.HasPrefix(digest, "sha256:") {
		return "", "", fmt.Errorf("push %s: the registry reported no digest", tag)
	}
	a.log.Info("filesystem image published", "container_id", c.id, "archive_bytes", archive.n,
		"import_ms", importedAt.Sub(began).Milliseconds(), "push_ms", time.Since(importedAt).Milliseconds())
	return request.GetRepository() + "@" + digest, source.Architecture, nil
}

// archiveBound is the most a container's filesystem archive may hold: its
// disk limit, or unlimitedDiskArchiveBytes, and the size of every attached
// disk.
func (c *container) archiveBound(storage map[string]string) int64 {
	bound := int64(unlimitedDiskArchiveBytes)
	if size, err := strconv.ParseInt(storage["size"], 10, 64); err == nil && size > 0 {
		bound = size
	}
	c.disks.mu.Lock()
	defer c.disks.mu.Unlock()
	for _, d := range c.disks.disks {
		var fs unix.Statfs_t
		if err := unix.Statfs(c.a.diskMount(c.id, d.Name), &fs); err == nil {
			bound += int64(fs.Blocks) * fs.Bsize //nolint:gosec // block counts fit
		}
	}
	return bound
}

// errArchiveTooLarge ends an archive longer than its container can store.
var errArchiveTooLarge = errors.New("the filesystem archive is larger than the container's disks")

// countingReader counts the bytes read through it and fails past max.
type countingReader struct {
	r   io.Reader
	n   int64
	max int64
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n += int64(n)
	if c.n > c.max {
		return n, errArchiveTooLarge
	}
	return n, err //nolint:wrapcheck // readers compare io.EOF
}

// imageChanges restates the source image's environment, working directory,
// entrypoint, command and user for the imported image.
// A value with a line end cannot be written as a change and is left out.
func imageChanges(environment []string, workingDir string, entrypoint, cmd []string, user string) ([]string, error) {
	var changes []string
	env := map[string]string{}
	for _, entry := range environment {
		key, value, _ := strings.Cut(entry, "=")
		if !strings.ContainsAny(value, "\r\n") {
			env[key] = value
		}
	}
	for _, key := range slices.Sorted(maps.Keys(env)) {
		changes = append(changes, "ENV "+key+"="+dockerfileQuote(env[key]))
	}
	if workingDir != "" {
		changes = append(changes, "WORKDIR "+workingDir)
	}
	for _, exec := range []struct {
		instruction string
		args        []string
	}{{"ENTRYPOINT", entrypoint}, {"CMD", cmd}} {
		if len(exec.args) == 0 {
			continue
		}
		encoded, err := json.Marshal(exec.args)
		if err != nil {
			return nil, fmt.Errorf("encode %s: %w", exec.instruction, err)
		}
		changes = append(changes, exec.instruction+" "+string(encoded))
	}
	if user != "" {
		changes = append(changes, "USER "+user)
	}
	return changes, nil
}

// dockerfileQuote quotes an ENV value so the Dockerfile parser reads it back
// unchanged, with no variable expansion or word splitting.
func dockerfileQuote(value string) string {
	replacer := strings.NewReplacer(`\`, `\\`, `"`, `\"`, `$`, `\$`)
	return `"` + replacer.Replace(value) + `"`
}

// readJSONStream reads a Docker progress stream to its end, returning the
// first error it reports.
func readJSONStream(r io.Reader) error {
	decoder := json.NewDecoder(r)
	for {
		var message jsonstream.Message
		err := decoder.Decode(&message)
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("read progress: %w", err)
		}
		if message.Error != nil {
			return message.Error
		}
	}
}
