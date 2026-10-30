import { useEffect, useRef, useState, type FocusEvent } from "react";
import { Check, Loader2, X, type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/** How long an armed control waits for an answer before it folds back. */
const ARMED_MS = 5000;

/**
 * An icon control that asks before it acts, in place of a confirmation dialog.
 *
 * The first press arms it: the icon turns into a cancel mark and a confirm mark
 * slides out to its left. Cancel stays where the icon was and keeps focus, so a
 * double click or a second Enter cancels rather than confirming. Escape, focus leaving, or five idle
 * seconds fold it back. Use it where the action and its target are plain from
 * the row it sits in; keep a dialog for anything that needs explaining.
 *
 * `label` names the action, such as "Delete feedback".
 */
export function ConfirmButton({
  label,
  icon: Icon,
  onConfirm,
  onArm,
  pending = false,
  disabled = false,
  className,
}: {
  label: string;
  icon: LucideIcon;
  onConfirm: () => void;
  /** Runs when the control arms, to put away anything the action would race. */
  onArm?: () => void;
  pending?: boolean;
  disabled?: boolean;
  className?: string;
}) {
  const [armed, setArmed] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const wasPending = useRef(pending);

  const disarm = (refocus: boolean) => {
    setArmed(false);
    if (refocus) requestAnimationFrame(() => trigger.current?.focus());
  };

  useEffect(() => {
    if (!armed || pending) return;
    const timer = setTimeout(() => setArmed(false), ARMED_MS);
    return () => clearTimeout(timer);
  }, [armed, pending]);

  // An answer folds it back; a failure is for the caller to report.
  useEffect(() => {
    if (wasPending.current && !pending) setArmed(false);
    wasPending.current = pending;
  }, [pending]);

  return (
    <span
      role="group"
      aria-label={label}
      data-armed={armed}
      onKeyDown={(event) => {
        if (event.key === "Escape" && armed && !pending) {
          event.stopPropagation();
          disarm(true);
        }
      }}
      onBlur={(event: FocusEvent<HTMLSpanElement>) => {
        if (pending || event.currentTarget.contains(event.relatedTarget)) return;
        setArmed(false);
      }}
      className={cn(
        "relative inline-flex h-8 shrink-0 flex-row-reverse items-center justify-start gap-1 overflow-hidden",
        "transition-[width] duration-200 ease-out motion-reduce:transition-none",
        armed ? "w-[4.25rem]" : "w-8",
        className,
      )}
    >
      <button
        ref={trigger}
        type="button"
        disabled={disabled || pending}
        aria-label={armed ? "Cancel" : label}
        title={armed ? "Cancel" : label}
        onClick={() => {
          if (armed) return disarm(true);
          onArm?.();
          setArmed(true);
        }}
        className={cn(
          "flex size-8 shrink-0 items-center justify-center rounded-md outline-none transition-colors",
          "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset disabled:opacity-50",
          armed
            ? "bg-muted text-foreground hover:bg-muted/60"
            : "text-muted-foreground hover:bg-destructive/10 hover:text-destructive",
        )}
      >
        {armed ? (
          <X
            key="cancel"
            aria-hidden="true"
            className="size-4 animate-in spin-in-90 fade-in-0 duration-200 motion-reduce:animate-none"
          />
        ) : (
          <Icon key="idle" aria-hidden="true" className="size-4" />
        )}
      </button>
      <button
        type="button"
        tabIndex={armed ? 0 : -1}
        aria-hidden={!armed}
        // Stays enabled while the action runs: disabling it would drop focus
        // and fold the control back mid-request.
        disabled={!armed}
        aria-disabled={pending}
        aria-label={`Confirm: ${label}`}
        title={`Confirm: ${label}`}
        onClick={() => {
          if (!pending) onConfirm();
        }}
        className={cn(
          "flex size-8 shrink-0 items-center justify-center rounded-md bg-destructive text-destructive-foreground outline-none",
          "transition-[opacity,translate,background-color] duration-200 ease-out motion-reduce:transition-none",
          "hover:bg-destructive/85 focus-visible:ring-2 focus-visible:ring-foreground focus-visible:ring-offset-2 focus-visible:ring-offset-background",
          armed ? "translate-x-0 opacity-100" : "pointer-events-none translate-x-3 opacity-0",
        )}
      >
        {pending ? (
          <Loader2 aria-hidden="true" className="size-4 animate-spin" />
        ) : (
          <Check aria-hidden="true" className="size-4" />
        )}
      </button>
    </span>
  );
}
