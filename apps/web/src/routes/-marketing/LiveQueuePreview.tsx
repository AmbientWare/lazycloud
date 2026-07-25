import { useEffect, useState } from "react";

import { useReducedMotion } from "./useReducedMotion";
import { usePreviewActivity } from "./usePreviewActivity";

/* Queue telemetry models one operational story from a deterministic sample
   stream: ingress surges, the autoscaler observes sustained depth, workers
   come online after a short delay, processing overtakes ingress, and the queue
   drains before idle workers scale down. Logical samples represent two seconds
   even though the accelerated preview advances faster on the landing page. */

const CYCLE = 64;
const WINDOW = 31;
const STEP = 14;
const WIDTH = (WINDOW - 1) * STEP;
const CHART_HEIGHT = 168;
const TICK_MS = 450;
const START_TICK = 10;
const REDUCED_MOTION_TICK = 41;

const MAX_WORKERS = 8;
const TARGET_TASKS_PER_WORKER = 7;
const PROCESSED_PER_WORKER = 3;
const SCALE_EVALUATION_SAMPLES = 3;

type Sample = {
  depth: number;
  workers: number;
  targetWorkers: number;
  processing: number;
};

const EMPTY_SAMPLE: Sample = {
  depth: 0,
  workers: 0,
  targetWorkers: 0,
  processing: 0,
};

/* Stable pseudo-noise keeps the trace organic without changing SSR output. */
function noise(index: number, salt: number) {
  const value = Math.sin(index * 12.9898 + salt * 78.233) * 43758.5453;
  return value - Math.floor(value);
}

function burstAt(index: number, start: number, end: number, peak: number) {
  if (index < start || index > end) return 0;
  const progress = (index - start) / (end - start);
  const envelope = Math.sin(progress * Math.PI) * peak;
  return Math.max(0, Math.round(1 + envelope + (noise(index, peak) - 0.5) * 2));
}

function arrivalsAt(index: number) {
  return burstAt(index, 4, 18, 8) + burstAt(index, 32, 44, 5);
}

const SAMPLES: Sample[] = (() => {
  const samples: Sample[] = [];
  let depth = 0;
  let workers = 0;
  let targetWorkers = 0;
  let idleSamples = 0;

  for (let index = 0; index < CYCLE; index += 1) {
    const arrivals = arrivalsAt(index);
    const queuedBeforeProcessing = depth + arrivals;
    const processing = Math.min(queuedBeforeProcessing, workers * 2);
    const availableCapacity = workers * PROCESSED_PER_WORKER;
    const utilization = 0.78 + noise(index, 3) * 0.18;
    const completed = Math.min(
      queuedBeforeProcessing,
      Math.max(0, Math.round(availableCapacity * utilization)),
    );
    depth = queuedBeforeProcessing - completed;
    idleSamples = depth === 0 ? idleSamples + 1 : 0;

    /* The control loop evaluates periodically. Scale-out adds one ready worker
       per interval; scale-down waits through an idle grace period. Processing
       for this sample used the previous worker count, preserving startup lag. */
    if (index % SCALE_EVALUATION_SAMPLES === 0) {
      targetWorkers = Math.min(
        MAX_WORKERS,
        Math.ceil(depth / TARGET_TASKS_PER_WORKER),
      );
    }
    if (workers < targetWorkers && index % 2 === 1) {
      workers += 1;
    } else if (
      workers > targetWorkers &&
      idleSamples >= 5 &&
      index % SCALE_EVALUATION_SAMPLES === 0
    ) {
      workers -= 1;
    }

    samples.push({ depth, workers, targetWorkers, processing });
  }
  return samples;
})();

const TASK_CEILING =
  Math.ceil(
    Math.max(
      ...SAMPLES.flatMap((sample) => [sample.depth, sample.processing]),
    ) / 10,
  ) * 10;

function plotY(value: number, ceiling: number, height: number) {
  const top = 5;
  const bottom = height - 4;
  return bottom - (value / ceiling) * (bottom - top);
}

function tracePath(
  samples: Sample[],
  value: (sample: Sample) => number,
  ceiling: number,
  height: number,
) {
  return samples
    .map((sample, index) => {
      const command = index === 0 ? "M" : "L";
      return `${command}${index * STEP} ${plotY(value(sample), ceiling, height)}`;
    })
    .join(" ");
}

