package edge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// Provider terminates TLS for customer hostnames. The edge decides when a
// hostname should exist; the provider reports whether it serves.
type Provider interface {
	CreateHostname(ctx context.Context, hostname string) (ProviderHostname, error)
	// GetHostname reports (ProviderHostname{}, false, nil) when the provider
	// no longer holds the hostname.
	GetHostname(ctx context.Context, id string) (ProviderHostname, bool, error)
	// DeleteHostname succeeds when the provider already forgot it.
	DeleteHostname(ctx context.Context, id string) error
}

// ProviderHostname is what a provider reports about one hostname.
type ProviderHostname struct {
	ID    string
	Phase apitypes.DomainPhase
	// Records are what the provider still waits for beyond the routing
	// CNAME.
	Records      []apitypes.DnsRecord
	ErrorCode    *apitypes.DomainErrorCode
	ErrorMessage *string
}

var (
	// ErrDomainsUnavailable means no provider is configured.
	ErrDomainsUnavailable = errors.New("custom domains require LAZYCLOUD_CLOUDFLARE_API_TOKEN and LAZYCLOUD_CLOUDFLARE_ZONE_ID")
	// ErrDomainNotFound means the caller registered no such hostname.
	ErrDomainNotFound = errors.New("domain is not registered")
)

// InvalidDomainError rejects a hostname that cannot be registered.
type InvalidDomainError struct{ Reason string }

func (e *InvalidDomainError) Error() string { return e.Reason }

// DomainConflictError means another account holds the hostname, or a
// deployment still serves it.
type DomainConflictError struct{ Reason string }

func (e *DomainConflictError) Error() string { return e.Reason }

// ProviderError is a failure the provider reported. Rejected means it
// refused the caller's input; otherwise it was unreachable.
type ProviderError struct {
	Rejected bool
	Message  string
}

func (e *ProviderError) Error() string { return e.Message }

