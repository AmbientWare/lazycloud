import type { ReactNode } from "react";
import { useEffect, useState } from "react";

import { MarketingCard, StatusDot } from "./MarketingPrimitives";
import { useReducedMotion } from "./useReducedMotion";

const TICK_MS = 90;
const CHARS_PER_TICK = 3;

function typingTicks(count: number): number {
  return Math.ceil(count / CHARS_PER_TICK);
}

type Tone = "kw" | "id" | "attr" | "num" | "type" | "punc" | "note";

const TONE: Record<Tone, string> = {
  kw: "text-brand",
  id: "text-foreground",
  attr: "text-muted-foreground",
  num: "text-warning",
  type: "text-positive",
  punc: "text-muted-foreground",
  note: "text-muted-foreground",
};

type Seg = [Tone, string];

function segmentLength(segments: Seg[]): number {
  return segments.reduce((total, [, text]) => total + text.length, 0);
}

const GENERATE_COMMAND = "lazycloud app export review_app";

const IMPORT_LINE: Seg[] = [
  ["kw", "from"],
  ["id", " lazycloud_clients "],
  ["kw", "import"],
  ["id", " review_app"],
];

const CALL_TRIGGER_SEGMENTS: Seg[] = [
  ["id", "review"],
  ["punc", " = "],
  ["id", "review_app"],
  ["punc", "."],
  ["id", "review_patch"],
  ["punc", "."],
];

const CALL_ACCEPTED_SEGMENTS: Seg[] = [
  ["id", "request"],
  ["punc", "("],
  ["attr", "diff"],
  ["punc", "="],
  ["id", "patch"],
  ["punc", ")"],
];

const CALL_LINE: Seg[] = [...CALL_TRIGGER_SEGMENTS, ...CALL_ACCEPTED_SEGMENTS];

const ACCESSED_FIELD = "risks";

const FIELD_LINE: Seg[] = [
  ["id", "risks"],
  ["punc", " = "],
  ["id", "review"],
  ["punc", "."],
  ["attr", ACCESSED_FIELD],
];

const CHECKS_LINE: Seg[] = [
  ["id", "checks"],
  ["punc", " = "],
  ["id", "review_app"],
  ["punc", "."],
  ["id", "run_checks"],
  ["punc", "."],
  ["id", "remote"],
  ["punc", "("],
  ["attr", "commit_sha"],
  ["punc", "="],
  ["id", "sha"],
  ["punc", ")"],
];

const VALUE_LINE: Seg[] = [
  ["id", "passed"],
  ["punc", " = "],
  ["id", "all"],
  ["punc", "("],
  ["id", "checks"],
  ["punc", "."],
  ["id", "values"],
  ["punc", "())"],
];

const CALL_TRIGGER_CHARS = segmentLength(CALL_TRIGGER_SEGMENTS);
const CALL_ACCEPTED_CHARS = segmentLength(CALL_ACCEPTED_SEGMENTS);

const BEAT = 3;
const HANDOFF = 5;

const POPUP_READ_TICKS = 20;

const DEFINE_START = 0;
const GENERATE_START = DEFINE_START + 10;

const COMMAND_AT = GENERATE_START;
const COMMAND_DONE_AT = COMMAND_AT + typingTicks(GENERATE_COMMAND.length);
const VERSION_AT = COMMAND_DONE_AT + 4;
const FILES_AT = [VERSION_AT + 4, VERSION_AT + 7, VERSION_AT + 10];
const SYMBOLS_AT = [FILES_AT[2] + 4, FILES_AT[2] + 9, FILES_AT[2] + 14, FILES_AT[2] + 19];
const GENERATE_DONE_AT = SYMBOLS_AT[3] + 2;

const IMPORT_START = GENERATE_DONE_AT + HANDOFF;
const IMPORT_LINE_AT = IMPORT_START;
const IMPORT_LINE_DONE_AT = IMPORT_LINE_AT + typingTicks(segmentLength(IMPORT_LINE));
const CALL_LINE_AT = IMPORT_LINE_DONE_AT + 1;

const POPUP_OPEN_AT = CALL_LINE_AT + typingTicks(CALL_TRIGGER_CHARS);

const POPUP_CLOSE_AT = POPUP_OPEN_AT + POPUP_READ_TICKS;
const CALL_DONE_AT = POPUP_CLOSE_AT + typingTicks(CALL_ACCEPTED_CHARS);

