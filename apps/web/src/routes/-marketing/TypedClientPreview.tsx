import type { ReactNode } from "react";
import { useEffect, useState } from "react";

import { MarketingCard, StatusDot } from "./MarketingPrimitives";
import { useReducedMotion } from "./useReducedMotion";

/* Typed-client visuals for the "Deployments you can import." section.

   One clock drives all three phases so the section reads as a single story:
   the app is defined, `lazycloud client get review_app` writes the package, and
   only once the symbols exist does the consuming editor start typing against
   them. The whole sequence is one module-scope timeline (TIMELINE below), so
   causality is tunable in one place and phase 03 can never reference a symbol
   phase 02 has not emitted yet.

   GeneratedPackagePanel replays what the generator writes: the content-hash
   version directory, the lock file, the `py.typed` markers, and the exported
   symbols for an endpoint and a function.

   TypedImportPanel replays the consuming side in an editor: a member popup on
   the generated handle, the accepted completion, and the resolved return type.

   Every symbol, signature, filename, and version format is taken from
   `lazycloud.client_codegen`: the version directory is `v_` + the first 12 hex
   characters of a sha256 over the manifest, each package writes `py.typed`,
   the root writes `lazycloud-clients.lock.json`, endpoints export
   `request`/`async_request` plus public aliases for their response models, and
   functions export `remote`/`async_remote` returning the decoded result.

   The section owns activation. Each entry mounts a clock at zero, each exit
   discards it, and reduced-motion viewers receive the completed static frame.
   No randomness, no dates. */

const TICK_MS = 90;
const CHARS_PER_TICK = 3;

/* Ticks needed to type `count` characters at the shared typing speed. */
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

/* ---- the exact text the editor types, and nothing else ---- */

const GENERATE_COMMAND = "lazycloud client get review_app";

const IMPORT_LINE: Seg[] = [
  ["kw", "from"],
  ["id", " lazycloud_clients "],
  ["kw", "import"],
  ["id", " review_app"],
];

/* Typing stops on the trailing dot: that dot is the completion trigger. */
const CALL_TRIGGER_SEGMENTS: Seg[] = [
  ["id", "review"],
  ["punc", " = "],
  ["id", "review_app"],
  ["punc", "."],
  ["id", "review_patch"],
  ["punc", "."],
];

/* Inserted only once a member is accepted from the popup. */
const CALL_ACCEPTED_SEGMENTS: Seg[] = [
  ["id", "request"],
  ["punc", "("],
  ["attr", "diff"],
  ["punc", "="],
  ["id", "patch"],
  ["punc", ")"],
];

const CALL_LINE: Seg[] = [...CALL_TRIGGER_SEGMENTS, ...CALL_ACCEPTED_SEGMENTS];

/* Reading a field off the returned model: this is what proves the result is
   typed, and it is the only place `Review`'s fields are invoked by name. */
const ACCESSED_FIELD = "risks";

const FIELD_LINE: Seg[] = [
  ["id", "risks"],
  ["punc", " = "],
  ["id", "review"],
  ["punc", "."],
  ["attr", ACCESSED_FIELD],
];

const TASK_LINE: Seg[] = [
  ["id", "task"],
  ["punc", " = "],
  ["id", "review_app"],
  ["punc", "."],
  ["id", "run_checks"],
  ["punc", "."],
  ["id", "put"],
  ["punc", "("],
  ["attr", "commit_sha"],
  ["punc", "="],
  ["id", "sha"],
  ["punc", ")"],
];

const VALUE_LINE: Seg[] = [
  ["id", "checks"],
  ["punc", " = "],
  ["id", "task"],
  ["punc", "."],
  ["id", "wait"],
  ["punc", "()."],
  ["id", "value"],
  ["note", "  # dict[str, bool]"],
];

/* Every phase-03 boundary below is derived from these lengths, so a boundary
   can never land before the text it depends on has actually been typed. */
const CALL_TRIGGER_CHARS = segmentLength(CALL_TRIGGER_SEGMENTS);
const CALL_ACCEPTED_CHARS = segmentLength(CALL_ACCEPTED_SEGMENTS);

/* Beats between steps, in ticks, so pacing is tunable without arithmetic. */
const BEAT = 3;
const HANDOFF = 5;
/* How long the completion popup stays open before a member is accepted. */
const POPUP_READ_TICKS = 20;

/* 01 Define holds the eye first, then hands off to the generator. */
const DEFINE_START = 0;
const GENERATE_START = DEFINE_START + 10;

