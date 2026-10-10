package storage_test

import (
	"cmp"
	"context"
	"encoding/xml"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
	. "github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// customerAccount is the connected AWS account of these tests.
const customerAccount = "222222222222"

// assumed is one AssumeRole call the STS stand-in answered.
type assumed struct {
	role, session, externalID, policy string
}

// connectedStorage is a storage owner whose connected account is a key of
// the test Garage, reached through an STS stand-in that hands out that key
// for the connection's role.
type connectedStorage struct {
	t       *testing.T
	pool    *pgxpool.Pool
	cfg     Config
	storage *Storage
	compute *compute.Compute
	account storagetest.Account
	// user owns the connection; connection's active authorization grants
	// role with externalID.
	user       identity.UserID
	connection uuid.UUID
	role       string
	externalID string

	// s3 stands in for AWS S3, in front of the test Garage.
	s3 *httptest.Server

	mu    sync.Mutex
	calls []assumed
	// unowned counts requests to s3 that named no expected bucket owner,
	// other than CreateBucket, which takes none; owners counts the ones
	// that did, by owner.
	unowned int
	owners  map[string]int
}

func newConnectedStorage(t *testing.T) *connectedStorage {
	t.Helper()
	c := &connectedStorage{t: t, pool: dbtest.New(t), account: storagetest.ConnectedAccount(t)}
	sts := httptest.NewServer(http.HandlerFunc(c.assumeRole))
	t.Cleanup(sts.Close)
	c.cfg = withLinks(t, func() *Storage { return c.storage })
	garage, err := url.Parse(c.cfg.Endpoint)
	if err != nil {
		t.Fatal(err)
	}
	c.owners = map[string]int{}
	c.s3 = httptest.NewServer(&httputil.ReverseProxy{Rewrite: func(r *httputil.ProxyRequest) {
		r.SetURL(garage)
		r.Out.Host = r.In.Host
		owner := cmp.Or(r.In.Header.Get("X-Amz-Expected-Bucket-Owner"), r.In.URL.Query().Get("x-amz-expected-bucket-owner"))
		c.mu.Lock()
		defer c.mu.Unlock()
		switch {
		case owner != "":
			c.owners[owner]++
		case r.In.Method != http.MethodPut || r.In.URL.RawQuery != "" || strings.Count(strings.Trim(r.In.URL.Path, "/"), "/") > 0:
			c.unowned++
		}
	}})
	t.Cleanup(c.s3.Close)
	c.compute = compute.NewCompute(c.pool, nil, compute.Config{Fleet: compute.Fleet{
		AWS:       aws.Config{Region: "us-east-2", Credentials: credentials.NewStaticCredentialsProvider("AKIAPLATFORM0000TEST", "platform-secret", "")},
		Endpoints: compute.Endpoints{STS: sts.URL, S3: c.s3.URL},
	}})
	c.storage = NewStorage(c.pool, c.cfg, c.compute)

	var user uuid.UUID
	c.scan(`insert into users (email) values ('connected-' || gen_random_uuid() || '@example.test') returning id`, &user)
	c.user = identity.UserID(user)
	c.scan(`insert into cloud_connections (account_id, aws_account_id, phase) values ($1, $2, 'ready') returning id`,
		&c.connection, user, customerAccount)
	c.role, c.externalID = "arn:aws:iam::"+customerAccount+":role/lazycloud-connection-g1", "external-"+uuid.NewString()
	if _, err := c.pool.Exec(t.Context(), `insert into cloud_authorizations
		(connection_id, generation, mode, slot, phase, role_arn, external_id, region)
		values ($1, 1, 'managed_stack', 'active', 'ready', $2, $3, $4)`, c.connection, c.role, c.externalID, c.account.Region); err != nil {
		t.Fatal(err)
	}
	return c
}

func (c *connectedStorage) scan(sql string, into *uuid.UUID, args ...any) {
	c.t.Helper()
	if err := c.pool.QueryRow(c.t.Context(), sql, args...).Scan(into); err != nil {
		c.t.Fatalf("%s: %v", sql, err)
	}
}

// workspace creates a workspace in connection, nil for the platform's.
func (c *connectedStorage) workspace(connection *uuid.UUID) identity.WorkspaceID {
	c.t.Helper()
	var id uuid.UUID
	c.scan(`insert into workspaces (name, connection_id) values ('ws-' || substr(gen_random_uuid()::text, 1, 8), $1) returning id`, &id, connection)
	dbtest.OwnWorkspaces(c.t, c.pool)
	return identity.WorkspaceID(id)
}

func (c *connectedStorage) assumeRole(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil || r.Form.Get("Action") != "AssumeRole" {
		http.Error(w, "only AssumeRole", http.StatusBadRequest)
		return
	}
	call := assumed{
		role: r.Form.Get("RoleArn"), session: r.Form.Get("RoleSessionName"),
		externalID: r.Form.Get("ExternalId"), policy: r.Form.Get("Policy"),
	}
	c.mu.Lock()
	c.calls = append(c.calls, call)
	c.mu.Unlock()
	if call.role != c.role || call.externalID != c.externalID {
		w.WriteHeader(http.StatusForbidden)
		_, _ = fmt.Fprint(w, `<ErrorResponse><Error><Type>Sender</Type><Code>AccessDenied</Code><Message>not authorized</Message></Error></ErrorResponse>`)
		return
	}
	var escaped strings.Builder
	_ = xml.EscapeText(&escaped, []byte(c.account.SecretAccessKey))
	_, _ = fmt.Fprintf(w, `<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleResult>
<AssumedRoleUser><AssumedRoleId>AROAEXAMPLE:%[3]s</AssumedRoleId><Arn>arn:aws:sts::%[4]s:assumed-role/connection/%[3]s</Arn></AssumedRoleUser>
<Credentials><AccessKeyId>%[1]s</AccessKeyId><SecretAccessKey>%[2]s</SecretAccessKey><SessionToken></SessionToken><Expiration>%[5]s</Expiration></Credentials>
</AssumeRoleResult></AssumeRoleResponse>`, c.account.AccessKeyID, escaped.String(), call.session, customerAccount, time.Now().Add(time.Hour).UTC().Format(time.RFC3339))
}

func (c *connectedStorage) assumedCalls() []assumed {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]assumed(nil), c.calls...)
}

