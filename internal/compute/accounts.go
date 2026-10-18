package compute

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

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

// key names the account in cooldowns, quotas, published plans and logs.
func (a account) key() string {
	if a.connection == nil {
		return ownerPlatform
	}
	return a.connection.String()
}

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
		awsAccount: c.fleet.AccountID, scope: awsScope{key: ownerPlatform}, networks: c.fleet.Networks,
		nodeRole: c.fleet.NodeRoleARN, instanceProfile: c.fleet.InstanceProfile, hosting: true, managed: true, inUse: inUse,
	}
}

// connectionAccount is a connected account as ConnectionAccounts reads it.
func (c *Compute) connectionAccount(row ConnectionAccountsRow) (account, error) {
	var networks map[string]Network
	if err := json.Unmarshal(row.Networks, &networks); err != nil {
		return account{}, fmt.Errorf("decode networks of connection %s: %w", row.ID, err)
	}
	return account{
		connection: &row.ID, authorization: &row.AuthorizationID, awsAccount: accountOfRole(row.RoleArn),
		scope:    c.aws().assume(row.RoleArn, row.ExternalID, "lazycloud-fleet-"+row.ID.String()[:8], row.ID.String()),
		networks: networks, nodeRole: deref(row.NodeRoleArn), instanceProfile: deref(row.NodeInstanceProfile),
		pays: true, hosting: row.Hosting, managed: row.Managed, inUse: row.InUse,
	}, nil
}

// accounts are the platform's account and every connected account with an
// active authorization, read through q.
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
		a, err := c.connectionAccount(row)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, nil
}

// accountOf is the account of a host that records connection.
func (c *Compute) accountOf(ctx context.Context, q *Queries, connection *uuid.UUID) (account, error) {
	if connection == nil {
		inUse, err := q.PlatformInUse(ctx)
		if err != nil {
			return account{}, fmt.Errorf("read platform use: %w", err)
		}
		return c.platformAccount(inUse), nil
	}
	rows, err := q.ConnectionAccounts(ctx, connection)
	if err != nil {
		return account{}, fmt.Errorf("read connected account: %w", err)
	}
	if len(rows) == 0 {
		return account{}, errors.New("the connection has no active authorization")
	}
	return c.connectionAccount(rows[0])
}

// hostOwner is the key of the account of a host that records connection.
func hostOwner(connection *uuid.UUID) string {
	return account{connection: connection}.key()
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
