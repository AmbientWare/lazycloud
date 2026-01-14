import Replicate from "replicate";

const replicate = new Replicate({
  auth: process.env.REPLICATE_API_TOKEN,
});

export const STYLES = {
  ghibli: {
    name: "Studio Ghibli",
    prompt: "in the style of studio ghibli anime, soft colors, hand-drawn",
  },
  pixar: {
    name: "Pixar 3D",
    prompt: "as a pixar 3d animated character, smooth render, vibrant colors",
  },
  watercolor: {
    name: "Watercolor",
    prompt: "as a watercolor painting, soft edges, artistic brushstrokes",
  },
  comic: {
    name: "Comic Book",
    prompt: "as a comic book illustration, bold lines, cel shading",
  },
  pixel: {
    name: "Pixel Art",
    prompt: "as pixel art, 16-bit style, retro game aesthetic",
  },
} as const;

export type StyleKey = keyof typeof STYLES;

export async function transformImage(
  imageUrl: string,
  style: StyleKey,
  customPrompt?: string
): Promise<string> {
  const styleConfig = STYLES[style];
  const prompt = customPrompt
    ? `${customPrompt}, ${styleConfig.prompt}`
    : `Transform this image ${styleConfig.prompt}`;

  const output = await replicate.run(
    "black-forest-labs/flux-kontext-dev",
    {
      input: {
        prompt,
        image: imageUrl,
        guidance_scale: 7.5,
        num_inference_steps: 28,
      },
    }
  );

  // Output is typically an array with the image URL
  if (Array.isArray(output) && output.length > 0) {
    return output[0] as string;
  }

  throw new Error("No output from model");
}
