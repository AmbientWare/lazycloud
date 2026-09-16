import { useEffect, useRef, useState } from "react";
import { ArrowUpRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EXAMPLES_URL } from "@/lib/env";
import { GetStartedButton, MarketingHero } from "./MarketingPrimitives";
import "./cloudHero.css";

export function CloudHero() {
  const canvas = useRef<HTMLCanvasElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    const target = canvas.current;
    const frame = stage.current;
    if (!target || !frame) return;
    let disposed = false;
    let instance: { dispose: () => void } | undefined;
    void import("./cloudMascotScene")
      .then(({ createCloudMascotScene }) => {
        if (disposed) return;
        instance = createCloudMascotScene(target, frame);
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
    <MarketingHero className="cloud-hero">
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
        <p>
          Your agents build the application. LazyCloud makes deploying and running it just as
          programmable.
        </p>
        <div className="cloud-actions">
          <GetStartedButton />
          <Button asChild size="lg" variant="secondary" className="marketing-button-link">
            <a href={`${EXAMPLES_URL}/index`}>
              Explore examples <ArrowUpRight aria-hidden="true" />
            </a>
          </Button>
        </div>
      </div>
      <div
        ref={stage}
        className="cloud-stage"
        role="img"
        aria-label="A soft blue cloud friend rendered in ASCII. Its eyes blink and follow your cursor as it floats."
      >
        <canvas ref={canvas} aria-hidden="true" />
        {error && (
          <p className="cloud-error" role="status">
            The cloud could not load. Please reload the page.
          </p>
        )}
      </div>
    </MarketingHero>
  );
}
