import { useEffect, useRef, useState } from "react";
import { ArrowUpRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EXAMPLES_URL } from "@/lib/env";
import { GetStartedButton } from "./MarketingPrimitives";
import type { createFlightScene } from "./flightScene";
import "./spaceship.css";

type Flight = ReturnType<typeof createFlightScene>;

export function SpaceshipHero() {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    const target = canvas.current;
    if (!target) return;
    let disposed = false;
    let instance: Flight | undefined;
    void import("./flightScene")
      .then(({ createFlightScene }) => {
        if (disposed) return;
        return createFlightScene(target);
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
        console.error("Spaceship animation could not load", cause);
        if (!disposed) setError(true);
      });
    return () => {
      disposed = true;
      instance?.dispose();
    };
  }, []);

  return (
    <section className="spaceship-hero" aria-labelledby="flight-headline">
      <div className="flight-copy">
        <h1 id="flight-headline">
          Ship as fast
          <br />
          <span>as you develop.</span>
        </h1>
        <p>Run your Python apps, APIs, and background jobs in the cloud.</p>
        <div className="flight-actions">
          <GetStartedButton />
          <Button asChild size="lg" variant="secondary" className="marketing-button-link">
            <a href={`${EXAMPLES_URL}/index`}>
              Explore examples <ArrowUpRight aria-hidden="true" />
            </a>
          </Button>
        </div>
      </div>
      <div className="flight-stage">
        <canvas
          ref={canvas}
          className="flight-canvas"
          role="img"
          aria-label="An angular spacecraft assembles on a circular launch platform, lifts off, and orbits an abstract globe."
        />
        {!ready && (
          <p className="flight-loading" role="status">
            {error
              ? "The 3D animation could not load. Please check that WebGL is enabled and reload."
              : "Preparing for liftoff…"}
          </p>
        )}
      </div>
    </section>
  );
}
