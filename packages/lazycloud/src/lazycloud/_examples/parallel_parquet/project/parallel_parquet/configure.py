"""Store S3 credentials for the secret names configured by the Parquet app.

Run ``uv run python -m parallel_parquet.configure`` with the bucket configuration
and PARQUET_S3_ACCESS_KEY_ID / PARQUET_S3_SECRET_ACCESS_KEY in the environment.
Running it again replaces the selected secrets.
"""

import os

from lazycloud import Secret

from .app import CONFIG


def configure() -> None:
    if not CONFIG.access_key_secret or not CONFIG.secret_key_secret:
        raise ValueError(
            "Set both LAZYCLOUD_PARQUET_ACCESS_KEY_SECRET and LAZYCLOUD_PARQUET_SECRET_KEY_SECRET"
        )
    values = {
        CONFIG.access_key_secret: os.environ["PARQUET_S3_ACCESS_KEY_ID"],
        CONFIG.secret_key_secret: os.environ["PARQUET_S3_SECRET_ACCESS_KEY"],
    }
    for name, value in values.items():
        Secret(name).set(value)


if __name__ == "__main__":
    configure()
