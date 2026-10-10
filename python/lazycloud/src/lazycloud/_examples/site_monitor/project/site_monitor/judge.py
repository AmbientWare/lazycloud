"""Ask an OpenAI model whether a page change matters and what it was."""

from openai import OpenAI

from site_monitor.models import Verdict, Watch

MODEL = "gpt-5-mini"
INSTRUCTIONS = """\
You review changes to a web page that someone monitors. In the diff, lines
starting with - were removed and lines starting with + were added. Decide
whether the reader would want to hear about the change, using their focus when
given. Rotating ads, dates, counters, reordering and small wording edits do not
matter. Summarize what changed in one or two plain sentences."""


class JudgeError(RuntimeError):
    pass


def judge_change(watch: Watch, diff: str) -> Verdict:
    client = OpenAI(timeout=60, max_retries=2)
    response = client.responses.parse(
        model=MODEL,
        instructions=INSTRUCTIONS,
        input=(
            f"Page: {watch.label or watch.url}\n"
            f"URL: {watch.url}\n"
            f"Focus: {watch.focus or 'any change to what the page says'}\n\n"
            f"Diff:\n{diff}"
        ),
        text_format=Verdict,
        reasoning={"effort": "minimal"},
    )
    if response.output_parsed is None:
        raise JudgeError(f"{MODEL} returned no verdict for {watch.url}")
    return response.output_parsed
