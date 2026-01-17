import { createFileRoute } from "@tanstack/react-router";
import { createServerFn } from "@tanstack/react-start";
import { useState } from "react";
import { transformImage, type StyleKey } from "@/lib/replicate";
import {
  createTransformation,
  completeTransformation,
  failTransformation,
} from "@/lib/db";
import { saveOriginalImage, saveTransformedImage, getImageAsDataUrl } from "@/lib/storage";
import { UploadScreen, ConfigureScreen, ResultScreen } from "@/components/screens";

const transform = createServerFn({ method: "POST" })
  .inputValidator(
    (data: { imageData: string; filename: string; style: StyleKey; prompt?: string }) => data
  )
  .handler(async ({ data }) => {
    const { imageData, filename, style, prompt } = data;
    const id = crypto.randomUUID();

    // Save original image to storage
    const originalPath = await saveOriginalImage(id, imageData);

    // Create database record
    createTransformation({
      id,
      original_filename: filename,
      original_path: originalPath,
      style,
      custom_prompt: prompt,
    });

    try {
      // Transform with Replicate
      const resultUrl = await transformImage(imageData, style, prompt);

      // Save transformed image to storage
      const transformedPath = await saveTransformedImage(id, resultUrl);

      // Update database record
      completeTransformation(id, transformedPath);

      // Return data URLs for immediate display
      const originalDataUrl = getImageAsDataUrl(originalPath);
      const transformedDataUrl = getImageAsDataUrl(transformedPath);

      return {
        id,
        resultUrl: transformedDataUrl || resultUrl,
        originalUrl: originalDataUrl || imageData,
      };
    } catch (error) {
      failTransformation(id);
      throw error;
    }
  });

export const Route = createFileRoute("/")({
  component: Home,
});

function Home() {
  const [imageData, setImageData] = useState<string | null>(null);
  const [filename, setFilename] = useState<string>("image");
  const [style, setStyle] = useState<StyleKey>("ghibli");
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ originalUrl: string; resultUrl: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleImageSelect = (dataUrl: string, name: string) => {
    setImageData(dataUrl);
    setFilename(name);
  };

  const handleTransform = async () => {
    if (!imageData) return;

    setLoading(true);
    setError(null);

    try {
      const res = await transform({
        data: { imageData, filename, style, prompt: prompt || undefined },
      });
      setResult({ originalUrl: res.originalUrl, resultUrl: res.resultUrl });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Transformation failed");
    } finally {
      setLoading(false);
    }
  };

  const handleReset = () => {
    setImageData(null);
    setFilename("image");
    setResult(null);
    setError(null);
    setPrompt("");
  };

  const handleNewTransform = () => {
    setResult(null);
  };

  // Upload screen
  if (!imageData) {
    return <UploadScreen onImageSelect={handleImageSelect} />;
  }

  // Result screen
  if (result) {
    return (
      <ResultScreen
        result={result}
        style={style}
        onStyleChange={setStyle}
        onTryAnotherStyle={handleNewTransform}
        onReset={handleReset}
      />
    );
  }

  // Configure screen
  return (
    <ConfigureScreen
      imageData={imageData}
      style={style}
      prompt={prompt}
      loading={loading}
      error={error}
      onStyleChange={setStyle}
      onPromptChange={setPrompt}
      onTransform={handleTransform}
      onReset={handleReset}
    />
  );
}
