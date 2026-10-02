package execution

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/ssh"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// SSH reaches pods through a server in their supervisor that trusts the
// workspace's user certificate authority. The authority and every pod's
// host key are ed25519 keys created on first use and stored sealed, each in
// its own row, so either rotates by replacing its row.

const (
	// SSHPrincipal is the account every session logs in as.
	SSHPrincipal = "root"
	// SSHCertificateTTL is how long a signed certificate lasts.
	SSHCertificateTTL = 12 * time.Hour
	// certificateBackdate tolerates clock skew between the CLI's host and
	// the server.
	certificateBackdate = 5 * time.Minute
)

// KeySealer encrypts keys this owner stores; the secrets owner implements
// it.
type KeySealer interface {
	Seal(ctx context.Context, binding string, value []byte) ([]byte, error)
	Open(ctx context.Context, binding string, blob []byte) ([]byte, error)
}

// SSHKeys holds the workspace authorities and pod host keys.
type SSHKeys struct {
	queries *Queries
	sealer  KeySealer
}

// NewSSHKeys returns the SSH key store over pool.
func NewSSHKeys(pool *pgxpool.Pool, sealer KeySealer) *SSHKeys {
	return &SSHKeys{queries: New(pool), sealer: sealer}
}

// keyRow is a stored key as either table holds it.
type keyRow struct {
	public string
	sealed []byte
}

// key reads a key, or creates, seals and inserts one; a concurrent creator
// wins and its key is read back.
func (k *SSHKeys) key(ctx context.Context, binding, comment string, read func() (keyRow, error), insert func(keyRow) error) (ssh.Signer, string, []byte, error) {
	row, err := read()
	if errors.Is(err, pgx.ErrNoRows) {
		_, private, genErr := ed25519.GenerateKey(rand.Reader)
		if genErr != nil {
			return nil, "", nil, fmt.Errorf("generate key: %w", genErr)
		}
		block, marshalErr := ssh.MarshalPrivateKey(private, comment)
		if marshalErr != nil {
			return nil, "", nil, fmt.Errorf("encode key: %w", marshalErr)
		}
		signer, signerErr := ssh.NewSignerFromKey(private)
		if signerErr != nil {
			return nil, "", nil, fmt.Errorf("load key: %w", signerErr)
		}
		sealed, sealErr := k.sealer.Seal(ctx, binding, pem.EncodeToMemory(block))
		if sealErr != nil {
			return nil, "", nil, fmt.Errorf("seal key: %w", sealErr)
		}
		public := strings.TrimSpace(string(ssh.MarshalAuthorizedKey(signer.PublicKey()))) + " " + comment
		if err := insert(keyRow{public: public, sealed: sealed}); err != nil {
			return nil, "", nil, fmt.Errorf("store key: %w", err)
		}
		row, err = read()
	}
	if err != nil {
		return nil, "", nil, fmt.Errorf("read key: %w", err)
	}
	private, err := k.sealer.Open(ctx, binding, row.sealed)
	if err != nil {
		return nil, "", nil, fmt.Errorf("open key: %w", err)
	}
	signer, err := ssh.ParsePrivateKey(private)
	if err != nil {
		return nil, "", nil, fmt.Errorf("parse key: %w", err)
	}
	return signer, row.public, private, nil
}

// Authority returns the workspace's user certificate authority and its
// authorized_keys line.
func (k *SSHKeys) Authority(ctx context.Context, workspace identity.WorkspaceID) (ssh.Signer, string, error) {
	ws := uuid.UUID(workspace)
	signer, public, _, err := k.key(ctx, "ssh-authority:"+ws.String(), "lazycloud-user-ca",
		func() (keyRow, error) {
			r, err := k.queries.SSHAuthority(ctx, ws)
			return keyRow{public: r.PublicKey, sealed: r.SealedKey}, err //nolint:wrapcheck // key wraps it.
		},
		func(r keyRow) error {
			return k.queries.InsertSSHAuthority(ctx, InsertSSHAuthorityParams{WorkspaceID: ws, PublicKey: r.public, SealedKey: r.sealed}) //nolint:wrapcheck // key wraps it.
		})
	if err != nil {
		return nil, "", fmt.Errorf("workspace SSH authority: %w", err)
	}
	return signer, public, nil
}

