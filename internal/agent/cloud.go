package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials/ec2rolecreds"
	"github.com/aws/aws-sdk-go-v2/feature/ec2/imds"
	"github.com/aws/aws-sdk-go-v2/service/sts"
	"github.com/aws/smithy-go/middleware"
	smithyhttp "github.com/aws/smithy-go/transport/http"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	// hostIDHeader is signed into the identity request, so a proof names one
	// host and cannot enroll another.
	hostIDHeader = "LazyCloud-Host-Id"
	// identityExpirySeconds bounds how long a captured proof can be replayed.
	identityExpirySeconds = "60"
	// interruptionPoll is how often the agent asks IMDS for a Spot notice.
	// EC2 gives two minutes' warning.
	interruptionPoll = 5 * time.Second
	imdsCallTimeout  = 3 * time.Second
	spotActionPath   = "spot/instance-action"
)

// DefaultIMDSEndpoint is the EC2 instance metadata service.
const DefaultIMDSEndpoint = "http://169.254.169.254"

func newIMDS(endpoint string) *imds.Client {
	return imds.New(imds.Options{Endpoint: endpoint})
}

// cloudIdentity presigns an STS GetCallerIdentity request with the instance
// profile's credentials. The server sends it to STS to learn the instance's
// role session, and the signature covers the host id header.
func cloudIdentity(ctx context.Context, metadata *imds.Client, hostID string) (*hostproto.CloudIdentity, error) {
	region, err := readMetadata(ctx, metadata, "placement/region")
	if err != nil {
		return nil, err
	}
	credentials := aws.NewCredentialsCache(ec2rolecreds.New(func(o *ec2rolecreds.Options) { o.Client = metadata }))
	client := sts.New(sts.Options{Region: region, Credentials: credentials})
	presigned, err := sts.NewPresignClient(client).PresignGetCallerIdentity(ctx, &sts.GetCallerIdentityInput{},
		sts.WithPresignClientFromClientOptions(func(o *sts.Options) {
			o.APIOptions = append(o.APIOptions, func(stack *middleware.Stack) error {
				return stack.Build.Add(signIdentityFields(hostID), middleware.After)
			})
		}))
	if err != nil {
		return nil, fmt.Errorf("presign instance identity: %w", err)
	}
	headers := map[string]string{}
	for name, values := range presigned.SignedHeader {
		headers[name] = strings.Join(values, ",")
	}
	return &hostproto.CloudIdentity{HostId: hostID, Url: presigned.URL, Method: presigned.Method, Headers: headers}, nil
}

// signIdentityFields adds the host id header and a short expiry before the
// request is signed.
func signIdentityFields(hostID string) middleware.BuildMiddleware {
	return middleware.BuildMiddlewareFunc("LazyCloudIdentityFields", func(ctx context.Context, in middleware.BuildInput, next middleware.BuildHandler) (middleware.BuildOutput, middleware.Metadata, error) {
		if req, ok := in.Request.(*smithyhttp.Request); ok {
			req.Header.Set(hostIDHeader, hostID)
			query := req.URL.Query()
			query.Set("X-Amz-Expires", identityExpirySeconds)
			req.URL.RawQuery = query.Encode()
		}
		return next.HandleBuild(ctx, in)
	})
}

func readMetadata(ctx context.Context, metadata *imds.Client, path string) (string, error) {
	out, err := metadata.GetMetadata(ctx, &imds.GetMetadataInput{Path: path})
	if err != nil {
		return "", fmt.Errorf("read instance metadata %s: %w", path, err)
	}
	defer func() { _ = out.Content.Close() }()
	data, err := io.ReadAll(io.LimitReader(out.Content, 4096))
	if err != nil {
		return "", fmt.Errorf("read instance metadata %s: %w", path, err)
	}
	return strings.TrimSpace(string(data)), nil
}

// interruption is a provider's reclaim notice.
type interruption struct {
	reason    string
	reclaimAt time.Time
}

func (i *interruption) message() *hostproto.HostMessage {
	return &hostproto.HostMessage{Body: &hostproto.HostMessage_Interruption{Interruption: &hostproto.Interruption{
		Reason: i.reason, ReclaimAt: timestamppb.New(i.reclaimAt),
	}}}
}

// watchInterruptions polls IMDS for a Spot interruption notice and reports
// it once; every later session restates it in runSession.
func (a *Agent) watchInterruptions(ctx context.Context) {
	ticker := time.NewTicker(interruptionPoll)
	defer ticker.Stop()
	for {
		notice, err := spotNotice(ctx, a.metadata)
		switch {
		case err != nil:
			if ctx.Err() != nil {
				return
			}
			a.log.Warn("reading the spot interruption notice failed", "error", err)
		case notice != nil:
			a.mu.Lock()
			changed := a.interruption == nil || a.interruption.reason != notice.reason || !a.interruption.reclaimAt.Equal(notice.reclaimAt)
			a.interruption = notice
			a.mu.Unlock()
			if changed {
				a.log.Warn("the provider will reclaim this host", "reason", notice.reason, "reclaim_at", notice.reclaimAt)
				a.report(notice.message())
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		case <-a.interruptionNow:
		}
	}
}

// spotNotice reads spot/instance-action. IMDS answers 404 while there is
// none; the client refreshes its session token on 401.
func spotNotice(ctx context.Context, metadata *imds.Client) (*interruption, error) {
	ctx, cancel := context.WithTimeout(ctx, imdsCallTimeout)
	defer cancel()
	body, err := readMetadata(ctx, metadata, spotActionPath)
	if err != nil {
		var response *smithyhttp.ResponseError
		if errors.As(err, &response) && response.HTTPStatusCode() == http.StatusNotFound {
			return nil, nil
		}
		return nil, err
	}
	var notice struct {
		Action string    `json:"action"`
		Time   time.Time `json:"time"`
	}
	if err := json.Unmarshal([]byte(body), &notice); err != nil || notice.Action == "" || notice.Time.IsZero() {
		return nil, fmt.Errorf("unreadable %s notice %q", spotActionPath, body)
	}
	return &interruption{reason: "aws-ec2-spot-" + notice.Action, reclaimAt: notice.Time}, nil
}
