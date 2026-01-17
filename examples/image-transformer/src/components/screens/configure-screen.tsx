import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { StyleSelector, ImagePreview } from "@/components/image-transformer";
import { Loader2, RotateCcw, Wand2 } from "lucide-react";
import { PageLayout } from "./header";
import type { StyleKey } from "@/lib/replicate";

interface ConfigureScreenProps {
  imageData: string;
  style: StyleKey;
  prompt: string;
  loading: boolean;
  error: string | null;
  onStyleChange: (style: StyleKey) => void;
  onPromptChange: (prompt: string) => void;
  onTransform: () => void;
  onReset: () => void;
}

export function ConfigureScreen({
  imageData,
  style,
  prompt,
  loading,
  error,
  onStyleChange,
  onPromptChange,
  onTransform,
  onReset,
}: ConfigureScreenProps) {
  return (
    <PageLayout
      maxWidth="7xl"
      action={
        <Button variant="ghost" onClick={onReset}>
          <RotateCcw className="h-4 w-4" />
          Start Over
        </Button>
      }
    >
      <div className="flex flex-col lg:flex-row gap-8">
        {/* Left: Image preview */}
        <div className="flex-1 min-w-0">
          <Label className="text-base mb-3 block">Your Image</Label>
          <ImagePreview src={imageData} alt="Selected image" onRemove={onReset} />
        </div>

        {/* Right: Options */}
        <div className="w-full lg:w-[360px] shrink-0 space-y-6">
          <div>
            <Label className="text-base mb-3 block">Choose a Style</Label>
            <StyleSelector value={style} onChange={onStyleChange} />
          </div>

          <div>
            <Label htmlFor="prompt" className="text-base mb-3 block">
              Custom Instructions
              <span className="text-muted-foreground font-normal ml-2">(optional)</span>
            </Label>
            <Textarea
              id="prompt"
              value={prompt}
              onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) =>
                onPromptChange(e.target.value)
              }
              placeholder="Add specific details or adjustments..."
              className="h-24 resize-none overflow-y-auto"
            />
            <p className="text-xs text-muted-foreground mt-2">
              E.g., "make it more colorful" or "add a sunset background"
            </p>
          </div>

          {error && (
            <div className="rounded-lg border border-destructive bg-destructive/10 px-4 py-3">
              <p className="text-sm text-destructive font-medium">{error}</p>
            </div>
          )}

          <Button
            onClick={onTransform}
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
    </PageLayout>
  );
}
