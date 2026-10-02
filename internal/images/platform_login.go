package images

import (
	"context"
	"encoding/base64"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ecr"
	"github.com/aws/aws-sdk-go-v2/service/sts"
)

// ecrRefresh is how long before its expiry an ECR login is replaced. It
// outlasts a build's one-hour deadline, so a login handed to a host stays
// valid while the host uses it.
const ecrRefresh = 2 * time.Hour

// platformLogin is the platform registry's login: Config.Auth, or, with
// Config.ECR, a token GetAuthorizationToken mints from the server's AWS
// credentials. The server's own login (auth) can read and write every
// workload repository and never leaves the server; hosts get logins scoped
// to one command (host). ECR tokens last 12 hours, so the server's login is
// replaced before ecrRefresh remains.
type platformLogin struct {
	static *Auth
	ecr    *ecr.Client
	// ecrConfig, sts, hostRole and repositoryARN mint host logins.
	ecrConfig     aws.Config
	sts           *sts.Client
	hostRole      string
	repositoryARN string

	mu      sync.Mutex
	current *Auth
	expires time.Time
	hosts   map[string]hostLogin
}

func newPlatformLogin(config Config) *platformLogin {
	login := &platformLogin{static: config.Auth, hostRole: config.HostRole, hosts: map[string]hostLogin{}}
	if config.ECR != nil {
		cfg := config.ECR.Copy()
		if match := ecrHost.FindStringSubmatch(config.Registry); match != nil {
			cfg.Region = match[1]
		}
		login.ecr = ecr.NewFromConfig(cfg)
		login.ecrConfig = cfg
		login.sts = sts.NewFromConfig(cfg)
		login.repositoryARN = ecrRepositoryARN(config.Registry)
	}
	return login
}

// IsECR reports whether registry is an Amazon ECR registry host.
func IsECR(registry string) bool { return ecrHost.MatchString(registry) }

// auth returns the current login; nil means the registry is anonymous.
func (l *platformLogin) auth(ctx context.Context) (*Auth, error) {
	if l.ecr == nil {
		return l.static, nil
	}
	l.mu.Lock()
	current, expires := l.current, l.expires
	l.mu.Unlock()
	if current != nil && time.Until(expires) > ecrRefresh {
		return current, nil
	}
	// Concurrent refreshes each fetch a token; any of them is valid.
	out, err := l.ecr.GetAuthorizationToken(ctx, &ecr.GetAuthorizationTokenInput{})
	if err != nil {
		return nil, fmt.Errorf("get platform registry login: %w", err)
	}
	if len(out.AuthorizationData) == 0 {
		return nil, fmt.Errorf("ECR returned no authorization data")
	}
	data := out.AuthorizationData[0]
	auth, err := decodeECRToken(aws.ToString(data.AuthorizationToken))
	if err != nil {
		return nil, err
	}
	l.mu.Lock()
	l.current, l.expires = auth, aws.ToTime(data.ExpiresAt)
	l.mu.Unlock()
	return auth, nil
}

// decodeECRToken splits an ECR authorization token, base64 user:password.
func decodeECRToken(token string) (*Auth, error) {
	decoded, err := base64.StdEncoding.DecodeString(token)
	if err != nil {
		return nil, fmt.Errorf("decode ECR token: %w", err)
	}
	user, password, ok := strings.Cut(string(decoded), ":")
	if !ok {
		return nil, fmt.Errorf("ECR token is not user:password")
	}
	return &Auth{Username: user, Password: password}, nil
}
