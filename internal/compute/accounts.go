package compute

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"time"

	"github.com/google/uuid"
)

// account is an AWS account the fleet runs hosts in: the platform's own,
// or a connected account's through its role. The fleet plans, launches,
// stops, resumes and reconciles every account alike; accounts differ only
// in what they hold here.
type account struct {
	// connection is the connected account's connection; nil for the
	// platform.
	connection *uuid.UUID
	// authorization is the connection authorization whose role the calls
	// assume.
	authorization *uuid.UUID
	// awsAccount is the AWS account id.
	awsAccount string
	scope      awsScope
	networks   map[string]Network
	// nodeRole and instanceProfile are what the account's hosts run as.
	nodeRole, instanceProfile string
	// pays reports an account that pays AWS for its hosts itself: its
	// purchases keep no margin.
	pays bool
	// hosting reports an account that takes new hosts; managed one whose
	// existing instances the fleet still runs; inUse one an active
	// workspace runs in, which keeps reserves.
	hosting, managed, inUse bool
}

// key names the account in cooldowns, quotas, networks, published plans
// and logs.
func (a account) key() string { return hostOwner(a.connection) }

// kind is what the account's hosts bill as.
func (a account) kind() HostKind {
	if a.connection == nil {
		return KindPlatform
	}
	return KindConnection
}

// owns reports a host of the account's, by the connection it records.
func (a account) owns(connection *uuid.UUID) bool {
	return sameOwner(a.connection, connection)
}

// platformAccount is the platform's own account, whose calls use the
// platform's credentials.
func (c *Compute) platformAccount(inUse bool) account {
	return account{
		awsAccount: c.fleet.AccountID, scope: awsScope{key: ownerPlatform}, nodeRole: c.fleet.NodeRoleARN,
		instanceProfile: c.fleet.InstanceProfile, hosting: true, managed: true, inUse: inUse,
	}
}

// connectionAccount is a connected account as ConnectionAccounts reads it.
func (c *Compute) connectionAccount(row ConnectionAccountsRow) account {
	return account{
		connection: &row.ID, authorization: &row.AuthorizationID, awsAccount: accountOfRole(row.RoleArn),
		scope:    c.aws().assume(row.RoleArn, row.ExternalID, "lazycloud-fleet-"+row.ID.String()[:8], row.ID.String()),
		nodeRole: deref(row.NodeRoleArn), instanceProfile: deref(row.NodeInstanceProfile),
		pays: true, hosting: row.Hosting, managed: row.Managed, inUse: row.InUse,
	}
}

// accounts are the platform's account and every connected account with an
// active authorization, with their networks, read through q.
func (c *Compute) accounts(ctx context.Context, q *Queries) ([]account, error) {
	inUse, err := q.PlatformInUse(ctx)
	if err != nil {
		return nil, fmt.Errorf("read platform use: %w", err)
	}
	rows, err := q.ConnectionAccounts(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("read connected accounts: %w", err)
	}
	out := []account{c.platformAccount(inUse)}
	for _, row := range rows {
		out = append(out, c.connectionAccount(row))
	}
	return c.withNetworks(ctx, q, out, nil)
}

// accountOf is the account of a host that records connection, for its
// calls; inUse is read only by accounts.
func (c *Compute) accountOf(ctx context.Context, q *Queries, connection *uuid.UUID) (account, error) {
	a := c.platformAccount(false)
	if connection != nil {
		rows, err := q.ConnectionAccounts(ctx, connection)
		if err != nil {
			return account{}, fmt.Errorf("read connected account: %w", err)
		}
		if len(rows) == 0 {
			return account{}, errors.New("the connection has no active authorization")
		}
		a = c.connectionAccount(rows[0])
	}
	accounts, err := c.withNetworks(ctx, q, []account{a}, ptr(a.key()))
	if err != nil {
		return account{}, err
	}
	return accounts[0], nil
}

// withNetworks fills in each account's networks in the fleet's regions,
// reading only owner's when one is given.
func (c *Compute) withNetworks(ctx context.Context, q *Queries, accounts []account, owner *string) ([]account, error) {
	rows, err := q.FleetNetworks(ctx, owner)
	if err != nil {
		return nil, fmt.Errorf("read fleet networks: %w", err)
	}
	byOwner := map[string]map[string]Network{}
	for _, row := range rows {
		if !slices.Contains(c.fleet.Regions, row.Region) {
			continue
		}
		var n Network
		if err := json.Unmarshal(row.Network, &n); err != nil {
			return nil, fmt.Errorf("decode the network of %s in %s: %w", row.Owner, row.Region, err)
		}
		if byOwner[row.Owner] == nil {
			byOwner[row.Owner] = map[string]Network{}
		}
		byOwner[row.Owner][row.Region] = n
	}
	for i := range accounts {
		accounts[i].networks = byOwner[accounts[i].key()]
	}
	return accounts, nil
}

