import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * The full-viewport card the screens outside the app shell are made of: sign-in,
 * the OAuth callback, device approval, and the workspace states that have no
 * shell to render into.
 *
 * The screen carries the scroll rather than the page, because `html` and `body`
 * are `overflow: hidden` for the shell's sake — a card taller than the viewport
 * would otherwise have no way to reach its own bottom.
 */
export function PreShellScreen({
  children,
  width = "md",
  role,
}: {
  children: ReactNode;
  width?: "md" | "lg";
  role?: "alert";
}) {
  return (
    <main className="flex h-dvh items-center justify-center overflow-auto bg-background p-4">
      <section
        role={role}
        className={cn("panel w-full rounded-md p-5", width === "lg" ? "max-w-lg" : "max-w-md")}
      >
        {children}
      </section>
    </main>
  );
}
