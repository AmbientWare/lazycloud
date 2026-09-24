import { useRef, type ReactNode, type RefObject } from "react";
import { Loader2 } from "lucide-react";

import { ContentTransition } from "@/components/shared/ContentTransition";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

/** The dialog a file opens in: its name, one line about it, header actions, and the preview. */
export function FilePreviewDialog({
  open,
  onOpenChange,
  title,
  description,
  actions,
  returnFocus,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: ReactNode;
  actions?: ReactNode;
  /** Where focus goes when the dialog closes, since no trigger element opened it. */
  returnFocus: RefObject<HTMLElement | null>;
  children: ReactNode;
}) {
  const titleRef = useRef<HTMLHeadingElement>(null);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="flex h-[min(40rem,80svh)] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          titleRef.current?.focus();
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          returnFocus.current?.focus();
        }}
      >
        <DialogHeader className="shrink-0 flex-row items-start gap-3 border-b py-3 pr-12 pl-4 text-left">
          <div className="grid min-w-0 flex-1 gap-1">
            <DialogTitle
              ref={titleRef}
              tabIndex={-1}
              className="mono truncate text-sm outline-none"
              title={title}
            >
              {title}
            </DialogTitle>
            <DialogDescription className="truncate text-xs">{description}</DialogDescription>
          </div>
          {actions}
        </DialogHeader>
        {children}
      </DialogContent>
    </Dialog>
  );
}

/** The region a preview renders in, with a centered spinner while it loads. */
export function FilePreviewBody({ pending, children }: { pending: boolean; children: ReactNode }) {
  return (
    <div
      aria-busy={pending}
      className="relative flex min-h-0 flex-1 flex-col overflow-hidden bg-card"
    >
      {pending && (
        <ContentTransition
          pending
          role="status"
          aria-label="Loading preview"
          className="absolute inset-0 flex items-center justify-center"
        >
          <Loader2
            className="size-5 animate-spin text-muted-foreground motion-reduce:animate-none"
            aria-hidden="true"
          />
        </ContentTransition>
      )}
      {children}
    </div>
  );
}

/**
 * An image from an object URL. An `<img>` never runs script, which is what makes
 * it the one safe way to show an SVG someone else wrote.
 */
export function ImagePreview({ url, alt }: { url: string; alt: string }) {
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center p-3">
      <img src={url} alt={alt} className="max-h-full max-w-full object-contain" />
    </div>
  );
}

export function TextPreview({ text, className }: { text: string; className?: string }) {
  return (
    <pre
      className={cn(
        "mono min-h-0 flex-1 overflow-auto p-4 text-xs whitespace-pre-wrap break-words",
        className,
      )}
    >
      {text}
    </pre>
  );
}