// HostKey returns a pod's host key as OpenSSH PEM and its public line.
func (k *SSHKeys) HostKey(ctx context.Context, workload uuid.UUID) ([]byte, string, error) {
	_, public, private, err := k.key(ctx, "ssh-host-key:"+workload.String(), "lazycloud-host",
		func() (keyRow, error) {
			r, err := k.queries.SSHHostKey(ctx, workload)
			return keyRow{public: r.PublicKey, sealed: r.SealedKey}, err //nolint:wrapcheck // key wraps it.
		},
		func(r keyRow) error {
			return k.queries.InsertSSHHostKey(ctx, InsertSSHHostKeyParams{WorkloadID: workload, PublicKey: r.public, SealedKey: r.sealed}) //nolint:wrapcheck // key wraps it.
		})
	if err != nil {
		return nil, "", fmt.Errorf("pod host key: %w", err)
	}
	return private, public, nil
}

// Certificate is a signed user certificate.
type Certificate struct {
	Line      string
	ExpiresAt time.Time
}

// Sign certifies an ed25519 public key for SSHCertificateTTL of root logins
// to the workspace's pods. holder names who asked, in the key id.
func (k *SSHKeys) Sign(ctx context.Context, workspace identity.WorkspaceID, holder, publicKey string) (Certificate, error) {
	key, _, _, _, err := ssh.ParseAuthorizedKey([]byte(publicKey))
	if err != nil || key.Type() != ssh.KeyAlgoED25519 {
		return Certificate{}, &InvalidError{Reason: "public_key must be an ssh-ed25519 public key line"}
	}
	authority, _, err := k.Authority(ctx, workspace)
	if err != nil {
		return Certificate{}, err
	}
	var serial [8]byte
	if _, err := rand.Read(serial[:]); err != nil {
		return Certificate{}, fmt.Errorf("generate serial: %w", err)
	}
	now := time.Now()
	expires := now.Add(SSHCertificateTTL).Truncate(time.Second)
	cert := &ssh.Certificate{
		Key:             key,
		Serial:          binary.BigEndian.Uint64(serial[:]),
		CertType:        ssh.UserCert,
		KeyId:           fmt.Sprintf("workspace=%s holder=%s", uuid.UUID(workspace), holder),
		ValidPrincipals: []string{SSHPrincipal},
		ValidAfter:      uint64(now.Add(-certificateBackdate).Unix()), //nolint:gosec // Times are after 1970.
		ValidBefore:     uint64(expires.Unix()),                       //nolint:gosec // Times are after 1970.
		Permissions: ssh.Permissions{Extensions: map[string]string{
			"permit-port-forwarding": "", "permit-pty": "",
		}},
	}
	if err := cert.SignCert(rand.Reader, authority); err != nil {
		return Certificate{}, fmt.Errorf("sign certificate: %w", err)
	}
	return Certificate{Line: strings.TrimSpace(string(ssh.MarshalAuthorizedKey(cert))), ExpiresAt: expires}, nil
}

// SSHHost is a pod that serves SSH.
type SSHHost struct {
	App, Pod  string
	Role      apitypes.PodRole
	Workload  uuid.UUID
	Alias     string
	PublicKey string
}

// SSHHostFilter narrows a host listing.
type SSHHostFilter struct {
	App, Pod *string
	Role     *apitypes.PodRole
}

