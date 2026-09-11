import { useLiveNow } from "@/hooks/use-live-now";
import type { Task } from "@/lib/api/schemas/tasks";

export function TaskPendingNotice({ task, compact = false }: { task: Task; compact?: boolean }) {
  const pending = task.status === "pending" || task.status === "retry";
  const progress = pending ? task.pending_progress : null;
  const now = useLiveNow(Boolean(progress));
  if (!progress) return null;
  const elapsed = Math.max(0, Math.floor((now - Date.parse(progress.pending_since)) / 1_000));
  if (elapsed < 5) return null;
  const fresh = now - Date.parse(progress.observed_at) <= 30_000;
  const message = fresh ? progress.message : "Waiting for an update on this function.";

  if (compact) {
    return (
      <span className="mt-1 block max-w-64 truncate text-xs text-muted-foreground" title={message}>
        {message}
      </span>
    );
  }
  return (
    <div className="mx-4 mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border px-3 py-2 text-sm">
      <span role="status">{message}</span>
      <span className="font-mono text-xs text-muted-foreground">Waiting {elapsed}s</span>
    </div>
  );
}
