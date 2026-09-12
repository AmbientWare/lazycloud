import { useLiveNow } from "@/hooks/use-live-now";
import { durationBetween, exactTime, relativeTime } from "@/lib/format";

export function LiveRelativeTime({
  value,
  className,
}: {
  value: string | undefined;
  className?: string;
}) {
  const now = useLiveNow(Boolean(value));
  if (!value) return <>None</>;
  return (
    <time dateTime={value} title={exactTime(value)} className={className} suppressHydrationWarning>
      {relativeTime(value, now)}
    </time>
  );
}

export function LiveDuration({
  startedAt,
  finishedAt,
  fallback = "—",
}: {
  startedAt: string | null | undefined;
  finishedAt: string | null | undefined;
  fallback?: string;
}) {
  const now = useLiveNow(Boolean(startedAt && !finishedAt));
  return (
    <span suppressHydrationWarning>{durationBetween(startedAt, finishedAt, now) ?? fallback}</span>
  );
}