// Hosts lists the workspace's active pods that serve SSH by app and name,
// after the cursor "<app>/<pod>". Pods without a host key get one.
func (k *SSHKeys) Hosts(ctx context.Context, workspace identity.Workspace, filter SSHHostFilter, cursor string, limit int) ([]SSHHost, string, error) {
	afterApp, afterPod, _ := strings.Cut(cursor, "/")
	var role *string
	if filter.Role != nil {
		r := string(*filter.Role)
		role = &r
	}
	size := pageSize(limit)
	rows, err := k.queries.SSHHosts(ctx, SSHHostsParams{
		WorkspaceID: uuid.UUID(workspace.ID), App: filter.App, Pod: filter.Pod, Role: role,
		AfterApp: afterApp, AfterPod: afterPod, MaxRows: size + 1,
	})
	if err != nil {
		return nil, "", fmt.Errorf("list SSH hosts: %w", err)
	}
	next := ""
	if len(rows) > int(size) {
		rows = rows[:size]
		next = rows[len(rows)-1].AppName + "/" + rows[len(rows)-1].PodName
	}
	out := make([]SSHHost, len(rows))
	for n, row := range rows {
		public := ""
		if row.PublicKey != nil {
			public = *row.PublicKey
		} else if _, public, err = k.HostKey(ctx, row.WorkloadID); err != nil {
			return nil, "", err
		}
		r := apitypes.PodRolePod
		if row.PodKind == string(apitypes.PodKindDevbox) {
			r = apitypes.PodRoleDevbox
		}
		out[n] = SSHHost{
			App: row.AppName, Pod: row.PodName, Role: r, Workload: row.WorkloadID,
			Alias: SSHAlias(workspace.Name, row.AppName, row.PodName), PublicKey: public,
		}
	}
	return out, next, nil
}

var unsafeLabel = regexp.MustCompile(`[^a-z0-9-]+`)

// SSHAlias is the host name a pod answers to in SSH config:
// lazycloud-<workspace>-<app>-<pod>, each lowercased with other characters
// as -.
func SSHAlias(workspace, app, pod string) string {
	label := func(v string) string {
		return strings.Trim(unsafeLabel.ReplaceAllString(strings.ToLower(strings.TrimSpace(v)), "-"), "-")
	}
	return "lazycloud-" + label(workspace) + "-" + label(app) + "-" + label(pod)
}

// SSHPod is a pod a tunnel connects to.
type SSHPod struct {
	Workload uuid.UUID
	Spec     apitypes.WorkloadSpec
}

// ErrPodStopped and ErrPodWithoutSSH refuse a tunnel to a pod that cannot
// take one.
var (
	ErrPodStopped    = errors.New("the pod is stopped; deploy it again to connect")
	ErrPodWithoutSSH = errors.New("the pod does not serve SSH; deploy it with ssh=True")
)

// PodForSSH resolves the pod a tunnel connects to.
func (e *Execution) PodForSSH(ctx context.Context, workspace identity.WorkspaceID, app, name string) (SSHPod, error) {
	row, err := e.queries.PodByName(ctx, PodByNameParams{WorkspaceID: uuid.UUID(workspace), App: app, Name: name})
	if errors.Is(err, pgx.ErrNoRows) {
		return SSHPod{}, ErrNotFound
	}
	if err != nil {
		return SSHPod{}, fmt.Errorf("read pod: %w", err)
	}
	if row.DesiredState != "active" || row.AppState != "active" || row.Spec == nil {
		return SSHPod{}, ErrPodStopped
	}
	out := SSHPod{Workload: row.ID}
	if err := json.Unmarshal(row.Spec, &out.Spec); err != nil {
		return SSHPod{}, fmt.Errorf("decode release spec: %w", err)
	}
	if !ServesSSH(out.Spec) {
		return SSHPod{}, ErrPodWithoutSSH
	}
	return out, nil
}

// ServesSSH reports whether a release runs the SSH server.
func ServesSSH(spec apitypes.WorkloadSpec) bool {
	p := spec.Pod
	return p != nil && (p.Kind == apitypes.PodKindDevbox || (p.Ssh != nil && *p.Ssh))
}
