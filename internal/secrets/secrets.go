// Package secrets owns workspace secrets: names and values sealed at rest,
// and the values a container start resolves. A value is encrypted with its
// own data key, which a master key held outside PostgreSQL wraps.
package secrets

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// CallbackSigningKey is the secret that signs task callbacks. It is created
// on first use, and users reveal it to verify signatures and set it to
// rotate.
const CallbackSigningKey = "LAZYCLOUD_CALLBACK_SIGNING_KEY"

// reservedPrefix marks platform variables; only CallbackSigningKey uses it.
const reservedPrefix = "LAZYCLOUD_"

// NotFoundError names a secret the workspace does not have.
type NotFoundError struct{ Name string }

func (e *NotFoundError) Error() string { return "secret not found: " + e.Name }

// ExistsError rejects creating a secret that already exists.
type ExistsError struct{ Name string }

func (e *ExistsError) Error() string { return "secret already exists: " + e.Name }

// UnreadableError means a stored secret cannot be opened: its master key is
// not held here, or the row does not authenticate.
type UnreadableError struct {
	Name string
	Err  error
}

func (e *UnreadableError) Error() string {
	return fmt.Sprintf("secret %s cannot be read: %v", e.Name, e.Err)
}

func (e *UnreadableError) Unwrap() error { return e.Err }

// ReservedNameError rejects a name in the platform's namespace.
type ReservedNameError struct{ Name string }

func (e *ReservedNameError) Error() string {
	return fmt.Sprintf("secret name %s is reserved: names starting with %s belong to the platform", e.Name, reservedPrefix)
}

// ErrInvalidCursor means a list cursor was not produced by List.
var ErrInvalidCursor = errors.New("invalid cursor")

// Secret is a secret's metadata; values leave the owner only through Reveal
// and Resolve.
type Secret struct {
	Name      string
	CreatedAt time.Time
	UpdatedAt time.Time
	// UsedBy are the workloads whose active release receives the secret.
	UsedBy []Use
}

// Use is a workload that receives a secret.
type Use struct {
	App      string
	Kind     string
	Workload string
}

// withUsers fills in the workloads that receive each secret.
func (s *Secrets) withUsers(ctx context.Context, workspace identity.WorkspaceID, secrets []Secret) ([]Secret, error) {
	if len(secrets) == 0 {
		return secrets, nil
	}
	names := make([]string, len(secrets))
	index := make(map[string]int, len(secrets))
	for n, secret := range secrets {
		names[n] = secret.Name
		index[secret.Name] = n
		secrets[n].UsedBy = []Use{}
	}
	users, err := s.queries.SecretUsers(ctx, SecretUsersParams{WorkspaceID: uuid.UUID(workspace), Names: names})
	if err != nil {
		return nil, fmt.Errorf("read secret users: %w", err)
	}
	for _, u := range users {
		secret := &secrets[index[u.Secret]]
		secret.UsedBy = append(secret.UsedBy, Use{App: u.App, Kind: u.Kind, Workload: u.Workload})
	}
	return secrets, nil
}

func (s *Secrets) oneWithUsers(ctx context.Context, workspace identity.WorkspaceID, secret Secret) (Secret, error) {
	out, err := s.withUsers(ctx, workspace, []Secret{secret})
	if err != nil {
		return Secret{}, err
	}
	return out[0], nil
}

// Secrets is the secrets owner.
type Secrets struct {
	pool    *pgxpool.Pool
	queries *Queries
	keys    KeyWrapper
}

// NewSecrets returns the secrets owner; keys wraps every data key.
func NewSecrets(pool *pgxpool.Pool, keys KeyWrapper) *Secrets {
	return &Secrets{pool: pool, queries: New(pool), keys: keys}
}

type sealed struct {
	keyID      string
	wrappedKey []byte
	nonce      []byte
	ciphertext []byte
}

// additionalData binds a ciphertext to its workspace and name, so a row
// copied to another name or workspace fails to open.
func additionalData(workspace identity.WorkspaceID, name string) []byte {
	return []byte("lazycloud-secret:v1\x00" + workspace.String() + "\x00" + name)
}

func (s *Secrets) seal(ctx context.Context, workspace identity.WorkspaceID, name, value string) (sealed, error) {
	dataKey := make([]byte, masterKeyBytes)
	if _, err := rand.Read(dataKey); err != nil {
		return sealed{}, fmt.Errorf("generate data key: %w", err)
	}
	aead, err := newAEAD(dataKey)
	if err != nil {
		return sealed{}, err
	}
	box, err := seal(aead, []byte(value), additionalData(workspace, name))
	if err != nil {
		return sealed{}, err
	}
	wrapped, err := s.keys.Wrap(ctx, dataKey)
	if err != nil {
		return sealed{}, fmt.Errorf("wrap data key: %w", err)
	}
	return sealed{
		keyID: s.keys.KeyID(), wrappedKey: wrapped,
		nonce: box[:aead.NonceSize()], ciphertext: box[aead.NonceSize():],
	}, nil
}

