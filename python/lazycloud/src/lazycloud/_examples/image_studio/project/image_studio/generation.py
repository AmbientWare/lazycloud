"""What a user can ask the studio for, and the limits on each request."""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_PROMPT_CHARS = 500
MAX_IMAGES_PER_JOB = 4
# FLUX.2 [klein] is distilled to four denoising steps.
STEPS_PER_IMAGE = 4


class Style(StrEnum):
    NONE = "none"
    PHOTO = "photo"
    CINEMATIC = "cinematic"
    ILLUSTRATION = "illustration"
    ANIME = "anime"
    WATERCOLOR = "watercolor"
    PIXEL_ART = "pixel-art"


STYLE_PHRASES: dict[Style, str] = {
    Style.NONE: "",
    Style.PHOTO: "natural light photograph, 35mm lens, sharp focus, realistic detail",
    Style.CINEMATIC: "cinematic film still, dramatic lighting, shallow depth of field",
    Style.ILLUSTRATION: "clean digital illustration, bold shapes, flat colors",
    Style.ANIME: "anime key visual, cel shading, expressive line art",
    Style.WATERCOLOR: "loose watercolor painting on textured paper, soft washes",
    Style.PIXEL_ART: "16-bit pixel art, limited palette, crisp pixels",
}


class Aspect(StrEnum):
    SQUARE = "square"
    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"


# Width and height in pixels: multiples of 16, about one megapixel each.
ASPECT_SIZES: dict[Aspect, tuple[int, int]] = {
    Aspect.SQUARE: (1024, 1024),
    Aspect.PORTRAIT: (832, 1216),
    Aspect.LANDSCAPE: (1216, 832),
}


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_PROMPT_CHARS)
    ]
    style: Style = Style.NONE
    aspect: Aspect = Aspect.SQUARE
    count: Annotated[int, Field(ge=1, le=MAX_IMAGES_PER_JOB)] = 1
    seed: Annotated[int | None, Field(ge=0, lt=2**32)] = None

    def full_prompt(self) -> str:
        phrase = STYLE_PHRASES[self.style]
        return f"{self.prompt}, {phrase}" if phrase else self.prompt
