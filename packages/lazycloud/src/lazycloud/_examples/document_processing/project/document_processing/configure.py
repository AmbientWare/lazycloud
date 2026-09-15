"""Create the signing secret once before deploying the document app.

Run ``uv run python -m document_processing.configure`` from the downloaded project directory.
Existing secrets are left intact; a duplicate name raises an error. Redeploying
the app does not require running this module again.
"""

import secrets

from lazycloud import Secret

from .resources import JOB_TOKEN_SECRET_NAME


def configure() -> None:
    Secret(JOB_TOKEN_SECRET_NAME).create(secrets.token_urlsafe(48))


if __name__ == "__main__":
    configure()
