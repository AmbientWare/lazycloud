package edge

import (
	"context"
	"errors"
	"fmt"
	"io"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// ErrSyncRefused means the container's agent could not apply the sync.
var ErrSyncRefused = errors.New("the container could not apply the sync")

// Sync streams a tar of source changes to a container's workspace over its
// host's data connection, and returns what the agent applied.
func (e *Edge) Sync(ctx context.Context, host, container uuid.UUID, archive io.Reader) (written, removed int, err error) {
	p, err := e.stream(ctx, host)
	if err != nil {
		return 0, 0, err
	}
	defer p.finish(nil)
	stop := context.AfterFunc(ctx, func() { p.finish(ctx.Err()) })
	defer stop()
	if err := p.stream.Send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_Head{Head: &hostproto.RequestHead{
		ContainerId: container.String(), Kind: &hostproto.RequestHead_Sync{Sync: &hostproto.WorkspaceSync{}},
	}}}); err != nil {
		return 0, 0, fmt.Errorf("send sync head: %w", err)
	}
	sent := make(chan error, 1)
	go func() { //nolint:gocritic // waited for below
		sent <- sendRequestBody(p, &requestBody{stream: archive})
	}()
	var result *hostproto.SyncResult
	var refusal *hostproto.ForwardError
	for result == nil && refusal == nil {
		msg, rerr := p.stream.Recv()
		if rerr != nil {
			err = fmt.Errorf("receive sync result: %w", rerr)
			break
		}
		result, refusal = msg.GetSynced(), msg.GetError()
	}
	p.finish(nil)
	if serr := <-sent; err == nil && result == nil && serr != nil {
		err = serr
	}
	switch {
	case refusal != nil:
		return 0, 0, fmt.Errorf("%w: %s", ErrSyncRefused, refusal.GetMessage())
	case result == nil:
		return 0, 0, err
	}
	return int(result.GetWritten()), int(result.GetRemoved()), nil
}
