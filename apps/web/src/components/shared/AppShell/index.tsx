import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Link,
  Outlet,
  useNavigate,
  useRouter,
  useRouterState,
  useSearch,
} from "@tanstack/react-router";
import {
  Activity,
  ChartNoAxesCombined,
  Database,
  LayoutGrid,
  Loader2,
  LogOut,
  Menu,
  Moon,
  Plus,
  Search,
  Settings,
  Sun,
} from "lucide-react";

import { WorkspaceSwitcher } from "@/components/shared/AppShell/WorkspaceSwitcher";
import { useSession } from "@/components/shared/AuthGate/session";
import { DrawerHeader } from "@/components/shared/DrawerHeader";
import { SettingsDialog } from "@/components/shared/SettingsDialog";
import { settingsView, type SettingsView } from "@/components/shared/SettingsDialog/view";
import { useTheme } from "@/components/shared/ThemeProvider/theme";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { currentSessionQueryOptions } from "@/lib/queries/auth";
import { createWorkspace } from "@/lib/queries/workspace";
import { useWorkspace } from "@/lib/workspace-context";
import { cn } from "@/lib/utils";

type NavTarget =
  "/w/$workspace/apps" | "/w/$workspace/tasks" | "/w/$workspace/storage" | "/w/$workspace/usage";

type NavItem = {
  label: string;
  segment: string;
  to: NavTarget;
  icon: typeof LayoutGrid;
};

const primaryNav: NavItem[] = [
  { label: "Apps", segment: "apps", to: "/w/$workspace/apps", icon: LayoutGrid },
  { label: "Tasks", segment: "tasks", to: "/w/$workspace/tasks", icon: Activity },
  { label: "Storage", segment: "storage", to: "/w/$workspace/storage", icon: Database },
];

/**
 * Usage sits with Settings rather than above, because it answers about the
 * account and not the workspace the rail is scoped to: the provider invoices an
 * account, so the figure is the same wherever the workspace switcher is left.
 */
const accountNav: NavItem[] = [
  { label: "Usage", segment: "usage", to: "/w/$workspace/usage", icon: ChartNoAxesCombined },
];

const GlobalSearch = lazy(() =>
  import("@/components/shared/AppShell/GlobalSearch").then((module) => ({
    default: module.GlobalSearch,
  })),
);

