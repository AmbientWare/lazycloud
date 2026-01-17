import { Button } from "@/components/ui/button";
import { ComparisonSlider, StyleSelector } from "@/components/image-transformer";
import { Download, RotateCcw, ArrowLeft } from "lucide-react";
import { PageLayout } from "./header";
import { STYLES, type StyleKey } from "@/lib/replicate";

interface ResultScreenProps {
  result: { originalUrl: string; resultUrl: string };
  style: StyleKey;
  onStyleChange: (style: StyleKey) => void;
  onTryAnotherStyle: () => void;
  onReset: () => void;
}

export function ResultScreen({
  result,
  style,
  onStyleChange,
  onTryAnotherStyle,
  onReset,
}: ResultScreenProps) {
  return (
    <PageLayout
      maxWidth="4xl"
      action={
        <Button variant="ghost" onClick={onReset}>
          <RotateCcw className="h-4 w-4" />
          Start Over
        </Button>
      }
    >
      <div className="mb-6">
        <h2 className="text-2xl font-bold mb-2">Transformation Complete</h2>
        <p className="text-muted-foreground">
          Drag the slider to compare the original and {STYLES[style].name} versions
        </p>
      </div>

      <ComparisonSlider
        originalImage={result.originalUrl}
        transformedImage={result.resultUrl}
        originalLabel="Original"
        transformedLabel={STYLES[style].name}
        className="shadow-2xl"
      />

      <div className="flex flex-col sm:flex-row gap-3 mt-8">
        <Button variant="outline" onClick={onTryAnotherStyle} className="flex-1">
          <ArrowLeft className="h-4 w-4" />
          Try Another Style
        </Button>
        <Button asChild className="flex-1">
          <a href={result.resultUrl} download target="_blank" rel="noopener noreferrer">
            <Download className="h-4 w-4" />
            Download Result
          </a>
        </Button>
      </div>

      <div className="mt-8 p-6 rounded-xl border bg-card">
        <p className="text-sm font-medium mb-4">Quick transform to another style</p>
        <StyleSelector
          value={style}
          onChange={(newStyle) => {
            onStyleChange(newStyle);
            onTryAnotherStyle();
          }}
          variant="compact"
        />
      </div>
    </PageLayout>
  );
}
