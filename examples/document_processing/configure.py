"""Create the signing secret once before deploying the document app.

Run ``python -m examples.document_processing.configure`` from the repository root.
Existing secrets are left intact; a duplicate name raises an error. Redeploying
the app does not require running this module again.
"""

import secrets

from examples.document_processing.resources import JOB_TOKEN_SECRET_NAME
from lazycloud import Secret


def configure() -> None:
    Secret(JOB_TOKEN_SECRET_NAME).create(secrets.token_urlsafe(48))


if __name__ == "__main__":
    configure()
