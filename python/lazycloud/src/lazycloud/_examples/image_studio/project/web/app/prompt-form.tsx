"use client";

import {
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
  useState,
} from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

import type { AspectChoice, GenerateRequest, Options } from "./api";

const CHOICE =
  "h-9 rounded-full border border-plate-line px-3 text-plate-foreground sm:px-3.5 hover:bg-white/10 hover:text-plate-foreground data-[state=on]:border-plate-foreground data-[state=on]:bg-plate-foreground data-[state=on]:text-plate focus-visible:border-primary focus-visible:ring-3 focus-visible:ring-primary/60";

export function PromptForm({
  options,
  draft,
  promptRef,
  onSubmit,
}: {
  options: Options;
  draft: GenerateRequest;
  promptRef: RefObject<HTMLTextAreaElement | null>;
  onSubmit: (request: GenerateRequest) => Promise<void>;
}) {
  const [request, setRequest] = useState(draft);
  const [submitting, setSubmitting] = useState(false);
  const update = (change: Partial<GenerateRequest>) => setRequest({ ...request, ...change });
  const ready = request.prompt.trim().length > 0 && !submitting;
  const nearLimit = request.prompt.length > options.max_prompt_chars * 0.9;
  const shortcut = navigator.platform.startsWith("Mac") ? "⌘ ↵" : "Ctrl ↵";

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!ready) return;
    setSubmitting(true);
    try {
      await onSubmit({ ...request, prompt: request.prompt.trim() });
    } finally {
      setSubmitting(false);
    }
  };

  const submitOnShortcut = (event: KeyboardEvent) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void submit();
  };

  return (
    <form
      onSubmit={submit}
      className="grid gap-6 rounded-2xl bg-plate p-5 text-plate-foreground shadow-[0_24px_60px_-30px_rgb(19_59_132/70%)] sm:p-8"
    >
      <div className="grid gap-2">
        <label htmlFor="prompt" className="sr-only">
          Prompt
        </label>
        <Textarea
          id="prompt"
          ref={promptRef}
          rows={2}
          maxLength={options.max_prompt_chars}
          placeholder="A lighthouse on a cliff at dusk, waves below"
          value={request.prompt}
          onChange={(event) => update({ prompt: event.target.value })}
          onKeyDown={submitOnShortcut}
          aria-describedby="prompt-count"
          className="max-h-72 min-h-18 resize-none rounded-none border-0 bg-transparent p-0 font-serif text-[1.6rem] leading-snug text-plate-foreground shadow-none placeholder:text-plate-muted/60 focus-visible:ring-0 sm:text-[2rem] md:text-[2rem] dark:bg-transparent"
        />
        <p
          id="prompt-count"
          className={`justify-self-end text-xs tabular-nums ${nearLimit ? "text-primary" : "text-plate-muted"}`}
        >
          {request.prompt.length} / {options.max_prompt_chars}
        </p>
      </div>

      <div className="grid gap-5 border-t border-plate-line pt-5">
        <Group label="Style">
          <ToggleGroup
            type="single"
            aria-label="Style"
            value={request.style}
            onValueChange={(style) => style && update({ style })}
            className="-mx-5 w-auto max-w-none overflow-x-auto px-5 [scrollbar-width:none] sm:mx-0 sm:flex-wrap sm:px-0"
          >
            {options.styles.map((style) => (
              <ToggleGroupItem key={style.id} value={style.id} className={CHOICE}>
                {style.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </Group>

        <div className="flex flex-wrap items-end gap-x-6 gap-y-5 sm:gap-x-8">
          <Group label="Shape">
            <ToggleGroup
              type="single"
              aria-label="Shape"
              value={request.aspect}
              onValueChange={(aspect) => aspect && update({ aspect })}
              className="flex-wrap"
            >
              {options.aspects.map((aspect) => (
                <ToggleGroupItem
                  key={aspect.id}
                  value={aspect.id}
                  title={`${aspect.width} × ${aspect.height}`}
                  className={`${CHOICE} gap-2`}
                >
                  <ShapeIcon aspect={aspect} />
                  {aspect.label}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </Group>

          <Group label="Images">
            <ToggleGroup
              type="single"
              aria-label="Images"
              value={String(request.count)}
              onValueChange={(count) => count && update({ count: Number(count) })}
            >
              {Array.from({ length: options.max_images }, (_, n) => String(n + 1)).map((count) => (
                <ToggleGroupItem key={count} value={count} className={`${CHOICE} w-9 px-0`}>
                  {count}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </Group>

          <div className="grid gap-2">
            <label htmlFor="seed" className="text-sm text-plate-muted">
              Seed
            </label>
            <Input
              id="seed"
              type="number"
              inputMode="numeric"
              min={0}
              max={2 ** 32 - 1}
              placeholder="Random"
              value={request.seed ?? ""}
              onChange={(event) =>
                update({ seed: event.target.value === "" ? null : Number(event.target.value) })
              }
              className="h-9 w-28 rounded-full border-plate-line bg-transparent px-3.5 text-plate-foreground placeholder:text-plate-muted/70 focus-visible:border-plate-foreground focus-visible:ring-plate-foreground/30 dark:bg-transparent"
            />
          </div>

          <Button
            type="submit"
            disabled={!ready}
            className="h-12 w-full gap-3 rounded-full px-7 text-base font-semibold hover:bg-primary/90 focus-visible:ring-primary/50 disabled:bg-plate-foreground/15 disabled:text-plate-muted disabled:opacity-100 sm:ml-auto sm:w-auto"
          >
            {submitting ? "Queuing…" : "Generate"}
            <kbd className="hidden font-sans text-xs font-normal opacity-60 sm:inline">
              {shortcut}
            </kbd>
          </Button>
        </div>
      </div>
    </form>
  );
}

function Group({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid gap-2">
      <span className="text-sm text-plate-muted" aria-hidden>
        {label}
      </span>
      {children}
    </div>
  );
}

function ShapeIcon({ aspect }: { aspect: AspectChoice }) {
  const scale = 14 / Math.max(aspect.width, aspect.height);
  return (
    <span
      aria-hidden
      className="inline-block rounded-[2px] border-[1.5px] border-current"
      style={{ width: aspect.width * scale, height: aspect.height * scale }}
    />
  );
}
