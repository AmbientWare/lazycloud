import { useEffect, useState } from "react";

import { useReducedMotion } from "./useReducedMotion";
import { usePreviewActivity } from "./usePreviewActivity";

/* Live compute placement map: workloads landing on each capacity path the
   section describes — managed serverless (CPU only, no managed GPU fleet is
   implied), a connected AWS account, and self-hosted Linux machines — with
   live per-lane container counts and a shared named volume readout. Every
   number, the volume mount count, and the placement feed derive from the one
   LANES schedule, so nothing can disagree. The schedule is a fixed loop, the
   clock starts at a busy settled moment, and it only advances after mount, so
   prerendered markup matches the first client render. No Math.random /
   Date.now anywhere. */

const LOOP_SECONDS = 32;
const START_SECONDS = 11.5;
const TICK_MS = 110;
const TICK_SECONDS = 0.25;
const PLACING_SECONDS = 1.5;
/* Endpoints report monotonic uptime so they read as long-lived deployments
   while transient tasks loop around them. */
const BASE_UPTIME_SECONDS = 214;

type Assignment = {
  name: string;
  kind: string;
  resource: string;
  start: number;
  duration: number;
  volume?: boolean;
  persistent?: boolean;
};

type Slot = {
  key: string;
  machine?: string;
  assignments: Assignment[];
};

type Lane = {
  key: string;
  name: string;
  meta: string;
  target: string;
  slots: Slot[];
};

const LANES: Lane[] = [
  {
    key: "managed",
    name: "Managed serverless",
    meta: "default placement · no setup",
    target: "managed serverless",
    slots: [
      {
        key: "managed-0",
        assignments: [
          {
            name: "test-shard",
            kind: "function",
            resource: "2 vCPU",
            start: 0,
            duration: 12,
            volume: true,
          },
          {
            name: "build-index",
            kind: "function",
            resource: "4 vCPU",
            start: 18,
            duration: 13,
          },
        ],
      },
      {
        key: "managed-1",
        assignments: [
          /* The two managed slots overlap so the default path never reads
             "0 running" while still showing placing and idle transitions. */
          {
            name: "release-checks",
            kind: "function",
            resource: "4 vCPU",
            start: 4,
            duration: 15,
          },
          {
            name: "dependency-audit",
            kind: "function",
            resource: "1 vCPU",
            start: 23,
            duration: 9,
          },
        ],
      },
    ],
  },
  {
    key: "aws",
    name: "Connected AWS",
    meta: "workspace account · us-east-1",
    target: "connected aws",
    slots: [
      {
        key: "aws-0",
        assignments: [
          {
            name: "review-api",
            kind: "endpoint",
            resource: "A10G",
            start: 0,
            duration: LOOP_SECONDS,
            volume: true,
            persistent: true,
          },
        ],
      },
      {
        key: "aws-1",
        assignments: [
          {
            name: "evaluate",
            kind: "function",
            resource: "A100",
            start: 3,
            duration: 15,
            volume: true,
          },
          {
            name: "security-scan",
            kind: "function",
            resource: "L4",
            start: 22,
            duration: 8,
            volume: true,
          },
        ],
      },
    ],
  },
  {
    key: "metal",
    name: "Self-hosted Linux",
    meta: "machine join · 2 machines",
    target: "self-hosted",
    slots: [
      {
        key: "linux-01",
        machine: "linux-01",
        assignments: [
          {
            name: "integration-tests",
            kind: "function",
            resource: "16 vCPU",
            start: 1,
            duration: 10,
            volume: true,
          },
          {
            name: "benchmark",
            kind: "function",
            resource: "32 vCPU",
            start: 15,
            duration: 12,
            volume: true,
          },
        ],
      },
      {
        key: "linux-02",
        machine: "linux-02",
        assignments: [
          {
            name: "coding-agent",
            kind: "sandbox",
            resource: "4 vCPU",
            start: 5,
            duration: 21,
            volume: true,
          },
        ],
      },
    ],
  },
];

function activeAt(slot: Slot, clock: number): Assignment | undefined {
  return slot.assignments.find(
    (assignment) => clock >= assignment.start && clock < assignment.start + assignment.duration,
  );
}

function formatUptime(totalSeconds: number) {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = Math.floor(totalSeconds % 60);
  return `${minutes}m${String(seconds).padStart(2, "0")}s`;
}

/* Recent placements: transient starts ranked by wrapped distance behind the
   clock, so the feed always trails the map by construction. */
function recentPlacements(clock: number) {
  const placements = LANES.flatMap((lane) =>
    lane.slots.flatMap((slot) =>
      slot.assignments
        .filter((assignment) => !assignment.persistent)
        .map((assignment) => ({
          assignment,
          target: slot.machine ?? lane.target,
          ago: (clock - assignment.start + LOOP_SECONDS) % LOOP_SECONDS,
        })),
    ),
  );
  return placements.sort((a, b) => a.ago - b.ago).slice(0, 2);
}