export function AppShell() {
  const { logout } = useSession();
  const { workspace } = useWorkspace();
  const routerState = useRouterState();
  const navigate = useNavigate();
  const settingsSearch = useSearch({ strict: false });
  const openSettingsView = settingsView(settingsSearch.settings);
  const setSettings = useCallback(
    (next: SettingsView | undefined) => {
      void navigate({
        to: ".",
        search: (previous: Record<string, unknown>) => ({ ...previous, settings: next }),
        replace: next !== undefined && openSettingsView !== undefined,
      });
    },
    [navigate, openSettingsView],
  );
  const [searchOpen, setSearchOpen] = useState(false);
  const searchReturnFocus = useRef<HTMLElement | null>(null);
  const path = routerState.location.pathname;
  const basePath = `/w/${encodeURIComponent(workspace.name)}`;
  const setSearchVisibility = useCallback((open: boolean) => {
    if (open) {
      const activeElement = document.activeElement;
      searchReturnFocus.current =
        activeElement instanceof HTMLElement && activeElement !== document.body
          ? activeElement
          : null;
      setSearchOpen(true);
      return;
    }
    setSearchOpen(false);
    const returnTarget = searchReturnFocus.current;
    searchReturnFocus.current = null;
    requestAnimationFrame(() => {
      (returnTarget ?? document.getElementById("workspace-content"))?.focus();
    });
  }, []);
  const openSearch = useCallback(() => setSearchVisibility(true), [setSearchVisibility]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      const target = event.target;
      const isEditable =
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        (target instanceof HTMLElement && target.isContentEditable);
      const commandSearch = (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k";
      const slashSearch = event.key === "/" && !isEditable;
      if (!commandSearch && !slashSearch) return;
      event.preventDefault();
      openSearch();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [openSearch]);

  return (
    <div className="flex h-dvh overflow-hidden bg-background text-foreground">
      <a
        href="#workspace-content"
        className="fixed left-3 top-3 z-[70] -translate-y-20 rounded-md bg-foreground px-3 py-2 text-sm font-semibold text-background transition-transform focus:translate-y-0"
      >
        Skip to content
      </a>

      <DesktopRail
        path={path}
        basePath={basePath}
        onOpenSearch={openSearch}
        onOpenSettings={() => setSettings("general")}
        settingsOpen={openSettingsView !== undefined}
        onLogout={logout}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <MobileHeader
          onOpenSearch={openSearch}
          onOpenSettings={() => setSettings("general")}
          onLogout={logout}
        />
        <main
          id="workspace-content"
          data-workspace-shell-main=""
          tabIndex={-1}
          className="min-h-0 flex-1 overflow-hidden bg-background outline-none"
        >
          <Outlet />
        </main>
        <MobileNavigation path={path} basePath={basePath} />
      </div>

      {searchOpen ? (
        <Suspense fallback={null}>
          <GlobalSearch open onOpenChange={setSearchVisibility} />
        </Suspense>
      ) : null}

      {openSettingsView ? (
        <SettingsDialog
          view={openSettingsView}
          onViewChange={setSettings}
          onClose={() => setSettings(undefined)}
        />
      ) : null}
    </div>
  );
}

function DesktopRail({
  path,
  basePath,
  onOpenSearch,
  onOpenSettings,
  settingsOpen,
  onLogout,
}: {
  path: string;
  basePath: string;
  onOpenSearch: () => void;
  onOpenSettings: () => void;
  settingsOpen: boolean;
  onLogout: () => void;
}) {
  const { workspace } = useWorkspace();

  return (
    <aside className="hidden w-56 shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground shadow-[inset_-1px_0_0_oklch(1_0_0/0.02)] lg:flex">
      <div className="flex h-14 shrink-0 items-center px-4">
        <Link
          to="/w/$workspace/apps"
          params={{ workspace: workspace.name }}
          className="flex items-center gap-2 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring"
        >
          <img src="/lazycloud.png" alt="" className="size-8" />
          <span className="text-xl font-bold text-brand">LazyCloud</span>
        </Link>
      </div>

      <div className="px-3 pb-3">
        <div className="flex items-center gap-1">
          <WorkspaceSwitcher className="min-w-0 flex-1" />
          <CreateWorkspaceControl />
        </div>
        <button
          type="button"
          onClick={onOpenSearch}
          className="mt-2 flex h-9 w-full items-center gap-2 rounded-md border border-input bg-background/45 px-2.5 text-left text-xs text-muted-foreground outline-none transition-colors hover:border-muted-foreground/40 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Search className="size-3.5" aria-hidden="true" />
          <span className="flex-1">Search</span>
          <kbd className="mono text-[10px] text-muted-foreground">⌘K</kbd>
        </button>
      </div>

      <nav aria-label="Main navigation" className="space-y-0.5 px-3">
        {primaryNav.map((item) => (
          <RailLink
            key={item.segment}
            item={item}
            active={navItemActive(path, basePath, item.segment)}
            workspaceName={workspace.name}
          />
        ))}
      </nav>

      <div className="mt-auto border-t border-sidebar-border px-3 py-3">
        <nav aria-label="Account navigation" className="space-y-0.5">
          <SettingsRailButton active={settingsOpen} onOpen={onOpenSettings} />
          {accountNav.map((item) => (
            <RailLink
              key={item.segment}
              item={item}
              active={navItemActive(path, basePath, item.segment)}
              workspaceName={workspace.name}
            />
          ))}
        </nav>
        <ThemeToggle className="mt-2" />
        <button
          type="button"
          onClick={onLogout}
          className="interactive-row mt-0.5 flex h-9 w-full items-center gap-2.5 rounded-md px-2.5 text-[13px] text-muted-foreground hover:text-foreground"
        >
          <LogOut className="size-4" aria-hidden="true" />
          Sign out
        </button>
      </div>
    </aside>
  );
}

function RailLink({
  item,
  active,
  workspaceName,
}: {
  item: NavItem;
  active: boolean;
  workspaceName: string;
}) {
  const Icon = item.icon;
  return (
    <Link
      to={item.to}
      params={{ workspace: workspaceName }}
      aria-current={active ? "page" : undefined}
      data-selected={active}
      className={cn(
        "flex h-9 items-center gap-2.5 rounded-none border-l-2 border-transparent bg-transparent px-2.5 text-[13px] outline-none transition-[border-color,color] duration-150 hover:text-sidebar-accent-foreground focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-sidebar-ring",
        active
          ? "font-medium text-sidebar-foreground"
          : "text-muted-foreground hover:text-sidebar-foreground",
        active && "border-l-brand",
      )}
    >
      <Icon className="size-4 shrink-0" aria-hidden="true" />
      {item.label}
    </Link>
  );
}

function MobileHeader({
  onOpenSearch,
  onOpenSettings,
  onLogout,
}: {
  onOpenSearch: () => void;
  onOpenSettings: () => void;
  onLogout: () => void;
}) {
  const { workspace } = useWorkspace();
  return (
    <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border bg-card/45 px-3 lg:hidden">
      <Link
        to="/w/$workspace/apps"
        params={{ workspace: workspace.name }}
        aria-label="Apps"
        className="flex size-8 shrink-0 items-center justify-center rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <img src="/lazycloud.png" alt="" className="size-8" />
      </Link>
      <WorkspaceSwitcher className="min-w-0 flex-1" compact />
      <Button
        variant="ghost"
        size="icon"
        onClick={onOpenSearch}
        aria-label="Search workspace"
        title="Search workspace"
      >
        <Search className="size-4" />
      </Button>
      <MobileMenu onLogout={onLogout} onOpenSettings={onOpenSettings} />
    </header>
  );
}

function MobileMenu({
  onLogout,
  onOpenSettings,
}: {
  onLogout: () => void;
  onOpenSettings: () => void;
}) {
  const [open, setOpen] = useState(false);
  const { workspace } = useWorkspace();
  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <Button
        variant="ghost"
        size="icon"
        onClick={() => setOpen(true)}
        aria-label="Open workspace menu"
        title="Workspace menu"
      >
        <Menu className="size-4" />
      </Button>
      <SheetContent aria-describedby={undefined} className="max-w-xs gap-0">
        <DrawerHeader>
          <SheetTitle>{workspace.name}</SheetTitle>
        </DrawerHeader>
        <nav aria-label="Account menu" className="p-3">
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onOpenSettings();
            }}
            className="interactive-row flex h-10 w-full items-center gap-3 rounded-md px-3 text-sm text-muted-foreground"
          >
            <Settings className="size-4" aria-hidden="true" />
            Settings
          </button>
          {accountNav.map((item) => {
            const Icon = item.icon;
            return (
              <Link
                key={item.segment}
                to={item.to}
                params={{ workspace: workspace.name }}
                onClick={() => setOpen(false)}
                className="interactive-row mt-0.5 flex h-10 w-full items-center gap-3 rounded-md px-3 text-sm text-muted-foreground"
              >
                <Icon className="size-4" aria-hidden="true" />
                {item.label}
              </Link>
            );
          })}
          <ThemeToggle className="mt-0.5 h-10 gap-3 px-3 text-sm" />
        </nav>
        <div className="mt-auto border-t border-border p-3">
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onLogout();
            }}
            className="interactive-row flex h-10 w-full items-center gap-3 rounded-md px-3 text-sm text-muted-foreground hover:text-foreground"
          >
            <LogOut className="size-4" aria-hidden="true" />
            Sign out
          </button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

