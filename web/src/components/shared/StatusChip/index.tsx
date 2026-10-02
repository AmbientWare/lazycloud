import { Badge } from "@/components/ui/badge";

type Tone = "success" | "warning" | "danger" | "muted";

/**
 * Green for working and done, amber for waiting, red for failure. Anything
 * else, cancelled and stopped among them, stays neutral.
 */
const TONES: Record<string, Tone> = {
  // Tasks and requests.
  queued: "warning",
  running: "success",
  succeeded: "success",
  failed: "danger",
  // Containers.
  pending: "warning",
  starting: "warning",
  ready: "success",
  // Other resources' states and the labels settings pages derive from them.
  ok: "success",
  active: "success",
  deployed: "success",
  true: "success",
  complete: "success",
  completed: "success",
  success: "success",
  healthy: "success",
  retry: "warning",
  retrying: "warning",
  building: "warning",
  warning: "warning",
  error: "danger",
  timeout: "danger",
  expired: "danger",
  unhealthy: "danger",
  "not ok": "danger",
};

/** The only filled chip in the app; strictly for status values, shown as the API words them. */
export function StatusChip({ status, live = false }: { status: string; live?: boolean }) {
  return (
    <Badge tone={TONES[status.toLowerCase()] ?? "muted"} className="gap-1.5">
      {live ? (
        <span className="pulse-live size-1.5 rounded-full bg-current" aria-hidden="true" />
      ) : null}
      {status || "None"}
    </Badge>
  );
}
