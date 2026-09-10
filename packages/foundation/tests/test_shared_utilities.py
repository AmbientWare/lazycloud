from __future__ import annotations

import pytest
from foundation.network import (
    normalize_worker_network_prefix,
    parse_worker_network_prefix,
    worker_network_prefix,
)
from foundation.shell import shell_quote
from foundation.validation import (
    validate_allow_list,
    validate_cidr,
)


def test_validation_network_prefix_and_shell_quote() -> None:
    cidr = validate_cidr("10.0.0.1/24")
    assert cidr.normalized == "10.0.0.0/24"
    assert not cidr.is_ipv6
    assert validate_allow_list(["2001:db8::/32"])[0].is_ipv6
    with pytest.raises(ValueError):
        validate_allow_list(["10.0.0.0/8"] * 11)

    prefix = worker_network_prefix("cluster/a", "node one")
    assert prefix == "cluster:cluster_a:node:node_one"
    parsed_prefix = parse_worker_network_prefix(prefix)
    assert parsed_prefix is not None
    assert parsed_prefix.node_name == "node_one"
    assert (
        normalize_worker_network_prefix("cluster", "node\tone") == "cluster:cluster:node:node_one"
    )
    assert shell_quote("can't") == "'can'\\''t'"
