"use client";

import { type FormEvent, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export function SignIn({ error, onKey }: { error: string | null; onKey: (key: string) => void }) {
  const [key, setKey] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (key.trim()) onKey(key.trim());
  };

  return (
    <main className="grid min-h-dvh content-end gap-12 bg-plate p-6 text-plate-foreground sm:p-12 lg:grid-cols-[1fr_24rem] lg:items-end lg:gap-16 lg:p-16">
      <h1 className="text-[clamp(4.5rem,14vw,12rem)] leading-[0.8] font-bold tracking-[-0.045em]">
        Image
        <br />
        studio
      </h1>
      <form onSubmit={submit} className="grid gap-6 border-t border-plate-line pt-6">
        <p className="font-serif text-xl leading-snug text-plate-muted italic">
          FLUX.2 [klein] on a LazyCloud GPU that stops when nobody is generating.
        </p>
        <div className="grid gap-2">
          <label htmlFor="studio-key" className="text-sm text-plate-muted">
            Studio key
          </label>
          <Input
            id="studio-key"
            type="password"
            autoComplete="current-password"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "studio-key-error" : "studio-key-hint"}
            autoFocus
            className="h-11 rounded-full border-plate-line bg-white/10 px-4 text-base text-plate-foreground focus-visible:border-primary focus-visible:ring-primary/40 aria-invalid:border-plate-error aria-invalid:ring-plate-error/30 dark:bg-white/10"
          />
          {error ? (
            <p id="studio-key-error" className="text-sm text-plate-error">
              {error}
            </p>
          ) : (
            <p id="studio-key-hint" className="text-sm text-plate-muted">
              The key you chose when you ran configure.
            </p>
          )}
        </div>
        <Button
          type="submit"
          disabled={!key.trim()}
          className="h-11 rounded-full text-base font-semibold hover:bg-primary/90 disabled:bg-plate-foreground/15 disabled:text-plate-muted disabled:opacity-100"
        >
          Open the studio
        </Button>
      </form>
    </main>
  );
}
