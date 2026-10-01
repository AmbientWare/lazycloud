from __future__ import annotations

import base64
import importlib
import inspect
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import typer
from lazycloud.cli.app_export import app_export
from lazycloud.cli.components.output import CliContextState
from lazycloud.client_codegen import ClientGenerationError, write_client_package
from lazycloud.client_contracts import build_client_contract
from lazycloud.values import cloudpickle_bytes
from pydantic import BaseModel, Field
from shared.deployments import DeploymentKind
from typer.testing import CliRunner, Result

from tests.api_server import ApiRequest, FakeApi, Reply, json_reply

pytestmark = pytest.mark.usefixtures("isolated_imports")

NOW = "2026-09-30T12:00:00Z"
DEPLOYMENTS = "/v1/workspaces/team/deployments"
FUNCTIONS = "/v1/workspaces/team/apps/reports/functions"


class Sale(BaseModel):
    region_name: str = Field(alias="regionName")
    amount: int


class Summary(BaseModel):
    total: int
    regions: list[str]


def summarize_sales(values: list[int], *, scale: int = 1) -> int:
    return sum(values) * scale


def build_summary(sales: list[Sale]) -> Summary:
    return Summary(total=sum(s.amount for s in sales), regions=[s.region_name for s in sales])


def _uuid(index: int) -> str:
    return f"0192f0a0-0000-7000-8000-{index:012d}"


def _workload(index: int, name: str, state: str = "active") -> dict[str, object]:
    return {
        "id": _uuid(index),
        "app": "reports",
        "name": name,
        "kind": "function",
        "state": state,
        "version": 1,
        "release_id": _uuid(100 + index),
        "created_at": NOW,
    }


def _function(name: str, release_id: str, contract: dict[str, Any] | None) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": name,
        "handler": f"reports:{name}",
        "source": {"sha256": "0" * 64},
        "image": {"python_version": "3.11"},
        "resources": {"cpu_millis": 500, "memory_mib": 1024},
    }
    if contract is not None:
        spec["client_contract"] = contract
    release: dict[str, object] = {
        "id": release_id,
        "function": name,
        "version": 1,
        "created_at": NOW,
        "spec": spec,
    }
    return {"name": name, "app": "reports", "state": "active", "active_release": release}


def _contract(func: Callable[..., Any]) -> dict[str, Any]:
    contract = build_client_contract(func, kind=DeploymentKind.Function)
    assert contract is not None
    return contract.model_dump(mode="json")


def _serve_app(api: FakeApi, *, contracts: Mapping[str, dict[str, Any] | None]) -> dict[str, str]:
    """Serve the reports app over two deployment pages; returns each function's release id."""
    releases = {name: _uuid(100 + index) for index, name in enumerate(contracts, start=1)}
    workloads = [_workload(index, name) for index, name in enumerate(contracts, start=1)]
    pages: dict[str | None, dict[str, object]] = {
        None: {"deployments": workloads[:1], "next_cursor": "page-2"},
        "page-2": {"deployments": [*workloads[1:], _workload(9, "retired", "deleted")]},
    }

    @api.route("GET", DEPLOYMENTS)
    def deployments(request: ApiRequest) -> Reply:
        assert request.query["app"] == ["reports"]
        cursor = request.query.get("cursor", [None])[0]
        return json_reply(pages[cursor])

    @api.route("GET", f"{FUNCTIONS}/[^/]+")
    def function(request: ApiRequest) -> Reply:
        name = request.path.rsplit("/", 1)[1]
        return json_reply(_function(name, releases[name], contracts[name]))

    return releases


def _export(*args: str) -> Result:
    cli = typer.Typer()
    cli.command("export")(app_export)

    @cli.callback()
    def main(ctx: typer.Context) -> None:
        ctx.obj = CliContextState(json=True)

    return CliRunner().invoke(cli, ["export", *args])


def _task(task_id: str, function: str, status: str, **extra: object) -> dict[str, object]:
    return {
        "id": task_id,
        "app": "reports",
        "function": function,
        "release_id": _uuid(101),
        "status": status,
        "attempts": 1,
        "max_attempts": 1,
        "created_at": NOW,
        **extra,
    }


def _serve_task(
    api: FakeApi, function: str, task_id: str, *, result: object = None, **finished: object
) -> None:
    api.route("POST", f"{FUNCTIONS}/{function}/tasks")(
        lambda _: json_reply({"tasks": [_task(task_id, function, "queued")]}, 201)
    )
    status = "failed" if "failure" in finished else "succeeded"
    api.route("GET", f"/v1/workspaces/team/tasks/{task_id}")(
        lambda _: json_reply(_task(task_id, function, status, **finished))
    )
    api.route("GET", f"/v1/workspaces/team/tasks/{task_id}/result")(
        lambda _: json_reply({"encoding": "json", "value": result})
    )


def _submitted(api: FakeApi, function: str) -> list[Any]:
    return [request.json() for request in api.calls("POST", f"{FUNCTIONS}/{function}/tasks")]


