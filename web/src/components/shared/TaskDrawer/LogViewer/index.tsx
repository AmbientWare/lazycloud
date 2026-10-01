import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { Download, Pause, Play, Search } from "lucide-react";

import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { CopyButton } from "@/components/shared/CopyButton";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  LOG_TAIL,
  useLogStream,
  type LogEntry,
  type LogScope,
  type LogStreamStatus,
} from "@/hooks/useLogStream";
import { downloadBlob } from "@/lib/files";
import { cn } from "@/lib/utils";

const MAX_RENDERED_LINES = 1_000;

export function LogViewer({
  workspace,
  scope,
  className,
}: {
  workspace: string;
  scope: LogScope;
  className?: string;
}) {
  const [follow, setFollow] = useState(true);
  const [filter, setFilter] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const { entries, status, error } = useLogStream(workspace, scope, follow);

  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const filtered = needle
      ? entries.filter((entry) => logSearchText(entry).includes(needle))
      : entries;
    return filtered.slice(-MAX_RENDERED_LINES);
  }, [filter, entries]);

  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (follow && node) node.scrollTop = node.scrollHeight;
  }, [follow, visible]);

  const loading = entries.length === 0 && status === "connecting";

  return (
    <div className={cn("flex min-h-0 flex-col", className)}>
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border p-2">
        <label className="flex h-8 min-w-40 flex-1 items-center gap-2 rounded-md border border-input bg-muted px-2 focus-within:border-ring">
          <Search className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="sr-only">Filter logs</span>
          <input
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="Filter logs"
            className="mono min-w-0 flex-1 bg-transparent text-xs text-foreground outline-none"
          />
        </label>
        <Button
          variant={follow ? "default" : "outline"}
          size="sm"
          onClick={() => setFollow((previous) => !previous)}
        >
          {follow ? <Pause className="size-3" /> : <Play className="size-3" />}
          {follow ? "Following" : "Follow"}
        </Button>
        {follow ? (
          <span
            className="flex items-center gap-1.5 text-[11px] text-muted-foreground"
            data-stream-stale={status === "reconnecting" ? "" : undefined}
          >
            <span
              className={cn(
                "size-1.5 rounded-full",
                status === "open"
                  ? "pulse-live bg-positive"
                  : status === "reconnecting"
                    ? "bg-warning"
                    : "bg-muted-foreground/50",
              )}
              aria-hidden="true"
            />
            {streamStatusLabel(status)}
          </span>
        ) : null}
        <CopyButton
          value={() => formatLogEntries(visible)}
          label="visible logs"
          disabled={visible.length === 0}
        />
        {entries.length >= LOG_TAIL && !filter ? (
          <span className="text-[11px] text-muted-foreground">Latest 1,000 lines</span>
        ) : null}
        <Button
          variant="ghost"
          size="icon"
          disabled={entries.length === 0}
          aria-label="Download loaded logs"
          title="Download loaded logs"
          onClick={() => downloadLogs(entries)}
        >
          <Download className="size-3.5" />
        </Button>
      </div>
      <div
        ref={scrollRef}
        data-log-scroll=""
        className="min-h-0 flex-1 overflow-auto bg-background/60"
      >
        {loading ? (
          <div className="space-y-1.5 p-2" aria-hidden="true">
            {[80, 60, 90, 45, 70].map((width, index) => (
              <Skeleton key={index} className="h-4" style={{ width: `${width}%` }} />
            ))}
          </div>
        ) : status === "error" && entries.length === 0 ? (
          <ApiErrorNotice error={error} title="Logs could not be loaded" />
        ) : visible.length === 0 ? (
          <PanelEmpty message="No log lines" className="h-32" />
        ) : (
          <div
            className="content-transition mono py-2 text-xs leading-5"
            role="list"
            aria-label="Log output"
          >
            {visible.map((entry, index) => (
              <LogLine
                key={entry.id}
                entry={entry}
                showDate={index === 0 || logDateKey(entry) !== logDateKey(visible[index - 1])}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function streamStatusLabel(status: LogStreamStatus): string {
  if (status === "open") return "Live";
  if (status === "reconnecting") return "Reconnecting · loaded logs";
  if (status === "connecting") return "Connecting";
  if (status === "error") return "Disconnected";
  return "Closed";
}

function LogLine({ entry, showDate }: { entry: LogEntry; showDate: boolean }) {
  const showStream = entry.stream !== "stdout";
  const stderr = entry.stream === "stderr";
  const system = entry.stream === "system";

  return (
    <div role="listitem">
      {showDate ? (
        <div className="flex items-center gap-2 px-3 py-1.5 text-[10px] text-muted-foreground">
          <span className="shrink-0">{formatLogDate(entry.time)}</span>
          <span className="h-px min-w-4 flex-1 bg-border/70" aria-hidden="true" />
        </div>
      ) : null}
      <div
        className={cn(
          "grid grid-cols-[4.5rem_minmax(0,1fr)] items-start gap-2 border-l-2 border-transparent px-3 py-px whitespace-pre-wrap break-all transition-colors hover:bg-accent/45",
          stderr && "border-destructive bg-destructive/8",
          system && "border-brand/40 bg-brand/5",
        )}
      >
        <time
          dateTime={entry.time}
          title={entry.time}
          aria-label={`Log timestamp ${entry.time}`}
          className="shrink-0 tabular-nums text-muted-foreground"
        >
          {formatLogClock(entry.time)}
        </time>
        <span className="min-w-0">
          {showStream ? (
            <span
              className={cn(
                "mr-2 inline text-[10px] font-medium uppercase text-muted-foreground",
                stderr && "text-destructive",
                system && "text-brand",
              )}
            >
              {entry.stream}
            </span>
          ) : null}
          <span>{entry.data}</span>
        </span>
      </div>
    </div>
  );
}

function formatLogClock(timestamp: string): string {
  const parsed = new Date(timestamp);
  return Number.isNaN(parsed.getTime()) ? timestamp : parsed.toISOString().slice(11, 19);
}

function formatLogDate(timestamp: string): string {
  const parsed = new Date(timestamp);
  if (Number.isNaN(parsed.getTime())) return timestamp.split("T", 1)[0];
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(parsed);
}

function logDateKey(entry: LogEntry | undefined): string {
  if (!entry) return "";
  const parsed = new Date(entry.time);
  return Number.isNaN(parsed.getTime())
    ? entry.time.split("T", 1)[0]
    : parsed.toISOString().slice(0, 10);
}

function logSearchText(entry: LogEntry): string {
  return [entry.time, entry.stream, entry.data, entry.task_id].join(" ").toLowerCase();
}

function formatLogEntries(entries: LogEntry[]): string {
  return entries
    .map((entry) => `${entry.time} ${entry.stream} ${entry.task_id} ${entry.data}`)
    .join("\n");
}

function downloadLogs(entries: LogEntry[]): void {
  downloadBlob(
    "task-logs.txt",
    new Blob([formatLogEntries(entries)], { type: "text/plain;charset=utf-8" }),
  );
}