const RETURN_TYPE_AT = CALL_DONE_AT + BEAT;

const FIELD_LINE_AT = RETURN_TYPE_AT + BEAT;
const FIELD_LINE_DONE_AT = FIELD_LINE_AT + typingTicks(segmentLength(FIELD_LINE));
const CHECKS_LINE_AT = FIELD_LINE_DONE_AT + BEAT;
const CHECKS_LINE_DONE_AT = CHECKS_LINE_AT + typingTicks(segmentLength(CHECKS_LINE));

const VALUE_LINE_AT = CHECKS_LINE_DONE_AT + BEAT;
const VALUE_LINE_DONE_AT = VALUE_LINE_AT + typingTicks(segmentLength(VALUE_LINE));

const TIMELINE = {
  defineStart: DEFINE_START,
  generateStart: GENERATE_START,
  commandAt: COMMAND_AT,
  commandDoneAt: COMMAND_DONE_AT,
  versionAt: VERSION_AT,
  filesAt: FILES_AT,
  symbolsAt: SYMBOLS_AT,
  generateDoneAt: GENERATE_DONE_AT,
  importStart: IMPORT_START,
  importLineAt: IMPORT_LINE_AT,
  importLineDoneAt: IMPORT_LINE_DONE_AT,
  callLineAt: CALL_LINE_AT,
  popupOpenAt: POPUP_OPEN_AT,
  popupCloseAt: POPUP_CLOSE_AT,
  callDoneAt: CALL_DONE_AT,
  returnTypeAt: RETURN_TYPE_AT,
  fieldLineAt: FIELD_LINE_AT,
  fieldLineDoneAt: FIELD_LINE_DONE_AT,
  checksLineAt: CHECKS_LINE_AT,
  checksLineDoneAt: CHECKS_LINE_DONE_AT,
  valueLineAt: VALUE_LINE_AT,
  valueLineDoneAt: VALUE_LINE_DONE_AT,

  total: VALUE_LINE_DONE_AT + 2,
  hold: 22,
} as const;

export type TypedClientPhase = "define" | "generate" | "import";

export function typedClientPhase(clock: number): TypedClientPhase {
  if (clock >= TIMELINE.importStart) return "import";
  if (clock >= TIMELINE.generateStart) return "generate";
  return "define";
}

