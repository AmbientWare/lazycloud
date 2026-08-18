import { cn } from "@/lib/utils";

/**
 * The failure a framed region shows in place of the content it could not load.
 *
 * `role="alert"` is unconditional because every one of these replaces something
 * that was already on screen loading, so it arrives after the reader is there.
 *
 * `layout="framed"` draws the panel itself, for a region whose whole content is
 * the failure and which nothing else frames. Inside a `Panel` it would be a card
 * within a card, so the default stays unframed.
 */
export function PanelError({
  message,
  layout = "inline",
}: {
  message: string;
  layout?: "inline" | "centered" | "framed";
}) {
  return (
    <p
      role="alert"
      className={cn(
        "p-4 text-sm text-destructive",
        layout === "centered" && "flex min-h-0 flex-1 items-center justify-center",
        layout === "framed" && "panel flex min-h-48 items-center justify-center rounded-md",
      )}
    >
      {message}
    </p>
  );
}
