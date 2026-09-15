"""Store provider configuration from the local environment as workspace secrets.

Run ``python -m examples.sandboxed_coding_agent.configure`` after setting the
variables named in PROVIDER_SECRET_NAMES. Running it again replaces those values.
"""

import os

from examples.sandboxed_coding_agent.app import PROVIDER_SECRET_NAMES
from lazycloud import Secret


def configure() -> None:
    values = {name: os.environ[name] for name in PROVIDER_SECRET_NAMES}
    for name, value in values.items():
        Secret(name).set(value)


if __name__ == "__main__":
    configure()