export function useTypedClientClock(active: boolean): number {
  const reducedMotion = useReducedMotion();
  const [documentVisible, setDocumentVisible] = useState(true);
  const [clock, setClock] = useState<number>(0);

  useEffect(() => {
    const handleVisibility = () => {
      setDocumentVisible(document.visibilityState === "visible");
    };
    handleVisibility();
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, []);

  useEffect(() => {
    if (!active || !documentVisible || reducedMotion || document.visibilityState !== "visible") {
      return;
    }
    const timer = window.setInterval(() => {
      setClock((current) => (current >= TIMELINE.total + TIMELINE.hold ? 0 : current + 1));
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, [active, documentVisible, reducedMotion]);

  return reducedMotion ? TIMELINE.total : clock;
}

function Segments({ segments, shown }: { segments: Seg[]; shown: number }) {
  const parts: ReactNode[] = [];
  let used = 0;
  segments.forEach(([tone, text], index) => {
    const remaining = shown - used;
    used += text.length;
    if (remaining <= 0) return;
    parts.push(
      <span className={TONE[tone]} key={index}>
        {text.slice(0, remaining)}
      </span>,
    );
  });
  return <>{parts}</>;
}

function Caret() {
  return <span className="animate-pulse text-brand">▍</span>;
}

function panelState(active: boolean): string {
  return `marketing-typed-visual flex min-w-0 flex-col font-mono text-card-foreground transition-colors duration-500 motion-reduce:transition-none ${
    active ? "border-brand/45" : "border-input"
  }`;
}

function SkeletonBar({ width }: { width: string }) {
  return (
    <span
      aria-hidden="true"
      className={`block h-[7px] rounded-md bg-muted-foreground/20 ${width}`}
    />
  );
}

function typedFrom(clock: number, at: number): number {
  return Math.max(0, Math.floor((clock - at) * CHARS_PER_TICK));
}

type GeneratedSymbol = {
  symbol: string;
  kind: string;
  signature: Seg[];
};

const GENERATED_SYMBOLS: GeneratedSymbol[] = [
  {
    symbol: "review_patch.Review",
    kind: "model",
    signature: [
      ["attr", "summary: "],
      ["type", "str"],
      ["punc", " · "],
      ["attr", "risks: "],
      ["type", "list[str]"],
    ],
  },
  {
    symbol: "review_patch.request",
    kind: "endpoint",
    signature: [
      ["punc", "("],
      ["attr", "diff: "],
      ["type", "str"],
      ["punc", ") -> "],
      ["type", "Review"],
    ],
  },
  {
    symbol: "review_patch.async_request",
    kind: "endpoint",
    signature: [
      ["punc", "("],
      ["attr", "diff: "],
      ["type", "str"],
      ["punc", ") -> "],
      ["type", "Review"],
    ],
  },
  {
    symbol: "run_checks.remote",
    kind: "function",
    signature: [
      ["punc", "("],
      ["attr", "commit_sha: "],
      ["type", "str"],
      ["punc", ") -> "],
      ["type", "dict[str, bool]"],
    ],
  },
];

const GENERATED_FILES = [
  { name: "v_84bd1a7c20f3/", note: "content hash" },
  { name: "py.typed", note: "typed marker" },
  { name: "lazycloud-clients.lock.json", note: "pinned" },
];

export function GeneratedPackagePanel({ clock, active }: { clock: number; active: boolean }) {
  const shown = typedFrom(clock, TIMELINE.commandAt);
  const command = GENERATE_COMMAND.slice(0, shown);
  const typing = shown > 0 && shown < GENERATE_COMMAND.length;
  const versionReady = clock >= TIMELINE.versionAt;
  const done = clock >= TIMELINE.generateDoneAt;

  return (
    <MarketingCard className={panelState(active)}>
      <div
        className="typed-panel-bar flex min-h-9 min-w-0 flex-wrap items-center gap-x-2 gap-y-1 border-b border-border px-3.5 py-1.5 text-[10.5px]"
        data-marketing-terminal-surface=""
      >
        <span className="text-brand">$</span>
        <span className="min-w-[8rem] flex-1 break-words text-foreground [overflow-wrap:anywhere]">
          {command}
          {typing ? <Caret /> : null}
        </span>
        <span className="ml-auto shrink-0 text-[9px] tracking-[0.12em] text-muted-foreground uppercase">
          {done ? "written" : "resolving"}
        </span>
      </div>

      <div className="flex min-w-0 flex-1 flex-col gap-3 px-3.5 py-3">
        <div className="typed-package-heading flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1.5">
          <span className="typed-package-path min-w-0 truncate text-[12.5px] text-foreground">
            lazycloud_clients/review_app
          </span>
          <span
            className={
              versionReady
                ? "inline-flex shrink-0 items-center gap-1.5 rounded-md border border-positive/30 bg-positive/10 px-2 py-0.5 text-[10px] text-positive"
                : "inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border px-2 py-0.5 text-[10px] text-muted-foreground"
            }
          >
            {versionReady ? <StatusDot /> : null}
            {versionReady ? "v_84bd1a7c20f3" : "hashing manifest"}
          </span>
        </div>

        <div className="typed-file-grid grid grid-cols-[1fr_1fr_1.4fr] gap-1.5 max-[640px]:grid-cols-1">
          {GENERATED_FILES.map((file, index) => {
            const ready = clock >= TIMELINE.filesAt[index];
            return (
              <div
                className="flex min-h-[26px] min-w-0 items-center gap-1.5 rounded-md border border-border bg-background/40 px-2 py-1"
                key={file.name}
              >
                <i
                  aria-hidden="true"
                  className={`size-1 shrink-0 rounded-full ${
                    ready ? "bg-brand" : "bg-muted-foreground/40"
                  }`}
                />
                <span className={`min-w-0 flex-1 ${ready ? "hidden" : ""}`}>
                  <SkeletonBar width="w-3/4" />
                </span>
                <span
                  className={`min-w-0 truncate text-[10px] text-foreground ${ready ? "" : "hidden"}`}
                  title={file.note}
                >
                  {file.name}
                </span>
              </div>
            );
          })}
        </div>

        <div className="flex min-w-0 flex-col">
          <div className="mb-1.5 flex items-center justify-between text-[9px] tracking-[0.12em] text-muted-foreground">
            <span>Methods and models</span>
            <span className="uppercase">{done ? "4 symbols" : "generating"}</span>
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            {GENERATED_SYMBOLS.map((item, index) => {
              const ready = clock >= TIMELINE.symbolsAt[index];
              return (
                <div
                  className="typed-symbol-row flex min-h-[28px] min-w-0 items-baseline gap-2 rounded-md border border-border bg-background/40 px-2.5 py-1"
                  key={item.symbol}
                >
                  <span className={`min-w-0 flex-1 self-center ${ready ? "hidden" : ""}`}>
                    <SkeletonBar width="w-2/3" />
                  </span>
                  <span
                    className={`typed-symbol-name shrink-0 text-[11px] text-foreground ${ready ? "" : "hidden"}`}
                  >
                    {item.symbol}
                  </span>
                  <span
                    className={`typed-symbol-signature min-w-0 flex-1 truncate text-[10.5px] ${ready ? "" : "hidden"}`}
                  >
                    <Segments segments={item.signature} shown={segmentLength(item.signature)} />
                  </span>
                  <span
                    className={`shrink-0 text-[9px] tracking-[0.1em] text-muted-foreground uppercase max-[520px]:hidden ${
                      ready ? "" : "hidden"
                    }`}
                  >
                    {item.kind}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </MarketingCard>
  );
}

type EditorLine = {
  at: number;
  segments: Seg[];
  holdChars?: number;
  holdUntil?: number;
};

const EDITOR_LINES: EditorLine[] = [
  { at: TIMELINE.importLineAt, segments: IMPORT_LINE },
  {
    at: TIMELINE.callLineAt,
    segments: CALL_LINE,
    holdChars: CALL_TRIGGER_CHARS,
    holdUntil: TIMELINE.popupCloseAt,
  },
  { at: TIMELINE.fieldLineAt, segments: FIELD_LINE },
  { at: TIMELINE.checksLineAt, segments: CHECKS_LINE },
  { at: TIMELINE.valueLineAt, segments: VALUE_LINE },
];

type Member = {
  name: string;
  badge: string;
  detail: Seg[];
};

const MEMBERS: Member[] = [
  {
    name: "request",
    badge: "def",
    detail: [
      ["punc", "("],
      ["attr", "diff: "],
      ["type", "str"],
      ["punc", ") -> "],
      ["type", "Review"],
    ],
  },
  {
    name: "async_request",
    badge: "async",
    detail: [
      ["punc", "("],
      ["attr", "diff: "],
      ["type", "str"],
      ["punc", ") -> "],
      ["type", "Review"],
    ],
  },
];

const REVIEW_FIELDS: { name: string; annotation: Seg[] }[] = [
  { name: "summary", annotation: [["type", "str"]] },
  { name: "risks", annotation: [["type", "list[str]"]] },
];

function typedChars(clock: number, line: EditorLine): number {
  const raw = typedFrom(clock, line.at);
  if (raw <= 0) return 0;
  if (line.holdChars !== undefined && line.holdUntil !== undefined) {
    if (clock < line.holdUntil) return Math.min(raw, line.holdChars);
    return line.holdChars + typedFrom(clock, line.holdUntil);
  }
  return raw;
}

export function TypedImportPanel({ clock, active }: { clock: number; active: boolean }) {
  const popupOpen = clock >= TIMELINE.popupOpenAt && clock < TIMELINE.popupCloseAt;
  const returnReady = clock >= TIMELINE.returnTypeAt && clock >= TIMELINE.callDoneAt;
  const fieldRead = clock >= TIMELINE.fieldLineDoneAt;
  const settled = clock >= TIMELINE.valueLineDoneAt;

  return (
    <MarketingCard className={panelState(active)} data-marketing-terminal-surface="">
      <div className="typed-editor-tabs flex min-h-9 items-center gap-1 border-b border-border px-2.5 text-[10px]">
        <span className="rounded-md border border-border bg-background/60 px-2 py-0.5 text-foreground">
          release.py
        </span>
        <span className="px-2 py-0.5 text-muted-foreground">pyproject.toml</span>
        <span className="ml-auto shrink-0 truncate text-[9px] tracking-[0.12em] text-muted-foreground uppercase">
          your codebase
        </span>
      </div>

      <div className="flex min-w-0 flex-1 flex-col gap-2 px-2.5 py-2.5">
        <div className="flex min-w-0 flex-col">
          {EDITOR_LINES.map((line, index) => {
            const shown = typedChars(clock, line);
            const total = segmentLength(line.segments);
            const caret = shown > 0 && (shown < total || (popupOpen && index === 1));
            return (
              <div
                className="typed-code-line flex min-w-0 gap-2.5 text-[11px] leading-[1.6]"
                key={index}
              >
                <span className="w-4 shrink-0 text-right text-muted-foreground/50">
                  {index + 1}
                </span>
                <span className="min-w-0 break-words whitespace-normal [overflow-wrap:anywhere]">
                  {shown > 0 ? <Segments segments={line.segments} shown={shown} /> : " "}
                  {caret ? <Caret /> : null}
                </span>
              </div>
            );
          })}
        </div>

        <div className="flex min-h-[80px] min-w-0 flex-col justify-start">
          {popupOpen ? (
            <div
              className="typed-editor-inspector ml-6 min-w-0 overflow-hidden rounded-md border border-brand/40 bg-background shadow-lg"
              key="popup"
            >
              <div className="flex items-center justify-between border-b border-border px-2 py-1 text-[9px] tracking-[0.12em] text-muted-foreground uppercase">
                <span className="truncate">review_app.review_patch</span>
                <span className="shrink-0">{MEMBERS.length} members</span>
              </div>
              {MEMBERS.map((member, index) => (
                <div
                  className={`flex min-w-0 items-baseline gap-2 px-2 py-1 ${
                    index === 0 ? "bg-brand/15" : ""
                  }`}
                  key={member.name}
                >
                  <span className="w-8 shrink-0 text-[8.5px] tracking-[0.1em] text-brand uppercase">
                    {member.badge}
                  </span>
                  <span
                    className={`shrink-0 text-[11px] ${
                      index === 0 ? "text-foreground" : "text-muted-foreground"
                    }`}
                  >
                    {member.name}
                  </span>
                  <span className="min-w-0 truncate text-[10px]">
                    <Segments segments={member.detail} shown={segmentLength(member.detail)} />
                  </span>
                </div>
              ))}
            </div>
          ) : returnReady ? (
            <div
              className="typed-editor-inspector ml-6 min-w-0 overflow-hidden rounded-md border border-border bg-background/50"
              key="result"
            >
              <div className="flex min-w-0 items-baseline justify-between gap-2 border-b border-border px-2 py-1">
                <span className="min-w-0 truncate text-[10.5px]">
                  <span className={TONE.id}>review</span>
                  <span className={TONE.punc}>: </span>
                  <span className={TONE.type}>Review</span>
                </span>
                <span className="shrink-0 text-[8.5px] tracking-[0.12em] text-muted-foreground uppercase">
                  returned
                </span>
              </div>
              {REVIEW_FIELDS.map((field) => {
                const read = fieldRead && field.name === ACCESSED_FIELD;
                return (
                  <div
                    className={`flex min-w-0 items-baseline gap-2 px-2 py-0.5 ${
                      read ? "bg-brand/15" : ""
                    }`}
                    key={field.name}
                  >
                    <span className="min-w-0 truncate text-[10px]">
                      <span className={read ? "text-foreground" : "text-muted-foreground"}>
                        {field.name}
                      </span>
                      <span className={TONE.punc}>: </span>
                      <Segments
                        segments={field.annotation}
                        shown={segmentLength(field.annotation)}
                      />
                    </span>
                    {read ? (
                      <span className="ml-auto shrink-0 text-[8.5px] tracking-[0.12em] text-brand uppercase">
                        read
                      </span>
                    ) : null}
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>

        <div className="flex min-h-6 items-center gap-2 border-t border-border pt-1.5 mt-auto text-[9px] tracking-[0.1em] text-muted-foreground uppercase">
          <span
            className={`inline-flex items-center gap-1.5 ${
              settled ? "text-positive" : "text-muted-foreground"
            }`}
          >
            {settled ? <StatusDot /> : null} 0 problems
          </span>
          <span className="ml-auto shrink-0 truncate">python 3.12 · fully typed</span>
        </div>
      </div>
    </MarketingCard>
  );
}
