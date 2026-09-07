import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

const COPIED_FEEDBACK_MS = 1_500;

// A callback builds expensive values such as log buffers only when copied.
export function useCopyToClipboard(value: string | (() => string)): {
  copied: boolean;
  copy: () => void;
} {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const attempt = useRef(0);
  const errorToast = useRef<string | number | undefined>(undefined);

  useEffect(
    () => () => {
      attempt.current += 1;
      clearTimeout(timer.current);
      if (errorToast.current !== undefined) toast.dismiss(errorToast.current);
    },
    [],
  );

  const copy = () => {
    const currentAttempt = ++attempt.current;
    clearTimeout(timer.current);
    setCopied(false);
    if (errorToast.current !== undefined) toast.dismiss(errorToast.current);

    const write = async () => {
      try {
        if (!navigator.clipboard?.writeText) {
          errorToast.current = toast.error("Clipboard access is unavailable in this browser.");
          return;
        }
        const text = typeof value === "function" ? value() : value;
        await navigator.clipboard.writeText(text);
        if (attempt.current !== currentAttempt) return;
        setCopied(true);
        timer.current = setTimeout(() => setCopied(false), COPIED_FEEDBACK_MS);
      } catch {
        if (attempt.current !== currentAttempt) return;
        errorToast.current = toast.error(
          "Could not copy. Check clipboard permissions and try again.",
        );
      }
    };

    void write();
  };

  return { copied, copy };
}
