import { useEffect, useRef, useState } from "react";

import { useReducedMotion } from "./useReducedMotion";
import { usePreviewActivity } from "./usePreviewActivity";

/* Live application traffic is presented as one stacked step-area timeline. The
   total silhouette communicates request load, the bands preserve route mix,
   and every active mount starts from the same populated deterministic frame. */

const POINTS = 32;
const STEP = 16;
const WIDTH = (POINTS - 1) * STEP;
const HEIGHT = 180;
const PLOT_TOP = 14;
const PLOT_BOTTOM = 10;
const TICK_MS = 650;
const START_INDEX = 12;
const REQUESTS_PER_CONTAINER = 12;

const ENDPOINTS = [
  {
    route: "/review",
    tone: "is-review",
    base: 16,
    swing: 4.2,
    period: 5.2,
  },
  {
    route: "/generate",
    tone: "is-generate",
    base: 11,
    swing: 3.4,
    period: 6.7,
  },
  {
    route: "/status",
    tone: "is-status",
    base: 7,
    swing: 2.5,
    period: 8.1,
  },
] as const;

function noise(index: number, salt: number) {
  const value = Math.sin(index * 12.9898 + salt * 78.233) * 43758.5453;
  return value - Math.floor(value);
}

function boundedDrift(index: number, salt: number) {
  let value = 0;
  for (let step = Math.max(0, index - 24); step <= index; step += 1) {
    value = value * 0.72 + (noise(step, salt) - 0.5) * 1.35;
  }
  return value;
}

type Sample = number[];

function sampleAt(index: number): Sample {
  return ENDPOINTS.map((endpoint, series) =>
    Math.max(
      1,
      Math.round(
        endpoint.base +
          Math.sin(index / endpoint.period + series * 1.3) *
            endpoint.swing *
            0.36 +
          Math.sin(index / 2.1 + series * 0.8) * endpoint.swing * 0.16 +
          boundedDrift(index, series + 1) * endpoint.swing * 0.62,
      ),
    ),
  );
}

function initialSamples() {
  return Array.from({ length: POINTS + 1 }, (_, offset) =>
    sampleAt(START_INDEX + offset),
  );
}

function stepPath(values: number[], scale: number) {
  const y = (value: number) => HEIGHT - PLOT_BOTTOM - value * scale;
  let path = `M${-STEP} ${y(values[0] ?? 0)}`;
  values.forEach((value, index) => {
    path += ` H${(index - 1) * STEP} V${y(value)}`;
  });
  return `${path} H${WIDTH}`;
}

function areaPath(values: number[], scale: number) {
  return `${stepPath(values, scale)} V${HEIGHT} H${-STEP}Z`;
}

export function LiveEndpointChart() {
  const reducedMotion = useReducedMotion();
  const { active, previewRef } = usePreviewActivity();
  const [samples, setSamples] = useState<Sample[]>(initialSamples);
  const [tick, setTick] = useState(0);
  const nextIndex = useRef(START_INDEX + POINTS + 1);

  useEffect(() => {
    if (reducedMotion || !active) return;

    const timer = window.setInterval(() => {
      setSamples((current) => [
        ...current.slice(1),
        sampleAt(nextIndex.current++),
      ]);
      setTick((current) => current + 1);
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, [active, reducedMotion]);

  const bands = ENDPOINTS.map((_, series) =>
    samples.map((sample) =>
      sample.slice(0, series + 1).reduce((total, value) => total + value, 0),
    ),
  );
  const totals = bands.at(-1) ?? [];
  const axisMax = Math.ceil(Math.max(...totals) / 10) * 10;
  const scale = (HEIGHT - PLOT_TOP - PLOT_BOTTOM) / axisMax;
  const latest = samples.at(-2) ?? [0, 0, 0];
  const inFlight = latest.reduce((total, value) => total + value, 0);
  const containers = Math.max(1, Math.ceil(inFlight / REQUESTS_PER_CONTAINER));
  const capacity = containers * REQUESTS_PER_CONTAINER;
  const utilization = Math.round((inFlight / capacity) * 100);
  const routeMetrics = ENDPOINTS.map((endpoint, series) => {
    const active = latest[series] ?? 0;
    return {
      ...endpoint,
      active,
      latency: Math.round(76 + active * 3.4 + noise(tick, series + 8) * 12),
    };
  });
  return (
    <div
      className="marketing-endpoint-chart"
      data-animation-state={
        reducedMotion ? "settled" : active ? "running" : "paused"
      }
      ref={previewRef}
    >
      <div className="marketing-stat-grid endpoint-overview-stats">
        <div className="marketing-product-stat">
          <span>In flight</span>
          <strong>{inFlight}</strong>
        </div>
        <div className="marketing-product-stat">
          <span>Utilization</span>
          <strong>{utilization}%</strong>
        </div>
        <div className="marketing-product-stat">
          <span>Containers</span>
          <strong>{containers}</strong>
        </div>
      </div>
      <figure className="endpoint-plot">
        <figcaption className="endpoint-plot-caption">
          <strong>Live request load</strong>
          <span>Last 60 seconds</span>
        </figcaption>
        <div className="endpoint-chart-canvas">
          <div className="endpoint-y-axis" aria-hidden="true">
            <span>{axisMax}</span>
            <span>{axisMax / 2}</span>
            <span>0</span>
          </div>
          <div className="endpoint-plot-visual">
            <svg
              aria-label="Stacked live request load for /review, /generate, and /status over the last 60 seconds"
              preserveAspectRatio="none"
              role="img"
              viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            >
              {[PLOT_TOP, HEIGHT / 2, HEIGHT - PLOT_BOTTOM].map((y) => (
                <line
                  className="endpoint-grid-line"
                  key={y}
                  x1="0"
                  x2={WIDTH}
                  y1={y}
                  y2={y}
                />
              ))}
              <g className="endpoint-shift" key={tick}>
                {ENDPOINTS.map((endpoint, series) => ({ endpoint, series }))
                  .reverse()
                  .map(({ endpoint, series }) => (
                    <g className={endpoint.tone} key={endpoint.route}>
                      <path
                        className="endpoint-area"
                        d={areaPath(bands[series] ?? [], scale)}
                      />
                      <path
                        className="endpoint-line"
                        d={stepPath(bands[series] ?? [], scale)}
                      />
                    </g>
                  ))}
              </g>
            </svg>
          </div>
        </div>
        <div className="endpoint-time-axis" aria-hidden="true">
          <span>60s ago</span>
          <span>30s</span>
          <span>now</span>
        </div>
        <div className="endpoint-route-summaries">
          {routeMetrics.map(({ active, latency, route, tone }) => (
            <div className={`endpoint-route-summary ${tone}`} key={route}>
              <code className="endpoint-series-label">{route}</code>
              <span className="endpoint-series-metrics">
                <strong>{active} active</strong>
                <small>{latency} ms median</small>
              </span>
            </div>
          ))}
        </div>
      </figure>
    </div>
  );
}
