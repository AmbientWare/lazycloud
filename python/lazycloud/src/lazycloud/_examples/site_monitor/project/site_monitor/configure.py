"""Store the OpenAI API key and the Slack webhook URL as workspace secrets."""

from getpass import getpass

from site_monitor.app import OPENAI_API_KEY, SLACK_WEBHOOK_URL


def main() -> None:
    openai_key = getpass("OpenAI API key: ").strip()
    webhook_url = getpass("Slack incoming webhook URL: ").strip()
    if not webhook_url.startswith("https://hooks.slack.com/"):
        raise SystemExit("A Slack incoming webhook URL starts with https://hooks.slack.com/")
    OPENAI_API_KEY.set(openai_key)
    SLACK_WEBHOOK_URL.set(webhook_url)
    print(f"Stored {OPENAI_API_KEY.name} and {SLACK_WEBHOOK_URL.name}.")


if __name__ == "__main__":
    main()
