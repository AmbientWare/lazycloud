"""Store the sql-server URL and an access token for the compare endpoint.

Run ``uv run python -m lora_sql.configure`` after the first deploy. Values are
read from the terminal and never printed.
"""

from getpass import getpass

from .compare import server_token, server_url


def configure() -> None:
    url = input("sql-server URL from the deploy output: ").strip()
    if not url.startswith("https://"):
        raise SystemExit("the URL starts with https://")
    token = getpass("LazyCloud access token for the compare endpoint: ").strip()
    if not token:
        raise SystemExit("an access token is required")
    server_url.set(url)
    server_token.set(token)
    print(f"stored {server_url.name} and {server_token.name}")


if __name__ == "__main__":
    configure()