/** Shell theme control: switches between the light default and dark mode. */
function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();
  const next = theme === "dark" ? "light" : "dark";
  const Icon = theme === "dark" ? Sun : Moon;
  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      aria-label={`Switch to ${next} mode`}
      title={`Switch to ${next} mode`}
      className={cn(
        "interactive-row flex h-9 w-full items-center gap-2.5 rounded-md px-2.5 text-[13px] text-muted-foreground hover:text-foreground",
        className,
      )}
    >
      <Icon className="size-4" aria-hidden="true" />
      {theme === "dark" ? "Light mode" : "Dark mode"}
    </button>
  );
}

function MobileNavigation({ path, basePath }: { path: string; basePath: string }) {
  const { workspace } = useWorkspace();
  return (
    <nav
      aria-label="Mobile navigation"
      className="grid h-14 shrink-0 grid-cols-3 border-t border-sidebar-border bg-sidebar/90 shadow-[0_-6px_24px_oklch(0_0_0/0.14)] lg:hidden"
    >
      {primaryNav.map((item) => {
        const Icon = item.icon;
        const active = navItemActive(path, basePath, item.segment);
        return (
          <Link
            key={item.segment}
            to={item.to}
            params={{ workspace: workspace.name }}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex min-w-0 flex-col items-center justify-center gap-1 text-[10px] outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
              active ? "text-foreground" : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Icon className={cn("size-4", active && "text-brand")} aria-hidden="true" />
            <span className="truncate">{item.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}

function CreateWorkspaceControl() {
  const { user } = useSession();
  const [open, setOpen] = useState(false);
  if (user.role !== "administrator") return null;
  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        aria-label="Create workspace"
        title="Create workspace"
        className="size-8 text-muted-foreground"
        onClick={() => setOpen(true)}
      >
        <Plus className="size-3.5" />
      </Button>
      {open ? <CreateWorkspaceSheet onClose={() => setOpen(false)} /> : null}
    </>
  );
}

function CreateWorkspaceSheet({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => createWorkspace(name.trim()),
    onSuccess: async (created) => {
      // The shell resolves a workspace out of the session, so the session has to
      // know about the new one before the route changes to it.
      await queryClient.invalidateQueries({ queryKey: currentSessionQueryOptions().queryKey });
      onClose();
      router.history.push(`/w/${encodeURIComponent(created.name)}/apps`);
    },
  });

  return (
    <Sheet open onOpenChange={(next) => (next ? undefined : onClose())}>
      <SheetContent aria-describedby={undefined} className="gap-0 sm:max-w-md">
        <DrawerHeader>
          <SheetTitle>Create workspace</SheetTitle>
        </DrawerHeader>
        <form
          className="flex flex-col gap-3 p-4"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <label className="block text-xs font-medium text-muted-foreground">
            Name
            <input
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="workspace-name"
              className="mono mt-1 h-9 w-full rounded-md border border-input bg-muted px-3 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </label>
          <div className="flex items-center gap-2">
            <Button type="submit" size="sm" disabled={create.isPending || !name.trim()}>
              {create.isPending ? <Loader2 className="size-3.5 animate-spin" /> : "Create"}
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={onClose}>
              Cancel
            </Button>
          </div>
          {create.isError ? (
            <p className="text-xs text-destructive">{create.error.message}</p>
          ) : null}
        </form>
      </SheetContent>
    </Sheet>
  );
}

function navItemActive(path: string, basePath: string, segment: string): boolean {
  const target = `${basePath}/${segment}`;
  return path === target || path.startsWith(`${target}/`);
}

function SettingsRailButton({ active, onOpen }: { active: boolean; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      data-selected={active}
      className={cn(
        "interactive-row flex h-9 w-full items-center gap-2.5 rounded-none border-l-2 border-transparent px-2.5 text-[13px] outline-none transition-[border-color,color] duration-150 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-sidebar-ring",
        active
          ? "border-l-brand font-medium text-sidebar-foreground"
          : "text-muted-foreground hover:text-sidebar-foreground",
      )}
    >
      <Settings className="size-4" aria-hidden="true" />
      Settings
    </button>
  );
}