// accountClient reaches the connected account's buckets with its key.
func (c *connectedStorage) accountClient() *s3.Client {
	return s3.New(s3.Options{
		Region: c.account.Region, BaseEndpoint: aws.String(c.cfg.Endpoint), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider(c.account.AccessKeyID, c.account.SecretAccessKey, ""),
	})
}

// bucketRow is the workspace's recorded bucket.
func (c *connectedStorage) bucketRow(ws identity.WorkspaceID) (bucket, region string, connection *uuid.UUID) {
	c.t.Helper()
	if err := c.pool.QueryRow(c.t.Context(), `select b.bucket, b.region, w.connection_id from workspace_buckets b join workspaces w on w.id = b.workspace_id where b.workspace_id = $1`,
		uuid.UUID(ws)).Scan(&bucket, &region, &connection); err != nil {
		c.t.Fatal(err)
	}
	return bucket, region, connection
}

// deleteWorkspace deletes the workspace's storage a chunk at a time, then
// the workspace, as the scheduler's deletion pass does.
func (c *connectedStorage) deleteWorkspace(ws identity.WorkspaceID) {
	c.t.Helper()
	ctx := c.t.Context()
	if _, err := c.pool.Exec(ctx, `update workspaces set state = 'deleting', deletion_requested_at = now() where id = $1`, uuid.UUID(ws)); err != nil {
		c.t.Fatal(err)
	}
	for n := 0; ; n++ {
		empty, err := c.storage.DeleteWorkspaceStorage(ctx, slog.New(slog.DiscardHandler), ws)
		if err != nil {
			c.t.Fatalf("delete workspace storage: %v", err)
		}
		if empty {
			break
		}
		if n == 5 {
			c.t.Fatal("workspace storage is still not empty")
		}
	}
	if removed, err := identity.NewIdentity(c.pool, identity.Config{}).FinishWorkspaceDeletion(ctx, ws); err != nil || !removed {
		c.t.Fatalf("remove workspace: removed %v err %v", removed, err)
	}
}

