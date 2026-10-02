package images

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
)

type policyStatement struct {
	Effect   string
	Action   []string
	Resource []string
}

// A host login comes from a session of the host role whose policy grants
// exactly the command's repositories: pulls where it reads, pushes only
// where it writes. Logins are reused per access while they outlast the
// command, and never past the session.
func TestHostLoginsAreScopedToTheCommandsRepositories(t *testing.T) {
	var mu sync.Mutex
	var policies []string
	sessionEnds := time.Now().Add(time.Hour).UTC()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Amz-Target") == "AmazonEC2ContainerRegistry_V20150921.GetAuthorizationToken" {
			token := base64.StdEncoding.EncodeToString([]byte("AWS:host-token"))
			w.Header().Set("Content-Type", "application/x-amz-json-1.1")
			_, _ = fmt.Fprintf(w, `{"authorizationData":[{"authorizationToken":%q,"expiresAt":%d}]}`, token, time.Now().Add(12*time.Hour).Unix())
			return
		}
		if err := r.ParseForm(); err != nil || r.Form.Get("Action") != "AssumeRole" ||
			r.Form.Get("RoleArn") != "arn:aws:iam::123456789012:role/registry-hosts" {
			http.Error(w, "unexpected call", http.StatusBadRequest)
			return
		}
		mu.Lock()
		policies = append(policies, r.Form.Get("Policy"))
		mu.Unlock()
		w.Header().Set("Content-Type", "text/xml")
		_, _ = fmt.Fprintf(w, `<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleResult><Credentials>
<AccessKeyId>ASIAHOST</AccessKeyId><SecretAccessKey>secret</SecretAccessKey><SessionToken>token</SessionToken>
<Expiration>%s</Expiration></Credentials></AssumeRoleResult></AssumeRoleResponse>`, sessionEnds.Format(time.RFC3339))
	}))
	defer server.Close()
	login := newPlatformLogin(Config{
		Registry: "123456789012.dkr.ecr.us-east-2.amazonaws.com", Repository: "lazycloud/workload-images",
		HostRole: "arn:aws:iam::123456789012:role/registry-hosts",
		ECR: &aws.Config{
			BaseEndpoint: aws.String(server.URL),
			Credentials:  credentials.NewStaticCredentialsProvider("AKIA", "secret", ""),
			Retryer:      func() aws.Retryer { return aws.NopRetryer{} },
		},
	})
	const arn = "arn:aws:ecr:us-east-2:123456789012:repository/"
	image := "lazycloud/workload-images/images/" + "ab12"
	cache := "lazycloud/workload-images/cache/ws-a"

	statements := func(n int) map[string][]string {
		t.Helper()
		mu.Lock()
		defer mu.Unlock()
		if len(policies) != n {
			t.Fatalf("%d sessions, want %d", len(policies), n)
		}
		var doc struct{ Statement []policyStatement }
		if err := json.Unmarshal([]byte(policies[n-1]), &doc); err != nil {
			t.Fatal(err)
		}
		// Resources by action.
		grants := map[string][]string{}
		for _, s := range doc.Statement {
			if s.Effect != "Allow" {
				t.Fatalf("statement %+v is not an allow", s)
			}
			for _, a := range s.Action {
				grants[a] = append(grants[a], s.Resource...)
			}
		}
		for _, r := range grants {
			slices.Sort(r)
		}
		return grants
	}

	until := time.Now().Add(pullWindow)
	auth, err := login.host(t.Context(), hostAccess{pull: []string{image}}, until)
	if err != nil || auth.Password != "host-token" {
		t.Fatalf("pull login %+v %v", auth, err)
	}
	pull := statements(1)
	for _, action := range []string{"ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"} {
		if !slices.Equal(pull[action], []string{arn + image}) {
			t.Errorf("pull session grants %s on %v, want the image's repository only", action, pull[action])
		}
	}
	for _, action := range []string{"ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:CreateRepository"} {
		if len(pull[action]) != 0 {
			t.Errorf("a pull session grants %s on %v", action, pull[action])
		}
	}
	if !slices.Equal(pull["ecr:GetAuthorizationToken"], []string{"*"}) || len(pull) != 4 {
		t.Errorf("pull session grants %v, want the token and the three pull actions", pull)
	}

	if _, err := login.host(t.Context(), hostAccess{pull: []string{image}}, until); err != nil {
		t.Fatal(err)
	}
	statements(1)
	if _, err := login.host(t.Context(), hostAccess{pull: []string{image}}, sessionEnds.Add(time.Minute)); err != nil {
		t.Fatal(err)
	}
	statements(2)

	if _, err := login.host(t.Context(), hostAccess{push: []string{image, cache}}, until); err != nil {
		t.Fatal(err)
	}
	build := statements(3)
	want := []string{arn + cache, arn + image}
	for _, action := range []string{"ecr:BatchGetImage", "ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:CreateRepository"} {
		if !slices.Equal(build[action], want) {
			t.Errorf("build session grants %s on %v, want its image and cache repositories", action, build[action])
		}
	}
}

func TestRepositoryOfAReference(t *testing.T) {
	c := Config{Registry: "123456789012.dkr.ecr.us-east-2.amazonaws.com", Repository: "lazycloud/workload-images"}
	for reference, want := range map[string]string{
		c.Registry + "/lazycloud/workload-images/images/ab@sha256:00":     "lazycloud/workload-images/images/ab",
		c.Registry + "/lazycloud/workload-images/cache/ws:tag":            "lazycloud/workload-images/cache/ws",
		c.Registry + "/lazycloud/workload-images/filesystems/ws@sha256:0": "lazycloud/workload-images/filesystems/ws",
	} {
		if got, ok := c.repositoryOf(reference); !ok || got != want {
			t.Errorf("repositoryOf(%s) = %s %v, want %s", reference, got, ok, want)
		}
	}
	for _, outside := range []string{c.Registry + "/lazycloud/release/server:1", c.Registry + "/lazycloud/workload-imagesx/a@sha256:0", "docker.io/library/python:3.12"} {
		if _, ok := c.repositoryOf(outside); ok {
			t.Errorf("repositoryOf(%s) is inside the workload repositories", outside)
		}
	}
}
