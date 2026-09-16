import { useEffect, useRef, useState } from "react";
import { ArrowUpRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EXAMPLES_URL } from "@/lib/env";
import { GetStartedButton } from "./MarketingPrimitives";
import "./flightHero.css";

type SceneFactory = (canvas: HTMLCanvasElement, stage: HTMLDivElement) => { dispose: () => void };

export function FlightHero({
  loadScene,
  description,
}: {
  loadScene: () => Promise<SceneFactory>;
  description: string;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    const target = canvas.current;
    const frame = stage.current;
    if (!target || !frame) return;
    let disposed = false;
    let instance: ReturnType<SceneFactory> | undefined;
    void loadScene()
      .then((createScene) => {
        if (disposed) return;
        return createScene(target, frame);
      })
      .then((result) => {
        if (!result) return;
        if (disposed) {
          result.dispose();
          return;
        }
        instance = result;
        setReady(true);
      })
      .catch((cause: unknown) => {
        console.error("Flight animation could not load", cause);
        if (!disposed) setError(true);
      });
    return () => {
      disposed = true;
      instance?.dispose();
    };
  }, [loadScene]);

  return (
    <section className="flight-hero" aria-labelledby="flight-headline">
      <div className="flight-space" aria-hidden="true">
        <canvas ref={canvas} className="flight-canvas" />
      </div>
      <div className="flight-copy">
        <h1 id="flight-headline">
          Deploy as fast
          <br />
          <span>as you develop.</span>
        </h1>
        <p>Your agents build the software. LazyCloud runs it.</p>
        <div className="flight-actions">
          <GetStartedButton />
          <Button asChild size="lg" variant="secondary" className="marketing-button-link">
            <a href={`${EXAMPLES_URL}/index`}>
              Explore examples <ArrowUpRight aria-hidden="true" />
            </a>
          </Button>
        </div>
      </div>
      <div ref={stage} className="flight-stage" role="img" aria-label={description}>
        {!ready && (
          <p className="flight-loading" role="status">
            {error
              ? "The 3D animation could not load. Please check that WebGL is enabled and reload."
              : "Loading animation…"}
          </p>
        )}
      </div>
    </section>
  );
}
