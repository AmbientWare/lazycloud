"""Store the bucket keys and the OpenAI API key as workspace secrets."""

from getpass import getpass

from video_highlights.pipeline import BUCKET_ACCESS_KEY, BUCKET_SECRET_KEY, OPENAI_API_KEY

PROMPTS = (
    (BUCKET_ACCESS_KEY, "S3 access key ID for the video bucket: "),
    (BUCKET_SECRET_KEY, "S3 secret access key for the video bucket: "),
    (OPENAI_API_KEY, "OpenAI API key: "),
)


def main() -> None:
    for secret, prompt in PROMPTS:
        value = getpass(prompt).strip()
        if not value:
            raise SystemExit(f"{secret.name} needs a value")
        secret.set(value)
    print("Stored " + ", ".join(secret.name for secret, _ in PROMPTS) + ".")


if __name__ == "__main__":
    main()