/* 02 Generate: command, content hash, written files, exported symbols. */
const COMMAND_AT = GENERATE_START;
const COMMAND_DONE_AT = COMMAND_AT + typingTicks(GENERATE_COMMAND.length);
const VERSION_AT = COMMAND_DONE_AT + 4;
const FILES_AT = [VERSION_AT + 4, VERSION_AT + 7, VERSION_AT + 10];
const SYMBOLS_AT = [FILES_AT[2] + 4, FILES_AT[2] + 9, FILES_AT[2] + 14, FILES_AT[2] + 19];
const GENERATE_DONE_AT = SYMBOLS_AT[3] + 2;

/* 03 Import: cannot start until every symbol above exists. Each boundary is
   the previous one plus the text it must type, in strict editor order:
   line types -> trigger typed -> popup opens -> member accepted -> call
   finishes typing -> return type resolves -> a field is read off it -> task
   call -> resolved value. */
const IMPORT_START = GENERATE_DONE_AT + HANDOFF;
const IMPORT_LINE_AT = IMPORT_START;
const IMPORT_LINE_DONE_AT = IMPORT_LINE_AT + typingTicks(segmentLength(IMPORT_LINE));
const CALL_LINE_AT = IMPORT_LINE_DONE_AT + 1;
/* The popup opens on the frame the trigger dot lands, and not one tick before. */
const POPUP_OPEN_AT = CALL_LINE_AT + typingTicks(CALL_TRIGGER_CHARS);
/* Accepting the member closes the popup and starts inserting the call. */
const POPUP_CLOSE_AT = POPUP_OPEN_AT + POPUP_READ_TICKS;
const CALL_DONE_AT = POPUP_CLOSE_AT + typingTicks(CALL_ACCEPTED_CHARS);
/* The return type only resolves once the call is fully typed. */
const RETURN_TYPE_AT = CALL_DONE_AT + BEAT;
/* Only then can a field be read off the result. */
const FIELD_LINE_AT = RETURN_TYPE_AT + BEAT;
const FIELD_LINE_DONE_AT = FIELD_LINE_AT + typingTicks(segmentLength(FIELD_LINE));
const TASK_LINE_AT = FIELD_LINE_DONE_AT + BEAT;
const TASK_LINE_DONE_AT = TASK_LINE_AT + typingTicks(segmentLength(TASK_LINE));
/* Reading the resolved value is the last thing that happens. */
const VALUE_LINE_AT = TASK_LINE_DONE_AT + BEAT;
const VALUE_LINE_DONE_AT = VALUE_LINE_AT + typingTicks(segmentLength(VALUE_LINE));

/* One timeline for the whole section, in ticks. */
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
  taskLineAt: TASK_LINE_AT,
  taskLineDoneAt: TASK_LINE_DONE_AT,
  valueLineAt: VALUE_LINE_AT,
  valueLineDoneAt: VALUE_LINE_DONE_AT,
  /* Full sequence, then a ~2s beat before it replays. */
  total: VALUE_LINE_DONE_AT + 2,
  hold: 22,
} as const;

export type TypedClientPhase = "define" | "generate" | "import";

export function typedClientPhase(clock: number): TypedClientPhase {
  if (clock >= TIMELINE.importStart) return "import";
  if (clock >= TIMELINE.generateStart) return "generate";
  return "define";
}

/* One activation-owned interval for all three phases. */
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

/* Panels share one sheet; the active phase picks up the brand border. */
function panelState(active: boolean): string {
  return `marketing-typed-visual flex min-w-0 flex-col font-mono text-card-foreground transition-colors duration-500 motion-reduce:transition-none ${
    active ? "border-brand/45" : "border-input"
  }`;
}

/* Placeholder bar so a card keeps its shape while its content is pending. */
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

/* ------------------------------------------------------------------ 02 */

type GeneratedSymbol = {
  symbol: string;
  kind: string;
  signature: Seg[];
};

/* Exactly what the generator emits for the two deployed resources. */
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
            <span>__all__</span>
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

/* ------------------------------------------------------------------ 03 */

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
  { at: TIMELINE.taskLineAt, segments: TASK_LINE },
  { at: TIMELINE.valueLineAt, segments: VALUE_LINE },
];

type Member = {
  name: string;
  badge: string;
  detail: Seg[];
};

/* Only what you can actually CALL on the generated endpoint handle. The
   handle also re-exports the `Review` model as a public alias, but that is a
   type, not something you invoke, so it belongs on the result below and not
   in a completion list of callables. */
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

/* The fields on the returned model, shown as the RESULT of the call. */
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
  /* Strictly ordered against the shared clock: the popup of callable members
     only exists between the frame the trigger dot is typed and the frame a
     member is accepted; the returned type only exists once the accepted call
     is fully typed; the read field only highlights once it has been typed. */
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

        {/* Sized to the popup, the tallest frame, so nothing shifts or clips. */}
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
            /* The result of the call: what you get back, and that it is typed.
               Rendered only once the call is fully typed -- and keyed apart
               from the popup so React mounts a new node instead of reusing
               the popup's, which would leak the popup's visible state here. */
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
