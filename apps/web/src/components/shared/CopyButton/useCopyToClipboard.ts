import { useEffect, useRef, useState } from "react";

/**
 * How long the control reports a successful copy. Long enough to read, short
 * enough that the reader connects it to the press they just made.
 */
const COPIED_FEEDBACK_MS = 1_500;

/**
 * Put a value on the clipboard and report, briefly, that it landed.
 *
 * The acknowledgement is time-boxed rather than latched: a check that never
 * clears reads as the state of the control instead of the result of one press,
 * and the next press then looks like it did nothing. The timer is cleared on
 * unmount because the drawer, dialog, or row holding the control can close
 * inside the window.
 *
 * `value` may be a function so a caller whose text is expensive to assemble —
 * a whole log buffer — builds it on the press rather than on every render.
 */
export function useCopyToClipboard(value: string | (() => string)): {
  copied: boolean;
  copy: () => void;
} {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => () => clearTimeout(timer.current), []);

  const copy = () => {
    const text = typeof value === "function" ? value() : value;
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), COPIED_FEEDBACK_MS);
    });
  };

  return { copied, copy };
}
