"""Ask an OpenAI model to pick chapters and highlights from a transcript."""

from openai import OpenAI

from video_highlights.highlights import MAX_CLIP_SECONDS, MIN_CLIP_SECONDS, HighlightPlan
from video_highlights.transcript import Transcript

INSTRUCTIONS = """\
You turn long videos into short highlight clips. The input is a transcript with
one segment per line as [start-end] text, in seconds.

Pick up to {max_highlights} highlights. Each one stands on its own: a complete
point, story, answer or joke that makes sense without the rest of the video.
Each lasts {min_seconds:.0f} to {max_seconds:.0f} seconds, starts at a segment start and
ends at a segment end. Highlights do not overlap. List the best one first.
Give each a short, specific title and one sentence on why it works as a clip.

Split the video into chapters where the topic changes. The first chapter starts
at 0. Chapter titles are a few words each.

Summarize the video in two sentences."""


class ModelRefused(RuntimeError):
    """The model answered without a plan, for example by refusing."""


def draft_plan(transcript: Transcript, model: str, max_highlights: int) -> HighlightPlan:
    # OPENAI_API_KEY comes from the workspace secret the function lists.
    client = OpenAI(timeout=300.0)
    response = client.responses.parse(
        model=model,
        instructions=INSTRUCTIONS.format(
            max_highlights=max_highlights,
            min_seconds=MIN_CLIP_SECONDS,
            max_seconds=MAX_CLIP_SECONDS,
        ),
        input=transcript.timed_text(),
        text_format=HighlightPlan,
    )
    if response.output_parsed is None:
        raise ModelRefused(f"{model} returned no highlight plan")
    return response.output_parsed
