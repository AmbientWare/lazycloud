import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, Pause, Play, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EXAMPLES_URL } from "@/lib/env";
import { GetStartedButton } from "./MarketingPrimitives";
import type { createFlightScene, FlightPhase } from "./flightScene";
import "./spaceship.css";

type Flight = Awaited<ReturnType<typeof createFlightScene>>;

export function SpaceshipHero() {
  const canvas = useRef<HTMLCanvasElement>(null);
  const flight = useRef<Flight | null>(null);
  const [phase, setPhase] = useState<FlightPhase>("Assembling");
  const [ready, setReady] = useState(false);
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    const target = canvas.current;
    if (!target) return;
    let disposed = false;
    let instance: Flight | undefined;
    void import("./flightScene")
      .then(({ createFlightScene }) => {
        if (disposed) return;
        return createFlightScene(target, (next) => {
          if (!disposed) setPhase(next);
        });
      })
      .then((result) => {
        if (!result) return;
        if (disposed) {
          result.dispose();
          return;
        }
        instance = result;
        flight.current = result;
        setReady(true);
      })
      .catch((cause: unknown) => {
        console.error("Spaceship animation could not load", cause);
        if (!disposed) setError(true);
      });
    return () => {
      disposed = true;
      instance?.dispose();
      flight.current = null;
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
      <div className="flight-footer">
        <span className="sr-only" role="status">
          {ready ? `Animation: ${phase}` : "Loading animation"}
        </span>
        <div className="flight-controls">
          <button
            type="button"
            disabled={!ready}
            aria-label="Replay launch"
            onClick={() => {
              flight.current?.replay();
              setPaused(false);
            }}
          >
            <RotateCcw size={14} /> <span>Replay</span>
          </button>
          <button
            type="button"
            disabled={!ready}
            aria-label={paused ? "Play animation" : "Pause animation"}
            onClick={() => {
              flight.current?.setPaused(!paused);
              setPaused(!paused);
            }}
          >
            {paused ? <Play size={14} /> : <Pause size={14} />}
          </button>
        </div>
      </div>
    </section>
  );
}
