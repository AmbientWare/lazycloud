"use client";

import { type FormEvent, type KeyboardEvent, useState } from "react";

import type { GenerateRequest, Options } from "./api";

export function PromptForm({
  options,
  draft,
  onSubmit,
}: {
  options: Options;
  draft: GenerateRequest;
  onSubmit: (request: GenerateRequest) => Promise<void>;
}) {
  const [request, setRequest] = useState(draft);
  const [submitting, setSubmitting] = useState(false);
  const update = (change: Partial<GenerateRequest>) => setRequest({ ...request, ...change });
  const ready = request.prompt.trim().length > 0 && !submitting;

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
    <form className="panel composer" onSubmit={submit}>
      <label className="field">
        <span>Prompt</span>
        <textarea
          rows={5}
          maxLength={options.max_prompt_chars}
          placeholder="A lighthouse on a cliff at dusk, waves below"
          value={request.prompt}
          onChange={(event) => update({ prompt: event.target.value })}
          onKeyDown={submitOnShortcut}
        />
        <small className="muted">
          {request.prompt.length} / {options.max_prompt_chars}
        </small>
      </label>

      <fieldset className="field">
        <legend>Style</legend>
        <div className="chips">
          {options.styles.map((style) => (
            <button
              type="button"
              key={style.id}
              className={style.id === request.style ? "chip selected" : "chip"}
              onClick={() => update({ style: style.id })}
            >
              {style.label}
            </button>
          ))}
        </div>
      </fieldset>

      <fieldset className="field">
        <legend>Shape</legend>
        <div className="segmented">
          {options.aspects.map((aspect) => (
            <button
              type="button"
              key={aspect.id}
              className={aspect.id === request.aspect ? "selected" : ""}
              onClick={() => update({ aspect: aspect.id })}
            >
              {aspect.label}
            </button>
          ))}
        </div>
      </fieldset>

      <div className="row">
        <fieldset className="field">
          <legend>Images</legend>
          <div className="segmented">
            {Array.from({ length: options.max_images }, (_, n) => n + 1).map((count) => (
              <button
                type="button"
                key={count}
                className={count === request.count ? "selected" : ""}
                onClick={() => update({ count })}
              >
                {count}
              </button>
            ))}
          </div>
        </fieldset>
        <label className="field">
          <span>Seed</span>
          <input
            type="number"
            min={0}
            max={2 ** 32 - 1}
            placeholder="Random"
            value={request.seed ?? ""}
            onChange={(event) =>
              update({ seed: event.target.value === "" ? null : Number(event.target.value) })
            }
          />
        </label>
      </div>

      <button type="submit" className="primary" disabled={!ready}>
        {submitting ? "Queuing…" : "Generate"}
      </button>
    </form>
  );
}