export function LiveQueuePreview() {
  const reducedMotion = useReducedMotion();
  const { active, previewRef } = usePreviewActivity();
  const [tick, setTick] = useState(START_TICK);

  useEffect(() => {
    if (reducedMotion || !active) return;

    /* Selection owns the clock. Leaving this story unmounts the preview and
       clears the timer, so every reactivation begins from the same loaded
       operating frame and advances on the first interval. */
    const timer = window.setInterval(() => {
      setTick((current) => current + 1);
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, [active, reducedMotion]);

  const visibleTick = reducedMotion ? REDUCED_MOTION_TICK : tick;
  const cycleTick = ((visibleTick % CYCLE) + CYCLE) % CYCLE;
  const current = SAMPLES[cycleTick] ?? EMPTY_SAMPLE;

  /* Do not wrap the previous cycle into a fresh chart. Missing history is
     represented by empty samples until this activation has produced it. */
  const windowSamples = Array.from({ length: WINDOW }, (_, index) => {
    const sampleIndex = cycleTick - WINDOW + 1 + index;
    return sampleIndex < 0
      ? EMPTY_SAMPLE
      : (SAMPLES[sampleIndex] ?? EMPTY_SAMPLE);
  });

  const queuedPath = tracePath(
    windowSamples,
    (sample) => sample.depth,
    TASK_CEILING,
    CHART_HEIGHT,
  );
  const processingPath = tracePath(
    windowSamples,
    (sample) => sample.processing,
    TASK_CEILING,
    CHART_HEIGHT,
  );
  const workersPath = tracePath(
    windowSamples,
    (sample) => sample.workers,
    MAX_WORKERS,
    CHART_HEIGHT,
  );

  return (
    <div
      className="marketing-queue-preview flex min-h-0 flex-1 flex-col"
      data-animation-state={
        reducedMotion ? "settled" : active ? "running" : "paused"
      }
      ref={previewRef}
    >
      <div className="queue-metrics grid grid-cols-3 border-b border-border">
        <QueueMetric
          label="Queued tasks"
          value={`${current.depth}`}
          detail="waiting"
          tone="brand"
        />
        <QueueMetric
          label="Processing"
          value={`${current.processing}`}
          detail="tasks running"
          tone="warning"
        />
        <QueueMetric
          label="Workers online"
          value={`${current.workers}`}
          detail={`target ${current.targetWorkers} · max ${MAX_WORKERS}`}
          tone="positive"
        />
      </div>

      <div className="queue-chart">
        <div className="queue-chart-heading">
          <strong>Queue activity</strong>
          <span>tasks left · workers right · last 60 seconds</span>
        </div>
        <div className="queue-chart-canvas">
          <QueueAxis ceiling={TASK_CEILING} />
          <svg
            aria-label="Queued tasks, processing tasks, and online workers over the last 60 seconds"
            preserveAspectRatio="none"
            role="img"
            viewBox={`0 0 ${WIDTH} ${CHART_HEIGHT}`}
          >
            <ChartGrid ceiling={TASK_CEILING} height={CHART_HEIGHT} />
            <g className="queue-sample-shift" key={cycleTick}>
              <path className="queue-queued-line" d={queuedPath} />
              <path className="queue-processing-line" d={processingPath} />
              <path className="queue-workers-line" d={workersPath} />
            </g>
          </svg>
          <QueueAxis ceiling={MAX_WORKERS} workers />
        </div>
        <div className="queue-time-axis" aria-hidden="true">
          <span>60s ago</span>
          <span>30s</span>
          <span>now</span>
        </div>
      </div>
    </div>
  );
}

function QueueMetric({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail: string;
  tone: "brand" | "positive" | "warning";
}) {
  return (
    <div className={`queue-metric is-${tone}`}>
      <span className="queue-metric-label">
        <i aria-hidden="true" /> {label}
      </span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function QueueAxis({
  ceiling,
  workers = false,
}: {
  ceiling: number;
  workers?: boolean;
}) {
  return (
    <span
      className={`queue-y-axis ${workers ? "is-workers" : ""}`}
      aria-hidden="true"
    >
      <span>{ceiling}</span>
      <span>{ceiling / 2}</span>
      <span>0</span>
    </span>
  );
}

function ChartGrid({ ceiling, height }: { ceiling: number; height: number }) {
  return [ceiling, ceiling / 2, 0].map((value) => {
    const y = plotY(value, ceiling, height);
    return (
      <line
        className="queue-grid-line"
        key={value}
        x1={0}
        x2={WIDTH}
        y1={y}
        y2={y}
      />
    );
  });
}
