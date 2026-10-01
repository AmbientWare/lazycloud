package images

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/ecr"
)

// Credential names, the environment variables the SDK reads them from.
//
//nolint:gosec // Names of credentials, not credentials.
const (
	awsAccessKeyID         = "AWS_ACCESS_KEY_ID"
	awsSecretAccessKey     = "AWS_SECRET_ACCESS_KEY"
	awsSessionToken        = "AWS_SESSION_TOKEN"
	awsRegion              = "AWS_REGION"
	githubUsername         = "GITHUB_USERNAME"
	githubToken            = "GITHUB_TOKEN"
	gcpAccessToken         = "GCP_ACCESS_TOKEN"
	googleCredentials      = "GOOGLE_APPLICATION_CREDENTIALS"
	gcpProjectID           = "GCP_PROJECT_ID"
	azureClientID          = "AZURE_CLIENT_ID"
	azureClientSecret      = "AZURE_CLIENT_SECRET"
	azureTenantID          = "AZURE_TENANT_ID"
	ngcAPIKey              = "NGC_API_KEY"
	dockerhubToken         = "DOCKERHUB_TOKEN"
	dockerhubUsername      = "DOCKERHUB_USERNAME"
	dockerhubPassword      = "DOCKERHUB_PASSWORD"
	dockerUsername         = "DOCKER_USERNAME"
	dockerPassword         = "DOCKER_PASSWORD"
	registryUsername       = "REGISTRY_USERNAME"
	registryPassword       = "REGISTRY_PASSWORD"
	genericUsername        = "USERNAME"
	genericPassword        = "PASSWORD"
	lowercaseUsername      = "username"
	lowercasePassword      = "password"
	githubTokenOnlyAccount = "x-access-token"
)

// basicPairs are username and password names in the order they are tried.
func basicPairs() [][2]string {
	return [][2]string{
		{lowercaseUsername, lowercasePassword},
		{dockerhubUsername, dockerhubPassword},
		{dockerUsername, dockerPassword},
		{registryUsername, registryPassword},
		{genericUsername, genericPassword},
		{githubUsername, githubToken},
	}
}

// knownCredential reports whether name is a documented credential name.
func knownCredential(name string) bool {
	switch name {
	case awsAccessKeyID, awsSecretAccessKey, awsSessionToken, awsRegion, gcpAccessToken, googleCredentials,
		gcpProjectID, azureClientID, azureClientSecret, azureTenantID, ngcAPIKey, dockerhubToken:
		return true
	}
	for _, pair := range basicPairs() {
		if name == pair[0] || name == pair[1] {
			return true
		}
	}
	return false
}

var ecrHost = regexp.MustCompile(`^\d{12}\.dkr\.ecr(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$`)

// ecrToken exchanges AWS keys for an ECR registry login.
type ecrToken func(ctx context.Context, region, accessKey, secretKey, sessionToken string) (*Auth, error)

