package images

import (
	"context"
	"errors"
	"testing"
)

// The documented credential names per registry, as users set them.
func TestRegistryCredentialNames(t *testing.T) {
	account := `{"type":"service_account","client_email":"a@b.iam","private_key":"k"}`
	var ecrRegion string
	exchange := func(_ context.Context, region, key, secret, _ string) (*Auth, error) {
		ecrRegion = region
		if key != "AKIA" || secret != "s" {
			return nil, errors.New("bad keys")
		}
		return &Auth{Username: "AWS", Password: "ecr-token"}, nil
	}
	cases := []struct {
		name  string
		host  string
		creds map[string]string
		want  *Auth
		fails bool
	}{
		{"ghcr pair", "ghcr.io", map[string]string{"GITHUB_USERNAME": "u", "GITHUB_TOKEN": "t"}, &Auth{Username: "u", Password: "t"}, false},
		{"ghcr token", "ghcr.io", map[string]string{"GITHUB_TOKEN": "t"}, &Auth{Username: "x-access-token", Password: "t"}, false},
		{"ecr", "123456789012.dkr.ecr.us-east-2.amazonaws.com", map[string]string{"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "s"}, &Auth{Username: "AWS", Password: "ecr-token"}, false},
		{"ecr without secret", "123456789012.dkr.ecr.us-east-2.amazonaws.com", map[string]string{"AWS_ACCESS_KEY_ID": "AKIA"}, nil, true},
		{"gcr token", "gcr.io", map[string]string{"GCP_ACCESS_TOKEN": "ya29"}, &Auth{Username: "oauth2accesstoken", Password: "ya29"}, false},
		{"artifact registry account", "us-docker.pkg.dev", map[string]string{"GOOGLE_APPLICATION_CREDENTIALS": account}, &Auth{Username: "_json_key", Password: account}, false},
		{"gcr bad account", "gcr.io", map[string]string{"GOOGLE_APPLICATION_CREDENTIALS": "{}"}, nil, true},
		{"acr", "acme.azurecr.io", map[string]string{"AZURE_CLIENT_ID": "id", "AZURE_CLIENT_SECRET": "sec", "AZURE_TENANT_ID": "t"}, &Auth{Username: "id", Password: "sec"}, false},
		{"ngc", "nvcr.io", map[string]string{"NGC_API_KEY": "k"}, &Auth{Username: "$oauthtoken", Password: "k"}, false},
		{"docker hub token", "docker.io", map[string]string{"DOCKERHUB_TOKEN": "dckr"}, &Auth{IdentityToken: "dckr"}, false},
		{"registry pair", "registry.example.com", map[string]string{"REGISTRY_USERNAME": "u", "REGISTRY_PASSWORD": "p"}, &Auth{Username: "u", Password: "p"}, false},
		{"half a pair", "registry.example.com", map[string]string{"REGISTRY_USERNAME": "u"}, nil, true},
		{"other names are ignored", "docker.io", map[string]string{"MY_TOKEN": "x"}, nil, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := registryAuth(t.Context(), c.host, c.creds, exchange)
			var invalidErr *InvalidError
			if c.fails {
				if !errors.As(err, &invalidErr) {
					t.Fatalf("want an invalid definition, got %v %v", got, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if (got == nil) != (c.want == nil) || (got != nil && *got != *c.want) {
				t.Fatalf("got %+v, want %+v", got, c.want)
			}
		})
	}
	if ecrRegion != "us-east-2" {
		t.Fatalf("ECR logins use the registry's region, got %q", ecrRegion)
	}
}
