import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { Link, Outlet, useNavigate, useRouterState, useSearch } from "@tanstack/react-router";
import {
  Activity,
  ChartNoAxesCombined,
  Database,
  Gauge,
  LayoutGrid,
  LogOut,
  Menu,
  Search,
  Settings,
} from "lucide-react";

import { WorkspaceSwitcher } from "@/components/shared/AppShell/WorkspaceSwitcher";
import { useSession } from "@/components/shared/AuthGate/session";
import { DrawerHeader } from "@/components/shared/DrawerHeader";
import { AccountRail } from "@/components/shared/AppShell/AccountRail";
import { ThemeToggle } from "@/components/shared/AppShell/ThemeToggle";
import { SettingsDialog } from "@/components/shared/SettingsDialog";
import { settingsView, type SettingsView } from "@/components/shared/SettingsDialog/view";

import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
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

/* Loaded when the drawer is first opened: its charts pull recharts in, which
   nothing else in the shell needs to render navigation. */
const AccountMetricsDrawer = lazy(() =>
  import("@/components/shared/AppShell/AccountMetrics").then((module) => ({
    default: module.AccountMetricsDrawer,
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
      {/* Each row below reaches less far than the one above it: the product,
          then the workspace, then a search that only ever looks inside it, then
          that workspace's sections. The readings are the one control that spans
          workspaces, so they sit up here beside the mark rather than in the
          sequence, where they would read as scoped to whatever is selected. */}
      <div className="flex h-14 shrink-0 items-center justify-between gap-2 px-3">
        <Link
          to="/w/$workspace/apps"
          params={{ workspace: workspace.name }}
          className="flex min-w-0 items-center gap-2 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring"
        >
          <img src="/lazycloud.png" alt="" className="size-8 shrink-0" />
          <span className="truncate text-xl font-bold text-brand">LazyCloud</span>
        </Link>
        <AccountMetricsControl />
      </div>

      <div className="space-y-1.5 px-3 pb-3">
        <WorkspaceSwitcher className="w-full" />
        <button
          type="button"
          onClick={onOpenSearch}
          className="flex h-9 w-full min-w-0 items-center gap-2 rounded-md border border-input bg-background/45 px-2.5 text-left text-xs text-muted-foreground outline-none transition-colors hover:border-muted-foreground/40 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Search className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="flex-1 truncate">Search {workspace.name}</span>
          <kbd className="mono shrink-0 text-[10px] text-muted-foreground">⌘K</kbd>
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

      <AccountRail settingsOpen={settingsOpen} onOpenSettings={onOpenSettings} onLogout={onLogout}>
        {accountNav.map((item) => (
          <RailLink
            key={item.segment}
            item={item}
            active={navItemActive(path, basePath, item.segment)}
            workspaceName={workspace.name}
          />
        ))}
      </AccountRail>
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
      <AccountMetricsControl />
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

/**
 * Opens the account's readings beside whatever the rail is currently showing.
 *
 * A gauge rather than a chart glyph: the rail already spends one on Usage, and a
 * second beside it would read as a second page of charts. What this opens leads
 * with instruments — a live count, a figure against the ceiling it is refused
 * at — which is what a dial says and a bar chart does not.
 */
function AccountMetricsControl() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        aria-label="Account metrics"
        title="Account metrics"
        className="size-8 text-muted-foreground"
        onClick={() => setOpen(true)}
      >
        <Gauge className="size-3.5" />
      </Button>
      {open ? (
        <Suspense fallback={null}>
          <AccountMetricsDrawer onClose={() => setOpen(false)} />
        </Suspense>
      ) : null}
    </>
  );
}

function navItemActive(path: string, basePath: string, segment: string): boolean {
  const target = `${basePath}/${segment}`;
  return path === target || path.startsWith(`${target}/`);
}
