import { useId, useRef, useState } from "react";
import { ChevronUp, LogOut, Settings } from "lucide-react";

import { useSession } from "@/components/shared/AuthGate/session";
import { cn } from "@/lib/utils";

/**
 * The account end of the rail: who is signed in, and everything that acts on
 * them rather than on a workspace.
 *
 * The panel is rendered above the trigger and the trigger is pinned last, so
 * opening it grows upward into the rail instead of pushing the navigation off
 * the bottom of the screen.
 */
export function AccountRail({
  children,
  settingsOpen,
  onOpenSettings,
  onLogout,
}: {
  children: React.ReactNode;
  settingsOpen: boolean;
  onOpenSettings: () => void;
  onLogout: () => void;
}) {
  const { user } = useSession();
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const container = useRef<HTMLDivElement>(null);

  return (
    <div
      ref={container}
      className="mt-auto border-t border-sidebar-border px-3 py-3"
      onKeyDown={(event) => {
        if (event.key !== "Escape" || !open) return;
        setOpen(false);
        container.current?.querySelector<HTMLButtonElement>("[data-account-trigger]")?.focus();
      }}
    >
      {open ? (
        <div id={panelId} className="mb-1 space-y-0.5">
          <nav aria-label="Account navigation" className="space-y-0.5">
            {children}
            <button
              type="button"
              onClick={() => {
                setOpen(false);
                onOpenSettings();
              }}
              data-selected={settingsOpen}
              className={cn(
                "interactive-row flex h-9 w-full items-center gap-2.5 rounded-md px-2.5 text-[13px] outline-none transition-colors focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-sidebar-ring",
                settingsOpen
                  ? "font-medium text-sidebar-foreground"
                  : "text-muted-foreground hover:text-sidebar-foreground",
              )}
            >
              <Settings className="size-4" aria-hidden="true" />
              Settings
            </button>
          </nav>
          <button
            type="button"
            onClick={onLogout}
            className="interactive-row flex h-9 w-full items-center gap-2.5 rounded-md px-2.5 text-[13px] text-muted-foreground hover:text-foreground"
          >
            <LogOut className="size-4" aria-hidden="true" />
            Sign out
          </button>
        </div>
      ) : null}

      <button
        type="button"
        data-account-trigger=""
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((value) => !value)}
        className="interactive-row flex h-9 w-full items-center gap-2.5 rounded-md px-2.5 text-left text-[13px] text-muted-foreground outline-none hover:text-sidebar-foreground focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-sidebar-ring"
      >
        {user.avatar_url ? (
          <img src={user.avatar_url} alt="" className="size-5 shrink-0 rounded-full" />
        ) : (
          <span
            aria-hidden="true"
            className="flex size-5 shrink-0 items-center justify-center rounded-full bg-muted text-[10px] font-medium text-muted-foreground"
          >
            {user.display_name.slice(0, 1).toUpperCase()}
          </span>
        )}
        <span className="min-w-0 flex-1 truncate">{user.display_name}</span>
        <ChevronUp
          aria-hidden="true"
          className={cn("size-3.5 shrink-0 transition-transform", open && "rotate-180")}
        />
      </button>
    </div>
  );
}