const (
	// networkRecheck is how often an account's networks are found or made
	// again, which adds a subnet in a zone AWS opened since.
	networkRecheck = 30 * time.Minute
	// networkRetry is how long a region whose network failed waits before
	// the next try.
	networkRetry = 5 * time.Minute
	// networkDeadline bounds one account's network in one region, so a
	// slow account holds up no other.
	networkDeadline = 2 * time.Minute
)

// EnsureNetworks finds or makes a network in every fleet region of each
// account that takes new hosts, unless it was found within networkRecheck
// or failed within networkRetry, and records the network or why it failed.
// It holds the networks lock, so no removal deletes an account's network
// while it runs, and rereads each account under it. It reports how many
// networks it ensured.
func (c *Compute) EnsureNetworks(ctx context.Context, logger *slog.Logger) (int, error) {
	release, locked, err := c.networksLock(ctx)
	if err != nil || !locked {
		return 0, err
	}
	defer release()
	accounts, err := c.accounts(ctx, c.queries)
	if err != nil {
		return 0, err
	}
	checked, err := c.queries.CheckedNetworks(ctx, CheckedNetworksParams{
		EnsuredSeconds: networkRecheck.Seconds(), FailedSeconds: networkRetry.Seconds(),
	})
	if err != nil {
		return 0, fmt.Errorf("read checked networks: %w", err)
	}
	done := map[CheckedNetworksRow]bool{}
	for _, row := range checked {
		done[row] = true
	}
	ensured := 0
	var failed error
	for _, a := range accounts {
		for _, region := range c.fleet.Regions {
			if !a.hosting || done[CheckedNetworksRow{Owner: a.key(), Region: region}] {
				continue
			}
			if err := c.ensureAccountNetwork(ctx, a, region); err != nil {
				if ctx.Err() != nil {
					return ensured, err
				}
				logger.WarnContext(ctx, "fleet network unavailable", "owner", a.key(), "region", region, "error", err)
				failed = errors.Join(failed, fmt.Errorf("%s %s: %w", a.key(), region, err))
				continue
			}
			ensured++
		}
	}
	return ensured, failed
}

// ensureAccountNetwork finds or makes a's network in region, if a still
// takes hosts, and records it or the failure.
func (c *Compute) ensureAccountNetwork(ctx context.Context, a account, region string) error {
	now, err := c.accountOf(ctx, c.queries, a.connection)
	if err != nil || !now.hosting {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, networkDeadline)
	defer cancel()
	network, err := c.ensureNetwork(ctx, now.scope, region)
	if err != nil {
		if rerr := c.queries.FailFleetNetwork(context.WithoutCancel(ctx), FailFleetNetworkParams{
			Owner: a.key(), Region: region, Error: ptr(truncate(err.Error())),
		}); rerr != nil {
			return errors.Join(err, fmt.Errorf("record the failure: %w", rerr))
		}
		return err
	}
	raw, err := json.Marshal(network)
	if err != nil {
		return fmt.Errorf("encode network: %w", err)
	}
	if err := c.queries.UpsertFleetNetwork(ctx, UpsertFleetNetworkParams{Owner: a.key(), Region: region, Network: raw}); err != nil {
		return fmt.Errorf("record network: %w", err)
	}
	return nil
}

// networksLock takes the networks lock on a connection of its own, if no
// other session holds it; release gives it back.
func (c *Compute) networksLock(ctx context.Context) (release func(), locked bool, err error) {
	conn, err := c.pool.Acquire(ctx)
	if err != nil {
		return nil, false, fmt.Errorf("acquire a connection: %w", err)
	}
	q := New(conn)
	if locked, err = q.TryNetworksLock(ctx); err != nil || !locked {
		conn.Release()
		if err != nil {
			err = fmt.Errorf("take the networks lock: %w", err)
		}
		return nil, false, err
	}
	return func() {
		// A failed unlock leaves the lock to the session's end, which
		// destroying the connection brings.
		if err := q.ReleaseNetworksLock(context.WithoutCancel(ctx)); err != nil {
			_ = conn.Conn().Close(context.WithoutCancel(ctx)) //nolint:errcheck // The connection is discarded either way.
		}
		conn.Release()
	}, true, nil
}

// hostOwner is the key of the account of a host that records connection.
func hostOwner(connection *uuid.UUID) string {
	if connection == nil {
		return ownerPlatform
	}
	return connection.String()
}

// fleetZones are, by region, the zone ids of every managed account's
// subnets. Spot prices and zone offerings are market data by zone id, read
// once for all accounts with the platform's credentials.
func (c *Compute) fleetZones(ctx context.Context) (map[string]map[string]bool, error) {
	accounts, err := c.accounts(ctx, c.queries)
	if err != nil {
		return nil, err
	}
	zones := map[string]map[string]bool{}
	for _, a := range accounts {
		if !a.managed {
			continue
		}
		for region, n := range a.networks {
			for _, s := range n.Subnets {
				if s.ZoneID == "" {
					continue
				}
				if zones[region] == nil {
					zones[region] = map[string]bool{}
				}
				zones[region][s.ZoneID] = true
			}
		}
	}
	return zones, nil
}
