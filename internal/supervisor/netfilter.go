package supervisor

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net"
	"net/netip"

	"github.com/google/nftables"
	"github.com/google/nftables/expr"
	"golang.org/x/sys/unix"
)

// netfilterTable is the nftables table holding a container's egress policy.
const netfilterTable = "lazycloud"

// maxAllowedRanges bounds an allow list.
const maxAllowedRanges = 10

// An untagged Ethernet frame carries its EtherType at byte 12 and its
// packet after 14 bytes.
const (
	etherTypeOffset     = 12
	ethernetHeaderBytes = 14
)

// NetworkPolicy is the argument of `supervisor netfilter`: block drops all
// egress but ARP; an allow list permits only those ranges and wins over block.
type NetworkPolicy struct {
	Block bool     `json:"block"`
	Allow []string `json:"allow"`
}

// ApplyNetworkPolicy installs policy as a netdev egress filter on every
// non-loopback interface of the current network namespace, replacing the
// lazycloud table in one transaction. An empty policy deletes the table.
func ApplyNetworkPolicy(raw string) error {
	var policy NetworkPolicy
	decoder := json.NewDecoder(bytes.NewReader([]byte(raw)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&policy); err != nil {
		return fmt.Errorf("parse the network policy: %w", err)
	}
	if len(policy.Allow) > maxAllowedRanges {
		return fmt.Errorf("the allow list has %d ranges; at most %d are allowed", len(policy.Allow), maxAllowedRanges)
	}
	allowed := make([]netip.Prefix, 0, len(policy.Allow))
	for _, cidr := range policy.Allow {
		prefix, err := netip.ParsePrefix(cidr)
		if err != nil {
			return fmt.Errorf("invalid allow-list range %q: %w", cidr, err)
		}
		allowed = append(allowed, prefix.Masked())
	}
	ifaces, err := net.Interfaces()
	if err != nil {
		return fmt.Errorf("list interfaces: %w", err)
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("open netlink: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyNetdev, Name: netfilterTable}
	// Adding first makes the delete succeed whether or not the table exists;
	// the batch applies atomically.
	conn.AddTable(table)
	conn.DelTable(table)
	if policy.Block || len(allowed) > 0 {
		conn.AddTable(table)
		for _, iface := range ifaces {
			if iface.Flags&net.FlagLoopback != 0 {
				continue
			}
			addEgressChain(conn, table, iface.Name, allowed)
		}
	}
	if err := conn.Flush(); err != nil {
		return fmt.Errorf("apply the network policy: %w", err)
	}
	return nil
}

// addEgressChain drops every frame on device but ARP and IP packets to an
// allowed range. It reads the EtherType and destination from the frame
// itself rather than the packet's protocol metadata, which a raw socket
// sets freely; anything not Ethernet-framed IPv4, IPv6 or ARP, such as a
// VLAN tag, is dropped.
func addEgressChain(conn *nftables.Conn, table *nftables.Table, device string, allowed []netip.Prefix) {
	drop := nftables.ChainPolicyDrop
	chain := conn.AddChain(&nftables.Chain{
		Name: "egress-" + device, Table: table, Type: nftables.ChainTypeFilter,
		Hooknum: nftables.ChainHookEgress, Priority: nftables.ChainPriorityFilter, Policy: &drop, Device: device,
	})
	rule := func(exprs ...expr.Any) { conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: exprs}) }
	etherType := func(kind uint16) []expr.Any {
		data := make([]byte, 2)
		binary.BigEndian.PutUint16(data, kind)
		return []expr.Any{
			&expr.Payload{DestRegister: 1, Base: expr.PayloadBaseLLHeader, Offset: etherTypeOffset, Len: 2},
			&expr.Cmp{Op: expr.CmpOpEq, Register: 1, Data: data},
		}
	}
	accept := &expr.Verdict{Kind: expr.VerdictAccept}
	rule(append(etherType(unix.ETH_P_ARP), accept)...)
	for _, prefix := range allowed {
		kind, offset := uint16(unix.ETH_P_IP), uint32(ethernetHeaderBytes+16)
		if prefix.Addr().Is6() {
			kind, offset = unix.ETH_P_IPV6, ethernetHeaderBytes+24
		}
		addr := prefix.Addr().AsSlice()
		mask := net.CIDRMask(prefix.Bits(), len(addr)*8)
		rule(append(etherType(kind),
			&expr.Payload{DestRegister: 1, Base: expr.PayloadBaseLLHeader, Offset: offset, Len: uint32(len(addr))},              //nolint:gosec // 4 or 16
			&expr.Bitwise{SourceRegister: 1, DestRegister: 1, Len: uint32(len(addr)), Mask: mask, Xor: make([]byte, len(addr))}, //nolint:gosec // see above
			&expr.Cmp{Op: expr.CmpOpEq, Register: 1, Data: addr},
			accept,
		)...)
	}
}
