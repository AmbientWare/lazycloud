import { useEffect, useState } from "react";

import { useReducedMotion } from "./useReducedMotion";
import { usePreviewActivity } from "./usePreviewActivity";

/* Live task timeline: a dependency-aware run replayed on a loop, so the graph
   shows Tasks starting as their upstream results land. The clock only advances
   after mount, so prerendered markup matches the first client render. */

const TOTAL_SECONDS = 30;
const TICK_MS = 70;
const TICK_SECONDS = 0.75;
const HOLD_SECONDS = 4;

type TimelineTask = {
  name: string;
  kind: string;
  depth: number;
  start: number;
  duration: number;
  resource: string;
  volume?: boolean;
};

const TASKS: TimelineTask[] = [
  {
    name: "checkout",
    kind: "function",
    depth: 0,
    start: 0,
    duration: 4,
    resource: "2 vCPU",
    volume: true,
  },
  {
    name: "test",
    kind: "function",
    depth: 1,
    start: 4,
    duration: 6,
    resource: "8 vCPU",
  },
  {
    name: "evaluate",
    kind: "function",
    depth: 2,
    start: 10,
    duration: 12,
    resource: "A100 · 40 GiB",
    volume: true,
  },
  {
    name: "security_scan",
    kind: "function",
    depth: 3,
    start: 22,
    duration: 5,
    resource: "L4 · 24 GiB",
    volume: true,
  },
  {
    name: "publish",
    kind: "function",
    depth: 4,
    start: 27,
    duration: 3,
    resource: "1 vCPU",
    volume: true,
  },
];

const AXIS = [0, 10, 20, 30];

function percent(value: number) {
  return `${(value / TOTAL_SECONDS) * 100}%`;
}

/* Elapsed readout: dash while queued, live seconds while running. */
function formatElapsed(elapsed: number, duration: number) {
  if (elapsed <= 0) return "—";
  if (elapsed >= duration) return `${duration}s`;
  return `${elapsed.toFixed(1)}s`;
}

function formatClock(seconds: number) {
  const bounded = Math.min(Math.floor(seconds), TOTAL_SECONDS);
  return `0:${String(bounded).padStart(2, "0")}`;
}

export function LiveTaskTimeline() {
  const reducedMotion = useReducedMotion();
  const { active, previewRef } = usePreviewActivity();
  const [clock, setClock] = useState(0);

  useEffect(() => {
    if (reducedMotion || !active) return;

    /* A newly selected story starts at zero. The panel owner unmounts this
       preview when it is inactive, so returning creates a fresh run. */
    const timer = window.setInterval(() => {
      setClock((current) =>
        current >= TOTAL_SECONDS + HOLD_SECONDS ? 0 : current + TICK_SECONDS,
      );
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, [active, reducedMotion]);

  const visibleClock = reducedMotion ? TOTAL_SECONDS : clock;

  const running = TASKS.filter(
    (task) =>
      visibleClock > task.start && visibleClock < task.start + task.duration,
  ).length;
  const completed = TASKS.filter(
    (task) => visibleClock >= task.start + task.duration,
  ).length;
  const progress = Math.min(
    100,
    Math.round((visibleClock / TOTAL_SECONDS) * 100),
  );
  const runState =
    visibleClock >= TOTAL_SECONDS
      ? "Completed"
      : visibleClock > 0
        ? "Running"
        : "Ready";

  return (
    <div
      className="marketing-timeline"
      data-animation-state={
        reducedMotion ? "settled" : active ? "running" : "paused"
      }
      ref={previewRef}
    >
      <div className="pipeline-run-summary">
        <code>Deployments / release_evals · production</code>
        <strong data-state={runState.toLowerCase()}>
          {runState} · {formatClock(visibleClock)}
        </strong>
      </div>
      <div className="pipeline-run-metrics">
        <div className="pipeline-run-metric">
          <span>Run progress</span>
          <strong>{progress}%</strong>
          <small>current execution</small>
        </div>
        <div className="pipeline-run-metric">
          <span>Completed</span>
          <strong>
            {completed}/{TASKS.length}
          </strong>
          <small>dependency-aware</small>
        </div>
        <div className="pipeline-run-metric">
          <span>Elapsed</span>
          <strong>{formatClock(visibleClock)}</strong>
          <small>30s expected</small>
        </div>
      </div>
      <div className="timeline-panel">
        <div className="timeline-head">
          <span>Task timeline</span>
          <code>
            {TASKS.length} tasks · {running} running
          </code>
        </div>
        <div className="timeline-axis" aria-hidden="true">
          {AXIS.map((mark) => (
            <span key={mark} style={{ left: percent(mark) }}>
              0:{String(mark).padStart(2, "0")}
            </span>
          ))}
        </div>
        <div className="timeline-rows">
          {TASKS.map((task) => {
            const elapsed = Math.min(
              Math.max(visibleClock - task.start, 0),
              task.duration,
            );
            const state =
              elapsed <= 0
                ? "queued"
                : elapsed < task.duration
                  ? "running"
                  : "complete";
            return (
              <div className="timeline-row" key={task.name}>
                <span
                  className="timeline-name"
                  style={{ paddingLeft: `${task.depth * 9}px` }}
                >
                  {task.depth > 0 ? <i className="timeline-branch">↳</i> : null}
                  {task.name}
                  <small>{task.kind}</small>
                </span>
                <span className="timeline-resource">
                  {task.resource}
                  {task.volume ? <b title="artifacts volume">◆</b> : null}
                </span>
                <span className="timeline-track">
                  <span
                    className={`timeline-bar is-${state}`}
                    style={{
                      left: percent(task.start),
                      width: percent(elapsed),
                    }}
                  />
                </span>
                <code className="timeline-duration">
                  {formatElapsed(elapsed, task.duration)}
                </code>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
