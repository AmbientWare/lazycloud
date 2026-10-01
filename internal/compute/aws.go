package compute

import (
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials/stscreds"
	"github.com/aws/aws-sdk-go-v2/service/cloudformation"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	"github.com/aws/aws-sdk-go-v2/service/sts"
	"github.com/aws/smithy-go"
)

// awsScope is the credentials one account's calls use: the platform's own,
// or a connection role assumed with its external ID.
type awsScope struct {
	credentials aws.CredentialsProvider
	// key names the scope in cooldowns and logs: platform or a connection id.
	key string
}

// awsClients builds AWS clients for a scope and region with the fleet's
// endpoint overrides.
type awsClients struct {
	base      aws.Config
	endpoints Endpoints
}

func (a awsClients) config(scope awsScope, region string) aws.Config {
	cfg := a.base.Copy()
	if region != "" {
		cfg.Region = region
	}
	if scope.credentials != nil {
		cfg.Credentials = scope.credentials
	}
	if cfg.HTTPClient == nil {
		cfg.HTTPClient = &http.Client{Timeout: 30 * time.Second}
	}
	return cfg
}

func (a awsClients) ec2(scope awsScope, region string) *ec2.Client {
	return ec2.NewFromConfig(a.config(scope, region), func(o *ec2.Options) {
		if a.endpoints.EC2 != "" {
			o.BaseEndpoint = aws.String(a.endpoints.EC2)
		}
	})
}

func (a awsClients) sts(scope awsScope, region string) *sts.Client {
	return sts.NewFromConfig(a.config(scope, region), func(o *sts.Options) {
		if a.endpoints.STS != "" {
			o.BaseEndpoint = aws.String(a.endpoints.STS)
		}
	})
}

func (a awsClients) cloudFormation(scope awsScope, region string) *cloudformation.Client {
	return cloudformation.NewFromConfig(a.config(scope, region), func(o *cloudformation.Options) {
		if a.endpoints.CloudFormation != "" {
			o.BaseEndpoint = aws.String(a.endpoints.CloudFormation)
		}
	})
}

// assume returns a scope that assumes role with externalID through the
// platform's credentials. Credentials are cached until shortly before they
// expire.
func (a awsClients) assume(role, externalID, session, key string) awsScope {
	provider := stscreds.NewAssumeRoleProvider(a.sts(awsScope{}, ""), role, func(o *stscreds.AssumeRoleOptions) {
		o.ExternalID = aws.String(externalID)
		o.RoleSessionName = session
		o.Duration = time.Hour
	})
	return awsScope{credentials: aws.NewCredentialsCache(provider), key: key}
}

// awsCode is the AWS error code of err, or "".
func awsCode(err error) string {
	var api smithy.APIError
	if errors.As(err, &api) {
		return api.ErrorCode()
	}
	return ""
}

// capacityCodes are launch refusals that mean the offer has no capacity or
// quota now; the offer cools down and the controller picks another.
var capacityCodes = map[string]bool{
	"InsufficientInstanceCapacity": true,
	"InstanceLimitExceeded":        true,
	"VcpuLimitExceeded":            true,
	"MaxSpotInstanceCountExceeded": true,
	"SpotMaxPriceTooLow":           true,
	"Unsupported":                  true,
	"InsufficientCapacity":         true,
}

// accessDenied reports whether AWS refused the caller's authority.
func accessDenied(err error) bool {
	code := awsCode(err)
	return code == "AccessDenied" || code == "AccessDeniedException" || code == "UnauthorizedOperation" ||
		strings.HasSuffix(code, "AccessDenied")
}

// describeAWSError shortens an AWS error to its code and message.
func describeAWSError(err error) string {
	var api smithy.APIError
	if errors.As(err, &api) {
		return fmt.Sprintf("%s: %s", api.ErrorCode(), api.ErrorMessage())
	}
	return err.Error()
}
