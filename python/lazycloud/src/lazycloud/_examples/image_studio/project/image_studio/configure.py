"""Store the studio key and download the model weights, once per workspace.

uv run python -m image_studio.configure
"""

from getpass import getpass

from image_studio.auth import MIN_KEY_LENGTH, WeakKeyError, check_key_strength
from image_studio.resources import access_key
from image_studio.weights import download_weights


def main() -> None:
    key = getpass(f"Choose a studio key of {MIN_KEY_LENGTH} or more characters: ")
    try:
        access_key.set(check_key_strength(key))
    except WeakKeyError as error:
        raise SystemExit(str(error)) from error
    print(f"Stored the key in the {access_key.name} secret.")
    print(download_weights.remote())


if __name__ == "__main__":
    main()