def test_export_writes_a_typed_package_whose_functions_run_remotely(
    tmp_path: Path, fake_api: FakeApi
) -> None:
    contracts = {
        "summarize_sales": _contract(summarize_sales),
        "build_summary": _contract(build_summary),
    }
    releases = _serve_app(fake_api, contracts=contracts)
    output = tmp_path / "lazycloud_clients"

    result = _export("reports", "-o", str(output))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    version = payload["version"]
    assert payload["package"] == "lazycloud_clients.reports"
    assert payload["path"] == str(output / "reports")
    assert payload["asgi_without_schema"] == []
    assert payload["resources"] == [
        {"name": n, "kind": "function", "deployment_version": 1, "release_id": releases[n]}
        for n in ("build_summary", "summarize_sales")
    ]
    lock = json.loads((output / "lazycloud-clients.lock.json").read_text())
    assert lock["reports"]["version"] == version
    assert lock["reports"]["workspace"] == "team"
    assert (output / "reports" / f"v_{version}" / "py.typed").is_file()

    sys.path.insert(0, str(tmp_path))
    clients = importlib.import_module("lazycloud_clients.reports")
    assert clients.__all__ == ["build_summary", "summarize_sales"]
    signature = inspect.signature(clients.summarize_sales.remote)
    assert str(signature) == "(values: 'list[int]', *, scale: 'int' = 1) -> 'int'"

    _serve_task(fake_api, "summarize_sales", _uuid(201), result=12)
    assert clients.summarize_sales.remote([1, 2, 3], scale=2) == 12
    assert _submitted(fake_api, "summarize_sales") == [
        {"inputs": [{"encoding": "json", "value": {"args": [[1, 2, 3]], "kwargs": {"scale": 2}}}]}
    ]

    _serve_task(
        fake_api, "build_summary", _uuid(202), result={"total": 7, "regions": ["east", "west"]}
    )
    sale = clients.build_summary.Sale
    summary = clients.build_summary.remote(
        [sale(regionName="east", amount=3), sale(regionName="west", amount=4)]
    )
    assert summary == clients.build_summary.Summary(total=7, regions=["east", "west"])
    sent = _submitted(fake_api, "build_summary")[0]["inputs"][0]["value"]
    assert sent == {
        "args": [[{"regionName": "east", "amount": 3}, {"regionName": "west", "amount": 4}]],
        "kwargs": {},
    }


def test_generated_function_raises_the_remote_failure(tmp_path: Path, fake_api: FakeApi) -> None:
    _serve_app(fake_api, contracts={"summarize_sales": _contract(summarize_sales)})
    assert _export("reports", "-o", str(tmp_path / "lazycloud_clients")).exit_code == 0
    sys.path.insert(0, str(tmp_path))
    clients = importlib.import_module("lazycloud_clients.reports")
    exception = base64.b64encode(cloudpickle_bytes(ValueError("no sales"))).decode()
    failure = {"kind": "user_error", "type": "ValueError", "message": "no sales"}
    _serve_task(
        fake_api, "summarize_sales", _uuid(203), failure={**failure, "exception": exception}
    )

    with pytest.raises(ValueError, match="no sales"):
        clients.summarize_sales.remote([])


def test_a_new_release_needs_a_new_export(tmp_path: Path, fake_api: FakeApi) -> None:
    output = tmp_path / "lazycloud_clients"
    _serve_app(fake_api, contracts={"summarize_sales": _contract(summarize_sales)})
    first = json.loads(_export("reports", "-o", str(output)).stdout)["version"]

    @fake_api.route("GET", f"{FUNCTIONS}/summarize_sales")
    def redeployed(_: ApiRequest) -> Reply:
        return json_reply(_function("summarize_sales", _uuid(300), _contract(summarize_sales)))

    second = json.loads(_export("reports", "-o", str(output)).stdout)["version"]

    assert first != second
    assert f"v_{second}" in (output / "reports" / "__init__.py").read_text()
    assert json.loads((output / "lazycloud-clients.lock.json").read_text())["reports"][
        "version"
    ] == (second)


def test_export_rejects_openapi_options_for_resources_that_are_not_asgi(
    tmp_path: Path, fake_api: FakeApi
) -> None:
    _serve_app(fake_api, contracts={"summarize_sales": _contract(summarize_sales)})
    output = tmp_path / "lazycloud_clients"

    with pytest.raises(ClientGenerationError, match=r"^unknown ASGI resources: api, web$"):
        write_client_package(
            app="reports",
            workspace=None,
            output=output,
            openapi_files={"api": tmp_path / "schema.json"},
            openapi_paths={"web": "/docs"},
        )

    assert not output.exists()


def test_export_names_functions_deployed_without_a_contract(
    tmp_path: Path, fake_api: FakeApi
) -> None:
    _serve_app(fake_api, contracts={"summarize_sales": None, "build_summary": None})

    with pytest.raises(
        ClientGenerationError,
        match=r"contracts: function:summarize_sales@v1, function:build_summary@v1\. Redeploy",
    ):
        write_client_package(app="reports", workspace=None, output=tmp_path / "lazycloud_clients")


def test_export_requires_a_package_name_for_the_output_directory(
    tmp_path: Path, fake_api: FakeApi
) -> None:
    with pytest.raises(ClientGenerationError, match="must be a Python package identifier"):
        write_client_package(app="reports", workspace=None, output=tmp_path / "my-clients")

    assert fake_api.requests == []
