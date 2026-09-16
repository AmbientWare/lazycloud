import { useEffect, useRef, useState } from "react";
import { ArrowUpRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EXAMPLES_URL } from "@/lib/env";
import { GetStartedButton, MarketingHero } from "./MarketingPrimitives";
import "./cloudHero.css";

export function CloudHero() {
  const canvas = useRef<HTMLCanvasElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const fireworks = useRef<HTMLCanvasElement>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    const target = canvas.current;
    const frame = stage.current;
    const sparks = fireworks.current;
    if (!target || !frame || !sparks) return;
    let disposed = false;
    let instance: { dispose: () => void } | undefined;
    void import("./cloudMascotScene")
      .then(({ createCloudMascotScene }) => {
        if (disposed) return;
        instance = createCloudMascotScene(target, frame, sparks);
      })
      .catch((cause: unknown) => {
        console.error("Hero animation could not load", cause);
        if (!disposed) setError(true);
      });
    return () => {
      disposed = true;
      instance?.dispose();
    };
  }, []);

  return (
    <MarketingHero
      className="cloud-hero"
      overlay={
        <canvas
          ref={fireworks}
          className="pointer-events-none absolute inset-0 z-[3] h-full w-full"
          data-marketing-overlay=""
          aria-hidden="true"
        />
      }
    >
      <div className="cloud-copy">
        <h1>
          Infrastructure
          <br />
          <em>
            for agentic
            <br />
            development.
          </em>
        </h1>
        <p>Building with agents is fast. Deploying with LazyCloud is too.</p>
        <div className="cloud-actions">
          <GetStartedButton />
          <Button asChild size="lg" variant="secondary" className="marketing-button-link">
            <a href={`${EXAMPLES_URL}/index`}>
              Explore examples <ArrowUpRight aria-hidden="true" />
            </a>
          </Button>
        </div>
      </div>
      <div ref={stage} className="cloud-stage">
        <canvas
          ref={canvas}
          role="img"
          aria-label="A blue ASCII cloud friend. It follows your cursor and laughs at fireworks when you click the hero background."
        />
        {error && (
          <p className="cloud-error" role="status">
            The cloud could not load. Please reload the page.
          </p>
        )}
      </div>
    </MarketingHero>
  );
}
