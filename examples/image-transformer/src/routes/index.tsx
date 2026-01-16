import { createFileRoute } from "@tanstack/react-router";
import { createServerFn } from "@tanstack/react-start";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dropzone,
  StyleSelector,
  ComparisonSlider,
  ImagePreview,
} from "@/components/image-transformer";
import { transformImage, STYLES, type StyleKey } from "@/lib/replicate";
import {
  Loader2,
  Download,
  RotateCcw,
  Wand2,
  ArrowLeft,
} from "lucide-react";

const transform = createServerFn({ method: "POST" })
  .inputValidator((data: { imageUrl: string; style: StyleKey; prompt?: string }) => data)
  .handler(async ({ data }) => {
    const { imageUrl, style, prompt } = data;
    const resultUrl = await transformImage(imageUrl, style, prompt);
    return { resultUrl };
  });

export const Route = createFileRoute("/")({
  component: Home,
});

function Home() {
  const [imageData, setImageData] = useState<string | null>(null);
  const [style, setStyle] = useState<StyleKey>("ghibli");
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleTransform = async () => {
    if (!imageData) return;

    setLoading(true);
    setError(null);

    try {
      const res = await transform({
        data: { imageUrl: imageData, style, prompt: prompt || undefined },
      });
      setResult(res.resultUrl);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Transformation failed");
    } finally {
      setLoading(false);
    }
  };

  const handleReset = () => {
    setImageData(null);
    setResult(null);
    setError(null);
    setPrompt("");
  };

  const handleNewTransform = () => {
    setResult(null);
  };

  // ==========================================
  // STEP 1: Upload Screen
  // ==========================================
  if (!imageData) {
    return (
      <div className="min-h-screen bg-gradient-to-b from-background to-muted/30">
        {/* Header */}
        <header className="border-b bg-background/80 backdrop-blur-sm sticky top-0 z-10">
          <div className="container max-w-5xl mx-auto px-4 h-16 flex items-center">
            <div className="flex items-center gap-3">
              <div className="h-9 w-9 rounded-lg bg-primary flex items-center justify-center">
                <Wand2 className="h-5 w-5 text-primary-foreground" />
              </div>
              <span className="font-semibold text-lg">Image Transformer</span>
            </div>
          </div>
        </header>

        {/* Main content */}
        <main className="container max-w-3xl mx-auto px-4 py-16">
          <div className="text-center mb-12">
            <h1 className="text-4xl font-bold tracking-tight mb-4">
              Transform your images with AI
            </h1>
            <p className="text-lg text-muted-foreground max-w-xl mx-auto">
              Upload any image and instantly convert it into beautiful artistic styles
              using state-of-the-art AI models.
            </p>
          </div>

          <Dropzone onImageSelect={setImageData} />

          {/* Features */}
          <div className="grid grid-cols-3 gap-6 mt-16">
            {[
              { title: "Multiple Styles", desc: "Ghibli, Pixar, Watercolor & more" },
              { title: "High Quality", desc: "Powered by advanced AI models" },
              { title: "Fast Results", desc: "Get results in seconds" },
            ].map((feature) => (
              <div key={feature.title} className="text-center">
                <p className="font-medium mb-1">{feature.title}</p>
                <p className="text-sm text-muted-foreground">{feature.desc}</p>
              </div>
            ))}
          </div>
        </main>
      </div>
    );
  }

  // ==========================================
  // STEP 3: Result Screen
  // ==========================================
  if (result) {
    return (
      <div className="min-h-screen bg-gradient-to-b from-background to-muted/30">
        {/* Header */}
        <header className="border-b bg-background/80 backdrop-blur-sm sticky top-0 z-10">
          <div className="container max-w-5xl mx-auto px-4 h-16 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="h-9 w-9 rounded-lg bg-primary flex items-center justify-center">
                <Wand2 className="h-5 w-5 text-primary-foreground" />
              </div>
              <span className="font-semibold text-lg">Image Transformer</span>
            </div>
            <Button variant="ghost" onClick={handleReset}>
              <RotateCcw className="h-4 w-4" />
              Start Over
            </Button>
          </div>
        </header>

        {/* Main content */}
        <main className="container max-w-4xl mx-auto px-4 py-8">
          <div className="mb-6">
            <h2 className="text-2xl font-bold mb-2">Transformation Complete</h2>
            <p className="text-muted-foreground">
              Drag the slider to compare the original and {STYLES[style].name} versions
            </p>
          </div>

          {/* Comparison slider */}
          <ComparisonSlider
            originalImage={imageData}
            transformedImage={result}
            originalLabel="Original"
            transformedLabel={STYLES[style].name}
            className="shadow-2xl"
          />

          {/* Actions */}
          <div className="flex flex-col sm:flex-row gap-3 mt-8">
            <Button variant="outline" onClick={handleNewTransform} className="flex-1">
              <ArrowLeft className="h-4 w-4" />
              Try Another Style
            </Button>
            <Button asChild className="flex-1">
              <a href={result} download target="_blank" rel="noopener noreferrer">
                <Download className="h-4 w-4" />
                Download Result
              </a>
            </Button>
          </div>

          {/* Quick style switcher */}
          <div className="mt-8 p-6 rounded-xl border bg-card">
            <p className="text-sm font-medium mb-4">Quick transform to another style</p>
            <StyleSelector
              value={style}
              onChange={(newStyle) => {
                setStyle(newStyle);
                setResult(null);
              }}
              variant="compact"
            />
          </div>
        </main>
      </div>
    );
  }

  // ==========================================
  // STEP 2: Configure Screen
  // ==========================================
  return (
    <div className="min-h-screen bg-gradient-to-b from-background to-muted/30">
      {/* Header */}
      <header className="border-b bg-background/80 backdrop-blur-sm sticky top-0 z-10">
        <div className="container max-w-5xl mx-auto px-4 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="h-9 w-9 rounded-lg bg-primary flex items-center justify-center">
              <Wand2 className="h-5 w-5 text-primary-foreground" />
            </div>
            <span className="font-semibold text-lg">Image Transformer</span>
          </div>
          <Button variant="ghost" onClick={handleReset}>
            <RotateCcw className="h-4 w-4" />
            Start Over
          </Button>
        </div>
      </header>

      {/* Main content */}
      <main className="container max-w-7xl mx-auto px-4 py-8">
        <div className="flex flex-col lg:flex-row gap-8">
          {/* Left: Image preview */}
          <div className="flex-1 min-w-0">
            <Label className="text-base mb-3 block">Your Image</Label>
            <ImagePreview
              src={imageData}
              alt="Selected image"
              onRemove={handleReset}
            />
          </div>

          {/* Right: Options */}
          <div className="w-full lg:w-[360px] shrink-0 space-y-6">
            {/* Style selection */}
            <div>
              <Label className="text-base mb-3 block">Choose a Style</Label>
              <StyleSelector value={style} onChange={setStyle} />
            </div>

            {/* Custom prompt */}
            <div>
              <Label htmlFor="prompt" className="text-base mb-3 block">
                Custom Instructions
                <span className="text-muted-foreground font-normal ml-2">(optional)</span>
              </Label>
              <Textarea
                id="prompt"
                value={prompt}
                onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setPrompt(e.target.value)}
                placeholder="Add specific details or adjustments..."
                className="h-24 resize-none overflow-y-auto"
              />
              <p className="text-xs text-muted-foreground mt-2">
                E.g., "make it more colorful" or "add a sunset background"
              </p>
            </div>

            {/* Error message */}
            {error && (
              <div className="rounded-lg border border-destructive bg-destructive/10 px-4 py-3">
                <p className="text-sm text-destructive font-medium">{error}</p>
              </div>
            )}

            {/* Transform button */}
            <Button
              onClick={handleTransform}
              disabled={loading}
              size="lg"
              className="w-full h-12 text-base"
            >
              {loading ? (
                <>
                  <Loader2 className="h-5 w-5 animate-spin" />
                  Transforming...
                </>
              ) : (
                <>
                  <Wand2 className="h-5 w-5" />
                  Transform Image
                </>
              )}
            </Button>

            {loading && (
              <p className="text-sm text-muted-foreground text-center">
                This usually takes 10-30 seconds...
              </p>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}

