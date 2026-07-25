import { useEffect, useState } from "react";

import { useReducedMotion } from "./useReducedMotion";
import { usePreviewActivity } from "./usePreviewActivity";

/* Live sandbox preview: an agent's terminal session replayed on a loop --
   commands typed out character by character, with a Processes / Files /
   Network state strip that updates as the session progresses. The script is
   fixed data and the clock starts settled on the completed session, so the
   prerendered markup matches the first client render; motion starts only
   after mount and reduced-motion viewers keep the finished frame. */

const TICK_MS = 80;
const TYPE_CHARS_PER_TICK = 3;
const TOTAL_TICKS = 172;
const HOLD_TICKS = 45;

type LineKind = "cmd" | "out" | "ok" | "err" | "sys";

type SessionLine = {
  at: number;
  kind: LineKind;
  text: string;
  /* True for everything the agent process does inside the sandbox; the launch
     command and the SDK lifecycle calls stay on the host side of the rail. */
  sandbox?: boolean;
};

/* One agent work loop: reproduce a failing test, patch the file through the
   sandbox filesystem, hit the egress policy, re-run green, snapshot, exit. */
const SESSION: SessionLine[] = [
  {
    at: 2,
    kind: "cmd",
    text: 'python agent.py --task "fix failing auth test"',
  },
  {
    at: 28,
    kind: "out",
    text: "agent started · pid 12 · cwd /workspace",
    sandbox: true,
  },
  { at: 36, kind: "cmd", text: "pytest -q tests/test_auth.py", sandbox: true },
  { at: 56, kind: "err", text: "1 failed, 24 passed in 3.2s", sandbox: true },
  { at: 64, kind: "cmd", text: 'grep -rn "token_ttl" src/', sandbox: true },
  { at: 80, kind: "out", text: "src/auth.py:41: token_ttl = 0", sandbox: true },
  {
    at: 88,
    kind: "sys",
    text: "fs.replace_in_files · src/auth.py · 1 file updated",
    sandbox: true,
  },
  { at: 98, kind: "cmd", text: "curl https://pypi.org", sandbox: true },
  {
    at: 114,
    kind: "err",
    text: "blocked · network policy: block-all egress",
    sandbox: true,
  },
  { at: 122, kind: "cmd", text: "pytest -q tests/test_auth.py", sandbox: true },
  { at: 142, kind: "ok", text: "25 passed in 2.9s", sandbox: true },
  {
    at: 152,
    kind: "sys",
    text: "fs.create_image_from_filesystem → img_9d41c2",
  },
  { at: 164, kind: "sys", text: "sandbox.terminate() · workspace released" },
];

type CellState = {
  at: number;
  value: string;
  sub: string;
};

const PROCESS_STATES: CellState[] = [
  { at: 0, value: "1 running", sub: "python agent.py · pid 12" },
  { at: 36, value: "2 running", sub: "pytest · pid 31" },
  { at: 56, value: "1 running", sub: "python agent.py · pid 12" },
  { at: 64, value: "2 running", sub: "grep · pid 34" },
  { at: 80, value: "1 running", sub: "python agent.py · pid 12" },
  { at: 98, value: "2 running", sub: "curl · pid 38" },
  { at: 114, value: "1 running", sub: "python agent.py · pid 12" },
  { at: 122, value: "2 running", sub: "pytest · pid 41" },
  { at: 142, value: "1 running", sub: "python agent.py · pid 12" },
  { at: 164, value: "0 running", sub: "agent exited · code 0" },
];

const FILE_STATES: CellState[] = [
  { at: 0, value: "clean", sub: "/workspace · 24 files" },
  { at: 88, value: "1 modified", sub: "src/auth.py" },
  { at: 152, value: "snapshot saved", sub: "img_9d41c2" },
];

const NETWORK_STATES: CellState[] = [
  { at: 0, value: "block-all", sub: "egress blocked · 0 attempts" },
  { at: 114, value: "block-all", sub: "egress blocked · 1 attempt" },
];

const INSPECTOR_TABS = ["Terminal", "Files", "Processes", "Network"];

function latestState(states: CellState[], clock: number): CellState {
  let current = states[0];
  for (const state of states) {
    if (state.at <= clock) current = state;
  }
  return current;
}