var exactHostname = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$`)

// recheckInterval paces background re-reads of registrations the provider
// is still working on.
const recheckInterval = 5 * time.Minute

func normalizeHostname(value string) (string, error) {
	hostname := strings.TrimSuffix(strings.ToLower(strings.TrimSpace(value)), ".")
	if len(hostname) > 253 || !exactHostname.MatchString(hostname) {
		return "", &InvalidDomainError{Reason: "domain must be an exact hostname such as app.acme.com"}
	}
	return hostname, nil
}

func (e *Edge) provider() (Provider, error) {
	if e.domains == nil {
		return nil, ErrDomainsUnavailable
	}
	return e.domains, nil
}

// RegisterDomain registers a hostname for the user's workspaces. A hostname
// the user already registered is returned as it is.
func (e *Edge) RegisterDomain(ctx context.Context, user identity.UserID, value string) (apitypes.Domain, error) {
	hostname, err := normalizeHostname(value)
	if err != nil {
		return apitypes.Domain{}, err
	}
	if base := e.urls.Base(); hostname == base || strings.HasSuffix(hostname, "."+base) {
		return apitypes.Domain{}, &InvalidDomainError{Reason: base + " is this platform's own domain; every deployment already has a hostname under it"}
	}
	existing, err := e.queries.DomainByHostname(ctx, DomainByHostnameParams{UserID: uuid.UUID(user), Hostname: hostname})
	if err == nil {
		return e.domainOut(existing)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return apitypes.Domain{}, fmt.Errorf("read domain: %w", err)
	}
	provider, err := e.provider()
	if err != nil {
		return apitypes.Domain{}, err
	}
	// The provider call is a network round trip and holds no lock.
	state, err := provider.CreateHostname(ctx, hostname)
	if err != nil {
		return apitypes.Domain{}, err
	}
	records, err := json.Marshal(nonNil(state.Records))
	if err != nil {
		return apitypes.Domain{}, fmt.Errorf("encode records: %w", err)
	}
	row, err := e.queries.InsertDomain(ctx, InsertDomainParams{
		UserID: uuid.UUID(user), Hostname: hostname, Phase: string(state.Phase), ProviderHostnameID: &state.ID,
		RequiredRecords: records, ErrorCode: (*string)(state.ErrorCode), ErrorMessage: state.ErrorMessage,
	})
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		// Another account won the name; the provider hostname just made is
		// this call's to clean up.
		if derr := provider.DeleteHostname(context.WithoutCancel(ctx), state.ID); derr != nil {
			e.logger.WarnContext(ctx, "delete provider hostname after a conflict", "hostname", hostname, "error", derr)
		}
		return apitypes.Domain{}, &DomainConflictError{Reason: hostname + " is registered to another account"}
	}
	if err != nil {
		return apitypes.Domain{}, fmt.Errorf("insert domain: %w", err)
	}
	return e.domainOut(row)
}

// ListDomains lists the user's registrations by hostname, re-reading those
// the provider is still working on.
func (e *Edge) ListDomains(ctx context.Context, user identity.UserID, after string, limit int) ([]apitypes.Domain, string, error) {
	rows, err := e.queries.ListDomains(ctx, ListDomainsParams{UserID: uuid.UUID(user), After: after, MaxRows: int32(limit + 1)}) //nolint:gosec // bounded by the API
	if err != nil {
		return nil, "", fmt.Errorf("list domains: %w", err)
	}
	next := ""
	if len(rows) > limit {
		rows = rows[:limit]
		next = rows[limit-1].Hostname
	}
	out := make([]apitypes.Domain, len(rows))
	for n, row := range rows {
		if out[n], err = e.domainOut(e.refreshed(ctx, row)); err != nil {
			return nil, "", err
		}
	}
	return out, next, nil
}

// GetDomain returns one registration, re-read from the provider while it is
// unsettled. An unreachable provider leaves the last state.
func (e *Edge) GetDomain(ctx context.Context, user identity.UserID, hostname string) (apitypes.Domain, error) {
	row, err := e.queries.DomainByHostname(ctx, DomainByHostnameParams{UserID: uuid.UUID(user), Hostname: strings.ToLower(strings.TrimSpace(hostname))})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.Domain{}, ErrDomainNotFound
	}
	if err != nil {
		return apitypes.Domain{}, fmt.Errorf("read domain: %w", err)
	}
	return e.domainOut(e.refreshed(ctx, row))
}

// RemoveDomain retires a registration and its certificate, refusing while a
// deployment in any workspace the user owns still serves it.
func (e *Edge) RemoveDomain(ctx context.Context, user identity.UserID, hostname string) error {
	row, err := e.queries.DomainByHostname(ctx, DomainByHostnameParams{UserID: uuid.UUID(user), Hostname: strings.ToLower(strings.TrimSpace(hostname))})
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrDomainNotFound
	}
	if err != nil {
		return fmt.Errorf("read domain: %w", err)
	}
	serving, err := e.queries.DomainServes(ctx, DomainServesParams{UserID: uuid.UUID(user), Hostname: &row.Hostname})
	if err != nil {
		return fmt.Errorf("read deployments serving the domain: %w", err)
	}
	if len(serving) > 0 {
		// A route that keeps the hostname must give it up first, or a later
		// registration of the same name would serve it.
		var names []string
		hidden := 0
		for _, s := range serving {
			if s.Visible {
				names = append(names, s.AppName+":"+s.Name)
			} else {
				hidden++
			}
		}
		if hidden > 0 {
			names = append(names, fmt.Sprintf("%d deployment(s) in workspaces you do not own", hidden))
		}
		return &DomainConflictError{Reason: fmt.Sprintf("%s still serves %s; remove the domain from those deployments first", row.Hostname, strings.Join(names, ", "))}
	}
	if row.ProviderHostnameID != nil {
		provider, err := e.provider()
		if err != nil {
			return err
		}
		if err := provider.DeleteHostname(ctx, *row.ProviderHostnameID); err != nil {
			return err
		}
	}
	if err := e.queries.DeleteDomain(ctx, row.ID); err != nil {
		return fmt.Errorf("delete domain: %w", err)
	}
	return nil
}

// ReconcileDomains re-reads registrations the provider has been working on
// for longer than recheckInterval, so a domain becomes ready, and routable,
// without anyone asking. It returns how many it re-read.
func (e *Edge) ReconcileDomains(ctx context.Context) (int, error) {
	if e.domains == nil {
		return 0, nil
	}
	rows, err := e.queries.UnsettledDomains(ctx, UnsettledDomainsParams{Before: new(time.Now().Add(-recheckInterval)), MaxRows: 50})
	if err != nil {
		return 0, fmt.Errorf("list unsettled domains: %w", err)
	}
	for _, row := range rows {
		e.refreshed(ctx, row)
	}
	return len(rows), nil
}

// reconcileDomains runs ReconcileDomains every minute until ctx ends.
func (e *Edge) reconcileDomains(ctx context.Context) {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		if _, err := e.ReconcileDomains(ctx); err != nil && ctx.Err() == nil {
			e.logger.WarnContext(ctx, "reconcile custom domains", "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// refreshed re-reads an unsettled registration and records what the
// provider says, or returns it unchanged.
func (e *Edge) refreshed(ctx context.Context, row CustomDomain) CustomDomain {
	phase := apitypes.DomainPhase(row.Phase)
	if e.domains == nil || (phase != apitypes.DomainPhaseAwaitingVerification && phase != apitypes.DomainPhaseValidating) {
		return row
	}
	state := ProviderHostname{Phase: apitypes.DomainPhaseActionRequired}
	if row.ProviderHostnameID != nil {
		got, found, err := e.domains.GetHostname(ctx, *row.ProviderHostnameID)
		if err != nil {
			e.logger.WarnContext(ctx, "custom domain refresh failed; returning the last state", "hostname", row.Hostname, "error", err)
			return row
		}
		state = got
		if !found {
			code := apitypes.HostnameRejected
			message := "this domain is no longer registered with the certificate provider; remove it here and add it again"
			state = ProviderHostname{Phase: apitypes.DomainPhaseActionRequired, ErrorCode: &code, ErrorMessage: &message}
		}
	}
	records := state.Records
	if records == nil && state.Phase == phase {
		// Keep what the provider last asked for.
		_ = json.Unmarshal(row.RequiredRecords, &records)
	}
	encoded, err := json.Marshal(nonNil(records))
	if err != nil {
		return row
	}
	updated, err := e.queries.SettleDomain(ctx, SettleDomainParams{
		ID: row.ID, Phase: string(state.Phase), RequiredRecords: encoded,
		ErrorCode: (*string)(state.ErrorCode), ErrorMessage: state.ErrorMessage,
	})
	if err != nil {
		e.logger.WarnContext(ctx, "record custom domain state", "hostname", row.Hostname, "error", err)
		return row
	}
	if apitypes.DomainPhase(updated.Phase) == apitypes.DomainPhaseReady {
		e.logger.InfoContext(ctx, "custom domain ready", "hostname", row.Hostname)
	}
	return updated
}

func (e *Edge) domainOut(row CustomDomain) (apitypes.Domain, error) {
	out := apitypes.Domain{
		Id: row.ID, Hostname: row.Hostname, Phase: apitypes.DomainPhase(row.Phase), CnameTarget: e.urls.Base(),
		RequiredRecords: []apitypes.DnsRecord{}, ErrorMessage: row.ErrorMessage,
		VerifiedAt: row.VerifiedAt, LastCheckedAt: row.LastCheckedAt, CreatedAt: row.CreatedAt, UpdatedAt: row.UpdatedAt,
	}
	if err := json.Unmarshal(row.RequiredRecords, &out.RequiredRecords); err != nil {
		return apitypes.Domain{}, fmt.Errorf("decode domain records: %w", err)
	}
	if row.ErrorCode != nil {
		code := apitypes.DomainErrorCode(*row.ErrorCode)
		out.ErrorCode = &code
	}
	return out, nil
}

func nonNil(records []apitypes.DnsRecord) []apitypes.DnsRecord {
	if records == nil {
		return []apitypes.DnsRecord{}
	}
	return records
}
