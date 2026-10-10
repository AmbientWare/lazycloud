"""Store the bucket keys and the OpenAI API key, and make sure a signing key exists."""

from getpass import getpass
from secrets import token_urlsafe

from lazycloud import Secret

from video_highlights.pipeline import BUCKET_ACCESS_KEY, BUCKET_SECRET_KEY, OPENAI_API_KEY
from video_highlights.receiver import SIGNING_KEY

PROMPTS = (
    (BUCKET_ACCESS_KEY, "S3 access key ID for the video bucket: "),
    (BUCKET_SECRET_KEY, "S3 secret access key for the video bucket: "),
    (OPENAI_API_KEY, "OpenAI API key: "),
)


def ensure_signing_key() -> None:
    """Create the callback signing key unless the workspace has one.

    LazyCloud creates it with the first callback, but the receiver lists it as
    a secret, and a deploy that names a missing secret is refused.
    """
    signing_key = Secret(SIGNING_KEY)
    try:
        signing_key.record()
    except RuntimeError:
        signing_key.create("whsec_" + token_urlsafe(32))


def main() -> None:
    for secret, prompt in PROMPTS:
        value = getpass(prompt).strip()
        if not value:
            raise SystemExit(f"{secret.name} needs a value")
        secret.set(value)
    ensure_signing_key()
    print("Stored " + ", ".join(secret.name for secret, _ in PROMPTS) + f". {SIGNING_KEY} is set.")


if __name__ == "__main__":
    main()
