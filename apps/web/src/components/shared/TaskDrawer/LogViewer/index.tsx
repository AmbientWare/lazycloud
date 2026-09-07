import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Download, Pause, Play, Search } from "lucide-react";

import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { CopyButton } from "@/components/shared/CopyButton";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useEventStream } from "@/hooks/useEventStream";
import { logRecordSchema, type LogRecord } from "@/lib/api/schemas";
import { withWorkspace } from "@/lib/api/client";
import {
  logHistoryQueryOptions,
  logScopeParams,
  selectLogHistory,
  type LogScope,
} from "@/lib/queries/logs";
import { cn } from "@/lib/utils";

const MAX_LIVE_RECORDS = 2_000;

export function LogViewer({
  workspaceId,
  scope,
  follow: initialFollow = true,
  className,
}: {
  workspaceId: string;
  scope: LogScope;
  follow?: boolean;
  className?: string;
}) {
  const [follow, setFollow] = useState(initialFollow);
  const [filter, setFilter] = useState("");
  const [liveRecords, setLiveRecords] = useState<LogRecord[]>([]);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const history = useInfiniteQuery(logHistoryQueryOptions(workspaceId, scope));
  const historyList = useMemo(
    () => selectLogHistory(history.data, history.hasNextPage),
    [history.data, history.hasNextPage],
  );

  const streamParams = logScopeParams(scope);
  streamParams.set("follow", "true");
  const streamUrl = withWorkspace(`/api/v1/logs/stream?${streamParams.toString()}`, workspaceId);

  const streamStatus = useEventStream(follow ? streamUrl : null, {
    enabled: follow,
    onEvent: (event) => {
      const parsed = parseLogEvent(event.data);
      if (!parsed) return;
      setLiveRecords((previous) => {
        const next = [...previous, parsed];
        return next.length > MAX_LIVE_RECORDS ? next.slice(-MAX_LIVE_RECORDS) : next;
      });
    },
  });

  const records = useMemo(() => {
    return mergeLogRecords(historyList.items, liveRecords);
  }, [historyList.items, liveRecords]);

  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return needle ? records.filter((record) => logSearchText(record).includes(needle)) : records;
  }, [filter, records]);

  const getItemKey = useCallback((index: number) => recordKey(visible[index]), [visible]);
  const virtualizer = useVirtualizer({
    useFlushSync: false,
    count: visible.length,
    getScrollElement: () => scrollRef.current,
    getItemKey,
    estimateSize: () => 28,
    overscan: 12,
    paddingStart: 40,
    paddingEnd: 8,
    anchorTo: "end",
    followOnAppend: follow,
    scrollEndThreshold: 60,
    onChange: (instance, scrolling) => {
      if (!scrolling || instance.scrollDirection !== "backward") return;
      setFollow(false);
      if (
        (instance.scrollOffset ?? 0) < 160 &&
        history.hasNextPage &&
        !history.isFetching &&
        !history.isFetchNextPageError &&
        !filter.trim()
      ) {
        void history.fetchNextPage({ cancelRefetch: false });
      }
    },
  });
  const initialized = useRef(false);
  useLayoutEffect(() => {
    if (initialized.current || visible.length === 0) return;
    initialized.current = true;
    virtualizer.scrollToEnd();
  }, [virtualizer, visible.length]);
  const virtualRows = virtualizer.getVirtualItems();

  return (
    <div className={cn("flex min-h-0 flex-col", className)}>
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border p-2">
        <label className="flex h-8 min-w-40 flex-1 items-center gap-2 rounded-md border border-input bg-muted px-2 focus-within:border-ring">
          <Search className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="sr-only">Filter loaded logs</span>
          <input
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="Filter loaded logs"
            className="mono min-w-0 flex-1 bg-transparent text-xs text-foreground outline-none"
          />
        </label>
        <Button
          variant={follow ? "default" : "outline"}
          size="sm"
          onClick={() => {
            setFollow((previous) => !previous);
            if (!follow) virtualizer.scrollToEnd();
          }}
        >
          {follow ? <Pause className="size-3" /> : <Play className="size-3" />}
          {follow ? "Following" : "Follow"}
        </Button>
        {follow && streamStatus !== "open" ? (
          <span className="text-xs text-muted-foreground" role="status">
            {streamStatusLabel(streamStatus)}
          </span>
        ) : null}
        <CopyButton
          value={() => formatLogRecords(virtualRows.map((row) => visible[row.index]))}
          label="visible logs"
          disabled={visible.length === 0}
        />
        <Button
          variant="ghost"
          size="icon"
          disabled={records.length === 0}
          aria-label="Download loaded logs"
          title="Download loaded logs"
          onClick={() => downloadLogs(records)}
        >
          <Download className="size-3.5" />
        </Button>
      </div>
      <div
        ref={scrollRef}
        data-log-scroll=""
        className="min-h-0 flex-1 overflow-auto bg-background/60"
        tabIndex={0}
        aria-label="Log output"
      >
        {history.isPending ? (
          <div className="space-y-1.5 p-2" aria-hidden="true">
            {[80, 60, 90, 45, 70].map((width, index) => (
              <Skeleton key={index} className="h-4" style={{ width: `${width}%` }} />
            ))}
          </div>
        ) : history.isError && !history.data ? (
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
            className="mono relative w-full text-xs leading-5"
            style={{ height: virtualizer.getTotalSize() }}
            role="list"
            aria-label="Log output"
          >
            <div className="absolute inset-x-0 top-0 flex h-10 items-center justify-center text-muted-foreground">
              {history.isFetchNextPageError ? (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => void history.fetchNextPage({ cancelRefetch: false })}
                >
                  Retry older logs
                </Button>
              ) : history.isFetchingNextPage ? (
                <span role="status">Loading older logs</span>
              ) : historyList.nextCursor ? (
                "Scroll up for older logs"
              ) : (
                "Beginning of loaded history"
              )}
            </div>
            {virtualRows.map((row) => (
              <div
                key={row.key}
                data-index={row.index}
                ref={virtualizer.measureElement}
                className="absolute left-0 top-0 w-full"
                style={{ transform: `translateY(${row.start}px)` }}
              >
                <LogLine
                  record={visible[row.index]}
                  showDate={
                    row.index === 0 ||
                    logDateKey(visible[row.index]) !== logDateKey(visible[row.index - 1])
                  }
                />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function streamStatusLabel(status: ReturnType<typeof useEventStream>): string {
  if (status === "open") return "Live";
  if (status === "reconnecting") return "Reconnecting · loaded logs";
  if (status === "connecting") return "Connecting";
  if (status === "error") return "Disconnected";
  return status === "closed" ? "Closed" : "Paused";
}

function LogLine({ record, showDate }: { record: LogRecord; showDate: boolean }) {
  const stream = record.stream.trim().toLowerCase();
  const showStream = Boolean(stream && stream !== "stdout");
  const stderr = stream === "stderr";
  const system = stream === "system";

  return (
    <div role="listitem">
      {showDate ? (
        <div className="flex items-center gap-2 px-3 py-1.5 text-[10px] text-muted-foreground">
          <span className="shrink-0">{formatLogDate(record.timestamp)}</span>
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
          dateTime={record.timestamp}
          title={record.timestamp}
          aria-label={`Log timestamp ${record.timestamp}`}
          className="shrink-0 tabular-nums text-muted-foreground"
        >
          {formatLogClock(record.timestamp)}
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
              {stream}
            </span>
          ) : null}
          <span>{record.message}</span>
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

function logDateKey(record: LogRecord | undefined): string {
  if (!record) return "";
  const parsed = new Date(record.timestamp);
  return Number.isNaN(parsed.getTime())
    ? record.timestamp.split("T", 1)[0]
    : parsed.toISOString().slice(0, 10);
}

function logSearchText(record: LogRecord): string {
  return [
    record.timestamp,
    record.stream,
    record.message,
    record.app_id,
    record.stub_id,
    record.task_id,
    record.container_id,
  ]
    .join(" ")
    .toLowerCase();
}

function parseLogEvent(data: string): LogRecord | null {
  try {
    const parsed = logRecordSchema.safeParse(JSON.parse(data));
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

function mergeLogRecords(history: LogRecord[], live: LogRecord[]): LogRecord[] {
  const seen = new Set<string>();
  const merged: LogRecord[] = [];
  for (const record of [...history, ...live]) {
    const key = recordKey(record);
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(record);
  }
  merged.sort(compareLogRecords);
  return merged;
}

function recordKey(record: LogRecord): string {
  return record.id || `${record.timestamp}-${record.seq_num}-${record.message}`;
}

function compareLogRecords(a: LogRecord, b: LogRecord): number {
  if (a.timestamp !== b.timestamp) return a.timestamp.localeCompare(b.timestamp);
  return a.seq_num - b.seq_num;
}

function formatLogRecords(records: LogRecord[]): string {
  return records
    .map((record) => {
      const context = [
        record.stream,
        record.app_id,
        record.stub_id,
        record.task_id,
        record.container_id,
      ]
        .filter(Boolean)
        .join(" ");
      return `${record.timestamp}${context ? ` ${context}` : ""} ${record.message}`;
    })
    .join("\n");
}

function downloadLogs(records: LogRecord[]): void {
  const url = URL.createObjectURL(
    new Blob([formatLogRecords(records)], { type: "text/plain;charset=utf-8" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "task-logs.txt";
  anchor.click();
  URL.revokeObjectURL(url);
}
