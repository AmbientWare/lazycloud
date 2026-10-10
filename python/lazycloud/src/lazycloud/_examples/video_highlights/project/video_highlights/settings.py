"""Values to set before deploying: your bucket and the app that hears about runs."""

# The S3 bucket that holds your videos, mounted read-only.
BUCKET_NAME = "my-video-bucket"
BUCKET_REGION = "us-east-1"

# Receives a signed POST when a run is retried or finishes. None sends nothing.
CALLBACK_URL: str | None = None

# Any OpenAI model that supports structured outputs; each run can override it.
OPENAI_MODEL = "gpt-5.4-mini"