function LineText({ line, clock }: { line: SessionLine; clock: number }) {
  if (line.kind === "cmd") {
    const shown = line.text.slice(
      0,
      Math.max(0, Math.floor((clock - line.at) * TYPE_CHARS_PER_TICK)),
    );
    const typing = shown.length < line.text.length;
    return (
      <span className="break-words text-foreground">
        {line.sandbox ? (
          <span className="text-brand">sandbox$ </span>
        ) : (
          <span className="text-muted-foreground">$ </span>
        )}
        {shown}
        {typing ? <span className="animate-pulse text-brand">▍</span> : null}
      </span>
    );
  }
  if (line.kind === "sys") {
    return (
      <span className="break-words text-brand">
        <span aria-hidden="true">↳ </span>
        {line.text}
      </span>
    );
  }
  const tone =
    line.kind === "ok"
      ? "text-positive"
      : line.kind === "err"
        ? "text-warning"
        : "text-muted-foreground";
  return <span className={`break-words ${tone}`}>{line.text}</span>;
}

function StateCell({
  label,
  state,
  dotClass,
}: {
  label: string;
  state: CellState;
  dotClass: string;
}) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-background/40 px-2.5 py-2">
      <div className="flex items-center gap-1.5 text-[9px] uppercase tracking-[0.14em] text-muted-foreground">
        <i
          aria-hidden="true"
          className={`h-1.5 w-1.5 rounded-full ${dotClass}`}
        />
        {label}
      </div>
      <div className="mt-1 truncate text-[11px] text-foreground">
        {state.value}
      </div>
      <div className="truncate text-[10px] text-muted-foreground">
        {state.sub}
      </div>
    </div>
  );
}

export function LiveSandboxPreview() {
  const reducedMotion = useReducedMotion();
  const { active, previewRef } = usePreviewActivity();
  const [clock, setClock] = useState(0);

  useEffect(() => {
    if (reducedMotion || !active) return;

    /* The terminal begins empty on activation. Inactive panels unmount, so
       their clocks and timers are discarded instead of pausing mid-session. */
    const timer = window.setInterval(() => {
      setClock((current) =>
        current >= TOTAL_TICKS + HOLD_TICKS ? 0 : current + 1,
      );
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, [active, reducedMotion]);

  const visibleClock = reducedMotion ? TOTAL_TICKS : clock;

  const visible = SESSION.filter((line) => line.at <= visibleClock);
  const processes = latestState(PROCESS_STATES, visibleClock);
  const files = latestState(FILE_STATES, visibleClock);
  const network = latestState(NETWORK_STATES, visibleClock);

  return (
    <div
      className="marketing-sandbox-preview flex min-h-0 flex-1 flex-col gap-2.5 px-4 pt-3 pb-4 font-mono"
      data-animation-state={
        reducedMotion ? "settled" : active ? "running" : "paused"
      }
      ref={previewRef}
    >
      <div
        aria-hidden="true"
        className="flex items-center justify-between text-[10px] tracking-[0.08em]"
      >
        <div className="flex gap-1">
          {INSPECTOR_TABS.map((tab) => (
            <span
              className={
                tab === "Terminal"
                  ? "rounded border border-border bg-background/60 px-2 py-0.5 text-foreground"
                  : "px-2 py-0.5 text-muted-foreground"
              }
              key={tab}
            >
              {tab}
            </span>
          ))}
        </div>
        <span className="text-muted-foreground">coding-agent · /workspace</span>
      </div>
      <div
        className="flex min-h-0 min-w-0 flex-1 flex-col justify-end overflow-hidden rounded-md border border-border bg-background/60 px-3 py-2.5 text-[11px] leading-[1.55] [overflow-wrap:anywhere]"
        data-marketing-terminal-surface=""
      >
        {visible.map((line) => (
          <div
            className={
              line.sandbox
                ? "border-l border-brand/40 pl-2.5"
                : "pl-[calc(0.625rem+1px)]"
            }
            key={line.at}
          >
            <LineText clock={visibleClock} line={line} />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-3 gap-2">
        <StateCell
          dotClass={
            processes.value.startsWith("0")
              ? "bg-muted-foreground"
              : "bg-positive"
          }
          label="Processes"
          state={processes}
        />
        <StateCell dotClass="bg-brand" label="Files" state={files} />
        <StateCell dotClass="bg-warning" label="Network" state={network} />
      </div>
    </div>
  );
}
