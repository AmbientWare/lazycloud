import Replicate from "replicate";

// Validate environment variable at module load
if (!process.env.REPLICATE_API_TOKEN) {
  console.warn("Warning: REPLICATE_API_TOKEN environment variable is not set");
}

const replicate = new Replicate({
  auth: process.env.REPLICATE_API_TOKEN,
});

export const STYLES = {
  ghibli: {
    name: "Studio Ghibli",
    prompt: "in the style of studio ghibli anime, soft colors, hand-drawn",
    estimatedTime: "15-25 seconds",
  },
  pixar: {
    name: "Pixar 3D",
    prompt: "as a pixar 3d animated character, smooth render, vibrant colors",
    estimatedTime: "15-25 seconds",
  },
  watercolor: {
    name: "Watercolor",
    prompt: "as a watercolor painting, soft edges, artistic brushstrokes",
    estimatedTime: "10-20 seconds",
  },
  comic: {
    name: "Comic Book",
    prompt: "as a comic book illustration, bold lines, cel shading",
    estimatedTime: "10-20 seconds",
  },
  pixel: {
    name: "Pixel Art",
    prompt: "as pixel art, 16-bit style, retro game aesthetic",
    estimatedTime: "10-20 seconds",
  },
} as const;

export type StyleKey = keyof typeof STYLES;

// Validate image URL is accessible
export async function validateImageUrl(url: string): Promise<{ valid: boolean; error?: string }> {
  try {
    // Basic URL validation
    const parsedUrl = new URL(url);
    if (!["http:", "https:"].includes(parsedUrl.protocol)) {
      return { valid: false, error: "URL must use HTTP or HTTPS protocol" };
    }

    // Check if URL points to an image
    const response = await fetch(url, { method: "HEAD", signal: AbortSignal.timeout(10000) });

    if (!response.ok) {
      return { valid: false, error: `Unable to access image (HTTP ${response.status})` };
    }

    const contentType = response.headers.get("content-type");
    if (contentType && !contentType.startsWith("image/")) {
      return { valid: false, error: "URL does not point to an image file" };
    }

    return { valid: true };
  } catch (error) {
    if (error instanceof Error) {
      if (error.name === "AbortError" || error.name === "TimeoutError") {
        return { valid: false, error: "Request timed out - image URL may be slow or inaccessible" };
      }
      return { valid: false, error: `Invalid URL: ${error.message}` };
    }
    return { valid: false, error: "Invalid image URL" };
  }
}

// Timeout wrapper for promises
function withTimeout<T>(promise: Promise<T>, timeoutMs: number, message: string): Promise<T> {
  return Promise.race([
    promise,
    new Promise<never>((_, reject) => {
      setTimeout(() => reject(new Error(message)), timeoutMs);
    }),
  ]);
}

export async function transformImage(
  imageUrl: string,
  style: StyleKey,
  customPrompt?: string
): Promise<string> {
  // Validate image URL first
  const validation = await validateImageUrl(imageUrl);
  if (!validation.valid) {
    throw new Error(validation.error || "Invalid image URL");
  }

  const styleConfig = STYLES[style];
  const prompt = customPrompt
    ? `${customPrompt}, ${styleConfig.prompt}`
    : `Transform this image ${styleConfig.prompt}`;

  // 60 second timeout for transformation
  const TRANSFORM_TIMEOUT = 60000;

  const output = await withTimeout(
    replicate.run("black-forest-labs/flux-kontext-dev", {
      input: {
        prompt,
        image: imageUrl,
        guidance_scale: 7.5,
        num_inference_steps: 28,
      },
    }),
    TRANSFORM_TIMEOUT,
    "Transformation timed out. Please try again or use a different image."
  );

  // Output is typically an array with the image URL
  if (Array.isArray(output) && output.length > 0) {
    return output[0] as string;
  }

  throw new Error("No output from model - transformation may have failed");
}
