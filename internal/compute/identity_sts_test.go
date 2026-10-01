package compute

import (
	"context"
	"errors"
	"net/http"
	"os"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/sts"
	"github.com/aws/smithy-go/middleware"
	smithyhttp "github.com/aws/smithy-go/transport/http"
	"github.com/google/uuid"
)

// TestRealSTSVerifiesASignedHostHeader sends identity proofs to AWS STS
// itself, so it runs only with LAZYCLOUD_STS_ACCEPTANCE_PROFILE naming a
// disposable AWS profile. STS answers a proof signed for the host and
// refuses the same signature presented for another host.
func TestRealSTSVerifiesASignedHostHeader(t *testing.T) {
	profile := os.Getenv("LAZYCLOUD_STS_ACCEPTANCE_PROFILE")
	if profile == "" {
		t.Skip("set LAZYCLOUD_STS_ACCEPTANCE_PROFILE to a disposable AWS profile")
	}
	ctx := t.Context()
	cfg, err := config.LoadDefaultConfig(ctx, config.WithSharedConfigProfile(profile), config.WithRegion("us-east-2"))
	if err != nil {
		t.Fatal(err)
	}
	host := HostID(uuid.New())
	proof := presign(ctx, t, cfg, host)
	c := &Compute{http: &http.Client{}}

	caller, err := c.verifyProof(ctx, host, proof)
	if err != nil {
		t.Fatalf("verify a proof signed for the host: %v", err)
	}
	if !strings.HasPrefix(caller.Arn, "arn:aws:sts::") || caller.Account == "" {
		t.Fatalf("STS answered %+v", caller)
	}
	t.Logf("STS identity %s", caller.Arn)

	other := HostID(uuid.New())
	forged := proof
	forged.Headers = map[string]string{}
	for k, v := range proof.Headers {
		forged.Headers[k] = v
	}
	forged.Headers[HostIDHeader] = other.String()
	var refused *IdentityError
	if _, err := c.verifyProof(ctx, other, forged); !errors.As(err, &refused) {
		t.Fatalf("a proof re-addressed to another host: got %v, want STS to refuse it", err)
	}
}

func presign(ctx context.Context, t *testing.T, cfg aws.Config, host HostID) IdentityProof {
	t.Helper()
	presigned, err := sts.NewPresignClient(sts.NewFromConfig(cfg)).PresignGetCallerIdentity(ctx, &sts.GetCallerIdentityInput{},
		sts.WithPresignClientFromClientOptions(func(o *sts.Options) {
			o.APIOptions = append(o.APIOptions, func(stack *middleware.Stack) error {
				return stack.Build.Add(middleware.BuildMiddlewareFunc("HostHeader", func(ctx context.Context, in middleware.BuildInput, next middleware.BuildHandler) (middleware.BuildOutput, middleware.Metadata, error) {
					if req, ok := in.Request.(*smithyhttp.Request); ok {
						req.Header.Set(HostIDHeader, host.String())
						query := req.URL.Query()
						query.Set("X-Amz-Expires", "60")
						req.URL.RawQuery = query.Encode()
					}
					return next.HandleBuild(ctx, in)
				}), middleware.After)
			})
		}))
	if err != nil {
		t.Fatal(err)
	}
	headers := map[string]string{}
	for name, values := range presigned.SignedHeader {
		headers[name] = strings.Join(values, ",")
	}
	return IdentityProof{URL: presigned.URL, Method: presigned.Method, Headers: headers}
}

// Only STS endpoints pass: a lookalike such as an S3 website host does not.
func TestIdentityProofsTargetOnlySTS(t *testing.T) {
	for host, want := range map[string]bool{
		"sts.amazonaws.com":                      true,
		"sts.us-east-2.amazonaws.com":            true,
		"sts.us-gov-west-1.amazonaws.com":        true,
		"sts.s3-website-us-east-1.amazonaws.com": false,
		"sts.us-east-2.amazonaws.com.evil.test":  false,
		"evil.sts.us-east-2.amazonaws.com":       false,
	} {
		if got := stsHost.MatchString(host); got != want {
			t.Errorf("%s: accepted %v, want %v", host, got, want)
		}
	}
}
