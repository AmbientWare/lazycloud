import { useEffect, useRef, useState } from "react";
import { MarketingCard } from "./MarketingPrimitives";
import type { ParityKind } from "./parityScene";

function ParityFigure({ kind, description }: { kind: ParityKind; description: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const target = canvas.current;
    if (!target) return;
    let disposed = false;
    let instance: { dispose: () => void } | undefined;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        observer.disconnect();
        void import("./parityScene")
          .then(({ createParityScene }) => {
            if (!disposed) instance = createParityScene(target, kind);
          })
          .catch((cause: unknown) => {
            console.error("Run mode illustration could not load", cause);
            if (!disposed) setError(true);
          });
      },
      { rootMargin: "100px" },
    );
    observer.observe(target);
    return () => {
      disposed = true;
      observer.disconnect();
      instance?.dispose();
    };
  }, [kind]);
  return (
    <MarketingCard className="relative aspect-[2/1] w-full" role="img" aria-label={description}>
      <canvas ref={canvas} className="block size-full" aria-hidden="true" />
      {error && (
        <p className="absolute inset-x-4 top-1/2 text-center text-xs text-muted-foreground">
          The illustration could not load. Please reload the page.
        </p>
      )}
    </MarketingCard>
  );
}

export function LocalPlate() {
  return (
    <ParityFigure
      kind="local"
      description="A blue code block on an open laptop, ready for local development."
    />
  );
}

export function CloudPlate() {
  return (
    <ParityFigure
      kind="cloud"
      description="The same blue code block runs on a cloud compute chip."
    />
  );
}

export function ProductionPlate() {
  return (
    <ParityFigure
      kind="production"
      description="Blue code blocks connect across three deployed services."
    />
  );
}
