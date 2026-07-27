from __future__ import annotations

import argparse

import uvicorn
from foundation.environment_file import load_environment_file
from shared.app_identity import CONTROL_PLANE_SERVICE_NAME


class ApiServerArguments(argparse.Namespace):
    host: str
    port: int
    log_level: str
    workers: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=CONTROL_PLANE_SERVICE_NAME)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--workers", type=int, default=1)
    return parser


def run_api_server(
    *,
    host: str = "127.0.0.1",
    port: int = 9000,
    log_level: str = "info",
    workers: int = 1,
) -> None:
    uvicorn.run(
        "api.fastapi_app:create_production_app",
        factory=True,
        host=host,
        port=port,
        log_level=log_level,
        workers=workers,
    )


def main(argv: list[str] | None = None) -> None:
    load_environment_file()
    args = ApiServerArguments()
    build_parser().parse_args(argv, namespace=args)
    run_api_server(
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
