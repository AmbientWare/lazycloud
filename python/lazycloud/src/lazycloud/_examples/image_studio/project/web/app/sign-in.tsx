"use client";

import { type FormEvent, useState } from "react";

export function SignIn({ error, onKey }: { error: string | null; onKey: (key: string) => void }) {
  const [key, setKey] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (key.trim()) onKey(key.trim());
  };

  return (
    <main className="sign-in">
      <form className="panel" onSubmit={submit}>
        <h1>Image studio</h1>
        <p className="muted">Enter the studio key you chose when you configured the app.</p>
        <input
          type="password"
          autoComplete="current-password"
          placeholder="Studio key"
          value={key}
          onChange={(event) => setKey(event.target.value)}
          autoFocus
        />
        {error && <p className="error">{error}</p>}
        <button type="submit" className="primary" disabled={!key.trim()}>
          Open the studio
        </button>
      </form>
    </main>
  );
}