// A workspace in a connected account keeps its volumes in a bucket of that
// account, named for the account and in the connection's region, at AWS
// S3 rather than the platform's store: the connection role creates it,
// host grants assume that role with the external ID and a session policy
// for the bucket alone, and the API's uploads and download links are
// signed with the role's credentials. Every request names the account as
// the bucket's owner. The platform's own key cannot reach the bucket.
// Disconnecting is refused while the workspace lives there; deleting the
// workspace empties and deletes the bucket, after which the account can
// be disconnected.
func TestConnectedWorkspaceKeepsItsStorageInItsAccount(t *testing.T) {
	ctx := t.Context()
	c := newConnectedStorage(t)
	ws := c.workspace(&c.connection)
	host := compute.HostID(uuid.New())

	grant, err := c.storage.HostGrant(ctx, host, ws)
	if err != nil {
		t.Fatalf("host grant: %v", err)
	}
	bucket, region, connection := c.bucketRow(ws)
	if want := c.cfg.Workspaces.Prefix + "-" + customerAccount + "-"; !strings.HasPrefix(bucket, want) || len(bucket) > 63 {
		t.Fatalf("bucket %q (%d characters), want %s<workspace> within 63", bucket, len(bucket), want)
	}
	if region != c.account.Region || connection == nil || *connection != c.connection {
		t.Fatalf("bucket recorded in %s for connection %v, want %s and %s", region, connection, c.account.Region, c.connection)
	}
	if grant.Bucket != bucket || grant.Region != region || grant.AccessKeyID != c.account.AccessKeyID {
		t.Fatalf("grant %s in %s with key %s, want the connection role's for %s in %s", grant.Bucket, grant.Region, grant.AccessKeyID, bucket, region)
	}
	if grant.Endpoint != c.s3.URL || !grant.PathStyle {
		t.Fatalf("grant at %s (path-style %v), want AWS S3 at %s, not the platform's store", grant.Endpoint, grant.PathStyle, c.s3.URL)
	}
	var hostCall bool
	for _, call := range c.assumedCalls() {
		if call.role != c.role || call.externalID != c.externalID {
			t.Errorf("assumed %s with external id %q, want the connection's role and external id", call.role, call.externalID)
		}
		if call.session == "lazycloud-host-"+host.String() {
			hostCall = strings.Contains(call.policy, `"arn:aws:s3:::`+bucket+`/volumes/*"`) && !strings.Contains(call.policy, `"*"`)
		}
	}
	if !hostCall {
		t.Fatalf("no host session limited to %s among %+v", bucket, c.assumedCalls())
	}
	if _, err := storagetest.Client().ListObjectsV2(ctx, &s3.ListObjectsV2Input{Bucket: aws.String(bucket)}); err == nil {
		t.Fatal("the platform's key reached the connected account's bucket")
	}

	if _, err := c.storage.CreateVolume(ctx, ws, "data"); err != nil {
		t.Fatal(err)
	}
	put, err := c.storage.PresignVolumeFile(ctx, ws, "data", apitypes.PresignVolumeFileRequest{Path: "notes.txt", Method: apitypes.PresignVolumeFileRequestMethodPut})
	if err != nil {
		t.Fatal(err)
	}
	if status, _ := send(t, put.Url, []byte("kept in the customer's account")); status != http.StatusOK {
		t.Fatalf("presigned upload: status %d", status)
	}
	read, err := c.storage.PresignVolumeFile(ctx, ws, "data", apitypes.PresignVolumeFileRequest{Path: "notes.txt", Method: apitypes.PresignVolumeFileRequestMethodGet})
	if err != nil {
		t.Fatal(err)
	}
	if status, body, _ := get(t, read.Url); status != http.StatusOK || string(body) != "kept in the customer's account" {
		t.Fatalf("download link: status %d body %q", status, body)
	}

	var refused *compute.ConflictError
	if _, err := c.compute.Disconnect(ctx, c.user); !errors.As(err, &refused) {
		t.Fatalf("disconnect with a workspace's bucket in the account: %v, want a conflict", err)
	}
	c.deleteWorkspace(ws)
	if _, err := c.accountClient().HeadBucket(ctx, &s3.HeadBucketInput{Bucket: aws.String(bucket)}); err == nil {
		t.Fatal("the workspace's bucket outlived the workspace")
	}
	if _, err := c.compute.Disconnect(ctx, c.user); err != nil {
		t.Fatalf("disconnect after the workspace went: %v", err)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.unowned != 0 || len(c.owners) != 1 || c.owners[customerAccount] == 0 {
		t.Fatalf("AWS S3 got %d requests naming no owner and %v by owner, want all naming %s", c.unowned, c.owners, customerAccount)
	}
}

// A workspace on the platform's compute keeps its bucket in the platform's
// store, named for the platform's account, in the store's region, without
// the connection's role; deleting the workspace deletes the bucket.
func TestPlatformWorkspaceKeepsItsStorageInThePlatformAccount(t *testing.T) {
	ctx := t.Context()
	c := newConnectedStorage(t)
	ws := c.workspace(nil)
	var host uuid.UUID
	c.scan(`insert into hosts (name, state, cpu_millis, memory_bytes) values ('h', 'online', 1000, 1000) returning id`, &host)
	grant, err := c.storage.HostGrant(ctx, compute.HostID(host), ws)
	if err != nil {
		t.Fatalf("host grant: %v", err)
	}
	bucket, region, connection := c.bucketRow(ws)
	if want := c.cfg.Workspaces.Prefix + "-" + c.cfg.Workspaces.AccountID + "-"; !strings.HasPrefix(bucket, want) || len(bucket) > 63 {
		t.Fatalf("bucket %q, want %s<workspace> within 63 characters", bucket, want)
	}
	if region != c.cfg.Region || connection != nil || grant.Bucket != bucket {
		t.Fatalf("bucket %s in %s for connection %v, grant for %s; want the platform's in %s", bucket, region, connection, grant.Bucket, c.cfg.Region)
	}
	if calls := c.assumedCalls(); len(calls) != 0 {
		t.Fatalf("a platform workspace assumed %+v", calls)
	}
	hostClient := s3.New(s3.Options{
		Region: grant.Region, BaseEndpoint: aws.String(grant.Endpoint), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider(grant.AccessKeyID, grant.SecretAccessKey, grant.SessionToken),
	})
	if _, err := hostClient.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(bucket), Key: aws.String("volumes/x/file"), Body: strings.NewReader("x")}); err != nil {
		t.Fatal(err)
	}
	c.deleteWorkspace(ws)
	if _, err := storagetest.Client().HeadBucket(ctx, &s3.HeadBucketInput{Bucket: aws.String(bucket)}); err == nil {
		t.Fatal("the workspace's bucket outlived the workspace")
	}
}

