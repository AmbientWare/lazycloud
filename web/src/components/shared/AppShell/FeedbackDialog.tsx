import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { CircleCheck, Loader2 } from "lucide-react";

import { FeedbackKindIcon } from "@/components/shared/FeedbackKindIcon";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { Schemas } from "@/lib/api/client";
import { FEEDBACK_KINDS, FEEDBACK_MAX_LENGTH, submitFeedback } from "@/lib/queries/feedback";
import { cn } from "@/lib/utils";

type Kind = Schemas["FeedbackKind"];

const PROMPTS: Record<Kind, string> = {
  feature: "What should LazyCloud do that it doesn't yet?",
  bug: "What happened, and what did you expect instead?",
  support: "What do you need help with?",
};

/** The counter turns to a warning once this share of the limit is used. */
const NEAR_LIMIT = 0.9;

export function FeedbackDialog({ onClose }: { onClose: () => void }) {
  const [kind, setKind] = useState<Kind>("feature");
  const [message, setMessage] = useState("");
  const send = useMutation({ mutationFn: () => submitFeedback(kind, message.trim()) });
  const empty = message.trim().length === 0;
  const submit = () => {
    if (empty || send.isPending) return;
    send.mutate();
  };

  return (
    <Dialog open onOpenChange={(next) => (next || send.isPending ? undefined : onClose())}>
      <DialogContent className="sm:max-w-lg">
        {send.isSuccess ? (
          <div className="flex flex-col items-start gap-4">
            <CircleCheck className="size-6 text-positive" aria-hidden="true" />
            <DialogHeader>
              <DialogTitle>Feedback sent</DialogTitle>
              <DialogDescription>Administrators read every message.</DialogDescription>
            </DialogHeader>
            <Button type="button" size="sm" onClick={onClose} autoFocus>
              Done
            </Button>
          </div>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Send feedback</DialogTitle>
              <DialogDescription>Goes straight to the LazyCloud administrators.</DialogDescription>
            </DialogHeader>
            <form
              className="flex flex-col gap-4"
              onSubmit={(event) => {
                event.preventDefault();
                submit();
              }}
            >
              <div role="radiogroup" aria-label="Feedback type" className="grid grid-cols-3 gap-2">
                {FEEDBACK_KINDS.map((option) => {
                  const selected = option.value === kind;
                  return (
                    <button
                      key={option.value}
                      type="button"
                      role="radio"
                      aria-checked={selected}
                      disabled={send.isPending}
                      onClick={() => setKind(option.value)}
                      className={cn(
                        "flex flex-col items-start gap-2 rounded-md border px-3 py-2.5 text-left text-xs outline-none transition-colors",
                        "focus-visible:ring-2 focus-visible:ring-ring",
                        selected
                          ? "border-brand/60 bg-brand/10 font-medium text-foreground"
                          : "border-border text-muted-foreground hover:border-foreground/25 hover:text-foreground",
                      )}
                    >
                      <FeedbackKindIcon
                        kind={option.value}
                        className={cn("size-4", selected && "text-brand")}
                      />
                      {option.label}
                    </button>
                  );
                })}
              </div>
              <div>
                <textarea
                  autoFocus
                  aria-label="Message"
                  rows={6}
                  maxLength={FEEDBACK_MAX_LENGTH}
                  disabled={send.isPending}
                  value={message}
                  onChange={(event) => setMessage(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                      event.preventDefault();
                      submit();
                    }
                  }}
                  placeholder={PROMPTS[kind]}
                  className="block w-full resize-y rounded-md border border-input bg-background p-3 text-sm leading-relaxed text-foreground outline-none placeholder:text-muted-foreground/70 focus:border-ring"
                />
                <p
                  aria-live="polite"
                  className={cn(
                    "mt-1.5 text-right text-xs tabular-nums text-muted-foreground",
                    message.length >= FEEDBACK_MAX_LENGTH * NEAR_LIMIT && "text-warning",
                  )}
                >
                  {message.length.toLocaleString()} / {FEEDBACK_MAX_LENGTH.toLocaleString()}
                </p>
              </div>
              {send.isError ? (
                <p role="alert" className="text-xs text-destructive">
                  {send.error.message}
                </p>
              ) : null}
              <div className="flex items-center justify-end gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={send.isPending}
                  onClick={onClose}
                >
                  Cancel
                </Button>
                <Button type="submit" size="sm" disabled={send.isPending || empty}>
                  {send.isPending ? <Loader2 className="size-3.5 animate-spin" /> : null}
                  Send feedback
                </Button>
              </div>
            </form>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
