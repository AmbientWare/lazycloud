from __future__ import annotations

import re
from typing import Literal

from pydantic import JsonValue, TypeAdapter, ValidationError

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def validate_aws_account_connection_template_policy(payload: bytes) -> None:
    """Prove the authorization role can manage only its exact generation stack."""
    template = _JSON_OBJECT.validate_json(payload)
    statements = _connection_role_policy_statements(template)
    own_stack = (
        "arn:aws:cloudformation:us-east-1:123456789012:"
        "stack/compute-connection-example-g1/51af3dc0-0000-4000-8000-000000000001"
    )
    denied_stacks = (
        "arn:aws:cloudformation:us-east-1:123456789012:"
        "stack/unrelated-stack/51af3dc0-0000-4000-8000-000000000002",
        "arn:aws:cloudformation:us-east-1:123456789012:"
        "stack/compute-connection-example-g1/51af3dc0-0000-4000-8000-000000000003",
    )
    references = {"AWS::StackId": own_stack}
    actions = (
        "cloudformation:DeleteStack",
        "cloudformation:DescribeStackEvents",
        "cloudformation:DescribeStacks",
    )
    for action in actions:
        own_decision = _evaluate_identity_policy(
            statements,
            action=action,
            resource=own_stack,
            references=references,
        )
        if own_decision != "allowed":
            raise ValueError(
                f"AWS connection role must allow {action} on its exact AWS::StackId; "
                f"decision was {own_decision}"
            )
        for denied_resource in denied_stacks:
            other_decision = _evaluate_identity_policy(
                statements,
                action=action,
                resource=denied_resource,
                references=references,
            )
            if other_decision != "implicit_deny":
                raise ValueError(
                    f"AWS connection role must implicitly deny {action} on every other stack; "
                    f"decision was {other_decision}"
                )


def _connection_role_policy_statements(
    template: dict[str, JsonValue],
) -> list[dict[str, JsonValue]]:
    resources = _required_mapping(template, "Resources")
    role = _required_mapping(resources, "ConnectionRole")
    properties = _required_mapping(role, "Properties")
    policies = _required_sequence(properties, "Policies")
    control_policy = next(
        (
            _as_mapping(policy, path="ConnectionRole.Properties.Policies[]")
            for policy in policies
            if _mapping_string(policy, "PolicyName") == "managed-compute-control"
        ),
        None,
    )
    if control_policy is None:
        raise ValueError("AWS connection role is missing managed-compute-control policy")
    document = _required_mapping(control_policy, "PolicyDocument")
    return [
        _as_mapping(statement, path="managed-compute-control.Statement[]")
        for statement in _required_sequence(document, "Statement")
    ]


def _evaluate_identity_policy(
    statements: list[dict[str, JsonValue]],
    *,
    action: str,
    resource: str,
    references: dict[str, str],
) -> Literal["allowed", "explicit_deny", "implicit_deny"]:
    allowed = False
    for statement in statements:
        if not _statement_matches_action(statement, action):
            continue
        if "Condition" in statement:
            raise ValueError(
                "AWS connection CloudFormation permissions use an unsupported conditional rule"
            )
        if not _statement_matches_resource(statement, resource, references=references):
            continue
        effect = statement.get("Effect")
        if effect == "Deny":
            return "explicit_deny"
        if effect != "Allow":
            raise ValueError("AWS connection policy statement has an invalid effect")
        allowed = True
    return "allowed" if allowed else "implicit_deny"


def _statement_matches_action(statement: dict[str, JsonValue], action: str) -> bool:
    has_action = "Action" in statement
    has_not_action = "NotAction" in statement
    if has_action == has_not_action:
        raise ValueError("AWS connection policy statement must define Action or NotAction")
    key = "Action" if has_action else "NotAction"
    patterns = _policy_strings(statement[key], path=key)
    matched = any(
        _iam_pattern_matches(pattern, action, case_sensitive=False) for pattern in patterns
    )
    return matched if has_action else not matched


def _statement_matches_resource(
    statement: dict[str, JsonValue],
    resource: str,
    *,
    references: dict[str, str],
) -> bool:
    has_resource = "Resource" in statement
    has_not_resource = "NotResource" in statement
    if has_resource == has_not_resource:
        raise ValueError("AWS connection policy statement must define Resource or NotResource")
    key = "Resource" if has_resource else "NotResource"
    patterns = _policy_resources(statement[key], references=references, path=key)
    matched = any(
        _iam_pattern_matches(pattern, resource, case_sensitive=True) for pattern in patterns
    )
    return matched if has_resource else not matched


def _policy_resources(
    value: JsonValue,
    *,
    references: dict[str, str],
    path: str,
) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        resources: list[str] = []
        for index, item in enumerate(value):
            resources.extend(
                _policy_resources(item, references=references, path=f"{path}[{index}]")
            )
        return resources
    intrinsic = _as_mapping(value, path=path)
    if set(intrinsic) != {"Ref"} or not isinstance(intrinsic["Ref"], str):
        raise ValueError(f"AWS connection policy {path} uses an unsupported resource expression")
    reference = intrinsic["Ref"]
    resolved = references.get(reference)
    if resolved is None:
        raise ValueError(f"AWS connection policy {path} has an unresolved {reference} reference")
    return [resolved]


def _policy_strings(value: JsonValue, *, path: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return [item for item in value if isinstance(item, str)]
    raise ValueError(f"AWS connection policy {path} must be a string or non-empty string list")


def _iam_pattern_matches(pattern: str, value: str, *, case_sensitive: bool) -> bool:
    expression = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.fullmatch(expression, value, flags=flags) is not None


def _required_mapping(
    value: dict[str, JsonValue],
    key: str,
) -> dict[str, JsonValue]:
    if key not in value:
        raise ValueError(f"AWS connection template is missing {key}")
    return _as_mapping(value[key], path=key)


def _as_mapping(value: JsonValue, *, path: str) -> dict[str, JsonValue]:
    try:
        return _JSON_OBJECT.validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"AWS connection template {path} must be an object") from exc


def _required_sequence(value: dict[str, JsonValue], key: str) -> list[JsonValue]:
    sequence = value.get(key)
    if not isinstance(sequence, list) or not sequence:
        raise ValueError(f"AWS connection template {key} must be a non-empty list")
    return sequence


def _mapping_string(value: JsonValue, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    candidate = value.get(key)
    return candidate if isinstance(candidate, str) else None