// A workspace whose connected account no longer authorizes the platform
// is still deleted: its bucket, with the data in it, stays in the
// customer's account, and the account can then be disconnected.
func TestWorkspaceDeletionOutlivesItsAccountsAuthorization(t *testing.T) {
	ctx := t.Context()
	c := newConnectedStorage(t)
	ws := c.workspace(&c.connection)
	if _, err := c.storage.HostGrant(ctx, compute.HostID(uuid.New()), ws); err != nil {
		t.Fatal(err)
	}
	bucket, _, _ := c.bucketRow(ws)
	if _, err := c.accountClient().PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(bucket), Key: aws.String("volumes/x/kept"), Body: strings.NewReader("x")}); err != nil {
		t.Fatal(err)
	}
	if _, err := c.pool.Exec(ctx, `update cloud_authorizations set slot = null, phase = 'retired' where connection_id = $1`, c.connection); err != nil {
		t.Fatal(err)
	}
	// The scheduler deletes workspaces with storage of its own, which holds
	// no credentials of the connection yet.
	c.storage = NewStorage(c.pool, c.cfg, c.compute)
	c.deleteWorkspace(ws)
	if _, err := c.accountClient().HeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(bucket), Key: aws.String("volumes/x/kept")}); err != nil {
		t.Fatalf("the customer's data left their account: %v", err)
	}
	if _, err := c.compute.Disconnect(ctx, c.user); err != nil {
		t.Fatalf("disconnect after the workspace went: %v", err)
	}
}

// Servers that create a workspace's bucket at once use the bucket the
// first one recorded.
func TestAWorkspaceUsesTheBucketRecordedFirst(t *testing.T) {
	c := newConnectedStorage(t)
	ws := c.workspace(nil)
	ctx := t.Context()
	if _, err := c.pool.Exec(ctx, `insert into workspace_buckets (workspace_id, bucket, region) values ($1, 'recorded-first', 'elsewhere')`, uuid.UUID(ws)); err != nil {
		t.Fatal(err)
	}
	bucket, region, err := CreateWorkspaceBucket(ctx, c.storage, ws)
	if err != nil || bucket != "recorded-first" || region != "elsewhere" {
		t.Fatalf("created bucket %s in %s (%v), want the recorded recorded-first in elsewhere", bucket, region, err)
	}
}

// A connection without an active authorization cannot hold a workspace's
// storage, whether its bucket exists yet or not: the refusal is a typed
// conflict that fails the container needing it, not an error the host
// retries.
func TestConnectionWithoutAuthorizationRefusesStorage(t *testing.T) {
	c := newConnectedStorage(t)
	ctx := context.WithoutCancel(t.Context())
	fresh, used := c.workspace(&c.connection), c.workspace(&c.connection)
	if _, err := c.storage.HostGrant(ctx, compute.HostID(uuid.New()), used); err != nil {
		t.Fatal(err)
	}
	if _, err := c.pool.Exec(ctx, `update cloud_authorizations set slot = null, phase = 'retired' where connection_id = $1`, c.connection); err != nil {
		t.Fatal(err)
	}
	for name, ws := range map[string]identity.WorkspaceID{"new": fresh, "existing": used} {
		var refused *ConflictError
		if _, err := c.storage.HostGrant(ctx, compute.HostID(uuid.New()), ws); !errors.As(err, &refused) {
			t.Errorf("grant on the %s bucket without an authorization: %v, want a conflict", name, err)
		}
	}
}
