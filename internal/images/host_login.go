package images

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/ecr"
	"github.com/aws/aws-sdk-go-v2/service/sts"
)

// hostSession is how long a host login's role session lasts. The server's
// own credentials are a role session already, and a chained session lasts
// at most an hour.
const hostSession = time.Hour

// pullWindow is how long a pull login must stay valid once handed out.
const pullWindow = 15 * time.Minute

// hostAccess is what one host command may do in the platform registry:
// repositories, as paths under the registry, it may pull from and those it
// may also push to.
type hostAccess struct {
	pull []string
	push []string
}

func (a hostAccess) key() string {
	return strings.Join(a.pull, ",") + "|" + strings.Join(a.push, ",")
}

type hostLogin struct {
	auth    *Auth
	expires time.Time
}

// host returns the login for one host command, valid until at least until
// where the session allows. A static login is shared as it is. An ECR login
// is minted from a session of Config.HostRole whose policy names only
// access's repositories, so a host holds no grant to another workspace's
// images, caches or snapshots; a login for the same access is reused while
// it outlasts until.
func (l *platformLogin) host(ctx context.Context, access hostAccess, until time.Time) (*Auth, error) {
	if l.ecr == nil {
		return l.static, nil
	}
	if l.hostRole == "" {
		return nil, fmt.Errorf("the image registry has no host role to scope host logins")
	}
	key := access.key()
	l.mu.Lock()
	cached, ok := l.hosts[key]
	l.mu.Unlock()
	if ok && cached.expires.After(until) {
		return cached.auth, nil
	}
	policy, err := l.sessionPolicy(access)
	if err != nil {
		return nil, err
	}
	session, err := l.sts.AssumeRole(ctx, &sts.AssumeRoleInput{
		RoleArn: aws.String(l.hostRole), RoleSessionName: aws.String("lazycloud-registry-host"),
		Policy: aws.String(policy), DurationSeconds: aws.Int32(int32(hostSession.Seconds())),
	})
	if err != nil {
		return nil, fmt.Errorf("assume the registry host role: %w", err)
	}
	creds := session.Credentials
	if creds == nil {
		return nil, fmt.Errorf("STS returned no credentials for the registry host role")
	}
	cfg := l.ecrConfig.Copy()
	cfg.Credentials = credentials.NewStaticCredentialsProvider(
		aws.ToString(creds.AccessKeyId), aws.ToString(creds.SecretAccessKey), aws.ToString(creds.SessionToken))
	auth, expires, err := ecrLogin(ctx, ecr.NewFromConfig(cfg))
	if err != nil {
		return nil, fmt.Errorf("get host registry login: %w", err)
	}
	// The token may be bound to the session, so it is trusted no longer
	// than the session lasts.
	if ends := aws.ToTime(creds.Expiration); ends.Before(expires) {
		expires = ends
	}
	now := time.Now()
	l.mu.Lock()
	for k, h := range l.hosts {
		if !h.expires.After(now) {
			delete(l.hosts, k)
		}
	}
	l.hosts[key] = hostLogin{auth: auth, expires: expires}
	l.mu.Unlock()
	return auth, nil
}

// sessionPolicy limits a host role session to access: pulls from its pull
// and push repositories, pushes (and the first push creating the
// repository) to its push repositories only.
func (l *platformLogin) sessionPolicy(access hostAccess) (string, error) {
	arns := func(paths []string) []string {
		out := make([]string, 0, len(paths))
		for _, p := range paths {
			out = append(out, l.repositoryARN+p)
		}
		return out
	}
	type statement struct {
		Effect   string   `json:"Effect"`
		Action   []string `json:"Action"`
		Resource []string `json:"Resource"`
	}
	statements := []statement{{Effect: "Allow", Action: []string{"ecr:GetAuthorizationToken"}, Resource: []string{"*"}}}
	if readable := slices.Concat(access.pull, access.push); len(readable) > 0 {
		statements = append(statements, statement{
			Effect: "Allow", Action: []string{"ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"},
			Resource: arns(readable),
		})
	}
	if len(access.push) > 0 {
		statements = append(statements, statement{
			Effect: "Allow", Action: []string{
				"ecr:CreateRepository", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
			},
			Resource: arns(access.push),
		})
	}
	policy, err := json.Marshal(map[string]any{"Version": "2012-10-17", "Statement": statements})
	if err != nil {
		return "", fmt.Errorf("encode session policy: %w", err)
	}
	return string(policy), nil
}

// ecrRepositoryARN is the ARN prefix of repositories in registry, an ECR
// host <account>.dkr.ecr.<region>.amazonaws.com[.cn].
func ecrRepositoryARN(registry string) string {
	match := ecrHost.FindStringSubmatch(registry)
	if match == nil {
		return ""
	}
	partition := "aws"
	if strings.HasSuffix(registry, ".cn") {
		partition = "aws-cn"
	}
	account, _, _ := strings.Cut(registry, ".")
	return fmt.Sprintf("arn:%s:ecr:%s:%s:repository/", partition, match[1], account)
}
