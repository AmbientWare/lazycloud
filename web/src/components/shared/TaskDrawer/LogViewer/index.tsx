import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Download, Pause, Play, Search } from "lucide-react";

import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { CopyButton } from "@/components/shared/CopyButton";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useReconnectingStream, type EventStreamStatus } from "@/hooks/useEventStream";
import { followLogs, type LogLine, type LogSource } from "@/lib/api/logs";
import { downloadBlob } from "@/lib/files";
import { logHistoryQueryOptions } from "@/lib/queries/logs";
import { cn } from "@/lib/utils";

const MAX_RENDERED_LINES = 1_000;
const MAX_LIVE_LINES = 2_000;

/**
 * A task's, container's or request's output: the newest stored lines, then,
 * while following, each new line of a task or container. A request's output
 * is complete, so there is nothing to follow.
 */
export function LogViewer({
  workspace,
  source,
  className,
}: {
  workspace: string;
  source: LogSource;
  className?: string;
}) {
  const [follow, setFollow] = useState(true);
  const [filter, setFilter] = useState("");
  const [liveLines, setLiveLines] = useState<LogLine[]>([]);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const history = useQuery(logHistoryQueryOptions(workspace, source));
  const historyEnd = history.data?.at(-1)?.id ?? 0;

  // Followed from the end of the history, so the stream sends only new lines.
  const streamKey = follow && history.isSuccess ? `${workspace}/${JSON.stringify(source)}` : null;
  const streamStatus = useReconnectingStream(streamKey, ({ signal, cursor, onOpen }) => {
    if ("request" in source) return Promise.resolve("done" as const);
    const after = cursor.value ? Number(cursor.value) : historyEnd;
    return followLogs(workspace, source, after, {
      signal,
      onOpen,
      onLine: (line) => {
        cursor.value = String(line.id);
        setLiveLines((previous) => {
          if (previous.some((known) => known.id === line.id)) return previous;
          const next = [...previous, line];
          return next.length > MAX_LIVE_LINES ? next.slice(-MAX_LIVE_LINES) : next;
        });
      },
    }).then((outcome) => (outcome === "finished" ? "done" : "reconnect"));
  });

  const lines = useMemo(() => mergeLines(history.data ?? [], liveLines), [history.data, liveLines]);

  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const filtered = needle ? lines.filter((line) => searchText(line).includes(needle)) : lines;
    return filtered.slice(-MAX_RENDERED_LINES);
  }, [filter, lines]);

  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (follow && node) node.scrollTop = node.scrollHeight;
  }, [follow, visible]);

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
            data-stream-stale={streamStatus === "reconnecting" ? "" : undefined}
          >
            <span
              className={cn(
                "size-1.5 rounded-full",
                streamStatus === "open"
                  ? "pulse-live bg-positive"
                  : streamStatus === "reconnecting"
                    ? "bg-warning"
                    : "bg-muted-foreground/50",
              )}
              aria-hidden="true"
            />
            {streamStatusLabel(streamStatus)}
          </span>
        ) : null}
        <CopyButton
          value={() => formatLines(visible)}
          label="visible logs"
          disabled={visible.length === 0}
        />
        {lines.length > MAX_RENDERED_LINES && !filter ? (
          <span className="text-[11px] text-muted-foreground">Latest 1,000 lines</span>
        ) : null}
        <Button
          variant="ghost"
          size="icon"
          disabled={lines.length === 0}
          aria-label="Download loaded logs"
          title="Download loaded logs"
          onClick={() => downloadLines(lines)}
        >
          <Download className="size-3.5" />
        </Button>
      </div>
      <div
        ref={scrollRef}
        data-log-scroll=""
        className="min-h-0 flex-1 overflow-auto bg-background/60"
      >
        {history.isPending ? (
          <div className="space-y-1.5 p-2" aria-hidden="true">
            {[80, 60, 90, 45, 70].map((width, index) => (
              <Skeleton key={index} className="h-4" style={{ width: `${width}%` }} />
            ))}
          </div>
        ) : history.isError ? (
          <ApiErrorNotice
            error={history.error}
            title="Logs could not be loaded"
            onRetry={() => void history.refetch()}
            retrying={history.isFetching}
          />
        ) : visible.length === 0 ? (
          <PanelEmpty message="No log lines" className="h-32" />
        ) : (
          <div
            className="content-transition mono py-2 text-xs leading-5"
            role="list"
            aria-label="Log output"
          >
            {visible.map((line, index) => (
              <LogLineRow
                key={line.id}
                line={line}
                showDate={index === 0 || dateKey(line) !== dateKey(visible[index - 1])}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function streamStatusLabel(status: EventStreamStatus): string {
  if (status === "open") return "Live";
  if (status === "reconnecting") return "Reconnecting · loaded logs";
  if (status === "connecting") return "Connecting";
  if (status === "error") return "Disconnected";
  return status === "closed" ? "Closed" : "Paused";
}

function LogLineRow({ line, showDate }: { line: LogLine; showDate: boolean }) {
  const showStream = line.stream !== "stdout";
  const stderr = line.stream === "stderr";
  const system = line.stream === "system";

  return (
    <div role="listitem">
      {showDate ? (
        <div className="flex items-center gap-2 px-3 py-1.5 text-[10px] text-muted-foreground">
          <span className="shrink-0">{formatLogDate(line.time)}</span>
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
          dateTime={line.time}
          title={line.time}
          aria-label={`Log timestamp ${line.time}`}
          className="shrink-0 tabular-nums text-muted-foreground"
        >
          {formatLogClock(line.time)}
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
              {line.stream}
            </span>
          ) : null}
          <span>{line.data}</span>
        </span>
      </div>
    </div>
  );
}

function formatLogClock(timestamp: string): string {
  const parsed = new Date(timestamp);
  if (!Number.isNaN(parsed.getTime())) return parsed.toISOString().slice(11, 19);
  return timestamp.includes("T") ? timestamp.split("T", 2)[1].slice(0, 8) : timestamp;
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

function dateKey(line: LogLine | undefined): string {
  if (!line) return "";
  const parsed = new Date(line.time);
  return Number.isNaN(parsed.getTime()) ? line.time.split("T", 1)[0] : parsed.toISOString().slice(0, 10);
}

function searchText(line: LogLine): string {
  return `${line.time} ${line.stream} ${line.data}`.toLowerCase();
}

/** History and live lines in id order, each once. */
function mergeLines(history: LogLine[], live: LogLine[]): LogLine[] {
  const byId = new Map<number, LogLine>();
  for (const line of [...history, ...live]) byId.set(line.id, line);
  return [...byId.values()].sort((a, b) => a.id - b.id);
}

function formatLines(lines: LogLine[]): string {
  return lines.map((line) => `${line.time} ${line.stream} ${line.data}`).join("\n");
}

function downloadLines(lines: LogLine[]): void {
  downloadBlob(
    "task-logs.txt",
    new Blob([formatLines(lines)], { type: "text/plain;charset=utf-8" }),
  );
}