func (s *Secrets) open(ctx context.Context, workspace identity.WorkspaceID, row SealedSecretsRow) (string, error) {
	dataKey, err := s.keys.Unwrap(ctx, row.KeyID, row.WrappedKey)
	if err != nil {
		return "", &UnreadableError{Name: row.Name, Err: fmt.Errorf("unwrap data key: %w", err)}
	}
	aead, err := newAEAD(dataKey)
	if err != nil {
		return "", &UnreadableError{Name: row.Name, Err: err}
	}
	value, err := open(aead, append(append([]byte(nil), row.Nonce...), row.Ciphertext...), additionalData(workspace, row.Name))
	if err != nil {
		return "", &UnreadableError{Name: row.Name, Err: err}
	}
	return string(value), nil
}

func checkName(name string) error {
	if strings.HasPrefix(name, reservedPrefix) && name != CallbackSigningKey {
		return &ReservedNameError{Name: name}
	}
	return nil
}

func secretOf(name string, created, updated time.Time) Secret {
	return Secret{Name: name, CreatedAt: created, UpdatedAt: updated}
}

// Create stores a new secret. An existing name returns *ExistsError.
func (s *Secrets) Create(ctx context.Context, workspace identity.WorkspaceID, name, value string) (Secret, error) {
	if err := checkName(name); err != nil {
		return Secret{}, err
	}
	box, err := s.seal(ctx, workspace, name, value)
	if err != nil {
		return Secret{}, err
	}
	row, err := s.queries.InsertSecret(ctx, InsertSecretParams{
		WorkspaceID: uuid.UUID(workspace), Name: name,
		KeyID: box.keyID, WrappedKey: box.wrappedKey, Nonce: box.nonce, Ciphertext: box.ciphertext,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return Secret{}, &ExistsError{Name: name}
	}
	if err != nil {
		return Secret{}, fmt.Errorf("insert secret: %w", err)
	}
	return s.oneWithUsers(ctx, workspace, secretOf(row.Name, row.CreatedAt, row.UpdatedAt))
}

// Set creates the secret or replaces its value.
func (s *Secrets) Set(ctx context.Context, workspace identity.WorkspaceID, name, value string) (Secret, error) {
	if err := checkName(name); err != nil {
		return Secret{}, err
	}
	box, err := s.seal(ctx, workspace, name, value)
	if err != nil {
		return Secret{}, err
	}
	row, err := s.queries.UpsertSecret(ctx, UpsertSecretParams{
		WorkspaceID: uuid.UUID(workspace), Name: name,
		KeyID: box.keyID, WrappedKey: box.wrappedKey, Nonce: box.nonce, Ciphertext: box.ciphertext,
	})
	if err != nil {
		return Secret{}, fmt.Errorf("upsert secret: %w", err)
	}
	return s.oneWithUsers(ctx, workspace, secretOf(row.Name, row.CreatedAt, row.UpdatedAt))
}

// Update replaces an existing secret's value. A missing name returns
// *NotFoundError.
func (s *Secrets) Update(ctx context.Context, workspace identity.WorkspaceID, name, value string) (Secret, error) {
	box, err := s.seal(ctx, workspace, name, value)
	if err != nil {
		return Secret{}, err
	}
	row, err := s.queries.UpdateSecret(ctx, UpdateSecretParams{
		WorkspaceID: uuid.UUID(workspace), Name: name,
		KeyID: box.keyID, WrappedKey: box.wrappedKey, Nonce: box.nonce, Ciphertext: box.ciphertext,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return Secret{}, &NotFoundError{Name: name}
	}
	if err != nil {
		return Secret{}, fmt.Errorf("update secret: %w", err)
	}
	return s.oneWithUsers(ctx, workspace, secretOf(row.Name, row.CreatedAt, row.UpdatedAt))
}

// Get returns a secret's metadata.
func (s *Secrets) Get(ctx context.Context, workspace identity.WorkspaceID, name string) (Secret, error) {
	row, err := s.queries.SecretMetadata(ctx, SecretMetadataParams{WorkspaceID: uuid.UUID(workspace), Name: name})
	if errors.Is(err, pgx.ErrNoRows) {
		return Secret{}, &NotFoundError{Name: name}
	}
	if err != nil {
		return Secret{}, fmt.Errorf("read secret: %w", err)
	}
	return s.oneWithUsers(ctx, workspace, secretOf(row.Name, row.CreatedAt, row.UpdatedAt))
}

// Reveal returns a secret and its value. Callers authorize it separately
// from reading metadata.
func (s *Secrets) Reveal(ctx context.Context, workspace identity.WorkspaceID, name string) (Secret, string, error) {
	rows, err := s.queries.SealedSecrets(ctx, SealedSecretsParams{WorkspaceID: uuid.UUID(workspace), Names: []string{name}})
	if err != nil {
		return Secret{}, "", fmt.Errorf("read secret: %w", err)
	}
	if len(rows) == 0 {
		return Secret{}, "", &NotFoundError{Name: name}
	}
	value, err := s.open(ctx, workspace, rows[0])
	if err != nil {
		return Secret{}, "", err
	}
	return secretOf(rows[0].Name, rows[0].CreatedAt, rows[0].UpdatedAt), value, nil
}

// Delete removes a secret. A missing name returns *NotFoundError.
func (s *Secrets) Delete(ctx context.Context, workspace identity.WorkspaceID, name string) error {
	n, err := s.queries.DeleteSecret(ctx, DeleteSecretParams{WorkspaceID: uuid.UUID(workspace), Name: name})
	if err != nil {
		return fmt.Errorf("delete secret: %w", err)
	}
	if n == 0 {
		return &NotFoundError{Name: name}
	}
	return nil
}

// List returns up to limit secrets in name order after cursor, and the
// cursor of the next page when more follow.
func (s *Secrets) List(ctx context.Context, workspace identity.WorkspaceID, cursor string, limit int) ([]Secret, string, error) {
	after := ""
	if cursor != "" {
		decoded, err := base64.RawURLEncoding.DecodeString(cursor)
		if err != nil {
			return nil, "", ErrInvalidCursor
		}
		after = string(decoded)
	}
	rows, err := s.queries.ListSecrets(ctx, ListSecretsParams{
		WorkspaceID: uuid.UUID(workspace), After: after, MaxRows: int32(limit) + 1, //nolint:gosec // The API caps limit at 1000.
	})
	if err != nil {
		return nil, "", fmt.Errorf("list secrets: %w", err)
	}
	next := ""
	if len(rows) > limit {
		rows = rows[:limit]
		next = base64.RawURLEncoding.EncodeToString([]byte(rows[limit-1].Name))
	}
	out := make([]Secret, len(rows))
	for n, row := range rows {
		out[n] = secretOf(row.Name, row.CreatedAt, row.UpdatedAt)
	}
	out, err = s.withUsers(ctx, workspace, out)
	if err != nil {
		return nil, "", err
	}
	return out, next, nil
}

// Resolve returns the values of names in workspace for a container start.
// The first name the workspace lacks returns *NotFoundError.
func (s *Secrets) Resolve(ctx context.Context, workspace identity.WorkspaceID, names []string) (map[string]string, error) {
	if len(names) == 0 {
		return map[string]string{}, nil
	}
	rows, err := s.queries.SealedSecrets(ctx, SealedSecretsParams{WorkspaceID: uuid.UUID(workspace), Names: names})
	if err != nil {
		return nil, fmt.Errorf("read secrets: %w", err)
	}
	values := make(map[string]string, len(rows))
	for _, row := range rows {
		value, err := s.open(ctx, workspace, row)
		if err != nil {
			return nil, err
		}
		values[row.Name] = value
	}
	for _, name := range names {
		if _, ok := values[name]; !ok {
			return nil, &NotFoundError{Name: name}
		}
	}
	return values, nil
}

// SigningKey returns the workspace's callback signing key, creating a random
// one the first time.
func (s *Secrets) SigningKey(ctx context.Context, workspace identity.WorkspaceID) (string, error) {
	values, err := s.Resolve(ctx, workspace, []string{CallbackSigningKey})
	var missing *NotFoundError
	if err == nil {
		return values[CallbackSigningKey], nil
	}
	if !errors.As(err, &missing) {
		return "", err
	}
	random := make([]byte, 32)
	if _, err := rand.Read(random); err != nil {
		return "", fmt.Errorf("generate signing key: %w", err)
	}
	key := "whsec_" + base64.RawURLEncoding.EncodeToString(random)
	if _, err := s.Create(ctx, workspace, CallbackSigningKey, key); err != nil {
		var exists *ExistsError
		if !errors.As(err, &exists) {
			return "", err
		}
		// A concurrent delivery created it first.
		values, err := s.Resolve(ctx, workspace, []string{CallbackSigningKey})
		if err != nil {
			return "", err
		}
		return values[CallbackSigningKey], nil
	}
	return key, nil
}
