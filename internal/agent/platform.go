package agent

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/moby/moby/client"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// platformImages holds how to run each image the agent runs on its own,
// such as the image builder and the volume mount image, as the server
// last sent it. The server converts each image the Hello names and sends
// its copy in the platform registry with layer grants, like any workload
// image's, so these images too are read through the snapshotter with
// grants of their own.
type platformImages struct {
	named []string

	mu     sync.Mutex
	images map[string]*hostproto.PlatformImage
	// changed is closed and replaced whenever images changes.
	changed chan struct{}
}

func newPlatformImages(references ...string) *platformImages {
	return &platformImages{
		named:  slices.Compact(slices.Sorted(slices.Values(references))),
		images: map[string]*hostproto.PlatformImage{}, changed: make(chan struct{}),
	}
}

// update records the images the server sent for references the agent
// named.
func (p *platformImages) update(images []*hostproto.PlatformImage) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, image := range images {
		if slices.Contains(p.named, image.GetReference()) {
			p.images[image.GetReference()] = image
		}
	}
	close(p.changed)
	p.changed = make(chan struct{})
}

// platformListTimeout bounds the container listing a session open waits
// for, so a slow Docker does not hold the session back.
const platformListTimeout = 5 * time.Second

// forgetFailures drops the failures recorded for images, so that waiters
// wait for the next answer.
func (p *platformImages) forgetFailures() {
	p.mu.Lock()
	defer p.mu.Unlock()
	for reference, image := range p.images {
		if image.GetFailure() != "" {
			delete(p.images, reference)
		}
	}
}

// runningPlatformImages lists the images that this host's mount and
// network holder containers run, which may be copies of platform images
// an earlier agent named: their layers need grants while they run.
func (a *Agent) runningPlatformImages(ctx context.Context) ([]string, error) {
	ctx, cancel := context.WithTimeout(ctx, platformListTimeout)
	defer cancel()
	list, err := a.docker.ContainerList(ctx, client.ContainerListOptions{
		Filters: client.Filters{}.Add("label", labelHost+"="+a.identity.HostID),
	})
	if err != nil {
		return nil, fmt.Errorf("list containers: %w", err)
	}
	var out []string
	for _, summary := range list.Items {
		switch summary.Labels[labelKind] {
		case kindMount, kindBucket, kindHolder:
			if !slices.Contains(out, summary.Image) {
				out = append(out, summary.Image)
			}
		}
	}
	return out, nil
}

// platformImage makes reference, an image the agent named, present and
// returns the image to run: the server's copy, its layers granted to the
// snapshotter, pulled lazily. It waits for the server to send it until
// ctx ends.
func (a *Agent) platformImage(ctx context.Context, reference string) (string, error) {
	for {
		a.platform.mu.Lock()
		image, changed := a.platform.images[reference], a.platform.changed
		a.platform.mu.Unlock()
		if image == nil {
			select {
			case <-changed:
				continue
			case <-ctx.Done():
				return "", fmt.Errorf("wait for platform image %s: %w", reference, ctx.Err())
			}
		}
		if image.GetFailure() != "" {
			return "", fmt.Errorf("platform image %s cannot be converted: %s", reference, image.GetFailure())
		}
		// A start of the copy, named by its digest: its reads are not a
		// container's startup.
		_, digest, _ := strings.Cut(image.GetImage(), "@")
		if err := a.layers.grant(ctx, "platform:"+digest, image.GetLayers()); err != nil {
			return "", err
		}
		if _, err := a.images.ensureLazy(ctx, image.GetImage(), image.GetAuth(), image.GetPlatform()); err != nil {
			return "", err
		}
		return image.GetImage(), nil
	}
}