export function LiveComputePreview() {
  const reducedMotion = useReducedMotion();
  const { active: previewActive, previewRef } = usePreviewActivity();
  const [ticks, setTicks] = useState(0);

  useEffect(() => {
    if (reducedMotion || !previewActive) return;

    const timer = window.setInterval(() => {
      setTicks((current) => current + 1);
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, [previewActive, reducedMotion]);

  const clock = (START_SECONDS + ticks * TICK_SECONDS) % LOOP_SECONDS;
  const uptime = BASE_UPTIME_SECONDS + START_SECONDS + ticks * TICK_SECONDS;

  const active = LANES.map((lane) => lane.slots.map((slot) => activeAt(slot, clock)));
  const laneCounts = active.map(
    (slots) => slots.filter((assignment) => assignment !== undefined).length,
  );
  const running = laneCounts.reduce((total, count) => total + count, 0);
  const volumeMounts = active.flat().filter((assignment) => assignment?.volume).length;

  return (
    <div
      role="group"
      aria-label="Live placement map of workloads on managed serverless capacity, connected AWS, and self-hosted Linux machines"
      className="marketing-compute-preview flex min-h-0 flex-1 flex-col gap-2 p-3"
      data-animation-state={reducedMotion ? "settled" : previewActive ? "running" : "paused"}
      ref={previewRef}
    >
      <div className="compute-overview grid grid-cols-4 gap-1.5">
        {[
          { label: "Containers", value: String(running) },
          { label: "Managed", value: String(laneCounts[0]) },
          { label: "AWS", value: String(laneCounts[1]) },
          { label: "Self-hosted", value: String(laneCounts[2]) },
        ].map((stat) => (
          <div
            key={stat.label}
            className="compute-overview-stat rounded-md border border-[var(--border)] bg-[var(--background)]/40 px-2 py-1.5"
          >
            <span className="block truncate font-mono text-[8px] tracking-[0.12em] text-[var(--muted-foreground)] uppercase">
              {stat.label}
            </span>
            <strong className="text-[13px] font-semibold text-[var(--foreground)]">
              {stat.value}
            </strong>
          </div>
        ))}
      </div>

      {LANES.map((lane, laneIndex) => (
        <div
          key={lane.key}
          className="compute-lane flex min-h-0 flex-1 flex-col gap-1.5 rounded-lg border border-[var(--border)] bg-[var(--background)]/30 p-2"
        >
          <div className="compute-lane-heading flex items-baseline justify-between gap-2 font-mono text-[8px]">
            <span className="truncate text-[var(--foreground)]">
              {lane.name}
              <span className="text-[var(--muted-foreground)]"> · {lane.meta}</span>
            </span>
            <code className="shrink-0 text-[var(--positive)]">{laneCounts[laneIndex]} running</code>
          </div>
          <div className="compute-slots grid min-h-0 flex-1 grid-cols-2 gap-1.5">
            {lane.slots.map((slot, slotIndex) => {
              const assignment = active[laneIndex][slotIndex];
              if (assignment === undefined) {
                return (
                  /* Dimmed at the frame, not with `opacity`. Fading the whole
                     slot took its two labels down to 3.65:1 — an idle slot is
                     quieter, not less readable. */
                  <div
                    key={slot.key}
                    className="compute-slot flex min-h-[34px] flex-col justify-between rounded-md border border-dashed border-[var(--border)]/60 px-2 py-1.5"
                  >
                    <span className="truncate font-mono text-[8px] text-[var(--muted-foreground)]">
                      {slot.machine ?? "capacity"}
                    </span>
                    <span className="font-mono text-[8px] text-[var(--muted-foreground)]">
                      idle
                    </span>
                  </div>
                );
              }

              const elapsed = clock - assignment.start;
              const placing = !assignment.persistent && elapsed < PLACING_SECONDS;
              const elapsedText = assignment.persistent
                ? `up ${formatUptime(uptime)}`
                : `${elapsed.toFixed(1)}s`;
              return (
                <div
                  key={slot.key}
                  className="compute-slot flex min-h-[34px] flex-col justify-between overflow-hidden rounded-md border border-[var(--border)] bg-[var(--card)] px-2 py-1.5"
                >
                  <span className="flex items-center gap-1.5">
                    <i
                      aria-hidden="true"
                      className={
                        placing
                          ? "size-[5px] shrink-0 rounded-full bg-[var(--warning)]"
                          : "size-[5px] shrink-0 animate-pulse rounded-full bg-[var(--positive)] motion-reduce:animate-none"
                      }
                    />
                    <strong className="truncate text-[10px] font-semibold text-[var(--foreground)]">
                      {assignment.name}
                    </strong>
                    {assignment.volume ? (
                      <b
                        className="shrink-0 text-[8px] text-[var(--brand)]"
                        title="mounts the artifacts volume"
                      >
                        ◆
                      </b>
                    ) : null}
                  </span>
                  <span className="flex items-baseline justify-between gap-1.5 font-mono text-[7.5px] text-[var(--muted-foreground)]">
                    <span className="truncate">
                      {slot.machine ? `${slot.machine} · ` : ""}
                      {assignment.kind} · {assignment.resource}
                    </span>
                    <code className={placing ? "shrink-0 text-[var(--warning)]" : "shrink-0"}>
                      {placing ? "placing" : elapsedText}
                    </code>
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      ))}

      <div className="compute-volume flex items-baseline justify-between gap-2 rounded-md border border-[var(--border)] bg-[var(--background)]/40 px-2 py-1.5 font-mono text-[8px]">
        <span className="truncate text-[var(--foreground)]">
          <b aria-hidden="true" className="text-[var(--brand)]">
            ◆
          </b>{" "}
          {'Volume("artifacts")'} · /artifacts
        </span>
        <span className="shrink-0 text-[var(--muted-foreground)]">
          mounted in <strong className="text-[var(--foreground)]">{volumeMounts}</strong> running
          containers
        </span>
      </div>

      <div className="compute-events flex flex-col gap-0.5 font-mono text-[8px] text-[var(--muted-foreground)]">
        {recentPlacements(clock).map((event) => (
          <span key={event.assignment.name} className="truncate">
            <span className="text-[var(--positive)]">↳</span> placed{" "}
            <span className="text-[var(--foreground)]">{event.assignment.name}</span>
            {" → "}
            {event.target} · {event.assignment.resource} · {Math.max(1, Math.floor(event.ago))}s ago
          </span>
        ))}
      </div>
    </div>
  );
}
