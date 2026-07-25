from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import ParseResult, parse_qs, urlparse

from .instance_catalog import aws_console_host, aws_partition_for_region

_ACCOUNT_PATTERN = re.compile(r"^[0-9]{12}$")
_REGION_PATTERN = re.compile(r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$")
_STACK_PATTERN = re.compile(r"^compute-connection-[A-Za-z0-9-]+-g[0-9]+$")
_REQUIRED_PARAMETERS = {
    "ConnectionRoleName",
    "ExternalId",
    "NodeInstanceProfileName",
    "NodeRoleName",
    "PlatformPrincipalArn",
    "TargetAccountId",
}


@dataclass(frozen=True, slots=True)
class AwsConnectionStackCreateAction:
    name: str
    template_url: str
    parameters: dict[str, str]


@dataclass(frozen=True, slots=True)
class AwsConnectionStackCleanupAction:
    name: str
    stack_id: str


def parse_aws_connection_stack_create_action(
    action_url: str,
    *,
    account_id: str,
    region: str,
    expected_template_url: str,
    expected_platform_principal_arn: str,
) -> AwsConnectionStackCreateAction:
    _validate_target(account_id=account_id, region=region)
    parsed = _console_action(
        action_url,
        region=region,
        route="/stacks/create/review",
    )
    query = _fragment_query(parsed)
    if {key for key in query if not key.startswith("param_")} != {
        "stackName",
        "templateURL",
    }:
        raise ValueError("CloudFormation action contains an unsupported operation")
    parameters = {
        key.removeprefix("param_"): _one(query, key) for key in query if key.startswith("param_")
    }
    if set(parameters) != _REQUIRED_PARAMETERS:
        raise ValueError("CloudFormation action has an unexpected parameter contract")
    name = _one(query, "stackName")
    if _STACK_PATTERN.fullmatch(name) is None or parameters["ConnectionRoleName"] != name:
        raise ValueError("CloudFormation action has an invalid managed stack identity")
    if parameters["TargetAccountId"] != account_id:
        raise ValueError("CloudFormation action targets a different AWS account")
    if parameters["PlatformPrincipalArn"] != expected_platform_principal_arn:
        raise ValueError("CloudFormation action targets a different platform principal")
    template_url = _one(query, "templateURL")
    template = urlparse(template_url)
    if template.scheme != "https" or not template.netloc or template.username or template.password:
        raise ValueError("CloudFormation action has an invalid template URL")
    if template_url != expected_template_url:
        raise ValueError("CloudFormation action targets a different immutable template")
    return AwsConnectionStackCreateAction(
        name=name,
        template_url=template_url,
        parameters=parameters,
    )


def parse_aws_connection_stack_cleanup_action(
    action_url: str,
    *,
    account_id: str,
    region: str,
    expected_names: frozenset[str],
) -> AwsConnectionStackCleanupAction:
    _validate_target(account_id=account_id, region=region)
    if not expected_names or any(_STACK_PATTERN.fullmatch(name) is None for name in expected_names):
        raise ValueError("cleanup requires exact managed stack names")
    parsed = _console_action(
        action_url,
        region=region,
        route="/stacks/stackinfo",
    )
    query = _fragment_query(parsed)
    if set(query) != {"stackId"}:
        raise ValueError("cleanup action contains unsupported parameters")
    stack_id = _one(query, "stackId")
    match = re.fullmatch(
        r"arn:([^:]+):cloudformation:([^:]+):([0-9]{12}):stack/([^/]+)/([^/]+)",
        stack_id,
    )
    if match is None:
        raise ValueError("cleanup action has an invalid stack identity")
    partition, action_region, action_account, name, _ = match.groups()
    if (
        partition != aws_partition_for_region(region)
        or action_region != region
        or action_account != account_id
        or name not in expected_names
    ):
        raise ValueError("cleanup action is outside the approved target")
    return AwsConnectionStackCleanupAction(name=name, stack_id=stack_id)


def _validate_target(*, account_id: str, region: str) -> None:
    if _ACCOUNT_PATTERN.fullmatch(account_id) is None:
        raise ValueError("AWS account ID is invalid")
    if _REGION_PATTERN.fullmatch(region) is None:
        raise ValueError("AWS region is invalid")


def _console_action(action_url: str, *, region: str, route: str) -> ParseResult:
    try:
        parsed = urlparse(action_url)
        port = parsed.port
    except ValueError:
        raise ValueError("CloudFormation action URL is invalid") from None
    try:
        outer_query = parse_qs(parsed.query, strict_parsing=True)
    except ValueError:
        raise ValueError("CloudFormation action has an invalid outer query") from None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname != aws_console_host(region)
        or port is not None
        or parsed.path != "/cloudformation/home"
        or parsed.params
        or outer_query != {"region": [region]}
        or parsed.fragment.partition("?")[0] != route
    ):
        raise ValueError("CloudFormation action is outside the approved target")
    return parsed


def _fragment_query(parsed: ParseResult) -> dict[str, list[str]]:
    _, separator, query = parsed.fragment.partition("?")
    if not separator or not query:
        raise ValueError("CloudFormation action has an invalid fragment")
    try:
        return parse_qs(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ValueError("CloudFormation action has an invalid fragment") from None


def _one(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key, [])
    if len(values) != 1 or not values[0]:
        raise ValueError("CloudFormation action is missing an exact value")
    return values[0]


__all__ = [
    "AwsConnectionStackCleanupAction",
    "AwsConnectionStackCreateAction",
    "parse_aws_connection_stack_cleanup_action",
    "parse_aws_connection_stack_create_action",
]
