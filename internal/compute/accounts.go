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
	"github.com/jackc/pgx/v5"
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
	return c.withNetworks(ctx, q, out)
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
	accounts, err := c.withNetworks(ctx, q, []account{a})
	if err != nil {
		return account{}, err
	}
	return accounts[0], nil
}

// withNetworks fills in each account's networks in the fleet's regions.
func (c *Compute) withNetworks(ctx context.Context, q *Queries, accounts []account) ([]account, error) {
	rows, err := q.FleetNetworks(ctx)
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

// networkRecheck is how often an account's networks are found or made
// again, which adds a subnet in a zone AWS opened since.
const networkRecheck = 30 * time.Minute

// EnsureNetworks finds or makes a network in every fleet region of each
// account that takes new hosts, whose network is missing or last ensured
// networkRecheck ago, and records it. A region that fails is tried again
// next pass. It reports how many networks it ensured.
func (c *Compute) EnsureNetworks(ctx context.Context, logger *slog.Logger) (int, error) {
	ensured := 0
	var failed error
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		if locked, err := q.LockNetworks(ctx); err != nil || !locked {
			return err
		}
		// Read outside the transaction, which holds no connection's row
		// while AWS answers.
		accounts, err := c.accounts(ctx, c.queries)
		if err != nil {
			return err
		}
		rows, err := q.FleetNetworks(ctx)
		if err != nil {
			return fmt.Errorf("read fleet networks: %w", err)
		}
		fresh := map[[2]string]bool{}
		for _, row := range rows {
			fresh[[2]string{row.Owner, row.Region}] = time.Since(row.EnsuredAt) < networkRecheck
		}
		for _, a := range accounts {
			for _, region := range c.fleet.Regions {
				if !a.hosting || fresh[[2]string{a.key(), region}] {
					continue
				}
				network, err := c.ensureNetwork(ctx, a.scope, region)
				if err != nil {
					if ctx.Err() != nil {
						return err
					}
					logger.WarnContext(ctx, "fleet network unavailable", "owner", a.key(), "region", region, "error", err)
					failed = errors.Join(failed, fmt.Errorf("%s %s: %w", a.key(), region, err))
					continue
				}
				raw, err := json.Marshal(network)
				if err != nil {
					return fmt.Errorf("encode network: %w", err)
				}
				if err := q.UpsertFleetNetwork(ctx, UpsertFleetNetworkParams{Owner: a.key(), Region: region, Network: raw}); err != nil {
					return fmt.Errorf("record network: %w", err)
				}
				ensured++
			}
		}
		return nil
	})
	if err != nil {
		return 0, fmt.Errorf("ensure networks: %w", err)
	}
	return ensured, failed
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