// registryAuth turns the credentials a definition carries into the login for
// host. Names other than the documented ones are ignored; nil means the
// registry is read anonymously.
func registryAuth(ctx context.Context, host string, given map[string]string, exchange ecrToken) (*Auth, error) {
	creds := map[string]string{}
	for k, v := range given {
		if knownCredential(k) && v != "" {
			creds[k] = v
		}
	}
	if len(creds) == 0 {
		return nil, nil //nolint:nilnil // No credentials means anonymous access.
	}
	switch {
	case ecrHost.MatchString(host):
		if err := require(creds, awsAccessKeyID, awsSecretAccessKey); err != nil {
			return nil, err
		}
		region := ecrHost.FindStringSubmatch(host)[1]
		auth, err := exchange(ctx, region, creds[awsAccessKeyID], creds[awsSecretAccessKey], creds[awsSessionToken])
		if err != nil {
			return nil, invalid("ECR refused the AWS credentials for %s: %v", host, err)
		}
		return auth, nil
	case hostIn(host, "gcr.io", "pkg.dev"):
		if token := creds[gcpAccessToken]; token != "" {
			return &Auth{Username: "oauth2accesstoken", Password: token}, nil
		}
		if account := creds[googleCredentials]; account != "" {
			if err := validServiceAccount(account); err != nil {
				return nil, err
			}
			return &Auth{Username: "_json_key", Password: account}, nil
		}
		return nil, invalid("%s or %s is required for %s", gcpAccessToken, googleCredentials, host)
	case hostIn(host, "azurecr.io"):
		if err := require(creds, azureClientID, azureClientSecret); err != nil {
			return nil, err
		}
		return &Auth{Username: creds[azureClientID], Password: creds[azureClientSecret]}, nil
	case hostIn(host, "nvcr.io"):
		if err := require(creds, ngcAPIKey); err != nil {
			return nil, err
		}
		return &Auth{Username: "$oauthtoken", Password: creds[ngcAPIKey]}, nil
	case hostIn(host, "ghcr.io"):
		if pair, err := basicPair(creds, githubUsername, githubToken); pair != nil || err != nil {
			return pair, err
		}
		// GHCR takes a token with any user name.
		if token := creds[githubToken]; token != "" {
			return &Auth{Username: githubTokenOnlyAccount, Password: token}, nil
		}
		return nil, nil //nolint:nilnil // Anonymous.
	}
	for _, name := range []string{ngcAPIKey, githubToken, dockerhubToken} {
		if token := creds[name]; token != "" && (name != githubToken || creds[githubUsername] == "") {
			return &Auth{IdentityToken: token}, nil
		}
	}
	for _, pair := range basicPairs() {
		if auth, err := basicPair(creds, pair[0], pair[1]); auth != nil || err != nil {
			return auth, err
		}
	}
	return nil, nil //nolint:nilnil // Anonymous.
}

func hostIn(host string, suffixes ...string) bool {
	host = strings.ToLower(host)
	for _, s := range suffixes {
		if host == s || strings.HasSuffix(host, "."+s) {
			return true
		}
	}
	return false
}

func require(creds map[string]string, names ...string) error {
	for _, n := range names {
		if creds[n] == "" {
			return invalid("registry credential %s is missing or empty", n)
		}
	}
	return nil
}

// basicPair returns the pair when both names are set, nil when neither is,
// and an error when only one is.
func basicPair(creds map[string]string, user, password string) (*Auth, error) {
	u, p := creds[user], creds[password]
	switch {
	case u == "" && p == "":
		return nil, nil //nolint:nilnil // Neither is set.
	case u == "" || p == "":
		if user == githubUsername && u == "" {
			return nil, nil //nolint:nilnil // A token alone is handled by the caller.
		}
		return nil, invalid("set both %s and %s", user, password)
	}
	return &Auth{Username: u, Password: p}, nil
}

func validServiceAccount(value string) error {
	var account struct {
		Type        string `json:"type"`
		ClientEmail string `json:"client_email"`
		PrivateKey  string `json:"private_key"`
	}
	if err := json.Unmarshal([]byte(value), &account); err != nil {
		return invalid("%s must hold service-account JSON", googleCredentials)
	}
	if account.Type != "service_account" || account.ClientEmail == "" || account.PrivateKey == "" {
		return invalid("%s is not service-account JSON", googleCredentials)
	}
	return nil
}

// exchangeECR is the production ECR login: GetAuthorizationToken with the
// caller's keys in the registry's region.
func exchangeECR(ctx context.Context, region, accessKey, secretKey, sessionToken string) (*Auth, error) {
	client := ecr.New(ecr.Options{
		Region:      region,
		Credentials: credentials.NewStaticCredentialsProvider(accessKey, secretKey, sessionToken),
	})
	out, err := client.GetAuthorizationToken(ctx, &ecr.GetAuthorizationTokenInput{})
	if err != nil {
		return nil, fmt.Errorf("get ECR authorization token: %w", err)
	}
	if len(out.AuthorizationData) == 0 {
		return nil, fmt.Errorf("ECR returned no authorization data")
	}
	decoded, err := base64.StdEncoding.DecodeString(aws.ToString(out.AuthorizationData[0].AuthorizationToken))
	if err != nil {
		return nil, fmt.Errorf("decode ECR token: %w", err)
	}
	user, password, ok := strings.Cut(string(decoded), ":")
	if !ok {
		return nil, fmt.Errorf("ECR token is not user:password")
	}
	return &Auth{Username: user, Password: password}, nil
}
